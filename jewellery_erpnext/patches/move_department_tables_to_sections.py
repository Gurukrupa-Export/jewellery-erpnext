"""Give ``custom_designation_details`` and ``custom_department_contact_details``
their own full-width Section Breaks on the Department doctype.

Current layout (from live field_order, 24 entries):
  [15] custom_section_break_uomqj  (orphaned — deleted from DB)
  [16] custom_designation_details  (Table — no visible section header)
  [17] approvers ...

``custom_department_contact_details`` is a Custom Field Table that is NOT in
the field_order at all (appended at end of form).

Target:
  [15] custom_section_designation_details  (Section Break — new)
  [16] custom_designation_details           (Table)
  [17] custom_section_department_contact    (Section Break — new)
  [18] custom_department_contact_details    (Table — moved from end)
  [19] approvers ...

Both are Custom Fields so ``insert_after`` is DB-writable.  Layout is
controlled by the field_order Property Setter.  Child table data is safe
(linked by ``parent``).

Safe to re-run: guarded by existence checks and idempotent key lookups.

    bench --site <site> execute jewellery_erpnext.patches.move_department_tables_to_sections.execute
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

DOCTYPE = "Department"
# First table and its new section (replaces orphaned custom_section_break_uomqj)
DESIGNATION_SECTION = "custom_section_designation_details"
DESIGNATION_LABEL = "Designation Details"
DESIGNATION_TABLE = "custom_designation_details"
# Second table and its new section (moved from appended-at-end)
CONTACT_SECTION = "custom_section_department_contact"
CONTACT_LABEL = "Department Contact Details"
CONTACT_TABLE = "custom_department_contact_details"

FIELDS = (
	DESIGNATION_SECTION,
	DESIGNATION_TABLE,
	CONTACT_SECTION,
	CONTACT_TABLE,
)


def execute():
	# 1. Create both Section Breaks as Custom Fields
	create_custom_fields(
		{
			DOCTYPE: [
				{
					"fieldname": DESIGNATION_SECTION,
					"label": DESIGNATION_LABEL,
					"fieldtype": "Section Break",
					"insert_after": DESIGNATION_TABLE,
				},
				{
					"fieldname": CONTACT_SECTION,
					"label": CONTACT_LABEL,
					"fieldtype": "Section Break",
					"insert_after": CONTACT_TABLE,
				},
			]
		},
		update=True,
	)
	# Correct the insert_after — section comes BEFORE its table
	for sec_field, table_field in (
		(DESIGNATION_SECTION, DESIGNATION_TABLE),
		(CONTACT_SECTION, CONTACT_TABLE),
	):
		frappe.db.set_value(
			"Custom Field",
			{"dt": DOCTYPE, "fieldname": sec_field},
			"insert_after",
			table_field,
		)
		frappe.db.delete(
			"Property Setter",
			{
				"doc_type": DOCTYPE,
				"field_name": sec_field,
				"property": "insert_after",
			},
		)

	# 2. Pin both Tables to 100% width
	for table_field in (DESIGNATION_TABLE, CONTACT_TABLE):
		make_property_setter(DOCTYPE, table_field, "width", "100%", "Data")

	# 3. Update field_order Property Setter
	ps_row = frappe.db.sql(
		"SELECT name, value FROM `tabProperty Setter`"
		" WHERE doc_type=%s AND property='field_order'",
		(DOCTYPE,),
		as_dict=True,
	)
	if ps_row:
		field_order = json.loads(ps_row[0].value)

		# Drop any prior placement of our four fields plus the orphaned section
		for f in FIELDS + ("custom_section_break_uomqj",):
			while f in field_order:
				field_order.remove(f)

		# Re-inject designation section + table after the last field before
		# the old section area (leave_block_list is the preceding field)
		anchor = "leave_block_list"
		if anchor in field_order:
			idx = field_order.index(anchor) + 1
		else:
			idx = len(field_order)
		for i, f in enumerate((DESIGNATION_SECTION, DESIGNATION_TABLE)):
			field_order.insert(idx + i, f)

		# Inject contact section + table right after the designation table
		idx = field_order.index(DESIGNATION_TABLE) + 1
		for i, f in enumerate((CONTACT_SECTION, CONTACT_TABLE)):
			field_order.insert(idx + i, f)

		frappe.db.set_value(
			"Property Setter",
			ps_row[0].name,
			"value",
			json.dumps(field_order),
		)

	# 4. Re-index Custom Field rows
	frappe.db.sql("UPDATE `tabCustom Field` SET idx = 0 WHERE dt = %s", (DOCTYPE,))
	frappe.db.commit()
	frappe.clear_cache(doctype=DOCTYPE)
	print(
		f"[move_department_tables_to_sections]"
		f" {DESIGNATION_TABLE} under {DESIGNATION_SECTION},"
		f" {CONTACT_TABLE} under {CONTACT_SECTION}"
	)
