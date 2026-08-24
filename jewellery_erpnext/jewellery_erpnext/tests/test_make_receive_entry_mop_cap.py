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
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	_build_replacement_sre,
	_existing_receive_se,
	create_mr_wo_stock_entry,
	get_make_receive_entry_rows,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	get_available_qty_pcs_for_mop_item,
	get_mop_transfer_pcs_rows,
)


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


def _make_get_value_dispatcher(sre_qty=10.0, warehouse="WH-Y"):
	"""Return a callable suitable for `side_effect` on a single
	`frappe.db.get_value` patch.
	"""

	def _dispatcher(doctype, *args, **kwargs):
		if doctype == "Stock Entry":
			return None

		if doctype == "Warehouse":
			return "WH-Raw"

		if doctype == "Stock Reservation Entry":
			if args and args[0] == "SRE-T5":
				return frappe._dict(
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
				)
			if args and isinstance(args[0], dict):
				return warehouse

		if doctype == "Manufacturing Work Order":
			return "PMO-1"

		return None

	return _dispatcher


class TestPopupReservedVsMopFields(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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

		self._patch_popup(
			sre_rows=[_sre_dict(reserved_qty=10.0)],
			mop_rows=[_mop_log_row(qty=0.0)],
		)
		rows = get_make_receive_entry_rows("MOP-1")["rows"]
		self.assertEqual(rows, [])

	def test_t9_loss_delta_reduces_mop_available(self):
		"""MOP balance reflects a prior loss (8.5g vs 10g reserved) →
		mop_available_qty=8.5 caps available_to_receive_qty."""

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

		self._patch_popup(
			sre_rows=[_sre_dict(reserved_qty=2.0, item_code="D-BRI-VS1")],
			mop_rows=[_mop_log_row(item_code="D-BRI-VS1", qty=2.0, pcs=12)],
		)
		rows = get_make_receive_entry_rows("MOP-1")["rows"]
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertTrue(row["is_pcs_item"])
		self.assertEqual(row["mop_available_pcs"], 12)
		self.assertEqual(row["available_to_receive_pcs"], 12)


class TestServerQtyValidation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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
		the balance-cap message, which reports BOTH inputs.

		The old assertion matched the literal "MOP available qty". That label
		was wrong twice over: the number printed was min(SRE, balance), not the
		balance, and the balance is MWO-wide rather than per-operation. Assert
		on the numbers instead, so a future relabel does not silently pass while
		the cap itself regresses."""

		mock_new_doc = self._patch_validator(sre_qty=10.0, mop_qty=7.0)
		with self.assertRaisesRegex(
			frappe.exceptions.ValidationError,
			r"exceeds available qty 7\.0.*reserved 10\.0.*balance 7\.0",
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

		mock_new_doc = self._patch_validator(sre_qty=10.0, mop_qty=4.0)
		with self.assertRaisesRegex(
			frappe.exceptions.ValidationError,
			r"exceeds available qty 4\.0.*reserved 10\.0.*balance 4\.0",
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


class TestReplacementSafeRule(IntegrationTestCase):
	"""Replacement SRE qty must never exceed remaining MOP balance after
	the receive."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run_partial_receive(self, mop_qty, req_qty, sre_qty=10.0):
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
				side_effect=_make_get_value_dispatcher(sre_qty=sre_qty),
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


def _make_mo(name="MOP-461KI", mwo="MWO-1", department="DEPT-A", status="WIP"):
	mo = MagicMock()
	mo.name = name
	mo.manufacturing_work_order = mwo
	mo.manufacturing_operation = name
	mo.manufacturing_order = "PMO-1"
	mo.department = department
	mo.status = status
	return mo


def _sre(
	name,
	manufacturing_operation,
	item_code="M-G-18KT-75.4-Y",
	warehouse="Casting WO - GEPL",
	reserved_qty=10.0,
	delivered_qty=0.0,
	has_batch_no=0,
	reservation_based_on="Qty",
):
	return frappe._dict(
		{
			"name": name,
			"item_code": item_code,
			"warehouse": warehouse,
			"reserved_qty": reserved_qty,
			"delivered_qty": delivered_qty,
			"stock_uom": "Gram",
			"voucher_type": "Sales Order",
			"voucher_no": "SAL-ORD-1",
			"voucher_detail_no": "SOI-1",
			"has_serial_no": 0,
			"has_batch_no": has_batch_no,
			"reservation_based_on": reservation_based_on,
			"status": "Reserved",
			"manufacturing_work_order": "MWO-1",
			"manufacturing_operation": manufacturing_operation,
		}
	)


def _make_mwo_level_get_all_side_effect(
	sre_rows=None, mop_log_rows=None, sbe_rows=None
):
	"""Dispatch ``frappe.db.get_all`` by doctype.

	MOP Log returns ONE flat list, not a per-operation mapping: the balance
	behind Make Receive Entry is scoped to the Manufacturing Work Order, so
	there is no per-operation answer to dispatch on. This helper used to key
	MOP Log off ``filters['manufacturing_operation']``, which quietly returned
	``[]`` for any MWO-scoped query.
	"""
	sre_rows = sre_rows or []
	mop_log_rows = mop_log_rows or []
	sbe_rows = sbe_rows or []

	def _side_effect(doctype, *args, **kwargs):
		if doctype == "Stock Reservation Entry":
			return sre_rows
		if doctype == "MOP Log":
			return mop_log_rows
		if doctype == "Serial and Batch Entry":
			return sbe_rows
		return []

	return _side_effect


class TestMwoLevelMakeReceiveEntry(IntegrationTestCase):
	"""SREs from sibling MOPs under the same MWO must appear, with
	availability computed against the MWO-wide MOP Log balance.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.sql",
		return_value=[(0,)],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_mwo_balance_rows"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value",
		return_value="Casting WO - GEPL",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
	)
	def test_sibling_mop_sre_uses_mwo_wide_balance(
		self,
		mock_get_doc,
		_mock_single,
		_mock_get_value,
		mock_get_all,
		mock_get_mwo_balance_rows,
		_mock_sql,
	):
		"""SRE-OTHER belongs to MOP-EY179; popup opened on MOP-461KI.

		Both SREs are capped by ONE MWO-wide balance, because
		``qty_after_transaction_batch_based`` is written as an MWO-wide running
		sum and stamped with whichever operation wrote it — it carries no
		per-operation meaning (see ``get_mwo_balance_rows``). The old fixture
		here gave MOP-461KI 5.0 and MOP-EY179 8.0 for the same (item, batch),
		which ``create_mop_log_for_stock_transfer_to_mo`` cannot actually
		produce.

		This test previously asserted a per-MOP lookup and passed without
		exercising one: it stubbed ``get_current_mop_balance_rows`` with a flat
		``return_value``, so the per-MOP dispatcher it set up was never
		consulted and any scope would have satisfied it. That false green is
		why the popup and its validator were free to disagree.
		"""

		mock_get_doc.return_value = _make_mo()
		# ONE balance for the work order, deliberately not keyed by operation.
		mock_get_mwo_balance_rows.return_value = [
			frappe._dict(
				{
					"item_code": "M-G-18KT-75.4-Y",
					"batch_no": None,
					"qty_after_transaction_batch_based": 8.0,
					"pcs_after_transaction_batch_based": 0,
					"name": "MOP-LOG-MWO-1",
					"creation": "2026-05-01",
				}
			),
		]
		mock_get_all.side_effect = _make_mwo_level_get_all_side_effect(
			sre_rows=[
				_sre("SRE-OWN", manufacturing_operation="MOP-461KI", reserved_qty=5.0),
				_sre(
					"SRE-OTHER",
					manufacturing_operation="MOP-EY179",
					reserved_qty=8.0,
				),
			],
		)

		result = get_make_receive_entry_rows("MOP-461KI")
		rows = result["rows"]

		self.assertEqual(result["active_sre_count"], 2)
		# Both SREs surface.
		by_sre = {r["stock_reservation_entry"]: r for r in rows}
		self.assertIn("SRE-OWN", by_sre)
		self.assertIn("SRE-OTHER", by_sre)

		# The balance is scoped to the MWO, so it is queried once, by MWO --
		# never per operation.
		mock_get_mwo_balance_rows.assert_called_once()
		self.assertEqual(mock_get_mwo_balance_rows.call_args[0][0], "MWO-1")

		# Same balance reported for both rows, whichever operation stamped the
		# reservation.
		self.assertAlmostEqual(by_sre["SRE-OWN"]["mop_available_qty"], 8.0)
		self.assertAlmostEqual(by_sre["SRE-OTHER"]["mop_available_qty"], 8.0)

		# available = min(SRE remaining, balance): SRE-OWN is bound by its own
		# 5.0 reservation, SRE-OTHER by the 8.0 balance.
		self.assertAlmostEqual(by_sre["SRE-OWN"]["available_to_receive_qty"], 5.0)
		self.assertAlmostEqual(by_sre["SRE-OTHER"]["available_to_receive_qty"], 8.0)

		# Sibling MOP is still recorded on the row so the operator/audit can
		# see which operation owns the reservation.
		self.assertEqual(by_sre["SRE-OTHER"]["manufacturing_operation"], "MOP-EY179")

		# Source warehouse comes from the SRE itself, not the opened MOP's
		# department warehouse.
		self.assertEqual(by_sre["SRE-OTHER"]["s_warehouse"], "Casting WO - GEPL")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.sql",
		return_value=[(0,)],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_mwo_balance_rows"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_all"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value",
		return_value="Casting WO - GEPL",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
		return_value=3,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
	)
	def test_sre_filter_includes_status_excludes_cancelled_delivered(
		self,
		mock_get_doc,
		_mock_single,
		_mock_get_value,
		mock_get_all,
		mock_get_mwo_balance_rows,
		_mock_sql,
	):
		"""SRE filter must drop Cancelled/Delivered statuses (per ERPNext SRE
		lifecycle) — docstatus=1 alone is not sufficient.
		"""

		mock_get_doc.return_value = _make_mo()
		mock_get_mwo_balance_rows.return_value = []
		mock_get_all.side_effect = _make_mwo_level_get_all_side_effect()

		get_make_receive_entry_rows("MOP-461KI")

		# First call to frappe.db.get_all is the SRE listing. Filter must
		# scope by MWO and exclude terminal statuses, but NOT scope by
		# manufacturing_operation (MWO-level intent).
		sre_call = next(
			c
			for c in mock_get_all.call_args_list
			if c.args and c.args[0] == "Stock Reservation Entry"
		)
		filters = sre_call.kwargs["filters"]
		self.assertEqual(filters["manufacturing_work_order"], "MWO-1")
		self.assertEqual(filters["docstatus"], 1)
		self.assertEqual(filters["status"], ["not in", ("Cancelled", "Delivered")])
		self.assertNotIn("manufacturing_operation", filters)


class TestGetAvailableQtyPcsForMopItem(IntegrationTestCase):
	"""The helper is the single source of truth for popup, server validator,
	and Employee IR manual-loss PCS check. These tests pin its contract.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _ctx(self, **kwargs):
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


class TestCreateMrWoStockEntryPcsValidation(IntegrationTestCase):
	"""Server-side PCS rules: D/G validate against available; M/F/O force 0."""

	@classmethod
	def setUpClass(cls):
		pass

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
				side_effect=[None, "WH-Raw", sre_dict, "WH-Src"],
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


class TestMakeReceiveEntryPerRowPcs(IntegrationTestCase):
	"""Two reserved batch lines sharing the same (item, batch) must each show
	their OWN transferred PCS, not the batch-wide running total.

	Regression: the popup summed pcs by item+batch and showed the total (31)
	on every line; qty stayed per-line (0.072 / 0.046). The fix sources each
	line's pcs from its originating MOP Log transfer row (19 / 12).
	"""

	ITEM = "D-NT-RO-7-+5.5-6"
	BATCH = "GE2D075-DNTROX7E05F00-01"

	@classmethod
	def setUpClass(cls):
		pass

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


class TestMopTransferPcsRowsScope(IntegrationTestCase):
	"""``get_mop_transfer_pcs_rows`` must scope by Manufacturing Work Order, not
	a single MOP. The per-row transfer (pcs_change>0) is logged once at the
	operation that first received the material; a downstream operation carries
	only balance clones (pcs_change=0). MOP-scoping there finds nothing and the
	popup falls back to the batch aggregate (the GE-MR-MF-26-67712 / MOP-O692V
	bug). MWO is constant across operations, so the lookup must use it.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_scopes_by_manufacturing_work_order(self):
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


def _make_mo(name="MOP-1", mwo="MWO-1", department="DEPT-A", status="WIP"):
	mo = MagicMock()
	mo.name = name
	mo.manufacturing_work_order = mwo
	mo.manufacturing_operation = name
	mo.manufacturing_order = "PMO-1"
	mo.department = department
	mo.status = status
	return mo


def _qty_sre(
	name="SRE-1", reserved_qty=10.0, delivered_qty=0.0, manufacturing_operation="MOP-1"
):
	return frappe._dict(
		{
			"name": name,
			"item_code": "M-X",
			"warehouse": "WH-Src",
			"reserved_qty": reserved_qty,
			"delivered_qty": delivered_qty,
			"stock_uom": "Gram",
			"voucher_type": "Sales Order",
			"voucher_no": "SO-1",
			"voucher_detail_no": "SOI-1",
			"has_serial_no": 0,
			"has_batch_no": 0,
			"reservation_based_on": "Qty",
			"status": "Reserved",
			"manufacturing_work_order": "MWO-1",
			"manufacturing_operation": manufacturing_operation,
		}
	)


def _make_get_all_side_effect(sre_rows=None, mop_log_rows=None, sbe_rows=None):
	"""Return a side_effect that dispatches ``frappe.db.get_all`` by
	doctype. SBE / MOP Log default to empty so missing keys are inert.
	"""
	sre_rows = sre_rows or []
	mop_log_rows = mop_log_rows or []
	sbe_rows = sbe_rows or []

	def _side_effect(doctype, *args, **kwargs):
		if doctype == "Stock Reservation Entry":
			return sre_rows
		if doctype == "MOP Log":
			return mop_log_rows
		if doctype == "Serial and Batch Entry":
			return sbe_rows
		return []

	return _side_effect


class TestMakeReceiveEntrySecondPopup(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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
	def test_no_sre_returns_active_sre_count_zero(
		self,
		mock_get_doc,
		_mock_single,
		_mock_get_value,
		mock_get_all,
		_mock_sql,
	):
		mock_get_doc.return_value = _make_mo()
		mock_get_all.side_effect = _make_get_all_side_effect()
		result = get_make_receive_entry_rows("MOP-1")
		self.assertEqual(result["rows"], [])
		self.assertEqual(result["skipped"], [])
		self.assertEqual(result["active_sre_count"], 0)

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
	def test_sre_with_zero_mop_balance_goes_to_skipped(
		self,
		mock_get_doc,
		_mock_single,
		_mock_get_value,
		mock_get_all,
		_mock_sql,
	):
		"""Replacement SRE alive (qty=10) but a loss MOP Log row drove the
		MOP balance to 0 → row goes to ``skipped`` with
		reason='mop_zero_balance', not ``rows``. This is the exact failure
		mode that produced the misleading "no SRE found" message before
		the fix.
		"""

		mock_get_doc.return_value = _make_mo()
		mock_get_all.side_effect = _make_get_all_side_effect(
			sre_rows=[_qty_sre(reserved_qty=10.0)],
			mop_log_rows=[
				frappe._dict(
					{
						"item_code": "M-X",
						"batch_no": None,
						"qty_after_transaction_batch_based": 0.0,
						"pcs_after_transaction_batch_based": 0,
						"name": "MOP-LOG-LOSS",
						"creation": "2026-05-01",
					}
				)
			],
		)

		result = get_make_receive_entry_rows("MOP-1")
		self.assertEqual(result["rows"], [])
		self.assertEqual(result["active_sre_count"], 1)
		self.assertEqual(len(result["skipped"]), 1)
		self.assertEqual(result["skipped"][0]["reason"], "mop_zero_balance")
		self.assertEqual(result["skipped"][0]["sre"], "SRE-1")

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
	def test_sre_with_missing_mop_row_surfaced_with_warning(
		self,
		mock_get_doc,
		_mock_single,
		_mock_get_value,
		mock_get_all,
		_mock_sql,
	):
		"""When MOP Log has NO row for (item, batch), surface the SRE row
		with a ``warning`` and ``available_to_receive_qty == sre_remaining``
		— SRE is the only authoritative source.
		"""

		mock_get_doc.return_value = _make_mo()
		# MOP Log returns nothing for the (item, batch) lookup.
		mock_get_all.side_effect = _make_get_all_side_effect(
			sre_rows=[_qty_sre(reserved_qty=10.0)],
			mop_log_rows=[],
		)

		result = get_make_receive_entry_rows("MOP-1")
		self.assertEqual(result["active_sre_count"], 1)
		self.assertEqual(len(result["rows"]), 1)
		row = result["rows"][0]
		self.assertAlmostEqual(row["available_to_receive_qty"], 10.0)
		self.assertTrue(row["warning"])
		self.assertFalse(row["mop_data_present"])


def _make_mo(name="MOP-1", mwo="MWO-1", department="DEPT-1", status="WIP"):
	mo = MagicMock()
	mo.name = name
	mo.manufacturing_work_order = mwo
	mo.manufacturing_operation = name
	mo.manufacturing_order = "PMO-1"
	mo.department = department
	mo.status = status
	return mo


class TestGetMakeReceiveEntryRows(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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

		result = get_make_receive_entry_rows("MOP-1")
		rows = result["rows"]

		# get_all is called twice now: first for SRE listing, second for MOP
		# Log balance precompute. Inspect the first call (the SRE filter).
		_args, kwargs = mock_get_all.call_args_list[0]
		self.assertEqual(kwargs["filters"]["manufacturing_work_order"], "MWO-1")
		self.assertEqual(kwargs["filters"]["docstatus"], 1)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["available_to_receive_qty"], 10.0)
		self.assertEqual(rows[0]["reserved_qty"], 10.0)
		# Mock returns the SRE list for both `frappe.db.get_all` calls
		# (doctype-collision: only one mock is active for the whole
		# function), so the MOP Log lookup picks up the SRE dict and
		# `mop_available_qty` reflects whatever `qty_after_transaction_batch_based`
		# evaluates to on it (None → 0). The structured response shape
		# is what we check here.
		self.assertEqual(rows[0]["mop_available_qty"], 0)
		self.assertEqual(result["active_sre_count"], 1)
		self.assertEqual(result["skipped"], [])

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
	)
	def test_make_receive_entry_rejects_mwo_missing(self, mock_get_doc):
		mo = _make_mo(mwo=None)
		mock_get_doc.return_value = mo

		with self.assertRaises(frappe.exceptions.ValidationError):
			get_make_receive_entry_rows("MOP-1")


class TestCreateMrWoStockEntryValidation(IntegrationTestCase):
	"""Server-side validation must reject over-receive even if the client
	bypasses its own checks.
	"""

	@classmethod
	def setUpClass(cls):
		pass

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


class TestRequestIdIdempotency(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc"
	)
	def test_double_click_idempotency(self, mock_get_doc, mock_get_value):
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


class TestCreateMrWoStockEntryEdgeCases(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

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


class TestGetMakeReceiveEntryRowsFilters(IntegrationTestCase):
	"""Listing must enforce active SRE only, MWO scope, and remaining-qty > 0."""

	@classmethod
	def setUpClass(cls):
		pass

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

		rows = get_make_receive_entry_rows("MOP-1")["rows"]
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
		mock_get_doc.return_value = _make_mo()
		mock_get_all.return_value = []

		get_make_receive_entry_rows("MOP-1")

		# Inspect the first get_all call — the SRE listing. (Second call is
		# the MOP Log balance precompute introduced by the helper.)
		_args, kwargs = mock_get_all.call_args_list[0]
		filters = kwargs["filters"]
		self.assertEqual(filters["docstatus"], 1)
		self.assertEqual(filters["manufacturing_work_order"], "MWO-1")
		# Status filter excludes terminal-state SREs (Cancelled, Delivered)
		# per the MWO-scope fix; docstatus=1 alone is not sufficient because
		# ERPNext keeps Cancelled SREs at docstatus=1 with status flipped.
		self.assertEqual(filters["status"], ["not in", ("Cancelled", "Delivered")])

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

		rows = get_make_receive_entry_rows("MOP-1")["rows"]
		self.assertEqual(len(rows), 1)
		# 6 reserved - 0 delivered = 6 SRE remaining; MOP unmocked = 0 (no
		# data signal). available_to_receive_qty falls back to SRE
		# remaining when the helper has no MOP row.
		self.assertAlmostEqual(rows[0]["available_to_receive_qty"], 6.0)
		self.assertAlmostEqual(rows[0]["reserved_qty"], 6.0)
		self.assertAlmostEqual(rows[0]["already_received_qty"], 2.5)


class TestPartialReceiveReplacement(IntegrationTestCase):
	"""End-to-end mock-driven test: partial receive cancels original and
	submits a replacement carrying every voucher_* and metadata field.
	"""

	@classmethod
	def setUpClass(cls):
		pass

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

		# 1: idempotency miss; 2: t_warehouse; 3: SRE re-fetch dict; 4: source warehouse for resolve_and_validate.
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
			"WH-Src",
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


class TestBuildReplacementSreZeroQty(IntegrationTestCase):
	"""Defensive guards in _build_replacement_sre: zero / sub-precision
	remaining_qty and empty sb_entries must NOT trigger the
	'Available Qty to Reserve is required' validation.
	"""

	@classmethod
	def setUpClass(cls):
		pass

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


class TestPartialReceiveZeroBatchSkipsReplacement(IntegrationTestCase):
	"""End-to-end zero-qty contract for the Make Receive Entry caller:
	when a batch receive consumes the only positive batch row in full and
	all other batch rows were already delivered, no replacement SRE is
	created and no zero-qty SRE submit happens.
	"""

	@classmethod
	def setUpClass(cls):
		pass

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

		# get_value sequence: idempotency miss + t_warehouse + SRE re-fetch +
		# sb_row re-fetch + source warehouse + Batch inventory-type/customer.
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
			"WH-Src",
			("Regular Stock", None),
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
	def test_receive_row_inventory_type_follows_customer_goods_batch(
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
		"""Regression: the receive dialog sends no inventory_type/customer, so
		the SE row must inherit them from the Batch master. A Customer Goods
		batch must NOT be stamped "Regular Stock" (ledger mismatch).
		"""

		mo = _make_mo()
		original_sre = MagicMock()
		original_sre.name = "SRE-CG-ORIG"
		original_sre.voucher_type = "Sales Order"
		original_sre.voucher_no = "SO-1"
		original_sre.voucher_detail_no = "SOI-1"
		original_sre.item_code = "M-G-22KT-91.9-Y"
		original_sre.warehouse = "WH-Src"
		original_sre.voucher_qty = 5.0
		original_sre.company = "Test Co"
		original_sre.stock_uom = "Gram"
		original_sre.reservation_based_on = "Serial and Batch"
		original_sre.manufacturing_work_order = "MWO-1"
		original_sre.manufacturing_operation = "MOP-1"
		mock_get_doc.side_effect = [mo, original_sre]

		mock_get_all.return_value = [
			frappe._dict(
				{
					"name": "SB-1",
					"batch_no": "CG-BATCH",
					"qty": 5.0,
					"delivered_qty": 0.0,
				}
			)
		]

		# get_value sequence: idempotency miss + t_warehouse + SRE re-fetch +
		# sb_row re-fetch + source warehouse + Batch (Customer Goods + customer).
		mock_get_value.side_effect = [
			None,
			"WH-Raw",
			frappe._dict(
				{
					"name": "SRE-CG-ORIG",
					"docstatus": 1,
					"item_code": "M-G-22KT-91.9-Y",
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
				{
					"name": "SB-1",
					"batch_no": "CG-BATCH",
					"qty": 5.0,
					"delivered_qty": 0.0,
				}
			),
			"WH-Src",
			("Customer Goods", "KACU0043"),
		]

		stock_entry = MagicMock()
		stock_entry.doctype = "Stock Entry"
		stock_entry.name = "STE-CG-1"

		def _update_setattr(values):
			for k, v in values.items():
				setattr(stock_entry, k, v)

		stock_entry.update.side_effect = _update_setattr
		mock_new_doc.return_value = stock_entry

		create_mr_wo_stock_entry(
			{
				"manufacturing_operation": "MOP-1",
				"receive_items": [
					{
						"stock_reservation_entry": "SRE-CG-ORIG",
						"stock_reservation_entry_detail": "SB-1",
						"qty": 5.0,
						"idx": 1,
					}
				],
			},
			request_id="req-cg-batch",
		)

		# The appended SE item row carries the Batch's inventory_type + customer,
		# not the empty client value that would default to "Regular Stock".
		item_appends = [
			c for c in stock_entry.append.call_args_list if c.args[0] == "items"
		]
		self.assertEqual(len(item_appends), 1)
		appended = item_appends[0].args[1]
		self.assertEqual(appended["inventory_type"], "Customer Goods")
		self.assertEqual(appended["customer"], "KACU0043")
		self.assertEqual(appended["batch_no"], "CG-BATCH")


class TestAvailableQtyMandatoryContract(IntegrationTestCase):
	"""Regression for the 'Available Qty to Reserve is required' bug.

	ERPNext's Stock Reservation Entry.validate_mandatory requires
	`available_qty` (label "Available Qty to Reserve"). The replacement SRE
	must populate this field, mirroring the existing project pattern in
	doc_events/stock_entry.py:720.
	"""

	@classmethod
	def setUpClass(cls):
		pass

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


class TestMakeReceiveEntryWhitelisting(IntegrationTestCase):
	"""The popup's two endpoints must be reachable over HTTP, and nothing else.

	`create_mr_wo_stock_entry` shipped without `@frappe.whitelist()` — the
	decorator sat on the private `_existing_receive_se` directly above it — so
	"Make Receive Entry" raised PermissionError for every user while "Receive
	Unused/Loose Material" worked, because `create_scrap_wo_stock_entry` is
	whitelisted and calls it in-process. Nothing caught it: every test imports
	the function and calls it directly, which never touches the whitelist.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_create_mr_wo_stock_entry_is_whitelisted(self):
		self.assertIn(create_mr_wo_stock_entry, frappe.whitelisted)

	def test_get_make_receive_entry_rows_is_whitelisted(self):
		self.assertIn(get_make_receive_entry_rows, frappe.whitelisted)

	def test_private_receive_helper_is_not_whitelisted(self):
		"""A `_`-prefixed helper is not an API. Whitelisting it exposed an
		arbitrary Stock Entry name lookup as a public endpoint."""
		self.assertNotIn(_existing_receive_se, frappe.whitelisted)


def _handoff_get_all_dispatcher(mwo_balance_qty, sre_remaining):
	"""SRE stamped at the PREVIOUS operation; MOP Log carries the balance.

	Models the shape that broke in production: an Employee IR handoff zeroes the
	source operation's MOP Log row and writes a carry-forward row for the
	destination, while `Stock Reservation Entry.manufacturing_operation` keeps
	pointing at the source. Only the carry-forward row is returned, because the
	balance read is MWO-scoped and takes the latest row.
	"""
	sre_rows = [
		frappe._dict(
			{
				"name": "SRE-HANDOFF",
				"item_code": "M-X",
				"warehouse": "WH-Src",
				"reserved_qty": sre_remaining,
				"delivered_qty": 0.0,
				"stock_uom": "Gram",
				"voucher_type": "Sales Order",
				"voucher_no": "SO-1",
				"voucher_detail_no": "SOI-1",
				"has_serial_no": 0,
				"has_batch_no": 0,
				"reservation_based_on": "Qty",
				"status": "Partially Reserved",
				"manufacturing_work_order": "MWO-1",
				# The stale stamp: work has since moved to MOP-NEW.
				"manufacturing_operation": "MOP-OLD",
			}
		)
	]

	def _dispatcher(doctype, *args, **kwargs):
		if doctype == "Stock Reservation Entry":
			return list(sre_rows)
		if doctype == "MOP Log":
			# Dispatch on the filters the caller actually passed, so a
			# per-operation read and an MWO-wide read get DIFFERENT answers —
			# which is the whole point of this fixture. A dispatcher that
			# ignores filters cannot tell the two scopes apart and would pass
			# against the very bug it is meant to pin.
			filters = kwargs.get("filters") or {}
			mop = filters.get("manufacturing_operation")
			if mop == "MOP-OLD":
				# Source operation, explicitly zeroed by the handoff.
				return [_mop_log_row(qty=0.0)]
			if mop:
				# Destination operation carries the balance forward.
				return [_mop_log_row(qty=mwo_balance_qty)]
			# MWO-wide read: latest row is the carry-forward one.
			return [_mop_log_row(qty=mwo_balance_qty)]
		if doctype == "Serial and Batch Entry":
			return []
		return []

	return _dispatcher


def _handoff_get_value_dispatcher(sre_remaining):
	def _dispatcher(doctype, *args, **kwargs):
		if doctype == "Stock Entry":
			return None
		if doctype == "Warehouse":
			return "WH-Raw"
		if doctype == "Stock Reservation Entry":
			return frappe._dict(
				{
					"name": "SRE-HANDOFF",
					"docstatus": 1,
					"item_code": "M-X",
					"warehouse": "WH-Src",
					"reserved_qty": sre_remaining,
					"delivered_qty": 0.0,
					"stock_uom": "Gram",
					"has_batch_no": 0,
					"reservation_based_on": "Qty",
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-OLD",
					"voucher_type": "Sales Order",
					"voucher_no": "SO-1",
				}
			)
		return None

	return _dispatcher


class TestDialogValidatorAgreement(IntegrationTestCase):
	"""The popup and its validator must never disagree about availability.

	Regression for the reported failure: the popup offered 0.770 g and the
	server rejected 0.010 g of it with "exceeds MOP available qty 0.0
	(loss/consumption since reservation)" — with no loss and no consumption.
	The popup resolved the balance against the OPENED operation while the
	validator resolved it against `sre.manufacturing_operation`, which the
	handoff had explicitly zeroed.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _patch_both_sides(self, mwo_balance_qty=0.770, sre_remaining=0.770):
		patches = [
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc",
				return_value=_make_mo(name="MOP-NEW"),
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_single_value",
				return_value=3,
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.get_value",
				side_effect=_handoff_get_value_dispatcher(sre_remaining),
			),
			patch(
				"frappe.db.get_all",
				side_effect=_handoff_get_all_dispatcher(mwo_balance_qty, sre_remaining),
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.sql",
				return_value=[(0,)],
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.resolve_and_validate",
				return_value="WH-Src",
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
				"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation._build_replacement_sre",
				return_value="SRE-REPLACEMENT",
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

		stock_entry = MagicMock()
		stock_entry.doctype = "Stock Entry"
		stock_entry.name = "STE-AGREE-1"

		def _update_setattr(values):
			for k, v in values.items():
				setattr(stock_entry, k, v)

		stock_entry.update.side_effect = _update_setattr
		mock_new_doc.return_value = stock_entry
		return stock_entry

	def test_popup_surfaces_the_handed_off_balance(self):
		"""The reservation still points at MOP-OLD, but the popup opened on
		MOP-NEW reports the balance the work order actually holds."""
		self._patch_both_sides()
		rows = get_make_receive_entry_rows("MOP-NEW")["rows"]
		self.assertEqual(len(rows), 1)
		self.assertAlmostEqual(rows[0]["mop_available_qty"], 0.770)
		self.assertAlmostEqual(rows[0]["available_to_receive_qty"], 0.770)
		# The row is actionable, not a "MOP balance unknown" fallback.
		self.assertTrue(rows[0]["mop_data_present"])
		self.assertIsNone(rows[0]["warning"])

	def test_dialog_available_qty_is_accepted_by_validator(self):
		"""Take the popup's own number and hand it straight back.

		This is the reported bug end to end. Pre-fix the validator threw
		"exceeds MOP available qty 0.0" against a row the popup had just
		offered at 0.770.
		"""
		stock_entry = self._patch_both_sides()
		rows = get_make_receive_entry_rows("MOP-NEW")["rows"]
		offered = rows[0]["available_to_receive_qty"]

		out = create_mr_wo_stock_entry(
			{
				"manufacturing_operation": "MOP-NEW",
				"receive_items": [
					{
						"stock_reservation_entry": rows[0]["stock_reservation_entry"],
						"qty": offered,
						"idx": 1,
					}
				],
			},
			request_id="agree-full",
		)
		self.assertEqual(out["docname"], "STE-AGREE-1")
		stock_entry.submit.assert_called_once()

	def test_partial_receive_of_offered_qty_is_accepted(self):
		"""The reported interaction: 0.010 g against an offered 0.770 g."""
		stock_entry = self._patch_both_sides()
		out = create_mr_wo_stock_entry(
			{
				"manufacturing_operation": "MOP-NEW",
				"receive_items": [
					{
						"stock_reservation_entry": "SRE-HANDOFF",
						"qty": 0.010,
						"idx": 1,
					}
				],
			},
			request_id="agree-partial",
		)
		stock_entry.submit.assert_called_once()
		# The remainder is re-reserved, not discarded. Reading the zeroed
		# source operation made this min(0.760, 0.0) == 0, which silently
		# dropped the replacement and destroyed the reservation.
		actions = out["sre_actions"]
		self.assertEqual(len(actions), 1)
		self.assertEqual(actions[0]["action"], "recreated")
		self.assertEqual(actions[0]["new"], "SRE-REPLACEMENT")

	def test_validator_still_rejects_above_the_offered_qty(self):
		"""The cap is repaired, not removed."""
		self._patch_both_sides()
		with self.assertRaisesRegex(
			frappe.exceptions.ValidationError, "exceeds reserved qty"
		):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-NEW",
					"receive_items": [
						{
							"stock_reservation_entry": "SRE-HANDOFF",
							"qty": 0.900,
							"idx": 1,
						}
					],
				},
				request_id="agree-over",
			)

	def test_loss_since_reservation_still_caps_the_receive(self):
		"""Balance below the reservation — the case the cap exists for.

		The reservation still holds 0.770 but the work order's latest MOP Log
		row says 0.200, wherever that loss was booked. Scope is MWO-wide
		precisely so a loss booked at a sibling operation cannot slip past.
		"""
		self._patch_both_sides(mwo_balance_qty=0.200, sre_remaining=0.770)
		rows = get_make_receive_entry_rows("MOP-NEW")["rows"]
		self.assertAlmostEqual(rows[0]["available_to_receive_qty"], 0.200)

		with self.assertRaisesRegex(
			frappe.exceptions.ValidationError,
			r"exceeds available qty 0\.2.*reserved 0\.77.*balance 0\.2",
		):
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-NEW",
					"receive_items": [
						{
							"stock_reservation_entry": "SRE-HANDOFF",
							"qty": 0.500,
							"idx": 1,
						}
					],
				},
				request_id="agree-loss",
			)

	def test_validator_passes_sales_order_to_warehouse_resolution(self):
		"""`voucher_type`/`voucher_no` were read but never fetched.

		`as_dict=True` yields a frappe._dict, so the missing keys returned None
		silently and the server resolved the source warehouse WITHOUT the Sales
		Order the popup resolves it with — the two could pick different
		warehouses for the same row.
		"""
		self._patch_both_sides()
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.resolve_and_validate",
			return_value="WH-Src",
		) as mock_resolve:
			create_mr_wo_stock_entry(
				{
					"manufacturing_operation": "MOP-NEW",
					"receive_items": [
						{
							"stock_reservation_entry": "SRE-HANDOFF",
							"qty": 0.010,
							"idx": 1,
						}
					],
				},
				request_id="agree-so",
			)
		self.assertEqual(
			mock_resolve.call_args.kwargs["sales_order"],
			"SO-1",
		)
