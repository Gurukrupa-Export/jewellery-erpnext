"""Convert DRAFT ``is_external=1`` Refining Entries to the new
``refining_type == "External Refinery"`` model.

External refining used to be an ``is_external`` checkbox layered on one of the four
refining types (Dust/Work Order/Serial/Scrap), sending material into one shared
per-company "External Refining" warehouse. It is now its own 5th refining type with a
distinct submit-only lifecycle and a per-supplier warehouse
(see ``RefiningEntry.before_submit_external`` et al.).

Only genuinely UNTOUCHED drafts are converted: ``docstatus=0``, ``status="Draft"``,
AND no ``parent_refining_entry`` / ``refining_entry_po`` set. The latter two guards
matter because the OLD is_external "processing duplicate" mechanism
(``receive_materials``) creates a child Refining Entry that stays at ``docstatus=0``
for its ENTIRE lifecycle (classify/repack/verify/complete/transfer are all driven by
direct method calls on the Draft, never a submit) — such a child can reach status
"Transferred" while still docstatus=0, and can carry real linked Stock Entries /
Purchase Orders. A first version of this patch filtered on ``docstatus=0`` alone and
wrongly converted exactly such a mid-lifecycle child on gk.site (caught in review,
reverted by hand); this narrower filter is required to not repeat that. Submitted or
otherwise-in-progress historical records are left untouched: their lifecycle already
ran (or is running) under the old model, and re-typing a document with existing linked
Stock Entries / Purchase Orders breaks those links. ``is_external`` itself is kept on
the doctype (hidden) — not dropped — so historical records still read correctly.

Idempotent: once converted, refining_type is no longer one of "the old 4 types" so a
re-run matches nothing.
"""

import frappe


def execute():
	draft_names = frappe.get_all(
		"Refining Entry",
		filters={
			"is_external": 1,
			"docstatus": 0,
			"status": "Draft",
			"parent_refining_entry": ["in", ["", None]],
			"refining_entry_po": ["in", ["", None]],
		},
		pluck="name",
	)
	for name in draft_names:
		frappe.db.set_value(
			"Refining Entry",
			name,
			{"refining_type": "External Refinery", "is_external": 0},
			update_modified=False,
		)

	frappe.logger().info(
		f"convert_draft_external_refining_entries: converted {len(draft_names)} draft "
		"is_external Refining Entries to refining_type=External Refinery"
	)
