# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Recovery Summary <-> Gold Recovery Details consistency.

Regression cover for the defect where a plain no-op re-save of a Refining Entry inflated
``refining_loss`` — on one real document from 0.033 g to 0.860 g, on another from 0.000 g to
1.667 g. That field is not a statistic: ``create_repack_se`` mints exactly that many grams of
pure-24KT loss dust as a Stock Entry output row, so an inflated value mints gold that was
never lost.

Root cause: the Recovery Summary was made to adopt the per-karat ``gold_recovery_details``
table. The table is written once per lifecycle by two button actions that become unreachable
once the entry leaves "Refining In Progress", while ``calculate_totals`` runs on EVERY save.
Rows therefore outlive the formula that produced them, and three generations exist in real
data:

  Era A  loss measured against the GROSS alloy weight (input_weight - recovered)
  Era B  recovered split GROSS-proportionally but loss/pct scored on the PURE basis
  Era C  fully pure-proportional — internally consistent

The fix inverts the dependency (parent inputs are authoritative, the table is apportioned FROM
them) and removes the original ~1 mg multi-touch drift at the writer via exact apportionment.

The math module is deliberately DB-free, so most of this file needs no site. Note it does NOT
use ``frappe.utils.flt`` for rounding: ``flt(value, precision)`` resolves the rounding method
through ``frappe.local`` and returns 0.0 with no site bound.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.refining.doctype.refining_entry.recovery_distribution import (
	_round,
	apportion,
	build_row_targets,
)


class TestApportion(IntegrationTestCase):
	"""apportion() must be exact — that is its entire reason for existing."""

	def test_parts_sum_to_total_exactly(self):
		for weights, total in (
			([1.0], 0.5),
			([1.0, 1.0], 1.0),
			([1.0, 2.0, 3.0], 10.0),
			([12.517, 6.116], 18.6),
			([431.083, 7.781], 438.863),
		):
			parts = apportion(total, weights)
			self.assertAlmostEqual(sum(parts), total, places=9)

	def test_thirds_do_not_lose_a_milligram(self):
		# 1/3 each: naive per-row rounding drops a unit; largest-remainder must not.
		parts = apportion(1.0, [1.0, 1.0, 1.0])
		self.assertAlmostEqual(sum(parts), 1.0, places=9)
		self.assertEqual(len(parts), 3)

	def test_degenerate_inputs(self):
		self.assertEqual(apportion(10.0, []), [])
		self.assertEqual(apportion(0.0, [1.0, 2.0]), [0.0, 0.0])
		self.assertEqual(apportion(-5.0, [1.0]), [0.0])
		self.assertEqual(apportion(10.0, [0.0, 0.0]), [0.0, 0.0])

	def test_deterministic(self):
		self.assertEqual(
			apportion(1.0, [1.0, 1.0, 1.0]), apportion(1.0, [1.0, 1.0, 1.0])
		)


