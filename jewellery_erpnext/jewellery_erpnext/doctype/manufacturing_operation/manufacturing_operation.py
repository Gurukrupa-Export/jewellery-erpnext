# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import json
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import make_autoname
from frappe.query_builder import Criterion, CustomFunction
from frappe.query_builder.functions import Avg, IfNull, Max, Sum
from frappe.utils import (
	flt,
	get_datetime,
	get_timedelta,
	now,
	time_diff,
	time_diff_in_hours,
	time_diff_in_seconds,
)

from jewellery_erpnext.utils import set_values_in_bulk, update_existing


class OperationSequenceError(frappe.ValidationError):
	pass


class OverlapError(frappe.ValidationError):
	pass


class ManufacturingOperation(Document):
	# timer code
	def reset_timer_value(self, args):
		self.started_time = None

		if args.get("status") in ["WIP", "Finished"]:
			self.current_time = 0.0

			if args.get("status") == "WIP":
				self.started_time = get_datetime(args.get("start_time"))

		if args.get("status") == "Resume Job":
			args["status"] = "WIP"

		if args.get("status"):
			self.status = args.get("status")

	# timer code
	def add_start_time_log(self, args):
		if "department_from_time" in args:
			self.append("department_time_logs", args)
		else:
			self.append("time_logs", args)

	# timer code
	def add_time_log(self, args):
		last_row = []
		employees = args.employees
		# if isinstance(employees, str):
		# 	employees = json.loads(employees)
		if self.time_logs and len(self.time_logs) > 0:
			last_row = self.time_logs[-1]

		self.reset_timer_value(args)
		if last_row and args.get("complete_time"):
			for row in self.time_logs:
				if not row.to_time:
					row.update(
						{
							"to_time": get_datetime(args.get("complete_time")),
							# "operation": args.get("sub_operation")
							# "completed_qty": args.get("completed_qty") or 0.0,
						}
					)
		elif args.get("start_time"):
			new_args = frappe._dict(
				{
					"from_time": get_datetime(args.get("start_time")),
					# "operation": args.get("sub_operation"),
					# "completed_qty": 0.0,
				}
			)

			if employees:
				# for name in employees:
				new_args.employee = employees
				self.add_start_time_log(new_args)
			else:
				self.add_start_time_log(new_args)

		if self.status in ["QC Pending", "On Hold"]:
			self.current_time = time_diff_in_seconds(last_row.to_time, last_row.from_time)

		self.save()

	# def validate_sequence_id(self):
	# 	# if self.is_corrective_job_card:
	# 	# 	return

	# 	# if not (self.work_order and self.sequence_id):
	# 	# 	return

	# 	# current_operation_qty = 0.0
	# 	# data = self.get_current_operation_data()
	# 	# if data and len(data) > 0:
	# 	# 	current_operation_qty = flt(data[0].completed_qty)

	# 	# current_operation_qty += flt(self.total_completed_qty)

	# 	data = frappe.get_all(
	# 		"Work Order Operation",
	# 		fields=["operation", "status", "completed_qty", "sequence_id"],
	# 		filters={"docstatus": 1, "parent": self.work_order, "sequence_id": ("<", self.sequence_id)},
	# 		order_by="sequence_id, idx",
	# 	)

	# 	message = "Job Card {0}: As per the sequence of the operations in the work order {1}".format(
	# 		bold(self.name), bold(get_link_to_form("Work Order", self.work_order))
	# 	)

	# 	for row in data:
	# 		if row.status != "Completed" and row.completed_qty < current_operation_qty:
	# 			frappe.throw(
	# 				_("{0}, complete the operation {1} before the operation {2}.").format(
	# 					message, bold(row.operation), bold(self.operation)
	# 				),
	# 				OperationSequenceError,
	# 			)

	# 		if row.completed_qty < current_operation_qty:
	# 			msg = f"""The completed quantity {bold(current_operation_qty)}
	# 				of an operation {bold(self.operation)} cannot be greater
	# 				than the completed quantity {bold(row.completed_qty)}
	# 				of a previous operation
	# 				{bold(row.operation)}.
	# 			"""

	# 			frappe.throw(_(msg))

	def validate(self):
		if self.flags.ignore_validation:
			self.set_start_finish_time()
			return

		if self.is_new():
			return

		self.set_start_finish_time()
		# self.validate_time_logs()
		self.validate_loss()
		self.get_previous_se_details()
		self.remove_duplicate()
		self.set_mop_balance_table()  # To Set MOP Bailance Table on update source & target Table.
		self.update_weights()
		self.validate_operation()
		# self.validate_main_slip()

	# def validate_main_slip(self):
	# 	# Find Employee IR where the child table employee_ir_operations has manufacturing_operation = self.name
	# 	ir_operations = frappe.get_all(
	# 		"Employee IR Operation",
	# 		filters={
	# 			"manufacturing_operation": self.name,
	# 			"parenttype": "Employee IR"
	# 		},
	# 		fields=["parent"]
	# 	)

	# 	matched_ir = None

	# 	if ir_operations:
	# 		parent_ir_names = [op["parent"] for op in ir_operations]
	# 		employee_ir = frappe.get_all(
	# 			"Employee IR",
	# 			filters={
	# 				"employee": self.employee,
	# 				"operation": self.operation,
	# 				"docstatus": 1,
	# 				"type": "Issue",
	# 				"name": ["in", parent_ir_names]
	# 			},
	# 			fields=["name"]
	# 		)
	# 		if employee_ir:
	# 			ir_doc = frappe.get_doc("Employee IR", employee_ir[0].name)
	# 			if ir_doc.main_slip:
	# 				self.main_slip_no = ir_doc.main_slip
	# 			# else:
	# 			# 	frappe.msgprint(f"Main Slip is not set in Employee IR {ir_doc.name}.")




	def validate_operation(self):
		customer = frappe.db.get_value(
			"Parent Manufacturing Order", self.manufacturing_order, "customer"
		)

		ignored_department = []
		if customer:
			ignored_department = frappe.db.get_all(
				"Ignore Department For MOP", {"parent": customer}, ["department"]
			)

		ignored_department = [row.department for row in ignored_department]
		if self.operation in ignored_department:
			frappe.throw(_("Customer not requireed this operation"))

		if self.manufacturing_work_order:
			if self.department == 'Computer Aided Designing - GEPL' or self.department =='Computer Aided Manufacturing - GEPL':
				item=frappe.get_doc('Item',self.item_code)
				existing_row = item.custom_cam_weight_detail[0] if item.custom_cam_weight_detail else None
				if existing_row:
					existing_row.cad_numbering_file = self.cad_numbering_file
					existing_row.support_cam_file = self.support_cam_file
					existing_row.platform_wt = self.platform_wt
					existing_row.rpt_wt_issue = self.rpt_wt_issue
					existing_row.rpt_wt_receive = self.rpt_wt_receive
					existing_row.estimated_rpt_wt = self.estimated_rpt_wt
					existing_row.rpt_wt_loss = self.rpt_wt_loss
				else:
					item.append('custom_cam_weight_detail', {
						'cad_numbering_file': self.cad_numbering_file,
						'support_cam_file': self.support_cam_file,
						'platform_wt': self.platform_wt ,
						'rpt_wt_issue':self.rpt_wt_issue,
						'rpt_wt_receive' : self.rpt_wt_receive,
						'estimated_rpt_wt':self.estimated_rpt_wt,
						'rpt_wt_loss':self.rpt_wt_loss
					})
				item.save()

	# def remove_duplicate(self):
	# 	existing_data = {
	# 		"department_source_table": [],
	# 		"department_target_table": [],
	# 		"employee_source_table": [],
	# 		"employee_target_table": [],
	# 	}
	# 	to_remove = []
	# 	for row in existing_data:
	# 		for entry in self.get(row):
	# 			if entry.get("sed_item") and entry.get("sed_item") not in existing_data[row]:
	# 				existing_data[row].append(entry.get("sed_item"))
	# 			elif entry.get("sed_item") in existing_data[row]:
	# 				to_remove.append(entry)

	# 	for row in to_remove:
	# 		self.remove(row)

	def remove_duplicate(self):
		# Use sets for fast lookups
		existing_data = {
			"department_source_table": set(),
			"department_target_table": set(),
			"employee_source_table": set(),
			"employee_target_table": set(),
		}
		to_remove = []

		# Collect unique items and mark duplicates
		for row in existing_data.keys():
			for entry in self.get(row):
				sed_item = entry.get("sed_item")
				if sed_item:
					# Check and add to set directly
					if sed_item in existing_data[row]:
						to_remove.append(entry)
					else:
						existing_data[row].add(sed_item)

		for row in to_remove:
			self.remove(row)

	def on_update(self):
		self.attach_cad_cam_file_into_item_master()  # To set MOP doctype CAD-CAM Attachment's & respective details into Item Master.
		self.set_wop_weight_details()  # To Set WOP doctype Weight details from MOP Doctype.
		self.set_pmo_weight_details_in_bulk()  # To Set PMO doctype Weight details from MOP Doctype.

	def get_previous_se_details(self):
		if self.previous_se_data_updated:
			return

		d_warehouse = None
		e_warehouse = None
		if self.department:
			d_warehouse = frappe.db.get_value(
				"Warehouse", {"disabled": 0, "department": self.department, "warehouse_type": "Manufacturing"}
			)
		if self.employee:
			e_warehouse = frappe.db.get_value(
				"Warehouse", {"disabled": 0, "employee": self.employee, "warehouse_type": "Manufacturing"}
			)

		if self.previous_mop:
			existing_data = {
				"department_source_table": set(),
				"department_target_table": set(),
				"employee_source_table": set(),
				"employee_target_table": set(),
			}

			for row in existing_data:
				for entry in self.get(row):
					if entry.get("sed_item") and entry.get("sed_item") not in existing_data[row]:
						# existing_data[row].append(entry.get("sed_item"))
						existing_data[row].add(entry.get("sed_item"))

			department_source_table = frappe.db.get_all(
				"Department Source Table", {"parent": self.previous_mop, "s_warehouse": d_warehouse}, ["*"]
			)
			department_target_table = frappe.db.get_all(
				"Department Target Table", {"parent": self.previous_mop, "t_warehouse": d_warehouse}, ["*"]
			)
			employee_source_table = frappe.db.get_all(
				"Employee Source Table", {"parent": self.previous_mop, "s_warehouse": e_warehouse}, ["*"]
			)
			employee_target_table = frappe.db.get_all(
				"Employee Target Table", {"parent": self.previous_mop, "t_warehouse": e_warehouse}, ["*"]
			)

			for row in department_source_table:
				if row["sed_item"] not in existing_data["department_source_table"]:
					row["name"] = None
					row["idx"] = None
					self.append("department_source_table", row)

			for row in department_target_table:
				if row["sed_item"] not in existing_data["department_target_table"]:
					row["name"] = None
					row["idx"] = None
					self.append("department_target_table", row)

			for row in employee_source_table:
				if row["sed_item"] not in existing_data["employee_source_table"]:
					row["name"] = None
					row["idx"] = None
					self.append("employee_source_table", row)

			for row in employee_target_table:
				if row["sed_item"] not in existing_data["employee_target_table"]:
					row["name"] = None
					row["idx"] = None
					self.append("employee_target_table", row)

		self.db_set("previous_se_data_updated", 1)

	# timer code
	def validate_time_logs(self):
		self.total_minutes = 0.0
		# self.total_completed_qty = 0.0

		if self.get("time_logs"):
			# d = self.get("time_logs")[-1]
			# print(self)
			for d in self.get("time_logs")[-1:]:
				# print(d)
				if (
					d.to_time
					and get_datetime(d.from_time) > get_datetime(d.to_time)
					and get_datetime(d.from_time) < get_datetime(d.to_time)
				):
					frappe.throw(_("Row {0}: From time must be less than to time").format(d.idx))

				# data = self.get_overlap_for(d)
				# if data:
				# 	frappe.throw(
				# 		_("Row {0}: From Time and To Time of {1} is overlapping with {2}").format(
				# 			d.idx, self.name, data.name
				# 		),
				# 		OverlapError,
				# 	)

				if d.from_time and d.to_time:
					d.time_in_mins = time_diff_in_hours(d.to_time, d.from_time) * 60
					in_hours = time_diff(d.to_time, d.from_time)
					d.time_in_hour = str(in_hours)[:-3]
					for i in self.get("time_logs"):

						self.total_minutes += i.time_in_mins

					default_shift = frappe.db.get_value("Employee", d.employee, "default_shift")
					if default_shift:
						shift_hours = frappe.db.get_value("Shift Type", default_shift, ["start_time", "end_time"])
						total_shift_hours = time_diff(shift_hours[1], shift_hours[0])

						if in_hours >= total_shift_hours:
							d.time_in_days = in_hours / total_shift_hours

		# department timer code
		if self.get("department_time_logs"):
			for d in self.get("department_time_logs")[-1:]:
				if (
					d.department_to_time
					and get_datetime(d.department_from_time) > get_datetime(d.department_to_time)
					and get_datetime(d.department_from_time) < get_datetime(d.department_to_time)
				):
					frappe.throw(_("Row {0}: From time must be less than to time").format(d.idx))

				if d.department_from_time and d.department_to_time:
					d.time_in_mins = time_diff_in_hours(d.department_to_time, d.department_from_time) * 60

					in_hours = time_diff(d.department_to_time, d.department_from_time)
					d.time_in_hour = str(in_hours)[:-3]

					time_diff_hour = time_diff_in_hours(d.department_to_time, d.department_from_time) / 24
					d.time_in_days = str(time_diff_hour)[:6]
					# frappe.throw(f"{d.time_in_mins} ||| {d.time_in_hour}   ||| {str(d.time_in_days)[:6]} ||| {d.time_in_days}")

			# frappe.throw('HOLD')

			# if d.completed_qty and not self.sub_operations:
			# 	self.total_completed_qty += d.completed_qty

			# self.total_completed_qty = flt(self.total_completed_qty, self.precision("total_completed_qty"))

		# for row in self.sub_operations:
		# 	self.total_completed_qty += row.completed_qty

	# timer code
	# def update_corrective_in_work_order(self, wo):
	# 	wo.corrective_operation_cost = 0.0
	# 	for row in frappe.get_all(
	# 		"Job Card",
	# 		fields=["total_time_in_mins", "hour_rate"],
	# 		filters={"is_corrective_job_card": 1, "docstatus": 1, "work_order": self.work_order},
	# 	):
	# 		wo.corrective_operation_cost += flt(row.total_time_in_mins) * flt(row.hour_rate)

	# 	wo.calculate_operating_cost()
	# 	wo.flags.ignore_validate_update_after_submit = True
	# 	wo.save()

	# timer code
	def get_current_operation_data(self):
		return frappe.get_all(
			"Job Card",
			fields=[
				"sum(total_time_in_mins) as time_in_mins",
				"sum(total_completed_qty) as completed_qty",
				"sum(process_loss_qty) as process_loss_qty",
			],
			filters={
				"docstatus": 1,
				"work_order": self.work_order,
				"operation_id": self.operation_id,
				"is_corrective_job_card": 0,
			},
		)

	# timer code
	def get_overlap_for(self, args, check_next_available_slot=False):
		production_capacity = 1

		jc = frappe.qb.DocType("Manufacturing Operation")
		# jctl = frappe.qb.DocType("Job Card Time Log")
		jctl = frappe.qb.DocType("Manufacturing Operation Time Log")

		time_conditions = [
			((jctl.from_time < args.from_time) & (jctl.to_time > args.from_time)),
			((jctl.from_time < args.to_time) & (jctl.to_time > args.to_time)),
			((jctl.from_time >= args.from_time) & (jctl.to_time <= args.to_time)),
		]

		if check_next_available_slot:
			time_conditions.append(((jctl.from_time >= args.from_time) & (jctl.to_time >= args.to_time)))

		query = (
			frappe.qb.from_(jctl)
			.from_(jc)
			.select(jc.name.as_("name"), jctl.from_time, jctl.to_time)
			#    , jc.workstation, jc.workstation_type
			.where(
				(jctl.parent == jc.name)
				& (Criterion.any(time_conditions))
				& (jctl.name != f"{args.name or 'No Name'}")
				& (jc.name != f"{args.parent or 'No Name'}")
				& (jc.docstatus < 2)
			)
			.orderby(jctl.to_time, order=frappe.qb.desc)
		)

		# if self.workstation_type:
		# 	query = query.where(jc.workstation_type == self.workstation_type)

		# if self.workstation:
		# 	production_capacity = (
		# 		frappe.get_cached_value("Workstation", self.workstation, "production_capacity") or 1
		# 	)
		# 	query = query.where(jc.workstation == self.workstation)

		if args.get("employee"):
			# override capacity for employee
			production_capacity = 1
			query = query.where(jctl.employee == args.get("employee"))

		existing = query.run(as_dict=True)
		if not self.has_overlap(production_capacity, existing):
			return {}
		if existing and production_capacity > len(existing):
			return

		# if self.workstation_type:
		# 	if workstation := self.get_workstation_based_on_available_slot(existing):
		# 		self.workstation = workstation
		# 		return None

		return existing[0] if existing else None

	def has_overlap(self, production_capacity, time_logs):
		overlap = False
		if production_capacity == 1 and len(time_logs) >= 1:
			return True
		if not len(time_logs):
			return False

		# sorting overlapping job cards as per from_time
		time_logs = sorted(time_logs, key=lambda x: x.get("from_time"))
		# alloted_capacity has key number starting from 1. Key number will increment by 1 if non sequential job card found
		# if key number reaches/crosses to production_capacity means capacity is full and overlap error generated
		# this will store last to_time of sequential job cards
		alloted_capacity = {1: time_logs[0]["to_time"]}
		# flag for sequential Job card found
		sequential_job_card_found = False
		for i in range(1, len(time_logs)):
			# scanning for all Existing keys
			for key in alloted_capacity.keys():
				# if current Job Card from time is greater than last to_time in that key means these job card are sequential
				if alloted_capacity[key] <= time_logs[i]["from_time"]:
					# So update key's value with last to_time
					alloted_capacity[key] = time_logs[i]["to_time"]
					# flag is true as we get sequential Job Card for that key
					sequential_job_card_found = True
					# Immediately break so that job card to time is not added with any other key except this
					break
			# if sequential job card not found above means it is overlapping  so increment key number to alloted_capacity
			if not sequential_job_card_found:
				# increment key number
				key = key + 1
				# for that key last to time is assigned.
				alloted_capacity[key] = time_logs[i]["to_time"]
		if len(alloted_capacity) >= production_capacity:
			# if number of keys greater or equal to production caoacity means full capacity is utilized and we should throw overlap error
			return True
		return overlap

	def update_weights(self):
		res = get_material_wt(self)
		self.update(res)

	def validate_loss(self):
		if self.is_new() or not self.loss_details:
			return
		items = get_stock_entries_against_mfg_operation(self)
		for row in self.loss_details:
			if row.item_code not in items.keys():
				frappe.throw(_("Row #{0}: Invalid item for loss").format(row.idx), title=_("Loss Details"))
			if row.stock_uom != items[row.item_code].get("uom"):
				# frappe.throw(
				# 	_(f"Row #{row.idx}: UOM should be {items[row.item_code].get('uom')}"), title="Loss Details"
				# )
				frappe.throw(
					_("Row #{0}: UOM should be {1}").format(row.idx, items[row.item_code].get("uom")),
					title=_("Loss Details"),
				)
			if row.stock_qty > items[row.item_code].get("qty", 0):
				# frappe.throw(
				# 	_(f"Row #{row.idx}: qty cannot be greater than {items[row.item_code].get('qty',0)}"),
				# 	title="Loss Details",
				# )
				frappe.throw(
					_("Row #{0}: qty cannot be greater than {1}").format(
						row.idx, items[row.item_code].get("qty", 0)
					),
					title=_("Loss Details"),
				)

	def set_start_finish_time(self):
		if self.has_value_changed("status"):
			if self.status == "WIP" and not self.start_time and self.time_logs:
				self.start_time = self.time_logs[0].from_time
			elif not self.department_starttime and self.department_starttime:
				self.department_starttime = self.department_time_logs[0].department_from_time
			elif self.status == "Finished":
				if not self.start_time and self.time_logs:
					self.start_time = self.time_logs[0].from_time
				if self.time_logs:
					self.finish_time = self.time_logs[-1].to_time
					# self.time_taken = time_diff(self.finish_time, self.start_time)

			# elif self.status == "WIP" and not self.department_starttime:
			# 	if self.department_time_logs:
			# elif self.status == "Finished":
			# 	if not self.department_starttime and self.department_time_logs:
			# 		self.department_starttime = self.department_time_logs[0].department_from_time
			# 	if self.department_time_logs:
			# 		self.department_finishtime = self.department_time_logs[-1].department_to_time
			# 		self.time_taken = time_diff(self.department_finishtime, self.department_starttime)

	def attach_cad_cam_file_into_item_master(self):
		# self.ref_name = self.name
		existing_child = self.get_existing_child("Item", self.item_code, "Cam Weight Detail", self.name)

		# record_filter_from_mnf_setting = frappe.get_all(
		# 	"CAM Weight Details Mapping",
		# 	filters={"parent": self.company, "parenttype": "Manufacturing Setting"},
		# 	fields=["operation"],
		# )

		record_filter_from_mnf_setting = frappe.get_all(
			"CAM Weight Details Mapping",
			filters={"parent": self.manufacturer, "parenttype": "Manufacturing Setting"},
			fields=["operation"],
		)
		if existing_child:
			# Update the existing row
			existing_child.update(
				{
					"cad_numbering_file": self.cad_numbering_file,
					"support_cam_file": self.support_cam_file,
					"mop_series": self.name,
					"platform_wt": self.platform_wt,
					"rpt_wt_issue": self.rpt_wt_issue,
					"rpt_wt_receive": self.rpt_wt_receive,
					"rpt_wt_loss": self.rpt_wt_loss,
					"estimated_rpt_wt": self.estimated_rpt_wt,
				}
			)
			existing_child.save()
		else:
			# Create a new child record
			filter_record = [row.get("operation") for row in record_filter_from_mnf_setting]
			if self.operation in filter_record:
				self.add_child_record(
					"Item",
					self.item_code,
					"Cam Weight Detail",
					{
						"cad_numbering_file": self.cad_numbering_file,
						"support_cam_file": self.support_cam_file,
						"mop_reference": self.name,
						"mop_series": self.name,
						"platform_wt": self.platform_wt,
						"rpt_wt_issue": self.rpt_wt_issue,
						"rpt_wt_receive": self.rpt_wt_receive,
						"rpt_wt_loss": self.rpt_wt_loss,
						"estimated_rpt_wt": self.estimated_rpt_wt,
					},
				)

	def get_existing_child(self, parent_doctype, parent_name, child_doctype, mop_reference):
		# Check if the child record already exists
		existing_child = frappe.get_all(
			child_doctype,
			filters={
				"parent": parent_name,
				"parenttype": parent_doctype,
				"mop_reference": mop_reference,
				"mop_series": self.ref_name,
			},
			fields=["name"],
		)
		if existing_child:
			return frappe.get_doc(child_doctype, existing_child[0]["name"])
		else:
			return None

	def add_child_record(self, parent_doctype, parent_name, child_doctype, child_fields):
		# Create a new child document
		child_doc = frappe.get_doc(
			{
				"doctype": child_doctype,
				"parent": parent_name,
				"parenttype": parent_doctype,
				"parentfield": "custom_cam_weight_detail",
			}
		)
		# Set values for the child document fields
		for fieldname, value in child_fields.items():
			child_doc.set(fieldname, value)
		# Save the child document
		child_doc.insert()

	@frappe.whitelist()
	def create_fg(self):
		se_name = create_manufacturing_entry(self)
		pmo = frappe.db.get_value(
			"Manufacturing Work Order", self.manufacturing_work_order, "manufacturing_order"
		)
		wo = frappe.get_all("Manufacturing Work Order", {"manufacturing_order": pmo}, pluck="name")
		set_values_in_bulk("Manufacturing Work Order", wo, {"status": "Completed"})
		create_finished_goods_bom(self, se_name)

	@frappe.whitelist()
	def get_stock_entry(self):
		StockEntry = frappe.qb.DocType("Stock Entry")
		# StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")
		StockEntryMopItem = frappe.qb.DocType("Stock Entry Detail")

		data = (
			frappe.qb.from_(StockEntryMopItem)
			.left_join(StockEntry)
			.on(StockEntryMopItem.parent == StockEntry.name)
			.select(
				StockEntry.manufacturing_work_order,
				StockEntry.manufacturing_operation,
				StockEntry.department,
				StockEntry.to_department,
				StockEntry.employee,
				StockEntry.stock_entry_type,
				StockEntryMopItem.parent,
				StockEntryMopItem.item_code,
				StockEntryMopItem.item_name,
				StockEntryMopItem.qty,
				StockEntryMopItem.uom,
			)
			.where((StockEntry.docstatus == 1) & (StockEntryMopItem.manufacturing_operation == self.name))
			.orderby(StockEntry.modified, order=frappe.qb.desc)
		).run(as_dict=True)

		total_qty = len([item["qty"] for item in data])
		return frappe.render_template(
			"jewellery_erpnext/jewellery_erpnext/doctype/manufacturing_operation/stock_entry.html",
			{"data": data, "total_qty": total_qty},
		)

	@frappe.whitelist()
	def get_stock_summary(self):
		StockEntry = frappe.qb.DocType("Stock Entry")
		# StockEntryDetail = frappeqb.DocType("Stock Entry Detail")
		StockEntryMopItem = frappe.qb.DocType("Stock Entry Detail")

		# Subquery for max modified stock entry per manufacturing operation
		max_se_subquery = (
			frappe.qb.from_(StockEntry)
			.select(Max(StockEntry.modified).as_("max_modified"), StockEntry.manufacturing_operation)
			.where(StockEntry.docstatus == 1)
			.groupby(StockEntry.manufacturing_operation)
		).as_("max_se")

		# Main query
		data = (
			frappe.qb.from_(StockEntryMopItem)
			.left_join(max_se_subquery)
			.on(StockEntryMopItem.manufacturing_operation == max_se_subquery.manufacturing_operation)
			.left_join(StockEntry)
			.on(
				(StockEntryMopItem.parent == StockEntry.name)
				& (StockEntry.modified == max_se_subquery.max_modified)
			)
			.select(
				StockEntry.manufacturing_work_order,
				StockEntry.manufacturing_operation,
				StockEntryMopItem.parent,
				StockEntryMopItem.item_code,
				StockEntryMopItem.item_name,
				StockEntryMopItem.inventory_type,
				StockEntryMopItem.pcs,
				StockEntryMopItem.batch_no,
				StockEntryMopItem.qty,
				StockEntryMopItem.uom,
			)
			.where(
				(StockEntry.docstatus == 1) & (StockEntryMopItem.manufacturing_operation == self.name)
			)
		).run(as_dict=True)

		total_qty = 0
		for row in data:
			if row.uom == "Carat":
				total_qty += row.get("qty", 0) * 0.2
			else:
				total_qty += row.get("qty", 0)
		total_qty = round(total_qty, 4)
		return frappe.render_template(
			"jewellery_erpnext/jewellery_erpnext/doctype/manufacturing_operation/stock_summery.html",
			{"data": data, "total_qty": total_qty},
		)

	def set_wop_weight_details(self):
		get_wop_weight = frappe.db.get_value(
			"Manufacturing Operation",
			{"manufacturing_work_order": self.manufacturing_work_order, "status": ["!=", "Not Started"]},
			[
				"gross_wt",
				"net_wt",
				"diamond_wt",
				"gemstone_wt",
				"finding_wt",
				"other_wt",
				"received_gross_wt",
				"received_net_wt",
				"loss_wt",
				"diamond_wt_in_gram",
				"diamond_pcs",
				"gemstone_pcs",
			],
			order_by="modified DESC",
			as_dict=1,
		)
		if get_wop_weight is None:
			return
		else:
			frappe.db.set_value(
				"Manufacturing Work Order",
				self.manufacturing_work_order,
				{
					"gross_wt": get_wop_weight.gross_wt,
					"net_wt": get_wop_weight.net_wt,
					"finding_wt": get_wop_weight.finding_wt,
					"diamond_wt": get_wop_weight.diamond_wt,
					"gemstone_wt": get_wop_weight.gemstone_wt,
					"other_wt": get_wop_weight.other_wt,
					"received_gross_wt": get_wop_weight.received_gross_wt,
					"received_net_wt": get_wop_weight.received_net_wt,
					"loss_wt": get_wop_weight.loss_wt,
					"diamond_wt_in_gram": get_wop_weight.diamond_wt_in_gram,
					"diamond_pcs": get_wop_weight.diamond_pcs,
					"gemstone_pcs": get_wop_weight.gemstone_pcs,
				},
				update_modified=False,
			)
			# frappe.throw(str(get_wop_weight))

	def set_pmo_weight_details(self):
		ManufacturingWorkOrder = frappe.qb.DocType("Manufacturing Work Order")

		get_mwo_weight = (
			frappe.qb.from_(ManufacturingWorkOrder)
			.select(
				Sum(ManufacturingWorkOrder.gross_wt).as_("gross_wt"),
				Sum(ManufacturingWorkOrder.net_wt).as_("net_wt"),
				Sum(ManufacturingWorkOrder.finding_wt).as_("finding_wt"),
				Sum(ManufacturingWorkOrder.diamond_wt).as_("diamond_wt"),
				Sum(ManufacturingWorkOrder.gemstone_wt).as_("gemstone_wt"),
				Sum(ManufacturingWorkOrder.other_wt).as_("other_wt"),
				Sum(ManufacturingWorkOrder.received_gross_wt).as_("received_gross_wt"),
				Sum(ManufacturingWorkOrder.received_net_wt).as_("received_net_wt"),
				Sum(ManufacturingWorkOrder.loss_wt).as_("loss_wt"),
				Sum(ManufacturingWorkOrder.diamond_wt_in_gram).as_("diamond_wt_in_gram"),
				Sum(ManufacturingWorkOrder.diamond_pcs).as_("diamond_pcs"),
				Sum(ManufacturingWorkOrder.gemstone_pcs).as_("gemstone_pcs"),
			)
			.where(
				(ManufacturingWorkOrder.manufacturing_order == self.manufacturing_order)
				& (ManufacturingWorkOrder.docstatus == 1)
			)
		).run(as_dict=True)

		if get_mwo_weight is None:
			return
		else:
			# Have to Check this
			frappe.db.set_value(
				"Parent Manufacturing Order",
				self.manufacturing_order,
				{
					"gross_weight": get_mwo_weight[0].gross_wt or 0,
					"net_weight": get_mwo_weight[0].net_wt or 0,
					"diamond_weight": get_mwo_weight[0].diamond_wt or 0,
					"gemstone_weight": get_mwo_weight[0].gemstone_wt or 0,
					"finding_weight": get_mwo_weight[0].finding_wt or 0,
					"other_weight": get_mwo_weight[0].other_wt or 0,
				},
				update_modified=False,
			)

			# To Set Product WT on PMO Tolerance METAL/Diamond/Gemstone Table.
			docname = self.manufacturing_order
			metal_product_tolerance_list = frappe.db.get_all(
				"Metal Product Tolerance", {"parent": docname}, pluck="name"
			)
			for mpt_name in metal_product_tolerance_list:
				frappe.db.set_value(
					"Metal Product Tolerance",
					mpt_name,
					"product_wt",
					get_mwo_weight[0].gross_wt or get_mwo_weight[0].net_wt or 0,
				)

			diamond_product_tolerance_list = frappe.db.get_all(
				"Diamond Product Tolerance", {"parent": docname}, pluck="name"
			)
			for dpt_name in diamond_product_tolerance_list:
				frappe.db.set_value(
					"Diamond Product Tolerance", dpt_name, "product_wt", get_mwo_weight[0].diamond_wt or 0
				)

			gemstone_product_tolerance_list = frappe.db.get_all(
				"Gemstone Product Tolerance", {"parent": docname}, pluck="name"
			)
			for gpt_name in gemstone_product_tolerance_list:
				frappe.db.set_value(
					"Gemstone Product Tolerance", gpt_name, "product_wt", get_mwo_weight[0].gemstone_wt or 0
				)

	def set_pmo_weight_details_in_bulk(self):
		ManufacturingWorkOrder = frappe.qb.DocType("Manufacturing Work Order")

		# Step 1: Aggregate MWO weights
		get_mwo_weight = (
			frappe.qb.from_(ManufacturingWorkOrder)
			.select(
				Sum(ManufacturingWorkOrder.gross_wt).as_("gross_wt"),
				Sum(ManufacturingWorkOrder.net_wt).as_("net_wt"),
				Sum(ManufacturingWorkOrder.finding_wt).as_("finding_wt"),
				Sum(ManufacturingWorkOrder.diamond_wt).as_("diamond_wt"),
				Sum(ManufacturingWorkOrder.gemstone_wt).as_("gemstone_wt"),
				Sum(ManufacturingWorkOrder.other_wt).as_("other_wt"),
				Sum(ManufacturingWorkOrder.received_gross_wt).as_("received_gross_wt"),
				Sum(ManufacturingWorkOrder.received_net_wt).as_("received_net_wt"),
				Sum(ManufacturingWorkOrder.loss_wt).as_("loss_wt"),
				Sum(ManufacturingWorkOrder.diamond_wt_in_gram).as_("diamond_wt_in_gram"),
				Sum(ManufacturingWorkOrder.diamond_pcs).as_("diamond_pcs"),
				Sum(ManufacturingWorkOrder.gemstone_pcs).as_("gemstone_pcs"),
			)
			.where(
				(ManufacturingWorkOrder.manufacturing_order == self.manufacturing_order)
				& (ManufacturingWorkOrder.docstatus == 1)
			)
		).run(as_dict=True)

		if not get_mwo_weight:
			return

		data = get_mwo_weight[0]

		# Step 2: Update Parent Manufacturing Order
		frappe.db.set_value(
			"Parent Manufacturing Order",
			self.manufacturing_order,
			{
				"gross_weight": data.gross_wt or 0,
				"net_weight": data.net_wt or 0,
				"diamond_weight": data.diamond_wt or 0,
				"gemstone_weight": data.gemstone_wt or 0,
				"finding_weight": data.finding_wt or 0,
				"other_weight": data.other_wt or 0,
			},
			update_modified=False,
		)

		# Step 3: Dynamic bulk update of tolerance tables
		docname = self.manufacturing_order

		tolerance_map = {
			"Metal Product Tolerance": data.gross_wt or data.net_wt or 0,
			"Diamond Product Tolerance": data.diamond_wt or 0,
			"Gemstone Product Tolerance": data.gemstone_wt or 0,
		}

		for doctype, product_wt in tolerance_map.items():
			docnames = frappe.db.get_all(doctype, {"parent": docname}, pluck="name")
			updates = {name: {"product_wt": product_wt} for name in docnames}

			if updates:
				frappe.db.bulk_update(
					doctype=doctype,
					doc_updates=updates,
					update_modified=False
				)



	def set_mop_balance_table(self):
		self.mop_balance_table = []
		added_item_codes = set()
		final_balance_row = []
		bal_qty = {}
		bal_pcs = {}
		existing_data = {}
		row_dict = {}
		# Calculate sum of quantities for department source table
		# for row in self.department_source_table + self.employee_source_table:
		# 	key = (row.item_code, row.batch_no)
		# 	bal_qty[key] = bal_qty.get(key, 0) + row.qty
		# 	if not row_dict.get(key):
		# 		row_dict[key] = row.__dict__.copy()
		# if not bal_qty[key].get("row_data"):
		# 	bal_qty[key]["row_data"] = row.__dict__.copy()
		# Calculate sum of quantities for employee source table
		# for row in :
		# 	key = (row.item_code, row.batch_no)
		# 	bal_qty[key] = bal_qty.get(key, 0) + row.qty
		# 	if not row_dict.get(key):
		# 		row_dict[key] = row.__dict__.copy()
		# if not bal_qty[key].get("row_data"):
		# 	bal_qty[key]["row_data"] = row.__dict__.copy()
		# Subtract sum of quantities for department target table
		# for row in self.department_target_table + self.employee_target_table:
		# 	key = (row.item_code, row.batch_no)
		# 	bal_qty[key] = bal_qty.get(key, 0) - row.qty
		# 	if not row_dict.get(key):
		# 		row_dict[key] = row.__dict__.copy()
		# 	# if not bal_qty[key].get("row_data"):
		# 	# 	bal_qty[key]["row_data"] = row.__dict__.copy()
		# # Subtract sum of quantities for employee target table
		# # for row in self.employee_target_table:
		# # 	key = (row.item_code, row.batch_no)
		# # 	bal_qty[key] = bal_qty.get(key, 0) - row.qty
		# # 	if not row_dict.get(key):
		# # 		row_dict[key] = row.__dict__.copy()
		# # if not bal_qty[key].get("row_data"):
		# # 	bal_qty[key]["row_data"] = row.__dict__.copy()

		# for row_balance in self.mop_balance_table:
		# 	key = (row_balance.item_code, row_balance.batch_no)
		# 	if not bal_qty.get(key):
		# 		return

		# 	if row_balance.qty != bal_qty[key]["qty"]:
		# 		row_balance.qty = bal_qty[key]["qty"]
		# 		existing_data[key] = True

		for row in self.department_source_table:
			bal_qty[(row.item_code, row.batch_no)] = bal_qty.get((row.item_code, row.batch_no), 0) + row.qty
			bal_pcs[(row.item_code, row.batch_no)] = bal_pcs.get((row.item_code, row.batch_no), 0) + (flt(row.pcs) if row.pcs else 0)
		# Calculate sum of quantities for employee source table
		for row in self.employee_source_table:
			bal_qty[(row.item_code, row.batch_no)] = bal_qty.get((row.item_code, row.batch_no), 0) + row.qty
			bal_pcs[(row.item_code, row.batch_no)] = bal_pcs.get((row.item_code, row.batch_no), 0) + (flt(row.pcs) if row.pcs else 0)
		# Subtract sum of quantities for department target table
		for row in self.department_target_table:
			bal_qty[(row.item_code, row.batch_no)] = bal_qty.get((row.item_code, row.batch_no), 0) - row.qty
			bal_pcs[(row.item_code, row.batch_no)] = bal_pcs.get((row.item_code, row.batch_no), 0) - flt(row.pcs or 0)
		# Subtract sum of quantities for employee target table
		for row in self.employee_target_table:
			bal_qty[(row.item_code, row.batch_no)] = bal_qty.get((row.item_code, row.batch_no), 0) - row.qty
			bal_pcs[(row.item_code, row.batch_no)] = bal_pcs.get((row.item_code, row.batch_no), 0) - flt(row.pcs or 0)

		# for key in bal_qty:
		# 	if bal_qty[key] != 0 and not existing_data.get(key):
		# 		row_data = row_dict.get(key)
		# 		# if row_data is None and not self.employee_target_table:
		# 		# if self.department_target_table:
		# 		# 	for row_dtt in self.department_target_table:
		# 		# 		if row_dtt.item_code == key[0] and row_dtt.batch_no == key[1]:
		# 		# 			row_data = row_dtt.__dict__.copy()
		# 		# 			break
		# 		# if self.employee_target_table:
		# 		# 	for row_ett in self.employee_target_table:
		# 		# 		if row_ett.item_code == key[0] and row_ett.batch_no == key[1]:
		# 		# 			row_data = row_ett.__dict__.copy()
		# 		# 			break
		# 		if row_data:
		# 			row_data["qty"] = abs(bal_qty[key])
		# 			row_data["name"] = None
		# 			row_data["idx"] = None
		# 			row_data["parentfield"] = None
		# 			row_data["s_warehouse"] = row_data["t_warehouse"] or row_data["s_warehouse"]
		# 			row_data["t_warehouse"] = None
		# 			row_data["batch_no"] = key[1]
		# 			final_balance_row.append(row_data)

		# # To check Item_code already added or not balance table
		# # for row_balance in self.mop_balance_table:
		# # 	added_item_codes.add(row_balance.item_code)
		# # frappe.throw(f"{final_balance_row}")
		# # Append Final result into Balance Table
		# for row in final_balance_row:
		# 	# if row.get("item_code") not in added_item_codes:
		# 	self.append("mop_balance_table", row)
		for key in bal_qty:
			if bal_qty[key] != 0:
				row_data = None
				# if row_data is None and not self.employee_target_table:
				if self.department_target_table:
					for row_dtt in self.department_target_table:
						if row_dtt.item_code == key[0] and row_dtt.batch_no == key[1]:
							row_data = row_dtt.__dict__.copy()
							break
				if self.employee_target_table:
					for row_ett in self.employee_target_table:
						if row_ett.item_code == key[0] and row_ett.batch_no == key[1]:
							row_data = row_ett.__dict__.copy()
							break
				if row_data:
					row_data["qty"] = abs(bal_qty[key])
					row_data["name"] = None
					row_data["idx"] = None
					row_data["parentfield"] = None
					row_data["s_warehouse"] = row_data["t_warehouse"] or row_data["s_warehouse"]
					row_data["t_warehouse"] = None
					row_data["batch_no"] = key[1]

					# if frappe.flags.update_pcs:
					row_data["pcs"] = abs(bal_pcs.get(key))

					final_balance_row.append(row_data)

		# To check Item_code already added or not balance table
		for row_balance in self.mop_balance_table:
			added_item_codes.add(row_balance.item_code)
		# frappe.throw(f"{final_balance_row}")
		# Append Final result into Balance Table
		for row in final_balance_row:
			if row.get("item_code") not in added_item_codes:
				if not row.get("qty") > 0:
					continue

				self.append("mop_balance_table", row)

		# if frappe.db.exists("Manufacturing Operation", {'previous_mop': self.name}):
		# 	new_mop = frappe.db.get_value("Manufacturing Operation", {'previous_mop': self.name}, "name")
		# 	new_mop_doc = frappe.get_doc("Manufacturing Operation", new_mop)
		# 	update_new_mop(new_mop_doc, self)
		# 	new_mop_doc.save()


