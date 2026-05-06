# Copyright (c) 2026, Nirali and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
	update_balance_table,
)


class TestStockEntryLegacyBalanceTable(FrappeTestCase):
	def test_update_balance_table_appends_rows_per_legacy_key(self):
		mop_doc = MagicMock()
		mop_doc.append = MagicMock()
		mop_doc.save = MagicMock()

		row = {"name": "sed-1", "item_code": "M-X", "idx": 1}

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.get_doc",
			return_value=mop_doc,
		):
			update_balance_table(
				{
					"MOP-TEST-001": {
						"department_source_table": [row],
						"department_target_table": [],
						"employee_source_table": [],
						"employee_target_table": [],
					}
				}
			)

		mop_doc.append.assert_called()
		mop_doc.save.assert_called_once()
		first_table = mop_doc.append.call_args_list[0][0][0]
		self.assertEqual(first_table, "department_source_table")


class TestMopLogStockEntryWriter(FrappeTestCase):
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.db.sql")
	@patch("jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log.frappe.new_doc")
	def test_create_mop_log_for_stock_transfer_sets_stock_entry_voucher(
		self, mock_new_doc, mock_sql
	):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
			create_mop_log_for_stock_transfer_to_mo,
		)

		mock_sql.return_value = [
			{
				"sum_pcs_prefix": 0,
				"sum_pcs_item": 0,
				"sum_pcs_batch": 0,
				"sum_qty_prefix": 0.0,
				"sum_qty_item": 0.0,
				"sum_qty_batch": 0.0,
			}
		]
		mock_log = MagicMock()
		mock_new_doc.return_value = mock_log

		se = frappe._dict(name="SE-TEST-001", manufacturing_work_order="MWO-1")
		row = frappe._dict(
			name="sed-1",
			item_code="M-G-22KT-TEST",
			pcs=0,
			qty=1.0,
			batch_no="B1",
			s_warehouse="WH-A",
			t_warehouse="WH-B",
			manufacturing_operation="MOP-1",
		)

		create_mop_log_for_stock_transfer_to_mo(se, row, is_synced=False)

		self.assertEqual(mock_log.voucher_type, "Stock Entry")
		self.assertEqual(mock_log.voucher_no, "SE-TEST-001")
		self.assertEqual(mock_log.manufacturing_operation, "MOP-1")
		mock_log.save.assert_called_once()


class TestMopLineageAuditProofPack(FrappeTestCase):
	def test_get_sql_proof_templates_keys(self):
		from jewellery_erpnext.mop_lineage_audit import get_sql_proof_templates

		tpl = get_sql_proof_templates()
		self.assertIn("dir_duplicate_department_ir_mop_logs", tpl)
		self.assertIn("stock_entry_multiple_mops_same_voucher", tpl)
		self.assertIn("snc_submitted_empty_source_table", tpl)

	def test_stock_entry_trace_has_legacy_keys(self):
		from jewellery_erpnext.mop_lineage_audit import (
			get_stock_entry_legacy_balance_table_trace,
		)

		trace = get_stock_entry_legacy_balance_table_trace()
		self.assertIn("department_source_table", trace["legacy_keys_in_mop_data"])


