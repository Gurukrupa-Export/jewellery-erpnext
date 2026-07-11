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
				side_effect=lambda x, *args, **kwargs: float(x)
				if x is not None
				else 0.0,
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

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
			side_effect=[None, "HSN-1"],
		) as mock_get_value, patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
			side_effect=[
				[frappe._dict({"attribute": "Metal Type", "attribute_value": "Gold"})],
				[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
			],
		), patch(
			"jewellery_erpnext.utils.set_items_from_attribute",
			return_value=resolved_item,
		):
			result = get_item_loss_item("Test Co", "M-G-22KT-91.9-Y", "M")

		self.assertEqual(result, "M-G-22KT-91.9-Y")
		args, _kwargs = mock_get_value.call_args_list[0]
		self.assertEqual(args[0], "Variant Loss Table")
		self.assertEqual(args[1], {"variant": "M"})

	def test_throws_when_target_loss_variant_unresolvable(self):
		"""Mapping resolves to a loss_variant template, then creates the missing variant."""

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
			return_value="ML",
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
			side_effect=[
				[frappe._dict({"attribute": "Metal Type", "attribute_value": "Gold"})],
				[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
			],
		), patch(
			"jewellery_erpnext.utils.set_items_from_attribute",
			return_value=None,
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.create_loss_item",
			return_value="ML-G-22KT-91.9-Y",
		) as mock_create:
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
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
			side_effect=[expected_template, "HSN-1"],
		) as mock_get_value, patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
			side_effect=[
				[frappe._dict({"attribute": "Metal Type", "attribute_value": "Gold"})],
				[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
			],
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.set_value"
		), patch(
			"jewellery_erpnext.utils.set_items_from_attribute",
			return_value=resolved_item,
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

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_value",
			side_effect=["ML", "HSN-1"],
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_all",
			side_effect=[
				[frappe._dict({"attribute": "Metal Type", "attribute_value": "Gold"})],
				[{"item_attribute": "Metal Type", "attribute_value": "Gold"}],
			],
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.get_single_value"
		) as mock_single, patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.frappe.db.set_value"
		), patch(
			"jewellery_erpnext.utils.set_items_from_attribute",
			return_value=_loss_item_doc("ML-VARIANT", variant_of="ML"),
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
