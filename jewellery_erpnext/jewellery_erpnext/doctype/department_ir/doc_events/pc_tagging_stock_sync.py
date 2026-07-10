import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	get_available_qty_to_reserve,
	get_sre_reserved_qty_for_voucher_detail_no,
)
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
			"flow_index",
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


# ---------------------------------------------------------------------------
# Module-level cache for _doctype_has_field — avoids repeated DB round-trips.
# ---------------------------------------------------------------------------
_field_cache: dict = {}


def _doctype_has_field(doctype, fieldname):
	"""Return True if fieldname exists as a column in the doctype table."""
	key = (doctype, fieldname)
	if key not in _field_cache:
		_field_cache[key] = fieldname in frappe.db.get_table_columns(doctype)
	return _field_cache[key]


def _safe_set(doc, fieldname, value):
	"""Set doc.fieldname = value only if the field exists in the doctype meta."""
	if frappe.get_meta(doc.doctype).has_field(fieldname):
		setattr(doc, fieldname, value)


def _get_dept_ir_mop_logs_any(dept_ir_name, row_name):
	"""Like _get_dept_ir_mop_logs but WITHOUT the is_cancelled filter.

	Used in the cancel path because MOP Logs are bulk-marked is_cancelled=1
	*before* process_pc_tagging_stock_sync(cancel=True) is called, so
	_get_dept_ir_mop_logs (which filters is_cancelled=0) would return empty.
	"""
	return frappe.db.get_all(
		"MOP Log",
		filters={
			"voucher_type": "Department IR",
			"voucher_no": dept_ir_name,
			"row_name": row_name,
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
			"flow_index",
		],
	)


def _find_stock_entry_source_warehouse(
	dept_ir_name, row_name, item_code, batch_no, mwo
):
	"""Fallback: resolve s_warehouse from an existing submitted SE Detail row
	linked to this Department IR, for the given item/batch combination.

	This is the secondary fallback after MOP Log from_warehouse and before SRE.
	"""
	se_names = frappe.db.get_all(
		"Stock Entry",
		filters={"department_ir": dept_ir_name, "docstatus": 1},
		pluck="name",
	)
	if not se_names:
		return None
	filters = {"parent": ["in", se_names], "item_code": item_code}
	if batch_no:
		filters["batch_no"] = batch_no
	rows = frappe.db.get_all(
		"Stock Entry Detail",
		filters=filters,
		fields=["s_warehouse"],
		limit=1,
	)
	return rows[0]["s_warehouse"] if rows else None


def _physical_batch_qty(item_code, batch_no, warehouse):
	"""Physical SBB qty of (item, batch) in warehouse, ignoring reservations.

	Returns None for non-batch lines (batch_no falsy) — the SBB negative-batch
	validator only fires on batched items. ``ignore_reserved_stock=True`` is
	required: the batch we are about to consume is itself reserved by the SRE
	being processed, so the default (reservation-subtracted) qty would understate
	the correct warehouse. Mirrors ``_warehouse_has_batch_stock`` in
	serial_number_creator.py.
	"""
	if not batch_no or not warehouse:
		return None
	try:
		return flt(
			get_batch_qty(batch_no, warehouse, item_code, ignore_reserved_stock=True),
			3,
		)
	except Exception:
		return 0.0


def _warehouses_with_physical_batch(item_code, batch_no):
	"""Return [(warehouse, qty)] for warehouses physically holding the batch.

	Sorted by qty descending. Used only for the fail-fast diagnostic message.
	``get_batch_qty`` with no warehouse returns a list of {batch_no, warehouse,
	qty} dicts (negative/zero batches already filtered out by core).
	"""
	if not batch_no:
		return []
	try:
		rows = get_batch_qty(
			batch_no=batch_no, item_code=item_code, ignore_reserved_stock=True
		)
	except Exception:
		rows = None
	out = []
	for r in rows or []:
		if r.get("batch_no") == batch_no:
			q = flt(r.get("qty"), 3)
			if q > TOLERANCE:
				out.append((r.get("warehouse"), q))
	out.sort(key=lambda t: t[1], reverse=True)
	return out


