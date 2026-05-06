# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the Qty/PCS reconciliation extension to Make Receive Entry.

Covers:
- get_available_qty_pcs_for_mop_item (helper) — dict shape, MOP Log balance
  precedence, D/G vs M/F/O gating, missing-PCS-source handling.
- get_make_receive_entry_rows endpoint — new context fields surface.
- create_mr_wo_stock_entry server-side PCS validation.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


class TestGetAvailableQtyPcsForMopItem(FrappeTestCase):
	"""The helper is the single source of truth for popup, server validator,
	and Employee IR manual-loss PCS check. These tests pin its contract.
	"""

	def _ctx(self, **kwargs):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			get_available_qty_pcs_for_mop_item,
		)

		return get_available_qty_pcs_for_mop_item(**kwargs)

	def test_t1_dict_shape(self):
		"""T1 — helper returns the documented context dict keys."""
		ctx = self._ctx(
			manufacturing_operation="MOP-1",
			item_code="M-X",
			batch_no="B1",
			sre_remaining_qty=10.0,
			mop_log_balance_map={},
		)
		expected_keys = {
			"item_code",
			"batch_no",
			"source_warehouse",
			"stock_reservation_entry",
			"stock_reservation_entry_detail",
			"manufacturing_work_order",
			"reserved_qty",
			"reserved_pcs",
			"mop_log_balance_qty",
			"mop_log_balance_pcs",
			"stock_entry_transferred_qty",
			"stock_entry_transferred_pcs",
			"already_received_qty",
			"already_received_pcs",
			"available_qty",
			"available_pcs",
			"is_pcs_item",
			"mop_log_reference",
		}
		self.assertEqual(set(ctx.keys()), expected_keys)
		# SRE never stores PCS — reserved_pcs must be None (unknown).
		self.assertIsNone(ctx["reserved_pcs"])

	def test_t2_available_qty_min_of_sre_and_mop_log(self):
		"""T2 — available_qty = min(SRE remaining, MOP Log batch-based qty)."""
		mop_balance_map = {
			("M-X", "B1"): frappe._dict(
				{
					"item_code": "M-X",
					"batch_no": "B1",
					"qty_after_transaction_batch_based": 6.0,
					"pcs_after_transaction_batch_based": 0,
					"name": "MOPLOG-1",
				}
			)
		}
		ctx = self._ctx(
			manufacturing_operation="MOP-1",
			item_code="M-X",
			batch_no="B1",
			sre_remaining_qty=10.0,
			mop_log_balance_map=mop_balance_map,
		)
		# MOP Log shows 6 < SRE 10 — clamps to 6.
		self.assertAlmostEqual(ctx["available_qty"], 6.0)
		self.assertEqual(ctx["mop_log_reference"], "MOPLOG-1")

	def test_t3_d_item_available_pcs_from_mop_log(self):
		"""T3 — D-prefix item: available_pcs reads MOP Log batch-based PCS."""
		mop_balance_map = {
			("D-BRI-VS1", "BD1"): frappe._dict(
				{
					"item_code": "D-BRI-VS1",
					"batch_no": "BD1",
					"qty_after_transaction_batch_based": 2.0,
					"pcs_after_transaction_batch_based": 12,
					"name": "MOPLOG-D",
				}
			)
		}
		ctx = self._ctx(
			manufacturing_operation="MOP-1",
			item_code="D-BRI-VS1",
			batch_no="BD1",
			sre_remaining_qty=2.0,
			mop_log_balance_map=mop_balance_map,
		)
		self.assertTrue(ctx["is_pcs_item"])
		self.assertEqual(ctx["available_pcs"], 12)
		self.assertEqual(ctx["mop_log_balance_pcs"], 12)

	def test_t4_g_item_available_pcs_from_mop_log(self):
		"""T4 — G-prefix item: same path as D."""
		mop_balance_map = {
			("G-RUBY", "BG1"): frappe._dict(
				{
					"item_code": "G-RUBY",
					"batch_no": "BG1",
					"qty_after_transaction_batch_based": 5.0,
					"pcs_after_transaction_batch_based": 8,
					"name": "MOPLOG-G",
				}
			)
		}
		ctx = self._ctx(
			manufacturing_operation="MOP-1",
			item_code="G-RUBY",
			batch_no="BG1",
			sre_remaining_qty=5.0,
			mop_log_balance_map=mop_balance_map,
		)
		self.assertTrue(ctx["is_pcs_item"])
		self.assertEqual(ctx["available_pcs"], 8)

	def test_t5_m_f_o_items_are_not_pcs_items(self):
		"""T5 — M/F/O items have is_pcs_item=False, available_pcs=0."""
		for item_code in ("M-X", "F-Y", "O-Z"):
			ctx = self._ctx(
				manufacturing_operation="MOP-1",
				item_code=item_code,
				batch_no=None,
				sre_remaining_qty=1.0,
				mop_log_balance_map={},
			)
			self.assertFalse(ctx["is_pcs_item"], item_code)
			self.assertEqual(ctx["available_pcs"], 0, item_code)

	def test_t6_m_item_available_pcs_zero_no_false_positive(self):
		"""T6 — M item with explicit MOP Log PCS=0 still yields
		available_pcs=0 (non-D/G branch returns 0 unconditionally).
		"""
		mop_balance_map = {
			("M-X", "B1"): frappe._dict(
				{
					"item_code": "M-X",
					"batch_no": "B1",
					"qty_after_transaction_batch_based": 5.0,
					"pcs_after_transaction_batch_based": 0,
					"name": "MOPLOG-M",
				}
			)
		}
		ctx = self._ctx(
			manufacturing_operation="MOP-1",
			item_code="M-X",
			batch_no="B1",
			sre_remaining_qty=5.0,
			mop_log_balance_map=mop_balance_map,
		)
		self.assertFalse(ctx["is_pcs_item"])
		self.assertEqual(ctx["available_pcs"], 0)

	def test_t7_d_item_missing_pcs_source_does_not_force_zero_unfairly(self):
		"""T7 — D item with MOP Log row showing positive PCS yields that
		PCS as available, even when SRE has no PCS field at all.
		Spec: missing PCS source must NOT be treated as zero.
		"""
		mop_balance_map = {
			("D-BRI", "BD1"): frappe._dict(
				{
					"item_code": "D-BRI",
					"batch_no": "BD1",
					"pcs_after_transaction_batch_based": 7,
					"qty_after_transaction_batch_based": 1.4,
					"name": "MOPLOG-D7",
				}
			)
		}
		# SRE doesn't have any PCS data — must not pull available_pcs to 0.
		ctx = self._ctx(
			manufacturing_operation="MOP-1",
			item_code="D-BRI",
			batch_no="BD1",
			sre_remaining_qty=1.4,
			mop_log_balance_map=mop_balance_map,
		)
		self.assertEqual(ctx["available_pcs"], 7)

	def test_helper_no_mop_log_row_yields_sre_only_qty(self):
		"""Edge case — when MOP Log has no row for (item, batch), available_qty
		falls back to SRE remaining (no false zero constraint).
		"""
		ctx = self._ctx(
			manufacturing_operation="MOP-1",
			item_code="M-X",
			batch_no="B1",
			sre_remaining_qty=10.0,
			mop_log_balance_map={},
		)
		self.assertAlmostEqual(ctx["available_qty"], 10.0)
		# No D/G — available_pcs is 0 regardless of source.
		self.assertEqual(ctx["available_pcs"], 0)

	def test_helper_negative_mop_balance_clamps_to_zero(self):
		"""Edge case — a corrupted balance below zero should not yield a
		negative available_qty; clamp to 0.
		"""
		mop_balance_map = {
			("M-X", "B1"): frappe._dict(
				{
					"qty_after_transaction_batch_based": -1.5,
					"pcs_after_transaction_batch_based": 0,
					"name": "MOPLOG-NEG",
				}
			)
		}
		ctx = self._ctx(
			manufacturing_operation="MOP-1",
			item_code="M-X",
			batch_no="B1",
			sre_remaining_qty=10.0,
			mop_log_balance_map=mop_balance_map,
		)
		self.assertAlmostEqual(ctx["available_qty"], 0.0)


