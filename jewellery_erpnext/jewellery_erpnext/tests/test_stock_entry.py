# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the Stock Entry lifecycle doc-event hooks and the CustomStockEntry override.

Pure-logic: every DB access is patched, docs are SimpleNamespace fakes. Covers the
wired, previously-untested hook entry-points:

* doc_events/stock_entry.py: before_validate orchestration, validate_ir,
  validate_material_request_warehouses, validate_main_slip_warehouse,
  validate_duplicate_batches, before_submit, onsubmit dispatch, on_cancel,
  on_update_after_submit, prelock_bins / prelock_bins_on_cancel
* customization/stock_entry/stock_entry.py: CustomStockEntry.update_batches,
  validate_with_material_request, and the before_validate guard chain
* se_utils / inventory_utils guards invoked by that chain
* customer_subcontracting.batch_rename.create_parent_batches (SE before_submit)

Already covered elsewhere (see plan): validate_pcs, get_fifo_batches, validate_items,
sync_mop_log_for_stock_entry, stock_reservation_entry_for_mwo, set_basic_rate,
validate_sample_goods_not_consumed, create_child_batches, create_subcontracting_log.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.customer_subcontracting import batch_rename
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry import (
	stock_entry as cse_mod,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events import (
	inventory_utils as iu,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events import (
	se_utils,
)
from jewellery_erpnext.jewellery_erpnext.doc_events import stock_entry as se_events


class _Doc(SimpleNamespace):
	"""SimpleNamespace with Frappe-style ``.get()``."""

	def get(self, key, default=None):
		return getattr(self, key, default)


class _Row(_Doc):
	"""Stock Entry Detail row.

	A plain object (NOT ``frappe._dict``) on purpose: the ``row.__dict__``-clone path in
	CustomStockEntry.update_batches iterates instance attrs, which a dict subclass breaks.
	"""


def _bare_cse(**attrs):
	"""Bypass ``Document.__init__`` — only exercise the override branch logic.

	Same pattern as ``_bare_sre`` in test_stock_reservation_entry_mwo.py.
	"""
	obj = cse_mod.CustomStockEntry.__new__(cse_mod.CustomStockEntry)
	obj.appended = []
	for k, v in attrs.items():
		setattr(obj, k, v)
	obj.get = lambda key, default=None: getattr(obj, key, default)
	obj.append = lambda field, row: obj.appended.append(row)
	obj.db_update = MagicMock()
	return obj


def _mr_row(
	material_request="MR-1",
	material_request_item="MRI-1",
	s_warehouse=None,
	t_warehouse=None,
	idx=1,
):
	return _Row(
		material_request=material_request,
		material_request_item=material_request_item,
		s_warehouse=s_warehouse,
		t_warehouse=t_warehouse,
		idx=idx,
	)


def _capture_throw(callable_fn, *args, **kwargs):
	"""Run callable_fn with ``frappe.throw`` patched to raise RuntimeError.

	Returns ``(raised: bool, throw_mock)`` so tests can inspect ``throw.call_args[0][0]``.
	"""
	with patch.object(se_events.frappe, "throw", side_effect=RuntimeError) as throw:
		raised = False
		try:
			callable_fn(*args, **kwargs)
		except RuntimeError:
			raised = True
	return raised, throw


def _run_mapped(fn, source, target):
	"""Run a get_mapped_doc-based mapper under a capturing mock.

	The production mappers call ``frappe.get_mapped_doc(...)``, which drives the
	per-doctype ``map_dict`` / ``postprocess`` closures. This fakes that call,
	records the closures it receives, and invokes ``postprocess(source, tgt_doc)``
	the way the mapper does. Returns ``(result, {"map_dict": ..., "postprocess": ...})``.
	"""
	mapped_kwargs = {}

	def _gmd(src_doctype, src_name, map_dict, tgt_doc, postprocess):
		mapped_kwargs["map_dict"] = map_dict
		mapped_kwargs["postprocess"] = postprocess
		postprocess(source, tgt_doc)
		return tgt_doc

	with patch.object(se_events, "get_mapped_doc", side_effect=_gmd):
		res = fn("SE-1", target)
	return res, mapped_kwargs


def _query_mock(side_effect=None):
	"""A chained qb query mock: every builder method returns the query itself.

	``frappe.qb.from_(...)`` chains select/where/join/groupby calls that each
	return the query object. ``run()`` yields the row sets — pass ``side_effect``
	to return a different set per call, or set ``mock.run.return_value`` after.
	"""
	query = MagicMock()
	query.left_join.return_value = query
	query.inner_join.return_value = query
	query.on.return_value = query
	query.select.return_value = query
	query.where.return_value = query
	query.groupby.return_value = query
	query.as_.return_value = query
	if side_effect is not None:
		query.run.side_effect = side_effect
	return query


class _StockEntryTestCase(IntegrationTestCase):
	"""House-pattern base: no DB fixtures; setUpClass is a deliberate no-op."""

	@classmethod
	def setUpClass(cls):
		pass


# -------------------------------------------------------------------------- validate_ir
class TestValidateIr(_StockEntryTestCase):
	def _run(self, se, dept_rows=(), emp_rows=()):
		def _get_all(doctype, **kwargs):
			if doctype == "Department IR Operation":
				return list(dept_rows)
			if doctype == "Employee IR Operation":
				return list(emp_rows)
			return []

		with patch.object(se_events.frappe, "get_all", side_effect=_get_all) as get_all:
			raised, throw = _capture_throw(se_events.validate_ir, se)
		return get_all, throw, raised

	def _se(self, **extra):
		defaults = {
			"auto_created": 0,
			"stock_entry_type": "Material Transfer (WORK ORDER)",
			"manufacturing_work_order": "MWO-1",
		}
		defaults.update(extra)
		return _Doc(**defaults)

	def test_draft_department_ir_throws(self):
		get_all, throw, raised = self._run(self._se(), dept_rows=[{"parent": "IR-1"}])
		self.assertTrue(raised)
		msg = throw.call_args[0][0]
		self.assertIn("MWO-1", msg)
		self.assertIn("IR-1", msg)

	def test_draft_employee_ir_throws(self):
		get_all, throw, raised = self._run(self._se(), emp_rows=[{"parent": "IR-2"}])
		self.assertTrue(raised)
		msg = throw.call_args[0][0]
		self.assertIn("MWO-1", msg)
		self.assertIn("IR-2", msg)

	def test_no_draft_rows_passes(self):
		get_all, throw, raised = self._run(self._se())
		self.assertFalse(raised)
		# both Department and Employee lookups were issued
		self.assertEqual(get_all.call_count, 2)

	def test_queries_only_draft_ir(self):
		# Non-draft IRs are excluded by the query filter, not inspected post-hoc:
		# both lookups must carry docstatus=0.
		get_all, _throw, raised = self._run(self._se())
		self.assertFalse(raised)
		self.assertEqual(get_all.call_count, 2)
		for call in get_all.call_args_list:
			self.assertEqual(call.kwargs["filters"]["docstatus"], 0)

	def test_throws_on_any_returned_row_trusts_filter(self):
		# The hook never re-inspects docstatus: draft-exclusion is entirely the
		# query filter's job (asserted in test_queries_only_draft_ir). A row that
		# slips past the filter blocks regardless of its docstatus.
		_get_all, throw, raised = self._run(
			self._se(), dept_rows=[{"parent": "IR-1", "docstatus": 1}]
		)
		self.assertTrue(raised)
		self.assertIn("IR-1", throw.call_args[0][0])

	def test_auto_created_skips_queries(self):
		get_all, _throw, raised = self._run(self._se(auto_created=1))
		self.assertFalse(raised)
		get_all.assert_not_called()

	def test_non_work_order_type_skips_queries(self):
		get_all, _throw, raised = self._run(self._se(stock_entry_type="Material Issue"))
		self.assertFalse(raised)
		get_all.assert_not_called()

	def test_missing_work_order_skips_queries(self):
		get_all, _throw, raised = self._run(self._se(manufacturing_work_order=None))
		self.assertFalse(raised)
		get_all.assert_not_called()


# ------------------------------------------------- validate_material_request_warehouses
class TestValidateMaterialRequestWarehouses(_StockEntryTestCase):
	def _run(
		self,
		se,
		mr_item_map,
		mr_type_map,
		transit_map=None,
	):
		transit_map = transit_map or {}

		def _bulk_map(doctype, names, fields):
			if doctype == "Material Request Item":
				return mr_item_map
			if doctype == "Material Request":
				return mr_type_map
			if doctype == "Warehouse":
				return transit_map
			raise AssertionError(f"unexpected bulk_map doctype {doctype}")

		with patch.object(se_events, "bulk_map", side_effect=_bulk_map) as bulk_map:
			raised, throw = _capture_throw(
				se_events.validate_material_request_warehouses, se, method=None
			)
		return bulk_map, throw, raised

	def _se(self, items=None, reference="MR-1"):
		return _Doc(custom_material_request_reference=reference, items=items or [])

	def test_no_mr_reference_short_circuits(self):
		se = self._se(reference=None, items=[_mr_row()])
		with patch.object(se_events, "bulk_map") as bulk_map:
			se_events.validate_material_request_warehouses(se, method=None)
		bulk_map.assert_not_called()

	def test_no_mr_rows_short_circuits(self):
		se = self._se(items=[_Row(material_request=None, material_request_item=None)])
		with patch.object(se_events, "bulk_map") as bulk_map:
			se_events.validate_material_request_warehouses(se, method=None)
		bulk_map.assert_not_called()

	def test_missing_mr_item_throws(self):
		se = self._se(items=[_mr_row()])
		_bm, throw, raised = self._run(se, {}, {})
		self.assertTrue(raised)
		self.assertIn("MRI-1", throw.call_args[0][0])

	def test_mr_item_from_other_request_throws(self):
		se = self._se(items=[_mr_row()])
		mr_item_map = {
			"MRI-1": frappe._dict(
				parent="MR-2", warehouse="WH-1", from_warehouse="WH-0"
			)
		}
		_bm, throw, raised = self._run(
			se,
			mr_item_map,
			{"MR-1": frappe._dict(material_request_type="Material Issue")},
		)
		self.assertTrue(raised)
		self.assertIn("MR-2", throw.call_args[0][0])

	def test_material_issue_source_mismatch_throws(self):
		se = self._se(items=[_mr_row(s_warehouse="WH-WRONG")])
		mr_item_map = {
			"MRI-1": frappe._dict(parent="MR-1", warehouse="WH-1", from_warehouse=None)
		}
		_bm, throw, raised = self._run(
			se,
			mr_item_map,
			{"MR-1": frappe._dict(material_request_type="Material Issue")},
		)
		self.assertTrue(raised)
		msg = throw.call_args[0][0]
		self.assertIn("Source Warehouse", msg)
		self.assertIn("WH-1", msg)
		self.assertIn("WH-WRONG", msg)

	def test_material_issue_matching_source_with_blank_target_passes(self):
		se = self._se(items=[_mr_row(s_warehouse="WH-1", t_warehouse=None)])
		mr_item_map = {
			"MRI-1": frappe._dict(parent="MR-1", warehouse="WH-1", from_warehouse=None)
		}
		_bm, throw, raised = self._run(
			se,
			mr_item_map,
			{"MR-1": frappe._dict(material_request_type="Material Issue")},
		)
		self.assertFalse(raised)

	def test_customer_provided_target_mismatch_throws(self):
		se = self._se(items=[_mr_row(s_warehouse=None, t_warehouse="WH-WRONG")])
		mr_item_map = {
			"MRI-1": frappe._dict(parent="MR-1", warehouse="WH-1", from_warehouse=None)
		}
		_bm, throw, raised = self._run(
			se,
			mr_item_map,
			{"MR-1": frappe._dict(material_request_type="Customer Provided")},
		)
		self.assertTrue(raised)
		self.assertIn("Target Warehouse", throw.call_args[0][0])

	def test_material_transfer_matching_passes(self):
		se = self._se(items=[_mr_row(s_warehouse="WH-0", t_warehouse="WH-1")])
		mr_item_map = {
			"MRI-1": frappe._dict(
				parent="MR-1", warehouse="WH-1", from_warehouse="WH-0"
			)
		}
		_bm, throw, raised = self._run(
			se,
			mr_item_map,
			{"MR-1": frappe._dict(material_request_type="Material Transfer")},
		)
		self.assertFalse(raised)

	def test_transit_warehouse_target_allowed(self):
		se = self._se(items=[_mr_row(s_warehouse="WH-0", t_warehouse="WH-T")])
		mr_item_map = {
			"MRI-1": frappe._dict(
				parent="MR-1", warehouse="WH-1", from_warehouse="WH-0"
			)
		}
		transit_map = {"WH-1": frappe._dict(default_in_transit_warehouse="WH-T")}
		_bm, throw, raised = self._run(
			se,
			mr_item_map,
			{"MR-1": frappe._dict(material_request_type="Material Transfer")},
			transit_map,
		)
		self.assertFalse(raised)

	def test_material_transfer_target_mismatch_throws(self):
		se = self._se(items=[_mr_row(s_warehouse="WH-0", t_warehouse="WH-WRONG")])
		mr_item_map = {
			"MRI-1": frappe._dict(
				parent="MR-1", warehouse="WH-1", from_warehouse="WH-0"
			)
		}
		_bm, throw, raised = self._run(
			se,
			mr_item_map,
			{"MR-1": frappe._dict(material_request_type="Material Transfer")},
		)
		self.assertTrue(raised)
		self.assertIn("Target Warehouse", throw.call_args[0][0])

	def test_throw_warehouse_mismatch_message_format(self):
		row = _mr_row(s_warehouse="WH-WRONG")
		row.material_request = "MR-9"
		raised, throw = _capture_throw(
			se_events._throw_warehouse_mismatch,
			row,
			"Source Warehouse",
			"WH-1",
			"WH-WRONG",
		)
		self.assertTrue(raised)
		msg = throw.call_args[0][0]
		self.assertIn("Row #1", msg)
		self.assertIn("Source Warehouse", msg)
		self.assertIn("WH-1", msg)
		self.assertIn("MR-9", msg)
		self.assertIn("WH-WRONG", msg)


# ------------------------------------------------------------- validate_main_slip_warehouse
class TestValidateMainSlipWarehouse(_StockEntryTestCase):
	def test_matching_main_slip_source_passes(self):
		doc = _Doc(
			auto_created=0,
			items=[_Row(main_slip="MSL-1", to_main_slip=None, s_warehouse="WH-1")],
		)
		with patch.object(se_events.frappe.db, "get_value", return_value="WH-1") as gv:
			se_events.validate_main_slip_warehouse(doc)
		# auto_created=0 reads warehouse, then raw_material_warehouse
		self.assertEqual(gv.call_count, 2)

	def test_auto_created_reads_plain_warehouse(self):
		doc = _Doc(
			auto_created=1,
			items=[_Row(main_slip="MSL-1", to_main_slip=None, s_warehouse="WH-1")],
		)
		with patch.object(se_events.frappe.db, "get_value", return_value="WH-1") as gv:
			se_events.validate_main_slip_warehouse(doc)
		gv.assert_called_once_with("Main Slip", "MSL-1", "warehouse")

	def test_main_slip_source_mismatch_throws(self):
		doc = _Doc(
			auto_created=0,
			items=[_Row(main_slip="MSL-1", to_main_slip=None, s_warehouse="WH-WRONG")],
		)
		with patch.object(se_events.frappe.db, "get_value", return_value="WH-1"):
			raised, throw = _capture_throw(se_events.validate_main_slip_warehouse, doc)
		self.assertTrue(raised)
		self.assertIn("MSL-1", throw.call_args[0][0])

	def test_to_main_slip_target_mismatch_throws(self):
		doc = _Doc(
			auto_created=0,
			items=[_Row(main_slip=None, to_main_slip="MSL-2", t_warehouse="WH-WRONG")],
		)
		with patch.object(se_events.frappe.db, "get_value", return_value="WH-2"):
			raised, throw = _capture_throw(se_events.validate_main_slip_warehouse, doc)
		self.assertTrue(raised)
		self.assertIn("MSL-2", throw.call_args[0][0])

	def test_row_without_main_slip_skipped(self):
		# Single-row case only: it cannot distinguish the source's ``return``
		# from a ``continue`` (both skip the only row).
		doc = _Doc(
			auto_created=0,
			items=[_Row(main_slip=None, to_main_slip=None, s_warehouse="WH-1")],
		)
		with patch.object(se_events.frappe.db, "get_value") as gv:
			se_events.validate_main_slip_warehouse(doc)
		gv.assert_not_called()

	def test_main_slip_without_warehouse_throws_for_source(self):
		# Main Slip carries no raw_material_warehouse; a source row with a
		# warehouse still mismatches (WH-1 != None) and must throw.
		doc = _Doc(
			auto_created=0,
			items=[_Row(main_slip="MSL-1", to_main_slip=None, s_warehouse="WH-1")],
		)
		with patch.object(se_events.frappe.db, "get_value", return_value=None):
			raised, throw = _capture_throw(se_events.validate_main_slip_warehouse, doc)
		self.assertTrue(raised)
		self.assertIn("MSL-1", throw.call_args[0][0])

	def test_main_slip_without_warehouse_passes_when_row_blank(self):
		doc = _Doc(
			auto_created=0,
			items=[_Row(main_slip="MSL-1", to_main_slip=None, s_warehouse=None)],
		)
		with patch.object(se_events.frappe.db, "get_value", return_value=None):
			raised, throw = _capture_throw(se_events.validate_main_slip_warehouse, doc)
		self.assertFalse(raised)

	def test_auto_created_without_warehouse_throws_for_target(self):
		doc = _Doc(
			auto_created=1,
			items=[_Row(main_slip=None, to_main_slip="MSL-2", t_warehouse="WH-2")],
		)
		with patch.object(se_events.frappe.db, "get_value", return_value=None):
			raised, throw = _capture_throw(se_events.validate_main_slip_warehouse, doc)
		self.assertTrue(raised)
		self.assertIn("MSL-2", throw.call_args[0][0])


# ------------------------------------------------------------ validate_duplicate_batches
class TestValidateDuplicateBatches(_StockEntryTestCase):
	def _entry(self, batch_no="B-1"):
		return _Doc(
			idx=1,
			item_code="M-G-18KT",
			batch_no=batch_no,
			manufacturing_operation="MOP-1",
		)

	def test_allowed_batch_passes_and_caches(self):
		batch_data = {}
		with patch.object(
			se_events.frappe, "get_all", return_value=["B-1", "B-2"]
		) as get_all:
			se_events.validate_duplicate_batches(self._entry(), batch_data)
		get_all.assert_called_once()
		self.assertEqual(batch_data, {("MOP-1", "M-G-18KT"): ["B-1", "B-2"]})

	def test_disallowed_batch_throws_with_allowed_list(self):
		batch_data = {}
		with patch.object(se_events.frappe, "get_all", return_value=["B-1"]):
			raised, throw = _capture_throw(
				se_events.validate_duplicate_batches,
				self._entry(batch_no="B-X"),
				batch_data,
			)
		self.assertTrue(raised)
		msg = throw.call_args[0][0]
		self.assertIn("B-X", msg)
		self.assertIn("MOP-1", msg)
		self.assertIn("B-1", msg)

	def test_cached_data_not_re_queried(self):
		batch_data = {("MOP-1", "M-G-18KT"): ["B-1"]}
		with patch.object(se_events.frappe, "get_all") as get_all:
			se_events.validate_duplicate_batches(self._entry(), batch_data)
		get_all.assert_not_called()


# ------------------------------------------------------------------------- before_submit
class TestBeforeSubmit(_StockEntryTestCase):
	def test_sets_posting_time_for_non_manufacture(self):
		se = _Doc(stock_entry_type="Material Transfer")
		with patch.object(
			se_events.frappe.utils, "nowtime", return_value="12:00:00.000000"
		):
			se_events.before_submit(se, method=None)
		self.assertEqual(se.posting_time, "12:00:00.000000")

	def test_keeps_posting_time_for_manufacture(self):
		se = _Doc(stock_entry_type="Manufacture", posting_time="10:00:00.000000")
		with patch.object(
			se_events.frappe.utils, "nowtime", return_value="12:00:00.000000"
		):
			se_events.before_submit(se, method=None)
		self.assertEqual(se.posting_time, "10:00:00.000000")


# ------------------------------------------------------------------ onsubmit dispatch
class TestOnSubmitDispatch(_StockEntryTestCase):
	def _patched(self, se, pc=None, configured=(), get_value_return=None):
		return (
			patch.object(se_events, "validate_items"),
			patch.object(
				se_events.frappe.db,
				"get_value",
				return_value=get_value_return,
			),
			patch.object(
				se_events.frappe.db,
				"get_all",
				return_value=list(configured),
			),
			patch.object(se_events, "stock_reservation_entry_for_mwo"),
			patch.object(se_events, "sync_mop_log_for_stock_entry"),
		)

	def test_product_certification_receive_skips_reservation(self):
		for service in ["Fire Assy Service", "XRF Services"]:
			se = _Doc(
				stock_entry_type="Material Issue",
				product_certification="PC-1",
				items=[],
			)
			ctx = self._patched(
				se,
				get_value_return=frappe._dict(service_type=service, type="Receive"),
			)
			with ctx[0], ctx[1], ctx[2] as get_all, ctx[3] as reserve, ctx[4] as sync:
				se_events.onsubmit(se, method=None)
			reserve.assert_not_called()
			sync.assert_not_called()
			get_all.assert_not_called()

	def test_product_certification_issue_proceeds(self):
		se = _Doc(
			stock_entry_type="Material Transfer",
			product_certification="PC-1",
			items=[],
		)
		ctx = self._patched(
			se,
			configured=["Material Transfer"],
			get_value_return=frappe._dict(service_type="XRF Services", type="Issue"),
		)
		with ctx[0], ctx[1], ctx[2], ctx[3] as reserve, ctx[4] as sync:
			se_events.onsubmit(se, method=None)
		reserve.assert_called_once_with(se)
		sync.assert_called_once_with(se)

	def test_configured_type_reserves(self):
		se = _Doc(
			stock_entry_type="Material Transfer", product_certification=None, items=[]
		)
		ctx = self._patched(se, configured=["Material Issue", "Material Transfer"])
		with ctx[0], ctx[1], ctx[2], ctx[3] as reserve, ctx[4] as sync:
			se_events.onsubmit(se, method=None)
		reserve.assert_called_once_with(se)
		sync.assert_called_once_with(se)

	def test_repack_without_mwo_skips(self):
		se = _Doc(
			stock_entry_type="Repack",
			product_certification=None,
			manufacturing_order=None,
			manufacturing_work_order=None,
			items=[],
		)
		ctx = self._patched(se, configured=["Repack"])
		with ctx[0], ctx[1], ctx[2], ctx[3] as reserve, ctx[4] as sync:
			se_events.onsubmit(se, method=None)
		reserve.assert_not_called()
		sync.assert_not_called()

	def test_type_not_in_config_skips(self):
		se = _Doc(
			stock_entry_type="Material Issue", product_certification=None, items=[]
		)
		ctx = self._patched(se, configured=["Material Transfer"])
		with ctx[0], ctx[1], ctx[2], ctx[3] as reserve, ctx[4] as sync:
			se_events.onsubmit(se, method=None)
		reserve.assert_not_called()
		sync.assert_not_called()


# ----------------------------------------------------------------------------- on_cancel
class TestOnCancel(_StockEntryTestCase):
	def test_calls_sync_cancelled_and_update_operation(self):
		se = _Doc(name="SE-1", items=[])
		with patch.object(
			se_events, "update_manufacturing_operation"
		) as upd, patch.object(se_events, "sync_mop_log_for_stock_entry") as sync:
			se_events.on_cancel(se, method=None)
		upd.assert_called_once_with(se, True)
		sync.assert_called_once_with(se, is_cancelled=True)


# ------------------------------------------------------------ on_update_after_submit
class TestOnUpdateAfterSubmit(_StockEntryTestCase):
	def test_submits_draft_subcontracting(self):
		se = _Doc(subcontracting="SUB-1")
		subcontracting_doc = MagicMock()
		with patch.object(
			se_events.frappe.db, "get_value", return_value=0
		), patch.object(
			se_events.frappe, "get_doc", return_value=subcontracting_doc
		) as get_doc:
			se_events.on_update_after_submit(se, method=None)
		get_doc.assert_called_once_with("Subcontracting", "SUB-1")
		subcontracting_doc.submit.assert_called_once_with()

	def test_no_submit_when_already_submitted(self):
		se = _Doc(subcontracting="SUB-1")
		with patch.object(
			se_events.frappe.db, "get_value", return_value=1
		), patch.object(se_events.frappe, "get_doc") as get_doc:
			se_events.on_update_after_submit(se, method=None)
		get_doc.assert_not_called()

	def test_no_submit_when_subcontracting_missing(self):
		# Ghost/deleted Subcontracting row: get_value returns None, which must not
		# match the docstatus==0 guard, so get_doc is never reached.
		se = _Doc(subcontracting="SUB-GHOST")
		with patch.object(
			se_events.frappe.db, "get_value", return_value=None
		), patch.object(se_events.frappe, "get_doc") as get_doc:
			se_events.on_update_after_submit(se, method=None)
		get_doc.assert_not_called()

	def test_no_subcontracting_noop(self):
		se = _Doc(subcontracting=None)
		with patch.object(se_events.frappe.db, "get_value") as gv, patch.object(
			se_events.frappe, "get_doc"
		) as get_doc:
			se_events.on_update_after_submit(se, method=None)
		gv.assert_not_called()
		get_doc.assert_not_called()


# -------------------------------------------------------------------- prelock_bins
class TestPrelockBins(_StockEntryTestCase):
	def _make_se(self):
		return _Doc(
			items=[
				_Row(item_code="I-1", s_warehouse="WH-1", t_warehouse="WH-2"),
				_Row(item_code="I-2", s_warehouse="WH-2", t_warehouse="WH-1"),
			]
		)

	def test_prelock_bins_preallocates_and_locks(self):
		se = self._make_se()
		with patch.object(
			se_events.frappe, "conf", frappe._dict(serialize_stock_submit_by_item=False)
		), patch.object(se_events, "lock_items") as lock_items, patch.object(
			se_events, "preallocate_series_for_docs"
		) as prealloc, patch.object(se_events, "lock_bins_for_rows") as lock_bins:
			se_events.prelock_bins(se, method=None)
		prealloc.assert_called_once_with(se)
		lock_bins.assert_called_once_with(se.items, "s_warehouse", "t_warehouse")
		lock_items.assert_not_called()

	def test_prelock_bins_serialize_by_item(self):
		se = self._make_se()
		with patch.object(
			se_events.frappe, "conf", frappe._dict(serialize_stock_submit_by_item=True)
		), patch.object(se_events, "lock_items") as lock_items, patch.object(
			se_events, "preallocate_series_for_docs"
		), patch.object(se_events, "lock_bins_for_rows"):
			se_events.prelock_bins(se, method=None)
		lock_items.assert_called_once_with(["I-1", "I-2"])

	def test_prelock_bins_on_cancel_only_locks(self):
		se = self._make_se()
		with patch.object(
			se_events, "preallocate_series_for_docs"
		) as prealloc, patch.object(se_events, "lock_bins_for_rows") as lock_bins:
			se_events.prelock_bins_on_cancel(se, method=None)
		prealloc.assert_not_called()
		lock_bins.assert_called_once_with(se.items, "s_warehouse", "t_warehouse")


# ----------------------------------------------------------------- before_validate
class TestBeforeValidate(_StockEntryTestCase):
	def _make_se(self, items, **extra):
		defaults = {
			"auto_created": 0,
			"docstatus": 0,
			"stock_entry_type": "Material Receipt",
			"purpose": "Material Receipt",
			"company": "GE",
			"main_slip": None,
			"to_main_slip": None,
			"manufacturing_order": None,
			"manufacturer": None,
			"items": items,
		}
		defaults.update(extra)
		se = _Doc(**defaults)
		se.update_batches = MagicMock()
		return se

	def _row(self, **extra):
		defaults = {
			"item_code": "M-G-18KT",
			"s_warehouse": None,
			"t_warehouse": "WH-1",
			"batch_no": None,
			"serial_no": None,
			"custom_variant_of": None,
			"manufacturing_operation": None,
			"inventory_type": None,
			"qty": 5,
		}
		defaults.update(extra)
		return _Row(**defaults)

	def _patched_pipeline(self, **overrides):
		patches = {
			"validate_ir": patch.object(se_events, "validate_ir"),
			"validate_loss_ownership_carried": patch.object(
				se_events, "validate_loss_ownership_carried"
			),
			"validate_pcs": patch.object(se_events, "validate_pcs"),
			"get_receive_work_order_batch": patch.object(
				se_events, "get_receive_work_order_batch"
			),
			"validate_metal_properties": patch.object(
				se_events, "validate_metal_properties"
			),
			"allow_zero_valuation": patch.object(se_events, "allow_zero_valuation"),
			"bulk_map": patch.object(se_events, "bulk_map", return_value={}),
			# flt() with a precision calls rounded() -> frappe.get_system_settings(
			# "rounding_method"), a real DB/cache read. Only the scaled-purity branch
			# of before_validate reaches it (flt((item_purity * qty) /
			# pure_item_purity, 3)); the equal-purity branch assigns row.qty directly
			# and never calls flt. On CI's disposable test_site that lookup raised, and
			# flt swallowed the exception into 0.0 (test_pure_qty_scaled_purity:
			# 0.0 != 6.0). Pin a valid method so the round is deterministic -- every
			# valid method yields the same result for the assertion.
			"get_system_settings": patch.object(
				se_events.frappe,
				"get_system_settings",
				return_value="Banker's Rounding (legacy)",
			),
		}
		patches.update(overrides)
		return patches

	def test_draft_runs_pipeline_and_defaults_inventory_type(self):
		row = self._row()
		se = self._make_se([row])
		ctx = self._patched_pipeline()
		with ctx["validate_ir"], ctx["validate_loss_ownership_carried"], ctx[
			"validate_pcs"
		] as validate_pcs, ctx["get_receive_work_order_batch"] as get_receive, ctx[
			"validate_metal_properties"
		] as vmp, ctx["allow_zero_valuation"] as azv, ctx["bulk_map"]:
			se_events.before_validate(se, method=None)
		se.update_batches.assert_called_once()
		validate_pcs.assert_called_once_with(se)
		get_receive.assert_not_called()
		vmp.assert_not_called()
		azv.assert_called_once_with(se)
		self.assertEqual(row.inventory_type, "Regular Stock")

	def test_batch_tracked_source_without_batch_throws(self):
		row = self._row(s_warehouse="WH-1")
		se = self._make_se([row])
		item_map = {"M-G-18KT": frappe._dict(has_batch_no=1)}
		ctx = self._patched_pipeline(
			bulk_map=patch.object(se_events, "bulk_map", return_value=item_map)
		)
		with ctx["validate_ir"], ctx["validate_loss_ownership_carried"], ctx[
			"validate_pcs"
		], ctx["allow_zero_valuation"], ctx["bulk_map"]:
			raised, throw = _capture_throw(se_events.before_validate, se, method=None)
		self.assertTrue(raised)
		msg = throw.call_args[0][0]
		self.assertIn("M-G-18KT", msg)
		self.assertIn("WH-1", msg)

	def test_department_in_transit_throws(self):
		row = self._row(manufacturing_operation="MOP-1")
		se = self._make_se([row])
		ctx = self._patched_pipeline()
		with ctx["validate_ir"], ctx["validate_loss_ownership_carried"], ctx[
			"validate_pcs"
		], ctx["allow_zero_valuation"], ctx["bulk_map"], patch.object(
			se_events.frappe.db, "get_value", return_value="In-Transit"
		) as gv:
			raised, throw = _capture_throw(se_events.before_validate, se, method=None)
		self.assertTrue(raised)
		gv.assert_called_once_with(
			"Manufacturing Operation", "MOP-1", "department_ir_status"
		)
		self.assertIn("MOP-1", throw.call_args[0][0])

	def test_material_receipt_work_order_calls_get_receive_batch(self):
		row = self._row()
		se = self._make_se(
			[row],
			stock_entry_type="Material Receive (WORK ORDER)",
			purpose="Material Receipt",
		)
		ctx = self._patched_pipeline()
		with ctx["validate_ir"], ctx["validate_loss_ownership_carried"], ctx[
			"validate_pcs"
		], ctx["get_receive_work_order_batch"] as get_receive, ctx[
			"allow_zero_valuation"
		] as azv, ctx["bulk_map"]:
			se_events.before_validate(se, method=None)
		get_receive.assert_called_once_with(se)
		azv.assert_called_once_with(se)

	def test_material_transfer_purpose_routes_to_metal_properties(self):
		row = self._row()
		se = self._make_se(
			[row], stock_entry_type="Material Transfer", purpose="Material Transfer"
		)
		ctx = self._patched_pipeline()
		with ctx["validate_ir"], ctx["validate_loss_ownership_carried"], ctx[
			"validate_pcs"
		], ctx["validate_metal_properties"] as vmp, ctx[
			"allow_zero_valuation"
		] as azv, ctx["bulk_map"]:
			se_events.before_validate(se, method=None)
		vmp.assert_called_once_with(se)
		azv.assert_not_called()

	def _pure_se(self, row):
		return self._make_se(
			[row], stock_entry_type="Material Transfer", purpose="Material Transfer"
		)

	def test_pure_qty_equal_purity(self):
		row = self._row(custom_variant_of="M", qty=8)
		se = self._pure_se(row)
		purity = {"PURE-ITEM": 1.0, "M-G-18KT": 1.0}
		ctx = self._patched_pipeline(
			get_value=patch.object(
				se_events.frappe.db, "get_value", return_value="PURE-ITEM"
			),
			get_purity_percentage=patch.object(
				se_events,
				"get_purity_percentage",
				side_effect=lambda code: purity[code],
			),
			MANUFACTURER=patch.object(se_events, "MANUFACTURER", None),
		)
		with ctx["validate_ir"], ctx["validate_loss_ownership_carried"], ctx[
			"validate_pcs"
		], ctx["validate_metal_properties"], ctx["bulk_map"], ctx["get_value"], ctx[
			"get_purity_percentage"
		], ctx["MANUFACTURER"]:
			se_events.before_validate(se, method=None)
		self.assertEqual(row.custom_pure_qty, 8)

	def test_pure_qty_scaled_purity(self):
		row = self._row(custom_variant_of="M", qty=8)
		se = self._pure_se(row)
		purity = {"PURE-ITEM": 1.0, "M-G-18KT": 0.75}
		ctx = self._patched_pipeline(
			get_value=patch.object(
				se_events.frappe.db, "get_value", return_value="PURE-ITEM"
			),
			get_purity_percentage=patch.object(
				se_events,
				"get_purity_percentage",
				side_effect=lambda code: purity[code],
			),
			MANUFACTURER=patch.object(se_events, "MANUFACTURER", None),
		)
		with ctx["validate_ir"], ctx["validate_loss_ownership_carried"], ctx[
			"validate_pcs"
		], ctx["validate_metal_properties"], ctx["bulk_map"], ctx["get_value"], ctx[
			"get_purity_percentage"
		], ctx["get_system_settings"], ctx["MANUFACTURER"]:
			se_events.before_validate(se, method=None)
		self.assertEqual(row.custom_pure_qty, 6.0)

	def test_pure_item_company_fallback_when_single_setting(self):
		row = self._row(custom_variant_of="M", qty=8)
		se = self._pure_se(row)
		ctx = self._patched_pipeline(
			get_value=patch.object(se_events.frappe.db, "get_value", return_value=None),
			get_all=patch.object(
				se_events.frappe,
				"get_all",
				return_value=[
					frappe._dict(
						name="S-1", manufacturer=None, pure_gold_item="PURE-ITEM"
					)
				],
			),
			get_purity_percentage=patch.object(
				se_events, "get_purity_percentage", return_value=1.0
			),
			MANUFACTURER=patch.object(se_events, "MANUFACTURER", None),
		)
		with ctx["validate_ir"], ctx["validate_loss_ownership_carried"], ctx[
			"validate_pcs"
		], ctx["validate_metal_properties"], ctx["bulk_map"], ctx["get_value"], ctx[
			"get_all"
		] as get_all, ctx["get_purity_percentage"], ctx["MANUFACTURER"]:
			se_events.before_validate(se, method=None)
		get_all.assert_called_once()
		self.assertEqual(row.custom_pure_qty, 8)

	def test_missing_pure_item_setting_throws(self):
		row = self._row(custom_variant_of="M", qty=8)
		se = self._pure_se(row)
		ctx = self._patched_pipeline(
			get_value=patch.object(se_events.frappe.db, "get_value", return_value=None),
			get_all=patch.object(se_events.frappe, "get_all", return_value=[]),
			MANUFACTURER=patch.object(se_events, "MANUFACTURER", None),
		)
		with ctx["validate_ir"], ctx["validate_loss_ownership_carried"], ctx[
			"validate_pcs"
		], ctx["validate_metal_properties"], ctx["bulk_map"], ctx["get_value"], ctx[
			"get_all"
		], ctx["MANUFACTURER"]:
			raised, throw = _capture_throw(se_events.before_validate, se, method=None)
		self.assertTrue(raised)
		self.assertIn("Pure Gold Item", throw.call_args[0][0])

	def test_pure_qty_zero(self):
		row = self._row(custom_variant_of="M", qty=0)
		se = self._pure_se(row)
		purity = {"PURE-ITEM": 1.0, "M-G-18KT": 1.0}
		ctx = self._patched_pipeline(
			get_value=patch.object(
				se_events.frappe.db, "get_value", return_value="PURE-ITEM"
			),
			get_purity_percentage=patch.object(
				se_events,
				"get_purity_percentage",
				side_effect=lambda code: purity[code],
			),
			MANUFACTURER=patch.object(se_events, "MANUFACTURER", None),
		)
		with ctx["validate_ir"], ctx["validate_loss_ownership_carried"], ctx[
			"validate_pcs"
		], ctx["validate_metal_properties"], ctx["bulk_map"], ctx["get_value"], ctx[
			"get_purity_percentage"
		], ctx["MANUFACTURER"]:
			se_events.before_validate(se, method=None)
		self.assertEqual(row.custom_pure_qty, 0)

	def test_item_without_purity_skipped(self):
		row = self._row(custom_variant_of="M", qty=8)
		se = self._pure_se(row)
		ctx = self._patched_pipeline(
			get_value=patch.object(
				se_events.frappe.db, "get_value", return_value="PURE-ITEM"
			),
			get_purity_percentage=patch.object(
				se_events,
				"get_purity_percentage",
				side_effect=lambda code: {"PURE-ITEM": 1.0}.get(code),
			),
			MANUFACTURER=patch.object(se_events, "MANUFACTURER", None),
		)
		with ctx["validate_ir"], ctx["validate_loss_ownership_carried"], ctx[
			"validate_pcs"
		], ctx["validate_metal_properties"], ctx["bulk_map"], ctx["get_value"], ctx[
			"get_purity_percentage"
		], ctx["MANUFACTURER"]:
			se_events.before_validate(se, method=None)
		# item_purity is None -> the row is skipped, custom_pure_qty stays unset
		self.assertIsNone(getattr(row, "custom_pure_qty", None))

	def test_no_company_no_pure_item_setting_throws(self):
		row = self._row(custom_variant_of="M", qty=8)
		se = self._make_se(
			[row],
			company=None,
			stock_entry_type="Material Transfer",
			purpose="Material Transfer",
		)
		ctx = self._patched_pipeline(
			get_value=patch.object(se_events.frappe.db, "get_value", return_value=None),
			get_all=patch.object(se_events.frappe, "get_all", return_value=[]),
			MANUFACTURER=patch.object(se_events, "MANUFACTURER", None),
		)
		with ctx["validate_ir"], ctx["validate_loss_ownership_carried"], ctx[
			"validate_pcs"
		], ctx["validate_metal_properties"], ctx["bulk_map"], ctx["get_value"], ctx[
			"get_all"
		], ctx["MANUFACTURER"]:
			raised, throw = _capture_throw(se_events.before_validate, se, method=None)
		self.assertTrue(raised)
		self.assertIn("Set Pure Gold Item", throw.call_args[0][0])


# ------------------------------------------------- CustomStockEntry.update_batches
class TestCustomStockEntryUpdateBatches(_StockEntryTestCase):
	def _run(
		self,
		items,
		item_map,
		dept_map=None,
		batch_map=None,
		fifo_result=None,
		attribute_rows=(),
		sieve_weight=None,
	):
		dept_map = dept_map or {}
		batch_map = batch_map or {}
		cse = _bare_cse(
			auto_created=0, name=None, items=list(items), source_warehouse=None
		)
		fifo_calls = []

		def _bulk_map(doctype, names, fields):
			if doctype == "Item":
				return {n: frappe._dict(item_map.get(n, {})) for n in names}
			if doctype == "Department":
				return {n: frappe._dict(dept_map.get(n, {})) for n in names}
			return {n: frappe._dict(batch_map.get(n, {})) for n in names}

		def _fifo(se, row, consumed):
			fifo_calls.append((row, dict(consumed)))
			return list(fifo_result or [])

		def _get_all(doctype, *args, **kwargs):
			if doctype == "Item Variant Attribute":
				return [frappe._dict(r) for r in attribute_rows]
			return []

		with patch.object(cse_mod, "bulk_map", side_effect=_bulk_map), patch.object(
			cse_mod, "get_fifo_batches", side_effect=_fifo
		), patch.object(cse_mod.frappe, "get_all", side_effect=_get_all), patch.object(
			cse_mod.frappe.db, "get_value", return_value=sieve_weight
		), patch.object(
			cse_mod.frappe, "throw", side_effect=RuntimeError
		) as throw, patch.object(cse_mod.frappe.db, "exists", return_value=False):
			cse.update_batches()

		return cse, cse.appended, fifo_calls, throw

	def _row(self, **extra):
		defaults = {
			"item_code": "M-G-18KT",
			"s_warehouse": None,
			"t_warehouse": None,
			"batch_no": None,
			"qty": 5,
			"department": None,
			"inventory_type": None,
			"customer": None,
			"pcs": 0,
		}
		defaults.update(extra)
		return _Row(**defaults)

	def test_non_batch_row_passthrough(self):
		item = "M-G-18KT"
		row = self._row(item_code=item)
		item_map = {item: {"variant_of": "M", "has_batch_no": 0}}
		cse, appended, fifo_calls, throw = self._run([row], item_map)
		self.assertEqual(fifo_calls, [])
		throw.assert_not_called()
		self.assertEqual(len(appended), 1)
		self.assertEqual(appended[0].item_code, item)
		self.assertEqual(appended[0].qty, 5)

	def test_filled_batch_row_kept_and_consumption_recorded(self):
		item = "M-G-18KT"
		row1 = self._row(item_code=item, s_warehouse="WH-1", batch_no="B-1", qty=5)
		row2 = self._row(item_code=item, s_warehouse="WH-1", batch_no=None, qty=3)
		item_map = {item: {"variant_of": "M", "has_batch_no": 1}}
		batch_map = {
			"B-1": {
				"custom_inventory_type": "Customer Goods",
				"custom_customer": "CUST-1",
			}
		}
		cse, appended, fifo_calls, throw = self._run(
			[row1, row2], item_map, batch_map=batch_map
		)
		self.assertEqual(len(fifo_calls), 1)
		fifo_row, consumed_at_call = fifo_calls[0]
		self.assertEqual(fifo_row.item_code, item)
		# row1's consumption is visible to row2's FIFO allocation -> B-1 cannot double-book
		self.assertEqual(consumed_at_call, {("WH-1", "B-1"): 5.0})
		self.assertEqual(len(appended), 1)
		self.assertEqual(appended[0].batch_no, "B-1")
		self.assertEqual(appended[0].inventory_type, "Customer Goods")
		self.assertEqual(appended[0].customer, "CUST-1")
		throw.assert_not_called()

	def test_empty_batch_row_triggers_fifo(self):
		item = "M-G-18KT"
		row = self._row(item_code=item, s_warehouse="WH-1")
		item_map = {item: {"variant_of": "M", "has_batch_no": 1}}
		cse, appended, fifo_calls, throw = self._run([row], item_map)
		self.assertEqual(len(fifo_calls), 1)
		self.assertEqual(fifo_calls[0][0].item_code, item)
		# fifo returned no rows -> nothing to rebuild
		self.assertEqual(appended, [])
		throw.assert_not_called()

	def test_target_only_batch_row_cloned(self):
		item = "M-G-18KT"
		row = self._row(item_code=item, t_warehouse="WH-T")
		item_map = {item: {"variant_of": "M", "has_batch_no": 1}}
		cse, appended, fifo_calls, throw = self._run([row], item_map)
		self.assertEqual(fifo_calls, [])
		self.assertEqual(len(appended), 1)
		self.assertEqual(appended[0].t_warehouse, "WH-T")
		self.assertEqual(appended[0].batch_no, None)

	def test_dg_item_in_dg_blocked_department_throws(self):
		item = "D-1"
		row = self._row(
			item_code=item, s_warehouse="WH-1", batch_no="B-1", department="DEPT-DG"
		)
		item_map = {item: {"variant_of": "D", "has_batch_no": 1}}
		dept_map = {"DEPT-DG": {"custom_can_not_make_dg_entry": 1}}

		def _bulk_map(doctype, names, fields):
			if doctype == "Item":
				return {n: frappe._dict(item_map.get(n, {})) for n in names}
			if doctype == "Department":
				return {n: frappe._dict(dept_map.get(n, {})) for n in names}
			return {}

		cse = _bare_cse(auto_created=0, name=None, items=[row], source_warehouse=None)
		with patch.object(cse_mod, "bulk_map", side_effect=_bulk_map), patch.object(
			cse_mod.frappe, "throw", side_effect=RuntimeError
		) as throw:
			with self.assertRaises(RuntimeError):
				cse.update_batches()
		msg = throw.call_args[0][0]
		self.assertIn("D-1", msg)
		self.assertIn("DEPT-DG", msg)

	def test_diamond_pcs_computed_and_batch_backfill(self):
		item = "D-1"
		row = self._row(item_code=item, s_warehouse="WH-1", batch_no="B-DIA", qty=10)
		item_map = {item: {"variant_of": "D", "has_batch_no": 1}}
		batch_map = {
			"B-DIA": {
				"custom_inventory_type": "Customer Goods",
				"custom_customer": "CUST-1",
			}
		}
		attribute_rows = [
			{"parent": item, "attribute": "Diamond Grade", "attribute_value": "AG"},
			{
				"parent": item,
				"attribute": "Diamond Sieve Size",
				"attribute_value": "0.30",
			},
		]
		cse, appended, fifo_calls, throw = self._run(
			[row],
			item_map,
			batch_map=batch_map,
			attribute_rows=attribute_rows,
			sieve_weight=2.0,
		)
		self.assertEqual(len(appended), 1)
		self.assertEqual(appended[0].pcs, 5)  # int(10 / 2.0)
		self.assertEqual(appended[0].inventory_type, "Customer Goods")
		self.assertEqual(appended[0].customer, "CUST-1")
		throw.assert_not_called()


# --------------------------------------------- CustomStockEntry.validate_with_material_request
class TestCustomStockEntryValidateWithMaterialRequest(_StockEntryTestCase):
	def _cse(
		self,
		purpose="Material Transfer",
		outgoing_stock_entry=None,
		add_to_transit=False,
		items=None,
	):
		return _bare_cse(
			purpose=purpose,
			outgoing_stock_entry=outgoing_stock_entry,
			add_to_transit=add_to_transit,
			items=items or [],
		)

	def _mr_item_row(self, item_code="M-G-18KT"):
		return _Row(
			item_code=item_code,
			material_request="MR-1",
			material_request_item="MRI-1",
			idx=1,
			ste_detail=None,
		)

	def test_matching_item_passes(self):
		cse = self._cse(items=[self._mr_item_row()])
		with patch.object(cse_mod.frappe, "get_value", return_value=None), patch.object(
			cse_mod.frappe.db,
			"get_value",
			return_value=frappe._dict(
				item_code="M-G-18KT",
				custom_alternative_item=None,
				warehouse="WH-1",
				idx=1,
			),
		), patch.object(cse_mod.frappe, "throw") as throw:
			cse.validate_with_material_request()
		throw.assert_not_called()

	def test_alternative_item_matches(self):
		cse = self._cse(items=[self._mr_item_row(item_code="ALT-1")])
		with patch.object(cse_mod.frappe, "get_value", return_value=None), patch.object(
			cse_mod.frappe.db,
			"get_value",
			return_value=frappe._dict(
				item_code="M-G-18KT",
				custom_alternative_item="ALT-1",
				warehouse="WH-1",
				idx=1,
			),
		), patch.object(cse_mod.frappe, "throw") as throw:
			cse.validate_with_material_request()
		throw.assert_not_called()

	def test_mismatched_item_raises_mapping_mismatch(self):
		cse = self._cse(items=[self._mr_item_row(item_code="M-OTHER")])
		with patch.object(cse_mod.frappe, "get_value", return_value=None), patch.object(
			cse_mod.frappe.db,
			"get_value",
			return_value=frappe._dict(
				item_code="M-G-18KT",
				custom_alternative_item=None,
				warehouse="WH-1",
				idx=1,
			),
		), patch.object(
			cse_mod.frappe, "throw", side_effect=frappe.MappingMismatchError
		) as throw:
			with self.assertRaises(frappe.MappingMismatchError):
				cse.validate_with_material_request()
		self.assertIn("Item for row", throw.call_args[0][0])

	def test_material_transfer_with_outgoing_se_resolves_parent_mr(self):
		row = self._mr_item_row()
		row.ste_detail = "SED-9"
		cse = self._cse(outgoing_stock_entry="SE-OUT", items=[row])

		def _get_value(doctype, *args, **kwargs):
			if doctype == "Stock Entry Detail":
				return frappe._dict(
					material_request="MR-PARENT", material_request_item="MRI-PARENT"
				)
			return None

		with patch.object(
			cse_mod.frappe, "get_value", side_effect=_get_value
		), patch.object(
			cse_mod.frappe.db,
			"get_value",
			return_value=frappe._dict(
				item_code="M-G-18KT",
				custom_alternative_item=None,
				warehouse="WH-1",
				idx=1,
			),
		) as db_get_value, patch.object(cse_mod.frappe, "throw") as throw:
			cse.validate_with_material_request()
		throw.assert_not_called()
		# the Material Request Item lookup used the parent SE detail's MR fields
		filters = db_get_value.call_args[0][1]
		self.assertEqual(filters["name"], "MRI-PARENT")
		self.assertEqual(filters["parent"], "MR-PARENT")

	def test_add_to_transit_continues(self):
		cse = self._cse(add_to_transit=True, items=[self._mr_item_row()])
		with patch.object(cse_mod.frappe, "get_value", return_value=None), patch.object(
			cse_mod.frappe.db,
			"get_value",
			return_value=frappe._dict(
				item_code="M-G-18KT",
				custom_alternative_item=None,
				warehouse="WH-1",
				idx=1,
			),
		), patch.object(cse_mod.frappe, "throw") as throw:
			cse.validate_with_material_request()
		throw.assert_not_called()


# --------------------------------------------------- customization before_validate chain
class TestCustomizationBeforeValidate(_StockEntryTestCase):
	def test_freeze_time_throws_before_guards(self):
		se = _Doc()
		with patch.object(
			cse_mod, "in_configured_timeslot", return_value=False
		), patch.object(cse_mod, "validate_customer_voucher") as vcv, patch.object(
			cse_mod.frappe, "throw", side_effect=RuntimeError
		) as throw:
			with self.assertRaises(RuntimeError):
				cse_mod.before_validate(se, method=None)
		vcv.assert_not_called()
		self.assertIn("freeze time", throw.call_args[0][0])

	def test_happy_path_calls_all_guards_in_order(self):
		se = _Doc()
		calls = []

		def _record(name):
			def wrapper(*args, **kwargs):
				calls.append(name)

			return wrapper

		with patch.object(
			cse_mod, "in_configured_timeslot", return_value=True
		), patch.object(
			cse_mod,
			"validate_customer_voucher",
			side_effect=_record("validate_customer_voucher"),
		), patch.object(
			cse_mod,
			"validate_sample_goods_not_consumed",
			side_effect=_record("validate_sample_goods_not_consumed"),
		), patch.object(
			cse_mod, "set_employee", side_effect=_record("set_employee")
		), patch.object(
			cse_mod, "set_gross_wt", side_effect=_record("set_gross_wt")
		), patch.object(
			cse_mod, "set_jwelex_tag_no", side_effect=_record("set_jwelex_tag_no")
		), patch.object(
			cse_mod, "validate_warehouse", side_effect=_record("validate_warehouse")
		):
			cse_mod.before_validate(se, method=None)
		self.assertEqual(
			calls,
			[
				"validate_customer_voucher",
				"validate_sample_goods_not_consumed",
				"set_employee",
				"set_gross_wt",
				"set_jwelex_tag_no",
				"validate_warehouse",
			],
		)


# ------------------------------------------------------------------- se_utils guards
class TestSeUtilsGuards(_StockEntryTestCase):
	def test_set_employee_only_for_work_order_material_transfer(self):
		se = _Doc(stock_entry_type="Material Transfer")
		with patch.object(se_utils.frappe.db, "get_value") as gv:
			se_utils.set_employee(se)
		gv.assert_not_called()

	def test_set_employee_wip_sets_to_employee(self):
		se = _Doc(
			stock_entry_type="Material Transfer (WORK ORDER)",
			manufacturing_operation="MOP-1",
		)
		with patch.object(
			se_utils.frappe.db,
			"get_value",
			return_value=frappe._dict(status="WIP", employee="EMP-1"),
		):
			se_utils.set_employee(se)
		self.assertEqual(se.to_employee, "EMP-1")

	def test_set_employee_non_wip_skips(self):
		se = _Doc(
			stock_entry_type="Material Transfer (WORK ORDER)",
			manufacturing_operation="MOP-1",
		)
		with patch.object(
			se_utils.frappe.db,
			"get_value",
			return_value=frappe._dict(status="Completed", employee="EMP-1"),
		):
			se_utils.set_employee(se)
		self.assertFalse(hasattr(se, "to_employee"))

	def test_set_gross_wt_from_serial_no(self):
		row = _Row(serial_no="S-1", gross_weight=None)
		se = _Doc(items=[row])
		with patch.object(se_utils.frappe.db, "get_value", return_value=3.5) as gv:
			se_utils.set_gross_wt(se)
		gv.assert_called_once_with("Serial No", "S-1", "custom_gross_wt")
		self.assertEqual(row.gross_weight, 3.5)

	def test_set_gross_wt_ignores_non_serialized_rows(self):
		row = _Row(serial_no=None, gross_weight=None)
		se = _Doc(items=[row])
		with patch.object(se_utils.frappe.db, "get_value") as gv:
			se_utils.set_gross_wt(se)
		gv.assert_not_called()

	@staticmethod
	def _tag_map(**tags):
		"""``bulk_map`` shape: {serial: _dict(custom_jwelex_tag_no=...)}."""
		return {s: frappe._dict(custom_jwelex_tag_no=t) for s, t in tags.items()}

	def test_set_jwelex_tag_no_from_serial_no(self):
		row = _Row(serial_no="S-1", custom_jwelex_tag_no=None)
		se = _Doc(items=[row])
		with patch.object(
			se_utils, "bulk_map", return_value=self._tag_map(**{"S-1": "GXU56855"})
		) as bm:
			se_utils.set_jwelex_tag_no(se)
		bm.assert_called_once_with("Serial No", ["S-1"], ["custom_jwelex_tag_no"])
		self.assertEqual(row.custom_jwelex_tag_no, "GXU56855")

	def test_set_jwelex_tag_no_joins_all_serials(self):
		"""A row carries one serial per qty; the tag field mirrors that list."""
		row = _Row(serial_no="S-1\nS-2\nS-3", custom_jwelex_tag_no=None)
		se = _Doc(items=[row])
		tags = self._tag_map(**{"S-1": "T-1", "S-2": "T-2", "S-3": "T-3"})
		with patch.object(se_utils, "bulk_map", return_value=tags):
			se_utils.set_jwelex_tag_no(se)
		self.assertEqual(row.custom_jwelex_tag_no, "T-1\nT-2\nT-3")

	def test_set_jwelex_tag_no_keeps_blank_line_for_untagged_serial(self):
		"""Line N must stay the tag of serial N, so a gap is preserved."""
		row = _Row(serial_no="S-1\nS-2\nS-3", custom_jwelex_tag_no=None)
		se = _Doc(items=[row])
		tags = self._tag_map(**{"S-1": "T-1", "S-2": None, "S-3": "T-3"})
		with patch.object(se_utils, "bulk_map", return_value=tags):
			se_utils.set_jwelex_tag_no(se)
		self.assertEqual(row.custom_jwelex_tag_no, "T-1\n\nT-3")

	def test_set_jwelex_tag_no_handles_crlf_and_trailing_newline(self):
		row = _Row(serial_no="S-1\r\nS-2\n", custom_jwelex_tag_no=None)
		se = _Doc(items=[row])
		tags = self._tag_map(**{"S-1": "T-1", "S-2": "T-2"})
		with patch.object(se_utils, "bulk_map", return_value=tags) as bm:
			se_utils.set_jwelex_tag_no(se)
		bm.assert_called_once_with(
			"Serial No", ["S-1", "S-2"], ["custom_jwelex_tag_no"]
		)
		self.assertEqual(row.custom_jwelex_tag_no, "T-1\nT-2")

	def test_set_jwelex_tag_no_prefetches_once_for_all_rows(self):
		"""Guards the N+1: one query per document, not per serial."""
		rows = [
			_Row(serial_no="S-1\nS-2", custom_jwelex_tag_no=None),
			_Row(serial_no="S-3", custom_jwelex_tag_no=None),
		]
		se = _Doc(items=rows)
		tags = self._tag_map(**{"S-1": "T-1", "S-2": "T-2", "S-3": "T-3"})
		with patch.object(se_utils, "bulk_map", return_value=tags) as bm:
			se_utils.set_jwelex_tag_no(se)
		bm.assert_called_once_with(
			"Serial No", ["S-1", "S-2", "S-3"], ["custom_jwelex_tag_no"]
		)
		self.assertEqual(rows[0].custom_jwelex_tag_no, "T-1\nT-2")
		self.assertEqual(rows[1].custom_jwelex_tag_no, "T-3")

	def test_set_jwelex_tag_no_ignores_non_serialized_rows(self):
		row = _Row(serial_no=None, custom_jwelex_tag_no="STALE")
		se = _Doc(items=[row])
		with patch.object(se_utils, "bulk_map", return_value={}) as bm:
			se_utils.set_jwelex_tag_no(se)
		bm.assert_called_once_with("Serial No", [], ["custom_jwelex_tag_no"])
		self.assertEqual(row.custom_jwelex_tag_no, "STALE")

	def test_set_jwelex_tag_no_clears_when_no_serial_has_a_tag(self):
		"""Empty, not a run of blank lines."""
		row = _Row(serial_no="S-1\nS-2", custom_jwelex_tag_no="STALE")
		se = _Doc(items=[row])
		with patch.object(se_utils, "bulk_map", return_value={}):
			se_utils.set_jwelex_tag_no(se)
		self.assertIsNone(row.custom_jwelex_tag_no)

	def test_get_jwelex_tag_no_matches_the_stamper(self):
		"""The whitelisted client endpoint shares the resolver."""
		tags = self._tag_map(**{"S-1": "T-1", "S-2": None, "S-3": "T-3"})
		with patch.object(se_utils, "bulk_map", return_value=tags):
			self.assertEqual(se_utils.get_jwelex_tag_no("S-1\nS-2\nS-3"), "T-1\n\nT-3")
			self.assertIsNone(se_utils.get_jwelex_tag_no(""))

	def test_validate_warehouse_same_from_to_throws(self):
		se = _Doc(
			stock_entry_type="Material Transfer (WORK ORDER)",
			from_warehouse="WH-1",
			to_warehouse="WH-1",
		)
		raised, throw = _capture_throw(se_utils.validate_warehouse, se)
		self.assertTrue(raised)
		self.assertIn("source warehouse", throw.call_args[0][0])

	def test_validate_warehouse_other_type_noop(self):
		se = _Doc(
			stock_entry_type="Material Transfer",
			from_warehouse="WH-1",
			to_warehouse="WH-1",
		)
		with patch.object(se_utils.frappe, "throw") as throw:
			se_utils.validate_warehouse(se)
		throw.assert_not_called()


# ------------------------------------------------------------ inventory_utils guards
class TestInventoryUtilsGuards(_StockEntryTestCase):
	def test_customer_voucher_none_noop(self):
		se = _Doc(customer_voucher_type=None, items=[_Row(item_code="I-1")])
		with patch.object(iu.frappe.db, "get_value") as gv:
			iu.validate_customer_voucher(se)
		gv.assert_not_called()

	def test_subcontracting_serialized_item_throws(self):
		se = _Doc(
			customer_voucher_type="Customer Subcontracting",
			items=[_Row(item_code="I-1")],
		)
		with patch.object(iu.frappe.db, "get_value", return_value=1):
			raised, throw = _capture_throw(iu.validate_customer_voucher, se)
		self.assertTrue(raised)
		self.assertIn("Serialized", throw.call_args[0][0])

	def test_subcontracting_non_serialized_passes(self):
		se = _Doc(
			customer_voucher_type="Customer Subcontracting",
			items=[_Row(item_code="I-1")],
		)
		with patch.object(iu.frappe.db, "get_value", return_value=0):
			raised, throw = _capture_throw(iu.validate_customer_voucher, se)
		self.assertFalse(raised)

	def test_repair_batch_item_throws(self):
		se = _Doc(
			customer_voucher_type="Customer Repair", items=[_Row(item_code="I-1")]
		)
		with patch.object(iu.frappe.db, "get_value", return_value=1):
			raised, throw = _capture_throw(iu.validate_customer_voucher, se)
		self.assertTrue(raised)
		self.assertIn("Batch", throw.call_args[0][0])

	def test_sample_goods_manufacturing_warehouse_throws(self):
		se = _Doc(
			customer_voucher_type="Customer Sample Goods",
			items=[
				_Row(
					item_code="I-1",
					t_warehouse="WH-MFG",
					manufacturing_operation="MOP-1",
				)
			],
		)
		with patch.object(iu.frappe.db, "get_value", return_value="Manufacturing"):
			raised, throw = _capture_throw(iu.validate_customer_voucher, se)
		self.assertTrue(raised)
		self.assertIn("Manufacturing Type warehouse", throw.call_args[0][0])

	def test_timeslot_no_freeze(self):
		se = _Doc(company="GE")
		company_doc = frappe._dict(custom_freeze_entries=0)
		with patch.object(iu.frappe, "get_cached_doc", return_value=company_doc):
			self.assertTrue(iu.in_configured_timeslot(se))

	def test_timeslot_role_exempt(self):
		se = _Doc(company="GE")
		company_doc = frappe._dict(
			custom_freeze_entries=1,
			custom_ignore_freeze_for_role="Accounts Manager",
		)
		with patch.object(
			iu.frappe, "get_cached_doc", return_value=company_doc
		), patch.object(iu.frappe, "get_roles", return_value=["Accounts Manager"]):
			self.assertTrue(iu.in_configured_timeslot(se))

	def test_timeslot_monthly_not_end_of_month(self):
		se = _Doc(company="GE")
		company_doc = frappe._dict(
			custom_freeze_entries=1,
			custom_ignore_freeze_for_role=None,
			custom_freeze_type="Monthly",
			custom_end_of_month=1,
		)
		with patch.object(
			iu.frappe, "get_cached_doc", return_value=company_doc
		), patch.object(iu, "is_last_day_of_the_month", return_value=False):
			self.assertTrue(iu.in_configured_timeslot(se))


# ----------------------------------------------------- batch_rename.create_parent_batches
class TestCreateParentBatches(_StockEntryTestCase):
	def _run(self, doc, serial="01"):
		inserted = []

		def _new_doc(doctype):
			batch = frappe._dict()
			batch.insert = MagicMock()
			inserted.append(batch)
			return batch

		mock_dt = MagicMock()
		mock_dt.today.return_value = datetime(2023, 5, 15)

		with patch.object(
			batch_rename, "get_year_code", return_value="A"
		), patch.object(
			batch_rename, "get_next_serial", return_value=serial
		), patch.object(
			batch_rename, "_source_row_rate", return_value=100.0
		), patch.object(
			batch_rename.frappe, "new_doc", side_effect=_new_doc
		), patch.object(
			batch_rename.frappe.db, "exists", return_value=False
		), patch.object(batch_rename, "datetime", mock_dt):
			batch_rename.create_parent_batches(doc, method=None)
		return inserted

	def _se(self, items):
		return _Doc(
			doctype="Stock Entry",
			stock_entry_type="Customer Goods Received",
			_customer="CUST-1",
			name="SE-1",
			items=items,
		)

	def test_skips_non_se_doctype(self):
		inserted = self._run(_Doc(doctype="Sales Order"))
		self.assertEqual(inserted, [])

	def test_skips_non_24kt_item(self):
		doc = self._se([_Row(item_code="M-G-18KT", batch_no=None, customer=None)])
		inserted = self._run(doc)
		self.assertEqual(inserted, [])

	def test_skips_item_with_existing_batch(self):
		doc = self._se(
			[_Row(item_code="24KT-GOLD", batch_no="B-EXIST", customer="CUST-1")]
		)
		inserted = self._run(doc)
		self.assertEqual(inserted, [])

	def test_skips_without_customer(self):
		doc = _Doc(
			doctype="Stock Entry",
			stock_entry_type="Customer Goods Received",
			_customer=None,
			name="SE-1",
			items=[
				_Row(item_code="24KT-GOLD", batch_no=None, customer=None, name="ROW-1")
			],
		)
		inserted = self._run(doc)
		self.assertEqual(inserted, [])

	def test_mints_batch_for_24kt_row(self):
		row = _Row(
			item_code="24KT-GOLD",
			batch_no=None,
			customer="CUST-1",
			name="ROW-1",
			custom_metal_rate=None,
			basic_rate=100.0,
		)
		doc = self._se([row])
		inserted = self._run(doc)
		self.assertEqual(len(inserted), 1)
		batch = inserted[0]
		expected = "CUST-1-A05-24KT-GOLD-01"
		self.assertEqual(batch.batch_id, expected)
		self.assertEqual(batch.reference_doctype, "Stock Entry")
		self.assertEqual(batch.reference_name, "SE-1")
		self.assertEqual(batch.custom_voucher_detail_no, "ROW-1")
		self.assertEqual(batch.custom_customer, "CUST-1")
		self.assertEqual(batch.custom_inventory_type, "Customer Goods")
		self.assertEqual(batch.custom_customer_voucher_type, "Customer Subcontracting")
		self.assertEqual(batch.custom_metal_rate, 100.0)
		batch.insert.assert_called_once_with(ignore_permissions=True)
		self.assertEqual(row.batch_no, expected)

	def test_batch_name_collision_increments_serial(self):
		doc = self._se(
			[
				_Row(
					item_code="24KT-GOLD",
					batch_no=None,
					customer="CUST-1",
					name="ROW-1",
				)
			]
		)
		mock_dt = MagicMock()
		mock_dt.today.return_value = datetime(2023, 5, 15)

		with patch.object(
			batch_rename, "get_year_code", return_value="A"
		), patch.object(
			batch_rename, "get_next_serial", return_value="01"
		), patch.object(
			batch_rename, "_source_row_rate", return_value=0.0
		), patch.object(batch_rename.frappe, "new_doc") as new_doc, patch.object(
			batch_rename.frappe.db, "exists", side_effect=[True, False]
		), patch.object(batch_rename, "datetime", mock_dt):
			batch_rename.create_parent_batches(doc, method=None)
		expected = "CUST-1-A05-24KT-GOLD-02"
		self.assertEqual(new_doc.return_value.batch_id, expected)
		self.assertEqual(doc.items[0].batch_no, expected)


# -------------------------------------------------- validate_metal_properties
class TestValidateMetalProperties(_StockEntryTestCase):
	def _row(
		self,
		item_code="M-G-18KT",
		variant="M",
		mwo=None,
		main_slip=None,
		operation=None,
		inventory_type=None,
	):
		return _Row(
			item_code=item_code,
			custom_variant_of=variant,
			custom_manufacturing_work_order=mwo,
			main_slip=main_slip,
			to_main_slip=None,
			manufacturing_operation=operation,
			inventory_type=inventory_type,
			allow_zero_valuation_rate=None,
		)

	def _mwo(
		self,
		metal_type="Gold",
		metal_touch="18K",
		metal_purity="0.75",
		metal_colour="Yellow",
		**extra,
	):
		data = frappe._dict(
			metal_type=metal_type,
			metal_touch=metal_touch,
			metal_purity=metal_purity,
			metal_colour=metal_colour,
			multicolour=0,
			allowed_colours=None,
		)
		data.update(extra)
		return data

	def _msl(
		self,
		metal_type="Gold",
		metal_touch="18K",
		metal_purity="0.75",
		metal_colour="Yellow",
		check_color=1,
		for_subcontracting=0,
		multicolour=0,
		allowed_colours=None,
		**extra,
	):
		data = frappe._dict(
			metal_type=metal_type,
			metal_touch=metal_touch,
			metal_purity=metal_purity,
			metal_colour=metal_colour,
			check_color=check_color,
			for_subcontracting=for_subcontracting,
			multicolour=multicolour,
			allowed_colours=allowed_colours,
		)
		data.update(extra)
		return data

	def _attrs(
		self,
		metal_type="Gold",
		metal_touch="18K",
		metal_purity="0.75",
		metal_colour="Yellow",
	):
		return [
			{"attribute": "Metal Type", "attribute_value": metal_type},
			{"attribute": "Metal Touch", "attribute_value": metal_touch},
			{"attribute": "Metal Purity", "attribute_value": metal_purity},
			{"attribute": "Metal Colour", "attribute_value": metal_colour},
		]

	def _flags(self, is_manufacturing=0, ignore_work_order=0):
		return frappe._dict(
			custom_is_manufacturing_item=is_manufacturing,
			custom_ignore_work_order=ignore_work_order,
		)

	def _company(self, check_purity="Both", check_colour="Both", check_touch="Both"):
		return frappe._dict(
			check_purity=check_purity,
			check_colour=check_colour,
			check_touch=check_touch,
		)

	def _run(
		self,
		rows,
		*,
		doc_mwo=None,
		doc_manufacturer="MFR-1",
		MANUFACTURER=None,
		mwo=None,
		msl=None,
		item_flags=None,
		item_attrs=None,
		company=None,
	):
		doc = _Doc(
			manufacturing_work_order=doc_mwo,
			manufacturer=doc_manufacturer,
			items=rows,
		)
		calls = []

		def _get_value(doctype, name, *args, **kwargs):
			calls.append((doctype, name))
			if doctype == "Manufacturing Work Order":
				return (mwo or {}).get(name)
			if doctype == "Main Slip":
				return (msl or {}).get(name)
			if doctype == "Item":
				return (item_flags or {}).get(name)
			if doctype == "Manufacturing Setting":
				return company
			return None

		def _get_values(doctype, filters, *args, **kwargs):
			if doctype == "Item Variant Attribute":
				calls.append(("Item Variant Attribute", filters.get("parent")))
				rows = (item_attrs or {}).get(filters.get("parent"), [])
				return [frappe._dict(a) for a in rows]
			return []

		with patch.object(
			se_events.frappe.db, "get_value", side_effect=_get_value
		), patch.object(
			se_events.frappe.db, "get_values", side_effect=_get_values
		), patch.object(se_events, "MANUFACTURER", MANUFACTURER), patch.object(
			se_events.frappe, "throw", side_effect=RuntimeError
		) as throw:
			raised = False
			try:
				se_events.validate_metal_properties(doc)
			except RuntimeError:
				raised = True
		return doc, throw, raised, calls

	def test_clean_mwo_row_passes(self):
		row = self._row(mwo="MWO-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			mwo={"MWO-1": self._mwo()},
			item_attrs={"M-G-18KT": self._attrs()},
			item_flags={"M-G-18KT": self._flags()},
			company=self._company(),
		)
		self.assertFalse(raised)
		throw.assert_not_called()

	def test_metal_type_mismatch_throws(self):
		row = self._row(mwo="MWO-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			mwo={"MWO-1": self._mwo(metal_type="Silver")},
			item_attrs={"M-G-18KT": self._attrs()},
			company=self._company(),
		)
		self.assertTrue(raised)
		msg = throw.call_args[0][0]
		self.assertIn("Only Silver Metal type allowed", msg)
		self.assertIn("MWO-1", msg)

	def test_missing_company_validation_throws(self):
		row = self._row(mwo="MWO-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			mwo={"MWO-1": self._mwo()},
			item_attrs={"M-G-18KT": self._attrs()},
			company=frappe._dict(check_purity="Both", check_colour="Both"),
		)
		self.assertTrue(raised)
		msg = throw.call_args[0][0]
		self.assertIn(
			"Please set all validation options in Manufacturing Settings", msg
		)
		self.assertIn("MFR-1", msg)

	def test_no_company_validations_throws(self):
		row = self._row(mwo="MWO-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			mwo={"MWO-1": self._mwo()},
			item_attrs={"M-G-18KT": self._attrs()},
			company=None,
		)
		self.assertTrue(raised)
		self.assertIn("Please set all validation options", throw.call_args[0][0])

	def test_doc_manufacturer_used_when_module_default_empty(self):
		row = self._row(mwo="MWO-1")
		_ignored, throw, raised, calls = self._run(
			[row],
			mwo={"MWO-1": self._mwo()},
			item_attrs={"M-G-18KT": self._attrs()},
			company=self._company(),
			MANUFACTURER=None,
		)
		self.assertFalse(raised)
		self.assertEqual(
			[_n for d, _n in calls if d == "Manufacturing Setting"],
			[{"manufacturer": "MFR-1"}],
		)

	def test_module_manufacturer_takes_precedence(self):
		row = self._row(mwo="MWO-1")
		_ignored, throw, raised, calls = self._run(
			[row],
			mwo={"MWO-1": self._mwo()},
			item_attrs={"M-G-18KT": self._attrs()},
			company=self._company(),
			MANUFACTURER="MFR-DEFAULT",
		)
		self.assertFalse(raised)
		self.assertEqual(
			[_n for d, _n in calls if d == "Manufacturing Setting"],
			[{"manufacturer": "MFR-DEFAULT"}],
		)

	def test_touch_purity_colour_mismatches_aggregate_in_mwo_message(self):
		row = self._row(mwo="MWO-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			mwo={
				"MWO-1": self._mwo(
					metal_touch="14K", metal_purity="0.58", metal_colour="White"
				)
			},
			item_attrs={"M-G-18KT": self._attrs()},
			item_flags={"M-G-18KT": self._flags()},
			company=self._company(),
		)
		self.assertTrue(raised)
		msg = throw.call_args[0][0]
		self.assertIn("Metal Touch", msg)
		self.assertIn("Metal Purity", msg)
		self.assertIn("Metal Colour", msg)
		self.assertIn("MWO-1", msg)

	def test_ignore_work_order_skips_colour_check(self):
		row = self._row(mwo="MWO-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			mwo={"MWO-1": self._mwo(metal_colour="White")},
			item_attrs={"M-G-18KT": self._attrs()},
			item_flags={"M-G-18KT": self._flags(ignore_work_order=1)},
			company=self._company(),
		)
		self.assertFalse(raised)
		throw.assert_not_called()

	def test_manufacturing_item_skips_touch_and_purity(self):
		row = self._row(mwo="MWO-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			mwo={"MWO-1": self._mwo(metal_touch="14K", metal_purity="0.58")},
			item_attrs={"M-G-18KT": self._attrs()},
			item_flags={"M-G-18KT": self._flags(is_manufacturing=1)},
			company=self._company(),
		)
		self.assertFalse(raised)
		throw.assert_not_called()

	def test_check_touch_variant_gate_skips_touch(self):
		row = self._row(mwo="MWO-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			mwo={"MWO-1": self._mwo(metal_touch="14K")},
			item_attrs={"M-G-18KT": self._attrs()},
			company=self._company(check_touch="F"),
		)
		self.assertFalse(raised)
		throw.assert_not_called()

	def test_multiple_mwos_per_item_all_validated(self):
		row1 = self._row(mwo="MWO-1")
		row2 = self._row(mwo="MWO-2")
		_ignored, throw, raised, _calls = self._run(
			[row1, row2],
			mwo={
				"MWO-1": self._mwo(metal_touch="14K"),
				"MWO-2": self._mwo(),
			},
			item_attrs={"M-G-18KT": self._attrs()},
			item_flags={"M-G-18KT": self._flags()},
			company=self._company(),
		)
		self.assertTrue(raised)
		msg = throw.call_args[0][0]
		self.assertIn("MWO-1", msg)
		self.assertNotIn("MWO-2", msg)

	def test_doc_manufacturing_work_order_fetched_once(self):
		row = self._row(mwo="MWO-1")
		_ignored, throw, raised, calls = self._run(
			[row],
			doc_mwo="MWO-1",
			mwo={"MWO-1": self._mwo()},
			item_attrs={"M-G-18KT": self._attrs()},
			item_flags={"M-G-18KT": self._flags()},
			company=self._company(),
		)
		self.assertFalse(raised)
		self.assertEqual(
			[_n for d, _n in calls if d == "Manufacturing Work Order"], ["MWO-1"]
		)

	def test_same_item_two_rows_builds_item_data_once(self):
		row1 = self._row(mwo="MWO-1", operation="MOP-1", main_slip="MSL-1")
		row2 = self._row(mwo="MWO-1", operation="MOP-2", main_slip="MSL-2")
		_ignored, throw, raised, calls = self._run(
			[row1, row2],
			mwo={"MWO-1": self._mwo()},
			msl={
				"MSL-1": self._msl(for_subcontracting=1),
				"MSL-2": self._msl(for_subcontracting=1),
			},
			item_attrs={"M-G-18KT": self._attrs()},
			item_flags={"M-G-18KT": self._flags()},
			company=self._company(),
		)
		self.assertFalse(raised)
		self.assertEqual(
			[_n for d, _n in calls if d == "Item Variant Attribute"],
			["M-G-18KT"],
		)

	def test_customer_goods_sets_allow_zero_valuation_rate(self):
		row = self._row(variant="D", inventory_type="Customer Goods")
		doc, _throw, raised, _calls = self._run(
			[row],
			company=self._company(),
		)
		self.assertFalse(raised)
		self.assertEqual(row.allow_zero_valuation_rate, 1)

	def test_skips_row_without_mwo_or_main_slip(self):
		row = self._row()
		_ignored, throw, raised, calls = self._run(
			[row],
			company=self._company(),
		)
		self.assertFalse(raised)
		throw.assert_not_called()
		self.assertNotIn(("Item", "M-G-18KT"), calls)

	def test_skips_row_with_non_mf_variant(self):
		row = self._row(variant="D", mwo="MWO-1")
		_ignored, throw, raised, calls = self._run(
			[row],
			company=self._company(),
		)
		self.assertFalse(raised)
		throw.assert_not_called()
		self.assertNotIn(("Manufacturing Work Order", "MWO-1"), calls)

	def test_msl_touch_purity_colour_mismatches_aggregate(self):
		row = self._row(main_slip="MSL-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			msl={
				"MSL-1": self._msl(
					metal_touch="14K", metal_purity="0.58", metal_colour="White"
				)
			},
			item_attrs={"M-G-18KT": self._attrs()},
			company=self._company(),
		)
		self.assertTrue(raised)
		msg = throw.call_args[0][0]
		self.assertIn("Metal Touch", msg)
		self.assertIn("Metal Purity", msg)
		self.assertIn("Metal Colour", msg)
		self.assertIn("MSL-1", msg)

	def test_msl_for_subcontracting_skips_checks(self):
		row = self._row(main_slip="MSL-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			msl={
				"MSL-1": self._msl(
					metal_touch="14K",
					metal_purity="0.58",
					metal_colour="White",
					for_subcontracting=1,
				)
			},
			item_attrs={"M-G-18KT": self._attrs()},
			company=self._company(),
		)
		self.assertFalse(raised)
		throw.assert_not_called()

	def test_msl_colour_mismatch_needs_check_color(self):
		row = self._row(main_slip="MSL-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			msl={"MSL-1": self._msl(metal_colour="White", check_color=0)},
			item_attrs={"M-G-18KT": self._attrs()},
			company=self._company(),
		)
		self.assertFalse(raised)
		throw.assert_not_called()

	def test_msl_without_metal_colour_skips_all_checks(self):
		row = self._row(main_slip="MSL-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			msl={"MSL-1": self._msl(metal_touch="14K", metal_colour=None)},
			item_attrs={"M-G-18KT": self._attrs()},
			company=self._company(),
		)
		self.assertFalse(raised)
		throw.assert_not_called()

	def test_msl_multicolour_valid_match_passes(self):
		row = self._row(main_slip="MSL-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			msl={"MSL-1": self._msl(multicolour=1, allowed_colours=["Y"])},
			item_attrs={"M-G-18KT": self._attrs()},
			company=self._company(),
		)
		self.assertFalse(raised)
		throw.assert_not_called()

	def test_msl_multicolour_invalid_code_throws(self):
		row = self._row(main_slip="MSL-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			msl={"MSL-1": self._msl(multicolour=1, allowed_colours=["X"])},
			item_attrs={"M-G-18KT": self._attrs()},
			company=self._company(),
		)
		self.assertTrue(raised)
		msg = throw.call_args[0][0]
		self.assertIn("Invalid color code <b>X</b>", msg)
		self.assertIn("MSL-1", msg)

	def test_msl_multicolour_no_match_throws(self):
		row = self._row(main_slip="MSL-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			msl={"MSL-1": self._msl(multicolour=1, allowed_colours=["W", "P"])},
			item_attrs={"M-G-18KT": self._attrs()},
			company=self._company(),
		)
		self.assertTrue(raised)
		self.assertIn("Metal properties in MSL", throw.call_args[0][0])

	def test_msl_multicolour_no_check_color_no_throw(self):
		row = self._row(main_slip="MSL-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			msl={
				"MSL-1": self._msl(multicolour=1, allowed_colours=["W"], check_color=0)
			},
			item_attrs={"M-G-18KT": self._attrs()},
			company=self._company(),
		)
		self.assertFalse(raised)
		throw.assert_not_called()

	def test_mop_resolved_via_operation_to_msl_map(self):
		row = self._row(operation="MOP-1", main_slip="MSL-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			msl={"MSL-1": self._msl(metal_touch="14K")},
			item_attrs={"M-G-18KT": self._attrs()},
			company=self._company(),
		)
		self.assertTrue(raised)
		msg = throw.call_args[0][0]
		self.assertIn("Metal Touch", msg)
		self.assertIn("MSL-1", msg)

	def test_operation_without_msl_skipped(self):
		row = self._row(mwo="MWO-1", operation="MOP-1")
		_ignored, throw, raised, _calls = self._run(
			[row],
			mwo={"MWO-1": self._mwo()},
			item_attrs={"M-G-18KT": self._attrs()},
			company=self._company(),
		)
		self.assertFalse(raised)
		throw.assert_not_called()


