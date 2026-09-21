"""Restore the Batch Rate that the Repack-Metal Conversion blend wrote to zero.

THE DEFECT
----------
``batch.on_update`` blends the source batches' rates onto a conversion target, and it took its
inputs from ``Batch.custom_origin_entries.rate`` -- a copy of ``Serial and Batch Entry.incoming_rate``
frozen on bundle ``after_insert``.

``StockEntry.on_submit`` creates those bundles (``make_bundle_using_old_serial_batch_fields``)
BEFORE ``update_stock_ledger`` submits and prices them, and ERPNext never prices a Stock Entry
bundle while it is a draft. So any produced row that already carries a ``batch_no`` at submit --
which is every batch ``customer_subcontracting.batch_rename`` mints for a customer lane, and
deliberately NOT what a Regular lane gets -- froze ``rate = 0``. The blend then ended in an
unconditional ``db_set``, overwriting the correct rate ``create_child_batches`` had stamped at
``before_submit``.

Measured on kg-gk: origin rows written at 12:01:55.032029, the rates they wanted at .156075
(159000.00) and .174223 (62.00). The FINAL ``incoming_rate`` values are all correct -- only the
frozen copies are 0. The frozen copy is 0 on all 24 customer-lane origin rows while 33,512 of
33,784 rows site-wide are fine: it is per-flow, not per-run, and for conversions it never worked.

WHAT THIS REPAIRS
-----------------
Batches left at ``custom_metal_rate = 0``. On kg-gk that was 9 of 24 customer-owned batches, but
only TWO are primary corruption -- the Repack-Metal Conversion targets. The other seven inherited
the zero through ``carry_rates_from_source_batches``, which faithfully carried a rate that was
already wrong.

Candidates are the roots plus the transitive closure of whatever carried their zero forward, walked
to a FIXPOINT in creation order. Creation order alone is not enough: a conversion root can be created
after a batch derived from an earlier root, and blending that root against a dependent still reading
0 would store a wrong NON-ZERO rate that no re-run could correct, because every query here excludes
non-zero batches.

Two recovery sources are tried per batch, in order:

1. Re-run the blend from the source batches' own ``custom_metal_rate`` / ``custom_alloy_rate``,
   over THIS batch's own origin rows. Those masters are final today, and the origin rows are
   already lane-scoped by ``_conversion_lane_map``. This yields the business-correct number, not
   merely a non-zero one, and is the same rule the fixed ``batch.on_update`` now applies.
2. ``Stock Entry Detail.basic_rate`` of the row named by ``Batch.custom_voucher_detail_no`` --
   exactly what ``batch_rename._source_row_rate`` stamped and what the guard now preserves. It is
   the TARGET ROW's own rate, so it is lane-scoped by construction.

A third source was considered and rejected: averaging the voucher's submitted outward
``Serial and Batch Entry.incoming_rate``. It keys only on ``voucher_no``, so it pools every
ownership lane, both the metal and the alloy pool, and every item, and it applies no purity
conversion. On MAT-STE-17890 that average is 145876.6788990825688073 -- which is merely
``basic_rate`` again, i.e. source 2 by a longer route -- while a multi-lane conversion would have
blended one customer's gold rate into another's batch. ``_conversion_lane_map``
(``serial_and_batch_bundle/doc_events/utils.py:63-100``) exists precisely to stop that.

NEVER ``Stock Entry Detail.custom_metal_rate``: it is ``fetch_from: batch_no.custom_metal_rate``
with no ``fetch_if_empty``, so it mirrors the corrupt zero.

NO STOCK OR ACCOUNTING IS TOUCHED. This writes two Batch fields and, in a second pass, the
read-only mirror on Stock Entry Detail. It changes no ``basic_rate``, no Stock Ledger Entry, no GL
Entry and no Bin -- ``SELECT SUM(stock_value) FROM tabBin`` must be bit-identical before and after.

The second pass exists because a submitted Stock Entry Detail row never re-saves, so the
``fetch_from`` would never re-pull the healed value and the costing readers
(``manufacturing_operation._snc_se_detail_maps``, ``get_stock_entry_data``) would keep seeing 0.

OUT OF SCOPE, deliberately: FG BOMs already costed from the stale zero keep their stored
``custom_kg_cost_*`` / ``custom_gk_cost_*`` amounts. Recomputing those is a separate, business
approved operation.

Safe to re-run: every filter is ``IFNULL(..., 0) = 0``, so a second run finds no candidates.
Batches that already hold a correct rate are never in the candidate set and are never written.

Ad-hoc, restricted to named batches for a trial run::

    bench --site <site> execute jewellery_erpnext.patches.backfill_blended_batch_metal_rate.execute
"""

import frappe
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.batch.batch import (
	ALLOY_SOURCE_RATE_FIELDS,
	METAL_SOURCE_RATE_FIELDS,
	PURITY_TOLERANCE,
	_resolve_metal_purity,
)

