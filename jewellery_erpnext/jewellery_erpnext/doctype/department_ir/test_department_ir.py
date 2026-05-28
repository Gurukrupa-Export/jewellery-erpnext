# Copyright (c) 2026, Nirali and Contributors
# See license.txt

from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.types.frappedict import _dict as FrappeDict

from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir import (
	DepartmentIR,
)
from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.tagging_transfer import (
	handle_tagging_issue,
	handle_tagging_receive,
	should_handle_tagging_issue,
	should_handle_tagging_receive,
)

_TT_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.tagging_transfer"


class FakeDepartmentIR(FrappeDict):
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		if "department_ir_operation" not in self:
			self.department_ir_operation = []

	def append(self, key, value):
		self[key].append(FrappeDict(value))

	def on_submit_issue_new(self, cancel=False):
		DepartmentIR.on_submit_issue_new(self, cancel)

	def on_submit_receive(self, cancel=False):
		DepartmentIR.on_submit_receive(self, cancel)

	def validate_receive_lineage(self):
		DepartmentIR.validate_receive_lineage(self)


class TestDepartmentIR(FrappeTestCase):
	def setUp(self):
		pass

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.get_datetime",
		return_value="2026-01-01 12:00:00",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.create_operation_for_next_dept"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.create_mop_log_for_department_ir"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.add_time_log"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.set_value"
	)
	def test_on_submit_issue_creates_mop_log_and_transitions(
		self,
		mock_set_val,
		mock_add_time,
		mock_create_mop,
		mock_create_op,
		mock_get_doc,
		mock_get_val,
		mock_datetime,
	):
		doc = FakeDepartmentIR(
			doctype="Department IR",
			name="DIR-ISS-001",
			type="Issue",
			current_department="Dept A",
			next_department="Dept B",
		)

		doc.append(
			"department_ir_operation",
			{
				"manufacturing_operation": "MOP-CURRENT",
				"manufacturing_work_order": "MWO-1",
			},
		)

		# Mocking warehouse fetch
		def get_val_side_effect(dt, filters=None, fieldname=None, as_dict=False):
			if fieldname == "default_in_transit_warehouse":
				return "Transit-WH"
			return "Dept-WH"

		mock_get_val.side_effect = get_val_side_effect

		# Mock new operation
		mock_create_op.return_value = FrappeDict(name="MOP-NEW")

		doc.on_submit_issue_new(cancel=False)

		# Verify MOP transition
		mock_set_val.assert_any_call(
			"Manufacturing Operation", "MOP-CURRENT", "status", "Finished"
		)
		mock_create_op.assert_called_once_with(
			"DIR-ISS-001", "MWO-1", "MOP-CURRENT", "Dept B"
		)

		# Verify MOP Log Generation
		self.assertTrue(mock_create_mop.called)
		args, kwargs = mock_create_mop.call_args
		self.assertEqual(args[0].name, "DIR-ISS-001")
		self.assertEqual(args[2], "Transit-WH")  # In transit is source for issue log
		self.assertEqual(args[4], "MOP-NEW")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.get_datetime",
		return_value="2026-01-01 12:00:00",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.get_value",
		return_value="Test WH",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.get_value",
		return_value="Test WH",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.create_mop_log_for_department_ir"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.set_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.add_time_log"
	)
	def test_on_submit_receive_marks_received_and_logs(
		self,
		mock_add_time,
		mock_set_val,
		mock_create_mop,
		mock_get_doc,
		mock_gv1,
		mock_gv2,
		mock_datetime,
	):
		doc = FakeDepartmentIR(
			doctype="Department IR",
			name="DIR-REC-001",
			type="Receive",
			current_department="Dept B",
			receive_against="DIR-ISS-001",
		)

		doc.append(
			"department_ir_operation",
			{
				"manufacturing_operation": "MOP-NEW",
				"manufacturing_work_order": "MWO-1",
			},
		)

		doc.on_submit_receive(cancel=False)

		# Verify status updates
		args, kwargs = mock_set_val.call_args_list[0]
		self.assertEqual(args[0], "Manufacturing Operation")
		self.assertEqual(args[1], "MOP-NEW")
		self.assertEqual(args[2]["department_receive_id"], "DIR-REC-001")
		self.assertEqual(args[2]["department_ir_status"], "Received")

		# Verify MOP Log Generation
		mock_create_mop.assert_called_once()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.get_value"
	)
	def test_validate_receive_lineage_blocks_invalid_parents(self, mock_get_value):
		doc = FakeDepartmentIR(
			doctype="Department IR",
			name="DIR-REC-001",
			type="Receive",
			receive_against="DIR-ISS-BAD",
		)

		doc.append("department_ir_operation", {"manufacturing_operation": "MOP-ORPHAN"})

		# Simulate a receive_against document that is in Draft (0) not Submitted (1)
		mock_get_value.return_value = FrappeDict(docstatus=0, type="Issue")

		with self.assertRaises(frappe.ValidationError) as context:
			doc.validate_receive_lineage()

		self.assertIn("must be a submitted Department IR", str(context.exception))


