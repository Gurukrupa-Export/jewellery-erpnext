# Copyright (c) 2024, Nirali and Contributors
# See license.txt

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion import (
	gemstone_conversion as gc,
)
from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
	get_item_loss_item,
)
from jewellery_erpnext.utils import set_items_from_attribute


class TestGemstoneConversion(FrappeTestCase):
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


GC = "jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion"


def _attrs(size, pr="35"):
	return {"Gemstone Type": "Synthetic", "Gemstone Size": size, "Gemstone PR": pr}


class TestGemstoneSizeValue(FrappeTestCase):
	"""Gemstone Size is parsed as numbers; it used to be passed to eval()."""

	def test_single_and_multi_dimension_sizes(self):
		self.assertEqual(gc._gemstone_size_value("3.30 MM", "G-1"), 3.3)
		self.assertEqual(gc._gemstone_size_value("4.00*3.00 MM", "G-1"), 12.0)
		self.assertEqual(gc._gemstone_size_value("15.00*13.00 MM", "G-1"), 195.0)

	def test_code_in_size_is_rejected_not_run(self):
		for size in ("__import__('os').getcwd() MM", "1+1 MM", "4.00x3.00 MM"):
			with self.subTest(size=size):
				with patch("builtins.eval") as mock_eval:
					with self.assertRaises(frappe.ValidationError):
						gc._gemstone_size_value(size, "G-1")
				mock_eval.assert_not_called()

	def test_missing_size_throws_clear_error(self):
		for size in (None, ""):
			with self.subTest(size=size):
				with self.assertRaises(frappe.ValidationError):
					gc._gemstone_size_value(size, "G-1")


class TestValidateGemstoneItemSize(FrappeTestCase):
	def _run(self, source_size, target_size):
		doc = SimpleNamespace(
			g_source_item="G-SRC",
			g_loss_item="G-LOSS",
			sc_target_table=[SimpleNamespace(item_code="G-TGT")],
		)
		by_item = {"G-SRC": _attrs(source_size), "G-TGT": _attrs(target_size)}

		def _sql(query, values, as_dict=False):
			return [{"final_dict": json.dumps(by_item[values[0]])}]

		with patch(f"{GC}.frappe.db.sql", side_effect=_sql), patch(
			f"{GC}._get_gemstone_pr_neighbours", return_value=[35.0]
		):
			gc.validate_gemstone_item(doc)

	def test_smaller_target_passes(self):
		self._run("5.00*5.00 MM", "4.00*5.00 MM")

	def test_bigger_target_throws(self):
		with self.assertRaises(frappe.ValidationError):
			self._run("5.00*5.00 MM", "6.00*5.00 MM")

	def test_invalid_target_size_throws(self):
		with self.assertRaises(frappe.ValidationError):
			self._run("5.00*5.00 MM", "__import__('os').getcwd() MM")

	def test_target_without_attributes_is_a_clear_mismatch(self):
		# JSON_OBJECTAGG returns NULL for an item with no variant attributes; that used
		# to reach json.loads(None) and crash with a TypeError.
		doc = SimpleNamespace(
			g_source_item="G-SRC",
			g_loss_item="G-LOSS",
			sc_target_table=[SimpleNamespace(item_code="G-TGT")],
		)

		def _sql(query, values, as_dict=False):
			if values[0] == "G-SRC":
				return [{"final_dict": json.dumps(_attrs("5.00*5.00 MM"))}]
			return [{"final_dict": None}]

		with patch(f"{GC}.frappe.db.sql", side_effect=_sql), patch(
			f"{GC}._get_gemstone_pr_neighbours", return_value=[35.0]
		):
			with self.assertRaises(frappe.ValidationError):
				gc.validate_gemstone_item(doc)


class TestGetItemAttributes(FrappeTestCase):
	def test_parses_attributes(self):
		with patch(
			f"{GC}.frappe.db.sql",
			return_value=[{"final_dict": json.dumps({"Gemstone PR": "35"})}],
		):
			self.assertEqual(gc._get_item_attributes("G-1"), {"Gemstone PR": "35"})

	def test_item_without_attributes_returns_empty(self):
		for rows in ([{"final_dict": None}], []):
			with self.subTest(rows=rows):
				with patch(f"{GC}.frappe.db.sql", return_value=rows):
					self.assertEqual(gc._get_item_attributes("G-1"), {})


class TestGemstoneConversionEndpointPermission(FrappeTestCase):
	"""Whitelisted endpoints require write access on Gemstone Conversion."""

	def _as_guest(self, fn):
		user = frappe.session.user
		frappe.set_user("Guest")
		try:
			fn()
		finally:
			frappe.set_user(user)

	def test_get_loss_item_denied_without_permission(self):
		with patch(f"{GC}.get_item_loss_item") as mock_loss_item:
			with self.assertRaises(frappe.PermissionError):
				self._as_guest(lambda: gc.get_loss_item("Test Company", "G-SRC"))
		mock_loss_item.assert_not_called()

	def test_get_loss_item_allowed_with_permission(self):
		with patch(f"{GC}.get_item_loss_item", return_value="G-LOSS") as mock_loss_item:
			self.assertEqual(gc.get_loss_item("Test Company", "G-SRC", "L"), "G-LOSS")
		mock_loss_item.assert_called_once_with("Test Company", "G-SRC", "G", "L")

	def test_doc_methods_denied_without_permission(self):
		doc = SimpleNamespace(employee="EMP-1", batch="B-1", company="Test Company")
		for method in (
			gc.GemstoneConversion.get_detail_tab_value,
			gc.GemstoneConversion.get_batch_detail,
		):
			with self.subTest(method=method.__name__):
				with patch(f"{GC}.frappe.get_value") as mock_get_value:
					with self.assertRaises(frappe.PermissionError):
						self._as_guest(lambda: method(doc))
				mock_get_value.assert_not_called()
