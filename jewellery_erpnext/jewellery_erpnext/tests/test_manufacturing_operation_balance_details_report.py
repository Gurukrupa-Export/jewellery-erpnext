# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Unit tests for the Manufacturing Operation Balance Details Report.

The report was reworked to source Customer and Inventory Type from the ROW'S OWN
BATCH rather than from the Parent Manufacturing Order, which was giving every row
of an operation the same (often wrong) owner. These tests pin that, the mandated
column order, and the deliberate decision NOT to normalize batch ownership -- the
kind of call a future reader would otherwise "fix" by wiring in
``row_ownership.resolve_batch_ownership``.

Mocked/pure-logic style (see test_loss_row_ownership.py): no DB, no fixtures --
``frappe.db.get_all`` is patched with a side effect that dispatches on doctype.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.report.manufacturing_operation_balance_details_report import (
	manufacturing_operation_balance_details_report as rpt,
)

_EXPECTED_COLUMNS = [
	"manufacturing_work_order",
	"manufacturing_operation",
	"manufacturing_operation_status",
	"item_code",
	"qty",
	"pcs",
	"uom",
	"batch_no",
	"department",
	"inventory_type",
	"customer",
]

_FILTERS = frappe._dict(
	{
		"company": "Gurukrupa Exports Private Limited",
		"manufacturing_work_order": "MWO-1",
		"manufacturing_operation": "MOP-1",
	}
)


def _log(**fields):
	base = {
		"item_code": "M-G-18KT-75.4-Y",
		"manufacturing_work_order": "MWO-1",
		"manufacturing_operation": "MOP-1",
		"qty_after_transaction_batch_based": 10.0,
		"pcs_after_transaction_batch_based": 1,
		"batch_no": "BATCH-CG",
	}
	base.update(fields)
	return base


_DEFAULT_ROWS = {
	"MOP Log": [_log()],
	"Manufacturing Operation": [
		{"name": "MOP-1", "status": "WIP", "department": "Casting - GE"}
	],
	"Batch": [
		{
			"name": "BATCH-CG",
			"custom_inventory_type": "Customer Goods",
			"custom_customer": "MHCU0012",
		}
	],
	"Item": [{"name": "M-G-18KT-75.4-Y", "stock_uom": "Gram"}],
}


def _fake_get_all(rows_by_doctype):
	"""Stand in for frappe.db.get_all, dispatching on the doctype argument."""

	def _inner(doctype, **kwargs):
		return [frappe._dict(row) for row in rows_by_doctype.get(doctype, [])]

	return _inner


def _rows(**overrides):
	rows = dict(_DEFAULT_ROWS)
	rows.update(overrides)
	return rows


