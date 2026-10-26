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


if __name__ == "__main__":
	unittest.main()
