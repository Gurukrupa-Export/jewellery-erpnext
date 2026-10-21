# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Metal Conversions restrictions on the target item. Every DB call is mocked.

Customer Metal converts only into Metal, never into a Finding; Regular Metal may become either.
Single mode draws its source FIFO, so a Finding target must skip customer batches in the draw
(``update_source_betch``); both modes are then guarded by ``validate_customer_metal_target``.

Finding -> Finding is a resize only (``validate_finding_to_finding``): same Finding Category, a
different Finding Size, every other variant attribute equal.
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
	_finding_resize_errors,
	_variant_attributes,
	get_source_batches,
	validate_customer_finding_source,
	validate_customer_metal_target,
	validate_finding_to_finding,
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


# Finding -> Finding fixtures: one base Finding and one variation per rule.
FND_10 = "CHAIN-KODI-10"
FND_12 = "CHAIN-KODI-12"  # same everything, size 12: the one allowed resize
FND_10_TWIN = "CHAIN-KODI-10-TWIN"  # same everything, same size
FND_LOCK_12 = "LOCK-12"  # other category
FND_12_PINK = "CHAIN-KODI-12-PINK"  # other metal colour
FND_12_BLACK_BEAD = "CHAIN-BLACK-BEAD-12"  # other sub-category
FND_NO_SIZE = "CHAIN-KODI-NO-SIZE"
FND_NO_SIZE_TWIN = "CHAIN-KODI-NO-SIZE-TWIN"

_BASE = {
	"Metal Type": "Gold",
	"Metal Touch": "22KT",
	"Metal Purity": "91.75",
	"Metal Colour": "Yellow",
	"Finding Category": "Chains",
	"Finding Sub-Category": "Kodi Chain",
	"Finding Size": "10.00 MM",
}
ATTRIBUTES = {
	FND_10: _BASE,
	FND_12: {**_BASE, "Finding Size": "12.00 MM"},
	FND_10_TWIN: dict(_BASE),
	FND_LOCK_12: {
		**_BASE,
		"Finding Category": "Locks",
		"Finding Sub-Category": "J Hook Clasp",
		"Finding Size": "12.00 MM",
	},
	FND_12_PINK: {**_BASE, "Metal Colour": "Pink", "Finding Size": "12.00 MM"},
	FND_12_BLACK_BEAD: {
		**_BASE,
		"Finding Sub-Category": "Black Bead Chain",
		"Finding Size": "12.00 MM",
	},
	FND_NO_SIZE: {**_BASE, "Finding Size": None},
	FND_NO_SIZE_TWIN: {**_BASE, "Finding Size": None},
}

VARIANT_OF = {
	METAL: "M",
	FINDING: "F",
	F_NAMED_METAL: "M",
	**{item: "F" for item in ATTRIBUTES},
}

POINT_4_MESSAGE = (
	"Customer Finding batches cannot be used as a source in Metal Conversion."
)

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


def _attributes(item_codes):
	return {item: ATTRIBUTES[item] for item in item_codes if item in ATTRIBUTES}


_ITEM_ATTRIBUTES = patch(f"{MC_MODULE}._variant_attributes", side_effect=_attributes)


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