# -------------------------------------------------- get_receive_work_order_batch
class TestGetReceiveWorkOrderBatch(_StockEntryTestCase):
	def _row(self, operation="MOP-1", item_code="ITM-1", batch_no=None):
		return _Row(
			manufacturing_operation=operation,
			item_code=item_code,
			batch_no=batch_no,
		)

	def _run(self, rows, batch="B-1"):
		se = _Doc(items=rows)
		with patch.object(se_events.frappe.db, "get_value", return_value=batch) as gv:
			se_events.get_receive_work_order_batch(se)
		return gv

	def test_keeps_existing_batch_without_query(self):
		row = self._row(batch_no="B-X")
		gv = self._run([row])
		gv.assert_not_called()
		self.assertEqual(row.batch_no, "B-X")


# -------------------------------------------------------- allow_zero_valuation
class TestAllowZeroValuation(_StockEntryTestCase):
	def test_sets_rate_for_customer_goods_row(self):
		row = _Row(inventory_type="Customer Goods", allow_zero_valuation_rate=None)
		se_events.allow_zero_valuation(_Doc(items=[row]))
		self.assertEqual(row.allow_zero_valuation_rate, 1)

	def test_skips_other_inventory_types(self):
		rows = [
			_Row(inventory_type=None, allow_zero_valuation_rate=None),
			_Row(inventory_type="Regular Stock", allow_zero_valuation_rate=None),
		]
		se_events.allow_zero_valuation(_Doc(items=rows))
		for row in rows:
			self.assertIsNone(row.allow_zero_valuation_rate)


