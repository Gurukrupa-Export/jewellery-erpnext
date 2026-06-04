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
			# Added by Part 2: lets the popup distinguish "no MOP Log row"
			# (mop_data_present=False → fall back to SRE remaining) from
			# "MOP Log row exists with 0g" (mop_data_present=True → skip).
			"mop_data_present",
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


def _batch_sre(name="SRE-D", item_code="D-NT-RO-7-+5.5-6"):
	"""Batch-based SRE (one per Material Transfer item) — the popup expands it
	into one row per Serial and Batch Entry line.
	"""
	return frappe._dict(
		{
			"name": name,
			"item_code": item_code,
			"warehouse": "WH-Src",
			"reserved_qty": 0.0,
			"delivered_qty": 0.0,
			"stock_uom": "Carat",
			"voucher_type": "Sales Order",
			"voucher_no": "SO-1",
			"voucher_detail_no": "SOI-1",
			"has_serial_no": 0,
			"has_batch_no": 1,
			"reservation_based_on": "Serial and Batch",
			"manufacturing_work_order": "MWO-1",
			"manufacturing_operation": "MOP-1",
		}
	)


def _sb_entry(name, parent, batch_no, qty, delivered_qty=0.0):
	return frappe._dict(
		{
			"name": name,
			"parent": parent,
			"batch_no": batch_no,
			"qty": qty,
			"delivered_qty": delivered_qty,
		}
	)


def _balance_row(item_code, batch_no, qty_batch, pcs_batch):
	"""Latest running-balance row (what get_current_mop_balance_rows surfaces)."""
	return frappe._dict(
		{
			"name": f"MOPLOG-BAL-{batch_no}",
			"item_code": item_code,
			"batch_no": batch_no,
			"qty_after_transaction_batch_based": qty_batch,
			"pcs_after_transaction_batch_based": pcs_batch,
		}
	)


def _transfer_row(name, item_code, batch_no, qty_change, pcs_change):
	"""Per-row incoming-transfer row (what get_mop_transfer_pcs_rows surfaces)."""
	return frappe._dict(
		{
			"name": name,
			"item_code": item_code,
			"batch_no": batch_no,
			"qty_after_transaction_batch_based": 0.0,
			"pcs_after_transaction_batch_based": 0,
			"qty_change": qty_change,
			"pcs_change": pcs_change,
		}
	)


