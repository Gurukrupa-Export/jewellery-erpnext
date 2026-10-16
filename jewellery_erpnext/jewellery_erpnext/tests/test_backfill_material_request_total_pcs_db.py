# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""The Total Pcs backfill against a REAL MariaDB, not a fake.

``test_backfill_material_request_total_pcs`` mocks ``frappe.db`` and asserts on query text.
That is the right tool for pinning the write path -- grouping, chunking, never touching
``modified`` -- but it is structurally incapable of catching the bug this module exists for:
a fake cannot tell you that MariaDB and Python disagree about what ``'12abc'`` means.

So these tests execute the real queries against real rows. Specifically they pin the property
the patch is built around: **the number the backfill writes equals the number
``update_pure_qty`` computes for the same rows.** An earlier revision aggregated with
``SUM(CAST(NULLIF(pcs, '') AS SIGNED))`` and silently broke that for two classes of value --
``'12abc'`` (SQL 12, ``cint`` 0) and ``'1e3'`` (SQL 1, ``cint`` 1000). Both appear below as
fixtures, so a regression to SQL aggregation fails here rather than on production.

TWO THINGS THAT LOOK ODD AND ARE LOAD-BEARING
----------------------------------------------
``db_insert()`` rather than ``insert()``. Creating a Material Request through the ORM runs
``before_validate`` -> ``update_pure_qty``, which fills ``custom_total_pcs`` correctly and
leaves the backfill nothing to correct -- the test would pass while testing nothing.
``db_insert`` writes the row and runs no hooks, which is the only way to manufacture the
stale state this patch exists to repair. It also keeps these tests independent of
``create_test_data``'s company/item/warehouse names, so they behave the same on a dev bench
and in CI.

``frappe.db.commit`` is stubbed out. ``backfill()`` commits once per distinct value, and a
commit inside an IntegrationTestCase defeats the harness rollback
(``addClassCleanup(_rollback_db)``) -- the rows would survive onto the shared CI site and
every later test module in the same workflow would inherit them. Stubbing it keeps the
SELECTs and UPDATEs completely real while leaving them inside the transaction that gets
rolled back.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils import (
	before_validate as mr_before_validate,
)
from jewellery_erpnext.patches import backfill_material_request_total_pcs as backfill

# Namespaced so a half-rolled-back run is recognisable and cleanable by hand.
PREFIX = "_TEST-TOTALPCS-"

# The two values on which MariaDB CAST and frappe cint disagree, alongside ordinary ones.
# cint: '7' -> 7, '12abc' -> 0, '1e3' -> 1000, '' -> 0, None -> 0.  Expected total: 1007.
# A CAST-based aggregate would make this 7 + 12 + 1 = 20 instead.
DIVERGENT_ROWS = ["7", "12abc", "1e3", "", None]
DIVERGENT_TOTAL = 1007
CAST_WOULD_GIVE = 20


