# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Unit coverage for the casting-tree layer on Employee IR / Tree Number.

These exercise the pure decision logic without standing up a full casting
scenario (BOM -> Manufacturing Plan -> PMO -> MWO -> MOP -> EIR):

  * tree weight / flask arithmetic (tree_utils) — the formulas mirrored from
	Main Slip, must match it exactly.
  * Tree Number status machine (_tree_status): Issued -> Partially Received ->
	Received as the Material Details ledger fills.
  * validate_casting_tree: all-same-metal on one tree. (The all-or-nothing
	re-issue rule is enforced at submit by validate_casting_group_complete, NOT
	here — see TestValidateCastingGroupComplete. That rule is gated by
	MOP Settings.enforce_full_casting_tree_reissue and ships OFF.)

DB access inside the functions is mocked by doctype so the tests stay fast and
independent of master data.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	tree_casting,
)
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import tree_utils
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.doc_events import (
	tree_stock_entry as tse,
)

# Manufacturing Setting row used by the arithmetic tests.
_MFG_SETTING = {
	"wax_to_gold_18": 16.0,
	"powder_value": 100.0,
	"water_value": 40.0,
	"boric_value": 5.0,
	"special_powder_boric_value": 2.0,
	"power_value_individual": 80.0,
	"water_value_individual": 38.0,
}


class _FakeMWO:
	"""Minimal stand-in for a Manufacturing Work Order doc."""

	def __init__(self, name, **attrs):
		self.name = name
		self._attrs = attrs

	def get(self, key, default=None):
		return self._attrs.get(key, default)


def _md(issue, receive, loss):
	row = SimpleNamespace(issue_qty=issue, receive_qty=receive, loss_qty=loss)
	row.pending_qty = issue - receive - loss
	return row


def _eir(rows, op="Casting WO", typ="Issue"):
	return SimpleNamespace(
		operation=op,
		type=typ,
		employee_ir_operations=[
			SimpleNamespace(manufacturing_work_order=name) for name in rows
		],
	)