def _make_mo(name="MOP-1", mwo="MWO-1", department="DEPT-1", status="WIP"):
	mo = MagicMock()
	mo.name = name
	mo.manufacturing_work_order = mwo
	mo.manufacturing_operation = name
	mo.manufacturing_order = "PMO-1"
	mo.department = department
	mo.status = status
	return mo


class TestCreateMrWoStockEntryPcsValidation(FrappeTestCase):
	"""Server-side PCS rules: D/G validate against available; M/F/O force 0."""

	def _patches(self, sre_dict, mop_balance_map=None):
		"""Common patch stack — get_doc, get_value, get_all (MOP Log fetch),
		savepoint stubs.
		"""
		patches = [
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc",
				return_value=_make_mo(),
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
				return_value=3,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value",
				side_effect=[None, "WH-Raw", sre_dict],
			),
			# Helper inside create_mr_wo_stock_entry calls
			# get_current_mop_balance_rows → frappe.db.get_all on MOP Log.
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
				return_value=list((mop_balance_map or {}).values()),
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
		started = [p.start() for p in patches]
		for p in patches:
			self.addCleanup(p.stop)
		return started

	def test_t8_qty_over_available_rejected(self):
		"""T8 — Receive Qty > available_qty server-rejected (regression-guard)."""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		sre = frappe._dict(
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
		started = self._patches(sre)
		new_doc_mock = started[-1]

		with self.assertRaises(frappe.exceptions.ValidationError):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-1",
					"receive_items": [
						{"stock_reservation_entry": "SRE-1", "qty": 10.0, "idx": 1}
					],
				},
				request_id="t8",
			)
		new_doc_mock.assert_not_called()

	def test_t9_d_item_pcs_over_available_rejected(self):
		"""T9 — D item: receive_pcs > available_pcs server-rejected."""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		sre = frappe._dict(
			{
				"name": "SRE-D",
				"docstatus": 1,
				"item_code": "D-BRI-VS1",
				"warehouse": "WH-Src",
				"reserved_qty": 5.0,
				"delivered_qty": 0.0,
				"stock_uom": "Carat",
				"has_batch_no": 1,
				"reservation_based_on": "Qty",
				"manufacturing_work_order": "MWO-1",
			}
		)
		# MOP Log shows only 3 PCS available.
		mop_balance_map = {
			("D-BRI-VS1", None): frappe._dict(
				{
					"item_code": "D-BRI-VS1",
					"batch_no": None,
					"qty_after_transaction_batch_based": 5.0,
					"pcs_after_transaction_batch_based": 3,
					"is_cancelled": 0,
					"name": "MOPLOG-D9",
				}
			)
		}
		started = self._patches(sre, mop_balance_map=mop_balance_map)
		new_doc_mock = started[-1]

		with self.assertRaises(frappe.exceptions.ValidationError):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-1",
					"receive_items": [
						{
							"stock_reservation_entry": "SRE-D",
							"qty": 1.0,
							"pcs": 5,  # over limit
							"idx": 1,
						}
					],
				},
				request_id="t9",
			)
		new_doc_mock.assert_not_called()

	def test_t10_m_item_forces_pcs_zero(self):
		"""T10 — M item: even when client sends pcs > 0, server zeros it."""
		self._assert_non_dg_pcs_force_zero("M-G-22KT-91.9-Y", 5)

	def test_t11_f_item_forces_pcs_zero(self):
		"""T11 — F item: same forced-zero rule."""
		self._assert_non_dg_pcs_force_zero("F-G-18KT-CHA", 7)

	def test_t12_o_item_forces_pcs_zero(self):
		"""T12 — O item: same forced-zero rule."""
		self._assert_non_dg_pcs_force_zero("O-PACKAGING", 3)

	def _assert_non_dg_pcs_force_zero(self, item_code, client_pcs):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		sre = frappe._dict(
			{
				"name": "SRE-MFO",
				"docstatus": 1,
				"item_code": item_code,
				"warehouse": "WH-Src",
				"reserved_qty": 5.0,
				"delivered_qty": 0.0,
				"stock_uom": "Gram",
				"has_batch_no": 0,
				"reservation_based_on": "Qty",
				"manufacturing_work_order": "MWO-1",
			}
		)
		started = self._patches(sre, mop_balance_map={})
		new_doc_mock = started[-1]

		stock_entry = MagicMock()
		stock_entry.doctype = "Stock Entry"
		stock_entry.name = "STE-MFO"
		appended = []

		def _append(table, values):
			appended.append(values)

		stock_entry.append.side_effect = _append

		def _update_setattr(values):
			for k, v in values.items():
				setattr(stock_entry, k, v)

		stock_entry.update.side_effect = _update_setattr
		new_doc_mock.return_value = stock_entry

		create_mr_wo_stock_entry(
			{
				"manufacturing_operation": "MOP-1",
				"receive_items": [
					{
						"stock_reservation_entry": "SRE-MFO",
						"qty": 1.0,
						"pcs": client_pcs,
						"idx": 1,
					}
				],
			},
			request_id=f"t-mfo-{item_code}",
		)
		# At least one item appended, and the pcs field is forced to 0.
		self.assertTrue(appended, "Stock Entry should have at least one item row")
		self.assertEqual(
			appended[0]["pcs"], 0, f"Server must force pcs=0 for {item_code}"
		)

	def test_t13_d_item_negative_pcs_rejected(self):
		"""T13 — D item: receive_pcs < 0 server-rejected."""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		sre = frappe._dict(
			{
				"name": "SRE-D-NEG",
				"docstatus": 1,
				"item_code": "D-BRI-VS1",
				"warehouse": "WH-Src",
				"reserved_qty": 5.0,
				"delivered_qty": 0.0,
				"stock_uom": "Carat",
				"has_batch_no": 0,
				"reservation_based_on": "Qty",
				"manufacturing_work_order": "MWO-1",
			}
		)
		started = self._patches(sre, mop_balance_map={})
		new_doc_mock = started[-1]

		with self.assertRaises(frappe.exceptions.ValidationError):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-1",
					"receive_items": [
						{
							"stock_reservation_entry": "SRE-D-NEG",
							"qty": 1.0,
							"pcs": -1,
							"idx": 1,
						}
					],
				},
				request_id="t13",
			)
		new_doc_mock.assert_not_called()
