"""
EOD MOP Log Sync — converts unsynced MOP Log entries into consolidating Stock Entries.

MOP Logs with ``is_synced = 0`` represent virtual warehouse movements recorded by
Department IR and Employee IR.  This module groups those logs by Manufacturing
Operation and creates:

1. A **Material Transfer** Stock Entry that moves items from the first warehouse
   in the log chain to the last warehouse.
2. If the Manufacturing Operation has ``loss_wt`` < 0 (process loss), a **Repack**
   Stock Entry that books the loss to the configured loss warehouse.

The sync is designed to run via the "Sync MOP Log" button on MOP Settings or via
a scheduled background job.

**Stock Reservation:** Do not create ``Stock Reservation Entry`` here before submit unless
you mirror ``stock_reservation_entry_for_mwo`` in ``doc_events/stock_entry.py`` exactly
(append ``sb_entries`` with ``batch_no``, ``warehouse``, ``qty`` for batch items, or use
``reservation_based_on = Qty``). Otherwise ERPNext raises *Please select Serial/Batch Nos
to reserve...*. For ``Material Transfer to Department`` lines with ``t_warehouse``,
reservation is created on Stock Entry **onsubmit** by ``stock_reservation_entry_for_mwo``.

Before submit, batch lines are checked with ``get_batch_qty(..., ignore_reserved_stock=True)``
at the **source** warehouse so MOP Log totals cannot exceed **physical** batch stock (SLE).
Net pickable qty (after subtracting undelivered Stock Reservation Entry) is not used here,
so fully reserved metal is not false-rejected. Optional audit logs when physical allows the move
but open SRE undelivered for the same MWO/batch/warehouse is below the transfer qty.

**Per-operation fallback:** When multiple Manufacturing Operations share the same routing hop,
the sync first attempts one consolidated Material Transfer Stock Entry. If that fails (validation,
save, or submit — including hooks), it logs the error and retries with **one Stock Entry per
operation**, setting header ``manufacturing_operation`` and ``manufacturer`` like a typical
department transfer. Physical batch validation is unchanged; splitting does not bypass shortage.
"""

import frappe
from frappe import _
from frappe.utils import flt, cint


def sync_mop_logs():
	"""Main entry point. Returns a summary dict for the UI."""
	unsynced_groups = _get_unsynced_mop_groups()
	processed = 0
	stock_entries = []

	for group_key, mop_data_list in unsynced_groups.items():
		frappe.db.savepoint("mop_eod_sync_hop")
		try:
			se_names, count = _sync_consolidated_group(group_key, mop_data_list)
			stock_entries.extend(se_names)
			processed += count
			frappe.db.release_savepoint("mop_eod_sync_hop")
		except Exception:
			frappe.db.rollback(save_point="mop_eod_sync_hop")
			company, mwo, first_wh, last_wh = group_key
			frappe.log_error(
				title=f"MOP EOD Sync failed for MWO {mwo}",
				message=f"Failed routing {first_wh} -> {last_wh}\n{frappe.get_traceback()}",
			)

	return {"processed": processed, "stock_entries": stock_entries}


def _get_unsynced_mop_groups():
	"""
	Return a dict of {(company, mwo, first_wh, last_wh): [{'mop_name': ..., 'mop_doc': ..., 'logs': ...}]}
	for all unsynced logs grouped by routing hop.
	"""
	logs = frappe.db.get_all(
		"MOP Log",
		filters={"is_synced": 0, "is_cancelled": 0},
		fields=[
			"name",
			"manufacturing_operation",
			"manufacturing_work_order",
			"item_code",
			"batch_no",
			"serial_no",
			"qty_after_transaction_batch_based",
			"pcs_after_transaction_batch_based",
			"from_warehouse",
			"to_warehouse",
			"flow_index",
			"voucher_type",
			"voucher_no",
		],
		order_by="manufacturing_operation, flow_index asc, creation asc",
	)

	mop_logs = {}
	for log in logs:
		mop_logs.setdefault(log.manufacturing_operation, []).append(log)

	mop_cache = {}
	groups = {}

	for mop_name, op_logs in mop_logs.items():
		if mop_name not in mop_cache:
			mop_doc_dict = frappe.db.get_value(
				"Manufacturing Operation",
				mop_name,
				[
					"company",
					"manufacturer",
					"manufacturing_work_order",
					"manufacturing_order",
					"department",
					"loss_wt",
				],
				as_dict=True,
			)
			mop_cache[mop_name] = mop_doc_dict
			
		mop = mop_cache[mop_name]
		if not mop:
			continue
			
		first_wh, last_wh = _resolve_warehouses(op_logs)
		if not first_wh or not last_wh:
			frappe.log_error(
				title=f"MOP EOD Sync: cannot resolve warehouses for {mop_name}",
				message=(
					"Skipping sync for this MOP — logs stay unsynced until warehouses can be "
					f"derived (first_wh={first_wh!r}, last_wh={last_wh!r})."
				),
			)
			continue

		group_key = (mop.company, mop.manufacturing_work_order, first_wh, last_wh)
		groups.setdefault(group_key, []).append({
			"mop_name": mop_name,
			"mop_doc": mop,
			"logs": op_logs
		})

	return groups


