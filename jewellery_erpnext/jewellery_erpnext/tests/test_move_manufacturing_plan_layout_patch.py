# Copyright (c) 2026, Nirali and contributors
# See license.txt
#
# The upgrade path of patches/move_manufacturing_plan_layout_to_core.py. A fresh CI site never
# runs it (patches.txt entries are marked done on install), so the rows an old site carries are
# seeded here. Everything is rolled back with the test transaction.

import json

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import move_manufacturing_plan_layout_to_core as patch

DOCTYPE = "Manufacturing Plan"


def _custom_field(fieldname):
	return frappe.get_doc(
		{
			"doctype": "Custom Field",
			"dt": DOCTYPE,
			"fieldname": fieldname,
			"fieldtype": "Date" if fieldname == "estimated_delivery_date" else "Column Break",
			"label": "Estimated Delivery Date" if fieldname == "estimated_delivery_date" else None,
		}
	)


class TestMoveManufacturingPlanLayoutPatch(IntegrationTestCase):
	def _seed_old_site_rows(self):
		# Inserted straight into the table: Custom Field.validate refuses a name the standard
		# DocType already owns, which is exactly the clash the patch exists to clear.
		for fieldname in patch.CUSTOM_FIELDS:
			doc = _custom_field(fieldname)
			doc.name = f"{DOCTYPE}-{fieldname}"
			doc.db_insert()
		frappe.get_doc(
			{
				"doctype": "Property Setter",
				"doc_type": DOCTYPE,
				"doctype_or_field": "DocType",
				"property": "field_order",
				"value": '["company"]',
				"property_type": "Data",
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "Property Setter",
				"doc_type": DOCTYPE,
				"doctype_or_field": "DocField",
				"field_name": "custom_column_break_5wdau",
				"property": "hidden",
				"value": "0",
				"property_type": "Check",
			}
		).insert()

	def _custom_field_names(self):
		return frappe.get_all(
			"Custom Field",
			filters={"dt": DOCTYPE, "fieldname": ("in", patch.CUSTOM_FIELDS)},
			pluck="fieldname",
		)

	def _setters(self, **filters):
		return frappe.get_all("Property Setter", filters={"doc_type": DOCTYPE, **filters}, pluck="name")

	def test_removes_the_duplicate_custom_fields_and_the_stale_order(self):
		self._seed_old_site_rows()
		keep = frappe.get_doc(
			{
				"doctype": "Property Setter",
				"doc_type": DOCTYPE,
				"doctype_or_field": "DocField",
				"field_name": "get_mwo",
				"property": "depends_on",
				"value": 'eval:doc.flow_type=="Finding Manufacturing"',
				"property_type": "Data",
			}
		).insert()

		patch.execute()

		self.assertEqual(self._custom_field_names(), [])
		self.assertEqual(self._setters(property="field_order"), [])
		self.assertEqual(self._setters(field_name="custom_column_break_5wdau"), [])
		# Other site customisations on the doctype are not the patch's to touch.
		self.assertEqual(self._setters(field_name="get_mwo"), [keep.name])

	def test_keeps_the_estimated_delivery_date_column(self):
		self._seed_old_site_rows()
		self.assertTrue(frappe.db.has_column(DOCTYPE, "estimated_delivery_date"))

		patch.execute()

		self.assertTrue(frappe.db.has_column(DOCTYPE, "estimated_delivery_date"))

	def test_a_second_run_changes_nothing(self):
		self._seed_old_site_rows()
		patch.execute()
		patch.execute()
		self.assertEqual(self._custom_field_names(), [])

	def test_doctype_json_owns_the_layout_the_patch_hands_over(self):
		# Read from the JSON, not the site's meta: the patch runs before/after a sync and the
		# JSON is what ships.
		path = frappe.get_app_path(
			"jewellery_erpnext", "jewellery_erpnext", "doctype", "manufacturing_plan", "manufacturing_plan.json"
		)
		with open(path) as f:
			doctype = json.load(f)
		fieldnames = [df["fieldname"] for df in doctype["fields"]]
		for fieldname in ("estimated_delivery_date", "column_break_5wdau", "section_break_edsf8"):
			self.assertIn(fieldname, fieldnames)
		self.assertEqual(len(fieldnames), len(set(fieldnames)), "a field appears twice")
		self.assertCountEqual(doctype["field_order"], fieldnames)