def _pick_source_warehouse(item_code, batch_no, requested_qty, candidates):
	"""Return the first candidate warehouse that can physically source the line.

	``candidates`` is an ordered list of warehouse names (highest priority first:
	MOP Log from_warehouse → submitted SE Detail → active SRE warehouse). For
	non-batch items there is nothing for the batch validator to check, so the
	first candidate is returned (legacy behaviour). For batch-tracked items the
	first candidate whose physical batch qty covers ``requested_qty`` is returned;
	None if no candidate qualifies (the caller decides how to handle that).
	"""
	if not candidates:
		return None
	if not batch_no:
		return candidates[0]
	for wh in candidates:
		if (
			flt(_physical_batch_qty(item_code, batch_no, wh) or 0) + TOLERANCE
			>= requested_qty
		):
			return wh
	return None


def _resolve_voucher_fields_from_mwo(mwo):
	"""Return (voucher_type, voucher_no, voucher_detail_no) for a new SRE.

	The project convention (confirmed in stock_entry.py:stock_reservation_entry_for_mwo)
	is to link SREs to the Sales Order via the Parent Manufacturing Order:
	  voucher_type = "Sales Order"
	  voucher_no   = Parent Manufacturing Order → sales_order
	  voucher_detail_no = Parent Manufacturing Order → sales_order_item

	Falls back to None values when the chain cannot be resolved so the caller
	can decide whether to throw or skip.
	"""
	manufacturing_order = frappe.db.get_value(
		"Manufacturing Work Order", mwo, "manufacturing_order"
	)
	if not manufacturing_order:
		return None, None, None

	result = frappe.db.get_value(
		"Parent Manufacturing Order",
		manufacturing_order,
		["sales_order", "sales_order_item"],
		as_dict=True,
	)
	if not result:
		return None, None, None

	return "Sales Order", result.get("sales_order"), result.get("sales_order_item")


def _build_sre_from_context(
	item_code, batch_no, warehouse, qty, mwo, mop_name, dept_ir_doc, orig_sre=None
):
	"""Build and return an unsaved Stock Reservation Entry document.

	orig_sre is a dict from _get_active_sres_for_mwo or a cancelled-SRE lookup.
	When orig_sre is None, voucher fields are resolved from the MWO → PMO → Sales Order
	chain, matching the project convention in stock_reservation_entry_for_mwo.
	"""
	has_batch_no = cint(
		(orig_sre or {}).get("has_batch_no")
		or frappe.get_cached_value("Item", item_code, "has_batch_no")
	)

	# Voucher fields: use orig_sre values when available; otherwise resolve from MWO.
	v_type = (orig_sre or {}).get("voucher_type")
	v_no = (orig_sre or {}).get("voucher_no")
	v_detail = (orig_sre or {}).get("voucher_detail_no")

	if not (v_type and v_no and v_detail):
		_vt, _vno, _vdetail = _resolve_voucher_fields_from_mwo(mwo)
		v_type = v_type or _vt
		v_no = v_no or _vno
		v_detail = v_detail or _vdetail

	if not (v_type and v_no and v_detail):
		frappe.throw(
			_(
				"Unable to create Stock Reservation Entry for item {0}, batch {1}: "
				"voucher_type/voucher_no/voucher_detail_no could not be resolved from "
				"original SRE, MWO {2}, or Parent Manufacturing Order. "
				"Ensure the MWO is linked to a Parent Manufacturing Order with a Sales Order."
			).format(item_code, batch_no, mwo)
		)

	# Mirror stock_reservation_entry_for_mwo exactly:
	#   Primary: voucher_qty = MR custom_total_quantity (with tolerance from Manufacturing Setting)
	#   Fallback: total_so_reserved + qty  (when no linked MR exists)
	manufacturing_order = frappe.get_cached_value(
		"Manufacturing Work Order", mwo, "manufacturing_order"
	)
	base_mr_voucher_qty = None
	if manufacturing_order:
		voucher_qty_row = frappe.db.sql(
			"SELECT sum(custom_total_quantity) FROM `tabMaterial Request` WHERE manufacturing_order=%s AND docstatus!=2",
			(manufacturing_order,),
		)
		if voucher_qty_row and voucher_qty_row[0] and voucher_qty_row[0][0] is not None:
			base_mr_voucher_qty = flt(voucher_qty_row[0][0])
			_manufacturer = frappe.get_cached_value(
				"Parent Manufacturing Order", manufacturing_order, "manufacturer"
			)
			_tol = frappe.db.get_value(
				"Manufacturing Setting",
				_manufacturer,
				"addition_maximum_item__tolerance_percentage",
			)
			if _tol:
				base_mr_voucher_qty += base_mr_voucher_qty * flt(_tol) / 100

	total_so_reserved = flt(
		get_sre_reserved_qty_for_voucher_detail_no(item_code, v_type, v_no, v_detail)
	)
	if base_mr_voucher_qty is not None:
		effective_voucher_qty = flt(base_mr_voucher_qty)
	else:
		effective_voucher_qty = total_so_reserved + flt(qty, 3)

	# available_qty mirrors stock_reservation_entry_for_mwo: max(wh_available, qty)
	_available_at_wh = flt(
		get_available_qty_to_reserve(
			item_code, warehouse, batch_no=batch_no if batch_no else None
		)
	)
	effective_available_qty = max(_available_at_wh, flt(qty, 3))

	new_sre = frappe.new_doc("Stock Reservation Entry")
	new_sre.item_code = item_code
	new_sre.warehouse = warehouse
	new_sre.company = (orig_sre or {}).get("company") or dept_ir_doc.company
	new_sre.stock_uom = (
		(orig_sre or {}).get("stock_uom")
		or frappe.get_cached_value("Item", item_code, "stock_uom")
		or "Nos"
	)
	new_sre.voucher_type = v_type
	new_sre.voucher_no = v_no
	new_sre.voucher_detail_no = v_detail
	new_sre.reserved_qty = flt(qty, 3)
	new_sre.voucher_qty = effective_voucher_qty
	new_sre.available_qty = effective_available_qty
	_safe_set(new_sre, "manufacturing_work_order", mwo)
	_safe_set(new_sre, "manufacturing_operation", mop_name)
	new_sre.has_batch_no = has_batch_no
	new_sre.has_serial_no = 0

	if has_batch_no and batch_no:
		new_sre.reservation_based_on = "Serial and Batch"
		new_sre.append(
			"sb_entries",
			{
				"batch_no": batch_no,
				"warehouse": warehouse,
				"qty": flt(qty, 3),
			},
		)
	else:
		new_sre.reservation_based_on = "Qty"

	return new_sre


