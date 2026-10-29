# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Raw materials reserved against the finished good's Sales Order row.

A Sales Order carries only finished-good rows, e.g. one earring. Making that earring takes gold,
diamonds, gemstones, findings, ... and each of them is reserved against the earring's row:
voucher_type "Sales Order", voucher_detail_no = the finished-good row, item_code = the raw
material, manufacturing_work_order set. None of those items is a row of the Sales Order.

ERPNext 16.52 (frappe/erpnext#60428) refuses a Sales Order reservation whose item is not the row's
own item ("Item … is not a row of Sales Order …").
CustomStockReservationEntry.is_row_of_voucher lets a work-order reservation through when its row
belongs to that Sales Order.

Every reservation below is a real Stock Reservation Entry that goes through ERPNext's own
validate()/submit(), so the tests exercise the real 16.52 check rather than a stand-in. Fixtures
are created after super().setUpClass(), so the class-level rollback removes them, and each test
runs in its own savepoint so one test's reservations never count against another's.

Run: bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_sre_raw_material_against_fg_row
"""

from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

import frappe
from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	StockReservationEntry,
	get_sre_reserved_qty_for_voucher_detail_no,
)
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, today

from jewellery_erpnext.jewellery_erpnext.customization.stock_reservation_entry.stock_reservation_entry import (
	CustomStockReservationEntry,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
	stock_reservation_entry_for_mwo,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry import (
	CHILD_TABLE_EMPLOYEE,
	_reduce_sre,
	_restore_reduced_sres,
)

_STOCK_ENTRY_MODULE = "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry"
_SERVER_SCRIPT_MAP = (
	"frappe.core.doctype.server_script.server_script_utils.get_server_script_map"
)
_SAVEPOINT = "sre_raw_material_test"
_REAL_GET_CACHED_VALUE = frappe.get_cached_value

PREFIX = "_Test SRE"
MWO = f"{PREFIX} MWO-0001"
MOP = f"{PREFIX} MOP-0001"
NOT_A_ROW = "is not a row of"

# ERPNext 16.52 introduced the row check. On older versions nothing refuses a reservation, so the
# refusal and control tests have nothing to observe there.
HAS_ROW_CHECK = hasattr(StockReservationEntry, "validate_voucher")


class TestRawMaterialReservedAgainstFinishedGoodRow(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		# super() keeps IntegrationTestCase's class-level rollback, which most classes in this app
		# skip: every fixture below is undone when the class finishes, even on a site with real data.
		super().setUpClass()
		cls.addClassCleanup(frappe.clear_cache)

		cls.company = _company()
		cls.customer = (
			frappe.db.get_value("Customer", {}, "name") or f"{PREFIX} Customer"
		)
		cls.dept = _warehouse("Dept", cls.company)
		cls.mop = _warehouse("MOP", cls.company)

		for uom in ("Nos", "Gram", "Carat"):
			_uom(uom)
		finished_goods = _item_group("Finished Goods")
		raw_material = _item_group("Raw Material")

		cls.earring = _item("Earring", finished_goods, "Nos")
		cls.ring = _item("Ring", finished_goods, "Nos")
		cls.gold = _item("Gold 22KT", raw_material, "Gram", has_batch_no=1)
		cls.silver = _item("Silver 92.5", raw_material, "Gram", has_batch_no=1)
		cls.diamond = _item("Diamond Round", raw_material, "Carat", has_batch_no=1)
		cls.gemstone = _item("Ruby", raw_material, "Carat", has_batch_no=1)
		cls.finding = _item("Finding Screw", raw_material, "Gram", has_batch_no=1)
		cls.other = _item("Other Alloy", raw_material, "Gram")

		cls.so = _sales_order("A", [cls.earring, cls.ring], cls.company, cls.customer)
		cls.earring_row, cls.ring_row = cls.so.items
		cls.other_so = _sales_order("B", [cls.earring], cls.company, cls.customer)
		cls.draft_so = _sales_order(
			"Draft", [cls.earring], cls.company, cls.customer, docstatus=0
		)
		cls.cancelled_so = _sales_order(
			"Cancelled", [cls.earring], cls.company, cls.customer, docstatus=2
		)

	def setUp(self):
		super().setUp()
		frappe.db.savepoint(_SAVEPOINT)
		self.addCleanup(frappe.db.rollback, save_point=_SAVEPOINT)

	def _reserve(
		self,
		item_code,
		row,
		qty,
		warehouse=None,
		*,
		voucher_no=None,
		mwo=MWO,
		mop=MOP,
		batch_no=None,
		voucher_qty=None,
		submit=False,
	):
		"""A reservation with the fields stock_reservation_entry_for_mwo gives one."""
		warehouse = warehouse or self.dept
		has_batch_no = frappe.get_cached_value("Item", item_code, "has_batch_no")
		sre = frappe.new_doc("Stock Reservation Entry")
		sre.update(
			{
				"voucher_type": "Sales Order",
				"voucher_no": voucher_no or (row.parent if row else self.so.name),
				"voucher_detail_no": row.name if row else None,
				"item_code": item_code,
				"warehouse": warehouse,
				"company": self.company,
				"stock_uom": frappe.get_cached_value("Item", item_code, "stock_uom"),
				"reserved_qty": qty,
				"voucher_qty": qty if voucher_qty is None else voucher_qty,
				"available_qty": qty,
				"has_batch_no": has_batch_no,
				"has_serial_no": 0,
				"manufacturing_work_order": mwo,
				"manufacturing_operation": mop,
				"reservation_based_on": "Serial and Batch"
				if has_batch_no and batch_no
				else "Qty",
			}
		)
		if has_batch_no and batch_no:
			sre.append(
				"sb_entries", {"batch_no": batch_no, "warehouse": warehouse, "qty": qty}
			)
		sre.insert(ignore_links=1)
		if submit:
			sre.submit()
		return sre

	def _stock(
		self, item_code, qty, warehouse=None, batch_id=None, manufacturing_date=None
	):
		"""Put qty of item_code in warehouse, under batch_id when the item is batch-tracked."""
		if batch_id:
			_batch(item_code, batch_id, manufacturing_date)
		_receive(self.company, warehouse or self.dept, item_code, qty, batch_id)
		return batch_id

	# ------------------------------------------------------------------
	# Raw materials on the finished good's row -- accepted
	# ------------------------------------------------------------------

	def test_earring_row_takes_every_raw_material_type(self):
		"""S1: gold, diamond, gemstone, finding and an "other" item, each reserved against the
		earring row. None of them is a row of the Sales Order."""
		for item_code in (
			self.gold,
			self.diamond,
			self.gemstone,
			self.finding,
			self.other,
		):
			with self.subTest(item=item_code):
				sre = self._reserve(item_code, self.earring_row, 1)

				self.assertIsInstance(sre, CustomStockReservationEntry)
				self.assertTrue(frappe.db.exists("Stock Reservation Entry", sre.name))
				self.assertEqual(sre.voucher_detail_no, self.earring_row.name)
				self.assertNotEqual(sre.item_code, self.earring_row.item_code)

	def test_a_non_gold_metal_is_reserved_like_gold(self):
		"""S2: the rule doesn't depend on which raw material it is."""
		sre = self._reserve(self.silver, self.earring_row, 2.5)

		self.assertTrue(frappe.db.exists("Stock Reservation Entry", sre.name))

	def test_each_finished_good_row_reserves_its_own_raw_materials(self):
		"""S3: one order, an earring row and a ring row. The same gold item is reserved on both,
		and each row only counts its own reservation."""
		batch = self._stock(self.gold, 10, batch_id=f"{PREFIX}-S3-GOLD")
		self._reserve(self.gold, self.earring_row, 3, batch_no=batch, submit=True)
		self._reserve(self.gold, self.ring_row, 4, batch_no=batch, submit=True)

		self.assertEqual(self._row_reserved(self.gold, self.earring_row), 3)
		self.assertEqual(self._row_reserved(self.gold, self.ring_row), 4)

	def test_one_row_holds_the_same_gold_in_two_warehouses(self):
		"""S4: the earring's gold sits in two warehouses. ERPNext sums a Sales Order row's
		reservations across warehouses, so the second one carries a voucher_qty that covers both,
		the way stock_reservation_entry_for_mwo sets it."""
		dept_batch = self._stock(self.gold, 6, self.dept, f"{PREFIX}-S4-DEPT")
		mop_batch = self._stock(self.gold, 4, self.mop, f"{PREFIX}-S4-MOP")
		self._reserve(
			self.gold, self.earring_row, 6, self.dept, batch_no=dept_batch, submit=True
		)
		second = self._reserve(
			self.gold,
			self.earring_row,
			4,
			self.mop,
			batch_no=mop_batch,
			voucher_qty=self._row_reserved(self.gold, self.earring_row) + 4,
			submit=True,
		)

		self.assertEqual(second.docstatus, 1)
		self.assertEqual(self._row_reserved(self.gold, self.earring_row), 10)

	def test_the_picked_diamond_batch_is_kept_on_submit(self):
		"""S5: the operator picked the newer diamond batch. ERPNext's FIFO auto-pick would swap in
		the older one; the work-order reservation keeps the batch it was given."""
		self._stock(
			self.diamond,
			1,
			batch_id=f"{PREFIX}-S5-OLD",
			manufacturing_date=add_days(today(), -10),
		)
		picked = self._stock(self.diamond, 1, batch_id=f"{PREFIX}-S5-NEW")

		sre = self._reserve(
			self.diamond, self.earring_row, 0.5, batch_no=picked, submit=True
		)

		self.assertEqual([sb.batch_no for sb in sre.sb_entries], [picked])

	def test_work_order_transfer_reserves_only_the_stock_on_hand(self):
		"""S6: the transfer row asks for 8 g but only 5 g arrived; the reservation is for 5 g."""
		batch = self._stock(self.gold, 5, self.mop, f"{PREFIX}-S6-GOLD")

		created = self._reserve_through_work_order_transfer(
			"S6", [frappe._dict(item_code=self.gold, qty=8, uom="Gram", batch_no=batch)]
		)

		self.assertEqual(len(created), 1)
		self.assertEqual(flt(created[0].reserved_qty, 3), 5)

	def test_the_finished_good_itself_is_still_reservable_on_its_row(self):
		"""S7: the earring reserved against its own row passes, with or without a work order."""
		for mwo in (MWO, None):
			with self.subTest(mwo=mwo):
				sre = self._reserve(
					self.earring, self.earring_row, 1, mwo=mwo, mop=mwo and MOP
				)

				self.assertTrue(frappe.db.exists("Stock Reservation Entry", sre.name))

	def test_employee_ir_loss_rebuilds_and_restores_the_gold_reservation(self):
		"""S8: an Employee IR books 0.185 g of gold loss. _reduce_sre cancels the gold reservation
		and rebuilds it with less qty on the same earring row; cancelling the Employee IR puts the
		original qty back. This is the path that failed on ERPNext 16.52."""
		batch = self._stock(self.gold, 11.56, batch_id=f"{PREFIX}-S8-GOLD")
		sre = self._reserve(
			self.gold, self.earring_row, 11.56, batch_no=batch, submit=True
		)
		eir = frappe._dict(name=f"{PREFIX} EIR-0001")

		_reduce_sre(
			eir,
			frappe._dict(idx=1, batch_no=batch),
			frappe.get_doc("Stock Reservation Entry", sre.name),
			0.185,
			CHILD_TABLE_EMPLOYEE,
		)

		self.assertEqual(
			frappe.db.get_value("Stock Reservation Entry", sre.name, "docstatus"), 2
		)
		reduced = frappe.get_all(
			"Stock Reservation Entry",
			{"employee_ir": eir.name, "docstatus": 1},
			[
				"name",
				"item_code",
				"voucher_no",
				"voucher_detail_no",
				"reserved_qty",
				"manufacturing_work_order",
			],
		)
		self.assertEqual(len(reduced), 1)
		self.assertEqual(flt(reduced[0].reserved_qty, 3), 11.375)
		self.assertEqual(reduced[0].item_code, self.gold)
		self.assertEqual(reduced[0].voucher_no, self.so.name)
		self.assertEqual(reduced[0].voucher_detail_no, self.earring_row.name)
		self.assertEqual(reduced[0].manufacturing_work_order, MWO)

		_restore_reduced_sres(eir)

		live = frappe.get_all(
			"Stock Reservation Entry",
			{"manufacturing_work_order": MWO, "item_code": self.gold, "docstatus": 1},
			["reserved_qty", "voucher_detail_no", "employee_ir"],
		)
		self.assertEqual(len(live), 1)
		self.assertEqual(flt(live[0].reserved_qty, 3), 11.56)
		self.assertEqual(live[0].voucher_detail_no, self.earring_row.name)
		self.assertFalse(live[0].employee_ir)

	def test_work_order_transfer_reserves_raw_materials_on_the_finished_good_row(self):
		"""S9: a work-order transfer (what a Material Request's Transfer to MOP books) reserves its
		gold and diamond against the earring row. This is the path that failed on ERPNext 16.52."""
		gold_batch = self._stock(self.gold, 2, self.mop, f"{PREFIX}-S9-GOLD")
		diamond_batch = self._stock(self.diamond, 0.5, self.mop, f"{PREFIX}-S9-DIAMOND")

		created = self._reserve_through_work_order_transfer(
			"S9",
			[
				frappe._dict(
					item_code=self.gold, qty=2, uom="Gram", batch_no=gold_batch
				),
				frappe._dict(
					item_code=self.diamond, qty=0.5, uom="Carat", batch_no=diamond_batch
				),
			],
		)

		self.assertEqual(
			{row.item_code: flt(row.reserved_qty, 3) for row in created},
			{self.gold: 2, self.diamond: 0.5},
		)
		for row in created:
			with self.subTest(item=row.item_code):
				self.assertEqual(row.voucher_type, "Sales Order")
				self.assertEqual(row.voucher_no, self.so.name)
				self.assertEqual(row.voucher_detail_no, self.earring_row.name)
				self.assertEqual(row.warehouse, self.mop)
				self.assertEqual(row.manufacturing_operation, MOP)
				self.assertEqual(row.reservation_based_on, "Serial and Batch")

	def test_row_and_bin_reserved_quantities_are_erpnexts_own_sums(self):
		"""S10: what the fix leaves alone. ERPNext adds every reservation on a row into the row's
		stock_reserved_qty, whatever the item, and keeps the Bin per item and warehouse."""
		gold_batch = self._stock(self.gold, 2, batch_id=f"{PREFIX}-S10-GOLD")
		diamond_batch = self._stock(self.diamond, 0.5, batch_id=f"{PREFIX}-S10-DIAMOND")
		self._reserve(self.gold, self.earring_row, 2, batch_no=gold_batch, submit=True)
		self._reserve(
			self.diamond, self.earring_row, 0.5, batch_no=diamond_batch, submit=True
		)

		self.assertEqual(
			flt(
				frappe.db.get_value(
					"Sales Order Item", self.earring_row.name, "stock_reserved_qty"
				),
				3,
			),
			2.5,
		)
		self.assertEqual(self._bin_reserved(self.gold), 2)
		self.assertEqual(self._bin_reserved(self.diamond), 0.5)

	# ------------------------------------------------------------------
	# Still refused
	# ------------------------------------------------------------------

	@skipUnless(HAS_ROW_CHECK, "needs ERPNext 16.52+")
	def test_a_row_of_another_sales_order_is_refused(self):
		"""N1: the row belongs to a different Sales Order."""
		with self.assertRaisesRegex(frappe.ValidationError, NOT_A_ROW):
			self._reserve(self.gold, self.other_so.items[0], 1, voucher_no=self.so.name)

	@skipUnless(HAS_ROW_CHECK, "needs ERPNext 16.52+")
	def test_a_row_that_does_not_exist_is_refused(self):
		"""N2: the row name is not on any Sales Order."""
		missing = frappe._dict(name=f"{PREFIX} no such row", parent=self.so.name)

		with self.assertRaisesRegex(frappe.ValidationError, NOT_A_ROW):
			self._reserve(self.gold, missing, 1)

	@skipUnless(HAS_ROW_CHECK, "needs ERPNext 16.52+")
	def test_a_raw_material_without_a_work_order_is_refused(self):
		"""N3: without a work order the reservation is not a manufacturing one, so ERPNext's own
		rule applies and a raw material can't name the earring row."""
		with self.assertRaisesRegex(frappe.ValidationError, NOT_A_ROW):
			self._reserve(self.gold, self.earring_row, 1, mwo=None, mop=None)

	@skipUnless(HAS_ROW_CHECK, "needs ERPNext 16.52+")
	def test_a_reservation_without_a_row_is_refused(self):
		"""N4: no Sales Order row at all."""
		with self.assertRaisesRegex(frappe.ValidationError, "is required"):
			self._reserve(self.gold, None, 1)

	@skipUnless(HAS_ROW_CHECK, "needs ERPNext 16.52+")
	def test_a_draft_or_cancelled_sales_order_is_refused(self):
		"""N5: only a submitted Sales Order can hold reservations."""
		for so in (self.draft_so, self.cancelled_so):
			with (
				self.subTest(docstatus=so.docstatus),
				self.assertRaisesRegex(frappe.ValidationError, "must be submitted"),
			):
				self._reserve(self.gold, so.items[0], 1)

	@skipUnless(HAS_ROW_CHECK, "needs ERPNext 16.52+")
	def test_reservations_need_stock_reservation_enabled(self):
		"""N6: Stock Settings' switch still applies to work-order reservations."""
		enabled = frappe.db.get_single_value(
			"Stock Settings", "enable_stock_reservation"
		)
		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 0)
		try:
			with self.assertRaisesRegex(frappe.ValidationError, "Please enable"):
				self._reserve(self.gold, self.earring_row, 1)
		finally:
			# Restore through set_single_value: it also clears the cached Stock Settings, which the
			# savepoint rollback alone would leave holding 0 for every later test.
			frappe.db.set_single_value(
				"Stock Settings", "enable_stock_reservation", enabled
			)

	def test_more_than_the_stock_on_hand_is_refused(self):
		"""N7: quantity limits are untouched by the fix: 5 g on hand can't hold an 8 g reservation."""
		self._stock(self.other, 5)

		with self.assertRaisesRegex(
			frappe.ValidationError, "Cannot reserve more than Allowed Qty"
		):
			self._reserve(self.other, self.earring_row, 8, voucher_qty=8, submit=True)

	def test_a_second_warehouse_beyond_the_rows_voucher_qty_is_refused(self):
		"""N7: the row's voucher_qty is 8 g; 6 g is already reserved in one warehouse, so another
		4 g in a second warehouse is refused."""
		dept_batch = self._stock(self.gold, 6, self.dept, f"{PREFIX}-N7-DEPT")
		mop_batch = self._stock(self.gold, 4, self.mop, f"{PREFIX}-N7-MOP")
		self._reserve(
			self.gold, self.earring_row, 6, self.dept, batch_no=dept_batch, submit=True
		)

		with self.assertRaisesRegex(
			frappe.ValidationError, "Cannot reserve more than Allowed Qty"
		):
			self._reserve(
				self.gold,
				self.earring_row,
				4,
				self.mop,
				batch_no=mop_batch,
				voucher_qty=8,
				submit=True,
			)

	@skipUnless(HAS_ROW_CHECK, "needs ERPNext 16.52+")
	def test_erpnext_alone_refuses_raw_material_on_the_finished_good_row(self):
		"""C1: control. With the override swapped back to ERPNext's own method, the earring's gold
		is refused with the production error, so the tests above would catch the bug."""
		with (
			patch.object(
				CustomStockReservationEntry,
				"is_row_of_voucher",
				StockReservationEntry.is_row_of_voucher,
			),
			self.assertRaisesRegex(frappe.ValidationError, NOT_A_ROW),
		):
			self._reserve(self.gold, self.earring_row, 1)

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------

	def _row_reserved(self, item_code, row):
		return flt(
			get_sre_reserved_qty_for_voucher_detail_no(
				item_code, "Sales Order", row.parent, row.name
			),
			3,
		)

	def _bin_reserved(self, item_code, warehouse=None):
		return flt(
			frappe.db.get_value(
				"Bin",
				{"item_code": item_code, "warehouse": warehouse or self.dept},
				"reserved_stock",
			),
			3,
		)

	def _reserve_through_work_order_transfer(self, label, rows):
		"""Run the real stock_reservation_entry_for_mwo for a work-order transfer into the MOP
		warehouse. Only the Parent Manufacturing Order lookup (which Sales Order row the work order
		makes) and the MOP Log are stubbed; the reservations themselves are real."""
		mwo = f"{PREFIX} MWO-{label}"
		pmo = f"{PREFIX} PMO-{label}"
		# Not a frappe._dict: on a dict, `.items` is the dict method, not the rows. The names are
		# where a reservation records the Stock Entry row it came from (set_sre_provenance).
		stock_entry = SimpleNamespace(
			name=f"{PREFIX} STE-{label}",
			manufacturing_order=pmo,
			manufacturing_work_order=mwo,
			manufacturer=None,
			company=self.company,
			stock_entry_type="Material Transfer (WORK ORDER)",
			employee_ir=None,
			items=[
				frappe._dict(
					row,
					name=f"{PREFIX} STE-{label}-{idx}",
					s_warehouse=None,
					t_warehouse=self.mop,
					manufacturing_operation=MOP,
				)
				for idx, row in enumerate(rows, start=1)
			],
		)

		def _cached(doctype, name, *args, **kwargs):
			if doctype == "Parent Manufacturing Order" and name == pmo:
				return (self.so.name, self.earring_row.name, None)
			return _REAL_GET_CACHED_VALUE(doctype, name, *args, **kwargs)

		with (
			patch(
				f"{_STOCK_ENTRY_MODULE}.frappe.get_cached_value", side_effect=_cached
			),
			patch(f"{_STOCK_ENTRY_MODULE}.create_mop_log"),
		):
			stock_reservation_entry_for_mwo(stock_entry)

		return frappe.get_all(
			"Stock Reservation Entry",
			{"manufacturing_work_order": mwo, "docstatus": 1},
			[
				"item_code",
				"reserved_qty",
				"voucher_type",
				"voucher_no",
				"voucher_detail_no",
				"warehouse",
				"manufacturing_operation",
				"reservation_based_on",
			],
		)


