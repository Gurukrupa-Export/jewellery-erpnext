# Copyright (c) 2026, Aerele and Contributors
# See license.txt
"""Regression guard for the Sales Order precision-override custom fields.

Two Check fields (`custom_precision`, `custom_precision_for_stone`) are read by
`_get_bom_context` during every Sales Order save. They were declared only in the
app's `custom_fields/sales_order.json` -- dead config here (after_migrate hook
disabled, install-app marks patches done without running) -- so on un-provisioned
sites the Sales Order document had no such attribute and `create_new_bom1` crashed
with `AttributeError: 'SalesOrder' object has no attribute 'custom_precision'`.

This suite fails loudly if either the provisioning patch stops creating the fields
or `_get_bom_context` regresses on the "checkbox off -> default 3, on -> 2" contract.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events.sales_order import _get_bom_context
from jewellery_erpnext.patches.add_sales_order_precision_fields import (
	execute as provision_precision_fields,
)

_SO_MODULE = "jewellery_erpnext.jewellery_erpnext.doc_events.sales_order"


class TestSalesOrderPrecisionFields(IntegrationTestCase):
	def test_patch_provisions_precision_fields(self):
		# Idempotent: a no-op once the fields already exist.
		provision_precision_fields()

		meta = frappe.get_meta("Sales Order")
		for fieldname in ("custom_precision", "custom_precision_for_stone"):
			self.assertTrue(
				meta.has_field(fieldname),
				f"Sales Order.{fieldname} is missing -- _get_bom_context would raise "
				"AttributeError on every Sales Order save",
			)
			self.assertTrue(
				frappe.db.has_column("Sales Order", fieldname),
				f"Sales Order.{fieldname} column is missing on the DB",
			)

	def test_bom_context_defaults_to_3_when_override_off(self):
		# Customer precision fields all unset (None) and no SO override checkbox.
		with patch(
			f"{_SO_MODULE}.frappe.db.get_single_value", MagicMock(return_value=3)
		), patch(
			f"{_SO_MODULE}.frappe.db.get_value",
			MagicMock(return_value=("Internal", 3, None, None, None, None)),
		):
			ctx = _get_bom_context(frappe._dict(customer="Test"))

		# .get("custom_precision") is falsy -> Customer value (None) -> default 3
		self.assertEqual(ctx.metal_precision, 3)
		self.assertEqual(ctx.stone_precision, 3)

	def test_bom_context_honours_customer_value_when_override_off(self):
		with patch(
			f"{_SO_MODULE}.frappe.db.get_single_value", MagicMock(return_value=3)
		), patch(
			f"{_SO_MODULE}.frappe.db.get_value",
			MagicMock(return_value=("Internal", 4, 5, 6, None, None)),
		):
			ctx = _get_bom_context(frappe._dict(customer="Test"))

		# Override off -> the Customer's configured metal/stone precision wins.
		self.assertEqual(ctx.metal_precision, 5)
		self.assertEqual(ctx.stone_precision, 6)

	def test_bom_context_override_collapses_to_2(self):
		so = frappe._dict(
			customer="Test", custom_precision=1, custom_precision_for_stone=1
		)
		with patch(
			f"{_SO_MODULE}.frappe.db.get_single_value", MagicMock(return_value=3)
		), patch(
			f"{_SO_MODULE}.frappe.db.get_value",
			MagicMock(return_value=("Internal", 4, 5, 6, None, None)),
		):
			ctx = _get_bom_context(so)

		# Checkbox on -> forced 2 regardless of the Customer value.
		self.assertEqual(ctx.metal_precision, 2)
		self.assertEqual(ctx.stone_precision, 2)

	def test_bom_context_does_not_raise_when_field_absent(self):
		# A bare doc with no custom_precision attribute must not crash the read.
		with patch(
			f"{_SO_MODULE}.frappe.db.get_single_value", MagicMock(return_value=3)
		), patch(
			f"{_SO_MODULE}.frappe.db.get_value",
			MagicMock(return_value=("Internal", 3, None, None, None, None)),
		):
			try:
				ctx = _get_bom_context(frappe._dict(customer="Test"))
			except AttributeError as e:
				self.fail(f"_get_bom_context raised on absent override field: {e}")

		self.assertEqual(ctx.metal_precision, 3)
