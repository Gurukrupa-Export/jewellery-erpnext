# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Diagnostic-shape contract for the Make Receive Entry popup.

Three scenarios the dialog must distinguish:
  1. ``active_sre_count == 0`` → no SREs at all under the MWO.
  2. ``active_sre_count > 0`` AND ``rows`` non-empty AND ``mop_data_present``
     True on each row → normal popup.
  3. ``active_sre_count > 0`` AND ``rows`` empty AND ``skipped`` populated
     with reason="mop_zero_balance" → SRE alive but loss/consumption ate
     the MOP balance.
  4. ``rows`` non-empty AND a row has ``warning`` set → SRE alive but no
     MOP Log row found; UI shows the warning, server-side cap silenced.

NB: ``frappe.db.get_all`` is a global function — patching it via two
different module paths (``manufacturing_operation.frappe.db.get_all``
*and* ``mop_log.frappe.db.get_all``) collides on the same object, so only
the inner @patch wins. We patch it once and dispatch by doctype using
``side_effect``.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase


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


class TestMakeReceiveEntrySecondPopup(FrappeTestCase):
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
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

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
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

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
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

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
