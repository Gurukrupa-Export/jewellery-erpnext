# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for Serial Number Creator._compute_total_weight carat->gram rounding.

The SNC's ``total_weight`` (product / gross weight) must sum carats by FAMILY,
convert each family ONCE via carat_to_gram (never the raw ``qty * 0.2`` and never
per-row conversion) and round the total once, so it agrees with the FG BOM's
``gross_weight`` (create_finished_goods_bom) and the FG MWO's ``gross_wt``
(sync_mwo_weights), all three of which use the same round-of-sum.

Regression: the reported piece (diamond 4.256 ct) previously summed an unrounded
``4.256 * 0.2 = 0.8512`` into the total; the corrected total is 31.199 (not
31.1992), matching the BOM / MWO / Serial No. A second regression: if that 4.256 ct
arrives as two rows (2.503 + 1.753), rounding each row first would give
0.501 + 0.351 = 0.852 g and total 31.200 -- the family must convert once to 0.851 g.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator import (
	SerialNumberCreator,
)
from jewellery_erpnext.utils import carat_to_gram

ROUNDING = "Banker's Rounding (legacy)"


def _row(row_material, qty):
	return SimpleNamespace(row_material=row_material, qty=qty)


class TestSncTotalWeightRounding(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _compute(self, rows):
		doc = SerialNumberCreator.__new__(SerialNumberCreator)
		doc.fg_details = rows
		with patch.object(frappe, "get_system_settings", return_value=ROUNDING):
			doc._compute_total_weight()
		return doc.total_weight

	def test_total_weight_round_of_sum_matches_bom_mwo(self):
		"""Diamond 4.256 ct -> carat_to_gram, gross = 26.798 + 3.550 + 0.851 = 31.199."""
		total = self._compute(
			[_row("M-GOLD", 26.798), _row("F-FND", 3.550), _row("D-DIA", 4.256)]
		)
		self.assertAlmostEqual(total, 31.199, places=3)
		self.assertNotEqual(total, 31.1992)

	def test_carat_families_convert_once_per_family(self):
		"""Each D/G family is converted once via carat_to_gram, then the total is
		rounded once -- the same round-of-sum as the BOM/MWO."""
		rows = [
			_row("M-GOLD", 2.273),
			_row("F-FND", 2.334),
			_row("D-DIA", 1.0),
			_row("G-STONE", 2.0),
			_row("O-OTH", 0.5),
		]
		total = self._compute(rows)
		with patch.object(frappe, "get_system_settings", return_value=ROUNDING):
			expected = flt(
				2.273 + 2.334 + 0.5 + carat_to_gram(1.0) + carat_to_gram(2.0), 3
			)
		self.assertAlmostEqual(total, expected, places=3)

	def test_multiple_diamond_rows_convert_once_as_family(self):
		"""Two diamond rows summing to 4.256 ct must convert ONCE to 0.851 g, not
		0.501 + 0.351 = 0.852 g. Rounding each row first would give 31.200."""
		rows = [
			_row("M-GOLD", 26.798),
			_row("F-FND", 3.550),
			_row("D-DIA-A", 2.503),
			_row("D-DIA-B", 1.753),
		]
		total = self._compute(rows)
		self.assertAlmostEqual(total, 31.199, places=3)
		self.assertNotEqual(total, 31.200)

	def test_multiple_gemstone_rows_convert_once_as_family(self):
		"""Two gemstone rows (0.497 + 0.067 = 0.564 ct) convert once to 0.113 g;
		per-row rounding would give 0.099 + 0.013 = 0.112 g (31.198 vs 31.199)."""
		rows = [
			_row("M-GOLD", 30.000),
			_row("F-FND", 1.000),
			_row("G-STONE-A", 0.497),
			_row("G-STONE-B", 0.067),
		]
		total = self._compute(rows)
		with patch.object(frappe, "get_system_settings", return_value=ROUNDING):
			expected = flt(30.000 + 1.000 + carat_to_gram(0.564), 3)
		self.assertAlmostEqual(total, expected, places=3)
