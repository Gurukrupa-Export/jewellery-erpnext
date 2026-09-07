"""Widen ``Stock Entry Detail.custom_jwelex_tag_no`` from ``Data`` to ``Text``.

The field shipped as ``Data`` and held the tag of the row's FIRST serial only. A
Stock Entry row carries one serial per qty -- ``MAT-STE-07960`` row 1 has four --
so the field now mirrors ``serial_no``: same fieldtype (``Text``), one tag per line,
aligned line-for-line. A ``Data`` control is a single-line input and cannot display
a newline-separated list at all, and ``varchar(140)`` would truncate at ~14 tags.

Why a second patch rather than editing ``add_stock_entry_jwelex_tag_field``: that
patch is already recorded in ``tabPatch Log`` on every site that has the field, so
editing it in place would never re-apply. This module exists purely to earn a fresh
Patch Log entry; the field definition stays single-sourced in the original patch.

``create_custom_fields(update=True)`` (the default) saves the changed Custom Field
doc and then calls ``frappe.db.updatedb``, altering the column ``varchar(140)`` ->
``text``. Existing values are preserved by that ALTER; ``backfill_stock_entry_jwelex_tag_no``
then recomputes them. Run this BEFORE that backfill, or multi-line values would be
truncated on the way in. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.widen_stock_entry_jwelex_tag_field.execute

Idempotent: ``create_custom_fields`` writes only when the definition differs.
"""

import frappe


def execute():
	from jewellery_erpnext.patches.add_stock_entry_jwelex_tag_field import (
		execute as _ensure_stock_entry_jwelex_tag_field,
	)

	_ensure_stock_entry_jwelex_tag_field()
	frappe.logger().info(
		"widen_stock_entry_jwelex_tag_field: Stock Entry Detail.custom_jwelex_tag_no is Text"
	)
