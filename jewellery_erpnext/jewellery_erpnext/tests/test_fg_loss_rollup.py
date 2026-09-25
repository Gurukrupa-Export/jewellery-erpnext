# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""F14: the FG work order's header loss is the loss of every sibling operation.

sync_mwo_weights copies each sibling work order's LATEST operation onto the FG work
order. That is right for the weights, which are balances carried forward, and wrong for
loss_wt, which is the change measured on the one operation that was received -- the next
operation starts at 0. So the FG header always read 0 loss.

Only negative loss_wt is a loss: a positive value is material coming in (the casting
receipt carries the whole cast weight, assembly the findings it attaches).

The fake below serves the operation rows each query selects from an in-memory table, and
leaves the arithmetic to the code under test, so these tests pin which rows each field is
summed over and how, not just which query was issued.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
	ManufacturingWorkOrder,
	cumulative_loss_wt,
)

_WEIGHT_FIELDS = (
	"gross_wt",
	"net_wt",
	"finding_wt",
	"diamond_wt",
	"gemstone_wt",
	"other_wt",
	"received_gross_wt",
	"received_net_wt",
	"diamond_pcs",
	"gemstone_pcs",
)


def _op(name, mwo, creation, loss_wt=0.0, **weights):
	row = {field: 0.0 for field in _WEIGHT_FIELDS}
	row.update(weights)
	row.update(
		name=name, manufacturing_work_order=mwo, creation=creation, loss_wt=loss_wt
	)
	return frappe._dict(row)


class _FakeDb:
	"""Serves sync_mwo_weights from an in-memory table of operations."""

	def __init__(self, siblings, operations):
		self.siblings = siblings
		self.operations = operations
		self.writes = {}
		self.mwo_filters = None
		self.loss_query_params = None
		self._real_sql = frappe.db.sql
		self._real_get_all = frappe.db.get_all

	def get_all(self, doctype, *args, **kwargs):
		# Anything else (System Settings, meta) goes to the database untouched: frappe's flt
		# swallows an exception from its rounding-method lookup and silently returns 0.
		if doctype not in ("Manufacturing Work Order", "Manufacturing Operation"):
			return self._real_get_all(doctype, *args, **kwargs)
		filters = kwargs.get("filters", args[0] if args else None)
		if doctype == "Manufacturing Work Order":
			self.mwo_filters = filters
			return list(self.siblings)
		wanted = set(filters["manufacturing_work_order"][1])
		rows = [op for op in self.operations if op.manufacturing_work_order in wanted]
		return sorted(rows, key=lambda op: op.creation, reverse=True)

	def sql(self, query, values=None, *args, **kwargs):
		if "FROM `tabManufacturing Operation`" not in query:
			return self._real_sql(query, values, *args, **kwargs)
		if "WHERE manufacturing_work_order IN" in query:
			# Every operation of the named work orders, as the table would return them.
			self.loss_query_params = values
			mwos = set(values[0])
			return tuple(
				(op.loss_wt,)
				for op in self.operations
				if op.manufacturing_work_order in mwos
			)
		# The latest-operation aggregate. loss_wt is served here too, so a version that
		# still read the loss from this query gets the latest operations' 0.
		names = set(values[0])
		chosen = [op for op in self.operations if op.name in names]
		fields = (*_WEIGHT_FIELDS, "loss_wt")
		return [
			frappe._dict({field: sum(op[field] for op in chosen) for field in fields})
		]

	def set_value(self, doctype, name, values, *args, **kwargs):
		self.writes[(doctype, name)] = dict(values)


def _fg_mwo(**overrides):
	fields = {field: 0.0 for field in _WEIGHT_FIELDS}
	fields.update(
		name="MWO-FG",
		manufacturing_order="PMO-1",
		manufacturing_operation="MOP-FG",
		loss_wt=0.0,
		diamond_wt_in_gram=0.0,
	)
	fields.update(overrides)
	return SimpleNamespace(**fields)


def _sync(doc, fake):
	with (
		patch.object(frappe.db, "get_all", side_effect=fake.get_all),
		patch.object(frappe.db, "sql", side_effect=fake.sql),
		patch.object(frappe.db, "set_value", side_effect=fake.set_value),
	):
		ManufacturingWorkOrder.sync_mwo_weights(doc)


