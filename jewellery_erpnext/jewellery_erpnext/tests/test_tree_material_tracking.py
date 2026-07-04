# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Coverage for the button-driven Tree Number material flow (tree_stock_entry).

The documented "Issue Material" / "Receive Material" actions create plain
``Material Transfer`` Stock Entries and update the per-item ``material_details``
ledger. These tests exercise the decision logic — warehouse corridors, ledger
math, status transitions, the standalone-only guard (anti-double-count) and the
warehouse resolvers — with Stock Entry persistence mocked, so they stay fast and
independent of full master data. The real SLE/Bin movement is verified manually on
the ``gk`` site (see the implementation plan's Verification section), because
``bench run-tests`` on ``gk`` aborts at the global ``before_tests`` GST hook.
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

	def __init__(self):
		self.items = []
		self.name = None
		self.flags = SimpleNamespace()
		self.submitted = False

	def append(self, _table, row):
		child = SimpleNamespace(**row)
		self.items.append(child)
		return child

	def get(self, key, default=None):
		return getattr(self, key, default)

	def insert(self, *a, **k):
		self.name = "SE-TREE-TEST-0001"

	def submit(self, *a, **k):
		self.submitted = True


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
):
	"""Call an op helper with persistence + warehouse resolution mocked. Returns the FakeSE."""
	fake = _FakeSE()
	with (
		patch.object(tse.frappe, "new_doc", return_value=fake),
		patch.object(tse.frappe, "has_permission", return_value=True),
		patch.object(tse.frappe, "get_precision", return_value=3),
		patch.object(tse, "_apply_fifo_batches_to_stock_entry"),
		patch.object(tse, "preallocate_series_for_docs"),
		patch.object(tse, "lock_bins"),
		patch.object(tse, "_resolve_source_warehouse", return_value=source_wh),
		patch.object(tse, "_resolve_msl_warehouse", return_value=msl_wh),
		patch.object(tse, "_get_department_rm_warehouse", return_value=rm_wh),
		patch.object(tse, "_resolve_scrap_warehouse", return_value=scrap_wh),
	):
		fn(*args)
	return fake


# ---------------------------------------------------------------------------
# Issue Material
# ---------------------------------------------------------------------------
class TestIssueMaterial(IntegrationTestCase):
	def test_issue_builds_material_transfer_source_to_msl(self):
		tree = _new_tree()
		fake = _run(tse.issue_material, tree, "GOLD-18KT", 5.0)

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

	def test_receive_builds_two_legs(self):
		tree = self._issued_tree()
		fake = _run(
			tse.receive_material,
			tree,
			[{"item_code": "GOLD-18KT", "receive_qty": 6.0, "loss_qty": 1.0}],
		)
		self.assertEqual(fake.stock_entry_type, "Material Transfer")
		self.assertEqual(len(fake.items), 2)
		received = fake.items[0]
		loss = fake.items[1]
		# received leg: MSL -> Dept RM
		self.assertEqual(
			(received.s_warehouse, received.t_warehouse), ("EMP-MSL", "DEPT-RM")
		)
		self.assertEqual(received.qty, 6.0)
		# loss leg: MSL -> Dept Scrap
		self.assertEqual(
			(loss.s_warehouse, loss.t_warehouse), ("EMP-MSL", "DEPT-SCRAP")
		)
		self.assertEqual(loss.qty, 1.0)

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
		fake = _run(
			tse.receive_material,
			tree,
			[{"item_code": "GOLD-18KT", "receive_qty": 0, "loss_qty": 4.0}],
		)
		self.assertEqual(len(fake.items), 1)
		self.assertEqual(
			(fake.items[0].s_warehouse, fake.items[0].t_warehouse),
			("EMP-MSL", "DEPT-SCRAP"),
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
	"""Casting trees now support the buttons: Issue posts a physical SE and owns issue_qty;
	Receive is RECORD-ONLY (no SE — the Employee IR moves the physical metal) and auto-books
	the remaining pending as dust, capped at the issued qty."""

	def test_casting_issue_posts_se_and_increments_issue_qty(self):
		tree = _new_tree(employee_ir="EIR-CASTING-0001")
		fake = _run(tse.issue_material, tree, "GOLD-18KT", 5.0)
		self.assertEqual(fake.stock_entry_type, "Material Transfer")
		self.assertTrue(fake.submitted)
		self.assertEqual(tree.material_details[0].issue_qty, 5.0)

	def test_casting_receive_is_record_only_and_auto_dusts(self):
		tree = _new_tree(
			employee_ir="EIR-CASTING-0001",
			material_details=[
				{
					"item_code": "GOLD-18KT",
					"issue_qty": 10,
					"receive_qty": 0,
					"loss_qty": 0,
					"pending_qty": 10,
				}
			],
		)
		fake = _run(
			tse.receive_material, tree, [{"item_code": "GOLD-18KT", "receive_qty": 6.0}]
		)
		# Record-only: NO Stock Entry is built/submitted for a casting tree.
		self.assertEqual(len(fake.items), 0)
		self.assertFalse(fake.submitted)
		md = tree.material_details[0]
		self.assertEqual(md.receive_qty, 6.0)
		self.assertEqual(md.loss_qty, 4.0)  # dust = pending - receive
		self.assertEqual(md.pending_qty, 0.0)
		self.assertEqual(tree.status, "Received")

	def test_casting_receive_caps_at_issued(self):
		tree = _new_tree(
			employee_ir="EIR-CASTING-0001",
			material_details=[
				{
					"item_code": "GOLD-18KT",
					"issue_qty": 10,
					"receive_qty": 0,
					"loss_qty": 0,
					"pending_qty": 10,
				}
			],
		)
		with self.assertRaises(ValidationError):
			_run(
				tse.receive_material,
				tree,
				[{"item_code": "GOLD-18KT", "receive_qty": 11.0}],
			)

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
