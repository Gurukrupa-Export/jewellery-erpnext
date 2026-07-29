# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Employee IR Receive -> Tree Number material ledger (the E-series).

The casting Employee IR Receive may credit a tree ONLY with metal actually drawn
FROM that tree. That quantity is the per-row gain::

        draw_row = max(received_gross_wt - gross_wt, 0)

which is exactly what ``inject_extra_metal_for_eir_receive`` physically pulls out
of the employee MSL (Raw Material) warehouse -- the same warehouse the tree
"Issue Material" button funds. Metal already on the operation (``gross_wt``) never
came from the tree, and booked metal loss never leaves the MSL pool, so neither
belongs in the tree ledger.

The draw is capped by what the tree actually has outstanding; a draw with no
issued balance behind it is blocked outright rather than silently recorded.

Regression anchor (the reported defect): tree ``GEPL-TR-26-00154`` reached
``issue_qty 0 / receive_qty 2.36 / pending_qty 0`` at status "Received".

DB access is mocked by doctype, matching the house style in test_tree_casting.py.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	tree_casting,
)

ITEM = "M-G-18KT-75.4-P"
TREE = "GEPL-TR-26-00154"


class _MWODoc:
	"""Stand-in for a Manufacturing Work Order doc (supports .get and attributes)."""

	def __init__(self, name, tree_number=TREE, **attrs):
		self.name = name
		self.tree_number = tree_number
		self.metal_type = "Gold"
		self.metal_touch = "18KT"
		self.metal_purity = "75.4"
		self.metal_colour = "P"
		for k, v in attrs.items():
			setattr(self, k, v)

	def get(self, key, default=None):
		return getattr(self, key, default)


def make_tree(issue=0.0, receive=0.0, loss=0.0, status="Issued", name=TREE, rows=None):
	"""Fake Tree Number whose save() re-derives pending the way TreeNumber.validate does."""
	if rows is None:
		rows = [(ITEM, issue, receive, loss)]
	tree = SimpleNamespace(
		name=name,
		status=status,
		employee_ir="EIR-ISSUE-1",
		flags=SimpleNamespace(),
		material_details=[
			SimpleNamespace(
				item_code=item,
				issue_qty=i,
				receive_qty=r,
				loss_qty=lo,
				pending_qty=i - r - lo,
			)
			for item, i, r, lo in rows
		],
	)

	def _save(*a, **k):
		# Mirror TreeNumber.calculate_material_pending: single unfloored writer.
		for md in tree.material_details:
			md.pending_qty = md.issue_qty - md.receive_qty - md.loss_qty
		tree.saves = getattr(tree, "saves", 0) + 1

	tree.save = _save
	tree.saves = 0
	return tree


def make_recv_eir(rows, is_main_slip_required=1, subcontracting="No", typ="Receive"):
	"""Receive EIR. rows: [(mwo, gross_wt, received_gross_wt)]."""
	return SimpleNamespace(
		name="EIR-RECV-1",
		operation="Casting",
		type=typ,
		company="Gurukrupa Export Private Limited",
		department="Waxing - GEPL",
		employee="HR-EMP-00267",
		subcontracting=subcontracting,
		is_main_slip_required=is_main_slip_required,
		manually_book_loss_details=[],
		employee_loss_details=[],
		employee_ir_operations=[
			SimpleNamespace(
				name=f"row{idx}",
				manufacturing_work_order=mwo,
				gross_wt=gross,
				received_gross_wt=recv,
				tree_number=None,
			)
			for idx, (mwo, gross, recv) in enumerate(rows)
		],
	)


