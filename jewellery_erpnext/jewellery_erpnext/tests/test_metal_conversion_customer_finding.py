# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Metal Conversions restrictions on the target item. Every DB call is mocked.

Customer Metal converts only into Metal, never into a Finding; Regular Metal may become either.
Single mode draws its source FIFO, so a Finding target must skip customer batches in the draw
(``update_source_betch``); both modes are then guarded by ``validate_customer_metal_target``.
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
	validate_source_target_differ,
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


def _items_single(source_item, target_item, **extra):
	return _Doc(
		multiple_metal_converter=0,
		source_item=source_item,
		target_item=target_item,
		**extra,
	)


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

	def test_multiple_converter_not_checked(self, _map, _variant):
		# Only the single converter is used; the multiple converter is left as it was.
		validate_customer_metal_target(_multiple(FINDING, [{"batch": "B-CUST"}]))
		_map.assert_not_called()
		_variant.assert_not_called()


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


class TestValidateSourceTargetDiffer(IntegrationTestCase):
	"""MCON00379: M-G-22KT-91.75-Y converted into itself only re-batched the metal."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_metal_into_itself_refused(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_source_target_differ(_items_single(METAL, METAL))
		self.assertIn("Source Item and Target Item are the same", str(ctx.exception))

	def test_finding_into_itself_refused(self):
		with self.assertRaises(frappe.ValidationError):
			validate_source_target_differ(_items_single(FINDING, FINDING))

	def test_different_items_allowed(self):
		validate_source_target_differ(_items_single(METAL, FINDING))

	def test_blank_items_left_to_mandatory_checks(self):
		validate_source_target_differ(_items_single(None, None))

	def test_melting_loss_out_of_scope(self):
		validate_source_target_differ(_items_single(METAL, METAL, is_melting_loss=1))

	def test_multiple_converter_not_checked(self):
		validate_source_target_differ(
			_Doc(multiple_metal_converter=1, source_item=METAL, target_item=METAL)
		)


@_ITEM_VARIANT
@patch(f"{UTILS_MODULE}.frappe.msgprint")
@patch(f"{UTILS_MODULE}.get_sample_batches", return_value={"B-SAMPLE"})
@patch(f"{UTILS_MODULE}.get_batch_lane_map", side_effect=_lane_map)
@patch(f"{UTILS_MODULE}.capped_auto_batch_nos")
class TestSingleModeEnteredBatches(IntegrationTestCase):
	"""Batches entered in Source Batch Details survive the save instead of the FIFO pick."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		# B-CUST is oldest: a FIFO pick would always start there.
		self.stock = [
			frappe._dict(batch_no="B-CUST", qty=5),
			frappe._dict(batch_no="B-REG", qty=5),
			frappe._dict(batch_no="B-SAMPLE", qty=5),
		]

	def _doc(self, target_item, source_qty, rows, **extra):
		doc = _single(target_item, source_qty=source_qty, **extra)
		doc.source_batch_details = [
			frappe._dict(idx=idx, batch=batch, qty=qty)
			for idx, (batch, qty) in enumerate(rows, 1)
		]
		return doc

	def _refused(self, doc, text):
		with self.assertRaises(frappe.ValidationError) as ctx:
			update_source_betch(doc)
		self.assertIn(text, str(ctx.exception))

	def test_entered_batch_kept_over_fifo(
		self, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = self.stock
		doc = self._doc(METAL, 3, [("B-REG", 3)])
		update_source_betch(doc)
		self.assertEqual(
			[(r.batch, r.qty) for r in doc.source_batch_details], [("B-REG", 3)]
		)
		msgprint.assert_not_called()

	def test_several_entered_batches_kept(
		self, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = self.stock
		doc = self._doc(METAL, 4, [("B-REG", 1.5), ("B-CUST", 2.5)])
		update_source_betch(doc)
		self.assertEqual(
			[(r.batch, r.qty) for r in doc.source_batch_details],
			[("B-REG", 1.5), ("B-CUST", 2.5)],
		)

	def test_blank_rows_dropped_and_renumbered(
		self, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = self.stock
		doc = self._doc(METAL, 3, [(None, 0), ("B-REG", 3)])
		update_source_betch(doc)
		self.assertEqual(
			[(r.idx, r.batch) for r in doc.source_batch_details], [(1, "B-REG")]
		)

	def test_total_mismatch_reallocates_and_says_so(
		self, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = self.stock
		doc = self._doc(METAL, 3, [("B-REG", 2)])
		update_source_betch(doc)
		self.assertEqual([r.batch for r in doc.source_batch_details], ["B-CUST"])
		msgprint.assert_called_once()

	def test_empty_table_uses_fifo(self, capped, _map, _samples, msgprint, _variant):
		capped.return_value = self.stock
		doc = self._doc(METAL, 3, [])
		update_source_betch(doc)
		self.assertEqual([r.batch for r in doc.source_batch_details], ["B-CUST"])
		msgprint.assert_not_called()

	def test_customer_batch_for_finding_refused(
		self, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = self.stock
		self._refused(
			self._doc(FINDING, 3, [("B-CUST", 3)]),
			"Customer Goods cannot be converted into a Finding item",
		)

	def test_customer_batch_for_melting_loss_refused(
		self, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = self.stock
		doc = self._doc(METAL, 10, [("B-CUST", 1)], is_melting_loss=1, loss_qty=1)
		self._refused(doc, "a melting loss can only use Regular Stock")

	def test_batch_without_stock_refused(
		self, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = self.stock
		self._refused(self._doc(METAL, 3, [("B-ELSEWHERE", 3)]), "no stock of")

	def test_more_than_available_refused(
		self, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = self.stock
		self._refused(self._doc(METAL, 6, [("B-REG", 6)]), "only 5.0 is available")

	def test_repeated_batch_refused(self, capped, _map, _samples, msgprint, _variant):
		capped.return_value = self.stock
		self._refused(
			self._doc(METAL, 4, [("B-REG", 2), ("B-REG", 2)]), "entered more than once"
		)

	def test_sample_batch_refused(self, capped, _map, _samples, msgprint, _variant):
		capped.return_value = self.stock
		self._refused(self._doc(METAL, 3, [("B-SAMPLE", 3)]), "Customer Sample Goods")

	def test_row_without_batch_refused(
		self, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = self.stock
		self._refused(self._doc(METAL, 3, [(None, 3)]), "Batch is required")