def create_manufacturing_entry(doc, row_data, mo_data=None):
	if mo_data is None:
		mo_data = []

	target_wh = frappe.db.get_value(
		"Warehouse", {"disabled": 0, "department": doc.department, "warehouse_type": "Manufacturing"}
	)
	# to_wh = frappe.db.get_value(
	# 	"Manufacturing Setting", {"company": doc.company}, "default_fg_warehouse"
	# )
	to_wh = frappe.db.get_value(
		"Manufacturing Setting", {"manufacturer": doc.manufacturer}, "default_fg_warehouse"
	)
	if not to_wh:
		frappe.throw(_("<b>Manufacturing Setting</b> Default FG Warehouse Missing...!"))
	pmo = frappe.db.get_value(
		"Manufacturing Work Order", doc.manufacturing_work_order, "manufacturing_order"
	)
	pmo_det = frappe.db.get_value(
		"Parent Manufacturing Order",
		pmo,
		[
			"name",
			"sales_order_item",
			"manufacturing_plan",
			"item_code",
			"qty",
			"new_item",
			"serial_no",
			"repair_type",
			"product_type",
		],
		as_dict=1,
	)
	if not pmo_det.qty:
		frappe.throw(f"{pmo_det.name} : Have {pmo_det.qty} Cannot Create Stock Entry")

	get_item_doc = frappe.get_doc("Item", pmo_det.item_code)
	if get_item_doc.has_serial_no == 0:
		frappe.throw(f"The Item {pmo_det.name} does not have Serial No plese check item master")

	finish_other_tagging_operations(doc, pmo)

	finish_item = pmo_det.get("item_code")

	doc.serial_no = pmo_det.get("serial_no")
	doc.new_item = pmo_det.get("new_item")
	if pmo_det.get("repair_type") != "Refresh & Replace Defective Material" and pmo_det.get(
		"new_item"
	):
		finish_item = pmo_det.get("new_item")

	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"purpose": "Manufacture",
			"manufacturing_order": pmo,
			"stock_entry_type": "Manufacture",
			"department": doc.department,
			"to_department": doc.department,
			"manufacturing_work_order": doc.manufacturing_work_order,
			"manufacturing_operation": doc.manufacturing_operation,
			"custom_serial_number_creator": doc.name,
			# "inventory_type": "Regular Stock",
			"auto_created": 1,
		}
	)
	diamond_grade_data = frappe._dict()
	for entry in row_data:
		if diamond_grade := frappe.db.get_value(
			"Item Variant Attribute",
			{"parent": entry["item_code"], "attribute": "Diamond Grade"},
			"attribute_value",
		):
			diamond_grade_data.setdefault(diamond_grade, 0)
			diamond_grade_data[diamond_grade] += entry["qty"]
		se.append(
			"items",
			{
				"item_code": entry["item_code"],
				"qty": entry["qty"],
				"uom": entry["uom"],
				"batch_no": entry.get("batch_no"),
				"inventory_type": entry.get("inventory_type"),
				"customer": entry.get("customer"),
				"custom_sub_setting_type": entry.get("sub_setting_type"),
				"manufacturing_operation": doc.manufacturing_operation,
				"department": doc.department,
				"pcs": entry.get("pcs"),
				"use_serial_batch_fields": 1,
				"to_department": doc.department,
				"s_warehouse": target_wh,
			},
		)
	sr_no = ""
	compose_series = genrate_serial_no(doc, diamond_grade_data)
	sr_no = make_autoname(compose_series)
	new_bom_serial_no = sr_no
	# serial_no_pass_entry(doc,sr_no,to_wh,pmo_det)
	se.append(
		"items",
		{
			"item_code": finish_item,
			"qty": 1,
			"t_warehouse": to_wh,
			"department": doc.department,
			"to_department": doc.department,
			"inventory_type": "Regular Stock",
			"manufacturing_operation": doc.manufacturing_operation,
			"use_serial_batch_fields": 1,
			"serial_no": sr_no,
			"is_finished_item": 1,
		},
	)

	expense_account = frappe.db.get_value("Company", doc.company, "default_operating_cost_account")

	po_data = frappe.db.get_all(
		"Purchase Order Item",
		{"custom_pmo": doc.parent_manufacturing_order, "docstatus": 1},
		["name", "parent"],
	)

	for row in po_data:
		if not frappe.db.get_value("Purchase Invoice Item", {"po_detail": row.name}):
			frappe.throw(_("Purchase Invoice is created for {0}").format(row.parent))

	pi_data = frappe.db.get_all(
		"Purchase Invoice Item",
		{"custom_pmo": doc.parent_manufacturing_order, "docstatus": 1},
		["base_rate", "parent"],
	)

	pi_expense = 0
	pi_description = []
	for row in pi_data:
		pi_expense += row.base_rate
		if row.parent not in pi_description:
			pi_description.append(row.parent)

	if not expense_account:
		frappe.throw(_("Default Operating Cost account is not mentioned in Company."))

	# for row in mo_data:
	# 	se.append(
	# 		"additional_costs",
	# 		{
	# 			"expense_account": row.expense_account,
	# 			"amount": row.amount,
	# 			"description": row.description,
	# 			"exchange_rate": row.exchange_rate,
	# 			"custom_manufacturing_operation": row.manufacturing_operation,
	# 			"custom_workstation": row.workstation,
	# 			"custom_time_in_minutes": row.total_minutes,
	# 		},
	# 	)
	# if pi_expense > 0:
	# 	se.append(
	# 		"additional_costs",
	# 		{
	# 			"expense_account": expense_account,
	# 			"amount": pi_expense,
	# 			"description": ", ".join(pi_description),
	# 		},
	# 	)
	total_operating_cost = 0
	operation_descriptions = []
	for row in mo_data:
		mop_doc = frappe.get_doc("Manufacturing Operation", row.manufacturing_operation)
		employee = mop_doc.employee

		total_minutes = sum([log.time_in_mins or 0 for log in mop_doc.time_logs])

		matching_workstation = frappe.get_all(
			"Workstation",
			filters={"employee": employee},
			fields=["name", "hour_rate"]
		)

		if matching_workstation:
			ws = matching_workstation[0]
			hour_rate = ws.hour_rate or 0
		else:
			hour_rate = 0
			frappe.msgprint(f"No workstation found for employee: {employee}")

		operating_cost = (hour_rate / 60) * total_minutes
		total_operating_cost += operating_cost
		operation_descriptions.append(row.description or row.manufacturing_operation)

	if total_operating_cost > 0:
		se.append(
			"additional_costs",
			{
				"expense_account": expense_account,
				"amount": total_operating_cost,
				"description": ", ".join(operation_descriptions),
			}
		)
	# End of updated operation cost logic

	if pi_expense > 0:
		se.append(
			"additional_costs",
			{
				"expense_account": expense_account,
				"amount": pi_expense,
				"description": ", ".join(pi_description),
			},
		)

	se.save()
	se.submit()
	update_produced_qty(pmo_det)
	frappe.msgprint(_("Finished Good created successfully"))
	frappe.db.set_value("Serial No", sr_no, "custom_product_type", pmo_det.get("product_type"))
	frappe.db.set_value("Serial No", sr_no, "custom_repair_type", pmo_det.get("repair_type"))
	if doc.for_fg:
		for row in doc.fg_details:
			for entry in row_data:
				if row.id == entry["id"] and row.row_material == entry["item_code"]:
					row.serial_no = get_serial_no(new_bom_serial_no)

	return new_bom_serial_no


