import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder import DocType

# timer code
from frappe.utils import (
	cint,
	date_diff,
	flt,
	get_datetime,
	get_first_day,
	get_last_day,
	getdate,
	nowdate,
	time_diff,
	time_diff_in_hours,
	time_diff_in_seconds,
	today,
)

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_ir_utils import (
	get_po_rates,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.html_utils import (
	get_summary_data,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject import (
	cancel_injections_for_eir,
	inject_extra_metal_for_eir_receive,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.mould_utils import (
	create_mould,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.subcontracting_utils import (
	create_so_for_subcontracting,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
	validate_duplication_and_gr_wt,
	validate_loss_qty,
	validate_manually_book_loss_details,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	update_new_mop_wtg,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	create_mop_log_for_employee_ir_receive,
	creste_mop_log_for_employee_ir,
)
from jewellery_erpnext.utils import (
	get_item_from_attribute_full,
)


class EmployeeIR(Document):
	@frappe.whitelist()
	def get_operations(self):
		records = frappe.get_list(
			"Manufacturing Operation",
			{
				"department": self.department,
				"employee": ["is", "not set"],
				"operation": ["is", "not set"],
			},
			["name", "gross_wt"],
		)
		self.employee_ir_operations = []
		if records:
			for row in records:
				self.append(
					"employee_ir_operations", {"manufacturing_operation": row.name}
				)

	def on_submit(self):
		validate_qc(self)
		if self.type == "Issue":
			self.validate_qc("Warn")
			self.on_submit_issue_new()
			if self.subcontracting == "Yes":
				self.create_subcontracting_order()
		else:
			self.on_submit_receive()

	def before_validate(self):
		if self.docstatus != 0:
			return
		warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"department": self.department,
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

		validate_duplication_and_gr_wt(self)

	def validate(self):
		# self.validate_gross_wt()
		# self.validate_main_slip()
		# self.update_main_slip()
		self.validate_process_loss()
		validate_manually_book_loss_details(self)
		# valid_reparing_or_next_operation(self)
		validate_loss_qty(self)

	def on_cancel(self):
		if self.type == "Issue":
			self.on_submit_issue_new(cancel=True)
		else:
			self.on_submit_receive(cancel=True)

	def on_submit_issue_new(self, cancel=False):
		# if self.mop_data:
		# 	mop_data = json.loads(self.mop_data)
		# 	return create_single_se_entry(self, mop_data)
		if cancel:
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
		# Set initial values based on cancel flag
		employee = None if cancel else self.employee
		operation = None if cancel else self.operation
		status = "Not Started" if cancel else "WIP"
		values = {"operation": operation, "status": status}
		if self.subcontracting == "Yes":
			values["for_subcontracting"] = 1
			values["subcontractor"] = None if cancel else self.subcontractor
		else:
			values["employee"] = employee

		# mop_data = {}
		mops_to_update = {}
		time_log_args = []
		# stock_entry_data = []
		start_time = frappe.utils.now() if not cancel else None
		from_warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"department": self.department,
				"warehouse_type": "Manufacturing",
			},
		)
		if self.subcontracting == "Yes":
			to_warehouse = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"company": self.company,
					"subcontractor": self.subcontractor,
					"warehouse_type": "Manufacturing",
				},
			)
		else:
			to_warehouse = frappe.db.get_value(
				"Warehouse",
				{
					"warehouse_type": "Manufacturing",
					"disabled": 0,
					"employee": self.employee,
				},
			)
		if not (to_warehouse and from_warehouse):
			frappe.throw(_("To Warehouse or From Warehouse not available"))
		for row in self.employee_ir_operations:
			values.update(
				{
					"operation": operation,
					"rpt_wt_issue": row.rpt_wt_issue,
					"start_time": start_time,
				}
			)
			mops_to_update[row.manufacturing_operation] = values
			if not cancel:
				# stock_entry_data.append(
				# 	(row.manufacturing_work_order, row.manufacturing_operation)
				# )
				# mop_data[row.manufacturing_work_order] = row.manufacturing_operation
				time_log_args.append((row.manufacturing_operation, values))
				creste_mop_log_for_employee_ir(self, row, from_warehouse, to_warehouse)

		if mops_to_update:
			frappe.db.bulk_update(
				"Manufacturing Operation",
				mops_to_update,
				chunk_size=100,
				update_modified=True,
			)

		# Batch add time logs
		if time_log_args and not cancel:
			batch_add_time_logs(self, time_log_args)

	# for receive
	def on_submit_receive(self, cancel=False):
		precision = cint(
			frappe.db.get_single_value("System Settings", "float_precision")
		)

		mwo_loss_dict = {}
		for row in self.manually_book_loss_details + self.employee_loss_details:
			if row.variant_of in ["M", "F"]:
				mwo_loss_dict.setdefault(row.manufacturing_work_order, 0)
				mwo_loss_dict[row.manufacturing_work_order] += row.proportionally_loss

		is_mould_operation = frappe.db.get_value(
			"Department Operation", self.operation, "is_mould_manufacturer"
		)

		# Resolve warehouses for MOP Log entries
		department_wh = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"department": self.department,
				"warehouse_type": "Manufacturing",
			},
		)
		if self.subcontracting == "Yes":
			actor_wh = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"company": self.company,
					"subcontractor": self.subcontractor,
					"warehouse_type": "Manufacturing",
				},
			)
		else:
			actor_wh = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"employee": self.employee,
					"warehouse_type": "Manufacturing",
				},
			)

		curr_time = frappe.utils.now()

		if cancel:
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
			# Cancel any auto-created Main Slip Repack SEs; their on_cancel hook
			# flips the matching MOP Log rows to is_cancelled=1 via the bridge.
			cancel_injections_for_eir(self.name)

		for row in self.employee_ir_operations:
			if is_mould_operation and not cancel:
				create_mould(self, row)
			net_loss_wt = mwo_loss_dict.get(row.manufacturing_work_order) or 0

			net_wt = frappe.db.get_value(
				"Manufacturing Operation", row.manufacturing_operation, "net_wt"
			)
			is_received_gross_greater_than = (
				True if row.received_gross_wt > row.gross_wt else False
			)
			difference_wt = flt(row.received_gross_wt, precision) - flt(
				row.gross_wt, precision
			)

			res = frappe._dict(
				{
					"received_gross_wt": row.received_gross_wt,
					"loss_wt": difference_wt,
					"received_net_wt": flt(net_wt - net_loss_wt, precision),
					"status": "WIP",
					"is_received_gross_greater_than": is_received_gross_greater_than,
				}
			)

			if row.received_gross_wt == 0 and row.gross_wt != 0:
				frappe.throw(_("Row {0}: Received Gross Wt Missing").format(row.idx))

			time_log_args = []
			if not cancel:
				res["status"] = "Finished"
				res["employee"] = self.employee
				new_operation = create_operation_for_next_op(
					row.manufacturing_operation,
					employee_ir=self.name,
					gross_wt=row.gross_wt,
				)
				res["complete_time"] = curr_time
				frappe.db.set_value(
					"Manufacturing Work Order",
					row.manufacturing_work_order,
					"manufacturing_operation",
					new_operation.name,
				)
				time_log_args.append((row.manufacturing_operation, res))

				# Main Slip gain injection: when is_main_slip_required and
				# received_gross_wt > gross_wt, repack the delta from the
				# employee/subcontractor warehouse into the MOP warehouse.
				# The SE bridge then writes the positive MOP Log row that
				# create_mop_log_for_employee_ir_receive will see.
				stock_entry_name = inject_extra_metal_for_eir_receive(self, row)

				create_mop_log_for_employee_ir_receive(
					self, row, actor_wh, department_wh, stock_entry_name
				)
				update_new_mop_wtg(new_operation)
			else:
				for sre in frappe.db.get_all(
					"Stock Reservation Entry",
					{
						"manufacturing_work_order": row.manufacturing_work_order,
						"manufacturing_operation": row.manufacturing_operation,
						"docstatus": 1,
					},
					pluck="name",
				):
					frappe.get_doc("Stock Reservation Entry", sre).cancel()

				next_op_name = frappe.db.get_value(
					"Manufacturing Operation",
					{
						"employee_ir": self.name,
						"previous_mop": row.manufacturing_operation,
					},
				)

				frappe.db.set_value(
					"Manufacturing Work Order",
					row.manufacturing_work_order,
					"manufacturing_operation",
					row.manufacturing_operation,
				)
				if next_op_name:
					frappe.db.set_value(
						"Department IR Operation",
						{
							"docstatus": 2,
							"manufacturing_operation": next_op_name,
						},
						"manufacturing_operation",
						None,
					)
					frappe.delete_doc(
						"Manufacturing Operation",
						next_op_name,
						ignore_permissions=1,
					)

				frappe.db.set_value(
					"Manufacturing Operation",
					row.manufacturing_operation,
					"status",
					"Not Started",
				)

			if row.rpt_wt_receive:
				issue_wt = frappe.db.get_value(
					"Manufacturing Operation",
					row.manufacturing_operation,
					"rpt_wt_issue",
				)
				res["rpt_wt_receive"] = row.rpt_wt_receive
				res["rpt_wt_loss"] = flt(row.rpt_wt_receive - issue_wt, 3)

			frappe.db.set_value(
				"Manufacturing Operation", row.manufacturing_operation, res
			)

			if time_log_args and not cancel:
				batch_add_time_logs(self, time_log_args)

	def validate_qc(self, action="Warn"):
		if not self.is_qc_reqd or self.type == "Receive":
			return

		qc_list = []
		for row in self.employee_ir_operations:
			operation = frappe.db.get_value(
				"Manufacturing Operation",
				row.manufacturing_operation,
				["status"],
				as_dict=1,
			)
			if operation.get("status") == "Not Started":
				if action == "Warn":
					create_qc_record(row, self.operation, self.name)
				qc_list.append(row.manufacturing_operation)
		if qc_list:
			msg = _("Please complete QC for the following: {0}").format(
				", ".join(qc_list)
			)
			if action == "Warn":
				frappe.msgprint(msg)
			elif action == "Stop":
				frappe.msgprint(msg)

	@frappe.whitelist()
	def create_subcontracting_order(self):
		service_item = frappe.db.get_value(
			"Department Operation", self.operation, "service_item"
		)
		if not service_item:
			frappe.throw(_("Please set service item for {0}").format(self.operation))
		po = frappe.new_doc("Purchase Order")
		po.supplier = self.subcontractor
		company = frappe.db.get_value(
			"Company", {"supplier_code": self.subcontractor}, "name"
		)
		po.company = company or self.company
		po.employee_ir = self.name
		po.purchase_type = "FG Purchase"

		for row in self.employee_ir_operations:
			rate = get_po_rates(
				self.subcontractor, self.operation, po.purchase_type, row
			)
			pmo = frappe.db.get_value(
				"Manufacturing Work Order",
				row.manufacturing_work_order,
				"manufacturing_order",
			)
			po.append(
				"items",
				{
					"item_code": service_item,
					"qty": 1,
					"custom_gross_wt": row.gross_wt,
					"rate": flt(rate[0].get("rate_per_gm") * row.gross_wt, 3)
					if rate
					else 0,
					"schedule_date": today(),
					"manufacturing_operation": row.manufacturing_operation,
					"custom_pmo": pmo,
				},
			)
		if not po.items:
			return
		po.flags.ignore_mandatory = True
		po.taxes_and_charges = None
		po.taxes = []
		po.save()
		po.db_set("schedule_date", None)
		for row in po.items:
			row.db_set("schedule_date", None)

		supplier_group = frappe.db.get_value(
			"Supplier", self.subcontractor, "supplier_group"
		)
		if frappe.db.get_value(
			"Supplier Group", supplier_group, "custom_create_so_for_subcontracting"
		):
			create_so_for_subcontracting(po)

	@frappe.whitelist()
	def validate_process_loss(self):
		if (self.docstatus != 0) or self.type == "Issue":
			return
		allowed_loss_percentage = frappe.get_value(
			"Department Operation",
			{"company": self.company, "department": self.department},
			"allowed_loss_percentage",
		)
		rows_to_append = []
		for child in self.employee_ir_operations:
			if child.received_gross_wt and self.type == "Receive":
				mwo = child.manufacturing_work_order
				gwt = child.gross_wt
				opt = child.manufacturing_operation
				r_gwt = child.received_gross_wt
				rows_to_append += self.book_metal_loss(
					mwo, opt, gwt, r_gwt, allowed_loss_percentage
				)

		self.employee_loss_details = []
		proportionally_loss_sum = 0
		for row in rows_to_append:
			proportionally_loss = flt(row["proportionally_loss"], 3)
			if proportionally_loss > 0:
				variant_of = frappe.db.get_value("Item", row["item_code"], "variant_of")
				self.append(
					"employee_loss_details",
					{
						"item_code": row["item_code"],
						"net_weight": row["qty"],
						# "stock_uom": row["stock_uom"],
						"variant_of": variant_of,
						"batch_no": row["batch_no"],
						"manufacturing_work_order": row["manufacturing_work_order"],
						"manufacturing_operation": row["manufacturing_operation"],
						"proportionally_loss": proportionally_loss,
						"received_gross_weight": row["received_gross_weight"],
						"main_slip_consumption": row.get("main_slip_consumption"),
						# "inventory_type": row["inventory_type"],
						"customer": row.get("customer"),
					},
				)
				proportionally_loss_sum += proportionally_loss
		self.mop_loss_details_total = proportionally_loss_sum

	@frappe.whitelist()
	def book_metal_loss(self, mwo, opt, gwt, r_gwt, allowed_loss_percentage=None):
		doc = self
		# mnf_opt = frappe.get_doc("Manufacturing Operation", opt)

		# To Check Tollarance which book a loss down side.
		if allowed_loss_percentage:
			cal = round(flt((100 - allowed_loss_percentage) / 100) * flt(gwt), 2)
			if flt(r_gwt) < cal:
				frappe.throw(
					f"Department Operation Standard Process Loss Percentage set by <b>{allowed_loss_percentage}%. </br> Not allowed to book a loss less than {cal}</b>"
				)
		data = []  # for final data list
		# Fetching Stock Entry based on MNF Work Order
		if gwt != r_gwt:
			mop_balance_table = []
			fields = [
				"item_code",
				"batch_no",
				"pcs_after_transaction_batch_based as qty",
				"pcs_after_transaction_batch_based as pcs",
			]
			for row in frappe.db.get_all(
				"MOP Log",
				{
					"manufacturing_work_order": mwo,
					"manufacturing_operation": opt,
					"is_cancelled": 0,
					"voucher_type": "Employee IR",
				},
				fields,
			):
				mop_balance_table.append(row)
			# Declaration & fetch required value
			metal_item = []  # for check metal or not list
			unique = set()  # for Unique Item_Code
			sum_qty = {}  # for sum of qty matched item

			# getting Metal property from MNF Work Order
			mwo_metal_property = frappe.db.get_value(
				"Manufacturing Work Order",
				mwo,
				[
					"metal_type",
					"metal_touch",
					"metal_purity",
					"master_bom",
					"is_finding_mwo",
				],
				as_dict=1,
			)
			# To Check and pass thgrow Each ITEM metal or not function
			metal_item.append(
				get_item_from_attribute_full(
					mwo_metal_property.metal_type,
					mwo_metal_property.metal_touch,
					mwo_metal_property.metal_purity,
				)
			)
			# To get Final Metal Item

			total_qty = 0
			# To prepare Final Data with all condition's
			for child in mop_balance_table:
				if child["item_code"][0] not in ["M", "F"]:
					continue
				key = (child["item_code"], child["batch_no"], child["qty"])
				if key not in unique:
					unique.add(key)
					total_qty += child["qty"]
					if child["item_code"] in sum_qty:
						sum_qty[child["item_code"], child["batch_no"]]["qty"] += child[
							"qty"
						]
					else:
						sum_qty[child["item_code"], child["batch_no"]] = {
							"item_code": child["item_code"],
							"qty": child["qty"],
							# "stock_uom": child["uom"],
							"batch_no": child["batch_no"],
							"manufacturing_work_order": mwo,
							"manufacturing_operation": opt,
							"pcs": child["pcs"],
							"proportionally_loss": 0.0,
							"received_gross_weight": 0.0,
						}
			data = list(sum_qty.values())

			# -------------------------------------------------------------------------
			# Prepare data and calculation proportionally devide each row based on each qty.
			total_mannual_loss = 0
			if len(doc.manually_book_loss_details) > 0:
				for row in doc.manually_book_loss_details:
					if row.manufacturing_work_order == mwo:
						loss_qty = (
							row.proportionally_loss
							if row.stock_uom != "Carat"
							else (row.proportionally_loss * 0.2)
						)
						total_mannual_loss += loss_qty

			loss = flt(gwt) - flt(r_gwt) - flt(total_mannual_loss)
			ms_consum = 0
			ms_consum_book = 0
			stock_loss = 0
			if loss < 0:
				ms_consum = abs(round(loss, 2))

			# for entry in data:
			# 	total_qty += entry["qty"]
			for entry in data:
				if total_qty != 0 and loss > 0:
					stock_loss = (entry["qty"] * loss) / total_qty
					if stock_loss > 0:
						entry["received_gross_weight"] = entry["qty"] - stock_loss
						entry["proportionally_loss"] = stock_loss
						entry["main_slip_consumption"] = 0
					else:
						ms_consum_book = round(
							(ms_consum * entry["qty"]) / total_qty, 4
						)
						entry["proportionally_loss"] = 0
						entry["received_gross_weight"] = 0
						entry["main_slip_consumption"] = ms_consum_book
			# -------------------------------------------------------------------------
		return data

	@frappe.whitelist()
	def get_summary_data(self):
		return get_summary_data(self)


