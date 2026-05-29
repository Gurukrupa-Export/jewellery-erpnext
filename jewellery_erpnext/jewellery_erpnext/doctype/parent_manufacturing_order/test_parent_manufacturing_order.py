# Copyright (c) 2023, Nirali and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	_get_finding_base_data,
	create_finding_work_orders,
)

PMO = "PMO-GEPL-NE01725-002-0005"
CHAIN_ITEM = "F-G-18KT-75.4-P-CHA-6SC-9.50 INCH"


def _finding_mwo_count():
	return frappe.db.count(
		"Manufacturing Work Order",
		{"manufacturing_order": PMO, "is_finding_mwo": 1},
	)


class TestGetFindingBaseData(FrappeTestCase):
	def test_resolves_null_item_variant(self):
		"""BOM rows with item_variant=null are resolved via template + attributes."""
		doc = frappe.get_doc("Parent Manufacturing Order", PMO)
		not_to_include, finding_data = _get_finding_base_data(doc)
		self.assertTrue(len(finding_data) > 0, "finding_data should not be empty")
		self.assertEqual(finding_data[0].item_variant, CHAIN_ITEM)

	def test_not_to_include_populated(self):
		"""BOM row name is added to not_to_include so it is excluded from metal grouping."""
		doc = frappe.get_doc("Parent Manufacturing Order", PMO)
		not_to_include, _ = _get_finding_base_data(doc)
		self.assertTrue(len(not_to_include) > 0)

	def test_ignore_work_order_excluded(self):
		"""Rows with ignore_work_order=1 must be skipped."""
		bom_row_name = frappe.db.get_value(
			"BOM Finding Detail",
			{
				"parent": frappe.db.get_value(
					"Parent Manufacturing Order", PMO, "custom_tracking_bom"
				)
			},
			"name",
		)
		if not bom_row_name:
			self.skipTest("No BOM finding detail rows found")

		frappe.db.set_value("BOM Finding Detail", bom_row_name, "ignore_work_order", 1)
		try:
			doc = frappe.get_doc("Parent Manufacturing Order", PMO)
			not_to_include, finding_data = _get_finding_base_data(doc)
			self.assertEqual(
				finding_data, [], "ignored rows must not appear in finding_data"
			)
		finally:
			frappe.db.set_value(
				"BOM Finding Detail", bom_row_name, "ignore_work_order", 0
			)


class TestCreateFindingWorkOrders(FrappeTestCase):
	def test_no_duplicate_on_repeated_calls(self):
		"""Calling create_finding_work_orders multiple times must not create duplicate MWOs."""
		count_before = _finding_mwo_count()
		create_finding_work_orders(PMO)
		count_after = _finding_mwo_count()
		self.assertEqual(
			count_before,
			count_after,
			f"Duplicate MWO created: count went from {count_before} to {count_after}",
		)

	def test_existing_finding_mwo_not_duplicated(self):
		"""If a finding MWO already exists for the item, it must be skipped."""
		existing = frappe.db.get_all(
			"Manufacturing Work Order",
			{"manufacturing_order": PMO, "is_finding_mwo": 1, "item_code": CHAIN_ITEM},
			pluck="name",
		)
		create_finding_work_orders(PMO)
		after = frappe.db.get_all(
			"Manufacturing Work Order",
			{"manufacturing_order": PMO, "is_finding_mwo": 1, "item_code": CHAIN_ITEM},
			pluck="name",
		)
		self.assertEqual(
			sorted(existing),
			sorted(after),
			"New finding MWO was created even though one already existed",
		)
