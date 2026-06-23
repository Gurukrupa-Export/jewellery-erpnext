# Copyright (c) 2024, Nirali and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

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
