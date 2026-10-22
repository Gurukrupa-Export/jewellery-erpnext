# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Pins the chain Finding MWO back-fill: which PMOs it touches, and what it leaves alone.

The patch creates manufacturing documents and ticks Item flags on a live site during
``bench migrate``, so these tests care most about its blast radius:

* THAT a site without the listed PMOs (every site but KGGK) is left untouched.
* THAT a PMO is skipped once it has a Finding MWO, once its work orders are all finished or
  closed, or once a chain was issued from stock against them -- the piece has its chain then.
* THAT the chain variant is ticked before ``create_finding_mwo()`` runs, because that function
  silently creates nothing for an unticked one, and is not rewritten when already ticked.
* THAT one failing PMO is rolled back and logged without stopping the others or the migrate.
* THAT a dry run rolls every PMO back.

DB-free per the suite convention: ``setUpClass`` is neutralized and every frappe lookup is
served by an in-memory site.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import backfill_chain_finding_mwo as backfill

BC_100 = "F-G-22KT-91.75-Y-CHA-BC-1.00 INCH"
BC_075 = "F-G-22KT-91.75-Y-CHA-BC-0.75 INCH"


class _Site:
	"""A tiny in-memory site: PMOs, their work orders, chain variants and the patch's writes.

	pmos: {name: {"mwos": [(mwo, status)], "chains": [variant], "finding_mwo": bool,
	"docstatus": int, "tracking_bom": str}}; ticked: variants already ticked; issued: MWOs a
	chain was already issued against.
	"""

	def __init__(self, pmos, ticked=(), issued=(), fail=()):
		self.pmos = pmos
		self.ticked = set(ticked)
		self.issued = set(issued)
		self.fail = set(fail)
		self.created = {}  # pmo -> were all its chains ticked when create_finding_mwo ran
		self.ticks, self.queries, self.errors = [], [], []
		self.commits = self.rollbacks = 0

	# --- frappe.db ---
	def exists(self, doctype, filters):
		if doctype == "Parent Manufacturing Order":
			return filters in self.pmos
		name = filters["manufacturing_order"]
		return bool(self.pmos[name].get("finding_mwo") or name in self.created)

	def get_value(self, doctype, name, fieldname):
		assert (doctype, fieldname) == ("Item", "custom_is_manufacturing_item")
		return 1 if name in self.ticked else 0

	def set_value(self, doctype, name, fieldname, value):
		assert (doctype, fieldname, value) == (
			"Item",
			"custom_is_manufacturing_item",
			1,
		)
		self.ticks.append(name)
		self.ticked.add(name)

	def sql(self, query, values=None, **kwargs):
		self.queries.append(query)
		return [("SED-1",)] if self.issued & set(values["mwos"]) else []

	def commit(self):
		self.commits += 1

	def rollback(self):
		self.rollbacks += 1

	# --- frappe ---
	def get_doc(self, doctype, name):
		pmo = self.pmos[name]
		return frappe._dict(
			name=name,
			docstatus=pmo.get("docstatus", 1),
			custom_tracking_bom=pmo.get("tracking_bom", "TB-1"),
		)

	def get_all(self, doctype, filters, fields=None, pluck=None):
		name = filters["manufacturing_order"]
		if filters["is_finding_mwo"] == 1:
			return [f"MWO-{name}-CHAIN"] if name in self.created else []
		return [frappe._dict(name=m, status=s) for m, s in self.pmos[name]["mwos"]]

	def chain_rows(self, pmo):
		return [frappe._dict(item_variant=v) for v in self.pmos[pmo.name]["chains"]]

	def create_finding_mwo(self, pmo, rows):
		if pmo.name in self.fail:
			raise frappe.ValidationError("cannot create")
		self.created[pmo.name] = all(row.item_variant in self.ticked for row in rows)

	def log_error(self, message, title):
		self.errors.append(title)


def _pmo(chains=(BC_100,), mwos=(("MWO-1", "Not Started"),), **fields):
	return {"chains": list(chains), "mwos": list(mwos), **fields}