# ---------------------------------------------- consume_stock_reservation_entry
class TestConsumeStockReservationEntry(_StockEntryTestCase):
	def _sb_entry(self, qty):
		entry = _Row(qty=qty)
		entry.db_update = MagicMock()
		return entry

	def _sre(self, **attrs):
		defaults = {
			"flags": frappe._dict(),
			"reservation_based_on": "Serial and Batch",
			"sb_entries": [],
			"reserved_qty": 5,
			"item_code": "ITM-1",
			"warehouse": "WH-1",
		}
		defaults.update(attrs)
		sre = _Doc(**defaults)
		sre.db_set = MagicMock()
		sre.update_status = MagicMock()
		return sre

	def _run(self, sre, update_bin=True):
		bin_doc = MagicMock()
		with patch(
			"erpnext.stock.utils.get_or_make_bin", return_value="BIN-1"
		) as gomb, patch.object(
			se_events.frappe, "get_cached_doc", return_value=bin_doc
		) as gcd:
			se_events.consume_stock_reservation_entry(sre, update_bin=update_bin)
		return bin_doc, gomb, gcd

	def test_updates_sb_entries_delivered_qty(self):
		entries = [self._sb_entry(2), self._sb_entry(3)]
		sre = self._sre(sb_entries=entries)
		bin_doc, gomb, gcd = self._run(sre)
		self.assertEqual([e.delivered_qty for e in entries], [2.0, 3.0])
		for entry in entries:
			entry.db_update.assert_called_once()

	def test_sets_delivered_qty_and_status(self):
		sre = self._sre()
		bin_doc, gomb, gcd = self._run(sre)
		self.assertTrue(sre.flags.ignore_permissions)
		sre.db_set.assert_called_once_with("delivered_qty", 5.0, update_modified=True)
		self.assertEqual(sre.delivered_qty, 5.0)
		sre.update_status.assert_called_once_with(status="Delivered")

	def test_update_bin_true_refreshes_bin(self):
		bin_doc, gomb, gcd = self._run(self._sre())
		gomb.assert_called_once_with("ITM-1", "WH-1")
		gcd.assert_called_once_with("Bin", "BIN-1")
		bin_doc.update_reserved_stock.assert_called_once()

	def test_update_bin_false_skips_bin(self):
		bin_doc, gomb, gcd = self._run(self._sre(), update_bin=False)
		gomb.assert_not_called()
		gcd.assert_not_called()
		bin_doc.update_reserved_stock.assert_not_called()

	def test_non_sb_basis_skips_child_loop(self):
		entry = self._sb_entry(2)
		sre = self._sre(reservation_based_on="Item", sb_entries=[entry])
		bin_doc, gomb, gcd = self._run(sre)
		self.assertNotIn("delivered_qty", entry.__dict__)
		entry.db_update.assert_not_called()

	def test_empty_sb_entries_skips_child_loop(self):
		sre = self._sre(sb_entries=[])
		bin_doc, gomb, gcd = self._run(sre)
		sre.db_set.assert_called_once_with("delivered_qty", 5.0, update_modified=True)


