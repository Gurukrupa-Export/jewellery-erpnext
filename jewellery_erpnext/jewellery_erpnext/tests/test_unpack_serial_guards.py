"""Unit tests for the Unpack Serial No guards: the serial must be its Serial No BOM's item and,
with a Repair Order, that order's serial; finding and draft work orders are refused; a work order
is unpacked at most once. The design BOM lookup is covered too.

DB-free: frappe.db is patched in every test.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
	ManufacturingWorkOrder,
)
from jewellery_erpnext.utils import get_repair_order_design_bom

MWO_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order"
UTILS_MOD = "jewellery_erpnext.utils"

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


class TestUnpackSerialItemGuard(IntegrationTestCase):
	"""The serial must be the item its Serial No BOM makes; the MWO item is not compared."""

	def _assert_item(self, serial_item, bom_item, mwo_item="MU00128-002"):
		def gv(doctype, name, fieldname):
			if doctype == "Serial No":
				return serial_item
			if doctype == "BOM":
				return bom_item
			return None

		mock_db = MagicMock()
		mock_db.get_value.side_effect = gv
		fake = SimpleNamespace(serial_no="SN-1", item_code=mwo_item)
		with patch(f"{MWO_MOD}.frappe.db", mock_db):
			return ManufacturingWorkOrder._assert_serial_matches_bom(fake, "BOM-SN")

	def test_matching_item_is_allowed(self):
		self.assertEqual(self._assert_item("MU00128-001", "MU00128-001"), "MU00128-001")

	def test_design_change_repair_is_allowed(self):
		"""Attribute change: the MWO makes -002 but the serial and its BOM are -001."""
		self.assertEqual(
			self._assert_item("MU00128-001", "MU00128-001", mwo_item="MU00128-002"),
			"MU00128-001",
		)

	def test_serial_bom_of_another_item_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._assert_item("MU00128-001", "XX99999-001")

	def test_missing_serial_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._assert_item(None, "MU00128-001")


class TestUnpackSerialRepairOrderGuard(IntegrationTestCase):
	"""With a Repair Order, its serial must be the work order's serial."""

	def _check(self, pmo_link, order_serial, mwo_serial="SN-1"):
		def gv(doctype, name, fieldname):
			if doctype == "Parent Manufacturing Order":
				return pmo_link
			if doctype == "Repair Order":
				return order_serial
			return None

		mock_db = MagicMock()
		mock_db.get_value.side_effect = gv
		fake = SimpleNamespace(manufacturing_order="PMO-1", serial_no=mwo_serial)
		with patch(f"{MWO_MOD}.frappe.db", mock_db):
			ManufacturingWorkOrder._assert_serial_matches_repair_order(fake)

	def test_same_serial_is_allowed(self):
		self._check(("Repair Order", "RO-1"), "SN-1")

	def test_another_serial_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._check(("Repair Order", "RO-1"), "SN-OTHER")

	def test_repair_order_without_serial_is_not_checked(self):
		self._check(("Repair Order", "RO-1"), None)

	def test_no_repair_order_is_not_checked(self):
		self._check(("", None), "SN-OTHER")
		self._check(None, "SN-OTHER")


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
			is_finding_mwo=0,
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

	def test_finding_work_order_is_refused_before_anything_is_created(self):
		fake = SimpleNamespace(
			name="MWO-F-1",
			manufacturing_order="PMO-1",
			serial_no="SN-1",
			item_code="F-CHAIN",
			docstatus=1,
			is_finding_mwo=1,
			find_existing_unpack_entry=lambda: None,
		)
		mock_db = MagicMock()
		mock_db.get_value.return_value = "Repair"
		new_doc = MagicMock()
		with patch(f"{MWO_MOD}.frappe.db", mock_db), patch(
			f"{MWO_MOD}.frappe.new_doc", new_doc
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				ManufacturingWorkOrder.create_unpack_serial_no_stock_entry(fake)
		self.assertIn("finding work order", str(ctx.exception))
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
			is_finding_mwo=0,
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
			is_finding_mwo=0,
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


