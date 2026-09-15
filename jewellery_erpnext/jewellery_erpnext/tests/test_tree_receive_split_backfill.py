# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""``patches.backfill_tree_receive_split`` -- seeding the receive provenance split.

This patch is in ``patches.txt``, so it rewrites existing ``Tree Material Detail``
rows unattended on the next ``bench migrate``. The rule it has to hold to is that
the history it reconstructs is the history the LIVE code would have written:

  * eligibility is ``Department Operation.tree_no_reqd`` on the Employee IR's
    operation, the same gate ``update_tree_on_receive`` opens with;
  * the draw is ``max(received_gross_wt - gross_wt, 0)``, charged only for a
    non-subcontracted ``is_raw_material`` receive, exactly as ``tree_draw_by_tree``;
  * the gross weight is summed regardless of those two flags, exactly as
    ``tree_received_gross_by_tree``;
  * ``manual_receive_qty`` is the residual, floored at 0.

DB access is mocked by doctype, matching the house style in test_tree_casting.py.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import backfill_tree_receive_split as backfill

ITEM = "M-G-18KT-75.4-P"
TREE = "GEPL-TR-26-00154"
PREC = 3
EPS = 0.0005


def recv_row(mwo="MWO-A", gross=2.0, received=3.0, pinned=TREE, raw=1, subcon="No"):
	"""One row as ``_receive_rows`` returns it."""
	return frappe._dict(
		tree_number=pinned or TREE,
		pinned_tree_number=pinned,
		manufacturing_work_order=mwo,
		gross_wt=gross,
		received_gross_wt=received,
		is_raw_material=raw,
		subcontracting=subcon,
	)


def ledger_row(name="TMD-1", receive_qty=0.0, item=ITEM, parent=TREE):
	return frappe._dict(
		name=name, parent=parent, item_code=item, receive_qty=receive_qty
	)


class _BackfillHarness(IntegrationTestCase):
	def setUp(self):
		# Resolve the rounding method BEFORE frappe.db is mocked out below. flt(x, prec)
		# reaches System Settings for it, and flt swallows whatever that read raises and
		# returns 0.0 -- so against a MagicMock db every quantity in this module silently
		# collapses to zero and the split tests pass or fail for the wrong reason. Warming
		# it here caches it on frappe.local for the duration of the test, which is the same
		# guard test_tree_employee_ir_receive.py carries (0b4e638, the refining suite).
		super().setUp()
		frappe.get_system_settings("rounding_method")

	def split(self, rows, ledger, items=None):
		"""Run _split_chunk over ``rows``; returns ({row name: written dict}, stats)."""
		stats = {"fallback_rows": 0, "unresolved_items": 0}
		written = {}
		db = MagicMock()
		db.set_value.side_effect = lambda dt, name, values, **k: written.__setitem__(
			name, values
		)
		item_of = items if items is not None else {"MWO-A": ITEM, "MWO-B": ITEM}
		with (
			patch.object(backfill, "_receive_rows", return_value=rows),
			patch.object(
				backfill, "_metal_item", side_effect=lambda n, c: item_of.get(n)
			),
			patch.object(backfill.frappe, "get_all", return_value=ledger),
			patch.object(backfill.frappe, "db", db),
		):
			backfill._split_chunk([TREE], PREC, EPS, {}, stats)
		return written, stats


class TestOnlyCastingReceivesAreCounted(_BackfillHarness):
	"""The migration's eligibility must be the live code's eligibility.

	``employee_ir.on_submit`` calls ``pin_tree_numbers_on_receive`` for EVERY Receive,
	ungated, and that helper falls back to ``MWO.tree_number``. So a Pre Polish receive
	on a work order still carrying its casting tree gets the tree PINNED onto its row
	even though ``update_tree_on_receive`` returned at ``is_casting_eir`` without
	touching a single column. Nothing about the pinned row distinguishes it from a real
	casting receive -- only the operation does.
	"""

	def _query(self):
		db = MagicMock()
		db.sql.return_value = []
		with patch.object(backfill.frappe, "db", db):
			backfill._receive_rows([TREE])
		return " ".join(db.sql.call_args.args[0].split())

	def test_the_query_joins_department_operation(self):
		self.assertIn(
			"`tabDepartment Operation` dop ON dop.name = eir.operation", self._query()
		)

	def test_the_query_requires_tree_no_reqd(self):
		# Without this the backfill credits Pre Polish / Final Polish receives to the
		# casting tree: gross weight always, and the gain too on an is_raw_material EIR.
		self.assertIn("dop.tree_no_reqd = 1", self._query())

	def test_the_join_is_inner_so_an_operationless_receive_drops_out(self):
		# is_casting_eir returns False for an EIR with no operation; an INNER JOIN is
		# how SQL says the same thing.
		q = self._query()
		self.assertIn("INNER JOIN `tabDepartment Operation`", q)
		self.assertNotIn("LEFT JOIN `tabDepartment Operation`", q)


