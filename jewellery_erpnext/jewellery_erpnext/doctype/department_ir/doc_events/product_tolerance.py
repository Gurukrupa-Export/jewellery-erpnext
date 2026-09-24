# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Block a Department IR Issue whose weight is outside the PMO's product tolerance.

Armed per department: only when the ``next_department`` -- the one about to receive the
work -- has ``custom_apply_product_tolerance`` ticked. The bands come from the Parent
Manufacturing Order's own tolerance tables, stamped there at PMO submit from the
customer's Customer Product Tolerance Master, so a later edit of that master cannot
retroactively block work already in progress.

Wired on ``validate`` rather than ``before_submit`` so the error lands the moment the
Department IR is saved, not after the operator has assembled the whole document. Frappe
runs ``validate`` on both save and submit, so one hook covers both.

On the SAVE path ``before_validate`` runs first and resolves each row's weights via
``validate_and_update_gross_wt_from_mop``, so the grid weights -- including its
previous-MOP fallback -- are fresh. On the SUBMIT path they are not: that whole body is
wrapped in ``if self.docstatus != 1`` (department_ir.py:29) and Frappe sets docstatus
before the submit-save, so the check sees whatever was persisted at the last draft save.
That is the intended trade -- it validates exactly the numbers shown in the grid and in
get_summary_data -- but it does mean an operation whose weight changed between draft and
submit is judged on the older figure.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.customer_product_tolerance_master.tolerance_utils import (
	group_tolerance_rows,
)

# Manufacturing Operation declares its weight fields at precision 3 and the app pins
# System Settings float_precision to 3, while the PMO bands are stored as round(x, 4).
# Rounding BOTH sides to 3 before comparing means a float-noise boundary such as
# 16.0500001 against a limit of 16.05 compares equal instead of throwing.
WEIGHT_PRECISION = 3

# Stone band types that describe a PRODUCT TOTAL, and so are comparable to the single
# total a Department IR row carries. Both are stamped by set_diamond_tolerance_table's
# else-branch, which aggregates the whole BOM detail table -- keep this set in step with
# that branch. "MM Size wise" and "Group Size wise" are per-sieve subtotals and
# "Weight Range" / "Gemstone Type Range" are per-shape / per-type subtotals; those are
# skipped rather than mis-compared against a total.
TOTAL_WEIGHT_TYPES = {"Weight wise", "Universal"}

# Columns that narrow a stone band to part of the product. set_diamond_tolerance_table
# and set_gemstone_tolerance_table scope their BOM aggregate by these, so a band with
# one set is a SUBTOTAL -- not comparable to the single total a Department IR row
# carries, for the same reason the per-sieve weight types above are excluded.
SCOPE_FIELDS = {
	"diamond": ("diamond_type",),
	"gemstone": ("gemstone_type", "gemstone_shape"),
}

_MWO_FIELDS = (
	"name",
	"manufacturing_order",
	"metal_type",
	"is_finding_mwo",
	"for_fg",
	"qty",
)


def validate_product_tolerance(doc, method=None):
	"""Refuse the transfer when a work order's weight is outside its tolerance band."""
	if getattr(doc, "type", None) != "Issue":
		# Receive-leg rows are clones carrying the same weights, and blocking a Receive
		# would strand physical metal in the in-transit warehouse with no legal way out.
		return
	# Deliberately NOT gated on doc.is_finding. That field is
	# fetch_from: next_department.custom_is_finding -- it describes the DESTINATION
	# department, not the cargo, and Tagging / Final Polish / Central all carry the flag.
	# Skipping on it would disable this check for exactly the departments where a
	# product-level weight band is meaningful (34% of the Issues on the dev site, all of
	# them the Final Polish -> Tagging hand-offs this feature exists for). Finding
	# COMPONENTS are excluded precisely and per row by the is_finding_mwo test in
	# bucket_rows_by_pmo, which is the right granularity.
	rows = doc.get("department_ir_operation") or []
	if not rows or not doc.next_department:
		return

	# get_cached_value, not db.get_value: db.get_value names the column in the SELECT and
	# raises "Unknown column" on any site that has not got the custom field yet, which
	# would break every Department IR submit. get_cached_value reads the whole doc and
	# returns None for a missing field, so the feature degrades to "off".
	if not cint(
		frappe.get_cached_value(
			"Department", doc.next_department, "custom_apply_product_tolerance"
		)
	):
		return

	failures = get_tolerance_failures(doc, rows)
	if failures:
		frappe.throw(title=_("Product Tolerance Exceeded"), msg="<br>".join(failures))


