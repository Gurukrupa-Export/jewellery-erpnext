# Copyright (c) 2026, Gurukrupa Exports and Contributors
# See license.txt

"""Authorisation guards on the Job Card stock endpoints.

`create_stock_entry`, `create_stock_entry_material_receipt` and
`create_internal_transfer` each create **and submit** a Stock Entry. They are
whitelisted, which makes them callable by any authenticated session — including a
portal user holding no Desk roles — and they carried no permission check.

Stage 1 (this change) adds `frappe.has_permission("Stock Entry", "create",
throw=True)`. Stage 2 removes the `ignore_permissions=True` on the save, after
production logs confirm no legitimate caller is being turned away.

The two halves are not redundant, and the reason is subtle enough to encode as a
test: `Document._save` assigns `flags.ignore_permissions` onto the *document*, and
`_submit()` calls `self.save()` with **no arguments** — so `ignore_permissions is
None` and the existing flag is never cleared. A document inserted with
`ignore_permissions=True` therefore skips the submit permission check as well.
`test_submit_inherits_the_insert_bypass` pins that framework behaviour so the
follow-up cannot be dropped on the assumption that stage 1 was sufficient.
"""

import inspect

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import job_card

# All FOUR whitelisted writers in job_card.py, not just the three the audit named.
# `create_new_job_card` was missed by both audit reports and by the first remediation
# pass: it appends to an already-SUBMITTED Work Order with
# `ignore_validate_update_after_submit` AND `ignore_permissions`, so it disables the
# permission check and the update-after-submit validation at once.
GUARDED_ENDPOINTS = (
	"create_stock_entry",
	"create_stock_entry_material_receipt",
	"create_internal_transfer",
	"create_new_job_card",
)


class TestJobCardEndpointPermissions(IntegrationTestCase):
	def test_every_stock_creating_endpoint_checks_permission(self):
		"""Each endpoint must assert Stock Entry create rights before doing anything."""
		for name in GUARDED_ENDPOINTS:
			source = inspect.getsource(getattr(job_card, name))
			self.assertIn(
				"frappe.has_permission(",
				source,
				f"{name} creates or submits a document without an authorisation check",
			)

	def test_guard_precedes_document_creation(self):
		"""A check placed after `frappe.new_doc` would still let work begin."""
		for name in GUARDED_ENDPOINTS:
			source = inspect.getsource(getattr(job_card, name))
			guard_at = source.find("frappe.has_permission")
			newdoc_at = source.find("frappe.new_doc")
			self.assertNotEqual(guard_at, -1, f"{name} has no permission guard")
			if newdoc_at != -1:
				self.assertLess(
					guard_at, newdoc_at, f"{name} creates a document before authorising"
				)

	def test_endpoints_are_not_guest_accessible(self):
		"""Whitelisting alone does not expose a method to Guest; allow_guest does.

		These endpoints post stock, so allow_guest must never appear on them.
		"""
		for name in GUARDED_ENDPOINTS:
			fn = getattr(job_card, name)
			self.assertFalse(
				getattr(fn, "allow_guest", False),
				f"{name} is Guest-accessible and creates stock",
			)

	def test_unauthorised_user_is_refused(self):
		"""A session with no Stock Entry rights must be refused by the guard.

		Deliberately does NOT call `frappe.clear_cache(user=...)`. That needs the
		Redis cache on :13000, which is only up while `bench start` is running, and
		frappe raises "Should not fail silently in tests" when the cache is
		unreachable — so the assertion would fail for want of a running bench rather
		than for want of a permission check. A per-run unique address means the user
		has no cached role state to clear in the first place.
		"""
		user = f"test_jc_perm_guard_{frappe.generate_hash(length=8)}@example.com"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": user,
				"first_name": "JC Perm Guard",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)

		# Strip every role so refusal is the only available outcome. A freshly
		# inserted User still receives the automatic roles, and those must go too.
		frappe.db.delete("Has Role", {"parent": user})

		original = frappe.session.user
		try:
			frappe.set_user(user)
			self.assertFalse(
				frappe.has_permission("Stock Entry", "create"),
				"a role-less user should not hold Stock Entry create rights — "
				"if they did, the guard added to these endpoints would be a no-op",
			)
			with self.assertRaises(frappe.PermissionError):
				frappe.has_permission("Stock Entry", "create", throw=True)
		finally:
			frappe.set_user(original)

	def test_submit_inherits_the_insert_bypass(self):
		"""`_submit()` calls `save()` with no arguments, so an ignore_permissions
		flag set at insert survives into the submit and skips its check too.

		This is why removing `ignore_permissions=True` is a required second stage
		and not an optional tidy-up. If this assertion ever fails, upstream changed
		the behaviour and the follow-up can be re-scoped.
		"""
		source = inspect.getsource(frappe.model.document.Document._submit)
		self.assertIn(
			"self.save()",
			source,
			"_submit no longer delegates to a bare save(); re-verify the flag-leak "
			"reasoning behind the staged ignore_permissions removal",
		)

		save_source = inspect.getsource(frappe.model.document.Document._save)
		self.assertIn(
			"if ignore_permissions is not None:",
			save_source,
			"_save no longer guards the flag assignment; the bypass may no longer persist",
		)

	def test_no_ungated_whitelisted_writer_in_this_module(self):
		"""Catches the failure mode that produced this gap.

		The first remediation pass fixed exactly the three endpoints the audit named
		and never asked whether the module held a fourth. It did. This walks the AST
		instead of trusting a hand-written list, so the next one cannot slip through.
		"""
		import ast

		source = inspect.getsource(job_card)
		tree = ast.parse(source)
		ungated = []
		for node in ast.walk(tree):
			if not isinstance(node, ast.FunctionDef):
				continue
			if not any("whitelist" in ast.dump(d) for d in node.decorator_list):
				continue
			body = ast.get_source_segment(source, node) or ""
			writes = any(
				tok in body for tok in (".save(", ".insert(", ".submit(", "db_set(")
			)
			if writes and "has_permission" not in body:
				ungated.append(node.name)

		self.assertEqual(
			ungated,
			[],
			"whitelisted functions in job_card.py that write documents without a "
			f"permission check: {ungated}",
		)
