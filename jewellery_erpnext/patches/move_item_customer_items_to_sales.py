"""Item doctype layout moves — two operations in one patch.

Move 1: ``customer_items`` Table from the Manufacturing tab to the Sales tab,
wrapping it in a new ``Customer Items`` section break.

Move 2: ``design_attribute`` Table inside the Design Attribute tab gets its
own ``Design Attribute`` section break (currently sits directly after
``custom_zodiac`` with no section header).

Current layout (from live field_order):
  Sales tab ends at [175] no_of_months → [176] uom_tab
  Manufacturing tab: [194] manufacturing → [195] customer_details →
    [196] customer_items → [197] is_sub_contracted_item ...
  Design Attribute tab: ... [323] custom_zodiac → [324] design_attribute
    → [325] creativity (Tab Break)  — no section header on design_attribute

Target:
  Sales tab: ... [175] no_of_months → [NEW] custom_section_customer_items
    → [MOVED] customer_items → [176] uom_tab
  Manufacturing tab: [194] manufacturing → [195] customer_details →
    [197] is_sub_contracted_item (customer_items removed)
  Design Attribute tab: ... custom_zodiac →
    [NEW] custom_section_design_attribute → design_attribute →
    [325] creativity (Tab Break)

Sites with no Item ``field_order`` Property Setter get one seeded from the current
effective order first: ``customer_items`` is a standard field, so ``insert_after``
cannot move it and only a ``field_order`` entry can.

Safe to re-run: guarded by existence checks and idempotent key lookups.

    bench --site <site> execute jewellery_erpnext.patches.move_item_customer_items_to_sales.execute
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.patches.field_order_utils import (
	ensure_field_order_setter,
	rewrite_insert_after_chain,
)

DOCTYPE = "Item"

# --- Move 1: customer_items from Manufacturing tab to Sales tab ---
SALES_ANCHOR = "no_of_months"  # last field in Sales tab before uom_tab
SALES_SECTION = "custom_section_customer_items"
SALES_SECTION_LABEL = "Customer Items"
SALES_TABLE = "customer_items"
# Inherited from erpnext's `customer_details` Section Break, which is the only
# thing gating `customer_items` today (the table has no depends_on of its own).
SALES_SECTION_DEPENDS_ON = "eval:!doc.is_fixed_asset"

# --- Move 2: design_attribute gets its own section in Design Attribute tab ---
DA_ANCHOR = "custom_zodiac"  # last field before design_attribute
DA_SECTION = "custom_section_design_attribute"
DA_SECTION_LABEL = "Design Attribute"
DA_TABLE = "design_attribute"

ALL_NEW_SECTIONS = (SALES_SECTION, DA_SECTION)


def execute():
	# 1. Create both Section Breaks as Custom Fields
	create_custom_fields(
		{
			DOCTYPE: [
				{
					"fieldname": SALES_SECTION,
					"label": SALES_SECTION_LABEL,
					"fieldtype": "Section Break",
					"insert_after": SALES_ANCHOR,
					# erpnext gates `customer_items` through its enclosing
					# `customer_details` section; carry that gate over so the table
					# does not start showing on fixed-asset Items.
					"depends_on": SALES_SECTION_DEPENDS_ON,
				},
				{
					"fieldname": DA_SECTION,
					"label": DA_SECTION_LABEL,
					"fieldtype": "Section Break",
					"insert_after": DA_ANCHOR,
				},
			]
		},
		update=True,
	)
	# Anchor each Section Break on the field that PRECEDES it and chain its table
	# off the section. This only governs custom fields absent from field_order;
	# rewrite_insert_after_chain skips the standard `customer_items` (no Custom
	# Field row), so it is the field_order splice below that actually moves it.
	rewrite_insert_after_chain(DOCTYPE, (SALES_SECTION, SALES_TABLE), SALES_ANCHOR)
	rewrite_insert_after_chain(DOCTYPE, (DA_SECTION, DA_TABLE), DA_ANCHOR)

	# Drop any insert_after Property Setter that would override the chain above.
	for sec_field in ALL_NEW_SECTIONS:
		frappe.db.delete(
			"Property Setter",
			{
				"doc_type": DOCTYPE,
				"field_name": sec_field,
				"property": "insert_after",
			},
		)

	# 2. Update field_order Property Setter (the authoritative layout source).
	# Seeded from the effective order when missing — without it a standard field
	# like `customer_items` cannot be moved at all.
	frappe.clear_cache(doctype=DOCTYPE)
	ps = ensure_field_order_setter(DOCTYPE)
	known = {df.fieldname for df in frappe.get_meta(DOCTYPE, cached=False).fields}
	field_order = json.loads(ps.value)

	new_order = field_order
	for anchor, block in (
		(SALES_ANCHOR, (SALES_SECTION, SALES_TABLE)),
		(DA_ANCHOR, (DA_SECTION, DA_TABLE)),
	):
		spliced = splice_after(new_order, anchor, block, known)
		if spliced is None:
			print(
				f"[move_item_customer_items_to_sales] skipped {block}:"
				f" anchor {anchor} or a block field is missing — layout left as is"
			)
			continue
		new_order = spliced

	if new_order != field_order:
		frappe.db.set_value("Property Setter", ps.name, "value", json.dumps(new_order))

	# 3. Re-index Custom Field rows
	frappe.db.sql("UPDATE `tabCustom Field` SET idx = 0 WHERE dt = %s", (DOCTYPE,))
	frappe.db.commit()
	frappe.clear_cache(doctype=DOCTYPE)
	print(
		f"[move_item_customer_items_to_sales]"
		f" {SALES_TABLE} under {SALES_SECTION} ({SALES_SECTION_LABEL})"
		f" after {SALES_ANCHOR};"
		f" {DA_TABLE} under {DA_SECTION} ({DA_SECTION_LABEL})"
		f" after {DA_ANCHOR}"
	)


def splice_after(order, anchor, block, known):
	"""Return ``order`` with ``block`` placed directly after ``anchor``, or None if the
	anchor is not in ``order`` or a block field does not exist on the doctype.

	Validates before removing anything, so a missing anchor can never drop the block
	out of the order. Idempotent: an already-spliced order comes back unchanged.
	"""
	if anchor not in order or anchor in block or not set(block) <= known:
		return None
	remaining = [name for name in order if name not in block]
	idx = remaining.index(anchor) + 1
	return remaining[:idx] + list(block) + remaining[idx:]
