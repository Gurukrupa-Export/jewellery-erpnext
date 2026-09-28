# Copyright (c) 2026, Gurukrupa Exports and contributors
# For license information, please see license.txt

"""Tests for the Serial No list-view export precision override (doc_events.data_export).

The override rounds Float cells to each column's display precision before the
XLSX/CSV response is built, using ``frappe.utils.flt`` so the exported value
follows the same ``System Settings > rounding_method`` the Desk Float formatter
applies. These tests pin that behaviour and the passthrough/permission contract
for every other doctype.

Most classes here are mock-only and keep ``setUpClass: pass`` (the house pattern).
``TestSerialNoExportAgainstRealExporter`` is the exception: it writes a Serial No,
so it calls ``super().setUpClass()`` to keep ``IntegrationTestCase``'s rollback
cleanup and leaves nothing on the site.
"""

from unittest.mock import patch

import frappe
from frappe.core.doctype.data_import.exporter import Exporter
from frappe.model.meta import get_field_precision
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doc_events.data_export import (
	_round_float_cells,
	download_template,
)

# (value, precision) -> expected after flt(), per rounding method. Computed from
# frappe.utils.flt against the real Frappe helpers so the tests lock the exact
# behaviour of each system policy, including the half-way ties that distinguish
# them (P1 regression: Python round() ties-to-even diverged on 2.675).
_FLOAT_PRECISION = 2  # enforced via frappe.db.get_default in the tests below
_ROUNDING_TRUTH = {
	"Banker's Rounding (legacy)": {
		(25.3796, 3): 25.38,
		(2.675, 2): 2.68,
		(1.005, 2): 1.01,
		(-2.675, 2): -2.67,
		(0.0049, 2): 0.0,
		(-0.0049, 2): 0.0,
		(1.0000, 3): 1.0,
		(25.3800, 3): 25.38,
	},
	"Banker's Rounding": {
		(25.3796, 3): 25.38,
		(2.675, 2): 2.68,
		(1.005, 2): 1.0,
		(-2.675, 2): -2.68,
		(0.0049, 2): 0.0,
		(-0.0049, 2): 0.0,
		(1.0000, 3): 1.0,
		(25.3800, 3): 25.38,
	},
	"Commercial Rounding": {
		(25.3796, 3): 25.38,
		(2.675, 2): 2.68,
		(1.005, 2): 1.01,
		(-2.675, 2): -2.68,
		(0.0049, 2): 0.0,
		(-0.0049, 2): 0.0,
		(1.0000, 3): 1.0,
		(25.3800, 3): 25.38,
	},
}


def _field(fieldname, fieldtype="Float", precision=None):
	return frappe._dict(fieldname=fieldname, fieldtype=fieldtype, precision=precision)


class FakeExporter:
	"""Minimal stand-in for frappe Exporter: only fields + csv_array (+ a header)."""

	def __init__(self, fields, rows):
		self.fields = fields
		self.csv_array = [[f.fieldname for f in fields], *[list(r) for r in rows]]
		self.response_built = False

	def build_response(self):
		self.response_built = True