def get_tolerance_failures(doc, rows):
	"""Human-readable failure lines, empty when every work order is within tolerance.

	Never throws, so it can be unit-tested and reused by a report.
	"""
	mwo_names = sorted(
		{row.manufacturing_work_order for row in rows if row.manufacturing_work_order}
	)
	if not mwo_names:
		return []

	# Resolve the PMO from the Manufacturing Work Order rather than trusting
	# row.parent_manufacturing_order: that is a two-hop fetch_from chain
	# (manufacturing_operation -> manufacturing_work_order -> manufacturing_order) which
	# is not guaranteed to have resolved by the time this validation runs.
	mwo_map = {
		mwo.name: mwo
		for mwo in frappe.get_all(
			"Manufacturing Work Order",
			filters={"name": ["in", mwo_names]},
			fields=list(_MWO_FIELDS),
		)
	}

	buckets = bucket_rows_by_pmo(rows, mwo_map)
	if not buckets:
		return []

	bands = get_tolerance_bands(list(buckets))

	failures = []
	for pmo, bucket in buckets.items():
		pmo_bands = bands.get(pmo) or {}
		failures += _check_metal(doc, pmo, bucket, pmo_bands.get("metal") or [])
		failures += _check_stone(
			doc,
			pmo,
			bucket,
			pmo_bands.get("diamond") or [],
			"diamond_wt",
			_("Diamond"),
			SCOPE_FIELDS["diamond"],
		)
		failures += _check_stone(
			doc,
			pmo,
			bucket,
			pmo_bands.get("gemstone") or [],
			"gemstone_wt",
			_("Gemstone"),
			SCOPE_FIELDS["gemstone"],
		)
	return failures


def bucket_rows_by_pmo(rows, mwo_map):
	"""Sum the Department IR rows of each Parent Manufacturing Order into one bucket.

	create_manufacturing_work_order mints one MWO per BOM Metal Detail row, so a
	two-metal design has two producing work orders that together make one product. The
	tolerance band describes the finished product, so the rows must be summed before
	being compared to it.
	"""
	grouped = {}
	for row in rows:
		mwo = mwo_map.get(row.manufacturing_work_order)
		if not mwo or cint(mwo.is_finding_mwo):
			continue
		pmo = mwo.manufacturing_order or row.get("parent_manufacturing_order")
		if pmo:
			grouped.setdefault(pmo, []).append((row, mwo))

	buckets = {}
	for pmo, pairs in grouped.items():
		# sync_mwo_weights re-aggregates every sibling MWO onto the FG MWO when the work
		# reaches Tagging, so counting both the FG row and its producing rows would
		# double the product weight. Keep the producing rows when any are present; an
		# Issue carrying only the FG MWO keeps it, which is right because it then IS the
		# whole product.
		if any(not cint(mwo.for_fg) for _row, mwo in pairs):
			pairs = [(row, mwo) for row, mwo in pairs if not cint(mwo.for_fg)]

		bucket = frappe._dict(
			gross_wt=0.0,
			net_wt=0.0,
			finding_wt=0.0,
			diamond_wt=0.0,
			gemstone_wt=0.0,
			metal_types=set(),
			mops=[],
			mwos=[],
			qty=1,
		)
		for row, mwo in pairs:
			bucket.gross_wt += flt(row.gross_wt)
			bucket.net_wt += flt(row.net_wt)
			bucket.finding_wt += flt(row.finding_wt)
			bucket.diamond_wt += flt(row.diamond_wt)
			bucket.gemstone_wt += flt(row.gemstone_wt)
			if mwo.metal_type:
				bucket.metal_types.add(mwo.metal_type)
			if row.manufacturing_operation:
				bucket.mops.append(row.manufacturing_operation)
			bucket.mwos.append(mwo.name)
			bucket.qty = max(bucket.qty, cint(mwo.qty) or 1)
		buckets[pmo] = bucket
	return buckets


def get_tolerance_bands(pmo_names):
	"""``{pmo: {"metal"|"diamond"|"gemstone": [band rows]}}`` in three batched reads."""
	doctypes = {
		"metal": "Metal Product Tolerance",
		"diamond": "Diamond Product Tolerance",
		"gemstone": "Gemstone Product Tolerance",
	}
	bands = {}
	for category, doctype in doctypes.items():
		fields = [
			"parent",
			"from_tolerance_wt",
			"to_tolerance_wt",
			"standard_tolerance_wt",
		]
		if category == "metal":
			fields.append("metal_type")
		# weight_type on Metal Product Tolerance and diamond_type on Diamond Product
		# Tolerance were both added alongside this feature; guard every optional read so
		# the module still works on a site that has not migrated yet.
		meta = frappe.get_meta(doctype)
		for optional in ("weight_type",) + SCOPE_FIELDS.get(category, ()):
			if meta.has_field(optional):
				fields.append(optional)
		for row in frappe.get_all(
			doctype,
			# parenttype matters: these child tables must not be read across parents.
			filters={
				"parent": ["in", pmo_names],
				"parenttype": "Parent Manufacturing Order",
			},
			fields=fields,
		):
			bands.setdefault(row.parent, {}).setdefault(category, []).append(row)
	return bands


