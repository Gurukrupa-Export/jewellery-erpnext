# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for ``_reservation_voucher_qty``.

Recreating a (reduced) Stock Reservation Entry must clear ERPNext's
``validate_with_allowed_qty`` guard even when the Sales Order line is already
over-reserved by sibling MWO reservations. The helper lifts ``voucher_qty`` to
cover ``total_so_reserved + reserved_qty`` so the guard passes — mirroring
``stock_reservation_entry_for_mwo``.
"""

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	loss_stock_entry,
)

# The helper imports this name locally from erpnext, so patch it at its source.
_TOTAL_RESERVED_PATH = (
	"erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry"
	".get_sre_reserved_qty_for_voucher_detail_no"
)


def _sre(**fields):
	defaults = {
		"voucher_type": "Sales Order",
		"voucher_no": "SAL-ORD-2026-00956",
		"voucher_detail_no": "soi-1",
		"item_code": "M-G-22KT-91.9-Y",
		"name": "MAT-SRE-2026-73342",
		"voucher_qty": 28.2675,
	}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


class TestReservationVoucherQty(FrappeTestCase):
	def test_over_reserved_so_lifts_voucher_qty(self):
		# Other active SREs already hold 22.2 against a 28.2675 SO line; the
		# reduced reservation of 7.76 would otherwise exceed the 6.068 allowance.
		sre = _sre()
		with patch(_TOTAL_RESERVED_PATH, return_value=22.2) as mock_total:
			result = loss_stock_entry._reservation_voucher_qty(sre, 7.76)

		self.assertAlmostEqual(result, 29.96)
		mock_total.assert_called_once_with(
			"M-G-22KT-91.9-Y",
			"Sales Order",
			"SAL-ORD-2026-00956",
			"soi-1",
			ignore_sre="MAT-SRE-2026-73342",
		)

	def test_keeps_base_when_not_over_reserved(self):
		# total + qty (5.0 + 7.76 = 12.76) is below the SO qty → keep base.
		sre = _sre()
		with patch(_TOTAL_RESERVED_PATH, return_value=5.0):
			result = loss_stock_entry._reservation_voucher_qty(sre, 7.76)

		self.assertAlmostEqual(result, 28.2675)

	def test_non_sales_order_returns_base(self):
		sre = _sre(voucher_type="Work Order")
		with patch(_TOTAL_RESERVED_PATH) as mock_total:
			result = loss_stock_entry._reservation_voucher_qty(sre, 7.76)

		self.assertAlmostEqual(result, 28.2675)
		mock_total.assert_not_called()

	def test_missing_voucher_detail_returns_base(self):
		sre = _sre(voucher_detail_no=None)
		with patch(_TOTAL_RESERVED_PATH) as mock_total:
			result = loss_stock_entry._reservation_voucher_qty(sre, 7.76)

		self.assertAlmostEqual(result, 28.2675)
		mock_total.assert_not_called()