class TestMakeReceiveEntryPerRowPcs(FrappeTestCase):
	"""Two reserved batch lines sharing the same (item, batch) must each show
	their OWN transferred PCS, not the batch-wide running total.

	Regression: the popup summed pcs by item+batch and showed the total (31)
	on every line; qty stayed per-line (0.072 / 0.046). The fix sources each
	line's pcs from its originating MOP Log transfer row (19 / 12).
	"""

	ITEM = "D-NT-RO-7-+5.5-6"
	BATCH = "GE2D075-DNTROX7E05F00-01"

	def _patch_popup(self, sre_rows, mop_rows, sb_rows):
		def _dispatcher(doctype, *args, **kwargs):
			if doctype == "Stock Reservation Entry":
				return list(sre_rows)
			if doctype == "MOP Log":
				return list(mop_rows)
			if doctype == "Serial and Batch Entry":
				return list(sb_rows)
			return []

		patches = [
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc",
				return_value=_make_mo(),
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value",
				return_value="WH-Raw",
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
				return_value=3,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.sql",
				return_value=[],
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.resolve_and_validate",
				return_value="WH-Resolved",
			),
			patch("frappe.db.get_all", side_effect=_dispatcher),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def test_per_row_pcs_split_across_same_item_batch(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		# One transfer SRE, two batch lines of the SAME (item, batch).
		self._patch_popup(
			sre_rows=[_batch_sre()],
			mop_rows=[
				# Running balance (batch total) — used for the qty/pcs cap.
				_balance_row(self.ITEM, self.BATCH, qty_batch=0.118, pcs_batch=31),
				# Per-row incoming transfers — the authoritative per-line pcs.
				_transfer_row("T1", self.ITEM, self.BATCH, 0.072, 19),
				_transfer_row("T2", self.ITEM, self.BATCH, 0.046, 12),
			],
			sb_rows=[
				_sb_entry("SB1", "SRE-D", self.BATCH, 0.072),
				_sb_entry("SB2", "SRE-D", self.BATCH, 0.046),
			],
		)
		rows = get_make_receive_entry_rows("MOP-1")["rows"]
		by_qty = {round(r["available_to_receive_qty"], 3): r for r in rows}
		self.assertEqual(len(rows), 2)
		# Qty stays per-line; pcs is now ALSO per-line (was 31/31 before).
		self.assertEqual(by_qty[0.072]["available_to_receive_pcs"], 19)
		self.assertEqual(by_qty[0.046]["available_to_receive_pcs"], 12)
		# Sum equals the batch balance — never over-states.
		self.assertEqual(sum(r["available_to_receive_pcs"] for r in rows), 31)

	def test_fallback_to_batch_aggregate_when_no_transfer_rows(self):
		"""No per-row transfer rows (legacy data) → fall back to the batch
		aggregate, preserving prior behaviour."""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		self._patch_popup(
			sre_rows=[_batch_sre()],
			mop_rows=[_balance_row(self.ITEM, self.BATCH, qty_batch=0.05, pcs_batch=8)],
			sb_rows=[_sb_entry("SB1", "SRE-D", self.BATCH, 0.05)],
		)
		rows = get_make_receive_entry_rows("MOP-1")["rows"]
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["available_to_receive_pcs"], 8)

	def _run_loss_scenario(self, sb_rows):
		"""Two batch lines of the same (item, batch): one transferred 47 pcs
		(0.494 ct) then lost 5 pcs (now 0.482 ct remaining), the other 15 pcs
		(0.158 ct, untouched). Live batch balance after the loss is 57 pcs.
		Returns ``{remaining_qty: available_to_receive_pcs}``.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		self._patch_popup(
			sre_rows=[_batch_sre()],
			mop_rows=[
				# Latest running balance for the batch (post-loss cap).
				_balance_row(self.ITEM, self.BATCH, qty_batch=0.640, pcs_batch=57),
				# The two incoming Material Transfer rows (authoritative per-line).
				_transfer_row("T-BIG", self.ITEM, self.BATCH, 0.494, 47),
				_transfer_row("T-SML", self.ITEM, self.BATCH, 0.158, 15),
			],
			sb_rows=sb_rows,
		)
		rows = get_make_receive_entry_rows("MOP-1")["rows"]
		self.assertEqual(len(rows), 2)
		return {round(r["available_to_receive_qty"], 3): r for r in rows}

	def test_loss_attributed_to_shrunken_line(self):
		"""The line that absorbed the 5-pcs loss (0.494 → 0.482) shows 42; the
		untouched 0.158 line keeps its full 15. Sum equals the 57 balance.
		Mirrors GE-MR-MF-26-67704 / Employee IR tvhgsi1gk1.
		"""
		by_qty = self._run_loss_scenario(
			sb_rows=[
				_sb_entry("SB-BIG", "SRE-D", self.BATCH, 0.482),
				_sb_entry("SB-SML", "SRE-D", self.BATCH, 0.158),
			]
		)
		self.assertEqual(by_qty[0.482]["available_to_receive_pcs"], 42)
		self.assertEqual(by_qty[0.158]["available_to_receive_pcs"], 15)
		self.assertEqual(
			sum(r["available_to_receive_pcs"] for r in by_qty.values()), 57
		)

	def test_loss_split_is_order_independent(self):
		"""Same scenario with the sibling lines iterated in the opposite order
		must yield the identical 42 / 15 split (regression for the prior
		order-dependent cursor)."""
		by_qty = self._run_loss_scenario(
			sb_rows=[
				_sb_entry("SB-SML", "SRE-D", self.BATCH, 0.158),
				_sb_entry("SB-BIG", "SRE-D", self.BATCH, 0.482),
			]
		)
		self.assertEqual(by_qty[0.482]["available_to_receive_pcs"], 42)
		self.assertEqual(by_qty[0.158]["available_to_receive_pcs"], 15)

	def test_no_loss_clean_lines_keep_own_transfer_pcs(self):
		"""GE-MR-MF-26-67712: two reserved lines of +00-0 share a batch with no
		loss (transfers 24 @ 0.720 and 8 @ 0.173, balance 32). Each keeps its OWN
		transfer pcs — 24 and 8 — never the 32 aggregate. A sibling single-line
		item (+7.5-8, 24 @ 0.825) is unaffected.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		item_00 = "D-NT-RO-MH12A-+00-0"
		item_78 = "D-NT-RO-MH12A-+7.5-8"
		batch_00 = "GE2D075-DNTROMH12A0000-01"
		batch_78 = "GE2D075-DNTROMH12A7508-01"

		self._patch_popup(
			sre_rows=[
				_batch_sre(name="SRE-00", item_code=item_00),
				_batch_sre(name="SRE-78", item_code=item_78),
			],
			mop_rows=[
				# Balance rows FIRST per key (dedup keeps the first as latest).
				_balance_row(item_00, batch_00, qty_batch=0.893, pcs_batch=32),
				_transfer_row("T00-BIG", item_00, batch_00, 0.720, 24),
				_transfer_row("T00-SML", item_00, batch_00, 0.173, 8),
				_balance_row(item_78, batch_78, qty_batch=0.825, pcs_batch=24),
				_transfer_row("T78", item_78, batch_78, 0.825, 24),
			],
			sb_rows=[
				_sb_entry("SB00-BIG", "SRE-00", batch_00, 0.720),
				_sb_entry("SB00-SML", "SRE-00", batch_00, 0.173),
				_sb_entry("SB78", "SRE-78", batch_78, 0.825),
			],
		)
		rows = get_make_receive_entry_rows("MOP-1")["rows"]
		self.assertEqual(len(rows), 3)
		by_qty = {round(r["available_to_receive_qty"], 3): r for r in rows}
		self.assertEqual(by_qty[0.720]["available_to_receive_pcs"], 24)
		self.assertEqual(by_qty[0.173]["available_to_receive_pcs"], 8)
		self.assertEqual(by_qty[0.825]["available_to_receive_pcs"], 24)
		# The two +00-0 lines sum to their 32 batch balance — no over-statement.
		pcs_00 = [
			r["available_to_receive_pcs"] for r in rows if r["item_code"] == item_00
		]
		self.assertEqual(sum(pcs_00), 32)