@_ITEM_ATTRIBUTES
@_ITEM_VARIANT
class TestValidateFindingToFinding(IntegrationTestCase):
	"""The document's example table first, then the edge cases."""

	@classmethod
	def setUpClass(cls):
		pass

	def _refused(self, doc, *expected):
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_finding_to_finding(doc)
		for text in expected:
			self.assertIn(text, str(ctx.exception))

	# -- the document's example table -----------------------------------------
	def test_same_category_other_size_same_attributes_allowed(self, _variant, _attrs):
		validate_finding_to_finding(_items_single(FND_10, FND_12))

	def test_same_size_refused(self, _variant, _attrs):
		self._refused(_items_single(FND_10, FND_10_TWIN), "Finding Size is the same")

	def test_other_category_refused(self, _variant, _attrs):
		self._refused(
			_items_single(FND_10, FND_LOCK_12),
			"Finding Category differs: Chains → Locks",
		)

	def test_other_attribute_refused(self, _variant, _attrs):
		self._refused(
			_items_single(FND_10, FND_12_PINK), "Metal Colour differs: Yellow → Pink"
		)

	# -- decisions taken with the business --------------------------------------
	def test_other_sub_category_refused(self, _variant, _attrs):
		self._refused(
			_items_single(FND_10, FND_12_BLACK_BEAD),
			"Finding Sub-Category differs: Kodi Chain → Black Bead Chain",
		)

	def test_blank_source_size_refused(self, _variant, _attrs):
		self._refused(
			_items_single(FND_NO_SIZE, FND_12),
			"Finding Size is missing on the source item",
		)

	def test_blank_target_size_refused(self, _variant, _attrs):
		self._refused(
			_items_single(FND_10, FND_NO_SIZE),
			"Finding Size is missing on the target item",
		)

	def test_blank_size_on_both_sides_names_both(self, _variant, _attrs):
		self.assertIn(
			"Finding Size is missing on the source and target item",
			_finding_resize_errors(
				ATTRIBUTES[FND_NO_SIZE], ATTRIBUTES[FND_NO_SIZE_TWIN]
			),
		)

	# -- scope --------------------------------------------------------------------
	def test_metal_to_finding_not_checked(self, _variant, _attrs):
		validate_finding_to_finding(_items_single(METAL, FND_10_TWIN))
		_attrs.assert_not_called()

	def test_finding_to_metal_not_checked(self, _variant, _attrs):
		validate_finding_to_finding(_items_single(FND_10, METAL))
		_attrs.assert_not_called()

	def test_melting_loss_out_of_scope(self, _variant, _attrs):
		validate_finding_to_finding(
			_items_single(FND_10, FND_LOCK_12, is_melting_loss=1)
		)
		_attrs.assert_not_called()

	def test_multiple_converter_not_checked(self, _variant, _attrs):
		validate_finding_to_finding(
			_Doc(
				multiple_metal_converter=1, source_item=FND_10, target_item=FND_LOCK_12
			)
		)
		_attrs.assert_not_called()


