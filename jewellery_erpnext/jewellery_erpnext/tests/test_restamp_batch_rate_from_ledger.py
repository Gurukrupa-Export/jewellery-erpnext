# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""patches/restamp_batch_rate_from_ledger: conversion targets go back to their ledger rate (F26).

The helpers that read the site are patched per test; ``TestRestampQueries`` runs the three SQL
readers against the real tables with names that match nothing, so a wrong column or join fails
here rather than in the remediation run.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import restamp_batch_rate_from_ledger as restamp

LEDGER_22KT_RATE = (5 * 157655.0 + 0.45 * 62.0) / 5.45  # 144,642.733945
BLENDED_22KT_RATE = 157655.0 * 91.75 / 100  # 144,648.4625


def _target(
	name, row_name, rate, item="M-G-22KT-91.75-Y", inventory_type="Regular Stock", **kw
):
	return frappe._dict(
		name=name,
		item=item,
		custom_metal_rate=rate,
		custom_alloy_rate=kw.get("alloy_rate", 0.0),
		custom_inventory_type=inventory_type,
		custom_customer=kw.get("customer"),
		stock_entry=kw.get("stock_entry", "SE-1"),
		row_name=row_name,
	)


class _Site:
	"""The site as the script sees it: targets, ledger rates, pooled rows, mirrors."""

	def __init__(self, targets, ledger, pooled=(), alloy_items=(), mirrors=None):
		self.targets = targets
		self.ledger = ledger  # {batch: rate or None}
		self.pooled = set(pooled)
		self.alloy_items = list(alloy_items)
		self.mirrors = mirrors or {}  # {batch: [Stock Entry Detail names]}
		self.writes = []
		self.committed = False

	def patches(self):
		return (
			patch.object(
				restamp, "_conversion_target_batches", lambda batches=None: self.targets
			),
			patch.object(
				restamp, "_ledger_rate", lambda se, row, batch: self.ledger.get(batch)
			),
			patch.object(restamp, "_pooled_rows", lambda se, cache: self.pooled),
			patch.object(restamp, "_alloy_items", lambda: self.alloy_items),
			patch.object(
				restamp,
				"_mirror_rows",
				lambda change: self.mirrors.get(change["batch"], []),
			),
			patch.object(frappe.db, "set_value", side_effect=self._set_value),
			patch.object(frappe.db, "commit", side_effect=self._commit),
		)

	def _set_value(self, doctype, name, field, value=None, *args, **kwargs):
		self.writes.append((doctype, name, field, value))

	def _commit(self):
		self.committed = True

	def run(self, **kwargs):
		ctx = self.patches()
		for p in ctx:
			p.start()
		try:
			return restamp.execute(**kwargs)
		finally:
			for p in reversed(ctx):
				p.stop()


