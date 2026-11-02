# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Pins the two patches that repair the order-dimension chain on sites that ran the v15 patches.

``reconcile_v15_order_dimension_custom_fields`` removes v15 Custom Field rows that shadow the
standard sales_type / flow_type / design_type fields of the planning doctypes. The DB-free class
pins which pairs are examined, that only ``frappe.db.delete`` is used (never ``delete_doc``) and
that Property Setters are logged, not deleted. The DB-backed class plants a shadow row on the
site and shows the standard field, with its ``fetch_from``, coming back once the row is gone,
while a Property Setter on the same fieldname survives and is reported.

``backfill_order_dimensions_down_chain`` writes what the shadowed ``fetch_from`` never did. The
DB-free class pins the hop order, the blank-target / non-blank-source filter and the
missing-column skip. The DB-backed class plants one Sales Order -> PMO -> MWO / SNC -> Serial No
chain and shows blanks filled, an existing value kept and a second run writing nothing.

DB-backed classes keep everything inside one transaction: every test runs inside a savepoint
that is rolled back, and the doctype cache is cleared afterwards.
"""

import io
from contextlib import redirect_stdout
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import (
	backfill_order_dimensions_down_chain as backfill_patch,
)
from jewellery_erpnext.patches import (
	reconcile_v15_order_dimension_custom_fields as reconcile_patch,
)

SHADOWED_PAIRS = [
	("Parent Manufacturing Order", "sales_type"),
	("Parent Manufacturing Order", "flow_type"),
	("Parent Manufacturing Order", "design_type"),
	("Manufacturing Work Order", "sales_type"),
	("Manufacturing Work Order", "flow_type"),
	("Manufacturing Work Order", "design_type"),
	("Serial Number Creator", "sales_type"),
	("Serial Number Creator", "flow_type"),
]


class TestReconcileV15OrderDimensionFields(IntegrationTestCase):
	"""Which rows the reconcile patch removes, and how. DB-free: frappe is faked throughout."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, standard=SHADOWED_PAIRS, shadowed=SHADOWED_PAIRS, setters=()):
		"""Run reconcile() against faked metadata.

		``standard``: pairs that have a standard DocField. ``shadowed``: pairs that have a
		Custom Field row. ``setters``: (doctype, fieldname, property, value) Property Setters.
		"""
		standard, shadowed = set(standard), set(shadowed)

		def exists(doctype, filters=None, *args, **kwargs):
			if doctype == "DocField":
				return (filters["parent"], filters["fieldname"]) in standard
			return None

		def get_all(doctype, filters=None, *args, **kwargs):
			if doctype == "Custom Field":
				pair = (filters["dt"], filters["fieldname"])
				if pair not in shadowed:
					return []
				return [
					frappe._dict(
						name=f"{pair[0]}-{pair[1]}",
						dt=pair[0],
						fieldname=pair[1],
						fieldtype="Select",
						options="\nA\nB",
						fetch_from=None,
					)
				]
			if doctype == "Property Setter":
				return [
					frappe._dict(
						name=f"{dt}-{fn}-{prop}",
						doc_type=dt,
						field_name=fn,
						property=prop,
						value=value,
					)
					for dt, fn, prop, value in setters
					if (dt, fn) == (filters["doc_type"], filters["field_name"])
				]
			return []

		self.mocks = {}
		for name, target, kwargs in (
			("exists", reconcile_patch.frappe.db, {"side_effect": exists}),
			("get_all", reconcile_patch.frappe, {"side_effect": get_all}),
			("delete", reconcile_patch.frappe.db, {}),
			("clear_cache", reconcile_patch.frappe, {}),
			("delete_doc", reconcile_patch.frappe, {}),
			("logger", reconcile_patch.frappe, {}),
		):
			patcher = patch.object(target, name, **kwargs)
			self.mocks[name] = patcher.start()
			self.addCleanup(patcher.stop)

		out = io.StringIO()
		with redirect_stdout(out):
			removed = reconcile_patch.reconcile()
		self.printed = out.getvalue()
		self.logged = [
			c.args[0]
			for level in ("info", "warning")
			for c in getattr(self.mocks["logger"].return_value, level).call_args_list
		]
		return removed

	def _calls_for(self, doctype):
		return [c for c in self.mocks["get_all"].call_args_list if c.args[0] == doctype]

	def test_only_the_eight_approved_pairs_are_examined(self):
		self.assertEqual(
			[
				(dt, fn)
				for dt, fns in reconcile_patch.SHADOWED_FIELDS.items()
				for fn in fns
			],
			SHADOWED_PAIRS,
		)

		self._run()

		examined = [
			(c.args[1]["parent"], c.args[1]["fieldname"])
			for c in self.mocks["exists"].call_args_list
		]
		self.assertEqual(examined, SHADOWED_PAIRS)
		for doctype in ("Parent Manufacturing Order", "Manufacturing Work Order"):
			self.assertNotIn((doctype, "order_type"), examined)
		self.assertNotIn(("Serial Number Creator", "order_type"), examined)
		self.assertNotIn(("Serial Number Creator", "design_type"), examined)

	def test_standard_docfield_lookup_is_same_parent(self):
		self._run(shadowed=[])

		self.assertIn(
			call(
				"DocField",
				{
					"parent": "Serial Number Creator",
					"parenttype": "DocType",
					"fieldname": "flow_type",
				},
			),
			self.mocks["exists"].call_args_list,
		)

	def test_each_shadowed_pair_is_deleted_once_and_each_doctype_cleared_once(self):
		removed = self._run()

		self.assertEqual(
			self.mocks["delete"].call_args_list,
			[
				call("Custom Field", {"dt": doctype, "fieldname": fieldname})
				for doctype, fieldname in SHADOWED_PAIRS
			],
		)
		self.assertEqual(
			self.mocks["clear_cache"].call_args_list,
			[
				call(doctype="Parent Manufacturing Order"),
				call(doctype="Manufacturing Work Order"),
				call(doctype="Serial Number Creator"),
			],
		)
		self.assertEqual(
			[row.name for row in removed],
			[f"{doctype}-{fieldname}" for doctype, fieldname in SHADOWED_PAIRS],
		)
		self.mocks["delete_doc"].assert_not_called()

	def test_pair_without_standard_docfield_is_left_alone(self):
		removed = self._run(standard=[])

		self.assertEqual(removed, [])
		self.assertEqual(self._calls_for("Custom Field"), [])
		self.mocks["delete"].assert_not_called()
		self.mocks["clear_cache"].assert_not_called()

	def test_no_custom_field_rows_means_no_delete_and_no_cache_clear(self):
		removed = self._run(shadowed=[])

		self.assertEqual(removed, [])
		self.mocks["delete"].assert_not_called()
		self.mocks["clear_cache"].assert_not_called()
		self.assertEqual(self._calls_for("Property Setter"), [])
		self.assertEqual(self.printed, "")

	def test_only_the_touched_doctype_is_cleared(self):
		removed = self._run(shadowed=[("Serial Number Creator", "flow_type")])

		self.assertEqual(
			[row.name for row in removed], ["Serial Number Creator-flow_type"]
		)
		self.mocks["delete"].assert_called_once_with(
			"Custom Field", {"dt": "Serial Number Creator", "fieldname": "flow_type"}
		)
		self.mocks["clear_cache"].assert_called_once_with(
			doctype="Serial Number Creator"
		)

	def test_removed_rows_are_logged_and_summarised(self):
		self._run(shadowed=[("Manufacturing Work Order", "flow_type")])

		self.assertTrue(
			any("Manufacturing Work Order-flow_type" in line for line in self.logged)
		)
		self.assertIn("removed 1 shadowing Custom Field row(s)", self.printed)
		self.assertIn("Manufacturing Work Order-flow_type", self.printed)

	def test_property_setters_are_logged_and_never_deleted(self):
		self._run(
			shadowed=[("Serial Number Creator", "flow_type")],
			setters=[("Serial Number Creator", "flow_type", "hidden", "1")],
		)

		self.assertEqual(
			self._calls_for("Property Setter")[0].kwargs["filters"],
			{"doc_type": "Serial Number Creator", "field_name": "flow_type"},
		)
		for c in self.mocks["delete"].call_args_list:
			self.assertEqual(c.args[0], "Custom Field")
		self.mocks["delete_doc"].assert_not_called()

		warnings = [
			c.args[0] for c in self.mocks["logger"].return_value.warning.call_args_list
		]
		self.assertEqual(len(warnings), 1)
		self.assertIn("Serial Number Creator-flow_type-hidden", warnings[0])
		self.assertIn("Serial Number Creator-flow_type-hidden", self.printed)

	def test_property_setters_on_untouched_pairs_are_not_reported(self):
		"""A setter on a pair with no shadow row already applies; nothing changes for it."""
		self._run(
			shadowed=[],
			setters=[("Serial Number Creator", "flow_type", "hidden", "1")],
		)

		self.assertEqual(self._calls_for("Property Setter"), [])
		self.mocks["logger"].return_value.warning.assert_not_called()

	def test_execute_reconciles_then_commits(self):
		manager = MagicMock()
		for patcher in (
			patch.object(reconcile_patch, "reconcile", manager.reconcile),
			patch.object(reconcile_patch.frappe.db, "commit", manager.commit),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

		reconcile_patch.execute()

		self.assertEqual(
			[name for name, _args, _kwargs in manager.mock_calls],
			["reconcile", "commit"],
		)


class TestReconcileOnSite(IntegrationTestCase):
	"""The reconcile patch against real site metadata, on Serial Number Creator.flow_type.

	The real setUpClass runs, so the class rollback applies; each test also runs inside a
	savepoint that is rolled back, and the doctype cache is cleared afterwards. reconcile() itself
	never commits (only execute() does), and frappe.db.delete is plain DML.
	"""

	DOCTYPE = "Serial Number Creator"
	FIELDNAME = "flow_type"
	SHADOW_NAME = "Serial Number Creator-flow_type"
	SETTER_NAME = "Serial Number Creator-flow_type-hidden"
	SAVEPOINT = "reconcile_order_dimension_fields"

	def setUp(self):
		self.assertTrue(
			reconcile_patch.has_standard_field(self.DOCTYPE, self.FIELDNAME),
			"Serial Number Creator.flow_type is expected to be a standard field",
		)
		self.assertFalse(
			frappe.db.exists(
				"Custom Field", {"dt": self.DOCTYPE, "fieldname": self.FIELDNAME}
			),
			"a shadow row is already present; run the reconcile patch on this site first",
		)

		frappe.db.savepoint(self.SAVEPOINT)
		self.addCleanup(frappe.clear_cache, doctype=self.DOCTYPE)
		self.addCleanup(frappe.db.rollback, save_point=self.SAVEPOINT)

	def _plant_shadow_row(self):
		"""A Custom Field row as the v15 chain patch left it: Select, options, no fetch_from."""
		frappe.get_doc(
			{
				"doctype": "Custom Field",
				"name": self.SHADOW_NAME,
				"dt": self.DOCTYPE,
				"fieldname": self.FIELDNAME,
				"label": "Flow Type",
				"fieldtype": "Select",
				"options": "\n_TEST-FLOW-A\n_TEST-FLOW-B",
				"insert_after": "sales_type",
				"read_only": 1,
				"module": "Jewellery Erpnext",
			}
		).db_insert()
		frappe.clear_cache(doctype=self.DOCTYPE)

	def _plant_property_setter(self):
		frappe.get_doc(
			{
				"doctype": "Property Setter",
				"name": self.SETTER_NAME,
				"doctype_or_field": "DocField",
				"doc_type": self.DOCTYPE,
				"field_name": self.FIELDNAME,
				"property": "hidden",
				"property_type": "Check",
				"value": "1",
			}
		).db_insert()
		frappe.clear_cache(doctype=self.DOCTYPE)

	def _reconcile(self):
		"""reconcile(), with its logger captured and its summary kept off the test output."""
		logger = MagicMock()
		out = io.StringIO()
		with patch.object(reconcile_patch.frappe, "logger", return_value=logger):
			with redirect_stdout(out):
				removed = reconcile_patch.reconcile()
		logged = [
			c.args[0]
			for level in ("info", "warning")
			for c in getattr(logger, level).call_args_list
		]
		return removed, logged, out.getvalue()

	def test_shadow_row_is_removed_and_the_standard_field_returns(self):
		self._plant_shadow_row()
		# The premise: while the row exists, it is the field the meta serves.
		self.assertEqual(
			frappe.get_meta(self.DOCTYPE).get_field(self.FIELDNAME).fieldtype, "Select"
		)

		removed, logged, printed = self._reconcile()

		self.assertEqual([row.name for row in removed], [self.SHADOW_NAME])
		self.assertEqual(removed[0].fieldtype, "Select")
		self.assertFalse(frappe.db.exists("Custom Field", self.SHADOW_NAME))
		self.assertTrue(any(self.SHADOW_NAME in line for line in logged))
		self.assertIn(self.SHADOW_NAME, printed)

		field = frappe.get_meta(self.DOCTYPE).get_field(self.FIELDNAME)
		self.assertEqual(field.fieldtype, "Data")
		self.assertEqual(field.fetch_from, "parent_manufacturing_order.flow_type")
		self.assertTrue(
			reconcile_patch.has_standard_field(self.DOCTYPE, self.FIELDNAME)
		)

		removed_again, _logged, _printed = self._reconcile()
		self.assertEqual(removed_again, [])

	def test_property_setter_on_shadowed_field_survives_and_is_logged(self):
		self._plant_shadow_row()
		self._plant_property_setter()

		_removed, logged, printed = self._reconcile()

		self.assertTrue(frappe.db.exists("Property Setter", self.SETTER_NAME))
		self.assertTrue(any(self.SETTER_NAME in line for line in logged))
		self.assertIn(self.SETTER_NAME, printed)

		# Why it is reported: with the shadow row gone it now applies to the standard field.
		field = frappe.get_meta(self.DOCTYPE).get_field(self.FIELDNAME)
		self.assertEqual(field.fieldtype, "Data")
		self.assertEqual(field.hidden, 1)


class TestBackfillOrderDimensionsDownChain(IntegrationTestCase):
	"""What the backfill writes, in which order, and when it skips. DB-free: frappe.db is faked."""

	@classmethod
	def setUpClass(cls):
		pass

	def _patch_db(self, count=0, has_column=True):
		self.mocks = {}
		for name, kwargs in (
			("sql", {"return_value": [[count]]}),
			(
				"has_column",
				{"side_effect": has_column}
				if callable(has_column)
				else {"return_value": has_column},
			),
			("commit", {}),
		):
			patcher = patch.object(backfill_patch.frappe.db, name, **kwargs)
			self.mocks[name] = patcher.start()
			self.addCleanup(patcher.stop)

		patcher = patch.object(backfill_patch.frappe, "logger")
		self.mocks["logger"] = patcher.start()
		self.addCleanup(patcher.stop)

	def _hop(self, target):
		return next(hop for hop in backfill_patch.HOPS if hop["target"] == target)

	def test_hops_run_in_chain_order(self):
		self.assertEqual(
			[(hop["source"], hop["target"]) for hop in backfill_patch.HOPS],
			[
				("Sales Order", "Parent Manufacturing Order"),
				("Parent Manufacturing Order", "Manufacturing Work Order"),
				("Parent Manufacturing Order", "Serial Number Creator"),
				("Serial Number Creator", "Serial No"),
			],
		)

	def test_pmo_reads_the_sales_order_custom_fields(self):
		self.assertEqual(
			self._hop("Parent Manufacturing Order")["fields"],
			{
				"order_type": "order_type",
				"sales_type": "sales_type",
				"flow_type": "custom_flow_type",
				"design_type": "custom_design_type",
			},
		)

	def test_serial_no_comes_only_from_a_submitted_snc(self):
		hop = self._hop("Serial No")
		self.assertEqual(hop["join"], ("name", "fg_serial_no"))
		self.assertIn(
			"src.docstatus = 1",
			backfill_patch._where_clause(hop, "custom_flow_type", "flow_type"),
		)
		self.assertNotIn("custom_design_type", hop["fields"])

	def test_fill_never_overwrites_and_never_writes_a_blank(self):
		where = backfill_patch._where_clause(
			self._hop("Manufacturing Work Order"), "flow_type", "flow_type"
		)
		self.assertIn("IFNULL(tgt.`flow_type`, '') = ''", where)
		self.assertIn("IFNULL(src.`flow_type`, '') != ''", where)

	def test_fill_counts_then_updates_with_the_same_filter(self):
		self._patch_db(count=7)
		hop = self._hop("Manufacturing Work Order")

		self.assertEqual(backfill_patch.fill(hop, "flow_type", "flow_type"), 7)

		select, update = [c.args[0] for c in self.mocks["sql"].call_args_list]
		where = backfill_patch._where_clause(hop, "flow_type", "flow_type")
		self.assertTrue(
			select.startswith("SELECT COUNT(*) FROM `tabManufacturing Work Order`")
		)
		self.assertTrue(update.startswith("UPDATE `tabManufacturing Work Order` tgt"))
		self.assertIn("SET tgt.`flow_type` = src.`flow_type`", update)
		self.assertTrue(select.endswith(where) and update.endswith(where))

	def test_fill_skips_the_update_when_nothing_is_blank(self):
		self._patch_db(count=0)

		self.assertEqual(
			backfill_patch.fill(
				self._hop("Serial No"), "custom_flow_type", "flow_type"
			),
			0,
		)
		self.assertEqual(self.mocks["sql"].call_count, 1)

	def test_missing_column_skips_only_that_pair(self):
		self._patch_db(has_column=lambda doctype, column: column != "custom_flow_type")

		with patch.object(backfill_patch, "fill", return_value=1) as fill:
			counts = backfill_patch.backfill()

		self.assertIsNone(counts[("Parent Manufacturing Order", "flow_type")])
		self.assertIsNone(counts[("Serial No", "custom_flow_type")])
		self.assertEqual(counts[("Manufacturing Work Order", "flow_type")], 1)
		self.assertNotIn(
			call(
				self._hop("Parent Manufacturing Order"), "flow_type", "custom_flow_type"
			),
			fill.call_args_list,
		)
		self.mocks["logger"].return_value.warning.assert_called()

	def test_backfill_commits_only_when_asked(self):
		self._patch_db()
		with patch.object(backfill_patch, "fill", return_value=0):
			backfill_patch.backfill()
			self.mocks["commit"].assert_not_called()

			out = io.StringIO()
			with redirect_stdout(out):
				backfill_patch.execute()

		self.assertEqual(self.mocks["commit"].call_count, len(backfill_patch.HOPS))
		self.assertIn("Parent Manufacturing Order.flow_type=0", out.getvalue())


class TestBackfillOnSite(IntegrationTestCase):
	"""The backfill against real tables: one planted Sales Order -> Serial No chain."""

	PREFIX = "_TEST-ORDER-DIM-BACKFILL-"
	SAVEPOINT = "backfill_order_dimensions_down_chain"

	def setUp(self):
		for doctype, column in (
			("Sales Order", "custom_flow_type"),
			("Sales Order", "custom_design_type"),
			("Serial No", "custom_flow_type"),
		):
			self.assertTrue(
				frappe.db.has_column(doctype, column),
				f"{doctype}.{column} is expected on the test site (create_test_data)",
			)

		frappe.db.savepoint(self.SAVEPOINT)
		self.addCleanup(frappe.db.rollback, save_point=self.SAVEPOINT)

		self.so = self._insert(
			"Sales Order",
			"SO",
			order_type="Stock Order",
			sales_type="Outwork",
			custom_flow_type="FILLER",
			custom_design_type="As Per Design Type",
		)
		self.pmo = self._insert(
			"Parent Manufacturing Order", "PMO", sales_order=self.so
		)
		self.mwo = self._insert(
			"Manufacturing Work Order", "MWO", manufacturing_order=self.pmo
		)
		# A value already on a document is never overwritten.
		self.kept_mwo = self._insert(
			"Manufacturing Work Order",
			"MWO-KEPT",
			manufacturing_order=self.pmo,
			flow_type="MTO",
		)
		self.serial = self._insert("Serial No", "SN", serial_no=f"{self.PREFIX}SN")
		self.snc = self._insert(
			"Serial Number Creator",
			"SNC",
			parent_manufacturing_order=self.pmo,
			fg_serial_no=self.serial,
			docstatus=1,
		)

	def _insert(self, doctype, suffix, **values):
		name = f"{self.PREFIX}{suffix}"
		frappe.get_doc({"doctype": doctype, "name": name, **values}).db_insert()
		return name

	def _backfill(self):
		with redirect_stdout(io.StringIO()):
			return backfill_patch.backfill()

	def test_blanks_are_filled_down_the_chain(self):
		self._backfill()

		expected = {
			"order_type": "Stock Order",
			"sales_type": "Outwork",
			"flow_type": "FILLER",
			"design_type": "As Per Design Type",
		}
		for doctype, name in (
			("Parent Manufacturing Order", self.pmo),
			("Manufacturing Work Order", self.mwo),
		):
			self.assertEqual(
				frappe.db.get_value(doctype, name, list(expected), as_dict=True),
				expected,
			)

		self.assertEqual(
			frappe.db.get_value(
				"Serial Number Creator",
				self.snc,
				["order_type", "sales_type", "flow_type"],
				as_dict=True,
			),
			{
				"order_type": "Stock Order",
				"sales_type": "Outwork",
				"flow_type": "FILLER",
			},
		)
		self.assertEqual(
			frappe.db.get_value(
				"Serial No",
				self.serial,
				["custom_order_type", "custom_sales_type", "custom_flow_type"],
				as_dict=True,
			),
			{
				"custom_order_type": "Stock Order",
				"custom_sales_type": "Outwork",
				"custom_flow_type": "FILLER",
			},
		)

	def test_existing_value_is_kept(self):
		self._backfill()

		self.assertEqual(
			frappe.db.get_value("Manufacturing Work Order", self.kept_mwo, "flow_type"),
			"MTO",
		)
		# Its blank siblings on the same row are still filled.
		self.assertEqual(
			frappe.db.get_value(
				"Manufacturing Work Order", self.kept_mwo, "sales_type"
			),
			"Outwork",
		)

	def test_second_run_writes_nothing(self):
		self._backfill()

		self.assertFalse(any(self._backfill().values()))