class _TreeReceiveHarness(IntegrationTestCase):
	"""Shared mock plumbing for the casting receive path."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		# Resolve the rounding method against the REAL db before any test installs the
		# frappe.db mock.
		#
		# tree_draw_by_tree sizes the draw with flt(received_gross_wt - gross_wt, prec), and
		# flt(value, precision) -> rounded() -> frappe.get_system_settings("rounding_method"),
		# which loads the System Settings doc through frappe.db the first time a process needs
		# it and caches it on frappe.local. Mocked out before that read ever happens, the
		# lookup cannot resolve -- and flt SWALLOWS the failure and returns 0.0 rather than
		# raising. The draw then reads 0.0, falls under the eps guard, and
		# update_tree_on_receive returns before it ever touches a tree.
		#
		# Only ever bit the FIRST test in the module (TestAgreedWorkedExample sorts first, and
		# test_cancelling_that_receive_returns_the_1_g first within it): every later test warms
		# the cache incidentally, calling flt outside a patch. So it passed on a dev site whose
		# session was already warm, and failed on CI, which runs each module in its own freshly
		# booted process -- exactly the failure 0b4e638 fixed in the refining suite.
		super().setUp()
		frappe.get_system_settings("rounding_method")

	def run_update(self, eir, tree, cancel=False, mwos=None):
		mwos = mwos or {
			r.manufacturing_work_order: _MWODoc(r.manufacturing_work_order)
			for r in eir.employee_ir_operations
		}
		db = MagicMock()
		db.get_value.side_effect = lambda dt, *a, **k: (
			1 if dt == "Department Operation" else None
		)
		with (
			patch.object(tree_casting.frappe, "db", db),
			patch.object(
				tree_casting.frappe, "get_cached_doc", side_effect=lambda dt, n: mwos[n]
			),
			patch.object(tree_casting.frappe, "get_doc", return_value=tree),
			patch.object(tree_casting.frappe, "get_precision", return_value=3),
			patch.object(tree_casting, "get_item_from_attribute", return_value=ITEM),
		):
			tree_casting.update_tree_on_receive(eir, cancel=cancel)

	def run_validate(self, eir, tree, mwos=None):
		mwos = mwos or {
			r.manufacturing_work_order: _MWODoc(r.manufacturing_work_order)
			for r in eir.employee_ir_operations
		}
		db = MagicMock()
		db.get_value.side_effect = lambda dt, *a, **k: (
			1 if dt == "Department Operation" else None
		)
		with (
			patch.object(tree_casting.frappe, "db", db),
			patch.object(
				tree_casting.frappe, "get_cached_doc", side_effect=lambda dt, n: mwos[n]
			),
			patch.object(tree_casting.frappe, "get_doc", return_value=tree),
			patch.object(tree_casting.frappe, "get_precision", return_value=3),
			patch.object(tree_casting, "get_item_from_attribute", return_value=ITEM),
		):
			tree_casting.validate_casting_receive(eir)

	def row(self, tree, idx=0):
		return tree.material_details[idx]

	def tearDown(self):
		return super().tearDown()


class TestReportedDefect(_TreeReceiveHarness):
	"""The exact GEPL-TR-26-00154 state must be unreachable."""

	def test_receive_without_issue_is_blocked(self):
		# gross 0, received 2.36, tree never issued -> the 2.36 is a real tree draw with no
		# issued balance behind it. This is the reported defect; it must throw.
		tree = make_tree(issue=0.0)
		eir = make_recv_eir([("MWO-A", 0.0, 2.36)])
		with self.assertRaises(ValidationError):
			self.run_validate(eir, tree)

	def test_receive_without_issue_leaves_tree_untouched(self):
		tree = make_tree(issue=0.0)
		eir = make_recv_eir([("MWO-A", 0.0, 2.36)])
		with self.assertRaises(ValidationError):
			self.run_update(eir, tree)
		self.assertEqual(self.row(tree).receive_qty, 0.0)
		self.assertEqual(self.row(tree).issue_qty, 0.0)

	def test_status_never_received_with_zero_issue(self):
		# issue 0 / receive 2.36 must never read as a completed tree.
		tree = make_tree(rows=[(ITEM, 0.0, 2.36, 0.0)])
		self.assertNotEqual(tree_casting._tree_status(tree), "Received")


class TestTreeDrawAllocation(_TreeReceiveHarness):
	"""E04-E08, E11: how much of a receive lands on the tree."""

	def test_e04_zero_gross_partial_return(self):
		# THE named CI regression: issue 2.900, gross 0, received 2.390.
		tree = make_tree(issue=2.900)
		eir = make_recv_eir([("MWO-A", 0.0, 2.390)])
		self.run_update(eir, tree)
		md = self.row(tree)
		self.assertAlmostEqual(md.issue_qty, 2.900, places=3)
		self.assertAlmostEqual(md.receive_qty, 2.390, places=3)
		self.assertAlmostEqual(md.loss_qty, 0.0, places=3)
		self.assertAlmostEqual(md.pending_qty, 0.510, places=3)
		self.assertEqual(tree.status, "Partially Received")

	def test_e05_baseline_gross_plus_tree_return(self):
		# gross 5.000, received 7.390 -> only the 2.390 gain is tree metal.
		tree = make_tree(issue=2.900)
		eir = make_recv_eir([("MWO-A", 5.000, 7.390)])
		self.run_update(eir, tree)
		self.assertAlmostEqual(self.row(tree).receive_qty, 2.390, places=3)
		self.assertAlmostEqual(self.row(tree).pending_qty, 0.510, places=3)

	def test_e06_received_below_gross_leaves_tree_alone(self):
		# gross 5.000, received 4.800 -> no gain, nothing drawn from the tree.
		tree = make_tree(issue=2.900)
		eir = make_recv_eir([("MWO-A", 5.000, 4.800)])
		self.run_update(eir, tree)
		self.assertAlmostEqual(self.row(tree).receive_qty, 0.0, places=3)
		self.assertAlmostEqual(self.row(tree).pending_qty, 2.900, places=3)

	def test_e07_received_equals_gross(self):
		tree = make_tree(issue=2.900)
		eir = make_recv_eir([("MWO-A", 5.000, 5.000)])
		self.run_update(eir, tree)
		self.assertAlmostEqual(self.row(tree).receive_qty, 0.0, places=3)
		self.assertAlmostEqual(self.row(tree).pending_qty, 2.900, places=3)

	def test_e08_full_return_closes_tree(self):
		tree = make_tree(issue=2.900)
		eir = make_recv_eir([("MWO-A", 0.0, 2.900)])
		self.run_update(eir, tree)
		self.assertAlmostEqual(self.row(tree).pending_qty, 0.0, places=3)
		self.assertEqual(tree.status, "Received")

	def test_e11_adds_to_existing_partial_receipt(self):
		# issue 2.900, already received 1.000 -> pending 1.900; a further 1.500 draw fits.
		tree = make_tree(issue=2.900, receive=1.000)
		eir = make_recv_eir([("MWO-A", 0.0, 1.500)])
		self.run_update(eir, tree)
		self.assertAlmostEqual(self.row(tree).receive_qty, 2.500, places=3)
		self.assertAlmostEqual(self.row(tree).pending_qty, 0.400, places=3)


class TestOverDrawBlocked(_TreeReceiveHarness):
	"""E09, E10, E12: a draw larger than the tree's outstanding balance blocks the receive."""

	def test_e10_candidate_exceeds_pending_throws(self):
		# pending 2.900 but the gain is 3.500 -> block the whole receive (user decision).
		tree = make_tree(issue=2.900)
		eir = make_recv_eir([("MWO-A", 5.000, 8.500)])
		with self.assertRaises(ValidationError):
			self.run_validate(eir, tree)

	def test_e12_small_overdraw_throws(self):
		# pending 0.400, draw 0.600.
		tree = make_tree(issue=2.900, receive=2.500)
		eir = make_recv_eir([("MWO-A", 0.0, 0.600)])
		with self.assertRaises(ValidationError):
			self.run_validate(eir, tree)

	def test_exact_pending_is_allowed(self):
		tree = make_tree(issue=2.900, receive=2.500)
		eir = make_recv_eir([("MWO-A", 0.0, 0.400)])
		self.run_validate(eir, tree)

	def test_error_message_names_the_context(self):
		tree = make_tree(issue=2.900)
		eir = make_recv_eir([("MWO-A", 5.000, 8.500)])
		with self.assertRaises(ValidationError) as ctx:
			self.run_validate(eir, tree)
		msg = str(ctx.exception)
		for token in (TREE, ITEM, "MWO-A"):
			self.assertIn(token, msg)


