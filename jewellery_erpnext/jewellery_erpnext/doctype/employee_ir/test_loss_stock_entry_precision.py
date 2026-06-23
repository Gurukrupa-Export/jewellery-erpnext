# Copyright (c) 2026, Aerele and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.property_setter_guard import ensure_stock_entry_detail_precision


class TestLossStockEntryPrecision(IntegrationTestCase):
	"""Regression guard for the Employee IR Process Loss SE sub-precision crash.

	Employee IR's auto-created Process Loss Stock Entry builds rows as small as 0.001 g. With
	System Settings float_precision = 2 and no per-field precision on Stock Entry Detail
	transfer_qty, ERPNext's set_transfer_qty() rounds flt(0.001, 2) = 0.0 and throws
	"Qty in Stock UOM can not be zero." -- aborting the whole EIR submit. The fix pins
	transfer_qty precision to 3 via a Property Setter (property_setter_guard). These tests fail
	loudly if that provisioning ever regresses.
	"""

	def test_guard_provisions_transfer_qty_precision(self):
		# Idempotent: safe to run even if already provisioned.
		ensure_stock_entry_detail_precision()

		self.assertTrue(
			frappe.db.exists(
				"Property Setter", "Stock Entry Detail-transfer_qty-precision"
			),
			"transfer_qty precision Property Setter is missing -- Process Loss SE submit would "
			"round 0.001 g to 0 and crash the Employee IR submit",
		)

		self.assertGreaterEqual(
			frappe.get_precision("Stock Entry Detail", "transfer_qty"),
			3,
			"Stock Entry Detail.transfer_qty precision must be >= 3 so a 0.001 g process loss "
			"survives set_transfer_qty",
		)

	def test_sub_precision_loss_qty_is_representable(self):
		ensure_stock_entry_detail_precision()
		precision = frappe.get_precision("Stock Entry Detail", "transfer_qty")
		# The exact failing value from the reported EIR (uoq5um7vvt).
		self.assertGreater(
			flt(0.001, precision),
			0,
			"0.001 g must not round to 0 at the live transfer_qty precision",
		)
