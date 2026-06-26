# Copyright (c) 2026, Aerele and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from jewellery_erpnext.property_setter_guard import (
	ensure_field_precision_property_setters,
)


class TestLossStockEntryPrecision(IntegrationTestCase):
	"""Regression guard for the Employee IR Process Loss SE sub-precision crash.

	Employee IR's auto-created Process Loss Stock Entry builds rows as small as 0.001 g. With
	System Settings float_precision = 2 and no per-field precision, ERPNext rounds flt(0.001, 2)
	= 0.0 at two layers and aborts the whole EIR submit:
	  * Stock Entry Detail.transfer_qty -- set_transfer_qty() throws
	    "Qty in Stock UOM can not be zero." on the SE row.
	  * Serial and Batch Entry.qty -- the Serial and Batch Bundle built on submit rounds the
	    batch qty to 0 and throws "At row 1: Qty is mandatory for the batch ...".
	The fix pins these fields to precision 3 via Property Setters (property_setter_guard). The
	same guard also pins the Stock Reservation Entry qty fields (reserved_qty, available_qty,
	delivered_qty, voucher_qty, consumed_qty, transferred_qty): the Transfer-to-Reserve Material
	Request flow submits SREs whose validate_with_allowed_qty rounds a genuine sub-0.01 ct
	available qty to 0 and throws "Cannot reserve more than Allowed Qty 0.0". These tests fail
	loudly if any of that provisioning ever regresses.
	"""

	def test_guard_provisions_transfer_qty_precision(self):
		# Idempotent: safe to run even if already provisioned.
		ensure_field_precision_property_setters()

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

	def test_guard_provisions_serial_and_batch_entry_qty_precision(self):
		# Idempotent: safe to run even if already provisioned.
		ensure_field_precision_property_setters()

		self.assertTrue(
			frappe.db.exists("Property Setter", "Serial and Batch Entry-qty-precision"),
			"Serial and Batch Entry.qty precision Property Setter is missing -- the Process Loss "
			"SE's Serial and Batch Bundle would round 0.001 g to 0 and throw 'Qty is mandatory "
			"for the batch', crashing the Employee IR submit",
		)

		self.assertGreaterEqual(
			frappe.get_precision("Serial and Batch Entry", "qty"),
			3,
			"Serial and Batch Entry.qty precision must be >= 3 so a 0.001 g batch loss survives "
			"SerialBatchCreation.set_serial_batch_entries",
		)

	def test_sub_precision_loss_qty_is_representable(self):
		ensure_field_precision_property_setters()
		# The exact failing value from the reported EIRs (uoq5um7vvt, 54hej2sdud). It must not
		# round to 0 at the live precision of EITHER precision-sensitive field.
		for doctype, fieldname in (
			("Stock Entry Detail", "transfer_qty"),
			("Serial and Batch Entry", "qty"),
		):
			precision = frappe.get_precision(doctype, fieldname)
			self.assertGreater(
				flt(0.001, precision),
				0,
				f"0.001 g must not round to 0 at the live {doctype}.{fieldname} precision",
			)

	def test_guard_provisions_stock_reservation_entry_qty_precision(self):
		# Idempotent: safe to run even if already provisioned.
		ensure_field_precision_property_setters()

		self.assertTrue(
			frappe.db.exists(
				"Property Setter", "Stock Reservation Entry-reserved_qty-precision"
			),
			"Stock Reservation Entry.reserved_qty precision Property Setter is missing -- SRE submit "
			"would round a sub-0.01 ct available qty to 0 in validate_with_allowed_qty and throw "
			"'Cannot reserve more than Allowed Qty 0.0', aborting the Transfer-to-Reserve MR flow",
		)

		for fieldname in (
			"reserved_qty",
			"available_qty",
			"delivered_qty",
			"voucher_qty",
			"consumed_qty",
			"transferred_qty",
		):
			self.assertGreaterEqual(
				frappe.get_precision("Stock Reservation Entry", fieldname),
				3,
				f"Stock Reservation Entry.{fieldname} precision must be >= 3 so a sub-0.01 ct "
				"reservation qty survives the reserve->deliver->consume->transfer lifecycle",
			)

	def test_sub_precision_reserve_qty_is_representable(self):
		ensure_field_precision_property_setters()
		# The exact failing value from the reported SRE (Item D-NT-RO-7-+000-00 against
		# SAL-ORD-2026-01072): 0.005 ct available must not round to 0 at the live reserved_qty
		# precision, else allowed_qty collapses to 0.0 and the reservation is rejected.
		precision = frappe.get_precision("Stock Reservation Entry", "reserved_qty")
		self.assertGreater(
			flt(0.0050000000000000044, precision),
			0,
			"0.005 ct must not round to 0 at the live Stock Reservation Entry.reserved_qty precision",
		)
