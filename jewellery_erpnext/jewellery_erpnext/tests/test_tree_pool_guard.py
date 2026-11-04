# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""A manual Stock Entry may not take metal open Tree Numbers are still owed out of their MSL.

Shaped on "Casting KGJPL - 00294 WH - KGJPL": MAT-STE-66673 moved 20.86 g out by hand and
MAT-STE-75009 put 40.074 g back, leaving the warehouse 7.0 g short of its open trees until
KGJPL-TR-26-00361 could not Receive. The guard refuses the move out (and the cancel of a move
in) at the point it happens; inflows and every system flow pass untouched. The mocked classes are
pure; ``TestFreshClaimsAcrossTransactions`` uses a second real connection and commits its own
throwaway rows (removed in tearDown).
"""

import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import frappe

from jewellery_erpnext import hooks
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_material_balance as tree_balance,
)
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.doc_events import (
	tree_pool_guard as guard,
)

MSL = "Casting KGJPL - 00294 WH - KGJPL"
RM = "Waxing RM - KGJPL"
METAL = "M-G-22KT-91.75-Y"
INDEX = "msl_warehouse_index"


def _row(item_code=METAL, qty=0.0, s=None, t=None, **extra):
	return frappe._dict(
		item_code=item_code,
		transfer_qty=qty,
		qty=qty,
		s_warehouse=s,
		t_warehouse=t,
		**extra,
	)


def _entry(*rows, name="MAT-STE-TEST", **header):
	return frappe._dict(name=name, items=list(rows), **header)


def _tree(name, pending, status="Partially Received", warehouse=MSL, item_code=METAL):
	return frappe._dict(
		msl_warehouse=warehouse,
		item_code=item_code,
		tree=name,
		pending_qty=pending,
		status=status,
	)


@contextmanager
def _ledger(trees=(), bins=None, index=INDEX, sql_error=None):
	"""Stand in for the two reads: the open-tree SQL (filtered the way the WHERE clause does)
	and the locking Bin read. Every warehouse named "Casting ..." is an MSL. Yields the call log
	so a test can assert what was read."""
	bins = bins or {}
	calls = {"sql": 0, "bin": [], "queries": []}
	# flt(x, precision) reads System Settings' rounding method through the db and swallows any
	# error into 0 -- so every read other than these two must still reach the real one.
	real_sql, real_get_value = frappe.db.sql, frappe.db.get_value

	def fake_sql(query, values=None, *args, **kwargs):
		if "`tabTree Material Detail`" not in query:
			return real_sql(query, values, *args, **kwargs)
		calls["sql"] += 1
		calls["queries"].append(query)
		if sql_error:
			raise sql_error
		return [
			t
			for t in trees
			if t.status in values["statuses"]
			and t.msl_warehouse in values["warehouses"]
			and t.item_code in values["items"]
			and t.pending_qty > values["eps"]
		]

	def fake_get_value(doctype, filters=None, fieldname="name", *args, **kwargs):
		if doctype != "Bin":
			return real_get_value(doctype, filters, fieldname, *args, **kwargs)
		calls["bin"].append(kwargs.get("for_update"))
		return bins.get((filters["item_code"], filters["warehouse"]))

	with patch.object(tree_balance, "qty_precision", return_value=3), patch.object(
		frappe.db, "sql", side_effect=fake_sql
	), patch.object(frappe.db, "get_value", side_effect=fake_get_value), patch.object(
		guard, "is_msl_warehouse", side_effect=lambda wh: wh.startswith("Casting ")
	), patch.object(guard, "_msl_index_name", return_value=index):
		yield calls


class TestManualOutflow(unittest.TestCase):
	def test_refuses_a_move_out_that_dips_below_what_open_trees_are_owed(self):
		# 100 on hand, trees owed 90: 20 out would leave 80.
		trees = [_tree("TR-1", 50.0), _tree("TR-2", 40.0)]
		with _ledger(trees, {(METAL, MSL): 100.0}) as calls:
			with self.assertRaises(frappe.ValidationError) as ctx:
				guard.guard_tree_pool(_entry(_row(qty=20.0, s=MSL, t=RM)))
		msg = str(ctx.exception)
		self.assertIn("TR-1: 50.0", msg)
		self.assertIn("TR-2: 40.0", msg)
		# The most that may still move by hand, and the Bin read with a lock.
		self.assertIn("<strong>10.0</strong>", msg)
		self.assertEqual(calls["bin"], [True])

	def test_allows_a_move_out_of_only_the_unclaimed_surplus(self):
		trees = [_tree("TR-1", 90.0)]
		with _ledger(trees, {(METAL, MSL): 100.0}):
			guard.guard_tree_pool(_entry(_row(qty=10.0, s=MSL, t=RM)))

	def test_an_already_short_warehouse_refuses_even_one_milligram(self):
		# The production state: 1009.49 on hand against 1016.49 owed.
		trees = [_tree("KGJPL-TR-26-00353", 46.94), _tree("KGJPL-TR-26-00358", 969.55)]
		with _ledger(trees, {(METAL, MSL): 1009.49}):
			with self.assertRaises(frappe.ValidationError) as ctx:
				guard.guard_tree_pool(_entry(_row(qty=0.001, s=MSL, t=RM)))
		self.assertIn("<strong>0.0</strong>", str(ctx.exception))

	def test_material_issue_with_no_target_counts_as_a_move_out(self):
		with _ledger([_tree("TR-1", 90.0)], {(METAL, MSL): 100.0}):
			with self.assertRaises(frappe.ValidationError):
				guard.guard_tree_pool(_entry(_row(qty=10.001, s=MSL)))

	def test_qty_falls_back_to_qty_times_conversion_factor(self):
		row = _row(qty=0.0, s=MSL, t=RM)
		row.update(transfer_qty=0, qty=0.02, conversion_factor=1000)  # 20 in stock UOM
		with _ledger([_tree("TR-1", 90.0)], {(METAL, MSL): 100.0}):
			with self.assertRaises(frappe.ValidationError):
				guard.guard_tree_pool(_entry(row))


class TestWhatPassesUntouched(unittest.TestCase):
	def test_an_inflow_is_never_checked(self):
		# The top-up that repairs a short warehouse must always go through. Its source side is
		# still a draw, but from a warehouse no tree claims, so no Bin is ever weighed.
		with _ledger([_tree("TR-1", 1016.49)], {(METAL, MSL): 1009.49}) as calls:
			guard.guard_tree_pool(_entry(_row(qty=7.0, s=RM, t=MSL)))
		self.assertEqual(calls["bin"], [])

	def test_system_entries_are_left_to_their_own_ledgers(self):
		for header in (
			{"auto_created": 1},
			{"custom_tree_number": "TR-1"},
			{"employee_ir": "EMP-IR-1"},
		):
			with self.subTest(**header):
				with _ledger([_tree("TR-1", 90.0)], {(METAL, MSL): 100.0}) as calls:
					guard.guard_tree_pool(_entry(_row(qty=50.0, s=MSL, t=RM), **header))
				self.assertEqual(calls["sql"], 0)

	def test_a_warehouse_with_no_open_tree_is_free(self):
		with _ledger([], {(METAL, MSL): 100.0}) as calls:
			guard.guard_tree_pool(_entry(_row(qty=100.0, s=MSL, t=RM)))
		self.assertEqual(calls["bin"], [])

	def test_closed_trees_and_settled_rows_owe_nothing(self):
		trees = [
			_tree("TR-SUB", 90.0, status="Submitted"),
			_tree("TR-DRAFT", 90.0, status="Draft"),
			_tree("TR-ZERO", 0.0004),
			_tree("TR-OTHER-WH", 90.0, warehouse="Casting KGJPL - 00300 WH - KGJPL"),
			_tree("TR-OTHER-ITEM", 90.0, item_code="M-G-18KT-75.4-Y"),
		]
		with _ledger(trees, {(METAL, MSL): 100.0}):
			guard.guard_tree_pool(_entry(_row(qty=100.0, s=MSL, t=RM)))

	def test_a_move_out_and_back_into_the_same_warehouse_nets_to_nothing(self):
		with _ledger([_tree("TR-1", 100.0)], {(METAL, MSL): 100.0}) as calls:
			guard.guard_tree_pool(
				_entry(_row(qty=5.0, s=MSL, t=RM), _row(qty=5.0, s=RM, t=MSL))
			)
		self.assertEqual(calls["sql"], 0)


class TestClaimsReadPastTheSnapshot(unittest.TestCase):
	"""The claims must be as current as the locked Bin, without ever waiting after it."""

	def test_claims_are_a_nowait_locking_read_on_the_msl_index(self):
		with _ledger([_tree("TR-1", 90.0)], {(METAL, MSL): 100.0}) as calls:
			guard.guard_tree_pool(_entry(_row(qty=10.0, s=MSL, t=RM)))
		(query,) = calls["queries"]
		self.assertIn(f"FORCE INDEX (`{INDEX}`)", query)
		self.assertIn("STRAIGHT_JOIN `tabTree Material Detail`", query)
		self.assertTrue(query.strip().endswith("LOCK IN SHARE MODE NOWAIT"), query)

	def test_without_the_index_it_falls_back_to_the_plain_read(self):
		# Before migrate: a locking read with no index would hold every tree on the site.
		with _ledger([_tree("TR-1", 90.0)], {(METAL, MSL): 100.0}, index=None) as calls:
			guard.guard_tree_pool(_entry(_row(qty=10.0, s=MSL, t=RM)))
		(query,) = calls["queries"]
		self.assertNotIn("FORCE INDEX", query)
		self.assertNotIn("LOCK IN", query)

	def test_a_tree_row_being_written_refuses_as_busy(self):
		for error in (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
			with self.subTest(error=error.__name__):
				with _ledger(sql_error=error("busy")) as calls:
					with self.assertRaises(frappe.ValidationError) as ctx:
						guard.guard_tree_pool(_entry(_row(qty=10.0, s=MSL, t=RM)))
				self.assertIn("being updated by another user", str(ctx.exception))
				self.assertIn(MSL, str(ctx.exception))
				self.assertEqual(calls["bin"], [])

	def test_a_warehouse_that_is_not_an_msl_is_never_read(self):
		# MSL-ness comes from the Warehouse, not from a tree read that might be stale.
		with _ledger(
			[_tree("TR-1", 90.0, warehouse=RM)], {(METAL, RM): 100.0}
		) as calls:
			guard.guard_tree_pool(_entry(_row(qty=100.0, s=RM, t=MSL)))
		self.assertEqual(calls["sql"], 0)


class TestCancel(unittest.TestCase):
	def test_cancelling_a_manual_inflow_below_the_claim_is_refused(self):
		# Cancelling MAT-STE-75009 would take its 40.074 back out of a warehouse already short.
		entry = _entry(_row(qty=40.074, s=RM, t=MSL), name="MAT-STE-75009")
		with _ledger([_tree("TR-1", 1016.49)], {(METAL, MSL): 1009.49}):
			with self.assertRaises(frappe.ValidationError) as ctx:
				guard.guard_tree_pool_on_cancel(entry)
		self.assertIn("Cancelling MAT-STE-75009", str(ctx.exception))

	def test_cancelling_a_manual_outflow_gives_metal_back_and_passes(self):
		entry = _entry(_row(qty=20.86, s=MSL, t=RM), name="MAT-STE-66673")
		with _ledger([_tree("TR-1", 1016.49)], {(METAL, MSL): 1009.49}) as calls:
			guard.guard_tree_pool_on_cancel(entry)
		self.assertEqual(calls["bin"], [])


class TestHookOrder(unittest.TestCase):
	"""The Bin read is only serialised if the guard runs after the prelock on each path."""

	_GUARD = "jewellery_erpnext.jewellery_erpnext.doctype.tree_number.doc_events.tree_pool_guard"
	_SE = "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry"

	def _assert_after(self, event, prelock, fn):
		chain = hooks.doc_events["Stock Entry"][event]
		self.assertIn(f"{self._GUARD}.{fn}", chain)
		self.assertGreater(
			chain.index(f"{self._GUARD}.{fn}"), chain.index(f"{self._SE}.{prelock}")
		)

	def test_submit_guard_runs_after_prelock_bins(self):
		self._assert_after("before_submit", "prelock_bins", "guard_tree_pool")

	def test_cancel_guard_runs_after_prelock_bins_on_cancel(self):
		self._assert_after(
			"before_cancel", "prelock_bins_on_cancel", "guard_tree_pool_on_cancel"
		)


class TestFreshClaimsAcrossTransactions(unittest.TestCase):
	"""Two real connections. A (``frappe.db``) is the manual entry: its REPEATABLE READ snapshot
	is taken first. B is another user's tree Issue, committed after that snapshot. The guard must
	still count B's claim, and must refuse -- not wait -- while B is writing a tree row.

	B commits throwaway Tree Number rows at a made-up warehouse and tearDown deletes them. Only the
	Bin read is stubbed; the claims query runs for real.
	"""

	WH = "TPG Race MSL WH"

	def setUp(self):
		if frappe.db.db_type != "mariadb":
			self.skipTest("MariaDB locking reads")
		if not guard._msl_index_name():
			self.skipTest("Tree Number.msl_warehouse has no index yet (run migrate)")
		self.prefix = f"TPG-RACE-{frappe.generate_hash(length=8)}"
		self.other = frappe.database.get_db(
			socket=frappe.db.socket,
			host=frappe.db.host,
			user=frappe.db.user,
			password=frappe.db.password,
			port=frappe.db.port,
			cur_db_name=frappe.db.cur_db_name,
		)
		self.other.connect()
		self.other.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.rollback()
		# Applies to A's next transaction, which the rollback above has not started yet.
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
		frappe.db.rollback()

	def tearDown(self):
		frappe.db.rollback()
		self.other.rollback()
		self.other.sql(
			"DELETE FROM `tabTree Material Detail` WHERE parent LIKE %s",
			(f"{self.prefix}%",),
		)
		self.other.sql(
			"DELETE FROM `tabTree Number` WHERE name LIKE %s", (f"{self.prefix}%",)
		)
		self.other.commit()
		self.other.close()

	def _commit_tree(self, suffix, pending, status="Issued"):
		"""B: one open tree owing ``pending`` of METAL at WH, committed."""
		name = f"{self.prefix}-{suffix}"
		self.other.sql(
			"""INSERT INTO `tabTree Number` (name, creation, modified, status, msl_warehouse)
			VALUES (%s, NOW(6), NOW(6), %s, %s)""",
			(name, status, self.WH),
		)
		self.other.sql(
			"""INSERT INTO `tabTree Material Detail`
				(name, creation, modified, parent, parenttype, parentfield, idx, item_code, pending_qty)
			VALUES (%s, NOW(6), NOW(6), %s, 'Tree Number', 'material_details', 1, %s, %s)""",
			(f"{name}-M", name, METAL, pending),
		)
		self.other.commit()
		return name

	def _snapshot_claims(self):
		"""A's plain read -- also what pins A's snapshot if nothing has yet."""
		return flt_sum(
			frappe.db.sql(
				"""SELECT tmd.pending_qty FROM `tabTree Material Detail` tmd
				JOIN `tabTree Number` tn ON tn.name = tmd.parent
				WHERE tn.msl_warehouse = %s""",
				self.WH,
			)
		)

	@contextmanager
	def _bin(self, actual):
		real_get_value = frappe.db.get_value

		def fake_get_value(doctype, *args, **kwargs):
			return (
				actual if doctype == "Bin" else real_get_value(doctype, *args, **kwargs)
			)

		with patch.object(
			frappe.db, "get_value", side_effect=fake_get_value
		), patch.object(
			guard, "is_msl_warehouse", side_effect=lambda wh: wh == self.WH
		):
			yield

	def _draw(self, qty):
		guard.guard_tree_pool(
			_entry(_row(qty=qty, s=self.WH, t=RM), name="MAT-STE-RACE")
		)

	def test_an_issue_committed_after_the_snapshot_still_counts(self):
		# Stock 100 owed 90; B issues 10 more (stock 110, owed 100); A then draws 20.
		self._commit_tree("T1", 90.0)
		self.assertEqual(self._snapshot_claims(), 90.0)  # A's snapshot now exists
		t2 = self._commit_tree("T2", 10.0)
		self.assertEqual(
			self._snapshot_claims(), 90.0, "A must still be on its old snapshot"
		)
		with self._bin(110.0):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._draw(20.0)
		self.assertIn(t2, str(ctx.exception))
		self.assertIn("<strong>100.0</strong>", str(ctx.exception))

	def test_no_claims_in_the_snapshot_does_not_skip_the_warehouse(self):
		self.assertEqual(self._snapshot_claims(), 0.0)
		self._commit_tree("T1", 100.0)
		with self._bin(110.0):
			with self.assertRaises(frappe.ValidationError):
				self._draw(20.0)

	def test_the_surplus_still_moves(self):
		self._commit_tree("T1", 90.0)
		self._snapshot_claims()
		self._commit_tree("T2", 10.0)
		with self._bin(110.0):
			self._draw(10.0)

	def test_a_tree_being_written_refuses_at_once_instead_of_waiting(self):
		t1 = self._commit_tree("T1", 90.0)
		self._snapshot_claims()
		self.other.sql(
			"UPDATE `tabTree Material Detail` SET pending_qty = 95 WHERE parent = %s",
			t1,
		)  # B holds the row, uncommitted
		started = time.monotonic()
		with self._bin(110.0):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._draw(5.0)
		self.assertLess(time.monotonic() - started, 5)
		self.assertIn("being updated by another user", str(ctx.exception))


def flt_sum(rows):
	return sum(float(r[0] or 0) for r in rows or ())
