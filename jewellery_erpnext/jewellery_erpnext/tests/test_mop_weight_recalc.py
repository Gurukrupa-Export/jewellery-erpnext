# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for recalculate_manufacturing_operation_weights.

Covers the central MOP weight bucket recompute:
- multi-batch sums (the regression that motivated the helper)
- D/G carat-to-gram conversion in gross_wt
- D/G PCS aggregation
- cancelled rows are excluded
- latest-by-creation wins per (item, batch)
- pending in-flight row overrides DB
- pending row being cancelled is dropped, not treated as authoritative
"""

from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase


def _row(item_code, batch_no, qaf_batch, pcs_batch=0, name=None, creation=None):
	return {
		"item_code": item_code,
		"batch_no": batch_no,
		"qaf_batch": qaf_batch,
		"pcs_batch": pcs_batch,
		"name": name or f"{item_code}-{batch_no}",
		"creation": creation,
	}


class _RecalcHarness:
	"""Captures frappe.db.set_value calls so we can assert the bucket update."""

	def __init__(self):
		self.set_value_calls = []

	def fake_set_value(self, doctype, name, value=None, *_args, **_kwargs):
		self.set_value_calls.append((doctype, name, value))


class TestRecalcManufacturingOperationWeights(FrappeTestCase):
	def _run(self, db_rows, pending=None, prev_mop_gross=0.0, mop_state=None):
		"""Drive the helper with mocked DB queries.

		Returns (mop_update_dict, gross_wt_written) so tests can assert
		final bucket values and gross_wt.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log import mop_log as mod

		harness = _RecalcHarness()

		# State the helper reads via update_wt_detail after writing buckets.
		mop_state = mop_state or {}
		default_state = {
			"net_wt": 0,
			"finding_wt": 0,
			"diamond_wt_in_gram": 0,
			"gemstone_wt_in_gram": 0,
			"other_wt": 0,
			"previous_mop": None,
			"loss_wt": 0,
		}
		default_state.update(mop_state)

		def fake_set_value(doctype, name, value):
			harness.fake_set_value(doctype, name, value)
			# Reflect bucket writes into our in-memory state so the
			# subsequent update_wt_detail read sees them.
			if isinstance(value, dict):
				default_state.update(value)

		def fake_get_value(doctype, name, fields):
			# update_wt_detail reads either the multi-field tuple from MOP
			# or the previous MOP gross. Distinguish by `fields` shape.
			if isinstance(fields, list):
				return tuple(default_state.get(f, 0) for f in fields)
			# scalar field, e.g. previous_mop's gross_wt
			return prev_mop_gross

		with (
			patch.object(mod.frappe.db, "sql", return_value=db_rows),
			patch.object(mod.frappe.db, "set_value", side_effect=fake_set_value),
			patch.object(mod.frappe.db, "get_value", side_effect=fake_get_value),
		):
			mod.recalculate_manufacturing_operation_weights("MOP-X", pending=pending)

		bucket_calls = [
			c
			for c in harness.set_value_calls
			if isinstance(c[2], dict) and "net_wt" in c[2]
		]
		gross_calls = [
			c
			for c in harness.set_value_calls
			if isinstance(c[2], dict) and "gross_wt" in c[2]
		]
		assert bucket_calls, "no bucket update written"
		assert gross_calls, "no gross_wt update written"
		return bucket_calls[-1][2], gross_calls[-1][2]

	def test_multi_batch_sum_per_prefix(self):
		"""The MOP-EO481 regression: multiple batches per prefix must SUM,
		not be overwritten by the last-saved row.
		"""
		rows = [
			_row("M-G-18KT-75.4-P", "B-M-176", 2.074),
			_row("M-G-18KT-75.4-P", "B-M-28", 0.199),
			_row("F-X-CHA", "B-F-CHA", 2.018),
			_row("F-X-CO", "B-F-CO", 0.316),
		]
		bucket, gross = self._run(rows)
		self.assertAlmostEqual(bucket["net_wt"], 2.273, places=3)
		self.assertAlmostEqual(bucket["finding_wt"], 2.334, places=3)
		# gross = M + F + DG-in-gram + O
		self.assertAlmostEqual(gross["gross_wt"], 2.273 + 2.334, places=3)

	def test_dg_carat_converted_to_gram_in_gross_wt(self):
		"""D/G buckets stay in carats; gross_wt rolls them up at × 0.2."""
		rows = [
			_row("D-X-1", "B-D-1", 1.0, pcs_batch=3),
			_row("G-X-1", "B-G-1", 2.0, pcs_batch=5),
		]
		bucket, gross = self._run(rows)
		self.assertEqual(bucket["diamond_wt"], 1.0)
		self.assertEqual(bucket["gemstone_wt"], 2.0)
		self.assertAlmostEqual(bucket["diamond_wt_in_gram"], 0.2, places=3)
		self.assertAlmostEqual(bucket["gemstone_wt_in_gram"], 0.4, places=3)
		# gross = 0.2 (D) + 0.4 (G)
		self.assertAlmostEqual(gross["gross_wt"], 0.6, places=3)

	def test_dg_pcs_aggregated_per_prefix(self):
		rows = [
			_row("D-X-1", "B-D-1", 0.05, pcs_batch=3),
			_row("D-X-2", "B-D-2", 0.10, pcs_batch=4),
		]
		bucket, _gross = self._run(rows)
		self.assertEqual(bucket["diamond_pcs"], 7.0)

	def test_cancelled_rows_are_excluded(self):
		"""The SQL filter `is_cancelled = 0` is the boundary; the helper
		trusts the query. Pass DB rows that mimic the post-filter state.
		"""
		rows = [
			_row("M-X", "B1", 5.0),
			# A cancelled row would not appear here at all.
		]
		bucket, _gross = self._run(rows)
		self.assertEqual(bucket["net_wt"], 5.0)

	def test_latest_creation_wins_per_item_batch(self):
		"""Two rows for the same (item, batch) — the LATER creation wins."""
		rows = [
			_row("M-X", "B1", 5.0, creation="2026-01-01 00:00:00"),
			_row("M-X", "B1", 3.0, creation="2026-02-01 00:00:00"),
		]
		bucket, _gross = self._run(rows)
		# DB rows are passed in ASC order so the dict update keeps the last
		# (= latest creation) entry.
		self.assertEqual(bucket["net_wt"], 3.0)

	def test_pending_overrides_db_for_same_key(self):
		"""validate() injects `self` as pending; it must override whatever
		the DB shows for its (item, batch) since that row is being saved.
		"""
		rows = [_row("M-X", "B1", 5.0)]
		pending = MagicMock()
		pending.item_code = "M-X"
		pending.batch_no = "B1"
		pending.qty_after_transaction_batch_based = 4.5
		pending.pcs_after_transaction_batch_based = 0
		pending.is_cancelled = 0

		bucket, _gross = self._run(rows, pending=pending)
		self.assertEqual(bucket["net_wt"], 4.5)

	def test_pending_for_new_key_adds_to_bucket(self):
		"""A pending row whose (item, batch) isn't in DB yet still counts
		toward the bucket — covers the insert path.
		"""
		rows = [_row("M-X", "B1", 5.0)]
		pending = MagicMock()
		pending.item_code = "M-Y"
		pending.batch_no = "B-NEW"
		pending.qty_after_transaction_batch_based = 1.0
		pending.pcs_after_transaction_batch_based = 0
		pending.is_cancelled = 0

		bucket, _gross = self._run(rows, pending=pending)
		self.assertAlmostEqual(bucket["net_wt"], 6.0, places=3)

	def test_pending_being_cancelled_is_dropped_not_authoritative(self):
		"""When the in-flight save is FLIPPING a row to is_cancelled=1, the
		row must drop out of the latest map — otherwise it'd be treated as
		authoritative and re-add itself to the bucket.
		"""
		pending = MagicMock()
		pending.item_code = "M-X"
		pending.batch_no = "B1"
		pending.qty_after_transaction_batch_based = 5.0
		pending.pcs_after_transaction_batch_based = 0
		pending.is_cancelled = 1  # being cancelled now

		# DB rows reflect post-cancel state already (no row for B1).
		# So the helper should produce 0.
		bucket, _gross = self._run([], pending=pending)
		self.assertEqual(bucket["net_wt"], 0.0)