class TestTreeUtils(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_computed_gold_wt(self):
		with patch.object(tree_utils.frappe.db, "get_value", return_value=16.0):
			self.assertEqual(
				tree_utils.get_computed_gold_wt("MFR", "18KT", 10.0), 160.0
			)

	def test_unknown_touch_returns_zero(self):
		# No field map entry -> ratio 0 -> computed weight 0 (no DB call needed).
		self.assertEqual(tree_utils.get_computed_gold_wt("MFR", "GARBAGE", 10.0), 0.0)

	def test_flask_weights_without_wax_setting(self):
		with patch.object(
			tree_utils.frappe.db, "get_value", return_value=dict(_MFG_SETTING)
		):
			w = tree_utils.get_flask_weights("MFR", 100.0, is_wax_setting=False)
		# water = 100 * 40 / 100 = 40 ; boric/special suppressed
		self.assertEqual(w["water_weight"], 40.0)
		self.assertEqual(w["boric_powder_weight"], 0.0)
		self.assertEqual(w["special_powder_weight"], 0.0)

	def test_flask_weights_with_wax_setting(self):
		with patch.object(
			tree_utils.frappe.db, "get_value", return_value=dict(_MFG_SETTING)
		):
			w = tree_utils.get_flask_weights("MFR", 100.0, is_wax_setting=True)
		# water uses individual values: 100 * 38 / 80 = 47.5
		self.assertEqual(w["water_weight"], 47.5)
		# boric/special divide by ORIGINAL powder_value (100), matching main_slip
		self.assertEqual(w["boric_powder_weight"], 5.0)  # 100 * 5 / 100
		self.assertEqual(w["special_powder_weight"], 2.0)  # 100 * 2 / 100

	def tearDown(self):
		return super().tearDown()


class TestTreeStatus(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_issued_when_no_receipts(self):
		tree = SimpleNamespace(material_details=[_md(10, 0, 0)])
		self.assertEqual(tree_casting._tree_status(tree), "Issued")

	def test_partially_received(self):
		tree = SimpleNamespace(material_details=[_md(10, 4, 0)])
		self.assertEqual(tree_casting._tree_status(tree), "Partially Received")

	def test_received_when_pending_cleared_by_receive_and_loss(self):
		tree = SimpleNamespace(material_details=[_md(10, 9, 1)])
		self.assertEqual(tree_casting._tree_status(tree), "Received")

	def test_received_requires_all_rows_done(self):
		tree = SimpleNamespace(material_details=[_md(10, 10, 0), _md(5, 2, 0)])
		self.assertEqual(tree_casting._tree_status(tree), "Partially Received")

	def test_never_issued_row_blocks_received(self):
		# Row A issued+received (done), Row B never touched (all zeros — e.g. an unreceived
		# multicolour colour). Must NOT flip to "Received" while B still needs receiving.
		tree = SimpleNamespace(material_details=[_md(10, 10, 0), _md(0, 0, 0)])
		self.assertEqual(tree_casting._tree_status(tree), "Partially Received")

	def test_issue_zero_with_receive_is_never_received(self):
		# The GEPL-TR-26-00154 defect: metal recorded as received against a tree that was never
		# issued. Without issued metal there is nothing to have received, so the tree must never
		# read "Received" — it is an over-draw for the audit to flag.
		tree = SimpleNamespace(material_details=[_md(0, 8, 0)])
		self.assertEqual(tree_casting._tree_status(tree), "Partially Received")

	def test_untouched_tree_is_draft(self):
		tree = SimpleNamespace(material_details=[_md(0, 0, 0)])
		self.assertEqual(tree_casting._tree_status(tree), "Draft")

	def test_tree_without_material_rows_is_draft(self):
		# Bare Main Slip-created trees carry no ledger at all.
		self.assertEqual(
			tree_casting._tree_status(SimpleNamespace(material_details=[])), "Draft"
		)

	def test_received_when_receive_plus_loss_equals_issue_float_dust(self):
		# Regression for GEPL-TR-26-00147: 3 - 2.9 - 0.1 leaves floating-point dust (~8e-17)
		# just ABOVE zero, so a strict pending <= 0 wrongly stuck the tree at "Partially
		# Received" and the manual Submit button (shown only at "Received") never appeared.
		# The eps tolerance must treat it as fully received.
		self.assertGreater(_md(3, 2.9, 0.1).pending_qty, 0)  # confirm the dust is > 0
		tree = SimpleNamespace(material_details=[_md(3, 2.9, 0.1)])
		self.assertEqual(tree_casting._tree_status(tree), "Received")

	def test_partial_when_pending_above_eps(self):
		# A genuine shortfall larger than eps (0.1g remaining) must stay Partially Received.
		tree = SimpleNamespace(material_details=[_md(3, 2.8, 0.1)])
		self.assertEqual(tree_casting._tree_status(tree), "Partially Received")

	def tearDown(self):
		return super().tearDown()


class TestValidateCastingTree(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, eir, mwos, tree_links=None, tree_status=None):
		"""Invoke validate_casting_tree with DB calls mocked.

		mwos: {name: _FakeMWO}
		tree_links: {mwo_name: tree_name}  (current MWO.tree_number)
		tree_status: {tree_name: status}
		"""
		tree_links = tree_links or {}
		tree_status = tree_status or {}

		def fake_get_value(doctype, name, field, *a, **k):
			if doctype == "Department Operation":
				return 1  # tree_no_reqd -> casting
			if doctype == "Manufacturing Work Order" and field == "tree_number":
				return tree_links.get(name)
			if doctype == "Tree Number" and field == "status":
				return tree_status.get(name)
			return None

		with (
			patch.object(
				tree_casting.frappe.db, "get_value", side_effect=fake_get_value
			),
			patch.object(
				tree_casting.frappe, "get_cached_doc", side_effect=lambda dt, n: mwos[n]
			),
		):
			tree_casting.validate_casting_tree(eir)

	def test_same_metal_passes(self):
		mwos = {
			"MWO-A": _FakeMWO(
				"MWO-A",
				metal_type="Gold",
				metal_touch="18KT",
				metal_purity="75",
				metal_colour="Y",
			),
			"MWO-B": _FakeMWO(
				"MWO-B",
				metal_type="Gold",
				metal_touch="18KT",
				metal_purity="75",
				metal_colour="Y",
			),
		}
		self._run(_eir(["MWO-A", "MWO-B"]), mwos)  # no throw

	def test_metal_mismatch_throws(self):
		mwos = {
			"MWO-A": _FakeMWO(
				"MWO-A",
				metal_type="Gold",
				metal_touch="18KT",
				metal_purity="75",
				metal_colour="Y",
			),
			"MWO-B": _FakeMWO(
				"MWO-B",
				metal_type="Gold",
				metal_touch="22KT",
				metal_purity="91",
				metal_colour="Y",
			),
		}
		with self.assertRaises(ValidationError):
			self._run(_eir(["MWO-A", "MWO-B"]), mwos)

	def test_multicolour_skips_colour_check(self):
		mwos = {
			"MWO-A": _FakeMWO(
				"MWO-A",
				metal_type="Gold",
				metal_touch="18KT",
				metal_purity="75",
				metal_colour="Y",
				multicolour=1,
			),
			"MWO-B": _FakeMWO(
				"MWO-B",
				metal_type="Gold",
				metal_touch="18KT",
				metal_purity="75",
				metal_colour="W",
				multicolour=1,
			),
		}
		self._run(_eir(["MWO-A", "MWO-B"]), mwos)  # colour differs but allowed

	def test_active_tree_no_longer_blocks_in_validate(self):
		# The atomic-issue block was REMOVED from validate_casting_tree: a work order still linked to
		# an active (non-terminal) tree no longer throws here. Whole-group vs partial re-issue is now
		# enforced at submit by validate_casting_group_complete (see TestValidateCastingGroupComplete),
		# so validate must stay silent to keep partial drafts saveable during row assembly.
		mwos = {
			"MWO-A": _FakeMWO(
				"MWO-A",
				metal_type="Gold",
				metal_touch="18KT",
				metal_purity="75",
				metal_colour="Y",
			),
		}
		# Prior tree still Issued (active) — previously this threw; now it must not.
		self._run(
			_eir(["MWO-A"]),
			mwos,
			tree_links={"MWO-A": "2026-01-01-0001"},
			tree_status={"2026-01-01-0001": "Issued"},
		)  # no throw

	def test_validate_ignores_tree_state_entirely(self):
		# validate_casting_tree no longer inspects Tree Number status at all: a whole-group re-issue
		# passes here for EVERY prior-tree state (this reproduces the GEPL-TR-26-00147 report — a tree
		# stuck at "Partially Received" no longer blocks the re-issue; the received-state decision
		# moved to submit).
		mwos = {
			"MWO-A": _FakeMWO(
				"MWO-A",
				metal_type="Gold",
				metal_touch="18KT",
				metal_purity="75",
				metal_colour="Y",
			),
			"MWO-B": _FakeMWO(
				"MWO-B",
				metal_type="Gold",
				metal_touch="18KT",
				metal_purity="75",
				metal_colour="Y",
			),
		}
		for status in ("Issued", "Partially Received", "Received", "Submitted"):
			self._run(
				_eir(["MWO-A", "MWO-B"]),
				mwos,
				tree_links={
					"MWO-A": "2026-01-01-0001",
					"MWO-B": "2026-01-01-0001",
				},
				tree_status={"2026-01-01-0001": status},
			)  # no throw for any tree state

	def tearDown(self):
		return super().tearDown()


class _MWODoc(SimpleNamespace):
	"""Fake MWO supporting both attribute access and .get() (like a real Document)."""

	def get(self, key, default=None):
		return getattr(self, key, default)


class _FakeTreeDoc:
	"""Captures the Tree Number the issue path builds, without touching the DB."""

	def __init__(self):
		self.material_details = []
		self.name = "TREE-TEST-0001"
		self.flags = SimpleNamespace()

	def set(self, key, value):
		setattr(self, key, value)

	def get(self, key, default=None):
		return getattr(self, key, default)

	def append(self, _table, row):
		child = SimpleNamespace(**row)
		self.material_details.append(child)
		return child

	def insert(self, *a, **k):
		pass


class TestCastingIssueQtySeed(IntegrationTestCase):
	"""create_tree_on_issue lists the metal-item rows with issue_qty=0; the Tree Number
	Issue Material button owns issue_qty (button-driven casting ledger). The
	casting_issue_qty_by_item helper (MWO.metal_weight) stays the planned reference."""

	def test_helper_sums_metal_weight_ignoring_gross_wt(self):
		attrs = dict(
			metal_type="Gold",
			metal_touch="22KT",
			metal_purity="91.9",
			metal_colour="Yellow",
		)
		rows = [
			(None, _MWODoc(name="A", metal_weight=7.657, gross_wt=0.0, **attrs)),
			(None, _MWODoc(name="B", metal_weight=2.343, gross_wt=0.0, **attrs)),
		]
		with patch.object(
			tree_casting, "get_item_from_attribute", return_value="M-G-22KT-91.9-Y"
		):
			out = tree_casting.casting_issue_qty_by_item(rows)
		# Both MWOs resolve to the same metal item -> summed; gross_wt (0) is irrelevant.
		self.assertEqual(out, {"M-G-22KT-91.9-Y": 10.0})

	def test_create_tree_on_issue_lists_rows_with_zero_issue_qty(self):
		mwo = _MWODoc(
			name="MWO-A",
			metal_type="Gold",
			metal_touch="22KT",
			metal_purity="91.9",
			metal_colour="Yellow",
			metal_weight=7.657,
			gross_wt=0.0,  # casting issue: no metal on the operation yet
		)
		eir = SimpleNamespace(
			name="EIR-1",
			company="C",
			manufacturer="M",
			department="Waxing",
			operation="Casting",
			employee="E",
			employee_ir_operations=[SimpleNamespace(manufacturing_work_order="MWO-A")],
		)
		fake_tree = _FakeTreeDoc()

		def fake_get_value(doctype, name, field, *a, **k):
			if doctype == "Department Operation":
				return 1  # tree_no_reqd -> casting
			return None

		with (
			patch.object(
				tree_casting.frappe.db, "get_value", side_effect=fake_get_value
			),
			patch.object(
				tree_casting.frappe, "get_cached_doc", side_effect=lambda dt, n: mwo
			),
			patch.object(tree_casting.frappe, "new_doc", return_value=fake_tree),
			patch.object(
				tree_casting, "get_item_from_attribute", return_value="M-G-22KT-91.9-Y"
			),
			patch.object(tree_casting.frappe.db, "set_value"),
		):
			tree_casting.create_tree_on_issue(eir)

		self.assertEqual(len(fake_tree.material_details), 1)
		md = fake_tree.material_details[0]
		self.assertEqual(md.item_code, "M-G-22KT-91.9-Y")
		# issue_qty starts at 0 (button-owned); the row just lists the metal item.
		self.assertEqual(md.issue_qty, 0)
		self.assertEqual(md.pending_qty, 0)


def _mwo_doc(name, tree_number):
	"""Fake MWO for the receive-aggregation path (needs attribute access for _metal_item and
	.get('tree_number'))."""
	return _MWODoc(
		name=name,
		metal_type="Gold",
		metal_touch="18KT",
		metal_purity="75",
		metal_colour="Y",
		tree_number=tree_number,
	)


def _recv_eir(rows, typ="Receive", loss_rows=None, is_main_slip_required=1):
	"""Receive EIR. rows: [(mwo, received_gross_wt[, gross_wt])] — gross_wt defaults to received
	(exact fill / no gain); loss_rows: [(mwo, proportionally_loss)]."""

	def _op(r):
		name, recv = r[0], r[1]
		gross = r[2] if len(r) > 2 else recv
		return SimpleNamespace(
			manufacturing_work_order=name, received_gross_wt=recv, gross_wt=gross
		)

	return SimpleNamespace(
		operation="Casting WO",
		type=typ,
		is_main_slip_required=is_main_slip_required,
		manually_book_loss_details=[
			SimpleNamespace(
				variant_of="M", manufacturing_work_order=m, proportionally_loss=n
			)
			for m, n in (loss_rows or [])
		],
		employee_loss_details=[],
		employee_ir_operations=[_op(r) for r in rows],
	)


def _ledger_tree(issue=0.0, receive=0.0, loss=0.0, item="M-G-18KT-75-Y"):
	"""Fake Tree Number carrying one material row, for the receive guard."""
	tree = SimpleNamespace(
		name="TREE-0001",
		status="Issued",
		flags=SimpleNamespace(),
		material_details=[
			SimpleNamespace(
				item_code=item,
				issue_qty=issue,
				receive_qty=receive,
				loss_qty=loss,
				pending_qty=issue - receive - loss,
			)
		],
	)
	tree.save = lambda *a, **k: None
	return tree


class TestValidateCastingReceive(IntegrationTestCase):
	"""validate_casting_receive guards a casting Receive EIR against the metal ISSUED onto its tree.

	Only the per-row gain (received_gross_wt - gross_wt) is drawn from the tree — that is exactly
	what the Main Slip injection mints out of the MSL warehouse the tree funds. A receive with no
	gain touches nothing. A gain needs both a Main Slip to source it physically and enough
	outstanding tree balance to back it."""

	ITEM = "M-G-18KT-75-Y"

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, eir, mwos=None, tree=None):
		mwos = mwos or {"MWO-A": _mwo_doc("MWO-A", "TREE-0001")}
		tree = tree if tree is not None else _ledger_tree(issue=100.0, item=self.ITEM)

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
			patch.object(
				tree_casting, "get_item_from_attribute", return_value=self.ITEM
			),
		):
			tree_casting.validate_casting_receive(eir)

	def test_normal_receive_passes(self):
		# received (8) == gross (8) -> no gain, nothing drawn from the tree, no ledger read.
		self._run(_recv_eir([("MWO-A", 8.0, 8.0)]), tree=_ledger_tree(issue=0.0))

	def test_under_receipt_passes(self):
		# received 3 < gross 8 (rest is loss) -> no gain, tree untouched.
		self._run(_recv_eir([("MWO-A", 3.0, 8.0)]), tree=_ledger_tree(issue=0.0))

	def test_gain_draws_excess_from_tree(self):
		# received 10 > gross 8; the tree holds 100 outstanding -> the 2 fits, allowed.
		self._run(_recv_eir([("MWO-A", 10.0, 8.0)]))

	def test_gain_without_tree_issue_throws(self):
		# The headline rule: a 2g gain against a tree that was never issued has nothing behind it.
		with self.assertRaises(ValidationError):
			self._run(_recv_eir([("MWO-A", 10.0, 8.0)]), tree=_ledger_tree(issue=0.0))

	def test_gain_beyond_tree_pending_throws(self):
		# Gain 2.0 but only 1.5 outstanding on the tree -> block the whole receive.
		with self.assertRaises(ValidationError):
			self._run(_recv_eir([("MWO-A", 10.0, 8.0)]), tree=_ledger_tree(issue=1.5))

	def test_gain_without_main_slip_throws(self):
		# received 10 > gross 8 but no Main Slip to source the excess -> nothing can move.
		with self.assertRaises(ValidationError):
			self._run(_recv_eir([("MWO-A", 10.0, 8.0)], is_main_slip_required=0))

	def test_normal_receive_with_loss_passes(self):
		# recv 4 + loss 1 == gross 5. Booked metal loss never leaves the MSL pool, so it is not
		# a tree draw; the tree ledger stays out of it entirely.
		self._run(
			_recv_eir([("MWO-A", 4.0, 5.0)], loss_rows=[("MWO-A", 1.0)]),
			tree=_ledger_tree(issue=0.0),
		)

	def test_loss_over_book_is_not_a_tree_concern(self):
		# Over-booking loss is owned by validate_loss_qty / validate_loss_tables_required, which
		# cap total loss at (gross - received). The tree guard must not double-police it: with no
		# gain there is no tree draw, so this passes here.
		self._run(
			_recv_eir([("MWO-A", 4.0, 5.0)], loss_rows=[("MWO-A", 1.5)]),
			tree=_ledger_tree(issue=0.0),
		)

	def test_issue_type_is_ignored(self):
		# type != "Receive" -> early return, no guard even when grossly over.
		self._run(_recv_eir([("MWO-A", 99.0, 1.0)], typ="Issue"))

	def tearDown(self):
		return super().tearDown()


