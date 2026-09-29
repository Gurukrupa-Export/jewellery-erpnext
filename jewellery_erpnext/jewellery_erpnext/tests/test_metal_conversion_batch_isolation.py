# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Metal Conversions on real documents: every customer source batch converts on its own.

MCON00332 (kg-gk, 29 Sep 2026, single-converter mode) FIFO-drew customer GJCU0009's batch 11
(0.477 g), a Regular batch (0.850 g) and the same customer's batch 12 (8.673 g), plus 0.899 g
of company alloy, into 22KT. MAT-STE-19453 posted ONE customer target for both customer
batches, named after batch 11. This suite submits real Metal Conversions documents -- the
controller, the generated Stock Entry, its bundles, the Stock Ledger, GL, Batch lineage and the
Customer Gold ledger -- and reads every layer back.

WHERE IT RUNS
-------------
Only on a site that sets ``customer_gold_disposable_site`` (``cg-integration.test``), for the
reasons ``test_customer_gold_integration`` gives: it writes Singles, masters and stock, and
``IntegrationTestCase`` rolls back once per CLASS. Every test takes a fresh warehouse, so one
test's FIFO draw can never pick up another test's batches.

It runs at the site's own float precision -- 2 here, as on gk; 3 on kg-gk. A conversion posts at
Stock Entry Detail ``transfer_qty``'s three decimals either way (``metal_conversions._qty_precision``).
The suite used to force 3: the builder rounded at the document's float precision and, at 2,
booked batch 11's 0.477 g as 0.48 g and overdrew it.

