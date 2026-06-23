# Copyright (c) 2026, Aerele and Contributors
# See license.txt

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.fetch_from_guard import (
	ensure_fetch_from_columns,
	get_custom_fetch_targets,
)


class TestFetchFromColumns(IntegrationTestCase):
	"""Regression guard for the whole class of `fetch_from` -> missing custom_* column crashes.

	Frappe resolves every Link field's `fetch_from` on each save/submit; a missing target
	column raises 1054 ("Unknown column ...") and aborts the save -- this is what broke MWO
	submit via `manufacturing_order.custom_jewelex_batch_no`. The invariant below fails loudly
	if any app `fetch_from` ever targets an unprovisioned custom_* column again.
	"""

	def test_guard_provisions_all_custom_fetch_targets(self):
		# Idempotent: a no-op once every column exists.
		ensure_fetch_from_columns()

		missing = [
			f"{source_dt}.{link_field} -> {target_dt}.{target_column}"
			for source_dt, link_field, target_dt, target_column in get_custom_fetch_targets()
			if not frappe.db.has_column(target_dt, target_column)
		]

		self.assertFalse(
			missing,
			"fetch_from targets a custom_* column that does not exist on its target doctype "
			"(would raise 1054 on save/submit): " + ", ".join(missing),
		)

	def test_reported_pmo_jewelex_batch_no_column(self):
		ensure_fetch_from_columns()
		self.assertTrue(
			frappe.db.has_column(
				"Parent Manufacturing Order", "custom_jewelex_batch_no"
			),
			"Parent Manufacturing Order.custom_jewelex_batch_no is missing -- "
			"MWO.jewelex_batch_no fetch_from would crash on submit",
		)
