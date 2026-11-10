# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Process Transfer: move work orders from one process to the next inside one cell department.

Model: the header carries the transition (Current Process to Next Process). Every row must be
a work order whose active operation is currently in the header's Current Process. A row in
another process is refused, never rewritten.

Initial Process Assignment (``initial_assignment``, GK-DEC-0004): a work order that was already in
the department when process control was switched on has no process. An initial assignment
has no Current Process, and every row must be such a work order. Nothing is ever back-filled
or guessed.

DAY 1: the draft is complete. Each row's active operation and current process are re-read from
the database on every save (one batched read for all rows), and the department, processes,
scope and duplicates are validated. SUBMISSION IS REFUSED: the locked update of Manufacturing
Operation and Manufacturing Work Order, its history and its cancellation come with the Day 2
transition service. A Process Transfer can therefore never appear to have moved a job that did
not move.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr

from jewellery_erpnext.jewellery_erpnext.doc_events.process_control import (
	ActiveOperationError,
	ProcessControlError,
	bold,
	get_active_operations,
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
		if not self.department:
			frappe.throw(_("Select the Department."), ProcessControlError)
		department = frappe.db.get_value(
			"Department", self.department, ["company", "manufacturer"], as_dict=True
		)
		if not department:
			frappe.throw(
				_("Department {0} does not exist.").format(bold(self.department))
			)
		if not is_process_control_enabled(self.department):
			frappe.throw(
				_("Department {0} does not use process control.").format(
					bold(self.department)
				),
				ProcessControlError,
			)
		if department.company != self.company:
			frappe.throw(
				_("Department {0} belongs to Company {1}, not {2}.").format(
					bold(self.department), bold(department.company), bold(self.company)
				),
				ProcessControlError,
			)
		if department.manufacturer and department.manufacturer != self.manufacturer:
			frappe.throw(
				_("Department {0} belongs to Manufacturer {1}, not {2}.").format(
					bold(self.department),
					bold(department.manufacturer),
					bold(self.manufacturer),
				),
				ProcessControlError,
			)

	def validate_processes(self):
		if self.initial_assignment:
			if self.current_process:
				frappe.throw(
					_(
						"An Initial Process Assignment is for work orders that have no process yet, so "
						"Current Process must be empty."
					),
					ProcessControlError,
				)
			fieldnames = ("next_process",)
		else:
			if not self.current_process:
				frappe.throw(
					_(
						"Select the Current Process. For work orders that have no process yet, tick "
						"Initial Process Assignment."
					),
					ProcessControlError,
				)
			fieldnames = ("current_process", "next_process")
		for fieldname in fieldnames:
			label = _(self.meta.get_label(fieldname))
			process = get_process(self.get(fieldname))
			if not process:
				frappe.throw(
					_("Select the {0}.").format(label)
					if not self.get(fieldname)
					else _("{0} {1} does not exist.").format(
						label, bold(self.get(fieldname))
					),
					ProcessControlError,
				)
			if process.department != self.department:
				frappe.throw(
					_("{0} {1} belongs to Department {2}, not {3}.").format(
						label,
						bold(process.name),
						bold(process.department),
						bold(self.department),
					),
					ProcessControlError,
				)
			if process.disabled:
				frappe.throw(
					_("{0} {1} is disabled.").format(label, bold(process.name)),
					ProcessControlError,
				)
		if self.current_process == self.next_process:
			frappe.throw(
				_(
					"Current Process and Next Process are both {0}. Choose a different Next Process."
				).format(bold(self.current_process)),
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
			if not mwo:
				frappe.throw(
					_("Row #{0}: select a Manufacturing Work Order.").format(row.idx),
					ProcessControlError,
				)
			if mwo in seen:
				frappe.throw(
					_(
						"Row #{0}: Manufacturing Work Order {1} is already in row #{2}."
					).format(row.idx, bold(mwo), seen[mwo]),
					ProcessControlError,
				)
			seen[mwo] = row.idx
		active = get_active_operations(seen)
		for row in self.operations:
			self.set_row_from_active_operation(
				row, active[cstr(row.manufacturing_work_order).strip()]
			)

	def set_row_from_active_operation(self, row, mop):
		if not isinstance(mop, dict):
			frappe.throw(_("Row #{0}: {1}").format(row.idx, mop), ActiveOperationError)
		mwo = mop.work_order
		prefix = _("Row #{0}: Manufacturing Work Order {1}").format(
			row.idx, bold(mwo.name)
		)
		if mwo.docstatus != 1:
			frappe.throw(_("{0} is not submitted.").format(prefix), ProcessControlError)
		for fieldname, label in (
			("company", _("Company")),
			("manufacturer", _("Manufacturer")),
		):
			if mwo.get(fieldname) != self.get(fieldname):
				frappe.throw(
					_("{0} has {1} {2}, but this Process Transfer is for {3}.").format(
						prefix,
						label,
						bold(mwo.get(fieldname)),
						bold(self.get(fieldname)),
					),
					ProcessControlError,
				)
		if mop.department != self.department:
			frappe.throw(
				_("{0} is in Department {1}, not {2}.").format(
					prefix, bold(mop.department), bold(self.department)
				),
				ProcessControlError,
			)
		# Authoritative values from the database, never from the client.
		row.manufacturing_operation = mop.name
		row.current_process = mop.department_process
		row.next_process = self.next_process
		if self.initial_assignment:
			if mop.department_process:
				frappe.throw(
					_(
						"{0} is already in process {1}. Use a Process Transfer without Initial Process "
						"Assignment."
					).format(prefix, bold(mop.department_process)),
					ProcessControlError,
				)
			return
		if not mop.department_process:
			frappe.throw(
				_(
					"{0} has no current process yet. Give it its first process with an Initial "
					"Process Assignment."
				).format(prefix),
				ProcessControlError,
			)
		if mop.department_process != self.current_process:
			frappe.throw(
				_("{0} is in process {1}, not the Current Process {2}.").format(
					prefix, bold(mop.department_process), bold(self.current_process)
				),
				ProcessControlError,
			)
