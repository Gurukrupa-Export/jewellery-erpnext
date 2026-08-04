"""Give back WIP reservations that Product Certification wrongly consumed.

``ProductCertification`` built its SRE list from two queries. ``sre_list_1`` is correctly
scoped to the MWOs of the certified PMO; ``sre_list_2`` was filtered only by
``voucher_type="Sales Order"`` + ``voucher_no`` + ``item_code``. Every MWO on a Sales
Order shares ``voucher_no``, and the metal/finding item codes are common across the whole
order, so certifying one PMO handed every other MWO's reservation to
``consume_stock_reservation_entry`` -- which sets ``delivered_qty = reserved_qty``, sets
every ``Serial and Batch Entry.delivered_qty = qty`` and flips the status to "Delivered",
leaving ``docstatus = 1``.

Measured on the kg-gk site before the fix: one Product Certification covering 2 MWOs
marked 216 SREs across 115 MWOs as Delivered.

Consequences: the WIP metal is no longer reserved (ERPNext's Bin formula is
``reserved_qty - delivered_qty - transferred_qty - consumed_qty``, so a Delivered SRE
counts as zero), and Employee IR Process Loss cannot deduct against it.

This patch is the exact inverse of ``consume_stock_reservation_entry`` for the SREs that
were collateral damage, and is deliberately conservative -- it only restores a reservation
when it is unambiguously wrong AND still safe to take back.

Run standalone with a report first:

    bench --site <site> execute \
        jewellery_erpnext.patches.restore_over_consumed_pc_reservations.execute \
        --kwargs "{'dry_run': True}"
"""

import frappe
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.lock_order import lock_bins

# Float noise guard, same value/intent as loss_stock_entry.TOLERANCE.
TOLERANCE = 0.0001

# MOP states that mean the piece is still being worked on, so its raw material must
# still be reserved.
OPEN_MOP_STATUSES = ("WIP", "Not Started")


def execute(dry_run=False):
	"""Restore reservations consumed outside the certifying PMO's own MWO set."""
	if not frappe.db.has_column("Stock Reservation Entry", "manufacturing_work_order"):
		# Site never ran the jewellery MWO reservation flow; nothing to repair.
		return

	certified_mwos = _certified_mwos()
	candidates = _candidate_sres()

	restored, skipped = [], []

	# Availability is consumed CUMULATIVELY as we restore: 95 reservations of 3.4 g each
	# against 330 g of free stock is fine one at a time and a 20 g over-reservation in
	# aggregate. Track what earlier restores in this run have already claimed, keyed the
	# same way ERPNext meters it (warehouse level, and batch level for batched items).
	claimed_wh = {}
	claimed_batch = {}

	for sre in candidates:
		if sre.manufacturing_work_order in certified_mwos:
			# Legitimately consumed: this MWO's own PMO was certified.
			continue

		reason = _blocked_reason(sre, claimed_wh, claimed_batch)
		if reason:
			skipped.append((sre, reason))
			continue

		restored.append(sre)

	if restored and not dry_run:
		# RULE B: acquire every tabBin row lock this transaction needs up front, in the
		# canonical sorted order, before mutating any reservation state.
		lock_bins([(sre.item_code, sre.warehouse) for sre in restored])
		for sre in restored:
			_restore(sre)
		_refresh_bins(restored)

	_report(restored, skipped, dry_run)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _certified_mwos():
	"""Every MWO legitimately in scope of a submitted Product Certification.

	Mirrors ``sre_list_1`` in product_certification.py: the PC row names an MWO (or a
	PMO), and all MWOs of that PMO are in scope.
	"""
	pmos = set()
	rows = frappe.db.sql(
		"""
		SELECT pd.manufacturing_work_order, pd.parent_manufacturing_order
		FROM `tabProduct Details` pd
		INNER JOIN `tabProduct Certification` pc ON pc.name = pd.parent
		WHERE pc.docstatus = 1
		""",
		as_dict=True,
	)
	for row in rows:
		if row.parent_manufacturing_order:
			pmos.add(row.parent_manufacturing_order)
		elif row.manufacturing_work_order:
			pmo = frappe.db.get_value(
				"Manufacturing Work Order",
				row.manufacturing_work_order,
				"manufacturing_order",
			)
			if pmo:
				pmos.add(pmo)

	if not pmos:
		return set()

	return set(
		frappe.get_all(
			"Manufacturing Work Order",
			{"manufacturing_order": ["in", list(pmos)]},
			pluck="name",
		)
	)