def create_operation_for_next_op(docname, employee_ir=None, gross_wt=0):
	new_mop_doc = frappe.copy_doc(
		frappe.get_doc("Manufacturing Operation", docname), ignore_no_copy=False
	)
	new_mop_doc.name = None
	new_mop_doc.department_issue_id = None
	new_mop_doc.status = "Not Started"
	new_mop_doc.department_ir_status = None
	new_mop_doc.department_receive_id = None
	new_mop_doc.prev_gross_wt = gross_wt
	new_mop_doc.employee_ir = employee_ir
	new_mop_doc.employee = None
	new_mop_doc.previous_mop = docname
	new_mop_doc.operation = None
	new_mop_doc.previous_se_data_updated = 0
	new_mop_doc.main_slip_no = None
	new_mop_doc.save()
	return new_mop_doc


@frappe.whitelist()
def get_manufacturing_operations(source_name, target_doc=None):
	if not target_doc:
		target_doc = frappe.new_doc("Employee IR")
	elif isinstance(target_doc, str):
		target_doc = frappe.get_doc(json.loads(target_doc))
	if not target_doc.get(
		"employee_ir_operations", {"manufacturing_operation": source_name}
	):
		operation = frappe.db.get_value(
			"Manufacturing Operation",
			source_name,
			[
				"gross_wt",
				"manufacturing_work_order",
				"diamond_wt",
				"diamond_pcs",
				"gemstone_wt",
				"gemstone_pcs",
			],
			as_dict=1,
		)
		target_doc.append(
			"employee_ir_operations",
			{
				"manufacturing_operation": source_name,
				"gross_wt": operation["gross_wt"],
				"manufacturing_work_order": operation["manufacturing_work_order"],
				"diamond_wt": operation.get("diamond_wt"),
				"diamond_pcs": operation.get("diamond_pcs"),
				"gemstone_wt": operation.get("gemstone_wt"),
				"gemstone_pcs": operation.get("gemstone_pcs"),
			},
		)
	return target_doc