class TestBuildRowTargets(IntegrationTestCase):
	"""Every column must sum back to the parent total, for any number of touches."""

	def _assert_invariants(self, pure, refined_fine, loss):
		rows = build_row_targets(pure, refined_fine, loss)
		self.assertAlmostEqual(
			sum(r["pure_gold_weight"] for r in rows), round(sum(pure), 3), places=9
		)
		self.assertAlmostEqual(
			sum(r["recovered_weight"] for r in rows), refined_fine, places=9
		)
		self.assertAlmostEqual(sum(r["loss_weight"] for r in rows), loss, places=9)
		return rows

	def test_invariants_hold_for_one_to_five_karats(self):
		for count in range(1, 6):
			pure = [round(1.0 + index * 3.7, 3) for index in range(count)]
			total = round(sum(pure), 3)
			refined_fine = round(total * 0.98, 3)
			self._assert_invariants(pure, refined_fine, round(total - refined_fine, 3))

	def test_era_b_multi_karat_regression(self):
		# The real RFN-DST-26-00027 numbers. The stored table read 0.860 + 0.000 with a
		# 113.52% row; the mass balance says 0.033. Apportioning must reproduce 0.033.
		rows = self._assert_invariants([12.517, 6.116], 18.600, 0.033)
		self.assertEqual([r["loss_weight"] for r in rows], [0.022, 0.011])
		self.assertTrue(all(r["recovery_pct"] <= 100.0 for r in rows))

	def test_era_a_single_karat_regression(self):
		# RFN-DST-26-00019: stored loss 2.360 (gross basis) / pct 91.51 against a real
		# mass balance of 0.107 / 99.58.
		row = build_row_targets([25.557], 25.450, 0.107)[0]
		self.assertEqual(row["loss_weight"], 0.107)
		self.assertEqual(row["recovery_pct"], 99.58)

	def test_era_c_consistent_table_is_left_alone(self):
		# A table the current formula would produce must round-trip unchanged.
		rows = build_row_targets([1.433], 1.400, 0.033)
		self.assertEqual(rows[0]["pure_gold_weight"], 1.433)
		self.assertEqual(rows[0]["recovered_weight"], 1.400)
		self.assertEqual(rows[0]["loss_weight"], 0.033)

	def test_no_row_can_over_recover(self):
		# The old per-row max(pure - recovered, 0) allowed one row above 100% while another
		# absorbed a phantom loss. Apportioning by pure content cannot produce that.
		rows = build_row_targets([12.517, 6.116], 18.600, 0.033)
		self.assertTrue(
			all(r["recovered_weight"] <= r["pure_gold_weight"] for r in rows)
		)

	def test_recovery_pct_never_shows_100_beside_a_loss(self):
		rows = build_row_targets([1000.0], 999.999, 0.001)
		self.assertEqual(rows[0]["recovery_pct"], 99.99)

	def test_full_recovery_shows_exactly_100(self):
		rows = build_row_targets([10.0, 5.0], 15.0, 0.0)
		self.assertTrue(all(r["recovery_pct"] == 100.0 for r in rows))
		self.assertTrue(all(r["loss_weight"] == 0.0 for r in rows))

	def test_r_boundary_around_one(self):
		# ~1 in 20,000 consistent tables lands here; rare enough to be called impossible,
		# common enough to matter at production volume. The contract is that the column sums
		# to the total ROUNDED to the working precision, so compare against that.
		for refined_fine in (14.9995, 15.0, 15.0005, 14.999, 15.001):
			rows = build_row_targets([10.0, 5.0], refined_fine, 0.0)
			self.assertAlmostEqual(
				sum(r["recovered_weight"] for r in rows),
				_round(refined_fine, 3),
				places=9,
			)
			self.assertTrue(all(r["recovery_pct"] <= 100.0 for r in rows))

	def test_rounding_is_half_up_not_bankers(self):
		# Weights are grams of gold; Frappe's `rounded` is half-up by default, and Python's
		# round() is half-to-even (round(14.9995, 3) == 14.999). The module must follow
		# Frappe, or a half-milligram would round the other way from the rest of the app.
		self.assertEqual(_round(14.9995, 3), 15.0)
		self.assertEqual(_round(0.0005, 3), 0.001)
		self.assertEqual(_round(2.5, 0), 3.0)

	def test_empty_table(self):
		self.assertEqual(build_row_targets([], 1.0, 0.0), [])


def _entry(**kwargs):
	"""A RefiningEntry stand-in carrying only what the guards read."""
	from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
		RefiningEntry,
	)

	doc = SimpleNamespace(
		docstatus=0,
		repack_se=None,
		transfer_se=None,
		gold_recovery_details=[],
		gross_pure_weight=0.0,
		refined_fine_weight=0.0,
		actual_recovery=0.0,
		refining_loss=0.0,
		recovery_table_out_of_sync=0,
		SUMMARY_DRIFT_TOLERANCE=RefiningEntry.SUMMARY_DRIFT_TOLERANCE,
	)
	doc.__dict__.update(kwargs)
	doc.get = lambda field, default=None: getattr(doc, field, default)
	doc.set = lambda field, value: setattr(doc, field, value)
	return doc


