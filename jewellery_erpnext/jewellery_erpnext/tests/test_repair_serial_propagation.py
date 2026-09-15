"""Repair serial/BOM lineage: Manufacturing Plan row -> PMO -> MWO -> Unpack Serial No.

A Repair work order can only offer Unpack Serial No once it carries the repair's
serial and the repair's design BOM. Both used to reach the Parent Manufacturing
Order by side-channel rather than by assignment (serial_no via a fetch_from off
the Sales Order Item, serial_id_bom via after_insert re-deriving it from
BOM.tag_no), and master_bom was taken from serial_id_bom alone -- so a repair row
whose serial_id_bom was blank produced a PMO with no BOM and no serial, and every
MWO under it inherited the blanks. These tests pin each hop.

Run with:
  bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_repair_serial_propagation
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan import (
	_resolve_repair_master_bom,
	get_details_to_append,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.test_manufacturing_plan import (
	create_repair_sales_order,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
	ManufacturingWorkOrder,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	ParentManufacturingOrder,
	make_manufacturing_order,
)
from jewellery_erpnext.utils import get_repair_order_design_bom

MP_MOD = (
	"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan"
)
PMO_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order"
MWO_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order"
UTILS_MOD = "jewellery_erpnext.utils"


# ---------------------------------------------------------------------------
# 1. Which BOM is the repair's master_bom
# ---------------------------------------------------------------------------


def _row(**kwargs):
	row = frappe._dict(serial_id_bom=None, bom=None, serial_no=None)
	row.update(kwargs)
	return row


class TestResolveRepairMasterBom(IntegrationTestCase):
	"""A repair carries three distinct BOM identities. master_bom is the DESIGN BOM."""

	def _resolve(self, row, so_det, design_bom):
		with patch(f"{MP_MOD}.get_repair_order_design_bom", return_value=design_bom):
			return _resolve_repair_master_bom(row, so_det)

	def test_repair_order_design_bom_wins(self):
		"""The BOM the repair is manufactured/unpacked against is Repair Order.bom.

		This is the case the live defect produced wrongly: the PMO showed the serial's
		historical BOM (or nothing), so the operator never saw the repair BOM until the
		unpack button back-filled it on first click.
		"""
		row = _row(serial_id_bom="BOM-SERIAL-OLD", bom="BOM-COPY")
		so_det = {"order_form_type": "Repair Order", "order_form_id": "RO-1"}
		self.assertEqual(
			self._resolve(row, so_det, "BOM-REPAIR-DESIGN"), "BOM-REPAIR-DESIGN"
		)

	def test_falls_back_to_serial_id_bom_without_repair_order(self):
		"""Header-``order_type`` repairs carry no Repair Order link at all and have always
		used serial_id_bom. Keeping that second leaves their behaviour untouched."""
		row = _row(serial_id_bom="BOM-SERIAL-OLD", bom="BOM-COPY")
		self.assertEqual(self._resolve(row, {}, None), "BOM-SERIAL-OLD")

	def test_falls_back_to_row_bom_when_only_copy_bom_present(self):
		"""The regression that emptied PMO.master_bom.

		get_items_for_production resolves row.bom from copy_bom FIRST and only falls back
		to serial_id_bom, so a repair row can hold a perfectly good Copy BOM while
		serial_id_bom is blank. The old ``master_bom = row.serial_id_bom`` then yielded
		None -- a PMO, and every MWO under it, with no BOM.
		"""
		row = _row(serial_id_bom=None, bom="BOM-COPY")
		self.assertEqual(self._resolve(row, {}, None), "BOM-COPY")

	def test_returns_none_when_nothing_resolvable(self):
		self.assertIsNone(self._resolve(_row(), {}, None))

	def test_design_bom_is_not_confused_with_tracking_bom(self):
		"""serial_id_bom / design BOM / custom_tracking_bom must each keep their identity;
		resolving master_bom must never reach for the tracking BOM."""
		row = _row(serial_id_bom="BOM-SERIAL-OLD", bom="BOM-COPY")
		row.custom_tracking_bom = "TB-REPAIR-001"
		so_det = {"order_form_type": "Repair Order", "order_form_id": "RO-1"}
		resolved = self._resolve(row, so_det, "BOM-REPAIR-DESIGN")
		self.assertEqual(resolved, "BOM-REPAIR-DESIGN")
		self.assertNotEqual(resolved, row.custom_tracking_bom)
		self.assertNotEqual(resolved, row.serial_id_bom)


class TestGetRepairOrderDesignBom(IntegrationTestCase):
	def _call(self, oft, ofi, ro_bom="BOM-DESIGN"):
		mock_db = MagicMock()
		mock_db.get_value.return_value = ro_bom
		with patch(f"{UTILS_MOD}.frappe.db", mock_db):
			return get_repair_order_design_bom(oft, ofi), mock_db

	def test_reads_the_design_bom_field(self):
		bom, mock_db = self._call("Repair Order", "RO-1")
		self.assertEqual(bom, "BOM-DESIGN")
		mock_db.get_value.assert_called_once_with("Repair Order", "RO-1", "bom")

	def test_non_repair_rows_do_not_query_at_all(self):
		"""Normal Manufacturing rows must not pay for -- or be influenced by -- a Repair
		Order lookup."""
		bom, mock_db = self._call("Order", "ORD-1")
		self.assertIsNone(bom)
		mock_db.get_value.assert_not_called()

	def test_missing_order_form_id_returns_none(self):
		bom, mock_db = self._call("Repair Order", None)
		self.assertIsNone(bom)
		mock_db.get_value.assert_not_called()


# ---------------------------------------------------------------------------
# 2. The plan row -> PMO hop
# ---------------------------------------------------------------------------


class _FakePMODoc(frappe._dict):
	"""Records what make_manufacturing_order assigns, without touching the database."""

	def insert(self, **kwargs):
		self["_inserted"] = True


def _make_pmo(row, master_bom="BOM-DESIGN", select="Repair"):
	fake = _FakePMODoc()
	source_doc = frappe._dict(
		company="Test_Company", select_manufacture_order=select, name="MP-1"
	)
	with patch(f"{PMO_MOD}.frappe.new_doc", return_value=fake):
		make_manufacturing_order(source_doc, row, master_bom=master_bom, so_det={})
	return fake


class TestMakeManufacturingOrderCarriesRepairLineage(IntegrationTestCase):
	def _repair_row(self):
		return frappe._dict(
			sales_order="SO-1",
			docname="SO-1-ITEM-1",
			name="MPT-ROW-1",
			item_code="MU00128-001",
			custom_tracking_bom="TB-REPAIR-001",
			serial_no="TEST-REPAIR-SERIAL-001",
			serial_id_bom="BOM-SERIAL-OLD",
			qty_per_manufacturing_order=1,
			customer_sample=None,
			customer_voucher_no=None,
			customer_gold="No",
			customer_diamond="No",
			customer_stone="No",
			customer_good="Yes",
			customer_weight=0,
			repair_type=None,
			product_type=None,
		)

	def test_serial_no_is_assigned_from_the_plan_row(self):
		"""The row already holds the serial; relying on the sales_order_item fetch_from
		alone is what let it go missing."""
		doc = _make_pmo(self._repair_row())
		self.assertEqual(doc.serial_no, "TEST-REPAIR-SERIAL-001")

	def test_serial_id_bom_is_assigned_from_the_plan_row(self):
		doc = _make_pmo(self._repair_row())
		self.assertEqual(doc.serial_id_bom, "BOM-SERIAL-OLD")

	def test_all_four_bom_identities_stay_distinct(self):
		"""master_bom (design), serial_id_bom (history) and custom_tracking_bom (the
		manufacturing tracking BOM) must not collapse into one another."""
		doc = _make_pmo(self._repair_row(), master_bom="BOM-REPAIR-DESIGN")
		self.assertEqual(doc.master_bom, "BOM-REPAIR-DESIGN")
		self.assertEqual(doc.serial_id_bom, "BOM-SERIAL-OLD")
		self.assertEqual(doc.custom_tracking_bom, "TB-REPAIR-001")
		self.assertEqual(
			len({doc.master_bom, doc.serial_id_bom, doc.custom_tracking_bom}), 3
		)

	def test_type_still_follows_the_plan(self):
		self.assertEqual(_make_pmo(self._repair_row()).type, "Repair")

	def test_manufacturing_rows_are_unaffected(self):
		"""A normal Manufacturing row carries no serial; the same assignments must leave
		it blank rather than inventing values."""
		row = self._repair_row()
		row.serial_no = None
		row.serial_id_bom = None
		doc = _make_pmo(row, master_bom="BOM-MFG", select="Manufacturing")
		self.assertEqual(doc.type, "Manufacturing")
		self.assertIsNone(doc.serial_no)
		self.assertIsNone(doc.serial_id_bom)
		self.assertEqual(doc.master_bom, "BOM-MFG")


class TestPMOAfterInsertSerialIdBom(IntegrationTestCase):
	"""after_insert derives serial_id_bom from BOM.tag_no -- as a FALLBACK only."""

	def _run(self, serial_no, serial_id_bom, tagged_bom):
		written = {}
		fake = SimpleNamespace(
			custom_tracking_bom=None,
			serial_no=serial_no,
			serial_id_bom=serial_id_bom,
			db_set=lambda field, value: written.__setitem__(field, value),
		)
		mock_db = MagicMock()
		mock_db.exists.return_value = tagged_bom
		with patch(f"{PMO_MOD}.frappe.db", mock_db):
			ParentManufacturingOrder.after_insert(fake)
		return written

	def test_derives_from_tag_when_row_had_none(self):
		written = self._run("SN-1", None, "BOM-FROM-TAG")
		self.assertEqual(written.get("serial_id_bom"), "BOM-FROM-TAG")

	def test_does_not_overwrite_the_value_carried_from_the_plan_row(self):
		"""The order was raised against a specific serial BOM. A BOM that merely happens
		to be tagged to the same serial must not silently replace it."""
		written = self._run("SN-1", "BOM-FROM-ROW", "BOM-FROM-TAG")
		self.assertNotIn("serial_id_bom", written)

	def test_no_serial_means_no_derivation(self):
		self.assertNotIn("serial_id_bom", self._run(None, None, "BOM-FROM-TAG"))

	def test_untagged_serial_leaves_it_blank(self):
		self.assertNotIn("serial_id_bom", self._run("SN-1", None, None))


# ---------------------------------------------------------------------------
# 3. Unpack guards
# ---------------------------------------------------------------------------


class TestUnpackSerialItemGuard(IntegrationTestCase):
	def _assert_item(self, serial_item, mwo_item="MU00128-001"):
		mock_db = MagicMock()
		mock_db.get_value.return_value = serial_item
		fake = SimpleNamespace(serial_no="SN-1", item_code=mwo_item)
		with patch(f"{MWO_MOD}.frappe.db", mock_db):
			ManufacturingWorkOrder._assert_serial_matches_item(fake)

	def test_matching_item_is_allowed(self):
		self._assert_item("MU00128-001")

	def test_unrelated_serial_is_refused(self):
		"""Both fields are hand-settable, so a pasted serial can point at another
		customer's piece -- unpacking it would consume their jewellery."""
		with self.assertRaises(frappe.ValidationError):
			self._assert_item("XX99999-001")

	def test_missing_serial_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._assert_item(None)


