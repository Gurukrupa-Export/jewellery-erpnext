"""Restore WIP reservations that EOD relocation cancelled and never rebuilt.

``_snapshot_mwo_sres_for_relocation`` picks the reservations to relocate by
(MWO, item_code, source warehouse) — batch-blind. When an EOD run moves ONE batch out of a
warehouse, a reservation for a DIFFERENT batch of the same item parked there is cancelled
with it, ``_reserve_sres_from_eod_se_rows`` has no Stock Entry row to rebuild it from, and
``_hard_delete_cancelled_snapshots`` then makes the loss permanent. The MOP Log balance
keeps counting the metal, every later loss rebase carries the difference forward, and the
gap finally surfaces at Serial Number Creator submit as::

    NegativeStockError: 0.01 units of Item M-G-22KT-91.75-Y needed in Warehouse
    Model Filling WIP WH 1 - KGJPL ... you are allowed to consume only 3.176 units.

Live example: ``MAT-STE-03585`` issued 0.01 g of batch KG2F081-MGL229175Y0-P29A8 into
Model Making WO on 2026-08-20; the EOD run that evening moved only the OTHER batch out of
that warehouse but cancelled and deleted this reservation too, leaving SNC ``aomrem1779``
unsubmittable with 3.186 required against 3.176 reserved.

This patch repairs the reservations already lost, by reserving ONLY the missing difference
at a warehouse that physically holds the batch with enough free qty — never the whole
balance, which would double-reserve the part that survived. **No stock is moved, no qty is
revised**: it writes Stock Reservation Entries against metal that already exists, so MOP Log
weights and every Bin's actual qty are untouched.

SELF-CONTAINED ON PURPOSE. It imports only helpers that already exist in the deployed app,
so it can be shipped on its own, without the ``mop_eod_sync`` / ``serial_number_creator``
changes that fix the underlying relocation defect and the confusing error message. Those are
still worth deploying — this patch repairs history, they stop it recurring — and once they
are in, the same logic is available for ongoing use as
``mop_eod_sync.heal_reservation_shortfall``.

Safe to re-run: a job whose reservations already cover its balance reports no shortfall and
is skipped.
"""

import frappe
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	get_current_mop_balance_rows,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
	_build_and_submit_mwo_sre,
	_cancelled_and_sibling_sre_warehouses,
	_free_batch_qty_to_reserve,
	_heal_ownership_allowed,
	_mop_log_to_warehouses,
	_physical_batch_warehouses,
	_resolve_department_warehouse,
	_resolve_mwo_so_anchor,
	_warehouses_of_company,
)
from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator import (
	_active_sres_for,
	_pmo_mwo_names,
)

# Restrict the repair to specific Manufacturing Work Orders, e.g.
# ONLY_MWOS = ["MWO-KGJPL-RI00265-007-1-91.75-Y-01"]. Empty means every job carrying a
# shortfall — they are all the same defect, and each is blocked at Serial Number Creator
# submit until its reservation is restored.
ONLY_MWOS = []

# Force the top-up to be taken from one warehouse, e.g. PIN_WAREHOUSE = "Waxing WO - KGJPL".
# Left None, each shortfall is reserved where the job's own Stock Entries last moved that
# batch, falling back to whichever warehouse holds it with the most free qty. The choice
# depends on live free stock at run time, so pin it if a specific warehouse must be used.
PIN_WAREHOUSE = None