class TestUpdateTreeOnReceiveCancel(IntegrationTestCase):
	"""update_tree_on_receive(cancel=True) subtracts the same magnitude the forward pass added.

	The draw is computed at sign=+1 and the RESULT negated. Negating the inputs instead would push
	every gain through max(received - gross, 0) as a negative and silently reverse nothing, leaving
	the ledger inflated and the Issue EIR permanently uncancellable."""

	ITEM = "M-G-18KT-75-Y"

	@classmethod
	def setUpClass(cls):
		pass

	def _cancel(self, eir, tree, mwos=None):
		mwos = mwos or {"MWO-A": _mwo_doc("MWO-A", "TREE-0001")}
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
			patch.object(
				tree_casting, "get_item_from_attribute", return_value=self.ITEM
			),
		):
			tree_casting.update_tree_on_receive(eir, cancel=True)

	def test_cancel_reverses_the_gain(self):
		# Forward drew 2.0 (gross 8 -> received 10) against an issue of 5.
		tree = _ledger_tree(issue=5.0, receive=2.0, item=self.ITEM)
		self._cancel(_recv_eir([("MWO-A", 10.0, 8.0)]), tree)
		self.assertEqual(tree.material_details[0].receive_qty, 0.0)
		self.assertEqual(tree.material_details[0].issue_qty, 5.0)

	def test_cancel_without_guard(self):
		# Cancel is a credit, not a draw — the availability guard must never fire, even on a
		# tree with no issued balance at all.
		tree = _ledger_tree(issue=0.0, receive=2.0, item=self.ITEM)
		self._cancel(_recv_eir([("MWO-A", 10.0, 8.0)]), tree)
		self.assertEqual(tree.material_details[0].receive_qty, 0.0)

	def test_cancel_of_a_no_gain_receive_is_a_no_op(self):
		# received == gross drew nothing forward, so cancelling must take nothing back.
		tree = _ledger_tree(issue=5.0, receive=5.0, item=self.ITEM)
		self._cancel(_recv_eir([("MWO-A", 8.0, 8.0)]), tree)
		self.assertEqual(tree.material_details[0].receive_qty, 5.0)

	def test_cancel_never_drives_receive_negative(self):
		tree = _ledger_tree(issue=5.0, receive=1.0, item=self.ITEM)
		self._cancel(_recv_eir([("MWO-A", 10.0, 8.0)]), tree)
		self.assertGreaterEqual(tree.material_details[0].receive_qty, 0.0)

	def test_cancel_leaves_issue_and_loss_untouched(self):
		# The EIR path owns receive_qty only; issue_qty is button-owned and loss_qty belongs to
		# the tree's own Receive/Submit legs.
		tree = _ledger_tree(issue=5.0, receive=2.0, loss=0.5, item=self.ITEM)
		self._cancel(_recv_eir([("MWO-A", 10.0, 8.0)]), tree)
		self.assertEqual(tree.material_details[0].issue_qty, 5.0)
		self.assertEqual(tree.material_details[0].loss_qty, 0.5)

	def tearDown(self):
		return super().tearDown()


def _grp_mwo(name, group):
	"""Fake MWO for the casting-group path: carries only casting_group + name."""
	return _FakeMWO(name, casting_group=group)


def _grp_eir(work_orders, typ="Issue"):
	"""Issue EIR whose rows carry (manufacturing_work_order). work_orders: list of names."""
	return SimpleNamespace(
		operation="Casting WO",
		type=typ,
		department="Casting Dept",
		subcontracting="No",
		employee_ir_operations=[
			SimpleNamespace(manufacturing_work_order=name) for name in work_orders
		],
	)