def _scaled_band(band, qty):
	"""The stored band is per piece; a Department IR row covers the whole work order."""
	factor = cint(qty) or 1
	return (
		flt(flt(band.get("from_tolerance_wt")) * factor, WEIGHT_PRECISION),
		flt(flt(band.get("to_tolerance_wt")) * factor, WEIGHT_PRECISION),
	)


def _metal_actual(bucket, band):
	"""The weight a metal band should be compared against.

	A "Net Weight" band is built from the BOM's metal_and_finding_weight, whereas
	Manufacturing Operation.net_wt is metal ONLY -- mop_log.update_wt_detail keeps
	findings in their own bucket. So the counterpart of a Net band is net + finding, not
	net. A blank weight_type means a legacy row written before the field existed; the
	populator's own fallback for anything that is not "Gross Weight" was the net basis,
	so blank reads as Net.
	"""
	if (band.get("weight_type") or "Net Weight") == "Gross Weight":
		return flt(bucket.gross_wt)
	return flt(bucket.net_wt) + flt(bucket.finding_wt)


def _nearest_miss(misses):
	"""The band whose edge is closest to the actual -- the one they were aiming at."""
	return min(misses, key=lambda m: min(abs(m[0] - m[1]), abs(m[0] - m[2])))


def _check_metal(doc, pmo, bucket, band_rows):
	if not band_rows:
		return []
	# Zero is not a violation: a refined MWO legitimately arrives at zero because the
	# Refining Entry moved the metal out, and so does a finding whose stock was consumed.
	# Keying on the weight rather than on is_mwo_refined avoids permanently un-gating any
	# work order that was ever refined.
	if not flt(bucket.gross_wt, WEIGHT_PRECISION) and not flt(
		bucket.net_wt, WEIGHT_PRECISION
	):
		return []

	# Narrow to this order's metal; fall back to universal (blank metal_type) rows.
	candidates = [b for b in band_rows if b.get("metal_type") in bucket.metal_types]
	if not candidates:
		candidates = [b for b in band_rows if not b.get("metal_type")]
	if not candidates:
		# The PMO carries bands, but none for this order's metal and no universal row.
		# A Gold-only schedule must not be used to police a Platinum work order --
		# falling back to every band would reject a legitimate transfer against a limit
		# that was never meant to apply to it.
		return []

	return _check_bands(doc, pmo, bucket, candidates, _("Metal"), _("g"), _metal_actual)


def _check_bands(doc, pmo, bucket, candidates, label, uom, actual_for):
	"""One verdict per weight_type, not one for the whole set.

	Gross and Net bands measure different things, so a work order that is inside its Net
	band tells you nothing about whether it is inside its Gross band -- checking them as
	one pool would let a gross overrun ride through on a passing net figure. Within a
	single weight_type, being inside ANY band still passes: PMOs stamped before this
	feature carry a whole schedule rather than the one applicable row, and that fails
	open on legacy data rather than blocking real work on it.
	"""
	failures = []
	for weight_type, group in group_tolerance_rows(
		candidates, lambda band: band.get("weight_type") or ""
	).items():
		misses = []
		for band in group:
			actual = flt(actual_for(bucket, band), WEIGHT_PRECISION)
			lower, upper = _scaled_band(band, bucket.qty)
			if lower <= actual <= upper:
				misses = []
				break
			misses.append((actual, lower, upper))

		if misses:
			actual, lower, upper = _nearest_miss(misses)
			failures.append(
				_failure_line(
					doc,
					pmo,
					bucket,
					"{0} ({1})".format(label, weight_type) if weight_type else label,
					actual,
					lower,
					upper,
					uom,
				)
			)
	return failures


