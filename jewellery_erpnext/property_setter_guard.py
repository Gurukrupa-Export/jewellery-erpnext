"""
Idempotent provisioning of the Stock Entry Detail field-precision Property Setter
that the Employee IR Process Loss Stock Entry depends on.

WHY THIS EXISTS
---------------
Employee IR ``on_submit`` auto-creates a "Process Loss" Stock Entry. A loss row of
0.001 g builds an SE detail row with ``qty = transfer_qty = 0.001`` and
``conversion_factor = 1``. ERPNext's ``set_transfer_qty()``
(erpnext/stock/doctype/stock_entry/stock_entry.py) recomputes
``transfer_qty = flt(qty * conversion_factor, precision("transfer_qty"))`` and throws
``Row 1: Qty in Stock UOM can not be zero.`` when the result rounds to 0.

With System Settings ``float_precision = 2`` and no per-field precision on
``Stock Entry Detail.transfer_qty``, that precision is 2, so ``flt(0.001, 2) = 0.0`` ->
the whole Employee IR submit aborts.

The intended fix already exists as data in ``property_setter/stock_entry_detail.json``
(``transfer_qty`` precision = "3"), but it is only applied by
``migrate.create_property_setter()``, which runs from the ``after_migrate`` hook -- and
``after_migrate`` is disabled in hooks.py (the recurring "migrate-time config is dead on
real / CI sites" problem -- see ``fetch_from_guard``). So no Property Setter record reaches
real sites. This guard closes that gap.

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

    bench --site <site> execute jewellery_erpnext.property_setter_guard.ensure_stock_entry_detail_precision
"""

import json
import os

import frappe

# The ONLY property_setter files this guard provisions. Keep this list narrow on purpose.
_PROVISIONED_FILES = ("stock_entry_detail.json",)


def ensure_stock_entry_detail_precision():
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
			"ensure_stock_entry_detail_precision: asserted precision property setters -> "
			+ ", ".join(sorted(set(asserted)))
		)

	return asserted


def _load_property_setter_file(filename):
	"""Load a single ``property_setter/<filename>`` JSON from this app."""
	path = os.path.join(
		os.path.dirname(__file__), "jewellery_erpnext", "property_setter", filename
	)
	with open(path) as f:
		return json.load(f)
