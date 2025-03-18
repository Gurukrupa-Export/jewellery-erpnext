import json

import frappe
from frappe import _, qb
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.query_builder import DocType
from frappe.query_builder.functions import Sum

# timer code
from frappe.utils import (
	cint,
	date_diff,
	flt,
	get_datetime,
	get_first_day,
	get_last_day,
	now,
	nowdate,
	time_diff_in_seconds,
	today,
)

from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir import (
	update_stock_entry_dimensions, batch_update_stock_entry_dimensions
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.emp_ir_receive import (
	get_stock_data,
	get_warehouses,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_ir_utils import (
	create_chain_stock_entry,
	get_po_rates,
	valid_reparing_or_next_operation,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.html_utils import (
	get_summary_data,
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
from jewellery_erpnext.utils import (
	get_item_from_attribute,
	get_item_from_attribute_full,
	update_existing,
)


class EmployeeIR(Document):
	def before_insert(self):
		if not self.main_slip or self.type == "Issue":
			return

		existing_mwo = set(frappe.db.get_all(
			"Main Slip Operation", {"parent": self.main_slip}, pluck="manufacturing_work_order"
		))

		for row in self.employee_ir_operations:
			if row.manufacturing_work_order not in existing_mwo:
				frappe.throw(
					title=_("Invalid Manufacturing Work Order"),
					msg=_("Manufacturing Work Order {0} not available in Main Slip").format(
						row.manufacturing_work_order
					)
				)

	@frappe.whitelist()
	def get_operations(self):
		records = frappe.get_list(
			"Manufacturing Operation",
			{"department": self.department, "employee": ["is", "not set"], "operation": ["is", "not set"]},
			["name", "gross_wt"],
		)
		self.employee_ir_operations = []
		if records:
			for row in records:
				self.append("employee_ir_operations", {"manufacturing_operation": row.name})

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
			"Warehouse", {"disabled": 0, "department": self.department, "warehouse_type": "Manufacturing"}
		)
		if not warehouse:
			frappe.throw(_("MFG Warehouse not available for department"))
		if frappe.db.get_value(
			"Stock Reconciliation",
			{"set_warehouse": warehouse, "workflow_state": ["in", ["In Progress", "Send for Approval"]]},
		):
			frappe.throw(_("Stock Reconciliation is under process"))
		validate_duplication_and_gr_wt(self)

	def validate(self):
		# self.validate_gross_wt()
		self.validate_main_slip()
		self.update_main_slip()
		self.validate_process_loss()
		validate_manually_book_loss_details(self)
		valid_reparing_or_next_operation(self)
		validate_loss_qty(self)

	# def after_insert(self):
	# 	self.validate_qc("Warn")

	def on_cancel(self):
		if self.type == "Issue":
			self.on_submit_issue(cancel=True)
		else:
			self.on_submit_receive(cancel=True)

	# def validate_gross_wt(self):
	# 	precision = cint(frappe.db.get_single_value("System Settings", "float_precision"))
	# 	for row in self.employee_ir_operations:
	# 		row.gross_wt = frappe.db.get_value(
	# 			"Manufacturing Operation", row.manufacturing_operation, "gross_wt"
	# 		)
	# 		if not self.main_slip:
	# 			if flt(row.gross_wt, precision) < flt(row.received_gross_wt, precision):
	# 				frappe.throw(
	# 					_("Row #{0}: Received gross wt {1} cannot be greater than gross wt {2}").format(
	# 						row.idx, row.received_gross_wt, row.gross_wt
	# 					)
	# 				)

	# for issue
	def on_submit_issue(self, cancel=False):
		employee = None if cancel else self.employee
		operation = None if cancel else self.operation
		status = "Not Started" if cancel else "WIP"
		values = {"operation": operation, "status": status}
		if self.subcontracting == "Yes":
			values["for_subcontracting"] = 1
			values["subcontractor"] = None if cancel else self.subcontractor
		else:
			values["employee"] = employee

		mop_data = {}
		for row in self.employee_ir_operations:
			values["rpt_wt_issue"] = row.rpt_wt_issue
			frappe.db.set_value(
				"Manufacturing Operation", row.manufacturing_operation, "operation", operation
			)
			if not cancel:
				update_stock_entry_dimensions(self, row, row.manufacturing_operation, True)
				mop_data.update({row.manufacturing_work_order: row.manufacturing_operation})

			values["start_time"] = frappe.utils.now()
			add_time_log(row.manufacturing_operation, values)
			frappe.db.set_value("Manufacturing Operation", row.manufacturing_operation, values)

		create_single_se_entry(self, mop_data)

		# for row in self.employee_ir_operations:


	def on_submit_issue_new(self, cancel=False):
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

		mop_data = {}
		mops_to_update = {}
		time_log_args = []
		stock_entry_data = []
		start_time = frappe.utils.now() if not cancel else None

		for row in self.employee_ir_operations:
			values.update({
				"operation": operation,
				"rpt_wt_issue": row.rpt_wt_issue,
				"start_time": start_time
			})
			mops_to_update[row.manufacturing_operation] = values
			if not cancel:
				stock_entry_data.append((row.manufacturing_work_order, row.manufacturing_operation))
				mop_data[row.manufacturing_work_order] = row.manufacturing_operation
				time_log_args.append((row.manufacturing_operation, values))

		if mops_to_update:
			frappe.db.bulk_update(
				"Manufacturing Operation",
				mops_to_update,
				chunk_size=100,
				update_modified=True
			)

		# Batch update stock entries
		if stock_entry_data and not cancel:
			batch_update_stock_entry_dimensions(self, stock_entry_data, employee, True)

		# Batch add time logs
		if time_log_args and not cancel:
			batch_add_time_logs(time_log_args)

		create_single_se_entry(self, mop_data)

	# for receive
	def on_submit_receive(self, cancel=False):
		row_to_append = []
		main_slip_rows = []
		loss_rows = []
		repack_raws = []
		precision = cint(frappe.db.get_single_value("System Settings", "float_precision"))

		mwo_loss_dict = {}
		for row in self.manually_book_loss_details + self.employee_loss_details:
			if row.variant_of in ["M", "F"]:
				mwo_loss_dict.setdefault(row.manufacturing_work_order, 0)
				mwo_loss_dict[row.manufacturing_work_order] += row.proportionally_loss

		is_mould_operation = frappe.db.get_value(
			"Department Operation", self.operation, "is_mould_manufacturer"
		)

		filters = {
			"parentfield": "batch_details",
			"parent": self.main_slip,
			"qty": [">", 0],
		}

		main_slip_data = frappe.db.get_all(
			"Main Slip SE Details",
			filters,
			[
				"item_code",
				"batch_no",
				"qty",
				"(consume_qty + employee_qty) as consume_qty",
				"inventory_type",
				"customer",
			],
		)

		pure_gold_item = frappe.db.get_value("Manufacturing Setting", self.company, "pure_gold_item")

		msl_dict = frappe._dict({"regular_batch": {}, "pure_batch": [], "customer_batch": {}})

		warehouse_data = frappe._dict()

		metal_item_data = frappe._dict()

		loss_details = frappe._dict()

		for msl in main_slip_data:
			if pure_gold_item == msl.item_code:
				msl_dict.pure_batch.append(msl)
			elif msl.inventory_type in ["Customer Goods", "Customer Stock"]:
				msl_dict.customer_batch.setdefault(msl.item_code, [])
				msl_dict.customer_batch[msl.item_code].append(msl)
			else:
				msl_dict.regular_batch.setdefault(msl.item_code, [])
				msl_dict.regular_batch[msl.item_code].append(msl)

		curr_time = frappe.utils.now()

		for row in self.employee_ir_operations:
			if is_mould_operation:
				create_mould(self, row)
			net_loss_wt = mwo_loss_dict.get(row.manufacturing_work_order) or 0

			net_wt = frappe.db.get_value("Manufacturing Operation", row.manufacturing_operation, "net_wt")

			difference_wt = flt(row.received_gross_wt, precision) - flt(row.gross_wt, precision)

			res = frappe._dict(
				{
					"received_gross_wt": row.received_gross_wt,
					"loss_wt": difference_wt,
					"received_net_wt": flt(net_wt - net_loss_wt, precision),
					"status": "WIP",
				}
			)

			if row.received_gross_wt == 0 and row.gross_wt != 0:
				frappe.throw(_("Row {0}: Received Gross Wt Missing").format(row.idx))

			if not cancel:
				res["status"] = "Finished"
				res["employee"] = self.employee
				new_operation = create_operation_for_next_op(
					row.manufacturing_operation, employee_ir=self.name, received_gr_wt=row.received_gross_wt
				)
				res["complete_time"] = curr_time
				frappe.db.set_value(
					"Manufacturing Work Order",
					row.manufacturing_work_order,
					"manufacturing_operation",
					new_operation,
				)
				doc = frappe.get_doc("Manufacturing Operation", row.manufacturing_operation)
				add_time_log(doc, res)

			if row.get("is_finding_mwo"):
				if not cancel:
					create_chain_stock_entry(self, row)
			else:
				if not cancel:
					se_rows, msl_rows, product_loss, mfg_rows = create_stock_entry(
						self,
						row,
						warehouse_data,
						metal_item_data,
						loss_details,
						flt(difference_wt, precision),
						msl_dict,
					)

					row_to_append += se_rows
					main_slip_rows += msl_rows
					loss_rows += product_loss
					repack_raws += mfg_rows
					# res = get_material_wt(self, row.manufacturing_operation)
				else:
					# new_operation = frappe.db.get_value(
					# 	"Manufacturing Operation",
					# 	{"employee_ir": self.name, "manufacturing_work_order": row.manufacturing_work_order},
					# )
					se_list = frappe.db.get_list("Stock Entry", {"employee_ir": self.name})
					for se in se_list:
						se_doc = frappe.get_doc("Stock Entry", se.name)
						if se_doc.docstatus == 1:
							se_doc.cancel()

						frappe.db.set_value(
							"Stock Entry Detail", {"parent": se.name}, "manufacturing_operation", None
						)

					frappe.db.set_value(
						"Manufacturing Work Order",
						row.manufacturing_work_order,
						"manufacturing_operation",
						row.manufacturing_operation,
					)
					if new_operation:
						frappe.db.set_value(
							"Department IR Operation",
							{"docstatus": 2, "manufacturing_operation": new_operation},
							"manufacturing_operation",
							None,
						)
						frappe.db.set_value(
							"Stock Entry Detail",
							{"docstatus": 2, "manufacturing_operation": new_operation},
							"manufacturing_operation",
							None,
						)
						frappe.delete_doc("Manufacturing Operation", new_operation, ignore_permissions=1)
					frappe.db.set_value(
						"Manufacturing Operation", row.manufacturing_operation, "status", "Not Started"
					)

			if row.rpt_wt_receive:
				issue_wt = frappe.db.get_value(
					"Manufacturing Operation", row.manufacturing_operation, "rpt_wt_issue"
				)
				res["rpt_wt_receive"] = row.rpt_wt_receive
				res["rpt_wt_loss"] = flt(row.rpt_wt_receive - issue_wt, 3)
			frappe.set_value("Manufacturing Operation", row.manufacturing_operation, res)

		# workstation_data = frappe._dict()
		# Process Loss
		if loss_rows:
			pl_se_doc = frappe.new_doc("Stock Entry")
			pl_se_doc.company = self.company
			pl_se_doc.stock_entry_type = "Process Loss"
			pl_se_doc.purpose = "Repack"
			pl_se_doc.department = self.department
			pl_se_doc.to_department = self.department
			pl_se_doc.employee = self.employee
			pl_se_doc.subcontractor = self.subcontractor
			pl_se_doc.auto_created = 1
			pl_se_doc.employee_ir = self.name
			for row in loss_rows:
				pl_se_doc.append("items", row)
			pl_se_doc.flags.ignore_permissions = True
			pl_se_doc.save()
			pl_se_doc.submit()

		if repack_raws:
			re_se_doc = frappe.new_doc("Stock Entry")
			re_se_doc.company = self.company
			re_se_doc.stock_entry_type = "Manufacture"
			re_se_doc.purpose = "Manufacture"
			re_se_doc.department = self.department
			re_se_doc.to_department = self.department
			re_se_doc.employee = self.employee
			re_se_doc.subcontractor = self.subcontractor
			re_se_doc.auto_created = 1
			re_se_doc.employee_ir = self.name
			finished_item = {}
			for row in repack_raws:
				if row.get('is_finished_item'):
					if not finished_item.get('finish'):
						finished_item.update({'finish':'Finish Item'})
					else:
						row.update({'is_finished_item': 0})

				if not re_se_doc.main_slip:
					re_se_doc.main_slip = row.get("main_slip") or row.get("to_main_slip")
				re_se_doc.append("items", row)

			re_se_doc.flags.ignore_permissions = True
			re_se_doc.save()
			re_se_doc.submit()

		if main_slip_rows:
			mse_doc = frappe.new_doc("Stock Entry")
			mse_doc.company = self.company
			mse_doc.stock_entry_type = "Material Transfer (Main Slip)"
			mse_doc.purpose = "Material Transfer"
			mse_doc.department = self.department
			mse_doc.to_department = self.department
			mse_doc.main_slip = self.main_slip
			mse_doc.employee = self.employee
			mse_doc.subcontractor = self.subcontractor
			mse_doc.auto_created = True
			mse_doc.employee_ir = self.name
			for row in main_slip_rows:
				mse_doc.append("items", row)
			mse_doc.flags.ignore_permissions = True
			mse_doc.save()
			mse_doc.submit()

		operation_data = {}

		if row_to_append:
			expense_account = frappe.db.get_value("Company", self.company, "default_operating_cost_account")

			workstations = frappe.db.get_all(
				"Workstation",
				{"employee": self.employee},
				["name", "hour_rate_electricity", "hour_rate_rent", "hour_rate_consumable"],
				limit=1,
			)
			workstation = workstations[0] if workstations else None

			if not workstation and not self.subcontractor:
				frappe.throw(_("Please define Workstation for {0}").format(self.employee))

			if not self.subcontractor:
				hour_rate_labour = get_hourly_rate(self.employee)

			se_doc = frappe.new_doc("Stock Entry")
			se_doc.company = self.company
			se_doc.stock_entry_type = "Material Transfer to Department"
			se_doc.outgoing_stock_entry = None
			se_doc.set_posting_time = 1
			se_doc.inventory_type = None
			se_doc.from_warehouse = None
			se_doc.to_warehouse = None
			se_doc.auto_created = 1
			if self.main_slip:
				se_doc.main_slip = self.main_slip
				se_doc.to_main_slip = None
			else:
				se_doc.main_slip = None
				se_doc.to_main_slip = None

			mop_data = frappe._dict()
			pmo_data = frappe._dict()

			for row in row_to_append:
				se_doc.append("items", row)
				if isinstance(row, dict):
					row = frappe._dict(row)
				if row.employee and not operation_data.get(row.manufacturing_operation):
					if not mop_data.get(row.manufacturing_operation):
						mop_data[row.manufacturing_operation] = frappe.db.get_value(
							"Manufacturing Operation",
							row.manufacturing_operation,
							["total_minutes", "manufacturing_order"],
							as_dict=1,
						)

					if not self.subcontractor:
						total_expense = (
							workstation.hour_rate_electricity
							+ workstation.hour_rate_rent
							+ workstation.hour_rate_consumable
							+ hour_rate_labour
						)
						operation_data[row.manufacturing_operation] = {
							"workstation": workstation.name,
							"total_expense": total_expense,
							"operation_time": mop_data[row.manufacturing_operation].time_in_mins or 0,
							"mop": row.manufacturing_operation,
							"pmo": mop_data[row.manufacturing_operation].manufacturing_order,
						}

			if operation_data:
				for row in operation_data:
					additional_cost = {
						"expense_account": expense_account,
						"amount": operation_data[row]["total_expense"],
						"description": "Workstation Cost",
						"manufacturing_operation": operation_data[row]["mop"],
						"workstation": operation_data[row]["workstation"],
						"total_minutes": operation_data[row]["operation_time"],
					}
					# se_doc.append("additional_costs", additional_cost)

					pmo_data.setdefault(operation_data[row]["pmo"], [])
					pmo_data[operation_data[row]["pmo"]].append(additional_cost)

			se_doc.employee_ir = self.name
			se_doc.flags.ignore_permissions = True
			se_doc.save()
			se_doc.submit()

			for pmo, details in pmo_data.items():
				pmo_doc = frappe.get_doc("Parent Manufacturing Order", pmo)
				for row in details:
					pmo_doc.append("pmo_operation_cost", row)
				pmo_doc.flags.ignore_validations = True
				pmo_doc.flags.ignore_permissions = True
				pmo_doc.save()

	def validate_qc(self, action="Warn"):
		if not self.is_qc_reqd or self.type == "Receive":
			return

		qc_list = []
		for row in self.employee_ir_operations:
			operation = frappe.db.get_value(
				"Manufacturing Operation", row.manufacturing_operation, ["status"], as_dict=1
			)
			if operation.get("status") == "Not Started":
				if action == "Warn":
					create_qc_record(row, self.operation, self.name)
				qc_list.append(row.manufacturing_operation)
		if qc_list:
			msg = _("Please complete QC for the following: {0}").format(", ".join(qc_list))
			if action == "Warn":
				frappe.msgprint(msg)
			elif action == "Stop":
				frappe.msgprint(msg)

	def update_main_slip(self):
		if not self.main_slip or not self.is_main_slip_required:
			return

		existing_operations = frappe.db.get_all(
			"Main Slip Operation", {"parent": self.main_slip}, pluck="manufacturing_operation"
		)

		rows_to_append = [
			row.manufacturing_operation
			for row in self.employee_ir_operations
			if row.manufacturing_operation not in existing_operations
		]

		if rows_to_append:
			main_slip = frappe.get_doc("Main Slip", self.main_slip)
			for mop in rows_to_append:
				main_slip.append("main_slip_operation", {"manufacturing_operation": mop})
			main_slip.flags.ignore_validations = True
			main_slip.save()

	def validate_main_slip(self):
		if self.docstatus != 0:
			return
		dep_opr = frappe.get_value("Department Operation", self.operation, "check_colour_in_main_slip")
		if self.main_slip and dep_opr == 1:
			ms = frappe.db.get_value(
				"Main Slip",
				self.main_slip,
				[
					"metal_type",
					"metal_touch",
					"metal_purity",
					"metal_colour",
					"check_color",
					"for_subcontracting",
					"multicolour",
					"allowed_colours",
				],
				as_dict=1,
			)
			multi_colors_ms = (
				"".join(sorted([color.upper() for color in ms.allowed_colours]))
				if ms.get("allowed_colours")
				else None
			)
			single_colors_ms = (
				"".join([color.upper() for color in ms.metal_colour[0]]) if ms.get("metal_colour") else None
			)
			for row in self.employee_ir_operations:
				mwo = frappe.db.get_value(
					"Manufacturing Work Order",
					row.manufacturing_work_order,
					[
						"metal_type",
						"metal_touch",
						"metal_purity",
						"metal_colour",
						"multicolour",
						"allowed_colours",
					],
					as_dict=1,
				)
				if mwo.allowed_colours:
					# allowed_colors_mwo = "".join(sorted(map(str.upper, mwo.allowed_colours)))
					allowed_colors_mwo = "".join(sorted([color.upper() for color in mwo.allowed_colours]))
				else:
					# allowed_colors_mwo = "".join(map(str.upper, mwo.metal_colour[0]))
					allowed_colors_mwo = "".join([color.upper() for color in mwo.metal_colour[0]])
				# frappe.throw(f"{allowed_colors_mwo}")

				color_matched = False  # Flag to check if at least one color matches
				if ms.allowed_colours:
					# multi_colors_ms = "".join(sorted(map(str.upper, ms.allowed_colours)))
					# multi_colors_ms = "".join(sorted([color.upper() for color in ms.allowed_colours]))
					if allowed_colors_mwo == multi_colors_ms:
						color_matched = True

				if ms.metal_colour:
					# single_colors_ms = "".join(map(str.upper, ms.metal_colour[0]))
					for mwo_char in allowed_colors_mwo:
						for ms_char in single_colors_ms:
							if ms_char == mwo_char:
								color_matched = True

				if color_matched == False:
					frappe.throw(f"Main slip color mismatch, allowed color: <b>{allowed_colors_mwo}</b>")

	@frappe.whitelist()
	def create_subcontracting_order(self):
		service_item = frappe.db.get_value("Department Operation", self.operation, "service_item")
		if not service_item:
			frappe.throw(_("Please set service item for {0}").format(self.operation))
		skip_operations = []
		po = frappe.new_doc("Purchase Order")
		po.supplier = self.subcontractor
		company = frappe.db.get_value("Company", {"supplier_code": self.subcontractor}, "name")
		po.company = company or self.company
		po.employee_ir = self.name
		po.purchase_type = "FG Purchase"

		allow_zero_qty = frappe.db.get_value("Department Operation", self.operation, "allow_zero_qty_wo")
		for row in self.employee_ir_operations:
			if not row.gross_wt and not allow_zero_qty:
				skip_operations.append(row.manufacturing_operation)
				continue
			rate = get_po_rates(self.subcontractor, self.operation, po.purchase_type, row)
			pmo = frappe.db.get_value(
				"Manufacturing Work Order", row.manufacturing_work_order, "manufacturing_order"
			)
			po.append(
				"items",
				{
					"item_code": service_item,
					"qty": 1,
					"custom_gross_wt": row.gross_wt,
					"rate": flt(rate[0].get("rate_per_gm") * row.gross_wt, 3) if rate else 0,
					"schedule_date": today(),
					"manufacturing_operation": row.manufacturing_operation,
					"custom_pmo": pmo,
				},
			)
		if skip_operations:
			frappe.throw(
				f"PO creation skipped for following Manufacturing Operations due to zero gross weight: {', '.join(skip_operations)}"
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

		supplier_group = frappe.db.get_value("Supplier", self.subcontractor, "supplier_group")
		if frappe.db.get_value("Supplier Group", supplier_group, "custom_create_so_for_subcontracting"):
			create_so_for_subcontracting(po)

	@frappe.whitelist()
	def validate_process_loss(self):
		if self.docstatus != 0:
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
				rows_to_append += self.book_metal_loss(mwo, opt, gwt, r_gwt, allowed_loss_percentage)

		self.employee_loss_details = []
		for row in rows_to_append:
			proportionally_loss = flt(row["proportionally_loss"], 3)
			if proportionally_loss > 0:
				variant_of = frappe.db.get_value("Item", row["item_code"], "variant_of")
				self.append(
					"employee_loss_details",
					{
						"item_code": row["item_code"],
						"net_weight": row["qty"],
						"stock_uom": row["stock_uom"],
						"variant_of": variant_of,
						"batch_no": row["batch_no"],
						"manufacturing_work_order": row["manufacturing_work_order"],
						"manufacturing_operation": row["manufacturing_operation"],
						"proportionally_loss": proportionally_loss,
						"received_gross_weight": row["received_gross_weight"],
						"main_slip_consumption": row.get("main_slip_consumption"),
						"inventory_type": row["inventory_type"],
						"customer": row.get("customer"),
					},
				)

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
				"qty",
				"uom",
				"pcs",
				"customer",
				"inventory_type",
				"sub_setting_type",
			]
			for row in frappe.db.get_all("MOP Balance Table", {"parent": opt}, fields):
				mop_balance_table.append(row)
			# Declaration & fetch required value
			metal_item = []  # for check metal or not list
			unique = set()  # for Unique Item_Code
			sum_qty = {}  # for sum of qty matched item

			# getting Metal property from MNF Work Order
			mwo_metal_property = frappe.db.get_value(
				"Manufacturing Work Order",
				mwo,
				["metal_type", "metal_touch", "metal_purity", "master_bom", "is_finding_mwo"],
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
			if mwo_metal_property.get("is_finding_mwo"):
				bom_items = frappe.db.get_all(
					"BOM Item", {"parent": mwo_metal_property.master_bom}, pluck="item_code"
				)
				bom_items += frappe.db.get_all(
					"BOM Explosion Item", {"parent": mwo_metal_property.master_bom}, pluck="item_code"
				)
				flat_metal_item = list(set(bom_items))
			else:
				flat_metal_item = [
					item for sublist in metal_item for super_sub in sublist for item in super_sub
				]

			total_qty = 0
			# To prepare Final Data with all condition's
			for child in mop_balance_table:
				if child["item_code"] not in flat_metal_item:
					continue
				key = (child["item_code"], child["batch_no"], child["qty"])
				if key not in unique:
					unique.add(key)
					total_qty += child["qty"]
					if child["item_code"] in sum_qty:
						sum_qty[child["item_code"], child["batch_no"]]["qty"] += child["qty"]
					else:
						sum_qty[child["item_code"], child["batch_no"]] = {
							"item_code": child["item_code"],
							"qty": child["qty"],
							"stock_uom": child["uom"],
							"batch_no": child["batch_no"],
							"manufacturing_work_order": mwo,
							"manufacturing_operation": opt,
							"pcs": child["pcs"],
							"customer": child["customer"],
							"inventory_type": child["inventory_type"],
							"sub_setting_type": child["sub_setting_type"],
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
							row.proportionally_loss if row.stock_uom != "Carat" else (row.proportionally_loss * 0.2)
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
					if mwo_metal_property.get("is_finding_mwo"):
						stock_loss = flt((entry["qty"] * loss) / total_qty, 3)
					else:
						if loss <= entry["qty"]:
							stock_loss = loss
							loss = 0
						else:
							stock_loss = entry["qty"]
							loss -= entry["qty"]
					if stock_loss > 0:
						entry["received_gross_weight"] = entry["qty"] - stock_loss
						entry["proportionally_loss"] = stock_loss
						entry["main_slip_consumption"] = 0
					else:
						ms_consum_book = round((ms_consum * entry["qty"]) / total_qty, 4)
						entry["proportionally_loss"] = 0
						entry["received_gross_weight"] = 0
						entry["main_slip_consumption"] = ms_consum_book
			# -------------------------------------------------------------------------
		return data

	@frappe.whitelist()
	def get_summary_data(self):
		return get_summary_data(self)


def create_operation_for_next_op(docname, employee_ir=None, received_gr_wt=0):

	new_mop_doc = frappe.copy_doc(
		frappe.get_doc("Manufacturing Operation", docname), ignore_no_copy=False
	)
	new_mop_doc.department_issue_id = None
	new_mop_doc.status = "Not Started"
	new_mop_doc.department_ir_status = None
	new_mop_doc.department_receive_id = None
	new_mop_doc.prev_gross_wt = received_gr_wt
	new_mop_doc.employee_ir = employee_ir
	new_mop_doc.employee = None
	new_mop_doc.previous_mop = docname
	new_mop_doc.operation = None
	new_mop_doc.department_source_table = []
	new_mop_doc.department_target_table = []
	new_mop_doc.employee_source_table = []
	new_mop_doc.employee_target_table = []
	new_mop_doc.previous_se_data_updated = 0
	new_mop_doc.save()
	# def set_missing_value(source, target):
	# 	target.previous_operation = source.operation
	# 	target.prev_gross_wt = (
	# 		received_gr_wt or source.received_gross_wt or source.gross_wt or source.prev_gross_wt
	# 	)
	# 	target.previous_mop = source.name

	# copy doc
	# target_doc = frappe.copy_doc(docname)
	# field_no_map = [
	# "status", "employee", "start_time", "subcontractor", "for_subcontracting",
	# "finish_time", "time_taken", "department_issue_id", "department_receive_id",
	# "department_ir_status", "operation", "previous_operation", "started_time",
	# "current_time", "on_hold", "total_minutes", "time_logs"
	# ]

	# for field in field_no_map:
	# 	setattr(target_doc, field, None)
	# set_missing_value(source, target_doc)
	# target_doc = get_mapped_doc(
	# 	"Manufacturing Operation",
	# 	docname,
	# 	{
	# 		"Manufacturing Operation": {
	# 			"doctype": "Manufacturing Operation",
	# 			"field_no_map": [
	# 				"status",
	# 				"employee",
	# 				"start_time",
	# 				"subcontractor",
	# 				"for_subcontracting",
	# 				"finish_time",
	# 				"time_taken",
	# 				"department_issue_id",
	# 				"department_receive_id",
	# 				"department_ir_status",
	# 				"operation",
	# 				"previous_operation",
	# 				"start_time",
	# 				"finish_time",
	# 				"time_taken",
	# 				"started_time",
	# 				"current_time",
	# 				"on_hold",
	# 				"total_minutes",
	# 				"time_logs",
	# 			],
	# 		}
	# 	},
	# 	target_doc,
	# 	set_missing_value,
	# )
	# target_doc.department_source_table = []
	# target_doc.department_target_table = []
	# target_doc.employee_source_table = []
	# target_doc.employee_target_table = []
	# target_doc.employee_ir = employee_ir
	# target_doc.time_taken = None
	# target_doc.employee = None
	# # target_doc.save()
	# # target_doc.db_set("employee", None)

	# # timer code
	# target_doc.start_time = ""
	# target_doc.finish_time = ""
	# target_doc.time_taken = ""
	# target_doc.started_time = ""
	# target_doc.current_time = ""
	# target_doc.time_logs = []
	# target_doc.total_time_in_mins = ""
	# target_doc.save()
	return new_mop_doc.name


@frappe.whitelist()
def get_manufacturing_operations(source_name, target_doc=None):
	if not target_doc:
		target_doc = frappe.new_doc("Employee IR")
	elif isinstance(target_doc, str):
		target_doc = frappe.get_doc(json.loads(target_doc))
	if not target_doc.get("employee_ir_operations", {"manufacturing_operation": source_name}):
		operation = frappe.db.get_value(
			"Manufacturing Operation", source_name, ["gross_wt", "manufacturing_work_order"], as_dict=1
		)
		target_doc.append(
			"employee_ir_operations",
			{
				"manufacturing_operation": source_name,
				"gross_wt": operation["gross_wt"],
				"manufacturing_work_order": operation["manufacturing_work_order"],
			},
		)
	return target_doc


def create_stock_entry(
	doc, row, warehouse_data, metal_item_data, loss_details, difference_wt=0, msl_dict=None
):
	metal_item = None

	if not msl_dict:
		msl_dict = frappe._dict()

	se_rows = []
	msl_rows = []
	process_loss_rows = []
	repack_raws = []

	# Get Dep and Emp Warehouse
	department_wh, employee_wh = get_warehouses(doc, warehouse_data)

	# Get All Previous Stock Data (Manual Entry and Automated Entries both)
	stock_entries = get_stock_data(row.manufacturing_operation, employee_wh, doc.department)

	existing_items = frappe.get_all(
		"Stock Entry Detail",
		{"parent": ["in", stock_entries]},
		pluck="item_code",
	)

	loss_items = []
	if difference_wt != 0:
		loss_items = [
			{
				"item_code": loss_item.item_code,
				"loss_qty": loss_item.proportionally_loss,
				"batch_no": loss_item.batch_no,
				"inventory_type": loss_item.inventory_type,
				"customer": loss_item.customer,
				"pcs": loss_item.pcs,
				"manufacturing_work_order": loss_item.manufacturing_work_order,
				"manufacturing_operation": loss_item.manufacturing_operation,
				"variant_of": loss_item.variant_of if loss_item.get("variant_of") else None,
				"sub_setting_type": loss_item.sub_setting_type if loss_item.get("sub_setting_type") else None,
				"loss_type": loss_item.loss_type,
			}
			for loss_item in doc.manually_book_loss_details + doc.employee_loss_details
			if loss_item.manufacturing_work_order == row.manufacturing_work_order
		]

		mwo = frappe.db.get_value(
			"Manufacturing Work Order",
			row.manufacturing_work_order,
			["metal_type", "metal_touch", "metal_purity", "metal_colour", "manufacturing_order"],
			as_dict=1,
		)
		customer_details = (
			frappe.db.get_value(
				"Parent Manufacturing Order",
				mwo.manufacturing_order,
				["customer", "is_customer_gold"],
				as_dict=1,
			)
			or frappe._dict()
		)

		key = (mwo.metal_type, mwo.metal_touch, mwo.metal_purity, mwo.metal_colour)
		if not metal_item_data.get(key):
			metal_item_data[key] = get_item_from_attribute(
				mwo.metal_type, mwo.metal_touch, mwo.metal_purity, mwo.metal_colour
			)

		metal_item = metal_item_data.get(key)

		if difference_wt < 0 and (metal_item not in existing_items):

			if not loss_items:
				frappe.throw(
					_("Please Book Loss in <b>Manually Book Loss Details</b> for Row:{0}").format(row.idx)
				)
			else:
				manual_loss_qty = sum([row.get("loss_qty") for row in loss_items])
				if abs(difference_wt) != manual_loss_qty:
					frappe.throw(
						_("Total Loss found: {0} Please book Extra loss against MOP to continue").format(
							manual_loss_qty
						)
					)

		elif difference_wt < 0 and not doc.main_slip and (metal_item in existing_items):
			# Loss done through Manual Table + Loss Table
			if loss_items:
				process_loss_rows += process_loss_entry(
					doc, row.manufacturing_operation, loss_details, loss_items, employee_wh, department_wh
				)
		elif doc.main_slip:
			pure_ms_qty = 0
			# Loss done through Manual Table + Loss Table
			if loss_items:
				process_loss_rows += process_loss_entry(
					doc, row.manufacturing_operation, loss_details, loss_items, employee_wh, department_wh
				)

			if not warehouse_data.get(doc.main_slip):
				warehouse_data[doc.main_slip] = frappe.db.get_value(
					"Main Slip", doc.main_slip, "raw_material_warehouse"
				)

			msl_raw_warehouse = warehouse_data.get(doc.main_slip)
			if not msl_raw_warehouse:
				frappe.throw(_("Please set Raw material warehouse for employee"))

			ms_transfer_data = {}
			remaining_wt = abs(difference_wt)
			if difference_wt > 0:

				filters = {
					"parentfield": "batch_details",
					"parent": doc.main_slip,
					"item_code": metal_item,
					"qty": [">", 0],
				}

				if customer_details.get("is_customer_gold"):
					filters["inventory_type"] = ["in", ["Customer Goods", "Customer Stock"]]
					filters["customer"] = customer_details.customer

				ms_data = (
					msl_dict["customer_batch"].get(metal_item, []) + msl_dict["regular_batch"].get(metal_item, [])
					if customer_details.get("is_customer_gold")
					else msl_dict["regular_batch"].get(metal_item, [])
				)

				for b_id in ms_data:
					if b_id.qty <= b_id.consume_qty or (
						b_id.get("customer")
						and customer_details.get("customer")
						and b_id.customer != customer_details.customer
					):
						continue
					if not b_id.batch_no:
						frappe.throw(_("Batch details not available in Main slip"))
					if remaining_wt > 0:

						if (b_id.consume_qty + remaining_wt) <= b_id.qty:
							se_qty = remaining_wt
							remaining_wt = 0
						else:
							se_qty = b_id.qty - b_id.consume_qty
							remaining_wt -= se_qty

						b_id.consume_qty += se_qty

						# update this with Se Rows

						se_rows.append(
							{
								"item_code": metal_item,
								"s_warehouse": msl_raw_warehouse if difference_wt > 0 else department_wh,
								"t_warehouse": msl_raw_warehouse if difference_wt < 0 else department_wh,
								"to_employee": None,
								"employee": doc.employee,
								"to_subcontractor": None,
								"use_serial_batch_fields": True,
								"serial_and_batch_bundle": None,
								"subcontractor": doc.subcontractor,
								"to_main_slip": None,
								"main_slip": doc.main_slip,
								"qty": abs(se_qty),
								"manufacturing_operation": row.manufacturing_operation,
								"custom_manufacturing_work_order": row.manufacturing_work_order,
								"department": doc.department,
								"to_department": doc.department,
								"manufacturer": doc.manufacturer,
								"material_request": None,
								"material_request_item": None,
								"batch_no": b_id.batch_no,
								"inventory_type": b_id.inventory_type,
								"customer": customer_details.customer
								if customer_details.get("is_customer_gold")
								else None,
								"pcs": 1,
								"custom_employee_consumption": 1,
							}
						)
						ms_transfer_data.update({(b_id.batch_no, b_id.inventory_type): se_qty})

				if loss_items:
					for loss_row in loss_items:
						if loss_row["variant_of"] in ["M", "F"]:
							se_rows.append(
								{
									"item_code": loss_row["item_code"],
									"s_warehouse": employee_wh,
									"t_warehouse": msl_raw_warehouse,
									"to_employee": None,
									"employee": doc.employee,
									"to_subcontractor": None,
									"use_serial_batch_fields": True,
									"serial_and_batch_bundle": None,
									"subcontractor": doc.subcontractor,
									"to_main_slip": None,
									"main_slip": doc.main_slip,
									"qty": abs(loss_row["loss_qty"]),
									"manufacturing_operation": row.manufacturing_operation,
									"custom_manufacturing_work_order": row.manufacturing_work_order,
									"department": doc.department,
									"to_department": doc.department,
									"manufacturer": doc.manufacturer,
									"material_request": None,
									"material_request_item": None,
									"batch_no": loss_row["batch_no"],
									"inventory_type": loss_row["inventory_type"],
									"customer": customer_details.customer
									if customer_details.get("is_customer_gold")
									else None,
									"pcs": 1,
								}
							)

			elif difference_wt < 0:
				remaining_wt = 0
				batch_data = [
					{
						"qty": loss_item.proportionally_loss,
						"batch_no": loss_item.batch_no,
						"inventory_type": loss_item.inventory_type,
						"item_code": loss_item.item_code,
					}
					for loss_item in (doc.employee_loss_details)
					if (
						loss_item.manufacturing_work_order == row.manufacturing_work_order
						and loss_item.item_code == metal_item
					)
				]

				batch_data += [
					{
						"qty": loss_item.proportionally_loss,
						"batch_no": loss_item.batch_no,
						"inventory_type": loss_item.inventory_type,
						"item_code": loss_item.item_code,
					}
					for loss_item in (doc.manually_book_loss_details)
					if (
						loss_item.manufacturing_work_order == row.manufacturing_work_order
						and loss_item.variant_of in ["M", "F"]
						and doc.main_slip
					)
				]

				for batch in batch_data:
					se_rows.append(
						{
							"item_code": batch["item_code"],
							"s_warehouse": employee_wh,
							"t_warehouse": msl_raw_warehouse,
							"to_employee": None,
							"employee": doc.employee,
							"to_subcontractor": None,
							"use_serial_batch_fields": True,
							"serial_and_batch_bundle": None,
							"subcontractor": doc.subcontractor,
							"to_main_slip": None,
							"main_slip": doc.main_slip,
							"qty": batch["qty"],
							"manufacturing_operation": row.manufacturing_operation,
							"custom_manufacturing_work_order": row.manufacturing_work_order,
							"department": doc.department,
							"to_department": doc.department,
							"manufacturer": doc.manufacturer,
							"material_request": None,
							"material_request_item": None,
							"batch_no": batch["batch_no"],
							"inventory_type": batch["inventory_type"],
							"customer": customer_details.customer if customer_details.get("is_customer_gold") else None,
							"custom_employee_consumption": 1,
						}
					)

			if remaining_wt > 0 and doc.subcontractor:
				# pure_gold_item = frappe.db.get_value("Manufacturing Setting", doc.company, "pure_gold_item")

				pure_ms_data = frappe._dict()

				inventory_type = (
					"Customer Goods" if customer_details.get("is_customer_gold") else "Regular Stock"
				)
				if not metal_item_data.get(mwo.metal_purity):
					metal_item_data[mwo.metal_purity] = frappe.db.get_value(
						"Attribute Value", mwo.metal_purity, "purity_percentage"
					)
				purity = metal_item_data.get(mwo.metal_purity)
				total_conversion_qty = 0
				for data in msl_dict["pure_batch"]:

					pure_ms_data.setdefault((data.inventory_type, data.customer), 0)

					pure_ms_data[(data.inventory_type, data.customer)] += flt(data.qty - data.consume_qty, 3)

					existing_qty = flt(data.qty - data.consume_qty, 3)
					msl_qty = 0
					if purity > 0:
						msl_qty = (100 * existing_qty) / purity

					to_use_wt = 0

					if flt(remaining_wt, 3) > msl_qty:
						to_use_wt = existing_qty
						data.consume_qty += to_use_wt
						remaining_wt -= msl_qty

					else:
						to_use_wt = flt((remaining_wt / 100) * purity, 3)
						data.consume_qty += to_use_wt
						remaining_wt = 0

					if msl_qty and to_use_wt > 0:
						total_conversion_qty += (100 * to_use_wt) / purity
						repack_raws.append(
							{
								"item_code": data.item_code,
								"s_warehouse": msl_raw_warehouse,
								"t_warehouse": None,
								"to_employee": None,
								"employee": doc.employee,
								"to_subcontractor": None,
								"use_serial_batch_fields": True,
								"serial_and_batch_bundle": None,
								"subcontractor": doc.subcontractor,
								"to_main_slip": None,
								"main_slip": doc.main_slip,
								"qty": flt(to_use_wt, 3),
								"manufacturing_operation": row.manufacturing_operation,
								"custom_manufacturing_work_order": row.manufacturing_work_order,
								"department": doc.department,
								"to_department": doc.department,
								"manufacturer": doc.manufacturer,
								"material_request": None,
								"material_request_item": None,
								"batch_no": data.batch_no,
								"inventory_type": inventory_type,
								"customer": customer_details.customer
								if customer_details.get("is_customer_gold")
								else None,
								"pcs": 1,
							}
						)

				if abs(flt(remaining_wt)) > total_conversion_qty:
					frappe.throw(
						_("Required Qty is {remaining_wt} and available Qty is 0").format(
							remaining_wt=flt(remaining_wt, 3), title=(_("Insufficient Quantity in Main Slip"))
						)
					)

				from frappe.model.naming import make_autoname

				if not metal_item_data.get(metal_item):
					metal_item_data[metal_item] = frappe.db.get_value("Item", metal_item, "batch_number_series")

				batch_number_series = metal_item_data.get(metal_item)

				batch_doc = frappe.new_doc("Batch")
				batch_doc.item = metal_item

				if batch_number_series:
					batch_doc.batch_id = make_autoname(batch_number_series, doc=batch_doc)

				batch_doc.flags.ignore_permissions = True
				batch_doc.save()
				repack_raws.append(
					{
						"item_code": metal_item,
						"is_finished_item": 1,
						"s_warehouse": None,
						"t_warehouse": msl_raw_warehouse,
						"to_employee": None,
						"employee": doc.employee,
						"to_subcontractor": None,
						"use_serial_batch_fields": True,
						"serial_and_batch_bundle": None,
						"subcontractor": doc.subcontractor,
						"to_main_slip": None,
						"main_slip": doc.main_slip,
						"qty": flt(total_conversion_qty, 3),
						"manufacturing_operation": row.manufacturing_operation,
						"custom_manufacturing_work_order": row.manufacturing_work_order,
						"department": doc.department,
						"to_department": doc.department,
						"manufacturer": doc.manufacturer,
						"material_request": None,
						"material_request_item": None,
						"batch_no": batch_doc.name,
						"inventory_type": inventory_type,
						"customer": customer_details.customer if customer_details.get("is_customer_gold") else None,
						"pcs": 1,
					}
				)
				se_rows.append(
					{
						"item_code": metal_item,
						"s_warehouse": msl_raw_warehouse if difference_wt > 0 else department_wh,
						"t_warehouse": msl_raw_warehouse if difference_wt < 0 else department_wh,
						"to_employee": None,
						"employee": doc.employee,
						"to_subcontractor": None,
						"use_serial_batch_fields": True,
						"serial_and_batch_bundle": None,
						"subcontractor": doc.subcontractor,
						"to_main_slip": None,
						"main_slip": doc.main_slip,
						"qty": flt(total_conversion_qty, 3),
						"manufacturing_operation": row.manufacturing_operation,
						"custom_manufacturing_work_order": row.manufacturing_work_order,
						"department": doc.department,
						"to_department": doc.department,
						"manufacturer": doc.manufacturer,
						"material_request": None,
						"material_request_item": None,
						"batch_no": batch_doc.name,
						"inventory_type": inventory_type,
						"customer": customer_details.customer if customer_details.get("is_customer_gold") else None,
						"pcs": 1,
						"custom_employee_consumption": 1,
					}
				)

				# pure_key = (inventory_type, customer_details.get("customer"))
				# pure_ms_qty = pure_ms_data.get(pure_key) or 0
				# if not pure_ms_qty or pure_ms_qty <= 0:
				# 	frappe.throw(
				# 		_("Required Qty is {remaining_wt} and available Qty is 0").format(
				# 			remaining_wt=flt(remaining_wt, 3), title=(_("Insufficient Quantity in Main Slip"))
				# 		)
				# 	)
				# from frappe.model.naming import make_autoname

				# if not metal_item_data.get(mwo.metal_purity):
				# 	metal_item_data[mwo.metal_purity] = frappe.db.get_value(
				# 		"Attribute Value", mwo.metal_purity, "purity_percentage"
				# 	)

				# purity = metal_item_data.get(mwo.metal_purity)
				# mwo_qty = pure_ms_qty
				# if purity > 0:
				# 	mwo_qty = (100 * pure_ms_qty) / purity

				# if not metal_item_data.get(metal_item):
				# 	metal_item_data[metal_item] = frappe.db.get_value("Item", metal_item, "batch_number_series")

				# batch_number_series = metal_item_data.get(metal_item)

				# batch_doc = frappe.new_doc("Batch")
				# batch_doc.item = metal_item

				# if batch_number_series:
				# 	batch_doc.batch_id = make_autoname(batch_number_series, doc=batch_doc)

				# batch_doc.flags.ignore_permissions = True
				# batch_doc.save()

				# if flt(mwo_qty, 3) >= flt(remaining_wt, 3):
				# 	# se_rows.append(
				# 	# 	{
				# 	# 		"item_code": metal_item,
				# 	# 		"s_warehouse": msl_raw_warehouse if difference_wt > 0 else department_wh,
				# 	# 		"t_warehouse": msl_raw_warehouse if difference_wt < 0 else department_wh,
				# 	# 		"to_employee": None,
				# 	# 		"employee": doc.employee,
				# 	# 		"to_subcontractor": None,
				# 	# 		"use_serial_batch_fields": True,
				# 	# 		"serial_and_batch_bundle": None,
				# 	# 		"subcontractor": doc.subcontractor,
				# 	# 		"to_main_slip": None,
				# 	# 		"main_slip": doc.main_slip,
				# 	# 		"qty": abs(flt(remaining_wt, 3)),
				# 	# 		"manufacturing_operation": row.manufacturing_operation,
				# 	# 		"custom_manufacturing_work_order": row.manufacturing_work_order,
				# 	# 		"department": doc.department,
				# 	# 		"to_department": doc.department,
				# 	# 		"manufacturer": doc.manufacturer,
				# 	# 		"material_request": None,
				# 	# 		"material_request_item": None,
				# 	# 		"batch_no": batch_doc.name,
				# 	# 		"inventory_type": inventory_type,
				# 	# 		"customer": customer_details.customer if customer_details.get("is_customer_gold") else None,
				# 	# 		"pcs": 1,
				# 	# 		"custom_employee_consumption": 1,
				# 	# 	}
				# 	# )
				# 	ms_transfer_data.update({(batch_doc.name, inventory_type): abs(flt(remaining_wt, 3))})
				# else:
				# 	frappe.throw(
				# 		_("Required Qty is {remaining_wt} and available Qty is {mwo_qty}").format(
				# 			remaining_wt=flt(remaining_wt, 3),
				# 			mwo_qty=flt(mwo_qty, 3),
				# 			title=(_("Insufficient Quantity in Main Slip")),
				# 		)
				# 	)

			if flt(remaining_wt, 3) != 0 and (not pure_ms_qty):
				frappe.throw(_("{0} Quantity not available in Main Slip").format(remaining_wt))

	metal_loss = {}

	for metal_loss_item in loss_items:
		if row.manufacturing_work_order == metal_loss_item.get("manufacturing_work_order"):
			metal_loss[
				(metal_loss_item.get("item_code"), metal_loss_item.get("batch_no"))
			] = metal_loss.get(
				(metal_loss_item.get("item_code"), metal_loss_item.get("batch_no")), 0
			) + metal_loss_item.get(
				"loss_qty"
			)

	rejected_qty = {}
	for stock_entry in stock_entries:
		to_remove = []
		existing_doc = frappe.get_doc("Stock Entry", stock_entry)

		for child in existing_doc.items:
			child.name = None
			if child.manufacturing_operation != row.manufacturing_operation:
				to_remove.append(child)
			else:
				if not rejected_qty.get((child.item_code,child.batch_no)):
					StockEntryDetail = DocType("Stock Entry Detail").as_("sed")
					StockEntry = DocType("Stock Entry").as_("se")
					query = (
						qb.from_(StockEntryDetail)
						.join(StockEntry)
						.on(StockEntry.name == StockEntryDetail.parent)
						.select(Sum(StockEntryDetail.qty).as_("qty"))
						.where(
							(StockEntry.docstatus == 1)
							& (StockEntry.auto_created == 0)
							& (StockEntryDetail.s_warehouse == child.t_warehouse)
							& (StockEntryDetail.manufacturing_operation == child.manufacturing_operation)
							& (StockEntryDetail.batch_no == child.batch_no)
						)
					)
					trash_qty = query.run(as_dict=True)
					if trash_qty:
						trash_qty = trash_qty[0]["qty"] or 0

					rejected_qty[(child.item_code,child.batch_no)] = trash_qty

				child.s_warehouse = employee_wh
				child.t_warehouse = department_wh
				if doc.subcontracting == "Yes":
					child.to_subcontractor = None
					child.subcontractor = doc.subcontractor
				else:
					child.to_employee = None
					child.employee = doc.employee
				child.to_main_slip = None
				child.main_slip = doc.main_slip

				if child.item_code[0] in ["M", "F"]:
					if (
						metal_loss.get((child.item_code, child.batch_no))
						and metal_loss.get((child.item_code, child.batch_no)) > 0
					):
						if child.qty > metal_loss.get((child.item_code, child.batch_no)):
							child.qty = child.qty - metal_loss.get((child.item_code, child.batch_no))
							metal_loss[(child.item_code, child.batch_no)] = 0
						elif child.qty <= metal_loss.get((child.item_code, child.batch_no)):
							metal_loss[(child.item_code, child.batch_no)] = (
								metal_loss.get((child.item_code, child.batch_no)) - child.qty
							)
							to_remove.append(child)
				else:
					for loss_row in loss_items:
						if loss_row.get("manufacturing_work_order") == row.manufacturing_work_order:
							if (
								loss_row.get("item_code") == child.item_code and loss_row.get("batch_no") == child.batch_no
							):
								if loss_row.get("loss_qty") < child.qty:
									child.qty = child.qty - loss_row.get("loss_qty")
								elif loss_row.get("loss_qty") == child.qty:
									to_remove.append(child)
									continue

				child.use_serial_batch_fields = True
				child.serial_and_batch_bundle = None
				child.manufacturing_operation = row.manufacturing_operation
				child.custom_manufacturing_work_order = row.manufacturing_work_order
				child.department = doc.department
				child.to_department = doc.department
				child.manufacturer = doc.manufacturer
				child.material_request = None
				child.material_request_item = None
				if (metal_item == child.item_code) and difference_wt < 0:
					update_existing(
						"Manufacturing Operation",
						row.manufacturing_operation,
						{"gross_wt": f"gross_wt + {difference_wt}", "net_wt": f"net_wt + {difference_wt}"},
					)

				if rejected_qty.get((child.item_code,child.batch_no)) and rejected_qty.get((child.item_code,child.batch_no)) > 0:
					if flt(rejected_qty[(child.item_code,child.batch_no)],3) < child.qty:
						child.qty -= rejected_qty[(child.item_code,child.batch_no)]
						rejected_qty[(child.item_code,child.batch_no)] = 0
					else:
						if child not in to_remove:
							to_remove.append(child)
						rejected_qty[(child.item_code,child.batch_no)] -= child.qty

				if child.qty < 0:
					frappe.throw(_("Qty cannot be negative"))

				if child not in to_remove:
					se_rows.append(child)

	if difference_wt > 0:
		if not doc.main_slip:
			frappe.throw(_("Cannot add weight without Main Slip."))

		# for ms in ms_transfer_data:
		# 	se_rows.append(
		# 		{
		# 			"item_code": metal_item,
		# 			"s_warehouse": employee_wh,
		# 			"t_warehouse": department_wh,
		# 			"to_employee": None,
		# 			"employee": doc.employee,
		# 			"use_serial_batch_fields": True,
		# 			"serial_and_batch_bundle": None,
		# 			"to_subcontractor": None,
		# 			"subcontractor": doc.subcontractor,
		# 			"to_main_slip": None,
		# 			"main_slip": doc.main_slip,
		# 			"qty": ms_transfer_data[ms],
		# 			"manufacturing_operation": row.manufacturing_operation,
		# 			"custom_manufacturing_work_order": row.manufacturing_work_order,
		# 			"department": doc.department,
		# 			"to_department": doc.department,
		# 			"manufacturer": doc.manufacturer,
		# 			"material_request": None,
		# 			"material_request_item": None,
		# 			"batch_no": ms[0],
		# 			"inventory_type": ms[1],
		# 			"customer": customer_details.customer if customer_details.get("is_customer_gold") else None,
		# 		}
		# 	)

	return se_rows, msl_rows, process_loss_rows, repack_raws


def update_stock_details(docname):
	doc = frappe.get_doc("Main Slip", docname)
	doc.append("stock_details", {"item_code": None})
	doc.save()


def convert_pure_metal(mwo, ms, qty, s_warehouse, t_warehouse, reverse=False):
	from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import convert_metal_purity

	# source is ms(main slip) and passed qty is difference qty b/w issue and received gross wt i.e. mwo.qty
	mwo = frappe.db.get_value(
		"Manufacturing Work Order",
		mwo,
		["metal_type", "metal_touch", "metal_purity", "metal_colour"],
		as_dict=1,
	)
	ms = frappe.db.get_value(
		"Main Slip", ms, ["metal_type", "metal_touch", "metal_purity", "metal_colour"], as_dict=1
	)
	mwo.qty = qty
	if reverse:
		ms.qty = qty * flt(mwo.get("metal_purity")) / 100
		convert_metal_purity(mwo, ms, s_warehouse, t_warehouse)
	else:
		ms.qty = qty * flt(mwo.get("metal_purity")) / 100
		convert_metal_purity(ms, mwo, s_warehouse, t_warehouse)


def create_qc_record(row, operation, employee_ir):
	item = frappe.db.get_value("Manufacturing Operation", row.manufacturing_operation, "item_code")
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
					((QC.docstatus == 1) & (QC.status.isin(["Accepted", "Force Approved"]))) | (QC.docstatus == 0)
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


def batch_add_time_logs(mop_args_list):
	"""
	Batch update time logs and Manufacturing Operation fields via doc objects.
	mop_args_list: List of (mop_name, args) tuples.
	"""
	# Batch fetch minimal data for status check
	mop_names = [mop[0] for mop in mop_args_list]
	mop_docs = frappe.get_all(
		"Manufacturing Operation",
		filters={"name": ["in", mop_names]},
		fields=["name", "status"]
	)
	mop_dict = {d.name: d for d in mop_docs}
	full_docs = {}

	for mop_name, args in mop_args_list:
		doc_data = mop_dict.get(mop_name)
		if not doc_data:
			continue

		doc = full_docs.get(mop_name) or frappe.get_doc("Manufacturing Operation", mop_name)
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
					break

		elif args.get("start_time"):
			employee = args.get("employee")

			new_time_log = frappe._dict({
				"from_time": get_datetime(args.get("start_time")),
				"employee": employee
			})
			doc.add_start_time_log(new_time_log)

		if doc.status in ["QC Pending", "On Hold"] and last_row and last_row.to_time and last_row.from_time:
			doc.current_time = time_diff_in_seconds(last_row.to_time, last_row.from_time)

	for doc in full_docs.values():
		doc.flags.ignore_validation = True
		doc.flags.ignore_permissions = True
		doc.save()


def process_loss_entry(
	doc, manufacturing_operation, loss_details, manual_loss_items, employee_wh, department_wh
):
	process_loss_row = []

	for loss_item in manual_loss_items:
		if abs(loss_item.get("loss_qty")):
			if doc.main_slip and loss_item.get("variant_of") in ["M", "F"]:
				continue
			else:
				process_loss_row += process_loss_item(
					doc, manufacturing_operation, loss_details, loss_item, employee_wh, department_wh
				)

	return process_loss_row


def process_loss_item(
	doc, manufacturing_operation, loss_details, loss_item, employee_wh, loss_warehouse
):
	process_loss_row = []
	from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import get_item_loss_item

	key = (
		doc.company,
		loss_item.get("item_code"),
		loss_item.get("variant_of"),
		loss_item.get("loss_type"),
	)
	if not loss_details.get(key):
		loss_details[key] = get_item_loss_item(
			doc.company,
			loss_item.get("item_code"),
			loss_item.get("variant_of"),
			loss_item.get("loss_type"),
		)
	dust_item = loss_details.get(key)

	if not loss_details.get(loss_item["variant_of"]):
		loss_details[loss_item["variant_of"]] = frappe.db.get_value(
			"Variant Loss Warehouse",
			{"parent": doc.manufacturer, "variant": loss_item.get("variant_of")},
			["loss_warehouse", "consider_department_warehouse", "warehouse_type"],
			as_dict=1,
		)

	variant_loss_details = loss_details.get(loss_item["variant_of"])

	if variant_loss_details and variant_loss_details.get("loss_warehouse"):
		loss_warehouse = variant_loss_details.get("loss_warehouse")

	elif variant_loss_details.get("consider_department_warehouse") and variant_loss_details.get(
		"warehouse_type"
	):
		if not loss_details.get(variant_loss_details["warehouse_type"]):
			loss_details[variant_loss_details["warehouse_type"]] = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"department": doc.department,
					"warehouse_type": variant_loss_details.get("warehouse_type"),
				},
			)
		loss_warehouse = loss_details.get(variant_loss_details["warehouse_type"])

		if not loss_warehouse:
			frappe.throw(_("Default loss warehouse is not set in Manufacturer loss table"))

	common_fields = {
		"s_warehouse": employee_wh,
		"t_warehouse": None,
		"to_employee": None,
		"employee": doc.employee,
		"to_subcontractor": None,
		"use_serial_batch_fields": True,
		"serial_and_batch_bundle": None,
		"subcontractor": doc.subcontractor,
		"to_main_slip": None,
		"qty": abs(loss_item.get("loss_qty")),
		"department": doc.department,
		"to_department": doc.department,
		"manufacturer": doc.manufacturer,
		"material_request": None,
		"material_request_item": None,
		"inventory_type": loss_item.get("inventory_type"),
		"customer": loss_item.get("customer"),
		"custom_sub_setting_type": loss_item.get("sub_setting_type"),
		"manufacturing_operation": manufacturing_operation,
		"pcs": loss_item.get("pcs") or 0,
	}

	process_loss_row.append(
		{
			**common_fields,
			"item_code": loss_item.get("item_code"),
			"manufacturing_operation": None,
			"batch_no": loss_item.get("batch_no"),
		}
	)

	if frappe.db.get_value("Item", dust_item, "valuation_rate") == 0:
		parent_valuation = frappe.db.get_value("Item", loss_item.get("item_code"), "valuation_rate")
		frappe.db.set_value("Item", dust_item, "valuation_rate", parent_valuation)

	process_loss_row.append(
		{
			**common_fields,
			"item_code": dust_item,
			"s_warehouse": None,
			"t_warehouse": loss_warehouse,
		}
	)

	return process_loss_row


# Function to create Single Entry For Employee IR Issue
def create_single_se_entry(doc, mop_data):
	rows_to_append = []
	department_wh = frappe.get_value(
		"Warehouse", {"disabled": 0, "department": doc.department, "warehouse_type": "Manufacturing"}
	)
	if doc.subcontracting == "Yes":
		employee_wh = frappe.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"company": doc.company,
				"subcontractor": doc.subcontractor,
				"warehouse_type": "Manufacturing",
			},
		)
	else:
		employee_wh = frappe.get_value(
			"Warehouse", {"disabled": 0, "employee": doc.employee, "warehouse_type": "Manufacturing"}
		)
	if not department_wh:
		frappe.throw(_("Please set warhouse for department {0}").format(doc.department))
	if not employee_wh:
		subcontractor = "subcontractor" if doc.subcontracting == "Yes" else "employee"
		subcontractor_doc = doc.subcontractor if doc.subcontracting == "Yes" else doc.employee
		frappe.throw(_("Please set warhouse for {0} {1}").format(subcontractor, subcontractor_doc))

	mop_balance_details = frappe.db.get_all(
		"MOP Balance Table", {"parent": ["in", mop_data.values()]}, ["*"]
	)

	mop_balance_data = frappe._dict()

	for row in mop_balance_details:
		mop_balance_data.setdefault(row.parent, [])
		mop_balance_data[row.parent].append(row)

	for row in mop_data:
		rows_to_append += get_rows_to_append(
			doc, row, mop_data[row], mop_balance_data.get(mop_data[row]), department_wh, employee_wh
		)

	if rows_to_append:
		se_doc = frappe.new_doc("Stock Entry")
		se_doc.company = doc.company
		se_doc.inventory_type = None
		se_doc.department = doc.department
		se_doc.to_department = doc.department
		se_doc.to_employee = doc.employee if doc.type == "Issue" else None
		se_doc.to_subcontractor = doc.subcontractor if doc.type == "Issue" else None
		se_doc.auto_created = True
		se_doc.employee_ir = doc.name

		if doc.main_slip:
			se_doc.to_main_slip = doc.main_slip

		stock_entry_type = (
			"Material Transfer to Subcontractor"
			if doc.subcontracting == "Yes"
			else "Material Transfer to Employee"
		)

		for idx, row in enumerate(rows_to_append):
			se_doc.stock_entry_type = stock_entry_type
			if doc.subcontracting == "Yes":
				row.to_subcontractor = doc.subcontractor
				row.subcontractor = None
			else:
				row.to_employee = doc.employee
				row.employee = None
			row.department_operation = doc.operation
			row.main_slip = None
			row.to_main_slip = doc.main_slip

			se_doc.append("items", row)

		se_doc.flags.ignore_permissions = True
		se_doc.save()
		se_doc.submit()


def get_rows_to_append(doc, mwo, mop, mop_data, department_wh, employee_wh):
	rows_to_append = []
	import copy

	if not mop_data:
		mop_data = []

	for row in mop_data:
		if row.qty > 0:
			duplicate_row = copy.deepcopy(row)
			duplicate_row["name"] = None
			duplicate_row["idx"] = None
			duplicate_row["t_warehouse"] = employee_wh
			duplicate_row["s_warehouse"] = department_wh
			duplicate_row["manufacturing_operation"] = mop
			duplicate_row["use_serial_batch_fields"] = True
			duplicate_row["serial_and_batch_bundle"] = None
			duplicate_row["custom_manufacturing_work_order"] = mwo
			duplicate_row["department"] = doc.department
			duplicate_row["to_department"] = doc.department
			duplicate_row["manufacturer"] = doc.manufacturer

			rows_to_append.append(duplicate_row)

	return rows_to_append


def validate_qc(self):
	pending_qc = []
	for row in self.employee_ir_operations:
		if not row.get("qc"):
			continue

		if frappe.db.get_value("QC", row.qc, "status") not in ["Accepted", "Force Approved"]:
			pending_qc.append(row.qc)

	if pending_qc:
		frappe.throw(
			_("Following QC are not approved </n> {0}").format(", ".join(row for row in pending_qc))
		)


def get_hourly_rate(employee):
	hourly_rate = 0
	now_date = nowdate()
	start_date, end_date = get_first_day(now_date), get_last_day(now_date)
	shift = get_shift(employee, start_date, end_date)
	shift_hours = frappe.utils.flt(frappe.db.get_value("Shift Type", shift, "shift_hours")) or 10

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