def genrate_serial_no(doc, diamond_grade_data):
	errors = []
	mwo_no = doc.manufacturing_work_order
	if mwo_no:
		# series_start = frappe.db.get_value("Manufacturing Setting", doc.company, ["series_start"])
		series_start = frappe.db.get_value("Manufacturing Setting", {"manufacturer":doc.manufacturer}, ["series_start"])
		metal_type, manufacturer, posting_date = frappe.db.get_value(
			"Manufacturing Work Order",
			mwo_no,
			["metal_type", "manufacturer", "posting_date"],
		)
		m_abbr = frappe.db.get_value("Attribute Value", metal_type, "abbreviation")
		mnf_abbr = frappe.db.get_value("Manufacturer", manufacturer, ["custom_abbreviation"])
		if diamond_grade_data:
			diamond_grade = max(diamond_grade_data, key=diamond_grade_data.get)
			dg_abbr = frappe.db.get_value("Attribute Value", diamond_grade, ["abbreviation"])
		else:
			dg_abbr = '0'
		#diamond_grade = max(diamond_grade_data, key=diamond_grade_data.get)
		#dg_abbr = frappe.db.get_value("Attribute Value", diamond_grade, ["abbreviation"])
		date = f"{posting_date.year %100:02d}"
		date_to_letter = {0: "J", 1: "A", 2: "B", 3: "C", 4: "D", 5: "E", 6: "F", 7: "G", 8: "H", 9: "I"}
		final_date = date[0] + date_to_letter[int(date[1])]
		if not series_start:
			errors.append(
				f"Please set value <b>Series Start</b> on Manufacturing Setting for <strong>{doc.company}</strong>"
			)
		if not mnf_abbr:
			errors.append(
				f"Please set value <b>Abbreviation</b> on Manufacturer doctype for <strong>{doc.company}</strong>"
			)
		if not dg_abbr:
			errors.append(
				f"Please set value <b>Abbreviation</b> on Attribute Value doctype respective Diamond Grade:<b>{diamond_grade}</b>"
			)
		if not m_abbr:
			errors.append(
				f"Please set value <b>Abbreviation</b> on Attribute Value doctype respective Metal Type:<b>{diamond_grade}</b>"
			)
	if errors:
		frappe.throw("<br>".join(errors))

	compose_series = str(series_start + mnf_abbr + m_abbr + dg_abbr + final_date + ".####")
	return compose_series


def serial_no_pass_entry(doc, sr_no, to_wh, pmo_det):
	serial_nos_details = []
	serial_nos_details.append(
		(
			sr_no,
			sr_no,
			now(),
			now(),
			frappe.session.user,
			frappe.session.user,
			to_wh,
			doc.company,
			pmo_det.item_code,
			# self.item_name,
			# self.description,
			"Active",
			# self.batch_no,
		)
	)

	if serial_nos_details:
		fields = [
			"name",
			"serial_no",
			"creation",
			"modified",
			"owner",
			"modified_by",
			"warehouse",
			"company",
			"item_code",
			# "item_name",
			# "description",
			"status",
			# "batch_no",
		]

		frappe.db.bulk_insert("Serial No", fields=fields, values=set(serial_nos_details))


def update_produced_qty(pmo_det, cancel=False):
	qty = pmo_det.qty * (-1 if cancel else 1)
	if docname := frappe.db.exists(
		"Manufacturing Plan Table",
		{"docname": pmo_det.sales_order_item, "parent": pmo_det.manufacturing_plan},
	):
		update_existing("Manufacturing Plan Table", docname, {"produced_qty": f"produced_qty + {qty}"})
		update_existing(
			"Manufacturing Plan",
			pmo_det.manufacturing_plan,
			{"total_produced_qty": f"total_produced_qty + {qty}"},
		)


def get_stock_entries_against_mfg_operation(doc):
	if isinstance(doc, str):
		doc = frappe.get_doc("Manufacturing Operation", doc)
	wh = frappe.db.get_value(
		"Warehouse",
		{"disabled": 0, "department": doc.department, "warehouse_type": "Manufacturing"},
		"name",
	)
	if doc.employee:
		wh = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"company": doc.company,
				"employee": doc.employee,
				"warehouse_type": "Manufacturing",
			},
			"name",
		)
	if doc.for_subcontracting and doc.subcontractor:
		wh = frappe.db.get_value(
			"Warehouse", {"disabled": 0, "company": doc.company, "subcontractor": doc.subcontractor}, "name"
		)
	sed = frappe.db.get_all(
		"Stock Entry Detail",
		filters={"t_warehouse": wh, "manufacturing_operation": doc.name, "docstatus": 1},
		fields=["item_code", "qty", "uom"],
	)
	items = {}
	for row in sed:
		existing = items.get(row.item_code)
		if existing:
			qty = existing.get("qty", 0) + row.qty
		else:
			qty = row.qty
		items[row.item_code] = {"qty": qty, "uom": row.uom}
	return items


def get_loss_details(docname):
	data = frappe.get_all(
		"Operation Loss Details",
		{"parent": docname},
		["item_code", "stock_qty as qty", "stock_uom as uom"],
	)
	items = {}
	total_loss = 0
	for row in data:
		existing = items.get(row.item_code)
		if existing:
			qty = existing.get("qty", 0) + row.qty
		else:
			qty = row.qty
		total_loss += row.qty * 0.2 if row.uom == "Carat" else row.qty
		items[row.item_code] = {"qty": qty, "uom": row.uom}
	items["total_loss"] = total_loss
	return items


def get_previous_operation(manufacturing_operation):
	mfg_operation = frappe.db.get_value(
		"Manufacturing Operation",
		manufacturing_operation,
		["previous_operation", "manufacturing_work_order"],
		as_dict=1,
	)
	if not mfg_operation.previous_operation:
		return None
	return frappe.db.get_value(
		"Manufacturing Operation",
		{
			"operation": mfg_operation.previous_operation,
			"manufacturing_work_order": mfg_operation.manufacturing_work_order,
		},
	)