class TestValidateCastingGroupComplete(IntegrationTestCase):
	"""validate_casting_group_complete — no partial re-issue of a casting tree. The required set is
	EVERY member sharing the casting_group; a member still at casting is 'addable' (button), one
	that has advanced past casting is 'blocked' (must be reversed first). Grouping is via
	casting_group, so it holds even after the tree was cancelled/deleted (tree_number cleared).

	The rule is gated by MOP Settings.enforce_full_casting_tree_reissue, which ships OFF. Every
	enforcement test here forces it ON via _run(enforce=True) (the default) so they keep pinning
	the RULE; the gate itself is covered by the test_gate_* tests at the end of the class."""

	def _run(
		self, eir, present_mwos, required, eligible_mwos, casting=True, enforce=True
	):
		"""present_mwos: {name: _FakeMWO with casting_group}; required: full member name list;
		eligible_mwos: member names still issue-eligible (returned by eligible_casting_group_mops).

		``enforce`` drives the MOP Settings master switch
		(``enforce_full_casting_tree_reissue``). It defaults to True so the enforcement tests
		below keep asserting the RULE rather than the gate -- at the shipped default (OFF) the
		"no throw" ones would otherwise pass vacuously. Returns the get_single_value mock so the
		gate tests can assert whether, and with what, the switch was read.
		"""

		def fake_get_value(doctype, name, field, *a, **k):
			if doctype == "Department Operation":
				return 1 if casting else 0
			return None

		def fake_get_all(doctype, filters=None, *a, **k):
			return list(required)  # the "Manufacturing Work Order" casting_group query

		def fake_eligible(department, subcontracting, groups):
			return [
				{"manufacturing_work_order": m, "manufacturing_operation": f"MOP-{m}"}
				for m in eligible_mwos
			]

		# frappe.db is a LocalProxy, so an auto-created patch would mint an AsyncMock whose
		# truthy coroutine return would pin the gate ON in every test. Inject a real MagicMock.
		single = MagicMock(return_value=1 if enforce else 0)

		with (
			patch.object(tree_casting.frappe.db, "get_single_value", single),
			patch.object(
				tree_casting.frappe.db, "get_value", side_effect=fake_get_value
			),
			patch.object(
				tree_casting.frappe,
				"get_cached_doc",
				side_effect=lambda dt, n: present_mwos[n],
			),
			patch.object(tree_casting.frappe, "get_all", side_effect=fake_get_all),
			patch.object(
				tree_casting, "eligible_casting_group_mops", side_effect=fake_eligible
			),
		):
			tree_casting.validate_casting_group_complete(eir)

		return single

	def test_first_issue_no_group_skips(self):
		# No casting_group yet (stamped on submit) -> nothing to complete even if siblings exist.
		self._run(
			_grp_eir(["MWO-A"]),
			{"MWO-A": _grp_mwo("MWO-A", None)},
			required=["MWO-A", "MWO-B"],
			eligible_mwos=["MWO-A", "MWO-B"],
		)  # no throw

	def test_reissue_full_set_passes(self):
		self._run(
			_grp_eir(["MWO-A", "MWO-B"]),
			{"MWO-A": _grp_mwo("MWO-A", "G"), "MWO-B": _grp_mwo("MWO-B", "G")},
			required=["MWO-A", "MWO-B"],
			eligible_mwos=["MWO-A", "MWO-B"],
		)  # no throw

	def test_reissue_partial_set_throws_naming_missing(self):
		with self.assertRaises(ValidationError) as cm:
			self._run(
				_grp_eir(["MWO-A"]),
				{"MWO-A": _grp_mwo("MWO-A", "G")},
				required=["MWO-A", "MWO-B"],
				eligible_mwos=["MWO-A", "MWO-B"],  # B still at casting -> addable
			)
		self.assertIn("MWO-B", str(cm.exception))

	def test_advanced_sibling_blocks_with_reverse_message(self):
		# B is a member but no longer issue-eligible (advanced past casting): re-issue is blocked
		# and the message tells the operator to reverse it back first.
		with self.assertRaises(ValidationError) as cm:
			self._run(
				_grp_eir(["MWO-A"]),
				{"MWO-A": _grp_mwo("MWO-A", "G")},
				required=["MWO-A", "MWO-B"],
				eligible_mwos=["MWO-A"],  # B NOT eligible -> blocked
			)
		msg = str(cm.exception)
		self.assertIn("MWO-B", msg)
		self.assertIn("reverse", msg.lower())

	def test_group_retained_after_cancel_still_enforced(self):
		# tree_number is None here (tree was cancelled/deleted); grouping survives via casting_group.
		with self.assertRaises(ValidationError):
			self._run(
				_grp_eir(["MWO-A"]),
				{"MWO-A": _grp_mwo("MWO-A", "G")},
				required=["MWO-A", "MWO-B", "MWO-C"],
				eligible_mwos=["MWO-B", "MWO-C"],
			)

	def test_mixed_group_plus_new_mwo_throws(self):
		# A belongs to group G; N is brand-new (no group). N adds no group and does not satisfy G.
		with self.assertRaises(ValidationError) as cm:
			self._run(
				_grp_eir(["MWO-A", "MWO-N"]),
				{"MWO-A": _grp_mwo("MWO-A", "G"), "MWO-N": _grp_mwo("MWO-N", None)},
				required=["MWO-A", "MWO-B"],
				eligible_mwos=["MWO-A", "MWO-B"],
			)
		self.assertIn("MWO-B", str(cm.exception))

	def test_receive_type_skips(self):
		self._run(
			_grp_eir(["MWO-A"], typ="Receive"),
			{"MWO-A": _grp_mwo("MWO-A", "G")},
			required=["MWO-A", "MWO-B"],
			eligible_mwos=["MWO-A", "MWO-B"],
		)  # no throw

	def test_non_casting_eir_skips(self):
		self._run(
			_grp_eir(["MWO-A"]),
			{"MWO-A": _grp_mwo("MWO-A", "G")},
			required=["MWO-A", "MWO-B"],
			eligible_mwos=["MWO-A", "MWO-B"],
			casting=False,  # Department Operation.tree_no_reqd = 0
		)  # no throw

	def test_group_key_falls_back_to_tree_number(self):
		# Defense-in-depth: a tree'd MWO that lacks a casting_group must still be enforced via its
		# tree_number (the group key falls back), so a partial re-issue is still caught now that
		# validate_casting_tree no longer blocks in validate().
		with self.assertRaises(ValidationError) as cm:
			self._run(
				_grp_eir(["MWO-A"]),
				{"MWO-A": _FakeMWO("MWO-A", casting_group=None, tree_number="G")},
				required=["MWO-A", "MWO-B"],
				eligible_mwos=["MWO-A", "MWO-B"],
			)
		self.assertIn("MWO-B", str(cm.exception))

	# ------------------------------------------------------------------
	# MOP Settings gate: enforce_full_casting_tree_reissue (default OFF).
	# Every test above forces it ON; these prove the switch works both ways.
	# ------------------------------------------------------------------

	def test_gate_off_allows_partial_reissue(self):
		# THE REGRESSION TEST. Byte-identical inputs to
		# test_reissue_partial_set_throws_naming_missing, opposite outcome -- driven only by
		# the setting. OFF is the shipped default, so out of the box a partial re-issue submits.
		self._run(
			_grp_eir(["MWO-A"]),
			{"MWO-A": _grp_mwo("MWO-A", "G")},
			required=["MWO-A", "MWO-B"],
			eligible_mwos=["MWO-A", "MWO-B"],
			enforce=False,
		)  # no throw

	def test_gate_off_allows_partial_reissue_with_advanced_sibling(self):
		# The "blocked" branch (sibling already past casting, normally "reverse these back
		# first") is gated too: OFF means the whole rule is off, not just the addable half.
		self._run(
			_grp_eir(["MWO-A"]),
			{"MWO-A": _grp_mwo("MWO-A", "G")},
			required=["MWO-A", "MWO-B"],
			eligible_mwos=["MWO-A"],  # B not eligible -> would be "blocked" when ON
			enforce=False,
		)  # no throw

	def test_gate_off_allows_partial_reissue_via_tree_number_fallback(self):
		# The defence-in-depth tree_number group key is gated as well -- no leak through it.
		self._run(
			_grp_eir(["MWO-A"]),
			{"MWO-A": _FakeMWO("MWO-A", casting_group=None, tree_number="G")},
			required=["MWO-A", "MWO-B"],
			eligible_mwos=["MWO-A", "MWO-B"],
			enforce=False,
		)  # no throw

	def test_gate_on_preserves_message_and_title(self):
		# Switch ON -> today's error surface unchanged. Assertions stay tag-free: msgprint
		# strips HTML when stdin is a tty (interactive bench) but keeps it in CI, so only
		# markup-free substrings are stable across both.
		with self.assertRaises(ValidationError) as cm:
			self._run(
				_grp_eir(["MWO-A"]),
				{"MWO-A": _grp_mwo("MWO-A", "G")},
				required=["MWO-A", "MWO-B"],
				eligible_mwos=["MWO-A", "MWO-B"],
				enforce=True,
			)
		msg = str(cm.exception)
		self.assertIn("must be re-issued in full", msg)
		self.assertIn("Load Full Casting Tree", msg)
		self.assertIn("MWO-B", msg)

	def test_gate_on_full_group_reissue_still_passes(self):
		# Unchanged happy path under the switch: whole group present -> no throw.
		self._run(
			_grp_eir(["MWO-A", "MWO-B"]),
			{"MWO-A": _grp_mwo("MWO-A", "G"), "MWO-B": _grp_mwo("MWO-B", "G")},
			required=["MWO-A", "MWO-B"],
			eligible_mwos=["MWO-A", "MWO-B"],
			enforce=True,
		)  # no throw

	def test_gate_reads_the_mop_settings_switch(self):
		# Pins the wiring: exact Single + exact fieldname, read once. A typo in either makes
		# frappe.db.get_single_value throw "Field ... does not exist" in PRODUCTION, because it
		# resolves the df from meta before returning (frappe/database/database.py:914-920).
		single = self._run(
			_grp_eir(["MWO-A"]),
			{"MWO-A": _grp_mwo("MWO-A", "G")},
			required=["MWO-A", "MWO-B"],
			eligible_mwos=["MWO-A", "MWO-B"],
			enforce=False,
		)
		single.assert_called_once_with(
			"MOP Settings", "enforce_full_casting_tree_reissue"
		)

	def test_gate_not_consulted_for_non_casting_eir(self):
		# EmployeeIR.before_submit runs this for EVERY Issue EIR, not just casting ones
		# (employee_ir.py:100-102). The switch must therefore be read only AFTER the
		# type/casting guard, so a site running this code before `bench migrate` installs the
		# field cannot break every Issue submit with "Field ... does not exist".
		single = self._run(
			_grp_eir(["MWO-A"]),
			{"MWO-A": _grp_mwo("MWO-A", "G")},
			required=["MWO-A", "MWO-B"],
			eligible_mwos=["MWO-A", "MWO-B"],
			casting=False,
			enforce=False,
		)  # no throw
		single.assert_not_called()

	def test_gate_not_consulted_for_receive_type(self):
		single = self._run(
			_grp_eir(["MWO-A"], typ="Receive"),
			{"MWO-A": _grp_mwo("MWO-A", "G")},
			required=["MWO-A", "MWO-B"],
			eligible_mwos=["MWO-A", "MWO-B"],
			enforce=False,
		)  # no throw
		single.assert_not_called()

	def tearDown(self):
		return super().tearDown()


class TestCastingGroupStamp(IntegrationTestCase):
	"""create_tree_on_issue stamps ONE coalesced casting_group on every row: the fresh tree's name
	on a first issue, or an existing group carried forward on a re-issue (never split)."""

	def _stamp(self, mwos):
		"""Run create_tree_on_issue over `mwos` (dict name->_MWODoc); return {name: updates_dict}."""
		fake_tree = _FakeTreeDoc()  # .name == "TREE-TEST-0001"
		eir = SimpleNamespace(
			name="EIR-1",
			company="C",
			manufacturer="M",
			department="Waxing",
			operation="Casting",
			employee="E",
			employee_ir_operations=[
				SimpleNamespace(manufacturing_work_order=n) for n in mwos
			],
		)

		def fake_get_value(doctype, name, field, *a, **k):
			return 1 if doctype == "Department Operation" else None

		with (
			patch.object(
				tree_casting.frappe.db, "get_value", side_effect=fake_get_value
			),
			patch.object(
				tree_casting.frappe, "get_cached_doc", side_effect=lambda dt, n: mwos[n]
			),
			patch.object(tree_casting.frappe, "new_doc", return_value=fake_tree),
			patch.object(
				tree_casting, "get_item_from_attribute", return_value="M-ITEM"
			),
			patch.object(tree_casting.frappe.db, "set_value") as set_value,
		):
			tree_casting.create_tree_on_issue(eir)

		return {c.args[1]: c.args[2] for c in set_value.call_args_list}, fake_tree.name

	def _mwo(self, name, group):
		return _MWODoc(
			name=name,
			metal_type="Gold",
			metal_touch="18KT",
			metal_purity="75",
			metal_colour="Y",
			metal_weight=1.0,
			gross_wt=0.0,
			casting_group=group,
		)

	def test_first_issue_stamps_tree_name(self):
		updates, tree_name = self._stamp(
			{"MWO-A": self._mwo("MWO-A", None), "MWO-B": self._mwo("MWO-B", None)}
		)
		for name in ("MWO-A", "MWO-B"):
			self.assertEqual(updates[name]["tree_number"], tree_name)
			self.assertEqual(updates[name]["casting_group"], tree_name)

	def test_reissue_keeps_existing_group(self):
		updates, tree_name = self._stamp(
			{"MWO-A": self._mwo("MWO-A", "G-OLD"), "MWO-B": self._mwo("MWO-B", None)}
		)
		# A already on the group -> casting_group not re-written; B pulled onto the SAME group,
		# never the new tree name.
		self.assertNotIn("casting_group", updates["MWO-A"])
		self.assertEqual(updates["MWO-B"]["casting_group"], "G-OLD")
		self.assertNotEqual(updates["MWO-B"]["casting_group"], tree_name)

	def test_coalesces_two_prior_groups(self):
		updates, _ = self._stamp(
			{"MWO-A": self._mwo("MWO-A", "G1"), "MWO-B": self._mwo("MWO-B", "G2")}
		)
		# First-found group wins; the physically single tree ends up on ONE id.
		self.assertNotIn("casting_group", updates["MWO-A"])  # already G1
		self.assertEqual(updates["MWO-B"]["casting_group"], "G1")

	def tearDown(self):
		return super().tearDown()


