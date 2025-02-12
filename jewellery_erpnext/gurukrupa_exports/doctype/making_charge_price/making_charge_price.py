# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MakingChargePrice(Document):
	pass


def on_doctype_update():
	frappe.db.add_index("Making Charge Price", ["customer"])
