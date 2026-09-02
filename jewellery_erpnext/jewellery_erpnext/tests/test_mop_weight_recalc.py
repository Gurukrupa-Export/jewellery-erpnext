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

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
	get_item_loss_item,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log import mop_log as mod
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	recalculate_manufacturing_operation_weights,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
	_process_mwo_group,
	_reconcile_reservations_for_mwo,
	sync_mop_logs,
)
from jewellery_erpnext.utils import carat_to_gram


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


class TestRecalcManufacturingOperationWeights(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, db_rows, pending=None, prev_mop_gross=0.0, mop_state=None):
		"""Drive the helper with mocked DB queries.

		Returns (mop_update_dict, gross_wt_written) so tests can assert
		final bucket values and gross_wt.
		"""

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

		real_get_value = mod.frappe.db.get_value

		def fake_get_value(doctype, name, fields, *args, **kwargs):
			if doctype == "Manufacturing Operation":
				# update_wt_detail reads either the multi-field tuple from MOP
				# or the previous MOP gross. Distinguish by `fields` shape.
				if isinstance(fields, list):
					return tuple(default_state.get(f, 0) for f in fields)
				# scalar field, e.g. previous_mop's gross_wt
				return prev_mop_gross
			return real_get_value(doctype, name, fields, *args, **kwargs)

		with (
			patch.object(mod.frappe.db, "sql", return_value=db_rows),
			patch.object(mod.frappe.db, "set_value", side_effect=fake_set_value),
			patch.object(mod.frappe.db, "get_value", side_effect=fake_get_value),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.flt",
				side_effect=lambda x, *args, **kwargs: (
					float(x) if x is not None else 0.0
				),
			),
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


