# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""F5 -- a row is booked in the lane of the batch it actually draws, not the one the PMO expected.

KLHGX62F1119's order expected the customer's diamond. The manufacturer allows company stock in
its place, so the FIFO allocator took a company diamond batch (Regular Stock, no customer) --
and booked it as Customer Goods with a NULL customer through MAT-STE-18637/38/39. The GL was
unaffected; the custody records said the company's stone was the customer's.

Pure-logic, in the style of ``test_sample_goods_guard.TestSampleFifoExclusion``: fake docs, the
batch map and PMO reads stubbed, nothing written.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events import (
	se_utils,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.stock_entry import (
	lane_from_batch,
)

CUSTOMER = "GJCU0009"
PMO = "PMO-1"
CUSTOMER_DIAMOND = "B-CUST-DIA"
COMPANY_DIAMOND = "B-CO-DIA"

BATCHES = {
	CUSTOMER_DIAMOND: frappe._dict(
		custom_inventory_type="Customer Goods", custom_customer=CUSTOMER
	),
	COMPANY_DIAMOND: frappe._dict(
		custom_inventory_type="Regular Stock", custom_customer=None
	),
}


class _Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)


class _Row(_Doc):
	def db_set(self, key, value):
		setattr(self, key, value)

	def as_dict(self):
		return dict(self.__dict__)


class TestFifoTakesTheBatchLane(unittest.TestCase):
	"""``get_fifo_batches`` when the PMO expects the customer's diamond."""

	def _allocate(self, batches, qty, allow_substitution=1):
		se = _Doc(
			stock_entry_type="Material Transfer (WORK ORDER)",
			date=None,
			posting_time=None,
			posting_date=None,
			source_warehouse=None,
			main_slip=None,
			to_main_slip=None,
			flags=frappe._dict(),
		)
		row = _Row(
			qty=qty,
			item_code="D-NT-RO-6B",
			s_warehouse="WH-DIA",
			inventory_type=None,
			customer=None,
			custom_parent_manufacturing_order=PMO,
			custom_variant_of="D",
			manufacturing_operation=None,
			batch_no=None,
		)

		# Never raise inside these stubs: ``flt(x, precision)`` swallows any exception into 0.0
		# (see test_sample_goods_guard), which would silently empty the allocation.
		def get_value(doctype, *args, **kwargs):
			if doctype == "Parent Manufacturing Order":
				return frappe._dict(
					is_customer_gold=1,
					is_customer_diamond=1,
					is_customer_gemstone=0,
					is_customer_material=0,
					customer=CUSTOMER,
					manufacturer="MFR",
				)
			if doctype == "Manufacturer":
				return allow_substitution
			return None

		with (
			patch.object(
				se_utils,
				"get_auto_batch_nos",
				return_value=[frappe._dict(batch_no=b, qty=q) for b, q in batches],
			),
			patch.object(se_utils, "is_customer_sample_batch", return_value=False),
			patch.object(se_utils, "bulk_map", return_value=BATCHES),
			patch("frappe.db.get_value", side_effect=get_value),
		):
			rows = se_utils.get_fifo_batches(se, row)
		return {
			r.get("batch_no"): (r.get("inventory_type"), r.get("customer"))
			for r in rows
		}

	def test_an_allowed_company_diamond_stays_regular_stock(self):
		"""The KLHGX62F1119 case: before the fix this came back ("Customer Goods", "GJCU0009")."""
		lanes = self._allocate([(COMPANY_DIAMOND, 1.0)], qty=0.396)
		self.assertEqual(lanes, {COMPANY_DIAMOND: ("Regular Stock", None)})

	def test_the_customers_own_diamond_stays_the_customers(self):
		lanes = self._allocate([(CUSTOMER_DIAMOND, 1.0)], qty=0.396)
		self.assertEqual(lanes, {CUSTOMER_DIAMOND: ("Customer Goods", CUSTOMER)})

	def test_a_mixed_draw_labels_each_batch_by_its_owner(self):
		lanes = self._allocate(
			[(CUSTOMER_DIAMOND, 0.2), (COMPANY_DIAMOND, 1.0)], qty=0.396
		)
		self.assertEqual(lanes[CUSTOMER_DIAMOND], ("Customer Goods", CUSTOMER))
		self.assertEqual(lanes[COMPANY_DIAMOND], ("Regular Stock", None))

	def test_a_company_batch_first_does_not_relabel_the_customers_batch_after_it(self):
		"""The row is relabelled for the first batch; the copy made for the second must not inherit it."""
		lanes = self._allocate(
			[(COMPANY_DIAMOND, 0.2), (CUSTOMER_DIAMOND, 1.0)], qty=0.396
		)
		self.assertEqual(lanes[COMPANY_DIAMOND], ("Regular Stock", None))
		self.assertEqual(lanes[CUSTOMER_DIAMOND], ("Customer Goods", CUSTOMER))

	def test_without_the_manufacturer_allowance_a_company_batch_is_not_taken(self):
		self.assertEqual(
			self._allocate([(COMPANY_DIAMOND, 1.0)], qty=0.396, allow_substitution=0),
			{},
		)


class TestRebuildTakesTheBatchLane(unittest.TestCase):
	"""``update_batches`` rebuilds every row from its batch on each save."""

	def test_a_customer_row_drawing_a_company_batch_becomes_regular_stock(self):
		"""Was: the row's Customer Goods kept, the batch's NULL customer taken."""
		row = frappe._dict(
			inventory_type="Customer Goods", customer=CUSTOMER, batch_no=COMPANY_DIAMOND
		)
		self.assertEqual(
			lane_from_batch(row, BATCHES[COMPANY_DIAMOND]), ("Regular Stock", None)
		)

	def test_a_regular_row_drawing_a_customer_batch_becomes_the_customers(self):
		"""The other direction: company material never silently becomes customer material, and
		customer material never silently becomes company material."""
		row = frappe._dict(
			inventory_type="Regular Stock", customer=None, batch_no=CUSTOMER_DIAMOND
		)
		self.assertEqual(
			lane_from_batch(row, BATCHES[CUSTOMER_DIAMOND]),
			("Customer Goods", CUSTOMER),
		)

	def test_a_customer_goods_batch_without_a_customer_is_booked_regular(self):
		"""The coherence rule in ``normalize_ownership``: never emit Customer Goods with no customer."""
		row = frappe._dict(
			inventory_type="Customer Goods", customer=CUSTOMER, batch_no="B-ORPHAN"
		)
		batch = frappe._dict(
			custom_inventory_type="Customer Goods", custom_customer=None
		)
		self.assertEqual(lane_from_batch(row, batch), ("Regular Stock", None))

	def test_a_batch_with_no_lane_of_its_own_keeps_the_old_behaviour(self):
		row = frappe._dict(
			inventory_type="Customer Goods", customer="STALE", batch_no="B-LEGACY"
		)
		batch = frappe._dict(custom_inventory_type=None, custom_customer=CUSTOMER)
		self.assertEqual(lane_from_batch(row, batch), ("Customer Goods", CUSTOMER))