class TestRoundFloatCells(IntegrationTestCase):
	"""The pure helper: precision resolution + flt() rounding per system policy."""

	@classmethod
	def setUpClass(cls):
		pass

	def _round(self, values, fieldname="custom_gross_wt", precision=None):
		"""Round ``values`` (one per data row) inside a Data + Float two-column grid.

		Rows carry one cell per field, exactly like ``Exporter.csv_array``: the
		non-Float identifier cell is left untouched and only the Float cell rounds.
		The rounding method is pinned so the expected values hold on any site (CI's
		test_site defaults to the legacy method, which rounds -2.675 to -2.67).
		"""
		if not isinstance(values, (list, tuple)):
			values = [values]
		fields = [_field("serial_no", "Data"), _field(fieldname, precision=precision)]
		rows = [[f"row-{i}", value] for i, value in enumerate(values)]
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.data_export."
			"frappe.db.get_default",
			return_value=str(_FLOAT_PRECISION),
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.data_export."
			"frappe.get_system_settings",
			return_value="Banker's Rounding",
		):
			_round_float_cells(fields, rows)
		return [row[1] for row in rows]

	def test_normal_precision_case_matches_desk_display(self):
		# The reported bug: 25.3796 stored, 25.38 shown, export must be 25.38.
		self.assertEqual(self._round(25.3796), [25.38])

	def test_midpoint_tie_uses_flt_not_round(self):
		# round(2.675, 2) == 2.67 (ties-to-even on the binary float); the Desk
		# formatter and flt() both produce 2.68. This is the P1 blocker regression.
		self.assertEqual(self._round(2.675), [2.68])
		self.assertNotEqual(self._round(2.675)[0], round(2.675, 2))

	def test_every_rounding_method_matches_desk_policy(self):
		# The helper reads System Settings; each policy must round identically to
		# the client-side formatter that shares the same algorithm.
		for method, cases in _ROUNDING_TRUTH.items():
			for (value, precision), expected in cases.items():
				with self.subTest(method=method, value=value, precision=precision):
					fields = [
						_field("serial_no", "Data"),
						_field("custom_gross_wt", precision=precision),
					]
					rows = [["s", value]]
					with patch(
						"jewellery_erpnext.jewellery_erpnext.doc_events.data_export."
						"frappe.get_system_settings",
						return_value=method,
					):
						_round_float_cells(fields, rows)
					self.assertEqual(rows[0][1], expected)

	def test_negative_values(self):
		# -2.675 at precision 2 rounds away/toward per the active policy, never via
		# a bare round() call.
		self.assertEqual(self._round(-2.675), [-2.68])

	def test_near_zero_rounds_to_zero(self):
		self.assertEqual(self._round([0.0049, -0.0049]), [0.0, 0.0])

	def test_exact_values_are_unchanged(self):
		self.assertEqual(self._round(25.3800, precision=3), [25.38])
		self.assertEqual(self._round(1.0000, precision=3), [1.0])

	def test_explicit_field_precision_overrides_system_float_precision(self):
		# Field says precision 2 while the system default is 3: the field wins,
		# exactly as the Desk formatter resolves display precision. 25.376 at
		# precision 2 is 25.38; at precision 3 it stays 25.376.
		fields = [_field("serial_no", "Data"), _field("custom_gross_wt", precision=2)]
		rows = [["s", 25.376]]
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.data_export."
			"frappe.db.get_default",
			return_value="3",
		):
			_round_float_cells(fields, rows)
		self.assertEqual(rows[0][1], 25.38)

	def test_missing_field_precision_falls_back_to_system_float_precision(self):
		rows = [["s", 25.376]]
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.data_export."
			"frappe.db.get_default",
			return_value="3",
		):
			_round_float_cells(
				[_field("serial_no", "Data"), _field("custom_gross_wt")], rows
			)
		self.assertEqual(rows[0][1], 25.376)

	def test_non_float_columns_and_cells_are_untouched(self):
		fields = [
			_field("serial_no", "Data"),
			_field("item_code", "Link"),
			_field("custom_gross_wt", "Float"),
			_field(
				"custom_rate", "Currency"
			),  # excluded like the module docstring says
		]
		rows = [
			["serial-1", "ITEM-001", 25.3796, 34.556],
			["serial-2", "", "25.3796", "txt"],  # strings must survive verbatim
		]
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.data_export."
			"frappe.db.get_default",
			return_value=str(_FLOAT_PRECISION),
		):
			_round_float_cells(fields, rows)
		self.assertEqual(rows[0][0], "serial-1")
		self.assertEqual(rows[0][1], "ITEM-001")
		self.assertEqual(rows[0][2], 25.38)  # Float only
		self.assertEqual(rows[0][3], 34.556)  # Currency left raw
		self.assertListEqual(rows[1], ["serial-2", "", "25.3796", "txt"])

	def test_child_table_rows_keep_parent_cells_blank(self):
		# Child rows leave parent cells as empty strings, which the float guard
		# skips; their own Float cells are still rounded.
		fields = [_field("serial_no", "Data"), _field("custom_gross_wt")]
		rows = [["", 25.3796], ["", 12.3456]]
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.data_export."
			"frappe.db.get_default",
			return_value=str(_FLOAT_PRECISION),
		):
			_round_float_cells(fields, rows)
		self.assertEqual(rows[0][0], "")
		self.assertEqual(rows[0][1], 25.38)
		self.assertEqual(rows[1][1], 12.35)

	def tearDown(self):
		return super().tearDown()


