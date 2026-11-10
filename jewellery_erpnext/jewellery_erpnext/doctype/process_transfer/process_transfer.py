# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Process Transfer: move work orders from one process to the next inside one cell department.

Model: the header carries the transition (Current Process to Next Process). Every row must be
a work order whose active operation is currently in the header's Current Process. A row in
another process is refused, never rewritten.

DAY 1: the draft is complete. Each row's active operation and current process are re-read from
the database on every save, and the department, processes, scope and duplicates are validated.
SUBMISSION IS REFUSED: the atomic, locked update of Manufacturing Operation and Manufacturing
Work Order, its history and its cancellation come with the Day 2 transition service. A Process
Transfer can therefore never appear to have moved a job that did not move.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr

from jewellery_erpnext.jewellery_erpnext.doc_events.process_control import (
	ProcessControlError,
	get_active_operation,
	get_process,
	is_process_control_enabled,
)


class ProcessTransferNotAvailableError(ProcessControlError):
	pass


class ProcessTransfer(Document):
	def validate(self):
		self.validate_department()
		self.validate_processes()
		self.set_and_validate_rows()

	def before_submit(self):
		frappe.throw(
			_(
				"Process Transfer cannot be submitted yet: the step that moves work orders to the next "
				"process is not installed. Keep this entry as a draft."
			),
			ProcessTransferNotAvailableError,
		)

	def validate_department(self):
		department = frappe.db.get_value(
			"Department", self.department, ["company", "manufacturer"], as_dict=True
		)
		if not department:
			frappe.throw(
				_("Department {0} does not exist.").format(frappe.bold(self.department))
			)
		if not is_process_control_enabled(self.department):
			frappe.throw(
				_("Department {0} does not use process control.").format(
					frappe.bold(self.department)
				),
				ProcessControlError,
			)
		if department.company != self.company:
			frappe.throw(
				_("Department {0} belongs to Company {1}, not {2}.").format(
					frappe.bold(self.department),
					frappe.bold(department.company),
					frappe.bold(self.company),
				),
				ProcessControlError,
			)
		if department.manufacturer and department.manufacturer != self.manufacturer:
			frappe.throw(
				_("Department {0} belongs to Manufacturer {1}, not {2}.").format(
					frappe.bold(self.department),
					frappe.bold(department.manufacturer),
					frappe.bold(self.manufacturer),
				),
				ProcessControlError,
			)

	def validate_processes(self):
		for fieldname in ("current_process", "next_process"):
			process = get_process(self.get(fieldname))
			label = _(self.meta.get_label(fieldname))
			if not process:
				frappe.throw(
					_("{0} {1} does not exist.").format(
						label, frappe.bold(self.get(fieldname))
					)
				)
			if process.department != self.department:
				frappe.throw(
					_("{0} {1} belongs to Department {2}, not {3}.").format(
						label,
						frappe.bold(process.name),
						frappe.bold(process.department),
						frappe.bold(self.department),
					),
					ProcessControlError,
				)
			if process.disabled:
				frappe.throw(
					_("{0} {1} is disabled.").format(label, frappe.bold(process.name)),
					ProcessControlError,
				)
		if self.current_process == self.next_process:
			frappe.throw(
				_(
					"Current Process and Next Process are both {0}. Choose a different Next Process."
				).format(frappe.bold(self.current_process)),
				ProcessControlError,
			)

	def set_and_validate_rows(self):
		if not self.get("operations"):
			frappe.throw(
				_("Add at least one Manufacturing Work Order."), ProcessControlError
			)
		seen = {}
		for row in self.operations:
			mwo = cstr(row.manufacturing_work_order).strip()
			if mwo in seen:
				frappe.throw(
					_(
						"Row #{0}: Manufacturing Work Order {1} is already in row #{2}."
					).format(row.idx, frappe.bold(mwo), seen[mwo]),
					ProcessControlError,
				)
			seen[mwo] = row.idx
			self.set_row_from_active_operation(row)

	def set_row_from_active_operation(self, row):
		mop = get_active_operation(row.manufacturing_work_order)
		mwo = mop.work_order
		prefix = _("Row #{0}: Manufacturing Work Order {1}").format(
			row.idx, frappe.bold(mwo.name)
		)
		if mwo.docstatus != 1:
			frappe.throw(_("{0} is not submitted.").format(prefix), ProcessControlError)
		if mwo.company != self.company or mwo.manufacturer != self.manufacturer:
			frappe.throw(
				_("{0} belongs to {1} / {2}, not {3} / {4}.").format(
					prefix,
					mwo.company,
					mwo.manufacturer,
					self.company,
					self.manufacturer,
				),
				ProcessControlError,
			)
		if mop.department != self.department:
			frappe.throw(
				_("{0} is in Department {1}, not {2}.").format(
					prefix, frappe.bold(mop.department), frappe.bold(self.department)
				),
				ProcessControlError,
			)
		# Authoritative values from the database, never from the client.
		row.manufacturing_operation = mop.name
		row.current_process = mop.department_process
		row.next_process = self.next_process
		if not mop.department_process:
			frappe.throw(
				_(
					"{0} has no current process yet, so it cannot be transferred between processes."
				).format(prefix),
				ProcessControlError,
			)
		if mop.department_process != self.current_process:
			frappe.throw(
				_("{0} is in process {1}, not the Current Process {2}.").format(
					prefix,
					frappe.bold(mop.department_process),
					frappe.bold(self.current_process),
				),
				ProcessControlError,
			)
