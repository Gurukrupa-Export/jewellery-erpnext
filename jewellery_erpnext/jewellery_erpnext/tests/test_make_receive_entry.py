# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the SRE-source Make Receive Entry refactor.

These are unit tests against the whitelisted methods in manufacturing_operation.py;
they patch frappe DB / doc APIs to avoid needing a populated bench site.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


def _make_mo(name="MOP-1", mwo="MWO-1", department="DEPT-1", status="WIP"):
	mo = MagicMock()
	mo.name = name
	mo.manufacturing_work_order = mwo
	mo.manufacturing_operation = name
	mo.manufacturing_order = "PMO-1"
	mo.department = department
	mo.status = status
	return mo


class TestGetMakeReceiveEntryRows(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.sql",
		return_value=[(0,)],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value",
		return_value="WH-Raw",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
	)
	def test_get_rows_returns_only_mwo_sres(
		self, mock_get_doc, _mock_single, mock_get_value, mock_get_all, _mock_sql
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		mock_get_doc.return_value = _make_mo()

		# get_all is called once: SRE listing for the MWO. Filter is the contract.
		mock_get_all.return_value = [
			frappe._dict(
				{
					"name": "SRE-1",
					"item_code": "M-G-22KT-91.9-Y",
					"warehouse": "WH-Src",
					"reserved_qty": 10.0,
					"delivered_qty": 0.0,
					"stock_uom": "Gram",
					"voucher_type": "Sales Order",
					"voucher_no": "SO-1",
					"voucher_detail_no": "SOI-1",
					"has_serial_no": 0,
					"has_batch_no": 0,
					"reservation_based_on": "Qty",
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-1",
				}
			)
		]

		rows = get_make_receive_entry_rows("MOP-1")

		# get_all is called twice now: first for SRE listing, second for MOP
		# Log balance precompute. Inspect the first call (the SRE filter).
		_args, kwargs = mock_get_all.call_args_list[0]
		self.assertEqual(kwargs["filters"]["manufacturing_work_order"], "MWO-1")
		self.assertEqual(kwargs["filters"]["docstatus"], 1)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["available_to_receive_qty"], 10.0)
		self.assertEqual(rows[0]["reserved_qty"], 10.0)
		self.assertEqual(rows[0]["mop_available_qty"], 0)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
	)
	def test_make_receive_entry_rejects_mwo_missing(self, mock_get_doc):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		mo = _make_mo(mwo=None)
		mock_get_doc.return_value = mo

		with self.assertRaises(frappe.exceptions.ValidationError):
			get_make_receive_entry_rows("MOP-1")


class TestCreateMrWoStockEntryValidation(FrappeTestCase):
	"""Server-side validation must reject over-receive even if the client
	bypasses its own checks.
	"""

	def _patch_environment(self, sre_kwargs):
		"""Common patch stack for create_mr_wo_stock_entry.

		Returns the active patches as a list — caller starts/stops them.
		"""
		patches = [
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
				return_value=3,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value"
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.savepoint"
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.release_savepoint"
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.rollback"
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
			),
		]
		return patches, sre_kwargs

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.rollback"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.release_savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
	)
	def test_over_receive_rejected_server_side(
		self,
		mock_get_doc,
		_mock_single,
		mock_get_value,
		_mock_savepoint,
		_mock_release,
		_mock_rollback,
		mock_new_doc,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		mock_get_doc.return_value = _make_mo()
		# get_value side effects: idempotency lookup -> None, t_warehouse -> "WH-Raw",
		# SRE re-fetch (as_dict=True) -> dict.
		mock_get_value.side_effect = [
			None,  # idempotency lookup miss
			"WH-Raw",  # target warehouse resolution
			frappe._dict(
				{
					"name": "SRE-1",
					"docstatus": 1,
					"item_code": "M-G-22KT-91.9-Y",
					"warehouse": "WH-Src",
					"reserved_qty": 5.0,
					"delivered_qty": 0.0,
					"stock_uom": "Gram",
					"has_batch_no": 0,
					"reservation_based_on": "Qty",
					"manufacturing_work_order": "MWO-1",
				}
			),
		]

		se_data = {
			"manufacturing_operation": "MOP-1",
			"receive_items": [
				{"stock_reservation_entry": "SRE-1", "qty": 10.0, "idx": 1}
			],
		}

		with self.assertRaises(frappe.exceptions.ValidationError):
			create_mr_wo_stock_entry(se_data, request_id="req-1")

		# Stock Entry must NOT have been instantiated.
		mock_new_doc.assert_not_called()


class TestRequestIdIdempotency(FrappeTestCase):
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
	)
	def test_double_click_idempotency(self, mock_get_doc, mock_get_value):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		mock_get_doc.return_value = _make_mo()
		# Idempotency lookup hits an existing SE with the same request_id.
		mock_get_value.return_value = "STE-EXISTING-1"

		out = create_mr_wo_stock_entry(
			{
				"manufacturing_operation": "MOP-1",
				"receive_items": [{"stock_reservation_entry": "SRE-1", "qty": 1.0}],
			},
			request_id="req-dedupe",
		)

		self.assertEqual(out["docname"], "STE-EXISTING-1")
		self.assertTrue(out["idempotent"])


