# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

#: Flat Charge is a per-CONSIGNMENT fee ("Rs.800 for 0-50 g"), not a per-unit rate, so by
#: default its Purchase Order line stays qty 1 / uom Nos / rate = the flat amount — which
#: already satisfies amount = qty x rate. Deriving rate = flat / qty instead prints rates
#: nobody agreed to (Rs.750 on 0.003 g becomes 250,000.000 per Gram), leaves a rounding
#: tail, and writes junk into Item.last_purchase_rate on every PO submit. Flip this to True
#: to bill flat slabs on the weight as well; nothing else needs to change.
FLAT_CHARGE_PER_UNIT = False


class RefineryPriceList(Document):
	def validate(self):
		self.validate_slab_bands()
		self.validate_unique_item_scope()
		self.validate_covered_items()

	def validate_slab_bands(self):
		for slab in self.slabs:
			# to_weight == 0 encodes "no upper bound" ("Above N"), so only compare when set.
			if flt(slab.to_weight) and flt(slab.from_weight) > flt(slab.to_weight):
				frappe.throw(
					_(
						"Row #{0}: From Weight ({1}) cannot be greater than To Weight ({2})."
					).format(slab.idx, slab.from_weight, slab.to_weight)
				)

		# Overlapping bands within one document make pick_price_slab's first-by-idx rule an
		# arbitrary tie-break. Previously the bands were also keyed by Refining Process, so
		# two rows could legitimately share a band; without that column they cannot.
		for i, a in enumerate(self.slabs):
			for b in self.slabs[i + 1 :]:
				a_hi = flt(a.to_weight) or float("inf")
				b_hi = flt(b.to_weight) or float("inf")
				if flt(a.from_weight) <= b_hi and flt(b.from_weight) <= a_hi:
					frappe.throw(
						_(
							"Rows #{0} and #{1} cover overlapping weight bands "
							"({2}-{3} and {4}-{5}). Only one slab may match a given weight."
						).format(
							a.idx,
							b.idx,
							a.from_weight,
							a.to_weight or "∞",
							b.from_weight,
							b.to_weight or "∞",
						)
					)

	def validate_unique_item_scope(self):
		"""One price list document per (item, refining_type). Two documents for the same
		scope would make the pick order between them arbitrary.

		Compared with IFNULL so a blank refining_type — which means "every type" — actually
		matches the NULL stored in the column. The old supplier/company version of this
		guard compared ``self.supplier or ""`` against a NULL column and so never fired.
		"""
		existing = frappe.db.sql(
			"""
			SELECT name FROM `tabRefinery Price List`
			WHERE item = %(item)s
			  AND IFNULL(refining_type, '') = %(refining_type)s
			  AND name != %(name)s
			LIMIT 1
			""",
			{
				"item": self.item,
				"refining_type": self.refining_type or "",
				"name": self.name or "",
			},
		)
		if existing:
			frappe.throw(
				_(
					"Refinery Price List {0} already covers Item {1} for Refining Type {2}. "
					"Add slabs there instead."
				).format(
					frappe.bold(existing[0][0]),
					frappe.bold(self.item),
					frappe.bold(self.refining_type or _("(all types)")),
				)
			)

	def validate_covered_items(self):
		"""Each covered item code may resolve to exactly one price list in a given scope.

		Two guards: no duplicate within this document, and no clash with another document
		whose refining_type scope overlaps (a blank type overlaps everything). An exact code
		in one list and its TEMPLATE in another is NOT a clash — exact always wins.
		"""
		seen = {}
		for row in self.covered_items:
			if not row.item_code:
				continue
			if row.item_code in seen:
				frappe.throw(
					_("Rows #{0} and #{1}: Item {2} is listed twice.").format(
						seen[row.item_code], row.idx, frappe.bold(row.item_code)
					)
				)
			seen[row.item_code] = row.idx

		if not seen:
			return

		# Joined to the parent so orphaned child rows (left by a raw frappe.db.delete of a
		# price list) cannot produce a phantom clash.
		clashes = frappe.db.sql(
			"""
			SELECT c.item_code, c.parent
			FROM `tabRefinery Price List Item` c
			JOIN `tabRefinery Price List` p ON p.name = c.parent
			WHERE c.item_code IN %(codes)s
			  AND c.parenttype = 'Refinery Price List'
			  AND p.name != %(name)s
			  AND (IFNULL(p.refining_type, '') = '' OR %(refining_type)s = ''
			       OR p.refining_type = %(refining_type)s)
			LIMIT 1
			""",
			{
				"codes": tuple(seen),
				"name": self.name or "",
				"refining_type": self.refining_type or "",
			},
			as_dict=True,
		)
		if clashes:
			clash = clashes[0]
			other_type = frappe.db.get_value(
				"Refinery Price List", clash.parent, "refining_type"
			)
			frappe.throw(
				_(
					"Item {0} is already covered by Refinery Price List {1}, which applies "
					"to {2}. Set a Refining Type on one of them so the two do not overlap."
				).format(
					frappe.bold(clash.item_code),
					frappe.bold(clash.parent),
					frappe.bold(other_type or _("all Refining Types")),
				)
			)


