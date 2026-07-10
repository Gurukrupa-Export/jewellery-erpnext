# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for source-batch inventory-type resolution in the Employee IR loss flow.

The Process Loss Stock Entry is built with ``auto_created = 1``, so the
interactive Batch backfill (CustomStockEntry.update_batches) is skipped and the
loss rows arrive with a blank ``inventory_type``. Stock Entry ``before_validate``
then stamps every blank row as "Regular Stock" -- so a loss drawn from a
Customer Goods batch was being booked as Regular Stock.

``_resolve_batch_inventory`` closes that gap by reading the SOURCE batch's
``custom_inventory_type`` / ``custom_customer`` before the SE is inserted, and
``is_process_loss_repack`` exempts the minted scrap batch from the
"not allowed as Customer Goods" guard so the customer's scrapped metal can stay
the customer's.
"""

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.batch.doc_events import (
	utils as batch_utils,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	loss_stock_entry,
)


def _row(**fields):
	defaults = {
		"idx": 1,
		"item_code": "M-G-18KT-75.4-Y",
		"batch_no": "Kanish Ext-2F05-M-G-18KT-75.4-Y-01-A",
		"inventory_type": None,
		"customer": None,
	}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


class TestLossBatchInventoryResolution(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _resolve(self, row, batch_value):
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir."
			"doc_events.loss_stock_entry.frappe"
		) as mock_frappe:
			mock_frappe.db.get_value.return_value = batch_value
			return loss_stock_entry._resolve_batch_inventory(row)

	def test_customer_goods_batch_flows_through(self):
		# The reported bug: source batch is Customer Goods -> row must be too.
		inv, cust = self._resolve(
			_row(),
			{
				"custom_inventory_type": "Customer Goods",
				"custom_customer": "Kanish Ext",
			},
		)
		self.assertEqual(inv, "Customer Goods")
		self.assertEqual(cust, "Kanish Ext")

	def test_regular_stock_batch_yields_no_customer(self):
		# Batch has no custom_inventory_type -> Regular Stock and no stray customer.
		inv, cust = self._resolve(
			_row(), {"custom_inventory_type": None, "custom_customer": None}
		)
		self.assertEqual(inv, "Regular Stock")
		self.assertIsNone(cust)

	def test_batch_wins_over_row_value(self):
		# The batch is the physical truth; a stale value on the loss row must not win.
		inv, cust = self._resolve(
			_row(inventory_type="Regular Stock", customer="Someone Else"),
			{
				"custom_inventory_type": "Customer Goods",
				"custom_customer": "Kanish Ext",
			},
		)
		self.assertEqual(inv, "Customer Goods")
		self.assertEqual(cust, "Kanish Ext")

	def test_regular_stock_drops_stray_batch_customer(self):
		# Defensive coherence: a Regular Stock row never carries a customer, even
		# if the batch record somehow has one.
		inv, cust = self._resolve(
			_row(), {"custom_inventory_type": "Regular Stock", "custom_customer": "X"}
		)
		self.assertEqual(inv, "Regular Stock")
		self.assertIsNone(cust)

	def test_row_value_used_when_batch_missing_the_field(self):
		# Batch exists but custom_inventory_type unset -> fall back to the row's
		# own value (e.g. a manually_book_loss_details row the user tagged).
		inv, cust = self._resolve(
			_row(inventory_type="Customer Stock", customer="Kanish Ext"),
			{"custom_inventory_type": None, "custom_customer": None},
		)
		self.assertEqual(inv, "Customer Stock")
		self.assertEqual(cust, "Kanish Ext")

	def test_customer_type_without_customer_downgrades_to_regular(self):
		# Malformed batch: Customer Goods but no customer anywhere. Emitting a
		# customer type with no customer would defeat the scrap-batch guard
		# exemption and hard-fail the submit, so it downgrades to Regular Stock.
		inv, cust = self._resolve(
			_row(), {"custom_inventory_type": "Customer Goods", "custom_customer": None}
		)
		self.assertEqual(inv, "Regular Stock")
		self.assertIsNone(cust)

	def test_no_batch_no_defaults_to_regular(self):
		inv, cust = self._resolve(_row(batch_no=None), None)
		self.assertEqual(inv, "Regular Stock")
		self.assertIsNone(cust)


class TestProcessLossCustomerGoodsExemption(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _is_exempt(self, batch, se_type):
		with patch(
			"jewellery_erpnext.jewellery_erpnext.customization.batch."
			"doc_events.utils.frappe"
		) as mock_frappe:
			mock_frappe.db.get_value.return_value = se_type
			return batch_utils.is_process_loss_repack(batch)

	def _batch(self, **fields):
		defaults = {
			"reference_doctype": "Stock Entry",
			"reference_name": "MAT-STE-48001",
			"custom_customer": "Kanish Ext",
			"item": "ML-G-18KT-75.4-Y",
		}
		defaults.update(fields)
		return SimpleNamespace(**defaults)

	def test_process_loss_with_customer_is_exempt(self):
		self.assertTrue(self._is_exempt(self._batch(), "Process Loss"))

	def test_non_stock_entry_not_exempt(self):
		# Returns before any get_value; se_type is irrelevant.
		self.assertFalse(
			self._is_exempt(
				self._batch(reference_doctype="Purchase Receipt"), "Process Loss"
			)
		)

	def test_no_customer_not_exempt(self):
		self.assertFalse(
			self._is_exempt(self._batch(custom_customer=None), "Process Loss")
		)

	def test_other_stock_entry_type_not_exempt(self):
		# A non-loss SE must still be blocked when the item disallows customer goods.
		self.assertFalse(self._is_exempt(self._batch(), "Repack"))
