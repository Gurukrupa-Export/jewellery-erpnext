# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import copy
import json

import frappe
from frappe import _, scrub
from frappe.model.document import Document
from frappe.query_builder import CustomFunction
from frappe.query_builder.functions import IfNull, Sum
from frappe.utils import cint, flt, get_datetime

from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.department_ir_utils import (
	get_summary_data,
	valid_reparing_or_next_operation,
	validate_and_update_gross_wt_from_mop,
	validate_mwo,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	create_mop_log_for_department_ir,
	get_last_mop_index,
)
from jewellery_erpnext.utils import set_values_in_bulk


class DepartmentIR(Document):
	def before_validate(self):
		if self.docstatus != 1:
			if self.company != frappe.db.get_value(
				"Department", self.current_department, "company"
			):
				frappe.throw(
					_("{0} does not belongs to {1}").format(
						self.current_department, self.company
					)
				)

			other_department = self.previous_department or self.next_department
			if self.company != frappe.db.get_value(
				"Department", other_department, "company"
			):
				frappe.throw(
					_("{0} does not belongs to {1}").format(
						other_department, self.company
					)
				)

			warehouse = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"department": self.current_department,
					"warehouse_type": "Manufacturing",
				},
			)
			if not warehouse:
				frappe.throw(_("MFG Warehouse not available for department"))
			if frappe.db.get_value(
				"Stock Reconciliation",
				{
					"set_warehouse": warehouse,
					"workflow_state": ["in", ["In Progress", "Send for Approval"]],
				},
			):
				frappe.throw(_("Stock Reconciliation is under process"))
			mwo_list = validate_and_update_gross_wt_from_mop(self)
			valid_reparing_or_next_operation(self, mwo_list)

		validate_mwo(self)

	@frappe.whitelist()
	def get_operations(self):
		dir_status = (
			"In-Transit"
			if self.type == "Receive"
			else ["not in", ["In-Transit", "Received"]]
		)
		filters = {"department_ir_status": dir_status}
		if self.type == "Issue":
			filters["status"] = ["in", ["Finished", "Revert"]]
			filters["department"] = self.current_department
		records = frappe.get_list(
			"Manufacturing Operation", filters, ["name", "gross_wt"]
		)
		self.department_ir_operation = []
		if records:
			for row in records:
				self.append(
					"department_ir_operation", {"manufacturing_operation": row.name}
				)

	def before_submit(self):
		if not self.department_ir_operation:
			frappe.throw("Add row in <b>Department IR Operations Table</b>")

		if self.type == "Receive" and not self.receive_against:
			frappe.throw("<b>Receive Against</b> is not set for this Receive entry")

		if self.type == "Receive" and self.receive_against:
			self.validate_receive_lineage()

	def validate_receive_lineage(self):
		"""Ensure each child MOP belongs to this Receive's Issue and is still In-Transit."""
		issue = frappe.db.get_value(
			"Department IR",
			self.receive_against,
			["docstatus", "type"],
			as_dict=True,
		)
		if not issue or issue.type != "Issue" or cint(issue.docstatus) != 1:
			frappe.throw(
				_(
					"Receive Against must be a submitted Department IR Issue ({0})"
				).format(self.receive_against)
			)
		for row in self.department_ir_operation:
			mop_name = row.manufacturing_operation
			if not mop_name:
				frappe.throw(
					_(
						"Row {0}: Manufacturing Operation is required for Department IR Receive"
					).format(row.idx)
				)
			meta = frappe.db.get_value(
				"Manufacturing Operation",
				mop_name,
				["department_issue_id", "department_ir_status"],
				as_dict=True,
			)
			if not meta:
				frappe.throw(
					_("Manufacturing Operation {0} does not exist").format(mop_name)
				)
			if meta.department_issue_id != self.receive_against:
				frappe.throw(
					_(
						"Manufacturing Operation {0} is not linked to Department IR Issue {1}"
					).format(mop_name, self.receive_against)
				)
			if meta.department_ir_status != "In-Transit":
				frappe.throw(
					_(
						"Manufacturing Operation {0} must be In-Transit to receive (found {1})"
					).format(mop_name, meta.department_ir_status or "")
				)

	def on_submit(self):
		if self.type == "Issue":
			self.on_submit_issue_new()
		else:
			self.on_submit_receive()

	def on_cancel(self):
		if self.type == "Issue":
			self.on_submit_issue_new(cancel=True)
		else:
			self.on_submit_receive(cancel=True)

	# for Receive
	def on_submit_receive(self, cancel=False):
		# se_data = json.loads(self.se_data) if self.se_data else {}
		# if not se_data:
		# 	import copy

		values = {}
		values["department_receive_id"] = self.name
		values["department_ir_status"] = "Received"

		# se_item_list = []
		dt_string = get_datetime()

		in_transit_wh = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"department": self.current_department,
				"warehouse_type": "Manufacturing",
			},
			"default_in_transit_warehouse",
		)

		department_wh = frappe.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"department": self.current_department,
				"warehouse_type": "Manufacturing",
			},
		)
		if cancel:
			# Bulk db.set_value bypasses MOPLog.validate, so capture the affected
			# MOPs first and replay the central recompute after the flip.
			affected_mops_recv = [
				r[0]
				for r in frappe.db.sql(
					"""
					SELECT DISTINCT manufacturing_operation FROM `tabMOP Log`
					WHERE voucher_type = %s AND voucher_no = %s AND is_cancelled = 0
					  AND manufacturing_operation IS NOT NULL
					""",
					(self.doctype, self.name),
				)
			]
			frappe.db.set_value(
				"MOP Log",
				{
					"voucher_type": self.doctype,
					"voucher_no": self.name,
					"is_cancelled": 0,
				},
				"is_cancelled",
				1,
			)
			from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
				recalculate_manufacturing_operation_weights,
			)

			for mop_name in affected_mops_recv:
				recalculate_manufacturing_operation_weights(mop_name)
			values.update(
				{
					"department_receive_id": None,
					"department_ir_status": "In-Transit",
					"status": "Not Started",
				}
			)
		for row in self.department_ir_operation:
			if not cancel:
				create_mop_log_for_department_ir(
					self, row, department_wh, in_transit_wh, row.manufacturing_operation
				)

			frappe.db.set_value(
				"Manufacturing Operation", row.manufacturing_operation, values
			)
			frappe.db.set_value(
				"Manufacturing Work Order",
				row.manufacturing_work_order,
				"department",
				self.current_department,
			)

			doc = frappe.get_doc("Manufacturing Operation", row.manufacturing_operation)
			doc.set("department_time_logs", [])
			doc.save()

			time_values = copy.deepcopy(values)
			time_values["department_start_time"] = dt_string
			add_time_log(doc, time_values)
		# else:
		# 	se_item_list = se_data

		# if not se_item_list:
		# 	frappe.msgprint(
		# 		_("No Stock Entries were generated during this Department IR")
		# 	)
		# 	return

		# if not cancel:
		# 	stock_doc = frappe.new_doc("Stock Entry")
		# 	stock_doc.update(
		# 		{
		# 			"stock_entry_type": "Material Transfer to Department",
		# 			"company": self.company,
		# 			"department_ir": self.name,
		# 			"auto_created": True,
		# 			"add_to_transit": 0,
		# 			"inventory_type": None,
		# 		}
		# 	)

		# 	for row in se_item_list:
		# 		stock_doc.append("items", row)

		# 	stock_doc.flags.ignore_permissions = True
		# 	stock_doc.save()
		# 	stock_doc.submit()

		# 	self.update_fg_mwo()  # need to optimze this flow onverheading stock entry creation 50%

		# if cancel:
		# 	se_list = frappe.db.get_list("Stock Entry", {"department_ir": self.name})
		# 	for row in se_list:
		# 		se_doc = frappe.get_doc("Stock Entry", row.name)
		# 		se_doc.cancel()

		# 	for row in self.department_ir_operation:
		# 		frappe.db.set_value(
		# 			"Manufacturing Operation",
		# 			row.manufacturing_operation,
		# 			"status",
		# 			"Not Started",
		# 		)

	def on_submit_issue_new(self, cancel=False):
		# if not self.mop_data:
		dt_string = get_datetime()
		status = "Not Started" if cancel else "Finished"
		values = {"status": status}

		# 	mop_data = frappe._dict({})
		# 	stock_entry_data = []  # Accumulate data for batch update
		if not cancel:
			in_transit_wh = frappe.db.get_value(
				"Warehouse",
				{"department": self.next_department, "warehouse_type": "Manufacturing"},
				"default_in_transit_warehouse",
			)

			department_wh = frappe.db.get_value(
				"Warehouse",
				{
					"department": self.current_department,
					"warehouse_type": "Manufacturing",
				},
			)
			if not department_wh:
				frappe.throw(
					_("Please set warehouse for department {0}").format(
						self.current_department
					)
				)
			if not in_transit_wh:
				frappe.throw(
					_(
						"Please set default in transit warehouse for department {0}"
					).format(self.next_department)
				)
		else:
			# Bulk db.set_value bypasses MOPLog.validate; replay the recompute
			# after the flip so prefix buckets shed the just-cancelled rows.
			affected_mops_iss = [
				r[0]
				for r in frappe.db.sql(
					"""
					SELECT DISTINCT manufacturing_operation FROM `tabMOP Log`
					WHERE voucher_type = %s AND voucher_no = %s AND is_cancelled = 0
					  AND manufacturing_operation IS NOT NULL
					""",
					(self.doctype, self.name),
				)
			]
			frappe.db.set_value(
				"MOP Log",
				{
					"voucher_type": self.doctype,
					"voucher_no": self.name,
					"is_cancelled": 0,
				},
				"is_cancelled",
				1,
			)
			from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
				recalculate_manufacturing_operation_weights,
			)

			for mop_name in affected_mops_iss:
				recalculate_manufacturing_operation_weights(mop_name)
		for row in self.department_ir_operation:
			if cancel:
				new_operation = frappe.db.get_value(
					"Manufacturing Operation",
					{
						"department_issue_id": self.name,
						"manufacturing_work_order": row.manufacturing_work_order,
					},
				)
				new_operation = frappe.get_doc("Manufacturing Operation", new_operation)
				se_list = frappe.db.get_list(
					"Stock Entry", {"department_ir": self.name}
				)
				for se in se_list:
					se_doc = frappe.get_doc("Stock Entry", se.name)
					if se_doc.docstatus == 1:
						se_doc.cancel()

					frappe.db.set_value(
						"Stock Entry Detail",
						{"parent": se.name},
						"manufacturing_operation",
						None,
					)

				frappe.db.set_value(
					"Manufacturing Work Order",
					row.manufacturing_work_order,
					"manufacturing_operation",
					row.manufacturing_operation,
				)
				if new_operation.name:
					frappe.db.set_value(
						"Department IR Operation",
						{
							"docstatus": 2,
							"manufacturing_operation": new_operation.name,
						},
						"manufacturing_operation",
						None,
					)
					frappe.db.set_value(
						"Stock Entry Detail",
						{
							"docstatus": 2,
							"manufacturing_operation": new_operation.name,
						},
						"manufacturing_operation",
						None,
					)
					frappe.delete_doc(
						"Manufacturing Operation",
						new_operation.name,
						ignore_permissions=1,
					)
				frappe.db.set_value(
					"Manufacturing Operation",
					row.manufacturing_operation,
					"status",
					"In Transit",
				)
			else:
				values["complete_time"] = dt_string
				new_operation = create_operation_for_next_dept(
					self.name,
					row.manufacturing_work_order,
					row.manufacturing_operation,
					self.next_department,
				)
				# Accumulate data for batch update instead of calling the function here
				# stock_entry_data.append(
				# 	(row.manufacturing_work_order, new_operation.name)
				# )

				frappe.db.set_value(
					"Manufacturing Operation",
					row.manufacturing_operation,
					"status",
					"Finished",
				)
				doc = frappe.get_doc(
					"Manufacturing Operation", row.manufacturing_operation
				)
				# mop_data.update(
				# 	{
				# 		row.manufacturing_work_order: {
				# 			"cur_mop": row.manufacturing_operation,
				# 			"new_mop": new_operation.name,
				# 		}
				# 	}
				# )
				add_time_log(doc, values)
				create_mop_log_for_department_ir(
					self, row, in_transit_wh, department_wh, new_operation.name
				)

	@frappe.whitelist()
	def get_summary_data(self):
		return get_summary_data(self)

	@frappe.whitelist()
	def get_manufacturing_operations_from_department_ir(self, docname):
		self.department_ir_operation = []
		for row in frappe.get_all(
			"Manufacturing Operation",
			{"department_issue_id": docname, "department_ir_status": "In-Transit"},
			[
				"name as manufacturing_operation",
				"manufacturing_work_order",
				"prev_gross_wt as gross_wt",
				"previous_mop",
				"department",
			],
		):
			self.current_department = row.department
			mop_details = frappe.db.get_value(
				"Manufacturing Operation",
				row.previous_mop,
				[
					"diamond_wt",
					"net_wt",
					"finding_wt",
					"diamond_pcs",
					"gemstone_pcs",
					"gemstone_wt",
					"other_wt",
					"department",
				],
				as_dict=1,
			)
			self.previous_department = mop_details.get("department")
			self.append(
				"department_ir_operation",
				{
					"manufacturing_operation": row.manufacturing_operation,
					"manufacturing_work_order": row.manufacturing_work_order,
					"gross_wt": row.gross_wt,
					"net_wt": mop_details.get("net_wt"),
					"diamond_wt": mop_details.get("diamond_wt"),
					"finding_wt": mop_details.get("finding_wt"),
					"diamond_pcs": mop_details.get("diamond_pcs"),
					"gemstone_pcs": mop_details.get("gemstone_pcs"),
					"gemstone_wt": mop_details.get("gemstone_wt"),
					"other_wt": mop_details.get("other_wt"),
				},
			)

	def update_fg_mwo(self):
		"""Update FG MWO MOP Balance Table from MOP Log."""

		manufacturer = self.manufacturer

		if not manufacturer:
			manufacturer = frappe.defaults.get_user_default("manufacturer")

		if not manufacturer:
			frappe.throw("Select manufacturer for updating FG MWO MOP")

		last_operation_department = frappe.db.get_value(
			"Manufacturing Setting",
			self.manufacturer,
			"default_last_operation_department",
		)

		is_last_operation_dept = False
		if self.current_department == last_operation_department:
			is_last_operation_dept = True

		for row in self.department_ir_operation:
			mwo = row.manufacturing_work_order

			if not is_last_operation_dept:
				continue

			result = frappe.db.sql(
				"""
				SELECT child.name
				FROM `tabManufacturing Work Order` AS child
				JOIN `tabManufacturing Work Order` AS parent
					ON child.manufacturing_order = parent.manufacturing_order
				WHERE parent.name = %s
				AND child.for_fg = 1
				AND child.docstatus = 0
				LIMIT 1
			""",
				(mwo,),
				as_dict=True,
			)

			if not result:
				return

			flow_index = get_last_mop_index(row.manufacturing_operation)
			if flow_index is None:
				continue

			mop_log_data = frappe.db.get_all(
				"MOP Log",
				filters={
					"manufacturing_operation": row.manufacturing_operation,
					"is_cancelled": 0,
					"flow_index": flow_index,
				},
				fields=[
					"item_code",
					"batch_no",
					"serial_no",
					"qty_after_transaction_batch_based as qty",
					"pcs_after_transaction_batch_based as pcs",
					"serial_and_batch_bundle",
				],
				order_by="creation asc",
			)

			fg_mwo = result[0].name

			mwo_doc = frappe.get_doc("Manufacturing Work Order", fg_mwo)

			for log_row in mop_log_data:
				if flt(log_row.qty) <= 0 and not log_row.pcs:
					continue
				mwo_doc.append(
					"mwo_mop_balance_table",
					{
						"raw_material": log_row.item_code,
						"batch_no": log_row.batch_no,
						"serial_no": log_row.serial_no,
						"qty": flt(log_row.qty),
						"pcs": log_row.pcs,
					},
				)

			mwo_doc.update_child_table("mwo_mop_balance_table")
			mwo_doc.db_update_all()


