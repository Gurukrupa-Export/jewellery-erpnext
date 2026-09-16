"""Unanimity rules for the design type stamped onto a Manufacturing Plan / Purchase Order.

A single header value can only be stamped when every source Sales Order agrees. When they disagree
the field is simply left out and stays blank -- it never blocks a submit, because design type is
informational and one plan legitimately spans several designs. That also keeps a rollout safe: every
Sales Order predating the `custom_design_type` column reads back blank.

`source_order_dimensions` on this branch carries design type only, so it has no `strict` mode and
nothing here can raise. If the order type / sales type / flow type dimensions are ever backported,
their unanimity-throw and its tests belong here too.

DB-free: `frappe.get_all` is mocked, so no fixtures are built and no site data is read.
"""

import unittest
from unittest.mock import patch

import frappe

from jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order import (
	ORDER_DIMENSION_MAP,
	source_order_dimensions,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan import (
	ManufacturingPlan,
)


def plan(row_count=2):
	return frappe._dict(
		manufacturing_plan_table=[
			frappe._dict(sales_order=f"SO-{i + 1}") for i in range(row_count)
		]
	)


def sales_orders(*design_types):
	"""Build one source Sales Order row per design type given."""
	return [
		frappe._dict({"name": f"SO-{i + 1}", "custom_design_type": design_type})
		for i, design_type in enumerate(design_types)
	]


class TestSourceOrderDimensions(unittest.TestCase):
	def dimensions(self, rows):
		with patch.object(frappe, "get_all", return_value=rows):
			return source_order_dimensions(plan(len(rows)))

	def test_design_type_is_mapped(self):
		self.assertIn("custom_design_type", ORDER_DIMENSION_MAP.values())

	def test_unanimous_design_type_is_stamped(self):
		rows = sales_orders("New Design", "New Design")
		self.assertEqual(self.dimensions(rows).get("custom_design_type"), "New Design")

	def test_mixed_design_type_stays_blank_and_never_throws(self):
		# The acceptance test for the whole change.
		rows = sales_orders("New Design", "Sketch Design")
		self.assertNotIn("custom_design_type", self.dimensions(rows))

	def test_legacy_blank_design_type_stays_blank_and_never_throws(self):
		# A plan mixing pre-patch Sales Orders (blank) with new ones must still submit.
		rows = sales_orders(None, "New Design")
		self.assertNotIn("custom_design_type", self.dimensions(rows))

	def test_plan_with_no_sales_orders_returns_empty(self):
		with patch.object(frappe, "get_all", return_value=[]):
			self.assertEqual(
				source_order_dimensions(frappe._dict(manufacturing_plan_table=[])), {}
			)


class TestSetOrderDimensions(unittest.TestCase):
	"""The Manufacturing Plan header stamp, which `on_submit` then carries onward."""

	def stamp(self, *design_types):
		doc = plan()
		doc.set = lambda fieldname, value: doc.__setitem__(fieldname, value)
		with patch.object(frappe, "get_all", return_value=sales_orders(*design_types)):
			ManufacturingPlan.set_order_dimensions(doc)
		return doc.get("custom_design_type")

	def test_header_stamped_when_unanimous(self):
		self.assertEqual(self.stamp("New Design", "New Design"), "New Design")

	def test_header_blanked_when_mixed(self):
		self.assertIsNone(self.stamp("New Design", "Sketch Design"))
