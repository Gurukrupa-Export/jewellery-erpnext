"""Re-stamp Repack-Metal Conversion target batches with their ledger rate (F26).

Until F26, ``batch.on_update`` restated every conversion target's Batch Rate on each
provenance save: a qty-weighted, purity-scaled mix of the source batches' rates. It
overwrote the rate the batch was minted with -- the target row's ledger incoming rate, which
is what batch-wise valuation charges on every later issue. On the 22KT batch of the
KLHGX62F1119 audit the mix read 144,648.4625 against a ledger rate of 144,642.733945.

The blend is retired. This script re-stamps batches it already restated: for each conversion
target, the Batch Rate becomes the incoming rate of the ledger row that minted it (the
``Serial and Batch Entry`` of the target row's Stock Ledger Entry, else that entry's own
``incoming_rate``). The rate field is the one the minting stamp uses
(``_rate_field_for_item``: ``custom_alloy_rate`` for an alloy item, else ``custom_metal_rate``).
``patches/backfill_blended_batch_metal_rate`` cannot do this: it only selects batches at rate 0,
and these hold a non-zero wrong rate.

A second pass updates the ``Stock Entry Detail.custom_metal_rate`` mirror. That field is
``fetch_from: batch_no.custom_metal_rate`` and a submitted row never re-saves, so without it the
costing readers (``manufacturing_operation._snc_se_detail_maps``, ``get_stock_entry_data``) keep
the blended rate. Only rows whose mirror still equals the old Batch Rate are touched.

Listed for REVIEW and left alone:

* a target row whose run produces more than one row. ERPNext pools the ledger rate across such
  a run (``loss_valuation.set_process_loss_produce_rates`` leaves it to ERPNext on purpose), so the
  ledger rate is not the batch's own and needs the separate multi-output pricing fix;
* a target with no ledger row.

Customer-owned batches are reported separately. Under the Zero Value policy their ledger rate
is 0 or the company alloy's value alone, so the re-stamp moves them off the purity-scaled
customer rate the blend gave them. That is the F26 decision (the customer's booked value lives
in the Customer Gold Ledger, and sales pricing already forces customer items to 0), but it is
the change most worth reading in the dry run.

NO STOCK OR ACCOUNTING IS TOUCHED: no Stock Ledger Entry, GL Entry, Bin or ``basic_rate``. FG
BOMs already costed from a blended rate keep their stored costs; recomputing them is a separate,
business-approved operation.

NOT registered in patches.txt: it changes historical figures, so it runs by hand in the
controlled remediation phase. It is a dry run unless told otherwise:

    bench --site <site> execute jewellery_erpnext.patches.restamp_batch_rate_from_ledger.execute

    bench --site <site> execute jewellery_erpnext.patches.restamp_batch_rate_from_ledger.execute \\
        --kwargs "{'dry_run': False}"

Scope to specific batches with ``'batches': ['...']``. Safe to re-run: a re-stamped batch
matches its ledger rate and is not selected again.
"""

