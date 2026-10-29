"""Pins that a BOM cloned from a replicated Template does not keep its Source DocType/Site/Name.

``frappe.copy_doc`` copies ``no_copy`` fields, so every clone inherits the stamp unless it is cleared.
Three places clone a BOM; each must call ``clear_kggk_source``.

``setUpClass`` is neutralized; only the BOM meta is read.
"""

import inspect

import frappe
from frappe.tests import IntegrationTestCase

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


def _stamped_template():
	return frappe.get_doc({"doctype": "BOM", "bom_type": "Template", **STAMP})


class TestClearKggkSource(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_copy_doc_carries_the_stamp(self):
		# The reason the clear exists: the clone keeps all three values.
		clone = frappe.copy_doc(_stamped_template())
		self.assertEqual({f: clone.get(f) for f in KGGK_SOURCE_FIELDS}, STAMP)

	def test_clear_blanks_all_three_on_a_clone(self):
		clone = frappe.copy_doc(_stamped_template())
		clear_kggk_source(clone)
		self.assertEqual([clone.get(f) for f in KGGK_SOURCE_FIELDS], [None, None, None])

	def test_the_template_itself_is_untouched(self):
		template = _stamped_template()
		clear_kggk_source(frappe.copy_doc(template))
		self.assertEqual({f: template.get(f) for f in KGGK_SOURCE_FIELDS}, STAMP)

	def test_every_bom_clone_site_clears_the_stamp(self):
		for fn in (
			manufacturing_operation.create_finished_goods_bom,
			sales_order.create_serial_no_bom,
			sales_order.create_sales_order_bom,
		):
			self.assertIn("clear_kggk_source(", inspect.getsource(fn), fn.__name__)