def update_stock_entry_dimensions(
	doc, row, manufacturing_operation, for_employee=False
):
	filters = {}
	if for_employee:
		filters["employee" if doc.type == "Receive" else "to_employee"] = doc.employee
		current_dep = doc.department
		next_dep = doc.department
	else:
		current_dep = doc.current_department
		next_dep = doc.next_department
	filters.update(
		{
			"manufacturing_work_order": row.manufacturing_work_order,
			"docstatus": 1,
			"manufacturing_operation": ["is", "not set"],
			"department": current_dep,
			"to_department": next_dep,
		}
	)
	stock_entries = frappe.db.get_all("Stock Entry", filters=filters, pluck="name")
	values = {"manufacturing_operation": manufacturing_operation}
	for stock_entry in stock_entries:
		rows = frappe.db.get_all(
			"Stock Entry Detail", {"parent": stock_entry}, pluck="name"
		)
		set_values_in_bulk("Stock Entry Detail", rows, values)
		values[scrub(doc.doctype)] = doc.name
		frappe.db.set_value("Stock Entry", stock_entry, values)
		del values[scrub(doc.doctype)]


def batch_update_stock_entry_dimensions(
	doc, stock_entry_data, employee, for_employee=False
):
	"""
	Batch update Stock Entry and Stock Entry Detail with manufacturing_operation using ORM.
	stock_entry_data: List of (manufacturing_work_order, manufacturing_operation) tuples.
	"""
	# Prepare filters
	if for_employee:
		emp_field = "employee" if doc.type == "Receive" else "to_employee"
		filters = {emp_field: employee}
		current_dep = doc.department
		next_dep = doc.department
	else:
		filters = {}
		current_dep = doc.current_department
		next_dep = doc.next_department

	# Batch fetch all matching Stock Entries
	mwo_list = [d[0] for d in stock_entry_data]
	filters.update(
		{
			"manufacturing_work_order": ["in", mwo_list],
			"docstatus": 1,
			"manufacturing_operation": ["is", "not set"],
			"department": current_dep,
			"to_department": next_dep,
		}
	)
	stock_entries = frappe.db.get_all("Stock Entry", filters=filters, pluck="name")

	if not stock_entries:
		return

	# Map manufacturing_operation to Stock Entry names
	mwo_to_mop = dict(stock_entry_data)
	se_updates = {}
	sed_updates = {}

	# Fetch all Stock Entry Detail rows in one query
	sed_rows = frappe.db.get_all(
		"Stock Entry Detail",
		filters={"parent": ["in", stock_entries]},
		fields=["name", "parent", "manufacturing_operation"],
	)

	# Prepare batch updates
	for se in stock_entries:
		mop = mwo_to_mop.get(
			frappe.db.get_value("Stock Entry", se, "manufacturing_work_order")
		)
		if mop:
			se_updates[se] = {
				"manufacturing_operation": mop,
				scrub(doc.doctype): doc.name,
			}

	for sed in sed_rows:
		mop = se_updates.get(sed.parent, {}).get("manufacturing_operation")
		if mop:
			sed_updates[sed.name] = {"manufacturing_operation": mop}

	# Batch update Stock Entry
	if se_updates:
		frappe.db.bulk_update(
			"Stock Entry", se_updates, chunk_size=150, update_modified=True
		)

	# Batch update Stock Entry Detail
	if sed_updates:
		frappe.db.bulk_update(
			"Stock Entry Detail", sed_updates, chunk_size=150, update_modified=True
		)