def create_qc_record(row, operation, employee_ir):
	item = frappe.db.get_value(
		"Manufacturing Operation", row.manufacturing_operation, "item_code"
	)
	category = frappe.db.get_value("Item", item, "item_category")
	template_based_on_cat = frappe.db.get_all(
		"Category MultiSelect", {"category": category}, pluck="parent"
	)
	templates = frappe.db.get_all(
		"Operation MultiSelect",
		{
			"operation": operation,
			"parent": ["in", template_based_on_cat],
			"parenttype": "Quality Inspection Template",
		},
		pluck="parent",
	)
	if not templates:
		frappe.msgprint(
			f"No Templates found for given category and operation i.e. {category} and {operation}"
		)
	for template in templates:
		# if frappe.db.sql(
		# 	f"""select name from `tabQC` where manufacturing_operation = '{row.manufacturing_operation}' and
		# 			quality_inspection_template = '{template}' and ((docstatus = 1 and status in ('Accepted', 'Force Approved')) or docstatus = 0)"""
		# ):
		QC = DocType("QC")
		query = (
			frappe.qb.from_(QC)
			.select(QC.name)
			.where(
				(QC.manufacturing_operation == row.manufacturing_operation)
				& (QC.quality_inspection_template == template)
				& (
					(
						(QC.docstatus == 1)
						& (QC.status.isin(["Accepted", "Force Approved"]))
					)
					| (QC.docstatus == 0)
				)
			)
		)
		qc_output = query.run(as_dict=True)
		if qc_output:
			continue
		doc = frappe.new_doc("QC")
		doc.manufacturing_work_order = row.manufacturing_work_order
		doc.manufacturing_operation = row.manufacturing_operation
		doc.received_gross_wt = row.received_gross_wt
		doc.employee_ir = employee_ir
		doc.quality_inspection_template = template
		doc.posting_date = frappe.utils.getdate()
		doc.save(ignore_permissions=True)


