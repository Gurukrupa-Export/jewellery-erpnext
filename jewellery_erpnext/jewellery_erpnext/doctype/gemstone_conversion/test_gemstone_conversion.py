# Copyright (c) 2024, Nirali and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
	get_item_loss_item,
)
from jewellery_erpnext.utils import set_items_from_attribute


class TestGemstoneConversion(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_set_items_from_attribute_is_idempotent(self):
		"""When the loss/target variant already exists but get_variant() fails to
		match it (template has fewer attributes than the source item), the helper
		must return the existing Item instead of re-inserting it and raising
		DuplicateEntryError."""
		attributes = [
			{"item_attribute": "Gemstone Type", "attribute_value": "Synthetic"}
		]
		existing = frappe.get_doc({"doctype": "Item", "item_code": "GL-EXISTING-TEST"})

		created_variant = MagicMock()
		created_variant.item_code = "GL-EXISTING-TEST"
		created_variant.attributes = []

		with patch("jewellery_erpnext.utils.get_variant", return_value=None), patch(
			"jewellery_erpnext.utils.create_variant", return_value=created_variant
		), patch("frappe.db.exists", return_value="GL-EXISTING-TEST"), patch(
			"frappe.get_doc", return_value=existing
		):
			result = set_items_from_attribute("GL", attributes)

		self.assertIs(result, existing)
		created_variant.save.assert_not_called()

	def test_unconfigured_loss_type_raises_instead_of_source_item(self):
		"""When the selected loss type has no Variant Loss Table mapping for the
		variant, get_item_loss_item must raise a clear error rather than silently
		falling back to the source's own template (which resolves to the source
		item and trips 'Same Item should not allow')."""
		with patch("frappe.db.get_value", return_value=None) as mock_get_value:
			with self.assertRaises(frappe.ValidationError):
				get_item_loss_item(
					"Test Company",
					"G-BUS-SQR-SYN-AD-5.00*5.00 MM-FC-35-PC",
					"G",
					"Missing",
				)
		# It must fail at the Variant Loss Table lookup, never reaching item creation.
		mock_get_value.assert_called_once()

	def test_get_employee_conversion_cost_calculates_correctly(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import get_employee_conversion_cost
		issue_time = frappe.utils.now_datetime()
		receive_time = frappe.utils.add_to_date(issue_time, hours=2, minutes=30)
		with patch("frappe.db.get_value", return_value=100.0):
			cost = get_employee_conversion_cost("EMP-001", issue_time, receive_time)
			self.assertEqual(cost, 250.0)

	def test_get_employee_conversion_cost_missing_times_raises(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import get_employee_conversion_cost
		with self.assertRaises(frappe.ValidationError):
			get_employee_conversion_cost("EMP-001", None, frappe.utils.now_datetime())

	def test_get_employee_conversion_cost_missing_workstation_raises(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import get_employee_conversion_cost
		issue_time = frappe.utils.now_datetime()
		receive_time = frappe.utils.add_to_date(issue_time, hours=1)
		with patch("frappe.db.get_value", return_value=None):
			with self.assertRaises(frappe.ValidationError):
				get_employee_conversion_cost("EMP-001", issue_time, receive_time)

	def test_get_subcontracting_pi_amount_missing_pi_raises(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import get_subcontracting_pi_amount
		with patch("frappe.db.get_value", return_value=None):
			with self.assertRaises(frappe.ValidationError):
				get_subcontracting_pi_amount("PO-001")

	def test_get_subcontracting_pi_amount_returns_grand_total(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import get_subcontracting_pi_amount
		def mock_get_value(doctype, filters_or_name, fieldname):
			if doctype == "Purchase Invoice Item":
				return "PI-001"
			if doctype == "Purchase Invoice":
				return 5000.0
			return None

		with patch("frappe.db.get_value", side_effect=mock_get_value):
			amount = get_subcontracting_pi_amount("PO-001")
			self.assertEqual(amount, 5000.0)

	def test_get_source_batch_valuation_rate_empty_args_returns_zero(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import get_source_batch_valuation_rate
		self.assertEqual(get_source_batch_valuation_rate(None, "B-001", "WH-001"), 0)

	def test_get_source_batch_valuation_rate_bin_fallback(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import get_source_batch_valuation_rate
		def mock_get_value(doctype, filters, fieldname):
			if doctype == "Batch":
				return 0
			if doctype == "Bin":
				return 150.0

		with patch("frappe.db.get_value", side_effect=mock_get_value):
			rate = get_source_batch_valuation_rate("ITEM-001", "B-001", "WH-001")
			self.assertEqual(rate, 150.0)

	def test_get_source_batch_valuation_rate_batchwise(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import get_source_batch_valuation_rate
		def mock_get_value(doctype, name, fieldname):
			if doctype == "Batch":
				return 1
			return None

		mock_sql = MagicMock(return_value=[frappe._dict({"stock_value": 300.0, "qty": 2.0})])

		with patch("frappe.db.get_value", side_effect=mock_get_value), patch("frappe.db.sql", new=mock_sql):
			rate = get_source_batch_valuation_rate("ITEM-001", "B-001", "WH-001")
			self.assertEqual(rate, 150.0)

	def test_get_scrap_warehouse_returns_name(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import get_scrap_warehouse
		with patch("frappe.db.get_value", return_value="Scrap - WH"):
			self.assertEqual(get_scrap_warehouse("DEP-001"), "Scrap - WH")

	def test_get_scrap_warehouse_raises_when_not_found(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import get_scrap_warehouse
		with patch("frappe.db.get_value", return_value=None):
			with self.assertRaises(frappe.ValidationError):
				get_scrap_warehouse("DEP-001")

	def test_validate_gemstone_item_same_item_raises(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import validate_gemstone_item
		doc = frappe._dict(
			g_source_item="ITEM-001",
			g_loss_item="LOSS-001",
			sc_target_table=[frappe._dict(item_code="ITEM-001")]
		)
		mock_sql = MagicMock(return_value=[{"final_dict": '{"Gemstone Size": "5.00*5.00 MM"}'}])
		with patch("frappe.db.sql", new=mock_sql):
			with self.assertRaisesRegex(frappe.ValidationError, "Same Item should not allow"):
				validate_gemstone_item(doc)

	def test_validate_gemstone_item_skips_loss_item(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import validate_gemstone_item
		doc = frappe._dict(
			g_source_item="ITEM-001",
			g_loss_item="LOSS-001",
			sc_target_table=[frappe._dict(item_code="LOSS-001")]
		)
		mock_sql = MagicMock(return_value=[{"final_dict": '{"Gemstone Size": "5.00*5.00 MM"}'}])
		with patch("frappe.db.sql", new=mock_sql):
			validate_gemstone_item(doc)

	def test_validate_gemstone_item_attribute_mismatch_raises(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import validate_gemstone_item
		doc = frappe._dict(
			g_source_item="ITEM-001",
			g_loss_item="LOSS-001",
			sc_target_table=[frappe._dict(item_code="TARGET-001")]
		)
		def mock_sql_side_effect(query, args, **kwargs):
			if args[0] == "ITEM-001":
				return [{"final_dict": '{"Gemstone Size": "5.00*5.00 MM", "Color": "Red"}'}]
			elif args[0] == "TARGET-001":
				return [{"final_dict": '{"Gemstone Size": "5.00*5.00 MM"}'}]
		with patch("frappe.db.sql", side_effect=mock_sql_side_effect):
			with self.assertRaisesRegex(frappe.ValidationError, "Item Missmatch TARGET-001"):
				validate_gemstone_item(doc)

	def test_validate_gemstone_item_size_too_big_raises(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import validate_gemstone_item
		doc = frappe._dict(
			g_source_item="ITEM-001",
			g_loss_item="LOSS-001",
			sc_target_table=[frappe._dict(item_code="TARGET-001")]
		)
		def mock_sql_side_effect(query, args, **kwargs):
			if args[0] == "ITEM-001":
				return [{"final_dict": '{"Gemstone Size": "5.00*5.00 MM"}'}]
			elif args[0] == "TARGET-001":
				return [{"final_dict": '{"Gemstone Size": "6.00*6.00 MM"}'}]
		with patch("frappe.db.sql", side_effect=mock_sql_side_effect):
			with self.assertRaisesRegex(frappe.ValidationError, "Gemstone Size for this item TARGET-001 should not bigger than source item"):
				validate_gemstone_item(doc)

	def test_validate_gemstone_item_passes_valid(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import validate_gemstone_item
		doc = frappe._dict(
			g_source_item="ITEM-001",
			g_loss_item="LOSS-001",
			sc_target_table=[frappe._dict(item_code="TARGET-001")]
		)
		def mock_sql_side_effect(query, args, **kwargs):
			if args[0] == "ITEM-001":
				return [{"final_dict": '{"Gemstone Size": "5.00*5.00 MM", "Stone Shape": "Round", "Gemstone PR": "A", "Color": "Red"}'}]
			elif args[0] == "TARGET-001":
				return [{"final_dict": '{"Gemstone Size": "4.00*4.00 MM", "Stone Shape": "Square", "Gemstone PR": "B", "Color": "Red"}'}]
		with patch("frappe.db.sql", side_effect=mock_sql_side_effect):
			validate_gemstone_item(doc)

	def test_validate_gemstone_item_other_attr_mismatch_raises(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import validate_gemstone_item
		doc = frappe._dict(
			g_source_item="ITEM-001",
			g_loss_item="LOSS-001",
			sc_target_table=[frappe._dict(item_code="TARGET-001")]
		)
		def mock_sql_side_effect(query, args, **kwargs):
			if args[0] == "ITEM-001":
				return [{"final_dict": '{"Gemstone Size": "5.00*5.00 MM", "Color": "Red"}'}]
			elif args[0] == "TARGET-001":
				return [{"final_dict": '{"Gemstone Size": "5.00*5.00 MM", "Color": "Blue"}'}]
		with patch("frappe.db.sql", side_effect=mock_sql_side_effect):
			with self.assertRaisesRegex(frappe.ValidationError, "Color Missmatch for this item TARGET-001"):
				validate_gemstone_item(doc)

	def test_set_subcontractor_warehouse_not_resize(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import set_subcontractor_warehouse
		doc = frappe._dict(conversion_type="Conversion")
		set_subcontractor_warehouse(doc)
		self.assertNotIn("target_warehouse", doc)

	def test_set_subcontractor_warehouse_supplier_not_found(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import set_subcontractor_warehouse
		doc = frappe._dict(conversion_type="Resize", is_subcontracting=1, supplier="SUP-001")
		with patch("frappe.db.get_value", return_value=None):
			with self.assertRaisesRegex(frappe.ValidationError, "No Manufacturing Warehouse found for Subcontractor"):
				set_subcontractor_warehouse(doc)

	def test_set_subcontractor_warehouse_employee_not_found(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import set_subcontractor_warehouse
		doc = frappe._dict(conversion_type="Resize", is_subcontracting=0, to_employee="EMP-001")
		with patch("frappe.db.get_value", return_value=None):
			with self.assertRaisesRegex(frappe.ValidationError, "No Manufacturing Warehouse found for Employee"):
				set_subcontractor_warehouse(doc)

	def test_set_subcontractor_warehouse_sets_warehouse(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import set_subcontractor_warehouse
		doc = frappe._dict(conversion_type="Resize", is_subcontracting=1, supplier="SUP-001")
		with patch("frappe.db.get_value", return_value="SUP-WH"):
			set_subcontractor_warehouse(doc)
			self.assertEqual(doc.target_warehouse, "SUP-WH")

	def test_stamp_resize_conversion_time_issue_sets_time(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import stamp_resize_conversion_time
		doc = frappe._dict(workflow_state="Issue", issue_time=None, receive_time=None)
		stamp_resize_conversion_time(doc)
		self.assertIsNotNone(doc.issue_time)

	def test_stamp_resize_conversion_time_receive_checks_time_order(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import stamp_resize_conversion_time
		issue_time = frappe.utils.now_datetime()
		receive_time = frappe.utils.add_to_date(issue_time, hours=-1)
		doc = frappe._dict(workflow_state="Receive", issue_time=issue_time, receive_time=receive_time)
		with self.assertRaisesRegex(frappe.ValidationError, "Receive Time cannot be before Issue Time"):
			stamp_resize_conversion_time(doc)

	def test_stamp_resize_conversion_time_receive_calculates_employee_cost(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import stamp_resize_conversion_time
		issue_time = frappe.utils.now_datetime()
		receive_time = frappe.utils.add_to_date(issue_time, hours=1)
		doc = frappe._dict(workflow_state="Receive", issue_time=issue_time, receive_time=receive_time, is_subcontracting=0, to_employee="EMP-001")
		with patch("jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion.get_employee_conversion_cost", return_value=150.0):
			stamp_resize_conversion_time(doc)
			self.assertEqual(doc.conversion_cost, 150.0)

	def test_stamp_resize_conversion_time_receive_calculates_subcontracting_cost(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import stamp_resize_conversion_time
		issue_time = frappe.utils.now_datetime()
		receive_time = frappe.utils.add_to_date(issue_time, hours=1)
		doc = frappe._dict(workflow_state="Receive", issue_time=issue_time, receive_time=receive_time, is_subcontracting=1, fg_subcontracting_po="PO-001")
		with patch("jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion.get_subcontracting_pi_amount", return_value=500.0):
			stamp_resize_conversion_time(doc)
			self.assertEqual(doc.conversion_cost, 500.0)
