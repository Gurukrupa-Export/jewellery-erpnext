# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Day 3 of process control: Employee IR, Department IR, Stock Entry and Material Request respect
the job's process (``doc_events/process_enforcement.py``; GK-PROC-0011..0015).

Every test runs with the pilot switch ON (MOP Settings › Enable Process Control) in a department
with process control on, unless it is proving the opposite. A check tested with enforcement off
would prove nothing.

* ``TestEmployeeIR`` / ``TestDepartmentIR``: real documents through the real controllers.
* ``TestStockEntryRule``: the Stock Entry hook on in-memory entries over the real cell world, so it
  is judged against real operation and work-order rows, including the generated-entry and EOD
  classifications.
* ``TestStockEntrySubmission``: real ``insert`` / ``submit`` / ``cancel`` through the whole hook
  chain and the Stock Ledger (needs a company with stock accounts; skips elsewhere and says why).
* ``TestMaterialRequestTransfer``: the Transfer to MOP check on real rows, and its place in the
  dispatcher before either Stock Entry builder.
* ``TestScopeAndQueries``: unconfigured departments and the pilot switch; query counts that do not
  grow with the number of rows.
* ``TestStockEntryRace``: a genuine second connection holding the work order (gated like the
  guard's race suite).

Every class except the race one rolls back after each test.
"""

import time
from contextlib import contextmanager
from unittest import skipUnless
from unittest.mock import patch

import frappe
from frappe.permissions import add_permission, update_permission_property
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate
from frappe.utils import strip_html

from jewellery_erpnext.jewellery_erpnext.doc_events import material_request as mr_mod
from jewellery_erpnext.jewellery_erpnext.doc_events import process_control as pc
from jewellery_erpnext.jewellery_erpnext.doc_events import process_enforcement as pe
from jewellery_erpnext.jewellery_erpnext.doc_events.process_locks import (
	WorkOrderBusyError,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.process_enforcement import (
	ProcessEnforcementError,
)
from jewellery_erpnext.jewellery_erpnext.tests import _current_operation_fixtures as fx
from jewellery_erpnext.jewellery_erpnext.tests.test_material_request_mop_department import (
	_get_value_stub,
	_mop_row,
	_mr,
)
from jewellery_erpnext.jewellery_erpnext.tests.test_process_transfer_workflow import (
	_WorkflowHarness,
)

WORK_ORDER_TRANSFER = "Material Transfer (WORK ORDER)"
_MR = "jewellery_erpnext.jewellery_erpnext.doc_events.material_request"


def _text(exc) -> str:
	return strip_html(str(exc))


@contextmanager
def refused(case, exc_type, needle=None):
	"""``assertRaises`` that also requires the literal ``needle`` in the message's plain text
	(messages carry ``<strong>`` markup, and names carry regex metacharacters). Authority tests
	name the process-access wording, so a refusal by Frappe's own role permissions can never pass
	for one by process control."""
	with case.assertRaises(exc_type) as ctx:
		yield ctx
	if needle:
		case.assertIn(needle, _text(ctx.exception))


class _LiteralRefusals:
	"""In this module ``assertRaisesRegex(exc, text)`` means :func:`refused`: literal text."""

	def assertRaisesRegex(self, exc_type, needle, *args, **kwargs):  # noqa: N802
		return refused(self, exc_type, needle)


def _entry(world, mwo, mop, *, process=None, **fields):
	"""An unsaved WORK ORDER transfer of ``mop`` the way the form builds it (header and row)."""
	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": WORK_ORDER_TRANSFER,
			"purpose": "Material Transfer",
			"company": world.company,
			"manufacturing_work_order": mwo,
			"manufacturing_operation": mop,
			"items": [
				{
					"item_code": world.item.metal,
					"qty": 0.2,
					"manufacturing_operation": mop,
					"custom_manufacturing_work_order": mwo,
				}
			],
			**fields,
		}
	)
	if process:
		se.set(pe.STOCK_ENTRY_PROCESS_FIELD, process)
	return se


class _EnforcementHarness(_LiteralRefusals, _WorkflowHarness):
	"""Day 2's cell world (MM with processes A, B, C) plus a few Day 3 helpers."""

	def has_worker_operation(self):
		return frappe.get_meta("Employee").has_field("custom_operation")

	def clerk_role(self):
		"""A role with read / write / create / submit / cancel on the four transaction DocTypes,
		made inside this test's transaction (GK sites grant these per department role)."""
		role = f"{self.w.prefix} Clerk"
		if not frappe.db.exists("Role", role):
			frappe.get_doc(
				{"doctype": "Role", "role_name": role, "desk_access": 1}
			).insert(ignore_permissions=True)
			for doctype in (
				"Employee IR",
				"Department IR",
				"Stock Entry",
				"Material Request",
			):
				add_permission(doctype, role)
				for ptype in ("write", "create", "submit", "cancel"):
					update_permission_property(doctype, role, 0, ptype, 1)
				self.addCleanup(frappe.clear_cache, doctype=doctype)
		return role

	def clerk(self, key, *processes):
		"""A System User who may transact (System Manager to read the manufacturing DocTypes, plus
		:meth:`clerk_role`) and holds User Process Access for ``processes`` only."""
		user = self.user(key, roles=("System Manager", self.clerk_role()))
		for process in processes:
			self.grant(user, process)
		return user

	def employee(self, key, department_key="MM", operation=None):
		name = fx._insert(
			self.w,
			"Employee",
			{
				"name": f"{self.w.prefix}-EMP-{key}",
				"first_name": f"{self.w.prefix} {key}",
				"employee_name": f"{self.w.prefix} {key}",
				"company": self.w.company,
				"department": self.w.departments[department_key],
				"status": "Active",
				"gender": "Male",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
			},
		)
		self.w.employees[key] = name
		fx._insert(
			self.w,
			"Warehouse",
			{
				"name": f"{self.w.prefix} {key} Mfg - {self.w.abbr}",
				"warehouse_name": f"{self.w.prefix} {key} Mfg",
				"company": self.w.company,
				"employee": name,
				"warehouse_type": "Manufacturing",
				"is_group": 0,
				"disabled": 0,
			},
		)
		if operation:
			self.qualify(key, operation)
		return name

	def issue_doc(self, mwos, operation=None, employee="MM1"):
		doc = fx.employee_ir(
			self.w,
			"Issue",
			mwos if isinstance(mwos, list) else [mwos],
			employee=employee,
		)
		if operation:
			doc.operation = operation
		return doc

	def mop_state(self, mop):
		return frappe.db.get_value(
			"Manufacturing Operation",
			mop,
			["status", "employee", "operation", "department_process"],
			as_dict=True,
		)


# ------------------------------------------------------------------------------------------------
# Employee IR
# ------------------------------------------------------------------------------------------------


class TestEmployeeIR(_EnforcementHarness):
	def test_issue_in_the_jobs_process_goes_through(self):
		a, _b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		fx.issue(self.w, mwo)
		state = self.mop_state(mop)
		self.assertEqual(
			(state.status, state.employee, state.operation),
			("WIP", self.w.employees["MM1"], self.w.operations["MM"]),
		)

	def test_an_operation_of_another_process_cannot_be_issued(self):
		# The requirement's own example: the job is in one process, the operation belongs to
		# another process of the same department.
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(b)
		with self.assertRaises(ProcessEnforcementError) as ctx:
			fx.insert_ir(self.issue_doc(mwo))  # MM's operation is in A
		self.assertIn(f"is in process {b}", _text(ctx.exception))
		self.assertEqual(self.mop_state(mop).status, "Not Started")

	def test_a_worker_of_another_operation_in_the_same_process_is_refused(self):
		if not self.has_worker_operation():
			self.skipTest("Employee.custom_operation is not installed on this site")
		a, _b, _c = self.cell3()
		filing = self.operation("Op A Filing")
		self.map_operation(filing, a)  # also process A
		mwo, _mop = self.job_in(a)
		# MM1 holds MM's operation; the Issue asks for the other operation of the same process.
		with self.assertRaises(ProcessEnforcementError) as ctx:
			fx.insert_ir(self.issue_doc(mwo, operation=filing))
		self.assertIn("Being in the same process does not", _text(ctx.exception))

	def test_a_job_without_a_process_needs_its_first_one(self):
		self.cell3()
		mwo, _ = self.job_in(None)
		with self.assertRaisesRegex(
			ProcessEnforcementError, "Initial Process Assignment"
		):
			fx.insert_ir(self.issue_doc(mwo))

	def test_mixed_rows_are_refused_naming_the_bad_row_and_nothing_changes(self):
		a, b, _c = self.cell3()
		good, good_mop = self.job_in(a)
		bad, _bad_mop = self.job_in(b)
		with self.assertRaises(ProcessEnforcementError) as ctx:
			fx.insert_ir(self.issue_doc([good, bad]))
		message = _text(ctx.exception)
		self.assertIn("Row 2", message)
		self.assertNotIn("Row 1", message)
		self.assertEqual(self.mop_state(good_mop).status, "Not Started")

	def test_a_user_without_access_to_the_process_is_refused(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		frappe.set_user(self.clerk("clerk-b", b))
		with self.assertRaisesRegex(
			frappe.PermissionError, f"no access to process {a}"
		):
			fx.insert_ir(self.issue_doc(mwo))
		frappe.set_user("Administrator")
		self.assertEqual(self.mop_state(mop).status, "Not Started")

	def test_a_relief_user_with_a_grant_for_the_process_may_issue(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		# B is the clerk's own process (default); A is relief cover, an ordinary extra row.
		user = self.clerk("relief", a, b)
		frappe.db.set_value(
			"User Process Access",
			{"user": user, "department_process": b},
			"is_default",
			1,
		)
		frappe.set_user(user)
		fx.issue(self.w, mwo)
		frappe.set_user("Administrator")
		self.assertEqual(self.mop_state(mop).status, "WIP")

	def test_a_default_alone_never_grants(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(a)
		user = self.clerk("defaulted", b)
		frappe.db.set_value(
			"User Process Access",
			{"user": user, "department_process": b},
			"is_default",
			1,
		)
		frappe.set_user(user)
		with refused(self, frappe.PermissionError, f"no access to process {a}"):
			fx.insert_ir(self.issue_doc(mwo))

	def test_api_insert_with_a_tampered_pair_is_refused_and_changes_nothing(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(a)
		other, other_mop = self.job_in(b)
		doc = self.issue_doc(mwo)
		row = doc.employee_ir_operations[0]
		row.manufacturing_operation = (
			other_mop  # tampered pair: B's operation, A's work order
		)
		row.manufacturing_work_order = mwo
		# Either the row's work order is re-derived from the operation (kggk_uat: read-only fetch),
		# whose job is in B, or the mismatched pair itself is refused (fetch_if_empty). Both name
		# the operation's real job; the client's work order decides nothing.
		with self.assertRaises(ProcessEnforcementError) as ctx:
			frappe.client.insert(doc.as_dict())
		self.assertIn(other, _text(ctx.exception))
		self.assertEqual(self.mop_state(other_mop).status, "Not Started")

	def test_a_draft_issued_before_the_job_changed_process_cannot_submit(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		draft = fx.issue(self.w, mwo, submit=False)
		# The job moves to B behind the draft's back (the transition service writes these two).
		self.assign(mwo, mop, b)
		with self.assertRaisesRegex(ProcessEnforcementError, f"is in process {b}"):
			fx.submit_ir(draft)
		self.assertEqual(self.mop_state(mop).status, "Not Started")

	def test_receive_against_its_issue_survives_a_change_of_the_worker_master(self):
		a, _b, _c = self.cell3()
		filing = self.operation("Op A Filing")
		self.map_operation(filing, a)
		mwo, mop = self.job_in(a)
		fx.issue(self.w, mwo)
		# HR moves the karigar to another operation after the Issue: the Receive is still the
		# Issue's pair and goes through.
		self.qualify("MM1", filing)
		fx.receive(self.w, mwo)
		self.assertEqual(self.mop_state(mop).status, "Finished")

	def test_receive_by_another_employee_is_refused(self):
		a, _b, _c = self.cell3()
		self.employee("MM2", operation=self.w.operations["MM"])
		mwo, mop = self.job_in(a)
		fx.issue(self.w, mwo)
		with self.assertRaises(frappe.ValidationError):
			fx.receive(self.w, mwo, employee="MM2")
		self.assertEqual(self.mop_state(mop).employee, self.w.employees["MM1"])

	def test_receive_needs_access_to_the_operations_process(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		fx.issue(self.w, mwo)
		frappe.set_user(self.clerk("receiver-b", b))
		with refused(self, frappe.PermissionError, f"no access to process {a}"):
			fx.receive(self.w, mwo)
		frappe.set_user("Administrator")
		self.assertEqual(self.mop_state(mop).status, "WIP")

	def test_cancel_needs_access_to_the_operations_process(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		issue = fx.issue(self.w, mwo)
		frappe.set_user(self.clerk("canceller-b", b))
		with refused(self, frappe.PermissionError, f"no access to process {a}"):
			fx.reload(issue).cancel()
		frappe.set_user("Administrator")
		self.assertEqual(self.mop_state(mop).status, "WIP")


# ------------------------------------------------------------------------------------------------
# Department IR
# ------------------------------------------------------------------------------------------------


class TestDepartmentIR(_EnforcementHarness):
	def ds_cell(self):
		"""DS as a second cell with one process D (the job's destination)."""
		d = self.process("Process D", key="DS")
		self.map_operation(self.w.operations["DS"], d)
		self.enable("DS")
		return d

	def out_of_mm(self, mwo, destination=None):
		return fx.insert_ir(
			fx.department_ir(
				self.w,
				"Issue",
				[mwo],
				current="MM",
				to="DS",
				destination_process=destination,
			)
		)

	def test_a_cell_hands_a_job_to_another_cells_different_process(self):
		a, _b, _c = self.cell3()
		d = self.ds_cell()
		mwo, mop = self.job_in(a)
		issue = fx.submit_ir(self.out_of_mm(mwo, d))
		minted = frappe.db.get_value(
			"Manufacturing Operation", {"department_issue_id": issue.name}, "name"
		)
		self.assertEqual(self.mop_process(mop), a)  # the source keeps its history
		self.assertEqual(self.mop_process(minted), d)
		self.assertEqual(self.state(mwo).current_process, d)
		# Exactly one operation minted, however often validate ran.
		self.assertEqual(
			frappe.db.count(
				"Manufacturing Operation", {"department_issue_id": issue.name}
			),
			1,
		)

	def test_sending_out_needs_access_to_the_source_process_not_the_destination(self):
		a, b, _c = self.cell3()
		d = self.ds_cell()
		mwo, _ = self.job_in(a)
		frappe.set_user(self.clerk("sender-b", b, d))
		with self.assertRaisesRegex(
			frappe.PermissionError, f"no access to process {a}"
		):
			self.out_of_mm(mwo, d)
		frappe.set_user("Administrator")
		# With A (the source) and no access to D at all, the hand-off goes through.
		frappe.set_user(self.clerk("sender-a", a))
		fx.submit_ir(self.out_of_mm(mwo, d))

	def test_receiving_into_a_cell_needs_access_to_one_of_its_processes(self):
		a, _b, _c = self.cell3()
		mwo, _ = self.job_in(None, key="DS")
		issue = fx.submit_ir(
			fx.insert_ir(
				fx.department_ir(
					self.w, "Issue", [mwo], current="DS", to="MM", destination_process=a
				)
			)
		)

		def receive():
			return fx.insert_ir(
				fx.department_ir(
					self.w,
					"Receive",
					[mwo],
					current="MM",
					previous="DS",
					receive_against=issue.name,
				)
			)

		frappe.set_user(self.clerk("outsider"))
		with self.assertRaisesRegex(frappe.PermissionError, "cannot receive"):
			receive()
		frappe.set_user("Administrator")
		b = next(p for p in self.operation_of if p != a)
		frappe.set_user(
			self.clerk("cell-member", b)
		)  # some process of MM, not the job's
		fx.submit_ir(receive())

	def test_an_invalid_destination_is_still_refused(self):
		a, _b, _c = self.cell3()
		self.ds_cell()
		mwo, _ = self.job_in(a)
		with self.assertRaises(pc.ProcessControlError):
			self.out_of_mm(mwo, a)  # a process of MM, not of DS


# ------------------------------------------------------------------------------------------------
# Stock Entry: the rule on real rows
# ------------------------------------------------------------------------------------------------


class TestStockEntryRule(_EnforcementHarness):
	def entry(self, mwo, mop, **fields):
		return _entry(self.w, mwo, mop, **fields)

	def check(self, se, method="before_submit"):
		pe.check_stock_entry(se, method)
		return se

	def setUp(self):
		super().setUp()
		if not frappe.get_meta("Stock Entry").has_field(pe.STOCK_ENTRY_PROCESS_FIELD):
			from jewellery_erpnext.patches.add_process_control_fields import execute

			execute()

	def test_a_movement_in_the_jobs_process_passes_and_is_stamped(self):
		a, _b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		se = self.check(self.entry(mwo, mop))
		self.assertEqual(se.get(pe.STOCK_ENTRY_PROCESS_FIELD), a)

	def test_a_selected_process_other_than_the_jobs_is_refused(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		with self.assertRaisesRegex(ProcessEnforcementError, f"selected process {b}"):
			self.check(self.entry(mwo, mop, process=b))

	def test_a_user_without_access_is_refused(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		frappe.set_user(self.clerk("store-b", b))
		with self.assertRaisesRegex(
			frappe.PermissionError, f"no access to process {a}"
		):
			self.check(self.entry(mwo, mop))

	def test_an_operation_that_is_not_the_current_one_is_refused(self):
		a, _b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		newer = fx.make_operation(self.w, mwo, "MM")
		fx.set_pointer(mwo, newer)
		self.assign(mwo, newer, a)
		with self.assertRaisesRegex(
			ProcessEnforcementError, "is not the current operation"
		):
			self.check(self.entry(mwo, mop))

	def test_another_jobs_operation_is_refused(self):
		a, _b, _c = self.cell3()
		mwo, _mop = self.job_in(a)
		_other, other_mop = self.job_in(a)
		with self.assertRaisesRegex(ProcessEnforcementError, "belongs to work order"):
			self.check(self.entry(mwo, other_mop))

	def test_jobs_in_two_processes_on_one_entry_are_refused(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		other, other_mop = self.job_in(b)
		se = self.entry(mwo, mop)
		se.manufacturing_work_order = se.manufacturing_operation = None
		se.append(
			"items",
			{
				"item_code": self.w.item.metal,
				"qty": 0.1,
				"manufacturing_operation": other_mop,
				"custom_manufacturing_work_order": other,
			},
		)
		with self.assertRaisesRegex(ProcessEnforcementError, "different processes"):
			self.check(se)

	def test_a_job_without_a_process_is_refused(self):
		self.cell3()
		mwo, mop = self.job_in(None)
		with self.assertRaisesRegex(
			ProcessEnforcementError, "Initial Process Assignment"
		):
			self.check(self.entry(mwo, mop))

	def test_auto_created_and_source_fields_prove_nothing(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		issue = fx.issue(self.w, mwo)  # a real, submitted Employee IR of this job
		frappe.set_user(self.clerk("forger", b))
		se = self.entry(mwo, mop, auto_created=1, employee_ir=issue.name)
		with refused(self, frappe.PermissionError, "no access to process"):
			self.check(se)

	def test_an_entry_made_inside_a_checked_employee_ir_is_its_parents_business(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		parent = self.issue_doc(mwo)
		parent.name = "TPCF-PARENT"
		pe._register(parent, parent.employee_ir_operations)
		self.addCleanup(pe.release_parent, parent)
		frappe.set_user(
			self.clerk("generated", b)
		)  # no access to A: the parent was checked
		self.check(self.entry(mwo, mop, auto_created=1, employee_ir=parent.name))
		# ...but only for the parent's own work orders.
		other, other_mop = self.job_in(a)
		with refused(self, frappe.PermissionError, "no access to process"):
			self.check(self.entry(other, other_mop, employee_ir=parent.name))

	def test_an_issue_into_a_cell_owns_its_entries_before_the_process_is_stamped(self):
		# DS has no process control; MM does. The Issue mints the In-Transit operation in MM and
		# makes its Stock Entries before on_submit stamps the destination process on it.
		a, _b, _c = self.cell3()
		mwo, source = self.job_in(None, key="DS")
		minted = fx.make_operation(
			self.w, mwo, "MM", previous_mop=source, department_ir_status="In-Transit"
		)
		fx.set_pointer(mwo, minted)
		parent = fx.department_ir(
			self.w,
			"Issue",
			[(source, mwo)],
			current="DS",
			to="MM",
			destination_process=a,
		)
		parent.name = "TPCF-DIR-PARENT"
		self.addCleanup(pe.release_parent, parent)
		with self.assertRaisesRegex(
			ProcessEnforcementError, "Initial Process Assignment"
		):
			self.check(
				self.entry(mwo, minted, department_ir=parent.name)
			)  # not registered yet
		pe.check_department_ir(
			parent, "before_submit"
		)  # nothing to check; registers it
		self.check(self.entry(mwo, minted, auto_created=1, department_ir=parent.name))

	def eod_log(self, status="Running"):
		log = frappe.get_doc(
			{
				"doctype": "MOP EOD Sync Log",
				"status": status,
				"trigger_type": "Manual",
				"posting_date": frappe.utils.nowdate(),
			}
		)
		log.db_insert()
		return log.name

	def eod_entry(self, mwo, mop, log):
		se = self.entry(mwo, mop, auto_created=1)
		se.manufacturing_work_order = se.manufacturing_operation = None
		se.custom_is_eod_sync_stock_entry = 1
		se.custom_eod_sync_log = log
		se.stock_entry_type = "Material Transfer to Department"
		return se

	def moved_on(self, a):
		"""A job whose earlier operation has unsynced history, then moved on to a newer one."""
		mwo, old = self.job_in(a)
		fx.seed_balance(self.w, old, mwo, is_synced=0)
		newer = fx.make_operation(self.w, mwo, "MM", previous_mop=old)
		frappe.db.set_value("Manufacturing Operation", old, "status", "Finished")
		fx.set_pointer(mwo, newer)
		self.assign(mwo, newer, a)
		return mwo, old

	def test_the_eod_sync_posts_history_after_the_job_moved_on(self):
		a, _b, _c = self.cell3()
		mwo, old = self.moved_on(a)
		log = self.eod_log()
		with patch.dict(frappe.flags, {"in_eod_mop_sync": True}):
			self.check(self.eod_entry(mwo, old, log))

	def test_eod_fields_without_the_running_sync_are_a_new_movement(self):
		a, _b, _c = self.cell3()
		mwo, old = self.moved_on(a)
		log = self.eod_log()
		# Not inside the sync: the flags are only fields a client can write.
		with self.assertRaisesRegex(
			ProcessEnforcementError, "is not the current operation"
		):
			self.check(self.eod_entry(mwo, old, log))
		# Inside a sync whose log is not running: not genuine either.
		finished = self.eod_log(status="Completed")
		with patch.dict(frappe.flags, {"in_eod_mop_sync": True}):
			with self.assertRaises(ProcessEnforcementError):
				self.check(self.eod_entry(mwo, old, finished))

	def test_eod_posting_for_an_operation_without_pending_history_is_refused(self):
		a, _b, _c = self.cell3()
		mwo, old = self.moved_on(a)
		frappe.db.set_value("MOP Log", {"manufacturing_operation": old}, "is_synced", 1)
		log = self.eod_log()
		with patch.dict(frappe.flags, {"in_eod_mop_sync": True}):
			with self.assertRaises(ProcessEnforcementError):
				self.check(self.eod_entry(mwo, old, log))

	def test_a_saved_eod_draft_may_not_claim_more_than_its_log_recorded(self):
		a, _b, _c = self.cell3()
		mwo, old = self.moved_on(a)
		log = self.eod_log(status="Completed")
		se = self.eod_entry(mwo, old, log)
		se.name = "TPCF-EOD-DRAFT"
		se.flags.name_set = True
		se.set("__islocal", 0)
		with patch.object(se, "is_new", return_value=False):
			frappe.get_doc(
				{
					"doctype": "MOP EOD Sync Log Item",
					"parent": log,
					"parenttype": "MOP EOD Sync Log",
					"parentfield": "items",
					"manufacturing_work_order": mwo,
					"manufacturing_operation": old,
					"item_code": self.w.item.metal,
					"qty": 0.2,
					"draft_stock_entry": se.name,
				}
			).db_insert()
			self.check(se)  # exactly what the sync recorded
			se.items[0].qty = 0.5  # more than recorded
			with self.assertRaises(ProcessEnforcementError):
				self.check(se)

	def test_cancel_needs_access_to_the_process_it_was_made_in(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		se = self.entry(mwo, mop, process=a)
		frappe.set_user(self.clerk("cancel-b", b))
		with refused(self, frappe.PermissionError, "no access to process"):
			self.check(se, "before_cancel")


# Stock harness copied from test_work_order_transfer_destination (not on every branch).
EOD_LOCK = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock.is_eod_sync_locked"
RECON_WINDOW = "jewellery_erpnext.jewellery_erpnext.stock_recon_window._enabled"


def _leaked_rows(prefix):
	"""``{doctype: n}`` of committed rows carrying ``prefix`` (items embed it after their family letter)."""
	leaked = {}
	for doctype, condition in (
		("Company", "name LIKE %(start)s"),
		("Department", "name LIKE %(start)s"),
		("Employee", "name LIKE %(start)s"),
		("Warehouse", "name LIKE %(start)s"),
		("Item", "name LIKE %(anywhere)s"),
		("Manufacturing Work Order", "name LIKE %(start)s"),
		(
			"Manufacturing Operation",
			"name LIKE %(start)s OR manufacturing_work_order LIKE %(start)s",
		),
		("Employee IR", "name LIKE %(start)s"),
		("MOP Log", "manufacturing_work_order LIKE %(start)s"),
		("Stock Entry", "manufacturing_work_order LIKE %(start)s"),
		("Stock Ledger Entry", "warehouse LIKE %(start)s"),
	):
		n = frappe.db.sql(
			f"SELECT COUNT(*) FROM `tab{doctype}` WHERE {condition}",
			{"start": f"{prefix}%", "anywhere": f"%{prefix}%"},
		)[0][0]
		if n:
			leaked[doctype] = n
	return leaked


class _RecordingQueue:
	def __init__(self, sink):
		self.sink = sink

	def enqueue_call(self, *args, **kwargs):
		self.sink.append((kwargs.get("kwargs") or {}).get("method"))


class _StockHarness(IntegrationTestCase):
	"""Rollback per test, no commits, no enqueues (from test_work_order_transfer_destination)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.run_prefix = fx.new_prefix("TWOD")
		cls.count = 0

	@classmethod
	def tearDownClass(cls):
		# Every test rolls back and asserts nothing committed; this only proves it. fx.purge would
		# also clean up, but its whole-table LIKE scans take minutes on a site with real history.
		frappe.db.rollback()
		leaked = _leaked_rows(cls.run_prefix)
		super().tearDownClass()
		if leaked:
			raise AssertionError(f"{cls.__name__} committed rows: {leaked}")

	def setUp(self):
		super().setUp()
		type(self).count += 1
		self.prefix = f"{self.run_prefix}-{self.count:02d}"
		self.commits, self.enqueued = [], []
		for patcher in (
			patch.object(frappe.db, "commit", lambda *a, **k: self.commits.append(a)),
			patch(
				"frappe.utils.background_jobs.get_queue",
				lambda *a, **k: _RecordingQueue(self.enqueued),
			),
			patch(EOD_LOCK, return_value=False),
			patch(RECON_WINDOW, return_value=False),
		):
			patcher.start()
			self.addCleanup(patcher.stop)
		frappe.local.message_log = []

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.value_cache.clear()
		frappe.local.message_log = []
		super().tearDown()
		self.assertEqual(self.commits, [], "a cascade committed mid-transaction")
		self.assertEqual(self.enqueued, [], "a cascade enqueued a job")


# ------------------------------------------------------------------------------------------------
# Stock Entry: real submissions through the whole hook chain
# ------------------------------------------------------------------------------------------------


class TestStockEntrySubmission(_LiteralRefusals, _StockHarness):
	"""``insert`` + ``submit`` of a WORK ORDER transfer in a cell, reading the Stock Ledger back."""

	def insert(self, doctype, **values):
		doc = frappe.get_doc({"doctype": doctype, **values})
		doc.db_insert()
		return doc.name

	def warehouse(self, label, warehouse_type, **owner):
		return (
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": f"{self.prefix} {label}",
					"company": self.company,
					"parent_warehouse": self.root_warehouse,
					"warehouse_type": warehouse_type,
					**owner,
				}
			)
			.insert()
			.name
		)

	def build_masters(self, abbr):
		p = self.prefix
		self.root_warehouse = frappe.db.get_value(
			"Warehouse", {"company": self.company, "is_group": 1}, order_by="lft asc"
		)
		department = self.insert(
			"Department",
			name=f"{p} Dept - {abbr}",
			department_name=f"{p} Dept",
			company=self.company,
			is_group=0,
		)
		self.employees = {
			key: self.insert(
				"Employee",
				name=f"{p}-{key}",
				first_name=f"{p} {key}",
				employee_name=f"{p} {key}",
				company=self.company,
				department=department,
				status="Active",
				gender="Male",
				date_of_birth="1990-01-01",
				date_of_joining="2020-01-01",
			)
			for key in ("A", "B")
		}
		self.rm = self.warehouse("RM", "Raw Material", department=department)
		self.wo = self.warehouse("WO", "Manufacturing", department=department)
		self.worker = {
			key: self.warehouse(f"{key} WIP", "Manufacturing", employee=employee)
			for key, employee in self.employees.items()
		}
		self.item = self.insert(
			"Item",
			name=f"{p}-ITEM",
			item_code=f"{p}-ITEM",
			item_name=f"{p}-ITEM",
			is_stock_item=1,
			stock_uom="Gram",
			item_group=frappe.db.get_value("Item Group", {"is_group": 0}),
		)
		self.mwo = self.insert(
			"Manufacturing Work Order",
			name=f"{p}-MWO",
			docstatus=1,
			company=self.company,
			manufacturer=self.manufacturer,
			department=department,
			status="Not Started",
			posting_date=nowdate(),
			qty=1,
		)
		self.mop = self.insert(
			"Manufacturing Operation",
			name=f"{p}-MOP",
			company=self.company,
			manufacturer=self.manufacturer,
			department=department,
			manufacturing_work_order=self.mwo,
			status="Not Started",
			type="Manufacturing Work Order",
			qty=1,
		)
		frappe.db.set_value(
			"Manufacturing Work Order",
			self.mwo,
			"manufacturing_operation",
			self.mop,
			update_modified=False,
		)
		receipt = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"manufacturer": self.manufacturer,
				"items": [
					{
						"item_code": self.item,
						"qty": 10,
						"basic_rate": 100,
						"t_warehouse": self.rm,
						"uom": "Gram",
						"conversion_factor": 1,
					}
				],
			}
		)
		receipt.insert()
		receipt.submit()

	def transfer(self, *targets):
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": WORK_ORDER_TRANSFER,
				"company": self.company,
				"manufacturer": self.manufacturer,
				"manufacturing_work_order": self.mwo,
				"manufacturing_operation": self.mop,
				"from_warehouse": self.rm,
				"to_warehouse": self.wo,
				"items": [
					{
						"item_code": self.item,
						"qty": 1,
						"s_warehouse": self.rm,
						"t_warehouse": target,
						"uom": "Gram",
						"conversion_factor": 1,
						"manufacturing_operation": self.mop,
						"custom_manufacturing_work_order": self.mwo,
					}
					for target in targets
				],
			}
		)
		# The work-order reservation bridge runs at on_submit, after this rule, and needs a Sales
		# Order line this world does not have. It is the producers' own opt-out; the rule ignores it.
		se.flags.skip_mwo_reservation = True
		return se

	def ledger(self, name):
		return [
			list(row)
			for row in frappe.get_all(
				"Stock Ledger Entry",
				filters={"voucher_no": name, "is_cancelled": 0},
				fields=["warehouse", "actual_qty"],
				order_by="actual_qty asc",
				as_list=True,
			)
		]

	def assertNothingPosted(self, name):
		self.assertEqual(self.ledger(name), [])
		self.assertFalse(
			frappe.db.exists("GL Entry", {"voucher_no": name, "is_cancelled": 0})
		)

	def setUp(self):
		super().setUp()  # _StockHarness: rollback per test, no commits, no enqueues
		setting = frappe.db.get_value(
			"Manufacturing Setting",
			{"company": ["is", "set"]},
			["company", "manufacturer"],
			as_dict=True,
		)
		company = setting and frappe.get_cached_doc("Company", setting.company)
		if not (company and company.default_inventory_account and company.cost_center):
			self.skipTest(
				"needs a company with stock accounts and a Manufacturing Setting"
			)
		self.company, self.manufacturer = company.name, setting.manufacturer
		frappe.set_user("Administrator")
		self.build_masters(company.abbr)
		if not frappe.get_meta("Stock Entry").has_field(pe.STOCK_ENTRY_PROCESS_FIELD):
			from jewellery_erpnext.patches.add_process_control_fields import execute

			execute()
		self.addCleanup(frappe.set_user, "Administrator")
		self.department = frappe.db.get_value(
			"Manufacturing Operation", self.mop, "department"
		)
		self.process_a = self.cell_process("A")
		self.process_b = self.cell_process("B")
		frappe.db.set_value("Department", self.department, pc.DEPARTMENT_FLAG, 1)
		frappe.db.set_single_value("MOP Settings", pc.PILOT_FIELD, 1)
		frappe.db.set_value(
			"Manufacturing Operation", self.mop, "department_process", self.process_a
		)
		frappe.db.set_value(
			"Manufacturing Work Order", self.mwo, "current_process", self.process_a
		)
		for process in (self.process_a, self.process_b):
			self.grant("Administrator", process)

	def cell_process(self, label):
		return (
			frappe.get_doc(
				{
					"doctype": "Department Process",
					"process_name": f"{self.prefix} Process {label}",
					"department": self.department,
					"manufacturer": self.manufacturer,
				}
			)
			.insert()
			.name
		)

	def grant(self, user, process):
		return frappe.get_doc(
			{
				"doctype": "User Process Access",
				"user": user,
				"department_process": process,
			}
		).insert()

	def clerk(self, process=None):
		email = f"{self.prefix.lower()}-clerk@example.com"
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": f"{self.prefix} clerk",
				"user_type": "System User",
				"roles": [{"role": "System Manager"}, {"role": "Stock User"}],
			}
		).insert(ignore_permissions=True)
		# Real Stock Entry rights, so a refusal can only be process control's (asserted by text).
		role = f"{self.prefix} Store"
		frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
			ignore_permissions=True
		)
		add_permission("Stock Entry", role)
		for ptype in ("write", "create", "submit", "cancel"):
			update_permission_property("Stock Entry", role, 0, ptype, 1)
		self.addCleanup(frappe.clear_cache, doctype="Stock Entry")
		frappe.get_doc("User", email).add_roles(role)
		if process:
			self.grant(email, process)
		return email

	def test_a_movement_in_the_jobs_process_posts_and_records_it(self):
		se = self.transfer(self.wo)
		se.insert()
		se.submit()
		self.assertEqual(self.ledger(se.name), [[self.rm, -1.0], [self.wo, 1.0]])
		self.assertEqual(
			frappe.db.get_value("Stock Entry", se.name, pe.STOCK_ENTRY_PROCESS_FIELD),
			self.process_a,
		)

	def test_a_user_without_access_stores_and_posts_nothing(self):
		frappe.set_user(self.clerk(self.process_b))
		with refused(self, frappe.PermissionError, "no access to process"):
			self.transfer(self.wo).insert()
		frappe.set_user("Administrator")
		self.assertFalse(
			frappe.db.exists("Stock Entry", {"manufacturing_operation": self.mop})
		)

	def test_a_draft_saved_before_the_job_changed_process_cannot_submit(self):
		se = self.transfer(self.wo)
		se.insert()
		self.assertEqual(se.get(pe.STOCK_ENTRY_PROCESS_FIELD), self.process_a)
		# The job moves on to B (the transition service writes these two).
		frappe.db.set_value(
			"Manufacturing Operation", self.mop, "department_process", self.process_b
		)
		frappe.db.set_value(
			"Manufacturing Work Order", self.mwo, "current_process", self.process_b
		)
		draft = frappe.get_doc("Stock Entry", se.name)
		draft.flags.skip_mwo_reservation = True
		with self.assertRaisesRegex(ProcessEnforcementError, "selected process"):
			draft.submit()
		self.assertEqual(frappe.db.get_value("Stock Entry", se.name, "docstatus"), 0)
		self.assertNothingPosted(se.name)

	def test_cancel_needs_access_to_the_recorded_process(self):
		se = self.transfer(self.wo)
		se.insert()
		se.submit()
		frappe.set_user(self.clerk(self.process_b))
		with refused(self, frappe.PermissionError, "no access to process"):
			frappe.get_doc("Stock Entry", se.name).cancel()
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Stock Entry", se.name, "docstatus"), 1)


# ------------------------------------------------------------------------------------------------
# Material Request › Transfer to MOP
# ------------------------------------------------------------------------------------------------


class TestMaterialRequestTransfer(_EnforcementHarness):
	def request(self, mwo, **values):
		pmo = frappe.db.get_value(
			"Manufacturing Work Order", mwo, "manufacturing_order"
		)
		doc = frappe.new_doc("Material Request")
		doc.manufacturing_order = values.pop("manufacturing_order", pmo)
		doc.custom_manufacturing_work_order = values.pop(
			"custom_manufacturing_work_order", mwo
		)
		doc.update(values)
		return doc

	def test_transfer_to_the_current_operation_in_the_users_process_passes(self):
		a, _b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		pe.check_material_request_transfer(self.request(mwo), mop)

	def test_an_old_request_cannot_transfer_after_the_job_moved_on(self):
		a, _b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		newer = fx.make_operation(self.w, mwo, "MM", previous_mop=mop)
		fx.set_pointer(mwo, newer)
		with self.assertRaisesRegex(
			ProcessEnforcementError, "no longer the current operation"
		):
			pe.check_material_request_transfer(self.request(mwo), mop)

	def test_away_and_back_is_judged_by_who_acts_now(self):
		# A -> B -> A: the job is back in A. A user who holds only B cannot transfer to it, however
		# the request looked when it was raised.
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		self.assign(mwo, mop, b)
		self.assign(mwo, mop, a)
		frappe.set_user(self.clerk("store-b", b))
		with self.assertRaisesRegex(
			frappe.PermissionError, f"no access to process {a}"
		):
			pe.check_material_request_transfer(self.request(mwo), mop)

	def test_an_operation_of_another_work_order_is_refused(self):
		a, _b, _c = self.cell3()
		mwo, _ = self.job_in(a)
		_other, other_mop = self.job_in(a)
		with self.assertRaisesRegex(ProcessEnforcementError, "belongs to work order"):
			pe.check_material_request_transfer(self.request(mwo), other_mop)

	def test_the_dispatcher_checks_before_either_builder(self):
		doc = _mr()
		stub = _get_value_stub(_mop_row("MM"), "MM")
		refused = ProcessEnforcementError("refused")
		with patch(f"{_MR}.frappe.db.get_value", side_effect=stub), patch.object(
			mr_mod, "check_material_request_transfer", side_effect=refused
		) as check, patch.object(mr_mod, "make_mop_stock_entry") as plain, patch.object(
			mr_mod, "make_department_mop_stock_entry"
		) as department, patch.object(
			mr_mod, "_workflow_action_just_applied", return_value=True
		), patch.object(mr_mod, "validate_mop_department"), patch.object(
			mr_mod, "validate_department_transfer_frozen"
		), patch.object(mr_mod, "validate_transfer_se_created"):
			with self.assertRaises(ProcessEnforcementError):
				mr_mod.before_update_after_submit(doc, None)
		check.assert_called_once_with(doc, doc.custom_manufacturing_operation)
		plain.assert_not_called()
		department.assert_not_called()

	def test_the_builders_are_no_longer_api_endpoints(self):
		from jewellery_erpnext.jewellery_erpnext.customization.material_request import (
			material_request as builders,
		)

		for fn in (
			builders.make_mop_stock_entry,
			builders.make_department_mop_stock_entry,
		):
			with self.subTest(fn.__name__):
				self.assertNotIn(fn, frappe.whitelisted)


# ------------------------------------------------------------------------------------------------
# scope and query counts
# ------------------------------------------------------------------------------------------------


class TestScopeAndQueries(_EnforcementHarness):
	def test_a_department_without_process_control_is_untouched(self):
		self.cell3()
		mwo, mop = self.job_in(None, key="DS")  # DS has no process control
		se = _entry(self.w, mwo, mop)
		pe.check_stock_entry(se, "before_submit")
		self.assertFalse(se.get(pe.STOCK_ENTRY_PROCESS_FIELD))

	def test_with_the_pilot_off_nothing_is_checked(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(b)
		frappe.db.set_single_value("MOP Settings", pc.PILOT_FIELD, 0)
		fx.issue(self.w, mwo)  # MM's operation is in A; the job is in B
		self.assertEqual(self.mop_state(mop).status, "WIP")

	def queries(self, fn):
		count = {"n": 0}
		original = frappe.db.sql

		def counting(*args, **kwargs):
			count["n"] += 1
			return original(*args, **kwargs)

		with patch.object(frappe.db, "sql", side_effect=counting):
			fn()
		return count["n"]

	def test_the_check_reads_a_fixed_number_of_times_whatever_the_rows(self):
		a, _b, _c = self.cell3()
		one = [self.job_in(a)[0]]
		ten = [self.job_in(a)[0] for _ in range(10)]
		small = self.issue_doc(one)
		large = self.issue_doc(ten)
		pe.check_employee_ir(
			small, "validate"
		)  # warm the meta and settings caches first
		self.assertEqual(
			self.queries(lambda: pe.check_employee_ir(small, "validate")),
			self.queries(lambda: pe.check_employee_ir(large, "validate")),
		)


# ------------------------------------------------------------------------------------------------
# a genuine two-connection race
# ------------------------------------------------------------------------------------------------

try:
	from jewellery_erpnext.jewellery_erpnext.tests import (
		test_current_operation_race as _race,
	)
except ImportError:  # pragma: no cover - branch without the guard's race suite
	_race = None

RACES_ENABLED = bool(_race and _race.RACES_ENABLED)


@skipUnless(
	RACES_ENABLED, "set CURRENT_OPERATION_RACE_TESTS=1 to run the two-connection race"
)
class TestStockEntryRace(_race._RaceCase if _race else object):  # type: ignore[misc]
	"""While another connection holds the work order (as a Process Transfer or an Employee IR
	does), the Stock Entry check waits within its lock budget, then refuses as busy; once the
	holder is gone the same check passes. Committed world, purged afterwards."""

	if _race:
		from jewellery_erpnext.jewellery_erpnext.tests.test_process_transfer_workflow import (
			TestProcessTransferRace as _Day2,
		)

		RESIDUE_TABLES = _Day2.RESIDUE_TABLES
		_purge_and_verify = _Day2._purge_and_verify
		tearDown = _Day2.tearDown

	def test_a_stock_entry_waits_for_and_refuses_a_held_work_order(self):
		w = self.world
		a = (
			frappe.get_doc(
				{
					"doctype": "Department Process",
					"process_name": f"{w.prefix} A",
					"department": w.departments["MM"],
					"manufacturer": w.manufacturer,
				}
			)
			.insert()
			.name
		)
		op = frappe.get_doc("Department Operation", w.operations["MM"])
		op.department_process = a
		op.save()
		frappe.db.set_value("Department", w.departments["MM"], pc.DEPARTMENT_FLAG, 1)
		frappe.get_doc(
			{
				"doctype": "User Process Access",
				"user": "Administrator",
				"department_process": a,
			}
		).insert()
		mwo, mop = fx.make_work_order(w, "MM", seed=False)
		frappe.db.set_value("Manufacturing Operation", mop, "department_process", a)
		frappe.db.set_value("Manufacturing Work Order", mwo, "current_process", a)
		self.commit()

		race = self.new_race("W_ready", "H_parked", "H_release", "H_done")
		race.start("H", "_role_hold_work_order", mwo=mwo, waiter="W")
		race.hello("H")
		race.set("W_ready")
		race.await_event("H_parked")
		started = time.monotonic()
		with patch(f"{pe.__name__}.is_pilot_enabled", return_value=True), patch.dict(
			frappe.conf, {"current_operation_lock_wait": _race.SHORT_LOCK_WAIT}
		):
			with self.assertRaises(WorkOrderBusyError):
				pe.check_stock_entry(_entry(w, mwo, mop), "before_submit")
			waited = time.monotonic() - started
			frappe.db.rollback()
			race.set("H_release")
			race.finish()
			self.fresh()
			self.assertGreaterEqual(waited, _race.SHORT_LOCK_WAIT - 0.5)
			self.assertLess(waited, _race.SHORT_LOCK_WAIT + _race.TIMEOUT_SLACK + 5)
			se = _entry(w, mwo, mop)
			pe.check_stock_entry(se, "before_submit")  # the holder is gone
			self.assertEqual(se.get(pe.STOCK_ENTRY_PROCESS_FIELD), a)
		frappe.db.rollback()