def get_material_wt(doc):
	filters = {"disabled": 0, "company": doc.company}
	if doc.for_subcontracting:
		if doc.subcontractor:
			filters["subcontractor"] = doc.subcontractor
			filters["warehouse_type"] = "Manufacturing"
	else:
		if doc.employee:
			filters["employee"] = doc.employee
			filters["warehouse_type"] = "Manufacturing"
	if not filters:
		filters["department"] = doc.department
		filters["warehouse_type"] = "Manufacturing"

	gross_wt = 0
	net_wt = 0
	finding_wt = 0
	diamond_wt_in_gram = 0
	gemstone_wt_in_gram = 0
	diamond_wt = 0
	gemstone_wt = 0
	other_wt = 0
	diamond_pcs = 0
	gemstone_pcs = 0
	for row in doc.mop_balance_table:
		str_pcs = row.pcs
		if row.pcs and isinstance(row.pcs, str):
			str_pcs = row.pcs.strip()
		row.qty = flt(row.qty, 3)
		if row.item_code[0] in ["M", "F", "D", "G", "O"]:
			variant_of = row.item_code[0]
			if variant_of == "M":
				net_wt += row.qty
			elif variant_of == "F":
				finding_wt += row.qty
			elif variant_of == "D":
				diamond_wt += row.qty
				diamond_wt_in_gram += row.qty * 0.2
				diamond_pcs += int(str_pcs)
			elif variant_of == "G":
				gemstone_wt += row.qty
				gemstone_wt_in_gram += row.qty * 0.2
				gemstone_pcs += int(str_pcs)
			else:
				other_wt += row.qty
	gross_wt = (net_wt + finding_wt + diamond_wt_in_gram + gemstone_wt_in_gram + other_wt)
	if  doc.main_slip_no or doc.is_finding:
		if not frappe.db.get_value("Manufacturing Operation", doc.name,"is_received_gross_greater_than") and  doc.is_finding:
			gross_wt = (net_wt + finding_wt + diamond_wt_in_gram + gemstone_wt_in_gram + other_wt)+ abs(doc.loss_wt or 0)
		else:
			if doc.is_finding:
				gross_wt = (net_wt + finding_wt + diamond_wt_in_gram + gemstone_wt_in_gram + other_wt) - abs(doc.loss_wt or 0)
	result = {
		"gross_wt": gross_wt,
		"net_wt": net_wt,
		"finding_wt": finding_wt,
		"diamond_wt_in_gram": diamond_wt_in_gram,
		"gemstone_wt_in_gram": gemstone_wt_in_gram,
		"other_wt": other_wt,
		"diamond_pcs": diamond_pcs,
		"gemstone_pcs": gemstone_pcs,
		"diamond_wt": diamond_wt,
		"gemstone_wt": gemstone_wt,
	}

	# res = frappe.db.sql(
	# 	f"""select ifnull(sum(if(sed.uom='Carat',sed.qty*0.2, sed.qty)),0) as gross_wt, ifnull(sum(if(i.variant_of = 'M',sed.qty,0)),0) as net_wt, if(i.variant_of = 'D', pcs, 0) as diamond_pcs, if(i.variant_of = 'G',pcs, 0) as gemstone_pcs,
	# 	ifnull(sum(if(i.variant_of = 'D',sed.qty,0)),0) as diamond_wt, ifnull(sum(if(i.variant_of = 'D',if(sed.uom='Carat',sed.qty*0.2, sed.qty),0)),0) as diamond_wt_in_gram,
	# 	ifnull(sum(if(i.variant_of = 'G',sed.qty,0)),0) as gemstone_wt, ifnull(sum(if(i.variant_of = 'G',if(sed.uom='Carat',sed.qty*0.2, sed.qty),0)),0) as gemstone_wt_in_gram,
	# 	ifnull(sum(if(i.variant_of = 'O',sed.qty,0)),0) as other_wt
	# 	from `tabStock Entry Detail` sed left join `tabStock Entry` se on sed.parent = se.name left join `tabItem` i on i.name = sed.item_code
	# 		where sed.t_warehouse = "{t_warehouse}" and sed.manufacturing_operation = "{doc.name}" and se.docstatus = 1""",
	# 	as_dict=1,
	# )

	# get_previous = []
	# for row in res:
	# 	for key in row:
	# 		if key not in ["diamond_pcs", "gemstone_pcs"] and row.get(key) and row.get(key) != 0:
	# 			get_previous.append(key)

	# if not get_previous:
	# 	res = frappe.db.sql(
	# 		f"""select ifnull(sum(if(sed.uom='Carat',sed.qty*0.2, sed.qty)),0) as gross_wt, ifnull(sum(if(i.variant_of = 'M',sed.qty,0)),0) as net_wt, if(i.variant_of = 'D', pcs, 0) as diamond_pcs, if(i.variant_of = 'G',pcs, 0) as gemstone_pcs,
	# 		ifnull(sum(if(i.variant_of = 'D',sed.qty,0)),0) as diamond_wt, ifnull(sum(if(i.variant_of = 'D',if(sed.uom='Carat',sed.qty*0.2, sed.qty),0)),0) as diamond_wt_in_gram,
	# 		ifnull(sum(if(i.variant_of = 'G',sed.qty,0)),0) as gemstone_wt, ifnull(sum(if(i.variant_of = 'G',if(sed.uom='Carat',sed.qty*0.2, sed.qty),0)),0) as gemstone_wt_in_gram,
	# 		ifnull(sum(if(i.variant_of = 'O',sed.qty,0)),0) as other_wt
	# 		from `tabStock Entry Detail` sed left join `tabStock Entry` se on sed.parent = se.name left join `tabItem` i on i.name = sed.item_code
	# 			where sed.t_warehouse = "{t_warehouse}" and sed.manufacturing_operation = "{doc.previous_mop}" and se.docstatus = 1 limit 1""",
	# 		as_dict=1,
	# 	)

	# if doc.status in ["Not Started", "WIP", "QC Pending", "QC Completed"]:
	# 	los = frappe.db.sql(
	# 		f"""select ifnull(sum(if(sed.uom='Carat',sed.qty*0.2, sed.qty)),0) as gross_wt, ifnull(sum(if(i.variant_of = 'M',sed.qty,0)),0) as net_wt, if(i.variant_of = 'D', pcs, 0) as diamond_pcs, if(i.variant_of = 'G',pcs, 0) as gemstone_pcs,
	# 		ifnull(sum(if(i.variant_of = 'D',sed.qty,0)),0) as diamond_wt, ifnull(sum(if(i.variant_of = 'D',if(sed.uom='Carat',sed.qty*0.2, sed.qty),0)),0) as diamond_wt_in_gram,
	# 		ifnull(sum(if(i.variant_of = 'G',sed.qty,0)),0) as gemstone_wt, ifnull(sum(if(i.variant_of = 'G',if(sed.uom='Carat',sed.qty*0.2, sed.qty),0)),0) as gemstone_wt_in_gram,
	# 		ifnull(sum(if(i.variant_of = 'O',sed.qty,0)),0) as other_wt
	# 		from `tabStock Entry Detail` sed left join `tabStock Entry` se on sed.parent = se.name left join `tabItem` i on i.name = sed.item_code
	# 			where sed.s_warehouse = "{t_warehouse}" and sed.manufacturing_operation = "{doc.name}" and se.docstatus = 1""",
	# 		as_dict=1,
	# 	)
	# 	result = {}
	# 	for key in res[0].keys():
	# 		if key not in ["diamond_pcs", "gemstone_pcs"]:
	# 			result[key] = res[0][key] - los[0][key]
	# 		else:
	# 			result[key] = int(res[0][key]) - int(los[0][key])
	# else:
	# 	result = {}
	# 	for key in res[0].keys():
	# 		result[key] = res[0][key]

	if result:
		return result
	return {}