# -------------------------------------------------------- make_stock_in_entry
class TestMakeStockInEntry(_StockEntryTestCase):
	def _run(self, source, target):
		return _run_mapped(se_events.make_stock_in_entry, source, target)

	def test_customer_goods_received_to_issue(self):
		source = _Doc(stock_entry_type="Customer Goods Received", name="SE-1")
		target = _Doc(stock_entry_type="Customer Goods Received")
		target.set_missing_values = MagicMock()
		res, _ = self._run(source, target)
		self.assertEqual(res.stock_entry_type, "Customer Goods Issue")
		self.assertEqual(res.purpose, "Material Issue")
		self.assertEqual(res.custom_cg_issue_against, "SE-1")
		res.set_missing_values.assert_called_once()

	def test_customer_goods_issue_to_received(self):
		source = _Doc(stock_entry_type="Customer Goods Issue", name="SE-1")
		target = _Doc(stock_entry_type="Customer Goods Issue")
		target.set_missing_values = MagicMock()
		res, _ = self._run(source, target)
		self.assertEqual(res.stock_entry_type, "Customer Goods Received")
		self.assertEqual(res.purpose, "Material Receipt")
		res.set_missing_values.assert_called_once()

	def test_customer_goods_transfer(self):
		source = _Doc(stock_entry_type="Customer Goods Transfer", name="SE-1")
		target = _Doc(stock_entry_type="Some Other")
		target.set_missing_values = MagicMock()
		res, _ = self._run(source, target)
		self.assertEqual(res.stock_entry_type, "Customer Goods Transfer")
		self.assertEqual(res.purpose, "Material Transfer")

	def test_update_item(self):
		source = _Doc(stock_entry_type="Other")
		target = _Doc(stock_entry_type="Other")
		target.set_missing_values = MagicMock()
		res, kwargs = self._run(source, target)
		update_item = kwargs["map_dict"]["Stock Entry Detail"]["postprocess"]

		source_parent = _Doc(custom_material_request_reference="MR-1")
		source_row = _Row(item_code="ITM-1", t_warehouse="WH-SRC", qty=5)
		target_row = _Row()
		mr_doc = _Doc(items=[_Row(item_code="ITM-1", warehouse="WH-MR")])

		with patch.object(se_events.frappe, "get_doc", return_value=mr_doc):
			update_item(source_row, target_row, source_parent)

		self.assertEqual(target_row.t_warehouse, "WH-MR")
		self.assertEqual(target_row.s_warehouse, "WH-SRC")
		self.assertEqual(target_row.qty, 5)