class TestBackfillAgainstRealDatabase(IntegrationTestCase):
	"""Real rows, real queries, real MariaDB."""

	def setUp(self):
		self.created = []
		# Real SQL, no commit: see the module docstring.
		self._no_commit = patch.object(frappe.db, "commit", lambda *a, **k: None)
		self._no_commit.start()
		self.addCleanup(self._no_commit.stop)
		self.addCleanup(self._delete_created)

	def _delete_created(self):
		"""Belt and braces on top of the harness rollback."""
		for name in self.created:
			frappe.db.delete("Material Request Item", {"parent": name})
			frappe.db.delete("Material Request", {"name": name})

	def _make_request(self, suffix, pcs_values, stored_total=0):
		"""A Material Request with one item row per entry in ``pcs_values``.

		``stored_total`` is what ``custom_total_pcs`` starts at -- 0 models a request that
		predates the field, which is the whole population this patch targets.
		"""
		name = f"{PREFIX}{suffix}"
		frappe.get_doc(
			{
				"doctype": "Material Request",
				"name": name,
				"material_request_type": "Manufacture",
				"transaction_date": "2026-01-01",
				"schedule_date": "2026-01-01",
				"docstatus": 0,
				"custom_total_pcs": stored_total,
			}
		).db_insert()
		self.created.append(name)

		for idx, pcs in enumerate(pcs_values, 1):
			frappe.get_doc(
				{
					"doctype": "Material Request Item",
					"name": f"{name}-ROW-{idx}",
					"parent": name,
					"parenttype": "Material Request",
					"parentfield": "items",
					"idx": idx,
					"item_code": "_TEST-TOTALPCS-ITEM",
					"qty": 1,
					"pcs": pcs,
				}
			).db_insert()

		return name

	def _stored(self, name):
		return frappe.db.get_value("Material Request", name, "custom_total_pcs")

	@staticmethod
	def _runtime_total(pcs_values):
		"""What ``update_pure_qty`` computes for the same rows -- the authority."""
		doc = SimpleNamespace(
			custom_transfer_type="Transfer to Reserve",
			custom_manufacturer="_TEST-TOTALPCS-MANU",
			items=[
				SimpleNamespace(
					custom_variant_of="D",
					item_code="_TEST-TOTALPCS-ITEM",
					custom_alternative_item=None,
					qty=1,
					pcs=pcs,
					custom_pure_qty=None,
				)
				for pcs in pcs_values
			],
			custom_total_quantity=None,
			custom_total_pcs=None,
		)
		with patch(
			"jewellery_erpnext.jewellery_erpnext.customization.material_request.utils"
			".before_validate.prefetch_purity_percentages"
		):
			mr_before_validate.update_pure_qty(doc)
		return doc.custom_total_pcs

	# -- the tests ------------------------------------------------------------------

	def test_plain_rows_are_summed(self):
		name = self._make_request("PLAIN", ["1", "32"])

		backfill.backfill()

		self.assertEqual(self._stored(name), 33)

	def test_a_request_with_no_rows_stays_zero(self):
		name = self._make_request("EMPTY", [])

		backfill.backfill()

		self.assertEqual(self._stored(name), 0)

	def test_backfilled_value_equals_what_update_pure_qty_computes(self):
		"""The property the whole patch rests on, over values that break SQL aggregation."""
		name = self._make_request("DIVERGENT", DIVERGENT_ROWS)

		backfill.backfill()

		self.assertEqual(self._stored(name), self._runtime_total(DIVERGENT_ROWS))

	def test_divergent_values_follow_cint_not_sql_cast(self):
		"""Pins the exact numbers, so a regression names itself in the failure message."""
		name = self._make_request("CASTCHECK", DIVERGENT_ROWS)

		backfill.backfill()

		stored = self._stored(name)
		self.assertEqual(stored, DIVERGENT_TOTAL)
		self.assertNotEqual(
			stored,
			CAST_WOULD_GIVE,
			msg="backfill regressed to SQL CAST aggregation; it no longer matches cint()",
		)

	def test_an_already_correct_request_is_left_alone(self):
		name = self._make_request("CORRECT", ["4"], stored_total=4)

		# Nothing to do for this document, so it must not appear in the work list.
		pending = backfill.pending_updates(backfill.computed_totals())
		self.assertNotIn(name, [n for names in pending.values() for n in names])

	def test_a_second_run_writes_nothing(self):
		self._make_request("IDEMPOTENT", ["5", "6"])

		self.assertGreater(backfill.backfill(), 0)
		self.assertEqual(backfill.backfill(), 0)

	def test_modified_is_not_bumped(self):
		name = self._make_request("MODIFIED", ["9"])
		before = frappe.db.get_value("Material Request", name, "modified")

		backfill.backfill()

		self.assertEqual(self._stored(name), 9)
		self.assertEqual(
			frappe.db.get_value("Material Request", name, "modified"), before
		)

	def test_rows_of_another_parenttype_are_not_counted(self):
		"""The parenttype/parentfield filter, proven against real rows rather than query text."""
		name = self._make_request("SCOPED", ["3"])
		frappe.get_doc(
			{
				"doctype": "Material Request Item",
				"name": f"{name}-STRAY",
				"parent": name,
				"parenttype": "Some Other Doctype",
				"parentfield": "not_items",
				"idx": 99,
				"item_code": "_TEST-TOTALPCS-ITEM",
				"qty": 1,
				"pcs": "1000",
			}
		).db_insert()

		backfill.backfill()

		self.assertEqual(self._stored(name), 3)
