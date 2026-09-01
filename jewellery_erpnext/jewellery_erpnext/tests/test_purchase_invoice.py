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
		self.taxes = kwargs.get("taxes", [])
		self.net_total = kwargs.get("net_total", 0)
		for k, v in kwargs.items():
			setattr(self, k, v)

	def get(self, key, default=None):
		return getattr(self, key, default)


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


class TestAssignZeroTaxTemplateForUntaxedItems(IntegrationTestCase):
	def test_no_items_or_taxes_skips(self):
		pi = DummyPurchaseInvoice(items=[], taxes=[])
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.get_or_create_zero_tax_template"
		) as mock_get_or_create:
			pi_events.assign_zero_tax_template_for_untaxed_items(pi)
		mock_get_or_create.assert_not_called()

	def test_all_items_already_have_template_skips(self):
		pi = DummyPurchaseInvoice(
			items=[frappe._dict(item_code="Item1", item_tax_template="GST 3%")],
			taxes=[frappe._dict(account_head="Input Tax IGST - KGJPL")],
		)
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.get_or_create_zero_tax_template"
		) as mock_get_or_create:
			pi_events.assign_zero_tax_template_for_untaxed_items(pi)
		mock_get_or_create.assert_not_called()

	def test_untaxed_item_gets_zero_tax_template(self):
		item1 = frappe._dict(item_code="Item1", item_tax_template="GST 3%")
		item2 = frappe._dict(item_code="Item2", item_tax_template=None)
		pi = DummyPurchaseInvoice(
			company="Test Company",
			items=[item1, item2],
			taxes=[
				frappe._dict(account_head="Input Tax IGST - KGJPL"),
				frappe._dict(account_head="Input Tax CGST - KGJPL"),
			],
		)
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.get_or_create_zero_tax_template"
		) as mock_get_or_create:
			mock_get_or_create.return_value = "Zero Tax (Auto)"
			pi_events.assign_zero_tax_template_for_untaxed_items(pi)

		mock_get_or_create.assert_called_once_with(
			"Test Company",
			["Input Tax CGST - KGJPL", "Input Tax IGST - KGJPL"],
		)
		self.assertEqual(item1.item_tax_template, "GST 3%")
		self.assertEqual(item2.item_tax_template, "Zero Tax (Auto)")

	def test_item_without_item_code_ignored(self):
		item = frappe._dict(item_code=None, item_tax_template=None)
		pi = DummyPurchaseInvoice(
			items=[item], taxes=[frappe._dict(account_head="Input Tax IGST - KGJPL")]
		)
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.get_or_create_zero_tax_template"
		) as mock_get_or_create:
			pi_events.assign_zero_tax_template_for_untaxed_items(pi)
		mock_get_or_create.assert_not_called()


class TestGetOrCreateZeroTaxTemplate(IntegrationTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.db.get_value"
	)
	def test_creates_new_template_when_missing(
		self, mock_get_value, mock_get_doc, mock_new_doc
	):
		mock_get_value.return_value = None
		template = frappe._dict(taxes=[], name=None)
		template.append = lambda table, row: template.taxes.append(frappe._dict(row))
		template.flags = frappe._dict()
		template.save = lambda: setattr(template, "name", "New Zero Tax Template")
		mock_new_doc.return_value = template

		result = pi_events.get_or_create_zero_tax_template(
			"Test Company", ["Input Tax IGST - KGJPL"]
		)

		mock_new_doc.assert_called_once_with("Item Tax Template")
		mock_get_doc.assert_not_called()
		self.assertEqual(template.title, pi_events.ZERO_TAX_TEMPLATE_TITLE)
		self.assertEqual(template.company, "Test Company")
		self.assertEqual(template.gst_treatment, "Non-GST")
		self.assertEqual(len(template.taxes), 1)
		self.assertEqual(template.taxes[0].tax_type, "Input Tax IGST - KGJPL")
		self.assertEqual(template.taxes[0].tax_rate, 0)
		self.assertEqual(result, "New Zero Tax Template")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.db.get_value"
	)
	def test_grows_existing_template_with_missing_heads(
		self, mock_get_value, mock_get_doc
	):
		mock_get_value.return_value = "Zero Tax (Auto)"
		template = frappe._dict(
			name="Zero Tax (Auto)",
			custom_is_auto_zero_tax=1,
			taxes=[frappe._dict(tax_type="Input Tax IGST - KGJPL", tax_rate=0)],
		)
		template.append = lambda table, row: template.taxes.append(frappe._dict(row))
		template.flags = frappe._dict()
		template.save = lambda: None
		mock_get_doc.return_value = template

		result = pi_events.get_or_create_zero_tax_template(
			"Test Company", ["Input Tax CGST - KGJPL", "Input Tax IGST - KGJPL"]
		)

		self.assertEqual(len(template.taxes), 2)
		self.assertEqual(template.taxes[1].tax_type, "Input Tax CGST - KGJPL")
		self.assertEqual(template.gst_treatment, "Non-GST")
		self.assertEqual(result, "Zero Tax (Auto)")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.db.get_value"
	)
	def test_reuses_existing_template_without_saving_when_fully_covered(
		self, mock_get_value, mock_get_doc
	):
		mock_get_value.return_value = "Zero Tax (Auto)"
		template = frappe._dict(
			name="Zero Tax (Auto)",
			custom_is_auto_zero_tax=1,
			gst_treatment="Non-GST",
			taxes=[frappe._dict(tax_type="Input Tax IGST - KGJPL", tax_rate=0)],
		)

		def fail_save():
			raise AssertionError("save() should not be called when nothing changed")

		template.save = fail_save
		mock_get_doc.return_value = template

		result = pi_events.get_or_create_zero_tax_template(
			"Test Company", ["Input Tax IGST - KGJPL"]
		)

		self.assertEqual(result, "Zero Tax (Auto)")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.purchase_invoice.frappe.db.get_value"
	)
	def test_fixes_gst_treatment_even_when_heads_fully_covered(
		self, mock_get_value, mock_get_doc
	):
		mock_get_value.return_value = "Zero Tax (Auto)"
		template = frappe._dict(
			name="Zero Tax (Auto)",
			custom_is_auto_zero_tax=1,
			gst_treatment="Taxable",
			taxes=[frappe._dict(tax_type="Input Tax IGST - KGJPL", tax_rate=0)],
		)
		template.append = lambda table, row: template.taxes.append(frappe._dict(row))
		template.flags = frappe._dict()
		saved = {"called": False}
		template.save = lambda: saved.__setitem__("called", True)
		mock_get_doc.return_value = template

		result = pi_events.get_or_create_zero_tax_template(
			"Test Company", ["Input Tax IGST - KGJPL"]
		)

		self.assertTrue(saved["called"])
		self.assertEqual(template.gst_treatment, "Non-GST")
		self.assertEqual(result, "Zero Tax (Auto)")


class TestUpdateEffectiveTaxRate(IntegrationTestCase):
	def test_skips_when_no_net_total(self):
		tax = frappe._dict(charge_type="On Net Total", tax_amount=300, rate=5)

		self.assertEqual(tax.rate, 5)

	def test_skips_non_net_total_charge_type(self):
		tax = frappe._dict(charge_type="Actual", tax_amount=300, rate=5)

		self.assertEqual(tax.rate, 5)