def _latest_flow_logs(logs):
	if not logs:
		return []
	max_idx = max(l.flow_index for l in logs)
	return [l for l in logs if l.flow_index == max_idx]


def _mop_manufacturer_label(mop):
	"""Manufacturer from cached Manufacturing Operation row (dict or document-like object)."""
	if mop is None:
		return None
	if isinstance(mop, dict):
		return mop.get("manufacturer")
	return getattr(mop, "manufacturer", None)


def _build_transfer_rows_for_mop(mop_data, first_wh, last_wh):
	"""Stock Entry item rows for one Manufacturing Operation (latest flow_index only)."""
	mop_name = mop_data["mop_name"]
	mop = mop_data["mop_doc"]
	rows = []
	for log in _latest_flow_logs(mop_data["logs"]):
		qty = flt(log.qty_after_transaction_batch_based, 3)
		if qty <= 0:
			continue
		row = {
			"item_code": log.item_code,
			"qty": qty,
			"s_warehouse": first_wh,
			"t_warehouse": last_wh,
			"manufacturing_operation": mop_name,
			"custom_manufacturing_work_order": mop.manufacturing_work_order,
			"use_serial_batch_fields": 1,
		}
		if log.batch_no:
			row["batch_no"] = log.batch_no
		if getattr(log, "serial_no", None):
			row["serial_no"] = log.serial_no
		rows.append(row)
	return rows


def _submit_eod_material_transfer_se(
	company,
	mwo,
	manufacturing_order,
	items,
	header_mop_name=None,
	header_manufacturer=None,
):
	"""Create, save, and submit one Material Transfer to Department Stock Entry; return name."""
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Material Transfer to Department"
	se.company = company
	se.manufacturing_order = manufacturing_order
	se.manufacturing_work_order = mwo
	se.auto_created = 1
	if header_mop_name:
		se.manufacturing_operation = header_mop_name
	if header_manufacturer:
		se.manufacturer = header_manufacturer
	for item in items:
		se.append("items", item)
	se.flags.ignore_permissions = True
	se.save()
	se.submit()
	return se.name