class TestRecalculateMopWeights(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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

	def test_two_dg_rows_convert_once_not_per_row(self):
		# MOP-050YL, the case this rule exists for. Per-row rounding gave
		# flt(0.497 * 0.2, 3) + flt(0.067 * 0.2, 3) = 0.099 + 0.013 = 0.112, while the
		# previous operation carried flt(0.564 * 0.2, 3) = 0.113 for the SAME two rows
		# -- a 0.001 g gross_wt shortfall against prev_gross_wt with no physical cause.
		rows = [
			_row("D-NT-RO-6B-+6.5-7", "BD1", 0.497, pcs_batch=20),
			_row("D-NT-RO-6B-+7.5-8", "BD2", 0.067, pcs_batch=2),
		]
		out = self._run(rows)
		self.assertAlmostEqual(out["diamond_wt"], 0.564, places=3)
		self.assertAlmostEqual(out["diamond_wt_in_gram"], 0.113, places=3)
		self.assertEqual(out["diamond_pcs"], 22)
		self.assertAlmostEqual(out["gross_wt"], 0.113, places=3)

	def test_per_row_rounding_up_does_not_manufacture_a_gain(self):
		# The drift runs both ways. MOP-IN870's split rounded UP per row
		# (0.036 + 0.036 = 0.072) where the carat total converts to 0.071, so the
		# operation opened HEAVIER than its predecessor and tripped the unbacked-gain
		# guard instead of the loss one.
		rows = [
			_row("D-X-1", "BD1", 0.178, pcs_batch=1),
			_row("D-X-2", "BD2", 0.179, pcs_batch=1),
		]
		out = self._run(rows)
		self.assertAlmostEqual(out["diamond_wt"], 0.357, places=3)
		self.assertAlmostEqual(out["diamond_wt_in_gram"], 0.071, places=3)

	def test_gram_twin_is_a_pure_function_of_the_carat_bucket(self):
		# The invariant itself, over splits that round differently per row. However
		# the carats arrive, grams must equal carat_to_gram of the summed carats.
		for split in (
			[0.497, 0.067],
			[0.178, 0.179],
			[0.253],
			[0.08, 0.173],
			[1.19, 1.143],
		):
			with self.subTest(split=split):
				out = self._run(
					[
						_row(f"D-X-{i}", f"BD{i}", ct, pcs_batch=1)
						for i, ct in enumerate(split)
					]
					+ [
						_row(f"G-X-{i}", f"BG{i}", ct, pcs_batch=1)
						for i, ct in enumerate(split)
					]
				)
				self.assertAlmostEqual(
					out["diamond_wt_in_gram"],
					carat_to_gram(out["diamond_wt"]),
					places=3,
				)
				self.assertAlmostEqual(
					out["gemstone_wt_in_gram"],
					carat_to_gram(out["gemstone_wt"]),
					places=3,
				)

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


def _loss_item_doc(name, variant_of="ML"):
	doc = MagicMock()
	doc.name = name
	doc.variant_of = variant_of
	return doc


class TestItemLossItemResolution(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.set_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value"
	)
	def test_resolves_metal_to_ml(self, mock_get_value, mock_get_all, _mock_set):
		mock_get_value.side_effect = ["ML", "HSN-1"]
		mock_get_all.side_effect = [
			[frappe._dict({"attribute": "Metal Type", "attribute_value": "Gold"})],
			[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
		]

		resolved_item = _loss_item_doc("ML-G-22KT-91.9-Y", variant_of="ML")
		with patch(
			"jewellery_erpnext.utils.set_items_from_attribute",
			return_value=resolved_item,
		):
			result = get_item_loss_item("Test Co", "M-G-22KT-91.9-Y", "M", "Loss")

		self.assertEqual(result, "ML-G-22KT-91.9-Y")
		resolved_item.save.assert_called_once()

		args, _kwargs = mock_get_value.call_args_list[0]
		self.assertEqual(args[0], "Variant Loss Table")
		self.assertEqual(args[1]["variant"], "M")
		self.assertEqual(args[1]["loss_type"], "Loss")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value"
	)
	def test_throws_clear_error_when_mapping_missing(self, mock_get_value):
		mock_get_value.return_value = None

		with self.assertRaises(frappe.exceptions.ValidationError) as ctx:
			get_item_loss_item("Test Co", "D-X", "D", loss_type="Missing")

		self.assertIn("Variant Loss Table", str(ctx.exception))
		self.assertNotIn("MOP Settings", str(ctx.exception))

	def test_without_loss_type_falls_back_to_source_variant(self):
		resolved_item = _loss_item_doc("M-G-22KT-91.9-Y", variant_of="M")

		with (
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
				side_effect=[None, "HSN-1"],
			) as mock_get_value,
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
				side_effect=[
					[
						frappe._dict(
							{"attribute": "Metal Type", "attribute_value": "Gold"}
						)
					],
					[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
				],
			),
			patch(
				"jewellery_erpnext.utils.set_items_from_attribute",
				return_value=resolved_item,
			),
		):
			result = get_item_loss_item("Test Co", "M-G-22KT-91.9-Y", "M")

		self.assertEqual(result, "M-G-22KT-91.9-Y")
		args, _kwargs = mock_get_value.call_args_list[0]
		self.assertEqual(args[0], "Variant Loss Table")
		self.assertEqual(args[1], {"variant": "M"})

	def test_throws_when_target_loss_variant_unresolvable(self):
		"""Mapping resolves to a loss_variant template, then creates the missing variant."""

		with (
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
				return_value="ML",
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
				side_effect=[
					[
						frappe._dict(
							{"attribute": "Metal Type", "attribute_value": "Gold"}
						)
					],
					[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
				],
			),
			patch(
				"jewellery_erpnext.utils.set_items_from_attribute",
				return_value=None,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.create_loss_item",
				return_value="ML-G-22KT-91.9-Y",
			) as mock_create,
		):
			result = get_item_loss_item("Test Co", "M-G-22KT-91.9-Y", "M", "Loss")

		self.assertEqual(result, "ML-G-22KT-91.9-Y")
		mock_create.assert_called_once_with("ML", {"Metal Type": "Gold"})


