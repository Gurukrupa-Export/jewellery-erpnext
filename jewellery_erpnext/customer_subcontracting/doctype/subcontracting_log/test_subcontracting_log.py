# Copyright (c) 2026, Nirali and Contributors
# See license.txt

# import frappe
from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_log.subcontracting_log import (
	create_subcontracting_log,
)
from jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage import (
	classify_gold_usage,
	find_pending_settlements,
	get_inventory_data,
	get_mwo_type,
	get_order_customer,
	get_sales_order,
	is_gold_item,
	update_pending_settlement,
)

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class IntegrationTestSubcontractingLog(IntegrationTestCase):
	"""
	Integration tests for SubcontractingLog.
	Use this class for testing interactions between multiple components.
	"""

	@classmethod
	def setUpClass(cls):
		cls.doc = MagicMock()
		cls.doc.doctype = "Stock Entry"
		cls.doc.docstatus = 1
		cls.doc.name = "STE-001"

	def test_not_stock_entry(self):
		self.doc.doctype = "Sales Invoice"
		self.assertIsNone(create_subcontracting_log(self.doc))

	def test_not_submitted(self):
		self.doc.docstatus = 0
		self.assertIsNone(create_subcontracting_log(self.doc))

	def test_invalid_config(self):
		self.doc.stock_entry_type = "Manufacture"
		self.assertIsNone(create_subcontracting_log(self.doc))

	def test_item_no_batch(self):
		self.doc.stock_entry_type = "Customer Goods Received"
		item = MagicMock()
		item.batch_no = None
		self.doc.items = [item]
		self.assertIsNone(create_subcontracting_log(self.doc))

	@patch(
		"jewellery_erpnext.customer_subcontracting.doctype.subcontracting_log.subcontracting_log.get_inventory_data"
	)
	@patch("frappe.get_doc")
	def test_customer_goods_received(self, mock_get_doc, mock_get_inventory_data):
		self.doc.stock_entry_type = "Customer Goods Received"
		item = MagicMock()
		item.batch_no = "BATCH-001"
		self.doc.items = [item]

		mock_get_inventory_data.return_value = {"doctype": "Subcontracting Log"}
		mock_log = MagicMock()
		mock_get_doc.return_value = mock_log

		create_subcontracting_log(self.doc)

		mock_get_inventory_data.assert_called_once()
		mock_get_doc.assert_called_once_with({"doctype": "Subcontracting Log"})
		mock_log.insert.assert_called_once_with(ignore_permissions=True)


