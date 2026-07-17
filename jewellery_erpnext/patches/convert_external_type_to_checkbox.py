"""Convert ``refining_type == "External Refinery"`` entries to the checkbox model.

External refining was briefly modeled as a distinct 5th ``refining_type``; per the
final design it is the ``is_external`` CHECKBOX on top of the 4 refining types (the
type says WHAT material is refined — Dust/Work Order/Serial/Scrap; the checkbox says
WHERE — internally or at an outside supplier). "External Refinery" is removed from the
``refining_type`` Select options, so any record still carrying it must be retyped or
it would fail Select validation on its next save.

Every such record is mapped to ``Dust Refining + is_external=1`` — external refining
sends loss/dust/semi-finished material, for which Dust is the umbrella type — keeping
name, links (Stock Entries / Purchase Orders key off ``custom_refining_entry`` /
``refining_entry``, not the type) and all other fields untouched. Also covers records
that ``convert_draft_external_refining_entries`` previously moved TO the type model.

Idempotent: once converted, ``refining_type`` is no longer "External Refinery" so a
re-run matches nothing.
"""

import frappe

SERIES_BY_TYPE = {"Dust Refining": "RFN-DST-.YY.-.#####"}


def execute():
	names = frappe.get_all(
		"Refining Entry",
		filters={"refining_type": "External Refinery"},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value(
			"Refining Entry",
			name,
			{
				"refining_type": "Dust Refining",
				"is_external": 1,
				# Keep the stored series consistent with the new type; the already
				# assigned document NAME is immutable and unaffected.
				"naming_series": SERIES_BY_TYPE["Dust Refining"],
			},
			update_modified=False,
		)

	frappe.logger().info(
		f"convert_external_type_to_checkbox: converted {len(names)} External "
		"Refinery-typed entries to Dust Refining + is_external"
	)