# ── Tagging Transfer tests ────────────────────────────────────────────────────


def _make_dir(name="DIR-001", **extra):
	d = FrappeDict(
		doctype="Department IR",
		name=name,
		company="GEPL",
		type="Issue",
		current_department="Product Certification - GEPL",
		next_department="Tagging - GEPL",
		previous_department=None,
		**extra,
	)
	return d


def _make_row(mop="MOP-1", mwo="MWO-1"):
	return FrappeDict(
		manufacturing_operation=mop, manufacturing_work_order=mwo, name="row-1", idx=1
	)


def _fake_sre(item="M-GOLD", wh="WH-PC", batch="B1"):
	return FrappeDict(
		name="SRE-1",
		item_code=item,
		warehouse=wh,
		reserved_qty=10.0,
		delivered_qty=0.0,
		reservation_based_on="Qty",
		manufacturing_work_order="MWO-1",
		manufacturing_operation="MOP-1",
		voucher_type="Sales Order",
		voucher_no="SO-1",
		voucher_detail_no="SO-1-r1",
		voucher_qty=10.0,
		company="GEPL",
		stock_uom="gram",
	)


def _fake_balance_row(item="M-GOLD", qty=10.0, batch="B1"):
	return FrappeDict(
		item_code=item,
		qty=qty,
		qty_after_transaction=qty,
		qty_after_transaction_batch_based=qty,
		batch_no=batch,
	)


class TestTaggingTransferRouting(FrappeTestCase):
	"""should_handle_tagging_issue / should_handle_tagging_receive gate checks."""

	def test_issue_passes_for_pc_to_tagging(self):
		doc = _make_dir(
			type="Issue",
			current_department="Product Certification - GEPL",
			next_department="Tagging - GEPL",
		)
		self.assertTrue(should_handle_tagging_issue(doc))

	def test_issue_fails_for_wrong_departments(self):
		doc = _make_dir(
			type="Issue",
			current_department="Casting - GEPL",
			next_department="Tagging - GEPL",
		)
		self.assertFalse(should_handle_tagging_issue(doc))

	def test_issue_fails_for_receive_type(self):
		doc = _make_dir(
			type="Receive",
			current_department="Product Certification - GEPL",
			next_department="Tagging - GEPL",
		)
		self.assertFalse(should_handle_tagging_issue(doc))

	def test_receive_passes_for_pc_to_tagging(self):
		doc = _make_dir(
			type="Receive",
			previous_department="Product Certification - GEPL",
			current_department="Tagging - GEPL",
		)
		self.assertTrue(should_handle_tagging_receive(doc))

	def test_receive_fails_for_wrong_departments(self):
		doc = _make_dir(
			type="Receive",
			previous_department="Casting - GEPL",
			current_department="Tagging - GEPL",
		)
		self.assertFalse(should_handle_tagging_receive(doc))


