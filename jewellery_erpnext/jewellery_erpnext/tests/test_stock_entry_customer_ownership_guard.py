# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""F5 enforcement -- a consuming row must draw the owner's material its order was placed on.

``validate_inventory_dimention`` was commented out by 0040324c ("fix: sync v15 updates with v16",
8 May 2026), a 17-file bulk sync that gave no reason, and stayed dead. With nothing checking, a
manually built Stock Entry on a customer-diamond order could consume company stock, or another
customer's stock, and say nothing.

It now reads the BATCH rather than the row. The old version compared ``row.customer`` against the
order's, which is the row's own claim about itself -- and the claim is exactly what goes wrong:
KLHGX62F1119's company diamond travelled as "Customer Goods" with a NULL customer through
MAT-STE-18637/38/39 while the batch it drew was plain Regular Stock.

Pure-logic: fake documents, the batch and order reads stubbed, nothing written.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events import (
	se_utils,
)

CUSTOMER = "GJCU0009"
OTHER_CUSTOMER = "GJCU0010"
PMO = "PMO-KGJPL-EA00978-010-0027"

CUSTOMER_BATCH = "B-CUST-DIA"
OTHER_BATCH = "B-OTHER-DIA"
COMPANY_BATCH = "B-CO-DIA"

BATCHES = {
	CUSTOMER_BATCH: {
		"custom_inventory_type": "Customer Goods",
		"custom_customer": CUSTOMER,
	},
	OTHER_BATCH: {
		"custom_inventory_type": "Customer Goods",
		"custom_customer": OTHER_CUSTOMER,
	},
	COMPANY_BATCH: {"custom_inventory_type": "Regular Stock", "custom_customer": None},
}


class _Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)


def _row(batch_no, **kwargs):
	fields = {
		"idx": 1,
		"item_code": "D-NT-RO-AA001-+9.5-10",
		"s_warehouse": "WH-DIA",
		"t_warehouse": "WH-RESERVE",
		"batch_no": batch_no,
		"inventory_type": None,
		"customer": None,
		"custom_parent_manufacturing_order": PMO,
		"custom_variant_of": None,
	}
	fields.update(kwargs)
	return _Doc(**fields)


class _Case(unittest.TestCase):
	#: The order under test. Default: the customer supplied the diamonds, nothing else.
	order = frappe._dict(
		is_customer_gold=0,
		is_customer_diamond=1,
		is_customer_gemstone=0,
		is_customer_material=0,
		customer=CUSTOMER,
		manufacturer="MFR",
	)
	allow_substitution = 0
	variant_of = "D"

	def _validate(self, rows, warn_only=False, auto_created=0, header_pmo=None):
		"""Run the guard, returning every mismatch it let through (logged, never shown)."""
		se = _Doc(items=rows, manufacturing_order=header_pmo, auto_created=auto_created)

		def get_value(doctype, name, *args, **kwargs):
			if doctype == "Parent Manufacturing Order":
				return self.order
			if doctype == "Manufacturer":
				return self.allow_substitution
			if doctype == "Batch":
				return frappe._dict(BATCHES.get(name) or {})
			return None

		warnings = []
		def bulk_map(doctype, names, fields):
			if doctype == "Batch":
				return {n: dict(BATCHES.get(n) or {}) for n in names if n}
			return {n: {"variant_of": self.variant_of} for n in names if n}

		with (
			patch.object(se_utils, "bulk_map", side_effect=bulk_map),
			patch.object(
				se_utils, "WARN_ONLY_PMO_ROW_OWNERSHIP", warn_only
			),
			patch.object(
				se_utils, "_log_ownership_warning", side_effect=warnings.append
			),
			# Anything that reaches the screen without blocking. Must stay empty.
			patch.object(
				se_utils.frappe,
				"msgprint",
				side_effect=lambda msg, **kw: self.shown.append(msg),
			),
			patch("frappe.db.get_value", side_effect=get_value),
		):
			se_utils.validate_inventory_dimention(se)
		return warnings

	def setUp(self):
		self.shown = []