def on_doctype_update():
	# Index the lookup keys so build_refinery_price_index is cheap.
	frappe.db.add_index("Refinery Price List", ["item"], index_name="refinery_item")
	frappe.db.add_index(
		"Refinery Price List",
		["item", "refining_type"],
		index_name="refinery_item_type",
	)


def build_refinery_price_index(refining_type=None):
	"""Everything needed to price a whole consignment, in THREE queries.

	Returns ``{order, parents, covered, category, slabs}``:

	* ``order``    — parent names, best-scoped first (type-specific before blank).
	* ``parents``  — ``{name: parent dict}``.
	* ``covered``  — ``{item_code or template: parent name}`` from the Covered Items tables.
	* ``category`` — ``{parent.item: parent name}``, the historical category match.
	* ``slabs``    — ``{parent name: [slab dicts, idx asc]}``.

	Both maps are filled by walking ``order`` with ``setdefault``, so the best-scoped parent
	wins every key and lookups need no sorting of their own.
	"""
	filters = {}
	if refining_type:
		filters["refining_type"] = ["in", [refining_type, "", None]]

	parents = frappe.get_all(
		"Refinery Price List",
		filters=filters,
		fields=["name", "item", "refining_type"],
	)
	index = {
		"order": [],
		"parents": {},
		"covered": {},
		"category": {},
		"slabs": {},
	}
	if not parents:
		return index

	# A type-specific list outranks a blank ("all types") one.
	parents.sort(
		key=lambda p: 0 if (refining_type and p.refining_type == refining_type) else 1
	)
	index["order"] = [p.name for p in parents]
	index["parents"] = {p.name: p for p in parents}

	names = index["order"]
	covered_rows = frappe.get_all(
		"Refinery Price List Item",
		filters={"parent": ["in", names], "parenttype": "Refinery Price List"},
		fields=["parent", "item_code"],
		order_by="idx asc",
	)
	by_parent = {}
	for row in covered_rows:
		by_parent.setdefault(row.parent, []).append(row.item_code)

	slab_rows = frappe.get_all(
		"Refinery Price Slab",
		filters={"parent": ["in", names], "parenttype": "Refinery Price List"},
		fields=[
			"name",
			"parent",
			"from_weight",
			"to_weight",
			"charge_type",
			"rate",
			"weight_basis",
		],
		order_by="parent asc, idx asc",
	)
	for row in slab_rows:
		index["slabs"].setdefault(row.parent, []).append(row)

	for name in names:
		for code in by_parent.get(name, []):
			index["covered"].setdefault(code, name)
		item = index["parents"][name].item
		if item:
			index["category"].setdefault(item, name)

	return index


def resolve_from_index(index, item_code, variant_of=None):
	"""Price list covering ``item_code``, or ``None``. Three tiers, in order:

	1. the exact code in a Covered Items table — so a single variant can be carved out of
	   its template's list;
	2. its TEMPLATE in a Covered Items table — one row instead of the 11k+ variants some
	   templates have;
	3. the code equal to a list's category ``item`` — the historical behaviour, which keeps
	   the physically-stocked REF-* categories working with no configuration at all.

	The caller owns the last resort (fall back to the entry's ``pricing_item``), because
	that is a Refining Entry concept and consumables are deliberately excluded from it.
	"""
	if not item_code:
		return None
	return (
		index["covered"].get(item_code)
		or (variant_of and index["covered"].get(variant_of))
		or index["category"].get(item_code)
	)