def _process_row(dept_ir_doc, row, scenario):
	mwo = row.manufacturing_work_order
	mop_name = row.manufacturing_operation  # the PC MOP (from the DIR row)

	# Bug 6: Duplicate SE guard — if a submitted SE already exists for this
	# Department IR, the sync has already run. Return early to stay idempotent.
	existing_se = frappe.db.get_all(
		"Stock Entry",
		filters={"department_ir": dept_ir_doc.name, "docstatus": 1},
		limit=1,
		pluck="name",
	)
	if existing_se:
		frappe.log_error(
			"process_pc_tagging_stock_sync: SE {0} already exists for Department IR {1}. "
			"Skipping to prevent duplicate.".format(existing_se[0], dept_ir_doc.name),
			"PC Tagging Sync Duplicate Guard",
		)
		return

	dept_ir_logs_raw = _get_dept_ir_mop_logs(dept_ir_doc.name, row.name)
	if not dept_ir_logs_raw:
		return

	# create_mop_log_for_department_ir copies every source MOP Log for the
	# operation, so if a source MOP had multiple entries for the same
	# (item_code, batch_no) at different flow_index snapshots, we get
	# duplicate DIR logs.  qty_after_transaction_batch_based is a balance
	# snapshot, not a delta — keep only the highest flow_index per key so
	# we issue exactly what is currently in the warehouse.
	_dedup: dict = {}
	for _log in dept_ir_logs_raw:
		_key = (_log["item_code"], _log["batch_no"])
		existing = _dedup.get(_key)
		if existing is None or (_log.get("flow_index") or 0) > (
			existing.get("flow_index") or 0
		):
			_dedup[_key] = _log
	dept_ir_logs = list(_dedup.values())

	item_batch_keys = {(log["item_code"], log["batch_no"]) for log in dept_ir_logs}
	active_sres = _get_active_sres_for_mwo(mwo)
	sre_info_by_key = _build_sre_info_by_key(active_sres, item_batch_keys)

	# Bug 4: RECEIVE soft validation — only throw when SRE exists at wrong WH.
	# If no SRE but MOP Log confirms from_warehouse == transit_wh, allow proceeding.
	if scenario == SCENARIO_TAGGING_TO_PC_RECEIVE:
		transit_wh = _resolve_dept_transit_wh(dept_ir_doc.current_department)
		if not transit_wh:
			frappe.throw(
				_(
					"Transit warehouse not found for department {0}. "
					"Set 'Default In Transit Warehouse' on the department's "
					"manufacturing warehouse."
				).format(dept_ir_doc.current_department)
			)
		for key in item_batch_keys:
			sre_hit = sre_info_by_key.get(key)
			if sre_hit:
				sre_wh = sre_hit[1]
				if sre_wh != transit_wh:
					frappe.throw(
						_(
							"Cannot receive {0} batch {1}: the stock reservation is at "
							"{2}, not {3} (transit warehouse for {4}). "
							"Submit the PC-to-Tagging Issue movement first."
						).format(
							key[0],
							key[1],
							sre_wh,
							transit_wh,
							dept_ir_doc.current_department,
						)
					)
			else:
				# No SRE — check MOP Log from_warehouse to confirm stock was at transit_wh
				log_match = next(
					(
						log
						for log in dept_ir_logs
						if log["item_code"] == key[0] and log["batch_no"] == key[1]
					),
					None,
				)
				mop_from_wh = (log_match or {}).get("from_warehouse")
				if not mop_from_wh or mop_from_wh != transit_wh:
					frappe.throw(
						_(
							"No active Stock Reservation Entry found for item {0} batch {1} "
							"on work order {2}, and MOP Log does not confirm the stock was "
							"at transit warehouse {3}. "
							"Submit the PC-to-Tagging Issue first."
						).format(key[0], key[1], mwo, transit_wh)
					)

	# Build transfer lines from MOP Logs.
	# Bug 1: s_warehouse priority — MOP Log from_warehouse → SE Detail → SRE → error.
	transfer_lines = []
	for log in dept_ir_logs:
		item_code = log["item_code"]
		batch_no = log["batch_no"]
		qty = flt(log.get("qty_after_transaction_batch_based") or 0)
		if qty <= TOLERANCE:
			continue

		# Resolve the source warehouse, preferring a candidate that PHYSICALLY
		# holds the batch. The MOP Log from_warehouse is the *logical* position;
		# for findings/diamonds it can diverge from where the stock is physically
		# reserved (the department flow is logical-only, so physical WIP stays
		# parked in the reserved warehouse). Candidate priority:
		#   MOP Log from_warehouse → submitted SE Detail → active SRE warehouse.
		sre_hit = sre_info_by_key.get((item_code, batch_no))

		candidates = []
		if log.get("from_warehouse"):
			candidates.append(log["from_warehouse"])
		se_source_wh = _find_stock_entry_source_warehouse(
			dept_ir_doc.name, row.name, item_code, batch_no, mwo
		)
		if se_source_wh:
			candidates.append(se_source_wh)
		if sre_hit:
			candidates.append(sre_hit[1])

		s_warehouse = _pick_source_warehouse(
			item_code, batch_no, flt(qty, 3), candidates
		)

		if not s_warehouse:
			if not candidates:
				frappe.log_error(
					"Unable to resolve source warehouse for item {0}, batch {1}, "
					"Department IR {2}, row {3}. "
					"Checked MOP Log from_warehouse, submitted Stock Entry Detail, "
					"and active Stock Reservation Entry. Skipping line.".format(
						item_code, batch_no, dept_ir_doc.name, row.name
					),
					"PC Tagging Sync Source Warehouse Missing",
				)
				continue

			# Batch-tracked line with candidate warehouses but no physical stock:
			# the MOP Log logical balance has diverged from physical SBB stock.
			# Fail fast with an actionable diagnostic instead of letting the SE
			# submit blow up with an opaque BatchNegativeStockError.
			cand_desc = "; ".join(
				"{0} (physical {1})".format(
					wh, flt(_physical_batch_qty(item_code, batch_no, wh) or 0, 3)
				)
				for wh in candidates
			)
			where_stock = _warehouses_with_physical_batch(item_code, batch_no)
			where_desc = (
				"; ".join("{0} ({1})".format(wh, q) for wh, q in where_stock)
				or "no warehouse"
			)
			frappe.throw(
				_(
					"PC→Tagging transfer (Department IR {0}): cannot source {1} of "
					"item {2}, batch {3}. None of the candidate warehouses holds "
					"enough physical stock [{4}]. The batch physically has stock in: "
					"{5}. Reconcile the MOP Log / Stock Reservation before submitting."
				).format(
					dept_ir_doc.name,
					flt(qty, 3),
					item_code,
					batch_no,
					cand_desc,
					where_desc,
				),
				title=_("Insufficient physical batch stock for PC→Tagging transfer"),
			)

		t_warehouse = log["to_warehouse"]
		pcs = (
			cint(log.get("pcs_after_transaction_batch_based") or 0)
			if _requires_pcs(item_code)
			else 0
		)
		uom = frappe.get_cached_value("Item", item_code, "stock_uom") or "Nos"

		# MOP Log manufacturing_operation is the NEW Tagging MOP (not the PC MOP in row).
		log_mop_name = log.get("manufacturing_operation") or mop_name

		transfer_lines.append(
			{
				"item_code": item_code,
				"batch_no": batch_no,
				"qty": flt(qty, 3),
				"pcs": pcs,
				"s_warehouse": s_warehouse,
				"t_warehouse": t_warehouse,
				"uom": uom,
				"sre_name": sre_hit[0]
				if sre_hit
				else None,  # may be None — that is fine
				"mop_name": log_mop_name,
				"mwo": mwo,
			}
		)

	if not transfer_lines:
		return

	# Canonical lock order (F-002 fix): this flow previously took NO lock-ordering at
	# all -- it cancelled SREs and updated Bins before its transfer SE took the naming
	# series, an inverted order that races conformant SE submits into 1213 deadlock
	# cycles. Pin the Stock Entry naming-series row (position 2) and then the transfer
	# Bins (position 3) up front, before the SRE cancels below, so the whole flow is
	# Series -> Bin -> SRE like the canonical order. Additive: preallocate_series is
	# SELECT ... FOR UPDATE only (no increment), re-entrant with the real naming at the
	# se.save()/submit() below; lock_bins takes only Bin locks the transfer would take
	# anyway, just earlier and sorted. Placed after the no-op guard so an empty run
	# never locks anything.
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_bins,
		preallocate_series_for_docs,
	)

	_series_stub = frappe.new_doc("Stock Entry")
	_series_stub.company = dept_ir_doc.company
	_series_stub.stock_entry_type = "Material Transfer to Department"
	preallocate_series_for_docs(_series_stub)
	lock_bins(
		[(l["item_code"], l["s_warehouse"]) for l in transfer_lines]
		+ [(l["item_code"], l["t_warehouse"]) for l in transfer_lines]
	)

	# Step 1: Cancel old SREs (only when an SRE was found for the line)
	cancelled_sre_names = set()
	for line in transfer_lines:
		sre_name = line["sre_name"]
		if sre_name and sre_name not in cancelled_sre_names:
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
	_safe_set(se, "manufacturing_work_order", mwo)

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
			item_row["pcs"] = line["pcs"]  # Bug 3: field is "pcs" not "custom_pcs"
		se.append("items", item_row)

	se.save()
	se.submit()

	# Step 3: Create replacement SREs at new warehouse.
	# Bug 2 + Bug 5: use (item_code, batch_no, t_warehouse, mwo, mop_name) as key
	# so grouping never depends on orig_sre_name, and build from context when orig is None.
	original_sres_by_name = {s["name"]: s for s in active_sres}

	new_sre_groups = {}
	for line in transfer_lines:
		key = (
			line["item_code"],
			line["batch_no"],
			line["t_warehouse"],
			line["mwo"],
			line["mop_name"],
		)
		if key not in new_sre_groups:
			new_sre_groups[key] = {
				"qty": 0.0,
				"pcs": 0,
				"sre_name": line["sre_name"],
			}
		new_sre_groups[key]["qty"] += line["qty"]
		new_sre_groups[key]["pcs"] += line["pcs"]

	for (
		item_code,
		batch_no,
		new_wh,
		mwo_key,
		mop_key,
	), totals in new_sre_groups.items():
		if totals["qty"] <= TOLERANCE:
			continue
		orig = original_sres_by_name.get(
			totals["sre_name"]
		)  # may be None — handled below

		# Bug 2: _build_sre_from_context handles orig=None gracefully,
		# so SRE creation is never skipped merely because no original SRE exists.
		new_sre = _build_sre_from_context(
			item_code=item_code,
			batch_no=batch_no,
			warehouse=new_wh,
			qty=totals["qty"],
			mwo=mwo_key,
			mop_name=mop_key,
			dept_ir_doc=dept_ir_doc,
			orig_sre=orig,
		)
		new_sre.insert(ignore_links=1)
		new_sre.submit()


