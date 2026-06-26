"""
Idempotent provisioning of the field-precision Property Setters that the Employee IR
Process Loss Stock Entry depends on.

WHY THIS EXISTS
---------------
Employee IR ``on_submit`` auto-creates a "Process Loss" Stock Entry. A loss row of
0.001 g flows through two precision-sensitive layers, each of which truncates to 0 when
the relevant field's precision is the System Settings default of 2:

1. ``Stock Entry Detail.transfer_qty`` -- ERPNext's ``set_transfer_qty()``
   (erpnext/stock/doctype/stock_entry/stock_entry.py) recomputes
   ``transfer_qty = flt(qty * conversion_factor, precision("transfer_qty"))`` and throws
   ``Row 1: Qty in Stock UOM can not be zero.`` when the result rounds to 0.
2. ``Serial and Batch Entry.qty`` -- on submit ERPNext builds a Serial and Batch Bundle;
   ``SerialBatchCreation.set_serial_batch_entries`` (erpnext/stock/serial_batch_bundle.py)
   rounds the batch qty via ``flt(batch_qty, precision("Serial and Batch Entry", "qty"))``
   and the bundle validation then throws ``At row 1: Qty is mandatory for the batch ...``
   when that rounds to 0. (Serial and Batch Entry is a child of both Serial and Batch
   Bundle and Stock Reservation Entry, so this also keeps SRE sb_entries consistent.)
3. ``Stock Reservation Entry.reserved_qty`` -- the Transfer-to-Reserve Material Request flow
   submits SREs against the Sales Order; ERPNext's ``validate_with_allowed_qty``
   (erpnext/stock/doctype/stock_reservation_entry/stock_reservation_entry.py) computes
   ``allowed_qty = flt(min(available_qty, ...), precision("reserved_qty"))`` and throws
   ``Cannot reserve more than Allowed Qty 0.0 ...`` when a genuine sub-0.01 ct available qty
   (e.g. 0.005 ct) rounds to 0. (SRE's ``sb_entries`` child is Serial and Batch Entry, already
   covered by item 2; the parent qty fields are pinned by this guard.)

With System Settings ``float_precision = 2`` and no per-field precision on these fields,
that precision is 2, so ``flt(0.001, 2) = 0.0`` (and ``flt(0.005, 2) = 0.0``) -> the Employee
IR / reserve submit aborts.

The intended fixes already exist as data in ``property_setter/stock_entry_detail.json``
and ``property_setter/serial_and_batch_entry.json`` (precision = "3"), but they are only
applied by ``migrate.create_property_setter()``, which runs from the ``after_migrate``
hook -- and ``after_migrate`` is disabled in hooks.py (the recurring "migrate-time config
is dead on real / CI sites" problem -- see ``fetch_from_guard``). So no Property Setter
record reaches real sites. This guard closes that gap.

SCOPE (deliberately narrow): provisions ONLY the field-level ``precision`` Property Setters
declared in the files listed in ``_PROVISIONED_FILES``. It does NOT sweep every
``property_setter/*.json`` file. Those other files carry ``field_order`` / ``read_only`` /
``options`` changes across many doctypes (Sales Order, Purchase Order, Customer, ...) whose
blanket re-assertion would have a large blast radius and could clobber config owned by other
apps (e.g. ``gke_customization``). Adding a file or property type here is an explicit,
reviewed decision -- unlike ``fetch_from_guard`` (which is purely additive), Property Setters
are destructive (PropertySetter.validate delete-then-inserts).

``frappe.make_property_setter`` is idempotent: Property Setter autoname is
``{doc_type}-{field_name}-{property}`` and re-applying delete-then-inserts the same setter and
clears the doctype cache. Re-running rewrites the same value -- a safe no-op-equivalent.

Wired in two places (both idempotent): a ``post_model_sync`` patch (existing-site migrate) and
``create_test_data.setup_data`` (fresh / CI sites). Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.property_setter_guard.ensure_field_precision_property_setters
"""

import json
import os

import frappe

# The ONLY property_setter files this guard provisions. Keep this list narrow on purpose.
_PROVISIONED_FILES = (
	"stock_entry_detail.json",
	"serial_and_batch_entry.json",
	"stock_reservation_entry.json",
)


def ensure_field_precision_property_setters():
	"""Create / refresh the field-level precision Property Setters from JSON.

	Returns the list of ``"<doctype>.<fieldname>.<property>"`` keys it asserted (empty when
	nothing matched -- never the steady state, since the JSON always declares at least one).
	"""
	asserted = []

	for filename in _PROVISIONED_FILES:
		data = _load_property_setter_file(filename)
		for doctype, rows in data.items():
			for row in rows:
				# Only field-level precision setters belong to this guard.
				if (
					row.get("doctype_or_field") != "DocField"
					or row.get("property") != "precision"
					or not row.get("fieldname")
				):
					continue

				value = row["value"]
				if isinstance(value, list):
					value = json.dumps(value)

				# Same call migrate.create_property_setter() uses; the JSON's fieldname/doctype
				# keys pass through unchanged.
				frappe.make_property_setter(
					{
						"doctype_or_field": "DocField",
						"doctype": doctype,
						"fieldname": row["fieldname"],
						"property": "precision",
						"value": value,
						"property_type": row.get("property_type") or "Select",
					},
					is_system_generated=False,
				)
				asserted.append(f"{doctype}.{row['fieldname']}.precision")

	if asserted:
		frappe.db.commit()
		# make_property_setter already clears each doctype's cache via PropertySetter.validate.
		frappe.logger().info(
			"ensure_field_precision_property_setters: asserted precision property setters -> "
			+ ", ".join(sorted(set(asserted)))
		)

	return asserted


# Backward-compat alias: the original (narrower) name kept working for any external / ad-hoc
# ``bench execute`` callers and historical references after the function was generalized to
# provision more than just Stock Entry Detail.
ensure_stock_entry_detail_precision = ensure_field_precision_property_setters


def _load_property_setter_file(filename):
	"""Load a single ``property_setter/<filename>`` JSON from this app."""
	path = os.path.join(
		os.path.dirname(__file__), "jewellery_erpnext", "property_setter", filename
	)
	with open(path) as f:
		return json.load(f)
