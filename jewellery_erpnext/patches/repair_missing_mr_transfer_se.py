"""One-off repair for Material Requests whose "Material Transfer From Reserve" Stock Entry
was never created.

"Transfer Material" submits a Manufacture request and queues ``materialize_transfer_se``
(doc_events/material_request.py) to create that entry: Reserve warehouse -> the request's
warehouse. When the job failed -- mostly 1205 on the shared MAT-STE- naming row, bursts of
8 requests on 2026-09-21 and 2026-09-29 -- nothing checked the state, and operators went on
to Transfer to Department / Transfer to MOP. ``validate_transfer_se_created`` now refuses
that; this repairs what got through before it. Each request lands in one bucket, decided
live from the ledger at run time:

* ``transfer`` -- the request's own reservation is still in the Reserve warehouse, and the
  next step (if one ran) drew from set_warehouse: the department route, or a MOP transfer
  without a department. Creating the missing entry now moves the reserved stock where the
  chain expected it and gives set_warehouse back what that step borrowed. It is created by
  the same ``materialize_transfer_se`` the job runs. A request row whose alternative item
  was changed after reservation is first pointed back at the item actually reserved -- the
  only item that entry can move.
* ``relink`` -- a submitted transfer entry exists but was never stamped on the request.
  ``materialize_transfer_se`` links it; no stock moves.
* ``consistent`` -- the MOP entry took the material straight out of the Reserve warehouse
  (``make_department_mop_stock_entry`` falls back to the reserve entry's target). Nothing
  to move; reported only.
* ``short`` -- the reserved batch is no longer (fully) in the Reserve warehouse; other
  entries drew it. Cannot be completed; reported for a business decision.
* ``borrowed`` -- the reserve entry belongs to another request (copied by a split work
  order or desk Duplicate). On a non-Manufacture request the stale links are cleared. A
  Manufacture request never reserved its own share and its material left with the
  original: reported only, never auto-reserved -- that would issue the material twice.
* ``running`` / ``manual`` / ``failed`` -- the job is still queued, the ledger matches no
  case above, or creating the entry failed (the request is then marked Failed as usual).

Manual top-ups made to push a department transfer through (MAT-STE-57510 for
KGJPL-MR-MF-26-39090) are listed with their request: once the missing entry exists, each
one leaves a surplus in set_warehouse, to be reversed with a forward Material Transfer --
not a cancel, which would repost every later ledger entry of that item.

NOT registered in patches.txt; run it manually. Dry-run first:

    bench --site <site> execute \
        jewellery_erpnext.patches.repair_missing_mr_transfer_se.execute

Apply:

    bench --site <site> execute \
        jewellery_erpnext.patches.repair_missing_mr_transfer_se.execute \
        --kwargs "{'dry_run': False}"

Add ``'names': [...]`` to limit it to some requests. Run it outside the EOD sync and stock
reconciliation windows: their validators block Stock Entry submits.
"""

from collections import defaultdict
from datetime import timedelta

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe.utils import flt
from frappe.utils.background_jobs import is_job_enqueued

from jewellery_erpnext.jewellery_erpnext.doc_events import material_request as mr_events

# How far before the downstream entry to look for a manual top-up into set_warehouse.
TOPUP_LOOKBACK = timedelta(hours=12)

# Quantities are 3-dp grams; anything below this is float residue.
QTY_TOLERANCE = 0.0005

BUCKETS = (
	"transfer",
	"relink",
	"consistent",
	"short",
	"borrowed",
	"running",
	"manual",
	"failed",
)


def execute(dry_run=True, names=None):
	results = defaultdict(list)
	# A dry run posts nothing, so requests sharing a batch would each see its full balance.
	# Track what the earlier ones would have taken.
	planned = defaultdict(float)

	for mr in _candidates(names):
		outcome = _repair(mr, dry_run, planned)
		if outcome:
			bucket, detail = outcome
			results[bucket].append(detail)

	_report(results, dry_run)
	return {bucket: len(results[bucket]) for bucket in BUCKETS if results.get(bucket)}


def _candidates(names=None):
	"""Every non-cancelled request carrying a reserve entry but no transfer entry of its own."""
	condition = "AND mr.name IN %(names)s" if names else ""
	return frappe.db.sql(
		f"""
		SELECT
			mr.name, mr.docstatus, mr.workflow_state, mr.material_request_type,
			mr.set_warehouse, mr.custom_reserve_se, mr.custom_transfer_se,
			mr.custom_department_transfer_se, mr.custom_mop_se,
			EXISTS(
				SELECT 1 FROM `tabStock Entry` se
				JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
				WHERE se.name = mr.custom_reserve_se AND se.docstatus = 1
					AND sed.material_request = mr.name
			) AS reserve_owned
		FROM `tabMaterial Request` mr
		WHERE mr.docstatus < 2
			AND IFNULL(mr.custom_reserve_se, '') != ''
			AND NOT EXISTS(
				SELECT 1 FROM `tabStock Entry` se
				JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
				WHERE se.name = mr.custom_transfer_se AND se.docstatus = 1
					AND sed.material_request = mr.name
			)
			{condition}
		ORDER BY mr.creation
		""",
		{"names": tuple(names or ())},
		as_dict=True,
	)