# ---------------------------------------- make_stock_in_entry_on_transit_entry
class TestMakeStockInEntryOnTransitEntry(_StockEntryTestCase):
	def _run(self, source, target):
		return _run_mapped(
			se_events.make_stock_in_entry_on_transit_entry, source, target
		)

	def test_set_missing_values(self):
		source = _Doc(stock_entry_type="Material Transfer")
		target = _Doc()
		target.set_missing_values = MagicMock()
		res, _ = self._run(source, target)
		self.assertEqual(res.stock_entry_type, "Material Transfer")
		res.set_missing_values.assert_called_once()

	def test_update_item(self):
		source = _Doc(stock_entry_type="Material Transfer")
		target = _Doc()
		target.set_missing_values = MagicMock()
		res, kwargs = self._run(source, target)
		update_item = kwargs["map_dict"]["Stock Entry Detail"]["postprocess"]

		source_parent = _Doc()
		source_row = _Row(
			material_request_item="MRI-1",
			material_request="MR-1",
			t_warehouse="WH-SRC",
			qty=10,
			transferred_qty=2,
		)
		target_row = _Row()

		with patch.object(
			se_events.frappe.db, "get_value", return_value=1
		), patch.object(se_events.frappe, "get_value", return_value="WH-MR"):
			update_item(source_row, target_row, source_parent)

		self.assertEqual(target_row.t_warehouse, "WH-MR")
		self.assertEqual(target_row.s_warehouse, "WH-SRC")
		self.assertEqual(target_row.qty, 8)