def _company():
	return frappe.db.get_single_value(
		"Global Defaults", "default_company"
	) or frappe.db.get_value("Company", {}, "name", order_by="creation asc")


def _uom(name):
	if not frappe.db.exists("UOM", name):
		frappe.get_doc(
			{"doctype": "UOM", "uom_name": name, "must_be_whole_number": 0}
		).insert()


def _warehouse(label, company):
	"""A plain warehouse with no department, employee or type, so it starts empty and passes
	doc_events.warehouse.validate."""
	abbr = frappe.get_cached_value("Company", company, "abbr")
	name = f"{PREFIX} {label} - {abbr}"
	if not frappe.db.exists("Warehouse", name):
		frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": f"{PREFIX} {label}",
				"company": company,
				"parent_warehouse": frappe.db.get_value(
					"Warehouse",
					{
						"company": company,
						"is_group": 1,
						"parent_warehouse": ("is", "not set"),
					},
				),
			}
		).insert()
	return name


def _item_group(label):
	"""Own item groups: the "- V" groups this app uses need batch abbreviations on every
	attribute (doc_events.item.before_insert)."""
	name = f"{PREFIX} {label}"
	if not frappe.db.exists("Item Group", name):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": name,
				"parent_item_group": "All Item Groups",
				"is_group": 0,
			}
		).insert()
	return name


