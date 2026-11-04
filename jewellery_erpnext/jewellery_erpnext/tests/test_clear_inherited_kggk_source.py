"""Pins ``clear_inherited_kggk_source``: Source DocType/Site/Name belong on Template BOMs.

DB-free: ``setUpClass`` is neutralized and the BOM is a plain ``frappe._dict``.
"""

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events.bom import (
	KGGK_SOURCE_FIELDS,
	clear_inherited_kggk_source,
)


class _Bom(frappe._dict):
	def set(self, key, value):
		self[key] = value


def _make(bom_type, doctype=None, site=None, name=None):
	return _Bom(
		bom_type=bom_type,
		custom_kggk_source_doctype=doctype,
		custom_kggk_source_site=site,
		custom_kggk_source_name=name,
	)


class TestClearInheritedKggkSource(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _values(self, bom):
		return [bom.get(f) for f in KGGK_SOURCE_FIELDS]

	def test_template_keeps_its_source(self):
		bom = _make("Template", "BOM", "gepl-prod.frappe.cloud", "BOM-X")
		clear_inherited_kggk_source(bom)
		self.assertEqual(self._values(bom), ["BOM", "gepl-prod.frappe.cloud", "BOM-X"])

	def test_inherited_source_cleared_on_non_template(self):
		for bom_type in (
			"Finish Goods",
			"Manufacturing Process",
			"Sales Order",
			"Quotation",
		):
			bom = _make(bom_type, "BOM", "gepl-prod.frappe.cloud", "BOM-X")
			clear_inherited_kggk_source(bom)
			self.assertEqual(self._values(bom), [None, None, None], bom_type)

	def test_product_return_order_source_is_kept(self):
		bom = _make("Finish Goods", "Product Return Order", None, "KG-PRF-2026-1-1")
		clear_inherited_kggk_source(bom)
		self.assertEqual(
			self._values(bom), ["Product Return Order", None, "KG-PRF-2026-1-1"]
		)

	def test_blank_non_template_untouched(self):
		bom = _make("Finish Goods")
		clear_inherited_kggk_source(bom)
		self.assertEqual(self._values(bom), [None, None, None])
