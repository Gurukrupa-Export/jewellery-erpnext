"""Database-backed metadata helper regressions; use only a disposable test site.

Fixtures use real Item Attribute and Attribute Value controllers and rollback.
This module tests helper reads/child construction, not an entire Item save.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events.item import add_item_attributes


class TestItemMetadataIntegration(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.db.rollback)
		self.length = self.make_attribute("Length", 0.001, 25.5, 0.125)
		self.width = self.make_attribute("Width", 0, 10, 0.5)
		self.subcategory = frappe.get_doc(
			{
				"doctype": "Attribute Value",
				"attribute_value": "_Test BOM Item Metadata Subcategory",
				"is_subcategory": 1,
				"item_attributes": [
					{"item_attribute": self.width.name, "in_item_variant": 1},
					{"item_attribute": self.length.name, "in_item_variant": 1},
					{"item_attribute": self.width.name, "in_item_variant": 1},
					{"item_attribute": self.length.name, "in_item_variant": 0},
				],
			}
		).insert()

	def make_attribute(self, suffix, start, end, increment):
		return frappe.get_doc(
			{
				"doctype": "Item Attribute",
				"attribute_name": f"_Test BOM Item Metadata {suffix}",
				"numeric_values": 1,
				"from_range": start,
				"to_range": end,
				"increment": increment,
			}
		).insert()

	def template(self):
		doc = frappe.new_doc("Item")
		doc.has_variants = 1
		doc.subcategory = self.subcategory.name
		return doc

	def test_real_metadata_preserves_child_order_duplicates_and_values(self):
		doc = self.template()
		with patch.object(frappe.db, "get_value", wraps=frappe.db.get_value) as lookup:
			add_item_attributes(doc)
			metadata_reads = [
				call
				for call in lookup.call_args_list
				if call.args and call.args[0] == "Item Attribute"
			]
		self.assertEqual(len(metadata_reads), 2)
		self.assertEqual(
			[
				(
					row.attribute,
					row.numeric_values,
					row.from_range,
					row.to_range,
					row.increment,
				)
				for row in doc.attributes
			],
			[
				(self.width.name, 1, 0, 10, 0.5),
				(self.length.name, 1, 0.001, 25.5, 0.125),
				(self.width.name, 1, 0, 10, 0.5),
			],
		)

	def test_next_initialization_reads_master_edits_and_keeps_existing_attributes(self):
		first = self.template()
		add_item_attributes(first)
		self.length.increment = 0.25
		self.length.save()
		second = self.template()
		add_item_attributes(second)
		self.assertEqual(first.attributes[1].increment, 0.125)
		self.assertEqual(second.attributes[1].increment, 0.25)
		add_item_attributes(first)
		self.assertEqual(first.attributes[1].increment, 0.125)
