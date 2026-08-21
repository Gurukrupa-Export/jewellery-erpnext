# Copyright (c) 2023, Nirali and Contributors
# See license.txt

"""Controller-level coverage for Tree Number.

Kept to in-memory documents (``frappe.new_doc``) so it runs on a bare CI site: nothing here
needs casting master data, which ``create_test_data`` does not seed.

The ledger arithmetic itself is pinned in
``jewellery_erpnext.jewellery_erpnext.tests.test_tree_material_balance``; these tests check
that the controller actually routes through it.
"""

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_material_balance as tree_balance,
)


class TestTreeNumber(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _tree(self, rows):
		"""In-memory Tree Number carrying the given (item, issue, receive, loss) rows."""
		doc = frappe.new_doc("Tree Number")
		for item, issue, receive, loss in rows:
			doc.append(
				"material_details",
				{
					"item_code": item,
					"issue_qty": issue,
					"receive_qty": receive,
					"loss_qty": loss,
					"pending_qty": 0,
				},
			)
		return doc

	def test_calculate_material_pending_derives_each_row(self):
		doc = self._tree([("ITEM-A", 2.900, 2.390, 0), ("ITEM-B", 5.0, 4.0, 1.0)])
		doc.calculate_material_pending()
		self.assertAlmostEqual(doc.material_details[0].pending_qty, 0.510, places=3)
		self.assertAlmostEqual(doc.material_details[1].pending_qty, 0.0, places=3)

	def test_pending_is_not_floored(self):
		# The old casting branch clamped this to 0 and hid the over-draw that produced
		# GEPL-TR-26-00154. It must now read negative.
		doc = self._tree([("ITEM-A", 0, 2.360, 0)])
		doc.employee_ir = "EIR-DOES-NOT-MATTER"
		doc.calculate_material_pending()
		self.assertAlmostEqual(doc.material_details[0].pending_qty, -2.360, places=3)

	def test_validate_rejects_receive_without_issue(self):
		doc = self._tree([("ITEM-A", 0, 2.360, 0)])
		with self.assertRaises(ValidationError):
			tree_balance.validate_row_balance(doc)

	def test_validate_accepts_a_balanced_ledger(self):
		doc = self._tree([("ITEM-A", 2.900, 2.390, 0)])
		doc.calculate_material_pending()
		tree_balance.validate_row_balance(doc)

	def test_status_helper_is_the_controller_source(self):
		doc = self._tree([("ITEM-A", 2.900, 2.390, 0)])
		doc.calculate_material_pending()
		self.assertEqual(
			tree_balance.tree_status(doc), tree_balance.STATUS_PARTIALLY_RECEIVED
		)

	def test_zero_ledger_is_draft_not_received(self):
		doc = self._tree([("ITEM-A", 0, 0, 0)])
		doc.calculate_material_pending()
		self.assertEqual(tree_balance.tree_status(doc), tree_balance.STATUS_DRAFT)