class TestZeroDrawNeverConsultsLedger(_TreeReceiveHarness):
	"""A receive that draws nothing must work even against a legacy over-drawn tree."""

	def test_legacy_negative_pending_does_not_block_zero_draw(self):
		# 155 rows on gk store a negative pending. An ordinary receive (no gain) must not throw.
		tree = make_tree(rows=[(ITEM, 0.0, 37000.0, 0.0)])
		eir = make_recv_eir([("MWO-A", 5.000, 4.800)])
		self.run_validate(eir, tree)
		self.run_update(eir, tree)
		self.assertAlmostEqual(self.row(tree).receive_qty, 37000.0, places=3)

	def test_no_main_slip_means_no_draw(self):
		# Without is_main_slip_required nothing is injected, so nothing leaves the MSL pool.
		# The pre-existing unbacked-gain guard owns this case.
		tree = make_tree(issue=0.0)
		eir = make_recv_eir([("MWO-A", 5.000, 4.800)], is_main_slip_required=0)
		self.run_validate(eir, tree)
		self.run_update(eir, tree)
		self.assertAlmostEqual(self.row(tree).receive_qty, 0.0, places=3)

	def test_subcontracted_receive_draws_nothing_from_tree(self):
		# The injection sources from the SUBCONTRACTOR's RM warehouse -- a pool the tree
		# never owned -- so it must not be charged to the tree ledger.
		tree = make_tree(issue=0.0)
		eir = make_recv_eir([("MWO-A", 0.0, 2.360)], subcontracting="Yes")
		self.run_validate(eir, tree)
		self.run_update(eir, tree)
		self.assertAlmostEqual(self.row(tree).receive_qty, 0.0, places=3)


