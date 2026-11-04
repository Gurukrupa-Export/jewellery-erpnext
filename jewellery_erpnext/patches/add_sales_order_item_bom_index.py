"""Index ``Sales Order Item.bom`` for the BOM ``on_change`` customer-weight sync.

``doc_events.sales_order.sync_customer_material_weights`` runs on every BOM save
not made by a Sales Order save and looks up the order rows using that BOM:

    SELECT soi.name FROM `tabSales Order Item` soi JOIN `tabSales Order` so ...
    WHERE soi.bom = %s AND so.docstatus = 0 AND so.sales_type = 'Outwork'

``bom`` carries no index, so each of these was a full scan of Sales Order Item.

Uses the shared, column-guarded ``_add_index_if_missing``, so a re-run is a no-op.
"""

import frappe

from jewellery_erpnext.patches.add_stock_entry_idempotency_indexes import (
	_add_index_if_missing,
)


def execute():
	_add_index_if_missing("tabSales Order Item", "soi_bom_idx", ("bom",))
	frappe.logger().info(
		"add_sales_order_item_bom_index: ensured soi_bom_idx on tabSales Order Item(bom)"
	)
