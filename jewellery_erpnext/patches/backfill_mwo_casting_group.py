"""Backfill ``Manufacturing Work Order.casting_group`` on existing sites.

``casting_group`` is a stable, opaque casting-tree group id (the first tree's name) that is
retained across re-issues and tree cancellation so the full cast-together set is always
re-issued together (see ``doc_events/tree_casting.py``). New issues stamp it going forward; this
seeds it for work orders already attached to a tree at upgrade time, using their current
``tree_number`` as the group id.

This groups all in-flight and Received trees (both retain ``tree_number``). Trees that were
already cancelled/deleted before the upgrade have ``tree_number = NULL`` and cannot be recovered
— acceptable, as their re-issue history is already gone. Idempotent: only rows missing a group
and carrying a ``tree_number`` are touched.

Ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.backfill_mwo_casting_group.execute
"""

import frappe


def execute():
	frappe.db.sql(
		"""
		UPDATE `tabManufacturing Work Order`
		SET casting_group = tree_number
		WHERE (casting_group IS NULL OR casting_group = '')
		  AND tree_number IS NOT NULL AND tree_number != ''
		"""
	)
	frappe.logger().info(
		"backfill_mwo_casting_group: seeded casting_group from tree_number"
	)
