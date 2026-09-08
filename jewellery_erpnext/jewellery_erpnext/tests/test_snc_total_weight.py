# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for Serial Number Creator._compute_total_weight carat->gram rounding.

The SNC's ``total_weight`` (product / gross weight) must convert each carat family
once via carat_to_gram -- never the raw ``qty * 0.2`` -- so it agrees with the
FG BOM's ``gross_weight`` (create_finished_goods_bom) and the FG MWO's ``gross_wt``
(sync_mwo_weights), all three of which use the same round-of-sum.

Regression: the reported piece (diamond 4.256 ct) previously summed an unrounded
``4.256 * 0.2 = 0.8512`` into the total; the corrected total is 31.199 (not 31.1992),
matching the BOM / MWO / Serial No.
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
		"""Each D/G row is converted once via carat_to_gram, then the total is
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