class TestTheOrdersOwnerIsEnforced(_Case):
	def test_the_customers_own_batch_passes(self):
		self.assertEqual(self._validate([_row(CUSTOMER_BATCH)]), [])

	def test_another_customers_batch_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._validate([_row(OTHER_BATCH)])
		self.assertIn(OTHER_CUSTOMER, str(raised.exception))

	def test_company_stock_on_a_customer_supplied_order_is_refused(self):
		"""The reservation bug's other half: the order says the customer supplied these stones."""
		with self.assertRaises(frappe.ValidationError) as raised:
			self._validate([_row(COMPANY_BATCH)])
		self.assertIn("Regular Stock", str(raised.exception))

	def test_a_stale_row_claim_cannot_launder_a_company_batch(self):
		"""The KLHGX62F1119 shape: the row SAYS Customer Goods, the batch says otherwise."""
		with self.assertRaises(frappe.ValidationError):
			self._validate(
				[
					_row(
						COMPANY_BATCH,
						inventory_type="Customer Goods",
						customer=CUSTOMER,
					)
				]
			)

	def test_an_inward_only_row_is_not_judged(self):
		"""Nothing was drawn, so there is no wrong owner to find -- and Customer Gold receipts
		have no batch at all until create_parent_batches mints one at before_submit."""
		self.assertEqual(
			self._validate([_row(None, s_warehouse=None, t_warehouse="WH-IN")]), []
		)

	def test_a_row_with_no_order_is_not_judged(self):
		self.assertEqual(
			self._validate(
				[_row(COMPANY_BATCH, custom_parent_manufacturing_order=None)]
			),
			[],
		)


class TestCustomerGoodsOnACompanyOrder(_Case):
	order = frappe._dict(
		is_customer_gold=0,
		is_customer_diamond=0,
		is_customer_gemstone=0,
		is_customer_material=0,
		customer=CUSTOMER,
		manufacturer="MFR",
	)

	def test_a_customer_batch_on_a_company_order_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._validate([_row(CUSTOMER_BATCH)])
		self.assertIn("non provided customer Item", str(raised.exception))

	def test_company_stock_on_a_company_order_passes(self):
		self.assertEqual(self._validate([_row(COMPANY_BATCH)]), [])