class TestSerialNoExportPermissions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.data_export.frappe.has_permission",
		side_effect=frappe.PermissionError("no read"),
	)
	def test_missing_read_permission_raises_before_anything_else(self, mock_perm):
		with self.assertRaises(frappe.PermissionError):
			download_template("Serial No", export_fields=frappe.as_json({}))
		mock_perm.assert_called_once_with("Serial No", "read", throw=True)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.data_export.frappe.has_permission",
		return_value=True,
	)
	def test_read_gate_runs_even_for_other_doctypes(self, mock_perm):
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.data_export."
			"original_download_template",
			return_value=None,
		):
			download_template("Item", export_fields=frappe.as_json({}))
		mock_perm.assert_called_once_with("Item", "read", throw=True)

	def tearDown(self):
		return super().tearDown()


class TestSerialNoExportPassthrough(IntegrationTestCase):
	"""Non-Serial doctypes must hand off unchanged to the upstream endpoint."""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.data_export.frappe.has_permission",
		return_value=True,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.data_export."
		"original_download_template"
	)
	def test_delegates_every_argument_and_propagates_response(
		self, mock_orig, mock_perm
	):
		mock_orig.return_value = "ORIGINAL-RESPONSE"
		export_fields = frappe.as_json({"Item": ["item_name"]})
		export_filters = frappe.as_json({"item_group": "Jewellery"})
		result = download_template(
			"Item",
			export_fields=export_fields,
			export_records="all",
			export_filters=export_filters,
			file_type="Excel",
		)
		mock_orig.assert_called_once_with(
			"Item",
			export_fields=export_fields,
			export_records="all",
			export_filters=export_filters,
			file_type="Excel",
		)
		self.assertEqual(result, "ORIGINAL-RESPONSE")

	def tearDown(self):
		return super().tearDown()


