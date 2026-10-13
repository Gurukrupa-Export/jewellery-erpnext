# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Pins the order-dimension metadata patches.

``add_purchase_order_order_dimension_fields`` gives the Purchase Order header the four order
dimensions that ``make_subcontracting_order`` stamps from the Manufacturing Plan. The DB-free
class pins the definition and the calls the patch makes. The DB-backed class runs the real
``create_custom_fields`` against the site and pins what the patch exists for on databases that
already hold some of these rows as Link or Select: the rows become Data with no options, and
the values already stored in those columns survive.

``reconcile_v15_order_dimension_custom_fields`` removes v15 Custom Field rows that shadow the
standard sales_type / flow_type / design_type fields of the planning doctypes. The DB-free class
pins which pairs are examined, that only ``frappe.db.delete`` is used (never ``delete_doc``) and
that Property Setters are logged, not deleted. The DB-backed class plants a shadow row on the
site and shows the standard field, with its ``fetch_from``, coming back once the row is gone,
while a Property Setter on the same fieldname survives and is reported.

DB-backed classes keep everything inside one transaction. ``create_custom_fields`` ends with
``frappe.db.updatedb``, which commits, and a commit inside an IntegrationTestCase defeats the
harness rollback. So ``commit`` and ``updatedb`` are stubbed, every test runs inside a savepoint
that is rolled back, and the doctype cache is cleared afterwards. The schema side is checked
directly instead: the converted columns must already have the definition a Data field gets, so
the schema sync the patch triggers on a real migrate has nothing to alter.
"""

import io
from contextlib import redirect_stdout
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.database.schema import get_definition
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order import (
	PO_DIMENSION_FIELDS,
)
from jewellery_erpnext.patches import (
	add_purchase_order_order_dimension_fields as po_dims,
)
from jewellery_erpnext.patches import (
	reconcile_v15_order_dimension_custom_fields as reconcile_patch,
)

PO_FIELDNAMES = [
	"custom_order_type",
	"custom_sales_type",
	"custom_flow_type",
	"custom_design_type",
]

# Namespaced so a half-rolled-back run is recognisable and cleanable by hand.
PREFIX = "_TEST-ORDER-DIMS-"


class TestPurchaseOrderDimensionFields(IntegrationTestCase):
	"""The Purchase Order field definitions and the calls the patch makes. DB-free."""

	@classmethod
	def setUpClass(cls):
		pass

	def _fields(self):
		return po_dims.CUSTOM_FIELDS["Purchase Order"]

	def test_fields_are_read_only_data_with_blank_options(self):
		self.assertEqual([f["fieldname"] for f in self._fields()], PO_FIELDNAMES)
		for field in self._fields():
			with self.subTest(field["fieldname"]):
				self.assertEqual(field["fieldtype"], "Data")
				# Blank, not absent: the update must clear a stale Link target or Select list.
				self.assertIn("options", field)
				self.assertEqual(field["options"], "")
				self.assertEqual(field["read_only"], 1)
				self.assertEqual(field["module"], "Jewellery Erpnext")
				self.assertTrue(field["description"])
				self.assertFalse(field.get("reqd"))
				self.assertFalse(field.get("no_copy"))

	def test_fields_are_labelled(self):
		self.assertEqual(
			{f["fieldname"]: f["label"] for f in self._fields()},
			{
				"custom_order_type": "Order Type",
				"custom_sales_type": "Sales Type",
				"custom_flow_type": "Flow Type",
				"custom_design_type": "Design Type",
			},
		)

	def test_fieldnames_are_the_ones_make_subcontracting_order_stamps(self):
		self.assertEqual(
			{f["fieldname"] for f in self._fields()}, set(PO_DIMENSION_FIELDS.values())
		)

	def test_insert_after_chain_starts_at_ref_customer(self):
		self.assertEqual(
			[(f["fieldname"], f["insert_after"]) for f in self._fields()],
			[
				("custom_order_type", "ref_customer"),
				("custom_sales_type", "custom_order_type"),
				("custom_flow_type", "custom_sales_type"),
				("custom_design_type", "custom_flow_type"),
			],
		)

	def test_ensure_fields_skips_validation_and_updates_existing_rows(self):
		with patch.object(po_dims, "create_custom_fields") as mock_create:
			po_dims.ensure_fields()

		mock_create.assert_called_once_with(po_dims.CUSTOM_FIELDS, ignore_validate=True)
		# update must stay at its default (True), or existing Link/Select rows keep their type.
		self.assertNotIn("update", mock_create.call_args.kwargs)

	@patch.object(po_dims.frappe, "clear_cache")
	@patch.object(po_dims, "rewrite_insert_after_chain")
	@patch.object(po_dims, "rewrite_field_order", return_value=True)
	def test_place_fields_writes_property_setter_and_insert_after_chain(
		self, mock_order, mock_chain, mock_clear
	):
		po_dims.place_fields()

		mock_order.assert_called_once_with(
			"Purchase Order", ["ref_customer", *PO_FIELDNAMES]
		)
		mock_chain.assert_called_once_with(
			"Purchase Order", PO_FIELDNAMES, "ref_customer"
		)
		mock_clear.assert_called_once_with(doctype="Purchase Order")

	def test_execute_ensures_then_places_then_commits(self):
		manager = MagicMock()
		for patcher in (
			patch.object(po_dims, "ensure_fields", manager.ensure_fields),
			patch.object(po_dims, "place_fields", manager.place_fields),
			patch.object(po_dims.frappe.db, "commit", manager.commit),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

		po_dims.execute()

		self.assertEqual(
			[name for name, _args, _kwargs in manager.mock_calls],
			["ensure_fields", "place_fields", "commit"],
		)


class TestPurchaseOrderDimensionFieldsOnSite(IntegrationTestCase):
	"""The patch against the real site metadata. See the module docstring for the isolation."""

	SAVEPOINT = "po_order_dimension_fields"

	def setUp(self):
		missing = [
			f for f in PO_FIELDNAMES if not frappe.db.has_column("Purchase Order", f)
		]
		self.assertFalse(
			missing,
			"add_purchase_order_order_dimension_fields has not run on this site "
			"(migrate, or create_test_data on a CI site)",
		)

		frappe.db.savepoint(self.SAVEPOINT)
		self.addCleanup(frappe.clear_cache, doctype="Purchase Order")
		self.addCleanup(frappe.db.rollback, save_point=self.SAVEPOINT)

		no_commit = patch.object(frappe.db, "commit", lambda *a, **k: None)
		no_commit.start()
		self.addCleanup(no_commit.stop)
		no_updatedb = patch.object(frappe.db, "updatedb")
		self.mock_updatedb = no_updatedb.start()
		self.addCleanup(no_updatedb.stop)

	@staticmethod
	def _set_definition(fieldname, **values):
		"""Rewrite a Custom Field row in place, the way an older patch left it."""
		frappe.db.set_value(
			"Custom Field", {"dt": "Purchase Order", "fieldname": fieldname}, values
		)
		frappe.clear_cache(doctype="Purchase Order")

	@staticmethod
	def _definition(fieldname):
		return frappe.db.get_value(
			"Custom Field",
			{"dt": "Purchase Order", "fieldname": fieldname},
			["fieldtype", "options", "label", "read_only"],
			as_dict=True,
		)

	@staticmethod
	def _column_types():
		columns = frappe.db.get_table_columns_description("tabPurchase Order")
		return {c.name: c.type for c in columns if c.name in PO_FIELDNAMES}

	def _stored_purchase_order(self, **values):
		"""A bare Purchase Order row holding values in the dimension columns. No hooks run."""
		name = f"{PREFIX}PO-1"
		frappe.get_doc(
			{"doctype": "Purchase Order", "name": name, **values}
		).db_insert()
		return name

	def test_meta_has_four_read_only_data_fields_after_ref_customer(self):
		meta = frappe.get_meta("Purchase Order")
		for fieldname in PO_FIELDNAMES:
			with self.subTest(fieldname):
				field = meta.get_field(fieldname)
				self.assertEqual(field.fieldtype, "Data")
				self.assertFalse(field.options)
				self.assertEqual(field.read_only, 1)

		order = [df.fieldname for df in meta.fields]
		start = order.index("ref_customer")
		self.assertEqual(order[start : start + 5], ["ref_customer", *PO_FIELDNAMES])

	def test_link_and_select_rows_become_data_and_keep_values(self):
		name = self._stored_purchase_order(
			custom_sales_type="_TEST-SALES-TYPE", custom_flow_type="_TEST-FLOW-A"
		)
		self._set_definition(
			"custom_sales_type", fieldtype="Link", options="Sales Type"
		)
		self._set_definition(
			"custom_flow_type",
			fieldtype="Select",
			options="\n_TEST-FLOW-A\n_TEST-FLOW-B",
		)
		columns_before = self._column_types()

		po_dims.ensure_fields()

		for fieldname, label in (
			("custom_sales_type", "Sales Type"),
			("custom_flow_type", "Flow Type"),
		):
			with self.subTest(fieldname):
				definition = self._definition(fieldname)
				self.assertEqual(definition.fieldtype, "Data")
				self.assertFalse(definition.options)
				self.assertEqual(definition.label, label)
				self.assertEqual(definition.read_only, 1)

		self.assertEqual(
			frappe.db.get_value(
				"Purchase Order",
				name,
				["custom_sales_type", "custom_flow_type"],
				as_dict=True,
			),
			{
				"custom_sales_type": "_TEST-SALES-TYPE",
				"custom_flow_type": "_TEST-FLOW-A",
			},
		)

		# The schema sync is stubbed here, so these columns stay as they were created.
		# What makes the real sync safe on a site whose columns came from Link/Select
		# rows: all three fieldtypes map to the same column definition, so updatedb has
		# nothing to alter and the stored values stay.
		self.mock_updatedb.assert_called_with("Purchase Order")
		self.assertEqual(self._column_types(), columns_before)
		for fieldname, column_type in columns_before.items():
			with self.subTest(column=fieldname):
				self.assertEqual(column_type, get_definition("Data"))
		self.assertEqual(
			get_definition("Link", options="Sales Type"), get_definition("Data")
		)
		self.assertEqual(
			get_definition("Link", options="Attribute Value"), get_definition("Data")
		)
		self.assertEqual(get_definition("Select"), get_definition("Data"))

	def test_link_to_data_is_refused_without_ignore_validate(self):
		"""ignore_validate is load-bearing: CustomField.validate refuses Link -> Data."""
		self._set_definition(
			"custom_sales_type", fieldtype="Link", options="Sales Type"
		)

		with self.assertRaisesRegex(
			frappe.ValidationError, "cannot be changed from Link to Data"
		):
			create_custom_fields(po_dims.CUSTOM_FIELDS)

		self.assertEqual(self._definition("custom_sales_type").fieldtype, "Link")


# The 8 (doctype, fieldname) pairs the reconcile patch may touch, as approved.
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