class TestStockEntryMopLogBridge(FrappeTestCase):
	"""Cover the Stock Entry -> MOP Log bridge that fixes diamond visibility
	on Employee IR Receive after MR-driven bagging transfers."""

	def _se(self, items):
		return SimpleNamespace(
			name="SE-BRIDGE-001",
			manufacturing_work_order="MWO-1",
			items=items,
		)

	def _row(self, **overrides):
		base = dict(
			name="sed-d-1",
			item_code="D-1CT-VS-G",
			pcs=2,
			qty=0.4,
			batch_no="DBATCH-1",
			s_warehouse="WH-Bag",
			t_warehouse="WH-Trishul",
			manufacturing_operation="MOP-Trishul-1",
			serial_and_batch_bundle=None,
		)
		base.update(overrides)
		return frappe._dict(base)

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.exists",
		return_value=False,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.create_mop_log"
	)
	def test_bridge_writes_one_log_per_mop_bound_row(self, mock_create, mock_exists):
		from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
			sync_mop_log_for_stock_entry,
		)

		se = self._se(
			[
				self._row(),
				self._row(name="sed-d-2", item_code="G-EM-RD"),
				self._row(name="sed-no-mop", manufacturing_operation=None),
			]
		)
		sync_mop_log_for_stock_entry(se)

		self.assertEqual(mock_create.call_count, 2)
		for call in mock_create.call_args_list:
			args, kwargs = call
			self.assertIs(args[0], se)
			self.assertTrue(kwargs.get("is_synced"))

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.exists",
		return_value=True,
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.create_mop_log"
	)
	def test_bridge_is_idempotent_when_log_already_exists(self, mock_create, mock_exists):
		from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
			sync_mop_log_for_stock_entry,
		)

		se = self._se([self._row()])
		sync_mop_log_for_stock_entry(se)
		mock_create.assert_not_called()

	@patch("jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.frappe.db.sql")
	def test_cancel_path_marks_logs_cancelled(self, mock_sql):
		from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
			sync_mop_log_for_stock_entry,
		)

		se = self._se([self._row()])
		sync_mop_log_for_stock_entry(se, is_cancelled=True)
		mock_sql.assert_called_once()
		sql, params = mock_sql.call_args[0]
		self.assertIn("UPDATE `tabMOP Log`", sql)
		self.assertIn("is_cancelled = 1", sql)
		self.assertEqual(params, ("SE-BRIDGE-001",))


class TestEmployeeIrReceiveDiamondParity(FrappeTestCase):
	"""Server-side row builder for Employee IR must surface diamond / gemstone
	header weights so Receive does not render blank diamond columns."""

	def test_get_manufacturing_operations_appends_diamond_fields(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			get_manufacturing_operations,
		)

		target = MagicMock()
		target.get.return_value = []
		fake_op = {
			"gross_wt": 12.5,
			"manufacturing_work_order": "MWO-1",
			"diamond_wt": 0.4,
			"diamond_pcs": 2,
			"gemstone_wt": 0.1,
			"gemstone_pcs": 1,
		}
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir.frappe.db.get_value",
			return_value=fake_op,
		):
			get_manufacturing_operations("MOP-1", target_doc=target)

		target.append.assert_called_once()
		_, payload = target.append.call_args[0]
		self.assertEqual(payload["diamond_wt"], 0.4)
		self.assertEqual(payload["diamond_pcs"], 2)
		self.assertEqual(payload["gemstone_wt"], 0.1)
		self.assertEqual(payload["gemstone_pcs"], 1)


class TestEmployeeIrDiamondLineageAudit(FrappeTestCase):
	"""Audit helper used during staging verification of the failing chain."""

	def test_diagnoses_missing_mop_log_when_se_exists(self):
		from jewellery_erpnext import mop_lineage_audit

		with patch.object(
			mop_lineage_audit.frappe.db, "get_value", return_value=None
		), patch.object(
			mop_lineage_audit.frappe.db, "sql"
		) as mock_sql:
			mock_sql.side_effect = [
				[("EIR-ISSUE-1",)],
				[],
				[],
				[
					{
						"stock_entry": "SE-1",
						"stock_entry_type": "Material Transfer (WORK ORDER)",
						"docstatus": 1,
						"item_code": "D-1CT",
						"batch_no": "B1",
						"qty": 0.4,
						"pcs": 2,
						"uom": "Carat",
						"s_warehouse": "Bag",
						"t_warehouse": "Trishul",
						"material_request": "MR-1",
						"material_request_item": "MR-1-d1",
					}
				],
			]
			out = mop_lineage_audit.audit_employee_ir_diamond_lineage(
				manufacturing_operation="MOP-1",
			)

		self.assertEqual(out["counts"]["stock_entry_diamond_lines"], 1)
		self.assertEqual(out["counts"]["mop_log_diamond_or_gemstone"], 0)
		self.assertTrue(
			any("did not bridge" in d for d in out["diagnosis"]),
			out["diagnosis"],
		)
