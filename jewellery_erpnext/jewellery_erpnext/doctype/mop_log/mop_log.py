# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt

FIELD_MAP = {"M": "net", "F": "finding", "D": "diamond", "G": "gemstone", "O": "other"}
select_fields = [
	"item_code",
	"pcs_after_transaction",
	"pcs_after_transaction_item_based",
	"pcs_after_transaction_batch_based",
	"qty_after_transaction",
	"qty_after_transaction_item_based",
	"qty_after_transaction_batch_based",
	"serial_and_batch_bundle",
	"batch_no",
	"flow_index",
	"voucher_type",
	"voucher_no",
]
current_balance_fields = select_fields + [
	"name",
	"creation",
	"from_warehouse",
	"to_warehouse",
	"row_name",
	"manufacturing_work_order",
	"manufacturing_operation",
]


class MOPLog(Document):
	def validate(self):
		first_char = self.item_code[0] if self.item_code else None
		qty_after_prefix = self.qty_after_transaction
		pcs_after_prefix = self.pcs_after_transaction
		prefix = FIELD_MAP.get(first_char)
		if prefix:
			update_value = {f"{prefix}_wt": qty_after_prefix}
			if first_char in ("D", "G"):
				update_value.update(
					{
						f"{prefix}_wt_in_gram": qty_after_prefix * 0.2,
						f"{prefix}_pcs": pcs_after_prefix,
					}
				)
			frappe.db.set_value(
				"Manufacturing Operation",
				self.manufacturing_operation,
				update_value,
			)
			update_wt_detail(self.manufacturing_operation)


def update_wt_detail(manufacturing_operation):
	(
		net_wt,
		finding_wt,
		diamond_wt_in_gram,
		gemstone_wt_in_gram,
		other_wt,
		previous_mop,
		loss_wt,
	) = frappe.db.get_value(
		"Manufacturing Operation",
		manufacturing_operation,
		[
			"net_wt",
			"finding_wt",
			"diamond_wt_in_gram",
			"gemstone_wt_in_gram",
			"other_wt",
			"previous_mop",
			"loss_wt",
		],
	)
	prev_gross_wt = 0
	if previous_mop:
		prev_gross_wt = (
			frappe.db.get_value("Manufacturing Operation", previous_mop, "gross_wt")
			or 0
		)
	gross_wt = (
		flt(net_wt)
		+ flt(finding_wt)
		+ flt(diamond_wt_in_gram)
		+ flt(gemstone_wt_in_gram)
		+ flt(other_wt)
	)
	# if loss_wt:
	# 	if loss_wt > 0:
	# 		gross_wt += flt(loss_wt)
	# 	elif loss_wt < 0:
	# 		gross_wt -= abs(flt(loss_wt))

	frappe.db.set_value(
		"Manufacturing Operation",
		manufacturing_operation,
		{
			"gross_wt": gross_wt,
			"prev_gross_wt": prev_gross_wt,
		},
	)


