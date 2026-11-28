# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Stock Reconciliation enhancements: Item Group summary, positive / negative difference, pure
weight, the pending-transit submit block and multi-item Get Items.

Pure-logic: every read the code makes is answered by keyed stubs; nothing touches the database
and nothing is written.
"""

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import flt

from jewellery_erpnext import hooks
from jewellery_erpnext.jewellery_erpnext.customization.stock_reconciliation import (
	stock_reonciliation as sr,
)

SR_PATH = "jewellery_erpnext.jewellery_erpnext.customization.stock_reconciliation.stock_reonciliation"

# 22KT gold and a finding at the same touch; diamonds / gemstones have no purity.
PURITY = {"M-G-22KT-91.75-Y": 91.75, "F-G-22KT-91.75-Y-CHAIN": 91.75}


def _system_settings(key=None, *args, **kwargs):
	return {"rounding_method": "Banker's Rounding", "time_zone": "Asia/Kolkata"}.get(
		key
	)


def _row(**values):
	return frappe._dict(values)


class _Meta:
	def __init__(self, fields):
		self.fields = set(fields)

	def has_field(self, fieldname):
		return fieldname in self.fields


class _Doc(SimpleNamespace):
	"""Just enough of a Stock Reconciliation for the hooks."""

	def get(self, key, default=None):
		return getattr(self, key, default)

	def set(self, key, value):
		setattr(self, key, [frappe._dict(v) for v in value])


SR_FIELDS = (
	"custom_item_group_summary",
	"custom_grand_total_gram",
	"custom_total_pure_weight",
	"custom_positive_difference_qty",
	"custom_negative_difference_qty",
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


class TestItemGroupSummary(_Base):
	def test_client_example_in_grams(self):
		rows = [
			_row(item_code="D1", item_group="Diamond - V", stock_uom="Gram", qty=150),
			_row(item_code="G1", item_group="Gemstone - V", stock_uom="Gram", qty=80),
			_row(item_code="M1", item_group="Metal - V", stock_uom="Gram", qty=250),
			_row(item_code="F1", item_group="Finding - V", stock_uom="Gram", qty=120),
		]
		summary, grand_total = sr.build_item_group_summary(rows)
		self.assertEqual(grand_total, 600)
		self.assertEqual(
			{r["item_group"]: r["total_qty"] for r in summary},
			{
				"Diamond - V": 150,
				"Gemstone - V": 80,
				"Metal - V": 250,
				"Finding - V": 120,
			},
		)

	def test_carat_converts_at_one_fifth(self):
		rows = [
			_row(item_code="D1", item_group="Diamond - V", stock_uom="Carat", qty=100),
			_row(item_code="D2", item_group="Diamond - V", stock_uom="Carat", qty=50),
			_row(item_code="M1", item_group="Metal - V", stock_uom="Gram", qty=250),
		]
		summary, grand_total = sr.build_item_group_summary(rows)
		diamond = next(r for r in summary if r["item_group"] == "Diamond - V")
		self.assertEqual(diamond["total_qty"], 150)
		self.assertEqual(diamond["total_qty_in_gram"], 30)  # 150 ct x 0.2
		self.assertEqual(grand_total, 280)

	def test_rows_without_item_and_unconvertible_uoms(self):
		rows = [
			_row(item_code=None, custom_manufacturing_work_order="MWO-1", qty=5),
			_row(
				item_code="T1", item_group="Tools & Accessories", stock_uom="Nos", qty=3
			),
			_row(item_code="M1", item_group="Metal - V", stock_uom="Gram", qty=10),
		]
		summary, grand_total = sr.build_item_group_summary(rows)
		self.assertEqual(len(summary), 2)
		tools = next(r for r in summary if r["uom"] == "Nos")
		self.assertEqual((tools["total_qty"], tools["total_qty_in_gram"]), (3, 0))
		self.assertEqual(grand_total, 10)


class TestDifferenceSummary(_Base):
	def test_client_example(self):
		# Difference = Stock Reconciliation qty (currenty_qty) - Child Stock Reconciliation qty (qty).
		rows = [
			_row(item_code="Diamond-001", qty=100, currenty_qty=120),
			_row(item_code="Diamond-002", qty=150, currenty_qty=130),
			_row(item_code="Metal-001", qty=50, currenty_qty=70),
		]
		self.assertEqual(sr.difference_summary(rows), (40, 20))
		self.assertEqual([r.diff_qty for r in rows], [20, -20, 20])

	def test_no_difference(self):
		rows = [
			_row(item_code="X", currenty_qty=4.5, qty=4.5),
			_row(qty=0, currenty_qty=0),
		]
		self.assertEqual(sr.difference_summary(rows), (0, 0))


class TestSetReconciliationSummaries(_Base):
	def _doc(self):
		return _Doc(
			meta=_Meta(SR_FIELDS),
			items=[
				_row(
					item_code="M-G-22KT-91.75-Y",
					item_group="Metal - V",
					stock_uom="Gram",
					qty=10,
				),
				_row(item_code="DIA-1", item_group=None, stock_uom=None, qty=25),
			],
			custom_stock_comparison=[
				_row(item_code="M-G-22KT-91.75-Y", qty=12, currenty_qty=10),
				_row(item_code="DIA-1", qty=20, currenty_qty=25),
			],
		)

	def _run(self, doc, existing=("custom_pure_weight", "pure_weight")):
		self.stack.enter_context(
			patch.object(
				sr,
				"_metal_items",
				side_effect=lambda codes: {c: PURITY[c] for c in codes if c in PURITY},
			)
		)
		self.stack.enter_context(
			patch.object(sr, "_has_field", side_effect=lambda dt, fn: fn in existing)
		)
		self.stack.enter_context(
			patch.object(
				sr,
				"bulk_map",
				return_value={
					"DIA-1": {"item_group": "Diamond - V", "stock_uom": "Carat"}
				},
			)
		)
		sr.set_reconciliation_summaries(doc)

	def test_values(self):
		doc = self._doc()
		self._run(doc)
		metal, diamond = doc.items
		self.assertEqual(metal.custom_pure_weight, 9.175)  # 10 g x 91.75 %
		self.assertEqual(diamond.custom_pure_weight, 0)
		self.assertEqual(doc.custom_total_pure_weight, 9.175)
		self.assertEqual(diamond.item_group, "Diamond - V")  # filled from Item
		self.assertEqual(doc.custom_grand_total_gram, 15)  # 10 g + 25 ct x 0.2
		self.assertEqual(len(doc.custom_item_group_summary), 2)
		metal_cmp, diamond_cmp = doc.custom_stock_comparison
		# Stock Reconciliation qty - Child Stock Reconciliation qty: 10 - 12, 25 - 20
		self.assertEqual((metal_cmp.diff_qty, diamond_cmp.diff_qty), (-2, 5))
		self.assertEqual(metal_cmp.pure_weight, -1.835)  # -2 g x 91.75 %
		self.assertEqual(diamond_cmp.pure_weight, 0)
		self.assertEqual(doc.custom_positive_difference_qty, 5)
		self.assertEqual(doc.custom_negative_difference_qty, 2)

	def test_unchanged_save_keeps_the_summary_rows(self):
		doc = self._doc()
		self._run(doc)
		first = doc.custom_item_group_summary
		sr.set_reconciliation_summaries(doc)
		self.assertIs(doc.custom_item_group_summary, first)
		doc.items[0].qty = 11
		sr.set_reconciliation_summaries(doc)
		self.assertIsNot(doc.custom_item_group_summary, first)

	def test_site_without_the_new_fields(self):
		doc = self._doc()
		doc.meta = _Meta(())
		self._run(doc, existing=())
		self.assertNotIn("custom_pure_weight", doc.items[0])
		self.assertFalse(hasattr(doc, "custom_item_group_summary"))
		self.assertFalse(hasattr(doc, "custom_positive_difference_qty"))
		# the signed difference is still set: it is an existing Stock Comparison field
		self.assertEqual(doc.custom_stock_comparison[1].diff_qty, 5)


LEGS = [
	# Central RM -> Waxing Transit, not yet received.
	frappe._dict(
		name="MAT-STE-1",
		item_code="M-G-22KT-91.75-Y",
		qty=12.5,
		s_warehouse="Central RM - KGJPL",
		t_warehouse="Waxing Transit - KGJPL",
		source_department="Central - KGJPL",
		transit_department="Waxing - KGJPL",
	),
]


class TestPendingTransit(_Base):
	def _validate(self, departments_by_warehouse, custom_department=None, legs=LEGS):
		doc = _Doc(
			custom_department=custom_department,
			set_warehouse=None,
			items=[_row(warehouse=w) for w in departments_by_warehouse],
		)
		self.stack.enter_context(
			patch.object(
				sr,
				"bulk_map",
				side_effect=lambda dt, names, fields: {
					n: {"department": departments_by_warehouse.get(n)} for n in names
				},
			)
		)
		self.stack.enter_context(
			patch.object(sr, "get_pending_transit_legs", return_value=legs)
		)
		sr.validate_no_pending_transit(doc)

	def test_blocks_the_sending_department(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._validate({"Central RM - KGJPL": "Central - KGJPL"})
		message = str(raised.exception)
		self.assertIn(
			"Stock Reconciliation cannot be submitted because a Transit Stock Entry is pending "
			"receipt for this department.",
			message,
		)
		# what is still in transit, so the message is actionable even without a linked receipt
		self.assertIn(
			"MAT-STE-1: M-G-22KT-91.75-Y - 12.5 still in Waxing Transit - KGJPL",
			message,
		)

	def test_blocks_the_receiving_department(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate({"Waxing RM - KGJPL": "Waxing - KGJPL"})

	def test_blocks_through_custom_department(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate({}, custom_department="Waxing - KGJPL")

	def test_other_department_passes(self):
		self._validate({"Casting RM - KGJPL": "Casting - KGJPL"})

	def test_passes_once_received(self):
		self._validate({"Central RM - KGJPL": "Central - KGJPL"}, legs=[])

	def test_hook_is_wired_on_submit(self):
		self.assertIn(
			f"{SR_PATH}.validate_no_pending_transit",
			hooks.doc_events["Stock Reconciliation"]["before_submit"],
		)
		self.assertIn(
			f"{SR_PATH}.set_reconciliation_summaries",
			hooks.doc_events["Stock Reconciliation"]["validate"],
		)


class TestGetItemsForReconciliation(_Base):
	ARGS = {
		"warehouse": "Central RM - KGJPL",
		"posting_date": "2026-11-17",
		"posting_time": "10:00:00",
		"company": "KG GK Jewellers Private Limited",
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
		"Waxing RM - KGJPL": "Raw Material",
		"Waxing RSV - KGJPL": "Reserve",
		"Waxing Scrap - KGJPL": "Scrap",
	}

	def _lookup(self, warehouse, has_field=True, details=None):
		if details is None:
			details = {
				"department": "Waxing - KGJPL",
				"warehouse_type": self.DEPARTMENT_WAREHOUSES.get(
					warehouse, "Raw Material"
				),
				"company": "KG GK Jewellers Private Limited",
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
						"company": "KG GK Jewellers Private Limited",
						"custom_parent_department_warehouse": "Waxing - KGJPL",
						"name": ["!=", warehouse],
					},
				)
				self.assertEqual(kwargs["pluck"], "name")

	def test_warehouse_without_department_or_type(self):
		for details in (
			{"department": None, "warehouse_type": "Raw Material", "company": "C"},
			{"department": "Waxing - KGJPL", "warehouse_type": None, "company": "C"},
			{},
		):
			with self.subTest(details=details):
				self.assertEqual(self._lookup("X", details=details), [])
				self.assertEqual(self.list_calls, [])

	def test_site_without_the_parent_department_field(self):
		self.assertEqual(self._lookup("Waxing RM - KGJPL", has_field=False), [])
		self.assertEqual(self.list_calls, [])

	def test_main_slip_unticked_does_no_lookup(self):
		with patch.object(sr, "employee_warehouses") as lookup:
			self.assertEqual(
				sr._fetch_warehouses("Waxing RM - KGJPL", 0), ["Waxing RM - KGJPL"]
			)
			self.assertEqual(
				sr._fetch_warehouses("Waxing RM - KGJPL", "0"), ["Waxing RM - KGJPL"]
			)
		lookup.assert_not_called()

	def test_main_slip_ticked_adds_them_after_the_selected_warehouse(self):
		with patch.object(sr, "employee_warehouses", return_value=["EMP-1 WH"]):
			self.assertEqual(
				sr._fetch_warehouses("Waxing RM - KGJPL", "1"),
				["Waxing RM - KGJPL", "EMP-1 WH"],
			)


class TestMainSlipGetItems(_Base):
	ARGS = {
		"warehouse": "Waxing RM - KGJPL",
		"posting_date": "2026-11-24",
		"posting_time": "10:00:00",
		"company": "KG GK Jewellers Private Limited",
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
				("Waxing RM - KGJPL", None),
				("EMP-1 WH", "M1"),
				("EMP-2 WH", "M1"),
				("EMP-2 WH", "F1"),
			],
		)
		self.assertEqual(
			[r["warehouse"] for r in rows],
			["Waxing RM - KGJPL", "EMP-1 WH", "EMP-2 WH", "EMP-2 WH"],
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
				("Waxing RM - KGJPL", "M1"),
				("EMP-1 WH", "M1"),
				("EMP-2 WH", "M1"),
				("Waxing RM - KGJPL", "F1"),
				("EMP-2 WH", "F1"),
			],
		)

	def test_main_slip_unticked_is_the_selected_warehouse_only(self):
		sr.get_items_for_reconciliation(**self.ARGS, select_all=1)
		sr.get_items_for_reconciliation(**self.ARGS, item_codes=["M1"], main_slip=0)
		self.assertEqual(
			self.calls, [("Waxing RM - KGJPL", None), ("Waxing RM - KGJPL", "M1")]
		)

	def _item_list(self, main_slip):
		self.stack.enter_context(
			patch.object(frappe, "has_permission", return_value=True)
		)
		self.stack.enter_context(patch.object(sr, "bulk_map", return_value={}))
		return sr.get_warehouse_items("Waxing RM - KGJPL", txt="1", main_slip=main_slip)

	def test_item_list_includes_employee_warehouses(self):
		options = self._item_list(main_slip=1)
		self.assertEqual(
			self.bin_filters["warehouse"],
			["in", ["Waxing RM - KGJPL", "EMP-1 WH", "EMP-2 WH"]],
		)
		self.assertEqual(self.bin_filters["item_code"], ["like", "%1%"])
		self.assertEqual([o["value"] for o in options], ["F1", "M1"])

	def test_item_list_without_main_slip(self):
		self._item_list(main_slip=0)
		self.assertEqual(self.bin_filters["warehouse"], ["in", ["Waxing RM - KGJPL"]])
