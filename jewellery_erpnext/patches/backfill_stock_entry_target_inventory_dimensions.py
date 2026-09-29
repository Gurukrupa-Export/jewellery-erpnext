"""Repair inward Stock Entry inventory-dimension values on historical rows.

ERPNext reads a Stock Entry's INWARD leg dimension off ``Stock Entry Detail.to_<field>``
and its OUTWARD leg off ``<field>`` (controllers/stock_controller.py:1186-1195). Nothing
in this bench ever wrote ``to_inventory_type`` / ``to_customer``, while the blanket
default in ``doc_events/stock_entry.before_validate`` forces ``inventory_type`` to
"Regular Stock" on every row. Result: every inward Stock Entry SLE holds a NULL dimension
and every outward twin holds "Regular Stock".

``StockLedgerEntry.validate_serial_no_inventory_dimension`` (erpnext#58394, backported as
#58419, arrived with erpnext 16.34.1) compares an outward serialized SLE against that
serial's LAST INWARD SLE and rejects a mismatch, so every serial received through a Stock
Entry is now un-issuable::

    Serial No X is not available in the selected inventory dimensions:
    Inventory Type: expected "Not Set", got "Regular Stock"

``set_target_inventory_dimensions`` fixes new documents. It cannot fix old ones -- the
validator looks backwards at the last INWARD row -- so this patch repairs history.

Two writes per registered dimension:

1. ``tabStock Entry Detail.to_<field>`` <- the row's own ``<field>``. Not cosmetic:
   cancelling a historical Stock Entry re-derives the reversal from the persisted row, so
   a NULL row would emit a reversal tagged NULL against an original tagged "Regular
   Stock" -- a dimension bucket that never balances.
2. ``tabStock Ledger Entry.<target_fieldname>`` <- that same value, joined through
   ``voucher_detail_no``, for inward non-cancelled Stock Entry rows only.

``inventory_type`` falls back to "Regular Stock" where the source row is itself blank
(pre-blanket-default rows), because that is exactly what a future outward leg will carry
-- leaving it NULL would keep those serials blocked. Other dimensions (``customer``) get
a pure mirror and no invented value: ``normalize_ownership`` guarantees a non-customer
lane carries no customer, so a customer is never fabricated here.

Safe to write submitted rows: this column feeds no valuation, qty or reposting logic;
``validate_negative_stock`` is 0 for these dimensions so
``validate_inventory_dimension_negative_stock`` ignores it; and no jewellery_erpnext code
reads ``Stock Ledger Entry.inventory_type`` (ownership reporting goes through
``Batch.custom_inventory_type``). ``SUM(stock_value) FROM tabBin`` must be bit-identical
before and after. ``update_modified`` is deliberately not touched so this correction does
not look like a business edit.

Dimension-driven rather than hardcoded: ``gk`` registers only ``Inventory Type`` while
``kg-gk`` and ``alfarsi`` also register ``Customer``, which carries the identical defect.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.backfill_stock_entry_target_inventory_dimensions.execute

Idempotent: both passes skip rows that already hold a value, so a second run reports 0.
"""

import re

import frappe

from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	DEFAULT_INVENTORY_TYPE,
)

CHUNK = 5000

# Only ``inventory_type`` has a meaningful "what the blanket default would have produced"
# fallback. Never invent a customer.
BLANK_FALLBACK = {"inventory_type": DEFAULT_INVENTORY_TYPE}

_SAFE_FIELD = re.compile(r"^[a-z_][a-z0-9_]*$")


def _dimension_pairs():
	"""(source, target_on_sed, target_on_sle) for each registered, materialised dimension."""
	from erpnext.stock.doctype.inventory_dimension.inventory_dimension import (
		get_document_wise_inventory_dimensions,
	)

	sed_meta = frappe.get_meta("Stock Entry Detail")
	sle_meta = frappe.get_meta("Stock Ledger Entry")

	pairs = []
	for dimension in get_document_wise_inventory_dimensions("Stock Entry Detail"):
		source = dimension.get("source_fieldname")
		sle_field = dimension.get("target_fieldname")
		if not source or not sle_field or source.startswith("to_"):
			continue

		sed_target = f"to_{source}"
		# Fieldnames come from a user-editable doctype and are interpolated into SQL
		# identifiers below, so refuse anything that is not a plain identifier.
		if not all(_SAFE_FIELD.match(f) for f in (source, sed_target, sle_field)):
			frappe.logger().warning(
				f"{__name__}: skipping unsafe fieldname on {dimension.get('name')}"
			)
			continue

		if not (sed_meta.has_field(source) and sed_meta.has_field(sed_target)):
			continue
		if not sle_meta.has_field(sle_field):
			continue

		pairs.append((source, sed_target, sle_field))

	return pairs


def _apply(doctype, table, field, rows):
	"""Bulk-write ``field`` on ``table``, grouped by value so this stays a few statements."""
	by_value = {}
	for name, value in rows:
		by_value.setdefault(value, []).append(name)

	updated = 0
	for value, names in by_value.items():
		for start in range(0, len(names), CHUNK):
			batch = names[start : start + CHUNK]
			placeholders = ", ".join(["%s"] * len(batch))
			frappe.db.sql(
				f"UPDATE `{table}` SET `{field}` = %s WHERE name IN ({placeholders})",
				[value, *batch],
			)
			updated += len(batch)
			frappe.db.commit()

	return updated


def execute():
	pairs = _dimension_pairs()
	if not pairs:
		frappe.logger().info(
			f"{__name__}: no inventory dimensions registered, nothing to do"
		)
		return

	for source, sed_target, sle_field in pairs:
		fallback = BLANK_FALLBACK.get(source)

		# Pass 1 -- Stock Entry Detail.to_<field>
		sed_rows = frappe.db.sql(
			f"""
			SELECT name, `{source}` AS value
			FROM `tabStock Entry Detail`
			WHERE t_warehouse IS NOT NULL AND t_warehouse != ''
			  AND (`{sed_target}` IS NULL OR `{sed_target}` = '')
			""",
			as_dict=True,
		)
		resolved = [(r.name, (r.value or None) or fallback) for r in sed_rows]
		resolved = [(name, value) for name, value in resolved if value]
		sed_updated = _apply(
			"Stock Entry Detail", "tabStock Entry Detail", sed_target, resolved
		)

		# Pass 2 -- Stock Ledger Entry.<target_fieldname> for inward Stock Entry legs.
		sle_rows = frappe.db.sql(
			f"""
			SELECT sle.name AS name, sed.`{sed_target}` AS value
			FROM `tabStock Ledger Entry` sle
			INNER JOIN `tabStock Entry Detail` sed ON sed.name = sle.voucher_detail_no
			WHERE sle.voucher_type = 'Stock Entry'
			  AND sle.actual_qty > 0
			  AND sle.is_cancelled = 0
			  AND (sle.`{sle_field}` IS NULL OR sle.`{sle_field}` = '')
			  AND sed.`{sed_target}` IS NOT NULL AND sed.`{sed_target}` != ''
			""",
			as_dict=True,
		)
		sle_updated = _apply(
			"Stock Ledger Entry",
			"tabStock Ledger Entry",
			sle_field,
			[(r.name, r.value) for r in sle_rows],
		)

		frappe.logger().info(
			f"{__name__}: {source} -- Stock Entry Detail scanned {len(sed_rows)} "
			f"updated {sed_updated}; Stock Ledger Entry scanned {len(sle_rows)} "
			f"updated {sle_updated}"
		)
