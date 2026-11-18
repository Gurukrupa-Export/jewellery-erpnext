# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Write the Customer Gold narration onto postings made before it existed. Text only.

Receipts and returns posted with an empty ``Stock Entry.remarks`` (GL read "Accounting Entry for
Stock"); settlement JEs posted with an empty GL remark, their note sitting in the header
``user_remark`` erpnext never copies to GL. This writes the same text a new posting now gets
(``customer_gold_narration``) onto the document and onto its live GL lines.

No amount, account, status or ``modified`` changes, no new document. An existing settlement JE
keeps its single liability line -- a submitted JE cannot be split -- so its receipt lines go into
the one remark under the summary. Safe to run again: unchanged text is skipped.
"""

import frappe


def execute():
	if not frappe.db.table_exists("Customer Gold Ledger Entry"):
		return

	from jewellery_erpnext.customer_subcontracting.customer_gold_narration import (
		backfill_settlements,
		backfill_stock_entries,
	)
	from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
		get_customer_gold_settings,
	)

	stock_entries = backfill_stock_entries(get_customer_gold_settings())
	settlements = backfill_settlements()
	print(
		f"Customer Gold narration: {len(stock_entries)} Stock Entries, "
		f"{len(settlements)} settlement Journal Entries"
	)
