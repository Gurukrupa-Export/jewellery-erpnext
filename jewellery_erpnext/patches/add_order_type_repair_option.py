"""
Provision the ``order_type`` Select ``options`` Property Setters that add "Repair" (and, on Sales
Order, "Stock Order") to Quotation and Sales Order.

WHY THIS EXISTS
---------------
A repair is now marked at the HEADER level via the standard ``order_type`` Select == "Repair" on
Quotation / Sales Order (it flows Quotation -> Sales Order automatically through
``get_mapped_doc``, and drives the Manufacturing Plan "Repair" fetch). But Frappe validates Select
values SERVER-SIDE in ``Document._validate_selects`` -- and ``frappe.in_test`` does NOT bypass it
(only ``_T-``-prefixed values are exempt). So saving/submitting an order with ``order_type="Repair"``
throws unless "Repair" is a real option in the field's meta.

- Quotation's ``order_type`` options are overridden to ``\nSales\nStock Order\nValue Stock`` by the
  ``Quotation-order_type-options`` Property Setter -- no "Repair".
- Sales Order's ``order_type`` list is broadened only CLIENT-SIDE in
  ``public/js/doctype_js/sales_order.js`` (adds "Stock Order" and "Repair"); there is NO server-side
  Property Setter, so saving a Sales Order with ``order_type`` in {Stock Order, Repair} already fails
  ``_validate_selects`` today. This patch closes that gap too.

Like the other jewellery config, ``custom_fields/*.json`` + ``property_setter/*.json`` are dead
config (``after_migrate`` is disabled). Property Setters only reach real / CI sites via patches +
``create_test_data.setup_data`` (see ``property_setter_guard`` / ``fetch_from_guard``). This patch is
that mechanism for ``order_type``.

ADDITIVE ON PURPOSE
-------------------
``frappe.make_property_setter`` autonames ``{doctype}-{field}-{property}`` and delete-then-inserts,
so it is idempotent but DESTRUCTIVE -- it overwrites whatever options list is live. To avoid
clobbering a manually- or gke-broadened list, we READ the field's current options from meta and
UNION-in only the missing values, preserving order, rather than hardcoding a full list.

Wired in two places (both idempotent): a ``post_model_sync`` patch (existing-site migrate) and
``create_test_data.setup_data`` (fresh / CI sites). Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_order_type_repair_option.ensure_order_type_repair_option
"""

import frappe

# doctype -> option values that must be present on ``order_type`` (appended if missing, in order).
_REQUIRED_ORDER_TYPE_OPTIONS = {
	"Quotation": ["Repair"],
	# Sales Order also needs "Stock Order" so the server list is a superset of the client dropdown
	# (sales_order.js), closing the pre-existing server-validation gap for "Stock Order".
	"Sales Order": ["Stock Order", "Repair"],
}


def _current_options(doctype):
	field = frappe.get_meta(doctype).get_field("order_type")
	if not field:
		return None
	# options is a newline-separated string; the leading blank line (a "" option) is preserved.
	return (field.options or "").split("\n")


def ensure_order_type_repair_option():
	"""Idempotently union the required ``order_type`` options into Quotation / Sales Order.

	Returns the list of ``{doctype}.order_type`` fields whose options were (re)asserted.
	"""
	asserted = []

	for doctype, required in _REQUIRED_ORDER_TYPE_OPTIONS.items():
		options = _current_options(doctype)
		if options is None:
			# Field missing (should never happen for a standard field) -- skip rather than throw.
			continue

		missing = [opt for opt in required if opt not in options]
		if not missing:
			continue

		new_options = "\n".join(options + missing)
		frappe.make_property_setter(
			{
				"doctype_or_field": "DocField",
				"doctype": doctype,
				"fieldname": "order_type",
				"property": "options",
				"value": new_options,
				"property_type": "Text",
			},
			is_system_generated=False,
		)
		asserted.append(f"{doctype}.order_type")

	if asserted:
		frappe.db.commit()
		# make_property_setter already clears each doctype's cache via PropertySetter.validate.
		frappe.logger().info(
			"add_order_type_repair_option: asserted order_type options -> "
			+ ", ".join(sorted(set(asserted)))
		)

	return asserted


def execute():
	ensure_order_type_repair_option()