def execute():
	for name in _disable_unrunnable_sre_server_scripts():
		print(
			f"heal_lost_wip_reservations: disabled Server Script {name!r} — it raises on "
			"every Stock Reservation Entry insert, blocking ALL reservation creation"
		)

	healed = skipped = failed = 0
	seen = set()
	for mwo in ONLY_MWOS or _live_mwos():
		pmo, op, company = frappe.db.get_value(
			"Manufacturing Work Order",
			mwo,
			["manufacturing_order", "manufacturing_operation", "company"],
		) or (None, None, None)
		if not op:
			continue

		for row in get_current_mop_balance_rows(
			op,
			include_fields=[
				"item_code",
				"batch_no",
				"qty_after_transaction_batch_based as qty",
			],
		):
			item_code = row.get("item_code")
			batch_no = row.get("batch_no")
			qty = flt(row.get("qty"), 3)
			# A non-batch balance row has no lot to reserve against.
			if not (item_code and batch_no) or qty <= 0:
				continue

			# One shortfall per (job, item, batch): a PMO's main MWO and its metal MWO see
			# the SAME balance row and the SAME PMO-wide reservations, so without this the
			# same gap would be healed once per sibling — double-reserving it.
			key = (pmo or mwo, item_code, batch_no)
			if key in seen:
				continue
			seen.add(key)

			shortfall = flt(qty - _sre_remaining_for_batch(mwo, item_code, batch_no), 3)
			if shortfall <= 0:
				continue

			savepoint = "heal_lost_wip_reservations"
			try:
				frappe.db.savepoint(savepoint)
				sre, warehouse = _reserve_batch_shortfall(
					mwo, item_code, batch_no, shortfall, op, company
				)
				frappe.db.release_savepoint(savepoint)
			except Exception as exc:
				# One unrepairable row must not abort the migration or roll back the rows
				# already repaired in this run.
				frappe.db.rollback(save_point=savepoint)
				frappe.log_error(
					title=f"heal_lost_wip_reservations: {mwo} / {batch_no}",
					message=frappe.get_traceback(),
				)
				failed += 1
				print(
					f"heal_lost_wip_reservations: FAILED {mwo} {item_code} batch "
					f"{batch_no} short {shortfall} — {type(exc).__name__}: {exc}"
				)
				continue

			if sre:
				healed += 1
				print(
					f"heal_lost_wip_reservations: {mwo} {item_code} batch {batch_no} — "
					f"reserved {shortfall} at {warehouse} ({sre})"
				)
			else:
				# Reported, never guessed at: nothing holds this batch with enough free qty,
				# so reserving anywhere would only relocate the failure. Needs a stock
				# investigation, not a reservation.
				skipped += 1
				print(
					f"heal_lost_wip_reservations: NOT repaired — {mwo} {item_code} batch "
					f"{batch_no} short {shortfall} ({warehouse})"
				)

	print(
		f"heal_lost_wip_reservations: repaired {healed}, "
		f"not repairable {skipped}, failed {failed}"
	)


def _live_mwos():
	"""Submitted work orders that are not finished — the only ones a shortfall can block."""
	return [
		r[0]
		for r in frappe.db.sql(
			"""
			SELECT name
			FROM `tabManufacturing Work Order`
			WHERE docstatus = 1
			  AND IFNULL(status, '') != 'Completed'
			ORDER BY modified DESC
			"""
		)
	]


def _pmo_mwo_scope(mwo):
	"""Every submitted MWO of this MWO's Parent Manufacturing Order.

	A job's reservations do NOT all hang off the MWO whose MOP Log carries the balance: a
	PMO splits into a main MWO and per-metal MWOs, the reservations are created against
	whichever one issued the stock, and the same (item, batch) balance is visible from both.
	Reading reservations MWO-by-MWO therefore reports zero for the sibling that holds the
	balance, and healing on that would mint a SECOND reservation for metal the job already
	has. Serial Number Creator reads reservations at exactly this scope.
	"""
	_pmo, names = _pmo_mwo_names(frappe._dict({"manufacturing_work_order": mwo}))
	return names or [mwo]