def _sync_consolidated_group(group_key, mop_data_list):
	"""Create consolidating Stock Entry for a merged routing hop."""
	company, mwo, first_wh, last_wh = group_key
	se_names = []
	all_processed_logs = []

	items_to_transfer = []
	loss_tasks = []
	manufacturing_order = None

	for mop_data in mop_data_list:
		mop_name = mop_data["mop_name"]
		mop = mop_data["mop_doc"]
		logs = mop_data["logs"]
		all_processed_logs.extend(logs)

		if not manufacturing_order:
			manufacturing_order = mop.manufacturing_order

		items_to_transfer.extend(_build_transfer_rows_for_mop(mop_data, first_wh, last_wh))

		latest_logs = _latest_flow_logs(logs)
		loss_wt = flt(mop.loss_wt, 3)
		if loss_wt < 0:
			loss_tasks.append((mop, mop_name, latest_logs, last_wh, abs(loss_wt)))

	used_per_mop_fallback = False
	if items_to_transfer and first_wh != last_wh:
		try:
			_validate_eod_items_for_mwo_reservation(items_to_transfer)
			_validate_eod_source_batch_stock(
				items_to_transfer,
				manufacturing_work_order=mwo,
				mop_data_list=mop_data_list,
				company=company,
			)
			se_names.append(
				_submit_eod_material_transfer_se(
					company,
					mwo,
					manufacturing_order,
					items_to_transfer,
					header_mop_name=None,
					header_manufacturer=None,
				)
			)
		except Exception:
			if len(mop_data_list) <= 1:
				raise
			frappe.log_error(
				title=_(
					"MOP EOD Sync: consolidated transfer failed, retrying per Manufacturing Operation"
				),
				message=(
					f"MWO {mwo}\n{first_wh} -> {last_wh}\n{frappe.get_traceback(with_context=1)}"
				),
			)
			used_per_mop_fallback = True
			for mop_data in mop_data_list:
				mop_name = mop_data["mop_name"]
				mop = mop_data["mop_doc"]
				logs = mop_data["logs"]
				latest_logs = _latest_flow_logs(logs)
				sub_items = _build_transfer_rows_for_mop(mop_data, first_wh, last_wh)
				if sub_items:
					_validate_eod_items_for_mwo_reservation(sub_items)
					_validate_eod_source_batch_stock(
						sub_items,
						manufacturing_work_order=mwo,
						mop_data_list=[mop_data],
						company=company,
					)
					header_mfr = _mop_manufacturer_label(mop)
					se_names.append(
						_submit_eod_material_transfer_se(
							company,
							mwo,
							manufacturing_order,
							sub_items,
							header_mop_name=mop_name,
							header_manufacturer=header_mfr,
						)
					)
				loss_wt = flt(mop.loss_wt, 3)
				if loss_wt < 0:
					se_names.extend(
						_create_loss_entries(mop, mop_name, latest_logs, last_wh, abs(loss_wt))
					)

	if not used_per_mop_fallback:
		for task in loss_tasks:
			loss_se_names = _create_loss_entries(*task)
			se_names.extend(loss_se_names)

	_mark_synced(all_processed_logs)
	return se_names, len(all_processed_logs)





def _resolve_warehouses(logs):
	"""Determine the first source warehouse and last target warehouse from the log chain."""
	if not logs:
		return None, None

	first_wh = None
	last_wh = None

	min_idx = min(l.flow_index for l in logs)
	max_idx = max(l.flow_index for l in logs)

	for log in logs:
		if log.flow_index == min_idx and log.from_warehouse and not first_wh:
			first_wh = log.from_warehouse
		if log.flow_index == max_idx and log.to_warehouse:
			last_wh = log.to_warehouse

	return first_wh, last_wh


def _validate_eod_items_for_mwo_reservation(items_to_transfer):
	"""
	Ensure lines carry batch/serial data required by ``stock_reservation_entry_for_mwo``
	on submit (Serial/Batch SRE needs ``row.batch_no`` on the Stock Entry row when the
	item is batch-tracked).
	"""
	for item in items_to_transfer:
		item_code = item.get("item_code")
		if not item_code or flt(item.get("qty")) <= 0:
			continue
		item_flags = frappe.db.get_value(
			"Item", item_code, ["has_batch_no", "has_serial_no"]
		)
		if not item_flags:
			frappe.throw(
				_("MOP EOD Sync: Item {0} not found.").format(frappe.bold(item_code))
			)
		has_batch_no, has_serial_no = item_flags
		mop_label = item.get("manufacturing_operation") or "?"
		if cint(has_batch_no) and not item.get("batch_no"):
			frappe.throw(
				_(
					"MOP EOD Sync: item {0} is batch-tracked but the MOP Log line has no Batch No "
					"(Manufacturing Operation {1}). Stock Reservation on submit cannot build "
					"sb_entries — fix the source MOP Log / vouchers, then retry."
				).format(frappe.bold(item_code), frappe.bold(mop_label))
			)
		if cint(has_serial_no) and not item.get("serial_no"):
			frappe.throw(
				_(
					"MOP EOD Sync: item {0} is serialized but the MOP Log line has no Serial No "
					"(Manufacturing Operation {1})."
				).format(frappe.bold(item_code), frappe.bold(mop_label))
			)


def _resolve_eod_manufacturer_label(mop_data_list, manufacturing_work_order):
	"""Manufacturer from Manufacturing Operation rows; fallback to Manufacturing Work Order."""
	if not mop_data_list:
		if manufacturing_work_order:
			return frappe.db.get_value(
				"Manufacturing Work Order", manufacturing_work_order, "manufacturer"
			)
		return None
	mfrs = set()
	for md in mop_data_list:
		mdoc = md.get("mop_doc")
		if not mdoc:
			continue
		m = mdoc.get("manufacturer")
		if m:
			mfrs.add(m)
	if mfrs:
		return ", ".join(sorted(mfrs))
	if manufacturing_work_order:
		return frappe.db.get_value(
			"Manufacturing Work Order", manufacturing_work_order, "manufacturer"
		)
	return None