# ------------------------------------------------------------- make_mr_on_return
class TestMakeMrOnReturn(_StockEntryTestCase):
	def _run(self, source, target):
		return _run_mapped(se_events.make_mr_on_return, source, target)

	def test_set_missing_values(self):
		source = _Doc(
			stock_entry_type="Customer Goods Transfer",
			items=[_Row(item_code="ITM-1", batch_no="B-1", serial_no="S-1", idx=1)],
		)
		target = _Doc(items=[_Row(item_code="ITM-1", idx=1)])
		target.set_missing_values = MagicMock()
		res, _ = self._run(source, target)
		self.assertEqual(res.material_request_type, "Material Transfer")
		self.assertEqual(res.items[0].custom_batch_no, "B-1")
		self.assertEqual(res.items[0].custom_serial_no, "S-1")
		res.set_missing_values.assert_called_once()

	def test_update_item(self):
		source = _Doc(stock_entry_type="Other", items=[])
		target = _Doc(items=[])
		target.set_missing_values = MagicMock()
		res, kwargs = self._run(source, target)
		update_item = kwargs["map_dict"]["Stock Entry Detail"]["postprocess"]

		source_parent = _Doc(outgoing_stock_entry="SE-OUT")
		source_row = _Row(
			item_code="ITM-1",
			t_warehouse="WH-T",
			batch_no="B-1",
			creation="2023-01-01 12:00:00.000000",
		)
		target_row = _Row()

		out_se = _Doc(items=[_Row(item_code="ITM-1", s_warehouse="WH-S")])

		with patch.object(
			se_events.frappe, "get_doc", return_value=out_se
		), patch.object(se_events, "get_batch_qty", return_value=15):
			update_item(source_row, target_row, source_parent)

		self.assertEqual(target_row.from_warehouse, "WH-T")
		self.assertEqual(target_row.warehouse, "WH-S")
		self.assertEqual(target_row.qty, 15)