def create_finished_goods_bom(self, se_name, mo_data, total_time=0):
	
	data = get_stock_entry_data(self)

	ref_customer = frappe.db.get_value("Parent Manufacturing Order", self.parent_manufacturing_order, "ref_customer")
	diamond_price_list_ref_customer = frappe.db.get_value("Customer", ref_customer, "diamond_price_list")
	gemstone_price_list_ref_customer = frappe.db.get_value("Customer", ref_customer, "custom_gemstone_price_list_type")
	diamond_price_list = frappe.get_all(
								"Diamond Price List",
								filters={"customer": ref_customer, "price_list_type": diamond_price_list_ref_customer},
								fields=["name", "price_list_type"],
							)
	
	gemstone_price_list  = frappe.get_all(
								"Gemstone Price List",
								filters={"customer": ref_customer, "price_list_type": gemstone_price_list_ref_customer},
								fields=["name", "price_list_type"],
							)
	# frappe.throw(f"{gemstone_price_list}")

	bom_doc = None
	if self.get("new_item"):
		if frappe.db.exists("BOM", {"is_default": 1, "item": self.new_item}):
			bom_doc = frappe.get_doc("BOM", {"is_default": 1, "item": self.new_item})
		else:
			frappe.throw(_("Create default BOM for New Item"))
	if not bom_doc:
		bom_doc = frappe.get_doc("BOM", self.design_id_bom)

	pmo_data = frappe.db.get_value(
		"Parent Manufacturing Order",
		self.parent_manufacturing_order,
		["diamond_quality", "qty","finish_good_image"],
		as_dict=1,
	)
	quality_value = bom_doc.diamond_detail[0].quality
	new_bom = frappe.copy_doc(bom_doc)
	new_bom.front_view_finish = pmo_data.get("finish_good_image")
	new_bom.is_active = 1
	new_bom.custom_creation_doctype = self.doctype
	new_bom.custom_creation_docname = self.name
	new_bom.company = self.company
	new_bom.bom_type = "Finish Goods"
	new_bom.tag_no = get_serial_no(se_name)
	new_bom.custom_serial_number_creator = self.name
	new_bom.metal_detail = []
	new_bom.finding_detail = []
	new_bom.diamond_detail = []
	new_bom.gemstone_detail = []
	new_bom.other_detail = []
	new_bom.total_operation_time = total_time
	new_bom.actual_operation_time = 0
	if mo_data:
		new_bom.with_operations = 1
		new_bom.transfer_material_against = None
		for row in mo_data:
			mop_doc = frappe.get_doc("Manufacturing Operation", row.manufacturing_operation)
			employee = mop_doc.employee
			total_minutes = 0
			for time_log in mop_doc.time_logs:
				total_minutes += time_log.time_in_mins or 0
			matching_workstation = frappe.get_all(
				"Workstation",
				filters={"employee": employee},
				fields=["name", "hour_rate"]
			)
			if matching_workstation:
				ws = matching_workstation[0]
				workstation_name = ws.name
				hour_rate = ws.hour_rate
			else:
				frappe.msgprint(f"No workstation found for employee: {employee}")

			operating_cost = (hour_rate / 60) * total_minutes
			# Set correct hour_rate in BOM Operation
			operation_data = {
				"manufacturing_operation": row.manufacturing_operation,
				"workstation": workstation_name,
				"hour_rate": hour_rate,
				"time_in_mins": total_minutes,
				"operating_cost": operating_cost
			}
			new_bom.append("operations", operation_data)

	new_bom.operation_time_diff = new_bom.total_operation_time - new_bom.actual_operation_time
	# diamond_price_list_customer = frappe.db.get_value("Customer", new_bom.customer, "diamond_price_list")
	# diamond_price_customer = frappe.get_all(
	# 	"Diamond Price List",
	# 	filters={"customer": new_bom.customer, "price_list_type": diamond_price_list_customer},
	# 	fields=["name", "price_list_type"],
	# )
	# frappe.throw(f"{diamond_price_customer}")
	gemstone_price_list_type = frappe.db.get_value("Customer", new_bom.customer, "custom_gemstone_price_list_type")
	ref_gemstone_price_list_type = frappe.db.get_value("Customer", ref_customer, "custom_gemstone_price_list_type")
	if new_bom.customer and not gemstone_price_list_type:
		frappe.throw(_("Gemstone Price list type not mentioned into customer"))

	for item in data:
		item_row = frappe.get_doc("Item", item["item_code"])

		if item_row.variant_of == "D":
			row = {}
			row["stock_uom"] = item.get("uom")
			# row["is_customer_item"] = item_row.is_customer_provided_item

			# frappe.throw(f"{row["is_customer_item"]}")
			row["rate"] = new_bom.gold_rate_with_gst
			row["se_rate"] = item.get("rate")
			sieve_size_range = ''
			for attribute in item_row.attributes:
				atrribute_name = format_attrbute_name(attribute.attribute)
				row[atrribute_name] = attribute.attribute_value
				# frappe.throw(f"{attribute.attribute}")
				if attribute.attribute == 'Diamond Sieve Size':
					sieve_size_range = frappe.db.get_value("Attribute Value", attribute.attribute_value, "sieve_size_range")
					sieve_size_mm = frappe.db.get_value("Attribute Value", attribute.attribute_value, "diameter")
					weight_per_pcs = frappe.db.get_value("Attribute Value", attribute.attribute_value, "weight_in_cts")
					if attribute.attribute_value.startswith('+'):
						row["weight_per_pcs"] = weight_per_pcs
					elif "MM" in attribute.attribute_value:
						stock_entry_details = frappe.db.sql("""
							SELECT SUM(qty) AS total_qty, SUM(pcs) AS total_pcs
							FROM `tabStock Entry Detail`
							WHERE parent = %s
						""", (item.get("parent")), as_dict=True)
						row["total_qty"] = stock_entry_details[0]["total_qty"] if stock_entry_details else 0
						row["total_pcs"] = stock_entry_details[0]["total_pcs"] if stock_entry_details else 0
						row["weight_per_pcs"] = row["total_qty"] / row["total_pcs"] if row["total_pcs"] else 0


					elif "MM" in attribute.attribute_value:
						stock_entry_details = frappe.db.sql("""
							SELECT SUM(qty) AS total_qty, SUM(pcs) AS total_pcs
							FROM `tabStock Entry Detail`
							WHERE parent = %s
						""", (item.get("parent")), as_dict=True)
						row["total_qty"] = stock_entry_details[0]["total_qty"] if stock_entry_details else 0
						row["total_pcs"] = stock_entry_details[0]["total_pcs"] if stock_entry_details else 0
						row["weight_per_pcs"] = row["total_qty"] / row["total_pcs"] if row["total_pcs"] else 0

			row["weight_per_pcs"] = round(row["weight_per_pcs"], 3)
			
			stone_shape = row["stone_shape"]
			diamond_price_list_customer_new = frappe.db.get_value(
				"Diamond Price List Table",    
				{
					"parent": new_bom.customer,
					"diamond_shape": stone_shape
				},
				"diamond_price_list"
			)
			diamond_price_customer = frappe.get_all(
				"Diamond Price List",
				filters={"customer": new_bom.customer, "price_list_type": diamond_price_list_customer_new},
				fields=["name", "price_list_type"],
			)

			row["quantity"] = item["qty"] / pmo_data.get("qty")
			row["sieve_size_range"] = sieve_size_range
			row["sieve_size_mm"] = sieve_size_mm
			# if item.get("inventory_type") and item.get("inventory_type") == "Customer Goods":
			# 	row["is_customer_item"] = 1
			row["is_customer_item"] = 1 if item.get("inventory_type") == "Customer Goods" else 0
			# frappe.throw(f"{row["is_customer_item"]}")
			row["pcs"] = item.get("pcs")
			row["total_diamond_rate"] = 0
			if pmo_data.get("diamond_quality"):
				row["quality"] = pmo_data.get("diamond_quality")
			if self.company == "Gurukrupa Export Private Limited":
				if diamond_price_customer and any(dpl["price_list_type"] == diamond_price_list_customer_new for dpl in diamond_price_customer):
					row["quality"] = quality_value
					if diamond_price_list_customer_new == "Size (in mm)":
						size_in_mm_diamond_price_list_entry = frappe.db.sql(
							"""
							SELECT name, supplier_fg_purchase_rate,rate,outright_handling_charges_rate,outright_handling_charges_in_percentage,diamond_quality,
							outwork_handling_charges_rate,outwork_handling_charges_in_percentage
							FROM `tabDiamond Price List`
							WHERE customer = %s
							AND price_list_type = %s
							AND diamond_quality = %s
							AND size_in_mm = %s
							ORDER BY creation DESC
							LIMIT 1
							""",
							(new_bom.customer, diamond_price_list_customer_new,row["quality"], row["sieve_size_mm"]),
							as_dict=True
						)

						if size_in_mm_diamond_price_list_entry:
							latest_entry = size_in_mm_diamond_price_list_entry[0]
							# row["total_diamond_rate"] = latest_entry.get("rate", 0)
							row["fg_purchase_rate"] = latest_entry.get("supplier_fg_purchase_rate", 0)
							row["fg_purchase_amount"] = row["fg_purchase_rate"] * row["quantity"]
							if row["is_customer_item"]:
								row["total_diamond_rate"] = latest_entry.get("outwork_handling_charges_rate", 0)
								row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["sieve_size_mm"]
								if latest_entry.get("outwork_handling_charges_rate") == 0:
									percentage = latest_entry.get("outwork_handling_charges_in_percentage", 0)
									amount = latest_entry.get("rate", 0) * (percentage / 100)
									row["total_diamond_rate"] = amount
									row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["sieve_size_mm"]
							else:
								row["total_diamond_rate"] = latest_entry.get("rate", 0) + latest_entry.get("outright_handling_charges_rate", 0)
								row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["sieve_size_mm"]
								if latest_entry.get("outright_handling_charges_rate") == 0:
									percentage = latest_entry.get("outright_handling_charges_in_percentage", 0)
									rate = latest_entry.get("rate", 0) * (percentage / 100)
									row["total_diamond_rate"] = rate + latest_entry.get("rate", 0)
									row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["sieve_size_mm"]

					if diamond_price_list_customer_new == "Sieve Size Range":
						sieve_size_range_diamond_price_list_entry = frappe.db.sql(
							"""
							SELECT name, supplier_fg_purchase_rate,rate,outright_handling_charges_rate,diamond_quality
							FROM `tabDiamond Price List`
							WHERE customer = %s
							AND price_list_type = %s
							AND diamond_quality = %s
							AND sieve_size_range = %s
							ORDER BY creation DESC
							LIMIT 1
							""",
							(new_bom.customer, diamond_price_list_customer_new,row["quality"], row["sieve_size_range"]),
							as_dict=True
						)
						if sieve_size_range_diamond_price_list_entry:
							latest_entry = sieve_size_range_diamond_price_list_entry[0]  # Get the first entry
							row["total_diamond_rate"] = latest_entry.get("rate", 0)
							row["fg_purchase_rate"] = latest_entry.get("supplier_fg_purchase_rate", 0)
							row["fg_purchase_amount"] = row["fg_purchase_rate"] * row["quantity"]

					if diamond_price_list_customer_new == "Weight (in cts)":
						latest_diamond_price_list_entry  = frappe.db.sql(
							"""
							SELECT name, from_weight, to_weight, supplier_fg_purchase_rate,rate,outright_handling_charges_rate,outright_handling_charges_in_percentage,
							outwork_handling_charges_rate,outwork_handling_charges_in_percentage,diamond_quality
							FROM `tabDiamond Price List`
							WHERE customer = %s
							AND price_list_type = %s
							AND diamond_quality = %s
							AND %s BETWEEN from_weight AND to_weight
							ORDER BY creation DESC
							LIMIT 1
							""",
							(new_bom.customer, diamond_price_list_customer_new,row["quality"],row["weight_per_pcs"]),
							as_dict=True
						)
						
						if latest_diamond_price_list_entry:
							latest_entry = latest_diamond_price_list_entry[0]
							# frappe.throw(f"{latest_entry}")
							row["fg_purchase_rate"] = latest_entry.get("supplier_fg_purchase_rate", 0)
							row["fg_purchase_amount"] = row["fg_purchase_rate"] * row["quantity"]
							if row["is_customer_item"]:
								row["total_diamond_rate"] = latest_entry.get("outwork_handling_charges_rate", 0)
								row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["quantity"]

								if latest_entry.get("outwork_handling_charges_rate") == 0:
									# frappe.throw("HIIIIIIIII")
									percentage = latest_entry.get("outwork_handling_charges_in_percentage", 0)
									amount = latest_entry.get("rate", 0) * (percentage / 100)
									row["total_diamond_rate"] = amount
									row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["quantity"]

							else:
								
								row["total_diamond_rate"] = latest_entry.get("rate", 0) + latest_entry.get("outright_handling_charges_rate", 0)
								row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["quantity"]
								# frappe.throw(f"{row["diamond_rate_for_specified_quantity"]}")
								if latest_entry.get("outright_handling_charges_rate") == 0:
									percentage = latest_entry.get("outright_handling_charges_in_percentage", 0)
									rate = latest_entry.get("rate", 0) * (percentage / 100)
									row["total_diamond_rate"] = rate + latest_entry.get("rate", 0)
									row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["quantity"]




				# row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["quantity"]

			else:
				row["is_customer_item"] = 1 if item.get("inventory_type") == "Customer Goods" else 0
				if diamond_price_list and any(dpl["price_list_type"] == diamond_price_list_ref_customer for dpl in diamond_price_list):
					if diamond_price_list_ref_customer == "Size (in mm)":
						size_in_mm_diamond_price_list_entry = frappe.db.sql(
							"""
							SELECT name, supplier_fg_purchase_rate,rate,outwork_handling_charges_in_percentage,diamond_quality,
							outright_handling_charges_in_percentage,outright_handling_charges_rate,outwork_handling_charges_rate
							FROM `tabDiamond Price List`
							WHERE customer = %s
							AND price_list_type = %s
							AND diamond_quality = %s
							AND size_in_mm = %s
							ORDER BY creation DESC
							LIMIT 1
							""",
							(ref_customer, diamond_price_list_ref_customer,row["quality"], row["sieve_size_mm"]),
							as_dict=True
						)
						if size_in_mm_diamond_price_list_entry:
							latest_entry = size_in_mm_diamond_price_list_entry[0]# Get the first entry
							# row["total_diamond_rate"] = latest_entry.get("rate", 0)
							row["fg_purchase_rate"] = latest_entry.get("supplier_fg_purchase_rate", 0)
							row["fg_purchase_amount"] = row["fg_purchase_rate"] * row["quantity"]
							if row["is_customer_item"]:
								row["total_diamond_rate"] = latest_entry.get("outwork_handling_charges_rate", 0)
								row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["sieve_size_mm"]

								if latest_entry.get("outwork_handling_charges_rate") == 0:
									percentage = latest_entry.get("outwork_handling_charges_in_percentage", 0)
									amount = latest_entry.get("rate", 0) * (percentage / 100)
									row["total_diamond_rate"] = amount
									row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["sieve_size_mm"]
							else:
								row["total_diamond_rate"] = latest_entry.get("rate", 0) + latest_entry.get("outright_handling_charges_rate", 0)
								row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["sieve_size_mm"]
								if latest_entry.get("outright_handling_charges_rate") == 0:
									percentage = latest_entry.get("outright_handling_charges_in_percentage", 0)
									rate = latest_entry.get("rate", 0) * (percentage / 100)
									row["total_diamond_rate"] = rate + latest_entry.get("rate", 0)
									row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["sieve_size_mm"]


					if diamond_price_list_ref_customer == "Sieve Size Range":
						sieve_size_range_diamond_price_list_entry = frappe.db.sql(
							"""
							SELECT name, supplier_fg_purchase_rate,rate,diamond_quality
							FROM `tabDiamond Price List`
							WHERE customer = %s
							AND price_list_type = %s
							AND diamond_quality = %s
							AND sieve_size_range = %s
							ORDER BY creation DESC
							LIMIT 1
							""",
							(ref_customer, diamond_price_list_ref_customer,row["quality"], row["sieve_size_range"]),
							as_dict=True
						)
						if sieve_size_range_diamond_price_list_entry:
							latest_entry = sieve_size_range_diamond_price_list_entry[0]  # Get the first entry
							row["total_diamond_rate"] = latest_entry.get("rate", 0)
							row["fg_purchase_rate"] = latest_entry.get("supplier_fg_purchase_rate", 0)
							row["fg_purchase_amount"] = row["fg_purchase_rate"] * row["quantity"]
							if row["is_customer_item"]:
								row["total_diamond_rate"] = latest_entry.get("outwork_handling_charges_rate", 0)
								row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["quantity"]
								if latest_entry.get("outwork_handling_charges_rate") == 0:
									percentage = latest_entry.get("outwork_handling_charges_in_percentage", 0)
									amount = latest_entry.get("rate", 0) * (percentage / 100)
									row["total_diamond_rate"] = amount
									row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["quantity"]

							else:
							
								row["total_diamond_rate"] = latest_entry.get("rate", 0) + latest_entry.get("outright_handling_charges_rate", 0)
								row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["quantity"]
								# frappe.throw(f"{latest_entry.get("outright_handling_charges_rate")}")
								if latest_entry.get("outright_handling_charges_rate") == 0:
									percentage = latest_entry.get("outright_handling_charges_in_percentage", 0)
									rate = latest_entry.get("rate", 0) * (percentage / 100)
									row["total_diamond_rate"] = rate + latest_entry.get("rate", 0)
									row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["quantity"]

					if diamond_price_list_ref_customer == "Weight (in cts)":
						latest_diamond_price_list_entry  = frappe.db.sql(
							"""
							SELECT name, from_weight, to_weight, supplier_fg_purchase_rate,rate,diamond_quality
							FROM `tabDiamond Price List`
							WHERE customer = %s
							AND price_list_type = %s
							AND diamond_quality = %s
							AND %s BETWEEN from_weight AND to_weight
							ORDER BY creation DESC
							LIMIT 1
							""",
							(ref_customer, diamond_price_list_ref_customer,row["quality"],row["weight_per_pcs"]),
							as_dict=True
						)
						if latest_diamond_price_list_entry:
							latest_entry = latest_diamond_price_list_entry[0]
							# row["total_diamond_rate"] = latest_entry.get("rate", 0)
							row["fg_purchase_rate"] = latest_entry.get("supplier_fg_purchase_rate", 0)
							row["fg_purchase_amount"] = row["fg_purchase_rate"] * row["quantity"]
							if row["is_customer_item"]:
								row["total_diamond_rate"] = latest_entry.get("outwork_handling_charges_rate", 0)
								row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["quantity"]

								if latest_entry.get("outwork_handling_charges_rate") == 0:
									percentage = latest_entry.get("outwork_handling_charges_in_percentage", 0)
									amount = latest_entry.get("rate", 0) * (percentage / 100)
									row["total_diamond_rate"] = amount
									row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["quantity"]
							else:
								row["total_diamond_rate"] = latest_entry.get("rate", 0) + latest_entry.get("outright_handling_charges_rate", 0)
								row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["quantity"]
								if latest_entry.get("outright_handling_charges_rate") == 0:
									percentage = latest_entry.get("outright_handling_charges_in_percentage", 0)
									row["total_diamond_rate"] = latest_entry.get("rate", 0) * (percentage / 100)
									row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["quantity"]




				# row["diamond_rate_for_specified_quantity"] = row["total_diamond_rate"] * row["quantity"]

			new_bom.append("diamond_detail", row)

			total_diamond_rate_for_specified_quantity = 0
			for row in new_bom.get("diamond_detail", []):
				total_diamond_rate_for_specified_quantity = sum(row.get("diamond_rate_for_specified_quantity", 0) or 0  for row in new_bom.get("diamond_detail", []))
				new_bom.diamond_bom_amount = total_diamond_rate_for_specified_quantity
				total_diamond_purchase_amount = sum((row.get("fg_purchase_amount", 0) or 0) for row in new_bom.get("diamond_detail", []))
				new_bom.diamond_fg_purchase = total_diamond_purchase_amount
				total_diamond_pcs = sum(flt(row.get("pcs", 0) or 0) for row in new_bom.get("diamond_detail", []))
				new_bom.total_diamond_pcs = total_diamond_pcs
				new_bom.total_diamond_amount = total_diamond_rate_for_specified_quantity
				total_diamond_weight_per_gram = sum(flt(row.get("weight_in_gms", 0) or 0) for row in new_bom.get("diamond_detail", []))
				new_bom.total_diamond_weight_per_gram = total_diamond_weight_per_gram
				total_diamond_weight = sum(flt(row.get("quantity", 0) or 0) for row in new_bom.get("diamond_detail", []))
				new_bom.total_diamond_weight = total_diamond_weight

		elif item_row.variant_of == "M":
			row = {}
			rate_per_gm = 0
			fg_purchase_rate = 0
			fg_purchase_amount = 0
			wastage_rate = 0

			row["stock_uom"] = item.get("uom")
			row["quantity"] = item["qty"] / pmo_data.get("qty")
			row["is_customer_item"] = 1 if item.get("inventory_type") == "Customer Goods" else 0

			if self.company == "Gurukrupa Export Private Limited":
				making_charge_price_list = frappe.get_all(
					"Making Charge Price",
					filters={
						"customer": new_bom.customer,
						"setting_type": new_bom.setting_type,
					},
					fields=["name"]
				)

				making_charge_price_list_with_gold_rate = frappe.get_all(
					"Making Charge Price",
					filters={
						"customer": new_bom.customer,
						"setting_type": new_bom.setting_type,
						"from_gold_rate": ["<=", new_bom.gold_rate_with_gst],
						"to_gold_rate": [">=", new_bom.gold_rate_with_gst]
					},
					fields=["name"]
				)

				if making_charge_price_list:
					making_charge_price_subcategories = frappe.get_all(
						"Making Charge Price Item Subcategory",
						filters={"parent": making_charge_price_list[0]["name"]},
						fields=["subcategory", "rate_per_gm", "supplier_fg_purchase_rate", "wastage",
								"custom_subcontracting_rate", "custom_subcontracting_wastage"]
					)

					matching_subcategory = next(
						(row for row in making_charge_price_subcategories if row.get("subcategory") == new_bom.item_subcategory),
						None
					)

					if matching_subcategory:
						rate_per_gm = matching_subcategory.get("rate_per_gm", 0)
						fg_purchase_rate = matching_subcategory.get("supplier_fg_purchase_rate", 0)
						fg_purchase_amount = fg_purchase_rate * row["quantity"]

						if row["is_customer_item"]:
							row["rate"] = matching_subcategory.get("custom_subcontracting_rate", 0)
							wastage_rate = matching_subcategory.get("custom_subcontracting_wastage", 0) / 100
							fg_purchase_rate = 0
							fg_purchase_amount = 0
							rate_per_gm = 0
						else:
							row["rate"] = new_bom.gold_rate_with_gst
							wastage_rate = matching_subcategory.get("wastage", 0) / 100

				# Ensure 'rate' is set to avoid KeyError
				if "rate" not in row:
					row["rate"] = 0

				row["wastage_rate"] = wastage_rate
				row["amount"] = row["rate"] * row["quantity"]
				row["wastage_amount"] = row["wastage_rate"] * row["amount"]
				row["se_rate"] = item.get("rate")

				for attribute in item_row.attributes:
					attribute_name = format_attrbute_name(attribute.attribute)
					row[attribute_name] = attribute.attribute_value

				row["fg_purchase_rate"] = fg_purchase_rate
				row["fg_purchase_amount"] = fg_purchase_amount
				row["pcs"] = item.get("pcs")
				row["making_rate"] = rate_per_gm
				row["making_amount"] = row["making_rate"] * row["quantity"]

				new_bom.append("metal_detail", row)

				# Custom Metal Amount
				if not hasattr(new_bom, "custom_metal_amount"):
					new_bom.custom_metal_amount = 0
				if new_bom.get("metal_detail"):
					total_making_amount = sum(flt(r.get("making_amount", 0)) for r in new_bom.get("metal_detail", []))
					new_bom.custom_metal_amount = total_making_amount

				# Custom FG Metal Amount
				if not hasattr(new_bom, "custom_fg_metal_amount"):
					new_bom.custom_fg_metal_amount = 0
				if new_bom.get("metal_detail"):
					total_fg_purchase_amount = sum(flt(r.get("fg_purchase_amount", 0)) for r in new_bom.get("metal_detail", []))
					new_bom.custom_fg_metal_amount = total_fg_purchase_amount

			else:
				making_charge_price_list = frappe.get_all(
					"Making Charge Price",
					filters={
						"customer": ref_customer,
						"setting_type": new_bom.setting_type,
					},
					fields=["name"]
				)

				making_charge_price_list_with_gold_rate = frappe.get_all(
					"Making Charge Price",
					filters={
						"customer": ref_customer,
						"setting_type": new_bom.setting_type,
						"from_gold_rate": ["<=", new_bom.gold_rate_with_gst],
						"to_gold_rate": [">=", new_bom.gold_rate_with_gst]
					},
					fields=["name"]
				)

				if making_charge_price_list:
					making_charge_price_subcategories = frappe.get_all(
						"Making Charge Price Item Subcategory",
						filters={"parent": making_charge_price_list[0]["name"]},
						fields=["subcategory", "rate_per_gm", "supplier_fg_purchase_rate", "wastage",
								"custom_subcontracting_rate", "custom_subcontracting_wastage"]
					)

					matching_subcategory = next(
						(row for row in making_charge_price_subcategories if row.get("subcategory") == new_bom.item_subcategory),
						None
					)

					if matching_subcategory:
						rate_per_gm = matching_subcategory.get("rate_per_gm", 0)
						fg_purchase_rate = matching_subcategory.get("supplier_fg_purchase_rate", 0)
						fg_purchase_amount = fg_purchase_rate * row["quantity"]

						if row["is_customer_item"]:
							row["rate"] = matching_subcategory.get("custom_subcontracting_rate", 0)
							wastage_rate = matching_subcategory.get("custom_subcontracting_wastage", 0) / 100
							fg_purchase_rate = 0
							fg_purchase_amount = 0
							rate_per_gm = 0
						else:
							row["rate"] = new_bom.gold_rate_with_gst
							wastage_rate = matching_subcategory.get("wastage", 0) / 100

				# Ensure 'rate' is set to avoid KeyError
				if "rate" not in row:
					row["rate"] = 0

				row["wastage_rate"] = wastage_rate
				row["se_rate"] = item.get("rate")
				row["amount"] = row["rate"] * row["quantity"]
				row["wastage_amount"] = row["wastage_rate"] * row["amount"]

				for attribute in item_row.attributes:
					attribute_name = format_attrbute_name(attribute.attribute)
					row[attribute_name] = attribute.attribute_value

				row["fg_purchase_rate"] = fg_purchase_rate
				row["fg_purchase_amount"] = fg_purchase_amount
				row["pcs"] = item.get("pcs")
				row["making_rate"] = rate_per_gm
				row["making_amount"] = row["making_rate"] * row["quantity"]

				new_bom.append("metal_detail", row)

				if not hasattr(new_bom, "custom_metal_amount"):
					new_bom.custom_metal_amount = 0
				if new_bom.get("metal_detail"):
					total_making_amount = sum(flt(r.get("making_amount", 0)) for r in new_bom.get("metal_detail", []))
					new_bom.custom_metal_amount = total_making_amount

				if not hasattr(new_bom, "custom_fg_metal_amount"):
					new_bom.custom_fg_metal_amount = 0
				if new_bom.get("metal_detail"):
					total_fg_purchase_amount = sum(flt(r.get("fg_purchase_amount", 0)) for r in new_bom.get("metal_detail", []))
					new_bom.custom_fg_metal_amount = total_fg_purchase_amount

			if not hasattr(new_bom, "total_metal_weight"):
				new_bom.total_metal_weight = 0
			if new_bom.get("metal_detail"):
				total_metal_weight = sum(flt(r.get("quantity", 0)) for r in new_bom.get("metal_detail", []))
				new_bom.total_metal_weight = total_metal_weight

			if not hasattr(new_bom, "total_metal_amount"):
				new_bom.total_metal_amount = 0
			if new_bom.get("metal_detail"):
				total_metal_amount = sum(flt(r.get("amount", 0)) for r in new_bom.get("metal_detail", []))
				new_bom.total_metal_amount = total_metal_amount

			if not hasattr(new_bom, "total_wastage_amount"):
				new_bom.total_wastage_amount = 0
			if new_bom.get("metal_detail"):
				total_wastage_amount = sum(flt(r.get("wastage_amount", 0)) for r in new_bom.get("metal_detail", []))
				new_bom.total_wastage_amount = total_wastage_amount

		elif item_row.variant_of == "F":
			row = {}
			row["stock_uom"] = item.get("uom")
			rate_per_gm = 0
			fg_purchase_rate = 0
			fg_purchase_amount = 0
			wastage_rate = 0
			row["se_rate"] = item.get("rate")
			row["quantity"] = item["qty"] / pmo_data.get("qty")
			finding_type_value = None
			row["is_customer_item"] = 1 if item.get("inventory_type") == "Customer Goods" else 0

			for attribute in item_row.attributes:
				atrribute_name = format_attrbute_name(attribute.attribute)
				if atrribute_name == "finding_sub_category":
					atrribute_name = "finding_type"
					finding_type_value = attribute.attribute_value
				row[atrribute_name] = attribute.attribute_value

			row["finding_type"] = finding_type_value

			if self.company == "Gurukrupa Export Private Limited":
				making_charge_price_list = frappe.get_all(
					"Making Charge Price",
					filters={
						"customer": new_bom.customer,
						"setting_type": new_bom.setting_type,
					},
					fields=["name"]
				)
				making_charge_price_list_with_gold_rate = frappe.get_all(
					"Making Charge Price",
					filters={
						"customer": new_bom.customer,
						"setting_type": new_bom.setting_type,
						"from_gold_rate": ["<=", new_bom.gold_rate_with_gst],
						"to_gold_rate": [">=", new_bom.gold_rate_with_gst]
					},
					fields=["name"]
				)

				subcategory_value = None
				matching_subcategory = None

				if making_charge_price_list:
					if finding_type_value:
						subcategory_value = frappe.db.get_value(
							"Making Charge Price Finding Subcategory",
							{"subcategory": finding_type_value},
							["rate_per_gm", "wastage"],
							order_by="creation DESC"
						)
						# frappe.throw(f"{subcategory_value 900,0,0}")
					making_charge_price_subcategories = frappe.get_all(
						"Making Charge Price Finding Subcategory",
						filters={"parent": making_charge_price_list[0]["name"]},
						fields=["subcategory", "rate_per_gm", "supplier_fg_purchase_rate", "wastage", "custom_subcontracting_rate", "custom_subcontracting_wastage"]
					)
					# frappe.throw(f"{making_charge_price_subcategories}")
					if making_charge_price_subcategories:
						matching_subcategory = next(
							(row_ for row_ in making_charge_price_subcategories if row.get("subcategory") == new_bom.item_subcategory),
							None
						)
						# frappe.throw(f"{matching_subcategory}")
						if matching_subcategory:
							rate_per_gm = matching_subcategory.get("rate_per_gm", 0)
							fg_purchase_rate = matching_subcategory.get("supplier_fg_purchase_rate", 0)
							fg_purchase_amount = fg_purchase_rate * row["quantity"]

							if row["is_customer_item"]:
								row["rate"] = matching_subcategory.get("custom_subcontracting_rate", 0)
								wastage_rate = matching_subcategory.get("custom_subcontracting_wastage", 0) / 100
								fg_purchase_rate = 0
								fg_purchase_amount = 0
								rate_per_gm = 0
							else:
								row["rate"] = new_bom.gold_rate_with_gst
								wastage_rate = matching_subcategory.get("wastage", 0) / 100

				row["wastage_rate"] = wastage_rate
				row["making_rate"] = rate_per_gm
				row["rate"] = new_bom.gold_rate_with_gst
				# row["rate"] = row.get("rate", 0)  # Ensure rate is always set
				row["amount"] = row["rate"] * row["quantity"]

				if making_charge_price_list_with_gold_rate:
					row["making_amount"] = row["making_rate"] * row["quantity"]
					row.setdefault("wastage_rate", 0)

				row["pcs"] = item.get("pcs")
				row["fg_purchase_rate"] = fg_purchase_rate
				row["fg_purchase_amount"] = fg_purchase_amount
				row["wastage_amount"] = row.get("wastage_rate", 0) * row["amount"]

			else:
				making_charge_price_list = frappe.get_all(
					"Making Charge Price",
					filters={
						"customer": ref_customer,
						"setting_type": new_bom.setting_type,
					},
					fields=["name"]
				)

				row["is_customer_item"] = 1 if item.get("inventory_type") == "Customer Goods" else 0

				making_charge_price_list_with_gold_rate = frappe.get_all(
					"Making Charge Price",
					filters={
						"customer": ref_customer,
						"setting_type": new_bom.setting_type,
						"from_gold_rate": ["<=", new_bom.gold_rate_with_gst],
						"to_gold_rate": [">=", new_bom.gold_rate_with_gst]
					},
					fields=["name"]
				)

				subcategory_value = None
				matching_subcategory = None

				if making_charge_price_list:
					if finding_type_value:
						subcategory_value = frappe.db.get_value(
							"Making Charge Price Finding Subcategory",
							{"subcategory": finding_type_value},
							["rate_per_gm", "wastage"],
							order_by="creation DESC"
						)

					making_charge_price_subcategories = frappe.get_all(
						"Making Charge Price Item Subcategory",
						filters={"parent": making_charge_price_list[0]["name"]},
						fields=["subcategory", "rate_per_gm", "supplier_fg_purchase_rate", "wastage", "custom_subcontracting_rate", "custom_subcontracting_wastage"]
					)

					if making_charge_price_subcategories:
						matching_subcategory = next(
							(row for row in making_charge_price_subcategories if row.get("subcategory") == new_bom.item_subcategory),
							None
						)

					if matching_subcategory:
						rate_per_gm = matching_subcategory.get("rate_per_gm", 0)
						fg_purchase_rate = matching_subcategory.get("supplier_fg_purchase_rate", 0)
						fg_purchase_amount = fg_purchase_rate * row["quantity"]

						if row["is_customer_item"]:
							row["rate"] = matching_subcategory.get("custom_subcontracting_rate", 0)
							wastage_rate = matching_subcategory.get("custom_subcontracting_wastage", 0) / 100
							fg_purchase_rate = 0
							fg_purchase_amount = 0
							rate_per_gm = 0
						else:
							row["rate"] = new_bom.gold_rate_with_gst
							wastage_rate = matching_subcategory.get("wastage", 0) / 100
					else:
						row["rate"] = new_bom.gold_rate_with_gst
				row["wastage_rate"] = wastage_rate
				row["making_rate"] = rate_per_gm
				# row["rate"] = row.get("rate", 0)  # Ensure rate is always set

				if making_charge_price_list_with_gold_rate:
					row["making_amount"] = row["making_rate"] * row["quantity"]
					row.setdefault("wastage_rate", 0)

				row["pcs"] = item.get("pcs")
				row["fg_purchase_rate"] = fg_purchase_rate
				row["fg_purchase_amount"] = fg_purchase_amount
				row["amount"] = row["rate"] * row["quantity"]
				row["wastage_amount"] = row.get("wastage_rate", 0) * row["amount"]

			new_bom.append("finding_detail", row)

			if not hasattr(new_bom, "custom_finding_amount"):
				new_bom.custom_finding_amount = 0

			if not hasattr(new_bom, "custom_finding_fg_amount"):
				new_bom.custom_finding_fg_amount = 0

			# Calculate totals
			total_making_amount = sum(
				flt(row.get("making_rate", 0) * row.get("quantity", 0)) for row in new_bom.get("finding_detail", [])
			)
			total_fg_purchase_amount = sum(
				flt(row.get("fg_purchase_amount", 0)) for row in new_bom.get("finding_detail", [])
			)
			total_finding_amount = sum(flt(row.get("amount", 0)) for row in new_bom.get("finding_detail", []))
			total_finding_quantity = sum(flt(row.get("qty", 0)) for row in new_bom.get("finding_detail", []))
			total_finding_weight_per_gram = sum(flt(row.get("quantity", 0)) for row in new_bom.get("finding_detail", []))
			total_finding_weight = sum(flt(row.get("quantity", 0)) for row in new_bom.get("finding_detail", []))
			total_wastage_amount = sum(flt(row.get("wastage_amount", 0)) for row in new_bom.get("finding_detail", []))
			total_gemstone_rate_for_specified_quantity = sum(flt(row.get("gemstone_rate_for_specified_quantity", 0)) for row in new_bom.get("gemstone_detail", []))

			# Set the totals in the BOM document
			new_bom.custom_finding_amount = total_making_amount
			new_bom.custom_finding_fg_amount = total_fg_purchase_amount
			new_bom.finding_bom_amount = total_finding_amount
			new_bom.total_finding_amount = total_finding_amount
			new_bom.finding_pcs = total_finding_quantity
			new_bom.total_finding_weight_per_gram = total_finding_weight_per_gram
			new_bom.finding_weight_ = total_finding_weight
			new_bom.total_wastage_amount = total_wastage_amount
			new_bom.total_gemstone_rate_for_specified_quantity = total_gemstone_rate_for_specified_quantity

		elif item_row.variant_of == "G":
			row = {}
			row["stock_uom"] = item.get("uom")
			# Fetching basic details
			row["se_rate"] = item.get("rate")
			row["rate"] = new_bom.gold_rate_with_gst
			row["quantity"] = flt(item.get("qty", 0)) / flt(pmo_data.get("qty", 1))
			row["pcs"] = flt(item.get("pcs", 0))
			if self.company == "KG GK Jewellers Private Limited":
				row["price_list_type"] = ref_gemstone_price_list_type
			else:
				row["price_list_type"] = gemstone_price_list_type



			# Fetching attributes
			attributes = frappe.db.sql(
				"""
				SELECT attribute, attribute_value
				FROM `tabItem Variant Attribute`
				WHERE parent = %s
				AND attribute IN (
					'Gemstone Type', 'Stone Shape', 'Cut or Cab',
					'Gemstone Grade', 'Gemstone Size', 'Gemstone Quality', 'Gemstone PR'
				)
				""",
				(item_row.item_code),
				as_dict=True
			)

			# Mapping attributes to row
			attribute_map = {
				"Gemstone Type": "gemstone_type",
				"Stone Shape": "stone_shape",
				"Cut or Cab": "cut_or_cab",
				"Gemstone Grade": "gemstone_grade",
				"Gemstone Size": "gemstone_size",
				"Gemstone Quality": "gemstone_quality",
				"Gemstone PR": "gemstone_pr"
			}

			for attr in attributes:
				if attr.get("attribute") in attribute_map:
					row[attribute_map[attr["attribute"]]] = attr["attribute_value"]
					
			# if self.company == "Gurukrupa Export Private Limited":
			# 	gemstone_price_list_customer = frappe.db.get_value("Customer", new_bom.customer, "custom_gemstone_price_list_type")
			# 	gemstone_price_customer  = frappe.get_all(
			# 			"Gemstone Price List",
			# 			filters={"customer": new_bom.customer, "price_list_type": gemstone_price_list_customer},
			# 			fields=["name", "price_list_type"],
			# 		)
			# 	if not gemstone_price_customer:
			# 		frappe.throw(f"Customer {new_bom.customer} ke liye koi Gemstone Price List available nahi hai.")
			# 	# frappe.throw(f"{gemstone_price_customer}")
			# 	if gemstone_price_customer and any(dpl["price_list_type"] == gemstone_price_list_customer for dpl in gemstone_price_customer):
					
			# 		if gemstone_price_customer and any(dpl["price_list_type"] == "Multiplier" for dpl in gemstone_price_customer):
					
			# 			combined_query = frappe.db.sql("""
			# 				SELECT gpl.name, gpl.cut_or_cab, gpl.gemstone_grade,
			# 					gm.item_category, gm.precious, gm.semi_precious, gm.synthetic,
			# 					sfm.precious AS supplier_precious, sfm.semi_precious AS supplier_semi_precious, sfm.synthetic AS supplier_synthetic,
			# 					gm.precious_percentage as precious_percentage, gm.semi_precious_percentage as semi_precious_percentage, gm.synthetic_percentage as synthetic_percentage
			# 				FROM `tabGemstone Price List` gpl
			# 				INNER JOIN `tabGemstone Multiplier` gm
			# 					ON gm.parent = gpl.name AND gm.item_category = %s AND gm.parentfield = 'gemstone_multiplier'
			# 				LEFT JOIN `tabGemstone Multiplier` sfm
			# 					ON sfm.parent = gpl.name AND sfm.item_category = %s AND sfm.parentfield = 'supplier_fg_multiplier'
			# 				WHERE gpl.customer = %s
			# 				AND gpl.price_list_type = %s
			# 				AND gpl.cut_or_cab LIKE %s
			# 				AND gpl.gemstone_grade LIKE %s
			# 				AND %s BETWEEN gpl.from_gemstone_pr_rate AND gpl.to_gemstone_pr_rate
			# 				ORDER BY gpl.creation DESC
			# 				LIMIT 1
			# 				""", (
			# 					new_bom.item_category,
			# 					new_bom.item_category,
			# 					new_bom.customer,
			# 					gemstone_price_list_customer,
			# 					f"%{row.get('cut_or_cab')}%",
			# 					f"%{row.get('gemstone_grade')}%",
			# 					row.get('gemstone_pr')
			# 				), as_dict=True)

			# 			# frappe.throw(f"{combined_query}")
			# 			if combined_query:
			# 				entry = combined_query[0]
			# 				gemstone_quality = row.get("gemstone_quality")
			# 				gemstone_pr = flt(row.get("gemstone_pr", 0))

			# 				    # Multiplier value
			# 				multiplier_value = None
			# 				# percentage_value = None

			# 				if gemstone_quality == "Precious":
			# 					multiplier_value = entry.get("precious")
			# 					percentage_value = entry.get("precious_percentage")
			# 				elif gemstone_quality == "Semi Precious":
			# 					multiplier_value = entry.get("semi_precious")
			# 					percentage_value = entry.get("semi_precious_percentage")
			# 				elif gemstone_quality == "Synthetic":
			# 					multiplier_value = entry.get("synthetic")
			# 					percentage_value = entry.get("synthetic_percentage")

			# 				if multiplier_value not in [None, 0] and multiplier_value > 0:
			# 					row["total_gemstone_rate"] = multiplier_value
			# 					row["gemstone_rate_for_specified_quantity"] = row["total_gemstone_rate"] * gemstone_pr
			# 				elif percentage_value not in [None, 0] and percentage_value > 0:
			# 					# Agar multiplier 0 hai, percentage se calculate karo
			# 					row["total_gemstone_rate"] = gemstone_pr * (percentage_value / 100)
			# 					row["gemstone_rate_for_specified_quantity"] = row["total_gemstone_rate"] * gemstone_pr


			# 				supplier_selected_value = entry.get("supplier_precious") if gemstone_quality == "Precious" else \
			# 										entry.get("supplier_semi_precious") if gemstone_quality == "Semi Precious" else \
			# 										entry.get("supplier_synthetic") if gemstone_quality == "Synthetic" else None

			# 				# if multiplier_selected_value is not None:
			# 				# 	row["total_gemstone_rate"] = multiplier_selected_value
			# 				# 	row["gemstone_rate_for_specified_quantity"] = row["total_gemstone_rate"] * gemstone_pr

			# 				if supplier_selected_value is not None:
			# 					row["fg_purchase_rate"] = supplier_selected_value
			# 					row["fg_purchase_amount"] = row["fg_purchase_rate"] * gemstone_pr
					
			# 		if gemstone_price_customer and any(dpl["price_list_type"] == "Weight (in cts)" for dpl in gemstone_price_customer):
			# 		# if gemstone_price_customer == "Weight (in cts)":
			# 			import re

			# 			gemstone_size_str = row.get("gemstone_size", "")
			# 			numbers = re.findall(r"[-+]?\d*\.\d+|\d+", gemstone_size_str)

			# 			if len(numbers) == 2:
			# 				min_size, max_size = float(min(numbers)), float(max(numbers))
			# 			elif len(numbers) == 1:
			# 				min_size = max_size = float(numbers[0])
			# 			else:
			# 				frappe.throw(f"Invalid gemstone size format: {gemstone_size_str}")

			# 			# SQL Query for weight-based price list
			# 			weight_in_cts_gemstone_price_list_entry = frappe.db.sql(
			# 				"""
			# 				SELECT name, cut_or_cab, gemstone_type, stone_shape, gemstone_grade,
			# 					supplier_fg_purchase_rate, from_weight, to_weight, rate, per_pc_or_per_carat
			# 				FROM `tabGemstone Price List`
			# 				WHERE customer = %s
			# 				AND price_list_type = %s
			# 				AND cut_or_cab = %s
			# 				AND gemstone_grade = %s
			# 				AND %s BETWEEN from_weight AND to_weight
			# 				ORDER BY creation DESC
			# 				LIMIT 1
			# 				""",
			# 				(new_bom.customer, gemstone_price_customer, row.get("cut_or_cab"), row.get("gemstone_grade"), min_size),
			# 				as_dict=True
			# 			)

			# 			if weight_in_cts_gemstone_price_list_entry:
			# 				entry = weight_in_cts_gemstone_price_list_entry[0]

			# 				# Add safe access with .get()
			# 				row["fg_purchase_rate"] = flt(entry.get("supplier_fg_purchase_rate", 0))
			# 				row["total_gemstone_rate"] = flt(entry.get("rate", 0))

			# 				# Ensure safe multiplication
			# 				row["fg_purchase_amount"] = (
			# 					row.get("fg_purchase_rate", 0) * row.get("quantity", 0)
			# 					if entry.get("per_pc_or_per_carat") == "Per Carat"
			# 					else row.get("fg_purchase_rate", 0) * row.get("pcs", 0)
			# 				)

			# 		if gemstone_price_customer and any(dpl["price_list_type"] == "Fixed" for dpl in gemstone_price_customer):
			# 		# if gemstone_price_customer == "Fixed":
			# 			fixed_gemstone_price_list_entry = frappe.db.sql(
			# 				"""
			# 				SELECT name, stone_shape, gemstone_type, cut_or_cab, gemstone_grade,
			# 					supplier_fg_purchase_rate, rate, per_pc_or_per_carat
			# 				FROM `tabGemstone Price List`
			# 				WHERE customer = %s
			# 				AND price_list_type = %s
			# 				AND stone_shape = %s
			# 				AND gemstone_type = %s
			# 				AND cut_or_cab = %s
			# 				AND gemstone_grade = %s
			# 				ORDER BY creation DESC
			# 				LIMIT 1
			# 				""",
			# 				(
			# 					new_bom.customer, gemstone_price_customer,
			# 					row.get("stone_shape"), row.get("gemstone_type"),
			# 					row.get("cut_or_cab"), row.get("gemstone_grade")
			# 				),
			# 				as_dict=True
			# 			)

			# 			if fixed_gemstone_price_list_entry:
			# 				entry = fixed_gemstone_price_list_entry[0]

			# 				row["fg_purchase_rate"] = flt(entry.get("supplier_fg_purchase_rate", 0))
			# 				row["total_gemstone_rate"] = flt(entry.get("rate", 0))

			# 				row["fg_purchase_amount"] = row.get("fg_purchase_rate", 0) * row.get("quantity", 0)
			# 	# Add attributes to row
			# 	for attribute in item_row.attributes:
			# 		attribute_name = attribute.attribute.lower().replace(" ", "_")
			# 		row[attribute_name] = attribute.attribute_value

			# 	# Check for Customer Goods
			# 	if item.get("inventory_type") == "Customer Goods":
			# 		row["is_customer_item"] = 1

			# 	row["pcs"] = item.get("pcs", 0)

			if self.company == "Gurukrupa Export Private Limited":
				gemstone_price_list_customer = frappe.db.get_value("Customer", new_bom.customer, "custom_gemstone_price_list_type")
				# frappe.throw(f"hi,{gemstone_price_list_customer}")
				customer_group = frappe.db.get_value("Customer", new_bom.customer, "customer_group")
				gemstone_price_customer  = frappe.get_all(
						"Gemstone Price List",
						filters={"customer": new_bom.customer, "price_list_type": gemstone_price_list_customer,"gemstone_grade":row.get("gemstone_grade"),"cut_or_cab":row.get("cut_or_cab"),"gemstone_type":row.get("gemstone_type"),"stone_shape":row.get("stone_shape")},
						fields=["name", "price_list_type"],
					)
				# frappe.throw(f"hi,{new_bom.customer}")
				retail_price_list = frappe.get_all(
					"Gemstone Price List",
					filters={"is_standard": 0,"is_retail_customer":1},
					fields=["name", "price_list_type"],
					order_by="creation desc",
					limit=1
				)
				# frappe.throw(f"{retail_price_list}")
				if retail_price_list and retail_price_list[0]["price_list_type"] == "Diamond Range":
						# frappe.throw(f"jii")
						combined_query = frappe.db.sql("""
								SELECT gpl.name, gpl.cut_or_cab, gpl.gemstone_grade,
									gm.gemstone_type, 
										gm.precious_percentage as precious_percentage, gm.semi_precious_percentage as semi_precious_percentage, gm.synthetic_percentage as synthetic_percentage,
								sfm.precious_percentage AS supplier_precious_percentage, 
								sfm.semi_precious_percentage AS supplier_semi_precious_percentage, 
									sfm.synthetic_percentage AS supplier_synthetic_percentage
									 FROM `tabGemstone Price List` gpl
								INNER JOIN `tabGemstone Multiplier` gm
									ON gm.parent = gpl.name 
									AND gm.gemstone_type = %s 
									AND gm.parentfield = 'gemstone_multiplier'
								LEFT JOIN `tabGemstone Multiplier` sfm
									ON sfm.parent = gpl.name 
									AND sfm.gemstone_type = %s 
									AND sfm.parentfield = 'supplier_fg_multiplier'
								WHERE 
								gpl.price_list_type = %s
								AND gpl.cut_or_cab LIKE %s
								AND gpl.gemstone_grade LIKE %s
								AND %s BETWEEN gpl.from_gemstone_pr_rate AND gpl.to_gemstone_pr_rate
								ORDER BY gpl.creation DESC
								LIMIT 1
								""", (
									row.get('gemstone_type'),
									row.get('gemstone_type'),
									gemstone_price_list_customer,
									f"%{row.get('cut_or_cab')}%",
									f"%{row.get('gemstone_grade')}%",
									row.get('gemstone_pr')
								), as_dict=True)
						# frappe.throw(f"""
						# 	gemstone_type: {row.get('gemstone_type')},
						# 	customer: {new_bom.customer},
						# 	price_list_type: {gemstone_price_list_customer},
						# 	cut_or_cab: {row.get('cut_or_cab')},
						# 	gemstone_grade: {row.get('gemstone_grade')},
						# 	gemstone_pr: {row.get('gemstone_pr')}
						# 	""")
						# frappe.throw(f"{combined_query}")
						if combined_query:
							entry = combined_query[0]
							gemstone_quality = row.get("gemstone_quality")
							gemstone_pr = flt(row.get("gemstone_pr", 0))
						
							multiplier_selected_value = entry.get("supplier_precious_percentage") if gemstone_quality == "Precious" else \
														entry.get("supplier_semi_precious_percentage") if gemstone_quality == "Semi Precious" else \
														entry.get("supplier_synthetic_percentage") if gemstone_quality == "Synthetic" else None

							supplier_selected_value = entry.get("supplier_precious") if gemstone_quality == "Precious" else \
													entry.get("supplier_semi_precious") if gemstone_quality == "Semi Precious" else \
													entry.get("supplier_synthetic") if gemstone_quality == "Synthetic" else None
							# frappe.throw(f"hiiki,{multiplier_selected_value}")
							if multiplier_selected_value is not None:
								row["total_gemstone_rate"] = multiplier_selected_value
								row["gemstone_rate_for_specified_quantity"] =( row["total_gemstone_rate"]/100) * gemstone_pr
								

							if supplier_selected_value is not None:
								row["fg_purchase_rate"] = supplier_selected_value
								row["fg_purchase_amount"] = row["fg_purchase_rate"] * gemstone_pr
								# frappe.throw(f"{combined_query}")	
				if not gemstone_price_customer and customer_group != "Retail": 
					# Standard price list check karo
					standard_price_list = frappe.get_all(
						"Gemstone Price List",
						filters={"is_standard": 1},
						fields=["name", "price_list_type"],
						order_by="creation desc",
						limit=1
					)
				
					if standard_price_list and standard_price_list[0]["price_list_type"] == "Diamond Range":
						# gemstone_pr = row.get("gemstone_pr")
						# if gemstone_pr is None :
						# 		frappe.throw("Gemstone price is missing in the row data.")
						# frappe.throw(f"jii")
						combined_query = frappe.db.sql("""
								SELECT gpl.name, gpl.cut_or_cab, gpl.gemstone_grade,
									gm.gemstone_type, 
										gm.precious_percentage as precious_percentage, gm.semi_precious_percentage as semi_precious_percentage, gm.synthetic_percentage as synthetic_percentage,
								sfm.precious_percentage AS supplier_precious_percentage, 
								sfm.semi_precious_percentage AS supplier_semi_precious_percentage, 
									sfm.synthetic_percentage AS supplier_synthetic_percentage
									 FROM `tabGemstone Price List` gpl
								INNER JOIN `tabGemstone Multiplier` gm
									ON gm.parent = gpl.name 
									AND gm.gemstone_type = %s 
									AND gm.parentfield = 'gemstone_multiplier'
								LEFT JOIN `tabGemstone Multiplier` sfm
									ON sfm.parent = gpl.name 
									AND sfm.gemstone_type = %s 
									AND sfm.parentfield = 'supplier_fg_multiplier'
								WHERE 
								gpl.price_list_type = %s
								AND gpl.cut_or_cab LIKE %s
								AND gpl.gemstone_grade LIKE %s
								AND %s BETWEEN gpl.from_gemstone_pr_rate AND gpl.to_gemstone_pr_rate
								ORDER BY gpl.creation DESC
								LIMIT 1
								""", (
									row.get('gemstone_type'),
									row.get('gemstone_type'),
									gemstone_price_list_customer,
									f"%{row.get('cut_or_cab')}%",
									f"%{row.get('gemstone_grade')}%",
									row.get('gemstone_pr')
								), as_dict=True)
						
						if combined_query:
							entry = combined_query[0]
							gemstone_quality = row.get("gemstone_quality")
							gemstone_pr = flt(row.get("gemstone_pr", 0))
							# frappe.throw(f"hi,{gemstone_quality}")

							# multiplier_selected_value = entry.get("precious") if gemstone_quality == "Precious" else \
							# 							entry.get("semi_precious") if gemstone_quality == "Semi Precious" else \
							# 							entry.get("synthetic") if gemstone_quality == "Synthetic" else None

							multiplier_selected_value = entry.get("supplier_precious_percentage") if gemstone_quality == "Precious" else \
														entry.get("supplier_semi_precious_percentage") if gemstone_quality == "Semi Precious" else \
														entry.get("supplier_synthetic_percentage") if gemstone_quality == "Synthetic" else None

							supplier_selected_value = entry.get("supplier_precious") if gemstone_quality == "Precious" else \
													entry.get("supplier_semi_precious") if gemstone_quality == "Semi Precious" else \
													entry.get("supplier_synthetic") if gemstone_quality == "Synthetic" else None
							# frappe.throw(f"hiiki,{multiplier_selected_value}")
							if multiplier_selected_value is not None:
								row["total_gemstone_rate"] = multiplier_selected_value
								row["gemstone_rate_for_specified_quantity"] =( row["total_gemstone_rate"]/100) * gemstone_pr
							# frappe.throw(f"hiii,{row["gemstone_rate_for_specified_quantity"] }")	

							if supplier_selected_value is not None:
								row["fg_purchase_rate"] = supplier_selected_value
								row["fg_purchase_amount"] = row["fg_purchase_rate"] * gemstone_pr
								# frappe.throw(f"{combined_query}")	
				
				if gemstone_price_customer and any(dpl["price_list_type"] == gemstone_price_list_customer for dpl in gemstone_price_customer):
					
					if gemstone_price_customer and any(dpl["price_list_type"] == "Diamond Range" for dpl in gemstone_price_customer):
						combined_query = frappe.db.sql("""
							SELECT gpl.name, gpl.cut_or_cab, gpl.gemstone_grade,
								gm.gemstone_type, 
								sfm.precious_percentage AS supplier_precious_percentage, 
							sfm.semi_precious_percentage AS supplier_semi_precious_percentage, 
								sfm.synthetic_percentage AS supplier_synthetic_percentage,
									 gm.precious_percentage as precious_percentage, gm.semi_precious_percentage as semi_precious_percentage, gm.synthetic_percentage as synthetic_percentage
							FROM `tabGemstone Price List` gpl
							INNER JOIN `tabGemstone Multiplier` gm
								ON gm.parent = gpl.name 
								AND gm.gemstone_type = %s 
								AND gm.parentfield = 'gemstone_multiplier'
							LEFT JOIN `tabGemstone Multiplier` sfm
								ON sfm.parent = gpl.name 
								AND sfm.gemstone_type = %s 
								AND sfm.parentfield = 'supplier_fg_multiplier'
							WHERE gpl.customer = %s
							AND gpl.price_list_type = %s
							AND gpl.cut_or_cab LIKE %s
							AND gpl.gemstone_grade LIKE %s
							AND %s BETWEEN gpl.from_gemstone_pr_rate AND gpl.to_gemstone_pr_rate
							ORDER BY gpl.creation DESC
							LIMIT 1
							""", (
								row.get('gemstone_type'),
								row.get('gemstone_type'),
								new_bom.customer,
								gemstone_price_list_customer,
								f"%{row.get('cut_or_cab')}%",
								f"%{row.get('gemstone_grade')}%",
								row.get('gemstone_pr')
							), as_dict=True)

						# frappe.throw(f"{row.get('gemstone_type')}")

						# frappe.throw(f"{combined_query}")
						# frappe.throw(f"hi,{combined_query[0]}")
						if combined_query:
							entry = combined_query[0]
							gemstone_quality = row.get("gemstone_quality")
							gemstone_pr = flt(row.get("gemstone_pr", 0))
							# frappe.throw(f"hi,{gemstone_quality}")

							# multiplier_selected_value = entry.get("precious") if gemstone_quality == "Precious" else \
							# 							entry.get("semi_precious") if gemstone_quality == "Semi Precious" else \
							# 							entry.get("synthetic") if gemstone_quality == "Synthetic" else None

							multiplier_selected_value = entry.get("supplier_precious_percentage") if gemstone_quality == "Precious" else \
														entry.get("supplier_semi_precious_percentage") if gemstone_quality == "Semi Precious" else \
														entry.get("supplier_synthetic_percentage") if gemstone_quality == "Synthetic" else None

							supplier_selected_value = entry.get("supplier_precious") if gemstone_quality == "Precious" else \
													entry.get("supplier_semi_precious") if gemstone_quality == "Semi Precious" else \
													entry.get("supplier_synthetic") if gemstone_quality == "Synthetic" else None
							# frappe.throw(f"hiii,{multiplier_selected_value}")
							if multiplier_selected_value is not None:
								row["total_gemstone_rate"] = multiplier_selected_value
								row["gemstone_rate_for_specified_quantity"] =( row["total_gemstone_rate"]/100) * gemstone_pr
							# frappe.throw(f"hiii,{row["gemstone_rate_for_specified_quantity"] }")	

							if supplier_selected_value is not None:
								row["fg_purchase_rate"] = supplier_selected_value
								row["fg_purchase_amount"] = row["fg_purchase_rate"] * gemstone_pr
					
					# if gemstone_price_customer and any(dpl["price_list_type"] == "Weight (in cts)" for dpl in gemstone_price_customer):
					# # if gemstone_price_customer == "Weight (in cts)":
					# 	import re

					# 	gemstone_size_str = row.get("gemstone_size", "")
					# 	numbers = re.findall(r"[-+]?\d*\.\d+|\d+", gemstone_size_str)

					# 	if len(numbers) == 2:
					# 		min_size, max_size = float(min(numbers)), float(max(numbers))
					# 	elif len(numbers) == 1:
					# 		min_size = max_size = float(numbers[0])
					# 	else:
					# 		frappe.throw(f"Invalid gemstone size format: {gemstone_size_str}")

					# 	# SQL Query for weight-based price list
					# 	weight_in_cts_gemstone_price_list_entry = frappe.db.sql(
					# 		"""
					# 		SELECT name, cut_or_cab, gemstone_type, stone_shape, gemstone_grade,
					# 			supplier_fg_purchase_rate, from_weight, to_weight, rate, per_pc_or_per_carat
					# 		FROM `tabGemstone Price List`
					# 		WHERE customer = %s
					# 		AND price_list_type = %s
					# 		AND cut_or_cab = %s
					# 		AND gemstone_grade = %s
					# 		AND %s BETWEEN from_weight AND to_weight
					# 		ORDER BY creation DESC
					# 		LIMIT 1
					# 		""",
					# 		(new_bom.customer, gemstone_price_customer, row.get("cut_or_cab"), row.get("gemstone_grade"), min_size),
					# 		as_dict=True
					# 	)

					# 	if weight_in_cts_gemstone_price_list_entry:
					# 		entry = weight_in_cts_gemstone_price_list_entry[0]

					# 		# Add safe access with .get()
					# 		row["fg_purchase_rate"] = flt(entry.get("supplier_fg_purchase_rate", 0))
					# 		row["total_gemstone_rate"] = flt(entry.get("rate", 0))

					# 		# Ensure safe multiplication
					# 		row["fg_purchase_amount"] = (
					# 			row.get("fg_purchase_rate", 0) * row.get("quantity", 0)
					# 			if entry.get("per_pc_or_per_carat") == "Per Carat"
					# 			else row.get("fg_purchase_rate", 0) * row.get("pcs", 0)
					# 		)

					if gemstone_price_customer and any(dpl["price_list_type"] == "Fixed" for dpl in gemstone_price_customer):
					# if gemstone_price_customer == "Fixed":
						fixed_gemstone_price_list_entry = frappe.db.sql(
							"""
							SELECT name, stone_shape, gemstone_type, cut_or_cab, gemstone_grade,
								supplier_fg_purchase_rate, rate, per_pc_or_per_carat
							FROM `tabGemstone Price List`
							WHERE customer = %s
							AND price_list_type = %s
							AND stone_shape = %s
							AND gemstone_type = %s
							AND cut_or_cab = %s
							AND gemstone_grade = %s
							ORDER BY creation DESC
							LIMIT 1
							""",
							(
								new_bom.customer, gemstone_price_customer,
								row.get("stone_shape"), row.get("gemstone_type"),
								row.get("cut_or_cab"), row.get("gemstone_grade")
							),
							as_dict=True
						)

						if fixed_gemstone_price_list_entry:
							entry = fixed_gemstone_price_list_entry[0]

							row["fg_purchase_rate"] = flt(entry.get("supplier_fg_purchase_rate", 0))
							row["total_gemstone_rate"] = flt(entry.get("rate", 0))

							row["fg_purchase_amount"] = row.get("fg_purchase_rate", 0) * row.get("quantity", 0)
				# Add attributes to row
				for attribute in item_row.attributes:
					attribute_name = attribute.attribute.lower().replace(" ", "_")
					row[attribute_name] = attribute.attribute_value

				# Check for Customer Goods
				if item.get("inventory_type") == "Customer Goods":
					row["is_customer_item"] = 1

				row["pcs"] = item.get("pcs", 0)
				# row["gemstone_rate_for_specified_quantity"] = row["quantity"] * row["total_gemstone_rate"]

			else:
				if gemstone_price_list and any(dpl["price_list_type"] == gemstone_price_list_ref_customer for dpl in gemstone_price_list):
					# if gemstone_price_list_ref_customer == "Multiplier":
					if gemstone_price_customer and any(dpl["price_list_type"] == "Diamond Range" for dpl in gemstone_price_customer):
						combined_query = frappe.db.sql(
							"""
							SELECT gpl.name, gpl.cut_or_cab, gpl.gemstone_grade,
								gm.item_category, gm.precious, gm.semi_precious, gm.synthetic,
								sfm.precious AS supplier_precious, sfm.semi_precious AS supplier_semi_precious, sfm.synthetic AS supplier_synthetic
							FROM `tabGemstone Price List` gpl
							INNER JOIN `tabGemstone Multiplier` gm
								ON gm.parent = gpl.name AND gm.item_category = %s AND gm.parentfield = 'gemstone_multiplier'
							LEFT JOIN `tabGemstone Multiplier` sfm
								ON sfm.parent = gpl.name AND sfm.item_category = %s AND sfm.parentfield = 'supplier_fg_multiplier'
							WHERE gpl.customer = %s
							AND gpl.price_list_type = %s
							AND gpl.cut_or_cab = %s
							AND gpl.gemstone_grade = %s
							AND %s BETWEEN gpl.from_gemstone_pr_rate AND gpl.to_gemstone_pr_rate
							ORDER BY gpl.creation DESC
							LIMIT 1
							""",
							(new_bom.item_category, 
							 new_bom.item_category, 
							 ref_customer, 
							 gemstone_price_list_ref_customer, 
							 row.get("cut_or_cab"), 
							 row.get("gemstone_grade")),
							 row.get('gemstone_pr'),
							as_dict=True
						)

						if combined_query:
							entry = combined_query[0]
							gemstone_quality = row.get("gemstone_quality")
							gemstone_pr = flt(row.get("gemstone_pr", 0))

							    # Multiplier value
							multiplier_value = None
							# percentage_value = None

							if gemstone_quality == "Precious":
								multiplier_value = entry.get("precious")
								percentage_value = entry.get("precious_percentage")
							elif gemstone_quality == "Semi Precious":
								multiplier_value = entry.get("semi_precious")
								percentage_value = entry.get("semi_precious_percentage")
							elif gemstone_quality == "Synthetic":
								multiplier_value = entry.get("synthetic")
								percentage_value = entry.get("synthetic_percentage")

							if multiplier_value not in [None, 0] and multiplier_value > 0:
								row["total_gemstone_rate"] = multiplier_value
								row["gemstone_rate_for_specified_quantity"] = row["total_gemstone_rate"] * gemstone_pr

							elif percentage_value not in [None, 0] and percentage_value > 0:
								# Agar multiplier 0 hai, percentage se calculate karo
								row["total_gemstone_rate"] = gemstone_pr * (percentage_value / 100)
								row["gemstone_rate_for_specified_quantity"] = row["total_gemstone_rate"] * gemstone_pr

							supplier_selected_value = entry.get("supplier_precious") if gemstone_quality == "Precious" else \
													entry.get("supplier_semi_precious") if gemstone_quality == "Semi Precious" else \
													entry.get("supplier_synthetic") if gemstone_quality == "Synthetic" else None

							# if multiplier_selected_value is not None:
							# 	row["total_gemstone_rate"] = multiplier_selected_value
							# 	row["gemstone_rate_for_specified_quantity"] = row["total_gemstone_rate"] * gemstone_pr

							if supplier_selected_value is not None:
								row["fg_purchase_rate"] = supplier_selected_value
								row["fg_purchase_amount"] = row["fg_purchase_rate"] * gemstone_pr

							# Handle Fixed price list
					if gemstone_price_customer and any(dpl["price_list_type"] == "Fixed" for dpl in gemstone_price_customer):
					# if gemstone_price_list_ref_customer == "Fixed":
						fixed_gemstone_price_list_entry = frappe.db.sql(
							"""
							SELECT name, stone_shape, gemstone_type, cut_or_cab, gemstone_grade,
								supplier_fg_purchase_rate, rate, per_pc_or_per_carat
							FROM `tabGemstone Price List`
							WHERE customer = %s
							AND price_list_type = %s
							AND stone_shape = %s
							AND gemstone_type = %s
							AND cut_or_cab = %s
							AND gemstone_grade = %s
							ORDER BY creation DESC
							LIMIT 1
							""",
							(
								ref_customer, gemstone_price_list_ref_customer,
								row.get("stone_shape"), row.get("gemstone_type"),
								row.get("cut_or_cab"), row.get("gemstone_grade")
							),
							as_dict=True
						)

						if fixed_gemstone_price_list_entry:
							entry = fixed_gemstone_price_list_entry[0]

							row["fg_purchase_rate"] = flt(entry.get("supplier_fg_purchase_rate", 0))
							row["total_gemstone_rate"] = flt(entry.get("rate", 0))

							row["fg_purchase_amount"] = row.get("fg_purchase_rate", 0) * row.get("quantity", 0)
					

					# if gemstone_price_customer and any(dpl["price_list_type"] == "Weight (in cts)" for dpl in gemstone_price_customer):
					# # if gemstone_price_list_ref_customer == "Weight (in cts)":
					# 	import re

					# 	gemstone_size_str = row.get("gemstone_size", "")
					# 	numbers = re.findall(r"[-+]?\d*\.\d+|\d+", gemstone_size_str)

					# 	if len(numbers) == 2:
					# 		min_size, max_size = float(min(numbers)), float(max(numbers))
					# 	elif len(numbers) == 1:
					# 		min_size = max_size = float(numbers[0])
					# 	else:
					# 		frappe.throw(f"Invalid gemstone size format: {gemstone_size_str}")

					# 	# SQL Query for weight-based price list
					# 	weight_in_cts_gemstone_price_list_entry = frappe.db.sql(
					# 		"""
					# 		SELECT name, cut_or_cab, gemstone_type, stone_shape, gemstone_grade,
					# 			supplier_fg_purchase_rate, from_weight, to_weight, rate, per_pc_or_per_carat
					# 		FROM `tabGemstone Price List`
					# 		WHERE customer = %s
					# 		AND price_list_type = %s
					# 		AND cut_or_cab = %s
					# 		AND gemstone_grade = %s
					# 		AND %s BETWEEN from_weight AND to_weight
					# 		ORDER BY creation DESC
					# 		LIMIT 1
					# 		""",
					# 		(ref_customer, gemstone_price_list_ref_customer, row.get("cut_or_cab"), row.get("gemstone_grade"), min_size),
					# 		as_dict=True
					# 	)

					# 	if weight_in_cts_gemstone_price_list_entry:
					# 		entry = weight_in_cts_gemstone_price_list_entry[0]

					# 		# Add safe access with .get()
					# 		row["fg_purchase_rate"] = flt(entry.get("supplier_fg_purchase_rate", 0))
					# 		row["total_gemstone_rate"] = flt(entry.get("rate", 0))

					# 		# Ensure safe multiplication
					# 		row["fg_purchase_amount"] = (
					# 			row.get("fg_purchase_rate", 0) * row.get("quantity", 0)
					# 			if entry.get("per_pc_or_per_carat") == "Per Carat"
					# 			else row.get("fg_purchase_rate", 0) * row.get("pcs", 0)
					# 		)

				
				
				# Add attributes to row
				for attribute in item_row.attributes:
					attribute_name = attribute.attribute.lower().replace(" ", "_")
					row[attribute_name] = attribute.attribute_value

				# Check for Customer Goods
				if item.get("inventory_type") == "Customer Goods":
					row["is_customer_item"] = 1

				row["pcs"] = item.get("pcs", 0)
			# Append to BOM
			new_bom.append("gemstone_detail", row)

			# Calculate totals safely with flt() to avoid NoneType errors
			new_bom.gemstone_bom_amount = sum(flt(row.get("gemstone_rate_for_specified_quantity", 0)) for row in new_bom.get("gemstone_detail", []))
			new_bom.gemstone_fg_purchase = sum(flt(row.get("fg_purchase_rate", 0)) for row in new_bom.get("gemstone_detail", []))
			new_bom.total_gemstone_pcs = sum(flt(row.get("pcs", 0)) for row in new_bom.get("gemstone_detail", []))
			new_bom.total_gemstone_weight = sum(flt(row.get("quantity", 0)) for row in new_bom.get("gemstone_detail", []))
			new_bom.total_gemstone_weight_per_gram = sum(flt(row.get("weight_in_gms", 0)) for row in new_bom.get("gemstone_detail", []))
			new_bom.total_gemstone_amount = sum(flt(row.get("gemstone_rate_for_specified_quantity", 0)) for row in new_bom.get("gemstone_detail", []))

		elif item_row.variant_of == "O":
			row = {}
			row["se_rate"] = item.get("rate")
			for attribute in item_row.attributes:
				atrribute_name = format_attrbute_name(attribute.attribute)
				row[atrribute_name] = attribute.attribute_value
				row["item_code"] = item_row.name
				row["quantity"] = item["qty"] / pmo_data.get("qty")
				row["qty"] = item["qty"]
				row["uom"] = "Gram"
			new_bom.append("other_detail", row)
	new_bom.making_charge = new_bom.custom_metal_amount + new_bom.custom_finding_amount
	new_bom.making_fg_purchase = new_bom.custom_fg_metal_amount + new_bom.custom_finding_fg_amount
	new_bom.finding_weight_ = new_bom.finding_weight_
	new_bom.metal_weight = new_bom.total_metal_weight
	new_bom.metal_and_finding_weight = new_bom.finding_weight_ + new_bom.metal_weight
	new_bom.diamond_weight = new_bom.total_diamond_weight
	new_bom.total_diamond_weight_in_gms = new_bom.diamond_weight / 5
	new_bom.gemstone_weight = new_bom.total_gemstone_weight
	new_bom.total_gemstone_weight_in_gms = new_bom.gemstone_weight / 5
	new_bom.gross_weight = (
	new_bom.metal_weight
	+ new_bom.finding_weight_
	+ new_bom.total_diamond_weight_in_gms
	+ new_bom.total_gemstone_weight_in_gms
	)
	new_bom.total_bom_amount = (
	new_bom.diamond_bom_amount
	+ new_bom.total_metal_amount
	+ new_bom.making_charge
	+ new_bom.finding_bom_amount
	+ new_bom.gemstone_bom_amount
	)
	new_bom.gold_to_diamond_ratio = (
	flt(new_bom.metal_weight + new_bom.finding_weight_) / flt(new_bom.total_diamond_weight_in_gms)
	if new_bom.total_diamond_weight_in_gms else 0
	)
	new_bom.diamond_ratio = (
	flt(new_bom.total_diamond_weight_in_gms) / flt(new_bom.total_diamond_pcs)
	if new_bom.total_diamond_pcs else 0
	)
	new_bom.insert(ignore_mandatory=True)
	new_bom.submit()
	frappe.db.set_value("Serial No", new_bom.tag_no, "custom_bom_no", new_bom.name)
	self.fg_bom = new_bom.name


