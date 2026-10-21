"""Turn on ``Stock Settings.enable_serial_and_batch_no_for_item``.

WHY THIS IS LOAD-BEARING, NOT COSMETIC
--------------------------------------
erpnext ``a6fbb0c7`` ("fix: respect serial / batch activation in stock settings and item",
2026-09-29, ``version-16``) added
``Item.validate_serial_and_batch_no_enabled_in_stock_settings``. It throws

    Cannot enable **Has Batch No** as **Activate Serial / Batch No for Item** is disabled
    in Stock Settings

on any Item save where ``has_batch_no`` is set while that Stock Settings flag is off. The
field ships ``default: 0``, and an existing site that never turned it on keeps 0 through the
upgrade.

This app is batch-driven end to end -- batch naming, the Serial and Batch Bundle override,
ownership stamping, the loss engine. With the flag off on a post-``a6fbb0c7`` erpnext, every
batch-tracked Item becomes unsaveable, which in turn blocks Item creation from Sketch Order,
BOM and the KGGK replication path. So this is not an optional preference; it is the setting
that keeps the app functional across that erpnext bump.

It was caught by CI: the Create Test Data step started failing the moment the upstream commit
landed, because ``install.sh`` tracks ``version-16`` unpinned.

WHY BOTH HERE AND IN create_test_data
-------------------------------------
``install-app`` marks patches complete WITHOUT running them on fresh / CI sites, so a patch
alone never reaches those. ``create_test_data.setup_data`` covers them, and ``install.py``
records that setup_data is a test bootstrap that real sites never run -- so a patch is the
only thing that reaches an existing site. Same two-place convention as
``seed_stock_entry_types`` and ``add_snc_tolerance_override_fields``.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.enable_serial_and_batch_no_for_item.execute

Idempotent: a site that already has the flag on is left untouched and logs nothing.

Only ever turns the flag ON. erpnext refuses to turn it off once Serial and Batch Bundles
exist (``StockSettings.validate_serial_and_batch_no_settings``), and nothing here should be
able to reverse an administrator's deliberate choice in the other direction.
"""

import frappe

FIELDNAME = "enable_serial_and_batch_no_for_item"


def execute():
	# get_meta rather than a bare save: on an erpnext older than the one that introduced
	# the field, setting it would be silently dropped and the save would still rewrite
	# Stock Settings for no reason.
	if not frappe.get_meta("Stock Settings").has_field(FIELDNAME):
		return

	settings = frappe.get_single("Stock Settings")
	if settings.get(FIELDNAME):
		return

	settings.set(FIELDNAME, 1)
	settings.save(ignore_permissions=True)

	frappe.logger().info(
		"enable_serial_and_batch_no_for_item: activated Serial / Batch No for Item in Stock Settings"
	)
