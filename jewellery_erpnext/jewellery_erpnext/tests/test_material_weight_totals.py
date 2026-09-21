# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Raw-material totals on the Stock Entry header (the W-series).

Six header fields summed from the item rows, bucketed by MATERIAL FAMILY rather than by
item: metal / finding / diamond / gemstone weight, plus diamond and gemstone pcs. Nothing
had ever written them -- all four pre-existing fields read 0.000 on every one of the 27,709
submitted Stock Entries on the live site, which is how MAT-STE-28123 came to move 1.46 g of
gold and 1.46 g of findings and report nothing.

Pure-logic per the suite convention (see test_stock_entry.py): plain-dict rows, no DB.
``flt(x, 3)`` reaches ``frappe.get_system_settings("rounding_method")``, so the carat cases
pin it -- otherwise flt swallows the lookup failure into 0.0 and the assertions pass
vacuously against zeros.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.utils import (
	material_weights as mw,
)

METAL = "M-G-22KT-91.75-Y"
METAL_LOSS = "ML-G-22KT-91.75-Y"
FIND_A = "F-G-22KT-91.75-Y-PO-BP-1.10*7.50 MM"
FIND_B = "F-G-22KT-91.75-Y-SW-1FSS-9.00 MM"
FIND_LOSS = "FL-G-22KT-91.75-Y-BL-FE2CTB-6.50*3.50 MM"
MSL = "Casting KGJPL - 00294 WH - KGJPL"
DEPT = "Waxing WO - KGJPL"


def row(variant, qty, uom="Gram", pcs=None, s_warehouse=MSL, t_warehouse=None, **extra):
	"""One Stock Entry Detail row. Defaults to a consume row, the commonest shape."""
	d = {
		"item_code": f"{variant}-x",
		"custom_variant_of": variant,
		"qty": qty,
		"uom": uom,
		"stock_uom": uom,
		"pcs": pcs,
		"s_warehouse": s_warehouse,
		"t_warehouse": t_warehouse,
	}
	d.update(extra)
	return d


def produce(variant, qty, **extra):
	return row(variant, qty, s_warehouse=None, t_warehouse=MSL, **extra)


def transfer(variant, qty, **extra):
	return row(variant, qty, s_warehouse=MSL, t_warehouse=DEPT, **extra)


def pinned_rounding():
	"""flt(x, precision) reads the rounding method from system settings -- a real DB read."""
	return patch.object(
		frappe, "get_system_settings", return_value="Banker's Rounding (legacy)"
	)