# The blend that causes this defect did not exist before commit eb260a21, "feat: fetch metal and
# alloy rate for repack metal conversion stock entry" (2025-07-22); ``custom_origin_entries`` itself
# landed four days earlier. A batch created before that was never touched by the code being fixed,
# so its zero has some other cause -- most of them simply predate the field being populated at all.
#
# Without this cutoff the candidate set on the gk site is 718 batches, 591 of them from 2024, and
# the ``basic_rate`` fallback would happily invent a rate for every one. With it, gk yields 127 and
# kg-gk and alfarsi are unchanged at 2 and 5. Repairing history means repairing THIS defect's
# history, not backfilling a field that was never filled.
BLEND_INTRODUCED = "2025-07-22"

# Restrict the run to these batch names. Empty means every candidate on the site.
ONLY_BATCHES = []


def _is_alloy(item_code, cache):
	"""Same classification the blend uses: item group, or a single-attribute variant."""
	if item_code in cache:
		return cache[item_code]

	row = frappe.db.get_value("Item", item_code, ["item_group"], as_dict=True)
	if not row:
		cache[item_code] = False
		return False

	attributes = frappe.db.count("Item Variant Attribute", {"parent": item_code})
	cache[item_code] = row.item_group == "Alloy" or attributes == 1
	return cache[item_code]


def _first_rate(source, fieldnames):
	"""First populated rate field on a source Batch, in preference order.

	Mirrors ``batch._origin_row_rate``'s pool handling so a healed batch gets exactly the rate the
	live blend would now produce. An alloy source accepts either field because the
	``custom_is_alloy_group`` master flag decides where the writer stamps and is unset on kg-gk; a
	metal source accepts ``custom_metal_rate`` only, because ``custom_alloy_rate`` on a metal batch
	is the alloy blended into it, not its own rate.
	"""
	for fieldname in fieldnames:
		rate = flt(source.get(fieldname))
		if rate:
			return rate
	return 0.0


def _blend_from_source_batches(batch, origins, alloy_cache):
	"""Recovery 1 -- the fixed blend, qty-weighted and purity-converted, per pool."""
	names = {row.batch_no for row in origins if row.batch_no}
	rates = (
		{
			b.name: b
			for b in frappe.get_all(
				"Batch",
				filters={"name": ("in", list(names))},
				fields=["name", "custom_metal_rate", "custom_alloy_rate"],
			)
		}
		if names
		else {}
	)

	target_purity = _resolve_metal_purity(batch.item)
	alloy_value = alloy_qty = 0.0
	metal_value = metal_qty = 0.0

	for row in origins:
		row_qty = flt(row.qty) or 1.0
		source = rates.get(row.batch_no) or {}

		if _is_alloy(row.item_code, alloy_cache):
			alloy_value += _first_rate(source, ALLOY_SOURCE_RATE_FIELDS) * row_qty
			alloy_qty += row_qty
			continue

		source_rate = _first_rate(source, METAL_SOURCE_RATE_FIELDS)
		source_purity = _resolve_metal_purity(row.item_code)
		if target_purity and abs(source_purity - target_purity) > PURITY_TOLERANCE:
			source_rate = (source_rate * target_purity) / 100
		metal_value += source_rate * row_qty
		metal_qty += row_qty

	return (
		(alloy_value / alloy_qty) if alloy_qty else 0.0,
		(metal_value / metal_qty) if metal_qty else 0.0,
	)


def _rate_from_voucher_row(batch):
	"""Recovery 3 -- what ``batch_rename._source_row_rate`` stamped at before_submit."""
	if not batch.custom_voucher_detail_no:
		return 0.0

	return flt(
		frappe.db.get_value(
			"Stock Entry Detail", batch.custom_voucher_detail_no, "basic_rate"
		)
	)


def _conversion_roots():
	"""Batches the Repack-Metal Conversion blend itself zeroed.

	Scoped to that voucher type deliberately. ``batch.on_update`` returns early for every other
	stock entry type, so no other batch was corrupted BY THIS DEFECT, and a zero elsewhere is
	someone else's bug or a legitimately unrated batch. Widening the net would rewrite hundreds of
	unrelated batches on a live site.
	"""
	return frappe.db.sql_list(
		"""
		SELECT b.name
		FROM `tabBatch` b
		JOIN `tabStock Entry` se ON se.name = b.reference_name AND se.docstatus = 1
		WHERE b.reference_doctype = 'Stock Entry'
		  AND se.stock_entry_type = 'Repack-Metal Conversion'
		  AND IFNULL(b.custom_metal_rate, 0) = 0
		  AND b.creation >= %(cutoff)s
		  AND EXISTS (
			SELECT 1 FROM `tabBatch MultiSelect` o
			WHERE o.parent = b.name AND o.parenttype = 'Batch'
		  )
		ORDER BY b.creation
		""",
		{"cutoff": BLEND_INTRODUCED},
	)


