# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""MWO-level scope contract for Make Receive Entry.

The popup opened on Manufacturing Operation X must show every active SRE for
the MWO — including SREs whose own ``manufacturing_operation`` is a sibling
MOP — but compute MOP Log availability against each SRE's own operation,
not the popup's opened MOP.

Regression case (user-reported):
  MOP-461KI opened in UI; MWO has SREs 00163, 00164, 00165.
  SRE-00163.manufacturing_operation = MOP-EY179.
  Popup must show 00163 with availability calculated against MOP-EY179's
  MOP Log — never against MOP-461KI.

NB: ``frappe.db.get_all`` is one global function; patching it via two
module paths collides on the same target, so we install a single mock and
dispatch by doctype with ``side_effect``. The MOP Log dispatch keys off
the ``filters['manufacturing_operation']`` so each SRE's own MOP fetches
its own balance row.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase


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


def _make_get_all_side_effect(sre_rows=None, mop_log_rows_by_mop=None, sbe_rows=None):
	"""Dispatch ``frappe.db.get_all`` by doctype; for MOP Log dispatch
	additionally by ``filters['manufacturing_operation']`` so each SRE's
	own MOP gets its own balance row.
	"""
	sre_rows = sre_rows or []
	mop_log_rows_by_mop = mop_log_rows_by_mop or {}
	sbe_rows = sbe_rows or []

	def _side_effect(doctype, *args, **kwargs):
		if doctype == "Stock Reservation Entry":
			return sre_rows
		if doctype == "MOP Log":
			filters = kwargs.get("filters") or {}
			mop = filters.get("manufacturing_operation")
			return mop_log_rows_by_mop.get(mop, [])
		if doctype == "Serial and Batch Entry":
			return sbe_rows
		return []

	return _side_effect


class TestMwoLevelMakeReceiveEntry(IntegrationTestCase):
	"""SREs from sibling MOPs under the same MWO must appear, with
	availability computed against each SRE's own MOP Log.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.sql",
		return_value=[(0,)],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_current_mop_balance_rows"
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
	def test_sibling_mop_sre_returned_with_own_mop_balance(
		self,
		mock_get_doc,
		_mock_single,
		_mock_get_value,
		mock_get_all,
		mock_get_current_mop_balance_rows,
		_mock_sql,
	):
		"""SRE-OTHER belongs to MOP-EY179; popup opened on MOP-461KI.

		Expected: row appears with mop_available_qty taken from MOP-EY179's
		MOP Log (8.0), not MOP-461KI's (5.0). The fix computes the balance
		lookup using each SRE's own ``manufacturing_operation``.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		mock_get_doc.return_value = _make_mo()
		mock_get_current_mop_balance_rows.return_value = [
			frappe._dict(
				{
					"item_code": "M-G-18KT-75.4-Y",
					"batch_no": None,
					"qty_after_transaction_batch_based": 8.0,
					"pcs_after_transaction_batch_based": 0,
					"name": "MOP-LOG-EY179",
					"creation": "2026-05-01",
				}
			),
		]
		mock_get_all.side_effect = _make_get_all_side_effect(
			sre_rows=[
				_sre("SRE-OWN", manufacturing_operation="MOP-461KI", reserved_qty=5.0),
				_sre(
					"SRE-OTHER",
					manufacturing_operation="MOP-EY179",
					reserved_qty=8.0,
				),
			],
			mop_log_rows_by_mop={
				"MOP-461KI": [
					frappe._dict(
						{
							"item_code": "M-G-18KT-75.4-Y",
							"batch_no": None,
							"qty_after_transaction_batch_based": 5.0,
							"pcs_after_transaction_batch_based": 0,
							"name": "MOP-LOG-461",
							"creation": "2026-05-01",
						}
					)
				],
				"MOP-EY179": [
					frappe._dict(
						{
							"item_code": "M-G-18KT-75.4-Y",
							"batch_no": None,
							"qty_after_transaction_batch_based": 8.0,
							"pcs_after_transaction_batch_based": 0,
							"name": "MOP-LOG-EY179",
							"creation": "2026-05-01",
						}
					)
				],
			},
		)

		result = get_make_receive_entry_rows("MOP-461KI")
		rows = result["rows"]

		self.assertEqual(result["active_sre_count"], 2)
		# Both SREs surface.
		by_sre = {r["stock_reservation_entry"]: r for r in rows}
		self.assertIn("SRE-OWN", by_sre)
		self.assertIn("SRE-OTHER", by_sre)

		# Critical: SRE-OTHER (sibling MOP) gets MOP-EY179's balance (8.0),
		# NOT MOP-461KI's balance (5.0). Pre-fix this would have shown 5.0.
		self.assertAlmostEqual(by_sre["SRE-OTHER"]["mop_available_qty"], 8.0)
		self.assertAlmostEqual(by_sre["SRE-OTHER"]["available_to_receive_qty"], 8.0)

		# Sibling MOP is recorded on the row so the operator/audit can see
		# which operation owns the reservation.
		self.assertEqual(by_sre["SRE-OTHER"]["manufacturing_operation"], "MOP-EY179")

		# Source warehouse comes from the SRE itself, not the opened MOP's
		# department warehouse.
		self.assertEqual(by_sre["SRE-OTHER"]["s_warehouse"], "Casting WO - GEPL")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.db.sql",
		return_value=[(0,)],
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.get_current_mop_balance_rows"
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
		mock_get_current_mop_balance_rows,
		_mock_sql,
	):
		"""SRE filter must drop Cancelled/Delivered statuses (per ERPNext SRE
		lifecycle) — docstatus=1 alone is not sufficient.
		"""
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			get_make_receive_entry_rows,
		)

		mock_get_doc.return_value = _make_mo()
		mock_get_current_mop_balance_rows.return_value = []
		mock_get_all.side_effect = _make_get_all_side_effect()

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
