"""Fill Order Type / Sales Type / Flow Type / Design Type down the planning chain where blank.

Every hop below the Sales Order carries these four by ``fetch_from``, which fires once, at
insert::

    Sales Order -> Parent Manufacturing Order -> Manufacturing Work Order
                                              -> Serial Number Creator -> Serial No

On sites that ran the v15 chain patches, a Custom Field row with the same fieldname shadowed the
standard field on PMO, MWO and SNC, so the fetch never happened and every document created since
holds a blank. ``reconcile_v15_order_dimension_custom_fields`` removes the shadow rows, which
fixes new documents only -- it deliberately backfills nothing. This patch writes what the fetch
would have written. On gepl-prod (2026-11-02) that is every PMO, MWO and SNC.

WHAT IT DOES
------------
One set-based ``UPDATE ... JOIN`` per (hop, field), in chain order, so each hop reads the hop
above it after that hop has been filled. A target is written only when it is blank and its source
is not, so a value already on a document is never overwritten and a second run changes nothing.

Serial No is reached through ``Serial Number Creator.fg_serial_no`` of a submitted SNC, the same
definitive SNC -> piece link ``add_serial_no_stamping_no_field`` uses, and gets the three
``custom_*`` fields ``manufacturing_operation.create_manufacturing_entry`` stamps. SNC has no
design_type, so Serial No gets none either.

Raw SQL, never a document save: no hooks, Server Scripts (gepl-prod pushes every saved Serial No
to KGGK) or Version rows fire, and ``modified`` is left alone so the correction does not look like
a business edit. A pair whose column is missing on the site is skipped and reported.

Note for sites that never had ``sales_type`` on the PMO: once it is filled,
``ParentManufacturingOrder._validate_diamond_grade_policy`` enforces the Outright / Outwork
customer-diamond rule on the next submit of a draft PMO.

Must run AFTER ``reconcile_v15_order_dimension_custom_fields``.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.backfill_order_dimensions_down_chain.execute

Idempotent: a second run finds no blank target with a non-blank source and reports 0.
"""

import frappe

# Chain order matters: each hop's source is the previous hop's target.
HOPS = (
	{
		"target": "Parent Manufacturing Order",
		"source": "Sales Order",
		"join": ("sales_order", "name"),
		"fields": {
			"order_type": "order_type",
			"sales_type": "sales_type",
			"flow_type": "custom_flow_type",
			"design_type": "custom_design_type",
		},
	},
	{
		"target": "Manufacturing Work Order",
		"source": "Parent Manufacturing Order",
		"join": ("manufacturing_order", "name"),
		"fields": {
			"order_type": "order_type",
			"sales_type": "sales_type",
			"flow_type": "flow_type",
			"design_type": "design_type",
		},
	},
	{
		"target": "Serial Number Creator",
		"source": "Parent Manufacturing Order",
		"join": ("parent_manufacturing_order", "name"),
		"fields": {
			"order_type": "order_type",
			"sales_type": "sales_type",
			"flow_type": "flow_type",
		},
	},
	{
		"target": "Serial No",
		"source": "Serial Number Creator",
		"join": ("name", "fg_serial_no"),
		"source_condition": "src.docstatus = 1",
		"fields": {
			"custom_order_type": "order_type",
			"custom_sales_type": "sales_type",
			"custom_flow_type": "flow_type",
		},
	},
)

LOG_PREFIX = "backfill_order_dimensions_down_chain"


def _join_clause(hop):
	target_key, source_key = hop["join"]
	return (
		f"`tab{hop['target']}` tgt "
		f"JOIN `tab{hop['source']}` src ON tgt.`{target_key}` = src.`{source_key}`"
	)


def _where_clause(hop, target_field, source_field):
	"""Blank target, non-blank source: never overwrite a value, never write a blank."""
	clause = f"IFNULL(tgt.`{target_field}`, '') = '' AND IFNULL(src.`{source_field}`, '') != ''"
	if hop.get("source_condition"):
		clause += f" AND {hop['source_condition']}"
	return clause


def _missing_columns(hop, target_field, source_field):
	target_key, source_key = hop["join"]
	needed = [
		(hop["target"], target_key),
		(hop["target"], target_field),
		(hop["source"], source_key),
		(hop["source"], source_field),
	]
	return [
		f"{doctype}.{column}"
		for doctype, column in needed
		if column != "name" and not frappe.db.has_column(doctype, column)
	]


def fill(hop, target_field, source_field):
	"""Fill one blank field across one hop. Returns the number of rows written."""
	join = _join_clause(hop)
	where = _where_clause(hop, target_field, source_field)

	count = frappe.db.sql(f"SELECT COUNT(*) FROM {join} WHERE {where}")[0][0]
	if not count:
		return 0

	frappe.db.sql(
		f"UPDATE {join} SET tgt.`{target_field}` = src.`{source_field}` WHERE {where}"
	)
	return count


def backfill(commit=False):
	"""Fill every hop in chain order. Returns {(target, field): rows written}; skipped pairs
	are reported as ``None``. Commits after each hop only when ``commit`` is set."""
	counts = {}
	for hop in HOPS:
		for target_field, source_field in hop["fields"].items():
			missing = _missing_columns(hop, target_field, source_field)
			if missing:
				frappe.logger("jewellery_erpnext").warning(
					f"{LOG_PREFIX}: skipped {hop['target']}.{target_field}, "
					f"missing column(s): {', '.join(missing)}"
				)
				counts[(hop["target"], target_field)] = None
				continue

			counts[(hop["target"], target_field)] = fill(
				hop, target_field, source_field
			)

		if commit:
			frappe.db.commit()

	return counts


def execute():
	counts = backfill(commit=True)
	print(
		f"{LOG_PREFIX}: "
		+ ", ".join(
			f"{target}.{field}={'skipped' if rows is None else rows}"
			for (target, field), rows in counts.items()
		)
	)
