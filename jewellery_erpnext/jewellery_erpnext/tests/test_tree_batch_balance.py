# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""The batch-wise view of a tree's metal: the Tree Number form section and the shortfall error.

Shaped on "Casting KGJPL - 00294 WH - KGJPL" as of 2026-10-26: four open trees owed 1016.49 g
between them while the warehouse held 1009.49 g, all of it batch KG2F104. TR-26-00356 drew
24.669 g of KG2F103, a batch it never issued. The view has to show both: per batch what the tree
moved, and the 7.0 g the warehouse is short for all of them. Pure; for CI.
"""

import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import frappe
from frappe.exceptions import ValidationError

from jewellery_erpnext import tree_batch_reconciliation
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_batch_balance as tbb,
)
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_material_balance as tree_balance,
)
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import tree_number
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.doc_events import (
	tree_stock_entry as tse,
)

MSL = "Casting KGJPL - 00294 WH - KGJPL"
METAL = "M-G-22KT-91.75-Y"
OLD = "KG2F103-MGL229175Y0-2U74X"
NEW = "KG2F104-MGL229175Y0-D64D7"
REG = ("Regular Stock", None)
CUST = ("Customer Goods", "CUST-1")

OPEN_AT_00294 = [
	("KGJPL-TR-26-00353", 46.94),
	("KGJPL-TR-26-00354", 250.41),
	("KGJPL-TR-26-00356", 298.19),
	("KGJPL-TR-26-00358", 420.95),
]


def _tree(
	name="KGJPL-TR-26-00358", pending=420.95, status="Partially Received", **extra
):
	return frappe._dict(
		name=name,
		status=status,
		msl_warehouse=MSL,
		material_details=[frappe._dict(item_code=METAL, pending_qty=pending)],
		**extra,
	)


def _open_row(tree, pending):
	return frappe._dict(
		tree=frappe._dict(name=tree), md=frappe._dict(pending_qty=pending)
	)


@contextmanager
def _world(
	moves=None, held=None, owners=None, open_trees=OPEN_AT_00294, entries=("SE-1",)
):
	"""Stand in for the reads: the tree's entries, their batch sums, the warehouse's batch
	ledger, batch ownership and the open trees. Yields the call log."""
	moves = moves or {}
	held = held or {}
	owners = owners or {}
	calls = {"get_all": [], "sql": []}
	# flt(x, precision) reads System Settings' rounding method through the db and swallows any
	# error into 0 -- so every read other than these must still reach the real one.
	real_sql, real_get_all = frappe.db.sql, frappe.get_all

	def fake_get_all(doctype, *args, **kwargs):
		if doctype != "Stock Entry":
			return real_get_all(doctype, *args, **kwargs)
		calls["get_all"].append(kwargs.get("filters"))
		return list(entries)

	def fake_sql(query, values=None, *args, **kwargs):
		if "`tabStock Entry Detail`" not in query:
			return real_sql(query, values, *args, **kwargs)
		calls["sql"].append(values)
		return [
			frappe._dict(batch_no=b, issued=i, taken_out=o)
			for b, (i, o) in moves.items()
		]

	def priority(batch_nos, with_no_wastage=False):
		return {
			b: frappe._dict(
				inventory_type=owners.get(b, REG)[0],
				customer=owners.get(b, REG)[1],
				creation="",
				no_wastage=False,
			)
			for b in batch_nos or []
		}

	with patch.object(tree_balance, "qty_precision", return_value=3), patch.object(
		frappe, "get_all", side_effect=fake_get_all
	), patch.object(frappe.db, "sql", side_effect=fake_sql), patch.object(
		tse,
		"_warehouse_batch_balances",
		return_value=[frappe._dict(batch_no=b, qty=q) for b, q in held.items()],
	), patch.object(tbb, "batch_priority_map", side_effect=priority), patch.object(
		tse, "batch_priority_map", side_effect=priority
	), patch.object(
		tree_batch_reconciliation,
		"_open_rows",
		return_value=[_open_row(t, p) for t, p in open_trees],
	):
		yield calls


def _rows(balance):
	return {
		r.batch_no: (r.owner, r.issued, r.taken_out, r.in_warehouse, r.usable)
		for r in balance.rows
	}


class TestTreeBatchMoves(unittest.TestCase):
	def test_reads_the_trees_entries_then_sums_each_batch_both_ways(self):
		with _world(moves={NEW: (411.864, 0.0), OLD: (220.946, 211.86)}) as calls:
			moves = tbb.tree_batch_moves("KGJPL-TR-26-00358", METAL, MSL)
		self.assertEqual(moves, {NEW: (411.864, 0.0), OLD: (220.946, 211.86)})
		self.assertEqual(
			calls["get_all"],
			[{"custom_tree_number": "KGJPL-TR-26-00358", "docstatus": 1}],
		)
		(values,) = calls["sql"]
		self.assertEqual(values, {"names": ["SE-1"], "item_code": METAL, "msl": MSL})

	def test_a_tree_with_no_entries_skips_the_detail_query(self):
		with _world(entries=()) as calls:
			self.assertEqual(tbb.tree_batch_moves("KGJPL-TR-26-00358", METAL, MSL), {})
		self.assertEqual(calls["sql"], [])

	def test_an_unsaved_tree_reads_nothing(self):
		"""A None filter would match every entry not tagged to any tree."""
		with _world() as calls:
			self.assertEqual(tbb.tree_batch_moves(None, METAL, MSL), {})
		self.assertEqual(calls["get_all"], [])


class TestBatchBalance(unittest.TestCase):
	def test_tree_00358_shows_its_batches_and_the_warehouse_gap(self):
		with _world(
			moves={NEW: (411.864, 0.0), OLD: (220.946, 211.86)},
			held={NEW: 1009.49},
		):
			b = tbb.batch_balance(_tree(), METAL, MSL)
		self.assertEqual(
			_rows(b),
			{
				OLD: ("Regular Stock", 220.946, 211.86, 0.0, 0.0),
				NEW: ("Regular Stock", 411.864, 0.0, 1009.49, 1009.49),
			},
		)
		self.assertEqual(
			(b.issued, b.taken_out, b.net, b.pending), (632.81, 211.86, 420.95, 420.95)
		)
		self.assertFalse(b.mismatch)
		self.assertEqual((b.in_warehouse, b.owed, b.gap), (1009.49, 1016.49, 7.0))
		self.assertEqual(b.open_trees, OPEN_AT_00294)

	def test_a_batch_drawn_without_being_issued_is_shown_not_netted_away(self):
		"""TR-26-00356 issued only KG2F104 but its Work Order draws took KG2F103 too."""
		with _world(
			moves={OLD: (0.0, 24.669), NEW: (587.16, 264.301)},
			held={NEW: 1009.49},
		):
			b = tbb.batch_balance(_tree("KGJPL-TR-26-00356", 298.19), METAL, MSL)
		self.assertEqual(_rows(b)[OLD], ("Regular Stock", 0.0, 24.669, 0.0, 0.0))
		self.assertEqual((b.net, b.pending), (298.19, 298.19))
		self.assertFalse(b.mismatch)

	def test_another_owners_batch_is_listed_but_not_usable(self):
		with _world(
			moves={NEW: (411.864, 0.0)},
			held={NEW: 1009.49, "B-CUST": 50.0},
			owners={"B-CUST": CUST},
		):
			b = tbb.batch_balance(_tree(pending=411.864), METAL, MSL)
		self.assertEqual(
			_rows(b)["B-CUST"], ("Customer Goods / CUST-1", 0.0, 0.0, 50.0, 0.0)
		)
		self.assertEqual((b.in_warehouse, b.usable), (1059.49, 1009.49))

	def test_usable_is_what_the_allocation_offered_when_given(self):
		"""The error passes the allocator's pool; a pool batch the ledger read misses still shows."""
		with _world(moves={NEW: (212.16, 0.0)}, held={NEW: 205.16}):
			b = tbb.batch_balance(
				_tree("KGJPL-TR-26-00361", 212.16),
				METAL,
				MSL,
				usable={NEW: 200.0, "B-POOL": 5.16},
			)
		self.assertEqual(_rows(b)[NEW][4], 200.0)
		self.assertEqual(_rows(b)["B-POOL"], ("Regular Stock", 0.0, 0.0, 0.0, 5.16))
		self.assertEqual(b.usable, 205.16)

	def test_empty_and_negative_ledger_balances_are_left_out(self):
		with _world(
			moves={NEW: (10.0, 0.0)}, held={NEW: 10.0, "B-ZERO": 0.0, "B-NEG": -1.0}
		):
			b = tbb.batch_balance(_tree(pending=10.0), METAL, MSL)
		self.assertEqual(set(_rows(b)), {NEW})

	def test_metal_that_left_untagged_is_marked(self):
		"""An older tree: its Work Order draws carried no tree tag, so its columns stop short."""
		with _world(moves={NEW: (580.57, 376.01)}, held={}):
			b = tbb.batch_balance(_tree(pending=0.0, status="Submitted"), METAL, MSL)
		self.assertTrue(b.mismatch)
		self.assertEqual(b.net, 204.56)

	def test_no_gap_when_the_warehouse_covers_every_open_tree(self):
		with _world(
			moves={NEW: (10.0, 0.0)}, held={NEW: 30.0}, open_trees=[("T-1", 10.0)]
		):
			b = tbb.batch_balance(_tree("T-1", 10.0), METAL, MSL)
		self.assertEqual(b.gap, -20.0)


