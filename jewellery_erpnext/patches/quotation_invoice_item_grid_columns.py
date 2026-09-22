"""Make the ``Quotation E Invoice Item`` grid render its columns.

The child DocType ships with no field flagged ``in_list_view``, so the Table
in ``Quotation.custom_invoice_item`` draws rows with only the checkbox, row
number and edit pencil — an apparently empty grid. The data is present; there
are simply no columns to paint it into.

This patch applies the same layout its siblings (``Sales Order E Invoice
Item``, ``Product Return Form E Invoice Item``) already ship with, as
Property Setters — i.e. exactly what Customize Form would write — rather than
editing the DocType JSON, which is owned by ``gke_customization``::

    item_code 3 | delivery_date 2 | qty 1 | rate 2 | amount 2   (= 10, full width)

Safe to re-run: ``make_property_setter`` deletes and recreates each keyed
Property Setter, and every step is guarded by an existence check. Can be run
ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.quotation_invoice_item_grid_columns.execute
"""

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

DOCTYPE = "Quotation E Invoice Item"

# fieldname -> grid width, in Frappe's 10-column budget.
GRID_COLUMNS = {
	"item_code": 3,
	"delivery_date": 2,
	"qty": 1,
	"rate": 2,
	"amount": 2,
}


def execute():
	# Owned by gke_customization; skip cleanly if that app is not installed.
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	meta = frappe.get_meta(DOCTYPE)
	applied = []

	for fieldname, columns in GRID_COLUMNS.items():
		if not meta.get_field(fieldname):
			continue

		make_property_setter(DOCTYPE, fieldname, "in_list_view", "1", "Check")
		make_property_setter(DOCTYPE, fieldname, "columns", columns, "Int")
		applied.append(f"{fieldname}({columns})")

	frappe.db.commit()
	frappe.clear_cache(doctype=DOCTYPE)
	print(f"[quotation_invoice_item_grid_columns] {DOCTYPE}: {', '.join(applied)}")
