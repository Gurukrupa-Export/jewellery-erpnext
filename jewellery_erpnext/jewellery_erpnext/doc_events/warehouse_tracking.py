# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Employee MSL warehouse tracking (req #7 / #8).

Single source of truth for the per (employee warehouse x item) Issue / Receive /
Loss / Pending figures, where Pending = Issue - Receive - Loss. Everything is
derived on demand from Stock Ledger Entry (item-level qty, so the v16
NULL-batch_no-in-SLE caveat does not apply), so the maintained copy on the
Warehouse form can never drift from the ledger.

Consumers:
  * ``report/employee_warehouse_tracking`` — cross-warehouse / monthly reporting.
  * ``recalculate_msl_tracking`` — materializes the rows into the
    ``Warehouse.custom_msl_tracking`` child table (button + auto after an
    Employee Loss Entry).
"""

import frappe
from frappe.utils import flt

# Stock Entry types that represent a booked loss (not a return).
LOSS_STOCK_ENTRY_TYPES = ("Process Loss",)


def get_warehouse_item_tracking(filters=None):
	"""Return per (employee warehouse x item) Issue/Receive/Loss/Pending rows.

	``filters`` (all optional): company, employee, warehouse, department,
	item_code, from_date, to_date. Only warehouses with an ``employee`` set are
	considered. Loss = outward movements whose voucher is a Process Loss Stock
	Entry; every other outward movement is a Receive; every inward is an Issue.
	"""
	filters = frappe._dict(filters or {})

	conditions = ["sle.is_cancelled = 0", "wh.employee IS NOT NULL"]
	if filters.get("company"):
		conditions.append("sle.company = %(company)s")
	if filters.get("employee"):
		conditions.append("wh.employee = %(employee)s")
	if filters.get("warehouse"):
		conditions.append("sle.warehouse = %(warehouse)s")
	if filters.get("department"):
		conditions.append("emp.department = %(department)s")
	if filters.get("item_code"):
		conditions.append("sle.item_code = %(item_code)s")
	if filters.get("from_date"):
		conditions.append("sle.posting_date >= %(from_date)s")
	if filters.get("to_date"):
		conditions.append("sle.posting_date <= %(to_date)s")

	where = " AND ".join(conditions)
	loss_types = ", ".join(frappe.db.escape(t) for t in LOSS_STOCK_ENTRY_TYPES)

	rows = frappe.db.sql(
		f"""
		SELECT
			sle.warehouse AS warehouse,
			wh.employee AS employee,
			emp.employee_name AS employee_name,
			emp.department AS department,
			sle.item_code AS item_code,
			SUM(CASE WHEN sle.actual_qty > 0 THEN sle.actual_qty ELSE 0 END) AS issue_qty,
			SUM(CASE WHEN sle.actual_qty < 0
					 AND se.stock_entry_type IN ({loss_types})
					 THEN -sle.actual_qty ELSE 0 END) AS loss_qty,
			SUM(CASE WHEN sle.actual_qty < 0
					 AND (se.stock_entry_type IS NULL OR se.stock_entry_type NOT IN ({loss_types}))
					 THEN -sle.actual_qty ELSE 0 END) AS receive_qty
		FROM `tabStock Ledger Entry` sle
		INNER JOIN `tabWarehouse` wh ON wh.name = sle.warehouse
		LEFT JOIN `tabEmployee` emp ON emp.name = wh.employee
		LEFT JOIN `tabStock Entry` se
			ON sle.voucher_type = 'Stock Entry' AND se.name = sle.voucher_no
		WHERE {where}
		GROUP BY sle.warehouse, sle.item_code
		HAVING issue_qty != 0 OR receive_qty != 0 OR loss_qty != 0
		ORDER BY wh.employee, sle.warehouse, sle.item_code
		""",
		filters,
		as_dict=True,
	)

	for r in rows:
		r["issue_qty"] = flt(r.get("issue_qty"), 3)
		r["receive_qty"] = flt(r.get("receive_qty"), 3)
		r["loss_qty"] = flt(r.get("loss_qty"), 3)
		r["pending_qty"] = flt(r["issue_qty"] - r["receive_qty"] - r["loss_qty"], 3)

	return rows


@frappe.whitelist()
def recalculate_msl_tracking(warehouse):
	"""Recompute and materialize the ``custom_msl_tracking`` child table on an
	employee (MSL) warehouse from the ledger. Returns the row count.

	Always a full recompute from Stock Ledger Entry — never an incremental
	mutation — so the maintained copy cannot drift. Bypasses the MSL guards
	(``ignore_msl_guards``) since this is a system-driven derived-cache write.
	"""
	if not warehouse:
		return 0
	wh_meta = frappe.db.get_value(
		"Warehouse", warehouse, ["employee", "warehouse_type"], as_dict=True
	)
	# The maintained on-form table is scoped to the employee's Raw Material (MSL)
	# warehouse — its metal pool (a handful of items). Employee Manufacturing/WIP
	# warehouses can hold hundreds of items and are covered by the report instead.
	if not wh_meta or not wh_meta.employee or wh_meta.warehouse_type != "Raw Material":
		return 0

	rows = get_warehouse_item_tracking({"warehouse": warehouse})

	doc = frappe.get_doc("Warehouse", warehouse)
	doc.set("custom_msl_tracking", [])
	for r in rows:
		doc.append(
			"custom_msl_tracking",
			{
				"item_code": r["item_code"],
				"issue_qty": r["issue_qty"],
				"receive_qty": r["receive_qty"],
				"loss_qty": r["loss_qty"],
				"pending_qty": r["pending_qty"],
			},
		)
	doc.flags.ignore_msl_guards = True
	doc.flags.ignore_version = True
	doc.save(ignore_permissions=True)
	return len(rows)