import frappe
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.batch.doc_events.utils import (
	_rate_field_for_item,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.loss_valuation import (
	iter_loss_runs,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	METAL_CONVERSION_SE_TYPE,
)

TOLERANCE = 0.001
CUSTOMER_INVENTORY_TYPES = ("Customer Goods", "Customer Stock")


def _conversion_target_batches(batches=None):
	"""Every batch minted by the target row of a submitted Repack-Metal Conversion."""
	condition = "AND b.name IN %(batches)s" if batches else ""
	return frappe.db.sql(
		f"""
		SELECT
			b.name, b.item, b.custom_metal_rate, b.custom_alloy_rate,
			b.custom_inventory_type, b.custom_customer,
			b.reference_name AS stock_entry, b.custom_voucher_detail_no AS row_name
		FROM `tabBatch` b
		INNER JOIN `tabStock Entry` se ON se.name = b.reference_name
		WHERE b.reference_doctype = 'Stock Entry'
			AND se.stock_entry_type = %(se_type)s
			AND se.docstatus = 1
			AND IFNULL(b.custom_voucher_detail_no, '') != ''
			{condition}
		ORDER BY b.creation
		""",
		{"se_type": METAL_CONVERSION_SE_TYPE, "batches": tuple(batches or ())},
		as_dict=True,
	)


def _ledger_rate(stock_entry, row_name, batch_no):
	"""The incoming rate the ledger booked for this batch on its minting row, or None."""
	rate = frappe.db.sql(
		"""
		SELECT sbe.incoming_rate
		FROM `tabStock Ledger Entry` sle
		INNER JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sle.serial_and_batch_bundle
		WHERE sle.voucher_type = 'Stock Entry' AND sle.voucher_no = %s
			AND sle.voucher_detail_no = %s AND sle.is_cancelled = 0 AND sle.actual_qty > 0
			AND sbe.batch_no = %s
		LIMIT 1
		""",
		(stock_entry, row_name, batch_no),
	)
	if rate:
		return flt(rate[0][0])
	rate = frappe.db.sql(
		"""
		SELECT incoming_rate
		FROM `tabStock Ledger Entry`
		WHERE voucher_type = 'Stock Entry' AND voucher_no = %s AND voucher_detail_no = %s
			AND is_cancelled = 0 AND actual_qty > 0
		LIMIT 1
		""",
		(stock_entry, row_name),
	)
	return flt(rate[0][0]) if rate else None


def _pooled_rows(stock_entry, cache):
	"""Names of the produce rows that share a run with another produce row."""
	if stock_entry not in cache:
		items = frappe.get_all(
			"Stock Entry Detail",
			filters={"parent": stock_entry, "parenttype": "Stock Entry"},
			fields=["name", "s_warehouse", "t_warehouse"],
			order_by="idx",
		)
		pooled = set()
		for _consumed, produced in iter_loss_runs(items):
			if len(produced) > 1:
				pooled.update(row.name for row in produced)
		cache[stock_entry] = pooled
	return cache[stock_entry]


def _alloy_items():
	"""The site's alloy classifier, exactly as the minting stamp reads it."""
	groups = frappe.db.get_all("Item Group", {"custom_is_alloy_group": 1}, pluck="name")
	return frappe.db.get_all(
		"Item",
		{"item_group": ["in", groups], "variant_of": ["in", ["M", "F"]]},
		pluck="name",
	)


def detect(batches=None):
	"""``(changes, review)`` over every conversion target in scope."""
	alloy_items = _alloy_items()
	pooled_cache = {}
	changes, review = [], []
	for batch in _conversion_target_batches(batches):
		if batch.row_name in _pooled_rows(batch.stock_entry, pooled_cache):
			review.append(
				{
					"batch": batch.name,
					"reason": "multi-output run: ledger rate is pooled",
				}
			)
			continue
		ledger = _ledger_rate(batch.stock_entry, batch.row_name, batch.name)
		if ledger is None:
			review.append(
				{"batch": batch.name, "reason": "no ledger row for the minting row"}
			)
			continue
		field = _rate_field_for_item(batch.item, alloy_items)
		before = flt(batch.get(field))
		if abs(before - ledger) <= TOLERANCE:
			continue
		changes.append(
			{
				"batch": batch.name,
				"field": field,
				"before": before,
				"after": ledger,
				"stock_entry": batch.stock_entry,
				"customer_owned": batch.custom_inventory_type
				in CUSTOMER_INVENTORY_TYPES,
				"customer": batch.custom_customer,
			}
		)
	return changes, review


def _mirror_rows(change):
	"""Stock Entry Detail rows whose fetched Batch Rate still holds the old value."""
	if change["field"] != "custom_metal_rate":
		return []
	return frappe.db.sql_list(
		"""
		SELECT name FROM `tabStock Entry Detail`
		WHERE batch_no = %s AND docstatus < 2 AND ABS(IFNULL(custom_metal_rate, 0) - %s) <= %s
		""",
		(change["batch"], change["before"], TOLERANCE),
	)


def execute(dry_run=True, batches=None):
	changes, review = detect(batches)
	for change in changes:
		change["mirror_rows"] = _mirror_rows(change)

	customer_owned = sum(1 for c in changes if c["customer_owned"])
	mirrors = sum(len(c["mirror_rows"]) for c in changes)
	print(
		f"[restamp-batch-rate] {len(changes)} batch(es) to re-stamp "
		f"({customer_owned} customer-owned), {mirrors} Stock Entry Detail mirror row(s), "
		f"{len(review)} needing review."
	)
	for c in changes:
		owner = f"customer {c['customer']}" if c["customer_owned"] else "company"
		print(
			f"  {c['batch']}  {c['field']}  {c['before']} -> {c['after']}  "
			f"({owner}; {c['stock_entry']}; {len(c['mirror_rows'])} mirror row(s))"
		)
	for r in review:
		print(f"  REVIEW {r['batch']}: {r['reason']}")

	result = {"changes": changes, "review": review}
	if dry_run:
		print("[restamp-batch-rate] DRY RUN — nothing written.")
		return result

	for c in changes:
		frappe.db.set_value(
			"Batch", c["batch"], c["field"], c["after"], update_modified=False
		)
		for row in c["mirror_rows"]:
			frappe.db.set_value(
				"Stock Entry Detail",
				row,
				"custom_metal_rate",
				c["after"],
				update_modified=False,
			)
	frappe.db.commit()

	print(
		f"[restamp-batch-rate] Re-stamped {len(changes)} batch(es), {mirrors} mirror row(s)."
	)
	return result
