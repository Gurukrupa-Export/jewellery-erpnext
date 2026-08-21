# Copyright (c) 2026, Nirali and contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.refining.constants import (
	REFINING_TYPE_SCRAP,
	REFINING_TYPE_UNUSED,
)
from jewellery_erpnext.refining.doctype.refinery_price_list.refinery_price_list import (
	build_refinery_price_index,
	compute_refining_amount,
	get_refinery_rate,
	refining_line_terms,
	resolve_refinery_price_list,
)

DUST_ITEM = "_Test Vacuum Bag Dust"
VARIANT_ITEM = "ML-G-22KT-91.9-Y"
VARIANT_TEMPLATE = "ML"


def _ensure_dust_item():
	if not frappe.db.exists("Item", DUST_ITEM):
		# Temporarily bypass server scripts (like GK Item Naming) during test fixture setup
		in_migrate = getattr(frappe.flags, "in_migrate", False)
		frappe.flags.in_migrate = True
		try:
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": DUST_ITEM,
					"item_name": DUST_ITEM,
					"item_group": frappe.db.get_value(
						"Item Group", {"is_group": 0}, "name"
					),
					"is_stock_item": 1,
					"has_batch_no": 1,
					"create_new_batch": 1,
					"stock_uom": "Gram",
				}
			).insert(ignore_permissions=True)
		finally:
			frappe.flags.in_migrate = in_migrate


def _delete_price_lists(**filters):
	"""Delete parents AND their children — a raw frappe.db.delete of the parent leaves
	orphaned slab/covered rows behind."""
	for name in frappe.get_all("Refinery Price List", filters=filters, pluck="name"):
		frappe.delete_doc(
			"Refinery Price List",
			name,
			force=1,
			ignore_permissions=True,
			delete_permanently=True,
		)


def _price_list(item, refining_type=None, slabs=(), covered=()):
	doc = frappe.get_doc(
		{"doctype": "Refinery Price List", "item": item, "refining_type": refining_type}
	)
	for from_g, to_g, charge_type, rate, basis in slabs:
		doc.append(
			"slabs",
			{
				"from_weight": from_g,
				"to_weight": to_g,
				"charge_type": charge_type,
				"rate": rate,
				"weight_basis": basis,
			},
		)
	for code in covered:
		doc.append("covered_items", {"item_code": code})
	doc.insert(ignore_permissions=True)
	return doc