class TestFgLossRollup(IntegrationTestCase):
	def _two_siblings(self):
		# Sibling A was cast (10 g received against a gross-0 issue), lost 0.12 g, then
		# took in 0.02 g of findings; sibling B lost 0.05 g. The latest operation of each
		# carries loss 0, as Department IR leaves it. Process loss: 0.12 + 0.05.
		return _FakeDb(
			siblings=["MWO-A", "MWO-B"],
			operations=[
				_op(
					"MOP-A0",
					"MWO-A",
					0,
					loss_wt=10.0,
					gross_wt=0.0,
					received_gross_wt=10.0,
				),
				_op(
					"MOP-A1",
					"MWO-A",
					1,
					loss_wt=-0.12,
					gross_wt=10.0,
					received_gross_wt=9.88,
				),
				_op(
					"MOP-A2",
					"MWO-A",
					2,
					loss_wt=0.02,
					gross_wt=9.88,
					received_gross_wt=9.9,
				),
				_op(
					"MOP-A3",
					"MWO-A",
					3,
					loss_wt=0.0,
					gross_wt=9.9,
					received_gross_wt=0.0,
				),
				_op(
					"MOP-B1",
					"MWO-B",
					1,
					loss_wt=-0.05,
					gross_wt=4.0,
					received_gross_wt=3.95,
				),
				_op(
					"MOP-B2",
					"MWO-B",
					2,
					loss_wt=0.0,
					gross_wt=3.95,
					received_gross_wt=0.0,
				),
			],
		)

	def test_the_fg_header_carries_the_loss_of_every_sibling_operation(self):
		fake = self._two_siblings()
		doc = _fg_mwo()
		_sync(doc, fake)

		self.assertAlmostEqual(doc.loss_wt, -0.17, places=6)
		self.assertAlmostEqual(
			fake.writes[("Manufacturing Work Order", "MWO-FG")]["loss_wt"],
			-0.17,
			places=6,
		)
		self.assertAlmostEqual(
			fake.writes[("Manufacturing Operation", "MOP-FG")]["loss_wt"],
			-0.17,
			places=6,
		)

	def test_material_coming_in_does_not_offset_the_loss(self):
		"""PMO-KGJPL-NE05090-002-0001 on kg-gk: netting the casting receipt read +34.25 g."""
		fake = _FakeDb(
			siblings=["MWO-PIECE", "MWO-CHAIN"],
			operations=[
				_op("CAST", "MWO-PIECE", 1, loss_wt=39.88),
				_op("FILL", "MWO-PIECE", 2, loss_wt=-1.38),
				_op("PRE-POLISH", "MWO-PIECE", 3, loss_wt=-0.65),
				_op("SETTING", "MWO-PIECE", 4, loss_wt=-2.63),
				_op("FINAL-POLISH", "MWO-PIECE", 5, loss_wt=-0.81),
				_op("CHAIN-ASSEMBLY", "MWO-CHAIN", 1, loss_wt=-0.03),
				_op("CHAIN-POLISH", "MWO-CHAIN", 2, loss_wt=-0.13),
			],
		)
		doc = _fg_mwo()
		_sync(doc, fake)

		self.assertAlmostEqual(doc.loss_wt, -5.63, places=6)

	def test_nothing_lost_reads_zero_not_a_gain(self):
		fake = _FakeDb(
			siblings=["MWO-A"],
			operations=[
				_op("MOP-A0", "MWO-A", 0, loss_wt=10.0),
				_op("MOP-A1", "MWO-A", 1, loss_wt=0.2),
			],
		)
		doc = _fg_mwo()
		_sync(doc, fake)

		self.assertEqual(doc.loss_wt, 0.0)

	def test_the_other_weights_stay_on_the_latest_operation(self):
		fake = self._two_siblings()
		doc = _fg_mwo()
		_sync(doc, fake)

		# MOP-A3 + MOP-B2 only: a sum over every operation would read 37.73 g.
		self.assertAlmostEqual(doc.gross_wt, 13.85, places=6)
		self.assertAlmostEqual(doc.received_gross_wt, 0.0, places=6)
		self.assertAlmostEqual(
			fake.writes[("Manufacturing Operation", "MOP-FG")]["gross_wt"],
			13.85,
			places=6,
		)

	def test_the_loss_is_summed_over_the_siblings_only(self):
		fake = self._two_siblings()
		# The FG work order's own operation is not a sibling and must not feed its own loss.
		fake.operations.append(_op("MOP-FG", "MWO-FG", 4, loss_wt=-9.0))
		doc = _fg_mwo()
		_sync(doc, fake)

		self.assertEqual(fake.mwo_filters["for_fg"], 0)
		self.assertEqual(fake.mwo_filters["name"], ["!=", "MWO-FG"])
		self.assertEqual(set(fake.loss_query_params[0]), {"MWO-A", "MWO-B"})
		self.assertAlmostEqual(doc.loss_wt, -0.17, places=6)

	def test_a_repeated_sync_does_not_compound_the_loss(self):
		fake = self._two_siblings()
		doc = _fg_mwo()
		_sync(doc, fake)
		_sync(doc, fake)

		self.assertAlmostEqual(doc.loss_wt, -0.17, places=6)

	def test_no_sibling_leaves_the_header_as_it_was(self):
		fake = _FakeDb(siblings=[], operations=[])
		doc = _fg_mwo(loss_wt=-0.3, gross_wt=5.0)
		_sync(doc, fake)

		self.assertIsNone(fake.loss_query_params)
		self.assertAlmostEqual(doc.loss_wt, -0.3, places=6)
		self.assertAlmostEqual(
			fake.writes[("Manufacturing Work Order", "MWO-FG")]["loss_wt"],
			-0.3,
			places=6,
		)


