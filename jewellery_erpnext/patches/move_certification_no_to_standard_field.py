"""Move the emergency "Certification No" Custom Field on Exploded Product Details into the
standard ``certification_no`` field.

Production got a Custom Field (Customize Form, so named ``custom_...``) on the Product
Certification item grid as a stop-gap, and users have filled it in. The field is now
standard, but under a different fieldname, i.e. a different column -- without this patch the
standard field would show up empty and the entered values would stay behind in the old column.

Runs post_model_sync, so the standard ``certification_no`` column already exists. For every
Custom Field on the child table that is the stop-gap (label "Certification No" or fieldname
``custom_certification_no``), copy its non-blank values into rows whose standard value is
still blank, then delete the Custom Field. Deleting a Custom Field does not drop its column,
so the original values stay in the table as a backup.

Idempotent: once the Custom Field is gone there is nothing left to match.
"""

import frappe

DOCTYPE = "Exploded Product Details"
TARGET = "certification_no"


def execute():
	if not frappe.db.has_column(DOCTYPE, TARGET):
		return

	custom_fields = frappe.get_all(
		"Custom Field",
		filters={"dt": DOCTYPE},
		or_filters={
			"label": "Certification No",
			"fieldname": "custom_certification_no",
		},
		fields=["name", "fieldname"],
	)

	for cf in custom_fields:
		if cf.fieldname != TARGET and frappe.db.has_column(DOCTYPE, cf.fieldname):
			frappe.db.sql(
				f"""
				update `tab{DOCTYPE}`
				set `{TARGET}` = `{cf.fieldname}`
				where ifnull(`{cf.fieldname}`, '') != ''
					and ifnull(`{TARGET}`, '') = ''
				"""
			)

		frappe.delete_doc("Custom Field", cf.name, ignore_permissions=True, force=True)

	frappe.clear_cache(doctype=DOCTYPE)
