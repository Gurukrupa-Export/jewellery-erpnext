"""Composite index for the Material-Request-by-manufacturing-order reads.

Two hot call sites aggregate Material Requests for a PMO inside submit-path work:
``stock_reservation_entry_for_mwo`` (doc_events/stock_entry.py) and
``pc_tagging_stock_sync`` (department_ir/doc_events), both running

    SELECT sum(custom_total_quantity) FROM `tabMaterial Request`
    WHERE manufacturing_order = %s AND docstatus != 2

``manufacturing_order`` carries no index (it is a Custom Field, so frappe only
indexes it when ``search_index`` is set, which it is not), making each of these a
full scan. The leading ``manufacturing_order`` prefix also serves the plain
equality readers in ``update_utils.py`` and ``manufacturing_work_order.py``.

Deliberately NOT covering: adding ``custom_total_quantity`` as a third column
would answer the SUM from the index alone, but this index name already exists as
``(manufacturing_order, docstatus)`` on migrated sites and this patch is already
in their Patch Log, so widening it here would silently apply to new sites only.
That needs its own patch module under a new index name -- the same reasoning
``add_stock_entry_idempotency_indexes`` records in its docstring.

Uses the shared, column-guarded ``_add_index_if_missing`` so a site whose
``manufacturing_order`` Custom Field has not landed yet (it ships from
``gke_customization``'s fixtures, and post_model_sync patches run before
``sync_fixtures()``) logs and skips instead of aborting the whole migrate.
"""

import frappe

from jewellery_erpnext.patches.add_stock_entry_idempotency_indexes import (
	_add_index_if_missing,
)


def execute():
	_add_index_if_missing(
		"tabMaterial Request",
		"mr_manufacturing_order_idx",
		("manufacturing_order", "docstatus"),
	)
	frappe.logger().info(
		"add_material_request_manufacturing_order_index: ensured "
		"mr_manufacturing_order_idx on tabMaterial Request"
		"(manufacturing_order, docstatus)"
	)