class TestTaggingTransferIssue(FrappeTestCase):
	"""handle_tagging_issue happy path and error paths."""

	def test_throws_when_t_warehouse_missing(self):
		doc = _make_dir()
		row = _make_row()
		with self.assertRaises(frappe.ValidationError):
			handle_tagging_issue(
				doc, row, new_operation_name="MOP-NEW", t_warehouse=None
			)

	@patch(f"{_TT_MODULE}.frappe.db.get_value", return_value="SE-EXISTING")
	def test_idempotent_when_se_already_exists(self, _gv):
		doc = _make_dir()
		row = _make_row()
		with patch(f"{_TT_MODULE}.frappe.msgprint") as mock_msg:
			handle_tagging_issue(
				doc, row, new_operation_name="MOP-NEW", t_warehouse="WH-TRANSIT"
			)
		mock_msg.assert_called_once()
		# No SE created since existing found
		self.assertIn("SE-EXISTING", mock_msg.call_args[0][0])

	@patch(f"{_TT_MODULE}.frappe.db.get_value", return_value=None)
	@patch(f"{_TT_MODULE}.get_current_mop_balance_rows", return_value=[])
	def test_throws_when_no_mop_balance(self, _balance, _gv):
		doc = _make_dir()
		row = _make_row()
		with self.assertRaises(frappe.ValidationError):
			handle_tagging_issue(
				doc, row, new_operation_name="MOP-NEW", t_warehouse="WH-TRANSIT"
			)

	@patch(f"{_TT_MODULE}.frappe.db.get_value", return_value=None)
	@patch(
		f"{_TT_MODULE}.get_current_mop_balance_rows", return_value=[_fake_balance_row()]
	)
	@patch(f"{_TT_MODULE}._get_active_sres", return_value=[])
	def test_throws_when_no_active_sres(self, _sres, _balance, _gv):
		doc = _make_dir()
		row = _make_row()
		with self.assertRaises(frappe.ValidationError):
			handle_tagging_issue(
				doc, row, new_operation_name="MOP-NEW", t_warehouse="WH-TRANSIT"
			)

	@patch(f"{_TT_MODULE}.frappe.db.get_value", return_value=None)
	@patch(
		f"{_TT_MODULE}.get_current_mop_balance_rows", return_value=[_fake_balance_row()]
	)
	@patch(f"{_TT_MODULE}._get_active_sres", return_value=[_fake_sre()])
	@patch(f"{_TT_MODULE}._build_se_items_from_mop_balance", return_value=[])
	def test_throws_when_no_valid_se_items(self, _items, _sres, _balance, _gv):
		doc = _make_dir()
		row = _make_row()
		with self.assertRaises(frappe.ValidationError):
			handle_tagging_issue(
				doc, row, new_operation_name="MOP-NEW", t_warehouse="WH-TRANSIT"
			)

	@patch(f"{_TT_MODULE}.frappe.db.get_value", return_value=None)
	@patch(
		f"{_TT_MODULE}.get_current_mop_balance_rows", return_value=[_fake_balance_row()]
	)
	@patch(f"{_TT_MODULE}._get_active_sres", return_value=[_fake_sre()])
	@patch(
		f"{_TT_MODULE}._build_se_items_from_mop_balance",
		return_value=[{"item_code": "M-GOLD", "qty": 10.0}],
	)
	@patch(f"{_TT_MODULE}._cancel_sres", return_value=[MagicMock()])
	@patch(f"{_TT_MODULE}._create_and_submit_se")
	@patch(f"{_TT_MODULE}._recreate_sres")
	def test_happy_path_creates_se_and_recreates_sres(
		self, mock_recreate, mock_create_se, mock_cancel, _items, _sres, _balance, _gv
	):
		doc = _make_dir()
		row = _make_row()
		handle_tagging_issue(
			doc, row, new_operation_name="MOP-NEW", t_warehouse="WH-TRANSIT"
		)
		mock_cancel.assert_called_once()
		mock_create_se.assert_called_once()
		mock_recreate.assert_called_once()
		# SREs recreated pointing to transit warehouse under new MOP
		recreate_kwargs = mock_recreate.call_args
		self.assertEqual(recreate_kwargs[1]["new_warehouse"], "WH-TRANSIT")
		self.assertEqual(recreate_kwargs[1]["new_mop_name"], "MOP-NEW")


class TestTaggingTransferReceive(FrappeTestCase):
	"""handle_tagging_receive happy path and error paths."""

	def test_throws_when_t_warehouse_missing(self):
		doc = _make_dir(type="Receive")
		row = _make_row()
		with self.assertRaises(frappe.ValidationError):
			handle_tagging_receive(doc, row, t_warehouse=None)

	@patch(f"{_TT_MODULE}.frappe.db.get_value", return_value=None)
	@patch(
		f"{_TT_MODULE}.get_current_mop_balance_rows", return_value=[_fake_balance_row()]
	)
	@patch(f"{_TT_MODULE}._get_active_sres", return_value=[_fake_sre()])
	@patch(
		f"{_TT_MODULE}._build_se_items_from_mop_balance",
		return_value=[{"item_code": "M-GOLD", "qty": 10.0}],
	)
	@patch(f"{_TT_MODULE}._cancel_sres", return_value=[MagicMock()])
	@patch(f"{_TT_MODULE}._create_and_submit_se")
	@patch(f"{_TT_MODULE}._recreate_sres")
	def test_receive_happy_path_creates_se_keeps_same_mop(
		self, mock_recreate, mock_create_se, mock_cancel, _items, _sres, _balance, _gv
	):
		doc = _make_dir(type="Receive")
		row = _make_row(mop="MOP-TAGGING")
		handle_tagging_receive(doc, row, t_warehouse="WH-TAGGING")
		mock_create_se.assert_called_once()
		# Receive: SREs stay under same MOP name
		recreate_kwargs = mock_recreate.call_args
		self.assertEqual(recreate_kwargs[1]["new_warehouse"], "WH-TAGGING")
		self.assertEqual(recreate_kwargs[1]["new_mop_name"], "MOP-TAGGING")
