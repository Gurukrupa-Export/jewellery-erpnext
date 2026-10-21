# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Unit tests for ``jewellery_erpnext.patches.reshard_gurukrupa_stock_entry_naming``.

This gated patch selects Document Naming Rules by inspecting their conditions, and both of its
selectors previously discarded the condition OPERATOR — `_target_rules` fetched it and ignored
it, `_target_rules_enabled` did not even select it. A rule conditioned ``company != Gurukrupa``
carries the same field and value as one conditioned ``company = Gurukrupa``, so it was treated
as binding TO the company. That inverts the rule's meaning, and both callers act on it:
``reshard()`` would ENABLE a rule that excludes the company, ``rollback()`` would DISABLE it.

Same defect class as the one fixed in ``shard_stock_entry_naming_by_type._coverage``.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.patches import (
	reshard_gurukrupa_stock_entry_naming as reshard_mod,
)

_COMPANY = "Gurukrupa Export Private Limited"


def _cond(field, condition, value):
	return frappe._dict(field=field, condition=condition, value=value)


class TestBindsToCompany(IntegrationTestCase):
	"""The predicate both selectors share."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_equals_binds(self):
		self.assertTrue(
			reshard_mod._binds_to_company([_cond("company", "=", _COMPANY)], _COMPANY)
		)

	def test_not_equals_does_NOT_bind(self):
		# The exact inversion: same field, same value, opposite meaning.
		self.assertFalse(
			reshard_mod._binds_to_company([_cond("company", "!=", _COMPANY)], _COMPANY)
		)

	def test_other_comparison_operators_do_NOT_bind(self):
		for op in (">", "<", ">=", "<="):
			with self.subTest(op=op):
				self.assertFalse(
					reshard_mod._binds_to_company(
						[_cond("company", op, _COMPANY)], _COMPANY
					)
				)

	def test_a_missing_operator_does_NOT_bind(self):
		# `_target_rules_enabled` used to omit `condition` from its field list entirely, so the
		# key was absent. Absent must read as "cannot prove it binds", never as "=".
		self.assertFalse(
			reshard_mod._binds_to_company(
				[frappe._dict(field="company", value=_COMPANY)], _COMPANY
			)
		)

	def test_a_different_company_does_NOT_bind(self):
		self.assertFalse(
			reshard_mod._binds_to_company(
				[_cond("company", "=", "KG GK Jewellers Private Limited")], _COMPANY
			)
		)

	def test_a_non_company_field_does_NOT_bind(self):
		self.assertFalse(
			reshard_mod._binds_to_company(
				[_cond("stock_entry_type", "=", _COMPANY)], _COMPANY
			)
		)

	def test_binds_when_the_company_row_sits_among_others(self):
		conds = [
			_cond("stock_entry_type", "=", "Repack"),
			_cond("company", "=", _COMPANY),
		]
		self.assertTrue(reshard_mod._binds_to_company(conds, _COMPANY))

	def tearDown(self):
		return super().tearDown()


class TestTargetRulesRespectsTheOperator(IntegrationTestCase):
	"""Both selectors must route through the predicate, and must select `condition`."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, fn, rule_fields, conds):
		with patch.object(
			reshard_mod.frappe,
			"get_all",
			side_effect=[[frappe._dict(rule_fields)], conds],
		) as mock_get_all:
			out = fn(_COMPANY)
		return out, mock_get_all

	def test_disabled_selector_skips_a_not_equals_rule(self):
		out, _ = self._run(
			reshard_mod._target_rules,
			{
				"name": "R1",
				"prefix": "GE-SE-X-.YY.-",
				"prefix_digits": 5,
				"counter": 0,
				"priority": 0,
			},
			[_cond("company", "!=", _COMPANY)],
		)
		self.assertEqual(out, [], "a `company != X` rule must never be re-enabled")

	def test_disabled_selector_keeps_an_equals_rule(self):
		out, _ = self._run(
			reshard_mod._target_rules,
			{
				"name": "R1",
				"prefix": "GE-SE-X-.YY.-",
				"prefix_digits": 5,
				"counter": 0,
				"priority": 0,
			},
			[_cond("company", "=", _COMPANY)],
		)
		self.assertEqual([r.name for r, _c in out], ["R1"])

	def test_enabled_selector_skips_a_not_equals_rule(self):
		out, _ = self._run(
			reshard_mod._target_rules_enabled,
			{"name": "R1", "prefix": "GE-SE-X-.YY.-"},
			[_cond("company", "!=", _COMPANY)],
		)
		self.assertEqual(
			out, [], "a `company != X` rule must never be disabled by rollback"
		)

	def test_enabled_selector_now_selects_the_condition_field(self):
		# Regression guard: without `condition` in the field list the operator is invisible and
		# the predicate silently degrades to matching every rule mentioning the company.
		_out, mock_get_all = self._run(
			reshard_mod._target_rules_enabled,
			{"name": "R1", "prefix": "GE-SE-X-.YY.-"},
			[_cond("company", "=", _COMPANY)],
		)
		cond_call = mock_get_all.call_args_list[1]
		self.assertIn("condition", cond_call.kwargs["fields"])

	def tearDown(self):
		return super().tearDown()