def _collect_mop_names(mop_data_list):
	if not mop_data_list:
		return ""
	names = sorted({md.get("mop_name") for md in mop_data_list if md.get("mop_name")})
	return ", ".join(names)


def _list_open_sre_for_batch(item_code, warehouse, batch_no, manufacturing_work_order=None):
	"""Submitted Serial/Batch SRE rows with undelivered qty at ``warehouse`` (diagnostics)."""
	from frappe.query_builder.functions import Sum

	sb = frappe.qb.DocType("Serial and Batch Entry")
	sre = frappe.qb.DocType("Stock Reservation Entry")
	q = (
		frappe.qb.from_(sre)
		.inner_join(sb)
		.on(sre.name == sb.parent)
		.select(sre.name, sre.warehouse, Sum(sb.qty - sb.delivered_qty).as_("open_qty"))
		.where(sre.docstatus == 1)
		.where(sre.item_code == item_code)
		.where(sre.warehouse == warehouse)
		.where(sb.batch_no == batch_no)
		.where(sre.reserved_qty >= sre.delivered_qty)
		.where(sre.status.notin(["Delivered", "Cancelled"]))
		.where(sre.reservation_based_on == "Serial and Batch")
		.groupby(sre.name, sre.warehouse)
	)
	if manufacturing_work_order:
		q = q.where(sre.manufacturing_work_order == manufacturing_work_order)
	return q.run(as_dict=True)


def _list_open_sre_other_warehouses(
	item_code, batch_no, manufacturing_work_order=None, exclude_warehouse=None, limit=5
):
	"""Open SRE lines for same item/batch but different warehouse (wrong-WH hint)."""
	from frappe.query_builder.functions import Sum

	sb = frappe.qb.DocType("Serial and Batch Entry")
	sre = frappe.qb.DocType("Stock Reservation Entry")
	q = (
		frappe.qb.from_(sre)
		.inner_join(sb)
		.on(sre.name == sb.parent)
		.select(sre.name, sre.warehouse, Sum(sb.qty - sb.delivered_qty).as_("open_qty"))
		.where(sre.docstatus == 1)
		.where(sre.item_code == item_code)
		.where(sb.batch_no == batch_no)
		.where(sre.reserved_qty >= sre.delivered_qty)
		.where(sre.status.notin(["Delivered", "Cancelled"]))
		.where(sre.reservation_based_on == "Serial and Batch")
		.groupby(sre.name, sre.warehouse)
		.limit(limit)
	)
	if exclude_warehouse:
		q = q.where(sre.warehouse != exclude_warehouse)
	if manufacturing_work_order:
		q = q.where(sre.manufacturing_work_order == manufacturing_work_order)
	return q.run(as_dict=True)


def _format_batch_short_diagnostics(
	item_code,
	warehouse,
	batch_no,
	req_qty,
	physical,
	manufacturing_work_order,
	mop_data_list,
	company,
):
	"""Extra lines for ValidationError when physical batch qty is insufficient."""
	lines = []
	if company:
		lines.append(_("Company: {0}").format(company))
	if manufacturing_work_order:
		lines.append(_("Manufacturing Work Order: {0}").format(manufacturing_work_order))
	mops = _collect_mop_names(mop_data_list)
	if mops:
		lines.append(_("Manufacturing Operation(s): {0}").format(mops))
	mfr = _resolve_eod_manufacturer_label(mop_data_list, manufacturing_work_order)
	if mfr:
		lines.append(_("Manufacturer: {0}").format(mfr))
	else:
		lines.append(_("Manufacturer: (not set on Operation / Work Order)"))

	sre_here = _list_open_sre_for_batch(
		item_code, warehouse, batch_no, manufacturing_work_order=manufacturing_work_order
	)
	if not sre_here and manufacturing_work_order:
		sre_here = _list_open_sre_for_batch(item_code, warehouse, batch_no, manufacturing_work_order=None)

	total_open_here = 0.0
	for row in sre_here:
		oq = flt(row.get("open_qty"), 3)
		if oq <= 0:
			continue
		total_open_here += oq
		lines.append(
			_("Open Stock Reservation Entry {0} @ {1}: undelivered {2}").format(
				row.get("name"), row.get("warehouse"), oq
			)
		)

	if physical <= 1e-6 and total_open_here > 1e-6:
		lines.append(
			_(
				"Hint: physical batch qty is 0 but open reservation(s) exist at this warehouse — "
				"likely stale SRE or stock moved without updating reservation; cancel/amend SRE or restore stock."
			)
		)
	elif not sre_here:
		other = _list_open_sre_other_warehouses(
			item_code,
			batch_no,
			manufacturing_work_order=manufacturing_work_order,
			exclude_warehouse=warehouse,
		)
		if other:
			parts = [
				_("{0} @ {1} (open {2})").format(
					r.get("name"), r.get("warehouse"), flt(r.get("open_qty"), 3)
				)
				for r in other
				if flt(r.get("open_qty"), 3) > 0
			]
			if parts:
				lines.append(
					_("Open reservations on other warehouse(s) (sample): {0}").format("; ".join(parts))
				)

	return "\n".join(lines)


