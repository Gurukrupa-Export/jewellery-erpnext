import frappe
from frappe import _
from frappe.utils import cint, flt

SCENARIO_PC_TO_TAGGING_ISSUE = "PC_TO_TAGGING_ISSUE"
SCENARIO_TAGGING_TO_PC_RECEIVE = "TAGGING_TO_PC_RECEIVE"

_PC_DEPT = "Product Certification"
_TAGGING_DEPT = "Tagging"

TOLERANCE = 0.0001


def process_pc_tagging_stock_sync(dept_ir_doc, cancel=False):
	scenario = _resolve_scenario(dept_ir_doc)
	if not scenario:
		return

	for row in dept_ir_doc.department_ir_operation:
		if cancel:
			_handle_cancel_row(dept_ir_doc, row, scenario)
		else:
			_process_row(dept_ir_doc, row, scenario)


def _resolve_scenario(doc):
	current = _norm(doc.current_department or "")
	nxt = _norm(doc.next_department or "")
	prev = _norm(doc.previous_department or "")

	if doc.type == "Issue" and current == _PC_DEPT and nxt == _TAGGING_DEPT:
		return SCENARIO_PC_TO_TAGGING_ISSUE
	if doc.type == "Receive" and prev == _PC_DEPT and current == _TAGGING_DEPT:
		return SCENARIO_TAGGING_TO_PC_RECEIVE
	return None


def _norm(dept):
	return dept.split(" - ")[0].strip() if " - " in dept else dept.strip()


def _requires_pcs(item_code):
	return bool(item_code) and item_code[0] in ("D", "G")


def _resolve_dept_manufacturing_wh(department):
	return frappe.db.get_value(
		"Warehouse",
		{"disabled": 0, "department": department, "warehouse_type": "Manufacturing"},
		"name",
	)


def _resolve_dept_transit_wh(department):
	return frappe.db.get_value(
		"Warehouse",
		{"disabled": 0, "department": department, "warehouse_type": "Manufacturing"},
		"default_in_transit_warehouse",
	)


def _get_dept_ir_mop_logs(dept_ir_name, row_name):
	return frappe.db.get_all(
		"MOP Log",
		filters={
			"voucher_type": "Department IR",
			"voucher_no": dept_ir_name,
			"row_name": row_name,
			"is_cancelled": 0,
		},
		fields=[
			"name",
			"item_code",
			"batch_no",
			"qty_after_transaction_batch_based",
			"pcs_after_transaction_batch_based",
			"from_warehouse",
			"to_warehouse",
			"manufacturing_operation",
			"manufacturing_work_order",
		],
	)


def _get_active_sres_for_mwo(mwo):
	return frappe.db.get_all(
		"Stock Reservation Entry",
		filters={
			"manufacturing_work_order": mwo,
			"docstatus": 1,
			"status": ["not in", ["Cancelled", "Delivered"]],
		},
		fields=[
			"name",
			"item_code",
			"warehouse",
			"reserved_qty",
			"delivered_qty",
			"has_batch_no",
			"reservation_based_on",
			"voucher_type",
			"voucher_no",
			"voucher_detail_no",
			"company",
			"stock_uom",
			"manufacturing_operation",
			"manufacturing_work_order",
		],
	)


def _get_sre_batch_entries(sre_names):
	if not sre_names:
		return {}
	rows = frappe.db.get_all(
		"Serial and Batch Entry",
		filters={"parent": ["in", sre_names], "parenttype": "Stock Reservation Entry"},
		fields=["parent", "batch_no", "qty", "warehouse"],
	)
	result = {}
	for r in rows:
		result.setdefault(r["parent"], []).append(r)
	return result