def _candidate_sres():
	"""Submitted, fully-consumed, MWO-tagged SREs whose MWO is still in process."""
	return frappe.db.sql(
		"""
		SELECT
			sre.name, sre.item_code, sre.warehouse, sre.reserved_qty,
			sre.delivered_qty, sre.consumed_qty, sre.transferred_qty,
			sre.voucher_qty, sre.reservation_based_on,
			sre.manufacturing_work_order, sre.manufacturing_operation
		FROM `tabStock Reservation Entry` sre
		INNER JOIN `tabManufacturing Work Order` mwo
			ON mwo.name = sre.manufacturing_work_order
		INNER JOIN `tabManufacturing Operation` mop
			ON mop.name = mwo.manufacturing_operation
		WHERE sre.docstatus = 1
		  AND sre.status = 'Delivered'
		  AND sre.delivered_qty >= sre.reserved_qty
		  AND sre.transferred_qty = 0
		  AND sre.consumed_qty = 0
		  AND IFNULL(sre.manufacturing_work_order, '') != ''
		  AND mop.status IN %(open_statuses)s
		ORDER BY sre.item_code, sre.warehouse, sre.name
		""",
		{"open_statuses": OPEN_MOP_STATUSES},
		as_dict=True,
	)


def _blocked_reason(sre, claimed_wh, claimed_batch):
	"""``None`` when the reservation can be taken back, else a human-readable reason.

	Restoring must never over-reserve: the stock freed by the wrongful consumption may
	already have been reserved or consumed by another flow in the meantime, and each
	restore in this run eats into what is left for the next one.

	``claimed_wh`` / ``claimed_batch`` are running tallies of what this run has already
	committed to restore; both are updated in place when the SRE is accepted.
	"""
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_available_qty_to_reserve,
	)

	reserved_qty = flt(sre.reserved_qty, 3)
	if reserved_qty <= TOLERANCE:
		return "reserved_qty is zero"

	wh_key = (sre.item_code, sre.warehouse)
	already = flt(claimed_wh.get(wh_key, 0), 3)
	available = flt(
		get_available_qty_to_reserve(sre.item_code, sre.warehouse, ignore_sre=sre.name),
		3,
	)
	if (available - already) + TOLERANCE < reserved_qty:
		return (
			f"warehouse can only reserve {flt(available - already, 3)} of {reserved_qty} "
			f"({already} already claimed earlier in this run)"
		)

	sb_rows = []
	if sre.reservation_based_on == "Serial and Batch":
		sb_rows = frappe.get_all(
			"Serial and Batch Entry",
			{"parent": sre.name, "parenttype": "Stock Reservation Entry"},
			["batch_no", "qty"],
		)
		for sb in sb_rows:
			batch_key = (sre.item_code, sb.batch_no, sre.warehouse)
			batch_claimed = flt(claimed_batch.get(batch_key, 0), 3)
			physical = flt(
				_physical_batch_qty(sre.item_code, sb.batch_no, sre.warehouse), 3
			)
			if (physical - batch_claimed) + TOLERANCE < flt(sb.qty, 3):
				return (
					f"batch {sb.batch_no} has {flt(physical - batch_claimed, 3)} available "
					f"in {sre.warehouse}, needs {flt(sb.qty, 3)}"
				)

	# Accepted -- book what it claims so the next candidate sees a smaller pool.
	claimed_wh[wh_key] = already + reserved_qty
	for sb in sb_rows:
		batch_key = (sre.item_code, sb.batch_no, sre.warehouse)
		claimed_batch[batch_key] = flt(claimed_batch.get(batch_key, 0), 3) + flt(
			sb.qty, 3
		)

	return None


