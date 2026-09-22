# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Raw-material totals for the Stock Entry header.

Fills six header fields from the item rows, bucketed by the MATERIAL FAMILY each row
belongs to rather than by the item itself::

    custom_total_metal_weight     <- M, ML      row qty, GRAMS
    custom_total_finding_weight   <- F, FL      row qty, GRAMS
    custom_total_diamond_weight   <- D          row qty, CARATS
    custom_total_gemstone_weight  <- G          row qty, CARATS
    custom_total_diamond_pcs      <- D          row pcs
    custom_total_gemstone_pcs     <- G          row pcs

**Each total carries the unit of the rows it sums, and nothing is converted.** Metal and
finding rows are Gram, so those totals are grams; diamond and gemstone rows are Carat, so
those totals are carats. A header field is always the plain sum of the grid column above it
-- add the qty column up by hand and the header number comes back. Reporting 0.564 ct of
diamond as 0.113 g made the header disagree with every row it claimed to describe, and at
precision 3 it also threw away a digit: MAT-STE-99993's 0.029 ct landed as 0.006 g.

Four fields, two units, bare labels -- a deliberate repeat of the block they mirror.
``Stock Entry Detail.custom_bom_diamond_weight`` already carries a carat figure under the
bare label "Diamond Weight" on this very doctype, and
``patches/add_fg_serial_bom_weight_fields`` says why: "Units follow the BOM columns these
mirror: gross/metal/finding are grams, diamond/gemstone are carats." Carat is the PRIMARY
stored unit for stones app-wide and grams the derived twin -- ``utils.carat_to_gram``, and
``mop_log.recalculate_manufacturing_operation_weights``, which sums ``diamond_wt`` raw and
only then derives ``diamond_wt_in_gram`` from the total.

**No uom is read; a D or G row is summed exactly as keyed.** Every carat tally in the app
already works that way, over this same material: ``mop_log`` does a bare
``buckets[f"{prefix}_wt"] += qty`` and ``manufacturing_operation.get_material_wt`` a bare
``diamond_wt += qty``, neither looking at the row's uom. The ``IF(uom = 'Carat', qty * 0.2,
qty)`` branch does exist -- ``department_ir`` and ``SerialNumberCreator._compute_total_weight``
-- but every instance of it produces GRAMS. There is no gram -> carat branch anywhere in the
app and this is not the place to invent one: ``utils.gram_to_carat`` is barred from the use
in as many words ("never feed the result back into a stored weight"), and its one call site
is a message string. All 456,861 D and 53,393 G rows on the live site are ``Carat`` on both
``uom`` and ``stock_uom``, so a D row keyed in Gram contradicts its own ``Item.stock_uom``
and its Stock Ledger Entry is already wrong. Scaling such a row by 5 here would leave a
plausible header sitting on a broken ledger; summing it as given leaves a header that still
equals its grid, and a row that still looks wrong in it.

**Every row counts, whichever side of the entry it is on.** Consume, produce and transfer
rows all contribute, so a Repack that turns 1.46 g of metal into 1.46 g of findings reports
both -- metal 1.46 AND finding 1.46. That is the intended reading: the entry really did move
that much of each, and the four fields describe what the document touched, not a net.

**Finished goods are excluded, and fall out on their own.** An FG item's ``variant_of`` is a
DESIGN template (``MU00122``, ``RI02120``, ``NP00004``, ``EA00925`` ...), which is simply not
in ``VARIANT_BUCKETS``. Those rows are ``Nos`` with qty 1 -- a piece count, not a weight -- so
leaving them out is the whole point of bucketing by raw material instead of item-wise. ``O``
and a null ``variant_of`` drop out the same way.

Which is why the lookup is an EXACT match on the whole ``custom_variant_of`` and never on its
first character. ``mop_log.FIELD_MAP`` and ``manufacturing_operation.get_material_wt`` both
bucket on ``item_code[0]``, and copying that here would be a bug: ``MU00122`` and friends
start with ``M`` and would be counted as metal. The explicit dict is also what makes ``ML`` /
``FL`` a deliberate inclusion rather than an accident of string prefixes.