class TestBackfillChainFindingMWO(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, site, dry_run=False):
		with (
			patch.object(
				backfill, "PMOS", tuple(site.pmos) + ("PMO-NOT-ON-THIS-SITE",)
			),
			patch.object(backfill.frappe, "db", site),
			patch.object(backfill.frappe, "get_doc", site.get_doc),
			patch.object(backfill.frappe, "get_all", site.get_all),
			patch.object(backfill.frappe, "log_error", site.log_error),
			patch.object(backfill, "_get_chain_finding_rows", site.chain_rows),
			patch.object(backfill, "create_finding_mwo", site.create_finding_mwo),
			patch("builtins.print"),
		):
			return backfill.execute(dry_run=dry_run)

	def test_a_site_without_these_pmos_is_untouched(self):
		site = _Site({})
		result = self._run(site)
		self.assertEqual(result, {"created": [], "skipped": [], "failed": []})
		self.assertEqual(
			(site.commits, site.rollbacks, site.ticks, site.errors), (0, 0, [], [])
		)

	def test_unticked_chain_is_ticked_before_the_mwo_is_created(self):
		site = _Site({"PMO-1": _pmo()})
		result = self._run(site)
		self.assertEqual(site.ticks, [BC_100])
		self.assertEqual(site.created, {"PMO-1": True})
		self.assertEqual(result["created"], [("PMO-1", "created MWO-PMO-1-CHAIN")])
		self.assertEqual(site.commits, 1)

	def test_ticked_chain_is_not_rewritten(self):
		site = _Site({"PMO-1": _pmo(chains=(BC_075,))}, ticked={BC_075})
		self._run(site)
		self.assertEqual(site.ticks, [])
		self.assertIn("PMO-1", site.created)

	def test_pmo_with_a_finding_mwo_is_skipped(self):
		site = _Site({"PMO-1": _pmo(finding_mwo=True)})
		result = self._run(site)
		self.assertEqual(
			result["skipped"], [("PMO-1", "skipped: already has a Finding MWO")]
		)
		self.assertEqual((site.created, site.ticks), ({}, []))

	def test_finished_or_closed_work_orders_are_skipped(self):
		site = _Site(
			{
				"PMO-DONE": _pmo(mwos=(("MWO-1", "Completed"),)),
				"PMO-CLOSED": _pmo(mwos=(("MWO-2", "Closed"), ("MWO-3", "Completed"))),
				"PMO-NONE": _pmo(mwos=()),
			}
		)
		result = self._run(site)
		self.assertEqual(
			{outcome for _, outcome in result["skipped"]},
			{"skipped: work orders finished or closed"},
		)
		self.assertEqual((site.created, site.ticks), ({}, []))

	def test_one_live_work_order_beside_a_closed_one_is_back_filled(self):
		# EA13136-002-0002: its first MWO was closed when it was split; the split one is live.
		site = _Site(
			{"PMO-1": _pmo(mwos=(("MWO-1", "Closed"), ("MWO-2", "Not Started")))}
		)
		self._run(site)
		self.assertIn("PMO-1", site.created)

	def test_chain_already_issued_from_stock_is_skipped(self):
		site = _Site(
			{"PMO-1": _pmo(mwos=(("MWO-1", "Not Started"),))}, issued={"MWO-1"}
		)
		result = self._run(site)
		self.assertEqual(
			result["skipped"], [("PMO-1", "skipped: chain already issued from stock")]
		)
		self.assertEqual((site.created, site.ticks), ({}, []))

	def test_issued_check_counts_any_chain_against_the_work_orders(self):
		site = _Site({"PMO-1": _pmo()})
		self._run(site)
		query = " ".join(site.queries[0].split())
		self.assertIn("se.docstatus = 1", query)
		self.assertIn(
			"attr.attribute = 'Finding Category' and attr.attribute_value = 'Chains'",
			query,
		)
		self.assertIn("sed.custom_manufacturing_work_order in %(mwos)s", query)

	def test_unsubmitted_pmo_is_skipped(self):
		site = _Site({"PMO-1": _pmo(docstatus=2)})
		result = self._run(site)
		self.assertEqual(
			result["skipped"], [("PMO-1", "skipped: not submitted or no Tracking BOM")]
		)

	def test_a_failure_is_logged_and_the_next_pmo_still_runs(self):
		site = _Site(
			{"PMO-BAD": _pmo(), "PMO-GOOD": _pmo(chains=(BC_075,))}, fail={"PMO-BAD"}
		)
		result = self._run(site)
		self.assertEqual(result["failed"], ["PMO-BAD"])
		self.assertIn("PMO-GOOD", site.created)
		self.assertEqual(site.rollbacks, 1)
		self.assertEqual(site.commits, 1)
		self.assertIn("Chain Finding MWO back-fill failed: PMO-BAD", site.errors)

	def test_dry_run_rolls_every_pmo_back(self):
		site = _Site({"PMO-1": _pmo(), "PMO-2": _pmo(finding_mwo=True)})
		self._run(site, dry_run=True)
		self.assertEqual((site.commits, site.rollbacks), (0, 2))
		self.assertIn("Chain Finding MWO Back-fill", site.errors)