def _dependents_of(healed):
	"""Zero-rate batches that carried a zero forward from something we just repaired.

	``carry_rates_from_source_batches`` faithfully copies whatever the source held, so a poisoned
	conversion batch pushes its zero into every scrap, finding and repack batch derived from it.
	Those are in scope BECAUSE the root was -- and only those: a zero-rate batch whose sources were
	all healthy was never a victim of this defect.
	"""
	if not healed:
		return []

	return frappe.db.sql_list(
		"""
		SELECT DISTINCT o.parent
		FROM `tabBatch MultiSelect` o
		JOIN `tabBatch` b ON b.name = o.parent
		WHERE o.parenttype = 'Batch'
		  AND o.batch_no IN %(healed)s
		  AND IFNULL(b.custom_metal_rate, 0) = 0
		  AND b.creation >= %(cutoff)s
		ORDER BY b.creation
		""",
		{"healed": tuple(healed), "cutoff": BLEND_INTRODUCED},
	)


def _mirror_onto_stock_entry_detail(batch_no, rate):
	"""Fill the read-only ``fetch_from`` copy on rows that will never re-save."""
	frappe.db.sql(
		"""
		UPDATE `tabStock Entry Detail`
		SET custom_metal_rate = %s
		WHERE batch_no = %s AND IFNULL(custom_metal_rate, 0) = 0
		""",
		(rate, batch_no),
	)


def _collect_candidates():
	"""Every batch this defect could have zeroed: the conversion roots plus their closure.

	Returned in CREATION ORDER, which makes the common case -- a root older than everything
	derived from it -- converge in a single pass.
	"""
	seen = set()
	ordered = []
	frontier = _conversion_roots()

	while frontier:
		fresh = [name for name in frontier if name not in seen]
		if not fresh:
			break
		seen.update(fresh)
		ordered.extend(fresh)
		frontier = _dependents_of(fresh)

	if not ordered:
		return []

	rows = frappe.get_all(
		"Batch",
		filters={"name": ("in", ordered)},
		fields=["name"],
		order_by="creation",
	)
	return [row.name for row in rows]


def _repair_one(name, alloy_cache, sources_used):
	"""Recover and write one batch's rate. Returns the written rate, or 0.0 if unrecoverable."""
	batch = frappe.db.get_value(
		"Batch",
		name,
		[
			"name",
			"item",
			"reference_name",
			"custom_voucher_detail_no",
			"custom_alloy_rate",
		],
		as_dict=True,
	)
	if not batch:
		return 0.0

	origins = frappe.get_all(
		"Batch MultiSelect",
		filters={"parent": name, "parenttype": "Batch"},
		fields=["batch_no", "item_code", "qty"],
	)
	# ``item_code`` was only fetched onto these rows later; fall back to the source Batch.
	for row in origins:
		if not row.item_code and row.batch_no:
			row.item_code = frappe.db.get_value("Batch", row.batch_no, "item")

	alloy_rate, metal_rate = _blend_from_source_batches(batch, origins, alloy_cache)
	used = "blend"

	if not metal_rate:
		metal_rate = _rate_from_voucher_row(batch)
		used = "voucher_row"

	if not metal_rate:
		return 0.0

	frappe.db.set_value(
		"Batch", name, "custom_metal_rate", metal_rate, update_modified=False
	)
	# Only where the pool actually carried something, and only over a zero -- the same contract
	# the runtime guard now enforces.
	if alloy_rate and not flt(batch.custom_alloy_rate):
		frappe.db.set_value(
			"Batch", name, "custom_alloy_rate", alloy_rate, update_modified=False
		)

	_mirror_onto_stock_entry_detail(name, metal_rate)
	sources_used[used] += 1
	return metal_rate


def execute():
	# custom_fields/ JSON is not auto-loaded on this bench (``after_migrate`` is commented out at
	# hooks.py:12), so the column can legitimately be absent on a site that never ran the field
	# patch. Nothing to repair there.
	if not frappe.db.has_column("Batch", "custom_metal_rate"):
		return

	if not frappe.db.table_exists("Batch MultiSelect"):
		return

	alloy_cache = {}
	healed = 0
	sources_used = {"blend": 0, "voucher_row": 0}

	candidates = _collect_candidates()
	if ONLY_BATCHES:
		wanted = set(ONLY_BATCHES)
		candidates = [name for name in candidates if name in wanted]

	# A FIXPOINT, not one ordered walk.
	#
	# Creation order is the right first guess but it is not a guarantee: a conversion root can be
	# created AFTER a batch that derives from an earlier root, so repairing all roots first would
	# blend that later root against a dependent still reading 0. It would then hold a wrong
	# NON-ZERO rate, which every subsequent query excludes -- no re-run could correct it.
	#
	# Repeating until a pass writes nothing removes the dependence on order entirely. Each pass
	# either heals at least one batch or ends the loop, so it runs at most len(candidates) times.
	while True:
		wrote = False
		for name in candidates:
			if flt(frappe.db.get_value("Batch", name, "custom_metal_rate")):
				continue
			if _repair_one(name, alloy_cache, sources_used):
				healed += 1
				wrote = True
		if not wrote:
			break

	frappe.db.commit()
	frappe.logger().info(
		f"{__name__}: healed {healed} batch rate(s) "
		f"(blend={sources_used['blend']}, voucher_row={sources_used['voucher_row']})"
	)
