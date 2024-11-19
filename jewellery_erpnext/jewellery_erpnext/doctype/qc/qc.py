# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from erpnext.stock.doctype.quality_inspection_template.quality_inspection_template import (
	get_template_details,
)

# from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import EmployeeIR
from frappe import _, db
from frappe.model.document import Document
from frappe.utils import cint, flt, now, time_diff


class QC(Document):
	def after_insert(self):
		frappe.db.set_value(
			"Manufacturing Operation", self.manufacturing_operation, {"status": "QC Pending"}
		)

	def on_submit(self):
		if self.status not in ["Accepted", "Rejected", "Force Approved"] or any(
			[row.idx for row in self.readings if not row.status]
		):
			frappe.throw(_("QC can only be submitted in Accepted or Rejected state"))
		status = "WIP"
		if self.status in ["Accepted", "Force Approved"]:
			pending_qc = frappe.db.get_value(
				"QC",
				{
					"manufacturing_operation": self.manufacturing_operation,
					"status": ["not in", ["Accepted", "Force Approved", "Rejected"]],
					"docstatus": ["!=", 2],
				},
				"name",
			)
			if pending_qc:
				status = "QC Pending"
			else:
				status = "QC Completed"
		elif self.status == "Rejected":
			existing_doc = frappe.get_doc("QC", self.name)
			qc_doc = frappe.copy_doc(existing_doc)
			qc_doc.previous_qc = self.name
			qc_doc.save()
			frappe.db.set_value("QC", qc_doc.name, "status", "Pending")
			self.duplicate_qc = qc_doc.name
			self.save()

		frappe.db.set_value("Manufacturing Operation", self.manufacturing_operation, {"status": status})

	def validate(self):
		if self.status == "Force Approved" or any(
			[row.name for row in self.readings if row.status == "Force Approved"]
		):
			frappe.throw(_("Not allowed to select 'Force Approved'"))
		if not self.readings:
			self.get_specification_details()
		else:
			self.inspect_and_set_status()

		if self.has_value_changed("status") and self.status in ["Accepted", "Rejected"]:
			self.finish_time = now()
			self.time_taken = time_diff(self.finish_time, self.start_time)

	@frappe.whitelist()
	def force_approve(self):
		self.db_set("status", "Force Approved")
		for row in self.readings:
			if row.status == "Rejected":
				row.db_set("status", "Force Approved")
		self.on_submit()

		if self.duplicate_qc:
			frappe.get_doc("QC", self.duplicate_qc).force_approve()

	@frappe.whitelist()
	def get_specification_details(self):
		if not self.quality_inspection_template:
			return

		self.set("readings", [])
		parameters = get_template_details(self.quality_inspection_template)
		for d in parameters:
			child = self.append("readings", {})
			child.update(d)
			child.manual_inspection = 1

	def inspect_and_set_status(self):
		for reading in self.readings:
			if not reading.manual_inspection:  # dont auto set status if manual
				if reading.formula_based_criteria:
					self.set_status_based_on_acceptance_formula(reading)
				else:
					# if not formula based check acceptance values set
					self.set_status_based_on_acceptance_values(reading)

		if not self.manual_inspection:
			for reading in self.readings:
				if reading.status == "Rejected":
					self.status = "Rejected"
					frappe.msgprint(
						_("Status set to rejected as there are one or more rejected readings."), alert=True
					)
					break
				elif reading.status == "Accepted":
					self.status = "Accepted"

	def set_status_based_on_acceptance_values(self, reading):
		if not cint(reading.numeric):
			result = reading.get("reading_value") == reading.get("value")
		else:
			# numeric readings
			result = self.min_max_criteria_passed(reading)

		reading.status = "Accepted" if result else "Rejected"

	def min_max_criteria_passed(self, reading):
		"""Determine whether all readings fall in the acceptable range."""
		for i in range(1, 11):
			reading_value = reading.get("reading_" + str(i))
			frappe.msgprint(reading_value)
			if reading_value is not None and reading_value.strip():
				result = flt(reading.get("min_value")) <= flt(reading_value) <= flt(reading.get("max_value"))
				if not result:
					return False

		return True

	def set_status_based_on_acceptance_formula(self, reading):
		if not reading.acceptance_formula:
			frappe.throw(
				_("Row #{0}: Acceptance Criteria Formula is required.").format(reading.idx),
				title=_("Missing Formula"),
			)

		condition = reading.acceptance_formula
		data = self.get_formula_evaluation_data(reading)

		try:
			result = frappe.safe_eval(condition, None, data)
			reading.status = "Accepted" if result else "Rejected"
		except NameError as e:
			field = frappe.bold(e.args[0].split()[1])
			frappe.throw(
				_("Row #{0}: {1} is not a valid reading field. Please refer to the field description.").format(
					reading.idx, field
				),
				title=_("Invalid Formula"),
			)
		except Exception:
			frappe.throw(
				_("Row #{0}: Acceptance Criteria Formula is incorrect.").format(reading.idx),
				title=_("Invalid Formula"),
			)

	def get_formula_evaluation_data(self, reading):
		data = {}
		if not cint(reading.numeric):
			data = {"reading_value": reading.get("reading_value")}
		else:
			# numeric readings
			for i in range(1, 11):
				field = "reading_" + str(i)
				data[field] = flt(reading.get(field))
			data["mean"] = self.calculate_mean(reading)
			data["gross_wt"] = self.gross_wt
			data["received_gross_wt"] = self.received_gross_wt
			data["allowed_loss_percentage"] = self.allowed_loss_percentage
			data["allowed_diamond_loss"] = self.allowed_diamond_loss
			data["allowed_gemstone_loss"] = self.allowed_gemstone_loss

		return data

	def calculate_mean(self, reading):
		"""Calculate mean of all non-empty readings."""
		from statistics import mean

		readings_list = []

		for i in range(1, 11):
			reading_value = reading.get("reading_" + str(i))
			if reading_value is not None and reading_value.strip():
				readings_list.append(flt(reading_value))

		actual_mean = mean(readings_list) if readings_list else 0
		return actual_mean