class _Base(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def totals(self, rows):
		with pinned_rounding():
			return mw.material_totals(rows)


# ---------------------------------------------------------------------------
# W01-W05: the shapes that prompted this
# ---------------------------------------------------------------------------
class TestRealEntries(_Base):
	def test_w01_finding_repack_counts_both_sides(self):
		"""MAT-STE-28123. The entry really did move 1.46 g of each, so both are reported --
		this is the number the user gave."""
		t = self.totals(
			[
				row("M", 1.23),
				produce("F", 1.23),
				row("M", 0.23),
				produce("F", 0.23),
			]
		)
		self.assertAlmostEqual(t["metal"], 1.460, places=3)
		self.assertAlmostEqual(t["finding"], 1.460, places=3)
		self.assertAlmostEqual(t["diamond"], 0.0, places=3)
		self.assertAlmostEqual(t["gemstone"], 0.0, places=3)
		self.assertEqual(t["diamond_pcs"], 0)
		self.assertEqual(t["gemstone_pcs"], 0)

	def test_w02_material_transfer_rows_count(self):
		"""MAT-STE-28124: every row carries BOTH warehouses and must still count."""
		t = self.totals([transfer("M", 2.0), transfer("F", 1.23), transfer("F", 0.23)])
		self.assertAlmostEqual(t["metal"], 2.0, places=3)
		self.assertAlmostEqual(t["finding"], 1.460, places=3)

	def test_w03_process_loss_folds_the_loss_variant_into_metal(self):
		"""M consumed and ML produced are both metal, so a 0.523 g loss reports 1.046."""
		t = self.totals([row("M", 0.523), produce("ML", 0.523)])
		self.assertAlmostEqual(t["metal"], 1.046, places=3)

	def test_w04_finding_loss_folds_into_finding(self):
		t = self.totals([row("F", 0.015), produce("FL", 0.015)])
		self.assertAlmostEqual(t["finding"], 0.030, places=3)

	def test_w05_empty_and_none(self):
		for rows in ([], None):
			t = self.totals(rows)
			self.assertEqual(
				t,
				{
					"metal": 0.0,
					"finding": 0.0,
					"diamond": 0.0,
					"gemstone": 0.0,
					"diamond_pcs": 0,
					"gemstone_pcs": 0,
				},
			)


# ---------------------------------------------------------------------------
# W10-W14: what must NOT count -- the whole point of "not item wise"
# ---------------------------------------------------------------------------
class TestExclusions(_Base):
	def test_w10_finished_goods_design_code_is_excluded(self):
		"""An FG row's variant_of is a DESIGN template and its qty is a piece count in Nos.
		Counting it would add 1 to a weight total."""
		t = self.totals([transfer("MU00122", 1, uom="Nos"), transfer("M", 5.0)])
		self.assertAlmostEqual(t["metal"], 5.0, places=3)

	# A first-character rule would bucket every one of these as metal/diamond/etc.
	def test_w11_design_codes_are_not_matched_by_first_letter(self):
		t = self.totals(
			[
				transfer("MU00122", 1, uom="Nos"),
				transfer("MU00033", 1, uom="Nos"),
				transfer("DA00001", 1, uom="Nos"),
				transfer("GX00002", 1, uom="Nos"),
				transfer("FZ00003", 1, uom="Nos"),
			]
		)
		self.assertEqual(
			(t["metal"], t["finding"], t["diamond"], t["gemstone"]),
			(0.0, 0.0, 0.0, 0.0),
		)

	def test_w12_other_and_blank_variants_are_excluded(self):
		t = self.totals(
			[
				transfer("O", 3.0),
				transfer(None, 4.0),
				transfer("", 5.0),
				transfer("M", 1.0),
			]
		)
		self.assertAlmostEqual(t["metal"], 1.0, places=3)

	def test_w13_variant_is_whitespace_tolerant(self):
		t = self.totals([transfer(" M ", 2.0)])
		self.assertAlmostEqual(t["metal"], 2.0, places=3)

	def test_w14_metal_and_finding_pcs_are_ignored(self):
		"""Only stones carry a pcs count; a stray pcs on a metal row must not leak."""
		t = self.totals([row("M", 1.0, pcs="7"), row("F", 1.0, pcs="9")])
		self.assertEqual(t["diamond_pcs"], 0)
		self.assertEqual(t["gemstone_pcs"], 0)


# ---------------------------------------------------------------------------
# W20-W24: carats
# ---------------------------------------------------------------------------
class TestCaratConversion(_Base):
	def test_w20_diamond_converted_to_grams(self):
		t = self.totals([row("D", 1.0, uom="Carat", pcs="3")])
		self.assertAlmostEqual(t["diamond"], 0.2, places=3)

	def test_w21_converted_once_on_the_total_not_per_row(self):
		"""0.497 ct + 0.067 ct: per-row rounding gives 0.099 + 0.013 = 0.112, the total
		converts to 0.113. See the drift note in mop_log."""
		t = self.totals([row("D", 0.497, uom="Carat"), row("D", 0.067, uom="Carat")])
		self.assertAlmostEqual(t["diamond"], 0.113, places=3)

	def test_w22_gemstone_converted_independently(self):
		t = self.totals([row("D", 1.0, uom="Carat"), row("G", 2.0, uom="Carat")])
		self.assertAlmostEqual(t["diamond"], 0.2, places=3)
		self.assertAlmostEqual(t["gemstone"], 0.4, places=3)

	def test_w23_stone_row_already_in_grams_is_not_scaled(self):
		"""Not present on the live site today, but scaling a gram figure by 0.2 would
		invent a loss."""
		t = self.totals([row("D", 0.5, uom="Gram")])
		self.assertAlmostEqual(t["diamond"], 0.5, places=3)

	def test_w24_falls_back_to_stock_uom_when_uom_is_blank(self):
		t = self.totals([row("D", 1.0, uom=None, stock_uom="Carat")])
		self.assertAlmostEqual(t["diamond"], 0.2, places=3)


# ---------------------------------------------------------------------------
# W30-W34: pcs -- a Data field on the row
# ---------------------------------------------------------------------------
class TestPcs(_Base):
	def test_w30_diamond_and_gemstone_pcs_sum_separately(self):
		t = self.totals(
			[
				row("D", 1.0, uom="Carat", pcs="12"),
				row("D", 1.0, uom="Carat", pcs="5"),
				row("G", 1.0, uom="Carat", pcs="3"),
			]
		)
		self.assertEqual(t["diamond_pcs"], 17)
		self.assertEqual(t["gemstone_pcs"], 3)

	def test_w31_blank_and_none_pcs_count_as_zero(self):
		t = self.totals(
			[
				row("D", 1.0, uom="Carat", pcs=None),
				row("D", 1.0, uom="Carat", pcs=""),
				row("D", 1.0, uom="Carat", pcs="4"),
			]
		)
		self.assertEqual(t["diamond_pcs"], 4)

	def test_w32_non_numeric_pcs_counts_as_zero(self):
		t = self.totals([row("D", 1.0, uom="Carat", pcs="abc")])
		self.assertEqual(t["diamond_pcs"], 0)

	def test_w33_negative_pcs_is_summed_as_given(self):
		"""One live gemstone row carries pcs -1. This is a document total, not a running
		balance, so mop_log's clamp_negative_balance does not apply."""
		t = self.totals(
			[row("G", 1.0, uom="Carat", pcs="5"), row("G", 1.0, uom="Carat", pcs="-1")]
		)
		self.assertEqual(t["gemstone_pcs"], 4)

	def test_w34_pcs_counted_on_produce_rows_too(self):
		t = self.totals([produce("D", 1.0, uom="Carat", pcs="6")])
		self.assertEqual(t["diamond_pcs"], 6)


# ---------------------------------------------------------------------------
# W40-W42: the doc-event wrapper
# ---------------------------------------------------------------------------
class _FakeMeta:
	def __init__(self, fields):
		self._fields = set(fields)

	def has_field(self, fieldname):
		return fieldname in self._fields


class _FakeDoc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)

	def set(self, key, value):
		setattr(self, key, value)