class TestSerialNoExportEndpoint(IntegrationTestCase):
	"""Orchestration: exporter construction flags + rounding before build_response."""

	@classmethod
	def setUpClass(cls):
		pass

	_SERIAL_FIELDS = ["name", "custom_gross_wt"]

	def _patch_harness(self):
		"""Patch the DB/endpoint dependencies; returns (mocks, stop-callables)."""
		ctx = {
			"has_permission": patch(
				"jewellery_erpnext.jewellery_erpnext.doc_events.data_export."
				"frappe.has_permission",
				return_value=True,
			),
			"user_settings": patch(
				"jewellery_erpnext.jewellery_erpnext.doc_events.data_export."
				"get_user_settings",
				return_value="{}",
			),
			"float_precision": patch(
				"jewellery_erpnext.jewellery_erpnext.doc_events.data_export."
				"frappe.db.get_default",
				return_value=str(_FLOAT_PRECISION),
			),
			"exporter": patch(
				"jewellery_erpnext.jewellery_erpnext.doc_events.data_export.Exporter"
			),
			# Patched so the sort tests assert the mirror's own branching and never
			# depend on a Custom Field being present on the site under test.
			# Serial No.custom_gross_wt is declared only in custom_fields/serial_no.json,
			# which nothing applies -- migrate.after_migrate is disabled (hooks.py) -- so
			# it reaches CI's test_site only via the external git_action_v16 Custom Field
			# fixture. That holds today, but it is not this repo's to guarantee.
			"get_meta": patch(
				"jewellery_erpnext.jewellery_erpnext.doc_events.data_export."
				"frappe.get_meta"
			),
		}
		mocks = {name: m.start() for name, m in ctx.items()}
		return mocks, [m.stop for m in ctx.values()]

	def _export_fields(self):
		return frappe.as_json({"Serial No": self._SERIAL_FIELDS})

	def test_csv_rounds_float_cells_then_builds_response(self):
		mocks, stops = self._patch_harness()
		try:
			fake = FakeExporter(
				[_field("name", "Data"), _field("custom_gross_wt")],
				[["KLHG42F0177", 25.3796]],
			)
			mocks["exporter"].return_value = fake
			download_template(
				"Serial No",
				export_fields=self._export_fields(),
				export_records="all",
				file_type="CSV",
			)
			self.assertEqual(fake.csv_array[0][1], "custom_gross_wt")  # header intact
			self.assertEqual(fake.csv_array[1][1], 25.38)
			self.assertIsInstance(fake.csv_array[1][1], float)
			self.assertTrue(fake.response_built)
		finally:
			for stop in stops:
				stop()

	def test_excel_rounds_identically_and_stays_numeric(self):
		mocks, stops = self._patch_harness()
		try:
			fake = FakeExporter(
				[_field("name", "Data"), _field("custom_gross_wt")],
				[["KLHG42F0177", 25.3796]],
			)
			mocks["exporter"].return_value = fake
			download_template(
				"Serial No",
				export_fields=self._export_fields(),
				export_records="all",
				file_type="Excel",
			)
			# The rounding is file_type-agnostic: the numeric cell is rounded, not
			# coerced to a string, so XLSX keeps a real number cell.
			self.assertEqual(fake.csv_array[1][1], 25.38)
			self.assertIsInstance(fake.csv_array[1][1], float)
			self.assertTrue(fake.response_built)
		finally:
			for stop in stops:
				stop()

	def test_blank_template_has_no_data_rows_to_round(self):
		mocks, stops = self._patch_harness()
		try:
			fake = FakeExporter([_field("name", "Data"), _field("custom_gross_wt")], [])
			mocks["exporter"].return_value = fake
			download_template(
				"Serial No",
				export_fields=self._export_fields(),
				export_records="blank_template",
				file_type="CSV",
			)
			# csv_array holds only the header after the call: the rounding loop had
			# no data rows, and build_response adds the template filler itself.
			self.assertEqual(fake.csv_array, [["name", "custom_gross_wt"]])
			_, kwargs = mocks["exporter"].call_args
			self.assertFalse(kwargs["export_data"])
			self.assertIsNone(kwargs["export_page_length"])
			self.assertTrue(fake.response_built)
		finally:
			for stop in stops:
				stop()

	def test_5_records_sets_export_page_length(self):
		mocks, stops = self._patch_harness()
		try:
			fake = FakeExporter([_field("name", "Data"), _field("custom_gross_wt")], [])
			mocks["exporter"].return_value = fake
			download_template(
				"Serial No",
				export_fields=self._export_fields(),
				export_records="5_records",
			)
			_, kwargs = mocks["exporter"].call_args
			self.assertEqual(kwargs["export_page_length"], 5)
			self.assertTrue(kwargs["export_data"])
		finally:
			for stop in stops:
				stop()

	def test_filtered_export_parses_and_forwards_filters(self):
		mocks, stops = self._patch_harness()
		try:
			fake = FakeExporter([_field("name", "Data"), _field("custom_gross_wt")], [])
			mocks["exporter"].return_value = fake
			filters = {"custom_gross_wt": [">", 10]}
			download_template(
				"Serial No",
				export_fields=self._export_fields(),
				export_records="all",
				export_filters=frappe.as_json(filters),
			)
			_, kwargs = mocks["exporter"].call_args
			self.assertEqual(kwargs["export_filters"], filters)
			self.assertTrue(kwargs["export_data"])
		finally:
			for stop in stops:
				stop()

	def test_respects_list_sort_setting(self):
		mocks, stops = self._patch_harness()
		try:
			# get_meta is mocked, so this pins the mirror's branching only: a sort_by
			# that resolves to a DocField is forwarded as "<field> <order>". It must
			# not depend on custom_gross_wt existing on the site under test -- see the
			# note on the "get_meta" entry in _patch_harness.
			mocks["get_meta"].return_value.get_field.return_value = _field(
				"custom_gross_wt"
			)
			mocks["user_settings"].return_value = frappe.as_json(
				{"List": {"sort_by": "custom_gross_wt", "sort_order": "asc"}}
			)
			fake = FakeExporter([_field("name", "Data"), _field("custom_gross_wt")], [])
			mocks["exporter"].return_value = fake
			download_template(
				"Serial No",
				export_fields=self._export_fields(),
				export_records="all",
			)
			_, kwargs = mocks["exporter"].call_args
			self.assertEqual(kwargs["order_by"], "custom_gross_wt asc")
		finally:
			for stop in stops:
				stop()

	def test_unknown_sort_by_is_nulled(self):
		# Meta.get_field() returns None for default fields ("name", "modified") and for
		# a stale user setting naming a dropped column. Either way order_by must be
		# None, not a SQL fragment naming a column that is not in the query.
		mocks, stops = self._patch_harness()
		try:
			mocks["get_meta"].return_value.get_field.return_value = None
			mocks["user_settings"].return_value = frappe.as_json(
				{"List": {"sort_by": "name", "sort_order": "asc"}}
			)
			fake = FakeExporter([_field("name", "Data"), _field("custom_gross_wt")], [])
			mocks["exporter"].return_value = fake
			download_template(
				"Serial No",
				export_fields=self._export_fields(),
				export_records="all",
			)
			_, kwargs = mocks["exporter"].call_args
			self.assertIsNone(kwargs["order_by"])
		finally:
			for stop in stops:
				stop()

	def test_invalid_sort_order_is_nulled(self):
		mocks, stops = self._patch_harness()
		try:
			mocks["get_meta"].return_value.get_field.return_value = _field(
				"custom_gross_wt"
			)
			mocks["user_settings"].return_value = frappe.as_json(
				{"List": {"sort_by": "custom_gross_wt", "sort_order": "sideways"}}
			)
			fake = FakeExporter([_field("name", "Data"), _field("custom_gross_wt")], [])
			mocks["exporter"].return_value = fake
			download_template(
				"Serial No",
				export_fields=self._export_fields(),
				export_records="all",
			)
			_, kwargs = mocks["exporter"].call_args
			self.assertIsNone(kwargs["order_by"])
		finally:
			for stop in stops:
				stop()

	def tearDown(self):
		return super().tearDown()


