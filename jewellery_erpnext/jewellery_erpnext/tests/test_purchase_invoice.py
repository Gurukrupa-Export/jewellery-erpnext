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
		for k, v in kwargs.items():
			setattr(self, k, v)


class TestPurchaseInvoiceEvents(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.update_expense_account"
	)
	def test_before_validate(self, mock_update_expense_account):
		pi = DummyPurchaseInvoice()
		pi_events.before_validate(pi)
		mock_update_expense_account.assert_called_once_with(pi)

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
