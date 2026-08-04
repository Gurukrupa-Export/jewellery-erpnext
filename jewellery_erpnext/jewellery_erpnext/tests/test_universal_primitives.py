# Copyright (c) 2026, Gurukrupa Exports and Contributors
# See license.txt

"""Guards against universal read/write primitives on the whitelisted surface.

Two endpoints took a caller-supplied `(doctype, docname)` and acted on it with no
permission check, which makes them oracles over the entire database rather than
features of the DocType they happen to live on:

* `utils.db_get_value(doctype, docname, fields)` — a universal READ oracle. Its own
  comment said it existed "to bypass permission issue during db call from client
  script". `frappe.db.get_value` applies no permission layer, so any authenticated
  session could read any column of any DocType. Replaced with a scoped endpoint.

* `parent_manufacturing_order.add_hold_comment(doctype, docname, reason)` — a
  universal WRITE primitive: `frappe.get_doc` with no check, then `add_comment`.
  Now gated on read, matching `frappe.desk.form.utils.add_comment`.

The test that matters most is `test_no_unscoped_doctype_docname_endpoint`, which
generalises the class instead of pinning the two known instances.
"""

import ast
import inspect
from pathlib import Path

import frappe
from frappe.tests import UnitTestCase

from jewellery_erpnext import utils
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order import (
	parent_manufacturing_order as pmo,
)

APP_ROOT = Path(frappe.get_app_path("jewellery_erpnext"))

# Endpoints that legitimately take a doctype/docname pair AND already gate access.
# Additions here are a review decision.
ALLOWED = {
	"add_hold_comment",  # gated via doc.check_permission(), see below
}


class TestUniversalPrimitives(UnitTestCase):
	def test_universal_read_oracle_is_gone(self):
		self.assertFalse(
			hasattr(utils, "db_get_value"),
			"utils.db_get_value read any column of any DocType with no permission "
			"check; it must not come back",
		)

	def test_replacement_is_scoped_and_gated(self):
		self.assertTrue(hasattr(utils, "get_department_ir_transfer_departments"))
		source = inspect.getsource(utils.get_department_ir_transfer_departments)
		self.assertIn("has_permission", source, "replacement must check permission")
		self.assertIn(
			'"Department IR"', source, "replacement must be scoped to one DocType"
		)
		self.assertNotIn(
			"doctype,",
			inspect.signature(utils.get_department_ir_transfer_departments).__str__(),
			"replacement must not accept a caller-supplied doctype",
		)

	def test_add_hold_comment_checks_permission(self):
		source = inspect.getsource(pmo.add_hold_comment)
		self.assertIn(
			"check_permission",
			source,
			"add_hold_comment takes a caller-supplied doctype/docname and writes to the "
			"document timeline; without a check it is a write primitive over every DocType",
		)
		# and the check must precede the write
		self.assertLess(
			source.find("check_permission"),
			source.find("add_comment("),
			"the permission check must run before the comment is attached",
		)

	def test_add_hold_comment_does_not_log_caller_input(self):
		"""It logged all three caller-supplied arguments at info level on every call."""
		source = inspect.getsource(pmo.add_hold_comment)
		self.assertNotIn(
			"add_hold_comment called with",
			source,
			"caller-controlled arguments should not be written to the log on every call",
		)

	def test_no_unscoped_doctype_docname_endpoint(self):
		"""Generalises the defect class rather than pinning the two known instances.

		Any whitelisted function accepting BOTH a `doctype` and a `docname`/`name`
		parameter is a whole-database primitive unless it checks permission. This is
		the shape that produced both findings.
		"""
		offenders = []
		for path in sorted(APP_ROOT.rglob("*.py")):
			if "/tests/" in str(path):
				continue
			try:
				tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
			except SyntaxError:
				continue
			source = path.read_text(encoding="utf-8", errors="replace")
			for node in ast.walk(tree):
				if not isinstance(node, ast.FunctionDef):
					continue
				if not any("whitelist" in ast.dump(d) for d in node.decorator_list):
					continue
				params = {a.arg for a in node.args.args}
				if "doctype" not in params:
					continue
				if not params & {"docname", "name", "document_name"}:
					continue
				body = ast.get_source_segment(source, node) or ""
				gated = "has_permission" in body or "check_permission" in body
				if not gated and node.name not in ALLOWED:
					offenders.append(f"{path.relative_to(APP_ROOT.parent)}:{node.name}")

		self.assertEqual(
			offenders,
			[],
			"whitelisted endpoints taking caller-supplied (doctype, docname) with no "
			f"permission check — these act on the whole database: {offenders}",
		)


class TestRefiningEntryExposure(UnitTestCase):
	"""Refining Entry grants read to the `All` role, which includes website users.

	`frappe/permissions.py`: `ALL_USER_ROLE = "All"  # This includes website users too.`
	It is the only DocType in this app with such a permission row. Doc-bound whitelisted
	methods are reached through `run_doc_method`, which checks only READ — so on this
	DocType alone, "read" was sufficient to drive ledger mutations: eleven whitelisted
	methods performed insert/submit/save/db_set with no further check.

	The permission row itself is left in place deliberately — removing it is a schema
	change requiring `bench migrate`, and revoking portal read may break legitimate
	access. The exposure is closed in code instead, by requiring `write` on the
	document being mutated.
	"""

	def test_every_mutating_endpoint_requires_write(self):
		from jewellery_erpnext.refining.doctype.refining_entry import (
			refining_entry as re_mod,
		)

		source = inspect.getsource(re_mod)
		tree = ast.parse(source)
		ungated = []
		for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
			for node in cls.body:
				if not isinstance(node, ast.FunctionDef):
					continue
				if not any("whitelist" in ast.dump(d) for d in node.decorator_list):
					continue
				body = ast.get_source_segment(source, node) or ""
				mutates = any(
					tok in body
					for tok in (
						".insert(",
						".submit(",
						".save(",
						"db_set(",
						"set_value(",
					)
				)
				gated = "check_permission" in body or "has_permission" in body
				if mutates and not gated:
					ungated.append(node.name)

		self.assertEqual(
			ungated,
			[],
			"whitelisted Refining Entry methods that mutate with only a read gate — "
			f"reachable by any website user via the 'All' permission row: {ungated}",
		)

	def test_all_role_read_row_is_still_present_and_tracked(self):
		"""If the permission row is ever removed, the code guards become belt-and-braces
		rather than the sole defence — worth knowing, so this asserts the current state
		rather than the desired one."""
		import json

		path = (
			APP_ROOT / "refining" / "doctype" / "refining_entry" / "refining_entry.json"
		)
		perms = json.loads(path.read_text())["permissions"]
		all_rows = [p for p in perms if p.get("role") == "All"]
		self.assertEqual(
			len(all_rows),
			1,
			"expected exactly one 'All' permission row on Refining Entry; if this "
			"changed, re-read the exposure analysis in this test's docstring",
		)
		self.assertEqual(
			{k for k, v in all_rows[0].items() if v is True or v == 1} - {"role"},
			{"read"},
			"the 'All' row must remain read-only",
		)
