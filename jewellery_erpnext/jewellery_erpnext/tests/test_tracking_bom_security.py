# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""C13 -- authorization and row-ownership on ``update_tracking_bom_detail``.

The endpoint is ``@frappe.whitelist()`` and was reachable by any authenticated session
with no permission check. Worse, ``_update_child_table`` resolved each row with
``frappe.get_doc(child_doctype, d["docname"])`` -- a GLOBAL lookup by name that never
verified the row belonged to the Tracking Bom the caller named. Anyone who could write to
one Tracking Bom could therefore rewrite child rows of every other one.

Pure-logic per the suite convention: no document is created and every DB access is
patched. What is under test is which row the helper resolves and whether it refuses a
foreign name -- neither needs a database.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import quotation as q_events
from jewellery_erpnext.jewellery_erpnext.doctype.tracking_bom.tracking_bom import (
	_update_child_table,
	update_tracking_bom_detail,
)

MOD = "jewellery_erpnext.jewellery_erpnext.doctype.tracking_bom.tracking_bom"
Q_MOD = "jewellery_erpnext.jewellery_erpnext.doc_events.quotation"


class _Row(frappe._dict):
	"""A child row that records whether it was written to.

	``flags`` is set explicitly: ``frappe._dict`` returns ``None`` for any missing key, so
	without it ``child_doc.flags.ignore_validate_update_after_submit = True`` fails on a
	``NoneType`` rather than exercising the code under test.
	"""

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.flags = frappe._dict()

	def update(self, data):
		super().update(data)
		self.was_updated = True

	def save(self):
		self.was_saved = True


class _Parent(frappe._dict):
	def get(self, key, default=None):
		return super().get(key, default)

	def append(self, table_field, value):
		row = _Row(value or {})
		row.name = "NEW-ROW"
		self.setdefault(table_field, []).append(row)
		return row


def _parent_with(rows):
	return _Parent(
		doctype="Tracking Bom",
		name="TB-MINE",
		metal_detail=rows,
	)


class TestTrackingBomRowOwnership(IntegrationTestCase):
	"""A ``docname`` from another Tracking Bom must be refused, not silently written."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_own_row_is_updated(self):
		mine = _Row(name="ROW-MINE", qty=1)
		parent = _parent_with([mine])

		_update_child_table(
			parent,
			"BOM Metal Detail",
			"metal_detail",
			[{"docname": "ROW-MINE", "qty": 5}],
		)

		self.assertEqual(mine.qty, 5)
		self.assertTrue(mine.was_saved)

	def test_foreign_row_is_refused(self):
		mine = _Row(name="ROW-MINE", qty=1)
		parent = _parent_with([mine])

		with self.assertRaises(frappe.PermissionError):
			_update_child_table(
				parent,
				"BOM Metal Detail",
				"metal_detail",
				[{"docname": "ROW-SOMEONE-ELSES", "qty": 999}],
			)

	def test_foreign_row_is_never_fetched_globally(self):
		"""The regression itself: no global get_doc lookup by row name.

		If this fails, the helper has gone back to resolving rows outside the parent and
		the cross-document write is reachable again.
		"""
		parent = _parent_with([_Row(name="ROW-MINE", qty=1)])

		with patch(f"{MOD}.frappe.get_doc") as get_doc:
			with self.assertRaises(frappe.PermissionError):
				_update_child_table(
					parent,
					"BOM Metal Detail",
					"metal_detail",
					[{"docname": "ROW-SOMEONE-ELSES", "qty": 999}],
				)
		get_doc.assert_not_called()

	def test_foreign_row_leaves_own_rows_untouched(self):
		mine = _Row(name="ROW-MINE", qty=1)
		parent = _parent_with([mine])

		with self.assertRaises(frappe.PermissionError):
			_update_child_table(
				parent,
				"BOM Metal Detail",
				"metal_detail",
				[{"docname": "ROW-SOMEONE-ELSES", "qty": 999}],
			)

		self.assertEqual(mine.qty, 1)
		self.assertIsNone(mine.get("was_updated"))

	def test_row_without_docname_is_appended(self):
		parent = _parent_with([])
		_update_child_table(parent, "BOM Metal Detail", "metal_detail", [{"qty": 3}])
		self.assertEqual(len(parent.metal_detail), 1)
		self.assertEqual(parent.metal_detail[0].qty, 3)


class TestTrackingBomAuthorization(IntegrationTestCase):
	"""The endpoint must demand write permission on the named document."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_permission_is_checked_against_the_named_document(self):
		doc = MagicMock()
		doc.doctype = "Tracking Bom"

		with (
			patch(f"{MOD}.frappe.get_doc", return_value=doc),
			patch(f"{MOD}.frappe.has_permission") as has_permission,
		):
			update_tracking_bom_detail("TB-MINE")

		has_permission.assert_called_once()
		args, kwargs = has_permission.call_args
		self.assertEqual(args[0], "Tracking Bom")
		self.assertEqual(args[1], "write")
		self.assertIs(kwargs.get("doc"), doc)
		self.assertTrue(kwargs.get("throw"))

	def test_permission_failure_prevents_any_write(self):
		doc = MagicMock()
		doc.doctype = "Tracking Bom"

		with (
			patch(f"{MOD}.frappe.get_doc", return_value=doc),
			patch(
				f"{MOD}.frappe.has_permission",
				side_effect=frappe.PermissionError,
			),
			patch(f"{MOD}._update_child_table") as update_child,
		):
			with self.assertRaises(frappe.PermissionError):
				update_tracking_bom_detail(
					"TB-MINE", metal_detail='[{"docname": "ROW-X", "qty": 9}]'
				)

		update_child.assert_not_called()
		doc.save.assert_not_called()


