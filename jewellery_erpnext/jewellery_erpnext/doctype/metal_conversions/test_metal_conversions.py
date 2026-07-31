# Copyright (c) 2024, Nirali and Contributors
# See license.txt

import os
import json
import frappe
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from frappe.tests import IntegrationTestCase
from frappe.exceptions import ValidationError

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


from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events import (
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
_DOCTYPE_JSON = os.path.join(
	os.path.dirname(mc.__file__), "metal_conversions.json"
)


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
