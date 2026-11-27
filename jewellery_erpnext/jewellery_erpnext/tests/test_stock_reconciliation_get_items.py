# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Stock Reconciliation "Fetch Items from Warehouse": several items (or all) at once and, with
Main Slip ticked, the selected warehouse's employee warehouses.

Pure-logic: every read the code makes is answered by keyed stubs; nothing touches the database
and nothing is written.
"""

import unittest
from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.stock_reconciliation import (
	stock_reonciliation as sr,
)


def _system_settings(key=None, *args, **kwargs):
	return {"rounding_method": "Banker's Rounding", "time_zone": "Asia/Kolkata"}.get(
		key
	)


class _Base(unittest.TestCase):
	def setUp(self):
		self.stack = ExitStack()
		self.addCleanup(self.stack.close)
		self.stack.enter_context(
			patch.object(frappe, "get_system_settings", side_effect=_system_settings)
		)
		self.assertEqual(
			flt(1.0, 3), 1.0, "flt(x, 3) is zeroing -- the harness cannot fail"
		)


class TestGetItemsForReconciliation(_Base):
	ARGS = {
		"warehouse": "Central RM - GEPL",
		"posting_date": "2026-11-17",
		"posting_time": "10:00:00",
		"company": "Gurukrupa Export Private Limited",
	}

	def _calls(self, **kwargs):
		calls = []

		def fake_get_items(**args):
			self.ignore_empty_stock = args["ignore_empty_stock"]
			calls.append(args.get("item_code"))
			return [
				{
					"item_code": args.get("item_code") or "ALL",
					"warehouse": args["warehouse"],
				}
			]

		self.stack.enter_context(
			patch.object(sr, "erpnext_get_items", side_effect=fake_get_items)
		)
		rows = sr.get_items_for_reconciliation(**self.ARGS, **kwargs)
		return calls, rows

	def test_selected_items_once_each(self):
		calls, rows = self._calls(item_codes='["M1", "F1", "M1"]')
		self.assertEqual(calls, ["M1", "F1"])
		self.assertEqual([r["item_code"] for r in rows], ["M1", "F1"])

	def test_select_all_is_one_call_for_the_warehouse(self):
		calls, _rows = self._calls(item_codes=["M1"], select_all=1)
		self.assertEqual(calls, [None])

	def test_ignore_empty_stock_defaults_on(self):
		self._calls(item_codes=["M1"])
		self.assertEqual(self.ignore_empty_stock, 1)

	def test_ignore_empty_stock_can_be_unticked(self):
		self._calls(item_codes=["M1"], ignore_empty_stock="0")
		self.assertEqual(self.ignore_empty_stock, 0)

	def test_nothing_selected(self):
		with self.assertRaises(frappe.ValidationError):
			self._calls(item_codes="[]")


class TestEmployeeWarehouses(_Base):
	"""Main Slip: a department warehouse also brings in its employee warehouses."""

	DEPARTMENT_WAREHOUSES = {
		"Waxing RM - GEPL": "Raw Material",
		"Waxing RSV - GEPL": "Reserve",
		"Waxing Scrap - GEPL": "Scrap",
	}

	def _lookup(self, warehouse, has_field=True, details=None):
		if details is None:
			details = {
				"department": "Waxing - GEPL",
				"warehouse_type": self.DEPARTMENT_WAREHOUSES.get(
					warehouse, "Raw Material"
				),
				"company": "Gurukrupa Export Private Limited",
			}
		self.list_calls = []

		def fake_get_list(doctype, filters=None, **kwargs):
			self.list_calls.append((doctype, filters, kwargs))
			return ["EMP-1 WH", "EMP-2 WH"]

		with ExitStack() as stack:
			stack.enter_context(patch.object(sr, "_has_field", return_value=has_field))
			stack.enter_context(
				patch.object(
					frappe.db,
					"get_value",
					return_value=frappe._dict(details) if details else None,
				)
			)
			stack.enter_context(
				patch.object(frappe, "get_list", side_effect=fake_get_list)
			)
			return sr.employee_warehouses(warehouse)

	def test_matches_employee_type_and_parent_department(self):
		for warehouse, warehouse_type in self.DEPARTMENT_WAREHOUSES.items():
			with self.subTest(warehouse=warehouse):
				self.assertEqual(self._lookup(warehouse), ["EMP-1 WH", "EMP-2 WH"])
				doctype, filters, kwargs = self.list_calls[0]
				self.assertEqual(doctype, "Warehouse")
				self.assertEqual(
					filters,
					{
						"disabled": 0,
						"is_group": 0,
						"employee": ["is", "set"],
						"warehouse_type": warehouse_type,
						"company": "Gurukrupa Export Private Limited",
						"custom_parent_department_warehouse": "Waxing - GEPL",
						"name": ["!=", warehouse],
					},
				)
				self.assertEqual(kwargs["pluck"], "name")

	def test_warehouse_without_department_or_type(self):
		for details in (
			{"department": None, "warehouse_type": "Raw Material", "company": "C"},
			{"department": "Waxing - GEPL", "warehouse_type": None, "company": "C"},
			{},
		):
			with self.subTest(details=details):
				self.assertEqual(self._lookup("X", details=details), [])
				self.assertEqual(self.list_calls, [])

	def test_site_without_the_parent_department_field(self):
		self.assertEqual(self._lookup("Waxing RM - GEPL", has_field=False), [])
		self.assertEqual(self.list_calls, [])

	def test_main_slip_unticked_does_no_lookup(self):
		with patch.object(sr, "employee_warehouses") as lookup:
			self.assertEqual(
				sr._fetch_warehouses("Waxing RM - GEPL", 0), ["Waxing RM - GEPL"]
			)
			self.assertEqual(
				sr._fetch_warehouses("Waxing RM - GEPL", "0"), ["Waxing RM - GEPL"]
			)
		lookup.assert_not_called()

	def test_main_slip_ticked_adds_them_after_the_selected_warehouse(self):
		with patch.object(sr, "employee_warehouses", return_value=["EMP-1 WH"]):
			self.assertEqual(
				sr._fetch_warehouses("Waxing RM - GEPL", "1"),
				["Waxing RM - GEPL", "EMP-1 WH"],
			)


class TestMainSlipGetItems(_Base):
	ARGS = {
		"warehouse": "Waxing RM - GEPL",
		"posting_date": "2026-11-24",
		"posting_time": "10:00:00",
		"company": "Gurukrupa Export Private Limited",
	}
	EMPLOYEE_WAREHOUSES = ["EMP-1 WH", "EMP-2 WH"]
	# EMP-1 holds M1 only; EMP-2 holds M1 and F1.
	BINS = [("M1", "EMP-1 WH"), ("M1", "EMP-2 WH"), ("F1", "EMP-2 WH")]

	def setUp(self):
		super().setUp()
		self.calls = []

		def fake_get_items(**args):
			self.calls.append((args["warehouse"], args.get("item_code")))
			return [
				{
					"item_code": args.get("item_code") or "ALL",
					"warehouse": args["warehouse"],
				}
			]

		def fake_get_all(doctype, filters=None, **kwargs):
			self.assertEqual(doctype, "Bin")
			self.bin_filters = filters
			if kwargs.get("pluck") == "item_code":
				return ["F1", "M1"]
			codes, warehouses = filters["item_code"][1], filters["warehouse"][1]
			return [
				frappe._dict(item_code=i, warehouse=w)
				for i, w in self.BINS
				if i in codes and w in warehouses
			]

		self.stack.enter_context(
			patch.object(sr, "erpnext_get_items", side_effect=fake_get_items)
		)
		self.stack.enter_context(
			patch.object(
				sr, "employee_warehouses", return_value=self.EMPLOYEE_WAREHOUSES
			)
		)
		self.stack.enter_context(
			patch.object(frappe, "get_all", side_effect=fake_get_all)
		)

	def test_select_all_is_item_wise_in_employee_warehouses(self):
		# One whole-warehouse call for the selected warehouse; employee warehouses get one
		# item-wise call per Bin (the whole-warehouse report is ~200 s each).
		bins = self.stack.enter_context(
			patch.object(sr, "_employee_warehouse_bins", return_value=self.BINS)
		)
		rows = sr.get_items_for_reconciliation(**self.ARGS, select_all=1, main_slip=1)
		bins.assert_called_once_with(self.EMPLOYEE_WAREHOUSES, 1)
		self.assertEqual(
			self.calls,
			[
				("Waxing RM - GEPL", None),
				("EMP-1 WH", "M1"),
				("EMP-2 WH", "M1"),
				("EMP-2 WH", "F1"),
			],
		)
		self.assertEqual(
			[r["warehouse"] for r in rows],
			["Waxing RM - GEPL", "EMP-1 WH", "EMP-2 WH", "EMP-2 WH"],
		)

	def test_select_all_passes_ignore_empty_stock_to_the_bin_lookup(self):
		bins = self.stack.enter_context(
			patch.object(sr, "_employee_warehouse_bins", return_value=[])
		)
		sr.get_items_for_reconciliation(
			**self.ARGS, select_all=1, main_slip=1, ignore_empty_stock="0"
		)
		bins.assert_called_once_with(self.EMPLOYEE_WAREHOUSES, 0)

	def test_no_employee_warehouses_no_bin_query(self):
		self.assertEqual(sr._employee_warehouse_bins([], 1), [])

	def test_selected_items_only_from_employee_warehouses_that_hold_them(self):
		sr.get_items_for_reconciliation(
			**self.ARGS,
			item_codes='["M1", "F1", "M1"]',
			ignore_empty_stock=0,
			main_slip=1,
		)
		self.assertEqual(
			self.calls,
			[
				("Waxing RM - GEPL", "M1"),
				("EMP-1 WH", "M1"),
				("EMP-2 WH", "M1"),
				("Waxing RM - GEPL", "F1"),
				("EMP-2 WH", "F1"),
			],
		)

	def test_main_slip_unticked_is_the_selected_warehouse_only(self):
		sr.get_items_for_reconciliation(**self.ARGS, select_all=1)
		sr.get_items_for_reconciliation(**self.ARGS, item_codes=["M1"], main_slip=0)
		self.assertEqual(
			self.calls, [("Waxing RM - GEPL", None), ("Waxing RM - GEPL", "M1")]
		)

	def _item_list(self, main_slip):
		self.stack.enter_context(
			patch.object(frappe, "has_permission", return_value=True)
		)
		self.stack.enter_context(patch.object(sr, "bulk_map", return_value={}))
		return sr.get_warehouse_items("Waxing RM - GEPL", txt="1", main_slip=main_slip)

	def test_item_list_includes_employee_warehouses(self):
		options = self._item_list(main_slip=1)
		self.assertEqual(
			self.bin_filters["warehouse"],
			["in", ["Waxing RM - GEPL", "EMP-1 WH", "EMP-2 WH"]],
		)
		self.assertEqual(self.bin_filters["item_code"], ["like", "%1%"])
		self.assertEqual([o["value"] for o in options], ["F1", "M1"])

	def test_item_list_without_main_slip(self):
		self._item_list(main_slip=0)
		self.assertEqual(self.bin_filters["warehouse"], ["in", ["Waxing RM - GEPL"]])