def create_mop_log_for_stock_transfer_to_mo(doc, row, is_synced=False):
	item_code = row.get("item_code") or ""
	if not item_code:
		# nothing to log
		return

	first_char = item_code[0]
	# safe numeric conversions (pcs might be None)
	if doc.stock_entry_type == "Material Receive (WORK ORDER)":
		pcs = -cint(row.get("pcs") or 0)
		qty = -flt((row.get("qty") or 0.0), 3)
	else:
		pcs = cint(row.get("pcs") or 0)
		qty = flt((row.get("qty") or 0.0), 3)
	batch_no = row.get("batch_no")
	mwo = doc.get("manufacturing_work_order")
	mop_op = row.get("manufacturing_operation")

	# prepare prefix pattern e.g. 'D%' or 'G%'
	prefix_like = f"{first_char}%"
	sql = """
	SELECT
		COALESCE(SUM(CASE WHEN item_code LIKE %s THEN pcs_change END), 0) AS sum_pcs_prefix,
		COALESCE(SUM(CASE WHEN item_code = %s THEN pcs_change END), 0) AS sum_pcs_item,
		COALESCE(SUM(CASE WHEN item_code = %s AND batch_no = %s THEN pcs_change END), 0) AS sum_pcs_batch,
		COALESCE(SUM(CASE WHEN item_code LIKE %s THEN qty_change END), 0) AS sum_qty_prefix,
		COALESCE(SUM(CASE WHEN item_code = %s THEN qty_change END), 0) AS sum_qty_item,
		COALESCE(SUM(CASE WHEN item_code = %s AND batch_no = %s THEN qty_change END), 0) AS sum_qty_batch
	FROM `tabMOP Log`
	WHERE manufacturing_work_order = %s
	  AND is_cancelled = 0
	"""
	sql_params = [
		prefix_like,
		item_code,
		item_code,
		batch_no,
		prefix_like,
		item_code,
		item_code,
		batch_no,
		mwo,
	]

	previous_mop_qty = 0
	previous_mop_pcs = 0

	if mop_op:
		sql += " AND manufacturing_operation = %s"
		sql_params.append(mop_op)
		previous_mop = frappe.db.get_value(
			"Manufacturing Operation", mop_op, "previous_mop"
		)
		if previous_mop:
			previous_mop_qty = (
				frappe.db.get_value(
					"Manufacturing Operation",
					previous_mop,
					FIELD_MAP.get(first_char) + "_wt",
				)
				or 0
			)
			if first_char in ("D", "G"):
				previous_mop_pcs = (
					frappe.db.get_value(
						"Manufacturing Operation",
						previous_mop,
						FIELD_MAP.get(first_char) + "_pcs",
					)
					or 0
				)

	row_vals = frappe.db.sql(sql, tuple(sql_params), as_dict=True)

	stats = (
		row_vals[0]
		if row_vals
		else {
			"sum_pcs_prefix": 0,
			"sum_pcs_item": 0,
			"sum_pcs_batch": 0,
			"sum_qty_prefix": 0.0,
			"sum_qty_item": 0.0,
			"sum_qty_batch": 0.0,
			"sum_qty_mop_total": 0.0,
		}
	)
	last_mop_index = get_last_mop_index(row.manufacturing_operation)
	# compute fields
	pcs_after_prefix = pcs + cint(stats["sum_pcs_prefix"]) + previous_mop_pcs
	pcs_after_item = pcs + cint(stats["sum_pcs_item"]) + previous_mop_pcs
	pcs_after_batch = pcs + cint(stats["sum_pcs_batch"])

	qty_after_prefix = qty + flt(stats["sum_qty_prefix"]) + previous_mop_qty
	qty_after_item = qty + flt(stats["sum_qty_item"]) + previous_mop_qty
	qty_after_batch = qty + flt(stats["sum_qty_batch"])
	# create doc
	mop_log = frappe.new_doc("MOP Log")
	mop_log.item_code = item_code
	mop_log.pcs_change = pcs
	mop_log.pcs_after_transaction = pcs_after_prefix
	mop_log.pcs_after_transaction_item_based = pcs_after_item
	mop_log.pcs_after_transaction_batch_based = pcs_after_batch

	mop_log.from_warehouse = row.get("s_warehouse")
	mop_log.to_warehouse = row.get("t_warehouse")
	mop_log.voucher_type = "Stock Entry"
	mop_log.voucher_no = doc.name
	mop_log.manufacturing_work_order = mwo
	mop_log.manufacturing_operation = row.get("manufacturing_operation")
	mop_log.row_name = row.name
	mop_log.qty_change = qty
	mop_log.qty_after_transaction = qty_after_prefix
	mop_log.qty_after_transaction_item_based = qty_after_item
	mop_log.qty_after_transaction_batch_based = qty_after_batch

	mop_log.is_synced = is_synced
	mop_log.serial_and_batch_bundle = row.get("serial_and_batch_bundle")
	mop_log.batch_no = batch_no
	mop_log.flow_index = last_mop_index + 1 if last_mop_index == 0 else 0
	mop_log.save()