class TestTheSplitMirrorsTheLiveArithmetic(_BackfillHarness):
	def test_the_work_order_half_is_the_gain_not_the_gross(self):
		written, _ = self.split(
			[recv_row(gross=2.0, received=3.0)], [ledger_row(receive_qty=1.0)]
		)
		self.assertAlmostEqual(written["TMD-1"]["wo_receive_qty"], 1.0, places=3)
		self.assertAlmostEqual(written["TMD-1"]["manual_receive_qty"], 0.0, places=3)
		self.assertAlmostEqual(written["TMD-1"]["wo_received_gross_wt"], 3.0, places=3)

	def test_the_button_half_is_whatever_the_work_orders_do_not_explain(self):
		written, _ = self.split(
			[recv_row(gross=2.0, received=3.0)], [ledger_row(receive_qty=4.0)]
		)
		self.assertAlmostEqual(written["TMD-1"]["wo_receive_qty"], 1.0, places=3)
		self.assertAlmostEqual(written["TMD-1"]["manual_receive_qty"], 3.0, places=3)

	def test_the_halves_always_add_back_up_to_receive_qty(self):
		written, _ = self.split(
			[recv_row(gross=2.0, received=3.0)], [ledger_row(receive_qty=4.0)]
		)
		row = written["TMD-1"]
		self.assertAlmostEqual(
			row["wo_receive_qty"] + row["manual_receive_qty"], 4.0, places=3
		)

	def test_a_reconstructed_draw_bigger_than_the_column_is_clamped(self):
		# An over-drawn historical row: never claim more of receive_qty than it holds,
		# and never let the residual go negative.
		written, _ = self.split(
			[recv_row(gross=2.0, received=9.0)], [ledger_row(receive_qty=1.0)]
		)
		self.assertAlmostEqual(written["TMD-1"]["wo_receive_qty"], 1.0, places=3)
		self.assertEqual(written["TMD-1"]["manual_receive_qty"], 0.0)

	def test_no_gain_draws_nothing_but_still_records_the_gross(self):
		written, _ = self.split(
			[recv_row(gross=3.0, received=2.0)], [ledger_row(receive_qty=2.0)]
		)
		self.assertEqual(written["TMD-1"]["wo_receive_qty"], 0.0)
		self.assertAlmostEqual(written["TMD-1"]["manual_receive_qty"], 2.0, places=3)
		self.assertAlmostEqual(written["TMD-1"]["wo_received_gross_wt"], 2.0, places=3)

	def test_without_the_main_slip_injection_no_metal_left_the_tree(self):
		written, _ = self.split(
			[recv_row(gross=2.0, received=3.0, raw=0)], [ledger_row(receive_qty=1.0)]
		)
		self.assertEqual(written["TMD-1"]["wo_receive_qty"], 0.0)
		# The gross weight is NOT gated on the flag -- same asymmetry as live.
		self.assertAlmostEqual(written["TMD-1"]["wo_received_gross_wt"], 3.0, places=3)

	def test_a_subcontracted_receive_sources_from_a_pool_the_tree_never_owned(self):
		written, _ = self.split(
			[recv_row(gross=2.0, received=3.0, subcon="Yes")],
			[ledger_row(receive_qty=1.0)],
		)
		self.assertEqual(written["TMD-1"]["wo_receive_qty"], 0.0)
		self.assertAlmostEqual(written["TMD-1"]["wo_received_gross_wt"], 3.0, places=3)

	def test_several_receives_on_one_item_accumulate(self):
		written, _ = self.split(
			[
				recv_row(mwo="MWO-A", gross=2.0, received=3.0),
				recv_row(mwo="MWO-B", gross=1.0, received=2.5),
			],
			[ledger_row(receive_qty=5.0)],
		)
		self.assertAlmostEqual(written["TMD-1"]["wo_receive_qty"], 2.5, places=3)
		self.assertAlmostEqual(written["TMD-1"]["wo_received_gross_wt"], 5.5, places=3)

	def test_a_ledger_row_no_receive_explains_is_written_as_all_zero(self):
		written, _ = self.split([], [ledger_row(receive_qty=0.0)])
		self.assertEqual(
			written["TMD-1"],
			{
				"wo_receive_qty": 0.0,
				"manual_receive_qty": 0.0,
				"wo_received_gross_wt": 0.0,
			},
		)

	def test_recomputing_from_source_makes_a_rerun_a_no_op(self):
		# Idempotency: every value is recomputed, never incremented, so the second run
		# writes exactly what the first did.
		args = ([recv_row(gross=2.0, received=3.0)], [ledger_row(receive_qty=4.0)])
		first, _ = self.split(*args)
		second, _ = self.split(*args)
		self.assertEqual(first, second)


class TestTheCaveatsAreCounted(_BackfillHarness):
	"""Coverage gaps must surface in the summary, not vanish into a success message."""

	def test_a_pre_pinning_row_is_counted_as_a_fallback(self):
		_, stats = self.split([recv_row(pinned=None)], [ledger_row(receive_qty=1.0)])
		self.assertEqual(stats["fallback_rows"], 1)

	def test_a_pinned_row_is_not_counted_as_a_fallback(self):
		_, stats = self.split([recv_row()], [ledger_row(receive_qty=1.0)])
		self.assertEqual(stats["fallback_rows"], 0)

	def test_an_unresolvable_metal_item_is_counted_and_contributes_nothing(self):
		written, stats = self.split(
			[recv_row(mwo="MWO-X")], [ledger_row(receive_qty=4.0)], items={}
		)
		self.assertEqual(stats["unresolved_items"], 1)
		# KNOWN LIMITATION, reported rather than hidden: with no resolvable item the
		# work-order half cannot be reconstructed, so the whole column falls to the
		# manual residual. The count in the summary is the only signal that the split
		# for this row is an assumption rather than a reconstruction.
		self.assertEqual(written["TMD-1"]["wo_receive_qty"], 0.0)
		self.assertAlmostEqual(written["TMD-1"]["manual_receive_qty"], 4.0, places=3)
