# Copyright (c) 2023, Nirali and Contributors
# See license.txt

"""Controller-level coverage for Tree Number.

Kept to in-memory documents (``frappe.new_doc``) so it runs on a bare CI site: nothing here
needs casting master data, which ``create_test_data`` does not seed.

The ledger arithmetic itself is pinned in
``jewellery_erpnext.jewellery_erpnext.tests.test_tree_material_balance``; these tests check
that the controller actually routes through it.
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_material_balance as tree_balance,
)
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number import (
	tree_number as tree_number_module,
)
from jewellery_erpnext.jewellery_erpnext.doctype.tree_number.tree_number import (
	build_work_order_summary,
)


class TestTreeNumber(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _tree(self, rows):
		"""In-memory Tree Number carrying the given (item, issue, receive, loss) rows."""
		doc = frappe.new_doc("Tree Number")
		for item, issue, receive, loss in rows:
			doc.append(
				"material_details",
				{
					"item_code": item,
					"issue_qty": issue,
					"receive_qty": receive,
					"loss_qty": loss,
					"pending_qty": 0,
				},
			)
		return doc

	def test_calculate_material_pending_derives_each_row(self):
		doc = self._tree([("ITEM-A", 2.900, 2.390, 0), ("ITEM-B", 5.0, 4.0, 1.0)])
		doc.calculate_material_pending()
		self.assertAlmostEqual(doc.material_details[0].pending_qty, 0.510, places=3)
		self.assertAlmostEqual(doc.material_details[1].pending_qty, 0.0, places=3)

	def test_pending_is_not_floored(self):
		# The old casting branch clamped this to 0 and hid the over-draw that produced
		# GEPL-TR-26-00154. It must now read negative.
		doc = self._tree([("ITEM-A", 0, 2.360, 0)])
		doc.employee_ir = "EIR-DOES-NOT-MATTER"
		doc.calculate_material_pending()
		self.assertAlmostEqual(doc.material_details[0].pending_qty, -2.360, places=3)

	def test_validate_rejects_receive_without_issue(self):
		doc = self._tree([("ITEM-A", 0, 2.360, 0)])
		with self.assertRaises(ValidationError):
			tree_balance.validate_row_balance(doc)

	def test_validate_accepts_a_balanced_ledger(self):
		doc = self._tree([("ITEM-A", 2.900, 2.390, 0)])
		doc.calculate_material_pending()
		tree_balance.validate_row_balance(doc)

	def test_status_helper_is_the_controller_source(self):
		doc = self._tree([("ITEM-A", 2.900, 2.390, 0)])
		doc.calculate_material_pending()
		self.assertEqual(
			tree_balance.tree_status(doc), tree_balance.STATUS_PARTIALLY_RECEIVED
		)

	def test_zero_ledger_is_draft_not_received(self):
		doc = self._tree([("ITEM-A", 0, 0, 0)])
		doc.calculate_material_pending()
		self.assertEqual(tree_balance.tree_status(doc), tree_balance.STATUS_DRAFT)


def _issued(*mwos):
	return [{"manufacturing_work_order": m, "item_code": f"ITEM-{m}"} for m in mwos]


def _received(mwo, eir, on, gross=2.5):
	return {
		"manufacturing_work_order": mwo,
		"receive_eir": eir,
		"received_on": on,
		"received_gross_wt": gross,
	}


class TestTreeWorkOrderSummary(unittest.TestCase):
	"""``build_work_order_summary``: pure set logic behind the Tree Number Summary section."""

	def _counts(self, summary):
		return summary["issued"], summary["received"], summary["pending"]

	def test_all_received(self):
		summary = build_work_order_summary(
			_issued("MWO-1", "MWO-2"),
			[
				_received("MWO-1", "EIR-R1", "2026-11-01 10:00:00"),
				_received("MWO-2", "EIR-R1", "2026-11-01 10:00:00"),
			],
		)
		self.assertEqual(self._counts(summary), (2, 2, 0))
		self.assertTrue(all(d["status"] == "Received" for d in summary["work_orders"]))

	def test_partly_received_lists_pending_first(self):
		summary = build_work_order_summary(
			_issued("MWO-1", "MWO-2", "MWO-3"),
			[_received("MWO-1", "EIR-R1", "2026-11-01 10:00:00")],
		)
		self.assertEqual(self._counts(summary), (3, 1, 2))
		self.assertEqual(
			[
				(d["manufacturing_work_order"], d["status"])
				for d in summary["work_orders"]
			],
			[("MWO-2", "Pending"), ("MWO-3", "Pending"), ("MWO-1", "Received")],
		)
		self.assertIsNone(summary["work_orders"][0]["receive_eir"])
		self.assertIsNone(summary["work_orders"][0]["received_gross_wt"])

	def test_nothing_received(self):
		summary = build_work_order_summary(_issued("MWO-1"), [])
		self.assertEqual(self._counts(summary), (1, 0, 1))

	def test_repeat_receive_counts_once_and_keeps_the_earliest(self):
		summary = build_work_order_summary(
			_issued("MWO-1", "MWO-1"),
			[
				_received("MWO-1", "EIR-R2", "2026-11-03 09:00:00", 9.9),
				_received("MWO-1", "EIR-R1", "2026-11-01 09:00:00", 3.2),
			],
		)
		self.assertEqual(self._counts(summary), (1, 1, 0))
		self.assertEqual(summary["work_orders"][0]["receive_eir"], "EIR-R1")
		self.assertEqual(summary["work_orders"][0]["received_gross_wt"], 3.2)

	def test_receive_for_a_work_order_not_issued_is_ignored(self):
		summary = build_work_order_summary(
			_issued("MWO-1"), [_received("MWO-OTHER", "EIR-R1", "2026-11-01 09:00:00")]
		)
		self.assertEqual(self._counts(summary), (1, 0, 1))
		self.assertEqual(len(summary["work_orders"]), 1)

	def test_tree_without_an_issue_is_all_zero(self):
		summary = build_work_order_summary([], [_received("MWO-1", "EIR-R1", None)])
		self.assertEqual(self._counts(summary), (0, 0, 0))
		self.assertEqual(summary["work_orders"], [])

	# --- the SQL readers and the whitelisted method --------------------------------------------
	# Mock-only: each patch covers one call, so nothing else (DocType meta, translations) is hijacked.

	def _sql_call(self, reader, *args):
		db = MagicMock()
		db.sql.return_value = [{"manufacturing_work_order": "MWO-1"}]
		with patch.object(frappe, "db", db):
			result = reader(*args)
		return db.sql, result

	def test_issued_reader_without_an_issue_runs_no_query(self):
		sql, result = self._sql_call(tree_number_module._tree_issued_rows, None)
		self.assertEqual(result, [])
		sql.assert_not_called()

	def test_receive_reader_without_work_orders_runs_no_query(self):
		sql, result = self._sql_call(
			tree_number_module._tree_receive_rows, "TR-1", "Casting", []
		)
		self.assertEqual(result, [])
		sql.assert_not_called()

	def test_issued_reader_reads_the_submitted_issue_by_parent(self):
		sql, result = self._sql_call(tree_number_module._tree_issued_rows, "EIR-1")
		self.assertEqual(result, [{"manufacturing_work_order": "MWO-1"}])
		query, params = sql.call_args.args
		self.assertEqual(params, {"eir": "EIR-1"})
		for clause in (
			"eiro.parent = %(eir)s",
			"eir.type = 'Issue'",
			"eir.docstatus = 1",
		):
			self.assertIn(clause, query)
		self.assertTrue(sql.call_args.kwargs.get("as_dict"))

	def test_receive_reader_keeps_to_this_tree_and_its_operation(self):
		sql, _result = self._sql_call(
			tree_number_module._tree_receive_rows, "TR-1", "Casting", ["MWO-2", "MWO-1"]
		)
		query, params = sql.call_args.args
		self.assertEqual(
			params, {"mwos": ("MWO-2", "MWO-1"), "tree": "TR-1", "operation": "Casting"}
		)
		for clause in (
			"eiro.manufacturing_work_order IN %(mwos)s",
			"eiro.tree_number = %(tree)s",
			"eir.operation = %(operation)s",
			"eir.type = 'Receive'",
			"eir.docstatus = 1",
		):
			self.assertIn(clause, query)

	def _fake_tree(self):
		tree = MagicMock()
		tree.name, tree.employee_ir, tree.operation = "TR-1", "EIR-1", "Casting"
		return tree

	def test_endpoint_checks_read_permission_and_wires_the_readers(self):
		tree = self._fake_tree()
		with (
			patch.object(frappe, "get_doc", return_value=tree) as get_doc,
			patch.object(
				tree_number_module,
				"_tree_issued_rows",
				return_value=_issued("MWO-2", "MWO-1", "MWO-2"),
			) as issued,
			patch.object(
				tree_number_module,
				"_tree_receive_rows",
				return_value=[_received("MWO-1", "EIR-R1", "2026-11-01 10:00:00")],
			) as received,
		):
			summary = tree_number_module.get_work_order_summary("TR-1")
		get_doc.assert_called_once_with("Tree Number", "TR-1")
		tree.check_permission.assert_called_once_with("read")
		issued.assert_called_once_with("EIR-1")
		received.assert_called_once_with("TR-1", "Casting", ["MWO-1", "MWO-2"])
		self.assertEqual(self._counts(summary), (2, 1, 1))

	def test_endpoint_without_read_permission_reads_nothing(self):
		tree = self._fake_tree()
		tree.check_permission.side_effect = frappe.PermissionError
		with (
			patch.object(frappe, "get_doc", return_value=tree),
			patch.object(tree_number_module, "_tree_issued_rows") as issued,
			patch.object(tree_number_module, "_tree_receive_rows") as received,
		):
			with self.assertRaises(frappe.PermissionError):
				tree_number_module.get_work_order_summary("TR-1")
		issued.assert_not_called()
		received.assert_not_called()