class TestMintIdentity(IntegrationTestCase):
	"""actual_recovery + refining_loss <= gross_pure_weight.

	create_repack_se mints BOTH the recovered pure gold (sum of refined_gold rows, i.e.
	actual_recovery) and refining_loss grams of pure dust. Checking refining_loss against
	gross_pure - refined_fine_weight instead would be a tautology, since that IS how
	refining_loss is defined.
	"""

	def _run(self, doc):
		from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
			RefiningEntry,
		)

		RefiningEntry._validate_mint_identity(doc)

	def test_balanced_entry_passes(self):
		self._run(
			_entry(gross_pure_weight=18.633, actual_recovery=18.6, refining_loss=0.033)
		)

	def test_assay_margin_tolerated(self):
		self._run(
			_entry(gross_pure_weight=10.0, actual_recovery=10.05, refining_loss=0.0)
		)

	def test_over_mint_throws(self):
		# The real RFN-SRN-26-00002 shape: 2.410 recovered + 0.722 loss against 2.539 in.
		with self.assertRaises(frappe.ValidationError):
			self._run(
				_entry(
					gross_pure_weight=2.539, actual_recovery=2.410, refining_loss=0.722
				)
			)

	def test_inflated_loss_throws(self):
		# What the reverted behaviour would have booked on RFN-SRN-26-00014.
		with self.assertRaises(frappe.ValidationError):
			self._run(
				_entry(
					gross_pure_weight=2.445, actual_recovery=0.0, refining_loss=2.660
				)
			)


class TestSyncRecoveryTable(IntegrationTestCase):
	"""The save-time heal is deliberately narrow — prove each guard."""

	def _sync(self, doc, purity_map=None):
		from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
			RefiningEntry,
		)

		doc._collect_input_purity_map = lambda: (purity_map or {}, {})
		doc._recovery_table_diverges = lambda: RefiningEntry._recovery_table_diverges(
			doc
		)
		RefiningEntry._sync_recovery_table(doc)

	def test_no_op_without_a_table(self):
		doc = _entry()
		self._sync(doc)
		self.assertEqual(doc.gold_recovery_details, [])

	def test_no_op_on_submitted_document(self):
		row = SimpleNamespace(
			purity_percentage=91.9, loss_weight=2.360, update=MagicMock()
		)
		doc = _entry(docstatus=1, gold_recovery_details=[row], refining_loss=0.107)
		self._sync(doc, {91.9: 27.810})
		row.update.assert_not_called()

	def test_no_op_and_flag_without_a_purity_basis(self):
		# External pre-receipt: rows carry no purity. Leaving them alone beats blanking them,
		# but the divergence must still be visible.
		row = SimpleNamespace(
			purity_percentage=0.0, loss_weight=2.360, update=MagicMock()
		)
		doc = _entry(gold_recovery_details=[row], refining_loss=0.107)
		self._sync(doc, {})
		row.update.assert_not_called()
		self.assertEqual(doc.recovery_table_out_of_sync, 1)

	def test_heals_a_stale_gross_basis_row(self):
		row = SimpleNamespace(
			purity_percentage=91.9, loss_weight=2.360, recovery_pct=91.51
		)
		row.update = lambda values: row.__dict__.update(values)
		doc = _entry(
			gold_recovery_details=[row],
			gross_pure_weight=25.557,
			refined_fine_weight=25.450,
			refining_loss=0.107,
		)
		self._sync(doc, {91.9: 27.810})
		self.assertEqual(row.loss_weight, 0.107)
		self.assertEqual(row.recovery_pct, 99.58)
		self.assertEqual(doc.recovery_table_out_of_sync, 0)


