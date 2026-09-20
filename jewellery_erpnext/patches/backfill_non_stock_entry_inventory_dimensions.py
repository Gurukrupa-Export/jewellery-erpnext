"""Repair inward inventory-dimension values on historical NON-Stock-Entry Stock Ledger Entries.

``backfill_stock_entry_target_inventory_dimensions`` repaired the Stock Entry half of this
defect and is deliberately left alone: it works by copying
``Stock Entry Detail.to_<field>`` onto the SLE through ``voucher_detail_no``, and its pass
2 is filtered ``voucher_type = 'Stock Entry'``. Every other voucher is out of reach of that
mechanism -- a Sales Invoice SLE's ``voucher_detail_no`` points at a Sales Invoice Item, so
the join matches nothing even with the filter removed, and there is no ``to_`` column to
copy from. The patch therefore reports 0 rows and succeeds while leaving these NULLs in
place. This is the second half, not a fix to the first.

``StockLedgerEntry.validate_serial_no_inventory_dimension`` (erpnext#58394, backported as
#58419, arrived with erpnext 16.34.1) compares an outward serialized SLE against that
serial's LAST INWARD SLE, and that lookup has NO voucher_type filter
(stock_ledger_entry.py:239-248). So a serial whose last inward leg was a sales return, a
Purchase Receipt or a Stock Reconciliation is un-issuable::

    Serial No X is not available in the selected inventory dimensions:
    Inventory Type: expected "Not Set", got "Regular Stock"

``doc_events/inventory_dimension.set_sales_inventory_type`` stops Sales Invoice and
Delivery Note creating new NULLs. It cannot fix old ones -- the validator looks backwards
at an inward row that is already written -- so this patch repairs history.

The value comes from the row's BATCH (``Batch.custom_inventory_type`` /
``custom_customer``), resolved through ``Stock Ledger Entry.batch_no`` or, when the row
carries its batch in a bundle instead, through a Serial and Batch Bundle that maps to
exactly ONE batch. That is the same source of truth ``CustomStockEntry.update_batches``
uses to fill a Stock Entry row's lane, so a row repaired here agrees with the outward
Stock Entry row that later moves the same stock -- which is the comparison the validator
actually makes. Rows with no resolvable batch fall back to
``DEFAULT_INVENTORY_TYPE``, because that is precisely what a future outward leg will
carry: the blanket default in ``doc_events/stock_entry.before_validate``.

``normalize_ownership`` is applied to every resolved pair, so the two dimensions stay
coherent -- a non-customer lane never keeps a customer, and a customer lane with no
resolvable customer downgrades instead of writing a pair that would trip the Customer
Goods guard. A customer is never invented; it is only ever read off the batch.

Safe to write submitted rows, on the same grounds as the Stock Entry patch: this column
feeds no valuation, qty or reposting logic, and it is skipped entirely for any dimension
configured ``validate_negative_stock`` (which would make the column load-bearing for
``validate_inventory_dimension_negative_stock``). ``SUM(stock_value) FROM tabBin`` must be
bit-identical before and after. ``update_modified`` is deliberately not touched so this
correction does not look like a business edit.

Outward rows are left alone on purpose. The validator only ever inspects the LAST INWARD
row, and a submitted outward SLE is never re-validated, so rewriting them would be a large
write with no effect on the failure.

Can also be run ad-hoc::

    bench --site <site> execute jewellery_erpnext.patches.backfill_non_stock_entry_inventory_dimensions.execute

Idempotent: the scan skips rows that already hold a value, so a second run reports 0.
"""

import re

import frappe

from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	normalize_ownership,
)

CHUNK = 5000
PAGE = 20000

# The only dimensions a Batch can answer for. A dimension outside this set has no
# batch-side source of truth, so it is left alone rather than guessed at.
BATCH_SOURCED = {
	"inventory_type": "custom_inventory_type",
	"customer": "custom_customer",
}

# Target fieldnames come from the user-editable Inventory Dimension doctype and are
# interpolated into SQL identifiers below, so refuse anything that is not a plain
# identifier -- same guard as the Stock Entry patch.
_SAFE_FIELD = re.compile(r"^[a-z_][a-z0-9_]*$")

# Child doctype names reach SQL as table identifiers the same way, and unlike fieldnames
# they legitimately contain spaces.
_SAFE_DOCTYPE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]*$")