def _physical_batch_qty(item_code, batch_no, warehouse):
	"""Physical SBB qty of (item, batch) at ``warehouse``, reservations ignored.

	v16 keeps real per-batch stock in Serial and Batch Bundle, not
	``tabStock Ledger Entry.batch_no`` (which is NULL).
	"""
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	if not batch_no or not warehouse:
		return 0.0
	try:
		return flt(
			get_batch_qty(batch_no, warehouse, item_code, ignore_reserved_stock=True), 3
		)
	except Exception:
		return 0.0


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def _restore(sre):
	"""Undo ``consume_stock_reservation_entry`` for one SRE.

	These are submitted documents whose qty fields are read-only, so this writes through
	``db_set`` / ``db_update`` exactly as the consuming helper did. A cancel-and-recreate
	would mint a new SRE name and break every doc that references this one (MOP Log,
	MOP EOD Sync Log Item), so an in-place un-delivery is both safer and cheaper.

	The caller holds the Bin row locks (RULE B).
	"""
	doc = frappe.get_doc("Stock Reservation Entry", sre.name)
	doc.flags.ignore_permissions = True

	for entry in doc.sb_entries:
		if flt(entry.delivered_qty):
			entry.delivered_qty = 0
			entry.db_update()

	doc.db_set("delivered_qty", 0, update_modified=False)
	doc.delivered_qty = 0
	# Recomputes Reserved / Partially Reserved from voucher_qty vs reserved_qty.
	doc.update_status(update_modified=False)


def _refresh_bins(restored):
	"""Recompute ``Bin.reserved_stock`` once per affected (item, warehouse)."""
	from erpnext.stock.utils import get_or_make_bin

	pairs = sorted({(sre.item_code, sre.warehouse) for sre in restored})
	for item_code, warehouse in pairs:
		try:
			frappe.clear_document_cache("Bin")
			bin_doc = frappe.get_cached_doc(
				"Bin", get_or_make_bin(item_code, warehouse)
			)
			bin_doc.update_reserved_stock()
			frappe.clear_document_cache("Bin")
		except Exception:
			frappe.log_error(
				title=f"Bin reserved-stock refresh failed for {item_code} @ {warehouse}",
				message=frappe.get_traceback(),
			)


# Per-section cap on the row-level detail. Sites with years of history produce hundreds of
# rows; the counts above stay exact and any omission is stated explicitly (never silent).
_DETAIL_CAP = 200


def _report(restored, skipped, dry_run):
	"""One consolidated Error Log entry plus stdout, so a bench run is readable."""
	mode = "DRY RUN" if dry_run else "APPLIED"
	lines = [
		f"restore_over_consumed_pc_reservations [{mode}] on site {frappe.local.site}",
		f"  restored: {len(restored)} SRE(s) across "
		f"{len({s.manufacturing_work_order for s in restored})} MWO(s)",
		f"  skipped:  {len(skipped)}",
	]

	for sre in restored[:_DETAIL_CAP]:
		lines.append(
			f"    + {sre.name} {sre.item_code} @ {sre.warehouse} "
			f"qty={flt(sre.reserved_qty, 3)} mwo={sre.manufacturing_work_order}"
		)
	if len(restored) > _DETAIL_CAP:
		lines.append(
			f"    ... {len(restored) - _DETAIL_CAP} more restored row(s) not listed"
		)

	for sre, reason in skipped[:_DETAIL_CAP]:
		lines.append(
			f"    - {sre.name} {sre.item_code} @ {sre.warehouse} "
			f"qty={flt(sre.reserved_qty, 3)} mwo={sre.manufacturing_work_order} :: {reason}"
		)
	if len(skipped) > _DETAIL_CAP:
		lines.append(
			f"    ... {len(skipped) - _DETAIL_CAP} more skipped row(s) not listed"
		)

	message = "\n".join(lines)
	print(message)  # noqa: T201 -- intentional: this is a bench-run report

	if not (restored or skipped):
		return

	try:
		frappe.log_error(
			title=f"Restore over-consumed PC reservations ({mode})", message=message
		)
	except Exception:
		# Reporting must never abort the repair (or a migrate). Observed on a dev site
		# whose schema was missing tabWorkflow, which Document.insert consults.
		frappe.logger().warning(
			"restore_over_consumed_pc_reservations: Error Log write failed; "
			"the run itself is unaffected.\n" + message
		)
