# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Operations whose ``gross_wt`` is understated by a negative MOP Log batch balance.

A negative ``(operation, item, batch)`` balance says the ledger consumed more of a
batch than it ever held. ``recalculate_manufacturing_operation_weights`` used to sum
those raw while every other consumer dropped them, so one phantom row made MOP-3DP57
report ``gross_wt`` 16.440 against a Serial Number Creator ``total_weight`` of 16.720.
The header now clamps them; the ledger rows deliberately survive, and this report is
where they surface. See ``docs/jewellery-review/24-mop-gross-wt-negative-batch-balance.md``.

Driven entirely by ``mop_lineage_audit.negative_balance_findings`` -- the detector is
never reimplemented here, because two definitions of "current balance" is how a repair
script corrupts data.

Deliberately ``add_total_row: 0``. The ``qty`` column mixes Grams (M/F/O) and Carats
(D/G), so a column total would be a unit error. ``understatement_g`` is the one
unit-safe aggregate and it is what ``report_summary`` totals.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from jewellery_erpnext.mop_lineage_audit import negative_balance_findings


def execute(filters=None):
	filters = frappe._dict(filters or {})
	_validate_filters(filters)

	payload = negative_balance_findings(
		mwos=[filters.manufacturing_work_order]
		if filters.get("manufacturing_work_order")
		else None,
		mops=[filters.manufacturing_operation]
		if filters.get("manufacturing_operation")
		else None,
	)
	data = _apply_display_filters(payload["findings"], filters)
	return (
		_get_columns(),
		data,
		_get_message(payload, filters),
		None,
		_get_report_summary(payload, data),
	)


def _validate_filters(filters):
	"""An unscoped run sweeps the whole ledger; make that a deliberate choice."""
	if not (
		filters.get("manufacturing_operation")
		or filters.get("manufacturing_work_order")
		or cint(filters.get("allow_full_scan"))
	):
		frappe.throw(
			_(
				"Select a Manufacturing Work Order or Manufacturing Operation, or tick "
				"<b>Scan whole ledger</b>. An unscoped run reads every MOP Log row."
			)
		)


def _apply_display_filters(findings, filters):
	rows = findings
	if cint(filters.get("origins_only")):
		# One defect cloned onto ten operations is ONE finding with nine clones, not
		# ten findings. This is the anti-noise default.
		rows = [r for r in rows if not r.get("inherited")]
	if filters.get("item_code"):
		rows = [r for r in rows if r.get("item_code") == filters.item_code]
	if filters.get("department"):
		rows = [r for r in rows if r.get("department") == filters.department]
	if flt(filters.get("min_understatement_g")):
		floor = flt(filters.min_understatement_g)
		rows = [r for r in rows if flt(r.get("understatement_g")) >= floor]
	return sorted(
		rows,
		key=lambda r: (bool(r.get("inherited")), -flt(r.get("understatement_g"))),
	)


def _get_message(payload, filters):
	t = payload["totals"]
	if not t["keys"]:
		return _("No negative batch balances found in this scope.")
	shown = _("origins only") if cint(filters.get("origins_only")) else _("all rows")
	return _(
		"{0} negative key(s) across {1} operation(s) and {2} work order(s); "
		"{3} are origins and {4} are inherited clones. Showing {5}. "
		"<b>gross_wt no longer counts these</b> -- the ledger rows persist so the "
		"anomaly stays visible and auditable."
	).format(
		t["keys"],
		t["operations"],
		t["mwos"],
		t["origin_keys"],
		t["inherited_keys"],
		shown,
	)


def _get_report_summary(payload, data):
	t = payload["totals"]
	return [
		{"value": t["keys"], "label": _("Negative Keys"), "datatype": "Int"},
		{"value": t["origin_keys"], "label": _("Distinct Origins"), "datatype": "Int"},
		{
			"value": t["operations"],
			"label": _("Operations Affected"),
			"datatype": "Int",
		},
		{
			# The ONLY safe aggregate: carats are converted, so this does not add
			# Grams to Carats the way a total on `qty` would.
			"value": t["understatement_g"],
			"label": _("Gross Wt Suppressed (g)"),
			"datatype": "Float",
			"indicator": "Red" if t["understatement_g"] else "Green",
		},
		{"value": len(data), "label": _("Rows Shown"), "datatype": "Int"},
	]


def _get_columns():
	return [
		{
			"label": _("Operation"),
			"fieldname": "manufacturing_operation",
			"fieldtype": "Link",
			"options": "Manufacturing Operation",
			"width": 130,
		},
		{
			"label": _("Origin Operation"),
			"fieldname": "origin_mop",
			"fieldtype": "Link",
			"options": "Manufacturing Operation",
			"width": 130,
		},
		{
			"label": _("Inherited"),
			"fieldname": "inherited",
			"fieldtype": "Check",
			"width": 80,
		},
		{
			"label": _("Clones"),
			"fieldname": "downstream_clone_count",
			"fieldtype": "Int",
			"width": 70,
		},
		{
			"label": _("Work Order"),
			"fieldname": "manufacturing_work_order",
			"fieldtype": "Link",
			"options": "Manufacturing Work Order",
			"width": 200,
		},
		{
			"label": _("Parent Order"),
			"fieldname": "parent_manufacturing_order",
			"fieldtype": "Link",
			"options": "Parent Manufacturing Order",
			"width": 180,
		},
		{
			"label": _("Department"),
			"fieldname": "department",
			"fieldtype": "Data",
			"width": 130,
		},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 90},
		{
			"label": _("Item"),
			"fieldname": "item_code",
			"fieldtype": "Link",
			"options": "Item",
			"width": 180,
		},
		{
			# Data, not Link: MOP Log.batch_no is a Data pseudo-FK (DATA-002), so a
			# dangling value would render as a broken link.
			"label": _("Batch"),
			"fieldname": "batch_no",
			"fieldtype": "Data",
			"width": 200,
		},
		{"label": _("UOM"), "fieldname": "uom", "fieldtype": "Data", "width": 70},
		{
			"label": _("Balance"),
			"fieldname": "qty",
			"fieldtype": "Float",
			"precision": 3,
			"width": 90,
		},
		{"label": _("Pcs"), "fieldname": "pcs", "fieldtype": "Int", "width": 60},
		{
			"label": _("Gross Wt Suppressed (g)"),
			"fieldname": "understatement_g",
			"fieldtype": "Float",
			"precision": 3,
			"width": 170,
		},
		{
			"label": _("Stored Gross Wt"),
			"fieldname": "stored_gross_wt",
			"fieldtype": "Float",
			"precision": 3,
			"width": 130,
		},
		{
			"label": _("Latest Voucher"),
			"fieldname": "latest_voucher",
			"fieldtype": "Data",
			"width": 220,
		},
		{
			"label": _("Latest MOP Log Row"),
			"fieldname": "latest_row",
			"fieldtype": "Link",
			"options": "MOP Log",
			"width": 150,
		},
	]
