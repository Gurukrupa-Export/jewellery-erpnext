import frappe

from jewellery_erpnext.patches.add_stock_entry_idempotency_indexes import (
	_add_index_if_missing,
)


def execute():
	# ERPNext's SerialAndBatchBundle.check_future_entries_exists runs once per
	# serial/batch SLE on every stock submit and filters bundles by item_code and
	# posting_datetime; without this index it scanned all of tabSerial and Batch
	# Entry each time (~0.5s per row, ~2.5 min of a 300-row Delivery Note submit).
	_add_index_if_missing(
		"tabSerial and Batch Bundle",
		"item_code_posting_datetime_index",
		("item_code", "posting_datetime"),
	)
	frappe.logger().info(
		"add_serial_and_batch_bundle_item_index: ensured item_code_posting_datetime_index"
	)
