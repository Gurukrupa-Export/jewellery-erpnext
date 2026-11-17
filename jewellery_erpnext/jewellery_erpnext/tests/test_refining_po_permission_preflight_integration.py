# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""External Refining Entry PO permission preflight (C-03, plan T-04) as a non-Administrator.

Uses the real permission engine and the real ERPNext / India Compliance chain. Every test
creates a throwaway user with the roles of a real external-refining submitter (plus a role
that grants Refining Entry submit), revokes only the tax-template permission of those
roles, and rolls back to a savepoint afterwards. That is the 16 Nov situation: a user who
may submit and post stock, but has no Select on Purchase Taxes and Charges Template.

Writes test data, so it runs only on a site with ``allow_tests`` (CI ``test_site``, the
throwaway ``kggk-pi-test.localhost``) and refuses the local production / UAT copies.

Data is taken from the site: CI's Test_Company / Test_Supplier fixtures when present,
otherwise the company and supplier of the latest submitted external entry (a copy of
production). When India Compliance resolves no template for that pair (a company without
a GSTIN, as on CI), the regional hook is stubbed to run ERPNext's real template check on
one of the company's templates instead, so the denial is still exercised.
"""

import traceback
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import frappe
from erpnext.accounts import party as party_module
from erpnext.controllers.accounts_controller import get_taxes_and_charges
from frappe.permissions import add_permission
from frappe.tests import IntegrationTestCase
from frappe.utils import strip_html_tags

from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
	PO_TAX_TEMPLATE_DOCTYPE,
	RefiningEntry,
)

REFUSED_SITES = {"kggk-prod.localhost", "kggk-uat.localhost"}
USER = "c03-preflight@example.com"
ROLE = "C03 Refining Submitter"
SAVEPOINT = "c03_preflight"
AUTOMATIC_ROLES = ("Guest", "All", "Desk User", "Administrator")
#: The throwaway user's roles where no real external submitter exists (CI).
FALLBACK_SUBMITTER_ROLES = (
	"System Manager",
	"Stock Manager",
	"Stock User",
	"Manufacturing Manager",
	"Refining Manager",
)


def _ci_fixture_party():
	if (
		frappe.db.exists("Company", "Test_Company")
		and frappe.db.exists("Supplier", "Test_Supplier")
		and frappe.db.exists("Warehouse", "Waxing RM - T")
		and frappe.db.exists("Item", "ML-G-18KT-75.4-P")
	):
		return "Test_Company", "Test_Supplier"
	return None


def _site_party():
	return _ci_fixture_party() or frappe.db.get_value(
		"Refining Entry",
		{"is_external": 1, "docstatus": 1, "supplier": ["is", "set"]},
		["company", "supplier"],
		order_by="modified desc",
	)


def _naming_counters():
	# Every counter, MAT-STE- included: a denied submit must not draw any number.
	return dict(frappe.db.sql("select name, current from `tabSeries`"))


class TestRefiningPoPreflightAsNonAdmin(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		site = frappe.local.site
		if not frappe.conf.allow_tests or site in REFUSED_SITES:
			raise unittest.SkipTest(
				f"writes test data: needs allow_tests and a throwaway site, not {site}"
			)
		frappe.set_user("Administrator")

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint(SAVEPOINT)
		self.addCleanup(self._cleanup)
		frappe.local.message_log = []

		party = _site_party()
		if not party:
			self.skipTest("no external refinery supplier on this site")
		self.company, self.supplier = party
		self.template = self._resolve_template(self.company, self.supplier)
		self._make_submitter()

	def _cleanup(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point=SAVEPOINT)
		for doctype in (PO_TAX_TEMPLATE_DOCTYPE, "Refining Entry"):
			frappe.clear_cache(doctype=doctype)
		frappe.clear_cache(user=USER)
		frappe.local.role_permissions = {}
		frappe.local.message_log = []
		frappe.flags.disable_traceback = None

	# --- fixtures -------------------------------------------------------------------

	def _resolve_template(self, company, supplier):
		"""The template the refinery PO gets for (company, supplier), resolved exactly as
		po.insert resolves it; stubs the regional hook only when nothing resolves."""
		details = party_module._get_party_details(
			supplier,
			party_type="Supplier",
			doctype="Purchase Order",
			company=company,
			ignore_permissions=True,
		)
		if details.get("taxes_and_charges"):
			return details.taxes_and_charges

		template = frappe.db.get_value(
			PO_TAX_TEMPLATE_DOCTYPE, {"company": company, "disabled": 0}, "name"
		)
		if not template:
			self.skipTest(f"no {PO_TAX_TEMPLATE_DOCTYPE} for {company}")

		def regional_check(party_details, doctype, company):
			# The check India Compliance makes on the template it picks.
			get_taxes_and_charges(PO_TAX_TEMPLATE_DOCTYPE, template)

		stub = patch.object(
			party_module, "get_regional_address_details", side_effect=regional_check
		)
		stub.start()
		self.addCleanup(stub.stop)
		return template

	def _submitter_roles(self):
		"""Roles of the user who submitted the latest external entry for this pair."""
		entry = frappe.db.get_value(
			"Refining Entry",
			{
				"is_external": 1,
				"docstatus": 1,
				"company": self.company,
				"supplier": self.supplier,
			},
			["name", "owner"],
			as_dict=True,
			order_by="modified desc",
		)
		submitter = entry and (
			frappe.db.get_value(
				"Submission Queue",
				{
					"ref_doctype": "Refining Entry",
					"ref_docname": entry.name,
					"status": "Finished",
				},
				"owner",
			)
			or entry.owner
		)
		roles = (
			frappe.get_roles(submitter)
			if submitter and submitter != "Administrator"
			else FALLBACK_SUBMITTER_ROLES
		)
		return sorted(
			{
				r
				for r in roles
				if r not in AUTOMATIC_ROLES and frappe.db.exists("Role", r)
			}
		)

	def _make_submitter(self):
		roles = self._submitter_roles()
		if not frappe.db.exists("Role", ROLE):
			frappe.get_doc(
				{"doctype": "Role", "role_name": ROLE, "desk_access": 1}
			).insert(ignore_permissions=True)
		add_permission("Refining Entry", ROLE, ptype="read")
		for ptype in ("write", "create", "submit"):
			frappe.permissions.update_permission_property(
				"Refining Entry", ROLE, 0, ptype, 1
			)
		if not frappe.db.exists("User", USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": USER,
					"first_name": "C03 Preflight",
					"user_type": "System User",
					"send_welcome_email": 0,
					"roles": [{"role": role} for role in (ROLE, *roles)],
				}
			).insert(ignore_permissions=True)
		# Only the template permission goes: every other right of a real submitter stays.
		frappe.permissions.setup_custom_perms(PO_TAX_TEMPLATE_DOCTYPE)
		frappe.db.delete(
			"Custom DocPerm",
			{
				"parent": PO_TAX_TEMPLATE_DOCTYPE,
				"role": ["in", [ROLE, *roles, *AUTOMATIC_ROLES]],
			},
		)
		self._refresh_permissions()

		self.assertTrue(
			frappe.has_permission("Refining Entry", "submit", user=USER),
			"the throwaway user cannot submit Refining Entry",
		)
		self.assertFalse(
			frappe.has_permission(PO_TAX_TEMPLATE_DOCTYPE, "select", user=USER),
			f"the throwaway user still has Select on {PO_TAX_TEMPLATE_DOCTYPE}",
		)

	def _grant(self, ptype):
		frappe.set_user("Administrator")
		add_permission(PO_TAX_TEMPLATE_DOCTYPE, ROLE, ptype=ptype)
		self._refresh_permissions()
		self.assertTrue(
			frappe.has_permission(PO_TAX_TEMPLATE_DOCTYPE, "select", user=USER)
		)

	def _refresh_permissions(self):
		for doctype in (PO_TAX_TEMPLATE_DOCTYPE, "Refining Entry"):
			frappe.clear_cache(doctype=doctype)
		frappe.clear_cache(user=USER)
		frappe.local.role_permissions = {}

	def _entry(self, company=None, supplier=None):
		entry = frappe.new_doc("Refining Entry")
		entry.name = "RFN-C03-PREFLIGHT"
		entry.is_external = 1
		entry.company = company or self.company
		entry.supplier = supplier or self.supplier
		return entry

	def _draft(self):
		"""A saved external draft that passes validate, or skip the e2e case."""
		if _ci_fixture_party() == (self.company, self.supplier):
			from jewellery_erpnext.refining.doctype.refining_entry.test_refining_entry import (
				gold_receipt,
			)

			gold_receipt("Waxing RM - T", "ML-G-18KT-75.4-P", 5.0)
			entry = frappe.new_doc("Refining Entry")
			entry.refining_type = "Unused/Loose Material Refining"
			entry.is_external = 1
			entry.company = self.company
			entry.supplier = self.supplier
			entry.department = "Waxing - T"
			entry.warehouse = "Waxing RM - T"
			entry.refining_department = "Refinery - T"
			entry.append(
				"material_items",
				{
					"item_code": "ML-G-18KT-75.4-P",
					"warehouse": "Waxing RM - T",
					"qty": 5.0,
					"source_type": "Unused/Loose Material",
				},
			)
			entry.save()
			return entry.name

		name = frappe.db.get_value(
			"Refining Entry",
			{
				"docstatus": 0,
				"is_external": 1,
				"company": self.company,
				"supplier": self.supplier,
			},
			"name",
			order_by="modified desc",
		)
		if not name:
			self.skipTest(
				"no draft external Refining Entry for "
				f"{self.company} / {self.supplier}; the preflight cases cover the rest"
			)
		return name

	def _submit_as_user(self, name):
		"""Submit `name` as USER the way the Submission Queue job does (doc.submit());
		returns (exception or None, transfer spy)."""
		frappe.set_user(USER)
		doc = frappe.get_doc("Refining Entry", name)
		with ExitStack() as stack:
			transfer = stack.enter_context(
				patch.object(
					RefiningEntry,
					"create_material_transfer_se",
					autospec=True,
					side_effect=RefiningEntry.create_material_transfer_se,
				)
			)
			# on_submit multiplies the cap on the shared connection; keep it per test.
			stack.enter_context(
				patch.object(
					frappe.db,
					"MAX_WRITES_PER_TRANSACTION",
					frappe.db.MAX_WRITES_PER_TRANSACTION,
				)
			)
			try:
				doc.submit()
			except Exception as e:
				frappe.set_user("Administrator")
				return e, transfer
		frappe.set_user("Administrator")
		return None, transfer

	def _assert_clear_message(self, exc):
		self.assertIsInstance(exc, frappe.PermissionError)
		message = strip_html_tags(str(exc))
		for expected in (PO_TAX_TEMPLATE_DOCTYPE, "Select", USER, self.template):
			self.assertIn(expected, message)

	# --- preflight as the user ------------------------------------------------------

	def test_user_without_select_is_refused_with_a_clear_message(self):
		frappe.set_user(USER)
		frappe.local.message_log = []
		with self.assertRaises(frappe.PermissionError) as cm:
			self._entry()._preflight_external_po_permissions()
		self._assert_clear_message(cm.exception)
		messages = [
			strip_html_tags(str(m.get("message"))) for m in frappe.local.message_log
		]
		self.assertEqual(len(messages), 1, messages)
		self.assertNotIn("Not permitted", messages)
		self.assertFalse(frappe.flags.disable_traceback)

	def test_select_grant_lets_the_preflight_through(self):
		self._grant("select")
		frappe.set_user(USER)
		frappe.local.message_log = []
		self._entry()._preflight_external_po_permissions()
		self.assertEqual(frappe.local.message_log, [])

	def test_read_grant_also_lets_it_through(self):
		# frappe: select is implied by read.
		self._grant("read")
		frappe.set_user(USER)
		self._entry()._preflight_external_po_permissions()

	def test_administrator_is_unaffected(self):
		frappe.set_user("Administrator")
		self._entry()._preflight_external_po_permissions()

	def test_preflight_writes_nothing(self):
		frappe.set_user(USER)
		writes_before = frappe.db.transaction_writes
		with self.assertRaises(frappe.PermissionError):
			self._entry()._preflight_external_po_permissions()
		self.assertEqual(frappe.db.transaction_writes, writes_before)

	# --- full submit as the user (plan T-04) ----------------------------------------

	def test_denied_submit_fails_before_any_stock_entry(self):
		name = self._draft()
		counters_before = _naming_counters()
		entries_before = frappe.db.count("Stock Entry", {"custom_refining_entry": name})

		exc, transfer = self._submit_as_user(name)

		self.assertIsNotNone(
			exc, "the submit went through without the template permission"
		)
		self._assert_clear_message(exc)
		frames = [f.name for f in traceback.extract_tb(exc.__traceback__)]
		self.assertIn("before_submit_external", frames)
		self.assertIn("_preflight_external_po_permissions", frames)
		self.assertNotIn("create_external_refining_po", frames)
		transfer.assert_not_called()
		# Same transaction, so an increment by getseries would be visible here.
		self.assertEqual(_naming_counters(), counters_before)
		self.assertEqual(
			frappe.db.count("Stock Entry", {"custom_refining_entry": name}),
			entries_before,
		)
		self.assertEqual(frappe.db.get_value("Refining Entry", name, "docstatus"), 0)
		self.assertFalse(
			frappe.db.get_value("Refining Entry", name, "refining_entry_po")
		)

	def test_same_submit_succeeds_once_select_is_granted(self):
		name = self._draft()
		self._grant("select")

		exc, transfer = self._submit_as_user(name)

		self.assertIsNone(exc, f"submit failed after the Select grant: {exc!r}")
		transfer.assert_called_once()
		submitted = frappe.db.get_value(
			"Refining Entry",
			name,
			["docstatus", "material_transfer_se", "refining_entry_po"],
			as_dict=True,
		)
		self.assertEqual(submitted.docstatus, 1)
		self.assertTrue(submitted.material_transfer_se)
		self.assertTrue(frappe.db.exists("Purchase Order", submitted.refining_entry_po))