# timer code
def add_time_log(doc, args):
	doc = frappe.get_doc("Manufacturing Operation", doc)

	doc.status = args.get("status")
	last_row = []
	employees = args.get("employee")

	# if isinstance(employees, str):
	# 	employees = json.loads(employees)
	if doc.time_logs and len(doc.time_logs) > 0:
		last_row = doc.time_logs[-1]

	doc.reset_timer_value(args)
	if last_row and args.get("complete_time"):
		for row in doc.time_logs:
			if not row.to_time:
				row.update(
					{
						"to_time": get_datetime(args.get("complete_time")),
					}
				)
	elif args.get("start_time"):
		new_args = frappe._dict(
			{
				"from_time": get_datetime(args.get("start_time")),
			}
		)

		if employees:
			new_args.employee = employees
			doc.add_start_time_log(new_args)
		else:
			doc.add_start_time_log(new_args)

	if doc.status in ["QC Pending", "On Hold"]:
		# and self.status == "On Hold":
		doc.current_time = time_diff_in_seconds(last_row.to_time, last_row.from_time)

	doc.flags.ignore_validation = True
	doc.flags.ignore_permissions = True
	doc.save()


def batch_add_time_logs(self, mop_args_list):
	"""
	Batch update time logs and Manufacturing Operation fields via doc objects.
	mop_args_list: List of (mop_name, args) tuples.
	"""
	# Batch fetch minimal data for status check
	mop_names = [mop[0] for mop in mop_args_list]
	mop_docs = frappe.get_all(
		"Manufacturing Operation",
		filters={"name": ["in", mop_names]},
		fields=["name", "status"],
	)
	mop_dict = {d.name: d for d in mop_docs}
	full_docs = {}

	for mop_name, args in mop_args_list:
		doc_data = mop_dict.get(mop_name)
		if not doc_data:
			continue

		doc = full_docs.get(mop_name) or frappe.get_doc(
			"Manufacturing Operation", mop_name
		)
		full_docs[mop_name] = doc

		new_status = args.get("status")
		if new_status and doc.status != new_status:
			doc.status = new_status

		last_row = doc.time_logs[-1] if doc.time_logs else None
		doc.reset_timer_value(args)

		if args.get("complete_time") and last_row:
			for row in doc.time_logs:
				if not row.to_time:
					row.to_time = get_datetime(args.get("complete_time"))
					calculation_time_log(doc, row, self)
					break

		elif args.get("start_time"):
			employee = args.get("employee")

			new_time_log = frappe._dict(
				{
					"from_time": get_datetime(args.get("start_time")),
					"employee": employee,
				}
			)
			doc.add_start_time_log(new_time_log)

		if (
			doc.status in ["QC Pending", "On Hold"]
			and last_row
			and last_row.to_time
			and last_row.from_time
		):
			doc.current_time = time_diff_in_seconds(
				last_row.to_time, last_row.from_time
			)

	for doc in full_docs.values():
		doc.flags.ignore_validation = True
		doc.flags.ignore_permissions = True
		doc.save()