class TestCumulativeLossQuery(IntegrationTestCase):
	def test_no_siblings_returns_zero_without_querying(self):
		with patch.object(frappe.db, "sql") as sql:
			self.assertEqual(cumulative_loss_wt([]), 0.0)
		sql.assert_not_called()

	def test_the_query_runs_against_the_real_table(self):
		# No such work order: proves the SQL is valid and COALESCE turns no rows into 0.
		self.assertEqual(cumulative_loss_wt(["__F14_NO_SUCH_MWO__"]), 0.0)


class _FakeBackfillDb:
	"""Serves patches.backfill_fg_loss_wt from in-memory work orders and operations."""

	def __init__(self, fg_orders, siblings, operation_loss, sibling_loss):
		self.fg_orders = fg_orders
		self.siblings = siblings  # {pmo: [sibling mwo]}
		self.operation_loss = operation_loss  # {mop: loss_wt}
		self.sibling_loss = sibling_loss  # {pmo: cumulative loss}
		self.writes = []
		self.committed = False
		self._real_get_all = frappe.get_all
		self._real_exists = frappe.db.exists
		self._real_get_value = frappe.db.get_value

	def get_all(self, doctype, *args, **kwargs):
		# Anything else (System Settings, meta) goes to the database untouched: frappe's flt
		# swallows an exception from its rounding-method lookup and silently returns 0.
		if doctype != "Manufacturing Work Order":
			return self._real_get_all(doctype, *args, **kwargs)
		filters = kwargs.get("filters", args[0] if args else None)
		if filters.get("for_fg") == 1:
			return [frappe._dict(fg) for fg in self.fg_orders]
		return list(self.siblings.get(filters["manufacturing_order"], []))

	def exists(self, doctype, *args, **kwargs):
		if doctype != "Manufacturing Operation":
			return self._real_exists(doctype, *args, **kwargs)
		filters = kwargs.get("dn", args[0] if args else None)
		return bool(filters["manufacturing_work_order"][1])

	def get_value(self, doctype, *args, **kwargs):
		if doctype != "Manufacturing Operation":
			return self._real_get_value(doctype, *args, **kwargs)
		return self.operation_loss.get(args[0])

	def cumulative_loss_wt(self, siblings):
		for pmo, names in self.siblings.items():
			if names == siblings:
				return self.sibling_loss[pmo]
		return 0.0

	def set_value(self, doctype, name, field, value=None, *args, **kwargs):
		self.writes.append((doctype, name, field, value))

	def commit(self):
		self.committed = True


