# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for recalculate_manufacturing_operation_weights.

Mocks frappe.db.sql (active MOP Log rows) and frappe.db.set_value /
frappe.db.get_value (Manufacturing Operation reads/writes), so the aggregation
logic can be exercised without a site fixture.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	recalculate_manufacturing_operation_weights,
)


def _row(
	item_code,
	batch_no,
	qaf_batch,
	pcs_batch=0,
	name="ML-X",
	creation="2026-01-01 00:00:00",
):
	return {
		"item_code": item_code,
		"batch_no": batch_no,
		"name": name,
		"qaf_batch": qaf_batch,
		"pcs_batch": pcs_batch,
		"creation": creation,
	}


class TestRecalculateMopWeights(FrappeTestCase):
	def _run(self, rows, pending=None):
		# Capture writes; the helper calls set_value once for buckets, then
		# update_wt_detail does a get_value for the bucket fields and a
		# set_value for gross_wt + prev_gross_wt.
		writes = []

		orig_get_value = frappe.db.get_value

		def fake_set_value(doctype, name, update, *args, **kwargs):
			writes.append((doctype, name, update))

		def fake_get_value(doctype, name, fields, *args, **kwargs):
			if doctype != "Manufacturing Operation":
				return orig_get_value(doctype, name, fields, *args, **kwargs)

			# Replay the latest write to the same MOP for the requested fields.
			merged = {}
			for d, n, u in writes:
				if d == "Manufacturing Operation" and n == name and isinstance(u, dict):
					merged.update(u)
			# previous_mop is not under test here; treat as None.
			merged.setdefault("previous_mop", None)
			merged.setdefault("loss_wt", 0)

			if isinstance(fields, str):
				return merged.get(fields)
			return tuple(merged.get(f) for f in fields)

		with (
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.sql",
				return_value=rows,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.set_value",
				side_effect=fake_set_value,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_value",
				side_effect=fake_get_value,
			),
			patch(
				"frappe.get_system_settings",
				return_value="Banker's Rounding (legacy)",
			),
		):
			recalculate_manufacturing_operation_weights("MOP-TEST", pending=pending)
		# Merge all bucket writes into a single dict for assertions.
		merged = {}
		for _d, _n, u in writes:
			if isinstance(u, dict):
				merged.update(u)
		return merged

	def test_multi_batch_single_prefix_sums(self):
		# Two M batches on the same MOP. Old behavior would have stored only
		# the latest single-row balance into net_wt; the helper must sum.
		rows = [
			_row("M-G-18KT", "B1", 2.074, name="ML-1"),
			_row("M-G-18KT", "B2", 0.199, name="ML-2"),
		]
		out = self._run(rows)
		self.assertAlmostEqual(out["net_wt"], 2.273, places=3)
		self.assertAlmostEqual(out["gross_wt"], 2.273, places=3)

	def test_multi_prefix_gross_wt_is_sum_of_buckets(self):
		# M + F + D in carats; gross_wt should include D in grams (×0.2).
		rows = [
			_row("M-X", "BM", 2.273),
			_row("F-X", "BF", 2.334),
			_row("D-X", "BD", 0.253, pcs_batch=4),
		]
		out = self._run(rows)
		self.assertAlmostEqual(out["net_wt"], 2.273, places=3)
		self.assertAlmostEqual(out["finding_wt"], 2.334, places=3)
		self.assertAlmostEqual(out["diamond_wt"], 0.253, places=3)
		self.assertAlmostEqual(out["diamond_wt_in_gram"], 0.051, places=3)
		self.assertEqual(out["diamond_pcs"], 4)
		self.assertAlmostEqual(out["gross_wt"], 2.273 + 2.334 + 0.051, places=3)

	def test_dg_carat_to_gram_in_gross_wt(self):
		# 1 ct D + 2 ct G -> 0.2 + 0.4 = 0.6 g of gross_wt contribution.
		rows = [
			_row("D-X", "BD", 1.0, pcs_batch=1),
			_row("G-X", "BG", 2.0, pcs_batch=2),
		]
		out = self._run(rows)
		self.assertAlmostEqual(out["diamond_wt_in_gram"], 0.200, places=3)
		self.assertAlmostEqual(out["gemstone_wt_in_gram"], 0.400, places=3)
		self.assertAlmostEqual(out["gross_wt"], 0.600, places=3)

	def test_latest_qty_after_per_batch_wins(self):
		# Multiple rows for the same (item, batch); the dict-based "last write
		# wins" after ORDER BY creation ASC means the newest row's
		# qaf_batch is the active balance. We feed rows in chronological order.
		rows = [
			_row("M-X", "B1", 5.0, creation="2026-01-01"),
			_row("M-X", "B1", 4.0, creation="2026-01-02"),  # later — wins
		]
		out = self._run(rows)
		self.assertAlmostEqual(out["net_wt"], 4.000, places=3)
		self.assertAlmostEqual(out["gross_wt"], 4.000, places=3)

	def test_pending_row_overrides_db_for_same_key(self):
		# In MOPLog.validate the in-flight row is not yet in DB; pending must
		# be treated as authoritative for its (item, batch).
		rows = [_row("M-X", "B1", 5.0)]
		pending = SimpleNamespace(
			item_code="M-X",
			batch_no="B1",
			qty_after_transaction_batch_based=4.5,
			pcs_after_transaction_batch_based=0,
			is_cancelled=0,
		)
		out = self._run(rows, pending=pending)
		self.assertAlmostEqual(out["net_wt"], 4.500, places=3)

	def test_cancelled_pending_row_does_not_override(self):
		# When a row is being cancelled (is_cancelled=1 in pending), it drops
		# out of the balance completely.
		rows = [_row("M-X", "B1", 5.0)]
		pending = SimpleNamespace(
			item_code="M-X",
			batch_no="B1",
			qty_after_transaction_batch_based=999.0,  # bogus; must be ignored
			pcs_after_transaction_batch_based=0,
			is_cancelled=1,
		)
		out = self._run(rows, pending=pending)
		self.assertAlmostEqual(out["net_wt"], 0.000, places=3)

	def test_loss_row_reduces_gross_wt_exactly_once(self):
		# Simulates the post-loss state on MOP-EO481: latest active balance per
		# (item, batch) is the loss-row qty_after_batch. Total = sum of those
		# = 4.658 g. Verifies the post-loss MOP gross_wt matches the expected
		# real balance after the j59i8rf5m8-1 loss attribution.
		rows = [
			_row("M-G-18KT-75.4-P", "GE2D081-MGL18754P0-176", 2.074),
			_row("M-G-18KT-75.4-P", "GE2D081-MGL18754P0-28", 0.199),
			_row("F-CHA", "GE2D075-FGL754P0182FCC90MM0-02", 2.018),
			_row("F-CO", "GE2D075-FGL754P0184KKCJ00MM0-01", 0.316),
			_row("D-NT-RO-6B-+4-4.5", "GE2E094-DNTROX6D00D05-J70O9", 0.08, pcs_batch=1),
			_row(
				"D-NT-RO-6B-+4.5-5", "GE2E094-DNTROX6D05E00-261XA", 0.173, pcs_batch=1
			),
		]
		out = self._run(rows)
		# 2.074 + 0.199 + 2.018 + 0.316 + 0.253*0.2 = 4.6576 -> rounds to 4.658
		self.assertAlmostEqual(out["gross_wt"], 4.658, places=3)
		self.assertAlmostEqual(out["net_wt"], 2.273, places=3)
		self.assertAlmostEqual(out["finding_wt"], 2.334, places=3)
		self.assertAlmostEqual(out["diamond_wt_in_gram"], 0.051, places=3)

	def test_no_active_rows_yields_zero_buckets(self):
		out = self._run([])
		self.assertEqual(out["net_wt"], 0.0)
		self.assertEqual(out["finding_wt"], 0.0)
		self.assertEqual(out["diamond_wt"], 0.0)
		self.assertEqual(out["diamond_wt_in_gram"], 0.0)
		self.assertEqual(out["gemstone_wt"], 0.0)
		self.assertEqual(out["gross_wt"], 0.0)

	def test_unknown_prefix_skipped(self):
		# Item code with prefix outside FIELD_MAP must not crash and must not
		# contribute to any bucket.
		rows = [
			_row("X-FOO", "B?", 99.0),
			_row("M-X", "BM", 1.0),
		]
		out = self._run(rows)
		self.assertAlmostEqual(out["net_wt"], 1.000, places=3)
		self.assertAlmostEqual(out["gross_wt"], 1.000, places=3)
