import json

import frappe
from frappe.modules.import_file import import_doc


def execute():
	path = frappe.get_app_path(
		"jewellery_erpnext", "jewellery_erpnext", "custom_fields", "custom_field.json"
	)

	with open(path) as f:
		docs = json.load(f)

	for doc in docs:
		import_doc(doc, ignore_version=True)