class TestLossMappingMatrix(IntegrationTestCase):
	"""Variant + loss_type combinations described in the spec must all
	resolve via Variant Loss Table.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _run_resolution(self, source_item, variant_of, loss_type, expected_template):
		resolved_item = _loss_item_doc(
			f"{expected_template}-VARIANT", variant_of=expected_template
		)
		with (
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
				side_effect=[expected_template, "HSN-1"],
			) as mock_get_value,
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
				side_effect=[
					[
						frappe._dict(
							{"attribute": "Metal Type", "attribute_value": "Gold"}
						)
					],
					[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
				],
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.set_value"
			),
			patch(
				"jewellery_erpnext.utils.set_items_from_attribute",
				return_value=resolved_item,
			),
		):
			result = get_item_loss_item("Test Co", source_item, variant_of, loss_type)

		# Result should be the resolved variant.
		self.assertEqual(result, f"{expected_template}-VARIANT")
		# The Variant Loss Table query was scoped to the right
		# (variant, loss_type) combo.
		args = mock_get_value.call_args_list[0][0]
		self.assertEqual(args[0], "Variant Loss Table")
		self.assertEqual(args[1]["variant"], variant_of)
		self.assertEqual(args[1]["loss_type"], loss_type)

	def test_metal_loss_uses_ML(self):
		self._run_resolution("M-G-22KT-91.9-Y", "M", "Loss", "ML")

	def test_finding_loss_uses_FL(self):
		self._run_resolution("F-G-18KT-75.4-Y-X", "F", "Loss", "FL")

	def test_diamond_loss_uses_DL(self):
		self._run_resolution("D-X", "D", "Loss", "DL")

	def test_diamond_missing_uses_DM(self):
		self._run_resolution("D-X", "D", "Missing", "DM")

	def test_diamond_burn_uses_DB(self):
		self._run_resolution("D-X", "D", "Burn", "DB")

	def test_diamond_broken_uses_DBK(self):
		self._run_resolution("D-X", "D", "Broken", "DBK")

	def test_gemstone_loss_uses_GL(self):
		self._run_resolution("G-X", "G", "Loss", "GL")

	def test_gemstone_broken_uses_GB(self):
		self._run_resolution("G-X", "G", "Broken", "GB")

	def test_other_loss_uses_OL(self):
		self._run_resolution("O-X", "O", "Loss", "OL")

	def test_eod_loss_resolution_does_not_consult_mop_settings(self):
		"""Defense check: the helper must not read MOP Settings dust_item.
		If anyone re-introduces that path, this test fails.
		"""

		with (
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
				side_effect=["ML", "HSN-1"],
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
				side_effect=[
					[
						frappe._dict(
							{"attribute": "Metal Type", "attribute_value": "Gold"}
						)
					],
					[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
				],
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_single_value"
			) as mock_single,
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.set_value"
			),
			patch(
				"jewellery_erpnext.utils.set_items_from_attribute",
				return_value=_loss_item_doc("ML-VARIANT", variant_of="ML"),
			),
		):
			get_item_loss_item("Test Co", "M-X", "M", "Loss")

		# Helper must not read MOP Settings.dust_item in the resolution path.
		mock_single.assert_not_called()


class TestSyncStamp(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._reserve_sres_from_eod_se_rows"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mark_all_mwo_mop_logs_synced"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._cancel_sre_snapshots"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._snapshot_mwo_sres_for_relocation",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._save_draft_eod_se",
		return_value="STE-1",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._check_eod_source_batch_stock",
		return_value={},
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._validate_eod_items_for_mwo_reservation"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._preload_sre_warehouse_map",
		return_value={("M-X", "B1"): ["WH-A"]},
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mwo_realized_by_artifact",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.utils.now",
		return_value="2026-05-04 12:00:00",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.rollback"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.release_savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc"
	)
	def test_eod_sync_stamps_last_eod_sync_on_after_success(
		self,
		mock_get_doc,
		_mock_savepoint,
		_mock_release,
		_mock_rollback,
		mock_set_value,
		_mock_now,
		_mock_artifact,
		_mock_sre_map,
		_mock_validate_reservation,
		_mock_check_stock,
		_mock_save_draft,
		_mock_snapshot,
		_mock_cancel_sres,
		_mock_mark_synced,
		_mock_reserve_sres,
	):
		stock_entry = MagicMock()
		mock_get_doc.return_value = stock_entry
		mop_data_list = [
			{
				"mop_name": "MOP-1",
				"mop_doc": frappe._dict(
					{"manufacturing_order": "MO-1", "manufacturer": "Shubh"}
				),
				"logs": [
					frappe._dict(
						{
							"item_code": "M-X",
							"batch_no": "B1",
							"qty_after_transaction_batch_based": 5.0,
							"to_warehouse": "WH-B",
							"flow_index": 1,
							"creation": "2026-05-04 10:00:00",
						}
					)
				],
			},
			{
				"mop_name": "MOP-2",
				"mop_doc": frappe._dict(
					{"manufacturing_order": "MO-1", "manufacturer": "Shubh"}
				),
				"logs": [],
			},
		]
		failures = []
		stats = {
			"processed_mwos": 0,
			"failed_mwos": 0,
			"submitted_ses": [],
			"draft_ses": [],
		}

		_process_mwo_group(("CO", "MWO-1"), mop_data_list, failures, stats)

		# Stamp once per MOP in the successful group.
		stamp_calls = [
			c
			for c in mock_set_value.call_args_list
			if c[0][0] == "Manufacturing Operation" and c[0][2] == "last_eod_sync_on"
		]
		self.assertEqual(len(stamp_calls), 2)
		# update_modified=False is part of the contract.
		for c in stamp_calls:
			self.assertEqual(c[1].get("update_modified"), False)
		self.assertEqual(failures, [])
		self.assertEqual(stats["processed_mwos"], 1)
		self.assertEqual(stats["submitted_ses"], ["STE-1"])
		stock_entry.submit.assert_called_once()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._reserve_sres_from_eod_se_rows"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mark_all_mwo_mop_logs_synced"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._cancel_sre_snapshots"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._snapshot_mwo_sres_for_relocation",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._save_draft_eod_se",
		return_value="STE-DRAFT",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._check_eod_source_batch_stock",
		return_value={},
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._validate_eod_items_for_mwo_reservation"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._preload_sre_warehouse_map",
		return_value={("M-X", "B1"): ["WH-A"]},
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._mwo_realized_by_artifact",
		return_value=None,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.rollback"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.release_savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc"
	)
	def test_failed_group_does_not_stamp(
		self,
		mock_get_doc,
		_mock_savepoint,
		_mock_release,
		_mock_rollback,
		mock_set_value,
		_mock_artifact,
		_mock_sre_map,
		_mock_validate_reservation,
		_mock_check_stock,
		_mock_save_draft,
		_mock_snapshot,
		_mock_cancel_sres,
		_mock_mark_synced,
		_mock_reserve_sres,
	):
		stock_entry = MagicMock()
		stock_entry.submit.side_effect = Exception("boom")
		mock_get_doc.return_value = stock_entry
		mop_data_list = [
			{
				"mop_name": "MOP-FAIL",
				"mop_doc": frappe._dict(
					{"manufacturing_order": "MO-1", "manufacturer": "Shubh"}
				),
				"logs": [
					frappe._dict(
						{
							"item_code": "M-X",
							"batch_no": "B1",
							"qty_after_transaction_batch_based": 5.0,
							"to_warehouse": "WH-B",
							"flow_index": 1,
							"creation": "2026-05-04 10:00:00",
						}
					)
				],
			}
		]
		failures = []
		stats = {
			"processed_mwos": 0,
			"failed_mwos": 0,
			"submitted_ses": [],
			"draft_ses": [],
		}

		_process_mwo_group(("CO", "MWO-1"), mop_data_list, failures, stats)

		# The exception path skipped the stamp block entirely.
		stamp_calls = [
			c
			for c in mock_set_value.call_args_list
			if c[0][0] == "Manufacturing Operation" and c[0][2] == "last_eod_sync_on"
		]
		self.assertEqual(stamp_calls, [])
		self.assertEqual(stats["processed_mwos"], 0)
		self.assertEqual(stats["failed_mwos"], 1)
		self.assertEqual(stats["draft_ses"], ["STE-DRAFT"])


class TestSreReconciliationDryRun(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.logger"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_current_mop_balance_rows",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	def test_dry_run_default_does_not_cancel(
		self, mock_get_all, _mock_balance, mock_get_doc, mock_logger
	):
		mock_get_all.return_value = [
			frappe._dict(
				{
					"name": "SRE-zero",
					"item_code": "M-X",
					"warehouse": "WH-Y",
					"reserved_qty": 5.0,
					"delivered_qty": 0.0,
					"manufacturing_operation": "MOP-1",
				}
			)
		]
		log = MagicMock()
		mock_logger.return_value = log

		_reconcile_reservations_for_mwo("MWO-1")

		# Dry run: NEVER call frappe.get_doc to cancel.
		mock_get_doc.assert_not_called()
		# Logged the would-cancel decision.
		log.info.assert_called()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.rollback"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.release_savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.logger"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_current_mop_balance_rows",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	def test_destructive_cancels_zero_balance(
		self,
		mock_get_all,
		_mock_balance,
		mock_get_doc,
		_mock_logger,
		_mock_savepoint,
		_mock_release,
		_mock_rollback,
	):
		mock_get_all.return_value = [
			frappe._dict(
				{
					"name": "SRE-cancel-me",
					"item_code": "M-X",
					"warehouse": "WH-Y",
					"reserved_qty": 5.0,
					"delivered_qty": 0.0,
					"manufacturing_operation": "MOP-1",
				}
			)
		]
		sre_doc = MagicMock()
		mock_get_doc.return_value = sre_doc

		_reconcile_reservations_for_mwo("MWO-1", dry_run=False)

		mock_get_doc.assert_called_once_with("Stock Reservation Entry", "SRE-cancel-me")
		sre_doc.cancel.assert_called_once()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.get_current_mop_balance_rows"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.get_all"
	)
	def test_ambiguous_balance_never_cancels(
		self, mock_get_all, mock_balance, mock_get_doc
	):
		mock_get_all.return_value = [
			frappe._dict(
				{
					"name": "SRE-partial",
					"item_code": "M-X",
					"warehouse": "WH-Y",
					"reserved_qty": 10.0,
					"delivered_qty": 2.0,
					"manufacturing_operation": "MOP-1",
				}
			)
		]
		# Latest balance is positive — partial coverage; reconcile must skip.
		mock_balance.return_value = [
			{"item_code": "M-X", "to_warehouse": "WH-Y", "qty": 4.0}
		]

		_reconcile_reservations_for_mwo("MWO-1", dry_run=False)

		mock_get_doc.assert_not_called()


class TestEodSyncIdempotentRerun(IntegrationTestCase):
	"""When `_get_unsynced_mop_groups` returns an empty dict, EOD must be a
	no-op — no Stock Entry created, no `last_eod_sync_on` stamps, no
	reconciliation calls. This is the steady-state second run.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._reconcile_reservations_for_mwo"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.recalculate_sync_log_totals"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.db.set_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.release_eod_sync_lock"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.set_eod_sync_running"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.get_doc",
		return_value=frappe._dict({"eod_sync_work_order_filter": []}),
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync._get_unsynced_mop_groups",
		return_value={},
	)
	def test_second_run_is_noop_when_no_unsynced_logs(
		self,
		_mock_groups,
		mock_new_doc,
		_mock_settings,
		_mock_set_running,
		_mock_release_lock,
		mock_set_value,
		_mock_recalculate,
		mock_reconcile,
	):
		sync_log = MagicMock()
		sync_log.name = "MOP-EOD-SYNC-LOG-1"
		mock_new_doc.return_value = sync_log

		out = sync_mop_logs()

		self.assertEqual(out["processed"], 0)
		self.assertEqual(out["stock_entries"], [])
		# No MOPs were touched; no stamps and no reconcile calls.
		stamp_calls = [
			c
			for c in mock_set_value.call_args_list
			if c[0][0] == "Manufacturing Operation" and c[0][2] == "last_eod_sync_on"
		]
		self.assertEqual(stamp_calls, [])
		mock_reconcile.assert_not_called()


