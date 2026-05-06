# Copyright (c) 2026, Nirali and contributors
# See license.txt

from unittest.mock import patch

from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	StockReservationEntry,
)
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.customization.stock_reservation_entry.stock_reservation_entry import (
	CustomStockReservationEntry,
)


def _bare_sre(**fields):
	# Bypass Document.__init__ — we only exercise the override branch logic;
	# no DB, no meta load, no controller hooks.
	sre = CustomStockReservationEntry.__new__(CustomStockReservationEntry)
	for k, v in fields.items():
		setattr(sre, k, v)
	sre.get = lambda key, default=None: getattr(sre, key, default)
	return sre


class TestCustomStockReservationEntry(FrappeTestCase):
	def test_skips_auto_reserve_for_mwo_mop_flow(self):
		# Override only fires for Serial-and-Batch reservations — auto-pick is
		# a no-op for Qty-based anyway, so the gate matches production code.
		sre = _bare_sre(
			manufacturing_work_order="MWO-1",
			manufacturing_operation="MOP-1",
			reservation_based_on="Serial and Batch",
			sb_entries=[{"batch_no": "B-PICKED", "qty": 2.0, "warehouse": "WH-Dept"}],
		)

		with patch.object(
			StockReservationEntry, "auto_reserve_serial_and_batch"
		) as parent_mock:
			sre.auto_reserve_serial_and_batch("Voucher")

		parent_mock.assert_not_called()
		self.assertEqual(len(sre.sb_entries), 1)
		self.assertEqual(sre.sb_entries[0]["batch_no"], "B-PICKED")

	def test_delegates_to_super_when_only_mwo_set(self):
		sre = _bare_sre(manufacturing_work_order="MWO-1", manufacturing_operation=None)

		with patch.object(
			StockReservationEntry, "auto_reserve_serial_and_batch"
		) as parent_mock:
			sre.auto_reserve_serial_and_batch("Voucher")

		parent_mock.assert_called_once_with("Voucher")

	def test_delegates_to_super_when_only_mop_set(self):
		sre = _bare_sre(manufacturing_work_order=None, manufacturing_operation="MOP-1")

		with patch.object(
			StockReservationEntry, "auto_reserve_serial_and_batch"
		) as parent_mock:
			sre.auto_reserve_serial_and_batch("Voucher")

		parent_mock.assert_called_once_with("Voucher")

	def test_delegates_to_super_for_normal_flow(self):
		sre = _bare_sre(manufacturing_work_order=None, manufacturing_operation=None)

		with patch.object(
			StockReservationEntry, "auto_reserve_serial_and_batch"
		) as parent_mock:
			sre.auto_reserve_serial_and_batch(None)

		parent_mock.assert_called_once_with(None)