def validate_qc(self):
	pending_qc = []
	for row in self.employee_ir_operations:
		if not row.get("qc"):
			continue

		if frappe.db.get_value("QC", row.qc, "status") not in [
			"Accepted",
			"Force Approved",
		]:
			pending_qc.append(row.qc)

	if pending_qc:
		frappe.throw(
			_("Following QC are not approved </n> {0}").format(
				", ".join(row for row in pending_qc)
			)
		)


def get_hourly_rate(employee):
	hourly_rate = 0
	now_date = nowdate()
	start_date, end_date = get_first_day(now_date), get_last_day(now_date)
	shift = get_shift(employee, start_date, end_date)
	shift_hours = (
		frappe.utils.flt(frappe.db.get_value("Shift Type", shift, "shift_hours")) or 10
	)

	base = frappe.db.get_value("Employee", employee, "ctc")

	holidays = get_holidays_for_employee(employee, start_date, end_date)
	working_days = date_diff(end_date, start_date) + 1

	working_days -= len(holidays)

	total_working_days = working_days
	target_working_hours = frappe.utils.flt(shift_hours * total_working_days)

	if target_working_hours:
		hourly_rate = frappe.utils.flt(base / target_working_hours)

	return hourly_rate


def get_shift(employee, start_date, end_date):
	Attendance = frappe.qb.DocType("Attendance")

	shift = (
		frappe.qb.from_(Attendance)
		.select(Attendance.shift)
		.distinct()
		.where(
			(Attendance.employee == employee)
			& (Attendance.attendance_date.between(start_date, end_date))
			& (Attendance.shift.notnull())
		)
		.limit(1)
	).run(pluck=True)

	if shift:
		return shift[0]

	return ""


