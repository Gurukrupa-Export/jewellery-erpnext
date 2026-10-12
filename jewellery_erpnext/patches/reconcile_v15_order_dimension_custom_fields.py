"""Remove the v15 Custom Field rows that shadow the standard order-dimension fields.

Parent Manufacturing Order, Manufacturing Work Order and Serial Number Creator declare
``sales_type`` / ``flow_type`` (and, on the first two, ``design_type``) in their own DocType
JSON, each with a ``fetch_from`` that carries the value down the chain::

    Sales Order -> Parent Manufacturing Order -> Manufacturing Work Order
                                              -> Serial Number Creator

Databases that ran the older v15 chain patches (``add_chain_sales_order_fields`` and
``add_design_type_chain_fields``) also hold Custom Field rows with those same fieldnames, created
while the doctypes did not have the standard fields yet. Once the standard fields arrive, the
meta keeps one field per name and the custom row wins, so the form shows a field with no
``fetch_from`` (and, for Flow Type, a stale Select list) and the value is no longer fetched.

WHAT IT DOES
------------
For each of the 8 (doctype, fieldname) pairs in ``SHADOWED_FIELDS``, and only when the doctype
has a standard DocField of that name, the Custom Field rows for the pair are deleted with
``frappe.db.delete``. Nothing else is touched:

* never ``delete_doc``: ``CustomField.on_trash`` deletes every Property Setter on the fieldname,
  and those belong to the standard field just as much as to the shadow row;
* columns and stored values stay, because the standard and the custom field share one column;
* no data is backfilled.

PROPERTY SETTERS ARE LOGGED, NEVER DELETED
------------------------------------------
Meta applies a field-level Property Setter to the first field of that name, the standard one.
While the shadow row won the layout such a setter had no visible effect; once the shadow row is
gone it starts to apply to the standard field. So every Property Setter on a pair whose shadow
row was removed is listed in the log and in the migrate output, and left in place for review.

Every removed row (name, fieldtype, options, fetch_from) is logged too, and the doctype cache is
cleared once per touched doctype.

Not touched on purpose: every ``order_type`` (no v15 row shadows it) and Serial Number
Creator ``design_type``, which has no standard field to fall back to.

Wired in the same two idempotent places as its siblings: this ``post_model_sync`` patch and the
patch block of ``create_test_data.create_test_data``. On fresh and CI sites it finds nothing; it
is wired there because ``install-app`` marks patches complete WITHOUT running them.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.reconcile_v15_order_dimension_custom_fields.execute

Idempotent: a second run finds no Custom Field row for any pair, deletes nothing and returns [].
"""

import frappe

SHADOWED_FIELDS = {
	"Parent Manufacturing Order": ("sales_type", "flow_type", "design_type"),
	"Manufacturing Work Order": ("sales_type", "flow_type", "design_type"),
	"Serial Number Creator": ("sales_type", "flow_type"),
}

LOG_PREFIX = "reconcile_v15_order_dimension_custom_fields"


def has_standard_field(doctype, fieldname):
	"""True when the doctype's own JSON (synced into DocField) declares the fieldname."""
	return bool(
		frappe.db.exists(
			"DocField",
			{"parent": doctype, "parenttype": "DocType", "fieldname": fieldname},
		)
	)


def shadow_rows(doctype, fieldname):
	return frappe.get_all(
		"Custom Field",
		filters={"dt": doctype, "fieldname": fieldname},
		fields=["name", "dt", "fieldname", "fieldtype", "options", "fetch_from"],
		order_by="name",
	)


def property_setters(doctype, fieldname):
	return frappe.get_all(
		"Property Setter",
		filters={"doc_type": doctype, "field_name": fieldname},
		fields=["name", "doc_type", "field_name", "property", "value"],
		order_by="name",
	)


def reconcile():
	"""Delete the shadowing Custom Field rows. Returns the removed rows.

	Property Setters on the affected pairs are logged and never deleted.
	"""
	removed = []
	kept_setters = []
	touched = []

	for doctype, fieldnames in SHADOWED_FIELDS.items():
		for fieldname in fieldnames:
			if not has_standard_field(doctype, fieldname):
				continue

			rows = shadow_rows(doctype, fieldname)
			if not rows:
				continue

			frappe.db.delete("Custom Field", {"dt": doctype, "fieldname": fieldname})
			removed.extend(rows)
			kept_setters.extend(property_setters(doctype, fieldname))
			if doctype not in touched:
				touched.append(doctype)

	for doctype in touched:
		frappe.clear_cache(doctype=doctype)

	_report(removed, kept_setters)
	return removed


def _report(removed, kept_setters):
	logger = frappe.logger("jewellery_erpnext")
	if not removed:
		logger.info(f"{LOG_PREFIX}: no shadowing Custom Field rows found")
		return

	for row in removed:
		logger.info(
			f"{LOG_PREFIX}: removed Custom Field {row.name} "
			f"(fieldtype={row.fieldtype}, options={row.options!r}, fetch_from={row.fetch_from!r})"
		)
	for setter in kept_setters:
		logger.warning(
			f"{LOG_PREFIX}: kept Property Setter {setter.name} "
			f"({setter.property}={setter.value!r}); it now applies to the standard field "
			f"{setter.doc_type}.{setter.field_name}"
		)

	summary = (
		f"{LOG_PREFIX}: removed {len(removed)} shadowing Custom Field row(s): "
		+ ", ".join(row.name for row in removed)
	)
	if kept_setters:
		summary += (
			f". Kept {len(kept_setters)} Property Setter(s) for review, now applying to the "
			"standard fields: " + ", ".join(setter.name for setter in kept_setters)
		)
	print(summary)


def execute():
	reconcile()
	frappe.db.commit()
