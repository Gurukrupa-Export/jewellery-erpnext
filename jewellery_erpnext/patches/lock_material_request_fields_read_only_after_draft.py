"""Field-level System-Manager-only edit restriction for Material Request, past Draft.

Replaces the earlier whole-form Workflow.allow_edit lock (see
``lock_material_request_edit_to_system_manager``), which turned out to be the wrong tool:
it is document-wide (frappe.workflow.is_read_only, applied to the whole form in form.js),
with no way to carve out an exception for specific fields. That blocked the exact fields a
non-System-Manager operator legitimately needs to fill in to complete a workflow action
themselves -- custom_manufacturing_operation for "Transfer to MOP", the destination
department/warehouse for "Transfer to Department" -- since the whole form, including them,
went read-only for anyone without System Manager once out of Draft.

This patch creates Read Only Depends On Property Setters on exactly the fields the
server-side ``guard_non_system_manager_field_edits`` (doc_events.material_request) already
protects, and no others: Material Request's ``company``/``material_request_type``, and
Material Request Item's ``item_code``/``qty``/``pcs``. custom_manufacturing_operation and
the department-transfer fields are deliberately untouched, so they stay editable by every
role at every state, exactly as before any of this existed.

``doc.`` is used for the two Material Request (parent-level) fields; ``parent.`` for the
three Material Request Item (child-row) fields, since a child row's own ``doc`` in the
depends_on eval context is the row itself, not the Material Request.

Idempotent: skips a field whose Property Setter already carries the exact expression (as it
will on any site where this was first applied by hand through Customize Form, as it was
here), and only writes the ones still missing or different.
"""

import frappe

DOCTYPE = "Material Request"
CHILD_DOCTYPE = "Material Request Item"
PROPERTY = "read_only_depends_on"

MR_EXPRESSION = (
	'eval:doc.workflow_state && doc.workflow_state != "Draft" '
	'&& !in_list(frappe.user_roles, "System Manager")'
)
MRI_EXPRESSION = (
	'eval:parent.workflow_state && parent.workflow_state != "Draft" '
	'&& !in_list(frappe.user_roles, "System Manager")'
)

FIELDS = (
	(DOCTYPE, "company", MR_EXPRESSION),
	(DOCTYPE, "material_request_type", MR_EXPRESSION),
	(CHILD_DOCTYPE, "item_code", MRI_EXPRESSION),
	(CHILD_DOCTYPE, "qty", MRI_EXPRESSION),
	(CHILD_DOCTYPE, "pcs", MRI_EXPRESSION),
)


def _ensure_read_only_depends_on(doctype, fieldname, expression):
	existing_name = frappe.db.exists(
		"Property Setter",
		{"doc_type": doctype, "field_name": fieldname, "property": PROPERTY},
	)

	if existing_name:
		if frappe.db.get_value("Property Setter", existing_name, "value") != expression:
			frappe.db.set_value("Property Setter", existing_name, "value", expression)
			return "updated"
		return "already correct"

	frappe.make_property_setter(
		{
			"doctype": doctype,
			"fieldname": fieldname,
			"property": PROPERTY,
			"value": expression,
			"property_type": "Data",
		},
		is_system_generated=False,
	)
	return "created"


def execute():
	results = {
		f"{doctype}.{fieldname}": _ensure_read_only_depends_on(
			doctype, fieldname, expression
		)
		for doctype, fieldname, expression in FIELDS
	}

	frappe.db.commit()
	frappe.logger().info(
		"lock_material_request_fields_read_only_after_draft: "
		+ ", ".join(f"{k}={v}" for k, v in results.items())
	)