def _patch_create_mr_environment(test_self, sre_data, get_value_extra=None):
	"""Stand up the standard patch stack for create_mr_wo_stock_entry tests.

	Returns a dict of mocks the caller can use. The caller is responsible for
	configuring `mock_new_doc` if the test expects to reach the SE-creation
	branch.
	"""
	patches = {
		"get_doc": patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
		),
		"single": patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
			return_value=3,
		),
		"get_value": patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value"
		),
		"savepoint": patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.savepoint"
		),
		"release": patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.release_savepoint"
		),
		"rollback": patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.rollback"
		),
		"new_doc": patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
		),
	}
	started = {k: p.start() for k, p in patches.items()}
	for p in patches.values():
		test_self.addCleanup(p.stop)

	started["get_doc"].return_value = _make_mo()
	# Default get_value side effects: idempotency miss, t_warehouse, then SRE re-fetch dict.
	started["get_value"].side_effect = [None, "WH-Raw", sre_data] + (
		get_value_extra or []
	)
	return started


class TestCreateMrWoStockEntryEdgeCases(FrappeTestCase):
	def _sre(self, **overrides):
		base = frappe._dict(
			{
				"name": "SRE-1",
				"docstatus": 1,
				"item_code": "M-G-22KT-91.9-Y",
				"warehouse": "WH-Src",
				"reserved_qty": 5.0,
				"delivered_qty": 0.0,
				"stock_uom": "Gram",
				"has_batch_no": 0,
				"reservation_based_on": "Qty",
				"manufacturing_work_order": "MWO-1",
			}
		)
		base.update(overrides)
		return base

	def test_zero_qty_rejected(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		mocks = _patch_create_mr_environment(self, self._sre())
		with self.assertRaises(frappe.exceptions.ValidationError):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-1",
					"receive_items": [
						{"stock_reservation_entry": "SRE-1", "qty": 0, "idx": 1}
					],
				}
			)
		mocks["new_doc"].assert_not_called()

	def test_negative_qty_rejected(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		mocks = _patch_create_mr_environment(self, self._sre())
		with self.assertRaises(frappe.exceptions.ValidationError):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-1",
					"receive_items": [
						{"stock_reservation_entry": "SRE-1", "qty": -1.0, "idx": 1}
					],
				}
			)
		mocks["new_doc"].assert_not_called()

	def test_wrong_mwo_sre_rejected(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		mocks = _patch_create_mr_environment(
			self, self._sre(manufacturing_work_order="MWO-OTHER")
		)
		with self.assertRaises(frappe.exceptions.ValidationError):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-1",
					"receive_items": [
						{"stock_reservation_entry": "SRE-1", "qty": 1.0, "idx": 1}
					],
				}
			)
		mocks["new_doc"].assert_not_called()

	def test_cancelled_sre_rejected(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		# docstatus=2 means cancelled.
		mocks = _patch_create_mr_environment(self, self._sre(docstatus=2))
		with self.assertRaises(frappe.exceptions.ValidationError):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-1",
					"receive_items": [
						{"stock_reservation_entry": "SRE-1", "qty": 1.0, "idx": 1}
					],
				}
			)
		mocks["new_doc"].assert_not_called()

	def test_missing_sre_reference_rejected(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		mocks = _patch_create_mr_environment(self, self._sre())
		with self.assertRaises(frappe.exceptions.ValidationError):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-1",
					"receive_items": [{"qty": 1.0, "idx": 1}],
				}
			)
		mocks["new_doc"].assert_not_called()

	def test_finished_mo_rejected(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		# Override the default MO with status="Finished" before the function runs.
		patches = [
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc",
				return_value=_make_mo(status="Finished"),
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
				return_value=3,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
			),
		]
		started = [p.start() for p in patches]
		for p in patches:
			self.addCleanup(p.stop)

		with self.assertRaises(frappe.exceptions.ValidationError):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-1",
					"receive_items": [
						{"stock_reservation_entry": "SRE-1", "qty": 1.0, "idx": 1}
					],
				}
			)
		# new_doc must not have been touched.
		started[-1].assert_not_called()

	def test_no_receive_items_returns_msgprint(self):
		"""Empty receive_items list short-circuits without raising."""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.msgprint"
		) as mock_msg:
			create_mr_wo_stock_entry(
				{"manufacturing_operation": "MOP-1", "receive_items": []}
			)
			mock_msg.assert_called()

	def test_missing_target_warehouse_throws(self):
		"""When (department, warehouse_type='Raw Material') resolves to None,
		we throw the exact existing message — never silently fall through.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		patches = [
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc",
				return_value=_make_mo(),
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
				return_value=3,
			),
			# Idempotency miss + target warehouse miss.
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value",
				side_effect=[None, None],
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
			),
		]
		started = [p.start() for p in patches]
		for p in patches:
			self.addCleanup(p.stop)

		with self.assertRaises(frappe.exceptions.ValidationError):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-1",
					"receive_items": [
						{"stock_reservation_entry": "SRE-1", "qty": 1.0, "idx": 1}
					],
				}
			)
		started[-1].assert_not_called()


class TestGetMakeReceiveEntryRowsFilters(FrappeTestCase):
	"""Listing must enforce active SRE only, MWO scope, and remaining-qty > 0."""

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.sql",
		return_value=[(0,)],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value",
		return_value="WH-Raw",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
	)
	def test_zero_remaining_sre_filtered_out(
		self,
		mock_get_doc,
		_mock_single,
		_mock_get_value,
		mock_get_all,
		_mock_sql,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		mock_get_doc.return_value = _make_mo()
		mock_get_all.return_value = [
			# Fully delivered — should be filtered.
			frappe._dict(
				{
					"name": "SRE-DONE",
					"item_code": "M-X",
					"warehouse": "WH-Y",
					"reserved_qty": 5.0,
					"delivered_qty": 5.0,
					"stock_uom": "Gram",
					"voucher_type": "Sales Order",
					"voucher_no": "SO-1",
					"voucher_detail_no": "SOI-1",
					"has_serial_no": 0,
					"has_batch_no": 0,
					"reservation_based_on": "Qty",
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-1",
				}
			),
			# Active with remaining qty.
			frappe._dict(
				{
					"name": "SRE-ACTIVE",
					"item_code": "M-X",
					"warehouse": "WH-Y",
					"reserved_qty": 10.0,
					"delivered_qty": 4.0,
					"stock_uom": "Gram",
					"voucher_type": "Sales Order",
					"voucher_no": "SO-2",
					"voucher_detail_no": "SOI-2",
					"has_serial_no": 0,
					"has_batch_no": 0,
					"reservation_based_on": "Qty",
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-1",
				}
			),
		]

		rows = get_make_receive_entry_rows("MOP-1")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["stock_reservation_entry"], "SRE-ACTIVE")
		self.assertAlmostEqual(rows[0]["available_to_receive_qty"], 6.0)
		self.assertAlmostEqual(rows[0]["reserved_qty"], 6.0)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.sql",
		return_value=[(0,)],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value",
		return_value="WH-Raw",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
	)
	def test_get_all_filter_includes_docstatus_one(
		self,
		mock_get_doc,
		_mock_single,
		_mock_get_value,
		mock_get_all,
		_mock_sql,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		mock_get_doc.return_value = _make_mo()
		mock_get_all.return_value = []

		get_make_receive_entry_rows("MOP-1")

		# Inspect the first get_all call — the SRE listing. (Second call is
		# the MOP Log balance precompute introduced by the helper.)
		_args, kwargs = mock_get_all.call_args_list[0]
		filters = kwargs["filters"]
		self.assertEqual(filters["docstatus"], 1)
		self.assertEqual(filters["manufacturing_work_order"], "MWO-1")
		# No hardcoded status whitelist (Correction 2).
		self.assertNotIn("status", filters)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.sql",
		# Aggregated already-received shape: (item_code, s_warehouse, sum_qty, sum_pcs).
		return_value=[("M-X", "WH-Y", 2.5, 0)],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value",
		return_value="WH-Raw",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
	)
	def test_already_received_qty_does_not_reduce_available(
		self,
		mock_get_doc,
		_mock_single,
		_mock_get_value,
		mock_get_all,
		_mock_sql,
	):
		"""Critical Correction 1 invariant: prior receives must NOT be
		subtracted from active SRE remaining. SRE remaining is authoritative
		because partial-receive flow cancels and recreates the SRE.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		mock_get_doc.return_value = _make_mo()
		mock_get_all.return_value = [
			frappe._dict(
				{
					"name": "SRE-NEW",
					"item_code": "M-X",
					"warehouse": "WH-Y",
					"reserved_qty": 6.0,
					"delivered_qty": 0.0,
					"stock_uom": "Gram",
					"voucher_type": "Sales Order",
					"voucher_no": "SO-1",
					"voucher_detail_no": "SOI-1",
					"has_serial_no": 0,
					"has_batch_no": 0,
					"reservation_based_on": "Qty",
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-1",
				}
			)
		]

		rows = get_make_receive_entry_rows("MOP-1")
		self.assertEqual(len(rows), 1)
		# 6 reserved - 0 delivered = 6 SRE remaining; MOP unmocked = 0 (no
		# data signal). available_to_receive_qty falls back to SRE
		# remaining when the helper has no MOP row.
		self.assertAlmostEqual(rows[0]["available_to_receive_qty"], 6.0)
		self.assertAlmostEqual(rows[0]["reserved_qty"], 6.0)
		self.assertAlmostEqual(rows[0]["already_received_qty"], 2.5)


