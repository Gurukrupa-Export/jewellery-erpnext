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
from unittest.mock import patch

from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	tree_casting,
)
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import tree_utils

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