HOW THE EXPECTATIONS ARE MADE
-----------------------------
Quantities are written out by hand (10 g at 100% -> 91.75% is 10.899182561 g; per batch
0.477 -> 0.520, 0.850 -> 0.926, 8.673 -> 9.453). Values are computed with ``Decimal`` from the
rates the receipts actually POSTED, read from their Stock Ledger Entries -- the site's own
valuation, as the brief requires -- never from the code under test. Customer gold is received
under the Nominal policy, so it carries value; batch 12 is received at a different gold rate
from batch 11, so a copied or pooled rate cannot pass.
"""

import time
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe.utils import flt, nowdate

from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	SETTINGS_DOCTYPE,
)
from jewellery_erpnext.customer_subcontracting.report.subcontracting_report.subcontracting_report import (
	get_linked_batches,
)

from . import customer_gold_purity_fixtures as cg_purity
from .test_customer_gold_integration import (
	COMPANY,
	CUSTOMER,
	MANUFACTURER,
	OTHER_CUSTOMER,
	RAW_RATE,
	_CustomerGoldIntegrationCase,
)

MC_SE_TYPE = "Repack-Metal Conversion"
COMPANY_RECEIPT_TYPE = "CG Test Company Receipt"

#: kg-gk's '99.9' purity master reads 100.0, which is why 10 g made 10.899182561 g of 22KT.
#: The fixture states that effective purity explicitly instead of borrowing the live master.
SOURCE_PURITY = "CG-TEST-100.0"
SOURCE_ITEM = "CG-TEST-GOLD-100.0"
TARGET_PURITY = "CG-TEST-91.75"
TARGET_ITEM = "CG-TEST-GOLD-91.75"
ALLOY_ITEM = "CG-TEST-MC-ALLOY"
ALLOY_HSN = "74031900"

ALLOY_RATE = Decimal("62")
REGULAR_RATE = Decimal("7350.25")
#: Batch 11 is received at the suite's gold rate, batch 12 at this one (per 10 g). Both sit
#: well inside the receipt's 0.5x-2x outlier band.
RAW_RATE_12 = 70936.20

GRAM = Decimal("0.001")
PAISA = Decimal("0.01")


def _d(value):
	return Decimal(str(flt(value, 9)))


class _MetalConversionCase(_CustomerGoldIntegrationCase):
	"""Masters, stock and a real Metal Conversions document, under the Nominal policy."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._ensure_conversion_masters()
		settings = frappe.get_doc(SETTINGS_DOCTYPE)
		settings.customer_gold_valuation_policy = "Nominal"
		# The receipt restates the day's per-gram quote by the row's purity, which it parses
		# from the Metal Purity attribute's NAME; the isolated ``CG-TEST-`` names do not parse.
		# So the conversion's source item is made the quoted item itself -- as the 24KT item
		# is on kg-gk -- and is booked at the quote unchanged.
		settings.customer_24kt_item = SOURCE_ITEM
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype=SETTINGS_DOCTYPE)
		cls._warehouses = 0

	# ------------------------------------------------------------------ masters
	@classmethod
	def _ensure_conversion_masters(cls):
		cls._ensure_purity(TARGET_PURITY, 91.75)
		cls.source_item = cls._ensure_variant(SOURCE_ITEM, SOURCE_PURITY)
		cls.target_item = cls._ensure_variant(TARGET_ITEM, TARGET_PURITY)
		cls._ensure_alloy()
		for name, purpose in (
			(MC_SE_TYPE, "Repack"),
			(COMPANY_RECEIPT_TYPE, "Material Receipt"),
		):
			if not frappe.db.exists("Stock Entry Type", name):
				doc = frappe.new_doc("Stock Entry Type")
				doc.name = name
				doc.purpose = purpose
				doc.insert(ignore_permissions=True)
		cls.mc_department = cls._ensure_department("CG Test Metal Conversion")
		cls.employee = cls._ensure_employee()

	@classmethod
	def _ensure_purity(cls, value, purity):
		"""An isolated ``CG-TEST-`` purity, plus its place on the Metal Purity attribute."""
		assert value.startswith(cg_purity.FIXTURE_PREFIX), value
		if not frappe.db.exists("Attribute Value", value):
			frappe.get_doc(
				{
					"doctype": "Attribute Value",
					"attribute_value": value,
					"purity_percentage": purity,
				}
			).insert(ignore_permissions=True)
		attribute = frappe.get_doc("Item Attribute", cg_purity.METAL_PURITY_ATTRIBUTE)
		if value not in {
			row.attribute_value for row in attribute.item_attribute_values
		}:
			attribute.append(
				"item_attribute_values", {"attribute_value": value, "abbr": value}
			)
			attribute.save(ignore_permissions=True)

	@classmethod
	def _ensure_alloy(cls):
		if frappe.db.exists("Item", ALLOY_ITEM):
			return
		item = frappe.new_doc("Item")
		item.item_code = ALLOY_ITEM
		item.item_name = "CG Test Conversion Alloy"
		item.item_group = cls.item_group
		item.stock_uom = "Gram"
		item.is_stock_item = 1
		item.has_batch_no = 1
		item.create_new_batch = 1
		item.gst_hsn_code = ALLOY_HSN
		item.insert(ignore_permissions=True)

	@classmethod
	def _ensure_employee(cls):
		existing = frappe.db.get_value(
			"Employee", {"first_name": "CG Test Converter", "company": COMPANY}, "name"
		)
		if existing:
			return existing
		employee = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "CG Test Converter",
				"gender": "Male",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"company": COMPANY,
				"status": "Active",
			}
		)
		employee.flags.ignore_mandatory = True
		employee.insert(ignore_permissions=True)
		return employee.name

	# -------------------------------------------------------------------- stock
	@classmethod
	def _fresh_warehouse(cls):
		cls._warehouses += 1
		return cls._ensure_warehouse(f"CG MC {cls.__name__[:24]} {cls._warehouses}")

	@classmethod
	def _customer_batch(cls, warehouse, qty, customer=CUSTOMER, raw_rate=RAW_RATE):
		"""Receive customer gold into ``warehouse``; return ``(batch, posted rate per g)``.

		Receipts cannot be backdated, and the rate is today's Gold Rates row -- so a
		different rate for a later batch is a different rate on today's row.
		"""
		cls._ensure_gold_rate(nowdate(), raw_rate)
		se = cls._receipt(
			cls,
			qty=qty,
			customer=customer,
			item_code=SOURCE_ITEM,
			t_warehouse=warehouse,
		)
		cls._submit(cls, se)
		batch = se.items[0].batch_no
		assert frappe.db.get_value("Batch", batch, "custom_customer") == customer
		return batch, cls._posted_rate(se.name)

	@classmethod
	def _company_batch(cls, warehouse, item_code, qty, rate):
		"""Company (Regular Stock) metal or alloy; return ``(batch, posted rate per g)``."""
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = COMPANY_RECEIPT_TYPE
		se.purpose = "Material Receipt"
		se.company = COMPANY
		se.posting_date = nowdate()
		se.set_posting_time = 1
		se.append(
			"items",
			{
				"item_code": item_code,
				"qty": qty,
				"basic_rate": flt(rate, 9),
				"t_warehouse": warehouse,
				"uom": "Gram",
				"stock_uom": "Gram",
				"conversion_factor": 1,
				"inventory_type": "Regular Stock",
				"expense_account": cls.difference_account,
			},
		)
		se.flags.ignore_mandatory = True
		se.save()
		se.submit()
		# A Regular Stock row is minted through its Serial and Batch Bundle, so the batch
		# lives there rather than on the row.
		return cls._bundle_batch(se.items[0]), cls._posted_rate(se.name)

	@staticmethod
	def _posted_rate(voucher_no):
		return _d(
			frappe.db.get_value(
				"Stock Ledger Entry",
				{"voucher_no": voucher_no, "is_cancelled": 0},
				"incoming_rate",
			)
		)

	# --------------------------------------------------------------- conversion
	@classmethod
	def _conversion(
		cls, warehouse, source_qty, source_item=SOURCE_ITEM, target_item=TARGET_ITEM
	):
		"""A draft single-converter Metal Conversion, calculated the way the form does it."""
		doc = frappe.new_doc("Metal Conversions")
		doc.update(
			{
				"company": COMPANY,
				"department": cls.mc_department,
				"manufacturer": MANUFACTURER,
				"employee": cls.employee,
				"date": nowdate(),
				"source_warehouse": warehouse,
				"target_warehouse": warehouse,
				"multiple_metal_converter": 0,
				"source_item": source_item,
				"source_qty": source_qty,
				"target_item": target_item,
			}
		)
		target_qty, alloy_qty = doc.calculate_metal_conversion()
		doc.target_qty = target_qty
		if alloy_qty > 0:
			doc.source_alloy_check = 1
			doc.source_alloy = ALLOY_ITEM
			# A Data field: the form stores the string, e.g. "0.899".
			doc.source_alloy_qty = str(alloy_qty)
		elif alloy_qty < 0:
			doc.target_alloy_check = 1
			doc.target_alloy_qty = abs(alloy_qty)
		doc.insert(ignore_permissions=True)
		return doc

	@staticmethod
	def _entries(doc, docstatus=1):
		return frappe.get_all(
			"Stock Entry",
			filters={
				"custom_metal_conversion_reference": doc.name,
				"docstatus": docstatus,
			},
			pluck="name",
		)

	def _entry(self, doc):
		names = self._entries(doc)
		self.assertEqual(
			len(names), 1, f"{doc.name} has {len(names)} submitted entries"
		)
		return frappe.get_doc("Stock Entry", names[0])

	@staticmethod
	def _bundle_batch(row):
		"""The batch a row moved: on the row, or in its bundle (ERPNext links a bundle it
		builds during submit on the saved row, not on the in-memory one)."""
		batch_no, bundle = row.batch_no, row.serial_and_batch_bundle
		if not (batch_no or bundle):
			batch_no, bundle = frappe.db.get_value(
				"Stock Entry Detail", row.name, ["batch_no", "serial_and_batch_bundle"]
			)
		if batch_no:
			return batch_no
		batches = frappe.get_all(
			"Serial and Batch Entry", filters={"parent": bundle}, pluck="batch_no"
		)
		return batches[0] if len(batches) == 1 else None

	def _groups(self, se):
		"""``{lane tag: {"sources": [row], "alloy": [row], "targets": [row]}}``."""
		groups = {}
		for row in se.items:
			group = groups.setdefault(
				row.custom_conversion_lane, {"sources": [], "alloy": [], "targets": []}
			)
			if row.t_warehouse:
				group["targets"].append(row)
			elif row.item_code == ALLOY_ITEM:
				group["alloy"].append(row)
			else:
				group["sources"].append(row)
		return groups

	def _group_of_source(self, se, batch):
		for tag, group in self._groups(se).items():
			if any(self._bundle_batch(row) == batch for row in group["sources"]):
				return tag, group
		self.fail(f"no lane consumed {batch}")

	@staticmethod
	def _sle(se, row):
		return frappe.db.get_value(
			"Stock Ledger Entry",
			{"voucher_no": se.name, "voucher_detail_no": row.name, "is_cancelled": 0},
			["actual_qty", "stock_value_difference", "incoming_rate"],
			as_dict=True,
		)

	@staticmethod
	def _balance(batch, warehouse):
		return flt(
			frappe.db.sql(
				"""
				SELECT SUM(sbe.qty)
				FROM `tabSerial and Batch Entry` sbe
				JOIN `tabSerial and Batch Bundle` sbb ON sbb.name = sbe.parent
				WHERE sbe.batch_no = %s AND sbb.warehouse = %s
					AND sbb.docstatus = 1 AND sbb.is_cancelled = 0
				""",
				(batch, warehouse),
			)[0][0],
			3,
		)

	@staticmethod
	def _origin(batch):
		return {
			row.batch_no: flt(row.qty, 3)
			for row in frappe.get_all(
				"Batch MultiSelect",
				filters={"parent": batch, "parentfield": "custom_origin_entries"},
				fields=["batch_no", "qty"],
			)
		}

	@staticmethod
	def _components(batch):
		return frappe.get_all(
			"Batch Component",
			filters={"parent": batch},
			fields=["source_batch", "customer", "inventory_type", "qty", "pure_qty"],
		)

	@staticmethod
	def _ledger(se_name):
		return frappe.get_all(
			"Customer Gold Ledger Entry",
			filters={"reference_docname": se_name},
			fields=[
				"cg_event_kind",
				"batch_no",
				"customer",
				"cg_gross_qty_delta",
				"cg_fine_gold_delta",
				"cg_reversal_of",
			],
		)