class TestSetMaterialTotals(_Base):
	def _doc(self, rows, fields=None):
		fields = fields if fields is not None else list(mw.TARGET_FIELDS.values())
		doc = _FakeDoc(items=rows, meta=_FakeMeta(fields))
		for f in fields:
			setattr(doc, f, 0)
		return doc

	def test_w40_stamps_all_six_fields(self):
		doc = self._doc(
			[row("M", 1.23), produce("F", 1.23), row("D", 2.0, uom="Carat", pcs="8")]
		)
		with pinned_rounding():
			mw.set_material_totals(doc)
		self.assertAlmostEqual(doc.custom_total_metal_weight, 1.23, places=3)
		self.assertAlmostEqual(doc.custom_total_finding_weight, 1.23, places=3)
		self.assertAlmostEqual(doc.custom_total_diamond_weight, 0.4, places=3)
		self.assertAlmostEqual(doc.custom_total_gemstone_weight, 0.0, places=3)
		self.assertEqual(doc.custom_total_diamond_pcs, 8)
		self.assertEqual(doc.custom_total_gemstone_pcs, 0)

	def test_w41_recomputes_rather_than_accumulating(self):
		"""validate runs on every save; a second pass must not double the totals."""
		doc = self._doc([row("M", 4.0)])
		with pinned_rounding():
			mw.set_material_totals(doc)
			mw.set_material_totals(doc)
		self.assertAlmostEqual(doc.custom_total_metal_weight, 4.0, places=3)

	def test_w42_missing_pcs_fields_do_not_break_the_save(self):
		"""A site that has not run the patch yet has no pcs columns; the weights must
		still be stamped rather than the whole save failing."""
		weight_only = [
			mw.TARGET_FIELDS[k] for k in ("metal", "finding", "diamond", "gemstone")
		]
		doc = self._doc([row("M", 2.0)], fields=weight_only)
		with pinned_rounding():
			mw.set_material_totals(doc)
		self.assertAlmostEqual(doc.custom_total_metal_weight, 2.0, places=3)
		self.assertFalse(hasattr(doc, "custom_total_diamond_pcs"))