def resolve_refinery_price_list(item_code, refining_type=None):
	"""Convenience wrapper for console/one-off use: builds its own index."""
	index = build_refinery_price_index(refining_type)
	return resolve_from_index(
		index, item_code, frappe.db.get_value("Item", item_code, "variant_of")
	)


def pick_price_slab(index, parent, weight):
	"""First slab (row order) of ``parent`` whose band covers ``weight``, or ``None``.
	``to_weight = 0`` means no upper bound. Pure — no DB access."""
	weight = flt(weight)
	for slab in index["slabs"].get(parent) or []:
		upper = flt(slab.to_weight) or 1e12
		if flt(slab.from_weight) <= weight <= upper:
			return slab
	return None


def get_refinery_rate(item_code, weight=0, refining_type=None, variant_of=None):
	"""Best-matching price slab for ``item_code`` at ``weight`` grams, as a dict, or None.

	Returns ``{name, item, rate, charge_type, weight_basis, refining_type, slab_name,
	matched_by}`` — ``name`` is the parent document, so existing
	``Purchase Order Item.custom_refining_price_list`` back-links keep resolving.

	Convenience for single lookups; ``create_external_refining_po`` builds the index once
	and prices every line off it instead.
	"""
	if not item_code:
		return None

	index = build_refinery_price_index(refining_type)
	if variant_of is None:
		variant_of = frappe.db.get_value("Item", item_code, "variant_of")

	parent = resolve_from_index(index, item_code, variant_of)
	if not parent:
		return None
	slab = pick_price_slab(index, parent, weight)
	if not slab:
		return None

	if index["covered"].get(item_code) == parent:
		matched_by = "covered_item"
	elif variant_of and index["covered"].get(variant_of) == parent:
		matched_by = "covered_template"
	else:
		matched_by = "category_item"

	return {
		"name": parent,
		"item": index["parents"][parent].item,
		"rate": slab.rate,
		"charge_type": slab.charge_type,
		"weight_basis": slab.weight_basis,
		"refining_type": index["parents"][parent].refining_type,
		"slab_name": slab.name,
		"matched_by": matched_by,
	}


def compute_refining_amount(charge_type, rate, weight_g):
	"""TOTAL charge for a slab given its charge type and the billable weight (grams).

	``Flat Charge`` → rate; ``Per Gram`` → rate × grams; ``Per Kg`` → rate × grams / 1000.
	Kept as the money-side regression anchor: refining_line_terms splits the same total into
	a qty and a per-unit rate, and the two must always agree.
	"""
	rate = flt(rate)
	weight_g = flt(weight_g)
	if charge_type == "Per Gram":
		return flt(rate * weight_g, 2)
	if charge_type == "Per Kg":
		return flt(rate * weight_g / 1000.0, 2)
	# Flat Charge (default)
	return flt(rate, 2)


def refining_line_terms(charge_type, rate, qty, uom):
	"""``(line_qty, line_rate, line_uom)`` for a Purchase Order line.

	A PO line must satisfy ``amount = qty × rate`` (ERPNext recomputes amount in
	calculate_item_values), so a WEIGHT-based slab is expressed as a per-unit rate on a qty
	of grams/litres rather than as one lump on a qty of 1:

	  * ``Per Gram`` → the slab rate as-is, on the weight.
	  * ``Per Kg``   → rate / 1000 per gram, on the weight.
	  * ``Flat Charge`` → qty 1 / Nos / the flat amount (see FLAT_CHARGE_PER_UNIT).
	  * unpriced (no slab) → the weight at rate 0, for the buyer to price manually.
	"""
	qty = flt(qty)
	rate = flt(rate)
	if charge_type == "Per Gram":
		return qty, rate, uom
	if charge_type == "Per Kg":
		return qty, flt(rate / 1000.0, 9), uom
	if charge_type == "Flat Charge":
		if FLAT_CHARGE_PER_UNIT and qty > 0:
			return qty, flt(rate / qty, 9), uom
		return 1.0, rate, "Nos"
	return qty, 0.0, uom
