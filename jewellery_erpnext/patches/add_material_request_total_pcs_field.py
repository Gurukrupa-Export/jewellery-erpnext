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

MIGRATE IS THE ONLY ENTRY POINT ON THIS BRANCH
-----------------------------------------------
A patch does not reach a FRESH install: ``frappe/installer.py:358`` calls
``set_all_patches_as_completed`` before anything else, so every entry in ``patches.txt`` is
logged as done WITHOUT being run. On the uat line that gap is covered by re-calling
``apply()`` from ``install.after_sync`` (``installer.py:371``, the first point at which the
site is whole). This branch ships no ``install.py`` and declares neither ``after_install``
nor ``after_sync``, so there is nothing to hang that on -- and the gap is not specific to
this patch: every entry in ``patches.txt`` is skipped the same way, so a fresh install of
this branch is already incomplete by design and is only ever brought up by ``bench
migrate``.

``apply()`` is kept as a separate entry point anyway, so this file stays textually identical
to its uat counterpart and the two do not drift when the branches are reconciled. If an
``after_install``/``after_sync`` hook is ever added here, wire it to ``apply()``.

Note for whoever debugs this later: clicking **Reset Layout** in Customize Form on Material
Request deletes every ``field_order`` and ``insert_after`` Property Setter for the doctype
(``customize_form.py`` ``reset_layout``), which undoes this entire scheme. Re-run the patch
to restore it. An ordinary Customize Form *save* is safe.

Idempotent: field creation is keyed on ``(dt, fieldname)``, and both helpers below rewrite
to the same values on a re-run. Ad-hoc entry point::

    bench --site <site> execute jewellery_erpnext.patches.add_material_request_total_pcs_field.execute
"""

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
	)
	return "created"


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

	# What a customized site renders by, and what a fresh install renders by.
	rewrite_field_order(DOCTYPE, BLOCK)
	rewrite_insert_after_chain(DOCTYPE, BLOCK, ANCHOR)

	results = {
		fieldname: _ensure_insert_after_property_setter(fieldname, value)
		for fieldname, value in PINNED_ANCHORS
	}

	frappe.clear_cache(doctype=DOCTYPE)
	frappe.logger().info(
		"add_material_request_total_pcs_field: "
		+ ", ".join(f"{k}={v}" for k, v in results.items())
	)


def execute():
	apply()
