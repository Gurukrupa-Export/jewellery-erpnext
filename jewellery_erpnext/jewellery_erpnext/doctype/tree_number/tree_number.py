# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint


class TreeNumber(Document):
	def after_insert(self):
		counter = cint(frappe.db.get_value("Tree Number", {}, "max(counter)"))
		self.db_set("counter", counter + 1)
