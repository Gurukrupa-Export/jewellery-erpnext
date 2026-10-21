import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

DOCTYPE = "Customer"

# Each table gets a Section Break of its own so it renders full width, in this order.
TABLE_SECTIONS = [
	(
		"custom_section_order_type_criteria_tab",
		"Order Type Criteria Section",
		["custom_order_type_criteria"],
	),
	("custom_section_sales_type_tab", "Sales Type Section", ["sales_type"]),
	("custom_section_metal_criteria_tab", "Metal Criteria Section", ["metal_criteria"]),
	(
		"custom_section_diamond_price_list_tab",
		"Diamond Price List Section",
		["custom_diamond_price_list_table"],
	),
	(
		"custom_section_item_categories_tab",
		"Item Categories Section",
		[
			"custom_allowed_item_category_for_invoice",
			"custom_item_categories",
			"custom_approval_warehouse",
		],
	),
]

# Certification Charges block, placed right after the tables.
CERTIFICATION_FIELDS = [
	"custom_section_break_xxrxq",
	"custom_ignore_po_creation_for_certification",
	"custom_separate_hallmarking_invoice",
	"custom_block_refining",
	"custom_no_wastage",
	"custom_column_break_certification",
	"custom_fetch_certification_charge_from_price_list",
	"custom_making_rates_based_on_custom_code",
	"custom_allow_regular_goods_instead_of_customer_goods",
]

# The tables block goes after the first of these that exists.
BLOCK_ANCHORS = [
	"custom_gemstone_price_list_type",
	"diamond_price_list",
	"supplier_numbers",
]

# Breaks that used to wrap the tables, sent back next to their own fields.
STRAY_BREAKS = {
	"section_break_yfp4k": "brand",
	"column_break_6v9nw": "compute_making_charges_on",
}

REPS_FIELDS = ["custom_section_customer_reps_tab", "custom_customer_representatives"]


def execute():
	"""
	Layout is written as a "field_order" Property Setter rather than through insert_after:
	- a Section Break with the same insert_after as its table is pushed to the end of the
	  anchor's section by Meta.sort_fields, so the table renders above its own header;
	- gke_customization's Custom Field fixtures are imported with force=True on every
	  migrate and reset the tables' insert_after anyway.
	"""
	create_breaks()

	frappe.clear_cache(doctype=DOCTYPE)
	meta = frappe.get_meta(DOCTYPE, cached=False)
	# Start from the order currently rendered so every field is listed and untouched ones stay put
	field_order = [df.fieldname for df in meta.fields]
	fieldtypes = {df.fieldname: df.fieldtype for df in meta.fields}

	block = []
	for section_fieldname, _label, fieldnames in TABLE_SECTIONS:
		block += [section_fieldname, *fieldnames]
	block += CERTIFICATION_FIELDS

	remove_fields(field_order, block)
	anchor = next((f for f in BLOCK_ANCHORS if f in field_order), None)
	if anchor:
		insert_after(field_order, anchor, [f for f in block if f in fieldtypes])

	for fieldname, target in STRAY_BREAKS.items():
		if fieldname in field_order and target in field_order:
			remove_fields(field_order, [fieldname])
			insert_after(field_order, target, [fieldname])

	remove_fields(field_order, REPS_FIELDS)
	if "primary_address" in field_order:
		insert_after(
			field_order,
			last_field_of_section(field_order, fieldtypes, "primary_address"),
			[f for f in REPS_FIELDS if f in fieldtypes],
		)

	make_property_setter(
		DOCTYPE, None, "field_order", json.dumps(field_order), "Data", for_doctype=True
	)

	# Left behind by the previous version of this patch; width has no effect on form layout
	frappe.db.delete(
		"Property Setter",
		{
			"doc_type": DOCTYPE,
			"property": "width",
			"value": "100%",
			"field_name": [
				"in",
				[f for _s, _l, fields in TABLE_SECTIONS for f in fields] + REPS_FIELDS,
			],
		},
	)

	frappe.clear_cache(doctype=DOCTYPE)


def create_breaks():
	fields = []
	previous_field = BLOCK_ANCHORS[0]
	for section_fieldname, label, fieldnames in TABLE_SECTIONS:
		fields.append(
			{
				"fieldname": section_fieldname,
				"label": label,
				"fieldtype": "Section Break",
				"insert_after": previous_field,
			}
		)
		previous_field = fieldnames[-1]

	fields += [
		{
			"fieldname": "custom_section_customer_reps_tab",
			"label": "Customer Representatives Section",
			"fieldtype": "Section Break",
			"insert_after": "primary_address",
		},
		{
			"fieldname": "custom_column_break_certification",
			"fieldtype": "Column Break",
			"insert_after": "custom_no_wastage",
		},
	]

	create_custom_fields({DOCTYPE: fields}, update=True)


def remove_fields(field_order, fieldnames):
	for fieldname in fieldnames:
		if fieldname in field_order:
			field_order.remove(fieldname)


def insert_after(field_order, anchor, fieldnames):
	idx = field_order.index(anchor) + 1
	field_order[idx:idx] = fieldnames


def last_field_of_section(field_order, fieldtypes, fieldname):
	idx = field_order.index(fieldname)
	while idx + 1 < len(field_order) and fieldtypes[field_order[idx + 1]] not in (
		"Section Break",
		"Tab Break",
	):
		idx += 1
	return field_order[idx]
