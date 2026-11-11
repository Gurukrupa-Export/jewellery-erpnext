# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Day 2 of process control: the Process Transfer workflow through real documents.

* Scan / fetch (``ProcessTransfer.add_work_orders``) and the one validator for save and submit.
* Submit: locked re-read, stale-draft refusal, authority (GK-DEC-0002), readiness (GK-DEC-0003),
  one transaction for every row, history in Process Transition Log.
* Integration: Employee IR Receive keeps the process; Department IR Issue into a cell assigns its
  destination process (GK-DEC-0005) and its cancel restores it; leaving a cell clears it.
* Cancel (GK-DEC-0007): only the latest change, only while the job is untouched; Employee IR /
  Department IR Receive cannot be cancelled under a later transfer.
* A genuine two-connection race (``TestProcessTransferRace``, gated like the guard's race suite).

Every class except the race one rolls back after each test (Day 1 harness). The pilot switch
(MOP Settings › Enable Process Control) is switched on inside each test's transaction.
"""

import time
from unittest import skipUnless
from unittest.mock import patch

import frappe
from frappe.permissions import add_permission
from frappe.utils import add_to_date, cstr, now_datetime

from jewellery_erpnext.jewellery_erpnext.doc_events import process_control as pc
from jewellery_erpnext.jewellery_erpnext.doc_events.current_operation_guard import (
	WorkOrderBusyError,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.process_control import (
	ProcessControlError,
	ProcessTransitionConflictError,
)
from jewellery_erpnext.jewellery_erpnext.doctype.process_transfer.process_transfer import (
	ProcessTransferNotAvailableError,
)
from jewellery_erpnext.jewellery_erpnext.tests import _current_operation_fixtures as fx
from jewellery_erpnext.jewellery_erpnext.tests.test_process_control_foundation import (
	_ProcessHarness,
)

PT = "Process Transfer"
LOG = "Process Transition Log"
WEIGHT_FIELDS = (
	"gross_wt",
	"net_wt",
	"diamond_wt",
	"gemstone_wt",
	"finding_wt",
	"other_wt",
)


class _WorkflowHarness(_ProcessHarness):
	def setUp(self):
		super().setUp()
		frappe.db.set_single_value("MOP Settings", pc.PILOT_FIELD, 1)

	# -- world ----------------------------------------------------------------------------------

	def cell3(self):
		"""MM as a cell with processes A, B, C; Administrator may act in all three."""
		a, b, op_b = self.cell()
		c = self.process("Process C")
		self.map_operation(self.operation("Op C"), c)
		for p in (a, b, c):
			self.grant("Administrator", p)
		return a, b, c

	def assign(self, mwo, mop, process):
		frappe.db.set_value(
			"Manufacturing Operation", mop, "department_process", process
		)
		frappe.db.set_value("Manufacturing Work Order", mwo, "current_process", process)

	def job_in(self, process, key="MM"):
		mwo, mop = fx.make_work_order(self.w, key, seed=False)
		if process:
			self.assign(mwo, mop, process)
		return mwo, mop

	# -- documents ------------------------------------------------------------------------------

	def submitted(self, current, nxt, mwos, initial=False):
		doc = self.transfer(current, nxt, mwos)
		doc.initial_assignment = 1 if initial else 0
		doc.insert()
		doc.submit()
		return doc

	def state(self, mwo):
		return frappe.db.get_value(
			"Manufacturing Work Order",
			mwo,
			["manufacturing_operation", "current_process", "process_transition"],
			as_dict=True,
		)

	def mop_process(self, mop):
		return frappe.db.get_value("Manufacturing Operation", mop, "department_process")

	def logs(self, **filters):
		return frappe.get_all(LOG, filters=filters, fields=["*"], order_by="name asc")


# ------------------------------------------------------------------------------------------------
# A. scan and validation
# ------------------------------------------------------------------------------------------------


class TestScanAndValidate(_WorkflowHarness):
	def test_scan_fills_rows_from_the_database_and_reports_problems(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		fx.make_operation(self.w, mwo, "MM")  # newer, but not the pointer
		doc = self.transfer(a, b, [])
		doc.operations = []
		frappe.local.message_log = []
		doc.add_work_orders([mwo, f"{self.w.prefix}-NOPE"])
		self.assertEqual([r.manufacturing_operation for r in doc.operations], [mop])
		self.assertEqual(doc.operations[0].current_process, a)
		self.assertIn("was not found", str(frappe.local.message_log))
		frappe.local.message_log = []
		doc.add_work_orders(mwo)
		self.assertEqual(len(doc.operations), 1)
		self.assertIn("already listed", str(frappe.local.message_log))

	def test_scan_does_not_reveal_work_orders_the_user_cannot_read(self):
		a, b, _c = self.cell3()
		hidden, _ = self.job_in(a)
		allowed, _ = self.job_in(a)
		# Sites whose Manufacturing Work Order permissions live in Custom DocPerm (kggk-uat has 23
		# rows) ignore the DocType JSON's read for Process Transfer User; setup grants it in Role
		# Permission Manager, which is what this does.
		add_permission("Manufacturing Work Order", "Process Transfer User")
		frappe.clear_cache(doctype="Manufacturing Work Order")
		u = self.user("scanner", roles=("Process Transfer User",))
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": u,
				"allow": "Manufacturing Work Order",
				"for_value": allowed,
			}
		).insert(ignore_permissions=True)
		frappe.set_user(u)
		doc = self.transfer(a, b, [])
		doc.operations = []
		frappe.local.message_log = []
		doc.add_work_orders([hidden, allowed])
		self.assertEqual(
			[r.manufacturing_work_order for r in doc.operations], [allowed]
		)
		self.assertIn("no access", str(frappe.local.message_log))

	def test_jobs_not_at_rest_are_refused(self):
		a, b, _c = self.cell3()
		cases = {
			"is with employee": {"employee": self.w.employees["MM1"], "status": "WIP"},
			"QC Pending": {"status": "QC Pending"},
			"in transit": {"department_ir_status": "In-Transit"},
		}
		for needle, values in cases.items():
			with self.subTest(needle):
				mwo, mop = self.job_in(a)
				frappe.db.set_value("Manufacturing Operation", mop, values)
				with self.assertRaisesRegex(ProcessTransitionConflictError, needle):
					self.transfer(a, b, [mwo]).insert()
		mwo, _ = self.job_in(a)
		frappe.db.set_value("Manufacturing Work Order", mwo, "status", "Hold")
		with self.assertRaisesRegex(ProcessTransitionConflictError, "is Hold"):
			self.transfer(a, b, [mwo]).insert()

	def test_open_employee_issue_draft_is_refused(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(a)
		draft = fx.issue(self.w, mwo, submit=False)
		with self.assertRaisesRegex(ProcessTransitionConflictError, draft.name):
			self.transfer(a, b, [mwo]).insert()


# ------------------------------------------------------------------------------------------------
# B. submit
# ------------------------------------------------------------------------------------------------


class TestSubmit(_WorkflowHarness):
	def test_transfer_moves_every_row_and_records_history(self):
		a, b, _c = self.cell3()
		jobs = [self.job_in(a) for _ in range(3)]
		doc = self.submitted(a, b, [m for m, _ in jobs])
		for mwo, mop in jobs:
			st = self.state(mwo)
			self.assertEqual((self.mop_process(mop), st.current_process), (b, b))
			self.assertEqual(st.manufacturing_operation, mop)
			log = frappe.get_doc(LOG, st.process_transition)
			self.assertEqual(
				(
					log.event,
					log.from_process,
					log.to_process,
					log.voucher_no,
					log.manufacturing_operation,
				),
				("Process Transfer", a, b, doc.name, mop),
			)
		self.assertEqual(len(self.logs(voucher_no=doc.name)), 3)

	def test_initial_assignment_gives_jobs_their_first_process(self):
		a, _b, _c = self.cell3()
		mwo, mop = self.job_in(None)
		doc = self.submitted(None, a, [mwo], initial=True)
		self.assertEqual(
			(self.mop_process(mop), self.state(mwo).current_process), (a, a)
		)
		self.assertEqual(self.logs(voucher_no=doc.name)[0].event, "Initial Assignment")

	def test_an_invalid_row_changes_nothing(self):
		a, b, _c = self.cell3()
		good, good_mop = self.job_in(a)
		bad, bad_mop = self.job_in(a)
		doc = self.transfer(a, b, [good, bad]).insert()
		frappe.db.set_value(
			"Manufacturing Operation",
			bad_mop,
			{"status": "WIP", "employee": self.w.employees["MM1"]},
		)
		with self.assertRaisesRegex(ProcessTransitionConflictError, "Row #2"):
			frappe.get_doc(PT, doc.name).submit()
		self.assertEqual(self.mop_process(good_mop), a)
		self.assertFalse(self.logs(voucher_no=doc.name))

	def test_a_failure_while_writing_leaves_no_partial_transfer(self):
		a, b, _c = self.cell3()
		first, first_mop = self.job_in(a)
		second, _ = self.job_in(a)
		doc = self.transfer(a, b, [first, second]).insert()
		failing_mwo = max(
			first, second
		)  # rows are written in work-order order: this one last
		real = frappe.db.set_value
		calls = []

		def failing(doctype, name, *args, **kwargs):
			calls.append((doctype, name))
			if doctype == "Manufacturing Work Order" and name == failing_mwo:
				raise frappe.ValidationError("simulated failure on the second row")
			return real(doctype, name, *args, **kwargs)

		frappe.db.savepoint("pt_atomic")
		with patch.object(frappe.db, "set_value", failing):
			with self.assertRaisesRegex(
				frappe.ValidationError, "simulated failure on the second row"
			):
				frappe.get_doc(PT, doc.name).submit()
		# The first row really was written before the failure ...
		self.assertLess(first, failing_mwo)
		self.assertIn(("Manufacturing Operation", first_mop), calls)
		self.assertIn(("Manufacturing Work Order", first), calls)
		self.assertEqual(len(self.logs(voucher_no=doc.name)), 2)
		# ... and the request's rollback (here: to the savepoint) takes it with the rest. Nothing
		# was committed in between (the harness fails the test on any commit).
		frappe.db.rollback(save_point="pt_atomic")
		self.assertEqual(self.mop_process(first_mop), a)
		self.assertFalse(self.logs(voucher_no=doc.name))
		self.assertEqual(frappe.db.get_value(PT, doc.name, "docstatus"), 0)

	def test_history_is_written_once_per_voucher_row_and_event(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		doc = self.submitted(a, b, [mwo])
		with self.assertRaises(
			(frappe.UniqueValidationError, frappe.DuplicateEntryError)
		):
			pc.apply_transition(
				[
					frappe._dict(
						mwo=mwo,
						mop=mop,
						department=self.dept(),
						from_process=a,
						to_process=b,
					)
				],
				event="Process Transfer",
				voucher_type=PT,
				voucher_no=doc.name,
			)

	def test_a_draft_older_than_a_round_trip_is_refused(self):
		a, b, c = self.cell3()
		mwo, _ = self.job_in(a)
		stale = self.transfer(a, c, [mwo]).insert()
		self.submitted(a, b, [mwo])
		self.submitted(b, a, [mwo])  # back in A, as the stale draft saw it
		with self.assertRaisesRegex(
			ProcessTransitionConflictError, "changed process since this draft"
		):
			frappe.get_doc(PT, stale.name).submit()
		self.assertEqual(self.state(mwo).current_process, a)

	def test_draft_saved_again_after_the_round_trip_can_be_submitted(self):
		a, b, c = self.cell3()
		mwo, _ = self.job_in(a)
		stale = self.transfer(a, c, [mwo]).insert()
		self.submitted(a, b, [mwo])
		self.submitted(b, a, [mwo])
		doc = frappe.get_doc(PT, stale.name)
		doc.save()
		doc.submit()
		self.assertEqual(self.state(mwo).current_process, c)

	def test_a_process_change_moves_no_material(self):
		a, b, _c = self.cell3()
		mwo, mop = fx.make_work_order(self.w, "MM", seed=True)
		self.assign(mwo, mop, a)
		before = (
			frappe.db.get_value(
				"Manufacturing Operation", mop, list(WEIGHT_FIELDS), as_dict=True
			),
			frappe.db.count("MOP Log", {"manufacturing_operation": mop}),
			frappe.db.count("Stock Ledger Entry"),
			frappe.db.count("GL Entry"),
			frappe.db.get_value(
				"Manufacturing Operation",
				mop,
				["status", "employee", "department"],
				as_dict=True,
			),
		)
		self.submitted(a, b, [mwo])
		after = (
			frappe.db.get_value(
				"Manufacturing Operation", mop, list(WEIGHT_FIELDS), as_dict=True
			),
			frappe.db.count("MOP Log", {"manufacturing_operation": mop}),
			frappe.db.count("Stock Ledger Entry"),
			frappe.db.count("GL Entry"),
			frappe.db.get_value(
				"Manufacturing Operation",
				mop,
				["status", "employee", "department"],
				as_dict=True,
			),
		)
		self.assertEqual(before, after)

	def test_handoff_needs_access_to_the_current_process_only(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(a)
		doc = self.transfer(a, b, [mwo]).insert()
		outsider = self.user("outsider", roles=("Process Transfer User",))
		self.grant(outsider, b)  # may work in B, not hand over from A
		frappe.set_user(outsider)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(PT, doc.name).submit()
		frappe.set_user("Administrator")
		sender = self.user("sender", roles=("Process Transfer User",))
		self.grant(sender, a)  # works in A; needs nothing in B to hand over
		frappe.set_user(sender)
		frappe.get_doc(PT, doc.name).submit()
		frappe.set_user("Administrator")
		self.assertEqual(self.state(mwo).current_process, b)

	def test_no_role_bypasses_process_access(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(a)
		doc = self.transfer(a, b, [mwo]).insert()
		manager = self.user("sysmgr", roles=("System Manager", "Process Transfer User"))
		frappe.set_user(manager)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(PT, doc.name).submit()
		frappe.set_user("Administrator")
		frappe.db.set_value(
			"User Process Access",
			{"user": "Administrator", "department_process": a},
			"enabled",
			0,
		)
		with self.assertRaises(frappe.PermissionError):  # Administrator too
			frappe.get_doc(PT, doc.name).submit()
		self.assertEqual(self.state(mwo).current_process, a)

	def test_initial_assignment_needs_access_to_the_next_process(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(None)
		doc = self.transfer(None, a, [mwo])
		doc.initial_assignment = 1
		doc.insert()
		wrong = self.user("initwrong", roles=("Process Transfer User",))
		self.grant(wrong, b)
		frappe.set_user(wrong)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(PT, doc.name).submit()
		frappe.set_user("Administrator")
		right = self.user("initright", roles=("Process Transfer User",))
		self.grant(right, a)
		frappe.set_user(right)
		frappe.get_doc(PT, doc.name).submit()
		frappe.set_user("Administrator")
		self.assertEqual(self.state(mwo).current_process, a)

	def test_a_disabled_grant_does_not_authorise(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(a)
		doc = self.transfer(a, b, [mwo]).insert()
		user = self.user("disabledgrant", roles=("Process Transfer User",))
		self.grant(user, a, enabled=0)
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(PT, doc.name).submit()
		frappe.set_user("Administrator")

	def test_a_draft_saved_while_submit_waited_is_caught_at_the_end(self):
		"""The terminal re-check: the validation read is a snapshot that can miss a draft saved
		while the submit waited for its locks (emulated by hiding it from that read)."""
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		doc = self.transfer(a, b, [mwo]).insert()
		draft = fx.issue(self.w, mwo, submit=False)
		frappe.db.savepoint("pt_terminal")
		with patch(f"{PT_MODULE}.open_issue_drafts", return_value={}):
			with self.assertRaisesRegex(ProcessTransitionConflictError, draft.name):
				frappe.get_doc(PT, doc.name).submit()
		frappe.db.rollback(save_point="pt_terminal")
		self.assertEqual(self.mop_process(mop), a)

	def test_api_submit_is_checked_the_same_way(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(a)
		doc = self.transfer(a, b, [mwo]).insert()
		outsider = self.user("apiuser", roles=("Process Transfer User",))
		frappe.set_user(outsider)
		with self.assertRaises(frappe.PermissionError):
			frappe.client.submit(frappe.get_doc(PT, doc.name).as_dict())
		frappe.set_user("Administrator")
		self.assertEqual(self.state(mwo).current_process, a)

	def test_pilot_off_refuses_submit(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(a)
		doc = self.transfer(a, b, [mwo]).insert()
		frappe.db.set_single_value("MOP Settings", pc.PILOT_FIELD, 0)
		with self.assertRaises(ProcessTransferNotAvailableError):
			frappe.get_doc(PT, doc.name).submit()
		self.assertEqual(self.state(mwo).current_process, a)

	def test_document_inserted_already_submitted(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		doc = self.transfer(a, b, [mwo])
		doc.docstatus = 1
		doc.insert()
		self.assertEqual(
			(doc.operations[0].manufacturing_operation, self.mop_process(mop)), (mop, b)
		)


# ------------------------------------------------------------------------------------------------
# C. Employee IR / Department IR
# ------------------------------------------------------------------------------------------------


class TestManufacturingIntegration(_WorkflowHarness):
	def dir_issue(self, mwos, current, to, **fields):
		return fx.submit_ir(
			fx.insert_ir(
				fx.department_ir(
					self.w, "Issue", fx._as_rows(mwos), current=current, to=to, **fields
				)
			)
		)

	def dir_receive(self, issue, mwos, current, previous):
		return fx.submit_ir(
			fx.insert_ir(
				fx.department_ir(
					self.w,
					"Receive",
					mwos,
					current=current,
					previous=previous,
					receive_against=issue.name,
				)
			)
		)

	def test_employee_receive_successor_keeps_the_process(self):
		a, _b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		fx.work_cycle(self.w, mwo)
		st = self.state(mwo)
		self.assertNotEqual(st.manufacturing_operation, mop)
		self.assertEqual(self.mop_process(st.manufacturing_operation), a)
		self.assertEqual(st.current_process, a)

	def test_issue_into_a_cell_needs_a_destination_process(self):
		self.cell3()
		mwo, _ = self.job_in(None, key="DS")
		with self.assertRaisesRegex(
			ProcessControlError, "select the Destination Process"
		):
			self.dir_issue(mwo, "DS", "MM")

	def test_issue_assigns_destination_and_receive_keeps_it(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(None, key="DS")
		issue = self.dir_issue(mwo, "DS", "MM", destination_process=b)
		st = self.state(mwo)
		self.assertEqual(
			(self.mop_process(st.manufacturing_operation), st.current_process), (b, b)
		)
		self.assertEqual(
			frappe.get_doc(LOG, st.process_transition).event, "Department IR Issue"
		)
		self.dir_receive(issue, [mwo], "MM", "DS")
		self.assertEqual(self.state(mwo).current_process, b)
		self.submitted(b, a, [mwo])  # a different process inside the cell afterwards
		self.assertEqual(self.state(mwo).current_process, a)

	def test_issue_cancel_restores_the_source_process(self):
		_a, b, _c = self.cell3()
		mwo, mop = self.job_in(None, key="DS")
		issue = self.dir_issue(mwo, "DS", "MM", destination_process=b)
		fx.reload(issue.name, "Department IR").cancel()
		st = self.state(mwo)
		self.assertEqual((st.manufacturing_operation, st.current_process), (mop, None))
		self.assertEqual(
			frappe.get_doc(LOG, st.process_transition).event, "Cancellation"
		)

	def test_issue_out_of_a_cell_and_its_cancel_restore_the_process(self):
		a, _b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		issue = self.dir_issue(mwo, "MM", "DS")
		issue_log = frappe.get_doc(LOG, self.state(mwo).process_transition)
		self.assertEqual((issue_log.from_process, issue_log.to_process), (a, None))
		fx.reload(issue.name, "Department IR").cancel()
		st = self.state(mwo)
		self.assertEqual(
			(st.manufacturing_operation, st.current_process, self.mop_process(mop)),
			(mop, a, a),
		)
		last = frappe.get_doc(LOG, st.process_transition)
		self.assertEqual(
			(last.event, cstr(last.reverses)), ("Cancellation", cstr(issue_log.name))
		)

	def test_leaving_a_cell_clears_the_process(self):
		a, _b, _c = self.cell3()
		mwo, _ = self.job_in(a)
		self.dir_issue(mwo, "MM", "DS")
		st = self.state(mwo)
		self.assertIsNone(self.mop_process(st.manufacturing_operation))
		self.assertIsNone(st.current_process)

	def test_departments_without_process_control_are_unchanged(self):
		mwo, _ = self.job_in(None, key="DS")
		self.dir_issue(mwo, "DS", "MM")  # MM not a cell in this test
		st = self.state(mwo)
		self.assertEqual((st.current_process, st.process_transition), (None, None))
		self.assertFalse(self.logs(manufacturing_work_order=mwo))

	def test_destination_must_belong_to_the_next_department(self):
		self.cell3()
		other = self.process("Elsewhere", "DS")
		mwo, _ = self.job_in(None, key="DS")
		with self.assertRaisesRegex(
			ProcessControlError, "is not a process of Department"
		):
			self.dir_issue(mwo, "DS", "MM", destination_process=other)


# ------------------------------------------------------------------------------------------------
# D. cancel and amend
# ------------------------------------------------------------------------------------------------


class TestCancel(_WorkflowHarness):
	def test_cancel_restores_both_records_and_keeps_history(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		doc = self.submitted(a, b, [mwo])
		frappe.get_doc(PT, doc.name).cancel()
		st = self.state(mwo)
		self.assertEqual((self.mop_process(mop), st.current_process), (a, a))
		original, inverse = self.logs(voucher_no=doc.name)
		self.assertEqual(
			(inverse.event, cstr(inverse.reverses), inverse.to_process),
			("Cancellation", cstr(original.name), a),
		)

	def test_cancel_after_an_employee_issue_is_refused(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(a)
		doc = self.submitted(a, b, [mwo])
		fx.issue(self.w, mwo)
		with self.assertRaisesRegex(ProcessTransitionConflictError, "is with employee"):
			frappe.get_doc(PT, doc.name).cancel()

	def test_only_the_latest_change_can_be_cancelled(self):
		a, b, c = self.cell3()
		mwo, _ = self.job_in(a)
		first = self.submitted(a, b, [mwo])
		second = self.submitted(b, c, [mwo])
		with self.assertRaisesRegex(ProcessTransitionConflictError, second.name):
			frappe.get_doc(PT, first.name).cancel()
		frappe.get_doc(PT, second.name).cancel()
		with self.assertRaisesRegex(
			ProcessTransitionConflictError, "later process change"
		):
			frappe.get_doc(PT, first.name).cancel()
		self.assertEqual(self.state(mwo).current_process, b)

	def stock_entry(self, mop, *, seconds, on_row=False):
		"""A submitted Stock Entry naming ``mop`` (header, or only a Stock Entry Detail row),
		submitted ``seconds`` from now."""
		when = add_to_date(now_datetime(), seconds=seconds)
		name = fx.next_name(self.w, "SE")
		fx._insert(
			self.w,
			"Stock Entry",
			{
				"name": name,
				"docstatus": 1,
				"company": self.w.company,
				"purpose": "Material Transfer",
				"stock_entry_type": "Material Transfer",
				"manufacturing_operation": None if on_row else mop,
				"creation": add_to_date(when, seconds=-600),  # drafted long before
				"modified": when,
			},
		)
		if on_row:
			frappe.get_doc(
				{
					"doctype": "Stock Entry Detail",
					"name": fx.next_name(self.w, "SED"),
					"parent": name,
					"parenttype": "Stock Entry",
					"parentfield": "items",
					"idx": 1,
					"docstatus": 1,
					"manufacturing_operation": mop,
				}
			).db_insert()
		return name

	def test_cancel_after_material_moved_is_refused(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		doc = self.submitted(a, b, [mwo])
		entry = self.stock_entry(
			mop, seconds=2
		)  # drafted before the transfer, submitted after
		with self.assertRaisesRegex(ProcessTransitionConflictError, entry):
			frappe.get_doc(PT, doc.name).cancel()

	def test_material_named_only_on_a_row_also_refuses(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		doc = self.submitted(a, b, [mwo])
		entry = self.stock_entry(mop, seconds=2, on_row=True)
		with self.assertRaisesRegex(ProcessTransitionConflictError, entry):
			frappe.get_doc(PT, doc.name).cancel()

	def test_material_moved_before_the_transfer_does_not_refuse(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		self.stock_entry(mop, seconds=-60)
		doc = self.submitted(a, b, [mwo])
		self.stock_entry(mop, seconds=-30)  # an older entry next to it changes nothing
		frappe.get_doc(PT, doc.name).cancel()
		self.assertEqual(self.state(mwo).current_process, a)

	def test_cancel_needs_access_to_the_process_it_leaves(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(a)
		doc = self.submitted(a, b, [mwo])
		only_a = self.user("onlya", roles=("Process Transfer User",))
		self.grant(only_a, a)
		frappe.set_user(only_a)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(PT, doc.name).cancel()
		frappe.set_user("Administrator")
		in_b = self.user("inb", roles=("Process Transfer User",))
		self.grant(in_b, b)
		frappe.set_user(in_b)
		frappe.get_doc(PT, doc.name).cancel()
		frappe.set_user("Administrator")
		self.assertEqual(self.state(mwo).current_process, a)

	def test_cancel_with_an_open_employee_issue_draft_is_refused(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(a)
		doc = self.submitted(a, b, [mwo])
		draft = fx.issue(self.w, mwo, submit=False)
		with self.assertRaisesRegex(ProcessTransitionConflictError, draft.name):
			frappe.get_doc(PT, doc.name).cancel()

	def test_an_undone_department_issue_does_not_block_the_earlier_transfer(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		doc = self.submitted(a, b, [mwo])
		issue = fx.submit_ir(
			fx.insert_ir(
				fx.department_ir(self.w, "Issue", [mwo], current="MM", to="DS")
			)
		)
		self.assertIsNone(self.state(mwo).current_process)  # left the cell
		fx.reload(issue.name, "Department IR").cancel()
		self.assertEqual(
			(self.state(mwo).manufacturing_operation, self.state(mwo).current_process),
			(mop, b),
		)
		frappe.get_doc(PT, doc.name).cancel()
		self.assertEqual(
			(self.mop_process(mop), self.state(mwo).current_process), (a, a)
		)

	def test_amend_reads_the_jobs_again(self):
		a, b, _c = self.cell3()
		mwo, mop = self.job_in(a)
		doc = self.submitted(a, b, [mwo])
		frappe.get_doc(PT, doc.name).cancel()
		amended = frappe.copy_doc(frappe.get_doc(PT, doc.name))
		amended.amended_from = doc.name
		amended.docstatus = (
			0  # what Desk's Amend does (copy_doc keeps the cancelled docstatus)
		)
		amended.operations[
			0
		].current_process = b  # stale copy from the cancelled document
		amended.insert()
		row = amended.operations[0]
		self.assertEqual((row.manufacturing_operation, row.current_process), (mop, a))

	def test_employee_receive_cannot_be_cancelled_under_a_later_transfer(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(a)
		_issue, receive = fx.work_cycle(self.w, mwo)
		doc = self.submitted(a, b, [mwo])
		with self.assertRaisesRegex(ProcessTransitionConflictError, doc.name):
			fx.reload(receive.name, "Employee IR").cancel()
		frappe.get_doc(PT, doc.name).cancel()
		fx.reload(
			receive.name, "Employee IR"
		).cancel()  # allowed again once the transfer is undone

	def test_department_receive_cannot_be_cancelled_under_a_later_transfer(self):
		a, b, _c = self.cell3()
		mwo, _ = self.job_in(None, key="DS")
		issue = fx.submit_ir(
			fx.insert_ir(
				fx.department_ir(
					self.w, "Issue", [mwo], current="DS", to="MM", destination_process=a
				)
			)
		)
		receive = fx.submit_ir(
			fx.insert_ir(
				fx.department_ir(
					self.w,
					"Receive",
					[mwo],
					current="MM",
					previous="DS",
					receive_against=issue.name,
				)
			)
		)
		doc = self.submitted(a, b, [mwo])
		with self.assertRaisesRegex(ProcessTransitionConflictError, doc.name):
			fx.reload(receive.name, "Department IR").cancel()


# ------------------------------------------------------------------------------------------------
# E. genuine two-connection race (committed world, purged afterwards)
# ------------------------------------------------------------------------------------------------

try:
	from jewellery_erpnext.jewellery_erpnext.tests import (
		test_current_operation_race as _race,
	)
except ImportError:  # pragma: no cover - branch without the guard's race suite
	_race = None

RACES_ENABLED = bool(_race and _race.RACES_ENABLED)


@skipUnless(RACES_ENABLED, getattr(_race, "SKIP_REASON", "race suite not available"))
class TestProcessTransferRace(_race._RaceCase if _race else object):  # type: ignore[misc]
	"""A second database connection holds the work order (the lock a Stock-Entry-like writer
	takes); Process Transfer's submit must wait within its lock budget and then refuse as busy,
	leaving the job untouched; once the holder is gone the same submit succeeds."""

	def test_submit_refuses_while_another_transaction_holds_the_work_order(self):
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
		b = (
			frappe.get_doc(
				{
					"doctype": "Department Process",
					"process_name": f"{w.prefix} B",
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
		draft = frappe.get_doc(
			{
				"doctype": PT,
				"company": w.company,
				"manufacturer": w.manufacturer,
				"department": w.departments["MM"],
				"current_process": a,
				"next_process": b,
				"operations": [{"manufacturing_work_order": mwo}],
			}
		).insert()
		self.commit()

		race = self.new_race("W_ready", "H_parked", "H_release", "H_done")
		race.start("H", "_role_hold_work_order", mwo=mwo, waiter="W")
		race.hello("H")
		race.set("W_ready")
		race.await_event("H_parked")
		started = time.monotonic()
		with patch(f"{PT_MODULE}.is_pilot_enabled", return_value=True), patch.dict(
			frappe.conf, {"current_operation_lock_wait": _race.SHORT_LOCK_WAIT}
		):
			with self.assertRaises(WorkOrderBusyError):
				frappe.get_doc(PT, draft.name).submit()
			waited = time.monotonic() - started
			frappe.db.rollback()
			race.set("H_release")
			race.finish()
			self.fresh()
			self.assertEqual(
				frappe.db.get_value("Manufacturing Work Order", mwo, "current_process"),
				a,
			)
			self.assertGreaterEqual(waited, _race.SHORT_LOCK_WAIT - 0.5)
			self.assertLess(waited, _race.SHORT_LOCK_WAIT + _race.TIMEOUT_SLACK + 5)
			frappe.get_doc(
				PT, draft.name
			).submit()  # the holder is gone: the same submit goes through
			self.assertEqual(
				frappe.db.get_value("Manufacturing Work Order", mwo, "current_process"),
				b,
			)
		frappe.db.rollback()

	# Tables this world and this feature write (field holding the world's prefix).
	RESIDUE_TABLES = {
		"Company": "name",
		"Manufacturer": "name",
		"Department": "name",
		"Department Operation": "name",
		"Warehouse": "name",
		"Employee": "name",
		"Item": "name",
		"Manufacturing Work Order": "name",
		"Manufacturing Operation": "manufacturing_work_order",
		"MOP Log": "manufacturing_work_order",
		"Department Process": "department",
		"User Process Access": "department_process",
		"Process Transition Log": "manufacturing_work_order",
		"Process Transfer": "department",
	}

	def _purge_and_verify(self):
		"""The guard harness's purge, run twice, then a residue check of the tables above. The
		harness's own residue scan also LIKE-scans every text column of large shared tables
		(``tabDeleted Document``), which exceeds its 60 s statement limit on a full UAT copy."""
		frappe.db.rollback()
		_race._purge(self.prefix)
		fx.clear_caches(self.world)
		again = _race._purge(self.prefix)
		needle = f"%{self.prefix}%"
		left = {
			dt: count
			for dt, field in self.RESIDUE_TABLES.items()
			if (
				count := frappe.db.sql(
					f"SELECT COUNT(*) FROM `tab{dt}` WHERE `{field}` LIKE %s", (needle,)
				)[0][0]
			)
		}
		frappe.db.rollback()
		self.assertEqual(again, {}, "a second purge still found rows")
		self.assertEqual(left, {}, "rows naming the world survived its purge")

	def tearDown(self):
		# Rows of this feature's own DocTypes; the guard suite's purge knows only its own tables.
		if self.race:
			self.race.close()
		frappe.db.rollback()
		prefix = f"{self.world.prefix}%"
		frappe.db.delete(LOG, {"manufacturing_work_order": ["like", prefix]})
		for name in frappe.get_all(
			PT, filters={"department": ["like", prefix]}, pluck="name"
		):
			frappe.db.delete("Process Transfer Operation", {"parent": name})
			frappe.db.delete(PT, {"name": name})
		frappe.db.delete(
			"User Process Access", {"department_process": ["like", prefix]}
		)
		frappe.db.set_value(
			"Department Operation",
			{"name": ["like", prefix]},
			"department_process",
			None,
		)
		frappe.db.delete("Department Process", {"department": ["like", prefix]})
		frappe.db.commit()
		super().tearDown()


PT_MODULE = (
	"jewellery_erpnext.jewellery_erpnext.doctype.process_transfer.process_transfer"
)