class TestGetCastingGroupOperations(IntegrationTestCase):
	"""The 'Load Full Casting Tree' whitelist returns exactly the still-at-casting siblings not yet
	present — so one click satisfies the submit-time completeness check."""

	def _call(self, present, mop_to_mwo, groups, eligible):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir import employee_ir

		def fake_get_all(doctype, filters=None, *a, **k):
			if doctype == "Manufacturing Operation":
				return [mop_to_mwo[m] for m in filters["name"][1]]
			if doctype == "Manufacturing Work Order":
				return list(groups)
			return []

		def fake_eligible(department, subcontracting, grps):
			return eligible

		with (
			patch.object(employee_ir.frappe, "get_all", side_effect=fake_get_all),
			patch.object(
				tree_casting, "eligible_casting_group_mops", side_effect=fake_eligible
			),
		):
			return employee_ir.get_casting_group_operations(
				"Casting Dept", "No", present
			)

	def test_returns_missing_siblings(self):
		out = self._call(
			present=["MOP-A"],
			mop_to_mwo={"MOP-A": "MWO-A"},
			groups=["G"],
			eligible=[
				{
					"manufacturing_operation": "MOP-A",
					"manufacturing_work_order": "MWO-A",
				},
				{
					"manufacturing_operation": "MOP-B",
					"manufacturing_work_order": "MWO-B",
				},
			],
		)
		self.assertEqual([r["manufacturing_operation"] for r in out], ["MOP-B"])

	def test_returns_empty_when_full(self):
		out = self._call(
			present=["MOP-A", "MOP-B"],
			mop_to_mwo={"MOP-A": "MWO-A", "MOP-B": "MWO-B"},
			groups=["G"],
			eligible=[
				{
					"manufacturing_operation": "MOP-A",
					"manufacturing_work_order": "MWO-A",
				},
				{
					"manufacturing_operation": "MOP-B",
					"manufacturing_work_order": "MWO-B",
				},
			],
		)
		self.assertEqual(out, [])

	def test_empty_present_returns_empty(self):
		self.assertEqual(
			self._call(present=[], mop_to_mwo={}, groups=[], eligible=[]), []
		)

	def tearDown(self):
		return super().tearDown()


class _FakeSE:
	"""Captures the Stock Entry the helper builds without touching the DB."""

	def __init__(self, index=0):
		self.items = []
		self.name = None
		self.flags = SimpleNamespace()
		self.submitted = False
		self._index = index

	def append(self, _table, row):
		child = SimpleNamespace(**row)
		self.items.append(child)
		return child

	def get(self, key, default=None):
		return getattr(self, key, default)

	def insert(self, *a, **k):
		self.name = f"SE-TREE-TEST-{self._index:04d}"

	def submit(self, *a, **k):
		self.submitted = True


class _RunResult(list):
	"""The list of FakeSEs a helper created (in build order), plus ``.value`` = its return."""

	value = None


def _new_tree(employee_ir=None, material_details=None):
	"""A real (un-inserted) Tree Number doc with .save stubbed out."""
	tree = frappe.new_doc("Tree Number")
	tree.company = "_Test Company"
	tree.department = "_Test Dept"
	tree.employee = "_Test Emp"
	if employee_ir:
		tree.employee_ir = employee_ir
	for md in material_details or []:
		tree.append("material_details", md)
	tree.save = MagicMock()
	return tree


def _run(
	fn,
	*args,
	source_wh="SRC-MFG",
	msl_wh="EMP-MSL",
	rm_wh="DEPT-RM",
	scrap_wh="DEPT-SCRAP",
	loss_item="GOLD-18KT-ML",
	owed=None,
	ownership=None,
):
	"""Call an op helper with persistence + warehouse/loss-item resolution mocked.

	Returns a _RunResult (list of every FakeSE created, in build order — receive posts the
	received transfer first, then the loss Repack); ``.value`` holds the helper's return.

	``owed`` stubs the tree's owed-batch pool (``_tree_owed_batches``) — a ``[(batch_no, qty)]``
	list, or a ``{item_code: [(batch_no, qty)]}`` dict for multi-item trees. It defaults to a
	single unbounded batch per item so shape-focused tests need not model batch provenance;
	``TestTreeOwedBatches`` / ``TestReceiveBatchParity`` cover that layer directly.
	``ownership`` stubs ``_batch_ownership`` (``{batch_no: (inventory_type, customer)}``).
	"""
	fakes = _RunResult()

	def _mint(*_a, **_k):
		fake = _FakeSE(index=len(fakes))
		fakes.append(fake)
		return fake

	def _owed(_se, _tree, item_code, _msl_wh):
		if owed is None:
			return [(f"{item_code}-BATCH", 1e9)]
		if isinstance(owed, dict):
			return list(owed.get(item_code, []))
		return list(owed)

	with (
		patch.object(tse.frappe, "new_doc", side_effect=_mint),
		patch.object(tse.frappe, "has_permission", return_value=True),
		patch.object(tse.frappe, "get_precision", return_value=3),
		patch.object(tse, "_apply_fifo_batches_to_stock_entry"),
		patch.object(tse, "preallocate_series_for_docs"),
		patch.object(tse, "lock_bins"),
		patch.object(tse, "_tree_owed_batches", side_effect=_owed),
		patch.object(tse, "_batch_ownership", return_value=ownership or {}),
		patch.object(tse, "_resolve_source_warehouse", return_value=source_wh),
		patch.object(tse, "_resolve_msl_warehouse", return_value=msl_wh),
		patch.object(tse, "_get_department_rm_warehouse", return_value=rm_wh),
		patch.object(tse, "_resolve_scrap_warehouse", return_value=scrap_wh),
		patch.object(tse, "_resolve_tree_loss_item", return_value=loss_item),
	):
		fakes.value = fn(*args)
	return fakes


# ---------------------------------------------------------------------------
# Issue Material
# ---------------------------------------------------------------------------
class TestIssueMaterial(IntegrationTestCase):
	def test_issue_builds_material_transfer_source_to_msl(self):
		tree = _new_tree()
		fake = _run(tse.issue_material, tree, "GOLD-18KT", 5.0)[0]

		# Plain Material Transfer (NOT WORK ORDER) => ledger-invisible, no reservation.
		self.assertEqual(fake.stock_entry_type, "Material Transfer")
		self.assertEqual(fake.auto_created, 1)
		self.assertEqual(fake.custom_tree_number, tree.name)
		self.assertTrue(fake.submitted)
		self.assertEqual(len(fake.items), 1)
		self.assertEqual(fake.items[0].s_warehouse, "SRC-MFG")
		self.assertEqual(fake.items[0].t_warehouse, "EMP-MSL")
		self.assertEqual(fake.items[0].qty, 5.0)

	def test_issue_updates_ledger_and_status(self):
		tree = _new_tree()
		_run(tse.issue_material, tree, "GOLD-18KT", 5.0)

		self.assertEqual(len(tree.material_details), 1)
		md = tree.material_details[0]
		self.assertEqual(md.item_code, "GOLD-18KT")
		self.assertEqual(md.issue_qty, 5.0)
		self.assertEqual(md.pending_qty, 5.0)
		self.assertEqual(tree.status, "Issued")
		tree.save.assert_called_once()

	def test_issue_accumulates_same_item(self):
		tree = _new_tree()
		_run(tse.issue_material, tree, "GOLD-18KT", 5.0)
		_run(tse.issue_material, tree, "GOLD-18KT", 3.0)
		self.assertEqual(len(tree.material_details), 1)
		self.assertEqual(tree.material_details[0].issue_qty, 8.0)
		self.assertEqual(tree.material_details[0].pending_qty, 8.0)

	def test_issue_zero_qty_throws(self):
		tree = _new_tree()
		with self.assertRaises(ValidationError):
			_run(tse.issue_material, tree, "GOLD-18KT", 0)

	def test_issue_no_item_throws(self):
		tree = _new_tree()
		with self.assertRaises(ValidationError):
			_run(tse.issue_material, tree, "", 5.0)


