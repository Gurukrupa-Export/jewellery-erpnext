"""
Provision the ``Serial and Batch Entry.qty`` precision = 3 Property Setter.

Employee IR's auto-created Process Loss Stock Entry builds rows as small as 0.001 g. On submit
ERPNext builds a Serial and Batch Bundle and rounds the batch qty via
``flt(batch_qty, precision("Serial and Batch Entry", "qty"))``. With System Settings
``float_precision = 2`` and no per-field precision on ``qty``, ``flt(0.001, 2) = 0.0`` and the
bundle validation throws ``At row 1: Qty is mandatory for the batch ...`` -- aborting the whole
EIR submit. This is the twin of the already-shipped ``Stock Entry Detail.transfer_qty`` fix; the
two are complementary. The intended Property Setter lives in
``property_setter/serial_and_batch_entry.json`` but is only applied by the disabled
``after_migrate`` hook, so it never reaches real sites. This patch creates it.

The guard is idempotent (``make_property_setter`` delete-then-inserts the same setter) and now
re-asserts every file in ``_PROVISIONED_FILES``, so running it again is a safe no-op-equivalent.
"""

from jewellery_erpnext.property_setter_guard import (
	ensure_field_precision_property_setters,
)


def execute():
	ensure_field_precision_property_setters()