def get_last_mop_index(manufacturing_operation, voucher_type=None, voucher_no=None):
	filters = {"manufacturing_operation": manufacturing_operation, "is_cancelled": 0}
	if voucher_type:
		filters["voucher_type"] = voucher_type
	if voucher_no:
		filters["voucher_no"] = voucher_no

	last_log = frappe.db.get_value(
		"MOP Log",
		filters,
		"max(flow_index) as flow_index",
	)
	return last_log


def get_current_mop_balance_rows(manufacturing_operation, include_fields=None):
	"""Return the latest non-cancelled MOP Log row per item/batch for a MOP.

	Loss-attribution rows (log_category="Loss Attribution") ARE included —
	they post a real qty_change reduction so the balance after loss must be
	reflected to downstream readers (e.g. Make Receive Entry availability,
	manual loss validation, EOD SRE reconciliation).
	"""
	fields = list(
		dict.fromkeys((include_fields or current_balance_fields) + ["name", "creation"])
	)
	mop_logs = frappe.db.get_all(
		"MOP Log",
		filters={
			"manufacturing_operation": manufacturing_operation,
			"is_cancelled": 0,
		},
		fields=fields,
		order_by="creation desc",
	)
	if not mop_logs:
		return []

	latest_by_key = {}
	for log in mop_logs:
		key = (log.get("item_code"), log.get("batch_no"))
		if key not in latest_by_key:
			latest_by_key[key] = log

	return list(reversed(list(latest_by_key.values())))


def get_available_qty_pcs_for_mop_item(
	manufacturing_operation,
	item_code,
	batch_no=None,
	warehouse=None,
	stock_reservation_entry=None,
	stock_reservation_entry_detail=None,
	manufacturing_work_order=None,
	sre_remaining_qty=None,
	already_received_qty=0,
	already_received_pcs=0,
	mop_log_balance_map=None,
):
	"""Reconcile Qty/PCS for a single MOP item/batch row.

	Cross-checks Stock Reservation Entry, MOP Log, and Stock Entry to produce
	a single dict the Make Receive Entry popup, the server validator
	(``create_mr_wo_stock_entry``) and Employee IR manual-loss validation
	can all consume.

	is_pcs_item is gated by FIELD_MAP membership AND item_code[0] in (D, G).

	available_qty = min(positive authoritative values among SRE remaining qty
	and MOP Log batch-based qty). Missing values are treated as "no signal"
	(spec: ``If one source does not store PCS, do not treat missing PCS as
	zero. Treat it as unknown.`` — same rule applied to qty).

	available_pcs:
	  * 0 for non-D/G items.
	  * For D/G, MOP Log batch-based PCS is the authoritative source. SRE has
	    no PCS field, so it is excluded from the candidate set (treating it
	    as 0 would force Available PCS to 0 incorrectly per spec).
	  * 0 falls through when no MOP Log row exists for (item, batch).
	"""
	is_pcs_item = bool(item_code) and item_code[0] in ("D", "G")

	if mop_log_balance_map is None:
		rows = get_current_mop_balance_rows(manufacturing_operation)
		mop_log_balance_map = {
			(row.get("item_code"), row.get("batch_no")): row for row in rows
		}

	mop_row = mop_log_balance_map.get((item_code, batch_no))
	mop_qty_raw = mop_row.get("qty_after_transaction_batch_based") if mop_row else None
	mop_pcs_raw = mop_row.get("pcs_after_transaction_batch_based") if mop_row else None
	mop_log_reference = mop_row.get("name") if mop_row else None

	qty_candidates = []
	if sre_remaining_qty is not None:
		qty_candidates.append(flt(sre_remaining_qty))
	if mop_qty_raw is not None:
		qty_candidates.append(flt(mop_qty_raw))
	# Clamp negative balances to 0; downstream callers/UI round for display.
	available_qty = max(0.0, min(qty_candidates)) if qty_candidates else 0.0

	if not is_pcs_item:
		available_pcs = 0
	else:
		pcs_candidates = []
		if mop_pcs_raw is not None:
			pcs_candidates.append(cint(mop_pcs_raw))
		# SRE never stores PCS, so it does NOT enter the candidate set.
		available_pcs = max(0, min(pcs_candidates)) if pcs_candidates else 0

	return {
		"item_code": item_code,
		"batch_no": batch_no,
		"source_warehouse": warehouse,
		"stock_reservation_entry": stock_reservation_entry,
		"stock_reservation_entry_detail": stock_reservation_entry_detail,
		"manufacturing_work_order": manufacturing_work_order,
		"reserved_qty": flt(sre_remaining_qty or 0),
		"reserved_pcs": None,
		"mop_log_balance_qty": flt(mop_qty_raw or 0),
		"mop_log_balance_pcs": cint(mop_pcs_raw or 0),
		"stock_entry_transferred_qty": 0,
		"stock_entry_transferred_pcs": 0,
		"already_received_qty": flt(already_received_qty or 0),
		"already_received_pcs": cint(already_received_pcs or 0),
		"available_qty": available_qty,
		"available_pcs": available_pcs,
		"is_pcs_item": is_pcs_item,
		"mop_log_reference": mop_log_reference,
	}


