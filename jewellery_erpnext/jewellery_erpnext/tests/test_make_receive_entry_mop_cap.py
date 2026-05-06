# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the MOP-balance cap on Make Receive Entry.

Covers:
1. Popup surfaces BOTH `reserved_qty` (SRE remaining) and `mop_available_qty`
   (MOP Log balance), plus `available_to_receive_qty = min(reserved, mop)`.
2. Rows where `available_to_receive_qty <= 0` are excluded.
3. `create_mr_wo_stock_entry` rejects qty over MOP balance with a precise
   message — the SRE-cap and the MOP-cap give different errors.
4. Replacement SRE qty is capped by min(remaining_reserved_after_receive,
   remaining_mop_after_receive) — the safe rule.

Implementation note on mocking: `manufacturing_operation.frappe.db.get_all`
and `mop_log.frappe.db.get_all` resolve to the SAME underlying object, so
patching them separately would not work — the later patch silently
overwrites the earlier one. These tests instead install a single
dispatcher on `frappe.db.get_all` that switches on doctype.
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


def _sre_dict(reserved_qty=10.0, item_code="M-X", warehouse="WH-Y"):
	return frappe._dict(
		{
			"name": "SRE-1",
			"item_code": item_code,
			"warehouse": warehouse,
			"reserved_qty": reserved_qty,
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


def _mop_log_row(item_code="M-X", batch_no=None, qty=7.0, pcs=0):
	return frappe._dict(
		{
			"name": "MOPLOG-1",
			"item_code": item_code,
			"batch_no": batch_no,
			"qty_after_transaction_batch_based": qty,
			"pcs_after_transaction_batch_based": pcs,
		}
	)


def _make_get_all_dispatcher(sre_rows=None, mop_rows=None, sb_rows=None):
	"""Return a callable suitable for `side_effect` on a single
	`frappe.db.get_all` patch. Switches on the first positional arg.
	"""
	sre_rows = sre_rows or []
	mop_rows = mop_rows or []
	sb_rows = sb_rows or []

	def _dispatcher(doctype, *args, **kwargs):
		if doctype == "Stock Reservation Entry":
			return list(sre_rows)
		if doctype == "MOP Log":
			return list(mop_rows)
		if doctype == "Serial and Batch Entry":
			return list(sb_rows)
		# DocType lookups (is_virtual_doctype etc.) and anything else.
		return []

	return _dispatcher


# ---------------------------------------------------------------------------
# Popup row composition
# ---------------------------------------------------------------------------


class TestPopupReservedVsMopFields(FrappeTestCase):
	def _patch_popup(self, sre_rows, mop_rows):
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
				"frappe.db.get_all",
				side_effect=_make_get_all_dispatcher(
					sre_rows=sre_rows, mop_rows=mop_rows
				),
			),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def test_t1_popup_shows_reserved_and_mop_separately(self):
		"""SRE reserved 10g; MOP says only 7g available. Popup shows the
		SRE-side reservation untouched and the MOP value separately, with
		the min surfaced as `available_to_receive_qty`.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		self._patch_popup(
			sre_rows=[_sre_dict(reserved_qty=10.0)],
			mop_rows=[_mop_log_row(qty=7.0)],
		)
		rows = get_make_receive_entry_rows("MOP-1")["rows"]
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertAlmostEqual(row["reserved_qty"], 10.0)
		self.assertAlmostEqual(row["mop_available_qty"], 7.0)
		self.assertAlmostEqual(row["available_to_receive_qty"], 7.0)

	def test_t7_row_excluded_when_mop_available_zero(self):
		"""SRE has 10g reserved but MOP says 0 → row excluded from popup."""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		self._patch_popup(
			sre_rows=[_sre_dict(reserved_qty=10.0)],
			mop_rows=[_mop_log_row(qty=0.0)],
		)
		rows = get_make_receive_entry_rows("MOP-1")["rows"]
		self.assertEqual(rows, [])

	def test_t9_loss_delta_reduces_mop_available(self):
		"""MOP balance reflects a prior loss (8.5g vs 10g reserved) →
		mop_available_qty=8.5 caps available_to_receive_qty."""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		self._patch_popup(
			sre_rows=[_sre_dict(reserved_qty=10.0)],
			mop_rows=[_mop_log_row(qty=8.5)],
		)
		rows = get_make_receive_entry_rows("MOP-1")["rows"]
		self.assertEqual(len(rows), 1)
		self.assertAlmostEqual(rows[0]["mop_available_qty"], 8.5)
		self.assertAlmostEqual(rows[0]["available_to_receive_qty"], 8.5)

	def test_t10_d_item_pcs_columns_visible(self):
		"""D/G item: surface Reserved PCS (0 since SRE has no PCS), MOP
		Available PCS (12), Available to Receive PCS (12 — since SRE has
		no PCS the MOP cap is the only one).
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		self._patch_popup(
			sre_rows=[_sre_dict(reserved_qty=2.0, item_code="D-BRI-VS1")],
			mop_rows=[_mop_log_row(item_code="D-BRI-VS1", qty=2.0, pcs=12)],
		)
		rows = get_make_receive_entry_rows("MOP-1")["rows"]
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertTrue(row["is_pcs_item"])
		self.assertEqual(row["reserved_pcs"], 0)
		self.assertEqual(row["mop_available_pcs"], 12)
		self.assertEqual(row["available_to_receive_pcs"], 12)


# ---------------------------------------------------------------------------
# Server validation (qty MOP-cap, SRE-cap, server-recomputes-from-MOP-Log)
# ---------------------------------------------------------------------------


class TestServerQtyValidation(FrappeTestCase):
	def _patch_validator(self, sre_qty, mop_qty):
		"""Common patch stack for `create_mr_wo_stock_entry`.

		Returns the new_doc mock so callers can `.assert_not_called()`.
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
				side_effect=[
					None,
					"WH-Raw",
					frappe._dict(
						{
							"name": "SRE-1",
							"docstatus": 1,
							"item_code": "M-X",
							"warehouse": "WH-Y",
							"reserved_qty": sre_qty,
							"delivered_qty": 0.0,
							"stock_uom": "Gram",
							"has_batch_no": 0,
							"reservation_based_on": "Qty",
							"manufacturing_work_order": "MWO-1",
						}
					),
				],
			),
			patch(
				"frappe.db.get_all",
				side_effect=_make_get_all_dispatcher(
					mop_rows=[_mop_log_row(qty=mop_qty)]
				),
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
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

		new_doc_patch = patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc"
		)
		mock_new_doc = new_doc_patch.start()
		self.addCleanup(new_doc_patch.stop)
		return mock_new_doc

	def test_t2_qty_over_mop_available_rejected(self):
		"""SRE remaining 10, MOP available 7, qty=8 → reject with
		MOP-cap message."""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		mock_new_doc = self._patch_validator(sre_qty=10.0, mop_qty=7.0)
		with self.assertRaisesRegex(
			frappe.exceptions.ValidationError, "MOP available qty"
		):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-1",
					"receive_items": [
						{"stock_reservation_entry": "SRE-1", "qty": 8.0, "idx": 1}
					],
				},
				request_id="t2",
			)
		mock_new_doc.assert_not_called()

	def test_t3_qty_over_reserved_uses_sre_message(self):
		"""SRE remaining 5, MOP 10, qty=6 → reject with reserved-qty
		message (SRE cap fires first)."""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		mock_new_doc = self._patch_validator(sre_qty=5.0, mop_qty=10.0)
		with self.assertRaisesRegex(
			frappe.exceptions.ValidationError, "exceeds reserved qty"
		):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-1",
					"receive_items": [
						{"stock_reservation_entry": "SRE-1", "qty": 6.0, "idx": 1}
					],
				},
				request_id="t3",
			)
		mock_new_doc.assert_not_called()

	def test_t14_server_ignores_client_side_available_to_receive(self):
		"""Client passes a stale `available_to_receive_qty=10`. Server
		recomputes from MOP Log directly and uses MOP=4 → qty=5 rejected.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		mock_new_doc = self._patch_validator(sre_qty=10.0, mop_qty=4.0)
		with self.assertRaisesRegex(
			frappe.exceptions.ValidationError, "MOP available qty"
		):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-1",
					"receive_items": [
						{
							"stock_reservation_entry": "SRE-1",
							"qty": 5.0,
							# Client lies — server must ignore.
							"available_to_receive_qty": 10.0,
							"idx": 1,
						}
					],
				},
				request_id="t14",
			)
		mock_new_doc.assert_not_called()