class TestPerMaterialType(_Case):
	"""The diamond flag must not drag gemstone rows into the customer lane."""

	variant_of = "G"

	def test_a_gemstone_row_on_a_diamond_only_order_wants_company_stock(self):
		self.assertEqual(self._validate([_row(COMPANY_BATCH)]), [])

	def test_a_gemstone_row_may_not_take_customer_goods_on_a_diamond_only_order(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate([_row(CUSTOMER_BATCH)])


#: A gold order: what the manufacturer's allowance and the staged rollout are tested on, since a
#: diamond or gemstone on the customer's order is blocked whatever either says.
GOLD_ORDER = frappe._dict(
	is_customer_gold=1,
	is_customer_diamond=0,
	is_customer_gemstone=0,
	is_customer_material=0,
	customer=CUSTOMER,
	manufacturer="MFR",
)


class TestTheManufacturerEscapeHatch(_Case):
	allow_substitution = 1
	order = GOLD_ORDER
	variant_of = "F"

	def test_company_stock_only_warns_when_the_manufacturer_allows_it(self):
		warnings = self._validate([_row(COMPANY_BATCH)])
		self.assertEqual(len(warnings), 1)
		self.assertIn("Regular Stock", warnings[0])

	def test_it_is_not_permission_to_consume_another_customers_goods(self):
		"""'Allow regular goods instead of customer goods' says nothing about a third party."""
		with self.assertRaises(frappe.ValidationError):
			self._validate([_row(OTHER_BATCH)])


class TestTheStagedRollout(_Case):
	order = GOLD_ORDER
	variant_of = "F"

	def test_a_let_through_mismatch_is_logged_not_shown(self):
		"""No pop-up for a case that is allowed anyway -- it used to appear twice per entry."""
		logged = self._validate([_row(OTHER_BATCH)], warn_only=True)
		self.assertEqual(len(logged), 1)
		self.assertEqual(self.shown, [])

	def test_warn_only_reports_without_throwing(self):
		"""While WARN_ONLY_PMO_ROW_OWNERSHIP is set, months of unguarded entries keep working."""
		warnings = self._validate([_row(OTHER_BATCH)], warn_only=True)
		self.assertEqual(len(warnings), 1)
		self.assertIn(OTHER_CUSTOMER, warnings[0])

	def test_the_guard_ships_in_warn_only_mode(self):
		"""Flip this deliberately, after the audit -- not by accident."""
		self.assertTrue(
			se_utils.WARN_ONLY_PMO_ROW_OWNERSHIP,
			msg="enforcement is on; make sure the ownership audit was run first",
		)


class TestDiamondsAndGemstonesAreBlockedOutright(_Case):
	"""MAT-STE-57381: a customer-diamond order, a Regular Stock diamond, the manufacturer's
	allowance on and the rollout warn-only -- and the entry saved with an orange message.

	On a hand-built entry a diamond or gemstone row on the customer's order now throws whatever
	the allowance or the staged rollout says.
	"""

	allow_substitution = 1

	def test_company_diamond_is_refused_despite_the_allowance_and_warn_only(self):
		with self.assertRaises(frappe.ValidationError) as raised:
			self._validate([_row(COMPANY_BATCH)], warn_only=True)
		self.assertIn("Regular Stock", str(raised.exception))

	def test_another_customers_diamond_is_refused_despite_warn_only(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate([_row(OTHER_BATCH)], warn_only=True)

	def test_the_customers_own_diamond_passes(self):
		self.assertEqual(self._validate([_row(CUSTOMER_BATCH)], warn_only=True), [])

	def test_an_order_named_only_in_the_header_is_enforced(self):
		with self.assertRaises(frappe.ValidationError):
			self._validate(
				[_row(COMPANY_BATCH, custom_parent_manufacturing_order=None)],
				warn_only=True,
				header_pmo=PMO,
			)

	def test_an_auto_created_follow_on_move_only_warns(self):
		"""It carries what an earlier entry drew; refusing it would strand reserved work."""
		warnings = self._validate([_row(COMPANY_BATCH)], warn_only=True, auto_created=1)
		self.assertEqual(len(warnings), 1)

	def test_gemstones_follow_the_same_rule(self):
		self.order = frappe._dict(_Case.order, is_customer_diamond=0, is_customer_gemstone=1)
		self.variant_of = "G"
		with self.assertRaises(frappe.ValidationError):
			self._validate([_row(COMPANY_BATCH)], warn_only=True)

	def test_a_finding_still_only_warns(self):
		self.order = GOLD_ORDER
		self.variant_of = "F"
		warnings = self._validate([_row(COMPANY_BATCH)], warn_only=True)
		self.assertEqual(len(warnings), 1)

	def test_the_customers_diamond_on_a_company_order_is_refused(self):
		"""MAT-STE-22944: GJCU0009's own stones on GJCU0009's order that says "No" to diamonds."""
		self.order = frappe._dict(_Case.order, is_customer_diamond=0)
		with self.assertRaises(frappe.ValidationError) as raised:
			self._validate([_row(CUSTOMER_BATCH)], warn_only=True)
		self.assertIn("does not say the customer supplied", str(raised.exception))

	def test_the_customers_diamond_on_a_company_order_only_warns_when_auto_created(self):
		self.order = frappe._dict(_Case.order, is_customer_diamond=0)
		warnings = self._validate([_row(CUSTOMER_BATCH)], warn_only=True, auto_created=1)
		self.assertEqual(len(warnings), 1)

	def test_company_stock_on_a_company_order_passes(self):
		self.order = frappe._dict(_Case.order, is_customer_diamond=0)
		self.assertEqual(self._validate([_row(COMPANY_BATCH)], warn_only=True), [])

	def test_a_finding_of_the_customers_on_a_company_order_still_only_warns(self):
		self.order = frappe._dict(GOLD_ORDER, is_customer_gold=0)
		self.variant_of = "F"
		warnings = self._validate([_row(CUSTOMER_BATCH)], warn_only=True)
		self.assertEqual(len(warnings), 1)


class TestAMissingOrder(_Case):
	order = None

	def test_a_deleted_order_does_not_raise(self):
		"""The original subscripted the None straight away and raised TypeError."""
		self.assertEqual(self._validate([_row(COMPANY_BATCH)]), [])


class TestTheAuditAgreesWithTheGuard(unittest.TestCase):
	"""``audit_pmo_row_ownership`` decides whether enforcement gets turned on.

	It reuses the guard's helpers, but its own verdict ordering is a second copy of the three
	rules, and a clean audit that simply cannot find anything is worse than no audit at all --
	it reads as permission to enforce. Each rule is pinned here against the same shapes the
	guard's own cases above use.
	"""

	def setUp(self):
		from jewellery_erpnext.patches import audit_pmo_row_ownership

		self.audit = audit_pmo_row_ownership

	def _diamond_order(self):
		return frappe._dict(
			is_customer_gold=0,
			is_customer_diamond=1,
			is_customer_gemstone=0,
			is_customer_material=0,
			customer=CUSTOMER,
			manufacturer="MFR",
		)

	def _company_order(self):
		order = self._diamond_order()
		order.is_customer_diamond = 0
		return order

	def test_the_customers_own_material_is_not_flagged(self):
		self.assertIsNone(
			self.audit.classify_row(
				"Customer Goods", CUSTOMER, self._diamond_order(), "D"
			)
		)

	def test_company_stock_on_a_company_order_is_not_flagged(self):
		self.assertIsNone(
			self.audit.classify_row("Regular Stock", None, self._company_order(), "D")
		)

	def test_another_customers_material_is_flagged(self):
		self.assertEqual(
			self.audit.classify_row(
				"Customer Goods", OTHER_CUSTOMER, self._diamond_order(), "D"
			),
			self.audit.WRONG_OWNER,
		)

	def test_company_stock_on_a_customer_supplied_order_is_flagged(self):
		self.assertEqual(
			self.audit.classify_row("Regular Stock", None, self._diamond_order(), "D"),
			self.audit.WANTED_CUSTOMER_GOODS,
		)

	def test_customer_goods_on_a_company_order_is_flagged(self):
		self.assertEqual(
			self.audit.classify_row(
				"Customer Goods", CUSTOMER, self._company_order(), "D"
			),
			self.audit.WANTED_COMPANY_STOCK,
		)

	def test_a_gemstone_row_is_judged_by_the_gemstone_flag(self):
		"""Per material type: the diamond flag must not drag gemstone rows in."""
		self.assertIsNone(
			self.audit.classify_row("Regular Stock", None, self._diamond_order(), "G")
		)
		self.assertEqual(
			self.audit.classify_row("Regular Stock", None, self._diamond_order(), "D"),
			self.audit.WANTED_CUSTOMER_GOODS,
		)

	def test_wrong_owner_outranks_the_lane_rules(self):
		"""A third party's goods is the finding, whatever the order says about the lane."""
		self.assertEqual(
			self.audit.classify_row(
				"Customer Goods", OTHER_CUSTOMER, self._company_order(), "D"
			),
			self.audit.WRONG_OWNER,
		)

	def test_a_diamond_finding_is_an_error_even_under_the_allowance(self):
		"""The guard blocks it outright, so the audit must not count it as a mere warning."""
		self.assertTrue(
			self.audit._is_error(True, self.audit.WANTED_CUSTOMER_GOODS, strict=True)
		)
		self.assertFalse(
			self.audit._is_error(True, self.audit.WANTED_CUSTOMER_GOODS, strict=False)
		)
		self.assertTrue(self.audit._is_error(True, self.audit.WRONG_OWNER, strict=False))
		self.assertTrue(
			self.audit._is_error(False, self.audit.WANTED_CUSTOMER_GOODS, strict=False)
		)