class TestSerialNoExportAgainstRealExporter(IntegrationTestCase):
	"""One pass over a real ``Exporter`` -- the only class here that mocks nothing.

	Every other endpoint test runs against ``FakeExporter``, which hard-codes the
	contracts the production code leans on. This class asserts them against the real
	class, so a frappe upgrade that changes ``Exporter`` fails here instead of
	silently corrupting an export:

	  * ``add_header`` appends exactly one row, so ``csv_array[1:]`` is data-only
	    (exporter.py:229)
	  * every row is ``len(fields)`` wide, which is what makes ``fields[i]`` line up
	    with ``row[i]`` (exporter.py:152-157)
	  * ``add_data`` extends with the same list objects, so the in-place mutation in
	    ``_round_float_cells`` reaches the emitted file (exporter.py:251)
	  * MariaDB ``decimal(21,9)`` comes back as a Python ``float``, so the
	    ``isinstance(value, float)`` guard fires (mariadb/database.py:165)
	"""

	SERIAL_NO = "_T-GK-EXPORT-PRECISION-001"
	GROSS_WT = 25.3796

	@classmethod
	def setUpClass(cls):
		# NOT the bare `pass` the mock-only classes above use: this one writes a
		# Serial No, so it needs IntegrationTestCase's addClassCleanup(_rollback_db)
		# to roll it back. Nothing here commits.
		super().setUpClass()

		# Skip rather than fail when the site has no Serial No custom field / no Item
		# / no Company: a provisioning gap is not a defect in this code, and the
		# mock-based classes above already cover the logic either way.
		cls.item_code = frappe.db.get_value("Item", {"disabled": 0}, "name")
		cls.company = frappe.db.get_value("Company", {}, "name")
		cls.runnable = bool(
			cls.item_code
			and cls.company
			and frappe.db.has_column("Serial No", "custom_gross_wt")
		)
		if not cls.runnable:
			return

		if not frappe.db.exists("Serial No", cls.SERIAL_NO):
			frappe.get_doc(
				{
					"doctype": "Serial No",
					"serial_no": cls.SERIAL_NO,
					"item_code": cls.item_code,
					"company": cls.company,
				}
			).insert(ignore_permissions=True)

		# Written past the ORM on purpose: custom_gross_wt is read_only and a
		# fetch_from mirror of custom_bom_no.gross_weight, so a plain save would not
		# keep an independent value. The Exporter reads the column, not the doc.
		frappe.db.set_value(
			"Serial No",
			cls.SERIAL_NO,
			"custom_gross_wt",
			cls.GROSS_WT,
			update_modified=False,
		)

	def setUp(self):
		if not self.runnable:
			self.skipTest(
				"Serial No.custom_gross_wt / Item / Company not provisioned on this site"
			)

	def _export(self):
		return Exporter(
			"Serial No",
			export_fields={"Serial No": ["name", "custom_gross_wt"]},
			export_data=True,
			export_filters={"name": self.SERIAL_NO},
			file_type="CSV",
		)

	def test_header_is_exactly_one_row_and_fields_are_index_aligned(self):
		exporter = self._export()
		self.assertEqual(
			len(exporter.csv_array), 2, "one header row + one data row expected"
		)
		# Width, not content: add_header writes df.label ("Gross Wt"), not df.fieldname,
		# so csv_array[0] is never index-comparable to fields by name. The production
		# code only relies on row 0 being the single header and on every row being
		# len(fields) wide -- that is what makes fields[i] line up with row[i].
		self.assertEqual(len(exporter.csv_array[0]), len(exporter.fields))
		self.assertEqual(len(exporter.csv_array[1]), len(exporter.fields))
		self.assertEqual(
			[f.label for f in exporter.fields], list(exporter.csv_array[0])
		)

	def test_float_column_arrives_as_a_python_float(self):
		exporter = self._export()
		index = [f.fieldname for f in exporter.fields].index("custom_gross_wt")
		# If the DB layer ever hands back Decimal, the isinstance guard in
		# _round_float_cells stops firing and every export goes out unrounded.
		self.assertIsInstance(exporter.csv_array[1][index], float)
		self.assertEqual(exporter.csv_array[1][index], self.GROSS_WT)

	def test_in_place_rounding_lands_in_the_exported_grid(self):
		exporter = self._export()
		index = [f.fieldname for f in exporter.fields].index("custom_gross_wt")
		header = list(exporter.csv_array[0])

		_round_float_cells(exporter.fields, exporter.csv_array[1:])

		expected = flt(self.GROSS_WT, get_field_precision(exporter.fields[index]))
		self.assertEqual(exporter.csv_array[1][index], expected)
		self.assertIsInstance(exporter.csv_array[1][index], float)
		self.assertEqual(exporter.csv_array[0], header, "header must not be rounded")
		self.assertEqual(len(exporter.csv_array), 2, "no rows added or dropped")

	def tearDown(self):
		return super().tearDown()
