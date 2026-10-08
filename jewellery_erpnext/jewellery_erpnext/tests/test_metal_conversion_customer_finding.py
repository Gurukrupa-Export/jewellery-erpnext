# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Metal Conversions: Customer Metal converts only into Metal, never into a Finding.

Regular Metal may become Metal or a Finding. Single mode draws its source FIFO, so a Finding
target must skip customer batches in the draw (``update_source_betch``); both modes are then
guarded by ``validate_customer_metal_target``. Every DB call is mocked.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events import (
	utils as mc_utils,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events.utils import (
	is_finding_item,
	update_source_betch,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions import (
	validate_customer_metal_target,
)

MC_MODULE = (
	"jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions"
)
UTILS_MODULE = mc_utils.__name__

# Codes deliberately don't follow the M/F prefix: the template comes from Item.variant_of.
METAL = "GOLD-22KT-91.9-Y"
FINDING = "CHAIN-22KT-91.9-Y"
F_NAMED_METAL = "F-NAMED-METAL"

VARIANT_OF = {METAL: "M", FINDING: "F", F_NAMED_METAL: "M"}

LANE_MAP = {
	"B-CUST": ("Customer Goods", "CUST-1"),
	"B-REG": ("Regular Stock", None),
}


class _Doc(frappe._dict):
	"""A Metal Conversions stand-in: ``append`` adds a child row like the real Document."""

	def append(self, table, row):
		self.setdefault(table, []).append(frappe._dict(row))


def _single(target_item, batches=(), **extra):
	return _Doc(
		multiple_metal_converter=0,
		source_item=METAL,
		target_item=target_item,
		source_warehouse="RM - GE",
		source_batch_details=[frappe._dict(batch=b, qty=1) for b in batches],
		**extra,
	)


def _multiple(target_item, rows):
	return _Doc(
		multiple_metal_converter=1,
		m_target_item=target_item,
		mc_source_table=[frappe._dict(row) for row in rows],
	)


def _lane_map(batch_nos):
	return {b: LANE_MAP[b] for b in batch_nos if b in LANE_MAP}


def _cached_value(doctype, name, fieldname):
	assert (doctype, fieldname) == ("Item", "variant_of")
	return VARIANT_OF.get(name)


#: is_finding_item reads the Item record through frappe.get_cached_value.
_ITEM_VARIANT = patch("frappe.get_cached_value", side_effect=_cached_value)


@_ITEM_VARIANT
class TestIsFindingItem(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_variant_of_selects_finding(self, _variant):
		self.assertTrue(is_finding_item(FINDING))
		self.assertFalse(is_finding_item(METAL))

	def test_item_code_prefix_is_ignored(self, _variant):
		self.assertFalse(is_finding_item(F_NAMED_METAL))

	def test_blank_item_reads_nothing(self, _variant):
		self.assertFalse(is_finding_item(None))
		self.assertFalse(is_finding_item(""))
		_variant.assert_not_called()


@_ITEM_VARIANT
@patch(f"{MC_MODULE}.get_batch_lane_map", side_effect=_lane_map)
class TestValidateCustomerMetalTarget(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_single_customer_metal_to_finding_refused(self, _map, _variant):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_metal_target(_single(FINDING, ["B-REG", "B-CUST"]))

	def test_single_customer_metal_to_metal_allowed(self, _map, _variant):
		validate_customer_metal_target(_single(METAL, ["B-CUST"]))

	def test_single_regular_metal_to_finding_allowed(self, _map, _variant):
		validate_customer_metal_target(_single(FINDING, ["B-REG"]))

	def test_melting_loss_out_of_scope(self, _map, _variant):
		validate_customer_metal_target(_single(FINDING, ["B-CUST"], is_melting_loss=1))

	def test_multiple_customer_batch_to_finding_refused(self, _map, _variant):
		doc = _multiple(FINDING, [{"batch": "B-REG"}, {"batch": "B-CUST"}])
		with self.assertRaises(frappe.ValidationError):
			validate_customer_metal_target(doc)

	def test_multiple_batch_ownership_beats_row_type(self, _map, _variant):
		# A customer batch typed "Regular Stock" on its row is still customer metal.
		doc = _multiple(
			FINDING, [{"batch": "B-CUST", "inventory_type": "Regular Stock"}]
		)
		with self.assertRaises(frappe.ValidationError):
			validate_customer_metal_target(doc)

	def test_multiple_unbatched_customer_row_refused(self, _map, _variant):
		doc = _multiple(FINDING, [{"inventory_type": "Customer Stock"}])
		with self.assertRaises(frappe.ValidationError):
			validate_customer_metal_target(doc)

	def test_multiple_regular_to_finding_allowed(self, _map, _variant):
		validate_customer_metal_target(_multiple(FINDING, [{"batch": "B-REG"}]))

	def test_multiple_customer_to_metal_allowed(self, _map, _variant):
		validate_customer_metal_target(_multiple(METAL, [{"batch": "B-CUST"}]))


@_ITEM_VARIANT
@patch(f"{UTILS_MODULE}.get_sample_batches", return_value=set())
@patch(f"{UTILS_MODULE}.get_batch_lane_map", side_effect=_lane_map)
@patch(f"{UTILS_MODULE}.capped_auto_batch_nos")
class TestSingleModeDrawForFinding(IntegrationTestCase):
	"""The customer batch is oldest, so a plain FIFO draw would take it first."""

	@classmethod
	def setUpClass(cls):
		pass

	def _batches(self, capped, reg_qty=5):
		capped.return_value = [
			frappe._dict(batch_no="B-CUST", qty=5),
			frappe._dict(batch_no="B-REG", qty=reg_qty),
		]

	def test_finding_target_skips_customer_batches(
		self, capped, _map, _samples, _variant
	):
		self._batches(capped)
		doc = _single(FINDING, source_qty=3)
		update_source_betch(doc)
		self.assertEqual([r.batch for r in doc.source_batch_details], ["B-REG"])
		self.assertEqual(doc.source_batch_details[0].qty, 3)

	def test_metal_target_still_draws_fifo_across_owners(
		self, capped, _map, _samples, _variant
	):
		self._batches(capped)
		doc = _single(METAL, source_qty=7)
		update_source_betch(doc)
		self.assertEqual(
			[r.batch for r in doc.source_batch_details], ["B-CUST", "B-REG"]
		)

	def test_finding_target_short_of_regular_stock_refused(
		self, capped, _map, _samples, _variant
	):
		self._batches(capped, reg_qty=2)
		doc = _single(FINDING, source_qty=3)
		with self.assertRaises(frappe.ValidationError) as ctx:
			update_source_betch(doc)
		self.assertIn("Regular Stock", str(ctx.exception))