def fetch_and_update(doc, row, manufacturing_operation):
	filters = {}
	current_dep = doc.current_department
	filters.update(
		{
			"manufacturing_work_order": row.manufacturing_work_order,
			"docstatus": 1,
			# "manufacturing_operation": ["is", "not set"],
			"to_department": current_dep,
			#  "t_warehouse": department_wh
		}
	)
	stock_entries = frappe.get_all("Stock Entry", filters=filters, pluck="name")

	if not stock_entries:
		return False
	else:
		values = {"manufacturing_operation": manufacturing_operation}
		for stock_entry in stock_entries:
			rows = frappe.get_all(
				"Stock Entry Detail", {"parent": stock_entry}, pluck="name"
			)
			set_values_in_bulk("Stock Entry Detail", rows, values)
			values[scrub(doc.doctype)] = doc.name
			frappe.db.set_value("Stock Entry", stock_entry, values)
			del values[scrub(doc.doctype)]


def create_operation_for_next_dept(ir_name, mwo, mop, next_department):
	new_mop_doc = frappe.copy_doc(frappe.get_doc("Manufacturing Operation", mop))
	new_mop_doc.name = None
	new_mop_doc.department_issue_id = ir_name
	new_mop_doc.department_ir_status = "In-Transit"
	new_mop_doc.department_receive_id = None
	new_mop_doc.previous_operation = new_mop_doc.operation
	new_mop_doc.department = next_department
	new_mop_doc.previous_mop = mop
	new_mop_doc.operation = None
	new_mop_doc.previous_se_data_updated = 0
	new_mop_doc.insert()
	frappe.db.set_value(
		"Manufacturing Work Order", mwo, "manufacturing_operation", new_mop_doc.name
	)
	return new_mop_doc


