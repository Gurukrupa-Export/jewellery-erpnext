"""Keep the Transfer to Department destination fields on show after the transfer.

``custom_destination_department`` and ``custom_destination_warehouse`` shipped with their
visibility keyed on the route alone::

    depends_on: eval:doc.custom_operation_type == "Transfer to Department"

``custom_operation_type`` is ``allow_on_submit``, so an operator can switch it back to
"Transfer to MOP" after the transfer has already happened -- and both fields then vanish,
taking the record of where the material went with them. This widens the condition so any
value present keeps them visible, and locks them once the Stock Entry exists.

Three edits, all to configuration:

* ``depends_on`` on both fields -> ``DISPLAY_ROUTE``: the route OR either value present.
* ``read_only_depends_on`` on both fields -> ``TRANSFER_DONE``: set once
  ``custom_department_transfer_se`` is stamped. Editing them afterwards would let the
  request contradict the Stock Entry it already produced, and
  ``make_department_transfer_stock_entry``'s idempotency guard means no corrected entry is
  ever made. Mirrors ``custom_manufacturing_operation`` locking in the MOP state.
* The ``Material Transferred to Department -> Cancel`` transition loses its
  ``custom_operation_type`` condition. With the flip back to "Transfer to MOP" now an
  explicitly supported action, gating Cancel on the department route would leave a document
  in that state with no available transition at all.

``mandatory_depends_on`` is untouched: still the route alone. A completed transfer leaves
both fields read-only with values present, so the rule is satisfied either way.

Why a separate patch rather than editing the two originals: frappe records patches by module
path, so ``add_mr_department_transfer_fields`` and
``add_mr_transfer_to_department_workflow`` will never run again on a site that has them.
Their constants ARE updated in step, so a fresh site gets the right values directly and this
patch is a no-op there -- in particular ``add_mr_transfer_to_department_workflow`` is
create-only (it skips a ``(state, action)`` pair that already exists), which is exactly why
the Cancel rewrite has to happen here for an existing site.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.update_mr_department_transfer_visibility.execute

Idempotent: every write is an assignment of a fixed value, guarded on the target existing.
"""

import frappe

from jewellery_erpnext.patches.add_mr_department_transfer_fields import (
	DISPLAY_ROUTE,
	TRANSFER_DONE,
)
from jewellery_erpnext.patches.add_mr_transfer_to_department_workflow import (
	CANCEL_ACTION,
	CANCEL_CONDITION,
	NEW_STATE,
	WORKFLOW,
)

FIELDS = (
	"Material Request-custom_destination_department",
	"Material Request-custom_destination_warehouse",
)

PROPERTIES = {
	"depends_on": DISPLAY_ROUTE,
	"read_only_depends_on": TRANSFER_DONE,
}


def _update_fields():
	updated = []
	for name in FIELDS:
		if not frappe.db.exists("Custom Field", name):
			# add_mr_department_transfer_fields has not run yet; it now creates the field
			# with these values already set, so there is nothing to correct.
			continue
		# db.set_value rather than a doc save: Custom Field.on_update re-runs insert_after
		# resolution for the whole DocType, a far larger blast radius than two strings.
		frappe.db.set_value("Custom Field", name, PROPERTIES, update_modified=False)
		updated.append(name)

	if updated:
		frappe.clear_cache(doctype="Material Request")

	return updated


def _update_cancel_transition():
	if not frappe.db.exists("Workflow", WORKFLOW):
		return False

	doc = frappe.get_doc("Workflow", WORKFLOW)
	changed = False

	for row in doc.transitions:
		if (
			row.state == NEW_STATE
			and row.action == CANCEL_ACTION
			and row.condition != CANCEL_CONDITION
		):
			row.condition = CANCEL_CONDITION
			changed = True

	if changed:
		doc.save(ignore_permissions=True)

	return changed


def execute():
	updated = _update_fields()
	cancel_relaxed = _update_cancel_transition()

	frappe.db.commit()
	frappe.logger().info(
		"update_mr_department_transfer_visibility: "
		f"updated {len(updated)} field(s): {', '.join(updated) or 'none'}; "
		f"cancel transition {'relaxed' if cancel_relaxed else 'already correct or absent'}"
	)
