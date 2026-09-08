# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""A receive may only debit batches its work order actually holds.

``CustomStockEntry.update_batches`` fills an empty ``batch_no`` through
``get_fifo_batches`` -> ``get_auto_batch_nos``, which is plain WAREHOUSE FIFO with no
work-order awareness. Department WIP warehouses are shared by every job in the
department, so FIFO can hand one job another job's metal.

The live case: ``MAT-STE-06205`` returned 0.280 g of ``M-G-22KT-91.75-Y`` against
``KG2F081-MGL229175Y0-P29A8``, a shared casting batch its work order was never issued,
while that work order held the same item in five other batches. The result was a −0.280
MOP Log balance cloned onto ten operations and a Serial Number Creator reading 16.720 g
against a header (and an operator's scale) of 16.440 g.

Pure-logic: every DB access is patched, docs are SimpleNamespace fakes -- same style as
``test_stock_entry.py``, whose ``_Doc``/``_Row`` fakes this module reuses.
"""

from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import stock_entry as se_events
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log import mop_log
from jewellery_erpnext.jewellery_erpnext.tests.test_stock_entry import _Doc, _Row

MWO = "MWO-KGJPL-PE00081-001-1-91.75-Y-01"
MOP = "MOP-49T4D"
GOLD = "M-G-22KT-91.75-Y"
PHANTOM = "KG2F081-MGL229175Y0-P29A8"
OWN = "KG2F081-MGL229175Y0-12L9U"

# The work order's real holdings at the moment of the defect.
HELD = {
	(GOLD, OWN): {
		"item_code": GOLD,
		"batch_no": OWN,
		"qty_after_transaction_batch_based": 18.7,
	},
	(GOLD, "KG2F083-MGL229175Y0-1U6V7"): {
		"item_code": GOLD,
		"batch_no": "KG2F083-MGL229175Y0-1U6V7",
		"qty_after_transaction_batch_based": 0.15,
	},
	(GOLD, "KG2F083-MGL229175Y0-2S9L7"): {
		"item_code": GOLD,
		"batch_no": "KG2F083-MGL229175Y0-2S9L7",
		"qty_after_transaction_batch_based": 0.138,
	},
}


class _GuardTestCase(IntegrationTestCase):
	"""Pure-logic; no fixtures, no site data."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, doc, held=None):
		"""Invoke the guard with the held map stubbed. Returns the mock for assertions."""
		with patch.object(
			mop_log, "get_mwo_held_batch_map", return_value=dict(held or {})
		) as held_mock, patch.object(
			se_events, "_mop_summary_text", return_value="(stubbed)"
		), patch.object(se_events, "_other_work_orders_text", return_value="(stubbed)"):
			se_events.validate_receive_batches_are_held(doc)
		return held_mock

	def _doc(self, rows, **attrs):
		defaults = {
			"docstatus": 0,
			"stock_entry_type": "Material Receive (WORK ORDER)",
			"manufacturing_work_order": MWO,
			"items": rows,
		}
		defaults.update(attrs)
		return _Doc(**defaults)

	def _row(self, batch_no=OWN, item_code=GOLD, **attrs):
		defaults = {
			"idx": 1,
			"item_code": item_code,
			"batch_no": batch_no,
			"qty": 0.28,
			"manufacturing_operation": MOP,
			"s_warehouse": "Model Making WO - KGJPL",
		}
		defaults.update(attrs)
		return _Row(**defaults)


class TestForeignBatchIsRejected(_GuardTestCase):
	def test_mat_ste_06205_is_rejected(self):
		"""The live defect, in its real shape."""
		doc = self._doc([self._row(batch_no=PHANTOM)])
		with self.assertRaises(Exception) as caught:
			self._run(doc, HELD)
		message = str(caught.exception)
		for expected in (GOLD, PHANTOM, MOP, MWO, OWN):
			self.assertIn(expected, message)

	def test_held_batch_passes(self):
		self._run(self._doc([self._row(batch_no=OWN)]), HELD)

	def test_item_unknown_to_the_ledger_is_silent(self):
		"""The ledger has no opinion about this item on this work order.

		Legacy work orders, freshly-seeded test sites and transfer legs whose MOP Log
		row failed to write all land here. The guard narrows that fallback; it does
		not remove it.
		"""
		self._run(self._doc([self._row(batch_no=PHANTOM, item_code="M-G-18KT")]), HELD)

	def test_empty_ledger_is_silent(self):
		self._run(self._doc([self._row(batch_no=PHANTOM)]), {})


class TestGuardRowFilter(_GuardTestCase):
	"""The filter mirrors the MOP Log writer: a row it ignores cannot go negative."""

	def _assert_no_lookup(self, doc):
		held_mock = self._run(doc, HELD)
		held_mock.assert_not_called()

	def test_non_debiting_type_costs_nothing(self):
		self._assert_no_lookup(
			self._doc(
				[self._row(batch_no=PHANTOM)],
				stock_entry_type="Material Transfer (WORK ORDER)",
			)
		)

	def test_cancelled_document_is_skipped(self):
		self._assert_no_lookup(self._doc([self._row(batch_no=PHANTOM)], docstatus=2))

	def test_row_without_batch_is_skipped(self):
		self._assert_no_lookup(self._doc([self._row(batch_no=None)]))

	def test_row_without_operation_is_skipped(self):
		self._assert_no_lookup(
			self._doc([self._row(batch_no=PHANTOM, manufacturing_operation=None)])
		)

	def test_fire_assay_certification_receive_is_skipped(self):
		"""Writes no MOP Log rows at all -- `onsubmit` hard-returns before the sync."""
		self._assert_no_lookup(
			self._doc(
				[self._row(batch_no=PHANTOM)],
				department="Product Certification",
				service_type="Fire Assy Service",
			)
		)

	def test_unresolvable_work_order_is_silent(self):
		with patch.object(se_events.frappe.db, "get_value", return_value=None):
			self._assert_no_lookup(
				self._doc([self._row(batch_no=PHANTOM)], manufacturing_work_order=None)
			)


class TestGuardScopeRules(_GuardTestCase):
	def test_flow_index_zero_baseline_counts_as_held(self):
		"""A post-handoff holding is described ONLY by its `flow_index = 0` clone.

		The clone carries ``qty_change = 0`` and a non-zero balance. Filtering on
		``flow_index``, ``qty_change`` or ``voucher_type`` would block every receive
		made after a Department/Employee IR handoff.
		"""
		baseline = {
			(GOLD, OWN): {
				"item_code": GOLD,
				"batch_no": OWN,
				"qty_after_transaction_batch_based": 18.7,
				"qty_change": 0,
				"flow_index": 0,
				"voucher_type": "Manufacturing Operation",
			}
		}
		self._run(self._doc([self._row(batch_no=OWN)]), baseline)

	def test_pcs_only_fg_seed_key_is_held(self):
		"""The FG seed admits a qty-negative key whose pcs sum is positive.

		``HAVING SUM(qty_change) > 0 OR SUM(pcs_change) > 0`` -- presence, not
		sufficiency, is what this guard tests, so a stone row with no weight left but
		a surviving count must still pass.
		"""
		seed = {
			("D-NT-RO-6B-+6.5-7", "B-75JG2"): {
				"item_code": "D-NT-RO-6B-+6.5-7",
				"batch_no": "B-75JG2",
				"qty_after_transaction_batch_based": 0.0,
				"pcs_after_transaction_batch_based": 8,
			}
		}
		self._run(
			self._doc([self._row(batch_no="B-75JG2", item_code="D-NT-RO-6B-+6.5-7")]),
			seed,
		)

	def test_over_balance_on_a_held_batch_is_not_this_guards_job(self):
		"""Sufficiency is capped by `create_mr_wo_stock_entry`, not here.

		Throwing on a held batch whose balance is merely too small would duplicate
		that cap with a second definition of "available", which is the divergence this
		whole change exists to remove.
		"""
		thin = {
			(GOLD, OWN): {
				"item_code": GOLD,
				"batch_no": OWN,
				"qty_after_transaction_batch_based": 0.001,
			}
		}
		self._run(self._doc([self._row(batch_no=OWN, qty=99.0)]), thin)


class TestHeldSummaryText(_GuardTestCase):
	def test_lists_biggest_holdings_first_with_total(self):
		text = se_events._held_summary_text(HELD, GOLD)
		self.assertTrue(text.startswith(f"{OWN}: 18.7"))
		self.assertIn("total 18.988", text)

	def test_reports_nothing_for_an_unknown_item(self):
		self.assertEqual(se_events._held_summary_text(HELD, "M-G-18KT"), "nothing")

	def test_counts_the_tail_instead_of_printing_it(self):
		"""A shared casting batch sits in dozens of places; the tail is counted."""
		wide = {
			(GOLD, f"B-{i}"): {
				"item_code": GOLD,
				"batch_no": f"B-{i}",
				"qty_after_transaction_batch_based": float(i),
			}
			for i in range(9)
		}
		self.assertIn("(+4 more batches)", se_events._held_summary_text(wide, GOLD))


class TestTotalWeightIsRecomputedOnValidate(IntegrationTestCase):
	"""``total_weight`` is derived from fg_details, not operator input.

	It used to be computed once in ``before_insert`` on a form-writable field, with
	nothing at submit reconciling the two -- ``calulate_id_wise_sum_up`` checks
	fg_details against source_table and ignores total_weight. Three SNCs on kg-gk
	reached ``docstatus 1`` carrying 40.99 / 10.48 / 11.14 against FG BOMs of
	19.18 / 3.20 / 3.45 that way.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_validate_recomputes_total_weight(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator import (
			serial_number_creator as snc_mod,
		)

		doc = _Doc(_compute_total_weight=MagicMock())
		with patch.object(snc_mod, "split_source_rows_by_reservation") as split:
			snc_mod.SerialNumberCreator.validate(doc)
		split.assert_called_once_with(doc)
		doc._compute_total_weight.assert_called_once_with()
