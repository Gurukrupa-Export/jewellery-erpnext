"""Second pass of ``move_sales_invoice_invoice_item_to_e_invoice_child``.

That patch is already logged as run on prod, but ``sync_fixtures`` (which runs after the
patches) re-imported gke_customization's old Custom Field fixture and pointed
``Sales Invoice.invoice_item`` back at ``Sales Invoice Item``. Invoices saved in that window
wrote their e-invoice rows to ``tabSales Invoice Item`` again.

A patch is logged by name and never re-runs, so this one has its own name and repeats the
same idempotent move: field options first, then copy any ``invoice_item`` rows left in
``tabSales Invoice Item`` into ``tabSales Invoice E Invoice Item`` and delete the copied ones.
"""

from jewellery_erpnext.patches.move_sales_invoice_invoice_item_to_e_invoice_child import (
	execute as move_rows,
)


def execute():
	move_rows()
