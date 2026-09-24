# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Provenance of the batches a Diamond Conversion produces.

A ``Sieve Size to Sieve Size`` Diamond Conversion may not re-consume a batch that a Diamond
Conversion produced. That rule is enforced in three places -- the source batch picker
(``diamond_conversion.get_source_batches``), the FIFO auto-pick
(``se_utils.get_fifo_batches``) and ``DiamondConversion.validate`` -- so the lookup lives in a
leaf module all three can import without a cycle, exactly like ``sample_goods.py``.
``diamond_conversion.py`` already imports ``se_utils``, so this cannot live in either of them.

WHY TWO PROVENANCE PATHS
------------------------
``make_diamond_stock_entry`` leaves the target rows with no ``batch_no``; the batch is minted at
Stock Entry submit by ``custom_create_batch``
(``customization/serial_and_batch_bundle/doc_events/utils.py``), which stamps
``reference_doctype="Stock Entry"``, ``reference_name=<SE>`` and
``custom_voucher_detail_no=<Stock Entry Detail row>``. ERPNext NULLs ``reference_name`` and
``reference_doctype`` when that Stock Entry is cancelled
(``serial_and_batch_bundle.py`` ``set_batch_no_in_serial_and_batch_bundle``) but leaves
``custom_voucher_detail_no`` alone -- 940 batches on the live bench have already lost
``reference_name`` while keeping the detail row. So the detail row is the PRIMARY path and
``reference_name`` is the fallback; today both resolve the same 125 conversion-produced batches.

No ``docstatus`` filter: provenance should survive cancellation, and a cancelled Stock Entry's
batch has no stock so it never reaches the picker or FIFO anyway -- only a hand-typed batch can,
and barring that is still correct.

QUERY SHAPE
-----------
Three queries keyed on PRIMARY, never a scan. ``tabBatch`` carries only ``PRIMARY(name)``,
``batch_id`` and ``creation`` -- there is no index on ``item``, ``reference_name`` or
``custom_voucher_detail_no``, and ``tabStock Entry`` has none on ``custom_diamond_conversion``.
So the lookup is ALWAYS driven from a caller-supplied bounded batch list; driving it from
``Batch.item`` instead would full-scan the whole table on every save.

``frappe.db.get_all`` (not ``frappe.get_all``) is used throughout on purpose: it is a thin
delegating staticmethod that frappe internals never call, so a test may patch it without
hijacking DocType meta loading.
"""

import frappe

STOCK_ENTRY = "Stock Entry"


def get_diamond_conversion_target_batches(batch_nos):
	"""``{batch_no: diamond_conversion}`` for batches minted as a Diamond Conversion TARGET.

	Only conversion output appears in the result, so a caller can test membership
	(``batch in result``) and still have the producing document's name for the message.
	Returns ``{}`` for an empty / all-None input WITHOUT querying, so the FIFO and picker paths
	pay nothing when there is nothing to check.
	"""
	batch_nos = sorted({batch for batch in batch_nos or [] if batch})
	if not batch_nos:
		return {}

	batches = frappe.db.get_all(
		"Batch",
		filters={"name": ["in", batch_nos]},
		fields=[
			"name",
			"reference_doctype",
			"reference_name",
			"custom_voucher_detail_no",
		],
	)
	if not batches:
		return {}

	detail_names = sorted(
		{
			batch.custom_voucher_detail_no
			for batch in batches
			if batch.custom_voucher_detail_no
		}
	)
	detail_parent = {}
	if detail_names:
		detail_parent = {
			row.name: row.parent
			for row in frappe.db.get_all(
				"Stock Entry Detail",
				filters={"name": ["in", detail_names]},
				fields=["name", "parent"],
			)
		}

	se_names = set(detail_parent.values())
	se_names.update(
		batch.reference_name
		for batch in batches
		if batch.reference_doctype == STOCK_ENTRY and batch.reference_name
	)
	if not se_names:
		return {}

	conversion_by_se = {
		se.name: se.custom_diamond_conversion
		for se in frappe.db.get_all(
			STOCK_ENTRY,
			filters={
				"name": ["in", sorted(se_names)],
				"custom_diamond_conversion": ["is", "set"],
			},
			fields=["name", "custom_diamond_conversion"],
		)
	}
	if not conversion_by_se:
		return {}

	conversion_by_batch = {}
	for batch in batches:
		se_name = detail_parent.get(batch.custom_voucher_detail_no)
		if not se_name and batch.reference_doctype == STOCK_ENTRY:
			se_name = batch.reference_name
		conversion = conversion_by_se.get(se_name)
		if conversion:
			conversion_by_batch[batch.name] = conversion

	return conversion_by_batch
