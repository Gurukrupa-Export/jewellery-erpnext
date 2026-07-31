import json
import os


def execute():
	CUSTOM_FIELDS = {}
	path = os.path.join(os.path.dirname(__file__), "../jewellery_erpnext/custom_fields")
	for file in os.listdir(path):
		if file in [
			"sales_order.json","sales_order_item.json","delivery_note.json","delivery_note_item.json","sales_invoice.json","sales_invoice_item.json"
		]:
			with open(os.path.join(path, file), "r") as f:
				CUSTOM_FIELDS.update(json.load(f))

	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(CUSTOM_FIELDS)
