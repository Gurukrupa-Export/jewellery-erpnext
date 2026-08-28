# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Tests for doc_events/stock_entry_type.py — the per-role Stock Entry Type whitelist.

Pure-logic: every DB read is patched. Covers the three moving parts —

* ``get_permission_query_conditions`` (Layer 1, the dropdown / list filter),
* ``_request_root_doctype`` (the discriminator that keeps the dozen cascade-minted
  Stock Entries out of Layer 2),
* ``validate_stock_entry_type_permission`` (Layer 2, the API-bypass block).

The cascade cases are the important ones: a user submitting a Main Slip runs with
``cmd == "frappe.desk.form.save.savedocs"`` just like a user saving a Stock Entry, and
only the payload's doctype tells them apart.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import stock_entry_type as sett


def _se(stock_entry_type="Material Issue", is_new=True, before=None, **flags):
	"""A SimpleNamespace Stock Entry good enough for the validator."""
	doc = SimpleNamespace(
		stock_entry_type=stock_entry_type,
		flags=frappe._dict(flags),
		auto_created=flags.pop("auto_created", 0),
	)
	doc.is_new = lambda: is_new
	doc.get_doc_before_save = lambda: before
	doc.get = lambda key, default=None: getattr(doc, key, default)
	return doc


def _request(path="/api/method/frappe.desk.form.save.savedocs"):
	return SimpleNamespace(path=path)


