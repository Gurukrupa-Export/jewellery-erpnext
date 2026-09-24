# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""F11 -- a customer-diamond order gets the grade it ordered, or an approved substitute.

KLHGX62F1119's PMO ordered the customer's MH12A; its Material Request was edited to the company's
6B before submit, with no record of who allowed it. Pure-logic: the PMO, item attributes and roles
are stubbed; nothing is written.
"""

import unittest
from unittest.mock import patch

import frappe

from jewellery_erpnext.jewellery_erpnext.doc_events import material_request as mr

PMO = "PMO-KGJPL-EA02652-001-0002"
ORDERED = "D-NT-RO-MH12A-+6.5-7"
COMPANY_6B = "D-NT-RO-6B-+6.5-7"
METAL = "M-G-22KT-91.75-Y"
GRADES = {ORDERED: "MH12A", COMPANY_6B: "6B"}


def _request(*items, reason=None, pmo=PMO):
	return frappe._dict(
		doctype="Material Request",
		manufacturing_order=pmo,
		custom_diamond_substitution_reason=reason,
		items=[frappe._dict(idx=i, item_code=code) for i, code in enumerate(items, 1)],
	)


class _Case(unittest.TestCase):
	order = frappe._dict(is_customer_diamond=1, diamond_grade="MH12A")
	roles = ()

	def setUp(self):
		def get_all(doctype, filters=None, fields=None, **kwargs):
			return [
				frappe._dict(parent=code, attribute_value=GRADES[code])
				for code in filters["parent"][1]
				if code in GRADES
			]

		patches = [
			patch(
				f"{mr.__name__}.frappe.db.get_value",
				side_effect=lambda *a, **k: self.order,
			),
			patch(f"{mr.__name__}.frappe.get_all", side_effect=get_all),
			patch(
				f"{mr.__name__}.frappe.get_roles",
				side_effect=lambda *a, **k: list(self.roles),
			),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)


class TestCustomerDiamondGrade(_Case):
	def test_the_ordered_grade_passes(self):
		doc = _request(METAL, ORDERED)
		mr.validate_customer_diamond_grade(doc)
		self.assertIsNone(doc.custom_diamond_substitution_by)

	def test_a_different_grade_is_refused(self):
		"""The KLHGX62F1119 case: 6B on an MH12A order."""
		with self.assertRaises(frappe.ValidationError) as raised:
			mr.validate_customer_diamond_grade(_request(METAL, COMPANY_6B))
		self.assertIn("6B", str(raised.exception))

	def test_a_reason_without_the_role_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			mr.validate_customer_diamond_grade(
				_request(COMPANY_6B, reason="customer agreed by phone")
			)

	def test_an_approver_with_a_reason_may_submit_and_is_recorded(self):
		self.roles = (mr.DIAMOND_SUBSTITUTION_APPROVER_ROLE,)
		doc = _request(COMPANY_6B, reason="customer agreed in writing")
		mr.validate_customer_diamond_grade(doc)
		self.assertEqual(doc.custom_diamond_substitution_by, frappe.session.user)

	def test_the_role_without_a_reason_is_refused(self):
		self.roles = (mr.DIAMOND_SUBSTITUTION_APPROVER_ROLE,)
		with self.assertRaises(frappe.ValidationError):
			mr.validate_customer_diamond_grade(_request(COMPANY_6B))


class TestOnlyCustomerDiamondOrders(_Case):
	order = frappe._dict(is_customer_diamond=0, diamond_grade="MH12A")

	def test_a_company_diamond_order_may_use_any_grade(self):
		mr.validate_customer_diamond_grade(_request(COMPANY_6B))

	def test_a_request_with_no_order_is_not_checked(self):
		mr.validate_customer_diamond_grade(_request(COMPANY_6B, pmo=None))
