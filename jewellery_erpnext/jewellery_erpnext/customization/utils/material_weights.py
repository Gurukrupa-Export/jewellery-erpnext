# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Raw-material totals for the Stock Entry header.

Fills six header fields from the item rows, bucketed by the MATERIAL FAMILY each row
belongs to rather than by the item itself::

    custom_total_metal_weight     <- M, ML      row qty, grams
    custom_total_finding_weight   <- F, FL      row qty, grams
    custom_total_diamond_weight   <- D          row qty, carats -> grams
    custom_total_gemstone_weight  <- G          row qty, carats -> grams
    custom_total_diamond_pcs      <- D          row pcs
    custom_total_gemstone_pcs     <- G          row pcs

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

Carat conversion happens ONCE, on the bucket total, per ``utils.carat_to_gram`` and the drift
note in ``mop_log.recalculate_manufacturing_operation_weights``: rounding every row before
summing made 0.497 ct + 0.067 ct come to 0.112 g where the carat total converts to 0.113 g.
"""

from frappe.utils import cint, flt

from jewellery_erpnext.utils import carat_to_gram

# Whole-value match, never a prefix -- see the module docstring.
VARIANT_BUCKETS = {
	"M": "metal",
	"ML": "metal",
	"F": "finding",
	"FL": "finding",
	"D": "diamond",
	"G": "gemstone",
}

# The families measured in carats, and therefore the only ones that carry a pcs count.
STONE_BUCKETS = ("diamond", "gemstone")

CARAT_UOM = "Carat"
WEIGHT_PRECISION = 3

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

	Weights are grams at precision 3; pcs are integers. Pure -- no document, no DB read
	beyond the rounding mode ``flt`` consults -- so the arithmetic can be tested directly.
	"""
	grams = {"metal": 0.0, "finding": 0.0, "diamond": 0.0, "gemstone": 0.0}
	carats = {"diamond": 0.0, "gemstone": 0.0}
	pcs = {"diamond": 0, "gemstone": 0}

	for row in rows or []:
		bucket = _bucket(row)
		if not bucket:
			continue

		qty = flt(_get(row, "qty"))
		if bucket in STONE_BUCKETS:
			# Diamond and gemstone are Carat on every row on the live site, but a row that
			# is not carries a weight already -- scaling it by 0.2 would invent a loss.
			if (_get(row, "uom") or _get(row, "stock_uom")) == CARAT_UOM:
				carats[bucket] += qty
			else:
				grams[bucket] += qty
			# pcs is a Data field on Stock Entry Detail, so it arrives as a string.
			# Summed as given: this is a document total, not a running balance, so the
			# negative-clamping mop_log applies to a ledger has no place here.
			pcs[bucket] += cint(_get(row, "pcs"))
		else:
			grams[bucket] += qty

	# Convert once, on the total -- never per row.
	for bucket in STONE_BUCKETS:
		grams[bucket] += carat_to_gram(carats[bucket], WEIGHT_PRECISION)

	totals = {key: flt(value, WEIGHT_PRECISION) for key, value in grams.items()}
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
