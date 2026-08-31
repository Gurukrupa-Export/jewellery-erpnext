# Copyright (c) 2024, Nirali and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import (
	validate_gemstone_item,
)
from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
	get_item_loss_item,
)
from jewellery_erpnext.utils import set_items_from_attribute

GEMSTONE_CONVERSION_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion"

SOURCE_ITEM = "G-GRS-PR-SEP-X-6.00*4.00 MM-FC-60-PC"

# The eight attributes carried by a real gemstone variant, taken from SOURCE_ITEM.
SOURCE_ATTRIBUTES = {
	"Gemstone Type": "Green Stone",
	"Stone Shape": "Pear",
	"Gemstone Quality": "Semi-Precious",
	"Gemstone Grade": "Onyx",
	"Gemstone Size": "6.00*4.00 MM",
	"Cut or Cab": "Faceted",
	"Gemstone PR": "60",
	"Per Pc or Per Carat": "Per Carat",
}


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


class TestGemstoneItemAttributeValidation(FrappeTestCase):
	"""validate_gemstone_item: every attribute must match except Gemstone PR, which
	must move by exactly one or two 5-point notches."""

	def _target(self, item_code, **overrides):
		"""A target item identical to the source except for the given attributes."""
		attributes = dict(SOURCE_ATTRIBUTES)
		for attribute, value in overrides.items():
			if value is None:
				attributes.pop(attribute, None)
			else:
				attributes[attribute] = value
		return item_code, attributes

	def _validate(self, targets, loss_item=None):
		"""Run the validator over `targets` (a list of (item_code, attributes))."""
		attribute_map = {SOURCE_ITEM: dict(SOURCE_ATTRIBUTES)}
		attribute_map.update(dict(targets))

		doc = frappe._dict(
			g_source_item=SOURCE_ITEM,
			g_loss_item=loss_item,
			sc_target_table=[
				frappe._dict(item_code=item_code) for item_code, _attrs in targets
			],
		)

		with patch(
			f"{GEMSTONE_CONVERSION_MODULE}._get_item_attributes",
			side_effect=lambda item_code: attribute_map[item_code],
		):
			validate_gemstone_item(doc)

	def test_accepts_each_allowed_pr_delta(self):
		"""Source PR 60 converts to 50, 55, 65 and 70."""
		for delta in (-10, -5, 5, 10):
			pr = 60 + delta
			with self.subTest(gemstone_pr=pr):
				self._validate(
					[self._target(f"G-TARGET-{pr}", **{"Gemstone PR": str(pr)})]
				)

	def test_rejects_unchanged_pr(self):
		"""A zero delta is not a conversion, even on a differently-named item."""
		with self.assertRaises(frappe.ValidationError) as cm:
			self._validate([self._target("G-TARGET-60")])
		self.assertIn("Gemstone PR", str(cm.exception))

	def test_rejects_pr_delta_off_the_notch(self):
		"""63 is within +-10 of 60 but is not a 5-point notch."""
		with self.assertRaises(frappe.ValidationError):
			self._validate([self._target("G-TARGET-63", **{"Gemstone PR": "63"})])

	def test_rejects_pr_delta_beyond_two_notches(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate([self._target("G-TARGET-80", **{"Gemstone PR": "80"})])

	def test_rejects_gemstone_size_mismatch(self):
		"""The old validator allowed a smaller target size; it must not any more."""
		with self.assertRaises(frappe.ValidationError) as cm:
			self._validate(
				[
					self._target(
						"G-TARGET-SMALL",
						**{"Gemstone PR": "65", "Gemstone Size": "5.00*3.00 MM"},
					)
				]
			)
		self.assertIn("Gemstone Size", str(cm.exception))

	def test_rejects_stone_shape_mismatch(self):
		"""The old validator exempted Stone Shape; it must not any more."""
		with self.assertRaises(frappe.ValidationError) as cm:
			self._validate(
				[
					self._target(
						"G-TARGET-ROUND",
						**{"Gemstone PR": "65", "Stone Shape": "Round"},
					)
				]
			)
		self.assertIn("Stone Shape", str(cm.exception))

	def test_rejects_attribute_present_on_only_one_item(self):
		"""A target missing an attribute the source has is a mismatch, not a pass."""
		with self.assertRaises(frappe.ValidationError) as cm:
			self._validate(
				[
					self._target(
						"G-TARGET-NO-CUT",
						**{"Gemstone PR": "65", "Cut or Cab": None},
					)
				]
			)
		self.assertIn("Cut or Cab", str(cm.exception))

	def test_rejects_missing_gemstone_pr(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._validate([self._target("G-TARGET-NO-PR", **{"Gemstone PR": None})])
		self.assertIn("Gemstone PR", str(cm.exception))

	def test_skips_loss_item_row(self):
		"""The auto-appended loss row shares no attributes and must be exempt."""
		self._validate(
			[
				self._target("G-TARGET-65", **{"Gemstone PR": "65"}),
				("G-LOSS-ITEM", {"Gemstone Type": "Loss"}),
			],
			loss_item="G-LOSS-ITEM",
		)

	def test_rejects_target_equal_to_source(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._validate([(SOURCE_ITEM, dict(SOURCE_ATTRIBUTES))])
		self.assertIn("Same Item", str(cm.exception))