def create_mop_log_for_department_ir(
	self, row, to_warehouse, from_warehouse, operation
):
	mop_logs = []
	is_receive = getattr(self, "type", None) == "Receive" and getattr(
		self, "receive_against", None
	)

	if is_receive:
		mop_logs = frappe.db.get_all(
			"MOP Log",
			filters={
				"manufacturing_operation": row.manufacturing_operation,
				"is_cancelled": 0,
				"voucher_type": "Department IR",
				"voucher_no": self.receive_against,
			},
			fields=select_fields,
			order_by="creation asc",
		)

	else:
		filters = {
			"manufacturing_operation": row.manufacturing_operation,
			"is_cancelled": 0,
		}

		mop_logs = frappe.db.get_all(
			"MOP Log",
			filters,
			select_fields,
			order_by="creation asc",
		)

	for log in mop_logs:
		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = log.item_code
		mop_log.pcs_after_transaction = log.pcs_after_transaction
		mop_log.pcs_after_transaction_item_based = log.pcs_after_transaction_item_based
		mop_log.pcs_after_transaction_batch_based = (
			log.pcs_after_transaction_batch_based
		)
		mop_log.from_warehouse = from_warehouse
		mop_log.to_warehouse = to_warehouse
		mop_log.voucher_type = "Department IR"
		mop_log.voucher_no = self.name
		mop_log.row_name = row.name
		mop_log.qty_after_transaction = log.qty_after_transaction
		mop_log.qty_after_transaction_item_based = log.qty_after_transaction_item_based
		mop_log.qty_after_transaction_batch_based = (
			log.qty_after_transaction_batch_based
		)
		mop_log.is_synced = 0
		mop_log.manufacturing_operation = operation
		mop_log.manufacturing_work_order = row.manufacturing_work_order
		mop_log.serial_and_batch_bundle = log.serial_and_batch_bundle
		mop_log.batch_no = log.batch_no
		mop_log.flow_index = log.flow_index + 1
		mop_log.save()


def _get_mop_logs_for_employee_ir_issue(row, department_receive_id):
	"""Source rows for Employee IR Issue MOP Log cloning.

	Uses the canonical current-balance snapshot so bagging/material-request additions
	already written into MOP Log are issued alongside department-transferred metal.
	"""
	return get_current_mop_balance_rows(
		row.manufacturing_operation,
		include_fields=select_fields,
	)


