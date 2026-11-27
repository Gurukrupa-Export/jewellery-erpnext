"""Index ``Diamond Price List (customer, price_list_type)`` for Tracking BOM pricing.

Every diamond rate lookup made while a Quotation builds its Tracking BOMs filters on
``customer`` and ``price_list_type`` (``bom_utils._calculate_diamond_amount``, the
``quotation_pricing`` diamond helpers, ``_get_customer_pricing_context``). The only composite
index, ``bom_rate``, starts at ``price_list`` and does not hold ``customer``. On the GK
snapshot (22k rows) the ``_calculate_diamond_amount`` query read about 1,600 rows and took
5.6 ms; with this index it reads the customer's rows only (about 150) and takes 0.56 ms.

Composite, so Frappe's schema sync leaves it alone. ``_add_index_if_missing`` makes a re-run
a no-op.
"""

from jewellery_erpnext.patches.add_stock_entry_idempotency_indexes import (
	_add_index_if_missing,
)


def execute():
	_add_index_if_missing(
		"tabDiamond Price List",
		"dpl_customer_price_list_type_idx",
		("customer", "price_list_type"),
	)