def _check_stone(doc, pmo, bucket, band_rows, weight_field, label, scope_fields=()):
	# Carats throughout: the bands are built from BOM detail quantities, which are carats,
	# and so is Manufacturing Operation.diamond_wt / gemstone_wt. Never the _in_gram twins.
	actual = flt(bucket.get(weight_field), WEIGHT_PRECISION)
	if not actual:
		return []

	# A band scoped to one diamond type, gemstone type or shape covers part of the
	# product, while the operation reports one total across all of them. Comparing the
	# two would judge a subtotal's limits against the whole -- skipped, as the per-sieve
	# weight types already are. Only unscoped whole-product bands are enforceable here.
	candidates = [
		b
		for b in band_rows
		if b.get("weight_type") in TOTAL_WEIGHT_TYPES
		and not any(b.get(field) for field in scope_fields)
	]
	if not candidates:
		return []

	return _check_bands(
		doc, pmo, bucket, candidates, label, _("cts"), lambda _bucket, _band: actual
	)


def _failure_line(doc, pmo, bucket, label, actual, lower, upper, uom):
	return _(
		"{pmo} (Work Order {mwo}, Operation {mop}): {label} is <b>{actual} {uom}</b>, "
		"outside the product tolerance required by department {dept} — allowed "
		"<b>{lower} {uom}</b> to <b>{upper} {uom}</b>."
	).format(
		pmo=frappe.bold(pmo),
		mwo=", ".join(sorted(set(bucket.mwos))),
		mop=", ".join(sorted(set(bucket.mops))) or _("n/a"),
		label=label,
		actual=flt(actual, WEIGHT_PRECISION),
		lower=flt(lower, WEIGHT_PRECISION),
		upper=flt(upper, WEIGHT_PRECISION),
		dept=frappe.bold(doc.next_department),
		uom=uom,
	)


# ---------------------------------------------------------------------------------------------
# F6 -- the same bands, checked once more on the finished piece at Serial Number Creator.
#
# KLHGX62F1119 shipped two diamonds and a finding short of its design (0.396 ct against a
# 0.452-0.520 ct band, 5.440 g of metal against 3.787-4.357 g) and nothing stopped it: the
# Department IR check is armed per destination department and no KGJPL department had it on,
# and it skips diamond bands scoped to a diamond type. At the SNC the whole piece is known --
# every stone with its type -- so a scoped band can be checked against its own subtotal.
#
# Armed the same way as the Department IR check, by the SNC department's
# ``custom_apply_product_tolerance``, so nothing changes until a department is switched on.
# Outside tolerance the SNC is refused unless a Product Tolerance Approver records a reason.

TOLERANCE_APPROVER_ROLE = "Product Tolerance Approver"

#: The Item attribute that carries each band scope field, for reading an as-built piece.
SCOPE_ATTRIBUTES = {
	"diamond_type": "Diamond Type",
	"gemstone_type": "Gemstone Type",
	"gemstone_shape": "Stone Shape",
}

#: Grams per carat, for a piece's gross weight -- as ``SerialNumberCreator._compute_total_weight``.
GRAMS_PER_CARAT = 0.2

_STONE_FIELD = {"D": "diamond_wt", "G": "gemstone_wt"}
_METAL_FIELD = {"M": "net_wt", "F": "finding_wt"}


def validate_snc_design_tolerance(snc):
	"""``before_submit`` for Serial Number Creator: the finished piece against its design bands."""
	if not snc.get("parent_manufacturing_order") or not snc.get("department"):
		return
	if not cint(
		frappe.get_cached_value(
			"Department", snc.department, "custom_apply_product_tolerance"
		)
	):
		return

	snc.custom_tolerance_override_by = None
	failures = get_snc_tolerance_failures(snc)
	if not failures:
		return

	reason = (snc.get("custom_tolerance_override_reason") or "").strip()
	if reason and TOLERANCE_APPROVER_ROLE in frappe.get_roles():
		snc.custom_tolerance_override_by = frappe.session.user
		return

	frappe.throw(
		title=_("Product Tolerance Exceeded"),
		msg="<br>".join(failures)
		+ "<br><br>"
		+ _(
			"The finished piece differs from its design beyond the product tolerance. A {0} "
			"may submit it after entering a Tolerance Override Reason."
		).format(frappe.bold(TOLERANCE_APPROVER_ROLE)),
	)