class TestMCON00332Shape(_MetalConversionCase):
	"""T57/T58/T59/T60: the reported conversion's exact shape, through the real controller.

	Source order 11 / Regular / 12 with 0.477 / 0.850 / 8.673 g of a 100% item into 91.75%,
	0.899 g of alloy; batch 12 holds 10 g, so it is consumed only in part (T13). Built once
	for the class and read from every angle below.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.wh = cls._fresh_warehouse()
		cls.b11, cls.r11 = cls._customer_batch(cls.wh, 0.477)
		cls.reg, cls.r_reg = cls._company_batch(
			cls.wh, SOURCE_ITEM, 0.850, REGULAR_RATE
		)
		cls.b12, cls.r12 = cls._customer_batch(cls.wh, 10.0, raw_rate=RAW_RATE_12)
		cls.alloy, cls.r_alloy = cls._company_batch(cls.wh, ALLOY_ITEM, 1.5, ALLOY_RATE)
		cls._ensure_gold_rate(nowdate(), RAW_RATE)

		cls.mc = cls._conversion(cls.wh, 10)
		cls.mc.submit()

	def setUp(self):
		self.se = self._entry(self.mc)
		self.groups = self._groups(self.se)

	# -- the document ------------------------------------------------------------
	def test_the_fixture_is_the_reported_shape(self):
		"""Guard the guard: the draw really was 11 / Regular / 12 at 0.477 / 0.850 / 8.673,
		and the two customer batches really carry different rates."""
		self.assertEqual(
			[(row.batch, flt(row.qty, 3)) for row in self.mc.source_batch_details],
			[(self.b11, 0.477), (self.reg, 0.85), (self.b12, 8.673)],
		)
		self.assertAlmostEqual(flt(self.mc.target_qty), 10.899182561, places=9)
		self.assertEqual(self.mc.source_alloy_qty, "0.899")
		self.assertNotEqual(self.r11, self.r12)

	def test_one_entry_linked_both_ways(self):
		"""T60: the reverse link finds exactly one entry, and the forward link is saved."""
		self.assertEqual(
			frappe.db.get_value("Metal Conversions", self.mc.name, "stock_entry"),
			self.se.name,
		)
		self.assertEqual(self.se.stock_entry_type, MC_SE_TYPE)
		self.assertEqual(self.se._customer, CUSTOMER)
		# Customer and company metal share the voucher: no single owner on the header.
		self.assertFalse(self.se.inventory_type)

	# -- identity ----------------------------------------------------------------
	def test_each_customer_batch_makes_its_own_target(self):
		"""T57/T03, R1/R2: two customer targets, one per source batch -- not one merged."""
		customer_targets = [
			row for row in self.se.items if row.t_warehouse and row.customer
		]
		self.assertEqual(len(customer_targets), 2)
		self.assertEqual(len({self._bundle_batch(r) for r in customer_targets}), 2)

		for source, qty in ((self.b11, 0.52), (self.b12, 9.453)):
			with self.subTest(source=source):
				_tag, group = self._group_of_source(self.se, source)
				(target,) = group["targets"]
				batch = self._bundle_batch(target)
				self.assertAlmostEqual(flt(target.qty), qty, places=3)
				self.assertEqual(target.customer, CUSTOMER)
				self.assertEqual(
					frappe.db.get_value(
						"Batch",
						batch,
						["custom_customer", "custom_inventory_type", "item"],
					),
					(CUSTOMER, "Customer Goods", TARGET_ITEM),
				)
				# Named from its OWN source batch (R3): same serial as its parent.
				self.assertEqual(batch.rsplit("-", 2)[-2], source.rsplit("-", 1)[-1])

	def test_company_metal_stays_company_metal(self):
		"""T04 half / MC-03: the Regular batch converts in the Regular lane, unowned."""
		_tag, group = self._group_of_source(self.se, self.reg)
		(target,) = group["targets"]
		self.assertAlmostEqual(flt(target.qty), 0.926, places=3)
		self.assertFalse(target.customer)
		batch = self._bundle_batch(target)
		self.assertEqual(
			frappe.db.get_value(
				"Batch", batch, ["custom_customer", "custom_inventory_type"]
			),
			(None, "Regular Stock"),
		)

	# -- quantities --------------------------------------------------------------
	def test_each_target_is_its_source_plus_its_own_alloy(self):
		"""MC-05/MC-06, R4: gross balances inside every lane; the alloy is used once."""
		expected = {
			self.b11: (0.477, 0.043, 0.52),
			self.reg: (0.85, 0.076, 0.926),
			self.b12: (8.673, 0.78, 9.453),
		}
		for source, (src, alloy, target) in expected.items():
			with self.subTest(source=source):
				_tag, group = self._group_of_source(self.se, source)
				self.assertAlmostEqual(
					sum(flt(r.qty) for r in group["sources"]), src, places=3
				)
				self.assertAlmostEqual(
					sum(flt(r.qty) for r in group["alloy"]), alloy, places=3
				)
				self.assertAlmostEqual(
					sum(flt(r.qty) for r in group["targets"]), target, places=3
				)
		alloy_rows = [row for row in self.se.items if row.item_code == ALLOY_ITEM]
		self.assertAlmostEqual(sum(flt(r.qty) for r in alloy_rows), 0.899, places=3)
		self.assertAlmostEqual(
			self._balance(self.alloy, self.wh), 1.5 - 0.899, places=3
		)

	def test_sources_are_consumed_exactly(self):
		"""T13: 11 and the Regular batch are used up; 12 keeps 10 - 8.673 = 1.327 g."""
		self.assertAlmostEqual(self._balance(self.b11, self.wh), 0.0, places=3)
		self.assertAlmostEqual(self._balance(self.reg, self.wh), 0.0, places=3)
		self.assertAlmostEqual(self._balance(self.b12, self.wh), 1.327, places=3)

	def test_targets_reconcile_to_the_header_at_posting_precision(self):
		"""T59: the header keeps 10.899182561 g; the rows post 10.899 g at 3 dp."""
		targets = [flt(r.qty) for r in self.se.items if r.t_warehouse]
		self.assertAlmostEqual(sum(targets), 10.899, places=6)
		self.assertAlmostEqual(
			flt(self.mc.target_qty) - sum(targets), 0.000182561, places=9
		)

	# -- value -------------------------------------------------------------------
	def _expected_value(self, source_qty, rate, alloy_qty):
		return (_d(source_qty) * rate + _d(alloy_qty) * self.r_alloy).quantize(
			PAISA, rounding=ROUND_HALF_UP
		)

	def test_each_target_carries_only_its_own_value(self):
		"""T58/T27, R5: a target is valued from its own batch and its own alloy share.

		The rates are the ones the receipts POSTED, read back from their ledger rows; batch
		11 and batch 12 were received at different gold rates, so a pooled or copied rate
		misses by rupees, not paise.
		"""
		for source, src, rate, alloy in (
			(self.b11, "0.477", self.r11, "0.043"),
			(self.reg, "0.850", self.r_reg, "0.076"),
			(self.b12, "8.673", self.r12, "0.780"),
		):
			with self.subTest(source=source):
				_tag, group = self._group_of_source(self.se, source)
				(target,) = group["targets"]
				posted = _d(self._sle(self.se, target).stock_value_difference)
				self.assertLessEqual(
					abs(posted - self._expected_value(src, rate, alloy)), PAISA, posted
				)

		rate11 = _d(
			self._group_of_source(self.se, self.b11)[1]["targets"][0].valuation_rate
		)
		rate12 = _d(
			self._group_of_source(self.se, self.b12)[1]["targets"][0].valuation_rate
		)
		self.assertGreater(abs(rate11 - rate12), Decimal("1"))

	def test_each_lane_posts_what_it_consumed(self):
		"""T59: inside a lane, value in equals value out to the paisa; nothing crosses lanes."""
		for tag, group in self.groups.items():
			with self.subTest(lane=tag):
				out = sum(
					-_d(self._sle(self.se, r).stock_value_difference)
					for r in group["sources"] + group["alloy"]
				)
				into = sum(
					_d(self._sle(self.se, r).stock_value_difference)
					for r in group["targets"]
				)
				self.assertLessEqual(abs(out - into), PAISA)
		self.assertLessEqual(
			abs(_d(self.se.value_difference)), PAISA * len(self.groups)
		)

	# -- lineage -----------------------------------------------------------------
	def test_lineage_names_only_the_lanes_own_inputs(self):
		"""MC-08/T08: 11's target descends from 11 and the alloy -- never from 12."""
		for source in (self.b11, self.b12, self.reg):
			with self.subTest(source=source):
				_tag, group = self._group_of_source(self.se, source)
				batch = self._bundle_batch(group["targets"][0])
				self.assertEqual(set(self._origin(batch)), {source, self.alloy})

	def test_components_split_customer_metal_from_company_alloy(self):
		"""MC-02/MC-03: the customer's grams are the customer's; the alloy stays company."""
		for source, qty, alloy in ((self.b11, 0.477, 0.043), (self.b12, 8.673, 0.78)):
			with self.subTest(source=source):
				_tag, group = self._group_of_source(self.se, source)
				components = self._components(self._bundle_batch(group["targets"][0]))
				customer = [c for c in components if c.customer]
				company = [c for c in components if not c.customer]
				self.assertEqual({c.customer for c in customer}, {CUSTOMER})
				self.assertAlmostEqual(sum(flt(c.qty) for c in customer), qty, places=3)
				self.assertAlmostEqual(
					sum(flt(c.qty) for c in company), alloy, places=3
				)

	def test_the_ledger_conserves_fine_gold_per_batch(self):
		"""MC-05: each customer batch's Conversion Out is matched by its own Conversion In."""
		events = self._ledger(self.se.name)
		for source in (self.b11, self.b12):
			with self.subTest(source=source):
				_tag, group = self._group_of_source(self.se, source)
				target_batch = self._bundle_batch(group["targets"][0])
				out = [
					e
					for e in events
					if e.batch_no == source and e.cg_event_kind == "Conversion Out"
				]
				into = [
					e
					for e in events
					if e.batch_no == target_batch and e.cg_event_kind == "Conversion In"
				]
				self.assertEqual((len(out), len(into)), (1, 1))
				self.assertLessEqual(
					abs(
						flt(out[0].cg_fine_gold_delta) + flt(into[0].cg_fine_gold_delta)
					),
					0.001,
				)
				self.assertEqual(into[0].customer, CUSTOMER)

	def test_subcontracting_lineage_follows_the_lane(self):
		"""T53: batch 11's repack children are its own target, not 12's or the company's."""
		own = self._bundle_batch(
			self._group_of_source(self.se, self.b11)[1]["targets"][0]
		)
		other = self._bundle_batch(
			self._group_of_source(self.se, self.b12)[1]["targets"][0]
		)
		regular = self._bundle_batch(
			self._group_of_source(self.se, self.reg)[1]["targets"][0]
		)
		linked = set(get_linked_batches(self.b11))
		self.assertIn(own, linked)
		self.assertNotIn(other, linked)
		self.assertNotIn(regular, linked)


