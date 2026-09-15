# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Unit tests for the FG-serial BOM weight block on Material Request and Stock Entry.

Scanning an FG serial now yields one row per serial at qty 1, each carrying the weights
of that piece's own as-built BOM (``Serial No.custom_bom_no``). Two contracts are worth
pinning, because both are easy to break from a distance:

* **The gate is "the serial resolves an as-built BOM", nothing else.** A serialised row
  that is not FG, and every row with no serial, must come out completely untouched --
  otherwise the save guard would start rejecting legitimate non-FG documents that have
  always been allowed to carry several serials on one row.
* **A row that cannot be resolved is left alone, never blanked.** The per-row
  ``set_gross_wt`` this replaces zeroed a weight whenever its lookup missed.

Mocked/pure-logic style (see test_bulk_map.py): ``setUpClass`` is a no-op, fake docs are
SimpleNamespace, and every DB reader is patched -- these must stay runnable on a site
with no fixtures.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils import (
	before_validate as mr_before_validate,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils import bom_weights

WEIGHTS = {
	fieldname: float(i + 1) for i, fieldname in enumerate(bom_weights.BOM_WEIGHT_FIELDS)
}


def _row(idx=1, serial_no=None, qty=1):
	return SimpleNamespace(idx=idx, serial_no=serial_no, qty=qty)


class TestRowSerials(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_drops_blank_lines(self):
		# A trailing newline or a pasted CRLF list must not make a single-serial row
		# look like a merged one -- that would reject a perfectly good FG row.
		self.assertEqual(bom_weights.row_serials("S-1\n"), ["S-1"])
		self.assertEqual(bom_weights.row_serials("S-1\r\n\r\nS-2\n"), ["S-1", "S-2"])
		self.assertEqual(bom_weights.row_serials(None), [])

	def test_single_serial_only_for_exactly_one(self):
		self.assertEqual(bom_weights.single_serial("S-1"), "S-1")
		self.assertIsNone(bom_weights.single_serial("S-1\nS-2"))
		self.assertIsNone(bom_weights.single_serial(""))


class TestGetWeightsForSerials(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, serial_rows, bom_rows):
		def fake_bulk_map(doctype, names, fields):
			rows = serial_rows if doctype == "Serial No" else bom_rows
			return {r.name: r for r in rows if r.name in set(names)}

		with patch.object(bom_weights, "bulk_map", side_effect=fake_bulk_map) as bulk:
			return bom_weights.get_weights_for_serials(["S-1", "S-2"]), bulk

	def test_resolves_through_custom_bom_no(self):
		serials = [
			frappe._dict(name="S-1", custom_bom_no="BOM-1"),
			frappe._dict(name="S-2", custom_bom_no="BOM-1"),
		]
		boms = [
			frappe._dict(
				name="BOM-1", **{s: 5.0 for s in bom_weights.BOM_WEIGHT_FIELDS.values()}
			)
		]
		result, bulk = self._run(serials, boms)

		self.assertEqual(set(result), {"S-1", "S-2"})
		self.assertEqual(result["S-1"]["custom_bom_gross_weight"], 5.0)
		# Two queries regardless of serial count: one row per scanned serial means a
		# transfer can carry hundreds of rows, so this must never become an N+1.
		self.assertEqual(bulk.call_count, 2)

	def test_serial_without_bom_is_absent_not_zero(self):
		# Absence is how callers tell "not an FG serial" from "an FG piece weighing 0".
		serials = [frappe._dict(name="S-1", custom_bom_no=None)]
		result, _ = self._run(serials, [])
		self.assertEqual(result, {})

	def test_missing_bom_record_is_absent(self):
		serials = [frappe._dict(name="S-1", custom_bom_no="BOM-GONE")]
		result, _ = self._run(serials, [])
		self.assertEqual(result, {})


class TestApplyBomWeights(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _apply(self, rows, weights):
		with patch.object(bom_weights, "get_weights_for_serials", return_value=weights):
			bom_weights.apply_bom_weights(rows)

	def test_stamps_single_serial_row(self):
		row = _row(serial_no="S-1")
		self._apply([row], {"S-1": WEIGHTS})
		for fieldname, value in WEIGHTS.items():
			self.assertEqual(getattr(row, fieldname), value)

	def test_unresolvable_row_is_left_alone_not_blanked(self):
		# The regression this guards: the old per-row set_gross_wt wrote None on a miss,
		# wiping a value another path had already stamped.
		row = _row(serial_no="S-1")
		row.custom_bom_gross_weight = 9.0
		self._apply([row], {})
		self.assertEqual(row.custom_bom_gross_weight, 9.0)

	def test_multi_serial_row_is_skipped(self):
		row = _row(serial_no="S-1\nS-2")
		self._apply([row], {"S-1": WEIGHTS, "S-2": WEIGHTS})
		self.assertFalse(hasattr(row, "custom_bom_gross_weight"))

	def test_no_serials_performs_no_lookup(self):
		with patch.object(
			bom_weights,
			"get_weights_for_serials",
			side_effect=AssertionError("must not query"),
		):
			bom_weights.apply_bom_weights([_row(), _row(idx=2)])


class TestValidateFgSerialRows(IntegrationTestCase):
	"""The save-time backstop for the desk form's one-row-per-serial scanning."""

	@classmethod
	def setUpClass(cls):
		pass

	def _validate(self, rows, weights):
		doc = SimpleNamespace(items=rows)
		with patch.object(
			mr_before_validate, "get_weights_for_serials", return_value=weights
		):
			mr_before_validate.validate_fg_serial_rows(doc)
		return doc

	def test_fg_row_at_qty_one_is_stamped(self):
		row = _row(serial_no="S-1", qty=1)
		self._validate([row], {"S-1": WEIGHTS})
		self.assertEqual(
			row.custom_bom_gross_weight, WEIGHTS["custom_bom_gross_weight"]
		)

	def test_fg_row_rejects_qty_other_than_one(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate([_row(serial_no="S-1", qty=2)], {"S-1": WEIGHTS})

	def test_merged_fg_row_is_rejected(self):
		# The exact shape the scanner used to produce: several FG serials sharing a row.
		with self.assertRaises(frappe.ValidationError):
			self._validate(
				[_row(serial_no="S-1\nS-2", qty=2)],
				{"S-1": WEIGHTS, "S-2": WEIGHTS},
			)

	def test_non_fg_serial_row_is_untouched(self):
		row = _row(serial_no="S-9", qty=5)
		self._validate([row], {})
		self.assertEqual(row.qty, 5)
		self.assertFalse(hasattr(row, "custom_bom_gross_weight"))

	def test_non_fg_multi_serial_row_does_not_throw(self):
		# Non-FG serialised flows have always been allowed to carry several serials on
		# one row; the FG rule must not leak onto them or existing saves would break.
		row = _row(serial_no="S-8\nS-9", qty=3)
		self._validate([row], {})
		self.assertEqual(row.qty, 3)

	def test_rows_without_serials_perform_no_lookup(self):
		doc = SimpleNamespace(items=[_row(), _row(idx=2)])
		with patch.object(
			mr_before_validate,
			"get_weights_for_serials",
			side_effect=AssertionError("must not query"),
		):
			mr_before_validate.validate_fg_serial_rows(doc)