def get_stock_entry_data(self):
	target_wh = frappe.db.get_value(
		"Warehouse", {"disabled": 0, "department": self.department, "warehouse_type": "Manufacturing"}
	)
	pmo = frappe.db.get_value(
		"Manufacturing Work Order", self.manufacturing_work_order, "manufacturing_order"
	)
	# se = frappe.new_doc("Stock Entry")
	# se.stock_entry_type = "Manufacture"
	mop = frappe.get_all(
		"Manufacturing Work Order",
		{
			"name": ["!=", self.manufacturing_work_order],
			"manufacturing_order": pmo,
			"docstatus": ["!=", 2],
			"department": ["=", self.department],
		},
		pluck="manufacturing_operation",
	)
	StockEntry = frappe.qb.DocType("Stock Entry")
	StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")

	data = (
		frappe.qb.from_(StockEntryDetail)
		.left_join(StockEntry)
		.on(StockEntryDetail.parent == StockEntry.name)
		.select(
			StockEntryDetail.custom_manufacturing_work_order,
			StockEntry.manufacturing_operation,
			StockEntryDetail.parent,
			StockEntryDetail.item_code,
			StockEntryDetail.item_name,
			StockEntryDetail.qty,
			StockEntryDetail.uom,
			StockEntryDetail.inventory_type,
			StockEntryDetail.pcs,
			Avg(StockEntryDetail.basic_rate).as_("rate"),
		)
		.where(
			(StockEntry.docstatus == 1)
			& (StockEntryDetail.manufacturing_operation.isin(mop))
			& (StockEntryDetail.t_warehouse == target_wh)
		)
		.groupby(
			StockEntryDetail.manufacturing_operation,
			StockEntryDetail.item_code,
			StockEntryDetail.qty,
			StockEntryDetail.uom,
		)
	).run(as_dict=True)

	return data