class TestTwoCustomersAndCompanyMetal(_MetalConversionCase):
	"""T02/T04: two customers of the same item and inventory type, plus company metal."""

	def test_three_isolated_results(self):
		wh = self._fresh_warehouse()
		a, _ = self._customer_batch(wh, 4.0, customer=CUSTOMER)
		b, _ = self._customer_batch(wh, 3.0, customer=OTHER_CUSTOMER)
		reg, _ = self._company_batch(wh, SOURCE_ITEM, 2.0, REGULAR_RATE)
		self._company_batch(wh, ALLOY_ITEM, 1.0, ALLOY_RATE)

		mc = self._conversion(wh, 9)
		mc.submit()
		se = self._entry(mc)

		owners = {}
		for source in (a, b, reg):
			_tag, group = self._group_of_source(se, source)
			(target,) = group["targets"]
			owners[source] = (
				target.customer or None,
				frappe.db.get_value(
					"Batch", self._bundle_batch(target), "custom_customer"
				),
				flt(target.qty, 3),
			)
		# 9 g at 100% -> 91.75% is 9.809264 g, posted as 9.809. Per batch 4.359673 / 3.269755 /
		# 2.179837 round to 4.360 / 3.270 / 2.180 = 9.810, so the -0.001 residual goes to the
		# largest lane -- inside A's own lane, as 0.001 g less of the company's alloy.
		self.assertEqual(
			owners,
			{
				a: (CUSTOMER, CUSTOMER, 4.359),
				b: (OTHER_CUSTOMER, OTHER_CUSTOMER, 3.27),
				reg: (None, None, 2.18),
			},
		)
		self.assertIsNone(se.inventory_type or None)