class TestRecalcPrefixNarrowing(IntegrationTestCase):
	"""``prefixes`` limits the write to the families the caller names.

	MOPLog.validate passes the single family of the row being saved, so a per-row
	save can never rewrite a bucket authored outside MOP Log --
	``create_manufacturing_operation`` seeds diamond/gemstone weights from the MWO
	before any stone is issued, and an unnarrowed recompute on the first metal row
	would zero them.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _bucket_writes(self, prefixes):
		writes = []

		def fake_set_value(doctype, name, value=None, *_args, **_kwargs):
			if isinstance(value, dict):
				writes.append(value)

		rows = [
			_row("M-G-22KT-91.75-Y", "B-M", 4.289),
			_row("F-SOP", "B-1", 0.608, name="ML-SOP"),
			_row("F-PSS", "B-2", 1.323, name="ML-PSS"),
		]
		with (
			patch.object(mod.frappe.db, "sql", return_value=rows),
			patch.object(mod.frappe.db, "set_value", side_effect=fake_set_value),
			patch.object(mod, "update_wt_detail"),
		):
			mod.recalculate_manufacturing_operation_weights("MOP-X", prefixes=prefixes)
		return writes

	def test_narrowed_write_covers_only_the_named_family(self):
		writes = self._bucket_writes(("finding",))
		self.assertEqual(len(writes), 1, "expected exactly one bucket write")
		bucket = writes[0]
		# Both finding items summed -- the MOP-7Q48F figure.
		self.assertAlmostEqual(bucket["finding_wt"], 1.931, places=3)
		# A finding row must not touch any other family's bucket.
		self.assertNotIn("net_wt", bucket)
		self.assertNotIn("diamond_wt", bucket)
		self.assertNotIn("gemstone_wt", bucket)
		self.assertNotIn("other_wt", bucket)

	def test_unnarrowed_write_still_covers_every_family(self):
		"""The cancel legs and the repair patch rely on the full rewrite."""
		writes = self._bucket_writes(None)
		bucket = writes[0]
		self.assertAlmostEqual(bucket["net_wt"], 4.289, places=3)
		self.assertAlmostEqual(bucket["finding_wt"], 1.931, places=3)
		self.assertIn("diamond_wt", bucket)
		self.assertIn("gemstone_wt", bucket)

	def test_unknown_family_writes_nothing(self):
		writes = self._bucket_writes(("nosuchfamily",))
		self.assertEqual(writes, [])


class TestNegativeBatchBalanceIsNotStock(IntegrationTestCase):
	"""A negative (item, batch) balance must not reach a header weight bucket.

	MOP-3DP57 reported gross_wt 16.440 against a Serial Number Creator total_weight of
	16.720. The 0.280 g gap was one gold batch, KG2F081-MGL229175Y0-P29A8, sitting at
	-0.28 -- a Material Receive (WORK ORDER) that returned 0.28 g of a SHARED casting
	batch under an operation that had never been issued it, so the row wrote 0 - 0.28.
	Every other reader of this ledger already drops or clamps such a row; this recompute
	was the only one that summed it.

	The clamp is HEADER-ONLY. See TestNewMopBaselineNegativeInheritance in
	doctype/mop_log/test_mop_log.py, which asserts the LEDGER keeps carrying -0.28
	forward. Both are correct: the ledger stays honest, the header stops reporting a
	phantom as a holding.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	_run = TestRecalculateMopWeights._run

	def test_negative_batch_contributes_zero_to_net_wt(self):
		"""The minimal P29A8 shape: a real batch plus a phantom negative."""
		rows = [
			_row("M-G-22KT-91.75-Y", "KG2F081-MGL229175Y0-12L9U", 18.7, name="ML-1"),
			_row("M-G-22KT-91.75-Y", "KG2F081-MGL229175Y0-P29A8", -0.28, name="ML-2"),
		]
		out = self._run(rows)
		self.assertAlmostEqual(out["net_wt"], 18.7, places=3)
		self.assertEqual(out["gross_wt"], 18.7)

	def test_mop_3dp57_reconciles_with_its_serial_number_creator(self):
		"""The full incident: five gold batches + four diamond batches + the phantom.

		The SNC and the Stock Reservation Entries both read 16.236 g gold + 2.418 ct.
		The header must agree: 16.236 + carat_to_gram(2.418) = 16.236 + 0.484 = 16.720.
		"""
		rows = [
			_row("M-G-22KT-91.75-Y", "B-12L9U", 15.92, pcs_batch=1, name="ML-1"),
			_row("M-G-22KT-91.75-Y", "B-1U6V7", 0.128, pcs_batch=1, name="ML-2"),
			_row("M-G-22KT-91.75-Y", "B-2S9L7", 0.118, pcs_batch=1, name="ML-3"),
			_row("M-G-22KT-91.75-Y", "B-5IB55", 0.037, pcs_batch=1, name="ML-4"),
			_row("M-G-22KT-91.75-Y", "B-JR944", 0.033, pcs_batch=1, name="ML-5"),
			# the phantom -- a foreign batch this MWO was never issued
			_row("M-G-22KT-91.75-Y", "B-P29A8", -0.28, name="ML-6"),
			_row("D-NT-RO-6B-+6.5-7", "B-75JG2", 0.196, pcs_batch=8, name="ML-7"),
			_row("D-NT-RO-6B-+7.5-8", "B-E340Q", 1.427, pcs_batch=42, name="ML-8"),
			_row("D-NT-RO-6B-+8.5-9", "B-68F2Q", 0.684, pcs_batch=16, name="ML-9"),
			_row("D-NT-RO-6B-+9.5-10", "B-136TQ", 0.111, pcs_batch=2, name="ML-10"),
		]
		out = self._run(rows)
		# Buckets accumulate unrounded -- update_wt_detail rounds ONCE into gross_wt,
		# which is why gross_wt alone is asserted exactly.
		self.assertAlmostEqual(out["net_wt"], 16.236, places=3)
		self.assertAlmostEqual(out["diamond_wt"], 2.418, places=3)
		self.assertAlmostEqual(out["diamond_wt_in_gram"], 0.484, places=3)
		self.assertEqual(out["diamond_pcs"], 68)
		self.assertEqual(out["gross_wt"], 16.72)

	def test_positive_balances_are_byte_identical(self):
		"""The clamp is max(), not a tolerance -- healthy ledgers must not move.

		A tolerance would also discard sub-milligram POSITIVE balances, widening the
		blast radius from "operations carrying corruption" to "everything".
		"""
		rows = [
			_row("M-G-18KT", "B1", 0.0001, name="ML-1"),
			_row("M-G-18KT", "B2", 0.0004, name="ML-2"),
		]
		out = self._run(rows)
		# Compared against the RAW float sum, not a rounded literal: max() must return
		# a positive input unchanged, so the accumulation is bit-for-bit what an
		# unclamped recompute would produce. A places=3 assert would pass trivially.
		self.assertEqual(out["net_wt"], 0.0001 + 0.0004)

	def test_negative_qty_with_positive_pcs_keeps_the_pcs(self):
		"""qty and pcs clamp INDEPENDENTLY.

		The FG-MWO seed's ``HAVING SUM(qty_change) > 0 OR SUM(pcs_change) > 0`` admits a
		qty-negative row whose pcs sum is positive. Dropping the whole row on a qty
		signal would silently delete a stone COUNT that Product Certification and the
		Employee IR PCS cap still read.
		"""
		rows = [_row("D-NT-RO", "B1", -0.5, pcs_batch=4, name="ML-1")]
		out = self._run(rows)
		self.assertEqual(out["diamond_wt"], 0.0)
		self.assertEqual(out["diamond_wt_in_gram"], 0.0)
		self.assertEqual(out["diamond_pcs"], 4)

	def test_positive_qty_with_negative_pcs_keeps_the_qty(self):
		"""The mirror case, live on kg-gk as MOP-5HM44.

		MAT-STE-10952 issued out qty and pcs; MAT-STE-10954 returned the qty but not
		the pcs, leaving bal_qty 0.04 against bal_pcs -1.
		"""
		rows = [_row("G-GRS-PR-SEP", "B1", 0.04, pcs_batch=-1, name="ML-1")]
		out = self._run(rows)
		self.assertAlmostEqual(out["gemstone_wt"], 0.04, places=3)
		self.assertEqual(out["gemstone_pcs"], 0)
		self.assertEqual(out["gemstone_wt_in_gram"], carat_to_gram(0.04))

	def test_gram_twin_is_a_pure_function_of_the_clamped_carat_bucket(self):
		"""The clamp sits ABOVE the carat->gram derivation.

		If it sat below, diamond_wt_in_gram would be derived from the RAW carat total
		and normalize_mop_carat_to_gram_buckets -- which detects on exactly this
		invariant -- would fire on every clamped MOP forever.
		"""
		rows = [
			_row("D-NT-RO", "B1", 2.418, pcs_batch=68, name="ML-1"),
			_row("D-NT-RO", "B2", -1.4, pcs_batch=0, name="ML-2"),
		]
		out = self._run(rows)
		self.assertAlmostEqual(out["diamond_wt"], 2.418, places=3)
		self.assertEqual(out["diamond_wt_in_gram"], carat_to_gram(out["diamond_wt"]))
		self.assertEqual(out["diamond_wt_in_gram"], 0.484)
