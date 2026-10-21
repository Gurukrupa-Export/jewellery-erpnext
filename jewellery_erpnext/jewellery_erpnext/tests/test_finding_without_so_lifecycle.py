# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Plan checks, cancel / submit guards and Receive-cancel batch restamp for Finding Manufacturing - Without SO."""

from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.without_so_repack import (
	create_without_so_finding_repack,
	restamp_without_so_batches_on_receive_cancel,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan import (
	FINDING_WITHOUT_SO,
	ManufacturingPlan,
	validate_finding_without_so_context,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.finding_mwo import (
	delete_without_so_drafts,
	validate_without_so_source_submitted,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	ParentManufacturingOrder,
)

FINDING_MWO = "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.finding_mwo"
MP = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan"
PMO = "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order"
REPACK = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.without_so_repack"


class TestDeleteWithoutSoDrafts(IntegrationTestCase):
	@patch(f"{FINDING_MWO}.frappe.delete_doc")
	@patch(f"{FINDING_MWO}.frappe.get_all", return_value=["PMO-1", "PMO-2"])
	def test_deletes_only_drafts(self, mock_get_all, mock_delete):
		delete_without_so_drafts(
			"Parent Manufacturing Order", {"manufacturing_plan": "MP-1"}
		)

		mock_get_all.assert_called_once_with(
			"Parent Manufacturing Order",
			filters={"manufacturing_plan": "MP-1", "docstatus": 0},
			pluck="name",
		)
		mock_delete.assert_has_calls(
			[
				call("Parent Manufacturing Order", "PMO-1", ignore_permissions=True),
				call("Parent Manufacturing Order", "PMO-2", ignore_permissions=True),
			]
		)


class TestSourceSubmittedGuard(IntegrationTestCase):
	@patch(f"{FINDING_MWO}.frappe.db.get_value", return_value=1)
	def test_submitted_source_passes(self, _mock):
		validate_without_so_source_submitted(
			"Manufacturing Plan", "MP-1", "Manufacturing Plan"
		)

	@patch(f"{FINDING_MWO}.frappe.db.get_value", return_value=2)
	def test_cancelled_source_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			validate_without_so_source_submitted(
				"Manufacturing Plan", "MP-1", "Manufacturing Plan"
			)

	def test_missing_source_blocks(self):
		with self.assertRaises(frappe.ValidationError):
			validate_without_so_source_submitted(
				"Parent Manufacturing Order", None, "Parent Manufacturing Order"
			)


class TestCancelRemovesDrafts(IntegrationTestCase):
	@patch(f"{MP}.frappe.db.sql")
	@patch(f"{MP}.delete_without_so_drafts")
	def test_without_so_plan_cancel_deletes_draft_pmos(self, mock_delete, mock_sql):
		plan = MagicMock(select_manufacture_order=FINDING_WITHOUT_SO)
		plan.name = "MP-1"

		ManufacturingPlan.on_cancel(plan)

		mock_delete.assert_called_once_with(
			"Parent Manufacturing Order", {"manufacturing_plan": "MP-1"}
		)
		mock_sql.assert_not_called()

	@patch(f"{PMO}.update_existing")
	@patch(f"{PMO}.delete_without_so_drafts")
	def test_without_so_pmo_cancel_deletes_draft_mwo(self, mock_delete, mock_update):
		pmo = MagicMock(is_without_so=1)
		pmo.name = "PMO-1"

		ParentManufacturingOrder.on_cancel(pmo)

		mock_delete.assert_called_once_with(
			"Manufacturing Work Order", {"manufacturing_order": "PMO-1"}
		)
		mock_update.assert_not_called()

	@patch(f"{PMO}.update_existing")
	@patch(f"{PMO}.delete_without_so_drafts")
	def test_so_pmo_cancel_keeps_old_path(self, mock_delete, mock_update):
		pmo = MagicMock(is_without_so=0, qty=1)

		ParentManufacturingOrder.on_cancel(pmo)

		mock_delete.assert_not_called()
		self.assertEqual(mock_update.call_count, 2)


class TestPlanSubmitContext(IntegrationTestCase):
	def test_missing_manufacturer_blocks(self):
		with self.assertRaises(frappe.ValidationError):
			validate_finding_without_so_context(
				{"manufacturer": None, "finding_default_department": None}
			)

	def test_missing_department_blocks(self):
		with self.assertRaises(frappe.ValidationError):
			validate_finding_without_so_context(
				{"manufacturer": "MNF-1", "finding_default_department": None}
			)

	def test_complete_context_passes(self):
		validate_finding_without_so_context(
			{"manufacturer": "MNF-1", "finding_default_department": "Dept-1"}
		)


class TestRestampOnReceiveCancel(IntegrationTestCase):
	@patch(f"{REPACK}.apply_worker_cost_to_batch")
	@patch(f"{REPACK}._repack_finding_batches", return_value=["B-A"])
	@patch(f"{REPACK}.get_mwo_worker_cost", return_value=100.0)
	@patch(f"{REPACK}.is_without_so_mwo", side_effect=lambda mwo: mwo == "MWO-NOSO")
	def test_surviving_batch_gets_surviving_cost(
		self, _mock_flag, mock_cost, mock_batches, mock_apply
	):
		"""Receive 2 is cancelled: Batch A (from Receive 1) goes back to the cost that remains."""
		eir = frappe._dict(
			employee_ir_operations=[
				frappe._dict(manufacturing_work_order="MWO-NOSO"),
				frappe._dict(manufacturing_work_order="MWO-NOSO"),
				frappe._dict(manufacturing_work_order="MWO-SO"),
			]
		)

		restamp_without_so_batches_on_receive_cancel(eir)

		mock_cost.assert_called_once_with("MWO-NOSO")
		mock_batches.assert_called_once_with("MWO-NOSO")
		mock_apply.assert_called_once_with("B-A", 100.0)


class TestPlanItemChecks(IntegrationTestCase):
	"""The empty-table check moved from the Server Script into before_insert, and the
	Without-SO mode needs at least one Finding Details row on the server too."""

	def _plan(self, mode, table=None, findings=None):
		return frappe._dict(
			select_manufacture_order=mode,
			manufacturing_plan_table=table or [],
			finding_details=findings or [],
		)

	def test_without_so_insert_skips_item_table_check(self):
		ManufacturingPlan.before_insert(self._plan(FINDING_WITHOUT_SO))

	def test_other_mode_empty_table_blocks_insert(self):
		with self.assertRaises(frappe.ValidationError):
			ManufacturingPlan.before_insert(self._plan("Manufacturing"))

	def test_other_mode_with_rows_passes_insert(self):
		ManufacturingPlan.before_insert(self._plan("Manufacturing", table=[{"idx": 1}]))

	def test_without_so_with_no_finding_rows_blocks(self):
		with self.assertRaises(frappe.ValidationError):
			ManufacturingPlan.validate_finding_details(self._plan(FINDING_WITHOUT_SO))

	def test_other_mode_ignores_empty_finding_rows(self):
		ManufacturingPlan.validate_finding_details(self._plan("Manufacturing"))


class TestDisableGetItemTableScript(IntegrationTestCase):
	PATCH = "jewellery_erpnext.patches.disable_mp_get_item_table_server_script"

	def _run(self, exists, disabled):
		from jewellery_erpnext.patches.disable_mp_get_item_table_server_script import (
			execute,
		)

		script = MagicMock(disabled=disabled)
		with (
			patch(f"{self.PATCH}.frappe.db.exists", return_value=exists),
			patch(f"{self.PATCH}.frappe.get_doc", return_value=script),
		):
			execute()
		return script

	def test_enabled_script_is_disabled(self):
		script = self._run(exists=True, disabled=0)
		self.assertEqual(script.disabled, 1)
		script.save.assert_called_once()

	def test_already_disabled_is_left_alone(self):
		self._run(exists=True, disabled=1).save.assert_not_called()

	def test_missing_script_is_skipped(self):
		self._run(exists=False, disabled=0).save.assert_not_called()
class TestWithoutSoRepackNeedsFindingDetails(IntegrationTestCase):
	"""A Receive row is repacked into the finding only when its finding details are filled."""

	def _row(self, **findings):
		return frappe._dict(
			idx=1,
			manufacturing_work_order="MWO-NOSO",
			manufacturing_operation="MOP-1",
			**findings,
		)

	@patch(f"{REPACK}._received_metal_rows")
	@patch(f"{REPACK}._existing_repack")
	@patch(f"{REPACK}.is_without_so_mwo", return_value=True)
	def test_no_finding_details_makes_no_repack(
		self, _without_so, existing, metal_rows
	):
		row = self._row(
			finding_item1=None, finding_wt1=0, finding_item2=None, finding_wt2=0
		)

		self.assertIsNone(
			create_without_so_finding_repack(frappe._dict(name="EIR-1"), row, ["STE-1"])
		)
		existing.assert_not_called()
		metal_rows.assert_not_called()

	@patch(f"{REPACK}._received_metal_rows")
	@patch(f"{REPACK}._existing_repack")
	@patch(f"{REPACK}.is_without_so_mwo", return_value=True)
	def test_half_filled_slot_is_not_finding_details(
		self, _without_so, existing, metal_rows
	):
		"""An item without its weight is not a finding (validate_finding_repack rejects it)."""
		row = self._row(finding_item1="F-ITEM", finding_wt1=0)

		self.assertIsNone(
			create_without_so_finding_repack(frappe._dict(name="EIR-1"), row, ["STE-1"])
		)
		metal_rows.assert_not_called()

	@patch(f"{REPACK}._existing_repack", return_value="STE-REPACK")
	@patch(f"{REPACK}.is_without_so_mwo", return_value=True)
	def test_finding_details_go_on_to_the_repack(self, _without_so, existing):
		row = self._row(finding_item2="F-ITEM", finding_wt2=1.25)

		self.assertEqual(
			create_without_so_finding_repack(
				frappe._dict(name="EIR-1"), row, ["STE-1"]
			),
			"STE-REPACK",
		)
		existing.assert_called_once_with("EIR-1", "MOP-1")
