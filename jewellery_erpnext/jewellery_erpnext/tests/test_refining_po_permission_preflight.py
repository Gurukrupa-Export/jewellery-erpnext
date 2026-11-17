# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""External Refining Entry: the refinery Purchase Order permission preflight (C-03).

Submitting an external entry posts the material transfer Stock Entry (it takes the
shared MAT-STE- counter and can post thousands of lines) and only then creates the
draft service Purchase Order. ERPNext checks Select on the PO's GST tax template AS THE
SUBMITTING USER inside that PO insert, so a user without it failed after minutes of
stock work, and every other stock posting waited on the counter meanwhile (RFN-SCP-26-
00042/00043, 16 Nov). before_submit_external now replays the user-dependent call first.

Mock-only and DB-read-only: no fixtures, nothing written. The tripwire class pins the
ERPNext call the preflight replays, so an ERPNext upgrade that changes it fails here.
"""

import ast
import inspect
import textwrap
from contextlib import ExitStack
from unittest.mock import patch

import erpnext
import frappe
import frappe.model.document as document_module
from erpnext.accounts import party as party_module
from erpnext.controllers import buying_controller
from erpnext.controllers.accounts_controller import get_taxes_and_charges
from erpnext.controllers.buying_controller import BuyingController
from frappe.tests import IntegrationTestCase
from frappe.utils import strip_html_tags, today

from jewellery_erpnext.refining.doctype.refining_entry.refining_entry import (
	PO_TAX_TEMPLATE_DOCTYPE,
	RefiningEntry,
	_denied_po_tax_template,
)

TEMPLATE = "Input GST In-state - C03"
SUPPLIER = "C03-REFINER"
COMPANY = "C03-COMPANY"
ENTRY_NAME = "RFN-SCP-C03-00001"

#: What the preflight must pass to erpnext.accounts.party._get_party_details.
REPLAYED_KWARGS = {
	"party_type": "Supplier",
	"doctype": "Purchase Order",
	"company": COMPANY,
	"fetch_payment_terms_template": True,
	"ignore_permissions": True,
}

WRITE_VERBS = ("insert", "update", "delete", "replace")


def _deny_like_erpnext(template=TEMPLATE):
	"""side_effect for _get_party_details that fails exactly as production did: the real
	ERPNext get_taxes_and_charges -> require_permission -> _refuse path, with only the
	permission lookup (erpnext._is_permitted) stubbed to "no"."""

	def _side_effect(*args, **kwargs):
		with patch.object(erpnext, "_is_permitted", return_value=False):
			get_taxes_and_charges(PO_TAX_TEMPLATE_DOCTYPE, template)

	return _side_effect


def _new_entry(**fields):
	entry = frappe.new_doc("Refining Entry")
	entry.name = ENTRY_NAME
	entry.company = COMPANY
	entry.supplier = SUPPLIER
	entry.update(fields)
	return entry


def _messages():
	return [strip_html_tags(str(m.get("message"))) for m in frappe.local.message_log]


class _PreflightCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		# No fixtures: everything below is mocked or read-only.
		pass

	def setUp(self):
		self._saved_log = frappe.get_message_log()
		self._saved_flag = frappe.flags.disable_traceback
		frappe.local.message_log = []
		frappe.flags.disable_traceback = False

	def tearDown(self):
		frappe.local.message_log = self._saved_log
		frappe.flags.disable_traceback = self._saved_flag


class TestRefiningPoPermissionPreflight(_PreflightCase):
	def _run(self, side_effect, entry=None, has_select=False):
		entry = entry or _new_entry()
		with ExitStack() as stack:
			party_details = stack.enter_context(
				patch.object(
					party_module, "_get_party_details", side_effect=side_effect
				)
			)
			stack.enter_context(
				patch.object(frappe, "has_permission", return_value=has_select)
			)
			entry._preflight_external_po_permissions()
		return party_details

	def test_denied_template_select_raises_a_clear_permission_error(self):
		with self.assertRaises(frappe.PermissionError) as cm:
			self._run(_deny_like_erpnext())

		message = strip_html_tags(str(cm.exception))
		for expected in (
			PO_TAX_TEMPLATE_DOCTYPE,
			"Select",
			TEMPLATE,
			SUPPLIER,
			ENTRY_NAME,
			frappe.session.user,
			"none of their roles grants it",
			"No stock has been moved",
		):
			self.assertIn(expected, message)

	def test_denial_leaves_only_its_own_message_and_restores_the_traceback_flag(self):
		frappe.local.message_log = [frappe._dict(message="earlier message")]
		with self.assertRaises(frappe.PermissionError):
			self._run(_deny_like_erpnext())

		messages = _messages()
		# erpnext._refuse() queued "Not permitted" and set disable_traceback; neither may
		# survive, or the user sees a bare "Not permitted" next to the real reason.
		self.assertEqual(len(messages), 2, messages)
		self.assertEqual(messages[0], "earlier message")
		self.assertIn(PO_TAX_TEMPLATE_DOCTYPE, messages[1])
		self.assertNotIn("Not permitted", messages)
		self.assertIs(frappe.flags.disable_traceback, False)
		self.assertEqual(
			frappe.local.message_log[-1].get("title"),
			f"Missing permission: {PO_TAX_TEMPLATE_DOCTYPE}",
		)

	def test_reason_points_at_user_permissions_when_a_role_grants_select(self):
		with self.assertRaises(frappe.PermissionError) as cm:
			self._run(_deny_like_erpnext(), has_select=True)
		message = strip_html_tags(str(cm.exception))
		self.assertIn("User Permissions", message)
		self.assertNotIn("none of their roles grants it", message)

	def test_replays_the_set_missing_values_call_for_the_refinery_po(self):
		party_details = self._run(lambda *a, **k: frappe._dict())
		party_details.assert_called_once_with(SUPPLIER, **REPLAYED_KWARGS)

	def test_passing_run_shows_nothing_and_changes_no_flags(self):
		def resolves(*args, **kwargs):
			frappe.msgprint("printed during the dry run")
			return frappe._dict(taxes_and_charges=TEMPLATE)

		frappe.local.message_log = [frappe._dict(message="earlier message")]
		self._run(resolves)
		# po.insert repeats the call later and shows what it prints; the dry run does not.
		self.assertEqual(_messages(), ["earlier message"])
		self.assertIs(frappe.flags.disable_traceback, False)

	def test_other_errors_propagate_unchanged(self):
		def boom(*args, **kwargs):
			frappe.throw("GSTIN missing on the company address")

		with self.assertRaises(frappe.ValidationError) as cm:
			self._run(boom)
		self.assertNotIsInstance(cm.exception, frappe.PermissionError)
		# The callee's own message stays, so the user still sees the real reason.
		self.assertEqual(_messages(), ["GSTIN missing on the company address"])

		with self.assertRaises(KeyError):
			self._run(KeyError("supplier"))

	def test_permission_error_from_another_check_is_not_relabelled(self):
		raised = {}

		def other_check(*args, **kwargs):
			try:
				frappe.throw("Not permitted to read Address", frappe.PermissionError)
			except frappe.PermissionError as e:
				raised["exc"] = e
				raise

		with self.assertRaises(frappe.PermissionError) as cm:
			self._run(other_check)
		self.assertIs(cm.exception, raised["exc"])
		self.assertEqual(_messages(), ["Not permitted to read Address"])

	def test_select_check_on_another_doctype_is_not_relabelled(self):
		def sales_template_denied(*args, **kwargs):
			with patch.object(erpnext, "_is_permitted", return_value=False):
				get_taxes_and_charges("Sales Taxes and Charges Template", TEMPLATE)

		with self.assertRaises(frappe.PermissionError) as cm:
			self._run(sales_template_denied)
		self.assertNotIn(PO_TAX_TEMPLATE_DOCTYPE, str(cm.exception))

	def test_denied_template_is_read_from_the_require_permission_frame(self):
		try:
			_deny_like_erpnext("Some Template - X")()
		except frappe.PermissionError as e:
			self.assertEqual(_denied_po_tax_template(e), "Some Template - X")
		else:
			self.fail("the ERPNext permission path did not raise")

		try:
			frappe.throw("Not permitted", frappe.PermissionError)
		except frappe.PermissionError as e:
			self.assertIsNone(_denied_po_tax_template(e))

	def test_preflight_writes_nothing(self):
		statements = []
		real_sql = frappe.db.sql

		def spy(query, *args, **kwargs):
			statements.append(str(query).strip().lower())
			return real_sql(query, *args, **kwargs)

		writes_before = frappe.db.transaction_writes
		entry = _new_entry()
		with ExitStack() as stack:
			stack.enter_context(patch.object(frappe.db, "sql", side_effect=spy))
			stack.enter_context(
				patch.object(
					party_module, "_get_party_details", side_effect=_deny_like_erpnext()
				)
			)
			# Real doctype-level has_permission: the reason lookup must be read-only too.
			with self.assertRaises(frappe.PermissionError):
				entry._preflight_external_po_permissions()

		self.assertEqual(frappe.db.transaction_writes, writes_before)
		offending = [
			q for q in statements if q.startswith(WRITE_VERBS) or "for update" in q
		]
		self.assertEqual(offending, [])


class TestPreflightRunsBeforeAnyStockWork(_PreflightCase):
	STOCK_AND_BUILD_METHODS = (
		"build_material_table",
		"validate_dust_opening_material",
		"_get_supplier_warehouse",
		"create_material_transfer_se",
		"create_dust_opening_receipt_se",
		"create_external_refining_po",
	)

	def _patch_stock_work(self, stack):
		return {
			name: stack.enter_context(patch.object(RefiningEntry, name))
			for name in self.STOCK_AND_BUILD_METHODS
		}

	def test_denied_submit_stops_in_before_submit_external(self):
		entry = _new_entry(is_external=1, refining_type="Scrap Refining")
		with ExitStack() as stack:
			stock_work = self._patch_stock_work(stack)
			stack.enter_context(
				patch.object(
					party_module, "_get_party_details", side_effect=_deny_like_erpnext()
				)
			)
			with self.assertRaises(frappe.PermissionError):
				entry.before_submit()

		for name, mocked in stock_work.items():
			self.assertFalse(mocked.called, f"{name} ran before the preflight refused")
		# The preflight runs first: the pricing default below it was not reached either.
		self.assertFalse(entry.pricing_item)

	def test_missing_supplier_is_still_reported_first(self):
		entry = _new_entry(is_external=1, supplier=None)
		with patch.object(party_module, "_get_party_details") as party_details:
			with self.assertRaises(frappe.ValidationError) as cm:
				entry.before_submit()
		self.assertNotIsInstance(cm.exception, frappe.PermissionError)
		self.assertIn("Refinery Supplier is mandatory", str(cm.exception))
		party_details.assert_not_called()

	def test_passing_preflight_lets_the_existing_checks_run(self):
		entry = _new_entry(
			is_external=1, refining_type="Unused/Loose Material Refining"
		)
		with patch.object(
			party_module, "_get_party_details", return_value=frappe._dict()
		) as party_details:
			with self.assertRaises(frappe.ValidationError) as cm:
				entry.before_submit()
		party_details.assert_called_once()
		self.assertIn("No materials to refine", str(cm.exception))

	def test_internal_entries_do_not_run_the_preflight(self):
		entry = _new_entry(is_external=0, refining_warehouse=None)
		with patch.object(
			RefiningEntry, "_preflight_external_po_permissions"
		) as preflight:
			with self.assertRaises(frappe.ValidationError):
				entry.before_submit()
		preflight.assert_not_called()


def _function_ast(func):
	source = textwrap.dedent(inspect.getsource(func))
	return ast.parse(source).body[0]


def _calls_to(tree, name):
	return [
		node
		for node in ast.walk(tree)
		if isinstance(node, ast.Call)
		and (
			(isinstance(node.func, ast.Name) and node.func.id == name)
			or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
		)
	]


class TestPreflightTripwire(_PreflightCase):
	"""Fails when ERPNext (or this app) changes the call the preflight replays.

	If one of these fails after an upgrade: re-read BuyingController.set_missing_values
	and create_external_refining_po, then make _preflight_external_po_permissions pass the
	same arguments again (or drop the preflight if ERPNext no longer checks the template
	as the user).
	"""

	#: set_missing_values' _get_party_details call as ast.unparse prints it.
	EXPECTED_SET_MISSING_VALUES_CALL = {
		"args": ["self.supplier"],
		"keywords": {
			"party_type": "'Supplier'",
			"doctype": "self.doctype",
			"company": "self.company",
			"party_address": "self.get('supplier_address')",
			"shipping_address": "self.get('shipping_address')",
			"dispatch_address": "self.get('dispatch_address')",
			"company_address": "self.get('billing_address')",
			"fetch_payment_terms_template": "not self.get('ignore_default_payment_terms_template')",
			"ignore_permissions": "self.flags.ignore_permissions",
		},
	}

	#: Fields of the refinery PO that feed that call; create_external_refining_po must leave
	#: them unset for the replay's defaults (None / True) to match.
	PO_FIELDS_THE_REPLAY_LEAVES_UNSET = {
		"supplier_address",
		"shipping_address",
		"dispatch_address",
		"billing_address",
		"ignore_default_payment_terms_template",
	}

	def test_set_missing_values_still_makes_the_replayed_call(self):
		calls = _calls_to(
			_function_ast(BuyingController.set_missing_values), "_get_party_details"
		)
		self.assertEqual(len(calls), 1, "expected exactly one _get_party_details call")
		call = calls[0]
		self.assertEqual(
			{
				"args": [ast.unparse(a) for a in call.args],
				"keywords": {k.arg: ast.unparse(k.value) for k in call.keywords},
			},
			self.EXPECTED_SET_MISSING_VALUES_CALL,
		)

	def test_buying_controller_calls_the_function_the_preflight_imports(self):
		self.assertIs(
			buying_controller._get_party_details, party_module._get_party_details
		)

	def test_get_party_details_accepts_the_replayed_arguments(self):
		signature = inspect.signature(party_module._get_party_details)
		signature.bind(SUPPLIER, **REPLAYED_KWARGS)
		# Omitted by the preflight, so their defaults must equal the refinery PO's values.
		for omitted in (
			"party_address",
			"shipping_address",
			"dispatch_address",
			"company_address",
		):
			self.assertIsNone(signature.parameters[omitted].default, omitted)

	def test_erpnext_still_checks_select_on_the_template_by_name(self):
		# _denied_po_tax_template reads require_permission's doctype/name locals.
		self.assertEqual(
			list(inspect.signature(erpnext.require_permission).parameters),
			["doctype", "name", "ptype"],
		)
		calls = _calls_to(_function_ast(get_taxes_and_charges), "require_permission")
		self.assertEqual(
			[ast.unparse(c) for c in calls],
			["require_permission(master_doctype, master_name, 'select')"],
		)

	def test_refinery_po_is_inserted_as_the_preflight_assumes(self):
		tree = _function_ast(RefiningEntry.create_external_refining_po)
		assigned = {
			target.attr
			for node in ast.walk(tree)
			if isinstance(node, ast.Assign)
			for target in node.targets
			if isinstance(target, ast.Attribute)
			and isinstance(target.value, ast.Name)
			and target.value.id == "po"
		}
		self.assertTrue({"supplier", "company"} <= assigned, assigned)
		self.assertEqual(assigned & self.PO_FIELDS_THE_REPLAY_LEAVES_UNSET, set())
		self.assertEqual(
			[ast.unparse(c) for c in _calls_to(tree, "insert")],
			["po.insert(ignore_permissions=True)"],
		)
		self.assertEqual(
			[ast.unparse(c) for c in _calls_to(tree, "new_doc")],
			["frappe.new_doc('Purchase Order')"],
		)

	def test_refinery_po_reaches_set_missing_values_with_the_replayed_arguments(self):
		"""Builds the refinery PO with the real create_external_refining_po (insert
		stubbed), runs its real before_validate hooks and set_missing_values, and compares
		the _get_party_details call with the preflight's, argument by argument."""
		party = _site_refinery_party()
		if not party:
			self.skipTest("no Company / Supplier on this site")
		company, supplier = party
		entry = _new_entry(company=company, supplier=supplier, posting_date=today())

		class _Captured(Exception):
			pass

		class _Stop(Exception):
			pass

		captured = {}

		def capture_insert(doc, *args, **kwargs):
			captured.update(po=doc, args=args, kwargs=kwargs)
			raise _Captured

		with ExitStack() as stack:
			stack.enter_context(
				patch.object(RefiningEntry, "_external_po_lines", return_value=([], []))
			)
			stack.enter_context(
				patch(
					"frappe.model.document.Document.insert",
					autospec=True,
					side_effect=capture_insert,
				)
			)
			with self.assertRaises(_Captured):
				entry.create_external_refining_po()

		po = captured["po"]
		self.assertEqual(
			(captured["args"], captured["kwargs"]), ((), {"ignore_permissions": True})
		)
		# What Document.insert does with that argument before any hook runs.
		po.flags.ignore_permissions = True
		with ExitStack() as stack:
			# Controller + app before_validate hooks only (India Compliance sets place of
			# supply there); site Server Scripts, webhooks and notifications are not part
			# of the code path under test.
			for name in (
				"run_server_script_for_doc_event",
				"run_webhooks",
				"run_automations",
			):
				if hasattr(document_module, name):
					stack.enter_context(patch.object(document_module, name))
			stack.enter_context(patch.object(type(po), "run_notifications"))
			po.run_method("before_validate")

		seen = []

		def record(*args, **kwargs):
			seen.append((args, kwargs))
			raise _Stop

		with ExitStack() as stack:
			stack.enter_context(
				patch.object(
					buying_controller, "_get_party_details", side_effect=record
				)
			)
			# Only sets currency fields (and may fetch an exchange rate); not an input.
			stack.enter_context(patch.object(type(po), "set_price_list_currency"))
			with self.assertRaises(_Stop):
				po.set_missing_values(for_validate=True)

		with patch.object(
			party_module, "_get_party_details", return_value=frappe._dict()
		) as replay:
			entry._preflight_external_po_permissions()

		signature = inspect.signature(party_module._get_party_details)

		def bound(args, kwargs):
			arguments = signature.bind(*args, **kwargs)
			arguments.apply_defaults()
			return dict(arguments.arguments)

		self.assertEqual(len(seen), 1)
		self.assertEqual(
			bound(*replay.call_args),
			bound(*seen[0]),
			"preflight diverged from po.insert",
		)


def _site_refinery_party():
	"""(company, supplier) of the latest submitted external entry, else any pair."""
	row = frappe.db.get_value(
		"Refining Entry",
		{"is_external": 1, "docstatus": 1, "supplier": ["is", "set"]},
		["company", "supplier"],
		order_by="modified desc",
	)
	if row:
		return row
	company = frappe.db.get_value("Company", {}, "name")
	supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name")
	return (company, supplier) if company and supplier else None
