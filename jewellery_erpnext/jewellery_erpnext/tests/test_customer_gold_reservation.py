# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""F29 -- a consumed reservation gives the customer's gold back to the free quantity.

``consume_stock_reservation_entry`` marks a reservation Delivered instead of cancelling it, and no
document event fires, so the Release written on cancel never happened: on kg-gk GJCU0009's free
quantity went negative. Pure-logic: the ledger writer and every read are stubbed.
"""

import unittest
from unittest.mock import patch

import frappe

from jewellery_erpnext.customer_subcontracting import customer_gold_fulfilment as cgf

COMPANY = "CG Co"
CUSTOMER = "GJCU0009"


def _sre(*entries):
	return frappe._dict(
		doctype="Stock Reservation Entry",
		name="SRE-1",
		company=COMPANY,
		item_code="M-G-22KT-91.75-Y",
		stock_uom="Gram",
		voucher_no="SO-1",
		sb_entries=[frappe._dict(name=n, batch_no=b, qty=q) for n, b, q in entries],
	)


class TestConsumedReservationIsReleased(unittest.TestCase):
	def setUp(self):
		self.events = []
		self.allocated = {"SBE-1"}
		owners = {"B-CUST": CUSTOMER, "B-CO": None}

		def exists(doctype, filters):
			key = filters["cg_event_key"]
			return any(
				key
				== cgf.build_event_key(
					COMPANY, "Stock Reservation Entry", name, None, cgf.EVENT_ALLOCATION
				)
				for name in self.allocated
			)

		patches = [
			patch.object(cgf, "is_ledger_schema_ready", return_value=True),
			patch.object(cgf, "is_customer_gold_enabled", return_value=True),
			patch.object(
				cgf, "get_customer_gold_valuation_policy", return_value="Zero Value"
			),
			patch.object(cgf, "_batch_owner", side_effect=lambda b: owners.get(b)),
			patch.object(cgf, "quantity_basis", return_value={}),
			patch.object(
				cgf,
				"_write_event",
				side_effect=lambda **kw: self.events.append(frappe._dict(kw)),
			),
			patch(f"{cgf.__name__}.frappe.db.exists", side_effect=exists),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def test_an_allocated_customer_batch_is_released_in_full(self):
		cgf.release_consumed_allocation(_sre(("SBE-1", "B-CUST", 2.5)))
		(event,) = self.events
		self.assertEqual(event.cg_event_kind, cgf.EVENT_RELEASE)
		self.assertEqual(event.cg_gross_qty_delta, -2.5)
		self.assertEqual(event.customer, CUSTOMER)

	def test_it_uses_the_same_key_as_a_cancel_so_it_releases_once(self):
		cgf.release_consumed_allocation(_sre(("SBE-1", "B-CUST", 2.5)))
		consumed_key = self.events[0].cg_event_key
		self.events.clear()
		cgf.release_allocation(_sre(("SBE-1", "B-CUST", 2.5)))
		self.assertEqual(self.events[0].cg_event_key, consumed_key)

	def test_an_entry_that_never_recorded_an_allocation_releases_nothing(self):
		"""A Release with no Allocation would overstate the free quantity."""
		cgf.release_consumed_allocation(_sre(("SBE-2", "B-CUST", 2.5)))
		self.assertEqual(self.events, [])

	def test_a_company_batch_releases_nothing(self):
		self.allocated.add("SBE-3")
		cgf.release_consumed_allocation(_sre(("SBE-3", "B-CO", 1.0)))
		self.assertEqual(self.events, [])
