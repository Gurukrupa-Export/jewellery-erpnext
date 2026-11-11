# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Process Transfer: move work orders from one process to the next inside one cell department.

Model: the header carries the transition (Current Process to Next Process). Every row must be a
work order whose active operation is currently in the header's Current Process. A row in another
process is refused, never rewritten.

Initial Process Assignment (``initial_assignment``, GK-DEC-0004): a work order that was already in
the department when process control was switched on has no process. An initial assignment has no
Current Process, and every row must be such a work order. Nothing is back-filled or guessed.

Lifecycle:

* **Draft save** reads every row's active operation, current process and latest process change
  (``process_transition_snapshot``) from the database in one batch, and checks scope and
  readiness. Client values are overwritten.
* **Submit** takes the same MOP-then-MWO row locks as Employee IR / Department IR (RULE D: before
  Frappe locks this document's own rows), re-reads the locked rows, and refuses when a work order
  moved to another operation or had any process change since the draft was saved, even one that
  ended in the same process. Then authority (GK-DEC-0002), readiness (GK-DEC-0003) and the pilot
  gate, and :func:`apply_transition` changes every row in this one transaction.
* **Cancel** (GK-DEC-0007) is allowed only while this transfer is each work order's latest process
  change, the job is still on the same operation and at rest, and no Stock Entry has moved
  material for that operation since. It writes an inverse change; nothing is deleted.
* **Amend** copies the document; the draft save re-reads everything.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.functions import Max
from frappe.utils import cint, cstr

from jewellery_erpnext.jewellery_erpnext.doc_events.process_control import (
	TRANSFER_EVENTS,
	ActiveOperationError,
	ProcessControlError,
	ProcessTransitionConflictError,
	apply_transition,
	bold,
	effective_transitions,
	get_active_operations,
	get_process,
	is_pilot_enabled,
	is_process_control_enabled,
	lock_jobs,
	open_issue_drafts,
	outstanding_issue_drafts,
	read_locked_state,
	readiness_problem,
	require_process_access,
)


class ProcessTransferNotAvailableError(ProcessControlError):
	"""Process control is not active on this site (the pilot switch is off)."""


class ProcessTransfer(Document):
	# -- locking (RULE D: before Frappe locks this document's own rows) ---------------------------

	def before_insert(self):
		if (
			cint(self.docstatus) == 1
		):  # inserted and submitted in one call (API, import)
			self._lock_jobs()

	def check_if_latest(self):
		stored = frappe.db.get_value(self.doctype, self.name, "docstatus")
		if stored is not None and cint(stored) != cint(
			self.docstatus
		):  # submit or cancel
			self._lock_jobs()
		super().check_if_latest()

	def _lock_jobs(self):
		mwos = [
			cstr(r.manufacturing_work_order).strip()
			for r in self.get("operations") or ()
		]
		if self.is_new():
			# Inserted already submitted (API, import): the rows were never read, so lock the
			# operations the work orders point at now, not whatever the client sent; the locked
			# re-read in validate confirms them. Operations first, then work orders (RULE E).
			mops = [
				m.name
				for m in get_active_operations(mwos).values()
				if isinstance(m, dict)
			]
		else:
			mops = [
				r.manufacturing_operation
				for r in self.get("operations") or ()
				if r.manufacturing_operation
			]
		lock_jobs(self, mops, mwos)

	# -- validation ----------------------------------------------------------------------------

	def validate(self):
		self.validate_department()
		self.validate_processes()
		submitting = cint(self.docstatus) == 1
		if submitting and not is_pilot_enabled():
			frappe.throw(
				_(
					"Process Transfer cannot be submitted yet: process control is not active on this "
					"site (MOP Settings › Enable Process Control). Keep this entry as a draft."
				),
				ProcessTransferNotAvailableError,
			)
		self.set_and_validate_rows(locked=submitting)
		if submitting:
			require_process_access(
				self.next_process if self.initial_assignment else self.current_process
			)

	def validate_department(self):
		if not self.department:
			frappe.throw(_("Select the Department."), ProcessControlError)
		department = frappe.db.get_value(
			"Department", self.department, ["company", "manufacturer"], as_dict=True
		)
		if not department:
			frappe.throw(
				_("Department {0} does not exist.").format(bold(self.department))
			)
		if not is_process_control_enabled(self.department):
			frappe.throw(
				_("Department {0} does not use process control.").format(
					bold(self.department)
				),
				ProcessControlError,
			)
		if department.company != self.company:
			frappe.throw(
				_("Department {0} belongs to Company {1}, not {2}.").format(
					bold(self.department), bold(department.company), bold(self.company)
				),
				ProcessControlError,
			)
		if department.manufacturer and department.manufacturer != self.manufacturer:
			frappe.throw(
				_("Department {0} belongs to Manufacturer {1}, not {2}.").format(
					bold(self.department),
					bold(department.manufacturer),
					bold(self.manufacturer),
				),
				ProcessControlError,
			)

	def validate_processes(self):
		if self.initial_assignment:
			if self.current_process:
				frappe.throw(
					_(
						"An Initial Process Assignment is for work orders that have no process yet, so "
						"Current Process must be empty."
					),
					ProcessControlError,
				)
			fieldnames = ("next_process",)
		else:
			if not self.current_process:
				frappe.throw(
					_(
						"Select the Current Process. For work orders that have no process yet, tick "
						"Initial Process Assignment."
					),
					ProcessControlError,
				)
			fieldnames = ("current_process", "next_process")
		for fieldname in fieldnames:
			label = _(self.meta.get_label(fieldname))
			process = get_process(self.get(fieldname))
			if not process:
				frappe.throw(
					_("Select the {0}.").format(label)
					if not self.get(fieldname)
					else _("{0} {1} does not exist.").format(
						label, bold(self.get(fieldname))
					),
					ProcessControlError,
				)
			if process.department != self.department:
				frappe.throw(
					_("{0} {1} belongs to Department {2}, not {3}.").format(
						label,
						bold(process.name),
						bold(process.department),
						bold(self.department),
					),
					ProcessControlError,
				)
			if process.disabled:
				frappe.throw(
					_("{0} {1} is disabled.").format(label, bold(process.name)),
					ProcessControlError,
				)
		if self.current_process == self.next_process:
			frappe.throw(
				_(
					"Current Process and Next Process are both {0}. Choose a different Next Process."
				).format(bold(self.current_process)),
				ProcessControlError,
			)

	def set_and_validate_rows(self, *, locked: bool):
		if not self.get("operations"):
			frappe.throw(
				_("Add at least one Manufacturing Work Order."), ProcessControlError
			)
		seen = {}
		for row in self.operations:
			mwo = cstr(row.manufacturing_work_order).strip()
			if not mwo:
				frappe.throw(
					_("Row #{0}: select a Manufacturing Work Order.").format(row.idx),
					ProcessControlError,
				)
			if mwo in seen:
				frappe.throw(
					_(
						"Row #{0}: Manufacturing Work Order {1} is already in row #{2}."
					).format(row.idx, bold(mwo), seen[mwo]),
					ProcessControlError,
				)
			seen[mwo] = row.idx

		if locked and not self.is_new():
			# Submit of a saved draft: the rows must still describe the jobs exactly as they were
			# when the draft was saved (same operation, no process change since).
			state = read_locked_state(
				{
					cstr(r.manufacturing_work_order).strip(): r.manufacturing_operation
					for r in self.operations
				}
			)
			for row in self.operations:
				self._refuse_if_changed_since_save(
					row, state[cstr(row.manufacturing_work_order).strip()]
				)
		elif locked:
			pointers = get_active_operations(seen)
			state = {
				**pointers,
				**read_locked_state(
					{m: p.name for m, p in pointers.items() if isinstance(p, dict)}
				),
			}
		else:
			state = get_active_operations(seen)

		drafts = open_issue_drafts(seen, locked=locked)
		for row in self.operations:
			mwo = cstr(row.manufacturing_work_order).strip()
			self.set_row_from_active_operation(row, state[mwo])
			if mwo in drafts:
				frappe.throw(
					_(
						"Row #{0}: Manufacturing Work Order {1} is on draft Employee IR Issue {2}. Submit or delete it first."
					).format(row.idx, bold(mwo), bold(drafts[mwo])),
					ProcessTransitionConflictError,
				)

	def _refuse_if_changed_since_save(self, row, current):
		changed = None
		if not isinstance(current, dict):
			changed = current
		elif cstr(current.work_order.process_transition) != cstr(
			row.process_transition_snapshot
		):
			changed = _(
				"Manufacturing Work Order {0} changed process since this draft was saved."
			).format(bold(row.manufacturing_work_order))
		if changed:
			frappe.throw(
				_(
					"Row #{0}: {1} Save the draft again to load its current state, then review and submit."
				).format(row.idx, changed),
				ProcessTransitionConflictError,
			)

	def set_row_from_active_operation(self, row, mop):
		if not isinstance(mop, dict):
			frappe.throw(_("Row #{0}: {1}").format(row.idx, mop), ActiveOperationError)
		mwo = mop.work_order
		prefix = _("Row #{0}: Manufacturing Work Order {1}").format(
			row.idx, bold(mwo.name)
		)
		for fieldname, label in (
			("company", _("Company")),
			("manufacturer", _("Manufacturer")),
		):
			if mwo.get(fieldname) != self.get(fieldname):
				frappe.throw(
					_("{0} has {1} {2}, but this Process Transfer is for {3}.").format(
						prefix,
						label,
						bold(mwo.get(fieldname)),
						bold(self.get(fieldname)),
					),
					ProcessControlError,
				)
		if mop.department != self.department:
			frappe.throw(
				_("{0} is in Department {1}, not {2}.").format(
					prefix, bold(mop.department), bold(self.department)
				),
				ProcessControlError,
			)
		# Authoritative values from the database, never from the client.
		row.manufacturing_operation = mop.name
		row.current_process = mop.department_process
		row.next_process = self.next_process
		row.process_transition_snapshot = mwo.process_transition
		problem = readiness_problem(mop, mwo)
		if problem:
			frappe.throw(
				_("{0} {1}, so it cannot change process now.").format(prefix, problem),
				ProcessTransitionConflictError,
			)
		if self.initial_assignment:
			if mop.department_process:
				frappe.throw(
					_(
						"{0} is already in process {1}. Use a Process Transfer without Initial Process Assignment."
					).format(prefix, bold(mop.department_process)),
					ProcessControlError,
				)
			return
		if not mop.department_process:
			frappe.throw(
				_(
					"{0} has no current process yet. Give it its first process with an Initial Process Assignment."
				).format(prefix),
				ProcessControlError,
			)
		if mop.department_process != self.current_process:
			frappe.throw(
				_("{0} is in process {1}, not the Current Process {2}.").format(
					prefix, bold(mop.department_process), bold(self.current_process)
				),
				ProcessControlError,
			)

	# -- submit ----------------------------------------------------------------------------------

	def on_submit(self):
		apply_transition(
			[
				frappe._dict(
					mwo=row.manufacturing_work_order,
					mop=row.manufacturing_operation,
					department=self.department,
					from_process=row.current_process,
					to_process=self.next_process,
					previous_transition=row.process_transition_snapshot,
				)
				for row in self.operations
			],
			event="Initial Assignment"
			if self.initial_assignment
			else "Process Transfer",
			voucher_type=self.doctype,
			voucher_no=self.name,
		)
		self._refuse_open_issue_drafts_now()

	def _refuse_open_issue_drafts_now(self):
		"""Terminal re-check, as Employee IR / Department IR end their submits: an Employee IR Issue
		draft saved on one of these work orders while this transaction waited for its locks is
		invisible to the earlier (snapshot) read. Locking reads that never wait see it; a hit
		rolls the whole document back."""
		drafts = outstanding_issue_drafts(
			[r.manufacturing_work_order for r in self.operations],
			exclude=None,
			locking=True,
		)
		if drafts:
			hit = drafts[0]
			frappe.throw(
				_(
					"Manufacturing Work Order {0} is on draft Employee IR Issue {1}. Submit or delete it first."
				).format(bold(hit["mwo"]), bold(hit["draft"])),
				ProcessTransitionConflictError,
			)

	# -- cancel (GK-DEC-0007) ------------------------------------------------------------------------

	def before_cancel(self):
		# Cancelling moves the jobs out of the Next Process again: it needs the same authority as
		# handing them over from it (GK-DEC-0002 / GK-DEC-0007).
		require_process_access(self.next_process)
		logs = {
			log.manufacturing_work_order: log
			for log in frappe.get_all(
				"Process Transition Log",
				filters={
					"voucher_type": self.doctype,
					"voucher_no": self.name,
					"event": ["in", TRANSFER_EVENTS],
				},
				fields=[
					"name",
					"manufacturing_work_order",
					"manufacturing_operation",
					"from_process",
					"to_process",
					"transition_time",
				],
			)
		}
		state = read_locked_state(
			{mwo: log.manufacturing_operation for mwo, log in logs.items()}
		)
		latest = [
			s.work_order.process_transition
			for s in state.values()
			if isinstance(s, dict)
		]
		effective = effective_transitions(latest)
		later = self._later_transition_vouchers(latest)
		moved_material = self._stock_entries_since(logs)
		drafts = open_issue_drafts(list(logs), locked=True)
		entries = []
		for row in self.operations:
			log = logs.get(row.manufacturing_work_order)
			if not log:
				continue
			current = state[row.manufacturing_work_order]
			prefix = _("Row #{0}: Manufacturing Work Order {1}").format(
				row.idx, bold(row.manufacturing_work_order)
			)
			if not isinstance(current, dict):
				reason = current
			elif effective.get(cstr(current.work_order.process_transition)) != cstr(
				log.name
			):
				reason = _(
					"has a later process change ({0}); only the latest change can be cancelled."
				).format(
					bold(
						later.get(cstr(current.work_order.process_transition))
						or current.work_order.process_transition
					)
				)
			elif readiness_problem(current, current.work_order):
				reason = _("{0} since this transfer.").format(
					readiness_problem(current, current.work_order)
				)
			elif row.manufacturing_work_order in drafts:
				reason = _(
					"is on draft Employee IR Issue {0}; submit or delete it first."
				).format(bold(drafts[row.manufacturing_work_order]))
			elif log.manufacturing_operation in moved_material:
				reason = _(
					"had material moved for it by Stock Entry {0} after this transfer."
				).format(bold(moved_material[log.manufacturing_operation]))
			else:
				reason = None
			if reason:
				frappe.throw(
					_("{0}: this Process Transfer cannot be cancelled. {1}").format(
						prefix, reason
					)
					if not isinstance(current, dict)
					else _("{0} {1} This Process Transfer cannot be cancelled.").format(
						prefix, reason
					),
					ProcessTransitionConflictError,
				)
			entries.append(
				frappe._dict(
					mwo=row.manufacturing_work_order,
					mop=log.manufacturing_operation,
					department=self.department,
					from_process=log.to_process,
					to_process=log.from_process,
					previous_transition=current.work_order.process_transition,
					reverses=log.name,
				)
			)
		self.flags.process_cancel_entries = entries

	def on_cancel(self):
		apply_transition(
			self.flags.process_cancel_entries or [],
			event="Cancellation",
			voucher_type=self.doctype,
			voucher_no=self.name,
		)
		self._refuse_open_issue_drafts_now()

	@staticmethod
	def _later_transition_vouchers(log_names):
		names = [n for n in log_names if n]
		if not names:
			return {}
		return {
			cstr(r.name): f"{_(r.voucher_type)} {r.voucher_no}"
			for r in frappe.get_all(
				"Process Transition Log",
				filters={"name": ["in", names]},
				fields=["name", "voucher_type", "voucher_no"],
			)
		}

	@staticmethod
	def _stock_entries_since(logs) -> dict[str, str]:
		"""``{operation: Stock Entry}`` for submitted Stock Entries that moved material for a
		transferred operation after its transfer (header or row reference). "After" is judged by
		when the entry was submitted (``modified``), not drafted (``creation``): a draft from before
		the transfer submitted after it still blocks. Conservative: any later change to a submitted
		entry, or a delayed posting of older material, also blocks."""
		if not logs:
			return {}
		since = {
			log.manufacturing_operation: log.transition_time for log in logs.values()
		}
		SE = frappe.qb.DocType("Stock Entry")
		SED = frappe.qb.DocType("Stock Entry Detail")
		earliest = min(since.values())
		rows = (
			frappe.qb.from_(SE)
			.select(
				SE.manufacturing_operation.as_("mop"),
				Max(SE.name).as_("entry"),
				Max(SE.modified).as_("at"),
			)
			.where(
				(SE.docstatus == 1)
				& SE.manufacturing_operation.isin(list(since))
				& (SE.modified > earliest)
			)
			.groupby(SE.manufacturing_operation)
		).run(as_dict=True) + (
			frappe.qb.from_(SED)
			.join(SE)
			.on(SE.name == SED.parent)
			.select(
				SED.manufacturing_operation.as_("mop"),
				Max(SE.name).as_("entry"),
				Max(SE.modified).as_("at"),
			)
			.where(
				(SE.docstatus == 1)
				& SED.manufacturing_operation.isin(list(since))
				& (SE.modified > earliest)
			)
			.groupby(SED.manufacturing_operation)
		).run(as_dict=True)
		return {r.mop: r.entry for r in rows if r.mop in since and r.at > since[r.mop]}

	# -- scan / fetch ------------------------------------------------------------------------------

	@frappe.whitelist(methods=["POST"])
	def add_work_orders(self, work_orders: list[str] | str) -> None:
		"""Add scanned or selected work orders as rows, filled from the database. Work orders the
		user cannot read are reported as not found, without saying whether they exist."""
		frappe.has_permission(
			self.doctype,
			"create" if self.is_new() else "write",
			doc=None if self.is_new() else self,
			throw=True,
		)
		if cint(self.docstatus) != 0:
			frappe.throw(
				_("Work orders can only be added to a draft."), ProcessControlError
			)
		if isinstance(work_orders, str):
			parsed = (
				frappe.parse_json(work_orders)
				if work_orders.strip().startswith("[")
				else work_orders.replace(",", "\n").splitlines()
			)
			work_orders = parsed
		names = list(
			dict.fromkeys(cstr(w).strip() for w in work_orders or () if cstr(w).strip())
		)
		listed = {
			cstr(r.manufacturing_work_order).strip()
			for r in self.get("operations") or ()
		}
		visible = (
			set(
				frappe.get_list(
					"Manufacturing Work Order",
					filters={"name": ["in", names]},
					pluck="name",
				)
			)
			if names
			else set()
		)
		active = get_active_operations(
			[n for n in names if n in visible and n not in listed]
		)
		problems = []
		for name in names:
			if name in listed:
				problems.append(_("{0} is already listed.").format(bold(name)))
				continue
			if name not in visible:
				problems.append(
					_("{0} was not found, or you have no access to it.").format(
						bold(name)
					)
				)
				continue
			mop = active.get(name)
			if not isinstance(mop, dict):
				problems.append(cstr(mop))
				continue
			self.append(
				"operations",
				{
					"manufacturing_work_order": name,
					"manufacturing_operation": mop.name,
					"current_process": mop.department_process,
					"next_process": self.next_process,
					"process_transition_snapshot": mop.work_order.process_transition,
				},
			)
			listed.add(name)
		if problems:
			frappe.msgprint(
				"<br>".join(problems),
				title=_("Some work orders were not added"),
				indicator="orange",
			)