class TestFindExistingUnpackEntry(IntegrationTestCase):
	def _find(self, returned):
		mock_db = MagicMock()
		mock_db.get_value.return_value = returned
		fake = SimpleNamespace(name="MWO-1")
		with patch(f"{MWO_MOD}.frappe.db", mock_db):
			result = ManufacturingWorkOrder.find_existing_unpack_entry(fake)
		return result, mock_db.get_value.call_args

	def test_reports_a_live_entry(self):
		result, _ = self._find("STE-0001")
		self.assertEqual(result, "STE-0001")

	def test_reports_none_when_never_unpacked(self):
		result, _ = self._find(None)
		self.assertIsNone(result)

	def test_ignores_cancelled_entries_so_retry_stays_possible(self):
		"""Cancellation reverses the whole stock/MOP effect, so a cancelled entry must not
		keep blocking a legitimate second attempt."""
		_, call_args = self._find(None)
		self.assertEqual(call_args[0][1]["docstatus"], ["!=", 2])

	def test_is_keyed_on_the_work_order_and_entry_type(self):
		_, call_args = self._find(None)
		filters = call_args[0][1]
		self.assertEqual(filters["manufacturing_work_order"], "MWO-1")
		self.assertEqual(filters["stock_entry_type"], "Repair Unpack")