def format_attrbute_name(input_string):
	# Replace spaces with underscores and convert to lowercase
	formatted_string = input_string.replace(" ", "_").replace("-", "_").lower()
	return formatted_string


def get_serial_no(se_name):
	# se_doc = frappe.get_doc('Stock Entry',se_name)
	# for row in se_doc.items:
	# 	if row.is_finished_item:
	# 		serial_no = row.serial_no
	serial_no = se_name
	return str(serial_no)


def finish_other_tagging_operations(doc, pmo):
	ManufacturingOperation = frappe.qb.DocType("Manufacturing Operation")

	mop_data = (
		frappe.qb.from_(ManufacturingOperation)
		.select(
			ManufacturingOperation.manufacturing_order,
			ManufacturingOperation.name.as_("manufacturing_operation"),
			ManufacturingOperation.status,
		)
		.where(
			(ManufacturingOperation.manufacturing_order == pmo)
			& (ManufacturingOperation.name != doc.manufacturing_operation)
			& (ManufacturingOperation.status != "Finished")
			& (ManufacturingOperation.department == doc.department)
		)
	).run(
		as_dict=True
	)  # name

	for mop in mop_data:
		frappe.db.set_value("Manufacturing Operation", mop.manufacturing_operation, "status", "Finished")


# timer code
@frappe.whitelist()
def make_time_log(data):
	if isinstance(data, str):
		args = json.loads(data)
	args = frappe._dict(args)
	doc = frappe.get_doc("Manufacturing Operation", args.job_card_id)
	# doc.validate_sequence_id()
	doc.add_time_log(args)


