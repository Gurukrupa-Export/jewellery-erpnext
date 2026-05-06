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


def get_current_mop_balance_rows(
	manufacturing_operation, include_fields=None, keys=None
):
	"""Return the latest non-cancelled MOP Log row per item/batch for a MOP.

	Loss-attribution rows (log_category="Loss Attribution") ARE included —
	they post a real qty_change reduction so the balance after loss must be
	reflected to downstream readers (e.g. Make Receive Entry availability,
	manual loss validation, EOD SRE reconciliation).

	When ``keys`` is provided as a list of ``(item_code, batch_no)`` tuples,
	the underlying ``frappe.db.get_all`` is narrowed by the distinct
	``item_code`` set so popups never scan unrelated items. The composite
	index ``mop_balance_idx`` (added by ``add_make_receive_entry_indexes``)
	covers ``(manufacturing_operation, is_cancelled, item_code, batch_no,
	creation)`` so the narrowed filter is index-served. The Python-side
	dedup picks the latest row per ``(item_code, batch_no)``.
	"""
	fields = list(
		dict.fromkeys((include_fields or current_balance_fields) + ["name", "creation"])
	)
	filters = {
		"manufacturing_operation": manufacturing_operation,
		"is_cancelled": 0,
	}
	if keys:
		item_codes = sorted({k[0] for k in keys if k and k[0]})
		if not item_codes:
			return []
		filters["item_code"] = ["in", item_codes]
	mop_logs = frappe.db.get_all(
		"MOP Log",
		filters=filters,
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
		"mop_data_present": mop_log_reference is not None,
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
	"""Audit-only MOP Log clones on the SOURCE MOP for Employee IR Receive.

	Reads the MOP Logs created during the matching Employee IR **Issue** only
	(``voucher_no`` = Issue name), not every historical Employee IR log on
	the MOP.

	**Source MOP is left unchanged.** Per the new contract, loss-driven
	weight reductions land on the NEW Manufacturing Operation only — see
	``update_new_mop_wtg``, which both clones the source baseline AND
	subtracts loss in-place per ``(item, batch)``. The rows written here
	are pure clones (qty_change=0) of the issue-tier balance so audit
	metadata (loss_weight, loss_type, loss_source_row) stays attached to a
	real MOP Log row on the source MOP for traceability, but no balance
	shift happens.

	UOM rule: ``qty_after_transaction*`` stay in the item's stock UOM
	(carats for D/G, grams for M/F/O), copied verbatim from the source log.
	MOPLog.validate's prefix-bucket write is a no-op here because the qty
	matches what is already on the source MOP.
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

		# SOURCE MOP audit clone: qty_change=0, balances copied verbatim.
		# Loss is applied to the NEW MOP inside update_new_mop_wtg's
		# baseline-clone loop (one row per item/batch, already reduced).
		mop_log = frappe.new_doc("MOP Log")
		mop_log.item_code = log.item_code
		mop_log.qty_change = 0
		mop_log.pcs_change = 0

		mop_log.qty_after_transaction = flt(log.qty_after_transaction)
		mop_log.qty_after_transaction_item_based = flt(
			log.qty_after_transaction_item_based
		)
		mop_log.qty_after_transaction_batch_based = flt(
			log.qty_after_transaction_batch_based
		)

		mop_log.pcs_after_transaction = cint(log.pcs_after_transaction)
		mop_log.pcs_after_transaction_item_based = cint(
			log.pcs_after_transaction_item_based
		)
		mop_log.pcs_after_transaction_batch_based = cint(
			log.pcs_after_transaction_batch_based
		)

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


def create_mop_log_for_employee_ir_loss(
	eir_doc, loss_row, loss_type, total_loss_for_mwo, from_wh=None, to_wh=None
):
	"""Bridge writer for Employee IR Receive loss attribution.

	Posts each loss detail as a real MOP Log movement so the Manufacturing
	Operation weight bucket is reduced by the loss amount. Concretely:

	  qty_change                       = -loss_weight (gram)
	  qty_after_transaction*           = previous balance - loss_weight

	When MOPLog.validate() runs it writes ``qty_after_transaction`` into the
	prefix bucket (net_wt / finding_wt / diamond_wt / gemstone_wt / other_wt)
	on Manufacturing Operation, so the post-loss weight is reflected without
	any additional bookkeeping here.

	Carat-denominated rows (typical for D / G items) are converted to grams
	via x0.2 so the MOP Log balance — which is gram-based — stays consistent.

	Idempotent on (voucher_type, voucher_no, manufacturing_operation,
	loss_source_row, loss_type, is_cancelled=0). is_synced=1 keeps the EOD
	sync from re-materializing it.
	"""
	raw = flt(loss_row.proportionally_loss)
	stock_uom = frappe.get_cached_value("Item", loss_row.item_code, "stock_uom")
	loss_weight = raw * 0.2 if stock_uom == "Carat" else raw
	if loss_weight <= 0:
		return None

	if frappe.db.exists(
		"MOP Log",
		{
			"voucher_type": "Employee IR",
			"voucher_no": eir_doc.name,
			"manufacturing_operation": loss_row.manufacturing_operation,
			"loss_source_row": loss_row.name,
			"loss_type": loss_type,
			"is_cancelled": 0,
		},
	):
		return None

	pct = (loss_weight / total_loss_for_mwo) if total_loss_for_mwo else 0

	# Latest balance for this (item, batch) on the same MOP. Includes any
	# prior loss-attribution rows so successive loss postings stack.
	latest = (
		frappe.db.get_value(
			"MOP Log",
			{
				"manufacturing_operation": loss_row.manufacturing_operation,
				"item_code": loss_row.item_code,
				"batch_no": loss_row.batch_no,
				"is_cancelled": 0,
			},
			[
				"qty_after_transaction",
				"qty_after_transaction_item_based",
				"qty_after_transaction_batch_based",
				"pcs_after_transaction",
				"pcs_after_transaction_item_based",
				"pcs_after_transaction_batch_based",
				"flow_index",
			],
			order_by="creation desc",
			as_dict=True,
		)
		or {}
	)

	# Loss reduces qty balance by loss_weight; PCS balance is preserved (loss
	# is recorded by weight, not by piece count).
	pcs_change = 0
	if loss_row.item_code[0] in ("D", "G"):
		pcs_change = -cint(loss_row.pcs or 0)
	mop_log = frappe.new_doc("MOP Log")
	mop_log.item_code = loss_row.item_code
	mop_log.batch_no = loss_row.batch_no
	mop_log.qty_change = -flt(loss_weight, 3)
	mop_log.pcs_change = pcs_change
	for k in (
		"qty_after_transaction",
		"qty_after_transaction_item_based",
		"qty_after_transaction_batch_based",
	):
		mop_log.set(k, flt(latest.get(k) or 0) - flt(loss_weight, 3))
	for k in (
		"pcs_after_transaction",
		"pcs_after_transaction_item_based",
		"pcs_after_transaction_batch_based",
	):
		mop_log.set(k, latest.get(k) or 0)
	# Do not advance flow_index — loss attribution is booked at the same
	# materialization tier as the receive that triggered it.
	mop_log.flow_index = latest.get("flow_index") or 0
	mop_log.from_warehouse = from_wh
	mop_log.to_warehouse = to_wh
	mop_log.voucher_type = "Employee IR"
	mop_log.voucher_no = eir_doc.name
	mop_log.manufacturing_operation = loss_row.manufacturing_operation
	mop_log.manufacturing_work_order = loss_row.manufacturing_work_order
	mop_log.row_name = loss_row.name
	mop_log.is_synced = 1
	# Loss attribution fields (custom_fields/mop_log.json)
	mop_log.log_category = "Loss Attribution"
	mop_log.loss_type = loss_type
	mop_log.loss_weight = flt(loss_weight, 3)
	mop_log.loss_percentage = flt(pct * 100, 4)
	mop_log.loss_source_row = loss_row.name
	mop_log.save()
	return mop_log.name