class TestRestampBatchRate(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _site(self):
		return _Site(
			targets=[
				_target("B-22KT", "ROW-1", BLENDED_22KT_RATE),
				_target("B-OK", "ROW-2", 150000.0),
				_target("B-POOLED", "ROW-3", 99.0),
				_target("B-NO-LEDGER", "ROW-4", 88.0),
				_target(
					"B-CUSTOMER",
					"ROW-5",
					146000.0,
					inventory_type="Customer Goods",
					customer="TNCU0085",
				),
				_target("B-ALLOY", "ROW-6", 0.0, item="M-Genia-221", alloy_rate=70.0),
			],
			ledger={
				"B-22KT": LEDGER_22KT_RATE,
				"B-OK": 150000.0004,
				"B-POOLED": 55.0,
				"B-NO-LEDGER": None,
				"B-CUSTOMER": 5.1193,
				"B-ALLOY": 62.0,
			},
			pooled={"ROW-3"},
			alloy_items=["M-Genia-221"],
			mirrors={"B-22KT": ["SED-1", "SED-2"]},
		)

	def test_the_dry_run_lists_the_blended_batches_and_writes_nothing(self):
		site = self._site()
		result = site.run()

		changes = {c["batch"]: c for c in result["changes"]}
		self.assertEqual(set(changes), {"B-22KT", "B-CUSTOMER", "B-ALLOY"})
		self.assertAlmostEqual(changes["B-22KT"]["before"], 144648.4625, places=6)
		self.assertAlmostEqual(changes["B-22KT"]["after"], 144642.733945, places=6)
		self.assertEqual(changes["B-22KT"]["mirror_rows"], ["SED-1", "SED-2"])
		self.assertEqual(
			{r["batch"] for r in result["review"]}, {"B-POOLED", "B-NO-LEDGER"}
		)
		self.assertEqual(site.writes, [])
		self.assertFalse(site.committed)

	def test_customer_owned_batches_are_flagged(self):
		result = self._site().run()

		changes = {c["batch"]: c for c in result["changes"]}
		self.assertTrue(changes["B-CUSTOMER"]["customer_owned"])
		self.assertFalse(changes["B-22KT"]["customer_owned"])

	def test_an_alloy_batch_is_compared_on_its_alloy_rate(self):
		result = self._site().run()

		change = next(c for c in result["changes"] if c["batch"] == "B-ALLOY")
		self.assertEqual(change["field"], "custom_alloy_rate")
		self.assertEqual((change["before"], change["after"]), (70.0, 62.0))

	def test_the_real_run_writes_the_rate_and_the_mirrors_only(self):
		site = self._site()
		site.run(dry_run=False)

		self.assertIn(
			("Batch", "B-22KT", "custom_metal_rate", LEDGER_22KT_RATE), site.writes
		)
		self.assertIn(
			("Stock Entry Detail", "SED-1", "custom_metal_rate", LEDGER_22KT_RATE),
			site.writes,
		)
		self.assertIn(
			("Stock Entry Detail", "SED-2", "custom_metal_rate", LEDGER_22KT_RATE),
			site.writes,
		)
		self.assertIn(("Batch", "B-ALLOY", "custom_alloy_rate", 62.0), site.writes)
		written = {name for _dt, name, _f, _v in site.writes}
		self.assertFalse(written & {"B-OK", "B-POOLED", "B-NO-LEDGER"})
		self.assertEqual(len(site.writes), 5)
		self.assertTrue(site.committed)


class TestPooledRows(IntegrationTestCase):
	"""A run with two produce rows is ERPNext-pooled; a run with one is the batch's own."""

	@classmethod
	def setUpClass(cls):
		pass

	def _pooled(self, items):
		with patch.object(
			frappe, "get_all", return_value=[frappe._dict(i) for i in items]
		):
			return restamp._pooled_rows("SE-1", {})

	def test_one_output_per_run_is_not_pooled(self):
		items = [
			{"name": "C1", "s_warehouse": "W", "t_warehouse": None},
			{"name": "C2", "s_warehouse": "W", "t_warehouse": None},
			{"name": "P1", "s_warehouse": None, "t_warehouse": "W"},
			{"name": "C3", "s_warehouse": "W", "t_warehouse": None},
			{"name": "P2", "s_warehouse": None, "t_warehouse": "W"},
		]
		self.assertEqual(self._pooled(items), set())

	def test_two_outputs_in_one_run_are_pooled(self):
		items = [
			{"name": "C1", "s_warehouse": "W", "t_warehouse": None},
			{"name": "P1", "s_warehouse": None, "t_warehouse": "W"},
			{"name": "P2", "s_warehouse": None, "t_warehouse": "W"},
			{"name": "C2", "s_warehouse": "W", "t_warehouse": None},
			{"name": "P3", "s_warehouse": None, "t_warehouse": "W"},
		]
		self.assertEqual(self._pooled(items), {"P1", "P2"})


class TestRestampQueries(IntegrationTestCase):
	"""The SQL readers, against the real tables, matching nothing. Read-only."""

	def test_the_target_query_runs(self):
		self.assertEqual(
			restamp._conversion_target_batches(["__F26_NO_SUCH_BATCH__"]), []
		)

	def test_the_ledger_query_runs(self):
		self.assertIsNone(
			restamp._ledger_rate("__F26_NO_SE__", "__F26_NO_ROW__", "__F26_NO_BATCH__")
		)

	def test_the_mirror_query_runs(self):
		change = {
			"batch": "__F26_NO_SUCH_BATCH__",
			"field": "custom_metal_rate",
			"before": 1.0,
		}
		self.assertEqual(restamp._mirror_rows(change), [])

	def test_the_pooled_row_query_runs(self):
		self.assertEqual(restamp._pooled_rows("__F26_NO_SE__", {}), set())