# ---------------------------------------------------------------------------
# Replacement SRE safe-rule
# ---------------------------------------------------------------------------


class TestReplacementSafeRule(FrappeTestCase):
	"""Replacement SRE qty must never exceed remaining MOP balance after
	the receive."""

	def _run_partial_receive(self, mop_qty, req_qty, sre_qty=10.0):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_mr_wo_stock_entry,
		)

		original_sre = MagicMock()
		original_sre.name = "SRE-T5"
		original_sre.voucher_type = "Sales Order"
		original_sre.voucher_no = "SO-1"
		original_sre.voucher_detail_no = "SOI-1"
		original_sre.item_code = "M-X"
		original_sre.warehouse = "WH-Y"
		original_sre.voucher_qty = sre_qty
		original_sre.company = "Test Co"
		original_sre.stock_uom = "Gram"
		original_sre.reservation_based_on = "Qty"
		original_sre.manufacturing_work_order = "MWO-1"
		original_sre.manufacturing_operation = "MOP-1"

		stock_entry = MagicMock()
		stock_entry.doctype = "Stock Entry"
		stock_entry.name = "STE-T5"

		def _update_setattr(values):
			for k, v in values.items():
				setattr(stock_entry, k, v)

		stock_entry.update.side_effect = _update_setattr

		replacement_sre = MagicMock()
		replacement_sre.name = "SRE-REPLACEMENT"

		patches = [
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc",
				side_effect=[_make_mo(), original_sre],
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
				return_value=3,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value",
				side_effect=[
					None,
					"WH-Raw",
					frappe._dict(
						{
							"name": "SRE-T5",
							"docstatus": 1,
							"item_code": "M-X",
							"warehouse": "WH-Y",
							"reserved_qty": sre_qty,
							"delivered_qty": 0.0,
							"stock_uom": "Gram",
							"has_batch_no": 0,
							"reservation_based_on": "Qty",
							"manufacturing_work_order": "MWO-1",
						}
					),
				],
			),
			patch(
				"frappe.db.get_all",
				side_effect=_make_get_all_dispatcher(
					mop_rows=[_mop_log_row(qty=mop_qty)]
				),
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
				"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry.get_available_qty_to_reserve",
				return_value=20.0,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_cached_value",
				return_value=(0, 0),
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.flags",
				new_callable=MagicMock,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.new_doc",
				side_effect=[stock_entry, replacement_sre],
			),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

		create_mr_wo_stock_entry(
			{
				"manufacturing_operation": "MOP-1",
				"receive_items": [
					{
						"stock_reservation_entry": "SRE-T5",
						"qty": req_qty,
						"idx": 1,
					}
				],
			},
			request_id=f"t5-{mop_qty}-{req_qty}",
		)
		return replacement_sre, original_sre

	def test_t5_replacement_capped_by_remaining_mop(self):
		"""SRE 10g + MOP 8g + receive 3g → replacement = min(7, 5) = 5g."""
		replacement_sre, original_sre = self._run_partial_receive(
			mop_qty=8.0, req_qty=3.0
		)
		self.assertAlmostEqual(replacement_sre.reserved_qty, 5.0)
		original_sre.cancel.assert_called_once()
		replacement_sre.submit.assert_called_once()

	def test_t6_no_replacement_when_mop_fully_received(self):
		"""SRE 10g + MOP 7g + receive 7g → replacement = min(3, 0) = 0
		→ no replacement SRE."""
		replacement_sre, original_sre = self._run_partial_receive(
			mop_qty=7.0, req_qty=7.0
		)
		original_sre.cancel.assert_called_once()
		replacement_sre.submit.assert_not_called()
		replacement_sre.insert.assert_not_called()