class TestPerRowDraw(_TreeReceiveHarness):
	"""The gain clamp is per operation row -- the injection is minted per row."""

	def test_gain_and_loss_rows_do_not_net(self):
		# Row A gains 2 (injection mints 2g out of MSL); row B is short 1 (no injection).
		# Aggregating first would compute max(21-20,0)=1 and under-charge the tree.
		tree = make_tree(issue=2.900)
		eir = make_recv_eir([("MWO-A", 10.0, 12.0), ("MWO-B", 10.0, 9.0)])
		mwos = {"MWO-A": _MWODoc("MWO-A"), "MWO-B": _MWODoc("MWO-B")}
		self.run_update(eir, tree, mwos=mwos)
		self.assertAlmostEqual(self.row(tree).receive_qty, 2.0, places=3)

	def test_multi_row_tree_saved_once(self):
		tree = make_tree(issue=10.0)
		eir = make_recv_eir(
			[("MWO-A", 0.0, 1.0), ("MWO-B", 0.0, 2.0), ("MWO-C", 0.0, 3.0)]
		)
		mwos = {n: _MWODoc(n) for n in ("MWO-A", "MWO-B", "MWO-C")}
		self.run_update(eir, tree, mwos=mwos)
		self.assertAlmostEqual(self.row(tree).receive_qty, 6.0, places=3)
		self.assertEqual(tree.saves, 1)