def creste_mop_log_for_employee_ir(self, row, from_warehouse, to_warehouse):
	department_receive_id = frappe.db.get_value(
		"Manufacturing Operation", row.manufacturing_operation, "department_receive_id"
	)
	mop_logs = _get_mop_logs_for_employee_ir_issue(row, department_receive_id)
	for log in mop_logs:
		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = log.item_code
		mop_log.pcs_after_transaction = log.pcs_after_transaction
		mop_log.pcs_after_transaction_item_based = log.pcs_after_transaction_item_based
		mop_log.pcs_after_transaction_batch_based = (
			log.pcs_after_transaction_batch_based
		)
		mop_log.from_warehouse = from_warehouse
		mop_log.to_warehouse = to_warehouse
		mop_log.voucher_type = self.doctype
		mop_log.voucher_no = self.name
		mop_log.row_name = row.name
		mop_log.qty_after_transaction = log.qty_after_transaction
		mop_log.qty_after_transaction_item_based = log.qty_after_transaction_item_based
		mop_log.qty_after_transaction_batch_based = (
			log.qty_after_transaction_batch_based
		)
		mop_log.is_synced = 0
		mop_log.manufacturing_operation = row.manufacturing_operation
		mop_log.manufacturing_work_order = row.manufacturing_work_order
		mop_log.serial_and_batch_bundle = log.serial_and_batch_bundle
		mop_log.batch_no = log.batch_no
		mop_log.flow_index = log.flow_index + 1
		mop_log.save()


def resolve_employee_ir_issue_voucher_for_receive(doc, row):
	"""Employee IR Issue name whose MOP Logs this Receive must clone (voucher_no on Issue logs).

	Uses ``emp_ir_id`` when it points to a submitted Issue that includes this MOP;
	otherwise the latest submitted Employee IR Issue containing ``row.manufacturing_operation``.
	"""
	emp_ir_id = cstr(getattr(doc, "emp_ir_id", None) or "").strip()
	if emp_ir_id:
		meta = frappe.db.get_value(
			"Employee IR",
			emp_ir_id,
			["docstatus", "type"],
			as_dict=True,
		)
		if (
			meta
			and meta.type == "Issue"
			and cint(meta.docstatus) == 1
			and frappe.db.exists(
				"Employee IR Operation",
				{
					"parent": emp_ir_id,
					"manufacturing_operation": row.manufacturing_operation,
				},
			)
		):
			return emp_ir_id

	rows = frappe.db.sql(
		"""
		SELECT eir.name
		FROM `tabEmployee IR` eir
		INNER JOIN `tabEmployee IR Operation` op ON op.parent = eir.name
		WHERE eir.docstatus = 1
		  AND eir.type = 'Issue'
		  AND op.manufacturing_operation = %s
		ORDER BY eir.modified DESC, eir.name DESC
		LIMIT 1
		""",
		row.manufacturing_operation,
	)
	return rows[0][0] if rows else None


def get_employee_ir_loss_map(eir_doc):
	"""Build the (mop, mwo, item_code, batch_no) → loss bucket map.

	The bucket records loss in the *MOP Log UOM* (carats for D/G, grams for
	M/F/O) — there is NO carat→gram conversion at this layer. Conversion to
	grams happens once, downstream, in MOPLog.validate when it writes
	``diamond_wt_in_gram`` / ``gemstone_wt_in_gram`` as ``qty_after_transaction
	* 0.2``.

	Both ``employee_loss_details`` (auto, M/F-only) and
	``manually_book_loss_details`` (any prefix) feed the map. The bucket
	carries enough audit data to populate ``loss_weight`` (grams, for
	display), ``loss_source_row``, and ``loss_type`` on the combined receive
	MOP Log row downstream.
	"""
	loss_map = {}

	def _add(row, loss_type):
		if not (row.manufacturing_operation and row.item_code):
			return
		key = (
			row.manufacturing_operation,
			row.manufacturing_work_order,
			row.item_code,
			row.batch_no,
		)
		bucket = loss_map.setdefault(
			key,
			{
				"loss_qty": 0.0,
				"loss_pcs": 0,
				"loss_types": set(),
				"source_rows": [],
				"loss_weight_grams": 0.0,
			},
		)
		qty = flt(row.proportionally_loss)
		bucket["loss_qty"] += qty
		# loss_weight_grams is for AUDIT only; convert D/G carat→gram here.
		first_char = row.item_code[0] if row.item_code else ""
		if first_char in ("D", "G"):
			bucket["loss_weight_grams"] += qty * 0.2
			bucket["loss_pcs"] += cint(getattr(row, "pcs", 0) or 0)
		else:
			bucket["loss_weight_grams"] += qty
		bucket["loss_types"].add(loss_type)
		if row.name:
			bucket["source_rows"].append(row.name)

	for r in eir_doc.get("employee_loss_details") or []:
		_add(r, "Auto Employee Loss")
	for r in eir_doc.get("manually_book_loss_details") or []:
		_add(r, "Manually Booked Loss")

	# Sets aren't JSON-stable; flatten to a sorted list.
	for v in loss_map.values():
		v["loss_types"] = sorted(v["loss_types"])
	return loss_map


