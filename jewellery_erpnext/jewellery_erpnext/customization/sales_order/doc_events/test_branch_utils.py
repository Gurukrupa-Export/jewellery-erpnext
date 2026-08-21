# Copyright (c) 2026, Nirali and Contributors
# See license.txt
"""Unit tests for the company/customer address swap in ``branch_utils.create_so``.

Regression coverage for the bug where the auto-generated branch-mirror Sales Order
kept the originating branch's own ``company_address``/``customer_address`` unchanged
(carried verbatim by ``frappe.copy_doc``), so India Compliance derived an identical
``company_gstin``/``billing_address_gstin`` and threw "Cannot charge GST ... since
Company GSTIN and Party GSTIN are same" on save.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.customization.sales_order.doc_events.branch_utils import (
	create_so,
)

_MOD = "jewellery_erpnext.jewellery_erpnext.customization.sales_order.doc_events.branch_utils"


class TestCreateSo(FrappeTestCase):
	@patch(f"{_MOD}.frappe.copy_doc")
	@patch(f"{_MOD}.frappe.db.get_value")
	def test_swaps_company_and_customer_address(self, mock_get_value, mock_copy_doc):
		mock_get_value.side_effect = lambda doctype, name, field: {
			("Branch", "Central", "branch_address"): "Addr-Central",
			("Branch", "Surat", "branch_address"): "Addr-Surat",
		}[(doctype, name, field)]

		doc = MagicMock()
		mock_copy_doc.return_value = doc

		source = MagicMock()
		source.company = "Test Company"
		source.branch = "Surat"

		result = create_so(source, "Surat Customer", "Central")

		self.assertEqual(doc.company_address, "Addr-Central")
		self.assertEqual(doc.customer_address, "Addr-Surat")
		self.assertEqual(doc.customer, "Surat Customer")
		self.assertEqual(doc.branch, "Central")
		doc.save.assert_called_once()
		doc.submit.assert_called_once()
		self.assertEqual(result, doc.name)

	@patch(f"{_MOD}.frappe.copy_doc")
	@patch(f"{_MOD}.frappe.db.get_value")
	def test_throws_when_branch_address_missing(self, mock_get_value, mock_copy_doc):
		mock_get_value.side_effect = lambda doctype, name, field: {
			("Branch", "Central", "branch_address"): None,
			("Branch", "Surat", "branch_address"): "Addr-Surat",
		}[(doctype, name, field)]

		source = MagicMock()
		source.company = "Test Company"
		source.branch = "Surat"

		with self.assertRaises(frappe.ValidationError):
			create_so(source, "Surat Customer", "Central")

		mock_copy_doc.assert_not_called()