# ---------------------------------- create_material_receipt_for_sales_person
class TestCreateMaterialReceiptForSalesPerson(_StockEntryTestCase):
	def test_creates_receipt_with_filtered_items(self):
		source = _Doc(
			name="SE-1",
			stock_entry_type="Material Issue",
			items=[
				_Row(
					item_code="ITM-1",
					qty=10,
					serial_no="S-1",
					batch_no="B-1",
					s_warehouse="WH-1",
					t_warehouse="WH-2",
				),
				_Row(
					item_code="ITM-2",
					qty=5,
					serial_no="S-2",
					batch_no="B-2",
					s_warehouse="WH-1",
					t_warehouse="WH-2",
				),
			],
		)
		source.as_dict = lambda: {"name": "SE-1"}
		target = _Doc(
			items=[
				_Row(item_code="ITM-1", qty=10, t_warehouse="WH-2", s_warehouse="WH-1"),
				_Row(item_code="ITM-2", qty=5, t_warehouse="WH-2", s_warehouse="WH-1"),
			]
		)
		target.update = MagicMock()
		target.insert = MagicMock()

		def _new_doc(doctype):
			return target

		mock_query = _query_mock(
			side_effect=[
				[se_events.frappe._dict(item_code="ITM-1", quantity=3)],
				[
					se_events.frappe._dict(
						item_code="ITM-1", **{"sum(soic.quantity)": 2}
					)
				],
			]
		)

		with patch.object(
			se_events.frappe, "get_doc", return_value=source
		), patch.object(
			se_events.frappe, "new_doc", side_effect=_new_doc
		), patch.object(
			se_events.frappe.qb, "from_", return_value=mock_query
		), patch.object(
			se_events.frappe.utils, "nowdate", return_value="2023-01-01"
		), patch.object(se_events.frappe.utils, "nowtime", return_value="12:00:00"):
			res = se_events.create_material_receipt_for_sales_person("SE-1")

		self.assertEqual(res.stock_entry_type, "Material Receipt - Sales Person")
		self.assertEqual(res.docstatus, 0)
		self.assertEqual(res.custom_material_return_receipt_number, "SE-1")

		self.assertEqual(len(res.items), 2)
		self.assertEqual(res.items[0].qty, 5)
		self.assertEqual(res.items[1].qty, 5)

		self.assertEqual(res.items[0].serial_no, "S-1")
		self.assertEqual(res.items[0].batch_no, "B-1")
		self.assertEqual(res.items[0].s_warehouse, "WH-2")
		self.assertEqual(res.items[0].t_warehouse, "WH-1")

		target.insert.assert_called_once()