# ---------------------------------------------------------------------------
# Receive Material
# ---------------------------------------------------------------------------
class TestReceiveMaterial(IntegrationTestCase):
	def _issued_tree(self, issue=10.0):
		return _new_tree(
			material_details=[
				{
					"item_code": "GOLD-18KT",
					"issue_qty": issue,
					"receive_qty": 0,
					"loss_qty": 0,
					"pending_qty": issue,
				}
			]
		)

	def test_receive_builds_transfer_and_loss_repack(self):
		tree = self._issued_tree()
		fakes = _run(
			tse.receive_material,
			tree,
			[{"item_code": "GOLD-18KT", "receive_qty": 6.0, "loss_qty": 1.0}],
		)
		# Two SEs: [0] received transfer, [1] loss Repack. Return value lists both names.
		self.assertEqual(len(fakes), 2)
		se_recv, se_loss = fakes
		self.assertEqual(fakes.value, [se_recv.name, se_loss.name])

		# Received leg: plain Material Transfer (standalone), MSL -> Dept RM, same metal item.
		self.assertEqual(se_recv.stock_entry_type, "Material Transfer")
		self.assertEqual(len(se_recv.items), 1)
		received = se_recv.items[0]
		self.assertEqual(received.item_code, "GOLD-18KT")
		self.assertEqual(
			(received.s_warehouse, received.t_warehouse), ("EMP-MSL", "DEPT-RM")
		)
		self.assertEqual(received.qty, 6.0)

		# Loss leg: Process Loss Repack — consume metal @ MSL, produce ML variant @ Scrap.
		self.assertEqual(se_loss.stock_entry_type, "Process Loss")
		self.assertEqual(se_loss.purpose, "Repack")
		self.assertEqual(se_loss.auto_created, 1)
		self.assertEqual(se_loss.custom_tree_number, tree.name)
		self.assertEqual(len(se_loss.items), 2)
		consume, produce = se_loss.items
		self.assertEqual(consume.item_code, "GOLD-18KT")
		self.assertEqual((consume.s_warehouse, consume.t_warehouse), ("EMP-MSL", None))
		self.assertEqual(consume.qty, 1.0)
		# Produce row is the resolved ML loss variant, written off into Scrap.
		self.assertEqual(produce.item_code, "GOLD-18KT-ML")
		self.assertEqual(
			(produce.s_warehouse, produce.t_warehouse), (None, "DEPT-SCRAP")
		)
		self.assertEqual(produce.qty, 1.0)
		self.assertEqual(produce.is_finished_item, 1)
		self.assertEqual(produce.set_basic_rate_manually, 1)
		# basic_rate is deliberately NOT set by the builder: CustomStockEntry.set_basic_rate
		# assigns it from the consumed rows once ERPNext has resolved their outgoing rates
		# (customization/utils/loss_valuation) -- see test_process_loss_valuation.py.
		self.assertFalse(getattr(produce, "basic_rate", None))

	def test_receive_partial_then_full_status(self):
		tree = self._issued_tree(10.0)
		_run(
			tse.receive_material,
			tree,
			[{"item_code": "GOLD-18KT", "receive_qty": 6.0, "loss_qty": 1.0}],
		)
		md = tree.material_details[0]
		self.assertEqual(md.receive_qty, 6.0)
		self.assertEqual(md.loss_qty, 1.0)
		self.assertEqual(md.pending_qty, 3.0)
		self.assertEqual(tree.status, "Partially Received")

		# Clear the remaining 3 -> Received.
		_run(
			tse.receive_material,
			tree,
			[{"item_code": "GOLD-18KT", "receive_qty": 3.0, "loss_qty": 0.0}],
		)
		self.assertEqual(tree.material_details[0].pending_qty, 0.0)
		self.assertEqual(tree.status, "Received")

	def test_receive_only_loss_leg_when_no_receive(self):
		tree = self._issued_tree(4.0)
		fakes = _run(
			tse.receive_material,
			tree,
			[{"item_code": "GOLD-18KT", "receive_qty": 0, "loss_qty": 4.0}],
		)
		# Loss-only receive: a single Process Loss Repack SE (no transfer leg).
		self.assertEqual(len(fakes), 1)
		se_loss = fakes[0]
		self.assertEqual(se_loss.stock_entry_type, "Process Loss")
		self.assertEqual(se_loss.purpose, "Repack")
		consume, produce = se_loss.items
		self.assertEqual((consume.s_warehouse, consume.t_warehouse), ("EMP-MSL", None))
		self.assertEqual(produce.item_code, "GOLD-18KT-ML")
		self.assertEqual(
			(produce.s_warehouse, produce.t_warehouse), (None, "DEPT-SCRAP")
		)
		self.assertEqual(tree.material_details[0].pending_qty, 0.0)
		self.assertEqual(tree.status, "Received")

	def test_receive_over_pending_throws(self):
		tree = self._issued_tree(10.0)
		with self.assertRaises(ValidationError):
			_run(
				tse.receive_material,
				tree,
				[{"item_code": "GOLD-18KT", "receive_qty": 9.0, "loss_qty": 2.0}],
			)

	def test_receive_unknown_item_throws(self):
		tree = self._issued_tree(10.0)
		with self.assertRaises(ValidationError):
			_run(
				tse.receive_material,
				tree,
				[{"item_code": "SILVER-925", "receive_qty": 1.0, "loss_qty": 0}],
			)

	def test_receive_nothing_throws(self):
		tree = self._issued_tree(10.0)
		with self.assertRaises(ValidationError):
			_run(
				tse.receive_material,
				tree,
				[{"item_code": "GOLD-18KT", "receive_qty": 0, "loss_qty": 0}],
			)


# ---------------------------------------------------------------------------
# Casting (employee_ir-seeded) tree buttons + resolvers
# ---------------------------------------------------------------------------
class TestCastingTreeButtons(IntegrationTestCase):
	"""Casting trees: Issue posts a `Material Transfer (MAIN SLIP)` SE and owns issue_qty; the
	Employee IR Receive books the cast output, and the Receive button returns the post-cast
	leftover (bounded by the pending cap, so it can never re-receive the EIR-booked qty)."""

	def test_casting_issue_posts_main_slip_se(self):
		tree = _new_tree(employee_ir="EIR-CASTING-0001")
		fake = _run(tse.issue_material, tree, "GOLD-18KT", 5.0)[0]
		# Casting Issue is relabelled as MAIN SLIP (still ledger-invisible).
		self.assertEqual(fake.stock_entry_type, "Material Transfer (MAIN SLIP)")
		self.assertTrue(fake.submitted)
		self.assertEqual(fake.items[0].s_warehouse, "SRC-MFG")
		self.assertEqual(fake.items[0].t_warehouse, "EMP-MSL")
		self.assertEqual(tree.material_details[0].issue_qty, 5.0)

	def test_standalone_issue_stays_plain_material_transfer(self):
		tree = _new_tree()  # standalone (employee_ir empty)
		fake = _run(tse.issue_material, tree, "GOLD-18KT", 5.0)[0]
		self.assertEqual(fake.stock_entry_type, "Material Transfer")

	def _casting_tree(self, issue=3.0, receive=2.0, loss=0.0):
		# A casting tree whose Employee IR Receive has already booked `receive` (the cast output),
		# leaving `issue - receive - loss` as the returnable leftover still sitting in MSL.
		return _new_tree(
			employee_ir="EIR-CASTING-0001",
			material_details=[
				{
					"item_code": "GOLD-18KT",
					"issue_qty": issue,
					"receive_qty": receive,
					"loss_qty": loss,
					"pending_qty": issue - receive - loss,
				}
			],
		)

	def test_casting_receive_no_longer_throws(self):
		# Casting trees return the post-cast leftover via the tree button as a MAIN SLIP transfer.
		tree = self._casting_tree(issue=3.0, receive=2.0)  # EIR booked 2 -> pending 1
		fake = _run(
			tse.receive_material, tree, [{"item_code": "GOLD-18KT", "receive_qty": 1.0}]
		)[0]
		self.assertEqual(fake.stock_entry_type, "Material Transfer (MAIN SLIP)")
		self.assertEqual(len(fake.items), 1)
		self.assertEqual(
			(fake.items[0].s_warehouse, fake.items[0].t_warehouse),
			("EMP-MSL", "DEPT-RM"),
		)
		self.assertEqual(fake.items[0].qty, 1.0)
		md = tree.material_details[0]
		self.assertEqual(md.receive_qty, 3.0)
		self.assertEqual(md.pending_qty, 0.0)
		self.assertEqual(tree.status, "Received")

	def test_casting_receive_pending_cap_blocks_over_receive(self):
		# The (recv + loss) <= pending cap still fires for casting -> leftover-only, no double-count.
		tree = self._casting_tree(issue=3.0, receive=2.0)  # pending 1
		with self.assertRaises(ValidationError):
			_run(
				tse.receive_material,
				tree,
				[{"item_code": "GOLD-18KT", "receive_qty": 2.0}],
			)

	def test_casting_receive_stamps_employee(self):
		# The receive SE records the employee (tree.employee == the Issue Employee IR's employee).
		tree = self._casting_tree(issue=3.0, receive=2.0)
		fake = _run(
			tse.receive_material, tree, [{"item_code": "GOLD-18KT", "receive_qty": 1.0}]
		)[0]
		self.assertEqual(fake.employee, tree.employee)

	def test_casting_receive_legs_transfer_and_loss_repack(self):
		tree = self._casting_tree(issue=2.0, receive=0.0)  # pending 2
		fakes = _run(
			tse.receive_material,
			tree,
			[{"item_code": "GOLD-18KT", "receive_qty": 1.0, "loss_qty": 1.0}],
		)
		self.assertEqual(len(fakes), 2)
		se_recv, se_loss = fakes
		# Received leg -> MAIN SLIP transfer, MSL -> Dept RM.
		self.assertEqual(se_recv.stock_entry_type, "Material Transfer (MAIN SLIP)")
		self.assertEqual(
			(se_recv.items[0].s_warehouse, se_recv.items[0].t_warehouse),
			("EMP-MSL", "DEPT-RM"),
		)
		# Loss leg -> Process Loss Repack, metal @ MSL consumed, ML variant @ Scrap produced.
		self.assertEqual(se_loss.stock_entry_type, "Process Loss")
		self.assertEqual(se_loss.employee, tree.employee)
		consume, produce = se_loss.items
		self.assertEqual((consume.s_warehouse, consume.t_warehouse), ("EMP-MSL", None))
		self.assertEqual(produce.item_code, "GOLD-18KT-ML")
		self.assertEqual(
			(produce.s_warehouse, produce.t_warehouse), (None, "DEPT-SCRAP")
		)
		self.assertEqual(tree.status, "Received")

	def test_casting_receive_partial_then_status(self):
		tree = self._casting_tree(issue=3.0, receive=2.0)  # pending 1
		_run(
			tse.receive_material, tree, [{"item_code": "GOLD-18KT", "receive_qty": 0.5}]
		)
		self.assertEqual(tree.material_details[0].pending_qty, 0.5)
		self.assertEqual(tree.status, "Partially Received")
		_run(
			tse.receive_material, tree, [{"item_code": "GOLD-18KT", "receive_qty": 0.5}]
		)
		self.assertEqual(tree.material_details[0].pending_qty, 0.0)
		self.assertEqual(tree.status, "Received")

	def test_is_casting_tree(self):
		self.assertTrue(
			tse._is_casting_tree(SimpleNamespace(get=lambda k, d=None: "EIR-1"))
		)
		self.assertFalse(
			tse._is_casting_tree(SimpleNamespace(get=lambda k, d=None: None))
		)


