# Copyright (c) 2026, Nirali and contributors
# See license.txt
#
# DB-free per the suite convention: setUpClass is neutralised and frappe.qb is
# mocked. get_manual_loss_items backs the Manually Book Loss Details "Item Code"
# dropdown; the one piece of custom control flow is the empty-work-order guard
# (returns [] before building any query). The query-shape itself uses frappe.qb,
# so only the branch behaviour and result passthrough are asserted here.

from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import filters


class _QB:
	"""Minimal chainable stand-in for a frappe.qb builder/table.

	Every attribute access, call, item lookup and operator returns ``self`` so
	the fluent chain ``from_(...).select(...).where(...).orderby(...).run()`` can
	be exercised without a database; ``run()`` yields a configured sentinel.
	"""

	def __init__(self, run_result=None):
		self._run_result = run_result

	def __getattr__(self, name):
		return self

	def __call__(self, *args, **kwargs):
		return self

	def __getitem__(self, key):
		return self

	def __and__(self, other):
		return self

	def __eq__(self, other):
		return self

	__hash__ = None

	def run(self, *args, **kwargs):
		return self._run_result


def _call(**overrides):
	# doctype=None makes the @validate_and_sanitize_search_inputs decorator skip
	# its db.exists("DocType", ...) check, keeping the call DB-free.
	kwargs = {
		"doctype": None,
		"txt": "",
		"searchfield": "item_code",
		"start": 0,
		"page_len": 20,
		"filters": {},
	}
	kwargs.update(overrides)
	return filters.get_manual_loss_items(**kwargs)


class TestGetManualLossItems(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_no_work_order_returns_empty_without_building_a_query(self):
		# The dropdown is intentionally empty until a work order is chosen: the
		# function must return [] before touching the query builder.
		with patch.object(filters.frappe, "qb") as qb:
			result = _call(filters={})
		self.assertEqual(result, [])
		qb.DocType.assert_not_called()

	def test_blank_work_order_string_is_treated_as_absent(self):
		with patch.object(filters.frappe, "qb") as qb:
			result = _call(filters={"manufacturing_work_order": ""})
		self.assertEqual(result, [])
		qb.DocType.assert_not_called()

	def test_work_order_present_reads_mop_log_and_passes_result_through(self):
		sentinel = [["ITEM-A"], ["ITEM-B"]]
		qb = MagicMock()
		qb.DocType.return_value = _QB()  # ML
		qb.from_.return_value = _QB(run_result=sentinel)  # the query builder
		with patch.object(filters.frappe, "qb", qb):
			result = _call(filters={"manufacturing_work_order": "MWO-1"})
		# It reads from MOP Log (the same ledger get_batch_details uses) and
		# returns exactly what query.run() produced.
		qb.DocType.assert_called_once_with("MOP Log")
		self.assertEqual(result, sentinel)

	def test_optional_operation_filter_still_returns_query_result(self):
		# Supplying manufacturing_operation only adds a narrowing where-clause; the
		# work-order query is still built and its result returned.
		sentinel = [["ITEM-A"]]
		qb = MagicMock()
		qb.DocType.return_value = _QB()
		qb.from_.return_value = _QB(run_result=sentinel)
		with patch.object(filters.frappe, "qb", qb):
			result = _call(
				filters={
					"manufacturing_work_order": "MWO-1",
					"manufacturing_operation": "MOP-1",
				}
			)
		self.assertEqual(result, sentinel)
