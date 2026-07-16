# Copyright (c) 2026, Aerele and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry import (
	_find_sre,
)

_LOSS = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry"
_EOD = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync"


def _sre_row(warehouse="WH-X"):
	return {
		"name": "SRE-NEW",
		"warehouse": warehouse,
		"reserved_qty": 5.0,
		"available_qty": 5.0,
		"voucher_qty": 100.0,
		"reservation_based_on": "Serial and Batch",
		"has_batch_no": 1,
		"company": "Co",
		"voucher_type": "Sales Order",
		"voucher_no": "SO-1",
		"voucher_detail_no": "SOI-1",
		"stock_uom": "Gram",
		"manufacturing_operation": "MOP-1",
	}


class TestFindSreSelfHeal(FrappeTestCase):
	"""_find_sre self-heals an EOD-orphaned WIP reservation instead of throwing.

	When EOD sync cancels a batch's source SREs and silently skips re-reserving (v16 SBB
	batch stock parked at a non-target warehouse), zero active SREs remain and Process Loss
	used to hard-fail with "No active Stock Reservation Entry found". _find_sre now re-creates
	the reservation at the batch's physical warehouse and re-queries; the loss then consumes
	from that warehouse (s_warehouse = sre_doc.warehouse), never negative.
	"""

	def _row(self):
		return SimpleNamespace(
			item_code="M-1", batch_no="B1", manufacturing_operation="MOP-1", idx=5
		)

	def _eir(self):
		return SimpleNamespace(name="EIR-1", company="Co")

	def test_orphaned_reservation_is_healed_and_consumes_from_physical_wh(self):
		# First lookup empty; after the heal the re-query returns the new SRE at WH-X.
		with patch(
			f"{_LOSS}._query_batch_and_qty_sres", side_effect=[[], [_sre_row("WH-X")]]
		), patch(
			f"{_EOD}._reserve_batch_at_physical_warehouse", return_value=["SRE-NEW"]
		) as heal, patch(
			f"{_LOSS}.frappe.get_doc",
			return_value=SimpleNamespace(name="SRE-NEW", warehouse="WH-X"),
		):
			sre_doc, candidates = _find_sre(
				self._eir(), self._row(), "MWO-1", "employee_loss_details", 0.01
			)

		heal.assert_called_once_with("MWO-1", "M-1", "B1", 0.01, "MOP-1", "Co")
		# The loss will consume from the healed warehouse (physical truth), not negative stock.
		self.assertEqual(sre_doc.warehouse, "WH-X")
		self.assertEqual(candidates[0]["warehouse"], "WH-X")

	def test_still_missing_after_heal_throws_original_error(self):
		# Heal cannot reserve (no warehouse holds free batch qty) -> original throw preserved.
		with patch(f"{_LOSS}._query_batch_and_qty_sres", return_value=[]), patch(
			f"{_EOD}._reserve_batch_at_physical_warehouse", return_value=None
		):
			with self.assertRaises(frappe.exceptions.ValidationError):
				_find_sre(
					self._eir(), self._row(), "MWO-1", "employee_loss_details", 0.01
				)

	def test_healthy_lookup_never_calls_heal(self):
		# An SRE already exists -> the self-heal path is never reached (zero cost).
		with patch(
			f"{_LOSS}._query_batch_and_qty_sres", return_value=[_sre_row("WH-X")]
		), patch(f"{_EOD}._reserve_batch_at_physical_warehouse") as heal, patch(
			f"{_LOSS}.frappe.get_doc",
			return_value=SimpleNamespace(name="SRE-NEW", warehouse="WH-X"),
		):
			_find_sre(self._eir(), self._row(), "MWO-1", "employee_loss_details", 0.01)

		heal.assert_not_called()