class TestGoldUsage(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_is_gold_item(self):
		self.assertTrue(is_gold_item("M-GOLD-001"))
		self.assertFalse(is_gold_item("S-SILVER-001"))

	@patch("frappe.db.get_value")
	def test_get_sales_order(self, mock_get_value):
		doc = MagicMock()

		# Test no manufacturing order
		doc.manufacturing_order = None
		self.assertIsNone(get_sales_order(doc))

		# Test with manufacturing order
		doc.manufacturing_order = "PMO-001"
		mock_get_value.return_value = "SO-001"
		self.assertEqual(get_sales_order(doc), "SO-001")
		mock_get_value.assert_called_once_with(
			"Parent Manufacturing Order", "PMO-001", "sales_order"
		)

	@patch(
		"jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage.get_sales_order"
	)
	@patch("frappe.db.get_value")
	def test_get_order_customer(self, mock_get_value, mock_get_sales_order):
		doc = MagicMock()

		# Test no sales order
		mock_get_sales_order.return_value = None
		self.assertIsNone(get_order_customer(doc))

		# Test with sales order
		mock_get_sales_order.return_value = "SO-001"
		mock_get_value.return_value = "CUST-001"
		self.assertEqual(get_order_customer(doc), "CUST-001")
		mock_get_value.assert_called_once_with("Sales Order", "SO-001", "customer")

	@patch("frappe.db.get_value")
	def test_get_mwo_type(self, mock_get_value):
		doc = MagicMock()

		# Test no manufacturing order
		doc.manufacturing_order = None
		self.assertEqual(get_mwo_type(doc), "Regular")

		# Test regular order (is_customer_gold = 0)
		doc.manufacturing_order = "PMO-001"
		mock_get_value.return_value = 0
		self.assertEqual(get_mwo_type(doc), "Regular")

		# Test subcontracting order (is_customer_gold = 1)
		mock_get_value.return_value = 1
		self.assertEqual(get_mwo_type(doc), "Subcontracting")

	@patch(
		"jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage.get_order_customer"
	)
	@patch(
		"jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage.get_mwo_type"
	)
	def test_classify_gold_usage_not_gold_item(
		self, mock_get_mwo_type, mock_get_order_customer
	):
		doc = MagicMock()
		item = MagicMock()
		item.item_code = "SILVER-001"
		item.inventory_type = "Company Goods"

		mock_get_order_customer.return_value = "CUST-001"
		mock_get_mwo_type.return_value = "Regular"

		result = classify_gold_usage(doc, item)
		self.assertEqual(result["usage_type"], "Company Gold")
		self.assertEqual(result["settlement_required"], 0)

	@patch(
		"jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage.get_order_customer"
	)
	@patch(
		"jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage.get_mwo_type"
	)
	def test_classify_gold_usage_case_1(
		self, mock_get_mwo_type, mock_get_order_customer
	):
		# Case 1: Subcontracting + Same Customer Gold
		doc = MagicMock()
		item = MagicMock()
		item.item_code = "M-GOLD-001"
		item.inventory_type = "Customer Goods"
		item.customer = "CUST-001"

		mock_get_order_customer.return_value = "CUST-001"
		mock_get_mwo_type.return_value = "Subcontracting"

		result = classify_gold_usage(doc, item)
		self.assertEqual(result["mwo_type"], "Subcontracting")
		self.assertEqual(result["usage_type"], "Same Customer Gold")
		self.assertEqual(result["settlement_required"], 0)

	@patch(
		"jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage.get_order_customer"
	)
	@patch(
		"jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage.get_mwo_type"
	)
	def test_classify_gold_usage_case_2(
		self, mock_get_mwo_type, mock_get_order_customer
	):
		# Case 2: Subcontracting + Different Customer Gold
		doc = MagicMock()
		item = MagicMock()
		item.item_code = "M-GOLD-001"
		item.inventory_type = "Customer Goods"
		item.customer = "CUST-002"

		mock_get_order_customer.return_value = "CUST-001"
		mock_get_mwo_type.return_value = "Subcontracting"

		result = classify_gold_usage(doc, item)
		self.assertEqual(result["usage_type"], "Different Customer Gold")
		self.assertEqual(result["settlement_required"], 1)
		self.assertEqual(result["settlement_type"], "Customer Needs Gold")
		self.assertEqual(result["settlement_customer"], "CUST-002")

	@patch(
		"jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage.get_order_customer"
	)
	@patch(
		"jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage.get_mwo_type"
	)
	def test_classify_gold_usage_case_3(
		self, mock_get_mwo_type, mock_get_order_customer
	):
		# Case 3: Subcontracting + Company Gold
		doc = MagicMock()
		item = MagicMock()
		item.item_code = "M-GOLD-001"
		item.inventory_type = "Company Goods"
		item.customer = None

		mock_get_order_customer.return_value = "CUST-001"
		mock_get_mwo_type.return_value = "Subcontracting"

		result = classify_gold_usage(doc, item)
		self.assertEqual(result["usage_type"], "Company Gold")
		self.assertEqual(result["settlement_required"], 1)
		self.assertEqual(result["settlement_type"], "Company Needs Gold")
		self.assertEqual(result["settlement_customer"], "CUST-001")

	@patch(
		"jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage.get_order_customer"
	)
	@patch(
		"jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage.get_mwo_type"
	)
	def test_classify_gold_usage_case_4(
		self, mock_get_mwo_type, mock_get_order_customer
	):
		# Case 4: Regular + Company Gold
		doc = MagicMock()
		item = MagicMock()
		item.item_code = "M-GOLD-001"
		item.inventory_type = "Company Goods"
		item.customer = None

		mock_get_order_customer.return_value = "CUST-001"
		mock_get_mwo_type.return_value = "Regular"

		result = classify_gold_usage(doc, item)
		self.assertEqual(result["usage_type"], "Company Gold")
		self.assertEqual(result["settlement_required"], 0)

	@patch(
		"jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage.get_order_customer"
	)
	@patch(
		"jewellery_erpnext.customer_subcontracting.sub_utils.gold_usage.get_mwo_type"
	)
	def test_classify_gold_usage_case_5(
		self, mock_get_mwo_type, mock_get_order_customer
	):
		# Case 5: Regular + Customer Gold
		doc = MagicMock()
		item = MagicMock()
		item.item_code = "M-GOLD-001"
		item.inventory_type = "Customer Goods"
		item.customer = "CUST-001"

		mock_get_order_customer.return_value = "CUST-002"
		mock_get_mwo_type.return_value = "Regular"

		result = classify_gold_usage(doc, item)
		self.assertEqual(result["usage_type"], "Different Customer Gold")
		self.assertEqual(result["settlement_required"], 1)
		self.assertEqual(result["settlement_type"], "Customer Needs Gold")
		self.assertEqual(result["settlement_customer"], "CUST-001")

	def test_get_inventory_data(self):
		doc = MagicMock()
		doc.doctype = "Stock Entry"
		doc.name = "STE-001"

		item = MagicMock()
		item.customer = "CUST-001"
		item.batch_no = "BATCH-001"
		item.item_code = "M-GOLD-001"
		item.qty = 10
		item.custom_pure_qty = 8
		item.inventory_type = "Customer Goods"
		item.s_warehouse = "Source"
		item.t_warehouse = "Target"

		config = {"transaction_type": "Test Transfer"}

		result = get_inventory_data(doc, item, config)
		self.assertEqual(result["reference_docname"], "STE-001")
		self.assertEqual(result["ownership"], "Customer Gold")
		self.assertEqual(result["transaction_type"], "Test Transfer")

	@patch("frappe.get_all")
	def test_find_pending_settlements_customer_gold(self, mock_get_all):
		item = MagicMock()
		item.inventory_type = "Customer Goods"
		item.customer = "CUST-001"

		find_pending_settlements(item)
		mock_get_all.assert_called_once()

		# Assert the right filters are applied
		kwargs = mock_get_all.call_args[1]
		self.assertEqual(kwargs["filters"]["settlement_required"], 1)
		self.assertEqual(kwargs["filters"]["settlement_type"], "Company Needs Gold")
		self.assertEqual(kwargs["filters"]["settlement_customer"], "CUST-001")

	@patch("frappe.get_all")
	def test_find_pending_settlements_company_gold(self, mock_get_all):
		item = MagicMock()
		item.inventory_type = "Company Goods"

		find_pending_settlements(item)
		mock_get_all.assert_called_once()

		kwargs = mock_get_all.call_args[1]
		self.assertEqual(kwargs["filters"]["settlement_type"], "Customer Needs Gold")

	@patch("frappe.get_doc")
	def test_update_pending_settlement(self, mock_get_doc):
		log = MagicMock()
		log.pending_pure_qty = 10
		log.settled_pure_qty = 2
		mock_get_doc.return_value = log

		# Update partially
		update_pending_settlement("LOG-001", 3, "REPACK-001", "BATCH-001")

		self.assertEqual(log.settled_pure_qty, 5)
		self.assertEqual(log.balance_pure_qty, 5)
		self.assertEqual(log.settlement_status, "Partially Settled")
		self.assertEqual(log.settled_by_repack, "REPACK-001")
		log.save.assert_called_with(ignore_permissions=True)

		# Update fully
		update_pending_settlement("LOG-001", 5, "REPACK-002", "BATCH-002")
		self.assertEqual(log.settled_pure_qty, 10)
		self.assertEqual(log.balance_pure_qty, 0)
		self.assertEqual(log.settlement_status, "Settled")
