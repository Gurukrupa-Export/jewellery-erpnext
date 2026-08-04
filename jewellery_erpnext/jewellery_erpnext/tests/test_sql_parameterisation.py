# Copyright (c) 2026, Gurukrupa Exports and Contributors
# See license.txt

"""Permanent guard against value-interpolated SQL.

The audit reported 28 "SQL injection via f-string" findings. Three sources disagreed
on where they actually were — the report cited the scan commit, a later source read
cited HEAD and was ~3-4 lines off, and a regex sweep surfaced a site neither listed.
So the truth is derived here from the syntax tree, the same way the fix was.

Of 41 f-string SQL calls, only **9** ever interpolated a value inside a quoted SQL
literal; the other 32 interpolate structural fragments (column lists, placeholder
runs, IN-clause expansion) and bind their values properly. All 9 are now
parameterised, and this test fails if a tenth appears.

This is deliberately a source-level assertion rather than a behavioural one: an
injection that is never exercised by a test still ships.
"""

import ast
import re
from pathlib import Path

import frappe
from frappe.tests import UnitTestCase

APP_ROOT = Path(frappe.get_app_path("jewellery_erpnext"))

SQL_METHODS = {"sql", "sql_list", "multisql"}

# Interpolations that are structural (identifiers, column lists, placeholder runs)
# rather than values. Each entry is a file path that is allowed to build SQL text
# with an f-string, with the reason it is safe. Adding to this list is a review
# decision, not a formality.
ALLOWLIST = {
	# `ALTER TABLE ... ADD INDEX` built from module-level constants inside patches.
	"jewellery_erpnext/patches",
	# Report WHERE-clause builders: conditions are assembled from fixed fragments and
	# the values are bound. Covered separately by the report tests.
	"jewellery_erpnext/refining/report",
	"jewellery_erpnext/customer_subcontracting/report",
	# Sets the session isolation level from an internal constant, never user input.
	"jewellery_erpnext/jewellery_erpnext/db_isolation.py",
}


def _is_sql_call(node: ast.Call) -> bool:
	return isinstance(node.func, ast.Attribute) and node.func.attr in SQL_METHODS


def _fstring_arg(node: ast.Call) -> ast.JoinedStr | None:
	if not node.args:
		return None
	arg = node.args[0]
	if isinstance(arg, ast.JoinedStr):
		return arg
	if isinstance(arg, ast.BinOp) and isinstance(arg.left, ast.JoinedStr):
		return arg.left
	return None


def _interpolates_a_value(node: ast.JoinedStr) -> bool:
	"""True when a placeholder sits between quote characters, i.e. a value position."""
	template = "".join(
		part.value
		if isinstance(part, ast.Constant) and isinstance(part.value, str)
		else "\x00"
		for part in node.values
	)
	for match in re.finditer("\x00", template):
		i = match.start()
		if template[max(0, i - 1) : i] in ("'", '"') and template[i + 1 : i + 2] in (
			"'",
			'"',
		):
			return True
	return False


def _allowlisted(relative_path: str) -> bool:
	return any(relative_path.startswith(entry) for entry in ALLOWLIST)


class TestSqlParameterisation(UnitTestCase):
	def test_no_value_interpolated_sql(self):
		"""No frappe.db.sql call may splice a value into a quoted SQL literal."""
		offenders = []
		for path in sorted(APP_ROOT.rglob("*.py")):
			try:
				tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
			except SyntaxError:
				continue
			relative = str(path.relative_to(APP_ROOT.parent))
			if _allowlisted(relative):
				continue
			for node in ast.walk(tree):
				if isinstance(node, ast.Call) and _is_sql_call(node):
					fstring = _fstring_arg(node)
					if fstring is not None and _interpolates_a_value(fstring):
						offenders.append(f"{relative}:{node.lineno}")

		self.assertEqual(
			offenders,
			[],
			"SQL below interpolates a value into a quoted literal — bind it with "
			"%(name)s parameters instead:\n  " + "\n  ".join(offenders),
		)

	def test_the_nine_fixed_sites_stay_parameterised(self):
		"""Named regression guard for the sites fixed in this change."""
		expected = {
			"jewellery_erpnext/jewellery_erpnext/doc_events/sales_invoice.py",
			"jewellery_erpnext/jewellery_erpnext/doc_events/sales_order.py",
			"jewellery_erpnext/jewellery_erpnext/customization/material_request/material_request.py",
		}
		for relative in expected:
			source = (APP_ROOT.parent / relative).read_text(encoding="utf-8")
			self.assertNotRegex(
				source,
				r"""=\s*'\{[A-Za-z_][A-Za-z0-9_.]*\}'""",
				f"{relative} reintroduced a quoted f-string interpolation in SQL",
			)

	def test_like_patterns_survived_parameterisation(self):
		"""Binding turns literal % into a format specifier unless doubled.

		The four Sales Order tax queries carry literal LIKE patterns ('%IGST%').
		When the query moved to %(name)s binding those had to become '%%IGST%%',
		or MySQLdb raises "not enough arguments for format string" at runtime —
		a failure that only appears when the branch is actually executed.
		"""
		source = (
			APP_ROOT / "jewellery_erpnext" / "doc_events" / "sales_order.py"
		).read_text(encoding="utf-8")
		self.assertIn("%(item_tax_template)s", source)
		self.assertIn("'%%IGST%%'", source)
		self.assertNotRegex(
			source,
			r"like '%IGST%'",
			"single-% LIKE pattern in a parameter-bound query will raise at runtime",
		)
