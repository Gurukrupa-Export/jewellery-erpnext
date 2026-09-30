# Copyright (c) 2024, Nirali and Contributors
# See license.txt

import json
import os
import random
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
	BatchNegativeStockError,
)
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase
from frappe.utils import flt as _frappe_flt

from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions import (
	metal_conversions as mc,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events import (
	lanes as lanes_mod,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions import (
	REMARK_TEMPLATES,
	MetalConversions,
	render_remark_options,
	template_index,
)

_MC_PATH = (
	"jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions"
)

#: The real functions, saved before any test patches them (see ``_BuilderCase._build``).
_REAL_GET_DOC = frappe.get_doc
_REAL_NEW_DOC = frappe.new_doc


def _det_flt(value, precision=None, rounding_method=None):
	"""Deterministic stand-in for frappe.utils.flt (see the melting-loss suite)."""
	try:
		num = float(value or 0)
	except (TypeError, ValueError):
		return 0.0
	return round(num, precision) if precision is not None else num


def _bankers_flt(value, precision=None, rounding_method=None):
	"""frappe's own flt, with the rounding kg-gk, gk and the test site are set to."""
	return _frappe_flt(value, precision, rounding_method="Banker's Rounding")


def _alloc(qty, batch):
	"""A source_batch_details / alloy_batch_details row."""
	row = SimpleNamespace(qty=qty, batch=batch)
	row.get = lambda k, default=None: getattr(row, k, default)
	return row


def _shrink_last_lane(lanes, target_qty, precision=3):
	"""split_conversion stand-in that under-apportions, to trip the sum guard."""
	lanes = lanes_mod.split_conversion(lanes, target_qty, precision)
	lanes[-1]["target_qty"] = lanes[-1]["target_qty"] - 1.0
	return lanes


class TestBuildLanes(IntegrationTestCase):
	"""build_lanes groups a FIFO allocation by ownership, preserving FIFO order."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_the_20g_requirement(self):
		allocations = [_alloc(8.0, "REG"), _alloc(12.0, "CG")]
		lane_map = {
			"REG": ("Regular Stock", None),
			"CG": ("Customer Goods", "TNCU0001"),
		}
		result = lanes_mod.build_lanes(allocations, lane_map)

		self.assertEqual(len(result), 2)
		self.assertEqual(result[0]["inventory_type"], "Regular Stock")
		self.assertIsNone(result[0]["customer"])
		self.assertEqual(result[0]["source_qty"], 8.0)
		self.assertEqual(result[1]["inventory_type"], "Customer Goods")
		self.assertEqual(result[1]["customer"], "TNCU0001")
		self.assertEqual(result[1]["source_qty"], 12.0)

	def test_every_customer_batch_is_its_own_lane(self):
		"""Two batches of one customer are two lanes -- same customer is not same batch."""
		allocations = [
			_alloc(3.0, "A1"),
			_alloc(2.0, "REG"),
			_alloc(5.0, "B1"),
			_alloc(1.0, "A2"),
		]
		lane_map = {
			"A1": ("Customer Goods", "CUST-A"),
			"REG": ("Regular Stock", None),
			"B1": ("Customer Goods", "CUST-B"),
			"A2": ("Customer Goods", "CUST-A"),
		}
		result = lanes_mod.build_lanes(allocations, lane_map)

		# Ordered by FIRST appearance; A1 and A2 stay apart although both are CUST-A's.
		self.assertEqual(
			[
				(
					lane["inventory_type"],
					lane["customer"],
					lane["batch"],
					lane["source_qty"],
				)
				for lane in result
			],
			[
				("Customer Goods", "CUST-A", "A1", 3.0),
				("Regular Stock", None, None, 2.0),
				("Customer Goods", "CUST-B", "B1", 5.0),
				("Customer Goods", "CUST-A", "A2", 1.0),
			],
		)
		self.assertEqual([b["batch"] for b in result[0]["batches"]], ["A1"])

	def test_regular_stock_batches_still_pool(self):
		"""Company stock is one ownership: its batches share one lane, as before."""
		result = lanes_mod.build_lanes(
			[_alloc(2.0, "R1"), _alloc(3.0, "R2")],
			{"R1": ("Regular Stock", None), "R2": ("Regular Stock", None)},
		)
		self.assertEqual(len(result), 1)
		self.assertIsNone(result[0]["batch"])
		self.assertEqual(result[0]["source_qty"], 5.0)
		self.assertEqual([b["batch"] for b in result[0]["batches"]], ["R1", "R2"])

	def test_a_batch_listed_twice_folds_into_its_one_lane(self):
		"""Both rows convert together, so the batch is checked against its balance once."""
		result = lanes_mod.build_lanes(
			[_alloc(2.0, "A1"), _alloc(1.5, "A1")],
			{"A1": ("Customer Goods", "CUST-A")},
		)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["source_qty"], 3.5)
		self.assertEqual(len(result[0]["batches"]), 2)

	def test_unmapped_and_null_batches_are_regular_stock(self):
		"""An untyped batch is company stock, not a third ownership."""
		allocations = [_alloc(4.0, "UNTYPED"), _alloc(1.0, "MISSING")]
		result = lanes_mod.build_lanes(allocations, {"UNTYPED": (None, None)})

		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["inventory_type"], "Regular Stock")
		self.assertEqual(result[0]["source_qty"], 5.0)

	def test_zero_and_batchless_rows_ignored(self):
		allocations = [_alloc(0.0, "ZERO"), _alloc(5.0, None), _alloc(2.0, "REAL")]
		result = lanes_mod.build_lanes(allocations, {})
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["source_qty"], 2.0)


class TestApportion(IntegrationTestCase):
	"""apportion never invents or loses qty, whatever the rounding."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_exact_split(self):
		self.assertEqual(lanes_mod.apportion(26.667, [8.0, 12.0], 3), [10.667, 16.0])

	def test_residual_absorbed_by_largest_lane(self):
		# 10 / 3 lanes of equal weight = 3.333 each -> 9.999, residual 0.001.
		parts = lanes_mod.apportion(10.0, [1.0, 1.0, 1.0], 3)
		self.assertAlmostEqual(sum(parts), 10.0, places=9)
		# Equal weights: max() picks the first.
		self.assertEqual(parts, [3.334, 3.333, 3.333])

	def test_residual_goes_to_the_biggest_not_the_last(self):
		parts = lanes_mod.apportion(10.0, [7.0, 1.0, 1.0, 1.0], 3)
		self.assertAlmostEqual(sum(parts), 10.0, places=9)
		self.assertEqual(max(parts), parts[0])

	def test_degenerate_inputs(self):
		self.assertEqual(lanes_mod.apportion(10.0, []), [])
		self.assertEqual(lanes_mod.apportion(10.0, [0.0, 0.0], 3), [0.0, 0.0])
		self.assertEqual(lanes_mod.apportion(0.0, [1.0, 1.0], 3), [0.0, 0.0])


class TestSplitConversion(IntegrationTestCase):
	"""Per-lane target and alloy must sum EXACTLY to the document totals."""

	@classmethod
	def setUpClass(cls):
		pass

	def _lanes(self, *source_qtys):
		return [
			{
				"inventory_type": "Regular Stock" if i == 0 else "Customer Goods",
				"customer": None if i == 0 else f"CUST-{i}",
				"source_qty": qty,
				"batches": [],
			}
			for i, qty in enumerate(source_qtys)
		]

	def test_the_20g_requirement(self):
		lanes = lanes_mod.split_conversion(self._lanes(8.0, 12.0), 26.667, 3)
		self.assertEqual(lanes[0]["target_qty"], 10.667)
		self.assertEqual(lanes[1]["target_qty"], 16.0)
		# alloy = target - source
		self.assertEqual(lanes[0]["alloy_qty"], 2.667)
		self.assertEqual(lanes[1]["alloy_qty"], 4.0)
		self.assertAlmostEqual(
			sum(lane["alloy_qty"] for lane in lanes), 26.667 - 20.0, places=9
		)

	def test_alloy_sums_to_total_even_with_awkward_rounding(self):
		lanes = lanes_mod.split_conversion(self._lanes(1.0, 1.0, 1.0), 10.0, 3)
		self.assertAlmostEqual(
			sum(lane["target_qty"] for lane in lanes), 10.0, places=9
		)
		self.assertAlmostEqual(sum(lane["alloy_qty"] for lane in lanes), 7.0, places=9)

	def test_tiny_lane_may_round_to_zero_alloy(self):
		"""Near-equal purities give a wide zero-alloy window for a small lane.

		91.6 -> 91.5 is a factor of ~0.00109, so a 0.2 g residual lane's alloy
		rounds to 0.000 while the whole lot's does not. The lane must simply get no
		alloy -- not a phantom row, and not a sign flip.
		"""
		total_source = 100.2
		total_target = round(total_source * 91.6 / 91.5, 3)
		lanes = lanes_mod.split_conversion(self._lanes(100.0, 0.2), total_target, 3)

		self.assertEqual(lanes[1]["alloy_qty"], 0.0)
		self.assertGreater(lanes[0]["alloy_qty"], 0.0)
		self.assertAlmostEqual(
			sum(lane["alloy_qty"] for lane in lanes),
			total_target - total_source,
			places=9,
		)

	def test_alloy_sign_is_uniform_across_lanes(self):
		"""Source and target purity are shared, so no lane can be opposite."""
		# Purity up: target < source -> every lane's alloy is <= 0.
		lanes = lanes_mod.split_conversion(self._lanes(8.0, 12.0), 15.0, 3)
		self.assertTrue(all(lane["alloy_qty"] <= 0 for lane in lanes))
		# Purity down: target > source -> every lane's alloy is >= 0.
		lanes = lanes_mod.split_conversion(self._lanes(8.0, 12.0), 26.667, 3)
		self.assertTrue(all(lane["alloy_qty"] >= 0 for lane in lanes))

	def test_single_lane_takes_the_whole_target(self):
		lanes = lanes_mod.split_conversion(self._lanes(20.0), 26.667, 3)
		self.assertEqual(lanes[0]["target_qty"], 26.667)


class TestSplitAllocations(IntegrationTestCase):
	"""The single alloy FIFO pool is handed out per lane so it stays attributable."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_splits_one_batch_across_two_lanes(self):
		result = lanes_mod.split_allocations([_alloc(6.667, "AL1")], [2.667, 4.0], 3)
		self.assertEqual(result[0], [{"batch": "AL1", "qty": 2.667}])
		self.assertEqual(result[1], [{"batch": "AL1", "qty": 4.0}])

	def test_walks_on_to_the_next_batch(self):
		result = lanes_mod.split_allocations(
			[_alloc(3.0, "AL1"), _alloc(5.0, "AL2")], [4.0, 4.0], 3
		)
		self.assertEqual(
			result[0], [{"batch": "AL1", "qty": 3.0}, {"batch": "AL2", "qty": 1.0}]
		)
		self.assertEqual(result[1], [{"batch": "AL2", "qty": 4.0}])

	def test_zero_need_gets_nothing_and_does_not_consume(self):
		result = lanes_mod.split_allocations([_alloc(5.0, "AL1")], [0.0, 5.0], 3)
		self.assertEqual(result[0], [])
		self.assertEqual(result[1], [{"batch": "AL1", "qty": 5.0}])

	def test_total_handed_out_never_exceeds_the_pool(self):
		result = lanes_mod.split_allocations([_alloc(2.0, "AL1")], [3.0, 3.0], 3)
		handed = sum(r["qty"] for rows in result for r in rows)
		self.assertLessEqual(handed, 2.0)


class _FakeMCDoc:
	"""Stand-in Metal Conversions doc for the Stock Entry builder."""

	def __init__(self, **fields):
		self.source_batch_details = []
		self.alloy_batch_details = []
		for key, value in fields.items():
			setattr(self, key, value)

	def get(self, key, default=None):
		return getattr(self, key, default)

	def precision(self, fieldname):
		return 3

	def append(self, table, row):
		row = frappe._dict(row)
		getattr(self, table).append(row)
		return row

	def db_set(self, fieldname, value, *args, **kwargs):
		setattr(self, fieldname, value)


class _FakeSE:
	"""Captures the Stock Entry payload the builder constructs."""

	def __init__(self, payload):
		self.payload = payload if isinstance(payload, dict) else {}
		self.items = []
		self.name = "SE-CONV-0001"
		self.saved = False
		self.submitted = False

	def append(self, table, row):
		self.items.append(frappe._dict(row))

	def save(self):
		self.saved = True

	def submit(self):
		self.submitted = True


class _BuilderCase(IntegrationTestCase):
	"""Shared harness for the Stock Entry builder: a fake document, a captured voucher."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		if not hasattr(frappe, "db") or not frappe.db:
			frappe.db = MagicMock()
		self._patches = [patch.object(mc, "flt", _det_flt)]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()

	def _doc(self, **fields):
		defaults = {
			"name": "mc0001",
			"company": "GK",
			"branch": "Main",
			"department": "Casting - GK",
			"manufacturer": "Shubh",
			"employee": "EMP-0001",
			"source_warehouse": "Casting RM - GK",
			"target_warehouse": "Casting RM - GK",
			"source_item": "M-G-24KT-99.9-Y",
			"source_qty": 20.0,
			"target_item": "M-G-18KT-75.0-Y",
			"target_qty": 26.667,
			"source_alloy": None,
			"source_alloy_qty": 0,
			"target_alloy": None,
			"target_alloy_qty": 0,
			"stock_entry": None,
		}
		defaults.update(fields)
		return _FakeMCDoc(**defaults)

	def _two_lane_doc(self, **overrides):
		"""8 g Regular + 12 g Customer Goods -> 10.667 / 16.0 at 100 -> 75 purity.

		Nothing about the lanes is pre-seeded: the builder derives them from
		``source_batch_details`` and the batch ownership map, which is the whole point
		of not storing them.
		"""
		return self._doc(
			source_batch_details=[_alloc(8.0, "REG"), _alloc(12.0, "CG")], **overrides
		)

	def _build(self, doc, lane_map=None, company_component_qty=0.0):
		"""Build the Stock Entry a Metal Conversion would make.

		``company_component_qty`` is the C09 carve-out input: how many grams of the lane's
		source batches are RECORDED (in ``Batch Component``) as company-owned. It is patched
		rather than left to the real reader for two reasons, and the default is what matters
		most:

		* **Default 0.0 reproduces today's behaviour exactly**, so every test written before
		  C09 asserts the same thing it always did. That is not a coincidence to be relied on
		  -- it is the carve-out's own contract: nothing recorded, nothing carved out.
		* **It makes these tests deterministic.** Without the patch they would depend on
		  ``_recorded_components`` failing closed against a site that happens not to have the
		  ``Batch Component`` DocType. That passes today and would start failing the moment
		  the doctype is migrated onto the test site -- a test that breaks when unrelated
		  schema arrives is a trap, not coverage.
		"""
		lane_map = lane_map or {
			"REG": ("Regular Stock", None),
			"CG": ("Customer Goods", "TNCU0001"),
			"AL1": ("Regular Stock", None),
		}
		captured = {}

		# The fakes answer ONLY the Stock Entry the builder makes and hand every other read to
		# the real functions. ``flt(x, precision)`` reads the rounding method through System
		# Settings; a fake that answered that read too made ``flt`` swallow the error and
		# return 0, so every lane target read 0.0 -- but only while the cache was cold, which
		# made the builder tests pass or fail by run order.
		def _get_doc(*args, **kwargs):
			payload = args[0] if args else kwargs
			if isinstance(payload, dict) and payload.get("doctype") == "Stock Entry":
				se = _FakeSE(payload)
				captured["se"] = se
				return se
			return _REAL_GET_DOC(*args, **kwargs)

		def _new_doc(doctype, *args, **kwargs):
			if doctype == "Stock Entry":
				return _FakeSE({})
			return _REAL_NEW_DOC(doctype, *args, **kwargs)

		with (
			patch("frappe.get_doc", side_effect=_get_doc),
			patch("frappe.new_doc", side_effect=_new_doc),
			patch.object(mc, "get_batch_lane_map", return_value=lane_map),
			patch.object(
				mc, "get_company_component_qty", return_value=company_component_qty
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.lock_order.preallocate_series_for_docs"
			),
			patch("jewellery_erpnext.jewellery_erpnext.lock_order.lock_bins"),
		):
			mc.make_metal_stock_entry(doc)

		return captured["se"]


class TestMakeMetalStockEntry(_BuilderCase):
	"""One voucher, rows grouped lane by lane, every row carrying its lane tag."""

	def test_one_voucher_with_source_target_source_target(self):
		se = self._build(self._two_lane_doc())

		self.assertEqual(len(se.items), 4)
		shape = [
			("source" if row.get("s_warehouse") else "target", row.inventory_type)
			for row in se.items
		]
		self.assertEqual(
			shape,
			[
				("source", "Regular Stock"),
				("target", "Regular Stock"),
				("source", "Customer Goods"),
				("target", "Customer Goods"),
			],
		)

	def test_each_row_carries_its_own_ownership_and_lane_tag(self):
		se = self._build(self._two_lane_doc())

		regular_source, regular_target, cg_source, cg_target = se.items
		self.assertIsNone(regular_source.customer)
		self.assertIsNone(regular_target.customer)
		self.assertEqual(cg_source.customer, "TNCU0001")
		self.assertEqual(cg_target.customer, "TNCU0001")

		self.assertEqual(regular_source.custom_conversion_lane, "Regular Stock|")
		self.assertEqual(regular_target.custom_conversion_lane, "Regular Stock|")
		self.assertEqual(cg_source.custom_conversion_lane, "Customer Goods|TNCU0001|CG")
		self.assertEqual(cg_target.custom_conversion_lane, "Customer Goods|TNCU0001|CG")

	def test_target_qtys_are_per_lane_and_sum_to_the_document(self):
		se = self._build(self._two_lane_doc())
		targets = [row.qty for row in se.items if row.get("t_warehouse")]
		self.assertEqual(targets, [10.667, 16.0])
		self.assertAlmostEqual(sum(targets), 26.667, places=9)

	def test_header_carries_the_customer_but_no_single_inventory_type(self):
		se = self._build(self._two_lane_doc())
		# _customer opens create_child_batches' gate; that function is now row-aware,
		# so the Regular lane is not minted as the customer's.
		self.assertEqual(se.payload["_customer"], "TNCU0001")
		self.assertIsNone(se.payload["inventory_type"])
		self.assertEqual(se.payload["stock_entry_type"], "Repack-Metal Conversion")
		self.assertEqual(se.payload["custom_metal_conversion_reference"], "mc0001")

	def test_all_regular_draw_leaves_the_header_customer_blank(self):
		doc = self._doc(source_batch_details=[_alloc(20.0, "REG")])
		doc.conversion_lanes = [
			frappe._dict(
				inventory_type="Regular Stock",
				customer=None,
				source_qty=20.0,
				target_qty=26.667,
				alloy_qty=6.667,
			)
		]
		se = self._build(doc)

		# No customer lane -> create_child_batches must be skipped entirely.
		self.assertIsNone(se.payload["_customer"])
		self.assertEqual(se.payload["inventory_type"], "Regular Stock")

	def test_source_alloy_is_split_across_lanes_and_stays_regular_stock(self):
		doc = self._two_lane_doc(source_alloy="alloy", source_alloy_qty=6.667)
		doc.alloy_batch_details = [_alloc(6.667, "AL1")]
		se = self._build(doc)

		alloy_rows = [row for row in se.items if row.item_code == "alloy"]
		self.assertEqual(len(alloy_rows), 2)
		# Alloy IS company stock being consumed, so it stays Regular Stock...
		self.assertTrue(all(r.inventory_type == "Regular Stock" for r in alloy_rows))
		self.assertTrue(all(not r.get("customer") for r in alloy_rows))
		# ...but it is tagged to the lane it funds, which is what makes its Batch Rate
		# contribution attributable to the right target batch.
		self.assertEqual(
			[r.custom_conversion_lane for r in alloy_rows],
			["Regular Stock|", "Customer Goods|TNCU0001|CG"],
		)
		self.assertAlmostEqual(sum(r.qty for r in alloy_rows), 6.667, places=9)

	def test_target_alloy_belongs_to_its_lane_including_the_customer(self):
		doc = self._two_lane_doc(
			target_qty=15.0, target_alloy="talloy", target_alloy_qty=5.0
		)
		se = self._build(doc)

		alloy_rows = [row for row in se.items if row.item_code == "talloy"]
		self.assertEqual(len(alloy_rows), 2)
		self.assertTrue(all(r.get("t_warehouse") for r in alloy_rows))
		# Alloy freed by raising the purity of a customer's metal is the customer's.
		self.assertEqual(alloy_rows[0].inventory_type, "Regular Stock")
		self.assertIsNone(alloy_rows[0].customer)
		self.assertEqual(alloy_rows[1].inventory_type, "Customer Goods")
		self.assertEqual(alloy_rows[1].customer, "TNCU0001")
		self.assertAlmostEqual(sum(r.qty for r in alloy_rows), 5.0, places=9)

	# ------------------------------------------------------- C09 released-alloy carve-out
	def test_released_alloy_is_carved_out_by_recorded_company_component(self):
		"""C09. Alloy freed by raising purity is not automatically the customer's.

		The defect: when company alloy was blended into a customer lane by an EARLIER
		conversion, raising the purity again frees some of that same company alloy. Handing
		all of it back tagged to the customer turns company stock into customer stock with no
		transaction and no counterparty -- a silent transfer of value.

		The document releases 5.0 g in total, and that is apportioned across lanes by source
		share BEFORE this code sees it -- 8 g / 20 g Regular and 12 g / 20 g Customer, so the
		customer lane's release is 3.0 g, not 5.0 g. Of that 3.0 g, 2.0 g is recorded as
		company metal, so the customer keeps 1.0 g and 2.0 g goes back as Regular Stock.
		"""
		doc = self._two_lane_doc(
			target_qty=15.0, target_alloy="talloy", target_alloy_qty=5.0
		)
		se = self._build(doc, company_component_qty=2.0)

		alloy_rows = [row for row in se.items if row.item_code == "talloy"]
		# Regular lane: 1 row (no carve-out -- it is already company stock).
		# Customer lane: 2 rows -- the customer's share and the carved-out company share.
		self.assertEqual(len(alloy_rows), 3)

		customer_rows = [r for r in alloy_rows if r.get("customer")]
		self.assertEqual(len(customer_rows), 1)
		self.assertAlmostEqual(customer_rows[0].qty, 1.0, places=9)
		self.assertEqual(customer_rows[0].inventory_type, "Customer Goods")

		carved = [
			r
			for r in alloy_rows
			if not r.get("customer")
			and r.custom_conversion_lane == "Customer Goods|TNCU0001|CG"
		]
		self.assertEqual(len(carved), 1)
		self.assertAlmostEqual(carved[0].qty, 2.0, places=9)
		self.assertEqual(carved[0].inventory_type, "Regular Stock")

		# Nothing is created or destroyed by the split.
		self.assertAlmostEqual(sum(r.qty for r in alloy_rows), 5.0, places=9)

	def test_the_carved_out_row_keeps_the_lane_tag(self):
		"""Ownership changes; lane attribution does not.

		The lane tag is what makes a row's Batch Rate contribution attributable to the right
		target batch. The carved-out alloy still funded THIS lane, so it keeps the tag -- only
		``inventory_type``/``customer`` differ. Dropping the tag would misattribute its rate.
		"""
		doc = self._two_lane_doc(
			target_qty=15.0, target_alloy="talloy", target_alloy_qty=5.0
		)
		se = self._build(doc, company_component_qty=2.0)

		carved = next(
			r
			for r in se.items
			if r.item_code == "talloy"
			and not r.get("customer")
			and r.custom_conversion_lane == "Customer Goods|TNCU0001|CG"
		)
		self.assertEqual(carved.custom_conversion_lane, "Customer Goods|TNCU0001|CG")

	def test_carve_out_never_exceeds_what_was_released(self):
		"""A recorded company component larger than the release must not invent alloy.

		The customer lane's release is 3.0 g (12/20 of the document's 5.0 g). Without the
		``min()`` this would emit a 9 g company row and a -6 g customer row: stock from
		nowhere, and a negative quantity that erpnext would reject far downstream with a
		message naming neither C09 nor this code.
		"""
		doc = self._two_lane_doc(
			target_qty=15.0, target_alloy="talloy", target_alloy_qty=5.0
		)
		se = self._build(doc, company_component_qty=9.0)

		alloy_rows = [row for row in se.items if row.item_code == "talloy"]
		self.assertTrue(
			all(r.qty > 0 for r in alloy_rows), "a non-positive row was emitted"
		)
		self.assertAlmostEqual(sum(r.qty for r in alloy_rows), 5.0, places=9)

		# The whole release is company metal, so the customer gets no row at all.
		self.assertEqual([r for r in alloy_rows if r.get("customer")], [])

	def test_no_recorded_components_means_no_carve_out(self):
		"""The contract that makes this safe to ship: a site with no component history keeps
		byte-identical behaviour. This is the same assertion as
		``test_target_alloy_belongs_to_its_lane_including_the_customer``, stated from the
		carve-out's side so the guarantee is pinned even if that test is ever changed."""
		doc = self._two_lane_doc(
			target_qty=15.0, target_alloy="talloy", target_alloy_qty=5.0
		)
		se = self._build(doc, company_component_qty=0.0)

		alloy_rows = [row for row in se.items if row.item_code == "talloy"]
		self.assertEqual(len(alloy_rows), 2)
		self.assertEqual(alloy_rows[1].inventory_type, "Customer Goods")
		self.assertEqual(alloy_rows[1].customer, "TNCU0001")
		# 12/20 of the document's 5.0 g release -- the lane apportionment, untouched.
		self.assertAlmostEqual(alloy_rows[1].qty, 3.0, places=9)
		self.assertAlmostEqual(sum(r.qty for r in alloy_rows), 5.0, places=9)

	def test_a_regular_lane_is_never_carved_out(self):
		"""There is nothing to carve out of company stock, and doing so would split one
		Regular Stock row into two identical ones for no reason."""
		doc = self._doc(
			source_batch_details=[_alloc(20.0, "REG")],
			target_qty=15.0,
			target_alloy="talloy",
			target_alloy_qty=5.0,
		)
		doc.conversion_lanes = [
			frappe._dict(
				inventory_type="Regular Stock",
				customer=None,
				source_qty=20.0,
				target_qty=26.667,
				alloy_qty=6.667,
			)
		]
		se = self._build(doc, company_component_qty=2.0)

		alloy_rows = [row for row in se.items if row.item_code == "talloy"]
		self.assertEqual(len(alloy_rows), 1)
		self.assertAlmostEqual(alloy_rows[0].qty, 5.0, places=9)

	def test_single_lane_voucher_is_shaped_exactly_as_before(self):
		"""Regression: an unmixed conversion must be unchanged by the lane work."""
		doc = self._doc(source_batch_details=[_alloc(20.0, "CG")])
		se = self._build(doc, lane_map={"CG": ("Customer Goods", "TNCU0001")})

		self.assertEqual(len(se.items), 2)
		self.assertEqual(se.payload["inventory_type"], "Customer Goods")
		self.assertEqual(se.payload["_customer"], "TNCU0001")
		self.assertEqual(se.items[0].qty, 20.0)
		self.assertEqual(se.items[1].qty, 26.667)
		self.assertTrue(all(r.customer == "TNCU0001" for r in se.items))

	def test_one_lane_per_customer_batch_not_per_customer(self):
		"""Two batches of the same customer are TWO lanes, hence two target rows.

		Until MCON00332 this pinned the opposite: 12 g of one customer's metal across two
		batches made one 16 g target, named after the first batch only.
		"""
		doc = self._doc(
			source_batch_details=[
				_alloc(6.0, "CG"),
				_alloc(8.0, "REG"),
				_alloc(6.0, "CG2"),
			]
		)
		se = self._build(
			doc,
			lane_map={
				"CG": ("Customer Goods", "TNCU0001"),
				"REG": ("Regular Stock", None),
				"CG2": ("Customer Goods", "TNCU0001"),
			},
		)

		targets = [r for r in se.items if r.get("t_warehouse")]
		self.assertEqual(
			[(r.customer, r.qty, r.custom_conversion_lane) for r in targets],
			[
				("TNCU0001", 8.0, "Customer Goods|TNCU0001|CG"),
				(None, 10.667, "Regular Stock|"),
				("TNCU0001", 8.0, "Customer Goods|TNCU0001|CG2"),
			],
		)

	def test_throws_when_nothing_is_allocated(self):
		doc = self._doc(source_batch_details=[])
		with self.assertRaisesRegex(ValidationError, "No source batches are allocated"):
			self._build(doc)

	def test_throws_when_lane_targets_do_not_sum_to_the_document_target(self):
		"""Replaces the old "inventory types are not consistent" guard.

		Mixed ownership is now the point; what must still hold is that apportioning
		the target across lanes neither invents nor drops metal. Here the allocation
		covers 20 g but Target Qty claims a figure the purity ratio cannot produce.
		"""
		doc = self._two_lane_doc(target_qty=26.667)
		with patch.object(mc, "split_conversion", side_effect=_shrink_last_lane):
			with self.assertRaisesRegex(ValidationError, "does not match"):
				self._build(doc)


class TestEachCustomerBatchConvertsOnItsOwn(_BuilderCase):
	"""MCON00332 (kg-gk, 29 Sep 2026): two batches of ONE customer must not share a target.

	Single-converter mode drew, FIFO: customer batch 11 (0.477 g), a Regular batch (0.850 g),
	customer batch 12 (8.673 g) -- same customer -- plus 0.899 g of company alloy, into 22KT
	(10 g x 100 / 91.75 = 10.899182561 g). MAT-STE-19453 booked ONE customer target
	(``...-11-B``, 9.973 g) for both batches, so batch 12's metal sat in a batch named after
	11 at a blended rate. Each customer batch now converts on its own: its own target row,
	its own share of the alloy, its own lane tag.

	Every expected figure below is written out by hand (Decimal arithmetic in the evidence
	report), never computed with the helpers under test: 0.477 x 100 / 91.75 = 0.519891 ->
	0.520 g (alloy 0.043); 0.850 -> 0.926430 -> 0.926 g (0.076); 8.673 -> 9.452861 ->
	9.453 g (0.780). 0.520 + 0.926 + 9.453 = 10.899 and 0.043 + 0.076 + 0.780 = 0.899.
	"""

	LANE_MAP = {
		"C-11": ("Customer Goods", "CUST-1"),
		"REG": ("Regular Stock", None),
		"C-12": ("Customer Goods", "CUST-1"),
		"AL-04": ("Regular Stock", None),
	}

	def _mcon00332(self, order=("C-11", "REG", "C-12")):
		qty = {"C-11": 0.477, "REG": 0.85, "C-12": 8.673}
		return self._doc(
			source_item="M-G-24KT-99.9-Y",
			source_qty=10.0,
			target_item="M-G-22KT-91.75-Y",
			target_qty=10.899182561,
			source_alloy="M-Genia-221",
			# A Data field: the real document stores the string "0.899".
			source_alloy_qty="0.899",
			source_batch_details=[_alloc(qty[batch], batch) for batch in order],
			alloy_batch_details=[_alloc(0.899, "AL-04")],
		)

	@staticmethod
	def _groups(se):
		"""``{lane tag: {"source": [(batch, qty)], "alloy": [...], "target": [...]}}``."""
		groups = {}
		for row in se.items:
			group = groups.setdefault(
				row.custom_conversion_lane, {"source": [], "alloy": [], "target": []}
			)
			if row.get("t_warehouse"):
				group["target"].append((row.customer, row.qty))
			elif row.item_code == "M-Genia-221":
				group["alloy"].append((row.batch_no, row.qty))
			else:
				group["source"].append((row.batch_no, row.qty))
		return groups

	def test_two_batches_of_one_customer_make_two_targets(self):
		se = self._build(self._mcon00332(), lane_map=self.LANE_MAP)

		customer_targets = [
			row for row in se.items if row.get("t_warehouse") and row.customer
		]
		# Grouping by inventory type, or by customer without the batch, gives ONE here.
		self.assertEqual(len(customer_targets), 2)
		self.assertEqual(
			sorted(row.qty for row in customer_targets), [0.52, 9.453], customer_targets
		)
		self.assertTrue(all(row.customer == "CUST-1" for row in customer_targets))

	def test_each_target_is_its_own_source_plus_its_own_alloy(self):
		groups = self._groups(self._build(self._mcon00332(), lane_map=self.LANE_MAP))

		self.assertEqual(
			groups["Customer Goods|CUST-1|C-11"],
			{
				"source": [("C-11", 0.477)],
				"alloy": [("AL-04", 0.043)],
				"target": [("CUST-1", 0.52)],
			},
		)
		self.assertEqual(
			groups["Customer Goods|CUST-1|C-12"],
			{
				"source": [("C-12", 8.673)],
				"alloy": [("AL-04", 0.78)],
				"target": [("CUST-1", 9.453)],
			},
		)
		# Regular Stock still pools (a single Regular batch here, so one lane either way).
		self.assertEqual(
			groups["Regular Stock|"],
			{
				"source": [("REG", 0.85)],
				"alloy": [("AL-04", 0.076)],
				"target": [(None, 0.926)],
			},
		)

	def test_the_alloy_is_consumed_once_in_total(self):
		se = self._build(self._mcon00332(), lane_map=self.LANE_MAP)
		alloy = [row.qty for row in se.items if row.item_code == "M-Genia-221"]
		# Handing the whole 0.899 g to every group would read 2.697 here.
		self.assertAlmostEqual(sum(alloy), 0.899, places=9)
		self.assertTrue(
			all(
				row.inventory_type == "Regular Stock"
				for row in se.items
				if row.item_code == "M-Genia-221"
			)
		)

	def test_targets_still_sum_to_the_document_at_posting_precision(self):
		se = self._build(self._mcon00332(), lane_map=self.LANE_MAP)
		targets = [row.qty for row in se.items if row.get("t_warehouse")]
		self.assertAlmostEqual(sum(targets), 10.899, places=9)

	def test_each_group_is_contiguous_so_it_is_valued_on_its_own(self):
		"""The lane pricer values ROW-ORDER runs (sources, then produce). A group whose rows
		were interleaved with another's would be priced with the other's metal."""
		se = self._build(self._mcon00332(), lane_map=self.LANE_MAP)
		shape = [
			(row.custom_conversion_lane, "out" if row.get("s_warehouse") else "in")
			for row in se.items
		]
		self.assertEqual(
			shape,
			[
				("Customer Goods|CUST-1|C-11", "out"),
				("Customer Goods|CUST-1|C-11", "out"),
				("Customer Goods|CUST-1|C-11", "in"),
				("Regular Stock|", "out"),
				("Regular Stock|", "out"),
				("Regular Stock|", "in"),
				("Customer Goods|CUST-1|C-12", "out"),
				("Customer Goods|CUST-1|C-12", "out"),
				("Customer Goods|CUST-1|C-12", "in"),
			],
		)

	def test_row_order_does_not_decide_owner_or_quantity(self):
		"""T05: the same batches in another FIFO order map to the same results."""
		expected = {
			"Customer Goods|CUST-1|C-11": ("CUST-1", 0.52),
			"Customer Goods|CUST-1|C-12": ("CUST-1", 9.453),
			"Regular Stock|": (None, 0.926),
		}
		for order in (("REG", "C-12", "C-11"), ("C-12", "C-11", "REG")):
			with self.subTest(order=order):
				groups = self._groups(
					self._build(self._mcon00332(order=order), lane_map=self.LANE_MAP)
				)
				self.assertEqual(
					{tag: group["target"][0] for tag, group in groups.items()}, expected
				)

	def test_a_single_customer_voucher_keeps_its_header_ownership(self):
		"""Two batches, one customer and nothing else: the voucher still has ONE owner."""
		doc = self._mcon00332(order=("C-11", "C-12"))
		doc.source_qty = 9.15
		doc.target_qty = 9.972752044
		doc.source_alloy_qty = "0.823"
		doc.alloy_batch_details = [_alloc(0.823, "AL-04")]
		se = self._build(doc, lane_map=self.LANE_MAP)

		self.assertEqual(se.payload["inventory_type"], "Customer Goods")
		self.assertEqual(se.payload["_customer"], "CUST-1")
		self.assertEqual(
			[row.qty for row in se.items if row.get("t_warehouse")], [0.52, 9.453]
		)

	def test_mcon00333_two_batches_make_1_446_and_20_352(self):
		"""MCON00333 (kg-gk, 29 Sep 2026): 20 g of one customer's 24KT from two batches.

		FIFO drew the whole of batch -12 (1.327 g) and 18.673 g of batch -13, plus 1.798 g of
		alloy, into 20 x 100 / 91.75 = 21.798365123 g of 22KT; MAT-STE-19749 booked it as ONE
		21.798 g target. Per batch, the stored alloy is shared by source: 1.798 x 1.327 / 20 =
		0.1193 -> 0.119 g, and 1.798 x 18.673 / 20 = 1.6787 -> 1.679 g (0.119 + 1.679 = 1.798, no
		residual). So -12 makes 1.327 + 0.119 = 1.446 g and -13 makes 18.673 + 1.679 = 20.352 g,
		21.798 g in all -- the same metal, now in the batch it came from.
		"""
		doc = self._doc(
			source_item="M-G-24KT-99.9-Y",
			source_qty=20.0,
			target_item="M-G-22KT-91.75-Y",
			target_qty=21.798365123,
			source_alloy="M-Genia-221",
			source_alloy_qty="1.798",
			source_batch_details=[_alloc(1.327, "C-12"), _alloc(18.673, "C-13")],
			alloy_batch_details=[_alloc(1.798, "AL-04")],
		)
		se = self._build(
			doc,
			lane_map={
				"C-12": ("Customer Goods", "GJCU0009"),
				"C-13": ("Customer Goods", "GJCU0009"),
				"AL-04": ("Regular Stock", None),
			},
		)

		self.assertEqual(
			self._groups(se),
			{
				"Customer Goods|GJCU0009|C-12": {
					"source": [("C-12", 1.327)],
					"alloy": [("AL-04", 0.119)],
					"target": [("GJCU0009", 1.446)],
				},
				"Customer Goods|GJCU0009|C-13": {
					"source": [("C-13", 18.673)],
					"alloy": [("AL-04", 1.679)],
					"target": [("GJCU0009", 20.352)],
				},
			},
		)
		# The alloy is the company's in both lanes; only the lane tag says whose metal it joined.
		self.assertEqual(
			[
				(row.inventory_type, row.get("customer"))
				for row in se.items
				if row.item_code == "M-Genia-221"
			],
			[("Regular Stock", None), ("Regular Stock", None)],
		)
		self.assertAlmostEqual(
			sum(row.qty for row in se.items if row.get("t_warehouse")), 21.798, places=9
		)
		# One owner across both lanes, so the voucher still names it.
		self.assertEqual(se.payload["inventory_type"], "Customer Goods")
		self.assertEqual(se.payload["_customer"], "GJCU0009")


# ------------------------------------------------------------------------------------------
# lane_tag, the single-mode guards, the per-lane balance property, validate_target_qty, the
# cancel cascade and the Multiple Metal Converter's customer path. Every expected figure is
# hand arithmetic written out beside it -- none is computed with the helper under test.
# ------------------------------------------------------------------------------------------

#: Purity masters for these tests, by item code. "M-G-24KT-99.9-Y" is 100.0 because that is
#: what production's Attribute Value master holds for "99.9" -- the MCON00332 figures above
#: are 10 g x 100 / 91.75 for the same reason.
_PURITY = {
	"M-G-24KT-99.9-Y": 100.0,
	"M-G-22KT-91.75-Y": 91.75,
	"M-G-22KT-91.6-Y": 91.6,
	"M-G-18KT-75.4-Y": 75.4,
	"M-G-18KT-75.0-Y": 75.0,
}

#: The real purity reader, saved before any test patches it (see ``_purity_of``).
_REAL_PURITY = mc.get_metal_purity_percentage

_ALLOY = "M-Genia-221"
_SOURCE_WH = "Casting RM - GK"
_TARGET_WH = "Casting FG - GK"
#: What every row copies from the document (``_BuilderCase._doc``'s defaults).
_COMMON = {
	"department": "Casting - GK",
	"employee": "EMP-0001",
	"manufacturer": "Shubh",
}


def _purity_of(purities):
	"""A ``get_metal_purity_percentage`` that answers ONLY the items in ``purities``.

	Any other item goes to the real reader (the item's Metal Purity attribute), so a builder
	asking about an item a test did not declare fails loudly instead of getting a made-up
	purity.
	"""

	def _purity(item_code):
		if item_code in purities:
			return purities[item_code]
		return _REAL_PURITY(item_code)

	return _purity


def _mg(qty):
	"""A quantity in whole milligrams, so balances compare exactly rather than as floats."""
	return round(float(qty) * 1000)


class TestLaneTag(IntegrationTestCase):
	"""lane_tag: what each row of a conversion voucher carries in custom_conversion_lane."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_a_customer_lane_names_its_batch(self):
		"""T61. A customer lane's tag names its batch, so one customer's two batches differ.

		Detects R2 (grouping by customer without the batch): both of GJCU0009's batches would
		read "Customer Goods|GJCU0009" and share one target downstream.
		"""
		self.assertEqual(
			mc.lane_tag("Customer Goods", "GJCU0009", "GJCU0009-11"),
			"Customer Goods|GJCU0009|GJCU0009-11",
		)
		self.assertEqual(
			mc.lane_tag("Customer Goods", "GJCU0009", "GJCU0009-12"),
			"Customer Goods|GJCU0009|GJCU0009-12",
		)
		self.assertEqual(
			mc.lane_tag("Customer Stock", "GJCU0009", "GJCU0009-13"),
			"Customer Stock|GJCU0009|GJCU0009-13",
		)

	def test_pooled_company_stock_keeps_the_old_tag(self):
		"""T61. Company stock still pools under "<inventory type>|", whichever batch it is.

		The builders hand lane_tag the lane's batch, which lane_key has already dropped for
		company stock, so the composition is pinned as well as lane_tag on its own. Detects a
		Regular Stock lane per batch -- the pooled format changing under every reader.
		"""
		self.assertEqual(mc.lane_tag("Regular Stock", None), "Regular Stock|")
		self.assertEqual(mc.lane_tag(None, None), "Regular Stock|")
		self.assertEqual(mc.lane_tag("Regular Stock", None, None), "Regular Stock|")
		for batch in ("RB-1", "RB-2"):
			self.assertEqual(
				mc.lane_tag(*lanes_mod.lane_key("Regular Stock", None, batch)),
				"Regular Stock|",
			)
		# An untyped batch is company stock too.
		self.assertEqual(
			mc.lane_tag(*lanes_mod.lane_key(None, None, "UNTYPED-1")), "Regular Stock|"
		)
		# ...while a customer batch survives lane_key into its tag.
		self.assertEqual(
			mc.lane_tag(
				*lanes_mod.lane_key("Customer Goods", "GJCU0009", "GJCU0009-11")
			),
			"Customer Goods|GJCU0009|GJCU0009-11",
		)

	def test_a_tag_that_fits_the_field_keeps_the_batch_verbatim(self):
		"""T61. "Customer Goods|GJCU0009|" is 24 characters; with a 116-character batch the tag
		is exactly 140 -- the Data field's length -- and still names the batch verbatim."""
		batch = "X" * 116
		tag = mc.lane_tag("Customer Goods", "GJCU0009", batch)
		self.assertEqual(tag, "Customer Goods|GJCU0009|" + batch)
		self.assertEqual(len(tag), 140)

	def test_a_tag_too_long_for_the_field_names_the_batch_by_a_stable_digest(self):
		"""T61. One character more and the batch is named by sha1(batch)[:12] instead.

		"X" * 116 + "A" and "X" * 116 + "B" share their first 140 tag characters, so cutting
		the tag to fit would put both batches in ONE lane downstream (R2). Their digests
		differ, and sha1 -- unlike hash() -- gives the same tag in every process.
		"""
		first = "X" * 116 + "A"
		second = "X" * 116 + "B"

		# sha1(first).hexdigest()[:12] == "57982e03c55f"; 24 + "#" + 12 = 37 characters.
		tag = mc.lane_tag("Customer Goods", "GJCU0009", first)
		self.assertEqual(tag, "Customer Goods|GJCU0009|#57982e03c55f")
		self.assertEqual(len(tag), 37)
		self.assertLessEqual(len(tag), 140)
		# Deterministic: the same batch always gets the same tag.
		self.assertEqual(mc.lane_tag("Customer Goods", "GJCU0009", first), tag)
		# sha1(second).hexdigest()[:12] == "00ac1f793157"
		self.assertEqual(
			mc.lane_tag("Customer Goods", "GJCU0009", second),
			"Customer Goods|GJCU0009|#00ac1f793157",
		)


class TestSingleModeGuards(_BuilderCase):
	"""make_metal_stock_entry refuses a voucher it cannot balance -- and then emits nothing."""

	def _refused(self, doc, pattern, lane_map=None):
		"""Assert the build throws ``pattern`` before a Stock Entry is started or linked.

		``_conversion_header`` is the only source of the Stock Entry payload and
		``_append_lane_rows`` the only writer of its rows, so neither being called means no
		voucher was built at all.
		"""
		with (
			patch.object(
				mc, "_conversion_header", wraps=mc._conversion_header
			) as header,
			patch.object(mc, "_append_lane_rows", wraps=mc._append_lane_rows) as rows,
		):
			with self.assertRaisesRegex(ValidationError, pattern):
				self._build(doc, lane_map=lane_map)
		header.assert_not_called()
		rows.assert_not_called()
		self.assertIsNone(doc.stock_entry)

	def test_a_stored_source_alloy_qty_that_disagrees_is_refused(self):
		"""T43/T55. Source Qty plus the stored Source Alloy Qty must be Target Qty, to a unit.

		8 g Regular + 12 g Customer Goods -> 26.667 g needs 6.667 g of alloy. A stored 6.7 g
		would book 26.7 g of targets -- 33 mg more than the purities make -- so it is refused
		and nothing is emitted. (One unit apart is a rounding tie between the form's round()
		and the site's flt(), not a disagreement: TestRoundingTiesAreAccepted.)
		"""
		doc = self._two_lane_doc(source_alloy=_ALLOY, source_alloy_qty="6.7")
		doc.alloy_batch_details = [_alloc(6.7, "AL1")]
		self._refused(
			doc,
			r"Target Qty 26\.667 does not match the 26\.7 that the source quantity and the "
			r"stored alloy quantity make",
		)

	def test_a_stored_target_alloy_qty_that_disagrees_is_refused(self):
		"""T43/T55. Released alloy is checked the same way: 20 g -> 15.0 g releases 5.0 g, and
		a stored 5.01 g would leave 14.99 g of targets, 10 mg short of Target Qty."""
		doc = self._two_lane_doc(
			target_qty=15.0, target_alloy="talloy", target_alloy_qty=5.01
		)
		self._refused(
			doc,
			r"Target Qty 15\.0 does not match the 14\.99 that the source quantity and the "
			r"stored alloy quantity make",
		)

	def test_an_alloy_pool_short_of_the_need_is_refused(self):
		"""T25. Every lane's alloy must come from an allocated batch.

		The lanes need 2.667 + 4.0 = 6.667 g but only 5.0 g is allocated: split_allocations
		hands out 2.667 and then 2.333, 5.0 in all, and the conversion is refused rather than
		booked 1.667 g short (a lane whose target is not its source plus its alloy).
		"""
		doc = self._two_lane_doc(source_alloy=_ALLOY, source_alloy_qty="6.667")
		doc.alloy_batch_details = [_alloc(5.0, "AL1")]
		self._refused(
			doc,
			r"Alloy M-Genia-221: the allocated batches cover 5\.0 but this conversion "
			r"needs 6\.667",
		)

	def test_a_customer_batch_with_no_customer_is_refused(self):
		"""T11. A customer-owned batch naming no customer has no owner to mint its target for.

		Refused, naming the batch, before anything is emitted. Detects R3:
		create_child_batches would otherwise hand the metal to the voucher's first customer.
		"""
		for inventory_type in ("Customer Goods", "Customer Stock"):
			with self.subTest(inventory_type=inventory_type):
				self._refused(
					self._two_lane_doc(),
					rf"Batch <strong>CG</strong> is {inventory_type} but names no customer",
					lane_map={
						"REG": ("Regular Stock", None),
						"CG": (inventory_type, None),
					},
				)

	def test_two_customers_and_company_stock_make_three_isolated_targets(self):
		"""T02/T04. Customer A, company stock and customer B each get their own target.

		10 g of 24KT (purity 100) into 18KT (75.0): Target Qty 10 x 100 / 75 = 13.333333333
		(stored to 9 dp), Source Alloy Qty 3.333. apportion(13.333333333, [3, 2, 5]):
		3.9999999999 -> 4.0, 2.6666666666 -> 2.667, 6.6666666665 -> 6.667 = 13.334, so the
		-0.001 residual goes to the heaviest lane, B1: 6.666. Alloy = target - source:
		1.0 + 0.667 + 1.666 = 3.333, drawn ONCE from the 3.333 g pool.

		Detects R1 (A and B in one Customer Goods target), R3 (A's owner on every target), R4
		(3.333 g of alloy per lane, 9.999 g in all) and R5 (a rate copied onto the targets).
		"""
		doc = self._doc(
			source_qty=10.0,
			target_qty=13.333333333,
			source_alloy=_ALLOY,
			source_alloy_qty="3.333",
			source_batch_details=[
				_alloc(3.0, "A1"),
				_alloc(2.0, "REG"),
				_alloc(5.0, "B1"),
			],
			alloy_batch_details=[_alloc(3.333, "AL1")],
		)
		se = self._build(
			doc,
			lane_map={
				"A1": ("Customer Goods", "CUST-A"),
				"REG": ("Regular Stock", None),
				"B1": ("Customer Goods", "CUST-B"),
			},
		)

		self.assertEqual(
			[
				(row.inventory_type, row.customer, row.qty, row.custom_conversion_lane)
				for row in se.items
				if row.get("t_warehouse")
			],
			[
				("Customer Goods", "CUST-A", 4.0, "Customer Goods|CUST-A|A1"),
				("Regular Stock", None, 2.667, "Regular Stock|"),
				("Customer Goods", "CUST-B", 6.666, "Customer Goods|CUST-B|B1"),
			],
		)
		self.assertEqual(
			[
				(
					row.custom_conversion_lane,
					row.batch_no,
					row.qty,
					row.inventory_type,
					row.get("customer"),
				)
				for row in se.items
				if row.item_code == _ALLOY
			],
			[
				("Customer Goods|CUST-A|A1", "AL1", 1.0, "Regular Stock", None),
				("Regular Stock|", "AL1", 0.667, "Regular Stock", None),
				("Customer Goods|CUST-B|B1", "AL1", 1.666, "Regular Stock", None),
			],
		)
		# Three ownerships: the header names none; _customer is the first customer lane's.
		self.assertIsNone(se.payload["inventory_type"])
		self.assertEqual(se.payload["_customer"], "CUST-A")
		# Valuation is left to ERPNext and the lane pricer: no row carries a rate.
		for row in se.items:
			self.assertNotIn("basic_rate", row)
			self.assertNotIn("set_basic_rate_manually", row)

	def test_the_link_is_written_with_db_set(self):
		"""T60, R6. The entry is saved and submitted, and its link is written through db_set,
		once, with the entry built.

		on_submit runs after the row is written, so a plain ``self.stock_entry = ...`` was
		never saved and every single-mode conversion lost its link. The fake's db_set also
		sets the attribute, so only the call itself tells the two apart.
		"""
		doc = self._two_lane_doc()
		with patch.object(doc, "db_set", wraps=doc.db_set) as db_set:
			se = self._build(doc)
		self.assertTrue(se.saved)
		self.assertTrue(se.submitted)
		db_set.assert_called_once_with("stock_entry", "SE-CONV-0001")
		self.assertEqual(se.name, "SE-CONV-0001")


class TestPerLaneBalanceProperty(_BuilderCase):
	"""Every lane of every voucher is its own source plus its own alloy -- exactly."""

	SEED = 20260929
	DOCUMENTS = 200
	#: Company stock is drawn twice as often as any one customer's.
	OWNERS = (None, None, "CUST-1", "CUST-2", "CUST-3")
	TARGET_ITEMS = {"91.75": "M-G-22KT-91.75-Y", "75.4": "M-G-18KT-75.4-Y"}

	@classmethod
	def _draw(cls, rng, number):
		"""One document's FIFO rows ``[(milligrams, batch)]``, its lane map, its purities."""
		rows, lane_map = [], {}
		for position in range(rng.randint(2, 6)):
			if rows and rng.random() < 0.1:
				batch = rng.choice(rows)[1]  # the same batch listed twice
			else:
				batch = f"B{number}-{position}"
				customer = rng.choice(cls.OWNERS)
				lane_map[batch] = (
					("Regular Stock", None)
					if customer is None
					else (rng.choice(("Customer Goods", "Customer Stock")), customer)
				)
			rows.append((rng.randint(1, 20000), batch))
		return (
			rows,
			lane_map,
			rng.choice(("100", "99.9")),
			rng.choice(("91.75", "75.4")),
		)

	def test_every_lane_balances_across_generated_documents(self):
		"""T23/T61. 200 seeded documents; for every lane of every voucher, in milligrams:

		* target == the lane's source + the lane's alloy, exactly;
		* one lane per customer batch and one pooled Regular Stock lane (R1, R2);
		* every source and target row of a lane carries its batch's owner (R3);
		and per voucher: targets add up to the sources plus the stored Source Alloy Qty,
		within a milligram of Target Qty; alloy to that stored figure -- consumed once, not
		once per lane (R4) -- and sources to the allocation.

		Each document is what the form stores: 2-6 FIFO rows of 0.001-20.000 g, Regular Stock
		or one of three customers' (so one customer often has two batches), now and then a
		batch listed twice; source purity 100 or 99.9 into 91.75 or 75.4; Target Qty = source
		x Ps / Pt unrounded; Source Alloy Qty = round(target - source, 3), as
		calculate_metal_conversion returns it, held as a string (a Data field); the alloy pool
		is that figure split over 1-3 batches, as update_alloy_betch allocates it.

		A target exactly on a half milligram -- where the form's round() and the site's flt()
		part ways -- is rare in a random draw (none with this seed); TestRoundingTiesAreAccepted
		takes those on directly.
		"""
		rng = random.Random(self.SEED)
		coverage = {
			"one_customer_two_batches": 0,
			"customer_and_company": 0,
			"batch_listed_twice": 0,
		}

		for number in range(self.DOCUMENTS):
			rows, lane_map, source_purity, target_purity = self._draw(rng, number)
			source_mg = sum(qty_mg for qty_mg, _batch in rows)
			source_qty = source_mg / 1000
			# calculate_metal_conversion: the target unrounded, the alloy rounded by round().
			target_qty = (source_qty * float(source_purity)) / float(target_purity)
			stored_alloy = round(float(target_qty - source_qty), 3)
			alloy_mg = _mg(stored_alloy)
			pool, left = [], alloy_mg
			for _cut in range(rng.randint(0, 2)):
				if left > 1:
					take = rng.randint(1, left - 1)
					pool.append(take)
					left -= take
			pool.append(left)

			doc = self._doc(
				source_qty=source_qty,
				target_item=self.TARGET_ITEMS[target_purity],
				target_qty=target_qty,
				source_alloy=_ALLOY,
				source_alloy_qty=str(stored_alloy),
				source_batch_details=[
					_alloc(qty_mg / 1000, batch) for qty_mg, batch in rows
				],
				alloy_batch_details=[
					_alloc(qty_mg / 1000, f"AL{number}-{n}")
					for n, qty_mg in enumerate(pool)
					if qty_mg
				],
			)

			# The lanes this document must produce, straight from its batches' ownership.
			expected = {}
			for _qty_mg, batch in rows:
				inventory_type, customer = lane_map[batch]
				tag = (
					f"{inventory_type}|{customer}|{batch}"
					if customer
					else "Regular Stock|"
				)
				expected[tag] = (inventory_type, customer)
			ownerships = set(expected.values())
			first_customer = next(
				(lane_map[batch][1] for _qty_mg, batch in rows if lane_map[batch][1]),
				None,
			)

			batches = {batch for _qty_mg, batch in rows}
			customers = [lane_map[batch][1] for batch in batches if lane_map[batch][1]]
			coverage["one_customer_two_batches"] += len(customers) != len(
				set(customers)
			)
			coverage["customer_and_company"] += bool(customers) and len(
				customers
			) < len(batches)
			coverage["batch_listed_twice"] += len(batches) < len(rows)

			with self.subTest(
				document=number, rows=rows, purities=(source_purity, target_purity)
			):
				se = self._build(doc, lane_map=lane_map)

				lanes = {}
				for row in se.items:
					lane = lanes.setdefault(
						row.custom_conversion_lane,
						{"source": 0, "alloy": 0, "target": [], "owners": set()},
					)
					if row.get("t_warehouse"):
						lane["target"].append(_mg(row.qty))
						lane["owners"].add((row.inventory_type, row.customer))
					elif row.item_code == _ALLOY:
						lane["alloy"] += _mg(row.qty)
						# Alloy is company stock, whichever lane it funds.
						self.assertEqual(row.inventory_type, "Regular Stock")
						self.assertFalse(row.get("customer"))
					else:
						lane["source"] += _mg(row.qty)
						lane["owners"].add((row.inventory_type, row.customer))

				self.assertEqual(set(lanes), set(expected))
				for tag, lane in lanes.items():
					self.assertEqual(len(lane["target"]), 1, tag)
					self.assertEqual(
						lane["target"][0], lane["source"] + lane["alloy"], tag
					)
					self.assertEqual(lane["owners"], {expected[tag]}, tag)

				booked_mg = sum(lane["target"][0] for lane in lanes.values())
				self.assertEqual(booked_mg, source_mg + alloy_mg)
				self.assertLessEqual(abs(booked_mg - target_qty * 1000), 1 + 1e-6)
				self.assertEqual(
					sum(lane["alloy"] for lane in lanes.values()), alloy_mg
				)
				self.assertEqual(
					sum(lane["source"] for lane in lanes.values()), source_mg
				)
				# The header names an inventory type only for a single ownership.
				self.assertEqual(
					se.payload["inventory_type"],
					next(iter(ownerships))[0] if len(ownerships) == 1 else None,
				)
				self.assertEqual(se.payload["_customer"], first_customer)

		# The draw really exercised the cases the property is about (with this seed: 83
		# documents give one customer two batches, 144 mix customer and company metal, and 55
		# list a batch twice).
		self.assertGreaterEqual(coverage["one_customer_two_batches"], 50, coverage)
		self.assertGreaterEqual(coverage["customer_and_company"], 100, coverage)
		self.assertGreaterEqual(coverage["batch_listed_twice"], 30, coverage)


# Imported here, beside the classes that use it, rather than at the top of the file:
# moving it would change import order for the suites defined above.
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events import (  # noqa: E402
	melting_loss,
	utils,
)

_UTILS_PATH = (
	"jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events.utils"
)


def _det_flt(value, precision=None, rounding_method=None):
	"""Deterministic stand-in for frappe.utils.flt.

	frappe's flt(x, precision) rounds via get_system_settings("rounding_method");
	under the test transaction that lookup can raise and flt's bare except then
	returns 0.0, which is an environment artifact unrelated to the logic under
	test. Patching the module's flt with this shim keeps these pure-logic tests
	deterministic regardless of the site's rounding configuration.
	"""
	try:
		num = float(value or 0)
	except (TypeError, ValueError):
		return 0.0
	return round(num, precision) if precision is not None else num


def _batch_row(qty, batch):
	return SimpleNamespace(qty=qty, batch=batch)


def _doc_mc(**fields):
	"""A stand-in Metal Conversions document."""
	defaults = {
		"name": "mc0001",
		"is_melting_loss": 1,
		"multiple_metal_converter": 0,
		"source_item": "M-G-18KT-75.4-Y",
		"source_qty": 500.0,
		"loss_qty": 20.0,
		"customer": None,
		"company": "GK",
		"branch": "Main",
		"department": "Casting - GK",
		"manufacturer": "Shubh",
		"employee": "EMP-0001",
		"source_warehouse": "Casting RM - GK",
		"inventory_type": "Regular Stock",
		"source_batch_details": [_batch_row(20.0, "B-001")],
		"target_item": "leftover",
		"target_qty": 480.0,
		"source_alloy_check": 1,
		"source_alloy": "alloy",
		"source_alloy_qty": 5,
		"source_alloy_batch": "AB-1",
		"target_alloy_check": 1,
		"target_alloy": "talloy",
		"target_alloy_qty": 3,
		"alloy_batch_details": [_batch_row(5.0, "AB-1")],
	}
	defaults.update(fields)
	d = SimpleNamespace(**defaults)
	# SimpleNamespace has no .get(); melting_loss uses doc.get(...) in a few spots.
	d.get = lambda k, default=None: getattr(d, k, default)
	d.db_set = MagicMock()
	return d


class TestMeltingLossValidation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		if not hasattr(frappe, "db") or not frappe.db:
			frappe.db = MagicMock()

		self._patches = [
			patch.object(melting_loss, "_loss_precision", return_value=3),
			patch.object(melting_loss, "flt", _det_flt),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()

	def test_noop_when_not_melting_loss(self):
		doc = _doc_mc(is_melting_loss=0)
		# Must not touch conversion fields when the flag is off.
		melting_loss.validate_melting_loss(doc)
		self.assertEqual(doc.target_item, "leftover")

	def test_blocks_multi_mode(self):
		doc = _doc_mc(multiple_metal_converter=1)
		with self.assertRaises(ValidationError):
			melting_loss.validate_melting_loss(doc)

	def test_source_item_mandatory(self):
		doc = _doc_mc(source_item=None)
		with self.assertRaises(ValidationError):
			melting_loss.validate_melting_loss(doc)

	def test_source_qty_must_be_positive(self):
		for bad in (0, -5):
			with self.assertRaises(ValidationError):
				melting_loss.validate_melting_loss(_doc_mc(source_qty=bad))

	def test_loss_qty_mandatory(self):
		doc = _doc_mc(loss_qty=0)
		with self.assertRaises(ValidationError):
			melting_loss.validate_melting_loss(doc)

	def test_loss_qty_subprecision_blocked(self):
		# 0.0004 rounds to 0.000 at precision 3 -> V5.
		doc = _doc_mc(loss_qty=0.0004)
		with self.assertRaises(ValidationError):
			melting_loss.validate_melting_loss(doc)

	def test_loss_qty_negative_blocked(self):
		doc = _doc_mc(loss_qty=-1)
		with self.assertRaises(ValidationError):
			melting_loss.validate_melting_loss(doc)

	def test_loss_cannot_exceed_source(self):
		doc = _doc_mc(source_qty=500, loss_qty=501)
		with self.assertRaises(ValidationError):
			melting_loss.validate_melting_loss(doc)

	def test_full_loss_allowed(self):
		# Equality is legal: the whole melt is scrapped.
		doc = _doc_mc(source_qty=500, loss_qty=500)
		melting_loss.validate_melting_loss(doc)  # no raise
		self.assertIsNone(doc.target_item)

	def test_conversion_fields_cleared(self):
		doc = _doc_mc()
		melting_loss.validate_melting_loss(doc)
		self.assertIsNone(doc.target_item)
		self.assertEqual(doc.target_qty, 0)
		self.assertIsNone(doc.source_alloy)
		self.assertIsNone(doc.target_alloy)
		self.assertEqual(doc.source_alloy_check, 0)
		self.assertEqual(doc.target_alloy_check, 0)
		self.assertEqual(doc.alloy_batch_details, [])


class _FakeSELoss:
	"""Captures the Stock Entry payload the builder constructs."""

	def __init__(self, payload):
		self.payload = payload
		self.doctype = (
			payload.get("doctype", "Stock Entry")
			if isinstance(payload, dict)
			else "Stock Entry"
		)
		self.items = []
		self.name = "SE-LOSS-0001"
		self.saved = False
		self.submitted = False

	def append(self, table, row):
		self.items.append(row)

	def save(self):
		self.saved = True

	def submit(self):
		self.submitted = True


class TestMeltingLossBuilder(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		if not hasattr(frappe, "db") or not frappe.db:
			frappe.db = MagicMock()

		self._patches = [
			patch.object(melting_loss, "_loss_precision", return_value=3),
			patch.object(melting_loss, "flt", _det_flt),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()

	def _build(self, doc, existing=False):
		"""Run make_melting_loss_stock_entry with all externals mocked; return the SE."""
		captured = {}

		def _get_doc(payload):
			if isinstance(payload, str):
				# if payload is doctype name
				payload = {"doctype": payload}
			se = _FakeSELoss(payload)
			captured["se"] = se
			return se

		with patch("frappe.db.exists", return_value=existing), patch(
			"frappe.get_doc", side_effect=_get_doc
		), patch("frappe.new_doc", side_effect=_get_doc), patch.object(
			melting_loss, "_resolve_loss_item", return_value="ML-G-18KT-75.4-Y"
		), patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion.get_scrap_warehouse",
			return_value="Casting Scrap - GK",
		), patch("jewellery_erpnext.jewellery_erpnext.lock_order.lock_bins"), patch(
			"jewellery_erpnext.jewellery_erpnext.lock_order.stock_lock_key",
			side_effect=lambda i, w, b: (i, w, b or ""),
		):
			melting_loss.make_melting_loss_stock_entry(doc)
		return captured.get("se")

	def test_idempotency_guard_skips(self):
		doc = _doc_mc()
		se = self._build(doc, existing=True)
		self.assertIsNone(se)  # frappe.get_doc never called
		doc.db_set.assert_not_called()

	def test_se_header_and_rows(self):
		doc = _doc_mc(
			source_batch_details=[_batch_row(12.0, "B-001"), _batch_row(8.0, "B-002")]
		)
		se = self._build(doc)
		# Header
		self.assertEqual(se.payload["stock_entry_type"], "Process Loss")
		self.assertEqual(se.payload["purpose"], "Repack")
		self.assertEqual(se.payload["auto_created"], 1)
		self.assertEqual(se.payload["custom_metal_conversion_reference"], "mc0001")
		self.assertEqual(se.payload["manufacturer"], "Shubh")  # DR-1 header stamp
		self.assertNotIn("_customer", se.payload)  # DR-2: never set
		# Rows: 2 consume + 1 produce
		self.assertEqual(len(se.items), 3)
		consume = se.items[:2]
		produce = se.items[2]
		for c in consume:
			self.assertEqual(c["item_code"], "M-G-18KT-75.4-Y")
			self.assertEqual(c["s_warehouse"], "Casting RM - GK")
			self.assertNotIn("t_warehouse", c)
			self.assertTrue(c["batch_no"])
			self.assertEqual(c["use_serial_batch_fields"], 1)
		# Produce row: loss item -> scrap, no batch_no, Regular Stock write-off.
		self.assertEqual(produce["item_code"], "ML-G-18KT-75.4-Y")
		self.assertEqual(produce["qty"], 20.0)
		self.assertEqual(produce["t_warehouse"], "Casting Scrap - GK")
		self.assertEqual(produce["is_finished_item"], 1)
		self.assertEqual(produce["set_basic_rate_manually"], 1)
		self.assertEqual(produce["inventory_type"], "Regular Stock")
		self.assertNotIn("batch_no", produce)
		# basic_rate is deliberately NOT set by the builder: CustomStockEntry.set_basic_rate
		# assigns it from the consumed rows once ERPNext has resolved their outgoing rates
		# (customization/utils/loss_valuation) -- see test_process_loss_valuation.py.
		self.assertNotIn("basic_rate", produce)
		self.assertTrue(se.saved and se.submitted)
		doc.db_set.assert_called_once_with("stock_entry", "SE-LOSS-0001")

	def test_consume_rows_sorted_by_batch(self):
		# RULE A: rows consumed in deterministic batch order.
		doc = _doc_mc(
			source_batch_details=[_batch_row(8.0, "B-002"), _batch_row(12.0, "B-001")]
		)
		se = self._build(doc)
		consume_batches = [r["batch_no"] for r in se.items[:2]]
		self.assertEqual(consume_batches, ["B-001", "B-002"])

	def test_allocation_mismatch_throws(self):
		# V8: source_batch_details no longer sums to loss_qty.
		doc = _doc_mc(loss_qty=20.0, source_batch_details=[_batch_row(15.0, "B-001")])
		with self.assertRaises(ValidationError):
			self._build(doc)


class TestMeltingLossCancel(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_cancel_query_is_scoped(self):
		doc = _doc_mc()
		captured = {}

		def _get_all(dt, filters=None, pluck=None):
			captured["dt"] = dt
			captured["filters"] = filters
			return []

		with patch("frappe.db.get_all", side_effect=_get_all):
			melting_loss.cancel_melting_loss_stock_entries(doc)
		f = captured["filters"]
		self.assertEqual(captured["dt"], "Stock Entry")
		self.assertEqual(f["custom_metal_conversion_reference"], "mc0001")
		self.assertEqual(f["stock_entry_type"], "Process Loss")
		self.assertEqual(f["auto_created"], 1)
		self.assertEqual(f["docstatus"], 1)

	def test_cancel_cancels_each_found(self):
		doc = _doc_mc()
		cancelled = []
		fake = SimpleNamespace(cancel=lambda: cancelled.append(True))
		with patch("frappe.db.get_all", return_value=["SE-LOSS-0001"]), patch(
			"frappe.get_doc", return_value=fake
		):
			melting_loss.cancel_melting_loss_stock_entries(doc)
		self.assertEqual(len(cancelled), 1)


class _FakeMCDocUSB:
	"""Stand-in Metal Conversions doc for update_source_betch.

	SimpleNamespace lacks ``.append`` and ``.get``; update_source_betch reassigns
	``self.source_batch_details = []`` then appends dict rows to it.
	"""

	def __init__(self, **fields):
		self.source_batch_details = []
		for key, value in fields.items():
			setattr(self, key, value)

	def get(self, key, default=None):
		return getattr(self, key, default)

	def append(self, table, row):
		getattr(self, table).append(frappe._dict(row))


class TestUpdateSourceBatch(IntegrationTestCase):
	"""update_source_betch must consume ONLY the capped (authoritative) batch list.

	The bug: raw get_auto_batch_nos over-reports an orphan-SBB phantom batch, whose
	row later throws BatchNegativeStockError at SE submit. Swapping to
	capped_auto_batch_nos drops the phantom here; genuine shortfalls surface the
	friendly V7 throw at validate time instead.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		if not hasattr(frappe, "db") or not frappe.db:
			frappe.db = MagicMock()
		self._patches = [
			patch.object(utils, "flt", _det_flt),
			# Every source batch here is Regular Stock owned by no customer. An
			# empty lane map means get_batch_lane_map found nothing to override,
			# so update_source_betch falls back to its ("Regular Stock", None)
			# default for every batch -- patched (rather than mocking frappe.db)
			# so these stay pure-logic tests after the switch to a bulk read.
			patch(f"{_UTILS_PATH}.get_batch_lane_map", return_value={}),
			patch(f"{_UTILS_PATH}.get_sample_batches", return_value=set()),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()

	def _doc(self, **fields):
		defaults = {
			"is_melting_loss": 1,
			"loss_qty": 0.1,
			"source_qty": 2.0,
			"source_item": "M-G-18KT-75.4-Y",
			"source_warehouse": "Waxing RM - GEPL",
			"customer": None,
			# Set posting_time so the builder never calls the real nowtime().
			"date": "2026-07-15",
			"posting_time": "10:00:00",
		}
		defaults.update(fields)
		return _FakeMCDocUSB(**defaults)

	def test_phantom_dropped_and_qty_omitted(self):
		# capped_auto_batch_nos has already dropped the orphan phantom; only a real
		# batch survives. The phantom GE2D081-... must NOT appear in the result.
		real = [frappe._dict(batch_no="REAL", qty=0.5, warehouse="Waxing RM - GEPL")]
		with patch(
			f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=real
		) as mock_capped:
			doc = self._doc()
			utils.update_source_betch(doc)
		self.assertEqual(
			[dict(r) for r in doc.source_batch_details],
			[{"qty": 0.1, "batch": "REAL"}],
		)
		# qty must NOT be passed: it would let a phantom FIFO-truncate real batches
		# before the inventory-type / customer filter runs.
		self.assertNotIn("qty", mock_capped.call_args[0][0])

	def test_friendly_shortfall_throw_not_batch_negative(self):
		# After the phantom is dropped, the real batch can't cover loss_qty (0.1):
		# the friendly V7 throw fires at validate, standing in for the deep
		# BatchNegativeStockError that used to fire at SE submit.
		short = [frappe._dict(batch_no="REAL", qty=0.05, warehouse="Waxing RM - GEPL")]
		with patch(f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=short):
			doc = self._doc()
			with self.assertRaisesRegex(
				ValidationError, "source quantity is not available"
			):
				utils.update_source_betch(doc)

	def test_all_phantom_throws_no_batch(self):
		# Only the phantom existed; capping returns an empty list.
		with patch(f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=[]):
			doc = self._doc()
			with self.assertRaisesRegex(
				ValidationError, "No batch available for given warehouse"
			):
				utils.update_source_betch(doc)

	def test_conversion_mode_unchanged(self):
		# is_melting_loss=0 -> required_qty = source_qty; FIFO across two real batches.
		batches = [
			frappe._dict(batch_no="B1", qty=0.6, warehouse="Waxing RM - GEPL"),
			frappe._dict(batch_no="B2", qty=0.9, warehouse="Waxing RM - GEPL"),
		]
		with patch(f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=batches):
			doc = self._doc(is_melting_loss=0, source_qty=1.0)
			utils.update_source_betch(doc)
		rows = [dict(r) for r in doc.source_batch_details]
		self.assertEqual([r["batch"] for r in rows], ["B1", "B2"])
		self.assertAlmostEqual(sum(r["qty"] for r in rows), 1.0, places=3)

	def test_conversion_mode_allocates_across_mixed_ownership(self):
		"""The requirement: 20 g draws 8 g Regular + 12 g Customer Goods.

		The old code narrowed FIFO to ONE declared inventory type, so a warehouse
		holding both ownerships threw a shortfall even with enough metal present.
		"""
		batches = [
			frappe._dict(batch_no="REG", qty=8.0, warehouse="Waxing RM - GEPL"),
			frappe._dict(batch_no="CG", qty=12.0, warehouse="Waxing RM - GEPL"),
		]
		lanes = {
			"REG": ("Regular Stock", None),
			"CG": ("Customer Goods", "TNCU0001"),
		}
		with (
			patch(f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=batches),
			patch(f"{_UTILS_PATH}.get_batch_lane_map", return_value=lanes),
		):
			doc = self._doc(is_melting_loss=0, source_qty=20.0)
			utils.update_source_betch(doc)
		self.assertEqual(
			[dict(r) for r in doc.source_batch_details],
			[{"qty": 8.0, "batch": "REG"}, {"qty": 12.0, "batch": "CG"}],
		)

	def test_conversion_mode_spans_two_customers(self):
		"""Nothing stops FIFO crossing two customers -> three lanes downstream."""
		batches = [
			frappe._dict(batch_no="CG_A", qty=3.0, warehouse="Waxing RM - GEPL"),
			frappe._dict(batch_no="REG", qty=2.0, warehouse="Waxing RM - GEPL"),
			frappe._dict(batch_no="CG_B", qty=5.0, warehouse="Waxing RM - GEPL"),
		]
		lanes = {
			"CG_A": ("Customer Goods", "CUST-A"),
			"REG": ("Regular Stock", None),
			"CG_B": ("Customer Goods", "CUST-B"),
		}
		with (
			patch(f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=batches),
			patch(f"{_UTILS_PATH}.get_batch_lane_map", return_value=lanes),
		):
			doc = self._doc(is_melting_loss=0, source_qty=8.0)
			utils.update_source_betch(doc)
		# FIFO order preserved, partial draw on the last batch
		self.assertEqual(
			[dict(r) for r in doc.source_batch_details],
			[
				{"qty": 3.0, "batch": "CG_A"},
				{"qty": 2.0, "batch": "REG"},
				{"qty": 3.0, "batch": "CG_B"},
			],
		)

	def test_conversion_mode_skips_customer_sample_goods(self):
		"""Sample stock is never allocated: the SE would hard-throw at submit.

		"Repack-Metal Conversion" is not in SAMPLE_ALLOWED_SE_TYPES, so a sample
		row reaching validate_sample_goods_not_consumed makes the doc
		un-submittable. It must be skipped here, not surfaced later.
		"""
		batches = [
			frappe._dict(batch_no="SAMPLE", qty=5.0, warehouse="Waxing RM - GEPL"),
			frappe._dict(batch_no="CG", qty=6.0, warehouse="Waxing RM - GEPL"),
		]
		lanes = {
			"SAMPLE": ("Customer Goods", "TNCU0001"),
			"CG": ("Customer Goods", "TNCU0001"),
		}
		with (
			patch(f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=batches),
			patch(f"{_UTILS_PATH}.get_batch_lane_map", return_value=lanes),
			patch(f"{_UTILS_PATH}.get_sample_batches", return_value={"SAMPLE"}),
		):
			doc = self._doc(is_melting_loss=0, source_qty=6.0)
			utils.update_source_betch(doc)
		self.assertEqual(
			[dict(r) for r in doc.source_batch_details],
			[{"qty": 6.0, "batch": "CG"}],
		)

	def test_null_inventory_type_is_treated_as_regular_stock(self):
		"""A batch with no custom_inventory_type is company stock, not a third lane.

		Previously ``None != "Regular Stock"`` skipped it silently and surfaced a
		bogus "source quantity is not available" throw.
		"""
		batches = [
			frappe._dict(batch_no="UNTYPED", qty=4.0, warehouse="Waxing RM - GEPL")
		]
		with (
			patch(f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=batches),
			patch(
				f"{_UTILS_PATH}.get_batch_lane_map",
				return_value={"UNTYPED": ("Regular Stock", None)},
			),
		):
			doc = self._doc(is_melting_loss=0, source_qty=4.0)
			utils.update_source_betch(doc)
		self.assertEqual(
			[dict(r) for r in doc.source_batch_details],
			[{"qty": 4.0, "batch": "UNTYPED"}],
		)

	def test_melting_loss_stays_regular_stock_only(self):
		"""Melting loss must not draw customer metal.

		make_melting_loss_stock_entry books ONE scrap row force-typed
		"Regular Stock", so allocating a customer's batch here would silently
		convert customer metal into company scrap.
		"""
		batches = [
			frappe._dict(batch_no="CG", qty=5.0, warehouse="Waxing RM - GEPL"),
			frappe._dict(batch_no="REG", qty=5.0, warehouse="Waxing RM - GEPL"),
		]
		lanes = {
			"CG": ("Customer Goods", "TNCU0001"),
			"REG": ("Regular Stock", None),
		}
		with (
			patch(f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=batches),
			patch(f"{_UTILS_PATH}.get_batch_lane_map", return_value=lanes),
		):
			doc = self._doc(is_melting_loss=1, loss_qty=1.0)
			utils.update_source_betch(doc)
		self.assertEqual(
			[dict(r) for r in doc.source_batch_details],
			[{"qty": 1.0, "batch": "REG"}],
		)


_MODULE = (
	"jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions"
)
_DOCTYPE_JSON = os.path.join(os.path.dirname(mc.__file__), "metal_conversions.json")


def _doc(percentage=None, remarks=None, precision=3):
	"""A stand-in Metal Conversions carrying only what set_remarks touches."""
	doc = SimpleNamespace(
		percentage=percentage,
		remarks=remarks,
		precision=lambda fieldname: precision,
	)
	doc.set_remarks = MetalConversions.set_remarks.__get__(doc, SimpleNamespace)
	return doc


class TestRemarksFieldConfiguration(IntegrationTestCase):
	"""The two DocType JSON facts the feature rests on.

	Both read as tidy-up to anyone who does not know why they are there, and both are
	silently undone by a Customize Form export, so they are pinned here.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		with open(_DOCTYPE_JSON) as handle:
			self.fields = {df["fieldname"]: df for df in json.load(handle)["fields"]}

	def test_remarks_ships_no_options(self):
		"""Load-bearing: a falsy df.options is what makes frappe skip _validate_selects.

		Put any options back and every save of a rendered remark throws
		'Remarks cannot be "NR 1.80% ...". It should be one of "..."'.
		"""
		self.assertNotIn("options", self.fields["remarks"])

	def test_percentage_precision_pinned_to_two(self):
		"""Loss-book percentages are written to 2 decimals.

		Without this the sentence reads "NR 1.800%", because the site runs System
		Settings float_precision 3 for gram/carat weights (ensure_float_precision_three).
		"""
		self.assertEqual(self.fields["percentage"].get("precision"), "2")


class TestRenderRemarkOptions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_percentage_is_substituted_at_the_given_precision(self):
		self.assertEqual(
			render_remark_options(1.8, 2), ["NR 1.80% PLAIN ROUND BALLS LOSS BOOK"]
		)
		self.assertEqual(
			render_remark_options(1.8, 3), ["NR 1.800% PLAIN ROUND BALLS LOSS BOOK"]
		)

	def test_precision_comes_from_the_field_not_the_typed_digits(self):
		"""Trailing zeros are padded, extra digits rounded -- the field decides."""
		self.assertEqual(
			render_remark_options(1.8, 4), ["NR 1.8000% PLAIN ROUND BALLS LOSS BOOK"]
		)
		self.assertEqual(
			render_remark_options(1.8055, 2), ["NR 1.81% PLAIN ROUND BALLS LOSS BOOK"]
		)

	def test_blank_percentage_renders_zero_rather_than_raising(self):
		"""The dropdown is built on every refresh, including before anything is typed."""
		self.assertEqual(
			render_remark_options(None, 2), ["NR 0.00% PLAIN ROUND BALLS LOSS BOOK"]
		)
		self.assertEqual(
			render_remark_options("", 2), ["NR 0.00% PLAIN ROUND BALLS LOSS BOOK"]
		)

	def test_one_entry_rendered_per_template(self):
		"""Adding a sentence to REMARK_TEMPLATES must be the only change required."""
		self.assertEqual(len(render_remark_options(1.8, 2)), len(REMARK_TEMPLATES))


class TestTemplateIndex(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_rendered_option_round_trips_to_its_template(self):
		for precision in (2, 3, 4):
			rendered = render_remark_options(1.8, precision)[0]
			self.assertEqual(template_index(rendered), 0, rendered)

	def test_remark_rendered_at_a_different_percentage_is_still_recognised(self):
		"""The whole point: a stale remark is re-rendered, not rejected."""
		self.assertEqual(template_index("NR 1.80% PLAIN ROUND BALLS LOSS BOOK"), 0)
		self.assertEqual(template_index("NR 99.999% PLAIN ROUND BALLS LOSS BOOK"), 0)

	def test_non_template_text_is_rejected(self):
		for value in (
			"junk",
			"",
			None,
			"NR 1.80% PLAIN ROUND BALLS LOSS BOOKS",  # trailing S
			"nr 1.80% plain round balls loss book",  # wrong case
			"NR % PLAIN ROUND BALLS LOSS BOOK",  # no number at all
			"XX NR 1.80% PLAIN ROUND BALLS LOSS BOOK",  # prefixed
		):
			self.assertIsNone(template_index(value), value)


class TestSetRemarks(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_blank_remark_is_left_alone(self):
		doc = _doc(percentage=1.8, remarks=None)
		doc.set_remarks()
		self.assertIsNone(doc.remarks)

	def test_stale_percentage_is_re_rendered_on_validate(self):
		"""Pick at 1.80, edit Percentage to 2.5 -- the stored sentence must follow."""
		doc = _doc(
			percentage=2.5, remarks="NR 1.800% PLAIN ROUND BALLS LOSS BOOK", precision=3
		)
		doc.set_remarks()
		self.assertEqual(doc.remarks, "NR 2.500% PLAIN ROUND BALLS LOSS BOOK")

	def test_already_current_remark_is_unchanged(self):
		doc = _doc(
			percentage=1.8, remarks="NR 1.800% PLAIN ROUND BALLS LOSS BOOK", precision=3
		)
		doc.set_remarks()
		self.assertEqual(doc.remarks, "NR 1.800% PLAIN ROUND BALLS LOSS BOOK")

	def test_arbitrary_remark_is_rejected(self):
		"""Replaces the frappe Select check the empty JSON options turned off."""
		doc = _doc(percentage=1.8, remarks="whatever I like")
		with self.assertRaises(ValidationError):
			doc.set_remarks()

	def test_guard_holds_for_a_value_written_around_the_form(self):
		"""API / import writes reach validate too -- the guard is server-side."""
		doc = _doc(percentage=1.8, remarks="NR 1.80% PLAIN ROUND BALLS LOSS BOO")
		with self.assertRaises(ValidationError):
			doc.set_remarks()

	def test_renderer_is_the_single_source_of_truth(self):
		"""set_remarks must go through render_remark_options, never its own f-string."""
		doc = _doc(percentage=1.8, remarks="NR 1.800% PLAIN ROUND BALLS LOSS BOOK")
		with patch(
			f"{_MODULE}.render_remark_options", return_value=["SENTINEL"]
		) as renderer:
			doc.set_remarks()
		renderer.assert_called_once()
		self.assertEqual(doc.remarks, "SENTINEL")


# ---------------------------------------------------------------------------------------
# Server-side quantity check, cancel cascade, multiple-converter split, exact-stock draw.
# ---------------------------------------------------------------------------------------


def _purities(mapping):
	"""Patch the purity master read with an explicit item -> purity map."""

	def _lookup(item_code):
		if item_code not in mapping:
			frappe.throw("Attribute Value Missing")
		return mapping[item_code]

	return patch.object(mc, "get_metal_purity_percentage", side_effect=_lookup)


class TestValidateTargetQty(IntegrationTestCase):
	"""T43/T55/T20/T21: the server re-derives Target Qty from the purity masters at submit.

	The form computes it and clears it when the source changes, but nothing on the server
	checked it, so an API caller or a draft left stale by a master edit could post a target
	the source metal cannot make.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _doc(self, target_qty, source_qty=10.0):
		doc = frappe.new_doc("Metal Conversions")
		doc.update(
			{
				"source_item": "SRC-100",
				"target_item": "TGT-91.75",
				"source_qty": source_qty,
				"target_qty": target_qty,
			}
		)
		# The document's own float precision decides nothing now (gk and the test site run at
		# 2): raw figures are compared, a unit of transfer_qty's precision apart at most.
		doc.precision = lambda fieldname: 2
		return doc

	def test_the_value_the_form_stores_passes(self):
		"""10 g x 100 / 91.75 = 10.899182561 g, as MCON00332 stored it."""
		with _purities({"SRC-100": 100.0, "TGT-91.75": 91.75}):
			self._doc(10.899182561).validate_target_qty()

	def test_the_value_at_posting_precision_passes(self):
		with _purities({"SRC-100": 100.0, "TGT-91.75": 91.75}):
			self._doc(10.899).validate_target_qty()

	def test_a_stale_target_is_refused(self):
		"""A target the source cannot make -- e.g. left from an earlier 9.5 g source."""
		with _purities({"SRC-100": 100.0, "TGT-91.75": 91.75}):
			with self.assertRaisesRegex(ValidationError, "is not what"):
				self._doc(10.354223433).validate_target_qty()

	def test_the_purity_master_decides_not_the_item_name(self):
		"""T20: at 99.9% the same 10 g make 10.888283379 g, not 10.899182561 g."""
		with _purities({"SRC-100": 99.9, "TGT-91.75": 91.75}):
			self._doc(10.888283379).validate_target_qty()
			with self.assertRaisesRegex(ValidationError, "is not what"):
				self._doc(10.899182561).validate_target_qty()

	def test_a_zero_purity_is_refused_without_dividing_by_it(self):
		"""T21: a purity master at 0 stops the submit with the master named, no ZeroDivisionError."""
		with _purities({"SRC-100": 100.0, "TGT-91.75": 0.0}):
			with self.assertRaisesRegex(ValidationError, "Target Item"):
				self._doc(10.899182561).validate_target_qty()

	def test_a_blank_source_qty_is_left_to_the_builders_messages(self):
		"""Adversarial review (29 Sep): a blank Source Qty reached calculate_metal_conversion,
		which multiplied None -- a TypeError, not a message. With nothing to compare the check
		steps aside, and the builder says what is missing."""
		with patch.object(
			MetalConversions,
			"calculate_metal_conversion",
			side_effect=AssertionError("not reached"),
		):
			for blank in (None, 0):
				self.assertIsNone(
					self._doc(10.899182561, source_qty=blank).validate_target_qty()
				)


class TestCancelCascade(IntegrationTestCase):
	"""T40/T60/R6: cancelling a conversion cancels the entry it generated -- that one only."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_the_query_names_only_this_conversions_entry(self):
		captured = {}

		def _get_all(doctype, filters=None, pluck=None, **kwargs):
			captured.update(doctype=doctype, filters=filters, pluck=pluck)
			return []

		# The doc carries a forward link that is not its own, and the query must still be the
		# reverse reference alone. On gk, 20 submitted conversions name another document in
		# stock_entry: MCON00305/309/312/313/382 all name Material Receipt GE-SE-MR-24-00038.
		# A lookup by that link would cancel someone else's entry.
		with patch.object(mc.frappe.db, "get_all", side_effect=_get_all):
			mc.cancel_conversion_stock_entries(
				frappe._dict(name="MCON-T-1", stock_entry="GE-SE-MR-24-00038")
			)
		self.assertEqual(captured["doctype"], "Stock Entry")
		self.assertEqual(
			captured["filters"],
			{
				"custom_metal_conversion_reference": "MCON-T-1",
				"stock_entry_type": "Repack-Metal Conversion",
				"auto_created": 1,
				"docstatus": 1,
			},
		)
		self.assertEqual(captured["pluck"], "name")

	def test_each_entry_found_is_cancelled_through_its_controller(self):
		cancelled = []
		entry = SimpleNamespace(cancel=lambda: cancelled.append("SE-CONV-1"))

		def _get_doc(doctype, name=None, *args, **kwargs):
			if doctype == "Stock Entry":
				self.assertEqual(name, "SE-CONV-1")
				return entry
			return _REAL_GET_DOC(doctype, name, *args, **kwargs)

		with patch.object(
			mc.frappe.db, "get_all", return_value=["SE-CONV-1"]
		), patch.object(mc.frappe, "get_doc", side_effect=_get_doc):
			mc.cancel_conversion_stock_entries(frappe._dict(name="MCON-T-1"))
		self.assertEqual(cancelled, ["SE-CONV-1"])

	def test_on_cancel_runs_both_cascades(self):
		doc = frappe._dict(name="MCON-T-1")
		with patch.object(
			mc, "cancel_melting_loss_stock_entries"
		) as loss, patch.object(mc, "cancel_conversion_stock_entries") as conversion:
			MetalConversions.on_cancel(doc)
		loss.assert_called_once_with(doc)
		conversion.assert_called_once_with(doc)

	def test_a_refused_entry_cancel_propagates_after_the_loss_cascade(self):
		"""T41: ERPNext refuses the entry's cancel once an output was used, and the refusal
		must reach the caller. A try/except anywhere on the way would leave the conversion
		cancelled over a live entry."""
		order = []

		def _refuse():
			order.append("SE-CONV-1")
			raise BatchNegativeStockError("Batch T-12-A has negative stock")

		def _get_doc(doctype, name=None, *args, **kwargs):
			if doctype == "Stock Entry":
				return SimpleNamespace(cancel=_refuse)
			return _REAL_GET_DOC(doctype, name, *args, **kwargs)

		# Only the entry lookup is answered; any other read on the way goes to the real one.
		real_get_all = mc.frappe.db.get_all

		def _get_all(doctype, *args, **kwargs):
			if doctype == "Stock Entry":
				return ["SE-CONV-1"]
			return real_get_all(doctype, *args, **kwargs)

		with (
			patch.object(
				mc,
				"cancel_melting_loss_stock_entries",
				side_effect=lambda doc: order.append("loss"),
			),
			patch.object(mc.frappe.db, "get_all", side_effect=_get_all),
			patch.object(mc.frappe, "get_doc", side_effect=_get_doc),
		):
			with self.assertRaises(BatchNegativeStockError):
				MetalConversions.on_cancel(frappe._dict(name="MCON-T-1"))
		self.assertEqual(order, ["loss", "SE-CONV-1"])


def _source_row(
	idx, item_code, qty, batch, inventory_type=None, customer=None, purity=100.0
):
	row = frappe._dict(
		idx=idx,
		item_code=item_code,
		qty=qty,
		batch=batch,
		inventory_type=inventory_type,
		customer=customer,
		total=qty * purity,
	)
	return row


class _FakeMultiDoc(_FakeMCDoc):
	"""A multiple-converter document for make_multiple_metal_stock_entry."""

	doctype = "Metal Conversions"

	def get_mc_table_purity(self, item_code, qty):
		purity = self.purity_map[item_code]
		return qty * purity, purity


class TestMultipleConverterSplitsCustomerBatches(IntegrationTestCase):
	"""T15/T19/T26/T07: the multiple converter keeps every customer batch apart.

	The legacy build grouped by inventory type alone and appended its targets WITHOUT a
	customer, so two batches -- even two customers -- came out as one ownerless target.
	Rows of different purities are the point of this mode, so each lane needs alloy in
	proportion to its own FINE gold (fine / target purity - source), the stored Alloy Qty is
	shared by those needs, and each lane's target is its source plus its share.

	Hand arithmetic for the main case (G24 = 100%, G22 = 91.75%, target G18 = 75.4%):
	fine A1 3 x 100 = 300, A2 2 x 91.75 = 183.5, R 1 x 100 = 100; 583.5 / 75.4 = 7.738727
	-> 7.739 g, alloy 7.739 - 6 = 1.739 g. Needs 300 / 75.4 - 3 = 0.978780, 183.5 / 75.4 - 2
	= 0.433687, 100 / 75.4 - 1 = 0.326260 (sum 1.738727); 1.739 shared by them is 0.979 /
	0.434 / 0.326, so the targets are 3.979 / 2.434 / 1.326 (sum 7.739).
	"""

	PURITY = {"G24": 100.0, "G22": 91.75, "G18": 75.4, "G916": 91.6}
	LANE_MAP = {
		"A1": ("Customer Goods", "CUST-A"),
		"A2": ("Customer Goods", "CUST-A"),
		"R": ("Regular Stock", None),
		"B1": ("Customer Goods", "CUST-B"),
		"A9": ("Customer Goods", "CUST-A"),
		"B9": ("Customer Goods", "CUST-A"),
		"C9": ("Customer Goods", "CUST-B"),
	}

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		self._patches = [patch.object(mc, "flt", _det_flt)]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()

	def _doc(self, rows, **fields):
		defaults = {
			"name": "mcmulti1",
			"company": "GK",
			"branch": "Main",
			"department": "Casting - GK",
			"manufacturer": "Shubh",
			"employee": "EMP-0001",
			"source_warehouse": "Casting RM - GK",
			"target_warehouse": "Casting RM - GK",
			"multiple_metal_converter": 1,
			"m_target_item": "G18",
			"m_target_qty": 7.739,
			"alloy": "ALLOY",
			"alloy_qty": 1.739,
			"alloy_check": 0,
			"alloy_batch": None,
			"mc_source_table": rows,
			"purity_map": dict(self.PURITY),
		}
		defaults.update(fields)
		return _FakeMultiDoc(**defaults)

	def _main_rows(self):
		return [
			_source_row(1, "G24", 3.0, "A1", purity=100.0),
			_source_row(2, "G22", 2.0, "A2", purity=91.75),
			_source_row(3, "G24", 1.0, "R", purity=100.0),
		]

	def _build(self, doc, pool=None):
		captured = {"links": []}

		def _get_doc(*args, **kwargs):
			payload = args[0] if args else kwargs
			if isinstance(payload, dict) and payload.get("doctype") == "Stock Entry":
				captured["se"] = _FakeSE(payload)
				return captured["se"]
			return _REAL_GET_DOC(*args, **kwargs)

		def _new_doc(doctype, *args, **kwargs):
			if doctype == "Stock Entry":
				return _FakeSE({})
			return _REAL_NEW_DOC(doctype, *args, **kwargs)

		def _set_value(doctype, name, field, value, *args, **kwargs):
			if doctype == "Metal Conversions":
				captured["links"].append((doctype, name, field, value))
				return
			db = frappe.local.db
			return type(db).set_value(db, doctype, name, field, value, *args, **kwargs)

		pool = pool or [frappe._dict(batch_no="AL1", qty=5.0)]
		with (
			patch("frappe.get_doc", side_effect=_get_doc),
			patch("frappe.new_doc", side_effect=_new_doc),
			patch.object(mc.frappe.db, "set_value", side_effect=_set_value),
			patch.object(mc, "get_batch_lane_map", return_value=self.LANE_MAP),
			_purities(self.PURITY),
			patch.object(mc, "capped_auto_batch_nos", return_value=pool) as fifo,
			patch(
				"jewellery_erpnext.jewellery_erpnext.lock_order.preallocate_series_for_docs"
			),
			patch("jewellery_erpnext.jewellery_erpnext.lock_order.lock_bins"),
		):
			mc.make_multiple_metal_stock_entry(doc)
		captured["fifo"] = fifo
		return captured

	@staticmethod
	def _lanes(se):
		lanes = {}
		for row in se.items:
			lane = lanes.setdefault(
				row.custom_conversion_lane, {"sources": [], "alloy": [], "target": []}
			)
			if row.get("t_warehouse"):
				lane["target"].append((row.customer, row.qty))
			elif row.item_code == "ALLOY":
				lane["alloy"].append((row.batch_no, row.qty))
			else:
				lane["sources"].append((row.item_code, row.batch_no, row.qty))
		return lanes

	def test_each_customer_batch_is_its_own_target(self):
		"""T15/T19, R1/R2/R3: one target per customer batch, one pooled Regular target."""
		captured = self._build(self._doc(self._main_rows()))
		self.assertEqual(
			self._lanes(captured["se"]),
			{
				"Customer Goods|CUST-A|A1": {
					"sources": [("G24", "A1", 3.0)],
					"alloy": [("AL1", 0.979)],
					"target": [("CUST-A", 3.979)],
				},
				"Customer Goods|CUST-A|A2": {
					"sources": [("G22", "A2", 2.0)],
					"alloy": [("AL1", 0.434)],
					"target": [("CUST-A", 2.434)],
				},
				"Regular Stock|": {
					"sources": [("G24", "R", 1.0)],
					"alloy": [("AL1", 0.326)],
					"target": [(None, 1.326)],
				},
			},
		)
		payload = captured["se"].payload
		self.assertEqual(payload["_customer"], "CUST-A")
		self.assertIsNone(payload["inventory_type"])
		self.assertEqual(payload["custom_metal_conversion_reference"], "mcmulti1")
		self.assertEqual(
			captured["links"],
			[("Metal Conversions", "mcmulti1", "stock_entry", "SE-CONV-0001")],
		)

	def test_the_alloy_comes_from_the_picked_batch_when_there_is_one(self):
		captured = self._build(self._doc(self._main_rows(), alloy_batch="AL-PICKED"))
		alloy = [r for r in captured["se"].items if r.item_code == "ALLOY"]
		self.assertEqual({r.batch_no for r in alloy}, {"AL-PICKED"})
		self.assertAlmostEqual(sum(r.qty for r in alloy), 1.739, places=9)
		captured["fifo"].assert_not_called()

	def test_the_alloy_is_drawn_fifo_and_split_once_across_lanes(self):
		"""R4: 1.739 g handed out once, walking AL1 (1.0) then AL2."""
		pool = [
			frappe._dict(batch_no="AL1", qty=1.0),
			frappe._dict(batch_no="AL2", qty=5.0),
		]
		captured = self._build(self._doc(self._main_rows()), pool=pool)
		alloy = [
			(r.custom_conversion_lane, r.batch_no, r.qty)
			for r in captured["se"].items
			if r.item_code == "ALLOY"
		]
		self.assertEqual(
			alloy,
			[
				("Customer Goods|CUST-A|A1", "AL1", 0.979),
				("Customer Goods|CUST-A|A2", "AL1", 0.021),
				("Customer Goods|CUST-A|A2", "AL2", 0.413),
				("Regular Stock|", "AL2", 0.326),
			],
		)

	def test_two_customers_never_share_a_target(self):
		"""T02: CUST-B's batch keeps CUST-B -- the legacy build took the first row's customer."""
		rows = [
			_source_row(1, "G24", 3.0, "A1", purity=100.0),
			_source_row(2, "G22", 2.0, "B1", purity=91.75),
			_source_row(3, "G24", 1.0, "R", purity=100.0),
		]
		captured = self._build(self._doc(rows))
		targets = {
			tag: lane["target"] for tag, lane in self._lanes(captured["se"]).items()
		}
		self.assertEqual(targets["Customer Goods|CUST-B|B1"], [("CUST-B", 2.434)])
		self.assertEqual(targets["Customer Goods|CUST-A|A1"], [("CUST-A", 3.979)])

	def test_a_batch_needing_alloy_taken_out_is_refused(self):
		"""T26: 2 g of 75.4% cannot become 91.75% by adding alloy; nothing is netted against
		another lane. 300 + 150.8 = 450.8 / 91.75 = 4.913 g; A2's share 1.643 g < 2 g."""
		rows = [
			_source_row(1, "G24", 3.0, "A1", purity=100.0),
			_source_row(2, "G18", 2.0, "A2", purity=75.4),
		]
		doc = self._doc(rows, m_target_item="G22", m_target_qty=4.913, alloy_qty=0.913)
		with self.assertRaisesRegex(ValidationError, "A2"):
			self._build(doc)

	def test_the_batch_decides_ownership_not_the_row(self):
		"""T07: the batch is the physical truth (row_ownership rule 1). The form types a row
		from the header of the entry that minted its batch -- blank for a mixed conversion --
		so a row can claim what its batch is not, and the conversion posts the batch's
		ownership. (Adversarial review, 29 Sep: refusing instead blocked a customer's own
		converted batch, whose row read "Regular Stock".)"""
		rows = self._main_rows()
		rows[0].update(inventory_type="Regular Stock", customer="CUST-B")
		rows[2].update(inventory_type="Customer Goods", customer="CUST-A")
		lanes = self._lanes(self._build(self._doc(rows))["se"])
		self.assertEqual(
			{tag: lane["target"] for tag, lane in lanes.items()},
			{
				"Customer Goods|CUST-A|A1": [("CUST-A", 3.979)],
				"Customer Goods|CUST-A|A2": [("CUST-A", 2.434)],
				"Regular Stock|": [(None, 1.326)],
			},
		)

	def test_a_lane_at_the_target_purity_takes_no_alloy(self):
		"""Adversarial review (29 Sep, confirmed): apportioning the TARGET by fine gold left a
		lane already at the target purity a milligram short, and it was refused as needing
		alloy taken out. A9 8.906 g at 91.6% into 91.6%, B9 2.034 g and C9 2.787 g at 100%:
		fine 815.7896 + 203.4 + 278.7 = 1297.8896 / 91.6 = 14.1691 -> 14.169 g, alloy 14.169
		- 13.727 = 0.442 g. Needs 0, 203.4 / 91.6 - 2.034 = 0.186524 and 278.7 / 91.6 - 2.787
		= 0.255576; 0.442 shared by them is 0 / 0.186 / 0.256."""
		rows = [
			_source_row(1, "G916", 8.906, "A9", purity=91.6),
			_source_row(2, "G24", 2.034, "B9", purity=100.0),
			_source_row(3, "G24", 2.787, "C9", purity=100.0),
		]
		doc = self._doc(
			rows, m_target_item="G916", m_target_qty=14.169, alloy_qty=0.442
		)
		lanes = self._lanes(self._build(doc)["se"])
		self.assertEqual(
			{tag: (lane["alloy"], lane["target"]) for tag, lane in lanes.items()},
			{
				"Customer Goods|CUST-A|A9": ([], [("CUST-A", 8.906)]),
				"Customer Goods|CUST-A|B9": ([("AL1", 0.186)], [("CUST-A", 2.22)]),
				"Customer Goods|CUST-B|C9": ([("AL1", 0.256)], [("CUST-B", 3.043)]),
			},
		)

	def test_a_target_on_a_rounding_tie_is_accepted(self):
		"""Adversarial review (29 Sep, confirmed): 3.77 g of 22KT (91.75%) into 18KT (75.4%)
		makes 4.5875 g. Calculate stores round()'s 4.587 -- the float is a hair below the tie
		-- while the site's Banker's flt() makes it 4.588, so the old check refused the
		document and pressing Calculate stored the same 4.587 again."""
		rows = [_source_row(1, "G22", 3.77, "A1", purity=91.75)]
		doc = self._doc(rows, m_target_qty=4.587, alloy_qty=0.817)
		with (
			patch.object(mc, "flt", _bankers_flt),
			patch.object(lanes_mod, "flt", _bankers_flt),
		):
			lanes = self._lanes(self._build(doc)["se"])
		self.assertEqual(
			lanes,
			{
				"Customer Goods|CUST-A|A1": {
					"sources": [("G22", "A1", 3.77)],
					"alloy": [("AL1", 0.817)],
					"target": [("CUST-A", 4.587)],
				}
			},
		)

	def test_a_stale_target_is_refused(self):
		with self.assertRaisesRegex(ValidationError, "Press Calculate again"):
			self._build(self._doc(self._main_rows(), m_target_qty=7.5))

	def test_an_alloy_direction_that_disagrees_is_refused(self):
		with self.assertRaisesRegex(ValidationError, "Alloy Check"):
			self._build(self._doc(self._main_rows(), alloy_check=1))

	def test_a_regular_only_document_keeps_the_legacy_build(self):
		"""MC-12: no customer metal -> the old path, untouched (one pooled, untagged target)."""
		rows = [
			_source_row(1, "G24", 3.0, "R", inventory_type="Regular Stock"),
			_source_row(2, "G24", 1.0, "R", inventory_type="Regular Stock"),
		]
		doc = self._doc(rows, m_target_qty=5.305, alloy_qty=1.305)
		with patch.object(mc, "make_multiple_customer_metal_stock_entry") as new_path:
			captured = self._build(doc)
		new_path.assert_not_called()
		targets = [r for r in captured["se"].items if r.get("t_warehouse")]
		self.assertEqual(len(targets), 1)
		# The legacy build posts sum(total) / purity unrounded: 400 / 75.4.
		self.assertAlmostEqual(targets[0].qty, 400 / 75.4, places=9)
		self.assertFalse(
			any(r.get("custom_conversion_lane") for r in captured["se"].items)
		)


class TestUpdateSourceBatchDrawsExactStock(IntegrationTestCase):
	"""T13/T61: drawing exactly the stock on hand is not refused for a float residue.

	1.09 g + 2.18 g of batch balances reads 3.2699999999999996 as floats; the draw compared
	it with 3.27 exactly and refused a legitimate conversion as "not available".
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		self._patches = [
			patch.object(utils, "flt", _det_flt),
			patch(f"{_UTILS_PATH}.get_batch_lane_map", return_value={}),
			patch(f"{_UTILS_PATH}.get_sample_batches", return_value=set()),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in self._patches:
			p.stop()

	def _doc(self, source_qty):
		return _FakeMCDocUSB(
			is_melting_loss=0,
			loss_qty=0,
			source_qty=source_qty,
			source_item="G22",
			source_warehouse="RM",
			date="2026-09-29",
			posting_time="10:00:00",
		)

	def test_the_whole_of_two_batches_is_drawn(self):
		batches = [
			frappe._dict(batch_no="T11", qty=1.09),
			frappe._dict(batch_no="T12", qty=2.1799999999999997),
		]
		doc = self._doc(3.27)
		with patch(f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=batches):
			utils.update_source_betch(doc)
		self.assertEqual([r.batch for r in doc.source_batch_details], ["T11", "T12"])
		self.assertAlmostEqual(
			sum(r.qty for r in doc.source_batch_details), 3.27, places=9
		)

	def test_a_real_shortfall_is_still_refused(self):
		batches = [
			frappe._dict(batch_no="T11", qty=1.09),
			frappe._dict(batch_no="T12", qty=2.17),
		]
		with patch(f"{_UTILS_PATH}.capped_auto_batch_nos", return_value=batches):
			with self.assertRaisesRegex(ValidationError, "not available"):
				utils.update_source_betch(self._doc(3.27))


# ---------------------------------------------------------------------------------------
# Adversarial review of the per-batch conversion, 29 Sep 2026 (round 2)
# ---------------------------------------------------------------------------------------


def _half_mg_documents(source_purity, target_purity, count):
	"""The first ``count`` whole-milligram sources whose conversion lands exactly on a half
	milligram, with the figures calculate_metal_conversion hands the form: the target
	unrounded and the alloy rounded by round()."""
	documents, source_mg = [], 0
	while len(documents) < count:
		source_mg += 1
		halves = (
			Fraction(source_mg, 1000)
			* Fraction(source_purity)
			/ Fraction(target_purity)
			* 2000
		)
		if halves.denominator == 1 and halves.numerator % 2:
			source_qty = source_mg / 1000
			target_qty = float(
				(source_qty * float(source_purity)) / float(target_purity)
			)
			documents.append(
				(source_qty, target_qty, round(float(target_qty - source_qty), 3))
			)
	return documents


class TestRoundingTiesAreAccepted(_BuilderCase):
	"""Adversarial review (confirmed P2): a conversion landing on a half milligram was refused,
	and pressing Calculate stored the same figures again.

	The form's figures come from round() (calculate_metal_conversion); the builder derived its
	own with the site's flt() -- Banker's Rounding on kg-gk, gk and the test site -- and wanted
	the two within a tenth of a unit. On a half milligram they are a whole unit apart whenever
	the float sits on the side banker's rounding does not choose. Over every 1 mg source from
	0.001 to 50 g and every pair of kg-gk's 14 purity masters, that refused 11,083 of 9,095,581
	correct single-mode documents (2,809 adding alloy, 8,274 releasing it) and 2,753 of
	4,547,809 multiple-mode ones; the checks now refuse none. The stored alloy is shared out as
	it stands, and Target Qty compared with source plus it within one unit.
	"""

	def setUp(self):
		if not hasattr(frappe, "db") or not frappe.db:
			frappe.db = MagicMock()
		# The site's rounding, not the harness's round(): these documents exist because the
		# two differ.
		self._patches = [
			patch.object(mc, "flt", _bankers_flt),
			patch.object(lanes_mod, "flt", _bankers_flt),
		]
		for p in self._patches:
			p.start()

	@staticmethod
	def _alloy_fields(alloy):
		"""What the form stores from calculate_metal_conversion's alloy (metal_conversions.js)."""
		if alloy > 0:
			return {
				"source_alloy": _ALLOY,
				"source_alloy_qty": str(alloy),
				"alloy_batch_details": [_alloc(alloy, "AL1")],
			}
		return {"target_alloy": "talloy", "target_alloy_qty": abs(alloy)}

	@staticmethod
	def _qtys(se):
		"""(source, alloy, target) quantities of a one-lane voucher, in milligrams."""
		source = [
			_mg(r.qty)
			for r in se.items
			if not r.get("t_warehouse") and r.item_code not in (_ALLOY, "talloy")
		]
		alloy = [_mg(r.qty) for r in se.items if r.item_code in (_ALLOY, "talloy")]
		target = [
			_mg(r.qty)
			for r in se.items
			if r.get("t_warehouse") and r.item_code != "talloy"
		]
		return source, alloy, target

	def test_every_half_milligram_document_converts(self):
		"""22KT and 99.5% gold into 18KT, 91.6% and fine: alloy added and alloy released. The
		first 15 half-milligram documents of each pair post their target at exactly source
		plus (or less) the stored alloy, within a milligram of Target Qty -- and every pair
		includes documents the old check refused."""
		for source_purity, target_purity in (
			("91.75", "75.4"),
			("99.5", "91.6"),
			("91.75", "100"),
			("99.5", "100"),
		):
			refused_before = 0
			for source_qty, target_qty, alloy in _half_mg_documents(
				source_purity, target_purity, 15
			):
				derived = _bankers_flt(_bankers_flt(target_qty, 3) - source_qty, 3)
				refused_before += abs(abs(derived) - abs(alloy)) > 1e-4
				with self.subTest(
					source=source_qty, purities=(source_purity, target_purity)
				):
					se = self._build(
						self._doc(
							source_batch_details=[_alloc(source_qty, "CG")],
							source_qty=source_qty,
							target_qty=target_qty,
							**self._alloy_fields(alloy),
						)
					)
					source, alloy_rows, target = self._qtys(se)
					self.assertEqual(source, [_mg(source_qty)])
					self.assertEqual(alloy_rows, [_mg(abs(alloy))])
					self.assertEqual(target, [_mg(source_qty) + _mg(alloy)])
					self.assertLessEqual(abs(target[0] - target_qty * 1000), 1 + 1e-6)
			self.assertGreater(refused_before, 0, (source_purity, target_purity))

	def test_gk_mcon00656_converts(self):
		"""gk MCON00656: 4 g at 100% into 75.4% stores Target Qty 5.305039788 and Source Alloy
		Qty "1.305". At gk's float precision 2 the builder made the target 5.31 g, the alloy
		1.31 g against the stored 1.30 g, and refused it; it posts at transfer_qty's three
		decimals now: 4.000 + 1.305 = 5.305 g."""
		doc = self._doc(
			source_batch_details=[_alloc(4.0, "CG")],
			source_qty=4.0,
			target_qty=5.305039788,
			source_alloy=_ALLOY,
			source_alloy_qty="1.305",
			alloy_batch_details=[_alloc(1.305, "AL1")],
		)
		doc.precision = lambda fieldname: 2
		se = self._build(doc)
		self.assertEqual(
			[(r.qty, r.custom_conversion_lane) for r in se.items],
			[
				(4.0, "Customer Goods|TNCU0001|CG"),
				(1.305, "Customer Goods|TNCU0001|CG"),
				(5.305, "Customer Goods|TNCU0001|CG"),
			],
		)

	def test_quantities_post_at_the_rows_precision_not_the_documents(self):
		"""At float precision 2 the builder booked a 0.477 g batch as 0.48 g and overdrew it.
		0.477 g at 100% into 91.6%: 0.520742 g, alloy round(0.043742, 3) = 0.044 g."""
		doc = self._doc(
			source_batch_details=[_alloc(0.477, "CG")],
			source_qty=0.477,
			target_qty=0.477 * 100 / 91.6,
			source_alloy=_ALLOY,
			source_alloy_qty="0.044",
			alloy_batch_details=[_alloc(0.044, "AL1")],
		)
		doc.precision = lambda fieldname: 2
		self.assertEqual(self._qtys(self._build(doc)), ([477], [44], [521]))
		self.assertEqual(mc._qty_precision(), 3)


class TestBeforeValidate(IntegrationTestCase):
	"""Two document-level fixes from the adversarial review."""

	@classmethod
	def setUpClass(cls):
		pass

	@staticmethod
	def _before_validate(doc, lane_map=None):
		with (
			patch.object(mc, "update_batch_details"),
			patch.object(mc, "get_batch_lane_map", return_value=lane_map or {}),
		):
			doc.before_validate()
		return doc

	def test_a_draft_never_keeps_a_stock_entry_link(self):
		"""Only a submit writes the link, so a draft that names an entry -- an API caller's, or
		a copy's -- drops it. (Amend's copy is dropped by the form on load: Frappe checks
		links before this hook runs; see the amend test in test_metal_conversion_batch_isolation.)"""
		doc = frappe.new_doc("Metal Conversions")
		doc.stock_entry = "MAT-STE-OLD"
		self.assertIsNone(self._before_validate(doc).stock_entry)

	def test_a_submitted_conversion_keeps_its_link(self):
		doc = frappe.new_doc("Metal Conversions")
		doc.stock_entry = "MAT-STE-LIVE"
		doc.docstatus = 1
		self.assertEqual(self._before_validate(doc).stock_entry, "MAT-STE-LIVE")

	def test_each_source_row_shows_its_batchs_ownership(self):
		"""The form types a row from the entry that minted its batch -- blank for a mixed
		conversion -- so a customer's converted batch arrived as "Regular Stock", read-only.
		A row without a batch is left as it is."""
		doc = frappe.new_doc("Metal Conversions")
		doc.multiple_metal_converter = 1
		doc.append(
			"mc_source_table", {"batch": "B-CUST", "inventory_type": "Regular Stock"}
		)
		doc.append(
			"mc_source_table",
			{"batch": "B-REG", "inventory_type": "Customer Goods", "customer": "X"},
		)
		doc.append(
			"mc_source_table", {"inventory_type": "Customer Goods", "customer": "Y"}
		)
		self._before_validate(
			doc,
			{
				"B-CUST": ("Customer Goods", "CUST-A"),
				"B-REG": ("Regular Stock", None),
			},
		)
		self.assertEqual(
			[(r.batch, r.inventory_type, r.customer) for r in doc.mc_source_table],
			[
				("B-CUST", "Customer Goods", "CUST-A"),
				("B-REG", "Regular Stock", None),
				(None, "Customer Goods", "Y"),
			],
		)


class TestOwnershipIsTheBatchs(IntegrationTestCase):
	"""The multiple converter's dispatch reads the batch first (row_ownership rule 1)."""

	LANE_MAP = {"CUST": ("Customer Goods", "CUST-A"), "REG": ("Regular Stock", None)}

	@classmethod
	def setUpClass(cls):
		pass

	def _customer_owned(self, **row):
		return mc._has_customer_owned_source([frappe._dict(row)], self.LANE_MAP)

	def test_a_customer_batch_typed_as_company_stock_is_customer_metal(self):
		self.assertTrue(
			self._customer_owned(batch="CUST", inventory_type="Regular Stock")
		)

	def test_a_company_batch_typed_as_customer_goods_is_company_metal(self):
		self.assertFalse(
			self._customer_owned(batch="REG", inventory_type="Customer Goods")
		)

	def test_a_row_without_a_batch_is_judged_by_its_own_type(self):
		self.assertTrue(
			self._customer_owned(batch=None, inventory_type="Customer Goods")
		)
		self.assertFalse(
			self._customer_owned(batch=None, inventory_type="Regular Stock")
		)


from jewellery_erpnext.patches import (  # noqa: E402
	metal_conversion_system_manager_cancel as sm_patch,
)


class TestSystemManagerCanCancel(IntegrationTestCase):
	"""Decided 29 Sep 2026: System Manager cancels -- and amends -- a Metal Conversion.

	The conversion's own cancel is the only way to reverse its Stock Entry now that the link is
	saved, and on kg-gk and gk no role could cancel one. The standard rule carries the rights; a
	site that keeps Custom DocPerms for the doctype ignores it, so the patch adds them there.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		frappe.db.savepoint("mc_system_manager")

	def tearDown(self):
		frappe.db.rollback(save_point="mc_system_manager")
		frappe.clear_cache(doctype=sm_patch.DOCTYPE)

	def _rule(self, if_owner=0):
		return frappe.db.get_value(
			"Custom DocPerm",
			{
				"parent": sm_patch.DOCTYPE,
				"role": sm_patch.ROLE,
				"permlevel": 0,
				"if_owner": if_owner,
			},
			list(sm_patch.RIGHTS),
			as_dict=True,
		)

	def test_the_standard_rule_gives_the_rights(self):
		with open(_DOCTYPE_JSON) as f:
			rules = json.load(f)["permissions"]
		rule = next(
			r for r in rules if r["role"] == sm_patch.ROLE and not r.get("if_owner")
		)
		self.assertEqual(
			{right: rule.get(right) for right in sm_patch.RIGHTS},
			dict.fromkeys(sm_patch.RIGHTS, 1),
		)

	def test_a_site_with_custom_permissions_gets_them_too(self):
		"""As on kg-gk and gk: Custom DocPerms, and System Manager without Submit, Cancel or
		Amend. Run twice: the second run changes nothing."""
		from frappe.permissions import setup_custom_perms

		setup_custom_perms(sm_patch.DOCTYPE)
		frappe.db.set_value(
			"Custom DocPerm",
			{"parent": sm_patch.DOCTYPE, "role": sm_patch.ROLE, "if_owner": 0},
			{"submit": 0, "cancel": 0, "amend": 0},
		)
		for _run in range(2):
			sm_patch.execute()
			self.assertEqual(self._rule(), dict.fromkeys(sm_patch.RIGHTS, 1))
		self.assertEqual(
			frappe.db.count(
				"Custom DocPerm",
				{"parent": sm_patch.DOCTYPE, "role": sm_patch.ROLE},
			),
			1,
		)

	def test_a_missing_rule_is_added_and_a_creator_only_rule_left_alone(self):
		from frappe.permissions import setup_custom_perms

		setup_custom_perms(sm_patch.DOCTYPE)
		frappe.db.delete(
			"Custom DocPerm", {"parent": sm_patch.DOCTYPE, "role": sm_patch.ROLE}
		)
		frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": sm_patch.DOCTYPE,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": sm_patch.ROLE,
				"permlevel": 0,
				"if_owner": 1,
				"read": 1,
			}
		).insert(ignore_permissions=True)
		sm_patch.execute()
		self.assertEqual(self._rule(), dict.fromkeys(sm_patch.RIGHTS, 1))
		self.assertEqual(
			self._rule(if_owner=1),
			{**dict.fromkeys(sm_patch.RIGHTS, 0), "read": 1},
		)

	def test_a_site_without_custom_permissions_is_left_to_the_standard_rule(self):
		frappe.db.delete("Custom DocPerm", {"parent": sm_patch.DOCTYPE})
		sm_patch.execute()
		self.assertFalse(
			frappe.db.exists("Custom DocPerm", {"parent": sm_patch.DOCTYPE})
		)
