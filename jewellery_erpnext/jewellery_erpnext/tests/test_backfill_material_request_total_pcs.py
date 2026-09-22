# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Pins the Material Request Total Pcs backfill: its scope, its arithmetic, its blast radius.

The patch rewrites a column on every Material Request on a live site -- ~16k rows here and
roughly 22x that on production -- so these tests care less about the happy path than about
what gets touched and what value lands:

* THAT a missing column is a no-op rather than a SQL error.
* THAT the aggregate is scoped by ``parenttype`` AND ``parentfield``. Filtering on ``parent``
  alone would fold in a row from any other child table whose parent happened to share a name.
* THAT the totals come from ``cint`` -- the function the runtime uses -- and so agree with
  what ``update_pure_qty`` will compute on the document's next save.
* THAT only requests already disagreeing with the computed total are written, which is the
  whole of the patch's idempotency.
* THAT ``modified`` is never written. Bumping it would make every Material Request on the
  site look edited, and ``modified`` is what the sync machinery and several reports key off.
* THAT the paged scan terminates.

These run against a fake ``frappe.db``, which keeps them fast and total -- every branch is
reachable, including the ones real data does not currently exercise. They deliberately do NOT
prove that the queries execute, or that MariaDB and Python agree about a given string; a fake
cannot know either. ``test_backfill_material_request_total_pcs_db`` covers that against a real
database, and the two are meant to be read together.

DB-free per the suite convention: ``setUpClass`` is neutralized and ``frappe.db`` is mocked.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import backfill_material_request_total_pcs as backfill


class _Site:
	"""A tiny in-memory site: child rows, parent rows, and the UPDATEs the patch issues.

	``sql`` answers the two scan queries by looking at the query text, so a test that deletes
	the ``parenttype``/``parentfield`` filter from the patch fails here rather than passing
	against a fake that answers everything identically. Paging is honoured, so the scan's
	termination is exercised rather than assumed.
	"""

	def __init__(self, items=None, parents=None, has_column=True):
		# items: list of (name, parent, pcs). parents: dict name -> stored total.
		self.items = list(items or [])
		self.parents = dict(parents or {})
		self.has_column = has_column
		self.queries = []
		self.updates = []
		self.commits = 0

	def sql(self, query, values=None, as_dict=False, **kwargs):
		self.queries.append(query)
		values = values or {}

		if "UPDATE" in query:
			self.updates.append((values["value"], list(values["names"])))
			return []

		after, limit = values.get("after", ""), values.get("limit", 1000)

		if "tabMaterial Request Item" in query:
			rows = [
				{"name": n, "parent": p, "pcs": v}
				for n, p, v in sorted(self.items)
				# The scope the patch must keep; a fake that ignored it would hide its removal.
				if "parenttype = 'Material Request'" in query
				and "parentfield = 'items'" in query
				and n > after
			]
		else:
			rows = [
				{"name": n, "stored": s}
				for n, s in sorted(self.parents.items())
				if n > after
			]

		return [frappe._dict(r) for r in rows[:limit]]

	def has_column_(self, doctype, fieldname):
		return self.has_column

	def commit(self):
		self.commits += 1

	@property
	def written(self):
		"""``{material_request: value}`` actually written."""
		return {n: v for v, names in self.updates for n in names}


def _run(site, fn=None):
	"""Drive ``fn`` (default ``backfill``) against ``site``."""
	fake_db = type(
		"FakeDB",
		(),
		{
			"sql": lambda _s, *a, **k: site.sql(*a, **k),
			"has_column": lambda _s, *a, **k: site.has_column_(*a, **k),
			"commit": lambda _s: site.commit(),
		},
	)()
	with patch.object(backfill.frappe, "db", fake_db):
		return (fn or backfill.backfill)()


class _BackfillTestCase(IntegrationTestCase):
	"""House-pattern base: no DB fixtures; setUpClass is a deliberate no-op."""

	@classmethod
	def setUpClass(cls):
		pass


class TestBackfillScope(_BackfillTestCase):
	"""What the patch is allowed to touch. These decide blast radius on a live site."""

	def test_missing_column_is_a_no_op(self):
		"""The field patch may not have run; the scan would fail on an unknown column."""
		site = _Site(items=[("I1", "MR-1", "5")], parents={"MR-1": 0}, has_column=False)

		self.assertEqual(_run(site), 0)
		self.assertEqual(site.queries, [])
		self.assertEqual(site.updates, [])

	def test_nothing_pending_writes_nothing(self):
		site = _Site(items=[("I1", "MR-1", "5")], parents={"MR-1": 5})

		self.assertEqual(_run(site), 0)
		self.assertEqual(site.updates, [])
		self.assertEqual(site.commits, 0)

	def test_the_item_scan_is_scoped_by_parenttype_and_parentfield(self):
		"""Both are load-bearing, so both are asserted on the query text."""
		site = _Site(items=[("I1", "MR-1", "5")], parents={"MR-1": 0})
		_run(site)

		scan = next(q for q in site.queries if "tabMaterial Request Item" in q)
		self.assertIn("parenttype = 'Material Request'", scan)
		self.assertIn("parentfield = 'items'", scan)

	def test_modified_is_never_written(self):
		"""Bumping modified would make every Material Request look edited."""
		site = _Site(items=[("I1", "MR-1", "5")], parents={"MR-1": 0})
		_run(site)

		for query in site.queries:
			self.assertNotIn("modified", query)

	def test_only_requests_that_disagree_are_written(self):
		"""This IS the idempotency -- a re-run must find nothing to do."""
		site = _Site(
			items=[("I1", "MR-ok", "5"), ("I2", "MR-stale", "5")],
			parents={"MR-ok": 5, "MR-stale": 0},
		)

		self.assertEqual(_run(site), 1)
		self.assertEqual(site.written, {"MR-stale": 5})

	def test_a_second_run_writes_nothing(self):
		site = _Site(items=[("I1", "MR-1", "5")], parents={"MR-1": 0})
		self.assertEqual(_run(site), 1)

		site.parents["MR-1"] = 5
		site.updates.clear()

		self.assertEqual(_run(site), 0)
		self.assertEqual(site.updates, [])