class TestColumns(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_columns_are_in_the_mandated_order(self):
		self.assertEqual(
			[c["fieldname"] for c in rpt._get_columns()], _EXPECTED_COLUMNS
		)

	def test_status_column_is_data_not_select(self):
		# Manufacturing Operation.status is a Select, but report columns have no
		# Select renderer -- Data is the correct fieldtype.
		status = next(
			c
			for c in rpt._get_columns()
			if c["fieldname"] == "manufacturing_operation_status"
		)
		self.assertEqual(status["fieldtype"], "Data")


class TestValidateFilters(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_missing_manufacturing_operation_throws(self):
		# reqd:1 in the .js is bypassable via the API, so the server guards too.
		filters = frappe._dict(
			{
				"company": "Gurukrupa Exports Private Limited",
				"manufacturing_work_order": "MWO-1",
			}
		)
		with self.assertRaises(frappe.ValidationError):
			rpt._validate_filters(filters)

	def test_missing_manufacturing_work_order_throws(self):
		filters = frappe._dict(
			{
				"company": "Gurukrupa Exports Private Limited",
				"manufacturing_operation": "MOP-1",
			}
		)
		with self.assertRaises(frappe.ValidationError):
			rpt._validate_filters(filters)

	def test_full_filters_pass(self):
		self.assertIsNone(rpt._validate_filters(_FILTERS))


class TestGetData(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, rows_by_doctype):
		with patch("frappe.db.get_all", side_effect=_fake_get_all(rows_by_doctype)):
			return rpt._get_data(_FILTERS)

	def test_ownership_comes_from_the_batch_not_the_pmo(self):
		row = self._run(_DEFAULT_ROWS)[0]
		self.assertEqual(row["inventory_type"], "Customer Goods")
		self.assertEqual(row["customer"], "MHCU0012")
		self.assertEqual(row["manufacturing_operation_status"], "WIP")
		self.assertEqual(row["department"], "Casting - GE")
		self.assertEqual(row["uom"], "Gram")

	def test_two_batches_of_one_item_carry_their_own_owners(self):
		# The whole point of the rework: ownership varies row to row within a MOP.
		rows = _rows(
			**{
				"MOP Log": [_log(batch_no="BATCH-CG"), _log(batch_no="BATCH-RS")],
				"Batch": [
					{
						"name": "BATCH-CG",
						"custom_inventory_type": "Customer Goods",
						"custom_customer": "MHCU0012",
					},
					{
						"name": "BATCH-RS",
						"custom_inventory_type": "Regular Stock",
						"custom_customer": None,
					},
				],
			}
		)
		data = self._run(rows)
		self.assertEqual(len(data), 2)
		by_batch = {row["batch_no"]: row for row in data}
		self.assertEqual(by_batch["BATCH-CG"]["customer"], "MHCU0012")
		self.assertEqual(by_batch["BATCH-RS"]["inventory_type"], "Regular Stock")
		self.assertIsNone(by_batch["BATCH-RS"]["customer"])

	def test_blank_batch_leaves_ownership_empty(self):
		# 811 live MOP Log rows have no batch. They must NOT default to Regular Stock.
		rows = _rows(**{"MOP Log": [_log(batch_no=None)], "Batch": []})
		row = self._run(rows)[0]
		self.assertIsNone(row["inventory_type"])
		self.assertIsNone(row["customer"])

	def test_unknown_batch_leaves_ownership_empty(self):
		# batch_no is plain Data with no FK -- a renamed/deleted batch misses the map.
		rows = _rows(**{"Batch": []})
		row = self._run(rows)[0]
		self.assertIsNone(row["inventory_type"])
		self.assertIsNone(row["customer"])

	def test_customer_goods_with_no_customer_is_shown_raw_not_downgraded(self):
		# normalize_ownership would rewrite this to Regular Stock. A balance report
		# must expose the anomaly, not launder it -- see the module docstring.
		rows = _rows(
			Batch=[
				{
					"name": "BATCH-CG",
					"custom_inventory_type": "Customer Goods",
					"custom_customer": None,
				}
			]
		)
		row = self._run(rows)[0]
		self.assertEqual(row["inventory_type"], "Customer Goods")
		self.assertIsNone(row["customer"])

	def test_regular_stock_with_a_customer_is_shown_raw(self):
		# The mirror anomaly: normalize_ownership would blank the customer.
		rows = _rows(
			Batch=[
				{
					"name": "BATCH-CG",
					"custom_inventory_type": "Regular Stock",
					"custom_customer": "MHCU0012",
				}
			]
		)
		row = self._run(rows)[0]
		self.assertEqual(row["inventory_type"], "Regular Stock")
		self.assertEqual(row["customer"], "MHCU0012")

	def test_latest_log_per_batch_wins(self):
		# The query orders newest-first, so the first row seen for a key is the latest.
		rows = _rows(
			**{
				"MOP Log": [
					_log(qty_after_transaction_batch_based=10.0),
					_log(qty_after_transaction_batch_based=99.0),
				]
			}
		)
		data = self._run(rows)
		self.assertEqual(len(data), 1)
		self.assertEqual(data[0]["qty"], 10.0)

	def test_zero_qty_and_pcs_rows_are_dropped(self):
		rows = _rows(
			**{
				"MOP Log": [
					_log(
						qty_after_transaction_batch_based=0,
						pcs_after_transaction_batch_based=0,
					)
				]
			}
		)
		self.assertEqual(self._run(rows), [])

	def test_zero_qty_row_is_dropped_even_with_pcs(self):
		# Qty alone decides. 83 live rows sit at qty 0 with pcs 1 (gold metal at
		# zero weight); a zero balance is nothing to report.
		rows = _rows(
			**{
				"MOP Log": [
					_log(
						qty_after_transaction_batch_based=0,
						pcs_after_transaction_batch_based=3,
					)
				]
			}
		)
		self.assertEqual(self._run(rows), [])

	def test_negative_qty_row_is_kept(self):
		# Impossible in reality, so it must stay visible rather than be filtered
		# away silently -- same reasoning as the ownership anomalies above.
		rows = _rows(
			**{
				"MOP Log": [
					_log(
						qty_after_transaction_batch_based=-2.5,
						pcs_after_transaction_batch_based=1,
					)
				]
			}
		)
		data = self._run(rows)
		self.assertEqual(len(data), 1)
		self.assertEqual(data[0]["qty"], -2.5)

	def test_no_logs_returns_empty(self):
		self.assertEqual(self._run(_rows(**{"MOP Log": []})), [])

	def test_row_keys_match_the_declared_columns(self):
		row = self._run(_DEFAULT_ROWS)[0]
		self.assertEqual(sorted(row.keys()), sorted(_EXPECTED_COLUMNS))
