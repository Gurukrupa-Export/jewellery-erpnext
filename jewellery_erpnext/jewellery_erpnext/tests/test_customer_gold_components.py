# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""C09 -- unit tests for component-level provenance.

These are pure-function tests over the resolver. The two site-facing readers
(``_recorded_components`` and ``_batch_identity``) are the seam: they are patched, and
everything above them -- apportionment, recursion, merging, the ownership rule -- is exercised
for real. That is deliberate. The arithmetic is where C09's claims live, and it is the part a
fixture chain would obscure rather than prove.

The carve-out that CONSUMES these numbers is tested in
``doctype/metal_conversions/test_metal_conversions.py``; the recursive behaviour against a real
database is in ``tests/test_customer_gold_integration.py``.
"""

import unittest
from unittest.mock import patch

import frappe

from jewellery_erpnext.customer_subcontracting import customer_gold_components as cgc

CUSTOMER = "TNCU0001"
OTHER = "TNCU0002"


def _component(
	qty,
	customer=None,
	inventory_type=None,
	source_batch=None,
	pure_qty=None,
	item="M-G-24KT",
):
	return {
		"item_code": item,
		"inventory_type": inventory_type
		or (cgc.CUSTOMER_GOODS if customer else cgc.REGULAR_STOCK),
		"customer": customer,
		"qty": qty,
		"pure_qty": qty if pure_qty is None else pure_qty,
		"source_batch": source_batch,
		"source_voucher_type": None,
		"source_voucher_detail_no": None,
		"rate": None,
		"amount": None,
	}


class _ComponentsTestCase(unittest.TestCase):
	"""Patches the two DB readers with an in-memory batch graph."""

	#: batch -> component rows. A batch absent from this map is atomic.
	graph = {}
	#: batch -> identity fields, for atomic batches.
	identities = {}

	def setUp(self):
		self._patches = [
			patch.object(
				cgc,
				"_recorded_components",
				side_effect=lambda b: list(self.graph.get(b, [])),
			),
			patch.object(
				cgc,
				"_batch_identity",
				side_effect=lambda b: frappe._dict(self.identities.get(b, {})),
			),
		]
		for p in self._patches:
			p.start()
		self.addCleanup(lambda: [p.stop() for p in self._patches])


class TestAtomicBatches(_ComponentsTestCase):
	"""A batch nothing has been recorded about is one component: itself."""

	def setUp(self):
		self.graph = {}
		self.identities = {
			"B-CG": {
				"custom_inventory_type": cgc.CUSTOMER_GOODS,
				"custom_customer": CUSTOMER,
				"custom_pure_metal_qty": 99.9,
				"batch_qty": 100.0,
				"item": "M-G-24KT",
			}
		}
		super().setUp()

	def test_an_unrecorded_batch_resolves_to_itself(self):
		out = cgc.resolve_components("B-CG", 40.0)
		self.assertEqual(len(out), 1)
		self.assertEqual(out[0]["customer"], CUSTOMER)
		self.assertEqual(out[0]["qty"], 40.0)
		self.assertEqual(out[0]["source_batch"], "B-CG")

	def test_fine_grams_are_apportioned_from_the_batch_not_recomputed(self):
		"""99.9 fine in 100 gross; drawing 40 gross draws 39.96 fine.

		Apportioned from the batch's own recorded fine quantity rather than recomputed from a
		purity attribute -- deliberately. The two in-repo purity helpers disagree about the
		configured item (100.0 one way, 99.9 the other; the live-master defect recorded as
		D04), so recomputing would silently adopt whichever happened to be imported.
		"""
		out = cgc.resolve_components("B-CG", 40.0)
		self.assertAlmostEqual(out[0]["pure_qty"], 39.96, places=3)

	def test_drawing_nothing_resolves_to_nothing(self):
		self.assertEqual(cgc.resolve_components("B-CG", 0), [])
		self.assertEqual(cgc.resolve_components("B-CG", -5), [])
		self.assertEqual(cgc.resolve_components(None, 10), [])

	def test_an_unknown_batch_defaults_to_regular_stock(self):
		"""Fail closed on ownership: unknown metal is the company's until recorded otherwise,
		never a customer's. Guessing a customer would create a liability from nothing."""
		out = cgc.resolve_components("B-NOBODY", 10.0)
		self.assertEqual(out[0]["inventory_type"], cgc.REGULAR_STOCK)
		self.assertIsNone(out[0]["customer"])


