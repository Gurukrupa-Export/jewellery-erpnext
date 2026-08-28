"""Recompute ``Stock Entry Detail.custom_jwelex_tag_no`` for every existing row.

The field shipped holding the tag of the row's FIRST serial only. It now carries one
tag per serial (see ``widen_stock_entry_jwelex_tag_field``), but the stamper only
runs on save -- and a submitted Stock Entry never re-saves, so rows like
``MAT-STE-07960`` would keep their single tag forever. This backfills them.

Writes with ``frappe.db.set_value(..., update_modified=False)`` rather than
``doc.save()``: the target rows are overwhelmingly submitted (docstatus 1), and this
field feeds no ledger, valuation or reservation logic. A re-save would re-run the
whole Stock Entry validation chain against today's masters for a historical voucher;
a direct field write cannot. ``modified`` is deliberately left alone so this
correction does not look like a business edit.

Runs AFTER ``widen_stock_entry_jwelex_tag_field`` (patches.txt order) -- writing a
multi-line value into the old ``varchar(140)`` column would truncate it.

Resolution rules are NOT duplicated here: ``_row_serials`` / ``_join_tags`` are
imported from the same module the save-time stamper uses, so a backfilled row and a
freshly-saved row are guaranteed to agree.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.backfill_stock_entry_jwelex_tag_no.execute

Idempotent: recomputes the same value and writes only where it differs, so a second
run reports 0 updated.
"""

import frappe

from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.se_utils import (
	_join_tags,
	_row_serials,
)
from jewellery_erpnext.utils import bulk_map

CHUNK = 2000


def execute():
	rows = frappe.db.sql(
		"""
		SELECT name, serial_no, custom_jwelex_tag_no
		FROM `tabStock Entry Detail`
		WHERE serial_no IS NOT NULL AND serial_no != ''
		""",
		as_dict=True,
	)

	updated = 0
	for start in range(0, len(rows), CHUNK):
		chunk = rows[start : start + CHUNK]
		serials_by_row = {row.name: _row_serials(row.serial_no) for row in chunk}
		tag_map = bulk_map(
			"Serial No",
			[s for serials in serials_by_row.values() for s in serials],
			["custom_jwelex_tag_no"],
		)

		for row in chunk:
			value = _join_tags(serials_by_row[row.name], tag_map)
			if value == (row.custom_jwelex_tag_no or None):
				continue

			frappe.db.set_value(
				"Stock Entry Detail",
				row.name,
				"custom_jwelex_tag_no",
				value,
				update_modified=False,
			)
			updated += 1

		frappe.db.commit()

	frappe.logger().info(
		f"backfill_stock_entry_jwelex_tag_no: scanned {len(rows)} rows, updated {updated}"
	)
