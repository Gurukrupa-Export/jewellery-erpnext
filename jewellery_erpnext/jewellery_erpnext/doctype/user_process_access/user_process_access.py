# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Which processes a user may transact in. This is operator authority.

It is kept apart from worker eligibility (``Employee.custom_operation``) and from job state (the
work order's current process). One row grants one process, so:

* a user with one process has one row;
* a relief or substitute operator has one row per process covered, with at most one default per
  department;
* a data-entry user needs no Employee or Department Operation, only rows.

``is_default`` only pre-selects a process. Default and grant are the same row, and a default on
a disabled row is refused, so a default never widens access. Rows are written by the Process
Access Manager role only (System Manager can read them), and nobody may grant themselves access.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from jewellery_erpnext.jewellery_erpnext.doc_events.process_control import (
	ProcessControlError,
	get_process,
)


class UserProcessAccess(Document):
	def validate(self):
		self.validate_not_self_grant()
		self.validate_user()
		self.set_scope_from_process()
		self.set_employee()
		self.validate_duplicate()
		self.validate_default()

	def validate_not_self_grant(self):
		# PROVISIONAL rule (Day 1): who may grant access is an open business decision. Until it is
		# settled, nobody, the Process Access Manager included, may grant or edit their own access.
		if self.user == frappe.session.user and frappe.session.user != "Administrator":
			frappe.throw(
				_(
					"You cannot grant or change your own process access. Ask another Process Access Manager."
				),
				frappe.PermissionError,
			)

	def validate_user(self):
		user = frappe.db.get_value(
			"User", self.user, ["enabled", "user_type"], as_dict=True
		)
		if not user:
			frappe.throw(_("User {0} does not exist.").format(frappe.bold(self.user)))
		if user.user_type != "System User":
			frappe.throw(
				_(
					"User {0} is not a System User and cannot make manufacturing transactions."
				).format(frappe.bold(self.user)),
				ProcessControlError,
			)
		if self.enabled and not user.enabled:
			frappe.throw(
				_("User {0} is disabled; the access row must be disabled too.").format(
					frappe.bold(self.user)
				),
				ProcessControlError,
			)

	def set_scope_from_process(self):
		process = get_process(self.department_process)
		if not process:
			frappe.throw(
				_("Department Process {0} does not exist.").format(
					frappe.bold(self.department_process)
				)
			)
		if self.enabled and process.disabled:
			frappe.throw(
				_("Department Process {0} is disabled and cannot be granted.").format(
					frappe.bold(self.department_process)
				),
				ProcessControlError,
			)
		# The scope always comes from the process, never from separate inputs, so a grant cannot
		# combine one department's process with another company or manufacturer.
		self.department = process.department
		self.company = process.company
		self.manufacturer = process.manufacturer

	def set_employee(self):
		employees = frappe.get_all(
			"Employee",
			filters={"user_id": self.user, "status": "Active"},
			pluck="name",
			limit=2,
		)
		if len(employees) > 1:
			frappe.throw(
				_(
					"User {0} is linked to more than one active Employee. Fix the Employee master before "
					"granting process access."
				).format(frappe.bold(self.user)),
				ProcessControlError,
			)
		self.employee = employees[0] if employees else None

	def validate_duplicate(self):
		duplicate = frappe.db.get_value(
			"User Process Access",
			{
				"user": self.user,
				"department_process": self.department_process,
				"name": ["!=", self.name or ""],
			},
			"name",
		)
		if duplicate:
			frappe.throw(
				_("User {0} already has access to {1} in {2}.").format(
					frappe.bold(self.user),
					frappe.bold(self.department_process),
					duplicate,
				),
				ProcessControlError,
			)

	def validate_default(self):
		if not self.is_default:
			return
		if not self.enabled:
			frappe.throw(
				_("A disabled access row cannot be the default process."),
				ProcessControlError,
			)
		other = frappe.db.get_value(
			"User Process Access",
			{
				"user": self.user,
				"department": self.department,
				"is_default": 1,
				"enabled": 1,
				"name": ["!=", self.name or ""],
			},
			"department_process",
		)
		if other:
			frappe.throw(
				_("User {0} already has {1} as the default process for {2}.").format(
					frappe.bold(self.user),
					frappe.bold(other),
					frappe.bold(self.department),
				),
				ProcessControlError,
			)