class TestIsTypeAllowed(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_ungranted_type_is_hidden_from_everyone(self):
		"""Strict whitelist: no rows == nobody, not everybody."""
		with patch.object(sett, "_is_privileged", return_value=False), patch.object(
			sett, "get_allowed_roles", return_value=[]
		), patch.object(sett.frappe, "get_roles", return_value=["All", "Stock User"]):
			self.assertFalse(sett.is_type_allowed("Repack", "u@x"))

	def test_all_role_keeps_a_type_open_to_everyone(self):
		"""The escape hatch: frappe.get_roles() always contains "All"."""
		with patch.object(sett, "_is_privileged", return_value=False), patch.object(
			sett, "get_allowed_roles", return_value=["All"]
		), patch.object(
			sett.frappe, "get_roles", return_value=["All", "Guest", "Some Role"]
		):
			self.assertTrue(sett.is_type_allowed("Repack", "u@x"))

	def test_role_match_allows(self):
		with patch.object(sett, "_is_privileged", return_value=False), patch.object(
			sett, "get_allowed_roles", return_value=["Stock User", "Stock Manager"]
		), patch.object(sett.frappe, "get_roles", return_value=["All", "Stock User"]):
			self.assertTrue(sett.is_type_allowed("Material Issue", "u@x"))

	def test_no_role_match_blocks(self):
		with patch.object(sett, "_is_privileged", return_value=False), patch.object(
			sett, "get_allowed_roles", return_value=["Stock Manager"]
		), patch.object(sett.frappe, "get_roles", return_value=["All", "Stock User"]):
			self.assertFalse(sett.is_type_allowed("Material Issue", "u@x"))

	def test_privileged_always_allowed(self):
		with patch.object(sett, "_is_privileged", return_value=True):
			self.assertTrue(sett.is_type_allowed("Material Issue", "admin"))

	def test_blank_role_rows_are_dropped(self):
		with patch.object(
			sett.frappe, "get_all", return_value=["Stock User", None, ""]
		):
			self.assertEqual(sett.get_allowed_roles("Material Issue"), ["Stock User"])


class TestIsPrivileged(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_administrator_bypasses(self):
		self.assertTrue(sett._is_privileged("Administrator"))

	def test_system_manager_bypasses(self):
		with patch.object(
			sett.frappe, "get_roles", return_value=["All", "System Manager"]
		):
			self.assertTrue(sett._is_privileged("u@x"))

	def test_stock_user_does_not_bypass(self):
		with patch.object(sett.frappe, "get_roles", return_value=["All", "Stock User"]):
			self.assertFalse(sett._is_privileged("u@x"))


class TestPermissionQueryConditions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_privileged_no_restriction(self):
		with patch.object(sett, "_is_privileged", return_value=True):
			self.assertEqual(sett.get_permission_query_conditions("admin"), "")

	def test_condition_is_a_bare_grant_match(self):
		with patch.object(sett, "_is_privileged", return_value=False), patch.object(
			sett.frappe, "get_roles", return_value=["All", "Stock User"]
		), patch.object(sett.frappe.db, "escape", side_effect=lambda v: "'%s'" % v):
			cond = sett.get_permission_query_conditions("u@x")

		# Strict: ONLY granted types match -- no permissive "unconfigured" branch.
		self.assertNotIn("NOT IN", cond)
		self.assertIn("`tabStock Entry Type`.`name` IN", cond)
		self.assertIn("`role` IN ('All', 'Stock User')", cond)
		self.assertIn("`parenttype` = 'Stock Entry Type'", cond)
		self.assertIn("`parentfield` = 'custom_allowed_roles'", cond)

	def test_defaults_to_session_user(self):
		with patch.object(
			sett.frappe, "session", SimpleNamespace(user="sess@x")
		), patch.object(sett, "_is_privileged", return_value=True) as m:
			sett.get_permission_query_conditions()
			m.assert_called_once_with("sess@x")


class TestRequestRootDoctype(IntegrationTestCase):
	"""The discriminator that keeps cascade-minted Stock Entries out of Layer 2."""

	@classmethod
	def setUpClass(cls):
		pass

	def _resolve(self, form_dict, path="/api/method/x"):
		local = SimpleNamespace(
			form_dict=frappe._dict(form_dict), request=_request(path)
		)
		with patch.object(sett.frappe, "local", local):
			return sett._request_root_doctype()

	def test_direct_stock_entry_save(self):
		self.assertEqual(
			self._resolve(
				{
					"cmd": "frappe.desk.form.save.savedocs",
					"doc": '{"doctype": "Stock Entry", "stock_entry_type": "Repack"}',
				}
			),
			"Stock Entry",
		)

	def test_main_slip_cascade_is_not_a_stock_entry_save(self):
		# The killer case: same cmd, different payload doctype.
		self.assertEqual(
			self._resolve(
				{
					"cmd": "frappe.desk.form.save.savedocs",
					"doc": '{"doctype": "Main Slip", "name": "MS-0001"}',
				}
			),
			"Main Slip",
		)

	def test_run_doc_method_is_not_a_direct_save(self):
		self.assertIsNone(self._resolve({"cmd": "run_doc_method", "dt": "Main Slip"}))

	def test_rest_resource_path(self):
		self.assertEqual(
			self._resolve({}, path="/api/resource/Stock%20Entry"), "Stock Entry"
		)

	def test_rest_v2_document_path(self):
		self.assertEqual(
			self._resolve({}, path="/api/v2/document/Stock%20Entry/SE-0001"),
			"Stock Entry",
		)

	def test_malformed_payload_is_not_a_direct_save(self):
		self.assertIsNone(
			self._resolve({"cmd": "frappe.client.insert", "doc": "{not json"})
		)

	def test_dict_like_local_does_not_read_as_cached(self):
		"""A stand-in that returns None for a missing attr must not read as cached.

		frappe.local is a werkzeug Local (AttributeError on a missing attr), but
		anything dict-like returns None -- which as a bare cache value would mean
		"not a direct save" and silently disable Layer 2.
		"""
		local = frappe._dict(
			form_dict=frappe._dict(
				{
					"cmd": "frappe.desk.form.save.savedocs",
					"doc": '{"doctype": "Stock Entry"}',
				}
			),
			request=_request(),
		)
		with patch.object(sett.frappe, "local", local):
			self.assertEqual(sett._request_root_doctype(), "Stock Entry")

	def test_result_is_cached_on_local(self):
		local = SimpleNamespace(
			form_dict=frappe._dict(
				{
					"cmd": "frappe.desk.form.save.savedocs",
					"doc": '{"doctype": "Stock Entry"}',
				}
			),
			request=_request(),
		)
		with patch.object(sett.frappe, "local", local):
			self.assertEqual(sett._request_root_doctype(), "Stock Entry")
			local.form_dict = frappe._dict({})  # would resolve to None if re-read
			self.assertEqual(sett._request_root_doctype(), "Stock Entry")


class TestValidateStockEntryTypePermission(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, doc, allowed=False, direct=True):
		with patch.object(sett, "_is_privileged", return_value=False), patch.object(
			sett, "_is_direct_user_save", return_value=direct
		), patch.object(sett, "is_type_allowed", return_value=allowed), patch.object(
			sett, "get_allowed_roles", return_value=["Stock Manager"]
		), patch.object(sett.frappe, "session", SimpleNamespace(user="u@x")):
			sett.validate_stock_entry_type_permission(doc)

	def test_blocked_type_on_direct_save_throws(self):
		with self.assertRaises(frappe.PermissionError):
			self._run(_se("Repack"))

	def test_ungranted_type_throws_without_an_empty_role_list(self):
		"""A type with no grants at all must not render "restricted to: <blank>"."""
		with patch.object(sett, "_is_privileged", return_value=False), patch.object(
			sett, "_is_direct_user_save", return_value=True
		), patch.object(sett, "is_type_allowed", return_value=False), patch.object(
			sett, "get_allowed_roles", return_value=[]
		), patch.object(sett.frappe, "session", SimpleNamespace(user="u@x")):
			with self.assertRaises(frappe.PermissionError) as ctx:
				sett.validate_stock_entry_type_permission(_se("Repack"))
		self.assertIn("No roles have been granted", str(ctx.exception))

	def test_allowed_type_passes(self):
		self._run(_se("Material Issue"), allowed=True)

	def test_cascade_save_is_skipped(self):
		# Not a direct save -> no throw, even though the type is blocked.
		self._run(_se("Repack"), direct=False)

	def test_empty_type_is_skipped(self):
		self._run(_se(None))

	def test_privileged_user_skipped(self):
		with patch.object(sett, "_is_privileged", return_value=True), patch.object(
			sett.frappe, "session", SimpleNamespace(user="admin")
		):
			sett.validate_stock_entry_type_permission(_se("Repack"))

	def test_unchanged_type_on_existing_doc_passes(self):
		"""A cascade-created draft stays submittable by whoever owns the flow."""
		before = SimpleNamespace(stock_entry_type="Repack")
		self._run(_se("Repack", is_new=False, before=before))

	def test_changed_type_on_existing_doc_throws(self):
		before = SimpleNamespace(stock_entry_type="Material Issue")
		with self.assertRaises(frappe.PermissionError):
			self._run(_se("Repack", is_new=False, before=before))

	def test_missing_doc_before_save_does_not_raise_attributeerror(self):
		# get_doc_before_save() returns None when _doc_before_save was never loaded.
		self._run(_se("Repack", is_new=False, before=None))


class TestIsDirectUserSave(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, doc, root="Stock Entry", request=True, flags=None):
		local = SimpleNamespace(request=_request() if request else None)
		with patch.object(
			sett.frappe, "flags", frappe._dict(flags or {})
		), patch.object(sett.frappe, "local", local), patch.object(
			sett.frappe, "in_test", False
		), patch.object(sett, "_request_root_doctype", return_value=root):
			return sett._is_direct_user_save(doc)

	def test_direct_save(self):
		self.assertTrue(self._run(_se()))

	def test_cascade_root_doctype(self):
		self.assertFalse(self._run(_se(), root="Main Slip"))

	def test_no_request_is_background(self):
		self.assertFalse(self._run(_se(), request=False))

	def test_ignore_permissions_is_skipped(self):
		self.assertFalse(self._run(_se(ignore_permissions=True)))

	def test_auto_created_is_skipped(self):
		doc = _se()
		doc.auto_created = 1
		self.assertFalse(self._run(doc))

	def test_migrate_is_skipped(self):
		self.assertFalse(self._run(_se(), flags={"in_migrate": True}))

	def test_patch_is_skipped(self):
		self.assertFalse(self._run(_se(), flags={"in_patch": True}))
