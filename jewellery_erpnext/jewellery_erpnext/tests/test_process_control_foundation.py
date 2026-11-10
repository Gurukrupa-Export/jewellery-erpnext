# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Day 1 foundation of process control for manufacturing cells (Process Transfer).

What is under test (``doc_events/process_control.py`` and the four new DocTypes):

* Department Process scope: department, manufacturer, company. Disabling is refused while work is
  open in the process.
* Department Operation to process mapping: same department and manufacturer, required once the
  department uses process control, never ambiguous.
* The Department switch is refused until the cell is fully configured.
* Job state: ``Manufacturing Operation.department_process`` and ``Manufacturing Work Order.current_process``
  change only inside ``process_transition``. A successor operation inherits the process.
* Process Transfer drafts read each row's active operation and process from the database (one
  batched read), refuse mixed or duplicate rows, and cannot be submitted yet.
* User Process Access: one or several processes, data-entry users without an Employee, defaults
  that never grant, no self-grant, and System Manager alone cannot write.

Every test builds its own seedless world (``tests/_current_operation_fixtures.py``) and rolls the
whole transaction back in ``tearDown``. ``frappe.db.commit`` and RQ are recorded and must stay
unused. Everything carries the ``TPCF-`` prefix.
"""

import zlib
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events import process_control as pc
from jewellery_erpnext.jewellery_erpnext.doc_events.process_control import (
	ActiveOperationError,
	ProcessControlError,
	ProcessStateError,
)
from jewellery_erpnext.jewellery_erpnext.doctype.process_transfer.process_transfer import (
	ProcessTransferNotAvailableError,
)
from jewellery_erpnext.jewellery_erpnext.tests import _current_operation_fixtures as fx

EOD_LOCK_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock"
RECON_WINDOW_MODULE = "jewellery_erpnext.jewellery_erpnext.stock_recon_window"
PREFIX_BASE = "TPCF"


class _RecordingQueue:
	def __init__(self, sink):
		self.sink = sink

	def enqueue_call(self, *args, **kwargs):
		self.sink.append((kwargs.get("kwargs") or {}).get("method"))
		return SimpleNamespace(id=kwargs.get("job_id"))


class _ProcessHarness(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not pc.department_flag_installed():
			from jewellery_erpnext.patches.add_process_control_fields import execute

			execute()

	@classmethod
	def tearDownClass(cls):
		frappe.db.rollback()
		leaked = frappe.get_all(
			"Department Process",
			filters={"name": ["like", f"{PREFIX_BASE}-%"]},
			pluck="name",
		)
		super().tearDownClass()
		if leaked:
			raise AssertionError(f"{cls.__name__} committed rows: {leaked}")

	def setUp(self):
		super().setUp()
		self.commits = []
		self.enqueued = []
		for patcher in (
			patch.object(frappe.db, "commit", lambda *a, **k: self.commits.append(a)),
			patch(
				"frappe.utils.background_jobs.get_queue",
				lambda *a, **k: _RecordingQueue(self.enqueued),
			),
			patch(f"{EOD_LOCK_MODULE}.is_eod_sync_locked", return_value=False),
			patch(f"{RECON_WINDOW_MODULE}._enabled", return_value=False),
		):
			patcher.start()
			self.addCleanup(patcher.stop)
		# Deterministic per test (no random names): every test rolls back, so the test id is enough.
		self.w = fx.make_world(
			f"{PREFIX_BASE}-{zlib.crc32(self.id().encode()):08x}",
			departments=("MM", "DS"),
			employees={"MM1": "MM"},
			subcontractors=(),
		)
		frappe.local.message_log = []

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.value_cache.clear()
		fx.clear_caches(self.w)
		frappe.flags.pop(pc.TRANSITION_FLAG, None)
		frappe.local.message_log = []
		super().tearDown()
		self.assertEqual(self.commits, [], "something committed mid-transaction")
		self.assertEqual(self.enqueued, [], "something enqueued a background job")

	# -- helpers --------------------------------------------------------------------------

	def dept(self, key="MM"):
		return self.w.departments[key]

	def process(self, label, key="MM", **extra):
		doc = frappe.get_doc(
			{
				"doctype": "Department Process",
				"process_name": f"{self.w.prefix} {label}",
				"department": self.dept(key),
				"manufacturer": self.w.manufacturer,
				**extra,
			}
		).insert()
		return doc.name

	def operation(self, label, key="MM"):
		name = f"{self.w.prefix} {label}"
		return fx._insert(
			self.w,
			"Department Operation",
			{
				"name": name,
				"operation": name,
				"department": self.dept(key),
				"company": self.w.company,
				"manufacturer": self.w.manufacturer,
			},
		)

	def map_operation(self, operation, process):
		doc = frappe.get_doc("Department Operation", operation)
		doc.department_process = process
		doc.save()
		return doc

	def enable(self, key="MM"):
		frappe.db.set_value("Department", self.dept(key), pc.DEPARTMENT_FLAG, 1)

	def cell(self):
		"""MM as a fully configured cell with two processes (A holds MM's operation, B holds a
		second one). Returns (process_a, process_b, op_b)."""
		a = self.process("Process A")
		b = self.process("Process B")
		self.map_operation(self.w.operations["MM"], a)
		op_b = self.operation("Op B")
		self.map_operation(op_b, b)
		self.enable()
		return a, b, op_b

	def job(self, process=None, key="MM"):
		mwo, mop = fx.make_work_order(self.w, key, seed=False)
		if process:
			frappe.db.set_value(
				"Manufacturing Operation", mop, "department_process", process
			)
		return mwo, mop

	def transfer(self, current, nxt, work_orders, key="MM"):
		return frappe.get_doc(
			{
				"doctype": "Process Transfer",
				"company": self.w.company,
				"manufacturer": self.w.manufacturer,
				"department": self.dept(key),
				"current_process": current,
				"next_process": nxt,
				"operations": [{"manufacturing_work_order": m} for m in work_orders],
			}
		)

	def user(self, key, roles=()):
		email = f"{self.w.prefix.lower()}-{key.lower()}@example.com"
		fx._insert(
			self.w,
			"User",
			{
				"name": email,
				"email": email,
				"first_name": f"{self.w.prefix} {key}",
				"enabled": 1,
				"user_type": "System User",
			},
		)
		for role in roles:
			frappe.get_doc(
				{
					"doctype": "Has Role",
					"parent": email,
					"parenttype": "User",
					"parentfield": "roles",
					"role": role,
				}
			).db_insert()
		frappe.clear_cache(user=email)
		return email

	def grant(self, user, process, **extra):
		return frappe.get_doc(
			{
				"doctype": "User Process Access",
				"user": user,
				"department_process": process,
				**extra,
			}
		).insert()


# ------------------------------------------------------------------------------------------------
# configuration
# ------------------------------------------------------------------------------------------------


class TestProcessConfiguration(_ProcessHarness):
	def test_process_takes_company_from_department(self):
		name = self.process("Model Making")
		doc = frappe.get_doc("Department Process", name)
		self.assertEqual(doc.department, self.dept())
		self.assertEqual(doc.company, self.w.company)
		self.assertEqual(name, f"{self.w.prefix} Model Making - {self.dept()}")

	def test_same_process_name_in_two_departments_is_two_processes(self):
		self.assertNotEqual(
			self.process("Setting", "MM"), self.process("Setting", "DS")
		)

	def test_duplicate_process_in_one_department_is_refused(self):
		self.process("Setting")
		with self.assertRaises(frappe.DuplicateEntryError):
			self.process("Setting")

	def test_manufacturer_of_another_department_is_refused(self):
		other = fx._insert(
			self.w,
			"Manufacturer",
			{
				"name": f"{self.w.prefix}-MFR2",
				"short_name": f"{self.w.prefix}-MFR2",
				"company": self.w.company,
			},
		)
		with self.assertRaisesRegex(ProcessControlError, "does not match Department"):
			self.process("Setting", manufacturer=other)

	def test_manufacturer_of_another_company_is_refused(self):
		if not frappe.get_meta("Manufacturer").has_field("company"):
			self.skipTest("Manufacturer.company is not installed on this site")
		frappe.db.set_value("Department", self.dept(), "manufacturer", None)
		other = fx._insert(
			self.w,
			"Manufacturer",
			{
				"name": f"{self.w.prefix}-MFR3",
				"short_name": f"{self.w.prefix}-MFR3",
				"company": "Some Other Co",
			},
		)
		with self.assertRaisesRegex(ProcessControlError, "belongs to Company"):
			self.process("Setting", manufacturer=other)

	def test_department_cannot_be_moved_after_creation(self):
		doc = frappe.get_doc("Department Process", self.process("Setting"))
		doc.department = self.dept("DS")
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_process_with_open_work_cannot_be_disabled(self):
		a = self.process("Process A")
		self.job(process=a)
		doc = frappe.get_doc("Department Process", a)
		doc.disabled = 1
		with self.assertRaisesRegex(ProcessControlError, "cannot be disabled"):
			doc.save()

	def test_process_with_only_finished_work_can_be_disabled(self):
		a = self.process("Process A")
		_mwo, mop = self.job(process=a)
		frappe.db.set_value("Manufacturing Operation", mop, "status", "Finished")
		doc = frappe.get_doc("Department Process", a)
		doc.disabled = 1
		doc.save()
		self.assertEqual(frappe.db.get_value("Department Process", a, "disabled"), 1)


class TestOperationMapping(_ProcessHarness):
	def test_operation_maps_to_a_process_of_its_department(self):
		a = self.process("Process A")
		self.map_operation(self.w.operations["MM"], a)
		self.assertEqual(
			pc.get_operation_processes([self.w.operations["MM"], "missing-op"]),
			{self.w.operations["MM"]: a, "missing-op": None},
		)

	def test_process_of_another_department_is_refused(self):
		ds = self.process("Setting", "DS")
		with self.assertRaisesRegex(ProcessControlError, "belongs to Department"):
			self.map_operation(self.w.operations["MM"], ds)

	def test_disabled_process_cannot_be_mapped(self):
		a = self.process("Process A", disabled=1)
		with self.assertRaisesRegex(ProcessControlError, "is disabled"):
			self.map_operation(self.w.operations["MM"], a)

	def test_operation_in_enabled_department_needs_a_process(self):
		self.cell()
		doc = frappe.get_doc("Department Operation", self.w.operations["MM"])
		doc.department_process = None
		with self.assertRaisesRegex(ProcessControlError, "needs a Process"):
			doc.save()

	def test_operation_without_process_control_needs_nothing(self):
		# Non-enabled departments keep today's behaviour: saving an unmapped operation still works.
		frappe.get_doc("Department Operation", self.w.operations["DS"]).save()

	def test_mapping_cannot_move_while_work_is_open_under_a_process(self):
		a, b, _op_b = self.cell()
		_mwo, mop = self.job(process=a)
		frappe.db.set_value(
			"Manufacturing Operation", mop, "operation", self.w.operations["MM"]
		)
		with self.assertRaisesRegex(
			ProcessControlError, "cannot change while work is open"
		):
			self.map_operation(self.w.operations["MM"], b)

	def test_worker_process_derives_from_employee_operation(self):
		if not frappe.get_meta("Employee").has_field("custom_operation"):
			self.skipTest("Employee.custom_operation is not installed on this site")
		a = self.process("Process A")
		self.map_operation(self.w.operations["MM"], a)
		employee = self.w.employees["MM1"]
		frappe.db.set_value(
			"Employee", employee, "custom_operation", self.w.operations["MM"]
		)
		self.assertEqual(pc.get_worker_process(employee), a)
		frappe.db.set_value("Employee", employee, "custom_operation", None)
		self.assertIsNone(pc.get_worker_process(employee))


class TestDepartmentSwitch(_ProcessHarness):
	def switch_on(self, key="MM"):
		doc = frappe.get_doc("Department", self.dept(key))
		doc.set(pc.DEPARTMENT_FLAG, 1)
		pc.validate_department(doc)

	def test_switch_needs_an_enabled_process(self):
		with self.assertRaisesRegex(
			ProcessControlError, "no enabled Department Process"
		):
			self.switch_on()
		self.process("Process A", disabled=1)
		with self.assertRaisesRegex(
			ProcessControlError, "no enabled Department Process"
		):
			self.switch_on()

	def test_switch_names_unmapped_operations(self):
		self.process("Process A")
		with self.assertRaisesRegex(ProcessControlError, "have no Process") as ctx:
			self.switch_on()
		self.assertIn(self.w.operations["MM"], str(ctx.exception))

	def test_switch_on_for_a_configured_cell(self):
		a = self.process("Process A")
		self.map_operation(self.w.operations["MM"], a)
		self.switch_on()
		self.assertFalse(
			pc.is_process_control_enabled(self.dept())
		)  # not persisted by the hook
		self.enable()
		self.assertTrue(pc.is_process_control_enabled(self.dept()))
		self.assertFalse(pc.is_process_control_enabled(self.dept("DS")))

	def test_patch_is_idempotent(self):
		from jewellery_erpnext.patches.add_process_control_fields import execute

		execute()
		execute()
		self.assertEqual(
			frappe.db.count(
				"Custom Field", {"dt": "Department", "fieldname": pc.DEPARTMENT_FLAG}
			),
			1,
		)

	def test_hooks_are_registered(self):
		events = frappe.get_hooks("doc_events")
		guard = "jewellery_erpnext.jewellery_erpnext.doc_events.process_control.guard_process_field"
		self.assertIn(guard, events["Manufacturing Operation"]["validate"])
		self.assertIn(guard, events["Manufacturing Work Order"]["validate"])
		self.assertIn(
			guard, events["Manufacturing Work Order"]["before_update_after_submit"]
		)
		self.assertIn(
			"jewellery_erpnext.jewellery_erpnext.doc_events.process_control.validate_department",
			events["Department"]["validate"],
		)


# ------------------------------------------------------------------------------------------------
# job state
# ------------------------------------------------------------------------------------------------


class TestJobState(_ProcessHarness):
	def test_fields_exist_and_are_read_only(self):
		mop_field = frappe.get_meta("Manufacturing Operation").get_field(
			"department_process"
		)
		mwo_field = frappe.get_meta("Manufacturing Work Order").get_field(
			"current_process"
		)
		for field in (mop_field, mwo_field):
			self.assertEqual(field.options, "Department Process")
			self.assertTrue(field.read_only)
		self.assertFalse(
			mop_field.no_copy, "the successor operation must inherit the process"
		)
		self.assertTrue(mwo_field.no_copy)

	def test_manual_change_of_operation_process_is_refused(self):
		a, b, _ = self.cell()
		_mwo, mop = self.job(process=a)
		doc = frappe.get_doc("Manufacturing Operation", mop)
		doc.load_doc_before_save()
		doc.department_process = b
		with self.assertRaises(ProcessStateError):
			pc.guard_process_field(doc)
		with pc.process_transition():
			pc.guard_process_field(doc)  # the transition service may

	def test_manual_change_of_work_order_process_is_refused(self):
		a, _b, _ = self.cell()
		mwo, _mop = self.job()
		doc = frappe.get_doc("Manufacturing Work Order", mwo)
		doc.load_doc_before_save()
		doc.current_process = a
		with self.assertRaises(ProcessStateError):
			pc.guard_process_field(doc)

	def test_new_work_order_cannot_start_with_a_process(self):
		a, _b, _ = self.cell()
		doc = frappe.new_doc("Manufacturing Work Order")
		doc.current_process = a
		with self.assertRaises(ProcessStateError):
			pc.guard_process_field(doc)

	def test_new_operation_may_only_inherit_its_predecessors_process(self):
		a, b, _ = self.cell()
		_mwo, mop = self.job(process=a)
		doc = frappe.new_doc("Manufacturing Operation")
		doc.previous_mop = mop
		doc.department_process = a
		pc.guard_process_field(doc)
		doc.department_process = b
		with self.assertRaises(ProcessStateError):
			pc.guard_process_field(doc)

	def test_unchanged_save_passes(self):
		a, _b, _ = self.cell()
		_mwo, mop = self.job(process=a)
		doc = frappe.get_doc("Manufacturing Operation", mop)
		doc.load_doc_before_save()
		pc.guard_process_field(doc)

	def test_successor_from_employee_receive_keeps_the_process(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.employee_ir import (
			create_operation_for_next_op,
		)

		a, _b, _ = self.cell()
		_mwo, mop = self.job(process=a)
		frappe.db.set_value(
			"Manufacturing Operation",
			mop,
			{
				"operation": self.w.operations["MM"],
				"employee": self.w.employees["MM1"],
				"status": "Finished",
			},
		)
		successor = create_operation_for_next_op(
			mop, employee_ir=f"{self.w.prefix}-EIR", gross_wt=0
		)
		self.assertEqual(successor.department_process, a)
		self.assertFalse(successor.operation)
		self.assertFalse(successor.employee)

	def test_active_operation_is_the_pointer_not_the_newest(self):
		a, _b, _ = self.cell()
		mwo, mop = self.job(process=a)
		fx.make_operation(
			self.w, mwo, "MM", status="Not Started"
		)  # newer, but not the pointer
		self.assertEqual(pc.get_active_operation(mwo).name, mop)

	def test_pointer_with_a_live_successor_is_not_active(self):
		mwo, mop = self.job()
		fx.make_operation(self.w, mwo, "MM", previous_mop=mop)
		with self.assertRaisesRegex(ActiveOperationError, "successor"):
			pc.get_active_operation(mwo)

	def test_reverted_successor_does_not_count(self):
		mwo, mop = self.job()
		fx.make_operation(
			self.w, mwo, "MM", previous_mop=mop, department_ir_status="Revert"
		)
		self.assertEqual(pc.get_active_operation(mwo).name, mop)

	def test_finished_pointer_is_not_active(self):
		mwo, mop = self.job()
		frappe.db.set_value("Manufacturing Operation", mop, "status", "Finished")
		with self.assertRaisesRegex(ActiveOperationError, "not open"):
			pc.get_active_operation(mwo)

	def test_audit_lists_only_active_jobs_without_a_process(self):
		a, _b, _ = self.cell()
		blank, _ = self.job()
		with_process, _ = self.job(process=a)
		listed = [
			r.manufacturing_work_order
			for r in pc.report_active_jobs_without_process(self.dept())
		]
		self.assertIn(blank, listed)
		self.assertNotIn(with_process, listed)
		frappe.db.set_value("Manufacturing Work Order", blank, "status", "Completed")
		listed = [
			r.manufacturing_work_order
			for r in pc.report_active_jobs_without_process(self.dept())
		]
		self.assertNotIn(blank, listed)


# ------------------------------------------------------------------------------------------------
# Process Transfer draft
# ------------------------------------------------------------------------------------------------


class TestProcessTransferDraft(_ProcessHarness):
	def test_valid_draft_reads_rows_from_the_database(self):
		a, b, _ = self.cell()
		mwo1, mop1 = self.job(process=a)
		mwo2, mop2 = self.job(process=a)
		doc = self.transfer(a, b, [mwo1, mwo2])
		doc.operations[0].current_process = b  # a stale/forged client value is replaced
		doc.insert()
		self.assertEqual(
			[r.manufacturing_operation for r in doc.operations], [mop1, mop2]
		)
		self.assertEqual({r.current_process for r in doc.operations}, {a})
		self.assertEqual({r.next_process for r in doc.operations}, {b})
		self.assertEqual(doc.docstatus, 0)

	def test_submission_is_refused_and_nothing_moves(self):
		a, b, _ = self.cell()
		mwo, mop = self.job(process=a)
		doc = self.transfer(a, b, [mwo]).insert()
		with self.assertRaises(ProcessTransferNotAvailableError):
			doc.submit()
		self.assertEqual(
			frappe.db.get_value("Manufacturing Operation", mop, "department_process"), a
		)
		self.assertFalse(
			frappe.db.get_value("Manufacturing Work Order", mwo, "current_process")
		)
		self.assertEqual(
			frappe.db.get_value("Process Transfer", doc.name, "docstatus"), 0
		)

	def test_mixed_rows_are_refused(self):
		a, b, _ = self.cell()
		mwo1, _ = self.job(process=a)
		mwo2, _ = self.job(process=b)
		with self.assertRaisesRegex(
			ProcessControlError, "Row #2.*not the Current Process"
		):
			self.transfer(a, b, [mwo1, mwo2]).insert()

	def test_duplicate_work_order_is_refused(self):
		a, b, _ = self.cell()
		mwo, _ = self.job(process=a)
		with self.assertRaisesRegex(ProcessControlError, "already in row #1"):
			self.transfer(a, b, [mwo, mwo]).insert()

	def test_same_current_and_next_is_refused(self):
		a, _b, _ = self.cell()
		mwo, _ = self.job(process=a)
		with self.assertRaisesRegex(ProcessControlError, "are both"):
			self.transfer(a, a, [mwo]).insert()

	def test_job_without_a_process_is_refused(self):
		a, b, _ = self.cell()
		mwo, _ = self.job()
		with self.assertRaisesRegex(ProcessControlError, "has no current process"):
			self.transfer(a, b, [mwo]).insert()

	def test_department_without_process_control_is_refused(self):
		a, b, _ = self.cell()
		frappe.db.set_value("Department", self.dept(), pc.DEPARTMENT_FLAG, 0)
		mwo, _ = self.job(process=a)
		with self.assertRaisesRegex(
			ProcessControlError, "does not use process control"
		):
			self.transfer(a, b, [mwo]).insert()

	def test_process_of_another_department_is_refused(self):
		a, _b, _ = self.cell()
		ds = self.process("Setting", "DS")
		mwo, _ = self.job(process=a)
		with self.assertRaisesRegex(ProcessControlError, "belongs to Department"):
			self.transfer(a, ds, [mwo]).insert()

	def test_work_order_in_another_department_is_refused(self):
		a, b, _ = self.cell()
		mwo, _ = self.job(process=a, key="DS")
		with self.assertRaisesRegex(ProcessControlError, "is in Department"):
			self.transfer(a, b, [mwo]).insert()

	def test_work_order_with_stale_pointer_is_refused(self):
		a, b, _ = self.cell()
		mwo, mop = self.job(process=a)
		fx.make_operation(self.w, mwo, "MM", previous_mop=mop)
		with self.assertRaises(ActiveOperationError):
			self.transfer(a, b, [mwo]).insert()


# ------------------------------------------------------------------------------------------------
# user access
# ------------------------------------------------------------------------------------------------


class TestUserProcessAccess(_ProcessHarness):
	def test_single_process_user(self):
		a, b, _ = self.cell()
		u = self.user("op1")
		self.grant(u, a)
		self.assertEqual(pc.get_allowed_processes(u, self.dept()), [a])
		self.assertIsNone(pc.get_default_process(u, self.dept()))

	def test_substitute_covers_several_processes_with_one_default(self):
		a, b, _ = self.cell()
		u = self.user("relief")
		self.grant(u, a, is_default=1)
		self.grant(u, b)
		self.assertEqual(
			sorted(pc.get_allowed_processes(u, self.dept())), sorted([a, b])
		)
		self.assertEqual(pc.get_default_process(u, self.dept()), a)
		with self.assertRaisesRegex(
			ProcessControlError, "already has .* as the default"
		):
			frappe.get_doc(
				"User Process Access", {"user": u, "department_process": b}
			).update({"is_default": 1}).save()

	def test_data_entry_user_needs_no_employee(self):
		a, _b, _ = self.cell()
		u = self.user("entry")
		doc = self.grant(u, a)
		self.assertFalse(doc.employee)
		self.assertEqual(doc.department, self.dept())
		self.assertEqual(doc.company, self.w.company)
		self.assertEqual(doc.manufacturer, self.w.manufacturer)

	def test_employee_is_linked_and_ambiguity_is_refused(self):
		a, b, _ = self.cell()
		u = self.user("worker")
		frappe.db.set_value("Employee", self.w.employees["MM1"], "user_id", u)
		self.assertEqual(self.grant(u, a).employee, self.w.employees["MM1"])
		fx._insert(
			self.w,
			"Employee",
			{
				"name": f"{self.w.prefix}-EMP-DUP",
				"first_name": "dup",
				"employee_name": "dup",
				"company": self.w.company,
				"status": "Active",
				"gender": "Male",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"user_id": u,
			},
		)
		with self.assertRaisesRegex(
			ProcessControlError, "more than one active Employee"
		):
			self.grant(u, b)

	def test_disabled_grant_gives_nothing_and_cannot_be_default(self):
		a, _b, _ = self.cell()
		u = self.user("gone")
		self.grant(u, a, enabled=0)
		self.assertEqual(pc.get_allowed_processes(u, self.dept()), [])
		with self.assertRaisesRegex(ProcessControlError, "cannot be the default"):
			self.grant(self.user("gone2"), a, enabled=0, is_default=1)

	def test_disabled_process_is_not_granted_and_drops_out(self):
		a, b, _ = self.cell()
		u = self.user("op2")
		self.grant(u, a)
		self.grant(u, b)
		frappe.db.set_value("Department Process", b, "disabled", 1)
		self.assertEqual(pc.get_allowed_processes(u, self.dept()), [a])
		with self.assertRaisesRegex(
			ProcessControlError, "is disabled and cannot be granted"
		):
			self.grant(self.user("op3"), b)

	def test_duplicate_grant_is_refused(self):
		a, _b, _ = self.cell()
		u = self.user("dup")
		self.grant(u, a)
		with self.assertRaisesRegex(ProcessControlError, "already has access"):
			self.grant(u, a)

	def test_manager_cannot_grant_themselves(self):
		a, _b, _ = self.cell()
		manager = self.user("mgr", roles=("Process Access Manager",))
		other = self.user("op4")
		frappe.set_user(manager)
		self.grant(other, a)  # granting someone else works
		with self.assertRaises(frappe.PermissionError):
			self.grant(manager, a)

	def test_system_manager_alone_cannot_write_access(self):
		sm = self.user("sm", roles=("System Manager",))
		self.assertTrue(frappe.has_permission("User Process Access", "read", user=sm))
		self.assertFalse(
			frappe.has_permission("User Process Access", "create", user=sm)
		)
		self.assertFalse(frappe.has_permission("User Process Access", "write", user=sm))

	def test_operator_role_cannot_write_access_or_processes(self):
		op = self.user("ptu", roles=("Process Transfer User",))
		self.assertFalse(
			frappe.has_permission("User Process Access", "create", user=op)
		)
		self.assertFalse(frappe.has_permission("Department Process", "write", user=op))
		self.assertTrue(frappe.has_permission("Department Process", "read", user=op))
		self.assertTrue(frappe.has_permission("Process Transfer", "create", user=op))


# ------------------------------------------------------------------------------------------------
# Day 1 decisions: first process for existing work, department switch through a real save, forms
# ------------------------------------------------------------------------------------------------


class TestInitialAssignment(_ProcessHarness):
	def initial(self, nxt, work_orders, current=None):
		doc = self.transfer(current, nxt, work_orders)
		doc.initial_assignment = 1
		return doc

	def test_initial_assignment_takes_jobs_without_a_process(self):
		a, _b, _ = self.cell()
		mwo, mop = self.job()
		doc = self.initial(a, [mwo]).insert()
		row = doc.operations[0]
		self.assertEqual(row.manufacturing_operation, mop)
		self.assertFalse(row.current_process)
		self.assertEqual(row.next_process, a)

	def test_initial_assignment_refuses_a_job_that_has_a_process(self):
		a, b, _ = self.cell()
		mwo, _ = self.job(process=a)
		with self.assertRaisesRegex(ProcessControlError, "is already in process"):
			self.initial(b, [mwo]).insert()

	def test_initial_assignment_has_no_current_process(self):
		a, b, _ = self.cell()
		mwo, _ = self.job()
		with self.assertRaisesRegex(
			ProcessControlError, "Current Process must be empty"
		):
			self.initial(b, [mwo], current=a).insert()

	def test_normal_transfer_needs_a_current_process(self):
		_a, b, _ = self.cell()
		mwo, _ = self.job()
		with self.assertRaisesRegex(ProcessControlError, "Select the Current Process"):
			self.transfer(None, b, [mwo]).insert()

	def test_initial_assignment_cannot_be_submitted_yet(self):
		a, _b, _ = self.cell()
		mwo, mop = self.job()
		doc = self.initial(a, [mwo]).insert()
		with self.assertRaises(ProcessTransferNotAvailableError):
			doc.submit()
		self.assertFalse(
			frappe.db.get_value("Manufacturing Operation", mop, "department_process")
		)


class TestDepartmentSwitchSave(_ProcessHarness):
	def test_switch_on_lists_work_without_a_process(self):
		a = self.process("Process A")
		self.map_operation(self.w.operations["MM"], a)
		mwo, _ = self.job()
		doc = frappe.get_doc("Department", self.dept())
		doc.set(pc.DEPARTMENT_FLAG, 1)
		frappe.local.message_log = []
		pc.validate_department(doc)
		messages = " ".join(str(m) for m in frappe.local.message_log)
		self.assertIn(mwo, messages)
		self.assertIn("Initial Process Assignment", messages)

	def test_switch_through_a_real_department_save(self):
		doc = frappe.get_doc("Department", self.dept())
		doc.set(pc.DEPARTMENT_FLAG, 1)
		with self.assertRaisesRegex(
			ProcessControlError, "no enabled Department Process"
		):
			doc.save()
		a = self.process("Process A")
		self.map_operation(self.w.operations["MM"], a)
		doc = frappe.get_doc("Department", self.dept())
		doc.set(pc.DEPARTMENT_FLAG, 1)
		doc.save()
		self.assertTrue(pc.is_process_control_enabled(self.dept()))


class TestFormsLoad(_ProcessHarness):
	def test_forms_load_with_their_scripts(self):
		from frappe.desk.form.load import get_meta_bundle

		expected = {
			"Process Transfer": "scan_mwo(frm)",
			"Department Process": 'frappe.ui.form.on("Department Process"',
			"User Process Access": 'frm.set_query("department_process"',
			"Department Operation": 'frm.set_query("department_process"',
		}
		for doctype, snippet in expected.items():
			bundle = get_meta_bundle(doctype)
			self.assertIn(snippet, bundle[0].get("__js") or "", doctype)
		child = [d["name"] for d in get_meta_bundle("Process Transfer")]
		self.assertIn("Process Transfer Operation", child)


# ------------------------------------------------------------------------------------------------
# Review fixes: database uniqueness, batched reads, bounded scans, escaping, update after submit
# ------------------------------------------------------------------------------------------------


class TestReviewFixes(_ProcessHarness):
	def test_trailing_space_cannot_make_a_second_process(self):
		self.process("Setting")
		with self.assertRaises(frappe.DuplicateEntryError):
			self.process("Setting ")

	def test_database_refuses_a_second_process_of_the_same_name(self):
		a = self.process("Setting")
		row = frappe.get_doc("Department Process", a).as_dict()
		row.update(name=f"{a} copy")
		with self.assertRaises(
			(frappe.UniqueValidationError, frappe.DuplicateEntryError)
		):
			frappe.get_doc(row).db_insert()

	def test_database_refuses_a_duplicate_grant(self):
		a, _b, _ = self.cell()
		u = self.user("dupdb")
		row = self.grant(u, a).as_dict()
		row.update(name=f"{row.name}-copy")
		with self.assertRaises(
			(frappe.UniqueValidationError, frappe.DuplicateEntryError)
		):
			frappe.get_doc(row).db_insert()

	def test_row_reads_do_not_grow_with_the_number_of_rows(self):
		a, _b, _ = self.cell()
		one = [self.job(process=a)[0]]
		four = [self.job(process=a)[0] for _ in range(4)]

		def queries(work_orders):
			with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as spy:
				found = pc.get_active_operations(work_orders)
			self.assertTrue(all(isinstance(v, dict) for v in found.values()), found)
			return spy.call_count

		self.assertEqual(queries(one), queries(four))

	def test_row_errors_name_the_row(self):
		a, b, _ = self.cell()
		good, _ = self.job(process=a)
		stale, mop = self.job(process=a)
		fx.make_operation(self.w, stale, "MM", previous_mop=mop)
		with self.assertRaisesRegex(ActiveOperationError, "Row #2.*successor"):
			self.transfer(a, b, [good, stale]).insert()

	def test_empty_row_is_named(self):
		a, b, _ = self.cell()
		doc = self.transfer(a, b, [""])
		with self.assertRaisesRegex(
			ProcessControlError, "Row #1: select a Manufacturing Work Order"
		):
			doc.insert()

	def test_open_operations_is_bounded_and_skips_reverted(self):
		a, _b, _ = self.cell()
		reverted = None
		for i in range(7):
			_mwo, mop = self.job(process=a)
			if i == 0:
				frappe.db.set_value(
					"Manufacturing Operation", mop, "department_ir_status", "Revert"
				)
				reverted = mop
		found = pc.open_operations(department_process=a)
		self.assertEqual(len(found), pc.MESSAGE_LIST_LIMIT)
		self.assertNotIn(reverted, found)

	def test_mapping_message_names_old_and_new_process(self):
		a, b, _op_b = self.cell()
		_mwo, mop = self.job(process=a)
		frappe.db.set_value(
			"Manufacturing Operation", mop, "operation", self.w.operations["MM"]
		)
		with self.assertRaises(ProcessControlError) as ctx:
			self.map_operation(self.w.operations["MM"], b)
		self.assertIn(a, str(ctx.exception))
		self.assertIn(b, str(ctx.exception))

	def test_submitted_work_order_process_cannot_be_saved_outside_the_transition(self):
		a, _b, _ = self.cell()
		mwo, _ = self.job()
		doc = frappe.get_doc("Manufacturing Work Order", mwo)
		self.assertEqual(doc.docstatus, 1)
		doc.current_process = a
		with self.assertRaises(ProcessStateError):
			doc.save()
		self.assertFalse(
			frappe.db.get_value("Manufacturing Work Order", mwo, "current_process")
		)

	def test_messages_escape_values(self):
		self.assertNotIn("<img", pc.bold('<img src=x onerror="alert(1)">'))
		self.assertIn("&lt;img", pc.bold("<img>"))
