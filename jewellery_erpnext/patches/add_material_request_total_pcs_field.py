"""Add Total Pcs beside Total Quantity on the Material Request header.

Material Request already carries ``custom_total_quantity`` ("Total Quantity"), a
server-computed sum of ``qty`` over the item rows, shown directly under the items grid.
The grid also carries a ``pcs`` column, but nothing totals it, so the piece count of a
request could only be read by adding the grid up by eye. This adds ``custom_total_pcs``
("Total Pcs") next to it, filled by ``update_pure_qty`` on every save.

``Int`` rather than the ``Data`` that ``Material Request Item.pcs`` uses. The per-row
field is Data for historical reasons -- every consumer casts on the way in (see
``customization/utils/material_weights``) -- and a header total has no reason to inherit
that. Live data confirms nothing is lost: of 19,680 non-blank values, none is non-numeric
and none carries a decimal point.

``read_only`` unlike ``custom_total_quantity``, which is editable and so lets a user type a
number that the next save silently overwrites. Mirrors
``add_stock_entry_material_total_pcs_fields``.

WHY A SECTION BREAK AND NOT JUST A COLUMN BREAK
-----------------------------------------------
``items`` and ``custom_total_quantity`` sit in the SAME section (``items_section``), and
``Column.resize_all_columns`` (frappe/public/js/frappe/form/column.js) divides a section's
width equally between its columns with no special case for ``Table`` fields. A bare column
break after ``custom_total_quantity`` would therefore have rendered the items grid at half
width. The two totals get their own section instead, leaving the grid alone::

    items_section   -> items                       (full width)
    custom_total_section -> custom_total_quantity  | custom_total_pcs

WHY THIS TOUCHES THE ``field_order`` PROPERTY SETTER
----------------------------------------------------
Material Request carries a Customize Form ``field_order`` Property Setter, which OUTRANKS
``insert_after`` -- rewriting ``Custom Field.insert_after`` alone changes nothing on screen
for any field named in it, and both ``custom_total_quantity`` and ``custom_order_details``
are. See ``field_order_utils`` for the full explanation; this patch writes both, so a
customized site and a fresh install land on the same layout.

WHY THE TWO ``insert_after`` PROPERTY SETTERS
----------------------------------------------
``custom_total_quantity`` and ``custom_order_details`` are Custom Field rows owned by
``gke_customization``'s ``fixtures/custom_field.json``, which ``sync_fixtures()`` re-imports
with ``force=True`` on EVERY migrate -- and migrate runs patches (``run_schema_updates``)
before fixtures (``post_schema_updates``), so the re-anchoring done below would be reverted
within the same command. A Property Setter survives it: ``Meta.process`` applies property
setters after loading custom fields and before ``sort_fields``, so it wins. On a site that
has the ``field_order`` Property Setter this is belt-and-braces; on one without it, it is
what keeps the layout correct across a migrate.

``custom_order_details`` also has to move for a second reason: it currently anchors on
``custom_total_quantity``, and several custom fields sharing one anchor are ordered by
per-site Custom Field ``idx``, so the new fields would sort differently on different
installs. Chaining it onto ``custom_total_pcs`` removes the tie.

WHY ``apply()`` IS ALSO CALLED FROM ``install.after_sync``
----------------------------------------------------------
A patch alone does not reach a FRESH install: ``frappe/installer.py:358`` calls
``set_all_patches_as_completed`` before anything else, so every entry in ``patches.txt`` is
logged as done WITHOUT being run. ``after_install`` (``:360``) is too early for the same
reason the rest of this app's provisioning avoids it -- ``sync_fixtures`` runs afterwards at
``:367`` and would delete+re-insert the two fixture-owned rows straight back to their old
anchors. ``after_sync`` (``:371``) is the first point at which the site is whole, so the
layout is re-applied from there. Both entry points call the same idempotent ``apply()``.

Note for whoever debugs this later: clicking **Reset Layout** in Customize Form on Material
Request deletes every ``field_order`` and ``insert_after`` Property Setter for the doctype
(``customize_form.py`` ``reset_layout``), which undoes this entire scheme. Re-run the patch
to restore it. An ordinary Customize Form *save* is safe.

Idempotent: field creation is keyed on ``(dt, fieldname)``, and both helpers below rewrite
to the same values on a re-run. Ad-hoc entry point::

    bench --site <site> execute jewellery_erpnext.patches.add_material_request_total_pcs_field.execute
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.patches.field_order_utils import (
	rewrite_field_order,
	rewrite_insert_after_chain,
)

DOCTYPE = "Material Request"

SECTION = "custom_total_section"
COLUMN_BREAK = "custom_column_break_total_pcs"
TOTAL_PCS = "custom_total_pcs"

# The field the block is built around, and the standard field the chain hangs off.
TOTAL_QTY = "custom_total_quantity"
ANCHOR = "items"

# The Order Details tab break, which currently anchors on TOTAL_QTY and has to end up after
# the new fields rather than tie-breaking against them.
ORDER_DETAILS = "custom_order_details"

# Reading order: section, left column, break, right column, then whatever followed.
BLOCK = [SECTION, TOTAL_QTY, COLUMN_BREAK, TOTAL_PCS, ORDER_DETAILS]

# Fields this patch creates. Every one is anchored on TOTAL_QTY here purely so
# create_custom_fields' insert_after validation passes -- the real chain is written by
# rewrite_insert_after_chain below.
NEW_FIELDS = (
	{
		"fieldname": SECTION,
		"fieldtype": "Section Break",
		"insert_after": TOTAL_QTY,
		"module": "Jewellery Erpnext",
	},
	{
		"fieldname": COLUMN_BREAK,
		"fieldtype": "Column Break",
		"insert_after": TOTAL_QTY,
		"module": "Jewellery Erpnext",
	},
	{
		"fieldname": TOTAL_PCS,
		"fieldtype": "Int",
		"label": "Total Pcs",
		"insert_after": TOTAL_QTY,
		# Server-derived on every save by update_pure_qty; a typed value would be
		# overwritten at the next validate anyway.
		"read_only": 1,
		"module": "Jewellery Erpnext",
		# Explicitly blank, not omitted: create_custom_fields updates a pre-existing
		# field with ``custom_field.update(df)``, which only touches the keys present.
		# Omitting it would strand any description a future revision had shipped.
		"description": "",
	},
)

# The two fixture-owned fields whose anchors have to survive sync_fixtures.
PINNED_ANCHORS = (
	(TOTAL_QTY, SECTION),
	(ORDER_DETAILS, TOTAL_PCS),
)


def _ensure_insert_after_property_setter(fieldname, value):
	"""Pin ``fieldname``'s ``insert_after`` so a fixture re-import cannot move it back."""
	existing_name = frappe.db.exists(
		"Property Setter",
		{"doc_type": DOCTYPE, "field_name": fieldname, "property": "insert_after"},
	)

	if existing_name:
		if frappe.db.get_value("Property Setter", existing_name, "value") != value:
			frappe.db.set_value("Property Setter", existing_name, "value", value)
			return "updated"
		return "already correct"

	frappe.make_property_setter(
		{
			"doctype": DOCTYPE,
			"fieldname": fieldname,
			"property": "insert_after",
			"value": value,
			"property_type": "Data",
		},
		is_system_generated=False,
		# This runs from after_sync/after_migrate, when a sibling app's fields may not exist
		# yet. The default True re-validates every field on the doctype and can throw for
		# reasons that have nothing to do with this Property Setter.
		validate_fields_for_doctype=False,
	)
	return "created"