def create_operation_for_next_dept_new(ir_name, mwo, mop, next_department):
	operation = frappe.db.get_value("Manufacturing Operation", mop, "operation")
	new_mop_doc = frappe.new_doc("Manufacturing Operation")
	new_mop_doc.department_issue_id = ir_name
	new_mop_doc.department_ir_status = "In-Transit"
	new_mop_doc.department_receive_id = None
	new_mop_doc.previous_operation = operation
	new_mop_doc.department = next_department
	new_mop_doc.previous_mop = mop
	new_mop_doc.operation = None
	new_mop_doc.previous_se_data_updated = 0
	new_mop_doc.insert()
	frappe.db.set_value(
		"Manufacturing Work Order", mwo, "manufacturing_operation", new_mop_doc.name
	)
	return new_mop_doc.name


@frappe.whitelist()
def get_manufacturing_operations(source_name, target_doc=None):
	if not target_doc:
		target_doc = frappe.new_doc("Department IR")
	elif isinstance(target_doc, str):
		target_doc = frappe.get_doc(json.loads(target_doc))

	operation = frappe.db.get_value(
		"Manufacturing Operation",
		source_name,
		["gross_wt", "manufacturing_work_order", "diamond_wt"],
		as_dict=1,
	)
	if not target_doc.get(
		"department_ir_operation",
		{"manufacturing_work_order": operation["manufacturing_work_order"]},
	):
		target_doc.append(
			"department_ir_operation",
			{
				"manufacturing_operation": source_name,
				"manufacturing_work_order": operation["manufacturing_work_order"],
				"gross_wt": operation["gross_wt"],
				"diamond_wt": operation["diamond_wt"],
			},
		)
	return target_doc


