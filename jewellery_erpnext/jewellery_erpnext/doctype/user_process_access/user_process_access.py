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
Relief access has no expiry date: the manager disables the row when the relief ends (GK-DEC-0001).
"""

import frappe
from frappe import _
from frappe.model.document import Document

from jewellery_erpnext.jewellery_erpnext.doc_events.process_control import (
	ProcessControlError,
	bold,
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
		# GK-DEC-0001 (delegated decision, awaiting GK ratification): only a Process Access Manager
		# writes access, and nobody, a Process Access Manager included, grants or edits their own.
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
			frappe.throw(_("User {0} does not exist.").format(bold(self.user)))
		if user.user_type != "System User":
			frappe.throw(
				_(
					"User {0} is not a System User and cannot make manufacturing transactions."
				).format(bold(self.user)),
				ProcessControlError,
			)
		if self.enabled and not user.enabled:
			frappe.throw(
				_("User {0} is disabled; the access row must be disabled too.").format(
					bold(self.user)
				),
				ProcessControlError,
			)

	def set_scope_from_process(self):
		process = get_process(self.department_process)
		if not process:
			frappe.throw(
				_("Department Process {0} does not exist.").format(
					bold(self.department_process)
				)
			)
		if self.enabled and process.disabled:
			frappe.throw(
				_("Department Process {0} is disabled and cannot be granted.").format(
					bold(self.department_process)
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
				).format(bold(self.user)),
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
					bold(self.user),
					bold(self.department_process),
					bold(duplicate),
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
			# Two managers saving defaults for the same user at once: the row lock (through the
			# user/process unique index) makes the second save wait and then see the first.
			for_update=True,
		)
		if other:
			frappe.throw(
				_("User {0} already has {1} as the default process for {2}.").format(
					bold(self.user),
					bold(other),
					bold(self.department),
				),
				ProcessControlError,
			)


def on_doctype_update():
	# One row per user and process, enforced by the database: the validate check above only gives
	# the friendly message, and two simultaneous saves would both pass it.
	frappe.db.add_unique("User Process Access", ["user", "department_process"])