def update_new_mop(self, old_mop):
	import copy

	d_warehouse = None
	e_warehouse = None
	if self.department:
		d_warehouse = frappe.db.get_value(
			"Warehouse", {"disabled": 0, "department": self.department, "warehouse_type": "Manufacturing"}
		)
	if self.employee:
		e_warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"company": self.company,
				"employee": self.employee,
				"warehouse_type": "Manufacturing",
			},
		)

	if self.previous_mop:

		existing_data = {
			"department_source_table": [],
			"department_target_table": [],
			"employee_source_table": [],
			"employee_target_table": [],
		}

		department_source_table = []
		department_target_table = []
		employee_source_table = []
		employee_target_table = []

		for row in existing_data:
			for entry in self.get(row):
				if entry.get("sed_item") and entry.get("sed_item") not in existing_data[row]:
					existing_data[row].append(entry.get("sed_item"))

			for entry in old_mop.get(row):
				if entry.s_warehouse == d_warehouse:
					entry.name = None
					department_source_table.append(entry.__dict__)
				if entry.t_warehouse == d_warehouse:
					entry.name = None
					department_target_table.append(entry.__dict__)
				if entry.s_warehouse == e_warehouse:
					entry.name = None
					employee_source_table.append(entry.__dict__)
				if entry.t_warehouse == e_warehouse:
					entry.name = None
					employee_target_table.append(entry.__dict__)

		for row in department_source_table:
			temp_row = copy.deepcopy(row)
			if temp_row["sed_item"] not in existing_data["department_source_table"]:
				temp_row["name"] = None
				temp_row["idx"] = None
				self.append("department_source_table", row)

		for row in department_target_table:
			temp_row = copy.deepcopy(row)
			if temp_row["sed_item"] not in existing_data["department_target_table"]:
				temp_row["name"] = None
				temp_row["idx"] = None
				self.append("department_target_table", row)

		for row in employee_source_table:
			temp_row = copy.deepcopy(row)
			if temp_row["sed_item"] not in existing_data["employee_source_table"]:
				temp_row["name"] = None
				temp_row["idx"] = None
				self.append("employee_source_table", row)

		for row in employee_target_table:
			temp_row = copy.deepcopy(row)
			if temp_row["sed_item"] not in existing_data["employee_target_table"]:
				temp_row["name"] = None
				temp_row["idx"] = None
				self.append("employee_target_table", row)


@frappe.whitelist()
def get_bom_summary(design_id_bom:str=None):
	if design_id_bom:
		# use get_all with parent filter

		bom_data = frappe.db.get_all(
			"BOM Item", filters={"parent": design_id_bom}, fields=["item_code", "qty", "uom"]
		)

		# bom_data = frappe.get_doc("BOM", self.design_id_bom)
		item_records = [
			{"item_code": row.item_code, "qty": row.qty, "uom": row.uom} for row in bom_data
		]
		# for bom_row in bom_data.items:
		# 	item_record = {"item_code": bom_row.item_code, "qty": bom_row.qty, "uom": bom_row.uom}
		# 	item_records.append(item_record)
		return frappe.render_template(
			"jewellery_erpnext/jewellery_erpnext/doctype/manufacturing_operation/bom_summery.html",
			{"data": item_records},
		)


@frappe.whitelist()
def get_linked_stock_entries_for_serial_number_creator(mwo, department, design_id_bom, qty):
	target_wh = frappe.db.get_value(
		"Warehouse", {"disabled": 0, "department": department, "warehouse_type": "Manufacturing"}
	)
	pmo = frappe.db.get_value(
		"Manufacturing Work Order", mwo, "manufacturing_order"
	)
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Manufacture"
	operations = frappe.get_all(
		"Manufacturing Work Order",
		{
			"name": ["!=", mwo],
			"manufacturing_order": pmo,
			"docstatus": ["!=", 2],
			"department": ["=", department],
			"finding_transfer_entry":""
		},
		pluck="manufacturing_operation",
	)
	StockEntry = frappe.qb.DocType("Stock Entry")
	StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")
	IF = CustomFunction("IF", ["condition", "true_expr", "false_expr"])
	data = (
		frappe.qb.from_(StockEntryDetail)
		.left_join(StockEntry)
		.on(StockEntryDetail.parent == StockEntry.name)
		.select(
			StockEntryDetail.custom_manufacturing_work_order,
			StockEntryDetail.manufacturing_operation,
			StockEntryDetail.name,
			StockEntryDetail.parent,
			StockEntryDetail.item_code,
			StockEntryDetail.item_name,
			StockEntryDetail.batch_no,
			StockEntryDetail.qty,
			StockEntryDetail.uom,
			StockEntryDetail.inventory_type,
			StockEntryDetail.pcs,
			StockEntryDetail.custom_sub_setting_type,
			IfNull(
				Sum(IF(StockEntryDetail.uom == "Carat", StockEntryDetail.qty * 0.2, StockEntryDetail.qty)), 0
			).as_("gross_wt"),
		)
		.where(
			(StockEntry.docstatus == 1)
			& (StockEntryDetail.manufacturing_operation.isin(operations))
			& (StockEntryDetail.t_warehouse == target_wh)
		)
		.groupby(
			StockEntryDetail.manufacturing_operation,
			StockEntryDetail.item_code,
			StockEntryDetail.qty,
			StockEntryDetail.uom,
		)
	).run(as_dict=True)

	total_qty = 0
	for row in data:
		total_qty += row.get("gross_wt", 0)
	total_qty = round(total_qty, 4)  # sum(item['qty'] for item in data)
	bom_id = design_id_bom  # self.fg_bom
	mnf_qty = qty
	return data, bom_id, mnf_qty, total_qty



@frappe.whitelist()
def get_linked_stock_entries(mwo, department):
	target_wh = frappe.db.get_value("Warehouse", {"disabled": 0, "department": department})
	pmo = frappe.db.get_value(
		"Manufacturing Work Order", mwo, "manufacturing_order"
	)
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Manufacture"
	mwo = frappe.get_all(
		"Manufacturing Work Order",
		{
			"name": ["!=", mwo],
			"manufacturing_order": pmo,
			"docstatus": ["!=", 2],
			"department": ["=", department],
		},
		pluck="name",
	)
	if mwo == []:
		return
	StockEntry = frappe.qb.DocType("Stock Entry")
	StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")
	IF = CustomFunction("IF", ["condition", "true_expr", "false_expr"])
	data = (
		frappe.qb.from_(StockEntryDetail)
		.left_join(StockEntry)
		.on(StockEntryDetail.parent == StockEntry.name)
		.select(
			StockEntry.manufacturing_work_order,
			StockEntry.manufacturing_operation,
			StockEntryDetail.parent,
			StockEntryDetail.item_code,
			StockEntryDetail.item_name,
			StockEntryDetail.batch_no,
			StockEntryDetail.qty,
			StockEntryDetail.uom,
			IfNull(
				Sum(IF(StockEntryDetail.uom == "Carat", StockEntryDetail.qty * 0.2, StockEntryDetail.qty)), 0
			).as_("gross_wt"),
		)
		.where(
			(StockEntry.docstatus == 1)
			& (StockEntry.manufacturing_work_order.isin(mwo))
			& (StockEntryDetail.t_warehouse == target_wh)
		)
		.groupby(
			StockEntryDetail.manufacturing_operation,
			StockEntryDetail.item_code,
			StockEntryDetail.qty,
			StockEntryDetail.uom,
		)
	).run(as_dict=True)

	total_qty = 0
	for row in data:
		total_qty += row.get("gross_wt", 0)
	total_qty = round(total_qty, 4)  # sum(item['qty'] for item in data)

	return frappe.render_template(
		"jewellery_erpnext/jewellery_erpnext/doctype/manufacturing_operation/stock_entry_details.html",
		{"data": data, "total_qty": total_qty},
	)

@frappe.whitelist()
def create_mr_wo_stock_entry(se_data):
	from jewellery_erpnext.utils import get_warehouse_from_user

	if isinstance(se_data, str):
		se_data = json.loads(se_data)

	if not se_data.get("receive_items"):
		return frappe.msgprint("No Receive Items Found.")

	department = se_data.get("department")
	t_warehouse = frappe.db.get_value("Warehouse",{"warehouse_type": "Raw Material", "department": department},"name")

	# t_warehouse = get_warehouse_from_user(frappe.session.user, "Raw Material")
	if not t_warehouse:
		frappe.throw("No warehouse found for warehouse type Raw Material")

	s_warehouse = se_data.get("receive_items")[0].get("s_warehouse")
	department = se_data.get("department")
	to_department = se_data.get("receive_items")[0].get("to_department")

	se_doc = frappe.new_doc("Stock Entry")
	se_doc.update({
		"stock_entry_type": "Material Receive (WORK ORDER)",
		"manufacturing_work_order": se_data.get("manufacturing_work_order"),
		"manufacturing_order": se_data.get("manufacturing_order"),
		"manufacturing_operation": se_data.get("manufacturing_operation"),
		"department": department,
		"to_department": to_department,
		"to_warehouse": t_warehouse,
		"from_warehouse": s_warehouse
	})

	def validate_item_material(row):
		variant_of = frappe.db.get_value("Item", row.get("item_code"), "variant_of")

		if variant_of in ["D", "G"]:
			item_type = "Diamond" if variant_of == "D" else "Gemstone"
			frappe.throw(
				f"<b>Row: {row.get('idx')}</b> {item_type} Item should have pcs value. Please provide pcs for <b>{row.get('item_code')}</b>."
			)

	for row in se_data.get("receive_items"):
		# if not row.get("pcs"):
		# 	validate_item_material(row)
		se_doc.append("items", {
			"item_code": row.get("item_code"),
			"qty": row.get("qty"),
			"pcs": row.get("pcs"),
			"use_serial_batch_fields": 1,
			"batch_no": row.get("batch_no"),
			"manufacturing_operation": se_data.get("manufacturing_operation"),
			"s_warehouse": row.get("s_warehouse"),
			"t_warehouse": t_warehouse,
			"inventory_type": row.get("inventory_type"),
			"customer": row.get("customer")
		})

	# set flag to update pcs
	frappe.flags.update_pcs = True

	se_doc.save()
	se_doc.submit()


	return {"doctype": se_doc.doctype, "docname": se_doc.name}
