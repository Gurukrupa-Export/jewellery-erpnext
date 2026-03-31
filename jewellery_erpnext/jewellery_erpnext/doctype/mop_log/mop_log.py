# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

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

	row_vals = frappe.db.sql(
		sql,
		(
			prefix_like,
			item_code,
			item_code,
			batch_no,
			prefix_like,
			item_code,
			item_code,
			batch_no,
			mwo,
		),
		as_dict=True,
	)

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
	pcs_after_prefix = pcs + cint(stats["sum_pcs_prefix"])
	pcs_after_item = pcs + cint(stats["sum_pcs_item"])
	pcs_after_batch = pcs + cint(stats["sum_pcs_batch"])

	qty_after_prefix = qty + flt(stats["sum_qty_prefix"])
	qty_after_item = qty + flt(stats["sum_qty_item"])
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

	mop_log.save()


def get_last_mop_index(manufacturing_operation, voucher_type=None, voucher_no=None):
	filters = {"manufacturing_operation": manufacturing_operation, "is_cancelled": 0}
	if voucher_type:
		filters["voucher_type"] = voucher_type
	if voucher_no:
		filters["voucher_no"] = voucher_no

	last_log = frappe.db.get_value("MOP Log", filters, "max(flow_index) as flow_index")
	return last_log or None


def create_mop_log_for_department_ir(
	self, row, to_warehouse, from_warehouse, operation
):
	flow_index = (
		get_last_mop_index(row.manufacturing_operation, voucher_type="Department IR")
		or None
	)
	print(flow_index)
	filters = {
		"manufacturing_operation": row.manufacturing_operation,
		"voucher_type": "Department IR",
		"is_cancelled": 0,
	}
	if flow_index:
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


def creste_mop_log_for_employee_ir(self, row, from_warehouse, to_warehouse):
	manufacturing_operation_manufacturing_operation = frappe.db.get_value(
		"Manufacturing Operation", row.manufacturing_operation, "department_receive_id"
	)
	if not manufacturing_operation_manufacturing_operation:
		frappe.throw(
			_("Department Receive ID not set for Manufacturing Operation {0}").format(
				row.manufacturing_operation
			)
		)
	mop_logs = frappe.db.get_all(
		"MOP Log",
		{
			"manufacturing_operation": row.manufacturing_operation,
			"is_cancelled": 0,
			"voucher_type": "Department IR",
			"voucher_no": manufacturing_operation_manufacturing_operation,
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


def create_mop_log_for_employee_ir_receive(doc, row, from_warehouse, to_warehouse):
	"""Create MOP Log entries for the Receive side of Employee IR.

	Reads the MOP Logs created during Issue (voucher_type='Employee IR') for this
	manufacturing_operation, and creates corresponding receive entries with reversed
	warehouse direction (employee/subcontractor WH → department WH).

	The MOPLog.validate() hook automatically updates Manufacturing Operation weight
	fields (net_wt, finding_wt, diamond_wt, etc.) based on the logged item data.
	"""
	mop_logs = frappe.db.get_all(
		"MOP Log",
		{
			"manufacturing_operation": row.manufacturing_operation,
			"is_cancelled": 0,
			"voucher_type": "Employee IR",
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
