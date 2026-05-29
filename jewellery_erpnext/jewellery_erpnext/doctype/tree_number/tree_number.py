# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class TreeNumber(Document):
	def validate(self):
		if self.manufacturing_work_order:
			existing = frappe.db.get_value(
				"Tree Number",
				{
					"manufacturing_work_order": self.manufacturing_work_order,
					"name": ("!=", self.name or "__nonexistent__"),
				},
				"name",
			)
			if existing:
				frappe.throw(
					_(
						"Tree Number {0} is already assigned to Manufacturing Work Order {1}. "
						"Only one Tree Number per MWO is allowed."
					).format(
						frappe.bold(existing),
						frappe.bold(self.manufacturing_work_order),
					),
					title=_("Duplicate Tree Number"),
				)

	def after_insert(self):
		counter = cint(frappe.db.get_value("Tree Number", {}, "max(counter)"))
		self.db_set("counter", counter + 1)