class TestWarehouseResolvers(IntegrationTestCase):
	def test_msl_warehouse_resolves_from_employee(self):
		tree = SimpleNamespace(name="T1", employee="EMP-1")
		with patch.object(tse.frappe.db, "get_value", return_value="EMP-1 RM"):
			self.assertEqual(tse._resolve_msl_warehouse(tree), "EMP-1 RM")

	def test_msl_warehouse_missing_throws(self):
		tree = SimpleNamespace(name="T1", employee="EMP-1")
		with patch.object(tse.frappe.db, "get_value", return_value=None):
			with self.assertRaises(ValidationError):
				tse._resolve_msl_warehouse(tree)

	def test_msl_warehouse_no_employee_throws(self):
		tree = SimpleNamespace(name="T1", employee=None)
		with self.assertRaises(ValidationError):
			tse._resolve_msl_warehouse(tree)

	def test_scrap_warehouse_unique(self):
		with patch.object(
			tse.frappe.db, "get_all", return_value=[SimpleNamespace(name="SCRAP-1")]
		):
			self.assertEqual(tse._resolve_scrap_warehouse("DEPT"), "SCRAP-1")

	def test_scrap_warehouse_none_throws(self):
		with patch.object(tse.frappe.db, "get_all", return_value=[]):
			with self.assertRaises(ValidationError):
				tse._resolve_scrap_warehouse("DEPT")

	def test_scrap_warehouse_multiple_throws(self):
		dupes = [SimpleNamespace(name="SCRAP-1"), SimpleNamespace(name="SCRAP-2")]
		with patch.object(tse.frappe.db, "get_all", return_value=dupes):
			with self.assertRaises(ValidationError):
				tse._resolve_scrap_warehouse("DEPT")

	def test_source_warehouse_resolves_to_dept_rm(self):
		# Default Issue source = the department Raw Material warehouse (not Manufacturing).
		tree = SimpleNamespace(name="T1", department="DEPT", get=lambda k, d=None: None)
		with patch.object(tse, "_get_department_rm_warehouse", return_value="DEPT-RM"):
			self.assertEqual(tse._resolve_source_warehouse(tree), "DEPT-RM")

	def test_source_warehouse_explicit_arg_wins(self):
		# An explicit arg short-circuits before any tree/dept resolution.
		tree = SimpleNamespace(
			name="T1", department="DEPT", get=lambda k, d=None: "TREE-SRC"
		)
		self.assertEqual(
			tse._resolve_source_warehouse(tree, "EXPLICIT-WH"), "EXPLICIT-WH"
		)

	def test_source_warehouse_tree_value_wins_over_dept(self):
		# A source already stored on the tree wins over the dept-RM fallback.
		tree = SimpleNamespace(
			name="T1", department="DEPT", get=lambda k, d=None: "TREE-SRC"
		)
		self.assertEqual(tse._resolve_source_warehouse(tree), "TREE-SRC")


# ---------------------------------------------------------------------------
# Loss -> ML variant resolution
# ---------------------------------------------------------------------------
class TestLossItemResolution(IntegrationTestCase):
	def test_resolve_loss_item_missing_variant_of_throws(self):
		tree = SimpleNamespace(name="T1", company="_Test Company")
		with patch.object(tse.frappe.db, "get_value", return_value=None):
			with self.assertRaises(ValidationError):
				tse._resolve_tree_loss_item(tree, "GOLD-18KT")

	def test_resolve_loss_item_delegates_to_get_item_loss_item(self):
		tree = SimpleNamespace(name="T1", company="_Test Company")
		with (
			patch.object(tse.frappe.db, "get_value", return_value="M"),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip.get_item_loss_item",
				return_value="GOLD-18KT-ML",
			) as m,
		):
			out = tse._resolve_tree_loss_item(tree, "GOLD-18KT")
		self.assertEqual(out, "GOLD-18KT-ML")
		# variant_of derived from the Item; loss_type defaults to "Loss".
		m.assert_called_once_with("_Test Company", "GOLD-18KT", "M", "Loss")


# ---------------------------------------------------------------------------
# Submit-to-lock (manual finalize)
# ---------------------------------------------------------------------------
class TestSubmitAndLock(IntegrationTestCase):
	def _received_tree(self):
		# Fully reconciled: issue 10 = receive 8 + loss 2, pending 0 -> _tree_status == "Received".
		return _new_tree(
			material_details=[
				{
					"item_code": "GOLD-18KT",
					"issue_qty": 10.0,
					"receive_qty": 8.0,
					"loss_qty": 2.0,
					"pending_qty": 0.0,
				}
			]
		)

	def test_submit_tree_sets_submitted(self):
		# Fully received (pending 0): nothing to write off, so receive_material is never called.
		tree = self._received_tree()
		with (
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(tse, "receive_material") as mock_recv,
		):
			tree.submit_tree()
		mock_recv.assert_not_called()
		self.assertEqual(tree.status, "Submitted")
		tree.save.assert_called_once()

	def test_submit_tree_writes_off_partial_then_locks(self):
		# Partially Received (pending 7): submit books the leftover as loss via receive_material and
		# then locks the tree at "Submitted".
		tree = _new_tree(
			material_details=[
				{
					"item_code": "GOLD-18KT",
					"issue_qty": 10.0,
					"receive_qty": 3.0,
					"loss_qty": 0.0,
					"pending_qty": 7.0,
				}
			]
		)
		with (
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(tse, "receive_material") as mock_recv,
		):
			tree.submit_tree()
		mock_recv.assert_called_once()
		called_tree, called_rows = mock_recv.call_args[0]
		self.assertIs(called_tree, tree)
		self.assertEqual(called_rows, [{"item_code": "GOLD-18KT", "loss_qty": 7.0}])
		self.assertEqual(tree.status, "Submitted")

	def test_submit_tree_rejects_when_never_received(self):
		# No receive activity at all -> _tree_status == "Issued" -> cannot submit (receive/reverse first).
		tree = _new_tree(
			material_details=[
				{
					"item_code": "GOLD-18KT",
					"issue_qty": 10.0,
					"receive_qty": 0.0,
					"loss_qty": 0.0,
					"pending_qty": 10.0,
				}
			]
		)
		with patch.object(frappe, "has_permission", return_value=True):
			with self.assertRaises(ValidationError):
				tree.submit_tree()

	def test_submitted_tree_blocks_issue(self):
		tree = self._received_tree()
		tree.status = "Submitted"
		with self.assertRaises(ValidationError):
			_run(tse.issue_material, tree, "GOLD-18KT", 1.0)

	def test_submitted_tree_blocks_receive(self):
		tree = self._received_tree()
		tree.status = "Submitted"
		with self.assertRaises(ValidationError):
			_run(
				tse.receive_material,
				tree,
				[{"item_code": "GOLD-18KT", "receive_qty": 1.0}],
			)


