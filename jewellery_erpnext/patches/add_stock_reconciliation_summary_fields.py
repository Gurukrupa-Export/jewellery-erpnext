"""Provision the Stock Reconciliation summary fields.

* ``custom_item_group_summary`` (below ``items``): total qty per Item Group and UOM, with its gram
  value (carat x 0.2), plus ``custom_grand_total_gram``.
* ``custom_total_pure_weight`` and ``Stock Reconciliation Item.custom_pure_weight`` (below
  ``qty``): qty x purity % / 100 for metal and finding items.
* ``custom_positive_difference_qty`` / ``custom_negative_difference_qty`` (below
  ``custom_stock_comparison``): totals of the signed Stock Comparison difference.

Values are computed by ``stock_reonciliation.set_reconciliation_summaries`` on validate.

Because ``after_migrate`` is disabled (``hooks.py``) and ``install-app`` marks patches complete
WITHOUT running them on fresh / CI sites, a fixture-only column would never reach the DB. Per the
app convention this is wired in two idempotent places: this ``post_model_sync`` patch (the child
doctype ``Stock Reconciliation Item Group Summary`` is synced by then) and
``create_test_data.setup_data``. Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_stock_reconciliation_summary_fields.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

READ_ONLY_TOTAL = {
	"fieldtype": "Float",
	"precision": "3",
	"read_only": 1,
	"no_copy": 1,
	"module": "Jewellery Erpnext",
}


def execute():
	create_custom_fields(
		{
			"Stock Reconciliation": [
				{
					"fieldname": "custom_item_group_summary",
					"fieldtype": "Table",
					"label": "Item Group Summary",
					"options": "Stock Reconciliation Item Group Summary",
					"insert_after": "items",
					"read_only": 1,
					"no_copy": 1,
					"module": "Jewellery Erpnext",
				},
				{
					**READ_ONLY_TOTAL,
					"fieldname": "custom_grand_total_gram",
					"label": "Grand Total (Gram)",
					"insert_after": "custom_item_group_summary",
				},
				{
					**READ_ONLY_TOTAL,
					"fieldname": "custom_total_pure_weight",
					"label": "Total Pure Weight",
					"insert_after": "custom_grand_total_gram",
				},
				{
					**READ_ONLY_TOTAL,
					"fieldname": "custom_positive_difference_qty",
					"label": "Positive Difference Qty",
					"insert_after": "custom_stock_comparison",
				},
				{
					**READ_ONLY_TOTAL,
					"fieldname": "custom_negative_difference_qty",
					"label": "Negative Difference Qty",
					"insert_after": "custom_positive_difference_qty",
				},
			],
			"Stock Reconciliation Item": [
				{
					**READ_ONLY_TOTAL,
					"fieldname": "custom_pure_weight",
					"label": "Pure Weight",
					"insert_after": "qty",
					"no_copy": 0,
					"in_list_view": 1,
					"columns": 1,
					"description": "Qty x purity % / 100 (metal and finding items).",
				}
			],
		},
		ignore_validate=True,
	)
