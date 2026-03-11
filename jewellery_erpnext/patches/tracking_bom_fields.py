import json
import os

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

def execute():
    CUSTOM_FIELDS = {}
    path = os.path.join(os.path.dirname(__file__), "../jewellery_erpnext/doctype/tracking_bom/custom_fields")
    for file in os.listdir(path):
        if file.endswith(".json"):
             with open(os.path.join(path, file)) as f:
                 CUSTOM_FIELDS.update(json.load(f))
    create_custom_fields(CUSTOM_FIELDS)
    print("Custom Fields Created Successfully")
