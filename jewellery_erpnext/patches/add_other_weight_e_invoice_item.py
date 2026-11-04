"""Create the "Other Weight" E Invoice Item.

The Sales Order Invoice Item table ends with an "Other Weight" row that carries the order's
``custom_other_weight`` as its quantity, with rate, amount and tax all 0 (see
``validate_item_dharm`` in doc_events/sales_order.py). That table's ``item_code`` links to
E Invoice Item, so the record has to exist.

``charge_type`` and ``sales_type`` are left empty, as on the existing E Invoice Items, so
mandatory and link checks are skipped. Create-only: an existing record is left untouched.
"""

import frappe

from jewellery_erpnext.jewellery_erpnext.doc_events.sales_order import (
	OTHER_WEIGHT_INVOICE_ITEM,
)


def execute():
	if frappe.db.exists("E Invoice Item", OTHER_WEIGHT_INVOICE_ITEM):
		return

	doc = frappe.new_doc("E Invoice Item")
	doc.item_name = OTHER_WEIGHT_INVOICE_ITEM
	doc.hsn_code = "711319"
	doc.uom = "Gram"
	doc.flags.ignore_mandatory = True
	# Bare/CI sites may lack the Gram UOM or the HSN master; neither should block a migrate.
	doc.flags.ignore_links = True
	doc.insert(ignore_permissions=True)
