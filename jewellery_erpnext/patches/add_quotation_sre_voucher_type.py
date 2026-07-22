"""
Add "Quotation" to the ``Stock Reservation Entry.voucher_type`` Select ``options`` via a
Property Setter.

WHY THIS EXISTS
---------------
The manufacturing flow now anchors stock reservation on the **Quotation** for newly created
records (Sales Order has been removed from the Quotation -> Manufacturing Plan path). The MOP
EOD sync (``mop_eod_sync._build_and_submit_mwo_sre``) builds a ``Stock Reservation Entry`` with
``voucher_type = resolved["voucher_type"]`` -- "Quotation" for new records, "Sales Order" for
legacy ones.

Frappe validates Select values SERVER-SIDE in ``Document._validate_selects`` (``frappe.in_test``
does NOT bypass it), so saving an SRE with ``voucher_type="Quotation"`` throws unless "Quotation"
is a real option on the field's meta. ERPNext core ships the options as
``\nSales Order\nWork Order\nSubcontracting Inward Order\nProduction Plan\nSubcontracting Order``
-- no "Quotation". This patch closes that gap.

The SRE ``voucher_no`` is a Dynamic Link whose ``options`` is ``voucher_type``, so it resolves
against the Quotation doctype automatically; ``voucher_detail_no`` is a plain Data field that
stores the Quotation Item row name. No other core-field change is needed -- ``validate()`` has no
voucher-type allowlist, ``update_reserved_qty_in_voucher`` degrades gracefully for an unmapped
voucher type (``.get(voucher_type, None)`` -> skip), and the reservation cap treats a Quotation as
having zero delivered qty (correct).

Like the other jewellery config, ``property_setter/*.json`` is dead config (``after_migrate`` is
disabled), so Property Setters only reach real / CI sites via patches + ``create_test_data``. This
patch is that mechanism for the SRE ``voucher_type``.

ADDITIVE ON PURPOSE
-------------------
``frappe.make_property_setter`` autonames ``{doctype}-{field}-{property}`` and delete-then-inserts,
so it is idempotent but DESTRUCTIVE -- it overwrites whatever options list is live. To avoid
clobbering an already-broadened list, we READ the field's current options from meta and UNION-in
only the missing value, preserving order.

Can be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_quotation_sre_voucher_type.ensure_quotation_sre_voucher_type
"""

import frappe

_DOCTYPE = "Stock Reservation Entry"
_FIELD = "voucher_type"
_REQUIRED_OPTIONS = ["Quotation"]


def _current_options(doctype, fieldname):
	field = frappe.get_meta(doctype).get_field(fieldname)
	if not field:
		return None
	# options is a newline-separated string; the leading blank line (a "" option) is preserved.
	return (field.options or "").split("\n")


def ensure_quotation_sre_voucher_type():
	"""Idempotently union "Quotation" into the SRE ``voucher_type`` Select options."""
	options = _current_options(_DOCTYPE, _FIELD)
	if options is None:
		return []

	missing = [opt for opt in _REQUIRED_OPTIONS if opt not in options]
	if not missing:
		return []

	new_options = "\n".join(options + missing)
	frappe.make_property_setter(
		{
			"doctype_or_field": "DocField",
			"doctype": _DOCTYPE,
			"fieldname": _FIELD,
			"property": "options",
			"value": new_options,
			"property_type": "Text",
		},
		is_system_generated=False,
	)
	frappe.db.commit()
	frappe.logger().info(
		"add_quotation_sre_voucher_type: added 'Quotation' to "
		f"{_DOCTYPE}.{_FIELD} options"
	)
	return [f"{_DOCTYPE}.{_FIELD}"]


def execute():
	ensure_quotation_sre_voucher_type()