class TestUnpackIdempotency(IntegrationTestCase):
	"""A second click must never post a second Stock Entry: that would consume the serial
	twice, mint a second set of Customer Goods batches and double every MOP weight."""

	def test_second_call_is_blocked_before_anything_is_created(self):
		fake = SimpleNamespace(
			manufacturing_order="PMO-1",
			serial_no="SN-1",
			item_code="MU00128-001",
			docstatus=1,
			find_existing_unpack_entry=lambda: "STE-0001",
		)
		mock_db = MagicMock()
		mock_db.get_value.return_value = "Repair"
		new_doc = MagicMock()
		with patch(f"{MWO_MOD}.frappe.db", mock_db), patch(
			f"{MWO_MOD}.frappe.new_doc", new_doc
		):
			with self.assertRaises(frappe.ValidationError):
				ManufacturingWorkOrder.create_unpack_serial_no_stock_entry(fake)
		# Nothing may have been built: no Stock Entry, and no Customer Goods batches.
		new_doc.assert_not_called()
		mock_db.set_value.assert_not_called()

	def test_draft_work_order_is_refused(self):
		fake = SimpleNamespace(
			manufacturing_order="PMO-1",
			serial_no="SN-1",
			item_code="MU00128-001",
			docstatus=0,
			find_existing_unpack_entry=lambda: None,
		)
		mock_db = MagicMock()
		mock_db.get_value.return_value = "Repair"
		with patch(f"{MWO_MOD}.frappe.db", mock_db):
			with self.assertRaises(frappe.ValidationError):
				ManufacturingWorkOrder.create_unpack_serial_no_stock_entry(fake)

	def test_non_repair_work_order_is_refused(self):
		fake = SimpleNamespace(
			manufacturing_order="PMO-1",
			serial_no="SN-1",
			item_code="MU00128-001",
			docstatus=1,
			find_existing_unpack_entry=lambda: None,
		)
		mock_db = MagicMock()
		mock_db.get_value.return_value = "Manufacturing"
		with patch(f"{MWO_MOD}.frappe.db", mock_db):
			with self.assertRaises(frappe.ValidationError):
				ManufacturingWorkOrder.create_unpack_serial_no_stock_entry(fake)

	def test_repair_without_serial_is_refused(self):
		fake = SimpleNamespace(
			manufacturing_order="PMO-1",
			serial_no=None,
			item_code="MU00128-001",
			docstatus=1,
			find_existing_unpack_entry=lambda: None,
		)
		mock_db = MagicMock()
		mock_db.get_value.return_value = "Repair"
		with patch(f"{MWO_MOD}.frappe.db", mock_db):
			with self.assertRaises(frappe.ValidationError):
				ManufacturingWorkOrder.create_unpack_serial_no_stock_entry(fake)