def _dimension_fields():
	"""``{source_fieldname: sle_fieldname}`` for the batch-sourced, materialised dimensions."""
	from erpnext.stock.doctype.inventory_dimension.inventory_dimension import (
		get_inventory_dimensions,
	)

	sle_meta = frappe.get_meta("Stock Ledger Entry")

	fields = {}
	for dimension in get_inventory_dimensions():
		source = dimension.get("source_fieldname")
		sle_field = dimension.get("fieldname")
		if source not in BATCH_SOURCED or not sle_field:
			continue

		if dimension.get("validate_negative_stock"):
			# The column would then feed validate_inventory_dimension_negative_stock,
			# which turns a cosmetic correction into a ledger-affecting one.
			frappe.logger().warning(
				f"{__name__}: skipping {dimension.get('dimension_name')} -- "
				"validate_negative_stock is set"
			)
			continue

		if not _SAFE_FIELD.match(sle_field):
			frappe.logger().warning(
				f"{__name__}: skipping unsafe fieldname on {dimension.get('dimension_name')}"
			)
			continue

		if not sle_meta.has_field(sle_field):
			continue

		fields[source] = sle_field

	return fields


def _bundle_batches(bundles):
	"""``{bundle: batch_no}`` for bundles resolving to exactly ONE batch."""
	bundles = sorted({b for b in bundles if b})
	if not bundles:
		return {}

	by_bundle = {}
	for start in range(0, len(bundles), CHUNK):
		for entry in frappe.get_all(
			"Serial and Batch Entry",
			filters={
				"parent": ("in", bundles[start : start + CHUNK]),
				"batch_no": ("is", "set"),
			},
			fields=["parent", "batch_no"],
			limit_page_length=0,
		):
			by_bundle.setdefault(entry.parent, set()).add(entry.batch_no)

	return {
		bundle: next(iter(batches))
		for bundle, batches in by_bundle.items()
		if len(batches) == 1
	}


def _batch_ownership(batch_nos):
	"""``{batch_no: (custom_inventory_type, custom_customer)}``."""
	batch_nos = sorted({b for b in batch_nos if b})
	if not batch_nos:
		return {}

	ownership = {}
	for start in range(0, len(batch_nos), CHUNK):
		for row in frappe.get_all(
			"Batch",
			filters={"name": ("in", batch_nos[start : start + CHUNK])},
			fields=["name", "custom_inventory_type", "custom_customer"],
			limit_page_length=0,
		):
			ownership[row.name] = (row.custom_inventory_type, row.custom_customer)

	return ownership


def _apply(table, field, rows):
	"""Bulk-write ``field``, grouped by value so this stays a few statements."""
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


def _fetch_page(sle_field, last_name):
	"""One keyset page of inward, non-Stock-Entry SLEs still missing ``sle_field``."""
	return frappe.db.sql(
		f"""
		SELECT name, batch_no, serial_and_batch_bundle, item_code
		FROM `tabStock Ledger Entry`
		WHERE voucher_type != 'Stock Entry'
		  AND actual_qty > 0
		  AND is_cancelled = 0
		  AND (`{sle_field}` IS NULL OR `{sle_field}` = '')
		  AND name > %s
		ORDER BY name
		LIMIT {PAGE}
		""",
		(last_name,),
		as_dict=True,
	)


def _source_child_doctypes():
	"""``{voucher_type: child_doctype}`` for the vouchers this patch repaired.

	Read off each voucher's ``items`` field rather than assumed to be
	``f"{voucher_type} Item"``, so a voucher whose child table is named differently is
	handled rather than silently skipped.
	"""
	voucher_types = [
		r[0]
		for r in frappe.db.sql(
			"""
			SELECT DISTINCT voucher_type
			FROM `tabStock Ledger Entry`
			WHERE voucher_type != 'Stock Entry' AND actual_qty > 0 AND is_cancelled = 0
			"""
		)
	]

	mapping = {}
	for voucher_type in voucher_types:
		if not frappe.db.exists("DocType", voucher_type):
			continue

		field = frappe.get_meta(voucher_type).get_field("items")
		child = field.options if field else None
		if not child or not _SAFE_DOCTYPE.match(child):
			continue

		mapping[voucher_type] = child

	return mapping