class TestMopTransferPcsRowsScope(FrappeTestCase):
	"""``get_mop_transfer_pcs_rows`` must scope by Manufacturing Work Order, not
	a single MOP. The per-row transfer (pcs_change>0) is logged once at the
	operation that first received the material; a downstream operation carries
	only balance clones (pcs_change=0). MOP-scoping there finds nothing and the
	popup falls back to the batch aggregate (the GE-MR-MF-26-67712 / MOP-O692V
	bug). MWO is constant across operations, so the lookup must use it.
	"""

	def test_scopes_by_manufacturing_work_order(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			get_mop_transfer_pcs_rows,
		)

		captured = {}

		def _fake_get_all(doctype, filters=None, fields=None, order_by=None):
			captured["doctype"] = doctype
			captured["filters"] = filters
			return []

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.get_all",
			side_effect=_fake_get_all,
		):
			get_mop_transfer_pcs_rows("MWO-XYZ")

		self.assertEqual(captured["doctype"], "MOP Log")
		self.assertEqual(captured["filters"].get("manufacturing_work_order"), "MWO-XYZ")
		# Must NOT scope by a single MOP, and must keep only incoming transfers.
		self.assertNotIn("manufacturing_operation", captured["filters"])
		self.assertEqual(captured["filters"].get("pcs_change"), [">", 0])
		self.assertEqual(captured["filters"].get("is_cancelled"), 0)