def _item(label, item_group, stock_uom, has_batch_no=0):
	item_code = f"{PREFIX} {label}"
	if not frappe.db.exists("Item", item_code):
		# Server Scripts are a site's own customisations, not this app's. A site that has some on
		# Item but runs with server scripts switched off refuses every Item save, so the fixture
		# items are created without them.
		with patch(_SERVER_SCRIPT_MAP, return_value={}):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": item_code,
					"item_name": item_code,
					"item_group": item_group,
					"stock_uom": stock_uom,
					"is_stock_item": 1,
					"has_batch_no": has_batch_no,
					# Not a sales item: India Compliance would want an HSN code, and the
					# reservation rule never looks at this flag.
					"is_sales_item": 0,
				}
			).insert()
	return item_code


def _batch(item_code, batch_id, manufacturing_date=None):
	frappe.get_doc(
		{
			"doctype": "Batch",
			"item": item_code,
			"batch_id": batch_id,
			"manufacturing_date": manufacturing_date or today(),
		}
	).insert()


def _receive(company, warehouse, item_code, qty, batch_no=None):
	"""Material Receipt, following refining tests' gold_receipt."""
	se = frappe.new_doc("Stock Entry")
	se.company = company
	se.stock_entry_type = "Material Receipt"
	se.manufacturer = frappe.db.get_value("Manufacturer", {}, "name")
	se.branch = frappe.db.get_value("Branch", {}, "name")
	se.append(
		"items",
		{
			"t_warehouse": warehouse,
			"item_code": item_code,
			"qty": qty,
			"basic_rate": 100,
			"pcs": "1",
			"use_serial_batch_fields": 1 if batch_no else 0,
			"batch_no": batch_no,
		},
	)
	se.insert()
	se.submit()
	return se


