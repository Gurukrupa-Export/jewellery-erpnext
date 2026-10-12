# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Pins the order-dimension metadata patches.

``add_purchase_order_order_dimension_fields`` gives the Purchase Order header the four order
dimensions that ``make_subcontracting_order`` stamps from the Manufacturing Plan. The DB-free
class pins the definition and the calls the patch makes. The DB-backed class runs the real
``create_custom_fields`` against the site and pins what the patch exists for on databases that
already hold some of these rows as Link or Select: the rows become Data with no options, and
the values already stored in those columns survive.

DB-backed classes keep everything inside one transaction. ``create_custom_fields`` ends with
``frappe.db.updatedb``, which commits, and a commit inside an IntegrationTestCase defeats the
harness rollback. So ``commit`` and ``updatedb`` are stubbed, every test runs inside a savepoint
that is rolled back, and the doctype cache is cleared afterwards. The schema side is checked
directly instead: the converted columns must already have the definition a Data field gets, so
the schema sync the patch triggers on a real migrate has nothing to alter.
"""

from unittest.mock import MagicMock, patch

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
