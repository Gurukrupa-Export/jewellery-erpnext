"""Provision ``Stock Entry.custom_gold_nominal_value``.

``set_customer_gold_nominal_valuation`` derives a nominal value for a Customer Gold receipt
(qty x the frozen per-gram rate) and writes it to each row's ``basic_amount``. This field
stores the SAME total independently, and that duplication is the point: ``basic_amount`` is
recomputed by ``Repost Item Valuation`` (``stock_ledger.py:1373-1384`` reloads the document
and re-runs ``calculate_rate_and_amount``), whereas this field is written only by the
Customer Gold path on a draft validate. If the two ever disagree, something re-valued a
customer receipt behind our back -- that divergence IS the audit signal, and it cannot be
detected if only ERPNext's own field is kept.

``precision`` is deliberately left at the ``currency_precision`` default of 2, unlike the
rate fields. This is a booked rupee VALUE that must tie exactly to ``stock_value_difference``
and to the GL, both of which round to currency precision; carrying more decimals here than
the ledger can hold would manufacture a reconciliation difference rather than reveal one.

``read_only`` (machine-owned) and ``no_copy`` so an amended receipt re-derives against its
own posting date and its own quantities rather than inheriting a value describing a
different document.

``insert_after`` anchors on ``custom_gold_rate_per_gram``, created by the sibling patch
``add_customer_gold_rate_snapshot_fields``, which is registered earlier in ``patches.txt``
and is itself idempotent. Both run under ``post_model_sync``, in file order.

Because ``after_migrate`` is disabled (``hooks.py:12``) and ``install-app`` marks patches
complete WITHOUT running them on fresh / CI sites, this is wired in two idempotent places
per the app convention: this ``post_model_sync`` patch and ``create_test_data.setup_data``.
Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_customer_gold_nominal_value_field.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Stock Entry": [
			{
				"fieldname": "custom_gold_nominal_value",
				"fieldtype": "Currency",
				"label": "Customer Gold Nominal Value",
				"insert_after": "custom_gold_rate_per_gram",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"description": (
					"Total nominal value of the customer's metal on this receipt, derived as "
					"quantity x the frozen per-gram rate. Compare with the sum of row Amounts: "
					"a difference means the receipt was re-valued after posting."
				),
			},
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_customer_gold_nominal_value_field: ensured custom_gold_nominal_value on Stock Entry"
	)
