"""Provision the Customer Gold rate snapshot fields on ``Stock Entry``.

A Customer Gold receipt must permanently remember the rate that was resolved for its
posting date. Storing only a number would not be reproducible, so the snapshot records the
whole derivation: which ``Gold Rates`` document was read, for which date, which dealer row
(``particulars``), which rate column, the raw stored value, its unit, and the converted
per-gram rate. An auditor can then answer "where did this rate come from?" without opening
the current Gold Rates master -- which may since have been edited.

``custom_gold_rate_raw`` and ``custom_gold_rate_per_gram`` carry an explicit
``precision: 4``. Currency fields otherwise round to ``System Settings.currency_precision``,
which is ``2`` on these sites, and a per-gram gold rate genuinely needs more: 71,648.30 per
10 g is 7,164.83, and finer feeds exist. ``Sales Order.gold_rate`` shows what happens
without it -- it is physically ``decimal(21,2)`` and truncates.

Every field is ``read_only`` (machine-owned; the server overwrites whatever a client sends)
and ``no_copy`` so an amended receipt resolves against its own posting date rather than
inheriting evidence that describes a different document.

``insert_after`` anchors on the STANDARD ``value_difference`` rather than any custom field:
``post_model_sync`` patches run at ``frappe/migrate.py:144``, before ``sync_fixtures()`` at
``:171`` and ``sync_customizations()`` at ``:180``, so gke-owned fields such as
``_customer`` do not exist yet the first time this runs on a fresh site.

Because ``after_migrate`` is disabled (``hooks.py:12``) and ``install-app`` marks patches
complete WITHOUT running them on fresh / CI sites, this is wired in two idempotent places
per the app convention: this ``post_model_sync`` patch and ``create_test_data.setup_data``.
Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.add_customer_gold_rate_snapshot_fields.execute

Idempotent: ``create_custom_fields`` keys on ``(dt, fieldname)``.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

RATE_PRECISION = "4"


def execute():
	custom_fields = {
		"Stock Entry": [
			{
				"fieldname": "custom_customer_gold_rate_section",
				"fieldtype": "Section Break",
				"label": "Customer Gold Rate Details",
				"insert_after": "value_difference",
				"module": "Jewellery Erpnext",
				"collapsible": 1,
				"depends_on": "eval:doc.custom_gold_rate_per_gram",
			},
			{
				"fieldname": "custom_gold_rate_reference",
				"fieldtype": "Link",
				"options": "Gold Rates",
				"label": "Gold Rates Reference",
				"insert_after": "custom_customer_gold_rate_section",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"description": "Gold Rates document the rate was read from. Machine-owned -- never edit.",
			},
			{
				"fieldname": "custom_gold_rate_date",
				"fieldtype": "Date",
				"label": "Gold Rate Date",
				"insert_after": "custom_gold_rate_reference",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "custom_gold_rate_source",
				"fieldtype": "Data",
				"label": "Gold Rate Source",
				"insert_after": "custom_gold_rate_date",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"description": "Particulars of the Gold Rates branchs row that supplied the rate.",
			},
			{
				"fieldname": "custom_gold_rate_column_break",
				"fieldtype": "Column Break",
				"insert_after": "custom_gold_rate_source",
				"module": "Jewellery Erpnext",
			},
			{
				"fieldname": "custom_gold_rate_field",
				"fieldtype": "Data",
				"label": "Gold Rate Field",
				"insert_after": "custom_gold_rate_column_break",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"description": "Rate column that was read, for example live_rate.",
			},
			{
				"fieldname": "custom_gold_rate_raw",
				"fieldtype": "Currency",
				"label": "Raw Gold Rate",
				"insert_after": "custom_gold_rate_field",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"precision": RATE_PRECISION,
				"description": "Value exactly as stored in Gold Rates, before unit conversion.",
			},
			{
				"fieldname": "custom_gold_rate_unit",
				"fieldtype": "Data",
				"label": "Gold Rate Unit",
				"insert_after": "custom_gold_rate_raw",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
			},
			{
				"fieldname": "custom_gold_rate_per_gram",
				"fieldtype": "Currency",
				"label": "Gold Rate Per Gram",
				"insert_after": "custom_gold_rate_unit",
				"module": "Jewellery Erpnext",
				"read_only": 1,
				"no_copy": 1,
				"precision": RATE_PRECISION,
				"description": "Raw rate converted to per gram. Frozen at submit.",
			},
		]
	}

	create_custom_fields(custom_fields, ignore_validate=True)
	frappe.logger().info(
		"add_customer_gold_rate_snapshot_fields: ensured 9 Customer Gold rate fields on Stock Entry"
	)