def get_snc_tolerance_failures(snc):
	"""Failure lines for every finished piece outside its PMO's bands; empty when all are within."""
	pmo = snc.parent_manufacturing_order
	bands = get_tolerance_bands([pmo]).get(pmo) or {}
	if not bands:
		return []

	metal_type = frappe.db.get_value("Parent Manufacturing Order", pmo, "metal_type")
	destination = frappe._dict(next_department=snc.department)

	pieces = _snc_pieces(snc)
	failures = []
	for piece_id, bucket in sorted(pieces.items()):
		bucket.metal_types = {metal_type} if metal_type else set()
		bucket.mwos = [snc.get("manufacturing_work_order") or _("n/a")]
		bucket.mops = [snc.get("manufacturing_operation") or ""]
		label = _("{0} piece {1}").format(pmo, piece_id) if len(pieces) > 1 else pmo
		failures += _check_metal(destination, label, bucket, bands.get("metal") or [])
		for kind, weight_field, stone_label in (
			("diamond", "diamond_wt", _("Diamond")),
			("gemstone", "gemstone_wt", _("Gemstone")),
		):
			failures += _check_piece_stone(
				destination,
				label,
				bucket,
				bands.get(kind) or [],
				kind,
				weight_field,
				stone_label,
			)
	return failures


def _snc_pieces(snc):
	"""``{piece id: weight bucket}`` from the SNC's as-built FG details.

	Each ``id`` is one finished piece, so each bucket is one piece and the per-piece bands
	apply unscaled. Rows with no id are one bucket covering every piece, scaled by their count.
	"""
	rows = [r for r in snc.get("fg_details") or [] if r.get("row_material")]
	facts = _item_facts({r.row_material for r in rows})

	ids = {cint(r.get("id")) for r in rows}
	unsplit = ids == {0}
	pieces = {}
	for row in rows:
		piece_id = cint(row.get("id")) or 1
		bucket = pieces.setdefault(
			piece_id,
			frappe._dict(
				gross_wt=0.0,
				net_wt=0.0,
				finding_wt=0.0,
				diamond_wt=0.0,
				gemstone_wt=0.0,
				scoped={},
				qty=_piece_count(snc) if unsplit else 1,
			),
		)
		info = facts.get(row.row_material) or {}
		kind = info.get("variant_of")
		qty = flt(row.get("qty"))
		field = _METAL_FIELD.get(kind) or _STONE_FIELD.get(kind)
		if not field:
			continue
		bucket[field] += qty
		for scope_field, value in (info.get("scope") or {}).items():
			bucket.scoped[(scope_field, value)] = (
				bucket.scoped.get((scope_field, value), 0.0) + qty
			)

	for bucket in pieces.values():
		bucket.gross_wt = (
			bucket.net_wt
			+ bucket.finding_wt
			+ (bucket.diamond_wt + bucket.gemstone_wt) * GRAMS_PER_CARAT
		)
	return pieces


def _piece_count(snc):
	from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator import (
		_resolve_snc_mnf_qty,
	)

	return cint(_resolve_snc_mnf_qty(snc)) or 1


def _item_facts(item_codes):
	"""``{item: {"variant_of": ..., "scope": {band field: attribute value}}}`` in two reads."""
	if not item_codes:
		return {}
	facts = {
		item.name: {"variant_of": item.variant_of, "scope": {}}
		for item in frappe.get_all(
			"Item",
			filters={"name": ["in", list(item_codes)]},
			fields=["name", "variant_of"],
		)
	}
	field_for = {attribute: field for field, attribute in SCOPE_ATTRIBUTES.items()}
	for attr in frappe.get_all(
		"Item Variant Attribute",
		filters={
			"parent": ["in", list(item_codes)],
			"attribute": ["in", list(field_for)],
		},
		fields=["parent", "attribute", "attribute_value"],
	):
		if attr.parent in facts and attr.attribute_value:
			facts[attr.parent]["scope"][
				field_for[attr.attribute]
			] = attr.attribute_value
	return facts


def _check_piece_stone(doc, label, bucket, band_rows, kind, weight_field, stone_label):
	"""A stone band on a finished piece, against its own scope's subtotal.

	Unlike the Department IR check, a scoped band is enforceable here: the piece's stones are
	known item by item, so "Natural diamonds, 0.452-0.520 ct" compares with the Natural
	subtotal. Per-sieve diamond bands (MM Size wise, Group Size wise) are still skipped. A
	piece with none of a stone its design bands for is outside the band, not exempt.
	"""
	if kind == "diamond":
		band_rows = [b for b in band_rows if b.get("weight_type") in TOTAL_WEIGHT_TYPES]
	if not band_rows:
		return []

	def actual_for(_bucket, band):
		for field in SCOPE_FIELDS[kind]:
			if band.get(field):
				return bucket.scoped.get((field, band.get(field)), 0.0)
		return bucket.get(weight_field)

	return _check_bands(
		doc, label, bucket, band_rows, stone_label, _("cts"), actual_for
	)
