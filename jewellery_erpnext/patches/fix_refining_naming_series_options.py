"""Drop the Property Setter pinning Refining Entry's old naming-series options.

The series letters were corrected so they track the current type names — Scrap Refining
mints ``RFN-SCP-`` and Unused/Loose Material Refining mints ``RFN-ULM-``, instead of the
pre-rename ``RFN-DST-``/``RFN-SCP-`` pair that left the actual scrap type minting "dust".

``set_naming_series`` assigns the value, but ``naming_series`` is a Select and Frappe
validates the assigned value against the field's options. A Property Setter created via
Customize Form shadows the DocField, so the JSON's new option list never takes effect and
``RFN-ULM-`` is rejected as not a valid option. Deleting the Property Setter hands the
field back to the DocType JSON, which is the canonical list.

Idempotent: deleting an already-absent row is a no-op.
"""

import frappe

DOCTYPE = "Refining Entry"
FIELDNAME = "naming_series"


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	filters = {
		"doc_type": DOCTYPE,
		"field_name": FIELDNAME,
		"property": ["in", ["options", "default"]],
	}
	stale = frappe.db.count("Property Setter", filters)
	if stale:
		frappe.db.delete("Property Setter", filters)
		# Logged rather than silent: someone pinned these through Customize Form, and the
		# options they chose have just been dropped in favour of the DocType JSON.
		frappe.logger().warning(
			f"fix_refining_naming_series_options: removed {stale} shadowing Property "
			f"Setter(s) on {DOCTYPE}.{FIELDNAME}"
		)

	frappe.clear_cache(doctype=DOCTYPE)