class TestRenderHtml(unittest.TestCase):
	def _html(self, **world):
		tree = world.pop("tree", _tree())
		with _world(**world):
			return tbb.render_html(tbb.batch_balance(tree, METAL, MSL))

	def test_a_short_warehouse_names_the_gap_and_every_open_tree(self):
		html = self._html(moves={NEW: (411.864, 0.0)}, held={NEW: 1009.49})
		self.assertIn("Short by 7.0", html)
		self.assertIn("indicator-pill red", html)
		self.assertIn("KGJPL-TR-26-00353: 46.94", html)
		# The tree being looked at is picked out among the others.
		self.assertIn("<b>KGJPL-TR-26-00358</b>: 420.95", html)
		self.assertNotIn("Enough for all open trees", html)

	def test_a_covered_warehouse_says_so(self):
		html = self._html(
			moves={NEW: (10.0, 0.0)}, held={NEW: 30.0}, open_trees=[("T-1", 10.0)]
		)
		self.assertIn("Enough for all open trees", html)
		self.assertNotIn("Short by", html)

	def test_untagged_metal_gets_a_note_not_an_alarm(self):
		html = self._html(
			tree=_tree(pending=0.0, status="Submitted"),
			moves={NEW: (580.57, 376.01)},
		)
		self.assertIn("indicator-pill gray", html)
		self.assertIn("204.56", html)
		self.assertNotIn("indicator-pill orange", html)

	def test_names_are_escaped(self):
		html = self._html(
			moves={"B<1>": (5.0, 0.0)},
			held={"B<1>": 5.0},
			open_trees=[("T<x>", 5.0)],
		)
		self.assertIn("B&lt;1&gt;", html)
		self.assertIn("T&lt;x&gt;", html)
		self.assertNotIn("B<1>", html)
		self.assertNotIn("T<x>", html)


