# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from jewellery_erpnext.jewellery_erpnext.doctype.mould.doc_events.utils import (
	crate_autoname,
	update_details,
	validate_unique_item_code,
)


class Mould(Document):
	def autoname(self, method=None):
		crate_autoname(self)

	def validate(self, method=None):
		validate_unique_item_code(self)
		update_details(self)
