"""
Pure-logic unit tests for FG BOM Field Configuration.validate.

Run with:
  bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.doctype.fg_bom_field_configuration.test_fg_bom_field_configuration
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.fg_bom_field_configuration.fg_bom_field_configuration import (
	FGBOMFieldConfiguration,
)

MOD = "jewellery_erpnext.jewellery_erpnext.doctype.fg_bom_field_configuration.fg_bom_field_configuration"


def _row(idx, subcategory, field_name, fg_bom_field, is_active=1):
	return SimpleNamespace(
		idx=idx,
		subcategory=subcategory,
		field_name=field_name,
		field_label=field_name,
		fg_bom_field=fg_bom_field,
		is_active=is_active,
	)


class TestFGBOMFieldConfigurationValidate(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{MOD}.frappe.get_meta")
	def test_rejects_unknown_bom_field(self, mock_get_meta):
		meta = MagicMock()
		meta.has_field.side_effect = lambda f: f in ("length",)
		mock_get_meta.return_value = meta
		obj = SimpleNamespace(field_config=[_row(1, "Ring", "polish", "ghost_field")])
		with self.assertRaises(frappe.ValidationError):
			FGBOMFieldConfiguration.validate(obj)

	@patch(f"{MOD}.frappe.get_meta")
	def test_rejects_duplicate_field_name_in_subcategory(self, mock_get_meta):
		meta = MagicMock()
		meta.has_field.return_value = True
		mock_get_meta.return_value = meta
		obj = SimpleNamespace(
			field_config=[
				_row(1, "Ring", "polish", "length"),
				_row(2, "Ring", "polish", "width"),
			]
		)
		with self.assertRaises(frappe.ValidationError):
			FGBOMFieldConfiguration.validate(obj)

	@patch(f"{MOD}.frappe.get_meta")
	def test_accepts_same_field_name_across_subcategories(self, mock_get_meta):
		meta = MagicMock()
		meta.has_field.return_value = True
		mock_get_meta.return_value = meta
		obj = SimpleNamespace(
			field_config=[
				_row(1, "Ring", "polish", "length"),
				_row(2, "Bangle", "polish", "width"),
			]
		)
		# Should not raise.
		FGBOMFieldConfiguration.validate(obj)

	@patch(f"{MOD}.frappe.get_meta")
	def test_rejects_active_row_without_mapping(self, mock_get_meta):
		meta = MagicMock()
		# field_name is NOT a BOM field, so auto-map can't resolve a target.
		meta.has_field.return_value = False
		mock_get_meta.return_value = meta
		# Active field with no fg_bom_field -> captures a value that goes nowhere.
		obj = SimpleNamespace(field_config=[_row(1, "Ring", "polish", "", is_active=1)])
		with self.assertRaises(frappe.ValidationError):
			FGBOMFieldConfiguration.validate(obj)

	@patch(f"{MOD}.frappe.get_meta")
	def test_auto_maps_active_row_when_field_name_is_a_bom_field(self, mock_get_meta):
		meta = MagicMock()
		# field_name matches a real BOM field -> auto-mapped, no throw.
		meta.has_field.return_value = True
		mock_get_meta.return_value = meta
		row = _row(1, "Ring", "width", "", is_active=1)
		obj = SimpleNamespace(field_config=[row])
		FGBOMFieldConfiguration.validate(obj)
		self.assertEqual(row.fg_bom_field, "width")

	@patch(f"{MOD}.frappe.get_meta")
	def test_allows_inactive_row_without_mapping(self, mock_get_meta):
		meta = MagicMock()
		# Even though the name matches a BOM field, inactive rows are left untouched.
		meta.has_field.return_value = True
		mock_get_meta.return_value = meta
		row = _row(1, "Ring", "polish", "", is_active=0)
		obj = SimpleNamespace(field_config=[row])
		FGBOMFieldConfiguration.validate(obj)
		self.assertFalse(row.fg_bom_field)