def _get_sre_undelivered_batch_qty(item_code, warehouse, batch_no, manufacturing_work_order=None):
	"""Sum undelivered qty on submitted Serial/Batch Stock Reservation Entry rows (audit helper)."""
	from frappe.query_builder.functions import Sum

	sb = frappe.qb.DocType("Serial and Batch Entry")
	sre = frappe.qb.DocType("Stock Reservation Entry")
	q = (
		frappe.qb.from_(sre)
		.inner_join(sb)
		.on(sre.name == sb.parent)
		.select(Sum(sb.qty - sb.delivered_qty).as_("qty"))
		.where(sre.docstatus == 1)
		.where(sre.item_code == item_code)
		.where(sre.warehouse == warehouse)
		.where(sb.batch_no == batch_no)
		.where(sre.reserved_qty >= sre.delivered_qty)
		.where(sre.status.notin(["Delivered", "Cancelled"]))
		.where(sre.reservation_based_on == "Serial and Batch")
	)
	if manufacturing_work_order:
		q = q.where(sre.manufacturing_work_order == manufacturing_work_order)
	rows = q.run(as_list=True)
	if not rows or rows[0][0] is None:
		return 0.0
	return flt(rows[0][0], 3)


def _validate_eod_source_batch_stock(
	items_to_transfer,
	manufacturing_work_order=None,
	mop_data_list=None,
	company=None,
):
	"""
	Ensure aggregated transfer qty per (source warehouse, item, batch) does not exceed
	**physical** batch balance (SLE / serial-batch ledger), ignoring ERPNext's net
	“pickable” qty that subtracts undelivered Stock Reservation Entry rows.

	Without ``ignore_reserved_stock=True``, ``get_batch_qty`` can return 0.0 while metal
	still exists — all of it is reserved after MWO receive — and EOD would false-reject.
	Reservation does **not** replace physical stock: if physical is 0, we still throw.
	"""
	from erpnext.stock.doctype.batch.batch import get_batch_qty
	from frappe.utils import nowtime, today

	posting_date = today()
	posting_time = nowtime()
	needed = {}
	for item in items_to_transfer:
		wh = item.get("s_warehouse")
		item_code = item.get("item_code")
		batch_no = item.get("batch_no")
		qty = flt(item.get("qty"), 3)
		if not wh or not item_code or qty <= 0 or not batch_no:
			continue
		key = (wh, item_code, batch_no)
		needed[key] = flt(needed.get(key, 0) + qty, 3)

	for (wh, item_code, batch_no), req_qty in needed.items():
		try:
			physical_raw = get_batch_qty(
				batch_no=batch_no,
				warehouse=wh,
				item_code=item_code,
				posting_date=posting_date,
				posting_time=posting_time,
				ignore_reserved_stock=True,
			)
		except Exception:
			physical_raw = None
		physical = flt(physical_raw, 3) if physical_raw is not None else 0.0
		if req_qty > physical + 1e-6:
			short = flt(req_qty - physical, 3)
			detail = _format_batch_short_diagnostics(
				item_code,
				wh,
				batch_no,
				req_qty,
				physical,
				manufacturing_work_order,
				mop_data_list,
				company,
			)
			main = _(
				"MOP EOD Sync: cannot move {0} of item {1}, batch {2} from {3}: "
				"MOP Log(s) require {4} but only {5} physical qty exists for this batch in that warehouse "
				"(short by {6}; SLE / batch ledger — reservation does not create stock). "
				"Reconcile vouchers, MOP Log, or cancel stale reservations."
			).format(
				frappe.bold(req_qty),
				frappe.bold(item_code),
				frappe.bold(batch_no),
				frappe.bold(wh),
				req_qty,
				physical,
				short,
			)
			frappe.throw(
				main + "\n\n" + detail,
				title=_("MOP EOD Sync — insufficient batch stock"),
			)

		if manufacturing_work_order:
			sre_open = _get_sre_undelivered_batch_qty(
				item_code, wh, batch_no, manufacturing_work_order=manufacturing_work_order
			)
			if sre_open + 1e-6 < req_qty:
				frappe.log_error(
					title=_("MOP EOD Sync — reservation audit"),
					message=_(
						"Transfer {0} {1} batch {2} from {3} (MWO {4}): physical qty {5} allows the move, "
						"but undelivered Stock Reservation Entry qty for this item/batch/warehouse/MWO is only {6}. "
						"Verify SO reservation vs physical issue rules."
					).format(
						req_qty,
						item_code,
						batch_no,
						wh,
						manufacturing_work_order,
						physical,
						sre_open,
					),
				)