class TestRefineryPriceList(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		frappe.reload_doc("refining", "doctype", "refinery_price_list_item", force=True)
		frappe.reload_doc("refining", "doctype", "refinery_price_list", force=True)
		frappe.reload_doc("refining", "doctype", "refinery_price_slab", force=True)
		_ensure_dust_item()
		_delete_price_lists(item=DUST_ITEM)
		# ONE document per (item, refining_type); the slabs carry the weight bands. Bands may
		# no longer overlap, so these are strictly adjacent.
		_price_list(
			DUST_ITEM,
			slabs=[
				(0, 1000, "Per Kg", 1800, "Gross Weight"),
				(1000, 0, "Per Kg", 2200, "After Burning Weight"),
			],
		)

	def tearDown(self):
		# Only the base fixture survives between tests.
		_delete_price_lists(item=["!=", DUST_ITEM])
		_delete_price_lists(item=DUST_ITEM, refining_type=["is", "set"])

	# --- slab band matching (unchanged behaviour) ---

	def test_band_within_range(self):
		row = get_refinery_rate(DUST_ITEM, 500)
		self.assertIsNotNone(row)
		self.assertEqual(row["rate"], 1800)
		self.assertEqual(row["weight_basis"], "Gross Weight")

	def test_above_band_uses_to_weight_zero(self):
		row = get_refinery_rate(DUST_ITEM, 1500)
		self.assertIsNotNone(row)
		self.assertEqual(row["rate"], 2200)
		self.assertEqual(row["weight_basis"], "After Burning Weight")

	def test_lower_boundary_inclusive(self):
		self.assertEqual(get_refinery_rate(DUST_ITEM, 0)["rate"], 1800)

	def test_no_match_returns_none(self):
		self.assertIsNone(get_refinery_rate("_No Such Item _x", 500))
		self.assertIsNone(get_refinery_rate(None, 500))

	def test_result_carries_parent_and_slab(self):
		row = get_refinery_rate(DUST_ITEM, 500)
		self.assertTrue(
			frappe.db.exists("Refinery Price List", row["name"]),
			"result 'name' must be the parent document (PO back-links point at it)",
		)
		self.assertEqual(row["item"], DUST_ITEM)
		self.assertTrue(row["slab_name"])
		self.assertEqual(row["matched_by"], "category_item")

	# --- Covered Items: the new item -> price list mapping ---

	def test_covered_item_resolves_to_price_list(self):
		doc = _price_list(
			"REF-NB-001",
			REFINING_TYPE_SCRAP,
			slabs=[(0, 0, "Per Gram", 3, "Gross Weight")],
			covered=[VARIANT_ITEM],
		)
		row = get_refinery_rate(VARIANT_ITEM, 10, REFINING_TYPE_SCRAP)
		self.assertEqual(row["name"], doc.name)
		self.assertEqual(row["matched_by"], "covered_item")

	def test_covered_template_matches_every_variant(self):
		"""One row for the template instead of the 16 ML variants (or the 11k+ F ones)."""
		doc = _price_list(
			"REF-NB-001",
			REFINING_TYPE_SCRAP,
			slabs=[(0, 0, "Per Gram", 3, "Gross Weight")],
			covered=[VARIANT_TEMPLATE],
		)
		row = get_refinery_rate(VARIANT_ITEM, 10, REFINING_TYPE_SCRAP)
		self.assertEqual(row["name"], doc.name)
		self.assertEqual(row["matched_by"], "covered_template")

	def test_exact_covered_item_beats_template(self):
		"""A single variant can be carved out of its template's list."""
		_price_list(
			"REF-NB-001",
			REFINING_TYPE_SCRAP,
			slabs=[(0, 0, "Per Gram", 3, "Gross Weight")],
			covered=[VARIANT_TEMPLATE],
		)
		exact = _price_list(
			"REF-CF-001",
			REFINING_TYPE_UNUSED,
			slabs=[(0, 0, "Per Gram", 9, "Gross Weight")],
			covered=[VARIANT_ITEM],
		)
		row = get_refinery_rate(VARIANT_ITEM, 10, REFINING_TYPE_UNUSED)
		self.assertEqual(row["name"], exact.name)
		self.assertEqual(row["matched_by"], "covered_item")

	def test_category_item_match_is_the_last_resort(self):
		"""Guards the zero-config path for physically stocked REF-* categories."""
		self.assertEqual(
			resolve_refinery_price_list(DUST_ITEM),
			frappe.db.get_value("Refinery Price List", {"item": DUST_ITEM}, "name"),
		)

	# --- refining_type scoping ---

	def test_refining_type_specific_beats_blank(self):
		specific = _price_list(
			DUST_ITEM,
			REFINING_TYPE_SCRAP,
			slabs=[(0, 0, "Per Gram", 7, "Gross Weight")],
		)
		row = get_refinery_rate(DUST_ITEM, 500, REFINING_TYPE_SCRAP)
		self.assertEqual(row["name"], specific.name)
		self.assertEqual(row["rate"], 7)

	def test_blank_refining_type_matches_every_type(self):
		for refining_type in (REFINING_TYPE_SCRAP, REFINING_TYPE_UNUSED, None):
			self.assertIsNotNone(
				get_refinery_rate(DUST_ITEM, 500, refining_type),
				f"the blank-type list must answer for {refining_type!r}",
			)

	def test_refining_type_scope_excludes_other_types(self):
		_delete_price_lists(item=DUST_ITEM)
		_price_list(
			DUST_ITEM,
			REFINING_TYPE_SCRAP,
			slabs=[(0, 0, "Per Gram", 7, "Gross Weight")],
		)
		self.assertIsNotNone(get_refinery_rate(DUST_ITEM, 500, REFINING_TYPE_SCRAP))
		self.assertIsNone(get_refinery_rate(DUST_ITEM, 500, REFINING_TYPE_UNUSED))
		# Restore the class fixture for the remaining tests.
		_delete_price_lists(item=DUST_ITEM)
		_price_list(
			DUST_ITEM,
			slabs=[
				(0, 1000, "Per Kg", 1800, "Gross Weight"),
				(1000, 0, "Per Kg", 2200, "After Burning Weight"),
			],
		)

	# --- validation ---

	def test_from_gt_to_is_rejected(self):
		doc = frappe.get_doc({"doctype": "Refinery Price List", "item": "REF-NB-001"})
		doc.append(
			"slabs",
			{
				"from_weight": 100,
				"to_weight": 10,
				"charge_type": "Flat Charge",
				"rate": 1,
				"weight_basis": "Gross Weight",
			},
		)
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_overlapping_bands_are_rejected(self):
		"""Without the Refining Process column two rows can no longer share a band — the
		first-by-row-order tie-break would be arbitrary on money."""
		doc = frappe.get_doc({"doctype": "Refinery Price List", "item": "REF-NB-001"})
		for from_g, to_g in ((0, 50), (40, 100)):
			doc.append(
				"slabs",
				{
					"from_weight": from_g,
					"to_weight": to_g,
					"charge_type": "Flat Charge",
					"rate": 1,
					"weight_basis": "Gross Weight",
				},
			)
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_duplicate_item_scope_is_rejected(self):
		"""Now actually fires: the old guard compared `self.supplier or ""` against a NULL
		column and so never matched for the blank scope every production document uses."""
		doc = frappe.get_doc({"doctype": "Refinery Price List", "item": DUST_ITEM})
		doc.append(
			"slabs",
			{
				"from_weight": 0,
				"to_weight": 0,
				"charge_type": "Flat Charge",
				"rate": 5,
				"weight_basis": "Gross Weight",
			},
		)
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_same_item_different_refining_type_is_allowed(self):
		doc = _price_list(
			DUST_ITEM,
			REFINING_TYPE_SCRAP,
			slabs=[(0, 0, "Flat Charge", 5, "Gross Weight")],
		)
		self.assertTrue(frappe.db.exists("Refinery Price List", doc.name))

	def test_duplicate_covered_item_within_a_document_is_rejected(self):
		doc = frappe.get_doc({"doctype": "Refinery Price List", "item": "REF-NB-001"})
		doc.append(
			"slabs",
			{
				"from_weight": 0,
				"to_weight": 0,
				"charge_type": "Flat Charge",
				"rate": 5,
				"weight_basis": "Gross Weight",
			},
		)
		doc.append("covered_items", {"item_code": VARIANT_ITEM})
		doc.append("covered_items", {"item_code": VARIANT_ITEM})
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_covered_item_clash_across_overlapping_scopes_is_rejected(self):
		_price_list(
			"REF-NB-001",
			slabs=[(0, 0, "Per Gram", 3, "Gross Weight")],
			covered=[VARIANT_ITEM],
		)
		clash = frappe.get_doc({"doctype": "Refinery Price List", "item": "REF-CF-001"})
		clash.append(
			"slabs",
			{
				"from_weight": 0,
				"to_weight": 0,
				"charge_type": "Per Gram",
				"rate": 9,
				"weight_basis": "Gross Weight",
			},
		)
		clash.append("covered_items", {"item_code": VARIANT_ITEM})
		self.assertRaises(frappe.ValidationError, clash.insert)

	# --- pricing math ---

	def test_compute_amount_by_charge_type(self):
		"""The money-side anchor: unchanged, so the totals cannot have moved."""
		self.assertEqual(compute_refining_amount("Flat Charge", 800, 40), 800)
		self.assertEqual(compute_refining_amount("Per Gram", 18, 60), 1080)  # 18 × 60
		self.assertEqual(
			compute_refining_amount("Per Kg", 1800, 500), 900
		)  # 1800 × 0.5 kg

	def test_line_terms_by_charge_type(self):
		self.assertEqual(
			refining_line_terms("Per Gram", 18, 60, "Gram"), (60, 18, "Gram")
		)
		self.assertEqual(
			refining_line_terms("Per Kg", 1800, 500, "Gram"), (500, 1.8, "Gram")
		)
		# A flat charge is a per-consignment fee: qty stays 1 so the PO shows the agreed
		# figure rather than a derived per-gram rate (750 on 0.003 g -> 250,000/Gram).
		self.assertEqual(
			refining_line_terms("Flat Charge", 800, 40, "Gram"), (1.0, 800, "Nos")
		)
		self.assertEqual(
			refining_line_terms(None, None, 40, "Litre"), (40, 0.0, "Litre")
		)

	def test_line_terms_preserve_the_total(self):
		"""qty × rate must equal what compute_refining_amount says the charge is."""
		for charge_type, rate, qty in (
			("Per Gram", 18, 60),
			("Per Kg", 1800, 500),
			("Flat Charge", 800, 40),
		):
			line_qty, line_rate, _uom = refining_line_terms(
				charge_type, rate, qty, "Gram"
			)
			self.assertAlmostEqual(
				line_qty * line_rate,
				compute_refining_amount(charge_type, rate, qty),
				places=2,
				msg=f"{charge_type} split diverged from the total",
			)

	def test_index_shape_and_scoped_contents(self):
		"""The index is what makes pricing a whole consignment a fixed 3 queries instead of
		one lookup per row, so pin its shape."""
		doc = _price_list(
			"REF-NB-001",
			REFINING_TYPE_SCRAP,
			slabs=[(0, 0, "Per Gram", 3, "Gross Weight")],
			covered=[VARIANT_TEMPLATE],
		)
		index = build_refinery_price_index(REFINING_TYPE_SCRAP)

		self.assertEqual(
			set(index), {"order", "parents", "covered", "category", "slabs"}
		)
		self.assertEqual(index["covered"].get(VARIANT_TEMPLATE), doc.name)
		self.assertEqual(index["category"].get("REF-NB-001"), doc.name)
		self.assertEqual(len(index["slabs"][doc.name]), 1)
		# The type-specific list sorts ahead of the blank-type one.
		self.assertEqual(index["order"][0], doc.name)
