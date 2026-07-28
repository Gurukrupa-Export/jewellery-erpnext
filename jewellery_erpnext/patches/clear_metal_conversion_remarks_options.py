"""Drop any Property Setter that puts ``options`` back on ``Metal Conversions.remarks``.

``remarks`` is a Select whose option TEXT carries the document's ``percentage`` — e.g.
"NR 1.800% PLAIN ROUND BALLS LOSS BOOK". A Select's option list is SCHEMA: frappe stores
it once on the DocType and it is identical for every document, so a per-document number
cannot live in it. The option space is also unbounded (any percentage the operator types),
so the values can never be pre-registered.

The dropdown is therefore rendered per document by the client from
``MetalConversions.get_remark_options``, and the DocType JSON deliberately ships the field
with NO ``options`` key. That is load-bearing, not an oversight — it is what makes frappe
skip its own Select check::

    # frappe/model/base_document.py, _validate_selects
    if df.fieldname == "naming_series" or not self.get(df.fieldname) or not df.options:
        continue                                                              # <- the exit
    ...
    if value not in options and not (frappe.in_test and value.startswith("_T-")):
        frappe.throw(_('{0} {1} cannot be "{2}". It should be one of "{3}"')...)

``MetalConversions.set_remarks`` is the replacement guard: it rejects a remark that matches
no template and re-renders the rest at the current percentage.

WHY THIS PATCH EXISTS
---------------------
``frappe.get_meta`` layers Property Setters OVER the DocType JSON, so a Property Setter on
(Metal Conversions, remarks, options) beats the JSON and switches the framework check back
on. The field previously shipped a placeholder ``options`` of "\\nremark"; anyone who
opened Customize Form on that field — before or after this change — leaves such a Property
Setter behind, and every save then dies with::

    Remarks cannot be "NR 1.800% PLAIN ROUND BALLS LOSS BOOK". It should be one of "remark"

which points at the value rather than the cause and is easy to lose hours to. This patch
removes that Property Setter if it is there.

Deleting is the correct action, not rewriting: there is no correct static option list for
this field. It is also why the fix cannot be "widen the options" — no finite list covers an
arbitrary percentage.

Idempotent and safe to re-run; a no-op on a site that never had the Property Setter. Re-run
ad-hoc after anyone customizes the field::

    bench --site <site> execute jewellery_erpnext.patches.clear_metal_conversion_remarks_options.execute
"""

import frappe


def execute():
	stale = frappe.get_all(
		"Property Setter",
		filters={
			"doc_type": "Metal Conversions",
			"field_name": "remarks",
			"property": "options",
		},
		pluck="name",
	)

	for name in stale:
		frappe.delete_doc("Property Setter", name, ignore_permissions=True, force=True)

	if stale:
		frappe.clear_cache(doctype="Metal Conversions")

	frappe.logger().info(
		f"clear_metal_conversion_remarks_options: removed {len(stale)} remarks options Property Setter(s)"
	)