def _fg(name, pmo, mop, gross_wt=5.0, loss_wt=0.0):
	return {
		"name": name,
		"manufacturing_order": pmo,
		"manufacturing_operation": mop,
		"gross_wt": gross_wt,
		"loss_wt": loss_wt,
	}


class TestBackfillFgLossWt(IntegrationTestCase):
	def _run(self, fake, dry_run):
		from jewellery_erpnext.patches import backfill_fg_loss_wt as patch_mod

		with (
			patch.object(frappe, "get_all", side_effect=fake.get_all),
			patch.object(frappe.db, "exists", side_effect=fake.exists),
			patch.object(frappe.db, "get_value", side_effect=fake.get_value),
			patch.object(frappe.db, "set_value", side_effect=fake.set_value),
			patch.object(frappe.db, "commit", side_effect=fake.commit),
			patch.object(
				patch_mod, "cumulative_loss_wt", side_effect=fake.cumulative_loss_wt
			),
		):
			if dry_run is None:
				return patch_mod.execute()
			return patch_mod.execute(dry_run=dry_run)

	def _fake(self):
		return _FakeBackfillDb(
			fg_orders=[
				_fg("FG-STALE", "PMO-1", "MOP-FG1"),
				_fg("FG-NOT-SYNCED", "PMO-2", "MOP-FG2", gross_wt=0.0),
				_fg("FG-NO-OPS", "PMO-3", "MOP-FG3"),
				_fg("FG-OWN-LOSS", "PMO-4", "MOP-FG4"),
				_fg("FG-CURRENT", "PMO-5", "MOP-FG5", loss_wt=-0.2),
			],
			siblings={
				"PMO-1": ["S-1"],
				"PMO-2": ["S-2"],
				"PMO-3": [],
				"PMO-4": ["S-4"],
				"PMO-5": ["S-5"],
			},
			operation_loss={"MOP-FG1": 0.0, "MOP-FG4": -0.07, "MOP-FG5": -0.2},
			sibling_loss={"PMO-1": -0.15, "PMO-2": -0.3, "PMO-4": -0.4, "PMO-5": -0.2},
		)

	def test_the_dry_run_lists_only_stale_headers_and_writes_nothing(self):
		fake = self._fake()
		result = self._run(fake, dry_run=True)

		self.assertEqual([c["mwo"] for c in result["changes"]], ["FG-STALE"])
		self.assertEqual(result["changes"][0]["before"], 0.0)
		self.assertAlmostEqual(result["changes"][0]["after"], -0.15, places=6)
		self.assertEqual([r["mwo"] for r in result["review"]], ["FG-OWN-LOSS"])
		self.assertEqual(
			{s["mwo"] for s in result["skipped"]}, {"FG-NOT-SYNCED", "FG-NO-OPS"}
		)
		self.assertEqual(fake.writes, [])
		self.assertFalse(fake.committed)

	def test_it_is_a_dry_run_by_default(self):
		fake = self._fake()
		self._run(fake, dry_run=None)
		self.assertEqual(fake.writes, [])
		self.assertFalse(fake.committed)

	def test_the_real_run_writes_loss_wt_only(self):
		fake = self._fake()
		self._run(fake, dry_run=False)

		self.assertEqual(
			fake.writes,
			[
				("Manufacturing Work Order", "FG-STALE", "loss_wt", -0.15),
				("Manufacturing Operation", "MOP-FG1", "loss_wt", -0.15),
			],
		)
		self.assertTrue(fake.committed)


class TestBackfillFgLossQueries(IntegrationTestCase):
	"""The backfill's reads, against the real tables, matching nothing. Read-only."""

	def test_detect_runs_against_the_real_tables(self):
		from jewellery_erpnext.patches import backfill_fg_loss_wt as patch_mod

		self.assertEqual(patch_mod.detect(mwos=["__F14_NO_SUCH_MWO__"]), ([], [], []))
