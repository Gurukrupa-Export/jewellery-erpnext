# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for the Metal Conversions ownership-lane split.

Pure-logic tests: no DB / no Frappe site (``setUpClass`` is neutralised per the
suite's convention), ``SimpleNamespace`` stand-in docs, every DB call patched.

The feature: ``is_customer_metal`` is gone. FIFO draws across whatever ownerships
the source warehouse holds, and the single source qty splits into one lane per
``(inventory_type, customer)``. Each lane converts to the target purity on its own
and lands in its own target batch, all inside ONE Stock Entry whose rows read
source/target/source/target and carry ``custom_conversion_lane``.

Worked example (the requirement): 20 g of 24KT -> 18KT with 8 g Regular + 12 g
Customer Goods available gives 8 -> 10.667 Regular and 12 -> 16 Customer Goods.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions import (
	metal_conversions as mc,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events import (
	lanes as lanes_mod,
)

_MC_PATH = (
	"jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions"
)


def _det_flt(value, precision=None, rounding_method=None):
	"""Deterministic stand-in for frappe.utils.flt (see the melting-loss suite)."""
	try:
		num = float(value or 0)
	except (TypeError, ValueError):
		return 0.0
	return round(num, precision) if precision is not None else num


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

	def test_two_customers_make_three_lanes(self):
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

		# Ordered by FIRST appearance, and A1/A2 merge into one lane.
		self.assertEqual(
			[(l["inventory_type"], l["customer"], l["source_qty"]) for l in result],
			[
				("Customer Goods", "CUST-A", 4.0),
				("Regular Stock", None, 2.0),
				("Customer Goods", "CUST-B", 5.0),
			],
		)
		self.assertEqual([b["batch"] for b in result[0]["batches"]], ["A1", "A2"])

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
			sum(l["alloy_qty"] for l in lanes), 26.667 - 20.0, places=9
		)

	def test_alloy_sums_to_total_even_with_awkward_rounding(self):
		lanes = lanes_mod.split_conversion(self._lanes(1.0, 1.0, 1.0), 10.0, 3)
		self.assertAlmostEqual(sum(l["target_qty"] for l in lanes), 10.0, places=9)
		self.assertAlmostEqual(sum(l["alloy_qty"] for l in lanes), 7.0, places=9)

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
			sum(l["alloy_qty"] for l in lanes), total_target - total_source, places=9
		)

	def test_alloy_sign_is_uniform_across_lanes(self):
		"""Source and target purity are shared, so no lane can be opposite."""
		# Purity up: target < source -> every lane's alloy is <= 0.
		lanes = lanes_mod.split_conversion(self._lanes(8.0, 12.0), 15.0, 3)
		self.assertTrue(all(l["alloy_qty"] <= 0 for l in lanes))
		# Purity down: target > source -> every lane's alloy is >= 0.
		lanes = lanes_mod.split_conversion(self._lanes(8.0, 12.0), 26.667, 3)
		self.assertTrue(all(l["alloy_qty"] >= 0 for l in lanes))

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


class TestMakeMetalStockEntry(IntegrationTestCase):
	"""One voucher, rows grouped lane by lane, every row carrying its lane tag."""

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

	def _build(self, doc, lane_map=None):
		lane_map = lane_map or {
			"REG": ("Regular Stock", None),
			"CG": ("Customer Goods", "TNCU0001"),
			"AL1": ("Regular Stock", None),
		}
		captured = {}

		def _get_doc(payload):
			se = _FakeSE(payload)
			captured["se"] = se
			return se

		with (
			patch("frappe.get_doc", side_effect=_get_doc),
			patch("frappe.new_doc", side_effect=lambda dt: _FakeSE({})),
			patch.object(mc, "get_batch_lane_map", return_value=lane_map),
			patch(
				"jewellery_erpnext.jewellery_erpnext.lock_order.preallocate_series_for_docs"
			),
			patch("jewellery_erpnext.jewellery_erpnext.lock_order.lock_bins"),
		):
			mc.make_metal_stock_entry(doc)

		return captured["se"]

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
		self.assertEqual(cg_source.custom_conversion_lane, "Customer Goods|TNCU0001")
		self.assertEqual(cg_target.custom_conversion_lane, "Customer Goods|TNCU0001")

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
			["Regular Stock|", "Customer Goods|TNCU0001"],
		)
		self.assertAlmostEqual(sum(r.qty for r in alloy_rows), 6.667, places=9)

	def test_target_alloy_belongs_to_its_lane_including_the_customer(self):
		doc = self._two_lane_doc(target_alloy="talloy", target_alloy_qty=5.0)
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

	def test_one_lane_per_ownership_not_per_batch(self):
		"""Two batches of the same ownership are ONE lane, hence one target row."""
		doc = self._doc(
			source_batch_details=[
				_alloc(5.0, "CG"),
				_alloc(8.0, "REG"),
				_alloc(7.0, "CG2"),
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
		self.assertEqual(len(targets), 2)
		# Lanes keep FIFO first-appearance order: Customer Goods was seen first.
		self.assertEqual(targets[0].customer, "TNCU0001")
		self.assertIsNone(targets[1].customer)
		# 12 g of the customer's metal across two batches -> one 16 g target row.
		self.assertEqual(targets[0].qty, 16.0)
		self.assertEqual(targets[1].qty, 10.667)

	def test_saved_submitted_and_linked_back(self):
		doc = self._two_lane_doc()
		se = self._build(doc)
		self.assertTrue(se.saved)
		self.assertTrue(se.submitted)
		self.assertEqual(doc.stock_entry, "SE-CONV-0001")

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