With nothing to convert, ``carat_to_gram``'s "convert once, on the total" rule collapses to
"ROUND once, on the total" -- the single ``flt`` at the end of :func:`material_totals` is the
only rounding left in the function. That still bites: ``Stock Entry Detail.qty`` is
``decimal(21,9)`` however the form displays it, so three 0.1234 ct rows report 0.370, not the
0.369 that rounding each row before summing would give.
"""

from frappe.utils import cint, flt

# Whole-value match, never a prefix -- see the module docstring.
VARIANT_BUCKETS = {
	"M": "metal",
	"ML": "metal",
	"F": "finding",
	"FL": "finding",
	"D": "diamond",
	"G": "gemstone",
}

# The families measured in carats rather than grams, and therefore the only ones that carry a
# pcs count. One tuple for both facts because they have one cause: stones are counted as well
# as weighed, metal and findings are only weighed.
STONE_BUCKETS = ("diamond", "gemstone")

# 3 dp for grams and carats alike: Stock Entry Detail.qty displays at 3, and the four weight
# Custom Fields leave `precision` blank, which resolves to System Settings float_precision --
# pinned to 3 by patches/ensure_float_precision_three.
WEIGHT_PRECISION = 3

# NEVER sum these four together. Metal and finding are grams, diamond and gemstone are
# carats, so `metal + finding + diamond + gemstone` is meaningless. A gross weight is
# `metal + finding + carat_to_gram(diamond) + carat_to_gram(gemstone)` -- the identity
# `mop_log.update_wt_detail` and `manufacturing_operation.get_material_wt` both build from
# the `*_wt_in_gram` twins rather than from the carat figures.
TARGET_FIELDS = {
	"metal": "custom_total_metal_weight",
	"finding": "custom_total_finding_weight",
	"diamond": "custom_total_diamond_weight",
	"gemstone": "custom_total_gemstone_weight",
	"diamond_pcs": "custom_total_diamond_pcs",
	"gemstone_pcs": "custom_total_gemstone_pcs",
}


def _get(row, fieldname):
	"""Read ``fieldname`` off a row that may be a dict or a Document/namespace."""
	if isinstance(row, dict):
		return row.get(fieldname)
	return getattr(row, fieldname, None)


def _bucket(row):
	"""The material family this row belongs to, or ``None`` to ignore it."""
	variant = _get(row, "custom_variant_of")
	if not variant:
		return None
	return VARIANT_BUCKETS.get(str(variant).strip())


def material_totals(rows):
	"""``{metal, finding, diamond, gemstone, diamond_pcs, gemstone_pcs}`` for ``rows``.

	Metal and finding are GRAMS, diamond and gemstone are CARATS -- each bucket in the unit
	its own rows are keyed in, never converted. Weights are at precision 3; pcs are integers.
	Pure -- no document, no DB read beyond the rounding mode ``flt`` consults -- so the
	arithmetic can be tested directly.
	"""
	weights = {"metal": 0.0, "finding": 0.0, "diamond": 0.0, "gemstone": 0.0}
	pcs = {"diamond": 0, "gemstone": 0}

	for row in rows or []:
		bucket = _bucket(row)
		if not bucket:
			continue

		# Taken as keyed, with no look at `uom` -- the module docstring carries the argument
		# for why there is no gram -> carat branch here and why adding one would be the
		# app's first.
		weights[bucket] += flt(_get(row, "qty"))

		if bucket in STONE_BUCKETS:
			# pcs is a Data field on Stock Entry Detail, so it arrives as a string.
			# Summed as given: this is a document total, not a running balance, so the
			# negative-clamping mop_log applies to a ledger has no place here.
			pcs[bucket] += cint(_get(row, "pcs"))

	# Rounded once, on the total, never per row -- see the last docstring paragraph.
	totals = {key: flt(value, WEIGHT_PRECISION) for key, value in weights.items()}
	totals["diamond_pcs"] = pcs["diamond"]
	totals["gemstone_pcs"] = pcs["gemstone"]
	return totals


def set_material_totals(doc, method=None):
	"""Stamp the six header totals. Wired to Stock Entry ``validate``.

	``validate`` and not ``before_validate`` for three independent reasons: the earlier hook
	runs ``CustomStockEntry.update_batches``, which REPLACES ``self.items`` wholesale when it
	splits rows per batch, so anything summed before it is summed over rows that no longer
	exist; the six fields are ``allow_on_submit = 0``, so the write has to land at or before
	validate; and ``custom_variant_of`` is only trustworthy from ``before_validate`` onward,
	where ``doc_events/stock_entry`` re-derives it from the Item in one bulk query rather
	than trusting the posted value.
	"""
	totals = material_totals(doc.get("items"))
	for key, fieldname in TARGET_FIELDS.items():
		if hasattr(doc, fieldname) or doc.meta.has_field(fieldname):
			doc.set(fieldname, totals[key])
