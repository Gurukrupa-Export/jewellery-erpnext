"""Restore WIP reservations that EOD relocation cancelled and never rebuilt.

``_snapshot_mwo_sres_for_relocation`` used to pick the reservations to relocate by
(MWO, item_code, source warehouse) — batch-blind. When an EOD run moved ONE batch out of a
warehouse, a reservation for a DIFFERENT batch of the same item parked there was cancelled
with it, ``_reserve_sres_from_eod_se_rows`` had no Stock Entry row to rebuild it from, and
``_hard_delete_cancelled_snapshots`` then made the loss permanent. The MOP Log balance kept
counting the metal, every later loss rebase carried the difference forward, and the gap
finally surfaced at Serial Number Creator submit as::

    NegativeStockError: 0.01 units of Item M-G-22KT-91.75-Y needed in Warehouse
    Model Filling WIP WH 1 - KGJPL ... you are allowed to consume only 3.176 units.

Live example: ``MAT-STE-03585`` issued 0.01 g of batch KG2F081-MGL229175Y0-P29A8 into
Model Making WO on 2026-08-20; the EOD run that evening moved only the OTHER batch out of
that warehouse but cancelled and deleted this reservation too, leaving SNC ``aomrem1779``
unsubmittable with 3.186 required against 3.176 reserved.

The relocation defect itself is fixed in ``mop_eod_sync`` (batch-aware snapshot + a
post-condition guard that proves reserved qty did not drop). This patch repairs the
reservations already lost, by reserving ONLY the missing difference at a warehouse that
physically holds the batch with enough free qty — never the whole balance, which would
double-reserve the part that survived.

Safe to re-run: a job whose reservations already cover its balance reports no shortfall and
is skipped. Nothing is reserved where the stock is not free, and no stock is moved.
"""

import frappe
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
	heal_reservation_shortfall,
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
	disabled = _disable_unrunnable_sre_server_scripts()
	for name in disabled:
		print(
			f"heal_lost_wip_reservations: disabled Server Script {name!r} — it raises on "
			"every Stock Reservation Entry insert, blocking ALL reservation creation"
		)

	# One read-only pass to find the work, so the healing loop can savepoint per job.
	try:
		scan = heal_reservation_shortfall(dry_run=True) or []
	except Exception:
		frappe.log_error(
			title="heal_lost_wip_reservations: scan failed",
			message=frappe.get_traceback(),
		)
		return

	if ONLY_MWOS:
		scan = [row for row in scan if row["mwo"] in ONLY_MWOS]
	targets = sorted({row["mwo"] for row in scan if not row.get("skipped")})
	unreachable = [row for row in scan if row.get("skipped")]

	healed = 0
	for mwo in targets:
		savepoint = "heal_lost_wip_reservations"
		try:
			frappe.db.savepoint(savepoint)
			results = heal_reservation_shortfall(
				mwo=mwo, dry_run=False, warehouse=PIN_WAREHOUSE
			)
			for row in results or []:
				if row.get("error"):
					print(
						f"heal_lost_wip_reservations: {row['mwo']} {row['item_code']} "
						f"batch {row['batch_no']} — {row['error']}"
					)
				if not row.get("created"):
					continue
				healed += 1
				print(
					"heal_lost_wip_reservations: {mwo} {item} batch {batch} — reserved "
					"{qty} at {wh} ({sre})".format(
						mwo=row["mwo"],
						item=row["item_code"],
						batch=row["batch_no"],
						qty=flt(row["shortfall"], 3),
						wh=row["warehouse"],
						sre=row["created"],
					)
				)
			frappe.db.release_savepoint(savepoint)
		except Exception:
			# One unhealable job must not abort the migration or roll back the jobs
			# already repaired in this run.
			frappe.db.rollback(save_point=savepoint)
			frappe.log_error(
				title=f"heal_lost_wip_reservations: {mwo}",
				message=frappe.get_traceback(),
			)

	print(f"heal_lost_wip_reservations: repaired {healed} reservation(s)")
	for row in unreachable:
		# Reported, never guessed at: no warehouse physically holds this batch with free
		# qty, so reserving anywhere would only relocate the failure. These need a stock
		# investigation, not a reservation.
		print(
			"heal_lost_wip_reservations: NOT repaired — {mwo} {item} batch {batch} "
			"short {qty} ({why})".format(
				mwo=row["mwo"],
				item=row["item_code"],
				batch=row["batch_no"],
				qty=flt(row.get("shortfall"), 3),
				why=row["skipped"],
			)
		)


def _disable_unrunnable_sre_server_scripts():
	"""Disable Stock Reservation Entry server scripts that can never execute.

	A DocType Event script whose body uses ``frappe.whitelist`` was written as an API
	helper: that attribute does not exist inside Frappe's ``safe_exec`` sandbox, so the
	script raises ``AttributeError`` on EVERY run and takes the document event down with
	it. On the live site one such script ("Stock reservation submit", Before Insert) stopped
	*all* Stock Reservation Entry creation the moment it was saved — reservations went from
	~1000/day to zero — which blocks this patch's repair as well as Material Transfer
	(WORK ORDER) submits, EOD re-reservation and Employee IR loss rebases.

	Only scripts that provably cannot run are touched, and only on this DocType. Disabled
	rather than deleted, so the author can recover the code and move it to a whitelisted
	app method where the decorator is meaningful.
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
		frappe.db.set_value("Server Script", script.name, "disabled", 1)
		disabled.append(script.name)

	if disabled:
		# server_script_utils caches the doctype -> event -> scripts map, and db.set_value
		# does not run the doc's on_update that would clear it. Without this the running
		# workers keep executing the disabled script until the next restart.
		frappe.client_cache.delete_value("server_script_map")
	return disabled
