# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Regression guard for Stock Entry Detail ``transfer_qty`` precision.

A tiny but genuine metal loss (e.g. 0.005 g) is booked on the auto-created
"Process Loss" Stock Entry. ERPNext's ``StockEntry.set_transfer_qty`` rounds
``transfer_qty`` to the field precision and throws *"Qty in Stock UOM can not
be zero."* when it lands on 0. Core ``Stock Entry Detail.qty`` already carries
precision 3, but ``transfer_qty`` had no override and fell back to System
Settings ``float_precision`` (2), where ``flt(0.005, 2)`` rounds to 0.00 under
Banker's Rounding.

A property setter (``property_setter/stock_entry_detail.json``, applied via
``migrate.after_migrate`` → ``create_property_setter``) pins ``transfer_qty``
to precision 3 so the loss survives as a real stock movement. This test fails
if that customization is missing or regressed.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt


class TestStockEntryDetailPrecision(FrappeTestCase):
	def test_transfer_qty_precision_is_three(self):
		# The property setter must be in effect so transfer_qty matches qty (3).
		self.assertEqual(frappe.get_precision("Stock Entry Detail", "transfer_qty"), 3)
		self.assertEqual(frappe.get_precision("Stock Entry Detail", "qty"), 3)

	def test_sub_precision_loss_is_not_zeroed(self):
		# At precision 3 a 0.005 g loss is representable; at precision 2 (the
		# regressed state) it would round to 0.00 and trip the zero-qty throw.
		precision = frappe.get_precision("Stock Entry Detail", "transfer_qty")
		self.assertNotEqual(flt(0.005, precision), 0)
