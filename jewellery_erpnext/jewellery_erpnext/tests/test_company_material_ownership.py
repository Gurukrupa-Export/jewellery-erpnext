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


class _FifoCase(unittest.TestCase):
	"""Runs ``get_fifo_batches`` for one row against a stubbed order, manufacturer and batches."""

	def _allocate(
		self,
		batches,
		qty,
		allow_substitution=1,
		row_variant="F",
		item_variant="F",
		item_map=None,
		row_pmo=PMO,
		header_pmo=None,
		order=None,
	):
		se = _Doc(
			stock_entry_type="Material Transfer (WORK ORDER)",
			date=None,
			posting_time=None,
			posting_date=None,
			source_warehouse=None,
			main_slip=None,
			to_main_slip=None,
			manufacturing_order=header_pmo,
			flags=frappe._dict(),
		)
		row = _Row(
			idx=1,
			qty=qty,
			item_code="D-NT-RO-6B",
			s_warehouse="WH-DIA",
			inventory_type=None,
			customer=None,
			custom_parent_manufacturing_order=row_pmo,
			custom_variant_of=row_variant,
			manufacturing_operation=None,
			batch_no=None,
		)
		order = order or frappe._dict(
			is_customer_gold=1,
			is_customer_diamond=1,
			is_customer_gemstone=0,
			is_customer_material=0,
			customer=CUSTOMER,
			manufacturer="MFR",
		)
		self.pmo_reads = []

		# Never raise inside these stubs: ``flt(x, precision)`` swallows any exception into 0.0
		# (see test_sample_goods_guard), which would silently empty the allocation.
		def get_value(doctype, *args, **kwargs):
			if doctype == "Parent Manufacturing Order":
				self.pmo_reads.append(args[0] if args else None)
				return order
			if doctype == "Manufacturer":
				return allow_substitution
			if doctype == "Item":
				return item_variant
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
			rows = se_utils.get_fifo_batches(se, row, item_map=item_map)
		return {
			r.get("batch_no"): (r.get("inventory_type"), r.get("customer"))
			for r in rows
		}


class TestFifoTakesTheBatchLane(_FifoCase):
	"""``get_fifo_batches`` when the PMO expects the customer's material.

	The manufacturer's allowance to put company stock in the customer's place is exercised on a
	FINDING (gold, variant F): diamonds and gemstones never take it -- see
	``TestDiamondsAndGemstonesAreNeverSubstituted``.
	"""

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
		"""A finding runs short with a message, not a refusal -- the shortfall rule is unchanged."""
		self.assertEqual(
			self._allocate([(COMPANY_DIAMOND, 1.0)], qty=0.396, allow_substitution=0),
			{},
		)