class TestVariantAttributes(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	@patch(f"{MC_MODULE}.frappe.get_all")
	def test_groups_rows_by_item_in_one_read(self, get_all):
		get_all.return_value = [
			frappe._dict(
				parent=FND_10, attribute="Finding Size", attribute_value="10.00 MM"
			),
			frappe._dict(
				parent=FND_10, attribute="Finding Category", attribute_value="Chains"
			),
			frappe._dict(
				parent=FND_12, attribute="Finding Size", attribute_value="12.00 MM"
			),
		]
		self.assertEqual(
			_variant_attributes([FND_10, FND_12, FND_10]),
			{
				FND_10: {"Finding Size": "10.00 MM", "Finding Category": "Chains"},
				FND_12: {"Finding Size": "12.00 MM"},
			},
		)
		get_all.assert_called_once()


@_ITEM_VARIANT
@patch(f"{UTILS_MODULE}.frappe.msgprint")
@patch(f"{UTILS_MODULE}.get_sample_batches", return_value=set())
@patch(f"{UTILS_MODULE}.get_batch_lane_map", side_effect=_lane_map)
@patch(f"{UTILS_MODULE}.capped_auto_batch_nos")
@patch(f"{MC_MODULE}.get_batch_lane_map", side_effect=_lane_map)
class TestCustomerFindingToFinding(IntegrationTestCase):
	"""Customer stock never becomes a Finding: a customer Finding is refused like customer Metal."""

	@classmethod
	def setUpClass(cls):
		pass

	def _doc(self, source_item, target_item, rows=()):
		return _Doc(
			multiple_metal_converter=0,
			source_item=source_item,
			target_item=target_item,
			source_qty=3,
			source_warehouse="RM - GE",
			source_batch_details=[
				frappe._dict(idx=idx, batch=batch, qty=qty)
				for idx, (batch, qty) in enumerate(rows, 1)
			],
		)

	def test_guard_refuses_customer_finding_to_finding(
		self, _mc_map, capped, _map, _samples, msgprint, _variant
	):
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_customer_metal_target(self._doc(FND_10, FND_12, [("B-CUST", 3)]))
		self.assertIn("Customer stock (Metal or Finding)", str(ctx.exception))

	def test_guard_refuses_customer_metal_to_finding(
		self, _mc_map, capped, _map, _samples, msgprint, _variant
	):
		with self.assertRaises(frappe.ValidationError):
			validate_customer_metal_target(self._doc(METAL, FND_12, [("B-CUST", 3)]))

	def test_guard_allows_regular_finding_to_finding(
		self, _mc_map, capped, _map, _samples, msgprint, _variant
	):
		validate_customer_metal_target(self._doc(FND_10, FND_12, [("B-REG", 3)]))

	def test_fifo_skips_customer_finding_for_finding_target(
		self, _mc_map, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = [
			frappe._dict(batch_no="B-CUST", qty=5),
			frappe._dict(batch_no="B-REG", qty=5),
		]
		doc = self._doc(FND_10, FND_12)
		update_source_betch(doc)
		self.assertEqual([r.batch for r in doc.source_batch_details], ["B-REG"])

	def test_entered_customer_finding_batch_refused(
		self, _mc_map, capped, _map, _samples, msgprint, _variant
	):
		# Point 4's message wins: the customer Finding source itself is refused.
		capped.return_value = [frappe._dict(batch_no="B-CUST", qty=5)]
		with self.assertRaises(frappe.ValidationError) as ctx:
			update_source_betch(self._doc(FND_10, FND_12, [("B-CUST", 3)]))
		self.assertIn(POINT_4_MESSAGE, str(ctx.exception))


@_ITEM_VARIANT
@patch(f"{UTILS_MODULE}.frappe.msgprint")
@patch(f"{UTILS_MODULE}.get_sample_batches", return_value=set())
@patch(f"{UTILS_MODULE}.get_batch_lane_map", side_effect=_lane_map)
@patch(f"{UTILS_MODULE}.capped_auto_batch_nos")
@patch(f"{MC_MODULE}.get_batch_lane_map", side_effect=_lane_map)
class TestCustomerFindingSource(IntegrationTestCase):
	"""Point 4: a customer Finding batch is never a source, whatever the target."""

	@classmethod
	def setUpClass(cls):
		pass

	def setUp(self):
		# B-CUST is oldest: a FIFO pick would always start there.
		self.stock = [
			frappe._dict(batch_no="B-CUST", qty=5),
			frappe._dict(batch_no="B-REG", qty=5),
		]

	def _doc(self, source_item, target_item, rows=(), **extra):
		return _Doc(
			multiple_metal_converter=extra.pop("multiple_metal_converter", 0),
			source_item=source_item,
			target_item=target_item,
			source_qty=3,
			source_warehouse="RM - GE",
			source_batch_details=[
				frappe._dict(idx=idx, batch=batch, qty=qty)
				for idx, (batch, qty) in enumerate(rows, 1)
			],
			**extra,
		)

	# -- FIFO pick ----------------------------------------------------------------
	def test_fifo_skips_customer_finding_for_metal_target(
		self, _mc_map, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = self.stock
		doc = self._doc(FND_10, METAL)
		update_source_betch(doc)
		self.assertEqual([r.batch for r in doc.source_batch_details], ["B-REG"])

	def test_fifo_shortfall_names_point_4(
		self, _mc_map, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = [
			frappe._dict(batch_no="B-CUST", qty=5),
			frappe._dict(batch_no="B-REG", qty=1),
		]
		with self.assertRaises(frappe.ValidationError) as ctx:
			update_source_betch(self._doc(FND_10, METAL))
		self.assertIn(POINT_4_MESSAGE, str(ctx.exception))
		self.assertIn("Only Regular Stock", str(ctx.exception))

	def test_customer_metal_to_metal_still_draws_customer(
		self, _mc_map, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = self.stock
		doc = self._doc(METAL, "GOLD-OTHER")
		update_source_betch(doc)
		self.assertEqual([r.batch for r in doc.source_batch_details], ["B-CUST"])

	# -- batches entered by hand ----------------------------------------------------
	def test_entered_customer_finding_for_metal_target_refused(
		self, _mc_map, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = self.stock
		with self.assertRaises(frappe.ValidationError) as ctx:
			update_source_betch(self._doc(FND_10, METAL, [("B-CUST", 3)]))
		self.assertIn("Row 1", str(ctx.exception))
		self.assertIn(POINT_4_MESSAGE, str(ctx.exception))

	def test_entered_regular_finding_for_metal_target_kept(
		self, _mc_map, capped, _map, _samples, msgprint, _variant
	):
		capped.return_value = self.stock
		doc = self._doc(FND_10, METAL, [("B-REG", 3)])
		update_source_betch(doc)
		self.assertEqual(
			[(r.batch, r.qty) for r in doc.source_batch_details], [("B-REG", 3)]
		)

	# -- the safety net on save -----------------------------------------------------
	def test_guard_refuses_customer_finding_source(
		self, _mc_map, capped, _map, _samples, msgprint, _variant
	):
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_customer_finding_source(self._doc(FND_10, METAL, [("B-CUST", 3)]))
		self.assertIn(POINT_4_MESSAGE, str(ctx.exception))

	def test_guard_allows_regular_finding_source(
		self, _mc_map, capped, _map, _samples, msgprint, _variant
	):
		validate_customer_finding_source(self._doc(FND_10, METAL, [("B-REG", 3)]))

	def test_guard_ignores_customer_metal_source(
		self, _mc_map, capped, _map, _samples, msgprint, _variant
	):
		validate_customer_finding_source(
			self._doc(METAL, "GOLD-OTHER", [("B-CUST", 3)])
		)
		_mc_map.assert_not_called()

	def test_guard_skips_melting_loss_and_multiple(
		self, _mc_map, capped, _map, _samples, msgprint, _variant
	):
		validate_customer_finding_source(
			self._doc(FND_10, METAL, [("B-CUST", 3)], is_melting_loss=1)
		)
		validate_customer_finding_source(
			self._doc(FND_10, METAL, [("B-CUST", 3)], multiple_metal_converter=1)
		)
		_mc_map.assert_not_called()


_PICKER_ROWS = [("B-CUST", 5.0), ("B-REG", 5.0)]


@_ITEM_VARIANT
@patch(f"{MC_MODULE}.get_batch_lane_map", side_effect=_lane_map)
@patch(f"{MC_MODULE}.get_batch_no", return_value=_PICKER_ROWS)
class TestSourceBatchPicker(IntegrationTestCase):
	"""Customer batches are not offered wherever the save would refuse them."""

	@classmethod
	def setUpClass(cls):
		pass

	def _pick(self, **filters):
		return [
			row[0]
			for row in get_source_batches(
				"Batch", "", "name", 0, 20, frappe._dict(filters)
			)
		]

	def test_finding_source_hides_customer_batches(self, _batches, _map, _variant):
		self.assertEqual(self._pick(item_code=FND_10, target_item=METAL), ["B-REG"])

	def test_finding_target_hides_customer_batches(self, _batches, _map, _variant):
		self.assertEqual(self._pick(item_code=METAL, target_item=FND_12), ["B-REG"])

	def test_melting_loss_hides_customer_batches(self, _batches, _map, _variant):
		self.assertEqual(self._pick(item_code=METAL, is_melting_loss=1), ["B-REG"])

	def test_metal_to_metal_offers_every_batch(self, _batches, _map, _variant):
		self.assertEqual(
			self._pick(item_code=METAL, target_item="GOLD-OTHER"), ["B-CUST", "B-REG"]
		)
		_map.assert_not_called()

	def test_no_source_item_offers_nothing(self, _batches, _map, _variant):
		self.assertEqual(self._pick(), [])
		_batches.assert_not_called()