# ---------------------------------------------------------------------------
# 4. Button eligibility (display only -- the server re-validates everything)
# ---------------------------------------------------------------------------


class TestUnpackEligibility(IntegrationTestCase):
	def _eligibility(self, pmo_type, docstatus, serial_no, existing):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order import (
			manufacturing_work_order as mwo_module,
		)

		doc = SimpleNamespace(
			manufacturing_order="PMO-1",
			docstatus=docstatus,
			serial_no=serial_no,
			check_permission=lambda perm: None,
			find_existing_unpack_entry=lambda: existing,
		)
		mock_db = MagicMock()
		mock_db.get_value.return_value = pmo_type
		with patch(f"{MWO_MOD}.frappe.get_doc", return_value=doc), patch(
			f"{MWO_MOD}.frappe.db", mock_db
		):
			return mwo_module.get_unpack_eligibility("MWO-1")

	def test_submitted_repair_with_serial_shows_the_button(self):
		result = self._eligibility("Repair", 1, "SN-1", None)
		self.assertTrue(result["eligible"])
		self.assertFalse(result["already_unpacked"])

	def test_normal_manufacturing_hides_it(self):
		self.assertFalse(
			self._eligibility("Manufacturing", 1, "SN-1", None)["eligible"]
		)

	def test_finding_manufacturing_hides_it(self):
		self.assertFalse(
			self._eligibility("Finding Manufacturing", 1, "SN-1", None)["eligible"]
		)

	def test_draft_repair_hides_it(self):
		self.assertFalse(self._eligibility("Repair", 0, "SN-1", None)["eligible"])

	def test_cancelled_repair_hides_it(self):
		self.assertFalse(self._eligibility("Repair", 2, "SN-1", None)["eligible"])

	def test_repair_without_serial_hides_it_and_says_why(self):
		result = self._eligibility("Repair", 1, None, None)
		self.assertFalse(result["eligible"])
		self.assertIn("Serial No", result["reason"])

	def test_already_unpacked_hides_it_but_points_at_the_entry(self):
		result = self._eligibility("Repair", 1, "SN-1", "STE-0001")
		self.assertFalse(result["eligible"])
		self.assertTrue(result["already_unpacked"])
		self.assertEqual(result["stock_entry"], "STE-0001")


