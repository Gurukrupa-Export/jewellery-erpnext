"""Create the Finding Manufacturing Work Orders that KGGK PMOs missed on submit.

On submit, a Parent Manufacturing Order gets a Finding MWO for every Tracking BOM Chains
finding whose variant is ticked Is Manufacturing Item. Until this fix an unticked chain was
skipped silently -- the PMO submitted without its Finding MWO -- and ticking the item later
never raised the missing order. ``on_submit`` now refuses an unticked chain and new chain
variants are ticked on insert, so no new PMO can fall into this; this patch repairs the ones
that already did.

It touches only the PMOs listed below, checked against the 2026-10-22 production copy: the 8
KGGK reported (EA41604-002, EA15346-002, EA41608-001/003, all on Box Chain variants that were
never ticked) and EA13136-002-0001/0002 (submitted before BC-0.75 INCH was ticked on 10-07),
all still at the first department. Of the 46 PMOs missing a Finding MWO on that copy, these
are deliberately left out:

* 20 August NE PMOs completed at Tagging -- their chain was issued straight from stock.
* 16 August NE PMOs idle at Waxing since August -- only once KGGK confirms they are live.

The names exist only on KGGK, so on every other site this is a no-op. For each PMO it:

1. skips it if it already has a Finding MWO, if its work orders are all Completed/Closed, or
   if a chain was already issued from stock against them -- the piece has its chain then;
2. ticks the chain variant(s) the PMO needs, as ``before_insert`` now does for new ones;
3. creates the draft Finding MWO through ``create_finding_mwo()``, exactly as submit does.

Each PMO is committed on its own; a failure is logged and never stops ``bench migrate``.

Dry run (creates, reports, rolls back)::

    bench --site <site> execute jewellery_erpnext.patches.backfill_chain_finding_mwo.execute \
        --kwargs "{'dry_run': True}"
"""

import frappe

from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.finding_mwo import (
	create_finding_mwo,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	_get_chain_finding_rows,
)

PMOS = (
	"PMO-KGJPL-EA41604-002-0001",
	"PMO-KGJPL-EA41604-002-0002",
	"PMO-KGJPL-EA15346-002-0001",
	"PMO-KGJPL-EA15346-002-0002",
	"PMO-KGJPL-EA41608-003-0001",
	"PMO-KGJPL-EA41608-003-0002",
	"PMO-KGJPL-EA41608-001-0001",
	"PMO-KGJPL-EA41608-001-0002",
	"PMO-KGJPL-EA13136-002-0001",
	"PMO-KGJPL-EA13136-002-0002",
)

# A work order in either status will take no new material: the piece is done or stopped.
DONE_STATUSES = ("Completed", "Closed")


def execute(dry_run=False):
	created, skipped, failed = [], [], []

	for name in PMOS:
		if not frappe.db.exists("Parent Manufacturing Order", name):
			continue  # not a KGGK site

		try:
			outcome = _backfill(name)
		except Exception:  # noqa: BLE001 -- one PMO must never fail `bench migrate`
			# A PMO whose work orders have since moved into a state create_finding_mwo()
			# cannot handle needs a human, not a failed `bench migrate` for every other site.
			frappe.db.rollback()
			failed.append(name)
			frappe.log_error(
				frappe.get_traceback(), f"Chain Finding MWO back-fill failed: {name}"
			)
			continue

		if dry_run:
			frappe.db.rollback()
		else:
			frappe.db.commit()  # nosemgrep - each PMO is repaired independently
		(created if outcome.startswith("created") else skipped).append((name, outcome))

	if created or skipped or failed:
		_report(created, skipped, failed, dry_run)
	return {"created": created, "skipped": skipped, "failed": failed}


def _backfill(name):
	if frappe.db.exists(
		"Manufacturing Work Order",
		{"manufacturing_order": name, "is_finding_mwo": 1, "docstatus": ["!=", 2]},
	):
		return "skipped: already has a Finding MWO"

	pmo = frappe.get_doc("Parent Manufacturing Order", name)
	if pmo.docstatus != 1 or not pmo.custom_tracking_bom:
		return "skipped: not submitted or no Tracking BOM"

	main_mwos = frappe.get_all(
		"Manufacturing Work Order",
		{
			"manufacturing_order": name,
			"is_finding_mwo": 0,
			"for_fg": 0,
			"docstatus": ["<", 2],
		},
		["name", "status"],
	)
	if not main_mwos or all(mwo.status in DONE_STATUSES for mwo in main_mwos):
		return "skipped: work orders finished or closed"
	if _chain_issued_from_stock([mwo.name for mwo in main_mwos]):
		return "skipped: chain already issued from stock"

	rows = _get_chain_finding_rows(pmo)
	if not rows:
		return "skipped: no chain needs a Finding MWO"

	for variant in sorted({row.item_variant for row in rows}):
		if not frappe.db.get_value("Item", variant, "custom_is_manufacturing_item"):
			frappe.db.set_value("Item", variant, "custom_is_manufacturing_item", 1)

	create_finding_mwo(pmo, rows)
	finding_mwos = frappe.get_all(
		"Manufacturing Work Order",
		{"manufacturing_order": name, "is_finding_mwo": 1, "docstatus": 0},
		pluck="name",
	)
	return "created " + ", ".join(finding_mwos)


def _chain_issued_from_stock(mwos):
	"""True when a submitted Stock Entry already moved a Chains finding against these MWOs.

	Any chain counts, not only the BOM's: the August pieces were issued 6SC-1.00 INCH
	against a BOM asking for 6SC-8.25 INCH.
	"""
	return bool(
		frappe.db.sql(
			"""
			select sed.name
			from `tabStock Entry Detail` sed
			join `tabStock Entry` se on se.name = sed.parent and se.docstatus = 1
			join `tabItem Variant Attribute` attr on attr.parent = sed.item_code
				and attr.attribute = 'Finding Category' and attr.attribute_value = 'Chains'
			where sed.custom_manufacturing_work_order in %(mwos)s
			limit 1
			""",
			{"mwos": mwos},
		)
	)


def _report(created, skipped, failed, dry_run):
	lines = [
		f"backfill_chain_finding_mwo ({'dry run' if dry_run else 'applied'})",
		f"created: {len(created)}",
	]
	lines += [f"  {name}: {outcome}" for name, outcome in created]
	lines.append(f"skipped: {len(skipped)}")
	lines += [f"  {name}: {outcome}" for name, outcome in skipped]
	lines.append(f"failed: {len(failed)}")
	lines += [f"  {name}: see its own Error Log" for name in failed]

	message = "\n".join(lines)
	print(message)  # surfaces in the bench migrate / execute output
	frappe.log_error(message, "Chain Finding MWO Back-fill")