# ------------------------------------------------------------------ update_bom_detail
class _BomParent(_Parent):
	"""Stands in for a BOM / Tracking Bom parent."""


def _bom_parent(rows, doctype="BOM", name="BOM-1"):
	parent = _BomParent(doctype=doctype, name=name, metal_detail=rows)
	parent.company = "CG Test Co"
	parent.reload = lambda: None
	parent.save = lambda: None
	return parent


class TestUpdateBomDetailRowOwnership(IntegrationTestCase):
	"""C13 on the endpoint that is actually reachable.

	``update_bom_detail`` is called from six live forms, while the sibling
	``update_tracking_bom_detail`` has no callers at all. The five child DocTypes it writes
	are SHARED -- attached to ERPNext's ``BOM`` by ``custom_fields/bom.json`` and to gke's
	``Order`` / ``Repair Order`` -- so a row name addresses far more than the named parent.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_own_row_is_updated(self):
		mine = _Row(name="ROW-MINE", qty=1)
		parent = _bom_parent([mine])
		q_events.update_table(
			parent,
			"BOM Metal Detail",
			"metal_detail",
			{"docname": "ROW-MINE", "qty": 5},
		)
		self.assertEqual(mine.qty, 5)

	def test_foreign_row_is_refused(self):
		parent = _bom_parent([_Row(name="ROW-MINE", qty=1)])
		with self.assertRaises(frappe.PermissionError):
			q_events.update_table(
				parent,
				"BOM Metal Detail",
				"metal_detail",
				{"docname": "ROW-UNDER-ANOTHER-BOM", "qty": 999},
			)

	def test_foreign_row_is_never_fetched_globally(self):
		"""The regression itself -- no global get_doc lookup by row name."""
		parent = _bom_parent([_Row(name="ROW-MINE", qty=1)])
		with patch(f"{Q_MOD}.frappe.get_doc") as get_doc:
			with self.assertRaises(frappe.PermissionError):
				q_events.update_table(
					parent,
					"BOM Metal Detail",
					"metal_detail",
					{"docname": "ROW-UNDER-ANOTHER-BOM", "qty": 999},
				)
		get_doc.assert_not_called()

	def test_framework_fields_are_stripped(self):
		"""``parenttype`` is the dangerous one.

		``frappe.permissions.has_child_permission`` reads it from the row IN MEMORY, so a
		caller able to set it chooses which doctype their own permission is checked
		against -- steering the check meant to police them.
		"""
		mine = _Row(
			name="ROW-MINE", qty=1, parenttype="BOM", parent="BOM-1", docstatus=1
		)
		parent = _bom_parent([mine])

		q_events.update_table(
			parent,
			"BOM Metal Detail",
			"metal_detail",
			{
				"docname": "ROW-MINE",
				"qty": 7,
				"parenttype": "Quotation",
				"parent": "QTN-ELSEWHERE",
				"parentfield": "items",
				"docstatus": 0,
				"owner": "attacker@example.com",
			},
		)

		self.assertEqual(mine.qty, 7)
		self.assertEqual(mine.parenttype, "BOM")
		self.assertEqual(mine.parent, "BOM-1")
		self.assertEqual(mine.docstatus, 1)
		self.assertNotEqual(mine.owner, "attacker@example.com")

	def test_row_without_docname_is_appended(self):
		parent = _bom_parent([])
		q_events.update_table(parent, "BOM Metal Detail", "metal_detail", {"qty": 3})
		self.assertEqual(len(parent.metal_detail), 1)
		self.assertEqual(parent.metal_detail[0].qty, 3)


class TestUpdateBomDetailAuthorization(IntegrationTestCase):
	"""Parent allowlist and write permission."""

	@classmethod
	def setUpClass(cls):
		pass

	def _call(self, parent_doctype):
		return q_events.update_bom_detail(
			parent_doctype, "X-1", "[]", "[]", "[]", "[]", "[]"
		)

	def test_only_bom_and_tracking_bom_are_accepted(self):
		self.assertEqual(
			set(q_events.BOM_DETAIL_PARENTS),
			{"BOM", "Tracking Bom"},
			"the allowlist must match what the six call sites actually pass",
		)

	def test_foreign_parent_doctype_is_refused_before_any_load(self):
		with patch(f"{Q_MOD}.frappe.get_doc") as get_doc:
			with self.assertRaises(frappe.PermissionError):
				self._call("User")
		get_doc.assert_not_called()

	def test_permission_is_checked_on_the_named_parent(self):
		doc = MagicMock()
		# The five detail setters are patched out: this case asserts only that
		# authorization happens on the named parent, and a MagicMock parent would
		# otherwise reach frappe.db.get_value("Company", parent.company, ...).
		with (
			patch(f"{Q_MOD}.frappe.get_doc", return_value=doc),
			patch(f"{Q_MOD}.frappe.has_permission") as has_permission,
			patch(f"{Q_MOD}.set_metal_detail"),
			patch(f"{Q_MOD}.set_diamond_detail"),
			patch(f"{Q_MOD}.set_gemstone_detail"),
			patch(f"{Q_MOD}.set_finding_detail"),
			patch(f"{Q_MOD}.set_other_detail"),
			patch(f"{Q_MOD}.update_totals"),
		):
			self._call("BOM")

		has_permission.assert_called_once()
		args, kwargs = has_permission.call_args
		self.assertEqual(args[0], "BOM")
		self.assertEqual(args[1], "write")
		self.assertIs(kwargs.get("doc"), doc)
		self.assertTrue(kwargs.get("throw"))

	def test_permission_failure_prevents_any_write(self):
		doc = MagicMock()
		with (
			patch(f"{Q_MOD}.frappe.get_doc", return_value=doc),
			patch(f"{Q_MOD}.frappe.has_permission", side_effect=frappe.PermissionError),
			patch(f"{Q_MOD}.set_metal_detail") as set_metal,
			patch(f"{Q_MOD}.update_totals") as update_totals,
		):
			with self.assertRaises(frappe.PermissionError):
				self._call("BOM")

		set_metal.assert_not_called()
		update_totals.assert_not_called()
		doc.save.assert_not_called()

	def test_endpoint_is_post_only(self):
		"""A GET-callable write endpoint skips CSRF validation entirely.

		``frappe.whitelist`` records the allowed verbs in a module-level registry rather
		than on the function, so that is what has to be asserted.
		"""
		allowed = frappe.allowed_http_methods_for_whitelisted_func.get(
			q_events.update_bom_detail
		)
		self.assertEqual(
			allowed, ["POST"], "update_bom_detail must be POST-only (CSRF)"
		)

	def test_tracking_bom_endpoint_is_also_post_only(self):
		allowed = frappe.allowed_http_methods_for_whitelisted_func.get(
			update_tracking_bom_detail
		)
		self.assertEqual(allowed, ["POST"])
