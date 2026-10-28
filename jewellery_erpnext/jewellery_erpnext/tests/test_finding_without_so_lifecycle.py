# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Plan checks, cancel / submit guards and Receive-cancel batch restamp for Finding Manufacturing - Without SO."""

import json
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.without_so_repack import (
	create_without_so_finding_repack,
	restamp_without_so_batches_on_receive_cancel,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan import (
	FINDING_WITHOUT_SO,
	MANUFACTURE_ORDER_ROLE_FIELDS,
	ManufacturingPlan,
	get_allowed_manufacture_orders,
	get_manufacture_order_roles,
	validate_finding_without_so_context,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
	create_manufacturing_operation,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.finding_mwo import (
	create_finding_without_so_mwo,
	delete_without_so_drafts,
	validate_without_so_source_submitted,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	ParentManufacturingOrder,
	make_finding_without_so_order,
)

FINDING_MWO = "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.finding_mwo"
MP = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan"
MWO = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order"
PMO = "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order"
REPACK = "jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.without_so_repack"


def keyed(real, answers):
	"""side_effect answering the doctypes in ``answers`` and forwarding every other call to
	``real`` verbatim, so the stub never hijacks DocType meta loading or translations."""

	def side_effect(*args, **kwargs):
		if args and args[0] in answers:
			return answers[args[0]]
		return real(*args, **kwargs)

	return side_effect


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

	def test_missing_default_department_no_longer_blocks(self):
		"""The orders start in the plan's own department, not the Manufacturing Setting default."""
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


class TestPlanDepartment(IntegrationTestCase):
	"""Department is the Without-SO mode's only, filled once from the creator's Employee."""

	def _plan(self, mode, department=None):
		return frappe._dict(select_manufacture_order=mode, department=department)

	@patch(f"{MP}.get_user_department", return_value="Central - T")
	def test_without_so_fills_blank_department(self, _mock):
		plan = self._plan(FINDING_WITHOUT_SO)
		ManufacturingPlan.set_department(plan)
		self.assertEqual(plan.department, "Central - T")

	@patch(f"{MP}.get_user_department", return_value="Other - T")
	def test_existing_department_is_kept(self, mock_department):
		"""A later editor or submitter does not move the plan into their own department."""
		plan = self._plan(FINDING_WITHOUT_SO, "Central - T")
		ManufacturingPlan.set_department(plan)
		self.assertEqual(plan.department, "Central - T")
		mock_department.assert_not_called()

	@patch(f"{MP}.get_user_department", return_value=None)
	def test_user_without_employee_department_blocks(self, _mock):
		with self.assertRaises(frappe.ValidationError):
			ManufacturingPlan.set_department(self._plan(FINDING_WITHOUT_SO))

	@patch(f"{MP}.get_user_department")
	def test_other_modes_clear_department(self, mock_department):
		plan = self._plan("Manufacturing", "Central - T")
		ManufacturingPlan.set_department(plan)
		self.assertIsNone(plan.department)
		mock_department.assert_not_called()


class TestManufactureOrderRoles(IntegrationTestCase):
	"""Manufacturing Setting > Manufacture Order Roles decides which modes a user may pick."""

	ROLES = {FINDING_WITHOUT_SO: {"Finding Planner"}}

	def _plan(self, mode, changed=True):
		return frappe._dict(
			select_manufacture_order=mode, has_value_changed=lambda fieldname: changed
		)

	@patch(f"{MP}.frappe.get_roles", return_value=["Finding Planner"])
	@patch(f"{MP}.get_manufacture_order_roles", return_value=ROLES)
	def test_listed_role_may_pick_the_mode(self, _roles, _user_roles):
		ManufacturingPlan.validate_manufacture_order_permission(
			self._plan(FINDING_WITHOUT_SO)
		)

	@patch(f"{MP}.frappe.get_roles", return_value=["Manufacturing User"])
	@patch(f"{MP}.get_manufacture_order_roles", return_value=ROLES)
	def test_other_role_may_not_pick_the_mode(self, _roles, _user_roles):
		with self.assertRaises(frappe.PermissionError):
			ManufacturingPlan.validate_manufacture_order_permission(
				self._plan(FINDING_WITHOUT_SO)
			)

	@patch(f"{MP}.frappe.get_roles", return_value=["Manufacturing User"])
	@patch(f"{MP}.get_manufacture_order_roles", return_value=ROLES)
	def test_unlisted_mode_is_open_to_everyone(self, _roles, _user_roles):
		ManufacturingPlan.validate_manufacture_order_permission(
			self._plan("Manufacturing")
		)

	@patch(f"{MP}.get_manufacture_order_roles", return_value=ROLES)
	def test_unchanged_mode_is_not_rechecked(self, mock_roles):
		"""A draft someone else submits is not re-judged against the submitter's roles."""
		ManufacturingPlan.validate_manufacture_order_permission(
			self._plan(FINDING_WITHOUT_SO, changed=False)
		)
		mock_roles.assert_not_called()

	@patch(f"{MP}.frappe.get_meta")
	@patch(f"{MP}.frappe.get_roles", return_value=["Manufacturing User"])
	@patch(f"{MP}.get_manufacture_order_roles", return_value=ROLES)
	def test_form_offers_only_allowed_modes(self, _roles, _user_roles, mock_meta):
		mock_meta.return_value.get_options.return_value = (
			"\nManufacturing\nRepair\nFinding Manufacturing\n" + FINDING_WITHOUT_SO
		)
		self.assertEqual(
			get_allowed_manufacture_orders(),
			["Manufacturing", "Repair", "Finding Manufacturing"],
		)

	@patch(f"{MP}.frappe.defaults.get_user_default", return_value=None)
	def test_user_without_manufacturer_has_no_restrictions(self, _mock):
		self.assertEqual(get_manufacture_order_roles(), {})

	def test_roles_are_grouped_per_mode(self):
		"""Each mode's roles come from its own Table MultiSelect, told apart by parentfield."""
		rows = [
			frappe._dict(
				parentfield="finding_without_so_roles", role="Finding Planner"
			),
			frappe._dict(
				parentfield="finding_without_so_roles", role="Production Head"
			),
			frappe._dict(parentfield="repair_roles", role="Repair Desk"),
		]
		get_value = keyed(frappe.db.get_value, {"Manufacturing Setting": "MNF-1"})
		get_all = keyed(frappe.get_all, {"Manufacture Order Role": rows})
		with (
			patch(f"{MP}.frappe.defaults.get_user_default", return_value="MNF-1"),
			patch(f"{MP}.frappe.db.get_value", side_effect=get_value),
			patch(f"{MP}.frappe.get_all", side_effect=get_all),
		):
			self.assertEqual(
				get_manufacture_order_roles(),
				{
					FINDING_WITHOUT_SO: {"Finding Planner", "Production Head"},
					"Repair": {"Repair Desk"},
				},
			)

	def test_every_mode_has_a_role_field(self):
		"""A mode added to the plan without its own role field could never be restricted."""

		def fields(doctype):
			path = frappe.get_app_path(
				"jewellery_erpnext",
				"jewellery_erpnext",
				"doctype",
				frappe.scrub(doctype),
				f"{frappe.scrub(doctype)}.json",
			)
			with open(path) as f:
				return {df["fieldname"]: df for df in json.load(f)["fields"]}

		modes = fields("Manufacturing Plan")["select_manufacture_order"]["options"]
		self.assertEqual(
			set(MANUFACTURE_ORDER_ROLE_FIELDS), {m for m in modes.split("\n") if m}
		)
		setting = fields("Manufacturing Setting")
		for fieldname in MANUFACTURE_ORDER_ROLE_FIELDS.values():
			self.assertEqual(setting[fieldname]["fieldtype"], "Table MultiSelect")
			self.assertEqual(setting[fieldname]["options"], "Manufacture Order Role")


class TestWithoutSoOrdersFollowThePlan(IntegrationTestCase):
	"""The PMO, its Work Order and the first Manufacturing Operation start in the plan's
	department, and the PMO carries the plan's mode as its Type."""

	@patch(f"{PMO}.frappe.new_doc")
	def test_pmo_takes_plan_type_and_department(self, mock_new_doc):
		plan = frappe._dict(
			name="MP-1",
			company="Co",
			branch="Br",
			department="Central - T",
			select_manufacture_order=FINDING_WITHOUT_SO,
		)
		row = frappe._dict(name="ROW-1", item_code="F-ITEM")

		pmo = make_finding_without_so_order(
			plan,
			row,
			{"manufacturer": "MNF-1", "finding_default_department": "Default - T"},
		)

		self.assertEqual(pmo.type, FINDING_WITHOUT_SO)
		self.assertEqual(pmo.department, "Central - T")
		pmo.insert.assert_called_once_with()

	def test_mwo_takes_pmo_department(self):
		pmo = frappe._dict(
			name="PMO-T-F-0007",
			item_code="F-ITEM",
			manufacturer="MNF-1",
			department="Central - T",
		)
		get_value = keyed(frappe.db.get_value, {"Item": "BOM-F"})
		with (
			patch(f"{FINDING_MWO}.frappe.db.exists", return_value=False),
			patch(f"{FINDING_MWO}.frappe.db.get_value", side_effect=get_value),
			patch(f"{FINDING_MWO}.get_mapped_doc") as mock_map,
		):
			mwo = create_finding_without_so_mwo(pmo)

		self.assertIs(mwo, mock_map.return_value)
		self.assertEqual(mwo.department, "Central - T")
		mwo.save.assert_called_once_with()

	def _operation_for(self, mwo):
		settings = frappe._dict(
			default_operation="Op-Default", default_department="Default - T"
		)
		get_value = keyed(frappe.db.get_value, {"Manufacturing Setting": settings})
		with (
			patch(f"{MWO}.frappe.db.get_value", side_effect=get_value),
			patch(f"{MWO}.get_mapped_doc") as mock_map,
			patch(f"{MWO}.add_time_log") as mock_log,
		):
			create_manufacturing_operation(mwo)
		mop = mock_map.return_value
		mock_log.assert_called_once()
		self.assertEqual(mock_log.call_args.args[1]["operation"], mop.operation)
		return mop

	def _mwo(self, is_without_so, department):
		return frappe._dict(
			name="MWO-1",
			manufacturer="MNF-1",
			is_without_so=is_without_so,
			department=department,
			db_set=MagicMock(),
		)

	def test_without_so_operation_starts_in_plan_department(self):
		mop = self._operation_for(self._mwo(1, "Central - T"))
		self.assertEqual(mop.department, "Central - T")
		self.assertIsNone(mop.operation)

	def test_without_so_in_default_department_keeps_default_operation(self):
		mop = self._operation_for(self._mwo(1, "Default - T"))
		self.assertEqual(mop.department, "Default - T")
		self.assertEqual(mop.operation, "Op-Default")

	def test_sales_order_work_order_keeps_defaults(self):
		mop = self._operation_for(self._mwo(0, "Central - T"))
		self.assertEqual(mop.department, "Default - T")
		self.assertEqual(mop.operation, "Op-Default")