class TestMixedBatches(_ComponentsTestCase):
	"""The C09 case: customer metal and company alloy inside one batch."""

	def setUp(self):
		# 20 g customer 24KT + 1.763 g company alloy -- the exact shape already present in
		# production data on `gk`.
		self.graph = {
			"B-MIX": [
				_component(20.0, customer=CUSTOMER, source_batch="B-CG"),
				_component(1.763, source_batch="B-AL", item="M-AL"),
			]
		}
		self.identities = {}
		super().setUp()

	def test_components_are_apportioned_not_passed_through_whole(self):
		"""Gap 3. Drawing half the batch draws half of each component -- the existing
		origin-entry writer hands the FULL source list to every inward row instead."""
		total = 21.763
		out = cgc.resolve_components("B-MIX", total / 2)

		self.assertEqual(len(out), 2)
		self.assertAlmostEqual(out[0]["qty"], 10.0, places=2)
		self.assertAlmostEqual(out[1]["qty"], 0.882, places=2)
		self.assertAlmostEqual(sum(c["qty"] for c in out), total / 2, places=2)

	def test_ownership_survives_apportionment(self):
		out = cgc.resolve_components("B-MIX", 10.0)
		by_owner = {c["customer"]: c for c in out}
		self.assertIn(CUSTOMER, by_owner)
		self.assertIn(None, by_owner)
		self.assertEqual(by_owner[CUSTOMER]["inventory_type"], cgc.CUSTOMER_GOODS)
		self.assertEqual(by_owner[None]["inventory_type"], cgc.REGULAR_STOCK)

	def test_the_whole_batch_resolves_to_the_whole_component_list(self):
		out = cgc.resolve_components("B-MIX", 21.763)
		self.assertAlmostEqual(sum(c["qty"] for c in out), 21.763, places=3)

	def test_company_component_qty_reads_the_company_share(self):
		self.assertAlmostEqual(
			cgc.get_component_qty("B-MIX", inventory_type=cgc.REGULAR_STOCK),
			1.763,
			places=3,
		)
		self.assertAlmostEqual(
			cgc.get_component_qty("B-MIX", inventory_type=cgc.CUSTOMER_GOODS),
			20.0,
			places=3,
		)

	def test_component_qty_can_be_filtered_by_customer(self):
		self.assertAlmostEqual(
			cgc.get_component_qty("B-MIX", customer=CUSTOMER), 20.0, places=3
		)
		self.assertEqual(cgc.get_component_qty("B-MIX", customer=OTHER), 0.0)

	def test_an_unrecorded_batch_reports_no_company_component(self):
		"""The default that makes the Metal Conversion carve-out safe to ship."""
		self.assertEqual(cgc.get_company_component_qty("B-NOTHING"), 0.0)
		self.assertEqual(cgc.get_company_component_qty([]), 0.0)

	def test_company_component_sums_across_batches(self):
		self.assertAlmostEqual(
			cgc.get_company_component_qty(["B-MIX", "B-MIX"]), 3.526, places=3
		)