class TestShapesThatDoNotChange(_MetalConversionCase):
	"""T01/T46/MC-12: a one-batch customer conversion and a Regular-only conversion."""

	def test_one_customer_batch_is_one_target_with_the_owner_on_the_header(self):
		wh = self._fresh_warehouse()
		batch, _ = self._customer_batch(wh, 5.0)
		self._company_batch(wh, ALLOY_ITEM, 1.0, ALLOY_RATE)
		mc = self._conversion(wh, 5)
		mc.submit()
		se = self._entry(mc)
		targets = [row for row in se.items if row.t_warehouse]
		self.assertEqual(len(targets), 1)
		self.assertAlmostEqual(flt(targets[0].qty), 5.45, places=3)
		self.assertEqual(se.inventory_type, "Customer Goods")
		self.assertEqual(se._customer, CUSTOMER)
		self.assertEqual(
			self._bundle_batch(targets[0]).rsplit("-", 2)[-2], batch.rsplit("-", 1)[-1]
		)

	def test_regular_stock_batches_still_pool_into_one_target(self):
		wh = self._fresh_warehouse()
		self._company_batch(wh, SOURCE_ITEM, 1.0, REGULAR_RATE)
		self._company_batch(wh, SOURCE_ITEM, 2.0, REGULAR_RATE + 100)
		self._company_batch(wh, ALLOY_ITEM, 1.0, ALLOY_RATE)
		mc = self._conversion(wh, 3)
		mc.submit()
		se = self._entry(mc)
		targets = [row for row in se.items if row.t_warehouse]
		self.assertEqual(len(targets), 1)
		self.assertAlmostEqual(flt(targets[0].qty), 3.27, places=3)
		self.assertFalse(se._customer)
		self.assertEqual(se.inventory_type, "Regular Stock")


