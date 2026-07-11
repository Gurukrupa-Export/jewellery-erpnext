# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Coverage for the button-driven Tree Number material flow (tree_stock_entry).

The documented "Issue Material" / "Receive Material" actions create ledger-invisible
Stock Entries and update the per-item ``material_details`` ledger. Receive posts up to
TWO SEs: a received-metal transfer (``Material Transfer (MAIN SLIP)`` for casting trees,
plain ``Material Transfer`` for standalone) and, when loss is booked, a separate
``Process Loss`` Repack that CONVERTS the metal into its ML loss variant. These tests
exercise the decision logic — warehouse corridors, ledger math, status transitions, the
Submit-to-lock flow, the loss→ML conversion, and the warehouse resolvers — with Stock
Entry persistence mocked, so they stay fast and independent of full master data. The real
SLE/Bin movement is verified manually on the ``gk`` site (see the implementation plan's
Verification section), because ``bench run-tests`` on ``gk`` aborts at the global
``before_tests`` GST hook.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.doc_events import (
	tree_stock_entry as tse,
)


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
):
	"""Call an op helper with persistence + warehouse/loss-item resolution mocked.

	Returns a _RunResult (list of every FakeSE created, in build order — receive posts the
	received transfer first, then the loss Repack); ``.value`` holds the helper's return.
	"""
	fakes = _RunResult()

	def _mint(*_a, **_k):
		fake = _FakeSE(index=len(fakes))
		fakes.append(fake)
		return fake

	with (
		patch.object(tse.frappe, "new_doc", side_effect=_mint),
		patch.object(tse.frappe, "has_permission", return_value=True),
		patch.object(tse.frappe, "get_precision", return_value=3),
		patch.object(tse, "_apply_fifo_batches_to_stock_entry"),
		patch.object(tse, "preallocate_series_for_docs"),
		patch.object(tse, "lock_bins"),
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
