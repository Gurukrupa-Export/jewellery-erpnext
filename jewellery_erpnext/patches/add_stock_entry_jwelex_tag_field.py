"""Add ``Stock Entry Detail.custom_jwelex_tag_no`` -- the Jwelex tags of the row's
Serial Nos, mirrored onto the Stock Entry row so a serialised entry can be reconciled
against the legacy Jwelex system without opening each Serial No by hand.

A row carries one serial per qty, so this field is ``Text`` -- the same fieldtype
``serial_no`` itself uses -- and holds one tag per line, aligned line-for-line with
``serial_no``. It was originally ``Data`` (first serial only); see
``widen_stock_entry_jwelex_tag_field`` for the migration of sites that ran that
version.

``Serial No.custom_jwelex_tag_no`` already exists (Data, written by
``gke_price_list/doctype/product_return_order``). Note the spelling: ``jwelex`` here,
NOT the ``jewelex`` used by ``Parent Manufacturing Order.custom_jewelex_batch_no``.
Both spellings are live columns -- do not "correct" either one.

Why this is not a ``fetch_from``: Frappe resolves ``fetch_from`` only through Link /
Dynamic Link fields. ``Product Details`` gets the value declaratively because its own
``serial_no`` is a Link, but ``Stock Entry Detail.serial_no`` is a newline-separated
**Text** field, so a ``fetch_from`` here would never fire. The value is therefore
stamped in code, exactly as ``custom_gross_wt`` -> ``gross_weight`` already is
(``customization/stock_entry/doc_events/se_utils.py::set_gross_wt`` plus the client
handler in ``public/js/doctype_js/stock_entry.js``).

Why a patch and not ``custom_fields/stock_entry_detail.json``: this app's
``after_migrate`` hook is disabled (hooks.py), so its ``custom_fields/*.json`` are
never applied by ``bench migrate`` -- the recurring patch-only custom-field gap
documented in ``add_conversion_lane_tag_field`` / ``add_stock_entry_edit_bom_field``.
Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_stock_entry_jwelex_tag_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FIELD = {
	"fieldname": "custom_jwelex_tag_no",
	"fieldtype": "Text",
	"label": "Jwelex Tag No",
	"insert_after": "serial_no",
	"read_only": 1,
	"no_copy": 1,
	"translatable": 0,
	"module": "Jewellery Erpnext",
	"description": (
		"Jwelex tags of this row's Serial Nos, one per line, aligned with "
		"serial_no. Stamped from Serial No.custom_jwelex_tag_no on every save."
	),
}


def execute():
	create_custom_fields({"Stock Entry Detail": [FIELD]}, ignore_validate=True)
	frappe.logger().info(
		"add_stock_entry_jwelex_tag_field: ensured Stock Entry Detail.custom_jwelex_tag_no"
	)