# ------------------------------- create_material_receipt_for_customer_approval
class TestCreateMaterialReceiptForCustomerApproval(_StockEntryTestCase):
	def test_creates_receipt(self):
		source = _Doc(name="SE-1")
		source.as_dict = lambda: {"name": "SE-1"}
		target = _Doc()
		target.name = "NEW-SE"
		target.update = MagicMock()
		target.insert = MagicMock()
		target.append = lambda k, v: getattr(target, k).append(v)
		target.items = []

		def _new_doc(doctype):
			if doctype == "Stock Entry":
				return target

			row = _Row(s_warehouse="", t_warehouse="")

			def _update(d):
				d_items = d.__dict__.items() if hasattr(d, "__dict__") else d.items()
				for k, v in d_items:
					setattr(row, k, v)

			row.update = _update
			return row

		mock_query = _query_mock()
		mock_query.run.return_value = [
			se_events.frappe._dict(item_code="ITM-1", total_quantity=5, serial_no="S-1")
		]

		with patch.object(
			se_events.frappe, "get_doc", return_value=source
		), patch.object(
			se_events.frappe, "new_doc", side_effect=_new_doc
		), patch.object(
			se_events.frappe,
			"get_all",
			return_value=[
				_Row(item_code="ITM-1", s_warehouse="WH-1", t_warehouse="WH-2")
			],
		), patch.object(se_events.frappe.qb, "from_", return_value=mock_query):
			res_name = se_events.create_material_receipt_for_customer_approval(
				"SE-1", "CA-1"
			)

		self.assertEqual(res_name, "NEW-SE")
		self.assertEqual(target.stock_entry_type, "Material Receipt - Sales Person")
		self.assertEqual(target.custom_material_return_receipt_number, "SE-1")
		self.assertEqual(target.custom_customer_approval_reference, "CA-1")

		self.assertEqual(len(target.items), 1)
		self.assertEqual(target.items[0].qty, 5)
		self.assertEqual(target.items[0].serial_no, "S-1")
		self.assertEqual(target.items[0].s_warehouse, "WH-2")

		target.insert.assert_called_once()


# -------------------------------------------------------- convert_metal_purity
class TestConvertMetalPurity(_StockEntryTestCase):
	def test_creates_repack_entry(self):
		f_item = _Doc(
			metal_type="Gold",
			metal_touch="18K",
			metal_purity="0.75",
			metal_colour="Yellow",
			qty=10,
		)
		t_item = _Doc(
			metal_type="Gold",
			metal_touch="14K",
			metal_purity="0.58",
			metal_colour="White",
			qty=15,
		)

		doc = _Doc(items=[])
		doc.append = lambda k, v: getattr(doc, k).append(v)
		doc.save = MagicMock()
		doc.submit = MagicMock()

		with patch.object(
			se_events, "get_item_from_attribute", side_effect=["ITM-F", "ITM-T"]
		), patch.object(se_events.frappe, "new_doc", return_value=doc):
			se_events.convert_metal_purity(f_item, t_item, "WH-S", "WH-T")

		self.assertEqual(doc.stock_entry_type, "Repack")
		self.assertEqual(doc.purpose, "Repack")
		self.assertEqual(doc.inventory_type, "Regular Stock")

		self.assertEqual(len(doc.items), 2)
		self.assertEqual(doc.items[0]["item_code"], "ITM-F")
		self.assertEqual(doc.items[0]["s_warehouse"], "WH-S")
		self.assertEqual(doc.items[0]["t_warehouse"], None)
		self.assertEqual(doc.items[0]["qty"], 10)

		self.assertEqual(doc.items[1]["item_code"], "ITM-T")
		self.assertEqual(doc.items[1]["s_warehouse"], None)
		self.assertEqual(doc.items[1]["t_warehouse"], "WH-T")
		self.assertEqual(doc.items[1]["qty"], 15)

		doc.save.assert_called_once()
		doc.submit.assert_called_once()