def get_holidays_for_employee(employee, start_date, end_date):
	from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee
	from hrms.utils.holiday_list import get_holiday_dates_between

	HOLIDAYS_BETWEEN_DATES = "holidays_between_dates"

	holiday_list = get_holiday_list_for_employee(employee)
	key = f"{holiday_list}:{start_date}:{end_date}"
	holiday_dates = frappe.cache().hget(HOLIDAYS_BETWEEN_DATES, key)

	if not holiday_dates:
		holiday_dates = get_holiday_dates_between(holiday_list, start_date, end_date)
		frappe.cache().hset(HOLIDAYS_BETWEEN_DATES, key, holiday_dates)

	return holiday_dates


@frappe.whitelist()
def calculation_time_log(doc, row, self):
	# calculation of from and to time
	if row.from_time and row.to_time:
		if get_datetime(row.from_time) > get_datetime(row.to_time):
			frappe.throw(
				_("Row {0}: From time must be less than to time").format(row.idx)
			)

		row_date = getdate(row.from_time)
		doc_date = getdate(self.date_time)

		checkin_doc = frappe.db.sql(
			"""
				SELECT name, log_type ,time
				FROM `tabEmployee Checkin`
				WHERE employee = %s
				AND DATE(time) BETWEEN %s AND %s
			""",
			(row.employee, row_date, doc_date),
			as_dict=1,
		)

		# frappe.throw(f"{checkin_doc}")
		out_time = ""
		in_time = ""
		default_shift = frappe.db.get_value("Employee", row.employee, "default_shift")
		# frappe.throw(f"{default_shift}")
		for emp in checkin_doc:
			if emp.log_type == "OUT" and get_datetime(emp.time) >= row.from_time:
				out_time = get_datetime(emp.time)
			if emp.log_type == "IN" and get_datetime(emp.time) <= row.to_time:
				in_time = get_datetime(emp.time)

		if out_time and in_time:
			out_time_min = (
				time_diff_in_hours(out_time, row.from_time) * 60 if out_time else 0
			)
			in_time_min = (
				time_diff_in_hours(row.to_time, in_time) * 60 if in_time else 0
			)

			# Time in minutes
			row.time_in_mins = out_time_min + in_time_min

			# Time in HH:MM format
			out_hours = time_diff(out_time, row.from_time)
			in_hours = time_diff(row.to_time, in_time)
			total_duration = out_hours + in_hours
			row.time_in_hour = str(total_duration)[:-3]

			# Time in days based on shift
			if default_shift:
				shift_hours = frappe.db.get_value(
					"Shift Type", default_shift, ["start_time", "end_time"]
				)
				total_shift_hours = time_diff(shift_hours[1], shift_hours[0])

				if total_duration >= total_shift_hours:
					row.time_in_days = total_duration / total_shift_hours

		else:
			# Time in minutes
			row.time_in_mins = time_diff_in_hours(row.to_time, row.from_time) * 60

			# Time in HH:MM format
			full_hours = time_diff(row.to_time, row.from_time)
			row.time_in_hour = str(full_hours)[:-2]

			# Time in days based on shift
			if default_shift:
				shift_hours = frappe.db.get_value(
					"Shift Type", default_shift, ["start_time", "end_time"]
				)

				total_shift_hours = time_diff(shift_hours[1], shift_hours[0])

				if full_hours >= total_shift_hours:
					row.time_in_days = full_hours / total_shift_hours