def _sales_order(label, item_codes, company, customer, docstatus=1):
	"""A Sales Order of finished-good rows only, written straight to the database.

	The reservation rule reads two things from the order: whether it is submitted, and which rows
	it has. The order's own lifecycle (pricing, taxes, this app's BOM and serial hooks) plays no
	part in it, so the order isn't run through it; the full Order Form → Sales Order → work order
	chain is covered end to end by the manufacturing tests.
	"""
	so = frappe.new_doc("Sales Order")
	so.update(
		{
			"name": f"{PREFIX} SO {label}",
			"company": company,
			"customer": customer,
			"transaction_date": today(),
			"delivery_date": add_days(today(), 30),
			"docstatus": docstatus,
		}
	)
	for item_code in item_codes:
		so.append(
			"items",
			{
				"item_code": item_code,
				"item_name": item_code,
				"qty": 1,
				"stock_qty": 1,
				"uom": "Nos",
				"stock_uom": "Nos",
				"conversion_factor": 1,
				"delivery_date": so.delivery_date,
			},
		)
	for row in so.items:
		row.name = frappe.generate_hash(length=10)
		row.parent = so.name
		row.docstatus = docstatus
	so.set_user_and_timestamp()
	so.db_insert()
	for row in so.items:
		row.db_insert()
	return so