def _ensure_field_order_property_setter():
	"""Guarantee a DocType-level ``field_order`` Property Setter exists, seeding one if not.

	Without it, ``Meta.sort_fields`` resolves the layout from ``insert_after`` -- and
	``meta.py``'s Section/Column Break special case then RELOCATES ``custom_total_section``.
	It walks forward from the anchor until it meets a Section Break or a field matching the
	ANCHOR's own fieldtype (``Table``), which on a stock Material Request means it sails past
	``terms_tab`` and drops the whole block into the Terms tab. Measured, not theorised: a
	site with no such Property Setter resolves to
	``items, terms_tab, custom_total_section, custom_total_quantity, ...``.

	That special case only fires when the anchor is in ``field_order`` -- i.e. when it is a
	standard field, which ``items`` is. There is no way to anchor a Section Break directly
	after the items grid and avoid it, so the fix is to make ``field_order`` authoritative:
	rule 1 of ``sort_fields`` outranks ``insert_after`` entirely.

	Seeded from the CURRENT resolved order, relocation and all. That is safe because
	``rewrite_field_order`` then splices the block in at the position of its earliest member
	-- ``custom_total_quantity``, which is still sitting correctly after ``items`` -- so the
	displaced fields are pulled back up with it.

	Only ever creates; a site that already has one (anything customised through Customize
	Form, which includes production) is left completely alone.
	"""
	if frappe.db.exists(
		"Property Setter",
		{"doc_type": DOCTYPE, "property": "field_order", "doctype_or_field": "DocType"},
	):
		return "already present"

	frappe.clear_cache(doctype=DOCTYPE)
	order = [df.fieldname for df in frappe.get_meta(DOCTYPE).fields]

	frappe.make_property_setter(
		{
			"doctype": DOCTYPE,
			"doctype_or_field": "DocType",
			"property": "field_order",
			"value": json.dumps(order),
			"property_type": "Data",
		},
		is_system_generated=False,
		# See the sibling helper: validating every field on a half-built site can throw,
		# and this helper's caller swallows exceptions, so that would fail silently.
		validate_fields_for_doctype=False,
	)
	return "seeded"