class TestBackfillArithmetic(_BackfillTestCase):
	"""What number lands, including the values SQL aggregation gets wrong."""

	def test_rows_are_summed_per_request(self):
		site = _Site(
			items=[("I1", "MR-1", "1"), ("I2", "MR-1", "32"), ("I3", "MR-2", "4")],
			parents={"MR-1": 0, "MR-2": 0},
		)
		_run(site)

		self.assertEqual(site.written, {"MR-1": 33, "MR-2": 4})

	def test_blank_and_missing_pcs_count_as_zero(self):
		site = _Site(
			items=[("I1", "MR-1", "5"), ("I2", "MR-1", None), ("I3", "MR-1", "")],
			parents={"MR-1": 0},
		)
		_run(site)

		self.assertEqual(site.written, {"MR-1": 5})

	def test_a_request_with_no_rows_wants_zero(self):
		site = _Site(items=[], parents={"MR-1": 7})
		_run(site)

		self.assertEqual(site.written, {"MR-1": 0})

	def test_totals_follow_cint_not_sql_cast(self):
		"""The reason this patch aggregates in Python at all.

		MariaDB ``CAST('12abc' AS SIGNED)`` is 12 and ``CAST('1e3' AS SIGNED)`` is 1, where
		``cint`` gives 0 and 1000. The runtime uses ``cint``, so the backfill must too, or a
		backfilled total silently disagrees with the total the next save computes.
		"""
		site = _Site(
			items=[("I1", "MR-1", "12abc"), ("I2", "MR-1", "1e3"), ("I3", "MR-1", "7")],
			parents={"MR-1": 0},
		)
		_run(site)

		self.assertEqual(site.written, {"MR-1": 1007})
		self.assertNotEqual(
			site.written["MR-1"], 20, msg="regressed to SQL CAST semantics"
		)

	def test_negative_pcs_is_subtracted_not_wrapped(self):
		"""``CAST('-3' AS UNSIGNED)`` wraps to 18446744073709551615; ``cint`` gives -3."""
		site = _Site(
			items=[("I1", "MR-1", "10"), ("I2", "MR-1", "-3")], parents={"MR-1": 0}
		)
		_run(site)

		self.assertEqual(site.written, {"MR-1": 7})


class TestBackfillWrites(_BackfillTestCase):
	"""How the writes are issued, and in how many statements."""

	def test_requests_sharing_a_total_collapse_into_one_update(self):
		"""The row count drives the work, not the statement count."""
		site = _Site(
			items=[(f"I{i}", f"MR-{i}", "1") for i in range(50)],
			parents={f"MR-{i}": 0 for i in range(50)},
		)

		self.assertEqual(_run(site), 50)
		self.assertEqual(len(site.updates), 1)
		self.assertEqual(site.updates[0][0], 1)
		self.assertEqual(len(site.updates[0][1]), 50)

	def test_a_large_group_is_chunked(self):
		"""One IN list per CHUNK_SIZE names, so the statement stays inside max_allowed_packet."""
		count = backfill.CHUNK_SIZE + 10
		site = _Site(
			items=[(f"I{i:07d}", f"MR-{i:07d}", "4") for i in range(count)],
			parents={f"MR-{i:07d}": 0 for i in range(count)},
		)

		self.assertEqual(_run(site), count)
		self.assertEqual(len(site.updates), 2)
		self.assertEqual(
			[len(names) for _v, names in site.updates], [backfill.CHUNK_SIZE, 10]
		)

	def test_commits_per_value_not_once_at_the_end(self):
		"""A single transaction over the whole table would hold locks for its duration."""
		site = _Site(
			items=[("I1", "MR-1", "1"), ("I2", "MR-2", "2"), ("I3", "MR-3", "3")],
			parents={"MR-1": 0, "MR-2": 0, "MR-3": 0},
		)
		_run(site)

		self.assertEqual(site.commits, 3)


class TestScanPaging(_BackfillTestCase):
	"""The paged scan itself."""

	def test_every_row_is_seen_across_pages(self):
		count = backfill.SCAN_SIZE + 25
		site = _Site(
			items=[(f"I{i:07d}", "MR-1", "1") for i in range(count)],
			parents={"MR-1": 0},
		)
		_run(site)

		self.assertEqual(site.written, {"MR-1": count})

	def test_a_cursor_that_does_not_advance_terminates(self):
		"""Guards a hang. A stub returning a fixed page would otherwise spin forever.

		Two rows, not one: the stall is only detectable once a page has come back whose last
		``name`` is no greater than the cursor, so the second page is fetched and yielded
		before the scan gives up. What matters is that it gives up at all -- an earlier
		revision of this test hung the whole suite.
		"""
		frozen = type(
			"FrozenDB",
			(),
			{
				"sql": lambda _s, q, v=None, **k: []
				if "UPDATE" in q
				else [frappe._dict({"name": "SAME", "parent": "MR-1", "pcs": "1"})],
				"has_column": lambda _s, *a, **k: True,
				"commit": lambda _s: None,
			},
		)()

		with patch.object(backfill.frappe, "db", frozen):
			rows = list(backfill._scan(backfill.ITEM_SCAN_QUERY))

		self.assertEqual(len(rows), 2)
