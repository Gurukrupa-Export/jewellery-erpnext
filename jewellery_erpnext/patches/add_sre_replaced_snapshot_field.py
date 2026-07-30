"""Provision ``Stock Reservation Entry.custom_replaced_sre_snapshot``.

The Employee IR Process Loss path frees the loss quantity by cancelling the covering
Stock Reservation Entry and recreating it with a reduced ``reserved_qty``
(``loss_stock_entry._reduce_sre``). The replacement stores what it took away -- the
pre-loss remaining qty, the delivered qty, and the affected batch row -- as JSON in this
field, so ``_restore_reduced_sres`` can put the reservation back exactly when the
Employee IR is cancelled.

The field was referenced by that code but declared **nowhere**: not in a patch, not in
``patches.txt``, not in ``jewellery_erpnext/custom/stock_reservation_entry.json``, not in
``custom_fields/*.json``, not in a fixture. It only ever existed on the ``gk`` dev site,
hand-created in the UI on 2026-05-27. Every other site therefore raised
``1054 Unknown column 'custom_replaced_sre_snapshot'`` on the SELECT in
``_restore_reduced_sres``, which blocked **all** Employee IR cancels.

The write side failed more quietly and is the reason this went unnoticed for so long:
``new_sre.custom_replaced_sre_snapshot = ...`` is a plain attribute assignment, and
``Document.get_valid_dict()`` filters to meta fieldnames, so on a site without the column
``insert()`` silently dropped it -- no error, no snapshot, no way to restore.

Properties mirror the definition that already exists on ``gk`` so the sites converge:
``Long Text`` (the JSON is unbounded -- a multi-batch reservation snapshot easily exceeds
``Data``'s 140 chars), ``read_only`` + ``hidden`` because it is machine-owned bookkeeping
that must never be hand-edited, and ``no_copy`` so an amended or duplicated reservation
does not inherit a snapshot describing a different Employee IR.

``insert_after`` deliberately anchors on the STANDARD ``voucher_qty`` rather than on
``original_reserved_qty`` or the ``gk``-only ``custom_replaced_sre``: ``post_model_sync``
patches run at ``frappe/migrate.py:144``, *before* ``sync_customizations()`` at ``:180``,
so the fields declared in ``custom/stock_reservation_entry.json`` do not exist yet the
first time this patch runs on a fresh site.

Because ``after_migrate`` is disabled and ``install-app`` marks patches complete WITHOUT
running them on fresh / CI sites, this is wired in two idempotent places per the app
convention: this ``post_model_sync`` patch and ``create_test_data``. Can also be run
ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_sre_replaced_snapshot_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``, so it is a no-op on
``gk`` where the field already exists.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Stock Reservation Entry": [
			{
				"fieldname": "custom_replaced_sre_snapshot",
				"fieldtype": "Long Text",
				"label": "Replaced SRE Snapshot",
				"insert_after": "voucher_qty",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"hidden": 1,
				"no_copy": 1,
				"description": (
					"JSON written by the Employee IR Process Loss path when this reservation "
					"replaced a larger one, recording what was taken away so Employee IR "
					"cancel can restore it. Machine-owned -- never edit."
				),
			}
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_sre_replaced_snapshot_field: ensured "
		"Stock Reservation Entry.custom_replaced_sre_snapshot"
	)