# ---------------------------------------------------------------------------
# Batch provenance: Receive returns the batches the Issue put in
# ---------------------------------------------------------------------------
class TestTreeOwedBatches(IntegrationTestCase):
	"""``_tree_owed_batches`` — netting the tree's OWN Stock Entries, capped at physical stock.

	Regression origin: on GEPL-TR-26-00150 the Issue moved 6g of a Customer-Goods batch into the
	employee's shared MSL warehouse and the Receive handed back 6g of a *company* batch that
	merely happened to be one day older (blind warehouse-wide FIFO). Netting states the defect as
	an invariant: a batch with ``taken_out > issued_in`` is metal the tree never received.
	"""

	MSL = "EMP-MSL"

	def _call(self, rows, available, item_code="GOLD-18KT"):
		"""Drive the allocator over faked SED rows + faked physical availability."""
		db = MagicMock()
		db.sql.return_value = rows
		se = SimpleNamespace(posting_date="2026-07-17", posting_time="10:00:00")
		with (
			patch.object(tse, "frappe", MagicMock(db=db, _dict=frappe._dict)),
			patch.object(tse, "_ensure_posting_datetime"),
			patch.object(
				tse,
				"capped_auto_batch_nos",
				return_value=[frappe._dict(b) for b in available],
			),
			patch.object(tse, "_pending_eps", return_value=0.0005),
		):
			return tse._tree_owed_batches(
				se, SimpleNamespace(name="TREE-1"), item_code, self.MSL
			)

	def _in(self, batch, qty):
		return frappe._dict(
			batch_no=batch, s_warehouse="SRC", t_warehouse=self.MSL, qty=qty
		)

	def _out(self, batch, qty, t_warehouse="DEPT-RM"):
		return frappe._dict(
			batch_no=batch, s_warehouse=self.MSL, t_warehouse=t_warehouse, qty=qty
		)

	def test_issued_batch_is_owed_back(self):
		out = self._call(
			[self._in("B-CUST", 6.0)], [{"batch_no": "B-CUST", "qty": 10.226}]
		)
		# Capped at what the tree issued (6.0), not at everything of that batch sitting in MSL.
		self.assertEqual(out, [("B-CUST", 6.0)])

	def test_prior_receive_and_loss_net_out(self):
		rows = [
			self._in("B1", 6.0),
			self._out("B1", 2.0),  # earlier receive leg
			self._out("B1", 0.5, t_warehouse=None),  # earlier loss leg's consume row
		]
		out = self._call(rows, [{"batch_no": "B1", "qty": 99.0}])
		self.assertEqual(out, [("B1", 3.5)])

	def test_fully_returned_batch_drops_out(self):
		out = self._call(
			[self._in("B1", 6.0), self._out("B1", 6.0)],
			[{"batch_no": "B1", "qty": 99.0}],
		)
		self.assertEqual(out, [])

	def test_capped_at_physical_when_drawn_out_untagged(self):
		"""A casting EIR injection drains the tree's own batch without stamping custom_tree_number.

		Netting alone would still claim 6.0; the physical cap is what keeps it honest.
		"""
		out = self._call([self._in("B1", 6.0)], [{"batch_no": "B1", "qty": 1.25}])
		self.assertEqual(out, [("B1", 1.25)])

	def test_batch_gone_from_msl_yields_empty_pool(self):
		self.assertEqual(self._call([self._in("B1", 6.0)], []), [])

	def test_multi_batch_issue_keeps_availability_order(self):
		"""capped_auto_batch_nos orders by (Batch.creation, batch_no); the pool must not reorder."""
		out = self._call(
			[self._in("B-NEW", 4.0), self._in("B-OLD", 3.0)],
			[{"batch_no": "B-OLD", "qty": 50.0}, {"batch_no": "B-NEW", "qty": 50.0}],
		)
		self.assertEqual(out, [("B-OLD", 3.0), ("B-NEW", 4.0)])

	def test_query_filters_to_submitted_rows_of_this_tree(self):
		"""Cancelled SEs must not count — cancel_tree_stock_entries leaves their SED rows intact."""
		db = MagicMock()
		db.sql.return_value = []
		se = SimpleNamespace(posting_date="2026-07-17", posting_time="10:00:00")
		with (
			patch.object(tse, "frappe", MagicMock(db=db, _dict=frappe._dict)),
			patch.object(tse, "_ensure_posting_datetime"),
			patch.object(tse, "_pending_eps", return_value=0.0005),
		):
			tse._tree_owed_batches(
				se, SimpleNamespace(name="TREE-1"), "GOLD-18KT", self.MSL
			)
		query, params = db.sql.call_args[0][0], db.sql.call_args[0][1]
		self.assertIn("se.docstatus = 1", query)
		self.assertIn("se.custom_tree_number = %(tree)s", query)
		self.assertEqual(
			params, {"tree": "TREE-1", "item_code": "GOLD-18KT", "msl": self.MSL}
		)

	def test_sub_eps_owed_dust_is_dropped(self):
		out = self._call(
			[self._in("B1", 6.0), self._out("B1", 5.9999)],
			[{"batch_no": "B1", "qty": 99.0}],
		)
		self.assertEqual(out, [])


class TestAllocateTreeBatches(IntegrationTestCase):
	def _alloc(self, pool, need):
		with (
			patch.object(tse, "_tree_owed_batches", return_value=pool),
			patch.object(tse, "_se_precision", return_value=3),
			patch.object(tse, "_pending_eps", return_value=0.0005),
		):
			return tse._allocate_tree_batches(
				SimpleNamespace(),
				SimpleNamespace(name="TREE-1"),
				"GOLD-18KT",
				"EMP-MSL",
				need,
			)

	def test_allocates_in_pool_order_across_batches(self):
		self.assertEqual(
			self._alloc([("B-OLD", 2.0), ("B-NEW", 5.0)], 6.0),
			[("B-OLD", 2.0), ("B-NEW", 4.0)],
		)

	def test_stops_once_need_is_covered(self):
		self.assertEqual(self._alloc([("B1", 10.0), ("B2", 5.0)], 3.0), [("B1", 3.0)])

	def test_shortfall_throws_rather_than_returning_a_foreign_batch(self):
		with self.assertRaises(ValidationError) as cm:
			self._alloc([("B1", 2.0)], 5.9)
		msg = str(cm.exception)
		self.assertIn("need 5.9", msg)
		self.assertIn("only 2.0", msg)
		self.assertIn("B1: 2.0", msg)

	def test_empty_pool_throws_naming_none(self):
		with self.assertRaises(ValidationError) as cm:
			self._alloc([], 1.0)
		self.assertIn("none", str(cm.exception))


class TestReceiveBatchParity(IntegrationTestCase):
	"""End-to-end through ``receive_material``: the rows the buttons actually build."""

	def _tree(self, issue=6.0):
		return _new_tree(
			material_details=[
				{
					"item_code": "GOLD-18KT",
					"issue_qty": issue,
					"receive_qty": 0,
					"loss_qty": 0,
					"pending_qty": issue,
				}
			]
		)

	def test_receive_returns_the_issued_batch_with_its_ownership(self):
		"""The reported bug, pinned: an older foreign batch in MSL must not be picked."""
		fakes = _run(
			tse.receive_material,
			self._tree(),
			[{"item_code": "GOLD-18KT", "receive_qty": 5.9, "loss_qty": 0.1}],
			owed=[("B-CUST", 6.0)],
			ownership={"B-CUST": ("Customer Goods", "MHCU0012")},
		)
		se_recv, se_loss = fakes
		received = se_recv.items[0]
		self.assertEqual(received.batch_no, "B-CUST")
		self.assertEqual(received.inventory_type, "Customer Goods")
		self.assertEqual(received.customer, "MHCU0012")
		self.assertEqual(received.qty, 5.9)

		# The loss leg consumes the same batch — it must not write off company metal either.
		consume, produce = se_loss.items
		self.assertEqual(consume.batch_no, "B-CUST")
		self.assertEqual(consume.inventory_type, "Customer Goods")
		self.assertEqual(consume.qty, 0.1)
		# Produce row mints a fresh ML batch on submit, so it stays unstamped.
		self.assertFalse(hasattr(produce, "batch_no"))

	def test_both_legs_share_one_pool_and_never_double_book(self):
		"""6.0 owed, 5.9 received + 0.1 lost: the loss leg draws the remainder, not a second 6.0."""
		fakes = _run(
			tse.receive_material,
			self._tree(),
			[{"item_code": "GOLD-18KT", "receive_qty": 5.9, "loss_qty": 0.1}],
			owed=[("B1", 6.0)],
		)
		booked = sum(i.qty for i in fakes[0].items) + fakes[1].items[0].qty
		self.assertEqual(booked, 6.0)

	def test_allocation_splits_across_batches_and_spans_both_legs(self):
		"""One batch runs out mid-receive; the next covers the rest and then the loss."""
		fakes = _run(
			tse.receive_material,
			self._tree(10.0),
			[{"item_code": "GOLD-18KT", "receive_qty": 6.0, "loss_qty": 1.0}],
			owed=[("B1", 4.0), ("B2", 5.0)],
		)
		se_recv, se_loss = fakes
		self.assertEqual(
			[(i.batch_no, i.qty) for i in se_recv.items], [("B1", 4.0), ("B2", 2.0)]
		)
		# Loss consumes what is left of B2 only — B1 was exhausted by the receive leg.
		self.assertEqual(
			[(i.batch_no, i.qty) for i in se_loss.items if i.s_warehouse], [("B2", 1.0)]
		)

	def test_loss_only_receive_uses_the_issued_batch(self):
		fakes = _run(
			tse.receive_material,
			self._tree(),
			[{"item_code": "GOLD-18KT", "loss_qty": 0.5}],
			owed=[("B-CUST", 6.0)],
			ownership={"B-CUST": ("Customer Goods", "MHCU0012")},
		)
		self.assertEqual(len(fakes), 1)
		consume = fakes[0].items[0]
		self.assertEqual((consume.batch_no, consume.qty), ("B-CUST", 0.5))
		self.assertEqual(consume.customer, "MHCU0012")

	def test_shortfall_throws_before_anything_is_submitted(self):
		fakes = _RunResult()
		with self.assertRaises(ValidationError):
			fakes = _run(
				tse.receive_material,
				self._tree(),
				[{"item_code": "GOLD-18KT", "receive_qty": 5.9}],
				owed=[("B1", 2.0)],
			)
		self.assertFalse([f for f in fakes if f.submitted])

	def test_multi_item_tree_allocates_per_item(self):
		tree = _new_tree(
			material_details=[
				{
					"item_code": "GOLD-18KT",
					"issue_qty": 5.0,
					"receive_qty": 0,
					"loss_qty": 0,
					"pending_qty": 5.0,
				},
				{
					"item_code": "GOLD-22KT",
					"issue_qty": 3.0,
					"receive_qty": 0,
					"loss_qty": 0,
					"pending_qty": 3.0,
				},
			]
		)
		fakes = _run(
			tse.receive_material,
			tree,
			[
				{"item_code": "GOLD-18KT", "receive_qty": 5.0},
				{"item_code": "GOLD-22KT", "receive_qty": 3.0},
			],
			owed={"GOLD-18KT": [("B-18", 5.0)], "GOLD-22KT": [("B-22", 3.0)]},
		)
		self.assertEqual(
			[(i.item_code, i.batch_no, i.qty) for i in fakes[0].items],
			[("GOLD-18KT", "B-18", 5.0), ("GOLD-22KT", "B-22", 3.0)],
		)
