# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
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
				"Manufacturing Operation", self.manufacturing_operation, update_value
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
	if loss_wt:
		if loss_wt > 0:
			gross_wt += flt(loss_wt)
		elif loss_wt < 0:
			gross_wt -= abs(flt(loss_wt))

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
	pcs = cint(row.get("pcs") or 0)
	qty = flt(row.get("qty") or 0.0)
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
		previous_mop = frappe.get_cached_value(
			"Manufacturing Operation", mop_op, "previous_mop"
		)
		if previous_mop:
			previous_mop_qty = (
				frappe.get_cached_value(
					"Manufacturing Operation",
					previous_mop,
					FIELD_MAP.get(first_char) + "_wt",
				)
				or 0
			)

			previous_mop_pcs = (
				frappe.get_cached_value(
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

	# compute fields
	pcs_after_prefix = pcs + cint(stats["sum_pcs_prefix"]) + previous_mop_pcs
	pcs_after_item = pcs + cint(stats["sum_pcs_item"]) + previous_mop_pcs
	pcs_after_batch = pcs + cint(stats["sum_pcs_batch"]) + previous_mop_pcs

	qty_after_prefix = qty + flt(stats["sum_qty_prefix"]) + previous_mop_qty
	qty_after_item = qty + flt(stats["sum_qty_item"]) + previous_mop_qty
	qty_after_batch = qty + flt(stats["sum_qty_batch"]) + previous_mop_qty
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
	mop_log.flow_index = get_last_mop_index(row.manufacturing_operation) + 1
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
	"""Return the latest non-cancelled MOP Log row per item/batch for a MOP."""
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
		# Only clone the latest Issue snapshot tier (multiple rows share the same max flow_index).
		# Without this, historical Issue rows at lower flow_index would be replayed as extra Receive rows.
		if mop_logs:
			max_issue_flow = max(cint(log.get("flow_index") or 0) for log in mop_logs)
			mop_logs = [
				log
				for log in mop_logs
				if cint(log.get("flow_index") or 0) == max_issue_flow
			]
		if not mop_logs:
			frappe.log_error(
				title="MOP Log Fallback",
				message=f"DIR Receive missing Issue logs for {self.receive_against}, falling back to tail-snapshot.",
			)

	if not mop_logs:
		if is_receive and frappe.get_site_config().get(
			"department_ir_receive_strict_lineage"
		):
			frappe.throw(
				_(
					"No MOP Log rows found for Department IR Issue {0} on Manufacturing Operation {1}. "
					"Fix Issue-side MOP Logs or disable site config department_ir_receive_strict_lineage."
				).format(self.receive_against, row.manufacturing_operation)
			)
		flow_index = get_last_mop_index(row.manufacturing_operation)
		filters = {
			"manufacturing_operation": row.manufacturing_operation,
			"is_cancelled": 0,
		}
		if flow_index is not None:
			filters["flow_index"] = flow_index
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


def create_mop_log_for_employee_ir_receive(
	doc, row, from_warehouse, to_warehouse, stock_entry_name=[]
):
	"""Create MOP Log entries for the Receive side of Employee IR.

	Reads the MOP Logs created during the matching Employee IR **Issue** only
	(``voucher_no`` = Issue name), not every historical Employee IR log on the MOP.

	The MOPLog.validate() hook automatically updates Manufacturing Operation weight
	fields (net_wt, finding_wt, diamond_wt, etc.) based on the logged item data.
	"""
	issue_voucher = resolve_employee_ir_issue_voucher_for_receive(doc, row)

	mop_logs = frappe.db.get_all(
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
	if stock_entry_name:
		mop_logs += frappe.db.get_all(
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
		mop_log.voucher_type = "Employee IR"
		mop_log.voucher_no = doc.name
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