class TestConversionLifecycle(_MetalConversionCase):
	"""T35/T36/T40/T41/T42/T48/T60: submit, retry, cancel, amend and downstream use."""

	def _converted(self):
		wh = self._fresh_warehouse()
		b11, _ = self._customer_batch(wh, 1.0)
		b12, _ = self._customer_batch(wh, 2.0, raw_rate=RAW_RATE_12)
		self._company_batch(wh, ALLOY_ITEM, 2.0, ALLOY_RATE)
		mc = self._conversion(wh, 3)
		mc.submit()
		return wh, b11, b12, mc

	def test_a_repeated_submit_makes_nothing_new(self):
		"""T35/T36: a retried submit of an already submitted conversion (a lost response,
		then the user clicks again) creates nothing. Frappe treats it as a save of the
		submitted document -- on_submit does not run again -- so no second entry exists."""
		_wh, _b11, _b12, mc = self._converted()
		first = self._entries(mc)
		try:
			frappe.get_doc("Metal Conversions", mc.name).submit()
		except frappe.ValidationError:
			pass
		self.assertEqual(self._entries(mc), first)
		self.assertEqual(
			frappe.db.get_value("Metal Conversions", mc.name, "docstatus"), 1
		)

	def test_the_entry_cannot_be_cancelled_behind_the_conversion(self):
		"""T60: while the conversion is submitted, its linked entry cannot be cancelled alone."""
		_wh, _b11, _b12, mc = self._converted()
		se = self._entry(mc)
		frappe.db.savepoint("entry_cancel")
		with self.assertRaises(frappe.LinkExistsError):
			frappe.get_doc("Stock Entry", se.name).cancel()
		frappe.db.rollback(save_point="entry_cancel")
		self.assertEqual(frappe.db.get_value("Stock Entry", se.name, "docstatus"), 1)

	def test_cancelling_the_conversion_reverses_every_effect(self):
		"""T40: stock, targets and the customer's ledger all return to the pre-conversion state."""
		wh, b11, b12, mc = self._converted()
		se = self._entry(mc)
		targets = [self._bundle_batch(row) for row in se.items if row.t_warehouse]

		frappe.get_doc("Metal Conversions", mc.name).cancel()

		self.assertEqual(frappe.db.get_value("Stock Entry", se.name, "docstatus"), 2)
		self.assertEqual(self._balance(b11, wh), 1.0)
		self.assertEqual(self._balance(b12, wh), 2.0)
		for batch in targets:
			self.assertEqual(self._balance(batch, wh), 0.0)
		net = {}
		for event in self._ledger(se.name):
			net[event.batch_no] = flt(
				net.get(event.batch_no, 0) + flt(event.cg_gross_qty_delta), 6
			)
		self.assertTrue(net)
		self.assertTrue(all(value == 0 for value in net.values()), net)

	def test_amending_converts_again_without_double_consumption(self):
		"""T42: the amended conversion posts new outputs; the cancelled history stays."""
		wh, b11, b12, mc = self._converted()
		old_se = self._entry(mc)
		old_targets = {
			self._bundle_batch(row) for row in old_se.items if row.t_warehouse
		}
		frappe.get_doc("Metal Conversions", mc.name).cancel()

		# As the desk's Amend does (frappe.model.copy_doc with from_amend): no-copy fields come
		# along, the cancelled entry's ``stock_entry`` link among them, and Frappe checks links
		# before any hook runs -- so saved as it is, the amendment is refused ...
		amended = frappe.copy_doc(frappe.get_doc("Metal Conversions", mc.name))
		amended.amended_from = mc.name
		amended.docstatus = 0
		self.assertEqual(amended.stock_entry, old_se.name)
		frappe.db.savepoint("amend_as_copied")
		with self.assertRaises(frappe.CancelledLinkError):
			frappe.copy_doc(amended).insert(ignore_permissions=True)
		frappe.db.rollback(save_point="amend_as_copied")
		# ... which is why the form drops it on load (metal_conversions.js, onload).
		amended.stock_entry = None
		amended.insert(ignore_permissions=True)
		amended.submit()

		new_se = self._entry(amended)
		new_targets = {
			self._bundle_batch(row) for row in new_se.items if row.t_warehouse
		}
		self.assertNotEqual(new_se.name, old_se.name)
		self.assertFalse(old_targets & new_targets)
		self.assertEqual(
			frappe.db.get_value("Stock Entry", old_se.name, "docstatus"), 2
		)
		self.assertEqual(self._balance(b11, wh), 0.0)
		self.assertEqual(self._balance(b12, wh), 0.0)

	def test_a_second_conversion_keeps_each_customers_ancestry(self):
		"""T48, then T41: converting the converted metal again keeps each batch's root; once
		an output is used, the first conversion can no longer be cancelled."""
		wh, b11, b12, mc = self._converted()
		first = self._entry(mc)
		t11 = self._bundle_batch(self._group_of_source(first, b11)[1]["targets"][0])
		t12 = self._bundle_batch(self._group_of_source(first, b12)[1]["targets"][0])

		# 1.090 + 2.180 g of 91.75% -> 75.4%: the whole of both 22KT batches.
		self._company_batch(wh, ALLOY_ITEM, 2.0, ALLOY_RATE)
		second = self._conversion(
			wh,
			flt(1.09 + 2.18, 3),
			source_item=TARGET_ITEM,
			target_item=self.operating_item,
		)
		second.submit()
		se2 = self._entry(second)
		for parent, root in ((t11, b11), (t12, b12)):
			with self.subTest(parent=parent):
				_tag, group = self._group_of_source(se2, parent)
				grandchild = self._bundle_batch(group["targets"][0])
				self.assertEqual(
					set(self._origin(grandchild)) - {None},
					{parent} | {self._bundle_batch(r) for r in group["alloy"]},
				)
				roots = {
					c.source_batch for c in self._components(grandchild) if c.customer
				}
				self.assertEqual(roots, {root})

		# A refused cancel must leave nothing half-done. In a request the failed transaction is
		# rolled back; inside this class's one transaction a savepoint stands in for that.
		frappe.db.savepoint("blocked_cancel")
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc("Metal Conversions", mc.name).cancel()
		frappe.db.rollback(save_point="blocked_cancel")
		self.assertEqual(
			frappe.db.get_value("Metal Conversions", mc.name, "docstatus"), 1
		)
		self.assertEqual(frappe.db.get_value("Stock Entry", first.name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Stock Entry", se2.name, "docstatus"), 1)


class TestMultipleConverterSplitsCustomerBatches(_MetalConversionCase):
	"""T15/T19: the multiple converter isolates customer batches the same way."""

	def test_each_customer_batch_is_its_own_target(self):
		wh = self._fresh_warehouse()
		a1, _ = self._customer_batch(wh, 3.0)
		a2, _ = self._customer_batch(wh, 2.0, raw_rate=RAW_RATE_12)
		reg, _ = self._company_batch(wh, SOURCE_ITEM, 1.0, REGULAR_RATE)
		self._company_batch(wh, ALLOY_ITEM, 1.0, ALLOY_RATE)

		doc = frappe.new_doc("Metal Conversions")
		doc.update(
			{
				"company": COMPANY,
				"department": self.mc_department,
				"manufacturer": MANUFACTURER,
				"employee": self.employee,
				"date": nowdate(),
				"source_warehouse": wh,
				"target_warehouse": wh,
				"multiple_metal_converter": 1,
				"m_target_item": TARGET_ITEM,
			}
		)
		for batch, qty in ((a1, 3.0), (a2, 2.0), (reg, 1.0)):
			doc.append(
				"mc_source_table",
				{
					"item_code": SOURCE_ITEM,
					"qty": qty,
					"batch": batch,
					"total": qty * 100,
				},
			)
		target_qty, alloy_qty = doc.calculate_Multiple_conversion()
		# 600 / 91.75 = 6.539509... -> 6.540 g; alloy 0.540 g.
		self.assertAlmostEqual(target_qty, 6.54, places=3)
		doc.m_target_qty = target_qty
		doc.alloy = ALLOY_ITEM
		doc.alloy_qty = alloy_qty
		doc.alloy_check = 0
		doc.insert(ignore_permissions=True)
		doc.submit()

		se = self._entry(doc)
		self.assertEqual(
			frappe.db.get_value("Metal Conversions", doc.name, "stock_entry"), se.name
		)
		# Fine weights 300 / 200 / 100 -> 3.270 / 2.180 / 1.090 g; alloy 0.270 / 0.180 / 0.090.
		for source, owner, target_qty, alloy in (
			(a1, CUSTOMER, 3.27, 0.27),
			(a2, CUSTOMER, 2.18, 0.18),
			(reg, None, 1.09, 0.09),
		):
			with self.subTest(source=source):
				_tag, group = self._group_of_source(se, source)
				(target,) = group["targets"]
				self.assertEqual(target.customer or None, owner)
				self.assertAlmostEqual(flt(target.qty), target_qty, places=3)
				self.assertAlmostEqual(
					sum(flt(r.qty) for r in group["alloy"]), alloy, places=3
				)
				self.assertEqual(
					frappe.db.get_value(
						"Batch", self._bundle_batch(target), "custom_customer"
					),
					owner,
				)


class TestLargeConversionStaysLinear(_MetalConversionCase):
	"""T56 / MC-14: many customer batches in one conversion -- queries grow with the lanes,
	not with their square, and nothing commits per lane."""

	def _measure(self, lanes):
		wh = self._fresh_warehouse()
		for _ in range(lanes):
			self._customer_batch(wh, 1.0)
		self._company_batch(wh, ALLOY_ITEM, 1.0 * lanes, ALLOY_RATE)
		mc = self._conversion(wh, lanes)

		db = frappe.local.db
		real_sql, real_commit = db.sql, db.commit
		shadowed = {name: name in vars(db) for name in ("sql", "commit")}
		counts = {"sql": 0, "commit": 0}

		def _sql(*args, **kwargs):
			counts["sql"] += 1
			return real_sql(*args, **kwargs)

		def _commit(*args, **kwargs):
			counts["commit"] += 1
			return real_commit(*args, **kwargs)

		db.sql, db.commit = _sql, _commit
		started = time.perf_counter()
		try:
			mc.submit()
		finally:
			for name, real in (("sql", real_sql), ("commit", real_commit)):
				if shadowed[name]:
					setattr(db, name, real)
				else:
					delattr(db, name)
		seconds = time.perf_counter() - started

		se = self._entry(mc)
		self.assertEqual(
			len([r for r in se.items if r.t_warehouse and r.customer]), lanes
		)
		return counts, seconds

	def test_queries_scale_linearly_with_lanes(self):
		small, small_s = self._measure(5)
		large, large_s = self._measure(25)
		print(
			f"\nT56 measured: 5 lanes -> {small['sql']} queries, {small_s:.2f}s; "
			f"25 lanes -> {large['sql']} queries, {large_s:.2f}s; "
			f"commits {small['commit']}/{large['commit']}"
		)
		# 5x the lanes; quadratic work would be ~25x. Allow generous constant overheads.
		self.assertLess(large["sql"], small["sql"] * 7.5)
		self.assertEqual(large["commit"], small["commit"])