class TestCancelReversal(_TreeReceiveHarness):
	"""E14, E15: cancel reverses exactly its own contribution."""

	def test_e14_cancel_reverses_the_gain(self):
		# Forward booked 2.390 against issue 2.900; cancel must restore pending to 2.900.
		tree = make_tree(issue=2.900, receive=2.390)
		eir = make_recv_eir([("MWO-A", 0.0, 2.390)])
		self.run_update(eir, tree, cancel=True)
		md = self.row(tree)
		self.assertAlmostEqual(md.receive_qty, 0.0, places=3)
		self.assertAlmostEqual(md.issue_qty, 2.900, places=3)
		self.assertAlmostEqual(md.pending_qty, 2.900, places=3)
		self.assertEqual(tree.status, "Issued")

	def test_cancel_of_a_gain_row_is_not_a_no_op(self):
		# Regression: computing the draw from sign-negated inputs makes max(-2.39, 0) == 0,
		# so the cancel would silently do nothing and the Issue EIR could never be cancelled.
		tree = make_tree(issue=5.000, receive=2.390)
		eir = make_recv_eir([("MWO-A", 5.000, 7.390)])
		self.run_update(eir, tree, cancel=True)
		self.assertAlmostEqual(self.row(tree).receive_qty, 0.0, places=3)

	def test_e15_cancel_leaves_other_vouchers_alone(self):
		# Tree holds 3.000 from two receives; cancelling the 1.000 one leaves 2.000.
		tree = make_tree(issue=5.000, receive=3.000)
		eir = make_recv_eir([("MWO-A", 0.0, 1.000)])
		self.run_update(eir, tree, cancel=True)
		self.assertAlmostEqual(self.row(tree).receive_qty, 2.000, places=3)

	def test_cancel_never_drives_receive_negative(self):
		tree = make_tree(issue=5.000, receive=1.000)
		eir = make_recv_eir([("MWO-A", 0.0, 4.000)])
		self.run_update(eir, tree, cancel=True)
		self.assertGreaterEqual(self.row(tree).receive_qty, 0.0)

	def test_cancel_does_not_throw_on_overdraw(self):
		# A credit is not a draw -- the availability guard must be skipped on cancel.
		tree = make_tree(issue=0.0, receive=2.360)
		eir = make_recv_eir([("MWO-A", 0.0, 2.360)])
		self.run_update(eir, tree, cancel=True)
		self.assertAlmostEqual(self.row(tree).receive_qty, 0.0, places=3)


class TestIdempotency(_TreeReceiveHarness):
	"""E13: the ledger must not double-count a retried application."""

	def test_repeated_application_is_capped_by_pending(self):
		# Applying the same 2.900 draw twice would need 5.800 of issued metal; the second
		# application has no balance left and must throw rather than double-count.
		tree = make_tree(issue=2.900)
		eir = make_recv_eir([("MWO-A", 0.0, 2.900)])
		self.run_update(eir, tree)
		self.assertAlmostEqual(self.row(tree).receive_qty, 2.900, places=3)
		with self.assertRaises(ValidationError):
			self.run_update(eir, tree)
		self.assertAlmostEqual(self.row(tree).receive_qty, 2.900, places=3)


class TestSubmittedTreeImmutable(_TreeReceiveHarness):
	"""E23: a locked tree accepts no further ledger movement."""

	def test_submitted_tree_rejects_receive(self):
		tree = make_tree(issue=2.900, status="Submitted")
		eir = make_recv_eir([("MWO-A", 0.0, 1.000)])
		with self.assertRaises(ValidationError):
			self.run_update(eir, tree)

	def test_submitted_tree_message_does_not_promise_a_reopen_action(self):
		# There is no "reopen" action anywhere in the app; the message must not invent one.
		tree = make_tree(issue=2.900, status="Submitted")
		eir = make_recv_eir([("MWO-A", 0.0, 1.000)])
		with self.assertRaises(ValidationError) as ctx:
			self.run_update(eir, tree)
		self.assertNotIn("Reopen", str(ctx.exception))


