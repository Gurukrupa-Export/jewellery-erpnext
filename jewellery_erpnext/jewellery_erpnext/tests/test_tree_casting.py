# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Unit coverage for the casting-tree layer on Employee IR / Tree Number.

These exercise the pure decision logic without standing up a full casting
scenario (BOM -> Manufacturing Plan -> PMO -> MWO -> MOP -> EIR):

  * tree weight / flask arithmetic (tree_utils) — the formulas mirrored from
	Main Slip, must match it exactly.
  * Tree Number status machine (_tree_status): Issued -> Partially Received ->
	Received as the Material Details ledger fills.
  * validate_casting_tree guards: all-same-metal on one tree, and the
	atomic-issue rule (a single MWO cannot be issued onto an existing active
	tree).

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

	def test_atomic_issue_blocks_mwo_on_active_tree(self):
		mwos = {
			"MWO-A": _FakeMWO(
				"MWO-A",
				metal_type="Gold",
				metal_touch="18KT",
				metal_purity="75",
				metal_colour="Y",
			),
		}
		with self.assertRaises(ValidationError):
			self._run(
				_eir(["MWO-A"]),
				mwos,
				tree_links={"MWO-A": "2026-01-01-0001"},
				tree_status={"2026-01-01-0001": "Issued"},
			)

	def test_received_tree_allows_reissue(self):
		mwos = {
			"MWO-A": _FakeMWO(
				"MWO-A",
				metal_type="Gold",
				metal_touch="18KT",
				metal_purity="75",
				metal_colour="Y",
			),
		}
		# Prior tree fully Received -> MWO may join a fresh tree.
		self._run(
			_eir(["MWO-A"]),
			mwos,
			tree_links={"MWO-A": "2026-01-01-0001"},
			tree_status={"2026-01-01-0001": "Received"},
		)

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