class TestPreserveSettledSummary(IntegrationTestCase):
	"""Gold figures must not drift once the Stock Entries are posted."""

	def _run(self, doc, previous):
		from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
			RefiningEntry,
		)

		doc.get_doc_before_save = lambda: previous
		RefiningEntry._preserve_settled_summary(doc)

	def test_unsettled_entry_is_left_to_recompute(self):
		doc = _entry(gross_pure_weight=3.152, refining_loss=0.131)
		self._run(doc, _entry(gross_pure_weight=3.021, refining_loss=0.0))
		self.assertEqual(doc.gross_pure_weight, 3.152)
		self.assertEqual(doc.recovery_table_out_of_sync, 0)

	def test_settled_entry_keeps_stored_values_and_flags(self):
		# RFN-SRN-26-00016: recomputed 3.152 against a stored 3.021, with SEs already posted.
		doc = _entry(
			repack_se="KGJPL-SE-MF-26-02128",
			gross_pure_weight=3.152,
			refining_loss=0.131,
		)
		self._run(doc, _entry(gross_pure_weight=3.021, refining_loss=0.0))
		self.assertEqual(doc.gross_pure_weight, 3.021)
		self.assertEqual(doc.refining_loss, 0.0)
		self.assertEqual(doc.recovery_table_out_of_sync, 1)

	def test_settled_entry_without_drift_is_not_flagged(self):
		doc = _entry(repack_se="SE-1", gross_pure_weight=3.021, refining_loss=0.0)
		self._run(doc, _entry(gross_pure_weight=3.021, refining_loss=0.0))
		self.assertEqual(doc.recovery_table_out_of_sync, 0)

	def test_new_document_has_no_previous_version(self):
		doc = _entry(repack_se="SE-1", gross_pure_weight=1.0)
		self._run(doc, None)
		self.assertEqual(doc.gross_pure_weight, 1.0)


class TestCalculateTotalsUsesMassBalance(IntegrationTestCase):
	"""calculate_totals must never read the child table for its loss figure."""

	def test_loss_is_the_mass_balance_not_the_table_sum(self):
		from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
			RefiningEntry,
		)

		# Era-B rows summing to 0.860, against a 0.033 mass balance.
		rows = [
			SimpleNamespace(
				pure_gold_weight=12.517, loss_weight=0.860, purity_percentage=91.9
			),
			SimpleNamespace(
				pure_gold_weight=6.116, loss_weight=0.000, purity_percentage=75.4
			),
		]
		for row in rows:
			row.update = lambda values, row=row: row.__dict__.update(values)

		doc = _entry(gold_recovery_details=rows)
		doc.refined_gold = [
			SimpleNamespace(refining_gold_weight=18.6, pure_weight=18.6)
		]
		doc._compute_input_pure_weight = lambda: (18.633, 18.633)
		doc._collect_input_purity_map = lambda: ({91.9: 13.620, 75.4: 8.112}, {})
		doc.get_doc_before_save = lambda: None
		# Bind the real collaborators calculate_totals calls on self.
		doc._preserve_settled_summary = lambda: RefiningEntry._preserve_settled_summary(
			doc
		)
		doc._sync_recovery_table = lambda: RefiningEntry._sync_recovery_table(doc)
		doc._recovery_table_diverges = lambda: RefiningEntry._recovery_table_diverges(
			doc
		)

		# calculate_totals rounds with frappe.utils.flt, and flt(value, precision) resolves the
		# rounding method through frappe.get_system_settings -> frappe.db on first use in a
		# process. Mocking frappe.db before that read leaves flt unable to resolve it, and flt
		# SWALLOWS the failure and returns 0.0 -- every weight below silently becomes 0. Resolve
		# it against the real DB first so the value is cached on frappe.local; the mock then only
		# has to stand in for the writes we actually want to suppress.
		frappe.get_system_settings("rounding_method")
		with patch.object(frappe, "db", MagicMock()):
			RefiningEntry.calculate_totals(doc)

		self.assertEqual(doc.refining_loss, 0.033)
		self.assertEqual(doc.gross_pure_weight, 18.633)
		# and the table was healed to agree with it
		self.assertAlmostEqual(sum(r.loss_weight for r in rows), 0.033, places=9)
