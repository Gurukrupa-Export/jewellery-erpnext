# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Day 4 of process control: the whole cell workflow, end to end, through the real controllers.

A cell here is any department with process control switched on; nothing names a real department.
Each cell is configured the way the requirements meeting (2026-11-10) describes Trishul: three
processes (Model Making, Diamond Setting, Final Polish), each with its own operations, one karigar
per operation and one data operator per process, plus a relief operator. That mapping is a
PROPOSED test configuration for GK to confirm in UAT, not a confirmed one.

* ``TestCellWorkflow``: Department IR into the cell with a destination process, Employee IR in that
  process, Process Transfer to the next process, Employee IR in the new process, then material
  movement checks; for two cells. At every step: the job's process on its active operation and work
  order, the pointer, the history, who may act, which worker is eligible, no duplicate operation, and
  that the Process Transfer itself changed no material, weight, MOP Log, stock or ledger row.
* ``TestCrossCellHandoff``: a Department IR from one cell's process to another cell's different
  process.
* ``TestActors``: a background job / service context grants nothing; a selected worker's
  eligibility never authorises the acting user.
* ``TestDeferredPosting``: the EOD sync's own Stock Entry builder posting earlier history after the
  job moved to another process, on a company with stock accounts (skips elsewhere and says why).
  The sync's in-memory context is set as ``sync_mop_logs`` sets it, so this part is SIMULATED: a
  full ``sync_mop_logs`` run needs Sales-Order-anchored reservations this world does not have.