@frappe.whitelist()
def department_receive_query(doctype, txt, searchfield, start, page_len, filters):
	DIR = frappe.qb.DocType("Department IR")
	DP = frappe.qb.DocType("Department IR")
	query = (
		frappe.qb.from_(DIR)
		.select(DIR.name)
		.where(
			(DIR.type == "Issue")
			& (DIR.docstatus == 1)
			& (DIR.name.like("%{0}%".format(txt)))
			& (
				DIR.name.notin(
					frappe.qb.from_(DP)
					.select(DP.receive_against)
					.where(
						(DP.docstatus == 1)
						& (DP.type == "Receive")
						& (DP.receive_against.isnotnull())
					)
				)
			)
		)
	)
	if filters.get("current_department") and filters.get("current_department") != "":
		query = query.where(DIR.current_department == filters.get("current_department"))

	if filters.get("next_department") and filters.get("next_department") != "":
		query = query.where(DIR.next_department == filters.get("next_department"))
	data = query.run()

	return data if data else []


def get_material_wt(doc, manufacturing_operation):
	SED = frappe.qb.DocType("Stock Entry Detail")
	SE = frappe.qb.DocType("Stock Entry")
	Item = frappe.qb.DocType("Item")

	IF = CustomFunction("IF", ["condition", "true_expr", "false_expr"])
	query = (
		frappe.qb.from_(SED)
		.left_join(SE)
		.on(SED.parent == SE.name)
		.left_join(Item)
		.on(Item.name == SED.item_code)
		.select(
			IfNull(Sum(IF(SED.uom == "Carat", SED.qty * 0.2, SED.qty)), 0).as_(
				"gross_wt"
			),
			IfNull(Sum(IF(Item.variant_of == "M", SED.qty, 0)), 0).as_("net_wt"),
			IfNull(Sum(IF(Item.variant_of == "D", SED.qty, 0)), 0).as_("diamond_wt"),
			IfNull(
				Sum(
					IF(
						Item.variant_of == "D",
						IF(SED.uom == "Carat", SED.qty * 0.2, SED.qty),
						0,
					)
				),
				0,
			).as_("diamond_wt_in_gram"),
			IfNull(Sum(IF(Item.variant_of == "G", SED.qty, 0)), 0).as_("gemstone_wt"),
			IfNull(
				Sum(
					IF(
						Item.variant_of == "G",
						IF(SED.uom == "Carat", SED.qty * 0.2, SED.qty),
						0,
					)
				),
				0,
			).as_("gemstone_wt_in_gram"),
			IfNull(Sum(IF(Item.variant_of == "O", SED.qty, 0)), 0).as_("other_wt"),
		)
		.where(
			(SE[scrub(doc.doctype)] == doc.name)
			& (SED.manufacturing_operation == manufacturing_operation)
			& (SE.docstatus == 1)
		)
	)
	res = query.run(as_dict=True)

	if res:
		return res[0]
	return {}


