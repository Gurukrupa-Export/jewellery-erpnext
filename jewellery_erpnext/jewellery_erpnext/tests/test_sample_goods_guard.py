# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Unit tests for the Customer Sample Goods block (all four layers) + helpers.

Mocked/pure-logic style (see test_snc_submit_guard.py): SimpleNamespace fake docs, no
DB/persistence, throws asserted via ``assertRaises`` + inspection of ``throw.call_args``.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events import (
	inventory_utils as iu,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events import (
	se_utils,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils import sample_goods as sg
from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events import (
	department_ir_utils as dir_utils,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	validation_utils as eir_utils,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log import mop_log


class _Doc(SimpleNamespace):
	"""SimpleNamespace with Frappe-style ``.get()``."""

	def get(self, key, default=None):
		return getattr(self, key, default)


class _Row(_Doc):
	"""Stock Entry / FIFO row: adds ``.db_set`` and ``.as_dict``."""

	def db_set(self, key, value):
		setattr(self, key, value)

	def as_dict(self):
		return dict(self.__dict__)


def _se(stock_entry_type, items, **extra):
	defaults = {
		"doctype": "Stock Entry",
		"stock_entry_type": stock_entry_type,
		"items": items,
	}
	defaults.update(extra)
	return _Doc(**defaults)


def _item(s_warehouse=None, t_warehouse=None, batch_no=None, item_code="ITM-1"):
	return _Row(
		s_warehouse=s_warehouse,
		t_warehouse=t_warehouse,
		batch_no=batch_no,
		item_code=item_code,
	)


# --------------------------------------------------------------------------- helpers


class TestSampleGoodsHelpers(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_is_customer_sample_batch_empty_short_circuits(self):
		with patch.object(sg.frappe, "get_cached_value") as gcv:
			self.assertFalse(sg.is_customer_sample_batch(None))
			self.assertFalse(sg.is_customer_sample_batch(""))
		gcv.assert_not_called()

	def test_is_customer_sample_batch_true_false(self):
		with patch.object(
			sg.frappe, "get_cached_value", return_value="Customer Sample Goods"
		):
			self.assertTrue(sg.is_customer_sample_batch("B-1"))
		with patch.object(
			sg.frappe, "get_cached_value", return_value="Customer Subcontracting"
		):
			self.assertFalse(sg.is_customer_sample_batch("B-1"))
		with patch.object(sg.frappe, "get_cached_value", return_value=None):
			self.assertFalse(sg.is_customer_sample_batch("B-1"))

	def test_get_sample_batches_empty_no_query(self):
		with patch.object(sg.frappe, "get_all") as get_all:
			self.assertEqual(sg.get_sample_batches(set()), set())
			self.assertEqual(sg.get_sample_batches({None, ""}), set())
		get_all.assert_not_called()

	def test_get_sample_batches_returns_subset(self):
		with patch.object(sg.frappe, "get_all", return_value=["B-SAMPLE"]) as get_all:
			result = sg.get_sample_batches({"B-SAMPLE", "B-REG", None})
		self.assertEqual(result, {"B-SAMPLE"})
		# falsy entries are dropped before the query
		self.assertEqual(
			set(get_all.call_args.kwargs["filters"]["name"][1]), {"B-SAMPLE", "B-REG"}
		)

	def test_sample_batches_in_operation_none(self):
		with patch.object(mop_log, "get_current_mop_balance_rows") as balance:
			self.assertEqual(sg.sample_batches_in_operation(None), [])
		balance.assert_not_called()

	def test_sample_batches_in_operation_filters_positive_sample(self):
		rows = [
			{
				"batch_no": "B-SAMPLE",
				"item_code": "ITM",
				"qty_after_transaction_batch_based": 5,
			},
			{
				"batch_no": "B-REG",
				"item_code": "ITM",
				"qty_after_transaction_batch_based": 3,
			},
			{
				"batch_no": "B-SAMPLE0",
				"item_code": "ITM",
				"qty_after_transaction_batch_based": 0,
			},
		]
		with patch.object(
			mop_log, "get_current_mop_balance_rows", return_value=rows
		), patch.object(
			sg, "get_sample_batches", return_value={"B-SAMPLE", "B-SAMPLE0"}
		):
			result = sg.sample_batches_in_operation("MOP-1")
		self.assertEqual([r["batch_no"] for r in result], ["B-SAMPLE"])

	def test_assert_no_sample_in_operations_clean(self):
		ops = [_Doc(manufacturing_operation="MOP-1", manufacturing_work_order="MWO-1")]
		with patch.object(
			sg, "sample_batches_in_operation", return_value=[]
		), patch.object(sg.frappe, "throw") as throw:
			sg.assert_no_sample_in_operations(ops)
		throw.assert_not_called()

	def test_assert_no_sample_in_operations_throws(self):
		ops = [_Doc(manufacturing_operation="MOP-1", manufacturing_work_order="MWO-1")]
		offenders = [{"batch_no": "B-SAMPLE", "item_code": "ITM-9"}]
		with patch.object(
			sg, "sample_batches_in_operation", return_value=offenders
		), patch.object(sg.frappe, "throw", side_effect=RuntimeError) as throw:
			with self.assertRaises(RuntimeError):
				sg.assert_no_sample_in_operations(ops)
		msg = throw.call_args[0][0]
		self.assertIn("MOP-1", msg)
		self.assertIn("MWO-1", msg)
		self.assertIn("B-SAMPLE", msg)
		self.assertIn("ITM-9", msg)


# ------------------------------------------------------------------ Layer 1: SE guard


class TestSampleSEConsumptionGuard(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, se):
		# get_sample_batches is stubbed to treat only "B-SAMPLE" as a sample.
		with patch.object(
			iu,
			"get_sample_batches",
			side_effect=lambda batches: {b for b in batches if b == "B-SAMPLE"},
		) as gsb, patch.object(
			iu.frappe, "get_cached_value", return_value="CUST-1"
		), patch.object(iu.frappe, "throw", side_effect=RuntimeError) as throw:
			raised = None
			try:
				iu.validate_sample_goods_not_consumed(se)
			except RuntimeError as e:  # thrown message captured via throw.call_args
				raised = e
			return gsb, throw, raised

	def test_allow_listed_type_short_circuits(self):
		se = _se("Customer Goods Issue", [_item(s_warehouse="WH", batch_no="B-SAMPLE")])
		gsb, throw, raised = self._run(se)
		gsb.assert_not_called()
		throw.assert_not_called()
		self.assertIsNone(raised)

	def test_blocks_sample_on_manufacture(self):
		se = _se(
			"Manufacture",
			[_item(s_warehouse="WH", batch_no="B-SAMPLE", item_code="ITM-1")],
		)
		_gsb, throw, raised = self._run(se)
		self.assertIsNotNone(raised)
		msg = throw.call_args[0][0]
		self.assertIn("B-SAMPLE", msg)
		self.assertIn("ITM-1", msg)
		self.assertIn("CUST-1", msg)

	def test_blocks_sample_on_material_transfer_to_department(self):
		se = _se(
			"Material Transfer to Department",
			[_item(s_warehouse="WH", batch_no="B-SAMPLE")],
		)
		_gsb, throw, raised = self._run(se)
		self.assertIsNotNone(raised)
		self.assertIn("B-SAMPLE", throw.call_args[0][0])

	def test_target_only_row_ignored(self):
		se = _se("Manufacture", [_item(t_warehouse="WH", batch_no="B-SAMPLE")])
		_gsb, throw, raised = self._run(se)
		throw.assert_not_called()
		self.assertIsNone(raised)

	def test_batchless_source_row_ignored(self):
		se = _se("Manufacture", [_item(s_warehouse="WH", batch_no=None)])
		_gsb, throw, raised = self._run(se)
		throw.assert_not_called()
		self.assertIsNone(raised)

	def test_non_sample_batch_allowed(self):
		se = _se("Manufacture", [_item(s_warehouse="WH", batch_no="B-REG")])
		_gsb, throw, raised = self._run(se)
		throw.assert_not_called()
		self.assertIsNone(raised)

	def test_multi_row_later_row_is_sample(self):
		se = _se(
			"Manufacture",
			[
				_item(s_warehouse="WH", batch_no="B-REG", item_code="ITM-1"),
				_item(s_warehouse="WH", batch_no="B-SAMPLE", item_code="ITM-2"),
			],
		)
		_gsb, throw, raised = self._run(se)
		self.assertIsNotNone(raised)
		msg = throw.call_args[0][0]
		self.assertIn("B-SAMPLE", msg)
		self.assertIn("ITM-2", msg)


# ------------------------------------------------------ Layer 3 & 4: IR issue guards


class TestSampleIssueGuards(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_employee_ir_non_issue_is_noop(self):
		doc = _Doc(type="Receive", employee_ir_operations=[_Doc()])
		with patch.object(eir_utils, "assert_no_sample_in_operations") as guard:
			eir_utils.validate_no_sample_issue(doc)
		guard.assert_not_called()

	def test_employee_ir_issue_delegates(self):
		ops = [_Doc(manufacturing_operation="MOP-1", manufacturing_work_order="MWO-1")]
		doc = _Doc(type="Issue", employee_ir_operations=ops)
		with patch.object(eir_utils, "assert_no_sample_in_operations") as guard:
			eir_utils.validate_no_sample_issue(doc)
		guard.assert_called_once_with(ops, doc)

	def test_employee_ir_issue_propagates_throw(self):
		doc = _Doc(type="Issue", employee_ir_operations=[_Doc()])
		with patch.object(
			eir_utils, "assert_no_sample_in_operations", side_effect=RuntimeError
		):
			with self.assertRaises(RuntimeError):
				eir_utils.validate_no_sample_issue(doc)

	def test_department_ir_non_issue_is_noop(self):
		doc = _Doc(type="Receive", department_ir_operation=[_Doc()])
		with patch.object(dir_utils, "assert_no_sample_in_operations") as guard:
			dir_utils.validate_no_sample_issue(doc)
		guard.assert_not_called()

	def test_department_ir_issue_delegates(self):
		ops = [_Doc(manufacturing_operation="MOP-2", manufacturing_work_order="MWO-2")]
		doc = _Doc(type="Issue", department_ir_operation=ops)
		with patch.object(dir_utils, "assert_no_sample_in_operations") as guard:
			dir_utils.validate_no_sample_issue(doc)
		guard.assert_called_once_with(ops, doc)


# --------------------------------------------------------- Layer 2: FIFO exclusion


class TestSampleFifoExclusion(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run_fifo(self, stock_entry_type):
		se = _Doc(
			stock_entry_type=stock_entry_type,
			date=None,
			posting_time=None,
			posting_date=None,
			source_warehouse=None,
			main_slip=None,
			to_main_slip=None,
			flags=frappe._dict(),
		)
		row = _Row(
			qty=3,
			item_code="M-G-18KT",
			s_warehouse="WH-1",
			inventory_type="Customer Goods",
			customer="CUST-1",
			custom_parent_manufacturing_order=None,
			custom_variant_of=None,
			manufacturing_operation=None,
			batch_no=None,
		)
		batch_data = [
			frappe._dict(batch_no="B-SAMPLE", qty=5),
			frappe._dict(batch_no="B-REG", qty=5),
		]

		# get_fifo_batches prefetches the per-batch Batch fields through _bulk_map
		# (a frappe.get_all), not per-row frappe.db.get_value — stub the map itself so
		# the test stays DB-free. Both batches belong to the row's customer/inventory
		# type, so allocation is decided purely by the sample gate.
		batch_info = {
			"B-SAMPLE": frappe._dict(
				custom_inventory_type="Customer Goods", custom_customer="CUST-1"
			),
			"B-REG": frappe._dict(
				custom_inventory_type="Customer Goods", custom_customer="CUST-1"
			),
		}

		with patch.object(
			se_utils, "get_auto_batch_nos", return_value=batch_data
		), patch.object(
			se_utils, "is_customer_sample_batch", side_effect=lambda b: b == "B-SAMPLE"
		), patch.object(se_utils, "_bulk_map", return_value=batch_info), patch(
			# only remaining reader: the Manufacturer allow-regular-goods lookup
			"frappe.db.get_value",
			return_value=None,
		):
			rows = se_utils.get_fifo_batches(se, row)
		return [r.get("batch_no") for r in rows]

	def test_sample_skipped_for_manufacturing_type(self):
		allocated = self._run_fifo("Manufacture")
		self.assertNotIn("B-SAMPLE", allocated)
		self.assertIn("B-REG", allocated)

	def test_sample_retained_for_customer_goods_issue(self):
		allocated = self._run_fifo("Customer Goods Issue")
		self.assertIn("B-SAMPLE", allocated)