@frappe.whitelist()
def receive_gross_wt_from_qc(doc_name, mwo, mnf_opt, eir, g_wt, r_gwt):
	try:
		db.begin()
		# Set Recieving Weight same operation in QC List
		get_qc_list = frappe.get_list(
			"QC",
			fields=["name"],
			filters={"manufacturing_work_order": mwo, "manufacturing_operation": mnf_opt},
		)
		for qc_list in get_qc_list:
			frappe.db.set_value("QC", qc_list["name"], "received_gross_wt", r_gwt)

		emp_ir = frappe.get_doc("Employee IR", eir)
		# if g_wt == r_gwt:

		if g_wt != r_gwt:
			for (
				entry
			) in (
				emp_ir.employee_ir_operations
			):  # Set Recieving Weight in Employee IR Operation Table from QC
				entry.received_gross_wt = r_gwt
			eir_doc = frappe.get_doc("Employee IR", eir)
			result = eir_doc.book_metal_loss(mwo=mwo, opt=mnf_opt, gwt=g_wt, r_gwt=r_gwt)
			# Set Employee Loss Details Table in Employee IR Doctype
			if result and isinstance(result, tuple):
				data = result[0]
				emp_ir.employee_loss_details = []
				for r_data in data:
					row = emp_ir.append("employee_loss_details", {})
					row.item_code = r_data["item_code"]
					row.net_weight = r_data["qty"]
					row.stock_uom = r_data["stock_uom"]
					row.manufacturing_work_order = r_data["manufacturing_work_order"]
					row.proportionally_loss = r_data["proportionally_loss"]
					row.received_gross_weight = r_data["received_gross_weight"]
					row.main_slip_consumption = r_data["main_slip_consumption"]
				emp_ir.save()

				for qc_list in get_qc_list:
					# if qc_list["name"] == doc_name:
					# 	continue
					qc_doc = frappe.get_doc("QC", qc_list["name"])
					qc_doc.employee_loss_details = []
					if r_gwt != g_wt:
						for r_data in data:
							row = qc_doc.append("employee_loss_details", {})
							row.item_code = r_data["item_code"]
							row.net_weight = r_data["qty"]
							row.stock_uom = r_data["stock_uom"]
							row.manufacturing_work_order = r_data["manufacturing_work_order"]
							row.proportionally_loss = r_data["proportionally_loss"]
							row.received_gross_weight = r_data["received_gross_weight"]
							row.main_slip_consumption = r_data["main_slip_consumption"]
						qc_doc.save()
				if data:
					return data
		else:
			for qc_list in get_qc_list:
				# if qc_list["name"] == doc_name:
				# 	continue
				qc_doc = frappe.get_doc("QC", qc_list["name"])
				qc_doc.employee_loss_details = []
				qc_doc.save()

			for (
				entry
			) in (
				emp_ir.employee_ir_operations
			):  # Set Recieving Weight in Employee IR Operation Table from QC
				entry.received_gross_wt = r_gwt
			emp_ir.employee_loss_details = []
			emp_ir.save()

		db.commit()
	except Exception as e:
		db.rollback()
		frappe.throw(f"Error: {e}")