class TestItemMapping(_TreeReceiveHarness):
	"""E17, E18: the metal item must map to exactly one ledger row."""

	def test_missing_item_row_throws(self):
		tree = make_tree(rows=[("M-G-22KT-91.9-Y", 5.0, 0.0, 0.0)])
		eir = make_recv_eir([("MWO-A", 0.0, 1.000)])
		with self.assertRaises(ValidationError):
			self.run_update(eir, tree)

	def test_duplicate_item_rows_throw(self):
		tree = make_tree(rows=[(ITEM, 5.0, 0.0, 0.0), (ITEM, 3.0, 0.0, 0.0)])
		eir = make_recv_eir([("MWO-A", 0.0, 1.000)])
		with self.assertRaises(ValidationError):
			self.run_update(eir, tree)


class TestAgreedWorkedExample(_TreeReceiveHarness):
	"""The numbers the business signed off on, pinned end to end.

	    Employee IR Issue, operation gross 2.9  -> tree issue_qty 0   (NOT 2.9)
	    operator issues 2 g on the tree button  -> tree issue_qty 2
	    Employee IR Receive, received 2         -> tree receive_qty 0 (2 <= 2.9, no gain)
	    Employee IR Receive, received 3.9       -> tree receive_qty 1 (3.9 - 2.9)

	The Employee IR only ever hands the tree what came back ABOVE the weight the operation was
	already carrying; everything at or below 2.9 is the operation's own metal returning.
	"""

	def test_issue_seeds_zero_not_the_operation_gross(self):
		# create_tree_on_issue lists the metal item but leaves issue_qty at 0 -- the operation's
		# 2.9 g is planned weight, not metal that has been put on the tree.
		tree = make_tree(issue=0.0, status="Draft")
		self.assertEqual(self.row(tree).issue_qty, 0.0)
		self.assertEqual(tree_casting._tree_status(tree), "Draft")

	def test_manual_issue_of_2_is_what_the_ledger_records(self):
		tree = make_tree(issue=2.0)
		self.assertEqual(self.row(tree).issue_qty, 2.0)
		self.assertAlmostEqual(self.row(tree).pending_qty, 2.0, places=3)
		self.assertEqual(tree_casting._tree_status(tree), "Issued")

	def test_received_2_against_gross_2_9_draws_nothing(self):
		tree = make_tree(issue=2.0)
		eir = make_recv_eir([("MWO-A", 2.9, 2.0)])
		self.run_validate(eir, tree)
		self.run_update(eir, tree)
		self.assertAlmostEqual(self.row(tree).receive_qty, 0.0, places=3)
		self.assertAlmostEqual(self.row(tree).pending_qty, 2.0, places=3)

	def test_received_3_9_against_gross_2_9_draws_exactly_1(self):
		tree = make_tree(issue=2.0)
		eir = make_recv_eir([("MWO-A", 2.9, 3.9)])
		self.run_validate(eir, tree)
		self.run_update(eir, tree)
		self.assertAlmostEqual(self.row(tree).receive_qty, 1.0, places=3)
		self.assertAlmostEqual(self.row(tree).pending_qty, 1.0, places=3)
		self.assertEqual(tree.status, "Partially Received")

	def test_the_1_g_draw_needs_the_2_g_to_have_been_issued(self):
		# Same receive against a tree nobody issued to -> blocked, not silently recorded.
		tree = make_tree(issue=0.0)
		eir = make_recv_eir([("MWO-A", 2.9, 3.9)])
		with self.assertRaises(ValidationError):
			self.run_validate(eir, tree)

	def test_cancelling_that_receive_returns_the_1_g(self):
		tree = make_tree(issue=2.0, receive=1.0)
		eir = make_recv_eir([("MWO-A", 2.9, 3.9)])
		self.run_update(eir, tree, cancel=True)
		self.assertAlmostEqual(self.row(tree).receive_qty, 0.0, places=3)
		self.assertAlmostEqual(self.row(tree).pending_qty, 2.0, places=3)
