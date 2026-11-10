# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""A process inside a manufacturing-cell department, e.g. Model Making inside a cell.

Scope: one department, so one company and one manufacturer. A Department Operation belongs to at
most one process (``Department Operation.department_process``). Process names are configuration; nothing in
code refers to a particular department or process.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from jewellery_erpnext.jewellery_erpnext.doc_events.current_operation_guard import (
	OPEN_STATUSES,
	is_reverted,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.process_control import (
	ProcessControlError,
)


class DepartmentProcess(Document):
	def validate(self):
		self.process_name = (self.process_name or "").strip()
		self.validate_scope()
		self.validate_disable()

	def validate_scope(self):
		department = frappe.db.get_value(
			"Department",
			self.department,
			["company", "manufacturer", "is_group"],
			as_dict=True,
		)
		if not department:
			frappe.throw(
				_("Department {0} does not exist.").format(frappe.bold(self.department))
			)
		if department.is_group:
			frappe.throw(
				_(
					"Department {0} is a group. A process belongs to a working (non-group) department."
				).format(frappe.bold(self.department)),
				ProcessControlError,
			)
		self.company = department.company
		if department.manufacturer and department.manufacturer != self.manufacturer:
			frappe.throw(
				_(
					"Manufacturer {0} does not match Department {1}, whose manufacturer is {2}."
				).format(
					frappe.bold(self.manufacturer),
					frappe.bold(self.department),
					frappe.bold(department.manufacturer),
				),
				ProcessControlError,
			)
		if frappe.get_meta("Manufacturer").has_field("company"):
			manufacturer_company = frappe.db.get_value(
				"Manufacturer", self.manufacturer, "company"
			)
			if (
				manufacturer_company
				and self.company
				and manufacturer_company != self.company
			):
				frappe.throw(
					_("Manufacturer {0} belongs to Company {1}, not {2}.").format(
						frappe.bold(self.manufacturer),
						frappe.bold(manufacturer_company),
						frappe.bold(self.company),
					),
					ProcessControlError,
				)

	def validate_disable(self):
		if not self.disabled or self.is_new():
			return
		if not self.has_value_changed("disabled"):
			return
		active = [
			r.name
			for r in frappe.get_all(
				"Manufacturing Operation",
				filters={
					"department_process": self.name,
					"status": ["in", OPEN_STATUSES],
				},
				fields=["name", "status", "department_ir_status"],
				order_by="name asc",
			)
			if not is_reverted(r.status, r.department_ir_status)
		][:5]
		if active:
			frappe.throw(
				_(
					"Process {0} cannot be disabled while work is in it. Open operations include: {1}. "
					"Move those work orders to another process first."
				).format(frappe.bold(self.name), ", ".join(active)),
				ProcessControlError,
			)