class TestFormSection(unittest.TestCase):
	def _render(self, tree):
		with patch.object(
			tbb, "batch_balance", side_effect=lambda t, item, wh: item
		) as balance, patch.object(
			tbb, "render_html", side_effect=lambda b: f"[{b}]"
		), patch.object(tree_balance, "qty_precision", return_value=3):
			return tbb.render_tree(tree), balance

	def test_only_rows_receive_can_still_settle(self):
		tree = _tree()
		tree.material_details.append(
			frappe._dict(item_code="M-G-22KT-91.75-W", pending_qty=0.0)
		)
		html, balance = self._render(tree)
		self.assertEqual(html, f"<p><b>{METAL}</b></p>[{METAL}]")
		balance.assert_called_once_with(tree, METAL, MSL)

	def test_nothing_for_a_tree_that_cannot_receive(self):
		for tree in (
			_tree(status="Draft"),
			_tree(status="Submitted"),
			frappe._dict(_tree(), msl_warehouse=None),
		):
			html, balance = self._render(tree)
			self.assertEqual(html, "")
			balance.assert_not_called()

	def test_the_endpoint_reads_the_saved_tree_and_checks_read_permission(self):
		saved = MagicMock()
		with patch.object(
			frappe, "get_doc", return_value=saved
		) as get_doc, patch.object(
			tbb, "render_tree", return_value="<table/>"
		) as render:
			self.assertEqual(
				tree_number.get_batch_balance("KGJPL-TR-26-00358"), "<table/>"
			)
		get_doc.assert_called_once_with("Tree Number", "KGJPL-TR-26-00358")
		saved.check_permission.assert_called_once_with("read")
		render.assert_called_once_with(saved)


class TestShortfallError(unittest.TestCase):
	def test_the_error_keeps_its_wording_and_appends_the_batch_table(self):
		pool = [(NEW, 205.16)]
		with patch.object(tse, "_other_ownership_stock", return_value=[]), patch.object(
			tbb, "batch_balance", return_value="BALANCE"
		) as balance, patch.object(
			tbb, "render_html", return_value="<table>BATCH-WISE</table>"
		), patch.object(tree_balance, "qty_precision", return_value=3):
			with self.assertRaises(ValidationError) as ctx:
				tse._throw_tree_shortfall(
					frappe._dict(name="KGJPL-TR-26-00361"),
					METAL,
					MSL,
					212.16,
					7.0,
					pool,
					3,
				)
		msg = str(ctx.exception)
		self.assertTrue(
			msg.startswith(
				f"Tree KGJPL-TR-26-00361, Item {METAL}: cannot return 212.16"
			)
		)
		self.assertIn("short by 7.0", msg)
		self.assertLess(msg.index("No further metal"), msg.index("BATCH-WISE"))
		# "Usable" in the table is exactly what the allocation could offer.
		self.assertEqual(balance.call_args.kwargs, {"usable": {NEW: 205.16}})