def _handle_cancel_row(dept_ir_doc, row, scenario):
	"""On Department IR cancel: cancel replacement SREs and restore originals.

	SE ownership by scenario:
	- ISSUE cancel: department_ir.py lines 407-414 already cancel all SEs with
	  department_ir=self.name before this function runs. Do NOT cancel again.
	- RECEIVE cancel: no SE cancel exists elsewhere. This function cancels them.
	"""
	mwo = row.manufacturing_work_order

	# Step A: For RECEIVE cancel, cancel SEs (ISSUE cancel already done by department_ir.py)
	if scenario == SCENARIO_TAGGING_TO_PC_RECEIVE:
		se_list = frappe.db.get_all(
			"Stock Entry",
			filters={"department_ir": dept_ir_doc.name, "docstatus": 1},
			pluck="name",
		)
		for se_name in se_list:
			se_doc = frappe.get_doc("Stock Entry", se_name)
			if cint(se_doc.docstatus) == 1:
				se_doc.cancel()

	# Step B: Determine target_wh — the warehouse where replacement SREs were created
	if scenario == SCENARIO_PC_TO_TAGGING_ISSUE:
		target_wh = _resolve_dept_transit_wh(dept_ir_doc.next_department)
	else:
		target_wh = _resolve_dept_manufacturing_wh(dept_ir_doc.current_department)

	if not target_wh:
		return

	# Step C: Cancel replacement SREs at target_wh for this MWO
	replacement_sres = frappe.db.get_all(
		"Stock Reservation Entry",
		filters={
			"manufacturing_work_order": mwo,
			"warehouse": target_wh,
			"docstatus": 1,
			"status": ["not in", ["Cancelled", "Delivered"]],
		},
		fields=["name"],
	)
	for sre_row in replacement_sres:
		sre_doc = frappe.get_doc("Stock Reservation Entry", sre_row["name"])
		if cint(sre_doc.docstatus) == 1:
			sre_doc.cancel()

	# Step D: Determine source_wh — the warehouse to restore SREs to
	if scenario == SCENARIO_PC_TO_TAGGING_ISSUE:
		# Issue cancel: restore SREs back to PC WIP (current_department = PC at time of issue)
		source_wh = _resolve_dept_manufacturing_wh(dept_ir_doc.current_department)
	else:
		# Receive cancel: restore SREs back to Tagging Transit (current_department = Tagging)
		source_wh = _resolve_dept_transit_wh(dept_ir_doc.current_department)

	if not source_wh:
		return

	# Step E: Fetch MOP Logs for this DIR row using the cancel-path variant that
	# includes already-cancelled logs (regular _get_dept_ir_mop_logs filters them out).
	dept_ir_logs = _get_dept_ir_mop_logs_any(dept_ir_doc.name, row.name)
	if not dept_ir_logs:
		return

	# Group by (item_code, batch_no) to sum qty and capture the MOP name from the log
	restore_groups = {}
	for log in dept_ir_logs:
		item_code = log["item_code"]
		batch_no = log["batch_no"]
		qty = flt(log.get("qty_after_transaction_batch_based") or 0)
		if qty <= TOLERANCE:
			continue
		key = (item_code, batch_no)
		if key not in restore_groups:
			restore_groups[key] = {
				"qty": 0.0,
				"mop_name": log.get("manufacturing_operation"),
			}
		restore_groups[key]["qty"] += qty

	if not restore_groups:
		return

	# Step F: Find cancelled SRE for voucher context (most recently modified)
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
			order_by="modified desc",
		)
		or {}
	)

	# Step G: Restore SREs at source_wh using _build_sre_from_context
	for (item_code, batch_no), group_data in restore_groups.items():
		qty = group_data["qty"]
		mop_in_log = group_data["mop_name"]
		restore_mop = row.manufacturing_operation or mop_in_log

		orig_sre_dict = None
		if original_sre_info:
			orig_sre_dict = {
				"voucher_type": original_sre_info.get("voucher_type"),
				"voucher_no": original_sre_info.get("voucher_no"),
				"voucher_detail_no": original_sre_info.get("voucher_detail_no"),
				"company": original_sre_info.get("company"),
				"stock_uom": original_sre_info.get("stock_uom"),
				"has_batch_no": original_sre_info.get("has_batch_no"),
				"manufacturing_work_order": mwo,
				"manufacturing_operation": (
					original_sre_info.get("manufacturing_operation") or restore_mop
				),
			}

		new_sre = _build_sre_from_context(
			item_code=item_code,
			batch_no=batch_no,
			warehouse=source_wh,
			qty=qty,
			mwo=mwo,
			mop_name=restore_mop,
			dept_ir_doc=dept_ir_doc,
			orig_sre=orig_sre_dict,
		)
		new_sre.insert(ignore_links=1)
		new_sre.submit()