# ---------------------------------------------------------------------------
# 5. End-to-end: the repair BOM really lands on the persisted PMO and MWO
# ---------------------------------------------------------------------------


class TestRepairLineageReachesPMO(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")
		cls.warehouse = frappe.get_value(
			"Warehouse", {"warehouse_name": "Test_Warehouse"}, "name"
		)

	def test_repair_plan_creates_pmos_that_already_carry_their_bom(self):
		"""Persisted-database assertion, not an in-memory one: read the PMO back.

		Before the fix a repair row whose serial_id_bom was blank produced
		master_bom=None, so the operator opened a Repair PMO (and its MWOs) with no BOM
		at all and Unpack Serial No could never be reached.
		"""
		repair_so = create_repair_sales_order(self)

		doc = frappe.new_doc("Manufacturing Plan")
		doc.select_manufacture_order = "Repair"
		man_plan = get_details_to_append(json.dumps([repair_so]), doc)
		man_plan.company = "Test_Company"
		man_plan.branch = self.branch
		if man_plan.setting_type:
			man_plan.setting_type = "Nova Glow"
		man_plan.is_subcontracting = "No"
		self.assertTrue(man_plan.manufacturing_plan_table)
		man_plan.save()
		man_plan.submit()

		pmos = frappe.get_all(
			"Parent Manufacturing Order",
			filters={"manufacturing_plan": man_plan.name},
			fields=["name", "type", "master_bom", "serial_id_bom", "serial_no"],
		)
		self.assertTrue(pmos, "Repair plan created no Parent Manufacturing Order")
		for pmo in pmos:
			self.assertEqual(pmo.type, "Repair")
			self.assertTrue(
				pmo.master_bom,
				f"{pmo.name} was created without a master_bom -- the Unpack regression",
			)

		# ...and every MWO under them inherits the same BOM context.
		for pmo in pmos:
			for mwo in frappe.get_all(
				"Manufacturing Work Order",
				filters={"manufacturing_order": pmo.name},
				fields=["name", "master_bom", "serial_id_bom", "serial_no"],
			):
				self.assertEqual(mwo.master_bom, pmo.master_bom)
				self.assertEqual(mwo.serial_id_bom, pmo.serial_id_bom)
				self.assertEqual(mwo.serial_no, pmo.serial_no)
