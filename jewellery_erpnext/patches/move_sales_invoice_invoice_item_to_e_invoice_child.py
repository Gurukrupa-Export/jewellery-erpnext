"""Point ``Sales Invoice.invoice_item`` at its own child doctype.

The Custom Field was defined with ``options = "Sales Invoice Item"``, the same doctype as the
standard ``items`` table. The e-invoice lines (``Studded Natural Diamond Jewellery`` ...) are
E Invoice Item names, not Items, so link validation on save/submit rejects them:
``Could not find Row #1: Item: Studded Natural Diamond Jewellery``.

Every other order doctype already uses a dedicated child (``Sales Invoice E Invoice Item``
links ``item_code`` to E Invoice Item). This patch

1. sets the Custom Field's options to ``Sales Invoice E Invoice Item``, and
2. moves the existing rows (``tabSales Invoice Item`` with ``parentfield = 'invoice_item'``)
   into ``tabSales Invoice E Invoice Item``, keeping their names so nothing else changes.

Idempotent: re-running finds no rows left to move and the options already set.

	bench --site <site> execute jewellery_erpnext.patches.move_sales_invoice_invoice_item_to_e_invoice_child.execute
"""

import frappe

PARENT = "Sales Invoice"
FIELD = "invoice_item"
OLD_CHILD = "Sales Invoice Item"
NEW_CHILD = "Sales Invoice E Invoice Item"


def _columns(doctype):
	return frappe.db.get_table_columns(doctype)


def execute():
	if not frappe.db.exists("DocType", NEW_CHILD):
		return

	cf = frappe.db.get_value(
		"Custom Field",
		{"dt": PARENT, "fieldname": FIELD},
		["name", "options"],
		as_dict=True,
	)
	if cf and cf.options != NEW_CHILD:
		frappe.db.set_value(
			"Custom Field", cf.name, "options", NEW_CHILD, update_modified=False
		)

	# Copy the rows first, then delete only the ones that landed in the new table, so a failed
	# copy can never lose data.
	common = [c for c in _columns(NEW_CHILD) if c in set(_columns(OLD_CHILD))]
	cols = ", ".join(f"`{c}`" for c in common)
	where = "`parentfield` = %s AND `parenttype` = %s"
	frappe.db.sql(
		f"INSERT IGNORE INTO `tab{NEW_CHILD}` ({cols}) "
		f"SELECT {cols} FROM `tab{OLD_CHILD}` WHERE {where}",
		(FIELD, PARENT),
	)
	# Only rows that now exist in the new table (same name) are removed from the old one.
	frappe.db.sql(
		f"DELETE o FROM `tab{OLD_CHILD}` o JOIN `tab{NEW_CHILD}` n ON n.name = o.name "
		"WHERE o.`parentfield` = %s AND o.`parenttype` = %s",
		(FIELD, PARENT),
	)

	frappe.clear_cache(doctype=PARENT)
	frappe.db.commit()
	left = frappe.db.count(OLD_CHILD, {"parentfield": FIELD, "parenttype": PARENT})
	print(
		f"[move_sales_invoice_invoice_item_to_e_invoice_child] rows left in old table: {left}"
	)
