from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import purchase_invoice as pi_events


class DummyPurchaseInvoice:
	def __init__(self, **kwargs):
		self.name = kwargs.get("name", "PI-1")
		self.company = kwargs.get("company", "Test Company")
		self.purchase_type = kwargs.get("purchase_type", "Finished Goods")
		self.is_opening = kwargs.get("is_opening", "No")
		self.items = kwargs.get("items", [])
		self.docstatus = kwargs.get("docstatus", 0)
		self.is_return = kwargs.get("is_return", 0)
		for k, v in kwargs.items():
			setattr(self, k, v)

	def get(self, key, default=None):
		return getattr(self, key, default)


class TestPurchaseInvoiceEvents(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.update_rate_from_sales_invoice"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.update_expense_account"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice._get_job_work_purchase_orders",
		return_value={"PO-1"},
	)
	def test_before_validate_job_work(
		self, _mock_pos, mock_update_expense_account, mock_rate
	):
		"""A job-work (Employee IR) invoice gets the expense account and KGGK's amount."""
		pi = DummyPurchaseInvoice()
		pi_events.before_validate(pi)
		mock_update_expense_account.assert_called_once_with(pi)
		mock_rate.assert_called_once_with(pi, {"PO-1"})

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.update_expense_account"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice._get_job_work_purchase_orders",
		return_value=set(),
	)
	def test_before_validate_other_invoice_untouched(
		self, _mock_pos, mock_update_expense_account
	):
		"""Any other Purchase Invoice is left exactly as before (the hook is new)."""
		pi = DummyPurchaseInvoice()
		pi_events.before_validate(pi)
		mock_update_expense_account.assert_not_called()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.db.get_value"
	)
	def test_update_expense_account_success(self, mock_get_value):
		pi = DummyPurchaseInvoice(
			is_opening="No",
			company="Test Company",
			purchase_type="Test Type",
			items=[
				frappe._dict(item_code="Item1", expense_account="Old Account"),
				frappe._dict(item_code="Item2", expense_account="Old Account 2"),
			],
		)
		mock_get_value.return_value = "New Expense Account"

		pi_events.update_expense_account(pi)

		mock_get_value.assert_called_once_with(
			"Account",
			{"company": "Test Company", "custom_purchase_type": "Test Type"},
			"name",
		)
		self.assertEqual(pi.items[0].expense_account, "New Expense Account")
		self.assertEqual(pi.items[1].expense_account, "New Expense Account")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.db.get_value"
	)
	def test_update_expense_account_is_opening_yes(self, mock_get_value):
		pi = DummyPurchaseInvoice(
			is_opening="Yes",
			items=[frappe._dict(item_code="Item1", expense_account="Old Account")],
		)

		pi_events.update_expense_account(pi)

		mock_get_value.assert_not_called()
		self.assertEqual(pi.items[0].expense_account, "Old Account")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.db.get_value"
	)
	def test_update_expense_account_not_found(self, mock_get_value):
		pi = DummyPurchaseInvoice(
			is_opening="No",
			items=[frappe._dict(item_code="Item1", expense_account="Old Account")],
		)
		mock_get_value.return_value = None

		pi_events.update_expense_account(pi)

		mock_get_value.assert_called_once()
		self.assertEqual(pi.items[0].expense_account, "Old Account")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.db.get_value"
	)
	def test_update_expense_account_is_opening_other(self, mock_get_value):
		pi = DummyPurchaseInvoice(
			is_opening="Unknown",
			items=[frappe._dict(item_code="Item1", expense_account="Old Account")],
		)

		pi_events.update_expense_account(pi)

		mock_get_value.assert_not_called()
		self.assertEqual(pi.items[0].expense_account, "Old Account")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.db.get_value"
	)
	def test_update_expense_account_empty_items(self, mock_get_value):
		pi = DummyPurchaseInvoice(
			is_opening="No", company="Test Company", purchase_type="Test Type", items=[]
		)
		mock_get_value.return_value = "New Expense Account"

		pi_events.update_expense_account(pi)

		mock_get_value.assert_called_once_with(
			"Account",
			{"company": "Test Company", "custom_purchase_type": "Test Type"},
			"name",
		)
		self.assertEqual(len(pi.items), 0)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.db.get_value"
	)
	def test_update_expense_account_missing_attributes(self, mock_get_value):
		pi = DummyPurchaseInvoice(
			is_opening="No",
			company=None,
			purchase_type=None,
			items=[frappe._dict(item_code="Item1", expense_account="Old Account")],
		)
		mock_get_value.return_value = None

		pi_events.update_expense_account(pi)

		mock_get_value.assert_called_once_with(
			"Account", {"company": None, "custom_purchase_type": None}, "name"
		)
		self.assertEqual(pi.items[0].expense_account, "Old Account")