class TestDiamondsAndGemstonesAreNeverSubstituted(_FifoCase):
	"""On a customer's diamond or gemstone order only that customer's own batches are drawn.

	MAT-STE-57381: a Material Transfer (WORK ORDER) on customer-diamond order
	PMO-KGJPL-RI00650-006-0002 took a Regular Stock diamond, because the row named no order (only
	the header did) and the manufacturer allows company stock in the customer's place.
	"""

	def _diamond(self, batches, **kwargs):
		kwargs.setdefault("row_variant", "D")
		kwargs.setdefault("item_variant", "D")
		return self._allocate(batches, qty=0.396, **kwargs)

	def _gemstone_order(self):
		return frappe._dict(
			is_customer_gold=0,
			is_customer_diamond=0,
			is_customer_gemstone=1,
			is_customer_material=0,
			customer=CUSTOMER,
			manufacturer="MFR",
		)

	def test_the_allowance_does_not_put_a_company_diamond_first(self):
		"""FIFO order put the company batch first; the customer's must be drawn instead."""
		lanes = self._diamond([(COMPANY_DIAMOND, 1.0), (CUSTOMER_DIAMOND, 1.0)])
		self.assertEqual(lanes, {CUSTOMER_DIAMOND: ("Customer Goods", CUSTOMER)})

	def test_no_customer_diamond_at_all_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._diamond([(COMPANY_DIAMOND, 1.0)])
		self.assertIn(CUSTOMER, str(raised.exception))

	def test_too_little_customer_diamond_is_refused_not_topped_up(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._diamond([(CUSTOMER_DIAMOND, 0.2), (COMPANY_DIAMOND, 1.0)])
		self.assertIn("0.2", str(raised.exception))

	def test_without_the_allowance_a_shortfall_is_refused_too(self):
		with self.assertRaises(frappe.ValidationError):
			self._diamond([(COMPANY_DIAMOND, 1.0)], allow_substitution=0)

	def test_gemstones_follow_the_same_rule(self):
		with self.assertRaises(frappe.ValidationError):
			self._allocate(
				[(COMPANY_GEM, 1.0)],
				qty=0.5,
				row_variant="G",
				item_variant="G",
				order=self._gemstone_order(),
			)

	def test_a_customer_gemstone_is_taken_past_a_company_one(self):
		with patch.dict(BATCHES, GEM_BATCHES):
			lanes = self._allocate(
				[(COMPANY_GEM, 1.0), (CUSTOMER_GEM, 1.0)],
				qty=0.5,
				row_variant="G",
				item_variant="G",
				order=self._gemstone_order(),
			)
		self.assertEqual(lanes, {CUSTOMER_GEM: ("Customer Goods", CUSTOMER)})

	def test_an_order_named_only_in_the_header_is_read(self):
		"""The MAT-STE-57381 shape: row order empty, header order set."""
		lanes = self._diamond(
			[(COMPANY_DIAMOND, 1.0), (CUSTOMER_DIAMOND, 1.0)], row_pmo=None, header_pmo=PMO
		)
		self.assertEqual(self.pmo_reads, [PMO])
		self.assertEqual(lanes, {CUSTOMER_DIAMOND: ("Customer Goods", CUSTOMER)})

	def test_the_rows_own_order_wins_over_the_header(self):
		self._diamond([(CUSTOMER_DIAMOND, 1.0)], row_pmo=PMO, header_pmo="PMO-HEADER")
		self.assertEqual(self.pmo_reads, [PMO])

	def test_a_finding_on_the_same_order_still_takes_the_allowance(self):
		lanes = self._allocate([(COMPANY_DIAMOND, 1.0)], qty=0.396)
		self.assertEqual(lanes, {COMPANY_DIAMOND: ("Regular Stock", None)})


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


CUSTOMER_GEM = "B-CUST-GEM"
COMPANY_GEM = "B-CO-GEM"
GEM_BATCHES = {
	CUSTOMER_GEM: frappe._dict(
		custom_inventory_type="Customer Goods", custom_customer=CUSTOMER
	),
	COMPANY_GEM: frappe._dict(
		custom_inventory_type="Regular Stock", custom_customer=None
	),
}


class TestTheLaneDoesNotNeedTheFetchedVariantLetter(unittest.TestCase):
	"""The reserve Stock Entry's rows reach the allocator with ``custom_variant_of`` still empty.

	``custom_variant_of`` is ``fetch_from: item_code.variant_of``, resolved during ``validate``.
	``update_batches`` -> ``get_fifo_batches`` runs a step earlier in ``before_validate``, so a
	server-built entry whose rows were appended as plain dicts -- which is exactly how
	``material_request.create_stock_entry`` builds the reserve entry -- had no letter to match on.
	The Customer Goods lane therefore never applied and FIFO took the oldest batch in the
	warehouse: a company one, on every customer-diamond and customer-gemstone order.

	The old suite above hid this by hand-setting ``custom_variant_of="D"`` on its row.
	"""

	def _allocate(self, batches, batch_map, pmo, qty, **kwargs):
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
			item_code=kwargs.get("item_code", "D-NT-RO-6B"),
			s_warehouse="WH-DIA",
			inventory_type=None,
			customer=None,
			custom_parent_manufacturing_order=PMO,
			# The point of the whole class: the row does NOT carry the letter.
			custom_variant_of=None,
			manufacturing_operation=None,
			batch_no=None,
		)

		def get_value(doctype, *args, **kwargs_):
			if doctype == "Parent Manufacturing Order":
				return pmo
			if doctype == "Manufacturer":
				return kwargs.get("allow_substitution", 0)
			if doctype == "Item":
				return kwargs.get("item_variant")
			return None

		with (
			patch.object(
				se_utils,
				"get_auto_batch_nos",
				return_value=[frappe._dict(batch_no=b, qty=q) for b, q in batches],
			),
			patch.object(se_utils, "is_customer_sample_batch", return_value=False),
			patch.object(se_utils, "bulk_map", return_value=batch_map),
			patch("frappe.db.get_value", side_effect=get_value),
		):
			rows = se_utils.get_fifo_batches(
				se, row, item_map=kwargs.get("item_map")
			)
		return {
			r.get("batch_no"): (r.get("inventory_type"), r.get("customer"))
			for r in rows
		}

	def _diamond_order(self):
		return frappe._dict(
			is_customer_gold=0,
			is_customer_diamond=1,
			is_customer_gemstone=0,
			is_customer_material=0,
			customer=CUSTOMER,
			manufacturer="MFR",
		)

	def test_the_item_master_supplies_the_letter_the_row_lacks(self):
		lanes = self._allocate(
			[(CUSTOMER_DIAMOND, 1.0)],
			BATCHES,
			self._diamond_order(),
			qty=0.396,
			item_variant="D",
		)
		self.assertEqual(lanes, {CUSTOMER_DIAMOND: ("Customer Goods", CUSTOMER)})

	def test_a_company_diamond_is_refused_once_the_letter_resolves(self):
		"""The reservation bug: without the letter this quietly returned the company batch."""
		with self.assertRaises(frappe.ValidationError):
			self._allocate(
				[(COMPANY_DIAMOND, 1.0)],
				BATCHES,
				self._diamond_order(),
				qty=0.396,
				item_variant="D",
				allow_substitution=0,
			)

	def test_a_prefetched_item_map_is_used_instead_of_a_query(self):
		"""``update_batches`` already holds this map, so the common path costs no extra query."""
		lanes = self._allocate(
			[(CUSTOMER_DIAMOND, 1.0)],
			BATCHES,
			self._diamond_order(),
			qty=0.396,
			# No ``item_variant``: a stray Item query would answer None and lose the lane.
			item_map={"D-NT-RO-6B": {"variant_of": "D"}},
		)
		self.assertEqual(lanes, {CUSTOMER_DIAMOND: ("Customer Goods", CUSTOMER)})

	def test_gemstone_rows_follow_is_customer_gemstone(self):
		order = frappe._dict(
			is_customer_gold=0,
			is_customer_diamond=0,
			is_customer_gemstone=1,
			is_customer_material=0,
			customer=CUSTOMER,
			manufacturer="MFR",
		)
		lanes = self._allocate(
			[(CUSTOMER_GEM, 1.0)],
			GEM_BATCHES,
			order,
			qty=0.5,
			item_code="G-EM-OV",
			item_variant="G",
		)
		self.assertEqual(lanes, {CUSTOMER_GEM: ("Customer Goods", CUSTOMER)})

	def test_a_gemstone_row_on_a_diamond_only_order_stays_company_stock(self):
		"""Per material type: the diamond flag must not drag gemstone rows into the customer lane."""
		lanes = self._allocate(
			[(COMPANY_GEM, 1.0)],
			GEM_BATCHES,
			self._diamond_order(),
			qty=0.5,
			item_code="G-EM-OV",
			item_variant="G",
		)
		self.assertEqual(lanes, {COMPANY_GEM: ("Regular Stock", None)})

	def test_a_deleted_order_does_not_raise(self):
		"""``frappe.db.get_value`` answers None for a missing PMO; the old code subscripted it."""
		lanes = self._allocate(
			[(COMPANY_DIAMOND, 1.0)],
			BATCHES,
			None,
			qty=0.396,
			item_variant="D",
		)
		self.assertEqual(lanes, {COMPANY_DIAMOND: ("Regular Stock", None)})
