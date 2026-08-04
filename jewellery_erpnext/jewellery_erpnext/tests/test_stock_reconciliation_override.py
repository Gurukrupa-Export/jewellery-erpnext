# Copyright (c) 2026, Gurukrupa Exports and Contributors
# See license.txt

"""Guards the Stock Reconciliation controller override.

`override_doctype_class` accepted a two-element list for "Stock Reconciliation".
Frappe resolves `class_overrides[doctype][-1]` (frappe/model/base_document.py:117)
and silently discards everything before it, so the first entry was never loaded —
and the entry that *was* loaded stubbed out `remove_items_with_no_change` with a
bare `return`.

That method is upstream's only writer of `difference_amount`,
`current_qty` and `current_valuation_rate`. Measured on the production dataset
before the fix: 27 of 29 submitted Stock Reconciliations carried
`difference_amount = 0`, and 922 of 2,450 submitted item rows carried
`current_valuation_rate = 0`.

The failure mode was completely silent — no exception, no log line — so these
tests assert the wiring itself, not just behaviour.
"""

import inspect

from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import (
	StockReconciliation,
)
from frappe.model.base_document import get_controller
from frappe.tests import UnitTestCase

from jewellery_erpnext.jewellery_erpnext.customization.stock_reconciliation.stock_reonciliation import (
	CustomStockReconciliation,
)


class TestStockReconciliationOverride(UnitTestCase):
	def test_controller_resolves_to_the_custom_class(self):
		"""The whole defect was that the intended class was not the loaded class."""
		self.assertIs(get_controller("Stock Reconciliation"), CustomStockReconciliation)

	def test_hook_is_a_single_path_not_a_list(self):
		"""A list here is not composition — it is a silently dropped override.

		This is the assertion that would have caught the original bug. If someone
		re-introduces a second class, Frappe keeps only the last and this fails.
		"""
		from jewellery_erpnext import hooks

		override = hooks.override_doctype_class["Stock Reconciliation"]
		self.assertIsInstance(
			override,
			str,
			"override_doctype_class['Stock Reconciliation'] must be a single dotted path. "
			"Frappe resolves class_overrides[doctype][-1], so a list drops every earlier "
			"entry. To combine behaviour from several places use the extend_doctype_class "
			"hook (v16+), which builds a real MRO.",
		)

	def test_mro_reaches_upstream_erpnext(self):
		self.assertIn(StockReconciliation, CustomStockReconciliation.__mro__)

	def test_remove_items_with_no_change_is_not_a_stub(self):
		"""It must override upstream (to keep MWO rows) but must still call super(),
		because super() is what computes difference_amount."""
		method = CustomStockReconciliation.remove_items_with_no_change
		self.assertIsNot(
			method,
			StockReconciliation.remove_items_with_no_change,
			"the custom lane behaviour is expected to remain",
		)
		source = inspect.getsource(method)
		self.assertIn(
			"super().remove_items_with_no_change()",
			source,
			"must delegate to upstream — it is the only writer of difference_amount, "
			"current_qty and current_valuation_rate",
		)
		body = source.split('"""')[-1]
		self.assertNotRegex(
			body.strip(),
			r"^return\s*$",
			"a bare `return` silently disables difference_amount computation",
		)

	def test_dead_controller_class_is_gone(self):
		"""The never-loaded duplicate must not come back."""
		from jewellery_erpnext.jewellery_erpnext.doctype.stock_reconciliation_template import (
			stock_reconciliation_template_utils as utils,
		)

		self.assertFalse(
			hasattr(utils, "CustomStockReconciliation"),
			"a second Stock Reconciliation controller in this module was dead code — "
			"it was never loaded because it sat before the last list entry",
		)

	def test_no_doctype_override_is_declared_as_a_list(self):
		"""Generalises the guard to every override this app registers."""
		from jewellery_erpnext import hooks

		listed = {
			doctype: value
			for doctype, value in hooks.override_doctype_class.items()
			if isinstance(value, list)
		}
		self.assertEqual(
			listed,
			{},
			f"these overrides are lists and all but the last entry is silently dropped: {listed}",
		)
