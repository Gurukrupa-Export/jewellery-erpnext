"""
Provision the Stock Reservation Entry qty-field precision = 3 Property Setters.

The Transfer-to-Reserve Material Request flow (material_request_type = "Manufacture") submits a
reserve Stock Entry that creates Stock Reservation Entries against the originating Sales Order.
For a diamond item in Carat, ERPNext's ``validate_with_allowed_qty``
(erpnext/stock/doctype/stock_reservation_entry/stock_reservation_entry.py) computes::

    allowed_qty = flt(min(available_qty, ...), self.precision("reserved_qty"))

With System Settings ``float_precision = 2`` and no per-field precision on ``reserved_qty``,
that precision is 2, so a genuine sub-0.01 ct available qty rounds to 0 -- e.g.
``flt(0.0050000000000000044, 2) = 0.0`` -- and the SRE submit throws
``Cannot reserve more than Allowed Qty 0.0 Carat ...``, aborting the reserve flow.

This is the third sibling of the already-shipped ``Stock Entry Detail.transfer_qty`` and
``Serial and Batch Entry.qty`` precision fixes. The intended Property Setters live in
``property_setter/stock_reservation_entry.json`` (all qty fields -> precision "3") but are only
applied by the disabled ``after_migrate`` hook, so they never reach real sites. This patch
creates them.

The guard is idempotent (``make_property_setter`` delete-then-inserts the same setter) and
re-asserts every file in ``_PROVISIONED_FILES``, so running it again is a safe
no-op-equivalent. A new patch entry is required (rather than reusing the existing two) because
those are already marked "done" on migrated sites and would not re-run the now-3-file guard.
"""

from jewellery_erpnext.property_setter_guard import (
	ensure_field_precision_property_setters,
)


def execute():
	ensure_field_precision_property_setters()
