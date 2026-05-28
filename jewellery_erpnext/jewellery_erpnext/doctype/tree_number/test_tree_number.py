# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

_TN_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.tree_number.tree_number"


class TestTreeNumberMWOUniqueness(FrappeTestCase):
	"""M4/CAST-6: Only one Tree Number per MWO."""

	def _make_tn(self, name=None, mwo=None):
		doc = MagicMock()
		doc.name = name
		doc.manufacturing_work_order = mwo
		return doc

	@patch(f"{_TN_MODULE}.frappe.db.get_value", return_value=None)
	def test_passes_when_no_mwo(self, _gv):
		from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.tree_number import (
			TreeNumber,
		)

		doc = self._make_tn(name="TN-001", mwo=None)
		TreeNumber.validate(doc)  # must not raise
		_gv.assert_not_called()

	@patch(f"{_TN_MODULE}.frappe.db.get_value", return_value=None)
	def test_passes_when_no_duplicate(self, _gv):
		from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.tree_number import (
			TreeNumber,
		)

		doc = self._make_tn(name="TN-001", mwo="MWO-001")
		TreeNumber.validate(doc)  # must not raise

	@patch(f"{_TN_MODULE}.frappe.db.get_value", return_value="TN-999")
	def test_throws_when_duplicate_mwo(self, _gv):
		from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.tree_number import (
			TreeNumber,
		)

		doc = self._make_tn(name="TN-001", mwo="MWO-001")
		with self.assertRaises(frappe.ValidationError):
			TreeNumber.validate(doc)

	@patch(f"{_TN_MODULE}.frappe.db.get_value", return_value=None)
	def test_passes_when_mwo_is_same_tree_number(self, _gv):
		"""Editing an existing Tree Number with the same MWO should not self-conflict."""
		from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.tree_number import (
			TreeNumber,
		)

		doc = self._make_tn(name="TN-001", mwo="MWO-001")
		# DB returns None (no OTHER doc with same MWO), so editing own record is fine
		TreeNumber.validate(doc)  # must not raise
