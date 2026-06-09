import frappe

from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
	_get_t_warehouse_from_logs,
	_resolve_department_warehouse,
)

# Flush accumulated updates to the DB (and commit) every this many rows, so a
# large backlog never trips Frappe's MAX_WRITES_PER_TRANSACTION limit and the
# transaction / memory footprint stay bounded.
FLUSH_EVERY = 5000


def _flush(doc_updates):
	"""bulk_update + commit the pending rows, then clear the buffer.

	Returns the number of rows written. ``bulk_update`` collapses each chunk
	into a single CASE-based UPDATE and preserves existing values for any field
	a given row does not supply, so rows that only set ``to_warehouse`` keep
	their ``from_warehouse`` untouched (and vice-versa).
	"""
	if not doc_updates:
		return 0
	frappe.db.bulk_update("MOP Log", doc_updates, chunk_size=100, update_modified=False)
	frappe.db.commit()
	n = len(doc_updates)
	doc_updates.clear()
	return n


def execute():
	"""Backfill blank ``to_warehouse`` on Employee IR MOP Log rows.

	Employee IR *Receive* audit clones were historically written without a
	destination warehouse (the receive path, unlike the issue path, did not
	guarantee a resolved warehouse). Populate ``to_warehouse`` using the same
	fallback chain the on-submit fix now uses:

	  1. the row's Manufacturing Operation department's Manufacturing warehouse
	  2. the latest non-null ``to_warehouse`` already on the MWO's logs

	``from_warehouse`` is filled best-effort from the originating Employee IR's
	employee / subcontractor Manufacturing warehouse. Fields are read-only, so
	``frappe.db.set_value`` is used to bypass the form guard.
	"""
	rows = frappe.db.get_all(
		"MOP Log",
		filters={
			"voucher_type": "Employee IR",
			"to_warehouse": ["in", [None, ""]],
		},
		fields=[
			"name",
			"manufacturing_operation",
			"manufacturing_work_order",
			"voucher_no",
			"from_warehouse",
		],
	)
	if not rows:
		print("backfill_employee_ir_mop_log_to_warehouse: nothing to backfill")
		return

	to_wh_by_mop = {}
	to_wh_by_mwo = {}
	from_wh_by_eir = {}
	doc_updates = {}
	updated = 0

	for row in rows:
		# --- destination (to_warehouse) ---
		to_wh = None
		mop = row.manufacturing_operation
		if mop:
			if mop not in to_wh_by_mop:
				department = frappe.db.get_value(
					"Manufacturing Operation", mop, "department"
				)
				to_wh_by_mop[mop] = (
					_resolve_department_warehouse({"department": department})
					if department
					else None
				)
			to_wh = to_wh_by_mop[mop]

		mwo = row.manufacturing_work_order
		if not to_wh and mwo:
			if mwo not in to_wh_by_mwo:
				to_wh_by_mwo[mwo] = _get_t_warehouse_from_logs(
					frappe.get_all(
						"MOP Log",
						filters={
							"manufacturing_work_order": mwo,
							"is_cancelled": 0,
							"to_warehouse": ["not in", [None, ""]],
						},
						fields=["to_warehouse", "flow_index", "creation"],
					)
				)
			to_wh = to_wh_by_mwo[mwo]

		# --- source (from_warehouse), best-effort ---
		from_wh = row.from_warehouse
		eir = row.voucher_no
		if not from_wh and eir:
			if eir not in from_wh_by_eir:
				from_wh_by_eir[eir] = _resolve_employee_ir_actor_warehouse(eir)
			from_wh = from_wh_by_eir[eir]

		updates = {}
		if to_wh:
			updates["to_warehouse"] = to_wh
		if from_wh and not row.from_warehouse:
			updates["from_warehouse"] = from_wh
		if updates:
			doc_updates[row.name] = updates
			if len(doc_updates) >= FLUSH_EVERY:
				updated += _flush(doc_updates)

	updated += _flush(doc_updates)
	print(
		f"backfill_employee_ir_mop_log_to_warehouse: scanned {len(rows)}, "
		f"updated {updated} MOP Log row(s)"
	)


def _resolve_employee_ir_actor_warehouse(eir_name):
	"""Manufacturing warehouse of the Employee IR's employee / subcontractor."""
	eir = frappe.db.get_value(
		"Employee IR",
		eir_name,
		["subcontracting", "company", "subcontractor", "employee"],
		as_dict=True,
	)
	if not eir:
		return None
	if eir.subcontracting == "Yes":
		return frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"company": eir.company,
				"subcontractor": eir.subcontractor,
				"warehouse_type": "Manufacturing",
			},
		)
	return frappe.db.get_value(
		"Warehouse",
		{
			"disabled": 0,
			"employee": eir.employee,
			"warehouse_type": "Manufacturing",
		},
	)