# timer code
def add_time_log(doc, args):
	last_row = []

	if doc.department_time_logs and len(doc.department_time_logs) > 0:
		last_row = doc.department_time_logs[-1]

	doc.reset_timer_value(args)

	# issue - complete_time
	if last_row and args.get("complete_time"):
		for row in doc.department_time_logs:
			if not row.department_to_time:
				row.update(
					{
						"department_to_time": get_datetime(args.get("complete_time")),
					}
				)

	# receive - department_start_time
	elif args.get("department_start_time"):
		new_args = frappe._dict(
			{
				"department_from_time": get_datetime(args.get("department_start_time")),
			}
		)
		doc.add_start_time_log(new_args)

	doc.update_children()
	doc.db_update_all()


def add_time_log_optimize(mop_name, args):
	status = args.get("status")

	# Normalize status
	if status == "Resume Job":
		status = "WIP"

	# Reset timer values (status, current_time, started_time)
	update_fields = {}
	if status:
		update_fields["status"] = status
	if status in ["WIP", "Finished"]:
		update_fields["current_time"] = 0.0
	if status == "WIP" and args.get("start_time"):
		update_fields["started_time"] = get_datetime(args["start_time"])

	if update_fields:
		frappe.db.set_value("Manufacturing Operation", mop_name, update_fields)

	# 1. If complete_time exists → update all open department_time_logs
	if args.get("complete_time"):
		complete_time = get_datetime(args["complete_time"])
		frappe.db.sql(
			"""
			UPDATE `tabManufacturing Operation Department Time Log`
			SET department_to_time = %s
			WHERE parent = %s
				AND parenttype = 'Manufacturing Operation'
				AND department_to_time IS NULL
			""",
			(complete_time, mop_name),
		)

	# 2. Else if department_start_time exists → insert a department_time_log row
	elif args.get("department_start_time"):
		dept_from_time = get_datetime(args["department_start_time"])
		frappe.db.sql(
			"""
			INSERT INTO `tabManufacturing Operation Department Time Log`
			(name, parent, parenttype, parentfield, creation, modified,
			department_from_time)
			VALUES (%s, %s, 'Manufacturing Operation', 'department_time_logs', NOW(), NOW(), %s)
			""",
			(frappe.generate_hash(), mop_name, dept_from_time),
		)

	# 3. Else if start_time exists → insert into time_logs with optional employee
	elif args.get("start_time"):
		from_time = get_datetime(args["start_time"])
		employee = args.get("employee")
		frappe.db.sql(
			"""
			INSERT INTO `tabManufacturing Operation Time Log`
			(name, parent, parenttype, parentfield, creation, modified,
			from_time, employee)
			VALUES (%s, %s, 'Manufacturing Operation', 'time_logs', NOW(), NOW(), %s, %s)
			""",
			(frappe.generate_hash(), mop_name, from_time, employee),
		)