Every class rolls back after each test.
"""

from contextlib import contextmanager
from unittest import skipUnless

import frappe
from frappe.utils import add_days, flt, nowdate

from jewellery_erpnext.jewellery_erpnext.doc_events import process_control as pc
from jewellery_erpnext.jewellery_erpnext.doc_events import process_enforcement as pe
from jewellery_erpnext.jewellery_erpnext.doc_events.process_enforcement import (
	ProcessEnforcementError,
)
from jewellery_erpnext.jewellery_erpnext.tests import _current_operation_fixtures as fx

# The module, not its classes: a TestCase bound here would also run its tests in this module.
from jewellery_erpnext.jewellery_erpnext.tests import test_process_enforcement as tpe
from jewellery_erpnext.jewellery_erpnext.tests.test_process_enforcement import (
	_entry,
	refused,
)

PT = "Process Transfer"
LOG = "Process Transition Log"
WEIGHTS = ("gross_wt", "net_wt", "diamond_wt", "gemstone_wt", "finding_wt", "other_wt")


@contextmanager
def acting_as(user):
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user("Administrator")


class _CellHarness(tpe._EnforcementHarness):
	"""Configure a cell from scratch (no Administrator grants: every step names its actor)."""

	def configure_cell(self, key):
		c = frappe._dict(key=key)
		c.mm = self.process("Model Making", key=key)
		c.ds = self.process("Diamond Setting", key=key)
		c.fp = self.process("Final Polish", key=key)
		# The department's own fixture operation is Model Filling; the rest are added.
		c.filing = self.w.operations[key]
		self.map_operation(c.filing, c.mm)
		c.assembly = self.operation(f"{key} Assembly", key=key)
		self.map_operation(c.assembly, c.mm)
		c.setting = self.operation(f"{key} Setting", key=key)
		self.map_operation(c.setting, c.ds)
		c.polish = self.operation(f"{key} Final Polish", key=key)
		self.map_operation(c.polish, c.fp)
		self.enable(key)
		# One karigar per operation.
		for label, operation in (
			("FIL", c.filing),
			("ASM", c.assembly),
			("SET", c.setting),
			("POL", c.polish),
		):
			self.employee(f"{key}-{label}", key, operation=operation)
		# One data operator per process; the relief operator covers Diamond Setting too.
		c.op_mm = self.operator(f"{key}-op-mm", c.mm)
		c.op_ds = self.operator(f"{key}-op-ds", c.ds)
		c.op_fp = self.operator(f"{key}-op-fp", c.fp)
		c.relief = self.operator(f"{key}-relief", c.fp, c.ds)
		return c

	def operator(self, key, *processes):
		user = self.user(
			key, roles=("System Manager", self.clerk_role(), "Process Transfer User")
		)
		for process in processes:
			self.grant(user, process)
		return user

	# -- documents -------------------------------------------------------------------------

	def dir_issue(self, mwo, current, to, destination):
		return fx.submit_ir(
			fx.insert_ir(
				fx.department_ir(
					self.w,
					"Issue",
					[mwo],
					current=current,
					to=to,
					destination_process=destination,
				)
			)
		)

	def dir_receive(self, mwo, current, previous, issue):
		return fx.submit_ir(
			fx.insert_ir(
				fx.department_ir(
					self.w,
					"Receive",
					[mwo],
					current=current,
					previous=previous,
					receive_against=issue.name,
				)
			)
		)

	def eir(self, typ, mwo, key, operation, employee, submit=True):
		doc = fx.employee_ir(self.w, typ, [mwo], department=key, employee=employee)
		doc.operation = operation
		doc = fx.insert_ir(doc)
		return fx.submit_ir(doc) if submit else doc

	def transfer_to(self, current, nxt, mwos, key):
		doc = self.transfer(current, nxt, mwos, key=key)
		doc.insert()
		doc.submit()
		return doc

	# -- reads -----------------------------------------------------------------------------

	def job(self, mwo):
		state = frappe.db.get_value(
			"Manufacturing Work Order",
			mwo,
			["manufacturing_operation", "current_process", "process_transition"],
			as_dict=True,
		)
		state.mop = frappe.db.get_value(
			"Manufacturing Operation",
			state.manufacturing_operation,
			["name", "department", "department_process", "status", "employee"],
			as_dict=True,
		)
		return state

	def history(self, mwo):
		return [
			(r.event, r.from_process, r.to_process)
			for r in frappe.get_all(
				LOG,
				filters={"manufacturing_work_order": mwo},
				fields=["event", "from_process", "to_process"],
				order_by="name asc",
			)
		]

	def accountability(self, mwo):
		"""Everything a process-only change must leave alone: each operation's weights, status,
		holder and department, every MOP Log row with its quantities, and the company's stock
		ledger, GL and reservation rows."""
		operations = frappe.get_all(
			"Manufacturing Operation",
			filters={"manufacturing_work_order": mwo},
			fields=["name", "status", "employee", "department", *WEIGHTS],
			order_by="name asc",
		)
		company = self.w.company
		return {
			"operations": [dict(r) for r in operations],
			"mop_logs": [
				dict(r)
				for r in frappe.get_all(
					"MOP Log",
					filters={"manufacturing_work_order": mwo},
					fields=[
						"name",
						"item_code",
						"qty_change",
						"qty_after_transaction",
						"is_synced",
						"is_cancelled",
					],
					order_by="name asc",
				)
			],
			"sle": frappe.db.count("Stock Ledger Entry", {"company": company}),
			"gl": frappe.db.count("GL Entry", {"company": company}),
			"sre": frappe.db.count(
				"Stock Reservation Entry", {"company": company, "docstatus": 1}
			),
			"stock_entries": frappe.db.count("Stock Entry", {"company": company}),
		}


# ------------------------------------------------------------------------------------------------
# the whole workflow, per cell
# ------------------------------------------------------------------------------------------------


class TestCellWorkflow(_CellHarness):
	def run_cell(self, key, feeder):
		c = self.configure_cell(key)
		mwo, _first = fx.make_work_order(self.w, feeder, seed=True)

		# 1. The feeder department sends the job in, naming its first process. The feeder has no
		#    process control, so only Day 2's destination rule applies to the sender.
		issue = self.dir_issue(mwo, feeder, key, destination=c.mm)
		st = self.job(mwo)
		self.assertEqual(
			(st.mop.department, st.mop.department_process, st.current_process),
			(self.w.departments[key], c.mm, c.mm),
		)
		self.assertEqual(self.history(mwo), [("Department IR Issue", None, c.mm)])
		# Receiving into the cell needs some process of it: a stranger cannot, an operator can.
		with acting_as(self.operator(f"{key}-stranger")):
			with refused(self, frappe.PermissionError, "cannot receive"):
				self.dir_receive(mwo, key, feeder, issue)
		with acting_as(c.op_mm):
			self.dir_receive(mwo, key, feeder, issue)
		self.assertEqual(self.job(mwo).mop.department_process, c.mm)

		# 2. Model Making work: only its operations, only to the karigar of that exact operation,
		#    only by a user with Model Making access.
		# A Diamond Setting operator holds the Setting operation's process, but the job is in
		# Model Making: refused for the job's process, not for want of access.
		with acting_as(c.op_ds):
			with refused(self, ProcessEnforcementError, f"is in process {c.mm}"):
				self.eir("Issue", mwo, key, c.setting, f"{key}-SET")
		with acting_as(c.op_mm):
			with refused(self, ProcessEnforcementError, "Being in the same process"):
				self.eir("Issue", mwo, key, c.assembly, f"{key}-FIL")
		with acting_as(c.op_ds):
			with refused(self, frappe.PermissionError, f"no access to process {c.mm}"):
				self.eir("Issue", mwo, key, c.filing, f"{key}-FIL")
		before_issue = self.job(mwo).mop.name
		with acting_as(c.op_mm):
			self.eir("Issue", mwo, key, c.filing, f"{key}-FIL")
			self.eir("Receive", mwo, key, c.filing, f"{key}-FIL")
		st = self.job(mwo)
		self.assertNotEqual(st.mop.name, before_issue)  # the successor
		self.assertEqual(
			(st.mop.department_process, st.current_process, st.mop.status),
			(c.mm, c.mm, "Not Started"),
		)

		# 3. Process Transfer Model Making -> Diamond Setting, by the Model Making operator. It
		#    changes the process and nothing physical.
		before = self.accountability(mwo)
		pointer_before = self.job(mwo).mop.name
		with acting_as(c.op_ds):  # hand-off needs the current process
			with refused(self, frappe.PermissionError, f"no access to process {c.mm}"):
				self.transfer_to(c.mm, c.ds, [mwo], key)
		with acting_as(c.op_mm):
			pt = self.transfer_to(c.mm, c.ds, [mwo], key)
		self.assertEqual(self.accountability(mwo), before)
		st = self.job(mwo)
		self.assertEqual(
			(st.mop.name, st.mop.department_process, st.current_process),
			(pointer_before, c.ds, c.ds),
		)
		self.assertEqual(
			self.history(mwo),
			[("Department IR Issue", None, c.mm), ("Process Transfer", c.mm, c.ds)],
		)
		self.assertEqual(
			frappe.db.get_value(LOG, st.process_transition, "voucher_no"), pt.name
		)

		# 4. Diamond Setting work: Model Making operations are refused now; the Setting karigar
		#    works it, and the relief operator (Final Polish by default, Diamond Setting as cover)
		#    may record it.
		with acting_as(c.op_mm):
			with refused(self, ProcessEnforcementError, f"is in process {c.ds}"):
				self.eir("Issue", mwo, key, c.filing, f"{key}-FIL")
			with refused(self, frappe.PermissionError, f"no access to process {c.ds}"):
				self.eir("Issue", mwo, key, c.setting, f"{key}-SET")
		with acting_as(c.relief):
			self.eir("Issue", mwo, key, c.setting, f"{key}-SET")
			self.eir("Receive", mwo, key, c.setting, f"{key}-SET")
		st = self.job(mwo)
		self.assertEqual((st.mop.department_process, st.current_process), (c.ds, c.ds))

		# 5. Material against the job: only in Diamond Setting, only by its operators.
		with acting_as(c.op_ds):
			se = _entry(self.w, mwo, st.mop.name)
			pe.check_stock_entry(se, "before_submit")
			self.assertEqual(se.get(pe.STOCK_ENTRY_PROCESS_FIELD), c.ds)
			pe.check_material_request_transfer(self._request(mwo), st.mop.name)
		with acting_as(c.op_mm):
			with refused(self, frappe.PermissionError, f"no access to process {c.ds}"):
				pe.check_stock_entry(_entry(self.w, mwo, st.mop.name), "before_submit")
			with refused(self, frappe.PermissionError, f"no access to process {c.ds}"):
				pe.check_material_request_transfer(self._request(mwo), st.mop.name)

		# 6. No duplicate operation anywhere: feeder, minted, and one successor per Receive; one
		#    open operation, the pointer.
		ops = fx.operations(mwo)
		self.assertEqual(len(ops), 4, [o.name for o in ops])
		self.assertEqual(fx.open_operations(mwo), [st.mop.name])
		self.assertEqual(frappe.db.count(PT, {"docstatus": 1, "name": pt.name}), 1)

	def _request(self, mwo):
		doc = frappe.new_doc("Material Request")
		doc.manufacturing_order = frappe.db.get_value(
			"Manufacturing Work Order", mwo, "manufacturing_order"
		)
		doc.custom_manufacturing_work_order = mwo
		return doc

	def test_cell_t_runs_the_whole_flow(self):
		self.run_cell("MM", feeder="DS")

	def test_cell_s_runs_the_whole_flow(self):
		self.run_cell("DS", feeder="MM")


# ------------------------------------------------------------------------------------------------
# cell to cell, different processes
# ------------------------------------------------------------------------------------------------


class TestCrossCellHandoff(_CellHarness):
	def test_a_job_moves_from_one_cells_process_to_another_cells_different_process(
		self,
	):
		t = self.configure_cell("MM")
		s = self.configure_cell("DS")
		mwo, source = fx.make_work_order(self.w, "MM", seed=True)
		self.assign(mwo, source, t.ds)
		# Sending needs the source process (Cell T Diamond Setting), not the destination's.
		with acting_as(s.op_fp):
			with refused(self, frappe.PermissionError, f"no access to process {t.ds}"):
				self.dir_issue(mwo, "MM", "DS", destination=s.fp)
		with acting_as(t.op_ds):
			issue = self.dir_issue(mwo, "MM", "DS", destination=s.fp)
		st = self.job(mwo)
		self.assertEqual(self.mop_process(source), t.ds)  # the source keeps its history
		self.assertEqual(
			(st.mop.department, st.mop.department_process, st.current_process),
			(self.w.departments["DS"], s.fp, s.fp),
		)
		with acting_as(s.op_mm):  # any process of the receiving cell
			self.dir_receive(mwo, "DS", "MM", issue)
		self.assertEqual(self.history(mwo)[-1], ("Department IR Issue", t.ds, s.fp))
		self.assertEqual(
			frappe.db.count(
				"Manufacturing Operation", {"department_issue_id": issue.name}
			),
			1,
		)


# ------------------------------------------------------------------------------------------------
# who acts
# ------------------------------------------------------------------------------------------------


class TestActors(_CellHarness):
	def test_a_background_job_context_grants_nothing(self):
		c = self.configure_cell("MM")
		mwo, mop = fx.make_work_order(self.w, "MM", seed=False)
		self.assign(mwo, mop, c.mm)
		# Administrator with no grant, inside a job context (frappe.local.job): still refused.
		frappe.local.job = frappe._dict(name="test-process-control-job")
		self.addCleanup(lambda: setattr(frappe.local, "job", None))
		with refused(self, frappe.PermissionError, f"no access to process {c.mm}"):
			pe.check_stock_entry(_entry(self.w, mwo, mop), "before_submit")

	def test_the_selected_workers_eligibility_never_authorises_the_acting_user(self):
		c = self.configure_cell("MM")
		mwo, mop = fx.make_work_order(self.w, "MM", seed=False)
		self.assign(mwo, mop, c.mm)
		# The Model Filling karigar's own login, with no User Process Access row.
		karigar_user = self.user(
			"karigar-login", roles=("System Manager", self.clerk_role())
		)
		frappe.db.set_value(
			"Employee", self.w.employees["MM-FIL"], "user_id", karigar_user
		)
		for actor in (karigar_user, c.op_ds):
			with self.subTest(actor), acting_as(actor):
				with refused(
					self, frappe.PermissionError, f"no access to process {c.mm}"
				):
					self.eir("Issue", mwo, "MM", c.filing, "MM-FIL")
		self.assertEqual(fx.operation(mop).status, "Not Started")


# ------------------------------------------------------------------------------------------------
# deferred (EOD) posting of earlier history, through the sync's own builder
# ------------------------------------------------------------------------------------------------


class TestDeferredPosting(tpe.TestStockEntrySubmission):
	"""Stock-capable world of ``TestStockEntrySubmission`` (cell process A on the job, Administrator
	granted A and B). Its own four tests are not repeated here."""

	def setUp(self):
		super().setUp()
		# Earlier history on the job's operation, not yet posted by EOD.
		frappe.get_doc(
			{
				"doctype": "MOP Log",
				"name": f"{self.prefix}-LOG",
				"item_code": self.item,
				"qty_change": 1,
				"qty_after_transaction": 1,
				"qty_after_transaction_batch_based": 1,
				"to_warehouse": self.wo,
				"voucher_type": "Manufacturing Operation",
				"voucher_no": self.mop,
				"manufacturing_operation": self.mop,
				"manufacturing_work_order": self.mwo,
				"is_synced": 0,
				"is_cancelled": 0,
				"flow_index": 0,
			}
		).db_insert()
		# The job then moved on: a newer operation in process B is its pointer.
		self.newer = self.insert(
			"Manufacturing Operation",
			name=f"{self.prefix}-MOP2",
			company=self.company,
			manufacturer=self.manufacturer,
			department=self.department,
			manufacturing_work_order=self.mwo,
			previous_mop=self.mop,
			status="Not Started",
			type="Manufacturing Work Order",
			qty=1,
			department_process=self.process_b,
		)
		frappe.db.set_value("Manufacturing Operation", self.mop, "status", "Finished")
		frappe.db.set_value(
			"Manufacturing Work Order",
			self.mwo,
			{"manufacturing_operation": self.newer, "current_process": self.process_b},
		)
		self.log = self.insert(
			"MOP EOD Sync Log",
			name=f"{self.prefix}-EODLOG",
			status="Running",
			trigger_type="Manual",
			posting_date=nowdate(),
		)

	def rows(self, qty=1):
		return [
			{
				"item_code": self.item,
				"qty": qty,
				"s_warehouse": self.rm,
				"t_warehouse": self.wo,
				"uom": "Gram",
				"conversion_factor": 1,
				"manufacturing_operation": self.mop,
				"custom_manufacturing_work_order": self.mwo,
			}
		]

	def eod_draft(self, qty=1):
		from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings import (
			mop_eod_sync,
		)

		with self.in_sync():
			return mop_eod_sync._save_draft_eod_se(
				self.company, None, None, self.rows(qty), sync_log_name=self.log
			)

	@contextmanager
	def in_sync(self):
		"""What ``sync_mop_logs`` sets for its run (server memory, not a request field)."""
		frappe.flags.in_eod_mop_sync = True
		try:
			yield
		finally:
			frappe.flags.in_eod_mop_sync = False

	def test_the_sync_posts_history_after_the_job_moved_to_another_process(self):
		name = self.eod_draft()
		with self.in_sync():
			frappe.get_doc("Stock Entry", name).submit()
		self.assertEqual(self.ledger(name), [[self.rm, -1.0], [self.wo, 1.0]])
		# Historical: not stamped with today's process, and the job's state is untouched.
		self.assertFalse(
			frappe.db.get_value("Stock Entry", name, pe.STOCK_ENTRY_PROCESS_FIELD)
		)
		self.assertEqual(
			frappe.db.get_value(
				"Manufacturing Work Order", self.mwo, "current_process"
			),
			self.process_b,
		)

	def test_the_same_entry_made_by_a_user_is_a_new_movement_and_is_refused(self):
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Transfer to Department",
				"company": self.company,
				"auto_created": 1,
				"custom_is_eod_sync_stock_entry": 1,
				"custom_eod_sync_log": self.log,
				"custom_eod_sync_source": "MOP EOD Sync",
				"items": self.rows(),
			}
		)
		with self.assertRaisesRegex(
			ProcessEnforcementError, "is not the current operation"
		):
			se.insert()
		self.assertFalse(
			frappe.db.exists("Stock Entry", {"custom_eod_sync_log": self.log})
		)

	def test_a_saved_draft_submitted_later_may_carry_only_what_the_log_recorded(self):
		name = self.eod_draft()
		item = frappe.get_doc(
			{
				"doctype": "MOP EOD Sync Log Item",
				"parent": self.log,
				"parenttype": "MOP EOD Sync Log",
				"parentfield": "items",
				"manufacturing_work_order": self.mwo,
				"manufacturing_operation": self.mop,
				"item_code": self.item,
				"qty": 1,
				"draft_stock_entry": name,
			}
		)
		item.db_insert()
		draft = frappe.get_doc("Stock Entry", name)
		draft.items[0].qty = 2  # more than the sync recorded
		with self.assertRaisesRegex(
			ProcessEnforcementError, "is not the current operation"
		):
			draft.submit()
		self.assertNothingPosted(name)
		draft = frappe.get_doc("Stock Entry", name)  # as the sync saved it
		draft.submit()
		self.assertEqual(self.ledger(name), [[self.rm, -1.0], [self.wo, 1.0]])

	def test_backdating_a_new_movement_does_not_make_it_history(self):
		se = self.transfer(self.wo)
		se.set_posting_time = 1
		se.posting_date = add_days(nowdate(), -3)
		with self.assertRaisesRegex(
			ProcessEnforcementError, "is not the current operation"
		):
			se.insert()

	def test_a_posted_sync_entry_cancels_and_reverses_its_ledger(self):
		name = self.eod_draft()
		with self.in_sync():
			frappe.get_doc("Stock Entry", name).submit()
		frappe.get_doc("Stock Entry", name).cancel()
		self.assertEqual(self.ledger(name), [])
		self.assertEqual(
			flt(
				frappe.db.get_value(
					"Stock Ledger Entry",
					{"voucher_no": name, "is_cancelled": 1, "warehouse": self.wo},
					"actual_qty",
				)
			),
			1.0,
		)


# Only this class's own tests: the inherited ones belong to test_process_enforcement.
for _name in [n for n in dir(tpe.TestStockEntrySubmission) if n.startswith("test_")]:
	setattr(TestDeferredPosting, _name, None)


# ------------------------------------------------------------------------------------------------
# genuine two-connection races between process control and its competitors
# ------------------------------------------------------------------------------------------------

try:
	from jewellery_erpnext.jewellery_erpnext.tests import (
		test_current_operation_race as _race,
	)
except ImportError:  # pragma: no cover - branch without the guard's race suite
	_race = None

RACES_ENABLED = bool(_race and _race.RACES_ENABLED)
BUSY = "WorkOrderBusyError"


@skipUnless(
	RACES_ENABLED, "set CURRENT_OPERATION_RACE_TESTS=1 to run the two-connection races"
)
class TestProcessRaces(_race._RaceCase if _race else object):  # type: ignore[misc]
	"""Two real database connections. The holder takes its MOP-then-MWO lock block and parks; the
	waiter runs into it. The parent proves the waiter is in a real InnoDB row-lock wait, then lets
	the holder commit. Expected every time: the waiter is refused as busy within its budget, the
	holder's change is whole, nothing is half-written, and a retry is judged by the new state."""

	def setUp(self):
		super().setUp()
		w = self.world
		self.a, self.b, self.c = (
			frappe.get_doc(
				{
					"doctype": "Department Process",
					"process_name": f"{w.prefix} {label}",
					"department": w.departments["MM"],
					"manufacturer": w.manufacturer,
				}
			)
			.insert()
			.name
			for label in ("A", "B", "C")
		)
		op = frappe.get_doc("Department Operation", w.operations["MM"])
		op.department_process = self.a
		op.save()
		for label, process in (("B", self.b), ("C", self.c)):
			extra = fx._insert(
				w,
				"Department Operation",
				{
					"name": f"{w.prefix} Op {label}",
					"operation": f"{w.prefix} Op {label}",
					"department": w.departments["MM"],
					"company": w.company,
					"manufacturer": w.manufacturer,
				},
			)
			frappe.db.set_value(
				"Department Operation", extra, "department_process", process
			)
		frappe.db.set_value("Department", w.departments["MM"], pc.DEPARTMENT_FLAG, 1)
		if frappe.get_meta("Employee").has_field("custom_operation"):
			frappe.db.set_value(
				"Employee", w.employees["MM1"], "custom_operation", w.operations["MM"]
			)
		for process in (self.a, self.b, self.c):
			frappe.get_doc(
				{
					"doctype": "User Process Access",
					"user": "Administrator",
					"department_process": process,
				}
			).insert()
		self.mwo, self.mop = fx.make_work_order(w, "MM", seed=False)
		frappe.db.set_value(
			"Manufacturing Operation", self.mop, "department_process", self.a
		)
		frappe.db.set_value(
			"Manufacturing Work Order", self.mwo, "current_process", self.a
		)
		self.commit()

	def tearDown(self):
		# Feature rows first (the guard's purge knows only its own tables), then the harness's
		# purge and residue check, called explicitly (no zero-argument super() across classes).
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
		_race._RaceCase.tearDown(self)

	if _race:
		from jewellery_erpnext.jewellery_erpnext.tests.test_process_transfer_workflow import (
			TestProcessTransferRace as _Day2,
		)

		RESIDUE_TABLES = _Day2.RESIDUE_TABLES
		_purge_and_verify = _Day2._purge_and_verify

	# -- helpers ---------------------------------------------------------------------------

	@contextmanager
	def pilot(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.process_transfer import (
			process_transfer as pt_mod,
		)

		saved = [(m, m.is_pilot_enabled) for m in (pc, pe, pt_mod)]
		for m, _fn in saved:
			m.is_pilot_enabled = lambda: True
		try:
			yield
		finally:
			for m, fn in saved:
				m.is_pilot_enabled = fn

	def draft_transfer(self, nxt):
		w = self.world
		return (
			frappe.get_doc(
				{
					"doctype": PT,
					"company": w.company,
					"manufacturer": w.manufacturer,
					"department": w.departments["MM"],
					"current_process": self.a,
					"next_process": nxt,
					"operations": [{"manufacturing_work_order": self.mwo}],
				}
			)
			.insert()
			.name
		)

	def eir_issue(self):
		return {
			"op": "insert",
			"kind": "eir_issue",
			"rows": [self.mwo],
			"name": self.doc_name("EIR-RACE"),
			"label": "Employee IR Issue",
		}

	def race_pair(self, holder, holder_action, waiter_action):
		"""Run the holder (``holder``: role function name) and the waiter; prove the lock wait;
		return both reports."""
		race = self.new_race(
			"H_ready",
			"H_parked",
			"H_release",
			"H_done",
			"W_ready",
			"W_started",
			"W_done",
		)
		short = {"current_operation_lock_wait": _race.SHORT_LOCK_WAIT}
		race.start(
			"H", holder, world=self.world, action=holder_action, me="H", other="W"
		)
		race.start(
			"W",
			"_role_process_second",
			world=self.world,
			action=waiter_action,
			me="W",
			other="H",
			conf=short,
		)
		race.hello("H")
		race.hello("W")
		race.await_event("H_parked")
		race.await_event("W_started")
		proof = race.prove_lock_wait("W", holder="H")
		race.await_event("W_done")
		race.set("H_release")
		reports = race.finish()
		self.fresh()
		holder_try = reports["H"]["attempts"][-1]
		waiter_try = reports["W"]["attempts"][-1]
		self.assertTrue(holder_try["ok"], holder_try.get("error"))
		self.assertFalse(waiter_try["ok"], "the waiter got through a held job")
		self.assertIn(
			BUSY, waiter_try["error"]["type"] + waiter_try["error"]["message"]
		)
		self.assertLess(
			waiter_try["elapsed"], _race.SHORT_LOCK_WAIT + _race.TIMEOUT_SLACK + 5
		)
		return proof, holder_try, waiter_try

	def state(self):
		return frappe.db.get_value(
			"Manufacturing Work Order",
			self.mwo,
			["current_process", "process_transition", "manufacturing_operation"],
			as_dict=True,
		)

	def transfers_logged(self):
		return frappe.db.count(LOG, {"manufacturing_work_order": self.mwo})

	# -- races -----------------------------------------------------------------------------

	def test_process_transfer_against_process_transfer(self):
		first, second = self.draft_transfer(self.b), self.draft_transfer(self.c)
		self.commit()
		self.race_pair(
			"_role_park_after_process_locks",
			{"op": "submit", "doctype": PT, "name": first, "label": "submit A->B"},
			{"op": "submit", "doctype": PT, "name": second, "label": "submit A->C"},
		)
		self.assertEqual(self.state().current_process, self.b)
		self.assertEqual(self.transfers_logged(), 1)  # one change, never two
		with self.pilot(), self.assertRaises(frappe.ValidationError):
			frappe.get_doc(PT, second).submit()  # the job is no longer in A
		frappe.db.rollback()
		self.assertEqual(self.state().current_process, self.b)

	def test_employee_issue_holding_the_job_against_process_transfer(self):
		transfer = self.draft_transfer(self.b)
		self.commit()
		race = self.new_race(
			"H_ready",
			"H_parked",
			"H_release",
			"H_done",
			"W_ready",
			"W_started",
			"W_done",
		)
		race.start(
			"H",
			"_role_racer",
			world=self.world,
			action=self.eir_issue(),
			me="H",
			order="first",
			other="W",
		)
		race.start(
			"W",
			"_role_process_second",
			world=self.world,
			action={
				"op": "submit",
				"doctype": PT,
				"name": transfer,
				"label": "submit A->B",
			},
			me="W",
			other="H",
			conf={"current_operation_lock_wait": _race.SHORT_LOCK_WAIT},
		)
		race.hello("H")
		race.hello("W")
		race.await_event("H_parked")
		race.await_event("W_started")
		race.prove_lock_wait("W", holder="H")
		race.await_event("W_done")
		race.set("H_release")
		reports = race.finish()
		self.fresh()
		self.assertTrue(reports["H"]["attempts"][-1]["ok"])
		waiter = reports["W"]["attempts"][-1]
		self.assertFalse(waiter["ok"])
		self.assertIn(BUSY, waiter["error"]["type"] + waiter["error"]["message"])
		self.assertEqual(self.state().current_process, self.a)
		self.assertEqual(self.transfers_logged(), 0)
		# The Issue draft now holds the job: the retry is refused by the new state.
		with self.pilot(), self.assertRaises(pc.ProcessTransitionConflictError):
			frappe.get_doc(PT, transfer).submit()
		frappe.db.rollback()

	def test_process_transfer_holding_the_job_against_employee_issue(self):
		transfer = self.draft_transfer(self.b)
		self.commit()
		self.race_pair(
			"_role_park_after_process_locks",
			{"op": "submit", "doctype": PT, "name": transfer, "label": "submit A->B"},
			self.eir_issue(),
		)
		self.assertEqual(self.state().current_process, self.b)
		self.assertFalse(frappe.db.exists("Employee IR", self.doc_name("EIR-RACE")))
		# Retried now, an Issue of A's operation is refused: the job is in B.
		retry = fx.employee_ir(
			self.world, "Issue", [self.mwo], department="MM", employee="MM1"
		)
		with self.pilot(), self.assertRaises(ProcessEnforcementError):
			fx.insert_ir(retry)
		frappe.db.rollback()

	def test_process_transfer_holding_the_job_against_a_stock_entry(self):
		transfer = self.draft_transfer(self.b)
		self.commit()
		self.race_pair(
			"_role_park_after_process_locks",
			{"op": "submit", "doctype": PT, "name": transfer, "label": "submit A->B"},
			{
				"op": "check_stock_entry",
				"mwo": self.mwo,
				"mop": self.mop,
				"label": "Stock Entry check",
			},
		)
		# Retried now, the movement is judged in B and recorded as B.
		with self.pilot():
			se = _entry(self.world, self.mwo, self.mop)
			pe.check_stock_entry(se, "before_submit")
			self.assertEqual(se.get(pe.STOCK_ENTRY_PROCESS_FIELD), self.b)
		frappe.db.rollback()

	def test_process_transfer_cancel_holding_the_job_against_employee_issue(self):
		transfer = self.draft_transfer(self.b)
		with self.pilot():
			frappe.get_doc(PT, transfer).submit()
		self.commit()
		self.race_pair(
			"_role_park_after_process_locks",
			{"op": "cancel", "doctype": PT, "name": transfer, "label": "cancel A->B"},
			self.eir_issue(),
		)
		self.assertEqual(
			self.state().current_process, self.a
		)  # the cancel went through whole
		self.assertEqual(
			frappe.db.count(
				LOG, {"manufacturing_work_order": self.mwo, "event": "Cancellation"}
			),
			1,
		)
		# Retried now, the Issue of A's operation is accepted: the job is back in A.
		with self.pilot():
			fx.insert_ir(
				fx.employee_ir(
					self.world, "Issue", [self.mwo], department="MM", employee="MM1"
				)
			)
		frappe.db.rollback()