def _build_sre_info_by_key(active_sres, item_batch_keys):
	"""Map (item_code, batch_no) → (sre_name, warehouse, available_qty)."""
	sre_names = [s["name"] for s in active_sres]
	batch_entries_by_sre = _get_sre_batch_entries(sre_names)

	info = {}
	for sre in active_sres:
		avail = flt(sre["reserved_qty"]) - flt(sre["delivered_qty"])
		if (
			cint(sre["has_batch_no"])
			and sre["reservation_based_on"] == "Serial and Batch"
		):
			for be in batch_entries_by_sre.get(sre["name"], []):
				key = (sre["item_code"], be["batch_no"])
				if key in item_batch_keys:
					existing = info.get(key)
					if not existing or flt(be["qty"]) > existing[2]:
						info[key] = (sre["name"], sre["warehouse"], flt(be["qty"]))
		else:
			for key in item_batch_keys:
				if key[0] == sre["item_code"]:
					existing = info.get(key)
					if not existing or avail > existing[2]:
						info[key] = (sre["name"], sre["warehouse"], avail)
	return info


def _process_row(dept_ir_doc, row, scenario):
	mwo = row.manufacturing_work_order
	mop_name = row.manufacturing_operation

	dept_ir_logs = _get_dept_ir_mop_logs(dept_ir_doc.name, row.name)
	if not dept_ir_logs:
		return

	item_batch_keys = {(l["item_code"], l["batch_no"]) for l in dept_ir_logs}
	active_sres = _get_active_sres_for_mwo(mwo)
	sre_info_by_key = _build_sre_info_by_key(active_sres, item_batch_keys)

	if scenario == SCENARIO_TAGGING_TO_PC_RECEIVE:
		# SRE was placed at Tagging Transit by the Issue step.
		# Tagging Transit = current_department (Tagging) default_in_transit_warehouse.
		transit_wh = _resolve_dept_transit_wh(dept_ir_doc.current_department)
		if not transit_wh:
			frappe.throw(
				_(
					"Transit warehouse not found for department {0}. "
					"Set 'Default In Transit Warehouse' on the department's manufacturing warehouse."
				).format(dept_ir_doc.current_department)
			)
		for key in item_batch_keys:
			sre_hit = sre_info_by_key.get(key)
			if not sre_hit:
				frappe.throw(
					_(
						"No active Stock Reservation Entry found for item {0} batch {1} "
						"on work order {2}. Submit the PC-to-Tagging Issue first."
					).format(key[0], key[1], mwo)
				)
			sre_wh = sre_hit[1]
			if sre_wh != transit_wh:
				frappe.throw(
					_(
						"Cannot receive {0} batch {1}: the stock reservation is at {2}, "
						"not {3} (transit warehouse for {4}). "
						"Submit the PC-to-Tagging Issue movement first."
					).format(
						key[0],
						key[1],
						sre_wh,
						transit_wh,
						dept_ir_doc.current_department,
					)
				)

	# Build transfer lines from MOP Logs
	transfer_lines = []
	for log in dept_ir_logs:
		item_code = log["item_code"]
		batch_no = log["batch_no"]
		qty = flt(log.get("qty_after_transaction_batch_based") or 0)
		if qty <= TOLERANCE:
			continue

		sre_hit = sre_info_by_key.get((item_code, batch_no))
		if not sre_hit:
			frappe.throw(
				_(
					"No active Stock Reservation Entry for item {0} batch {1} on MWO {2}."
				).format(item_code, batch_no, mwo)
			)

		s_warehouse = sre_hit[1]
		t_warehouse = log["to_warehouse"]
		pcs = (
			cint(log.get("pcs_after_transaction_batch_based") or 0)
			if _requires_pcs(item_code)
			else 0
		)
		uom = frappe.get_cached_value("Item", item_code, "stock_uom") or "Nos"

		transfer_lines.append(
			{
				"item_code": item_code,
				"batch_no": batch_no,
				"qty": flt(qty, 3),
				"pcs": pcs,
				"s_warehouse": s_warehouse,
				"t_warehouse": t_warehouse,
				"uom": uom,
				"sre_name": sre_hit[0],
				"mop_name": mop_name,
				"mwo": mwo,
			}
		)

	if not transfer_lines:
		return

	# Step 1: Cancel old SREs first (unreserves stock so SE can proceed)
	cancelled_sre_names = set()
	for line in transfer_lines:
		sre_name = line["sre_name"]
		if sre_name not in cancelled_sre_names:
			sre_doc = frappe.get_doc("Stock Reservation Entry", sre_name)
			if cint(sre_doc.docstatus) == 1:
				sre_doc.cancel()
			cancelled_sre_names.add(sre_name)

	# Step 2: Create Material Transfer SE
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Material Transfer to Department"
	se.company = dept_ir_doc.company
	se.department_ir = dept_ir_doc.name
	se.auto_created = 1

	for line in transfer_lines:
		item_row = {
			"item_code": line["item_code"],
			"qty": line["qty"],
			"s_warehouse": line["s_warehouse"],
			"t_warehouse": line["t_warehouse"],
			"uom": line["uom"],
			"stock_uom": line["uom"],
			"use_serial_batch_fields": 1,
			"manufacturing_operation": line["mop_name"],
		}
		if line["batch_no"]:
			item_row["batch_no"] = line["batch_no"]
		if line["pcs"]:
			item_row["custom_pcs"] = line["pcs"]
		se.append("items", item_row)

	se.save()
	se.submit()

	# Step 3: Create replacement SREs at new warehouse
	# Group by (sre_name) to get the original SRE fields for copying
	original_sres_by_name = {s["name"]: s for s in active_sres}

	# Group transfer lines by (item_code, batch_no, t_warehouse) to sum qty per new SRE
	new_sre_groups = {}
	for line in transfer_lines:
		key = (
			line["item_code"],
			line["batch_no"],
			line["t_warehouse"],
			line["sre_name"],
		)
		if key not in new_sre_groups:
			new_sre_groups[key] = {"qty": 0.0, "pcs": 0}
		new_sre_groups[key]["qty"] += line["qty"]
		new_sre_groups[key]["pcs"] += line["pcs"]

	for (item_code, batch_no, new_wh, orig_sre_name), totals in new_sre_groups.items():
		if totals["qty"] <= TOLERANCE:
			continue
		orig = original_sres_by_name.get(orig_sre_name)
		if not orig:
			continue

		new_sre = frappe.new_doc("Stock Reservation Entry")
		new_sre.item_code = item_code
		new_sre.warehouse = new_wh
		new_sre.company = orig["company"] or dept_ir_doc.company
		new_sre.stock_uom = orig["stock_uom"] or frappe.get_cached_value(
			"Item", item_code, "stock_uom"
		)
		new_sre.voucher_type = orig["voucher_type"]
		new_sre.voucher_no = orig["voucher_no"]
		new_sre.voucher_detail_no = orig["voucher_detail_no"]
		new_sre.reserved_qty = flt(totals["qty"], 3)
		new_sre.voucher_qty = flt(totals["qty"], 3)
		new_sre.available_qty = flt(totals["qty"], 3)
		new_sre.manufacturing_work_order = orig["manufacturing_work_order"]
		new_sre.manufacturing_operation = orig["manufacturing_operation"]
		new_sre.has_batch_no = cint(orig["has_batch_no"])
		new_sre.has_serial_no = 0

		if cint(orig["has_batch_no"]) and batch_no:
			new_sre.reservation_based_on = "Serial and Batch"
			new_sre.append(
				"sb_entries",
				{
					"batch_no": batch_no,
					"warehouse": new_wh,
					"qty": flt(totals["qty"], 3),
				},
			)
		else:
			new_sre.reservation_based_on = "Qty"

		new_sre.insert(ignore_links=1)
		new_sre.submit()


