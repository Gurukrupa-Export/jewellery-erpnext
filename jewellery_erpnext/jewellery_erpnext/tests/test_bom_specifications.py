"""Tests for `follow_first_metal_row`, the BOM before_validate step that keeps the Specification
tab's Metal Touch and Metal Purity in step with the first metal row.

`update_specifications` fills the header only while blank, so a purity changed in the Metal
Details row never reached it - and KGGK sync sends the header as it is.
"""

import unittest

import frappe

from jewellery_erpnext.jewellery_erpnext.customization.bom.doc_events.utils import (
	follow_first_metal_row,
)
from jewellery_erpnext.jewellery_erpnext.doc_events import bom as bom_events


class _BOM(frappe._dict):
	"""Just enough of a BOM: fields and metal rows."""

	def set(self, key, value):
		self[key] = value


def _row(idx, touch, purity):
	return frappe._dict(idx=idx, metal_touch=touch, metal_purity=purity)


def _bom(header_touch, header_purity, rows):
	return _BOM(metal_touch=header_touch, metal_purity=header_purity, metal_detail=rows)


class TestFollowFirstMetalRow(unittest.TestCase):
	def test_the_header_takes_the_first_rows_values(self):
		"""The reported case: the row goes 75.4 -> 91.75 and the header stayed 75.4."""
		bom = _bom("18KT", "75.4", [_row(1, "22KT", "91.75")])

		follow_first_metal_row(bom)

		self.assertEqual((bom.metal_touch, bom.metal_purity), ("22KT", "91.75"))

	def test_a_header_a_client_script_reset_still_follows(self):
		"""BOM-EA01504-001-053: refresh set the header to 91.75 from 22KT, the row says 92.0."""
		bom = _bom("22KT", "91.75", [_row(1, "22KT", "92.0")])

		follow_first_metal_row(bom)

		self.assertEqual(bom.metal_purity, "92.0")

	def test_a_blank_header_is_filled(self):
		bom = _bom(None, None, [_row(1, "22KT", "91.75")])

		follow_first_metal_row(bom)

		self.assertEqual((bom.metal_touch, bom.metal_purity), ("22KT", "91.75"))

	def test_only_the_first_row_by_idx_counts(self):
		"""Rows can arrive out of order - a KGGK push replaces the table - so idx decides."""
		bom = _bom("18KT", "75.4", [_row(2, "14KT", "58.6"), _row(1, "22KT", "91.75")])

		follow_first_metal_row(bom)

		self.assertEqual((bom.metal_touch, bom.metal_purity), ("22KT", "91.75"))

	def test_a_cleared_row_never_clears_the_header(self):
		bom = _bom("22KT", "91.75", [_row(1, "22KT", None)])

		follow_first_metal_row(bom)

		self.assertEqual(bom.metal_purity, "91.75")

	def test_a_bom_without_metal_rows_is_left_alone(self):
		bom = _bom("22KT", "91.75", [])

		follow_first_metal_row(bom)

		self.assertEqual((bom.metal_touch, bom.metal_purity), ("22KT", "91.75"))


class TestBeforeValidateWiring(unittest.TestCase):
	"""The helper is only useful if every BOM save reaches it - the original bug was a call that
	only Quotation and submitted BOMs ever made."""

	def test_the_bom_hook_is_registered(self):
		hooks = (
			frappe.get_hooks("doc_events").get("BOM", {}).get("before_validate") or []
		)
		self.assertIn(
			"jewellery_erpnext.jewellery_erpnext.doc_events.bom.before_validate", hooks
		)

	def test_a_template_bom_follows_through_before_validate(self):
		"""A draft Template BOM never enters the Quotation/submitted block, so this fails if the
		call is removed or moved back inside it."""
		bom = _bom("22KT", "91.75", [_row(1, "22KT", "92.5")])
		bom.update(
			bom_type="Template",
			docstatus=0,
			diamond_detail=[],
			finding_detail=[],
			gemstone_detail=[],
			other_detail=[],
		)

		bom_events.before_validate(bom, "before_validate")

		self.assertEqual((bom.metal_touch, bom.metal_purity), ("22KT", "92.5"))


class TestTemplateBOMSave(unittest.TestCase):
	"""A real save of a draft Template BOM already on the site, rolled back afterwards. Skipped on
	a site without one - no test here builds BOMs from scratch."""

	def tearDown(self):
		frappe.db.rollback()

	def test_the_saved_header_follows_the_first_metal_row(self):
		name = frappe.db.get_value(
			"BOM",
			{"bom_type": "Template", "docstatus": 0, "is_active": 1},
			"name",
			order_by="modified desc",
		)
		bom = frappe.get_doc("BOM", name) if name else None
		if not bom or not bom.metal_detail:
			self.skipTest("no draft Template BOM with metal rows on this site")

		first = min(bom.metal_detail, key=lambda row: row.idx)
		purity = "75.4" if first.metal_purity != "75.4" else "91.75"
		if not frappe.db.exists("Attribute Value", purity):
			self.skipTest(f"Attribute Value {purity} is not on this site")
		first.metal_purity = purity
		bom.save(ignore_permissions=True)

		self.assertEqual(frappe.db.get_value("BOM", name, "metal_purity"), purity)


if __name__ == "__main__":
	unittest.main()
