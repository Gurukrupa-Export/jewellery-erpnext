"""Provision the Serial No *sales* pointer — ``custom_reference_doctype`` /
``custom_reference_docname`` — and relabel the two CORE reference fields.

WHY A SECOND PAIR
-----------------
``Serial No.reference_doctype`` / ``reference_name`` are CORE ERPNext fields and they
already have an owner and a meaning: **the voucher that CREATED the serial**. ERPNext
writes them in ``SerialandBatchBundle.set_source_document_no`` (only ``where reference_name
is null``) and clears them in ``remove_source_document_no``
(erpnext/stock/doctype/serial_and_batch_bundle/serial_and_batch_bundle.py). On this site
they hold a Stock Entry on ~19.2k of ~19.7k serials. Two readers in this app depend on that
meaning — ``_get_company_context`` in doc_events/sales_order.py resolves the Manufacture
Stock Entry from it, and ``SerialNumberCreator.get_serial_summary`` joins on it — and
``test_serial_number_creator`` asserts ``sn.reference_name == se.name``. Repurposing them
would be silently overwritten by core on the next bundle submit.

So the sales-lifecycle pointer gets its OWN pair, and the core pair is only RELABELLED
("Source Document Type/Name" -> "Reference Doctype/Docname") so the form stops reading like
the sales pointer.

The new pair is written by ``doc_events/serial_reference.py`` on Sales Order / Delivery
Note / Sales Invoice ``validate``, and cleared on ``on_cancel`` / ``on_trash``.

FORM PLACEMENT
--------------
The two fields sit directly under the standard ``brand`` field, in the same column, with no
section heading of their own — they fill the empty space at the foot of that column. An
earlier revision of this patch put them in a dedicated "Sales Reference" section further
down the form; ``execute`` removes those two layout-only fields if they are still present,
so a site that ran the earlier revision converges on the same layout.

Both data fields are ``read_only`` (a stamp, never user input) and ``no_copy`` (an amended
serial must not inherit somebody else's sales document). ``ignore_user_permissions`` is
load-bearing, not cosmetic: ``Document._validate_links`` runs on every later
``Serial No.save()`` — and this app has a Serial No ``validate`` hook — so without it a user
carrying a Sales Order User Permission could not save an unrelated Serial No. Core sets the
same flag on ``reference_doctype``.

Neither field carries a ``description``: the labels say what they are, and the form was
getting noisy. The rationale lives here instead.

Serial No has been through Customize Form, so it carries a ``field_order`` Property Setter
which OUTRANKS ``insert_after`` — see ``field_order_utils`` for why both must be written.

Because ``after_migrate`` is disabled and ``install-app`` marks patches complete WITHOUT
running them on fresh / CI sites, a fixture-only column would never reach the DB and the
``frappe.qb.update`` in serial_reference.py would raise ``1054 Unknown column`` on every
Sales Order save. Per the app convention this is wired in two idempotent places: this
``post_model_sync`` patch and ``create_test_data.setup_data``. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_serial_no_sales_reference_fields.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``; the Property Setter and
the ``insert_after`` chain are recomputed to the same values on every run.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.patches.field_order_utils import (
	drop_layout_fields,
	rewrite_field_order,
	rewrite_insert_after_chain,
	strip_field_order_entries,
)

# The pointer sits directly under this STANDARD field, so the layout does not depend on any
# of the twenty gke_customization Serial No fields being installed.
ANCHOR_FIELD = "brand"

POINTER_FIELDS = ("custom_reference_doctype", "custom_reference_docname")

# Layout-only fields from the first revision of this patch, when the pointer lived in its
# own section. Removed so a site that ran that revision converges on the current layout.
SUPERSEDED_LAYOUT_FIELDS = (
	"custom_sales_reference_section",
	"custom_sales_reference_column",
)

# Relabel of the two CORE fields. They keep their meaning and their writers; only the
# label changes, so "Source Document Type/Name" no longer reads like the sales pointer.
_CORE_LABELS = {
	"reference_doctype": "Reference Doctype",
	"reference_name": "Reference Docname",
}

# Fields whose description is cleared: the labels are self-explanatory and the descriptions
# were crowding the form. ``create_custom_fields`` only writes the keys it is given, so the
# empty string below is what actually clears an existing value.
_DESCRIPTIONS_TO_CLEAR = ("custom_ownership_tag",)


def ensure_serial_no_sales_reference_fields():
	"""Create / refresh the two Serial No sales-pointer fields, directly under Brand."""
	custom_fields = {
		"Serial No": [
			{
				"fieldname": "custom_reference_doctype",
				"fieldtype": "Link",
				"label": "Sales Reference Doctype",
				"options": "DocType",
				"insert_after": ANCHOR_FIELD,
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"ignore_user_permissions": 1,
				"in_standard_filter": 1,
				"description": "",
			},
			{
				"fieldname": "custom_reference_docname",
				"fieldtype": "Dynamic Link",
				"label": "Sales Reference Docname",
				"options": "custom_reference_doctype",
				"insert_after": "custom_reference_doctype",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"ignore_user_permissions": 1,
				"in_standard_filter": 1,
				"search_index": 1,
				"description": "",
			},
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_serial_no_sales_reference_fields: ensured Serial No.custom_reference_doctype "
		"/ custom_reference_docname"
	)


def place_pointer_under_brand():
	"""Move the pointer fields directly below ``brand``, and retire the old section.

	Writes the ``field_order`` Property Setter (what a Customize Form'd site renders by) AND
	the ``insert_after`` chain (what a fresh install renders by).
	"""
	# Retire the dedicated section from the first revision before repositioning, so its
	# fieldnames cannot linger in field_order as an empty section.
	dropped = drop_layout_fields("Serial No", SUPERSEDED_LAYOUT_FIELDS)
	strip_field_order_entries("Serial No", SUPERSEDED_LAYOUT_FIELDS)

	block = [ANCHOR_FIELD, *POINTER_FIELDS]
	had_property_setter = rewrite_field_order("Serial No", block)
	rewrite_insert_after_chain("Serial No", list(POINTER_FIELDS), ANCHOR_FIELD)

	frappe.clear_cache(doctype="Serial No")
	frappe.logger().info(
		"add_serial_no_sales_reference_fields: placed pointer under "
		f"{ANCHOR_FIELD} (field_order property setter="
		f"{'updated' if had_property_setter else 'absent'}, "
		f"retired layout fields={dropped})"
	)


def clear_noisy_descriptions():
	"""Blank the descriptions the form no longer needs (see module docstring)."""
	cleared = []
	for fieldname in _DESCRIPTIONS_TO_CLEAR:
		field = frappe.db.get_value(
			"Custom Field",
			{"dt": "Serial No", "fieldname": fieldname},
			["name", "description"],
			as_dict=True,
		)
		if field and field.description:
			frappe.db.set_value("Custom Field", field.name, "description", "")
			cleared.append(fieldname)

	if cleared:
		frappe.clear_cache(doctype="Serial No")
		frappe.logger().info(
			"add_serial_no_sales_reference_fields: cleared descriptions -> "
			+ ", ".join(cleared)
		)
	return cleared


def ensure_serial_no_reference_labels():
	"""Relabel the CORE Serial No reference fields. Label only — no behaviour change.

	Returns the list of ``"Serial No.<fieldname>.label"`` keys asserted.
	"""
	asserted = []

	for fieldname, label in _CORE_LABELS.items():
		field = frappe.get_meta("Serial No").get_field(fieldname)
		if not field:
			# Field missing (should never happen for a core field) — skip, don't throw.
			continue
		if field.label == label:
			continue

		frappe.make_property_setter(
			{
				"doctype_or_field": "DocField",
				"doctype": "Serial No",
				"fieldname": fieldname,
				"property": "label",
				"value": label,
				"property_type": "Data",
			},
			is_system_generated=False,
		)
		asserted.append(f"Serial No.{fieldname}.label")

	if asserted:
		frappe.db.commit()
		# make_property_setter already clears the doctype cache via PropertySetter.validate.
		frappe.logger().info(
			"add_serial_no_sales_reference_fields: relabelled -> "
			+ ", ".join(sorted(asserted))
		)

	return asserted


def execute():
	ensure_serial_no_sales_reference_fields()
	place_pointer_under_brand()
	clear_noisy_descriptions()
	ensure_serial_no_reference_labels()
	frappe.db.commit()
