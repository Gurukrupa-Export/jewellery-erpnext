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

from jewellery_erpnext.jewellery_erpnext.doc_events.process_control import (
	ProcessControlError,
	bold,
	open_operations,
)


class DepartmentProcess(Document):
	def before_naming(self):
		# The name is built from process_name before validate runs: strip here, so "Setting " and
		# "Setting" cannot become two processes of one department.
		self.process_name = (self.process_name or "").strip()

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
				_("Department {0} does not exist.").format(bold(self.department))
			)
		if department.is_group:
			frappe.throw(
				_(
					"Department {0} is a group. A process belongs to a working (non-group) department."
				).format(bold(self.department)),
				ProcessControlError,
			)
		self.company = department.company
		if department.manufacturer and department.manufacturer != self.manufacturer:
			frappe.throw(
				_(
					"Manufacturer {0} does not match Department {1}, whose manufacturer is {2}."
				).format(
					bold(self.manufacturer),
					bold(self.department),
					bold(department.manufacturer),
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
						bold(self.manufacturer),
						bold(manufacturer_company),
						bold(self.company),
					),
					ProcessControlError,
				)

	def validate_disable(self):
		if not self.disabled or self.is_new() or not self.has_value_changed("disabled"):
			return
		active = open_operations(department_process=self.name)
		if active:
			frappe.throw(
				_(
					"Process {0} cannot be disabled while work is in it. Open operations include: {1}. "
					"Move those work orders to another process first."
				).format(bold(self.name), ", ".join(bold(m) for m in active)),
				ProcessControlError,
			)


def on_doctype_update():
	# One process name per department, enforced by the database (the name alone can be renamed).
	frappe.db.add_unique("Department Process", ["department", "process_name"])