def apply():
	"""Create the fields and put the block where it belongs. Safe to run repeatedly.

	Shared by ``execute`` (migrate / ad-hoc) and ``install.after_sync`` (fresh install).
	"""
	create_custom_fields({DOCTYPE: list(NEW_FIELDS)}, ignore_validate=True)

	# A stale insert_after Property Setter on one of the NEW fields would override the
	# chain written below. The two fields in PINNED_ANCHORS are the deliberate exception.
	for fieldname in (SECTION, COLUMN_BREAK, TOTAL_PCS):
		frappe.db.delete(
			"Property Setter",
			{
				"doc_type": DOCTYPE,
				"field_name": fieldname,
				"property": "insert_after",
			},
		)

	# Must come BEFORE rewrite_field_order, which is a no-op when there is nothing to
	# splice into -- and before the chain, so the seed reflects the pre-chain order.
	seeded = _ensure_field_order_property_setter()

	# What a customized site renders by, and what a fresh install renders by.
	rewrite_field_order(DOCTYPE, BLOCK)
	rewrite_insert_after_chain(DOCTYPE, BLOCK, ANCHOR)

	results = {
		fieldname: _ensure_insert_after_property_setter(fieldname, value)
		for fieldname, value in PINNED_ANCHORS
	}
	results["field_order"] = seeded

	frappe.clear_cache(doctype=DOCTYPE)
	frappe.logger().info(
		"add_material_request_total_pcs_field: "
		+ ", ".join(f"{k}={v}" for k, v in results.items())
	)


def after_migrate():
	"""Re-assert the layout at the very end of every migrate. Wired from ``hooks.py``.

	``after_sync`` alone is not enough, because it fires during THIS app's install and the
	install order works against it: ``install.sh`` runs ``install-app jewellery_erpnext``
	BEFORE ``install-app gke_customization``, so the layout is settled first and
	``gke_customization``'s fixture import then delete+re-inserts
	``Material Request-custom_total_quantity`` and ``-custom_order_details`` back to their
	old anchors. Nothing re-asserted after that, and the final ``bench migrate``
	re-imported the same fixture again.

	``after_migrate`` is the last word: ``migrate.py`` runs it at the end of
	``post_schema_updates``, after ``sync_fixtures()``, so it is the only hook guaranteed to
	see the finished site whatever order the apps were installed in.

	Deliberately non-fatal: a form-layout problem must never be the thing that fails a
	migrate. Note that this makes failures silent, which is why the two Property Setter
	helpers above pass ``validate_fields_for_doctype=False`` rather than risk throwing.
	"""
	try:
		apply()
	except Exception:
		frappe.log_error(
			title="after_migrate: Material Request Total Pcs layout",
			message=frappe.get_traceback(),
		)


def execute():
	apply()