def _sre_remaining_for_batch(mwo, item_code, batch_no):
	"""Undelivered qty this job still has reserved for (item, batch), across all warehouses.

	``_active_sre_exists`` answers "is there a reservation at all?"; this answers "how much
	is still reserved?" — the question a partially-lost reservation needs. Batch scoping is
	delegated to ``_active_sres_for`` so this and Serial Number Creator cannot disagree.
	"""
	if not (mwo and item_code):
		return 0.0
	return flt(
		sum(
			rem
			for _sre, rem in _active_sres_for(item_code, batch_no, _pmo_mwo_scope(mwo))
		),
		3,
	)


def _mwo_inbound_batch_warehouses(mwo, item_code, batch_no):
	"""Warehouses this JOB's own submitted Stock Entries last moved (item, batch) INTO.

	Most recent first, across the PMO's MWOs. This is the only signal that distinguishes
	"the job's metal is parked here" from "this warehouse happens to hold a lot of the
	shared casting batch".
	"""
	if not (mwo and item_code and batch_no):
		return []
	return [
		r[0]
		for r in frappe.db.sql(
			"""
			SELECT sed.t_warehouse
			FROM `tabStock Entry Detail` sed
			INNER JOIN `tabStock Entry` se ON se.name = sed.parent
			WHERE se.docstatus = 1
			  AND sed.custom_manufacturing_work_order IN %(mwos)s
			  AND sed.item_code = %(item_code)s
			  AND sed.batch_no = %(batch_no)s
			  AND sed.t_warehouse IS NOT NULL AND sed.t_warehouse != ''
			ORDER BY se.creation DESC
			""",
			{
				"mwos": tuple(_pmo_mwo_scope(mwo)),
				"item_code": item_code,
				"batch_no": batch_no,
			},
		)
		if r[0]
	]


def _pick_shortfall_warehouse(mwo, item_code, batch_no, shortfall, operation, company):
	"""``(warehouse, free_qty)`` that can absorb ``shortfall`` — ``(None, 0.0)`` if none can.

	``free`` is ``min(free batch qty, free warehouse qty)``. The batch figure alone is not
	enough: ERPNext's negative-stock check runs at the item+warehouse Bin level, so a batch
	with plenty of free lot qty in a Bin that is fully reserved would mint a reservation
	that still fails at consumption — the exact failure this repair exists to prevent.

	Ranking is deliberately NOT "most free wins", which would hand the job whichever
	warehouse holds the biggest unreserved pool of a shared casting batch:

	  1. a warehouse where this job ALREADY reserves this batch (keeps consumption together),
	  2. the warehouse its own Stock Entries last moved the batch into (most recent first),
	  3. anything else holding the batch, most free first.
	"""
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_available_qty_to_reserve,
	)

	if PIN_WAREHOUSE:
		candidates = {PIN_WAREHOUSE}
	else:
		candidates = set(_physical_batch_warehouses(item_code, batch_no))
		candidates.update(
			_cancelled_and_sibling_sre_warehouses(mwo, item_code, batch_no)
		)
		if operation:
			# Only a hint for widening the candidate set, and every candidate is
			# re-validated against free qty below — a deleted or stale operation must not
			# take the whole repair down with a DoesNotExistError.
			try:
				dept = _resolve_department_warehouse(
					frappe.get_cached_doc("Manufacturing Operation", operation)
				)
			except Exception:
				dept = None
			if dept:
				candidates.add(dept)
		candidates.update(_mop_log_to_warehouses(mwo, item_code, batch_no))
		candidates = _warehouses_of_company(candidates, company)
	if not candidates:
		return None, 0.0

	own_whs = {
		r[0]
		for r in frappe.db.sql(
			"""
			SELECT DISTINCT sre.warehouse
			FROM `tabStock Reservation Entry` sre
			INNER JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sre.name
			WHERE sre.docstatus = 1
			  AND sre.manufacturing_work_order IN %(mwos)s
			  AND sre.item_code = %(item_code)s
			  AND sbe.batch_no = %(batch_no)s
			""",
			{
				"mwos": tuple(_pmo_mwo_scope(mwo)),
				"item_code": item_code,
				"batch_no": batch_no,
			},
		)
		if r[0]
	}
	inbound_rank = {}
	for idx, wh in enumerate(_mwo_inbound_batch_warehouses(mwo, item_code, batch_no)):
		inbound_rank.setdefault(wh, idx)

	scored = []
	for wh in candidates:
		free = min(
			flt(_free_batch_qty_to_reserve(item_code, wh, batch_no)),
			max(flt(get_available_qty_to_reserve(item_code, wh)), 0.0),
		)
		if free + 1e-6 < shortfall:
			continue
		if wh in own_whs:
			tier, within = 0, 0
		elif wh in inbound_rank:
			tier, within = 1, inbound_rank[wh]
		else:
			tier, within = 2, 0
		scored.append((tier, within, -free, wh))
	if not scored:
		return None, 0.0

	scored.sort()
	_tier, _within, neg_free, wh = scored[0]
	return wh, -neg_free


