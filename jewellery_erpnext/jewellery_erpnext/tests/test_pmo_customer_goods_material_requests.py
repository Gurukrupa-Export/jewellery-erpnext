# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Ownership on the Material Requests a Parent Manufacturing Order raises.

Three defects met on PMO-KGJPL-EA00978-010-0027, a customer-diamond order for grade AA001 whose
diamond request was raised against the company's 6B:

1. The header stamp fired only when customer_gold AND customer_diamond AND customer_stone AND
   customer_good all read "Yes", so an order where the customer supplied only the stones -- the
   ordinary customer-diamond order -- was requested entirely as company stock.
2. It then assigned ``mr_doc._customer``. Material Request HAS a real ``customer`` field; the
   leading underscore made it a throwaway Python attribute that ``get_valid_dict`` drops. So the
   request carried "Customer Goods" with NO owner, and ``normalize_ownership`` rule 3 turns that
   straight back into Regular Stock -- the customer's stones booked as the company's.
3. Only ``is_customer_diamond`` was read case-insensitively, so a plan row saved as "yes" set the
   diamond flag and silently cleared the other three.

The get_valid_dict cases assert the MECHANISM, not the assignment, in the style of
``TestMaterialRequestOwnershipStampPersists``: a test that inspected the dict handed to
``append()`` would have passed happily throughout defect 2's life, because the code always did
set a key -- just one that goes nowhere.
"""

import unittest

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	CUSTOMER_GOODS_INVENTORY_TYPE,
	VARIANT_CUSTOMER_FLAG,
	pmo_expects_customer_goods,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.filters_query import (
	is_customer_diamond_flag,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	_ITEM_TYPE_PREFIX,
)

CUSTOMER = "GJCU0009"


def _order(**flags):
	base = {
		"is_customer_gold": 0,
		"is_customer_diamond": 0,
		"is_customer_gemstone": 0,
		"is_customer_material": 0,
		"customer": CUSTOMER,
	}
	base.update(flags)
	return frappe._dict(base)


class TestOwnershipIsPerMaterialType(unittest.TestCase):
	"""Defect 1: the flag for the material the request carries is the one that decides it."""

	def test_a_customer_diamond_order_owns_only_its_diamonds(self):
		order = _order(is_customer_diamond=1)
		self.assertTrue(pmo_expects_customer_goods(order, "D"))
		for letter in ("M", "F", "G", "O"):
			self.assertFalse(
				pmo_expects_customer_goods(order, letter),
				msg=f"{letter} should not follow the diamond flag",
			)

	def test_a_customer_gemstone_order_owns_only_its_gemstones(self):
		order = _order(is_customer_gemstone=1)
		self.assertTrue(pmo_expects_customer_goods(order, "G"))
		self.assertFalse(pmo_expects_customer_goods(order, "D"))

	def test_metal_and_findings_share_the_gold_flag(self):
		"""Findings are gold too, so both letters read is_customer_gold."""
		order = _order(is_customer_gold=1)
		self.assertTrue(pmo_expects_customer_goods(order, "M"))
		self.assertTrue(pmo_expects_customer_goods(order, "F"))

	def test_diamond_and_gemstone_together(self):
		order = _order(is_customer_diamond=1, is_customer_gemstone=1)
		self.assertTrue(pmo_expects_customer_goods(order, "D"))
		self.assertTrue(pmo_expects_customer_goods(order, "G"))
		self.assertFalse(pmo_expects_customer_goods(order, "M"))

	def test_an_order_with_no_flags_owns_nothing(self):
		order = _order()
		for letter in VARIANT_CUSTOMER_FLAG:
			self.assertFalse(pmo_expects_customer_goods(order, letter))

	def test_a_missing_order_answers_false_rather_than_raising(self):
		"""A row pointing at a deleted PMO must not 500 the save."""
		self.assertFalse(pmo_expects_customer_goods(None, "D"))
		self.assertFalse(pmo_expects_customer_goods(_order(is_customer_diamond=1), None))
		self.assertFalse(pmo_expects_customer_goods(_order(is_customer_diamond=1), "X"))

	def test_a_checkbox_stored_as_a_string_still_counts(self):
		"""Check fields arrive as "1"/"0" through some paths; ``== 1`` would miss them."""
		self.assertTrue(pmo_expects_customer_goods(_order(is_customer_diamond="1"), "D"))
		self.assertFalse(pmo_expects_customer_goods(_order(is_customer_diamond="0"), "D"))

	def test_every_material_request_bucket_maps_to_a_known_letter(self):
		"""A new bucket added to _ITEM_TYPE_PREFIX must not silently lose its ownership rule."""
		for item_type, letter in _ITEM_TYPE_PREFIX.items():
			self.assertIn(
				letter,
				VARIANT_CUSTOMER_FLAG,
				msg=f"{item_type} -> {letter} has no customer flag",
			)


class TestEveryCustomerFlagReadsTheSameWay(unittest.TestCase):
	"""Defect 3: all four flags go through one case-insensitive reader."""

	def test_lowercase_yes_is_honoured(self):
		self.assertEqual(is_customer_diamond_flag("yes"), 1)
		self.assertEqual(is_customer_diamond_flag("Yes"), 1)
		self.assertEqual(is_customer_diamond_flag(" YES "), 1)

	def test_anything_else_is_not(self):
		for value in ("No", "no", "", None, "maybe"):
			self.assertEqual(is_customer_diamond_flag(value), 0, msg=repr(value))


class TestTheOwnerSurvivesTheInsert(IntegrationTestCase):
	"""Defect 2: the field the stamp is written to has to be one that exists."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_material_request_has_a_customer_field(self):
		"""The field the fix writes to. Without it the fix is the same bug again."""
		self.assertTrue(
			frappe.get_meta("Material Request").has_field("customer"),
			msg="Material Request has no customer field",
		)

	def test_the_owner_survives_get_valid_dict_but_the_underscore_does_not(self):
		"""``get_valid_dict`` is what discarded the value, so it is what the test exercises."""
		doc = frappe.new_doc("Material Request")
		doc.customer = CUSTOMER
		doc._customer = CUSTOMER

		valid = doc.get_valid_dict()

		self.assertEqual(
			valid.get("customer"),
			CUSTOMER,
			msg="the stamp the fix writes did not survive",
		)
		self.assertNotIn(
			"_customer",
			valid,
			msg="the attribute the defect wrote is silently dropped -- this is the whole bug",
		)

	def test_the_row_carries_both_halves_of_the_stamp(self):
		"""inventory_type without customer is the half-stamp rule 3 downgrades."""
		row = frappe.new_doc("Material Request Item")
		row.update(
			{
				"inventory_type": CUSTOMER_GOODS_INVENTORY_TYPE,
				"customer": CUSTOMER,
			}
		)
		valid = row.get_valid_dict()

		self.assertEqual(valid.get("inventory_type"), CUSTOMER_GOODS_INVENTORY_TYPE)
		self.assertEqual(valid.get("customer"), CUSTOMER)

	def test_customer_goods_is_a_real_inventory_type_record(self):
		"""``inventory_type`` is a Link, so an invented value hard-throws on insert."""
		self.assertTrue(
			frappe.db.exists("Inventory Type", CUSTOMER_GOODS_INVENTORY_TYPE),
			msg=f"{CUSTOMER_GOODS_INVENTORY_TYPE} does not exist as an Inventory Type",
		)
