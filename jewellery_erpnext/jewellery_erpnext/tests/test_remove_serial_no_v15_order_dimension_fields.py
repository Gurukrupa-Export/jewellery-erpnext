# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Pins ``remove_serial_no_v15_order_dimension_fields``.

gepl-prod showed Serial No's Sales Type / Order Type / Flow Type twice: the v15 ``sales_type`` /
``order_type`` / ``flow_type`` Custom Fields next to the v16 ``custom_*`` ones. The patch copies the
old values into the kept field where it is blank, prunes the old names from ``field_order``,
re-anchors dependants and deletes the old rows. DB-free: frappe is faked throughout.
"""

import io
from contextlib import redirect_stdout
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import (
	remove_serial_no_v15_order_dimension_fields as remove_patch,
)

OLD_ROWS = {
	"sales_type": frappe._dict(
		name="Serial No-sales_type", insert_after="custom_bom_no"
	),
	"order_type": frappe._dict(name="Serial No-order_type", insert_after="sales_type"),
	"flow_type": frappe._dict(name="Serial No-flow_type", insert_after="order_type"),
}
COLUMNS = {
	"sales_type",
	"order_type",
	"flow_type",
	"custom_sales_type",
	"custom_order_type",
	"custom_flow_type",
}


class TestRemoveSerialNoV15OrderDimensionFields(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, rows=OLD_ROWS, columns=COLUMNS, blanks=None, dependants=None):
		"""Run execute() against faked metadata.

		``rows``: old fieldname -> Custom Field row. ``columns``: Serial No columns that exist.
		``blanks``: kept fieldname -> rows the backfill would copy (default 0).
		``dependants``: old fieldname -> Custom Field names anchored on it.
		"""
		blanks = blanks or {}
		dependants = dependants or {}

		def get_value(doctype, filters=None, *args, **kwargs):
			if doctype == "Custom Field" and filters.get("dt") == "Serial No":
				return rows.get(filters["fieldname"])
			raise AssertionError(f"unexpected get_value({doctype!r}, {filters!r})")

		def has_column(doctype, column):
			self.assertEqual(doctype, "Serial No")
			return column in columns

		def get_all(doctype, filters=None, *args, **kwargs):
			if doctype == "Custom Field" and filters.get("dt") == "Serial No":
				return list(dependants.get(filters["insert_after"], ()))
			raise AssertionError(f"unexpected get_all({doctype!r}, {filters!r})")

		def sql(query, *args, **kwargs):
			if query.startswith("SELECT COUNT(*)"):
				kept = query.split("IFNULL(`", 1)[1].split("`", 1)[0]
				return [[blanks.get(kept, 0)]]
			return []

		self.mocks = {}
		for name, target, kwargs in (
			("get_value", remove_patch.frappe.db, {"side_effect": get_value}),
			("has_column", remove_patch.frappe.db, {"side_effect": has_column}),
			("get_all", remove_patch.frappe, {"side_effect": get_all}),
			("sql", remove_patch.frappe.db, {"side_effect": sql}),
			("set_value", remove_patch.frappe.db, {}),
			("delete_doc", remove_patch.frappe, {}),
			("clear_cache", remove_patch.frappe, {}),
			("logger", remove_patch.frappe, {}),
			("strip_field_order_entries", remove_patch, {}),
		):
			patcher = patch.object(target, name, **kwargs)
			self.mocks[name] = patcher.start()
			self.addCleanup(patcher.stop)

		out = io.StringIO()
		with redirect_stdout(out):
			remove_patch.execute()
		self.printed = out.getvalue()

	def _updates(self):
		return [
			c.args[0]
			for c in self.mocks["sql"].call_args_list
			if c.args[0].startswith("UPDATE")
		]

	def _deleted(self):
		return [c.args[1] for c in self.mocks["delete_doc"].call_args_list]

	def test_old_values_are_copied_into_the_kept_field_only_where_it_is_blank(self):
		self._run(blanks={"custom_sales_type": 210, "custom_order_type": 210})

		self.assertEqual(
			self._updates(),
			[
				"UPDATE `tabSerial No` SET `custom_sales_type` = `sales_type` WHERE "
				"IFNULL(`custom_sales_type`, '') = '' AND IFNULL(`sales_type`, '') != ''",
				"UPDATE `tabSerial No` SET `custom_order_type` = `order_type` WHERE "
				"IFNULL(`custom_order_type`, '') = '' AND IFNULL(`order_type`, '') != ''",
			],
		)

	def test_the_three_old_rows_are_deleted_and_the_form_order_pruned(self):
		self._run()

		self.assertEqual(
			self._deleted(),
			["Serial No-sales_type", "Serial No-order_type", "Serial No-flow_type"],
		)
		for c in self.mocks["delete_doc"].call_args_list:
			self.assertEqual(c.args[0], "Custom Field")
		self.mocks["strip_field_order_entries"].assert_called_once_with(
			"Serial No", ["sales_type", "order_type", "flow_type"]
		)
		self.mocks["clear_cache"].assert_called_once_with(doctype="Serial No")
		self.assertIn("sales_type, order_type, flow_type", self.printed)

	def test_the_kept_custom_fields_are_never_touched(self):
		self._run()

		for name in self._deleted():
			self.assertNotIn("custom_", name)

	def test_a_second_run_finds_nothing_to_do(self):
		self._run(rows={})

		self.mocks["sql"].assert_not_called()
		self.mocks["delete_doc"].assert_not_called()
		self.mocks["strip_field_order_entries"].assert_not_called()
		self.mocks["clear_cache"].assert_not_called()

	def test_a_pair_without_its_kept_column_is_left_alone(self):
		self._run(columns=COLUMNS - {"custom_flow_type"})

		self.assertEqual(
			self._deleted(), ["Serial No-sales_type", "Serial No-order_type"]
		)
		self.mocks["strip_field_order_entries"].assert_called_once_with(
			"Serial No", ["sales_type", "order_type"]
		)

	def test_dependants_skip_past_every_retired_anchor(self):
		# flow_type -> order_type -> sales_type -> custom_bom_no: all three go, so a field
		# anchored on flow_type must land on custom_bom_no. The old rows chained on each other
		# are deleted, not re-pointed.
		self._run(
			dependants={
				"sales_type": ["Serial No-order_type"],
				"order_type": ["Serial No-flow_type"],
				"flow_type": ["Serial No-custom_x"],
			}
		)

		self.mocks["set_value"].assert_called_once_with(
			"Custom Field", "Serial No-custom_x", "insert_after", "custom_bom_no"
		)

	def test_a_kept_old_field_stays_a_valid_anchor(self):
		# order_type is kept (no custom_order_type column), so flow_type's dependant stops there.
		self._run(
			columns=COLUMNS - {"custom_order_type"},
			dependants={"flow_type": ["Serial No-custom_x"]},
		)

		self.mocks["set_value"].assert_called_once_with(
			"Custom Field", "Serial No-custom_x", "insert_after", "order_type"
		)
