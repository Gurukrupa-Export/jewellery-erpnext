import json
import os

import frappe


def after_migrate():
	# pass
	create_custom_fields()
	create_property_setter()
	# create_indexes()


# def create_indexes():
# 	print("Creating/Updating Custom Fields....")
# 	index_data = frappe._dict()
# 	path = os.path.join(os.path.dirname(__file__), "jewellery_erpnext/doc_indexes")
# 	for file in os.listdir(path):
# 		with open(os.path.join(path, file), "r") as f:
# 			index_data.append(json.load(f))


def create_custom_fields():
	CUSTOM_FIELDS = {}
	print("Creating/Updating Custom Fields....")
	path = os.path.join(os.path.dirname(__file__), "jewellery_erpnext/custom_fields")
	for file in os.listdir(path):
		with open(os.path.join(path, file), "r") as f:
			CUSTOM_FIELDS.update(json.load(f))
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(CUSTOM_FIELDS)


def create_property_setter():
	from frappe import make_property_setter

	PPS = {}
	print("Creating/Updating Property Setter....")
	path = os.path.join(os.path.dirname(__file__), "jewellery_erpnext/property_setter")
	for file in os.listdir(path):
		with open(os.path.join(path, file), "r") as f:
			args = json.load(f)
			PPS.update(args)

	for row in PPS:
		for field in PPS[row]:
			if isinstance(field.get("value"), list):
				field["value"] = json.dumps(field["value"])
			make_property_setter(field, is_system_generated=False)