def _repair(mr, dry_run, planned):
	"""``(bucket, detail)`` for one request, or None when it is not this repair's business."""
	label = f"{mr.name} ({mr.workflow_state})"

	if not mr.reserve_owned:
		owner = frappe.db.get_value(
			"Stock Entry Detail", {"parent": mr.custom_reserve_se}, "material_request"
		)
		if owner != mr.name:
			return _borrowed(mr, label, owner, dry_run)
		if mr.docstatus == 0:
			# A draft whose own reservation was cancelled: no transfer was ever due.
			return None
		return (
			"manual",
			f"{label}: its reserve entry {mr.custom_reserve_se} is not submitted",
		)

	if mr.docstatus == 0:
		# A draft with its own reservation is simply still in flight.
		return None

	if is_job_enqueued(mr_events._transfer_se_job_id(mr.name)):
		return "running", f"{label}: transfer job still queued or running"

	if mr.custom_transfer_se:
		# materialize_transfer_se returns early on any link, so it cannot help here.
		return "manual", (
			f"{label}: linked to {mr.custom_transfer_se}, which is not a submitted "
			"transfer of this request"
		)

	existing = _existing_transfer(mr.name)
	if existing:
		if dry_run:
			return "relink", f"{label}: would link existing {existing}"
		error = _apply(mr.name, [])
		if error:
			return "failed", f"{label}: linking {existing} failed: {error}"
		return "relink", f"{label}: linked existing {existing}"

	reserve_rows = frappe.get_all(
		"Stock Entry Detail",
		filters={"parent": mr.custom_reserve_se},
		fields=[
			"item_code",
			"batch_no",
			"qty",
			"t_warehouse",
			"material_request_item",
		],
		order_by="idx",
	)

	request_rows = {
		row.name: row
		for row in frappe.get_all(
			"Material Request Item",
			filters={"parent": mr.name},
			fields=["name", "item_code", "custom_alternative_item", "warehouse"],
		)
	}
	request_warehouses = {row.warehouse for row in request_rows.values()} | {
		mr.set_warehouse
	}
	reserve_warehouses = {row.t_warehouse for row in reserve_rows}

	downstream = _own_downstream(mr)
	if downstream:
		sources = set(
			frappe.get_all(
				"Stock Entry Detail",
				filters={"parent": downstream},
				pluck="s_warehouse",
			)
		)
		if sources <= reserve_warehouses:
			return "consistent", (
				f"{label}: {downstream} took the material straight from "
				f"{', '.join(sorted(sources))}; nothing to move"
			)
		if not sources <= request_warehouses:
			return "manual", (
				f"{label}: {downstream} drew from {', '.join(sorted(sources))}, neither "
				"the request's warehouse nor the Reserve warehouse"
			)

	realign, unmatched = _realignments(request_rows, reserve_rows)
	if unmatched:
		return "manual", (
			f"{label}: reserve rows point at request rows that no longer exist: "
			f"{', '.join(unmatched)}"
		)

	needs = defaultdict(float)
	for row in reserve_rows:
		needs[(row.item_code, row.t_warehouse, row.batch_no or None)] += flt(row.qty)

	short = []
	for key, qty in needs.items():
		available = _available(*key) - planned[key]
		if available + QTY_TOLERANCE < qty:
			item_code, warehouse, batch_no = key
			short.append(
				f"{item_code} {batch_no or ''} needs {flt(qty, 3)} in {warehouse}, "
				f"{flt(available, 3)} there"
			)
	if short:
		return "short", f"{label}: " + "; ".join(short)

	# _create_transfer_se sends each row to its request row's warehouse.
	targets = {
		row.material_request_item: request_rows[row.material_request_item].warehouse
		for row in reserve_rows
	}
	if any(
		targets[row.material_request_item] == row.t_warehouse for row in reserve_rows
	):
		return (
			"manual",
			f"{label}: the request's own warehouse is the Reserve warehouse",
		)

	moves = "; ".join(
		f"{row.item_code} {row.batch_no or ''} {flt(row.qty, 3)} {row.t_warehouse} -> "
		f"{targets[row.material_request_item]}"
		for row in reserve_rows
	)
	notes = [
		f"realign row {r} alternative {old or '-'} -> {new}" for r, old, new in realign
	]
	topups = _manual_topups(mr, reserve_rows, downstream) if downstream else []
	if topups:
		notes.append(
			f"reverse manual top-up(s) {', '.join(topups)} after this "
			f"({mr.set_warehouse} -> their source)"
		)
	suffix = f" [{' | '.join(notes)}]" if notes else ""

	if dry_run:
		for key, qty in needs.items():
			planned[key] += qty
		return "transfer", f"{label}: would create {moves}{suffix}"

	error = _apply(mr.name, realign)
	if error:
		return "failed", f"{label}: {error}{suffix}"

	created = frappe.db.get_value("Material Request", mr.name, "custom_transfer_se")
	return "transfer", f"{label}: created {created}: {moves}{suffix}"


