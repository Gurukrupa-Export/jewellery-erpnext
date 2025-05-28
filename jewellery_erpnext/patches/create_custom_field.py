import os, json

def execute():
	CUSTOM_FIELDS = {}
	path = os.path.join(os.path.dirname(__file__), "../jewellery_erpnext/custom_fields")
	for file in os.listdir(path):
		if file in ["payment_entry_reference.json", "stock_entry_mop_item.json", "stock_entry.json"]:
			with open(os.path.join(path, file), "r") as f:
				CUSTOM_FIELDS.update(json.load(f))

	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(CUSTOM_FIELDS)