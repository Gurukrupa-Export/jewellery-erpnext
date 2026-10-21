# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Pins the Material Request Total Pcs backfill: its scope, its SQL, and its blast radius.

The patch rewrites a column on every Material Request on a live site -- ~16k rows here and
roughly 22x that on production -- so these tests care less about the happy path than about
the clauses that decide what gets touched and what the written value is:

* THAT a missing column is a no-op rather than a SQL error.
* THAT the aggregate is scoped by ``parenttype`` AND ``parentfield``. Filtering on ``parent``
  alone would fold in a row from any other child table whose parent happened to share a name.
* THAT the cast is ``SIGNED``. MariaDB wraps a negative string cast to UNSIGNED round to
  18446744073709551615, which would write garbage into a user-visible total.
* THAT ``NULLIF(pcs, '')`` is present, so a request with no pcs aggregates to 0 and not to
  whatever an empty-string cast produces.
* THAT the driving query only selects rows already disagreeing with the computed total --
  which is the whole of the patch's idempotency.
* THAT ``modified`` is never written. Bumping it would make every Material Request on the
  site look edited, and ``modified`` is what the sync machinery and several reports key off.

The fake below is deliberately query-text aware. A fake that answered every ``sql`` the same
way would let the entire scope filter be deleted with the suite still green.

DB-free per the suite convention: ``setUpClass`` is neutralized and ``frappe.db`` is mocked.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import backfill_material_request_total_pcs as backfill


class _Site:
	"""A tiny in-memory site: the pending rows the SELECT would return, and the UPDATEs run.

	``pending`` is what the real delta query would select -- requests whose stored total
	already differs from the sum of their rows. Everything else is recorded for assertions.
	"""

	def __init__(self, pending=None, has_column=True):
		self.pending = list(pending or [])
		self.has_column = has_column
		self.queries = []
		self.updates = []
		self.commits = 0

	def sql(self, query, values=None, as_dict=False, **kwargs):
		self.queries.append(query)
		if "UPDATE" in query:
			self.updates.append((values["value"], list(values["names"])))
			return []
		# frappe._dict, not dict: ``sql(as_dict=True)`` returns attribute-accessible rows
		# and the patch reads ``row.name`` / ``row.want``.
		return [frappe._dict(row) for row in self.pending]

	def has_column_(self, doctype, fieldname):
		return self.has_column

	def commit(self):
		self.commits += 1

	# -- helpers the assertions read ------------------------------------------------

	@property
	def select_sql(self):
		return next(q for q in self.queries if "UPDATE" not in q)

	@property
	def updated_names(self):
		return sorted(name for _value, names in self.updates for name in names)


def _run(site):
	"""Drive ``backfill()`` against ``site``, returning what it reports."""
	fake_db = type(
		"FakeDB",
		(),
		{
			"sql": lambda _self, *a, **k: site.sql(*a, **k),
			"has_column": lambda _self, *a, **k: site.has_column_(*a, **k),
			"commit": lambda _self: site.commit(),
		},
	)()
	with patch.object(backfill.frappe, "db", fake_db):
		return backfill.backfill()


def _row(name, want):
	return {"name": name, "want": want}


class _BackfillTestCase(IntegrationTestCase):
	"""House-pattern base: no DB fixtures; setUpClass is a deliberate no-op."""

	@classmethod
	def setUpClass(cls):
		pass


class TestBackfillScope(_BackfillTestCase):
	"""What the patch is allowed to touch. These decide blast radius on a live site."""

	def test_missing_column_is_a_no_op(self):
		"""The field patch may not have run; the query would fail on an unknown column."""
		site = _Site(pending=[_row("MR-1", 5)], has_column=False)

		self.assertEqual(_run(site), 0)
		self.assertEqual(site.queries, [])
		self.assertEqual(site.updates, [])

	def test_nothing_pending_writes_nothing(self):
		site = _Site(pending=[])

		self.assertEqual(_run(site), 0)
		self.assertEqual(site.updates, [])
		self.assertEqual(site.commits, 0)

	def test_the_aggregate_is_scoped_by_parenttype_and_parentfield(self):
		"""Both are load-bearing, so both are asserted on the query text."""
		site = _Site(pending=[_row("MR-1", 5)])
		_run(site)

		self.assertIn("parenttype = 'Material Request'", site.select_sql)
		self.assertIn("parentfield = 'items'", site.select_sql)

	def test_the_cast_is_signed(self):
		"""UNSIGNED would wrap a negative pcs to 18446744073709551615."""
		site = _Site(pending=[_row("MR-1", 5)])
		_run(site)

		self.assertIn("AS SIGNED", site.select_sql)
		self.assertNotIn("UNSIGNED", site.select_sql)

	def test_blank_pcs_is_nulled_before_summing(self):
		"""Without NULLIF, a request with no pcs does not aggregate to a clean 0."""
		site = _Site(pending=[_row("MR-1", 5)])
		_run(site)

		self.assertIn("NULLIF(pcs, '')", site.select_sql)
		self.assertIn("COALESCE", site.select_sql)

	def test_only_rows_that_disagree_are_selected(self):
		"""This clause IS the idempotency -- a re-run must find nothing to do."""
		site = _Site(pending=[_row("MR-1", 5)])
		_run(site)

		self.assertIn("<> COALESCE(agg.total_pcs, 0)", site.select_sql)

	def test_modified_is_never_written(self):
		"""Bumping modified would make every Material Request look edited."""
		site = _Site(pending=[_row("MR-1", 5), _row("MR-2", 7)])
		_run(site)

		for query in site.queries:
			self.assertNotIn("modified", query)


class TestBackfillWrites(_BackfillTestCase):
	"""What actually gets written, and in how many statements."""

	def test_each_request_gets_its_own_total(self):
		site = _Site(pending=[_row("MR-1", 33), _row("MR-2", 7)])

		self.assertEqual(_run(site), 2)
		self.assertEqual(
			dict((n[0], v) for v, n in site.updates), {"MR-1": 33, "MR-2": 7}
		)

	def test_requests_sharing_a_total_collapse_into_one_update(self):
		"""The row count drives the work, not the statement count."""
		site = _Site(pending=[_row(f"MR-{i}", 1) for i in range(50)])

		self.assertEqual(_run(site), 50)
		self.assertEqual(len(site.updates), 1)
		self.assertEqual(site.updates[0][0], 1)
		self.assertEqual(len(site.updates[0][1]), 50)

	def test_a_large_group_is_chunked(self):
		"""One IN list per CHUNK_SIZE names, so the statement stays inside max_allowed_packet."""
		count = backfill.CHUNK_SIZE + 10
		site = _Site(pending=[_row(f"MR-{i}", 4) for i in range(count)])

		self.assertEqual(_run(site), count)
		self.assertEqual(len(site.updates), 2)
		self.assertEqual(
			[len(names) for _value, names in site.updates],
			[backfill.CHUNK_SIZE, 10],
		)
		self.assertEqual(len(set(site.updated_names)), count)

	def test_a_zero_total_is_still_written(self):
		"""A request whose rows were emptied must be corrected back down to 0."""
		site = _Site(pending=[_row("MR-1", 0)])

		self.assertEqual(_run(site), 1)
		self.assertEqual(site.updates, [(0, ["MR-1"])])

	def test_commits_per_value_not_once_at_the_end(self):
		"""A single transaction over the whole table would hold locks for its duration."""
		site = _Site(pending=[_row("MR-1", 1), _row("MR-2", 2), _row("MR-3", 3)])
		_run(site)

		self.assertEqual(site.commits, 3)
