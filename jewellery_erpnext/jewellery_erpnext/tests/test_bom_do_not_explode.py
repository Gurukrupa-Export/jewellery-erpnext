"""New Template BOMs tick Do Not Explode on their rows; other and saved BOMs are left alone."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import cint

from jewellery_erpnext.jewellery_erpnext.doc_events.bom import set_do_not_explode


def _bom(rows, is_new=True, bom_type="Template"):
	doc = frappe.new_doc("BOM")
	doc.bom_type = bom_type
	for row in rows:
		doc.append("items", row)
	if not is_new:
		doc.name = "BOM-SAVED-0001"
		doc.set("__islocal", None)
	return doc


def _flags(doc):
	return [cint(row.do_not_explode) for row in doc.items]


class TestBOMDoNotExplode(IntegrationTestCase):
	def test_new_template_bom_ticks_every_row(self):
		doc = _bom([{"item_code": "A"}, {"item_code": "B", "do_not_explode": 0}])

		set_do_not_explode(doc)

		self.assertEqual(_flags(doc), [1, 1])

	def test_new_template_bom_skips_phantom_rows(self):
		doc = _bom([{"item_code": "A"}, {"item_code": "P", "is_phantom_item": 1}])

		set_do_not_explode(doc)

		self.assertEqual(_flags(doc), [1, 0])

	def test_new_non_template_bom_is_left_alone(self):
		for bom_type in (
			"Quotation",
			"Sales Order",
			"Manufacturing Process",
			"Finish Goods",
		):
			with self.subTest(bom_type=bom_type):
				doc = _bom([{"item_code": "A", "do_not_explode": 0}], bom_type=bom_type)

				set_do_not_explode(doc)

				self.assertEqual(_flags(doc), [0])

	def test_saved_template_bom_is_left_alone(self):
		doc = _bom([{"item_code": "A", "do_not_explode": 0}], is_new=False)

		set_do_not_explode(doc)

		self.assertEqual(_flags(doc), [0])
