"""
Guarantee every custom_* column targeted by a jewellery_erpnext `fetch_from` exists.

A `fetch_from` pointing at a missing column hard-fails link validation on the source
doctype's save/submit (1054 Unknown column) -- e.g. Manufacturing Work Order's
`jewelex_batch_no` -> `manufacturing_order.custom_jewelex_batch_no`. This app's
custom_fields/*.json are not synced on migrate (after_migrate hook disabled), so such
columns only exist if a patch creates them. This patch closes the whole class generically
and self-heals the doc-present/column-absent case. Idempotent.
"""

from jewellery_erpnext.fetch_from_guard import ensure_fetch_from_columns


def execute():
	ensure_fetch_from_columns()