def _create_loss_entries(mop, mop_name, latest_logs, warehouse, total_loss):
	"""Create Repack Stock Entry for process loss."""
	from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
		get_item_loss_item,
	)

	se_names = []
	metal_logs = [l for l in latest_logs if l.item_code and l.item_code[0] in ("M", "F")]
	if not metal_logs:
		return se_names

	total_metal_qty = sum(flt(l.qty_after_transaction_batch_based, 3) for l in metal_logs)
	if total_metal_qty <= 0:
		return se_names

	variant_loss_details = frappe.db.get_value(
		"Variant Loss Warehouse",
		{"parent": mop.manufacturer, "variant": "M"},
		["loss_warehouse", "consider_department_warehouse", "warehouse_type"],
		as_dict=True,
	)

	loss_warehouse = None
	if variant_loss_details:
		if variant_loss_details.get("loss_warehouse"):
			loss_warehouse = variant_loss_details.get("loss_warehouse")
		elif variant_loss_details.get("consider_department_warehouse") and variant_loss_details.get(
			"warehouse_type"
		):
			loss_warehouse = frappe.db.get_value(
				"Warehouse",
				{
					"department": mop.department,
					"warehouse_type": variant_loss_details.get("warehouse_type"),
				},
			)

	if not loss_warehouse:
		frappe.log_error(
			title=f"MOP EOD Sync: loss warehouse not found for {mop_name}",
			message="Skipping loss entry creation — configure Variant Loss Warehouse on Manufacturer.",
		)
		return se_names

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Repack"
	se.company = mop.company
	se.manufacturing_order = mop.manufacturing_order
	se.manufacturing_work_order = mop.manufacturing_work_order
	se.manufacturing_operation = mop_name
	se.auto_created = 1

	remaining_loss = total_loss
	for log in metal_logs:
		qty = flt(log.qty_after_transaction_batch_based, 3)
		if qty <= 0:
			continue
		proportional_loss = flt((qty / total_metal_qty) * total_loss, 3)
		if proportional_loss <= 0:
			continue
		remaining_loss -= proportional_loss
		se.append(
			"items",
			{
				"item_code": log.item_code,
				"qty": proportional_loss,
				"s_warehouse": warehouse,
				"batch_no": log.batch_no,
				"manufacturing_operation": mop_name,
				"use_serial_batch_fields": 1,
			},
		)

	if se.items:
		loss_item = get_item_loss_item(mop.company, se.items[0].item_code, "M")
		if loss_item:
			se.append(
				"items",
				{
					"item_code": loss_item,
					"qty": total_loss,
					"t_warehouse": loss_warehouse,
					"manufacturing_operation": mop_name,
					"use_serial_batch_fields": 1,
				},
			)
			se.flags.ignore_permissions = True
			se.save()
			se.submit()
			se_names.append(se.name)

	return se_names


def _mark_synced(logs):
	"""Mark all MOP Log entries as synced."""
	log_names = [l.name for l in logs]
	if log_names:
		frappe.db.set_value(
			"MOP Log",
			{"name": ["in", log_names]},
			"is_synced",
			1,
		)
