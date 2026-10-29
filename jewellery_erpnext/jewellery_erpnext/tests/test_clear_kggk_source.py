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


class _Bom(dict):
	"""Stands in for a BOM: the helper only needs ``set``. Independent of the site's BOM meta,
	because the three Source fields are Custom Fields that not every site (CI's) has."""

	def set(self, key, value):
		self[key] = value


class TestClearKggkSource(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_copy_doc_carries_the_stamp(self):
		# The reason the clear exists: the clone keeps all three values. Only meaningful on a site
		# that has the Custom Fields; elsewhere copy_doc has nowhere to keep them.
		meta = frappe.get_meta("BOM")
		if not all(meta.has_field(f) for f in KGGK_SOURCE_FIELDS):
			self.skipTest("BOM has no KGGK source Custom Fields on this site")

		template = frappe.get_doc({"doctype": "BOM", "bom_type": "Template", **STAMP})
		clone = frappe.copy_doc(template)
		self.assertEqual({f: clone.get(f) for f in KGGK_SOURCE_FIELDS}, STAMP)

	def test_clear_blanks_all_three(self):
		bom = _Bom(STAMP)
		clear_kggk_source(bom)
		self.assertEqual([bom[f] for f in KGGK_SOURCE_FIELDS], [None, None, None])

	def test_clear_leaves_other_fields_alone(self):
		bom = _Bom({**STAMP, "bom_type": "Finish Goods"})
		clear_kggk_source(bom)
		self.assertEqual(bom["bom_type"], "Finish Goods")

	def test_the_template_itself_is_untouched(self):
		template = _Bom(STAMP)
		clone = _Bom(template)
		clear_kggk_source(clone)
		self.assertEqual({f: template[f] for f in KGGK_SOURCE_FIELDS}, STAMP)

	def test_every_bom_clone_site_clears_the_stamp(self):
		for fn in (
			manufacturing_operation.create_finished_goods_bom,
			sales_order.create_serial_no_bom,
			sales_order.create_sales_order_bom,
		):
			self.assertIn("clear_kggk_source(", inspect.getsource(fn), fn.__name__)