class TestTransitivity(_ComponentsTestCase):
	"""CG-T083. A batch made from a mixed batch inherits its COMPONENTS, not its name."""

	def setUp(self):
		self.graph = {
			# Second-generation batch: made entirely from the mixed batch below.
			"B-GEN2": [_component(21.763, customer=CUSTOMER, source_batch="B-MIX")],
			"B-MIX": [
				_component(20.0, customer=CUSTOMER, source_batch="B-CG"),
				_component(1.763, source_batch="B-AL", item="M-AL"),
			],
		}
		self.identities = {}
		super().setUp()

	def test_a_second_generation_batch_sees_through_to_the_originals(self):
		"""Without recursion this returns one row saying "all of it came from B-MIX", and the
		company alloy inside it becomes invisible -- which is precisely the reverse-conversion
		blind spot C09 names."""
		out = cgc.resolve_components("B-GEN2", 21.763)

		self.assertEqual(len(out), 2, f"provenance stopped one level down: {out}")
		sources = {c["source_batch"] for c in out}
		self.assertEqual(sources, {"B-CG", "B-AL"})
		self.assertNotIn("B-MIX", sources)

	def test_the_company_share_is_still_visible_two_generations_down(self):
		self.assertAlmostEqual(
			cgc.get_company_component_qty("B-GEN2"),
			0.0,
			places=3,
		)  # recorded row on GEN2 names the customer; the company share is found by resolving
		out = cgc.resolve_components("B-GEN2", 21.763)
		company = sum(c["qty"] for c in out if not c["customer"])
		self.assertAlmostEqual(company, 1.763, places=2)

	def test_partial_draw_apportions_through_both_levels(self):
		out = cgc.resolve_components("B-GEN2", 10.8815)  # half
		self.assertAlmostEqual(sum(c["qty"] for c in out), 10.8815, places=2)
		company = sum(c["qty"] for c in out if not c["customer"])
		self.assertAlmostEqual(company, 0.882, places=2)


class TestTerminationGuards(_ComponentsTestCase):
	"""A malformed graph must degrade, never hang. An infinite loop inside a Stock Entry
	submit is not an acceptable failure mode, and 'acyclic by construction' has been wrong in
	this codebase before."""

	def setUp(self):
		self.graph = {
			"B-A": [_component(10.0, customer=CUSTOMER, source_batch="B-B")],
			"B-B": [_component(10.0, customer=CUSTOMER, source_batch="B-A")],
		}
		self.identities = {}
		super().setUp()

	def test_a_cycle_terminates(self):
		out = cgc.resolve_components("B-A", 10.0)
		self.assertTrue(out)
		self.assertAlmostEqual(sum(c["qty"] for c in out), 10.0, places=3)

	def test_a_deep_chain_terminates_at_the_depth_limit(self):
		self.graph = {
			f"B-{i}": [_component(10.0, customer=CUSTOMER, source_batch=f"B-{i + 1}")]
			for i in range(cgc.MAX_DEPTH + 5)
		}
		out = cgc.resolve_components("B-0", 10.0)
		self.assertTrue(out)
		self.assertAlmostEqual(sum(c["qty"] for c in out), 10.0, places=3)


class TestMerging(unittest.TestCase):
	"""Gap 2: the existing writer de-dupes on ``batch_no`` alone and loses facts."""

	def test_identical_components_merge_and_quantities_add(self):
		merged = cgc.merge_components(
			[
				_component(5.0, customer=CUSTOMER, source_batch="B-CG"),
				_component(3.0, customer=CUSTOMER, source_batch="B-CG"),
			]
		)
		self.assertEqual(len(merged), 1)
		self.assertAlmostEqual(merged[0]["qty"], 8.0, places=3)
		self.assertAlmostEqual(merged[0]["pure_qty"], 8.0, places=3)

	def test_same_batch_different_owner_does_not_merge(self):
		"""The key is the WHOLE identity, not the batch. Two owners is two facts -- collapsing
		them is exactly how the existing origin-entry writer loses a contribution."""
		merged = cgc.merge_components(
			[
				_component(5.0, customer=CUSTOMER, source_batch="B-X"),
				_component(3.0, customer=OTHER, source_batch="B-X"),
			]
		)
		self.assertEqual(len(merged), 2)

	def test_same_batch_different_item_does_not_merge(self):
		merged = cgc.merge_components(
			[
				_component(5.0, source_batch="B-X", item="M-G-24KT"),
				_component(3.0, source_batch="B-X", item="M-AL"),
			]
		)
		self.assertEqual(len(merged), 2)

	def test_order_is_stable(self):
		"""Rows are read back by ``idx``; a merge that reorders would make the stored table
		differ from run to run for the same inputs."""
		merged = cgc.merge_components(
			[
				_component(1.0, source_batch="B-3"),
				_component(1.0, source_batch="B-1"),
				_component(1.0, source_batch="B-2"),
				_component(1.0, source_batch="B-1"),
			]
		)
		self.assertEqual([m["source_batch"] for m in merged], ["B-3", "B-1", "B-2"])