def create_mop_log_for_employee_ir_receive(
	doc, row, from_warehouse, to_warehouse, stock_entry_name=[]
):
	"""Create combined Receive + Loss MOP Log entries for Employee IR Receive.

	Reads the MOP Logs created during the matching Employee IR **Issue** only
	(``voucher_no`` = Issue name), not every historical Employee IR log on
	the MOP.

	**Combined design (replaces the prior two-pass receive + loss writer).**
	For each issue source row, the loss attributable to the same
	(item_code, batch_no) is subtracted directly from this MOP Log's qty/pcs
	balance fields. The result: one MOP Log row per receive flow (no
	separate Loss Attribution rows), so MOPLog.validate updates Manufacturing
	Operation buckets exactly once.

	UOM rule: ``qty_change`` and ``qty_after_transaction*`` stay in the
	item's stock UOM (carats for D/G, grams for M/F/O). MOPLog.validate
	applies the carat→gram conversion when it writes
	``diamond_wt_in_gram`` / ``gemstone_wt_in_gram``. We do **not** convert
	carat→gram before subtracting loss.
	"""
	issue_voucher = resolve_employee_ir_issue_voucher_for_receive(doc, row)
	mop_logs = []
	mop_logs = (
		frappe.db.get_all(
			"MOP Log",
			{
				"manufacturing_operation": row.manufacturing_operation,
				"is_cancelled": 0,
				"voucher_type": "Employee IR",
				"voucher_no": issue_voucher,
			},
			select_fields,
			order_by="creation asc",
		)
		or []
	)
	if stock_entry_name:
		mop_logs += (
			frappe.db.get_all(
				"MOP Log",
				{
					"manufacturing_operation": row.manufacturing_operation,
					"is_cancelled": 0,
					"voucher_type": "Stock Entry",
					"voucher_no": ["in", stock_entry_name],
				},
				select_fields,
				order_by="creation asc",
			)
			or []
		)

	mop_logs += (
		frappe.db.get_all(
			"MOP Log",
			{
				"manufacturing_operation": row.manufacturing_operation,
				"is_cancelled": 0,
				"voucher_type": "Stock Entry",
				"voucher_no": [
					"in",
					frappe.db.get_all(
						"Stock Entry",
						filters={
							"employee_ir": ["is", "not set"],
							"manufacturing_operation": row.manufacturing_operation,
							"docstatus": 1,
							"to_employee": ["is", "set"],
						},
						pluck="name",
					),
				],
			},
			select_fields,
			order_by="creation asc",
		)
		or []
	)

	# Build the EIR-wide loss map once, then narrow to entries that match
	# THIS receive row's MOP+MWO. Prior loss buckets get consumed exactly
	# once across the source-log loop; if multiple source logs match the
	# same (item, batch), only the first one absorbs the loss to prevent
	# double-subtraction.
	full_loss_map = get_employee_ir_loss_map(doc)
	consumed_loss_keys = set()

	for log in mop_logs:
		loss_key = (
			row.manufacturing_operation,
			row.manufacturing_work_order,
			log.item_code,
			log.batch_no,
		)
		loss = (
			full_loss_map.get(loss_key) if loss_key not in consumed_loss_keys else None
		)

		loss_qty = flt(loss.get("loss_qty"), 3) if loss else 0.0
		loss_pcs = cint(loss.get("loss_pcs") or 0) if loss else 0

		# Subtract loss in the item's stock UOM (no conversion).
		adj_qty_after = flt(flt(log.qty_after_transaction) - loss_qty, 3)
		adj_qty_after_item = flt(
			flt(log.qty_after_transaction_item_based) - loss_qty, 3
		)
		adj_qty_after_batch = flt(
			flt(log.qty_after_transaction_batch_based) - loss_qty, 3
		)

		first_char = (log.item_code or "")[0] if log.item_code else ""
		# PCS only meaningful for D/G; loss_pcs is already 0 for non-D/G
		# (the loss-map builder gates on prefix). Keep the explicit guard
		# here too so a future loss-map shape change can't leak PCS into
		# M/F/O rows.
		if first_char in ("D", "G"):
			adj_pcs_after = cint(log.pcs_after_transaction) - loss_pcs
			adj_pcs_after_item = cint(log.pcs_after_transaction_item_based) - loss_pcs
			adj_pcs_after_batch = cint(log.pcs_after_transaction_batch_based) - loss_pcs
			pcs_change = -loss_pcs
		else:
			adj_pcs_after = cint(log.pcs_after_transaction)
			adj_pcs_after_item = cint(log.pcs_after_transaction_item_based)
			adj_pcs_after_batch = cint(log.pcs_after_transaction_batch_based)
			pcs_change = 0

		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = log.item_code
		# qty_change reflects ONLY the loss adjustment for this receive flow.
		# The receive itself clones the issue balance forward (zero net
		# delta); loss is the only thing that moves the balance, so
		# qty_change = -loss_qty captures the net movement of this row.
		mop_log.qty_change = -loss_qty
		mop_log.pcs_change = pcs_change

		mop_log.qty_after_transaction = adj_qty_after
		mop_log.qty_after_transaction_item_based = adj_qty_after_item
		mop_log.qty_after_transaction_batch_based = adj_qty_after_batch

		mop_log.pcs_after_transaction = adj_pcs_after
		mop_log.pcs_after_transaction_item_based = adj_pcs_after_item
		mop_log.pcs_after_transaction_batch_based = adj_pcs_after_batch

		mop_log.from_warehouse = from_warehouse
		mop_log.to_warehouse = to_warehouse
		mop_log.voucher_type = "Employee IR"
		mop_log.voucher_no = doc.name
		mop_log.row_name = row.name
		mop_log.is_synced = 0
		mop_log.manufacturing_operation = row.manufacturing_operation
		mop_log.manufacturing_work_order = row.manufacturing_work_order
		mop_log.serial_and_batch_bundle = log.serial_and_batch_bundle
		mop_log.batch_no = log.batch_no
		mop_log.flow_index = log.flow_index + 1

		# Carry loss audit metadata on the combined row (joined when more
		# than one loss-detail row contributes). We deliberately do NOT set
		# log_category="Loss Attribution" — that label is reserved for
		# pre-existing legacy audit rows and would cause downstream
		# consumers to mis-classify this real movement row.
		if loss:
			consumed_loss_keys.add(loss_key)
			mop_log.loss_weight = flt(loss.get("loss_weight_grams"), 3)
			loss_types = loss.get("loss_types") or []
			mop_log.loss_type = ", ".join(loss_types) if loss_types else None
			source_rows = loss.get("source_rows") or []
			# loss_source_row is a Data field — guard against length blow-up
			# by capping at 140 chars (Frappe Data default). Truncated
			# entries still carry the first N references.
			joined = ",".join(source_rows)
			mop_log.loss_source_row = joined[:140] if len(joined) > 140 else joined

		mop_log.save()