def _borrowed(mr, label, owner, dry_run):
	where = (
		f"{label} [{mr.material_request_type}]: reserve entry {mr.custom_reserve_se} "
		f"belongs to {owner or 'no request'}"
	)
	if mr.material_request_type == "Manufacture":
		return "borrowed", (
			f"{where} -- never reserved its own share; needs a business decision"
		)

	if not dry_run:
		frappe.db.set_value(
			"Material Request",
			mr.name,
			{
				"custom_reserve_se": None,
				"custom_transfer_se": None,
				"custom_transfer_se_state": None,
				"custom_transfer_se_error": None,
			},
			update_modified=False,
		)
		frappe.db.commit()
	return (
		"borrowed",
		f"{where} -- {'would clear' if dry_run else 'cleared'} the copied links",
	)


def _existing_transfer(mr_name):
	found = frappe.db.sql(
		"""
		SELECT se.name FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.stock_entry_type = 'Material Transfer From Reserve'
			AND se.docstatus = 1 AND sed.material_request = %s
		LIMIT 1
		""",
		(mr_name,),
	)
	return found[0][0] if found else None


def _own_downstream(mr):
	"""The request's own next-step entry; the department leg first, as it ran first."""
	for link in (mr.custom_department_transfer_se, mr.custom_mop_se):
		if mr_events._entry_belongs_to(link, mr.name):
			return link
	return None


def _realignments(request_rows, reserve_rows):
	"""Request rows whose item no longer matches the item their reservation moved.

	``_create_transfer_se`` swaps in the row's current alternative item, so a row edited
	after reservation would ask for an item the Reserve warehouse never received.
	"""
	realign, unmatched = [], []
	for reserved in reserve_rows:
		row = request_rows.get(reserved.material_request_item)
		if not row:
			unmatched.append(reserved.material_request_item or "(blank)")
			continue
		if (row.custom_alternative_item or row.item_code) != reserved.item_code:
			realign.append((row.name, row.custom_alternative_item, reserved.item_code))
	return realign, unmatched


def _available(item_code, warehouse, batch_no):
	if batch_no:
		return flt(
			get_batch_qty(batch_no=batch_no, warehouse=warehouse, item_code=item_code)
		)
	return flt(
		frappe.db.get_value(
			"Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"
		)
	)


def _manual_topups(mr, reserve_rows, downstream):
	"""Hand-made entries that put a reserved row's exact batch and qty into set_warehouse
	shortly before the downstream entry drew it -- the workaround for the negative stock."""
	posted = frappe.db.sql(
		"""
		SELECT MIN(posting_datetime) FROM `tabStock Ledger Entry`
		WHERE voucher_type = 'Stock Entry' AND voucher_no = %s AND is_cancelled = 0
		""",
		(downstream,),
	)[0][0]
	if not posted:
		return []

	found = set()
	for row in reserve_rows:
		if not row.batch_no:
			continue
		for (voucher,) in frappe.db.sql(
			"""
			SELECT DISTINCT sle.voucher_no
			FROM `tabStock Ledger Entry` sle
			JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sle.serial_and_batch_bundle
			JOIN `tabStock Entry` se ON se.name = sle.voucher_no
			WHERE sle.voucher_type = 'Stock Entry' AND sle.is_cancelled = 0
				AND sle.item_code = %s AND sle.warehouse = %s AND sbe.batch_no = %s
				AND sle.actual_qty > 0 AND ABS(sbe.qty - %s) < %s
				AND IFNULL(se.auto_created, 0) = 0
				AND sle.posting_datetime BETWEEN %s AND %s
			""",
			(
				row.item_code,
				mr.set_warehouse,
				row.batch_no,
				flt(row.qty),
				QTY_TOLERANCE,
				posted - TOPUP_LOOKBACK,
				posted,
			),
		):
			found.add(voucher)
	return sorted(found)


def _apply(mr_name, realign):
	try:
		for row_name, _old, item_code in realign:
			frappe.db.set_value(
				"Material Request Item",
				row_name,
				"custom_alternative_item",
				item_code,
				update_modified=False,
			)
		# Committed on its own: run_with_retry rolls back between attempts, which would
		# otherwise undo the realignment the new entry depends on.
		frappe.db.commit()
		# Marks the request Failed and re-raises on any error, exactly as the job does.
		mr_events.materialize_transfer_se(mr_name)
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		return str(e)
	return None


def _report(results, dry_run):
	print(
		("DRY RUN -- nothing posted. " if dry_run else "")
		+ "Material Requests without their own Material Transfer From Reserve:"
	)
	for bucket in BUCKETS:
		rows = results.get(bucket)
		if not rows:
			continue
		print(f"\n[{bucket}] {len(rows)}")
		for line in rows:
			print(f"  {line}")