class TestPartialReceiveReplacement(FrappeTestCase):
	"""End-to-end mock-driven test: partial receive cancels original and
	submits a replacement carrying every voucher_* and metadata field.
	"""

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
		return_value=[],
	)
	@patch(
		"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry.get_available_qty_to_reserve",
		return_value=20.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_cached_value",
		return_value=(0, 0),
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.flags",
		new_callable=MagicMock,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.release_savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.rollback"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	def test_partial_receive_cancels_original_and_recreates(
		self,
		_mock_single,
		mock_get_value,
		_mock_rollback,
		_mock_release,
		_mock_savepoint,
		mock_get_doc,
		mock_new_doc,
		_mock_flags,
		_mock_cached,
		_mock_available,
		_mock_mop_get_all,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		# 1) Manufacturing Operation lookup.
		# 2) Receive flow then loads Stock Reservation Entry to cancel.
		# We model both via separate calls to frappe.get_doc and pre-canned
		# return values via side_effect.
		mo = _make_mo()
		original_sre = MagicMock()
		original_sre.name = "SRE-ORIG"
		original_sre.voucher_type = "Sales Order"
		original_sre.voucher_no = "SO-1"
		original_sre.voucher_detail_no = "SOI-1"
		original_sre.item_code = "M-X"
		original_sre.warehouse = "WH-Src"
		original_sre.voucher_qty = 10.0
		original_sre.company = "Test Co"
		original_sre.stock_uom = "Gram"
		original_sre.reservation_based_on = "Qty"
		original_sre.manufacturing_work_order = "MWO-1"
		original_sre.manufacturing_operation = "MOP-1"
		mock_get_doc.side_effect = [mo, original_sre]

		# 1: idempotency miss; 2: t_warehouse; 3: SRE re-fetch dict.
		mock_get_value.side_effect = [
			None,
			"WH-Raw",
			frappe._dict(
				{
					"name": "SRE-ORIG",
					"docstatus": 1,
					"item_code": "M-X",
					"warehouse": "WH-Src",
					"reserved_qty": 10.0,
					"delivered_qty": 0.0,
					"stock_uom": "Gram",
					"has_batch_no": 0,
					"reservation_based_on": "Qty",
					"manufacturing_work_order": "MWO-1",
				}
			),
		]

		# new_doc is called twice: once for the Stock Entry, once for
		# the replacement Stock Reservation Entry.
		# Make Stock Entry's `.update(dict)` setattr each pair, mirroring the
		# Document.update contract Frappe code expects.
		stock_entry = MagicMock()
		stock_entry.doctype = "Stock Entry"
		stock_entry.name = "STE-NEW-1"

		def _update_setattr(values):
			for k, v in values.items():
				setattr(stock_entry, k, v)

		stock_entry.update.side_effect = _update_setattr

		replacement_sre = MagicMock()
		replacement_sre.name = "SRE-REPLACEMENT"
		mock_new_doc.side_effect = [stock_entry, replacement_sre]

		out = create_mr_wo_stock_entry(
			{
				"manufacturing_operation": "MOP-1",
				"receive_items": [
					{"stock_reservation_entry": "SRE-ORIG", "qty": 4.0, "idx": 1}
				],
			},
			request_id="req-partial",
		)

		# Stock Entry built and submitted.
		self.assertEqual(stock_entry.stock_entry_type, "Material Receive (WORK ORDER)")
		stock_entry.save.assert_called_once()
		stock_entry.submit.assert_called_once()
		# Replacement SRE preserves voucher_*, warehouse, MWO/MOP, stock_uom.
		self.assertEqual(replacement_sre.voucher_type, "Sales Order")
		self.assertEqual(replacement_sre.voucher_no, "SO-1")
		self.assertEqual(replacement_sre.voucher_detail_no, "SOI-1")
		self.assertEqual(replacement_sre.warehouse, "WH-Src")
		self.assertEqual(replacement_sre.manufacturing_work_order, "MWO-1")
		self.assertEqual(replacement_sre.manufacturing_operation, "MOP-1")
		self.assertEqual(replacement_sre.stock_uom, "Gram")
		# Remaining qty.
		self.assertAlmostEqual(replacement_sre.reserved_qty, 6.0)
		# ERPNext's validate_mandatory requires available_qty (label
		# "Available Qty to Reserve"). Mirror existing project pattern:
		# max(get_available_qty_to_reserve, reserved_qty).
		self.assertAlmostEqual(replacement_sre.available_qty, 20.0)
		# Project convention: insert(ignore_links=1), not save().
		replacement_sre.insert.assert_called_once_with(ignore_links=1)
		replacement_sre.submit.assert_called_once()
		# Original SRE cancelled.
		original_sre.cancel.assert_called_once()
		# Output reflects recreate action.
		self.assertEqual(out["doctype"], "Stock Entry")
		self.assertEqual(out["docname"], "STE-NEW-1")
		self.assertFalse(out["idempotent"])
		actions = out["sre_actions"]
		self.assertEqual(len(actions), 1)
		self.assertEqual(actions[0]["action"], "recreated")
		self.assertEqual(actions[0]["old"], "SRE-ORIG")


class TestBuildReplacementSreZeroQty(FrappeTestCase):
	"""Defensive guards in _build_replacement_sre: zero / sub-precision
	remaining_qty and empty sb_entries must NOT trigger the
	'Available Qty to Reserve is required' validation.
	"""

	def _original_sre(self, reservation_based_on="Qty"):
		mock = MagicMock()
		mock.voucher_type = "Sales Order"
		mock.voucher_no = "SO-1"
		mock.voucher_detail_no = "SOI-1"
		mock.item_code = "M-X"
		mock.warehouse = "WH-Src"
		mock.voucher_qty = 10.0
		mock.company = "Test Co"
		mock.stock_uom = "Gram"
		mock.reservation_based_on = reservation_based_on
		mock.manufacturing_work_order = "MWO-1"
		mock.manufacturing_operation = "MOP-1"
		return mock

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	def test_zero_remaining_qty_returns_none_no_save(self, _mock_single, mock_new_doc):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			_build_replacement_sre,
		)

		out = _build_replacement_sre(self._original_sre(), remaining_qty=0)
		self.assertIsNone(out)
		mock_new_doc.assert_not_called()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	def test_sub_precision_remaining_qty_returns_none(self, _mock_single, mock_new_doc):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			_build_replacement_sre,
		)

		# precision=3 -> tolerance=0.001. 0.0001 must be treated as zero.
		out = _build_replacement_sre(self._original_sre(), remaining_qty=0.0001)
		self.assertIsNone(out)
		mock_new_doc.assert_not_called()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	def test_serial_and_batch_with_no_positive_rows_returns_none(
		self, _mock_single, mock_new_doc
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			_build_replacement_sre,
		)

		# Even with positive remaining_qty, S+B reservations require at least
		# one positive sb_entries row to be valid.
		out = _build_replacement_sre(
			self._original_sre(reservation_based_on="Serial and Batch"),
			remaining_qty=4.0,
			sb_remaining=[
				{"batch_no": "B1", "qty": 0},
				{"batch_no": "B2", "qty": 0.0001},
			],
		)
		self.assertIsNone(out)
		mock_new_doc.assert_not_called()

	@patch(
		"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry.get_available_qty_to_reserve",
		return_value=15.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_cached_value",
		return_value=(1, 0),  # has_batch_no=1, has_serial_no=0
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	def test_serial_and_batch_filters_zero_rows_keeps_positive(
		self, _mock_single, mock_new_doc, _mock_cached, _mock_available
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			_build_replacement_sre,
		)

		new_sre = MagicMock()
		new_sre.name = "SRE-NEW"
		mock_new_doc.return_value = new_sre

		out = _build_replacement_sre(
			self._original_sre(reservation_based_on="Serial and Batch"),
			remaining_qty=2.0,
			sb_remaining=[
				{"batch_no": "B-ZERO", "qty": 0},
				{"batch_no": "B-OK", "qty": 2.0},
				{"batch_no": "B-NEAR-ZERO", "qty": 0.0001},
			],
		)

		self.assertEqual(out, "SRE-NEW")
		# Only the positive batch row was appended.
		appended_batches = [
			c.args[1]["batch_no"]
			for c in new_sre.append.call_args_list
			if c.args and c.args[0] == "sb_entries"
		]
		self.assertEqual(appended_batches, ["B-OK"])
		# ERPNext mandatory: available_qty (max of available_to_reserve, remaining).
		self.assertAlmostEqual(new_sre.available_qty, 15.0)
		# has_batch_no propagated from Item master.
		self.assertEqual(new_sre.has_batch_no, 1)
		# Project convention.
		new_sre.insert.assert_called_once_with(ignore_links=1)
		new_sre.submit.assert_called_once()


class TestPartialReceiveZeroBatchSkipsReplacement(FrappeTestCase):
	"""End-to-end zero-qty contract for the Make Receive Entry caller:
	when a batch receive consumes the only positive batch row in full and
	all other batch rows were already delivered, no replacement SRE is
	created and no zero-qty SRE submit happens.
	"""

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
		return_value=[],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.flags",
		new_callable=MagicMock,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.release_savepoint"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.rollback"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	def test_full_batch_receive_does_not_recreate_zero_sre(
		self,
		_mock_single,
		mock_get_value,
		_mock_rollback,
		_mock_release,
		_mock_savepoint,
		mock_get_all,
		mock_get_doc,
		mock_new_doc,
		_mock_flags,
		_mock_mop_get_all,
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		mo = _make_mo()
		original_sre = MagicMock()
		original_sre.name = "SRE-BATCH-ORIG"
		original_sre.voucher_type = "Sales Order"
		original_sre.voucher_no = "SO-1"
		original_sre.voucher_detail_no = "SOI-1"
		original_sre.item_code = "M-X"
		original_sre.warehouse = "WH-Src"
		original_sre.voucher_qty = 5.0
		original_sre.company = "Test Co"
		original_sre.stock_uom = "Gram"
		original_sre.reservation_based_on = "Serial and Batch"
		original_sre.manufacturing_work_order = "MWO-1"
		original_sre.manufacturing_operation = "MOP-1"
		# Two get_doc calls: MO load, then SRE load to cancel.
		mock_get_doc.side_effect = [mo, original_sre]

		# Single batch row exhausted by the receive request.
		mock_get_all.return_value = [
			frappe._dict(
				{"name": "SB-1", "batch_no": "B1", "qty": 5.0, "delivered_qty": 0.0}
			)
		]

		# get_value sequence: idempotency miss + t_warehouse + SRE re-fetch + sb_row re-fetch.
		mock_get_value.side_effect = [
			None,
			"WH-Raw",
			frappe._dict(
				{
					"name": "SRE-BATCH-ORIG",
					"docstatus": 1,
					"item_code": "M-X",
					"warehouse": "WH-Src",
					"reserved_qty": 5.0,
					"delivered_qty": 0.0,
					"stock_uom": "Gram",
					"has_batch_no": 1,
					"reservation_based_on": "Serial and Batch",
					"manufacturing_work_order": "MWO-1",
				}
			),
			frappe._dict(
				{"name": "SB-1", "batch_no": "B1", "qty": 5.0, "delivered_qty": 0.0}
			),
		]

		# new_doc called only for the Stock Entry — never for a replacement SRE
		# because all batches are zero after receive.
		stock_entry = MagicMock()
		stock_entry.doctype = "Stock Entry"
		stock_entry.name = "STE-NEW-1"

		def _update_setattr(values):
			for k, v in values.items():
				setattr(stock_entry, k, v)

		stock_entry.update.side_effect = _update_setattr
		mock_new_doc.return_value = stock_entry

		out = create_mr_wo_stock_entry(
			{
				"manufacturing_operation": "MOP-1",
				"receive_items": [
					{
						"stock_reservation_entry": "SRE-BATCH-ORIG",
						"stock_reservation_entry_detail": "SB-1",
						"qty": 5.0,
						"idx": 1,
					}
				],
			},
			request_id="req-zero-batch",
		)

		# Stock Entry was created; SRE was cancelled; NO replacement SRE.
		self.assertEqual(mock_new_doc.call_count, 1)
		original_sre.cancel.assert_called_once()
		actions = out["sre_actions"]
		self.assertEqual(len(actions), 1)
		self.assertEqual(actions[0]["action"], "cancelled")
		self.assertIsNone(actions[0]["new"])


class TestAvailableQtyMandatoryContract(FrappeTestCase):
	"""Regression for the 'Available Qty to Reserve is required' bug.

	ERPNext's Stock Reservation Entry.validate_mandatory requires
	`available_qty` (label "Available Qty to Reserve"). The replacement SRE
	must populate this field, mirroring the existing project pattern in
	doc_events/stock_entry.py:720.
	"""

	def _original_sre(self):
		mock = MagicMock()
		mock.voucher_type = "Sales Order"
		mock.voucher_no = "SO-1"
		mock.voucher_detail_no = "SOI-1"
		mock.item_code = "M-X"
		mock.warehouse = "WH-Src"
		mock.voucher_qty = 10.0
		mock.company = "Test Co"
		mock.stock_uom = "Gram"
		mock.reservation_based_on = "Qty"
		mock.manufacturing_work_order = "MWO-1"
		mock.manufacturing_operation = "MOP-1"
		return mock

	@patch(
		"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry.get_available_qty_to_reserve",
		return_value=12.5,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_cached_value",
		return_value=(0, 0),
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	def test_available_qty_set_to_max_of_available_and_remaining(
		self, _mock_single, mock_new_doc, _mock_cached, _mock_available
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			_build_replacement_sre,
		)

		new_sre = MagicMock()
		new_sre.name = "SRE-AVAIL"
		mock_new_doc.return_value = new_sre

		out = _build_replacement_sre(self._original_sre(), remaining_qty=6.0)

		self.assertEqual(out, "SRE-AVAIL")
		# available_qty == max(get_available_qty_to_reserve=12.5, reserved_qty=6.0).
		self.assertAlmostEqual(new_sre.available_qty, 12.5)
		self.assertAlmostEqual(new_sre.reserved_qty, 6.0)

	@patch(
		"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry.get_available_qty_to_reserve",
		return_value=2.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_cached_value",
		return_value=(0, 0),
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	def test_available_qty_falls_back_to_remaining_when_warehouse_short(
		self, _mock_single, mock_new_doc, _mock_cached, _mock_available
	):
		"""When ERPNext reports less than the qty we need to reserve (e.g.
		stock just landed in the same transaction), `available_qty` must
		still cover `reserved_qty` so validate_mandatory passes.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			_build_replacement_sre,
		)

		new_sre = MagicMock()
		new_sre.name = "SRE-FALLBACK"
		mock_new_doc.return_value = new_sre

		out = _build_replacement_sre(self._original_sre(), remaining_qty=6.0)

		self.assertEqual(out, "SRE-FALLBACK")
		# available_to_reserve=2.0 < remaining=6.0 ⇒ available_qty=6.0.
		self.assertAlmostEqual(new_sre.available_qty, 6.0)
		self.assertAlmostEqual(new_sre.reserved_qty, 6.0)

	@patch(
		"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry.get_available_qty_to_reserve",
		return_value=8.0,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_cached_value",
		return_value=(1, 0),
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	def test_batch_lookup_is_scoped_to_batch_no(
		self,
		_mock_single,
		mock_new_doc,
		_mock_cached,
		mock_available,
	):
		"""For batch-tracked items, get_available_qty_to_reserve must be
		called with the batch_no kwarg so the per-batch quantity drives
		`available_qty`.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			_build_replacement_sre,
		)

		original = self._original_sre()
		original.reservation_based_on = "Serial and Batch"

		new_sre = MagicMock()
		new_sre.name = "SRE-BATCH"
		mock_new_doc.return_value = new_sre

		_build_replacement_sre(
			original,
			remaining_qty=4.0,
			sb_remaining=[{"batch_no": "B-K1", "qty": 4.0}],
		)

		_, kwargs = mock_available.call_args
		self.assertEqual(kwargs.get("batch_no"), "B-K1")
