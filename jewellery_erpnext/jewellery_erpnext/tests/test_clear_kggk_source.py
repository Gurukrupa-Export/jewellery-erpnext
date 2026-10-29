"""Pins ``clear_kggk_source``: Source DocType/Site/Name belong on Template BOMs only.

``frappe.copy_doc`` copies ``no_copy`` fields, so every BOM cloned from a Template inherited
the replication identity. Each clone site calls the helper right after the copy.
"""

import inspect
import unittest

import frappe

from jewellery_erpnext.jewellery_erpnext.doc_events import sales_order
from jewellery_erpnext.jewellery_erpnext.doc_events.bom_utils import (
	KGGK_SOURCE_FIELDS,
	clear_kggk_source,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation import (
	manufacturing_operation,
)

STAMP = {
	"custom_kggk_source_doctype": "BOM",
	"custom_kggk_source_site": "gepl-prod.frappe.cloud",
	"custom_kggk_source_name": "BOM-X-001-001",
}


class _Bom(dict):
	"""Stand-in for a BOM doc; the Source fields are DB-only Custom Fields."""

	def set(self, key, value):
		self[key] = value


class TestClearKggkSource(unittest.TestCase):
	def test_copy_doc_carries_the_stamp(self):
		meta = frappe.get_meta("BOM")
		if not all(meta.has_field(f) for f in KGGK_SOURCE_FIELDS):
			self.skipTest("Source Custom Fields are not installed on this site")

		template = frappe.new_doc("BOM")
		for fieldname, value in STAMP.items():
			template.set(fieldname, value)

		clone = frappe.copy_doc(template)
		for fieldname, value in STAMP.items():
			self.assertEqual(clone.get(fieldname), value)

	def test_clear_blanks_all_three(self):
		bom = _Bom(STAMP)
		clear_kggk_source(bom)
		for fieldname in KGGK_SOURCE_FIELDS:
			self.assertIsNone(bom[fieldname])

	def test_clear_leaves_other_fields_alone(self):
		bom = _Bom(STAMP, bom_type="Finish Goods", item="ITEM-1")
		clear_kggk_source(bom)
		self.assertEqual(bom["bom_type"], "Finish Goods")
		self.assertEqual(bom["item"], "ITEM-1")

	def test_the_template_itself_is_untouched(self):
		template = _Bom(STAMP, bom_type="Template")
		clone = _Bom(template)
		clear_kggk_source(clone)
		for fieldname, value in STAMP.items():
			self.assertEqual(template[fieldname], value)

	def test_every_bom_clone_site_clears_the_stamp(self):
		for fn in (
			manufacturing_operation.create_finished_goods_bom,
			sales_order.create_serial_no_bom,
			sales_order.create_sales_order_bom,
		):
			self.assertIn("clear_kggk_source(", inspect.getsource(fn), fn.__name__)