def _repair_source_rows(fields):
	"""Copy the repaired LEDGER value back onto the SOURCE row.

	Not cosmetic, and the reason this patch has two passes at all. ``get_sl_entries``
	re-derives a voucher's dimensions from the persisted child row, and it is the SAME
	function that runs on cancel (``"is_cancelled": 1 if self.docstatus == 2``,
	controllers/stock_controller.py:1191). So a repaired ledger row sitting above a still
	blank source row would, on cancel, emit a reversal tagged NULL against an original
	tagged "Regular Stock" -- a dimension bucket that never balances. The Stock Entry twin
	guards the identical hazard with its own first pass.

	Copies FROM the ledger rather than re-deriving from the batch, so this pass is
	independent of the ledger pass and can be re-run on a site that has already migrated
	(where the ledger scan now correctly finds nothing left to do).
	"""
	totals = dict.fromkeys(fields, 0)

	for voucher_type, child in _source_child_doctypes().items():
		child_meta = frappe.get_meta(child)

		for source, sle_field in fields.items():
			if not child_meta.has_field(source):
				continue

			rows = frappe.db.sql(
				f"""
				SELECT child.name AS name, sle.`{sle_field}` AS value
				FROM `tab{child}` child
				INNER JOIN `tabStock Ledger Entry` sle
				        ON sle.voucher_detail_no = child.name
				WHERE sle.voucher_type = %s
				  AND sle.actual_qty > 0
				  AND sle.is_cancelled = 0
				  AND sle.`{sle_field}` IS NOT NULL AND sle.`{sle_field}` != ''
				  AND (child.`{source}` IS NULL OR child.`{source}` = '')
				""",
				(voucher_type,),
				as_dict=True,
			)

			# One child row can back more than one ledger row (an internal transfer books
			# both legs against it). They carry the same lane, so collapse to the first
			# rather than writing the row twice.
			deduped = {}
			for row in rows:
				deduped.setdefault(row.name, row.value)

			totals[source] += _apply(f"tab{child}", source, list(deduped.items()))

	return totals


def execute():
	fields = _dimension_fields()
	if not fields:
		frappe.logger().info(
			f"{__name__}: no batch-sourced inventory dimensions registered, nothing to do"
		)
		return

	# Drive the scan off inventory_type when it is registered: it is the dimension the
	# serial validator rejects on. ``customer`` is written from the same resolved pair
	# rather than scanned for separately, so a row that somehow holds an inventory_type
	# but no customer is NOT visited. That gap is empty in practice -- nothing on a
	# non-Stock-Entry row writes one without the other -- and widening the scan would
	# mean re-reading every inward row on every future run for no repair.
	scan_source = "inventory_type" if "inventory_type" in fields else next(iter(fields))
	scan_field = fields[scan_source]

	total_scanned = 0
	totals = dict.fromkeys(fields, 0)
	last_name = ""

	while True:
		page = _fetch_page(scan_field, last_name)
		if not page:
			break

		last_name = page[-1]["name"]
		total_scanned += len(page)

		bundle_batches = _bundle_batches(
			row.serial_and_batch_bundle for row in page if not row.batch_no
		)
		ownership = _batch_ownership(
			[row.batch_no for row in page] + list(bundle_batches.values())
		)

		resolved = {source: [] for source in fields}
		for row in page:
			batch_no = row.batch_no or bundle_batches.get(row.serial_and_batch_bundle)
			batch_inventory_type, batch_customer = ownership.get(batch_no) or (
				None,
				None,
			)

			inventory_type, customer = normalize_ownership(
				batch_inventory_type,
				batch_customer,
				batch_no=batch_no,
				item_code=row.item_code,
			)

			values = {"inventory_type": inventory_type, "customer": customer}
			for source in fields:
				# Never write a blank over a blank -- that is a no-op that would make the
				# run look like it did work, and would break idempotency reporting.
				if values.get(source):
					resolved[source].append((row.name, values[source]))

		for source, sle_field in fields.items():
			totals[source] += _apply(
				"tabStock Ledger Entry", sle_field, resolved[source]
			)

	# Pass 2 -- push the repaired ledger value back down onto the source rows, so a later
	# cancel re-derives the same lane instead of a NULL. Runs unconditionally: on a site
	# that already migrated, pass 1 legitimately finds nothing while the source rows it
	# wrote for are still blank.
	source_totals = _repair_source_rows(fields)

	frappe.logger().info(
		f"{__name__}: scanned {total_scanned} inward non-Stock-Entry rows; "
		+ "; ".join(
			f"{source} -- ledger updated {count}, source rows updated "
			f"{source_totals.get(source, 0)}"
			for source, count in totals.items()
		)
	)