def _handle_cancel_row(dept_ir_doc, row, scenario):
	"""On Department IR cancel: cancel replacement SREs, then let existing SE cancel handle the SE."""
	mwo = row.manufacturing_work_order

	# Find SREs linked to the new warehouse (created during submit)
	# The existing Department IR cancel code cancels the Stock Entries already.
	# We need to cancel any replacement SREs created by this service.
	# These SREs are at the transit WH (Issue) or working WH (Receive).
	if scenario == SCENARIO_PC_TO_TAGGING_ISSUE:
		target_wh = _resolve_dept_transit_wh(dept_ir_doc.next_department)
	else:
		target_wh = _resolve_dept_manufacturing_wh(dept_ir_doc.current_department)

	if not target_wh:
		return

	replacement_sres = frappe.db.get_all(
		"Stock Reservation Entry",
		filters={
			"manufacturing_work_order": mwo,
			"warehouse": target_wh,
			"docstatus": 1,
			"status": ["not in", ["Cancelled", "Delivered"]],
		},
		fields=[
			"name",
			"item_code",
			"warehouse",
			"reserved_qty",
			"delivered_qty",
			"has_batch_no",
			"reservation_based_on",
			"voucher_type",
			"voucher_no",
			"voucher_detail_no",
			"company",
			"stock_uom",
			"manufacturing_operation",
			"manufacturing_work_order",
		],
	)

	for sre_row in replacement_sres:
		sre_doc = frappe.get_doc("Stock Reservation Entry", sre_row["name"])
		if cint(sre_doc.docstatus) == 1:
			sre_doc.cancel()

	# Restore original SRE at source warehouse
	if scenario == SCENARIO_PC_TO_TAGGING_ISSUE:
		# Issue cancel: restore SRE back to PC WO (current_department = PC)
		source_wh = _resolve_dept_manufacturing_wh(dept_ir_doc.current_department)
	else:
		# Receive cancel: restore SRE back to Tagging Transit (current_department = Tagging)
		source_wh = _resolve_dept_transit_wh(dept_ir_doc.current_department)

	if not source_wh:
		return

	dept_ir_logs_all = []
	for row_obj in dept_ir_doc.department_ir_operation:
		if row_obj.name == row.name:
			dept_ir_logs_all = _get_dept_ir_mop_logs(dept_ir_doc.name, row.name)
			break

	if not dept_ir_logs_all:
		return

	# Group by (item_code, batch_no) to sum qty
	restore_groups = {}
	for log in dept_ir_logs_all:
		item_code = log["item_code"]
		batch_no = log["batch_no"]
		qty = flt(log.get("qty_after_transaction_batch_based") or 0)
		if qty <= TOLERANCE:
			continue
		key = (item_code, batch_no)
		restore_groups[key] = restore_groups.get(key, 0.0) + qty

	# Find original voucher info from any existing cancelled SRE for this MWO
	original_sre_info = (
		frappe.db.get_value(
			"Stock Reservation Entry",
			{"manufacturing_work_order": mwo, "docstatus": 2},
			[
				"voucher_type",
				"voucher_no",
				"voucher_detail_no",
				"company",
				"stock_uom",
				"manufacturing_operation",
				"manufacturing_work_order",
				"has_batch_no",
			],
			as_dict=True,
		)
		or {}
	)

	for (item_code, batch_no), qty in restore_groups.items():
		has_batch_no = frappe.get_cached_value("Item", item_code, "has_batch_no")
		new_sre = frappe.new_doc("Stock Reservation Entry")
		new_sre.item_code = item_code
		new_sre.warehouse = source_wh
		new_sre.company = original_sre_info.get("company") or dept_ir_doc.company
		new_sre.stock_uom = original_sre_info.get(
			"stock_uom"
		) or frappe.get_cached_value("Item", item_code, "stock_uom")
		new_sre.voucher_type = (
			original_sre_info.get("voucher_type") or "Manufacturing Order"
		)
		new_sre.voucher_no = original_sre_info.get("voucher_no") or mwo
		new_sre.voucher_detail_no = original_sre_info.get("voucher_detail_no")
		new_sre.reserved_qty = flt(qty, 3)
		new_sre.voucher_qty = flt(qty, 3)
		new_sre.available_qty = flt(qty, 3)
		new_sre.manufacturing_work_order = mwo
		new_sre.manufacturing_operation = (
			original_sre_info.get("manufacturing_operation")
			or row.manufacturing_operation
		)
		new_sre.has_batch_no = cint(has_batch_no)
		new_sre.has_serial_no = 0

		if cint(has_batch_no) and batch_no:
			new_sre.reservation_based_on = "Serial and Batch"
			new_sre.append(
				"sb_entries",
				{
					"batch_no": batch_no,
					"warehouse": source_wh,
					"qty": flt(qty, 3),
				},
			)
		else:
			new_sre.reservation_based_on = "Qty"

		new_sre.insert(ignore_links=1)
		new_sre.submit()