def _reserve_batch_shortfall(mwo, item_code, batch_no, shortfall, operation, company):
	"""Reserve exactly ``shortfall`` of (item, batch). ``(sre_name, warehouse)`` on success.

	On failure returns ``(None, reason)`` — never a partial or oversized reservation. Only
	the missing difference is reserved; the whole balance would double-reserve the part that
	survived.
	"""
	resolved = _resolve_mwo_so_anchor(mwo)
	if not resolved:
		return None, "no Sales Order anchor"
	if not _heal_ownership_allowed(item_code, batch_no, resolved):
		return None, "Customer Goods batch owned by another customer"

	warehouse, free = _pick_shortfall_warehouse(
		mwo, item_code, batch_no, shortfall, operation, company
	)
	if not warehouse:
		return None, "no warehouse holds enough free batch qty"

	has_batch_no, has_serial_no, stock_uom = frappe.get_cached_value(
		"Item", item_code, ["has_batch_no", "has_serial_no", "stock_uom"]
	)
	return (
		_build_and_submit_mwo_sre(
			company,
			mwo,
			item_code,
			warehouse,
			batch_no,
			shortfall,
			free,
			operation,
			resolved,
			has_batch_no,
			has_serial_no,
			stock_uom,
		),
		warehouse,
	)


def _disable_unrunnable_sre_server_scripts():
	"""Disable Stock Reservation Entry server scripts that can never execute.

	A DocType Event script whose body uses ``frappe.whitelist`` was written as an API
	helper: that attribute does not exist inside Frappe's ``safe_exec`` sandbox, so the
	script raises ``AttributeError`` on EVERY run and takes the document event down with it.
	On the live site one such script ("Stock reservation submit", Before Insert) stopped
	*all* Stock Reservation Entry creation the moment it was saved — reservations went from
	~1000/day to zero — which blocks Material Transfer (WORK ORDER) submits, EOD
	re-reservation and Employee IR loss rebases, and would block this repair too outside of
	migrate (server scripts are skipped while ``frappe.flags.in_migrate`` is set).

	Only scripts that provably cannot run are touched, and only on this DocType. Disabled
	rather than deleted, so the author can recover the code and move it to a whitelisted app
	method where the decorator is meaningful.
	"""
	disabled = []
	for script in frappe.get_all(
		"Server Script",
		filters={
			"reference_doctype": "Stock Reservation Entry",
			"script_type": "DocType Event",
			"disabled": 0,
		},
		fields=["name", "script"],
	):
		if "frappe.whitelist" not in (script.script or ""):
			continue
		frappe.db.set_value("Server Script", script.name, "disabled", cint(1))
		disabled.append(script.name)

	if disabled:
		# server_script_utils caches the doctype -> event -> scripts map, and db.set_value
		# does not run the doc's on_update that would clear it. Without this the running
		# workers keep executing the disabled script until the next restart.
		frappe.client_cache.delete_value("server_script_map")
	return disabled
