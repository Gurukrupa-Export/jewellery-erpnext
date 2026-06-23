"""
Provision the ``Stock Entry Detail.transfer_qty`` precision = 3 Property Setter.

Employee IR's auto-created Process Loss Stock Entry builds rows with qty / transfer_qty as
small as 0.001 g. With System Settings ``float_precision = 2`` and no per-field precision on
``transfer_qty``, ERPNext's ``set_transfer_qty()`` rounds ``flt(0.001, 2) = 0.0`` and throws
``Qty in Stock UOM can not be zero.`` on submit. The intended fix lives in
``property_setter/stock_entry_detail.json`` but is only applied by the disabled
``after_migrate`` hook, so the Property Setter never reaches real sites. This patch creates it.
Idempotent (``make_property_setter`` delete-then-inserts the same setter).
"""

from jewellery_erpnext.property_setter_guard import ensure_stock_entry_detail_precision


def execute():
	ensure_stock_entry_detail_precision()
