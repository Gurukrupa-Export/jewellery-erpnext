"""Stop the **Items** sections collapsing on Sales Order / Delivery Note / Sales Invoice.

The Items grid is the first thing anyone opens these forms to look at, so a section that
collapses (and starts collapsed) puts the item rows an extra click away on every form load.

WHERE THE COLLAPSE COMES FROM
-----------------------------
It is NOT an ERPNext default. Core ``sales_order.json`` declares ``items_section`` with no
``collapsible`` key at all, i.e. ``collapsible = 0``::

    {
        "fieldname": "items_section",
        "fieldtype": "Section Break",
        "hide_border": 1,
        "hide_days": 1,
        "hide_seconds": 1,
        "oldfieldtype": "Section Break",
        "options": "fa fa-shopping-cart",
    }

The behaviour comes from a Property Setter added through Customize Form — at the time of
writing, ``Sales Order / items_section / collapsible = 1``. So the primary fix is to DELETE
that Property Setter rather than layer a ``collapsible = 0`` one on top: removing the
customization restores the core default, which is exactly what Customize Form itself does
when a property is set back to its default (``CustomizeForm.set_property_setters_for_docfield``
-> ``delete_property_setter``).

WHICH SECTIONS
--------------
Each of these forms splits the item area over TWO section breaks, and which one is
collapsible differs per doctype, so both are targeted:

- the section holding the ``items`` grid itself — ``items_section`` on Sales Order,
  ``section_break_30`` on Delivery Note, ``section_break_42`` on Sales Invoice;
- the section labelled "Items" that holds ``scan_barcode`` / ``set_warehouse`` —
  ``sec_warehouse`` on Sales Order, ``items_section`` on Delivery Note and Sales Invoice.

They are discovered from the resolved meta rather than hardcoded, so the patch keeps working
if Customize Form renames or reshuffles them.

Only Sales Order currently has a collapsible Items section; Delivery Note and Sales Invoice
are already ``collapsible = 0`` and the patch is a no-op there. It is written to cover all
three anyway so the same customization cannot creep back in on the other two.

Should a section ever be collapsible in the CORE json (no such case today), deleting the
Property Setter would not be enough, so the patch re-reads the meta afterwards and writes an
explicit ``collapsible = 0`` if the section is still collapsing.

Idempotent: deleting an absent Property Setter is a no-op. Can be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.expand_sales_items_sections.execute
"""

import frappe
from frappe.custom.doctype.property_setter.property_setter import delete_property_setter

PARENT_DOCTYPES = ("Sales Order", "Delivery Note", "Sales Invoice")

# Properties that make a section collapse. collapsible_depends_on is meaningless once the
# section is not collapsible, and leaving it behind lets a later Customize Form save
# resurrect the collapse from a stale rule.
COLLAPSE_PROPERTIES = ("collapsible", "collapsible_depends_on")


def _items_sections(doctype):
	"""Fieldnames of the section breaks governing the item area of ``doctype``.

	Returns the section that directly contains the ``items`` grid plus the one labelled
	"Items" (the scan-barcode / warehouse block), which on these three doctypes are two
	different sections.
	"""
	meta = frappe.get_meta(doctype)
	fields = meta.fields
	names = [df.fieldname for df in fields]
	if "items" not in names:
		return []

	sections = []

	# The section the grid actually sits in: walk back from the grid.
	for i in range(names.index("items") - 1, -1, -1):
		if fields[i].fieldtype == "Section Break":
			sections.append(fields[i].fieldname)
			break

	# Any section labelled "Items" (the scan-barcode / set-warehouse block).
	for df in fields:
		if df.fieldtype == "Section Break" and (df.label or "").strip() == "Items":
			if df.fieldname not in sections:
				sections.append(df.fieldname)

	return sections


def _expand(doctype, fieldname):
	"""Make one section non-collapsible. Returns True when something changed."""
	changed = False

	for prop in COLLAPSE_PROPERTIES:
		if frappe.db.exists(
			"Property Setter",
			{"doc_type": doctype, "field_name": fieldname, "property": prop},
		):
			delete_property_setter(doctype, prop, fieldname)
			changed = True

	if not changed:
		return False

	frappe.clear_cache(doctype=doctype)

	# Deleting restores the core default. If the core itself marks the section collapsible
	# (no such case today), fall back to an explicit override.
	field = frappe.get_meta(doctype).get_field(fieldname)
	if field and field.collapsible:
		frappe.make_property_setter(
			{
				"doctype_or_field": "DocField",
				"doctype": doctype,
				"fieldname": fieldname,
				"property": "collapsible",
				"value": 0,
				"property_type": "Check",
			},
			is_system_generated=False,
		)
		frappe.clear_cache(doctype=doctype)

	return True


def execute():
	touched = []

	for doctype in PARENT_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue
		for fieldname in _items_sections(doctype):
			if _expand(doctype, fieldname):
				touched.append(f"{doctype}.{fieldname}")

	if touched:
		frappe.db.commit()
		frappe.logger().info(
			"expand_sales_items_sections: made non-collapsible -> " + ", ".join(touched)
		)
	else:
		frappe.logger().info(
			"expand_sales_items_sections: no collapsible Items sections found, nothing to do"
		)
