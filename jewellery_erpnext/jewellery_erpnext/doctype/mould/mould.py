# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from jewellery_erpnext.jewellery_erpnext.doctype.mould.doc_events.utils import (
	crate_autoname,
	update_details,
)


class Mould(Document):
	def autoname(self, method=None):
		crate_autoname(self)

	def validate(self, method=None):
		update_details(self)
