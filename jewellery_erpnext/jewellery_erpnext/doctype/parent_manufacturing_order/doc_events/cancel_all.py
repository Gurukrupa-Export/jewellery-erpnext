import re
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe import _
from frappe.desk.form.linked_with import SubmittableDocumentTree, get_exempted_doctypes
from frappe.model import delete_doc

PMO = "Parent Manufacturing Order"
DONE_EVENT = "pmo_cancel_all_done"

# ERPNext cancels a Stock Entry's bundles along with it and refuses to cancel a bundle on its own
# while the voucher is still submitted, so bundles are neither cancelled nor walked through here.
CANCELLED_WITH_VOUCHER = {"Serial and Batch Bundle"}

# Ledger rows a voucher reverses itself on cancel; ERPNext sets these as ignore_linked_doctypes in
# on_cancel (stock_entry.py), so Frappe's link check skips them and so does the pre-check here.
LEDGER_ROWS = {
	"GL Entry",
	"Stock Ledger Entry",
	"Repost Item Valuation",
	"Payment Ledger Entry",
}

# Records a PMO is made *from*, which other PMOs (of the same plan, order or item) can share. The
# walk from the PMO never enters them; each is cancelled only when nothing outside this cancel still
# uses it, in this order, since a Sales Order is used by its Plan and a Quotation by its Sales Order.
SHARED_UPSTREAM = ("Manufacturing Plan", "Sales Order", "Quotation", "Tracking Bom")


def _retryable_errors() -> tuple[type[Exception], ...]:
	"""Errors that only mean "something later in the plan has to go first", worth another pass."""
	from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
		BatchNegativeStockError,
		SerialNoExistsInFutureTransactionError,
	)
	from erpnext.stock.stock_ledger import (
		NegativeStockError,
		SerialNoExistsInFutureTransaction,
	)

	return (
		frappe.LinkExistsError,
		NegativeStockError,
		BatchNegativeStockError,
		SerialNoExistsInFutureTransaction,
		SerialNoExistsInFutureTransactionError,
	)


def _reset_after_failed_attempt(doctype: str, name: str) -> None:
	"""Rolling back to a savepoint restores the rows, not the process state around them.

	A cancel that dies half way leaves its cached copies (e.g. a Serial and Batch Bundle already
	flipped to docstatus 2) and its currently_saving entry behind; on retry ERPNext would read that
	stale bundle, skip cancelling it, and strand it under a cancelled Stock Entry.
	"""
	frappe.local.cache.clear()
	for dt in (*CANCELLED_WITH_VOUCHER, doctype):
		frappe.clear_document_cache(dt)
	frappe.flags.currently_saving = []
	frappe.clear_messages()


class LeveledSubmittableTree(SubmittableDocumentTree):
	"""Same walk as Frappe's "Cancel All", but deduplicated and remembering how deep each document sits.

	Frappe cancels its list top-down, so a child PMO is tried while its own Stock Entries are still
	submitted and the whole request rolls back. Depth is the tie-breaker in get_cancel_plan.
	"""

	def get_levels(self) -> dict[tuple[str, str], int]:
		levels = {(self.root_doctype, self.root_docname): 0}
		depth = 0
		while self.to_be_visited_documents:
			depth += 1
			next_level_children = {}
			for parent_dt, parent_docs in self.to_be_visited_documents.items():
				if not parent_docs:
					continue
				for linked_dt, linked_names in self.get_next_level_children(
					parent_dt, parent_docs
				).items():
					if (
						linked_dt in CANCELLED_WITH_VOUCHER
						or linked_dt in SHARED_UPSTREAM
					):
						continue
					for linked_name in linked_names:
						if (linked_dt, linked_name) in levels:
							continue
						if linked_dt == PMO:
							# Another PMO is never part of this cancel, nor is anything reached only through it.
							continue
						levels[(linked_dt, linked_name)] = depth
						next_level_children.setdefault(linked_dt, []).append(
							linked_name
						)
			self.to_be_visited_documents = next_level_children
		return levels


def get_cancel_plan(pmo_name: str) -> list[tuple[str, str]]:
	"""The PMO, everything made from it, and the shared records nothing else uses; newest first.

	A document can normally only link to documents that already existed when it was made, so
	newest-first satisfies almost every "cancel that one first". It also matches the two chains link
	depth gets wrong: stock (a batch hops RM -> Reserve -> Department -> WIP across several Stock
	Entries, cancelling an early hop first drives it negative) and IRs.
	"""
	return get_cancel_scope(pmo_name)[0]


def get_cancel_scope(pmo_name: str) -> tuple[list[tuple[str, str]], dict]:
	"""Returns (plan, kept): kept[shared record] = the records outside this cancel still using it."""
	levels = LeveledSubmittableTree(PMO, pmo_name).get_levels()
	exempted = set(get_exempted_doctypes() or [])
	root = (PMO, pmo_name)

	linked = [key for key in levels if key != root and key[0] not in exempted]
	shared, kept = _shared_upstream(root, {*linked, root})
	linked += shared
	created = _creation_times(linked)
	# Depth only breaks ties (and orders anything whose creation could not be read).
	linked.sort(
		key=lambda key: (str(created.get(key) or ""), levels.get(key, 0)), reverse=True
	)
	return [*linked, root], kept


def _shared_upstream(root: tuple[str, str], plan_docs: set) -> tuple[list, dict]:
	"""The PMO's Manufacturing Plan, Sales Order, Quotation and Tracking Bom that can go with it.

	One is cancelled only when every submitted record still using it is itself being cancelled: with
	another submitted PMO on the same plan (or another plan on the same Sales Order) it stays, and
	once those are cancelled, or when this PMO was the only one, it goes too. Checked in order, as
	cancelling the Plan is what frees the Sales Order, and the Sales Order the Quotation.
	"""
	pmo = frappe.db.get_value(
		PMO,
		root[1],
		["manufacturing_plan", "sales_order", "quotation", "custom_tracking_bom"],
		as_dict=True,
	)
	candidates = [
		("Manufacturing Plan", pmo.manufacturing_plan),
		("Sales Order", pmo.sales_order),
		("Quotation", pmo.quotation),
		("Tracking Bom", pmo.custom_tracking_bom),
	]
	if pmo.sales_order:
		for quotation in frappe.get_all(
			"Sales Order Item",
			{"parent": pmo.sales_order},
			pluck="prevdoc_docname",
			distinct=True,
		):
			candidates.insert(3, ("Quotation", quotation))

	# The order's other items carry their own Tracking BOMs (whose PMOs may be cancelled already);
	# each goes by the same rule once its Sales Order and Quotation are gone. Item BOMs never do.
	for dt, parent in [("Sales Order Item", pmo.sales_order)] + [
		("Quotation Item", key[1])
		for key in candidates
		if key[0] == "Quotation" and key[1]
	]:
		if parent:
			for tracking_bom in frappe.get_all(
				dt, {"parent": parent}, pluck="custom_tracking_bom", distinct=True
			):
				candidates.append(("Tracking Bom", tracking_bom))

	included, kept, seen = [], {}, set()
	for key in candidates:
		if not key[1] or key in seen or key in plan_docs:
			continue
		seen.add(key)
		if frappe.db.get_value(*key, "docstatus") != 1:
			continue
		users = [ref for ref in _submitted_referrers(key) if ref not in plan_docs]
		if users:
			kept[key] = users
		else:
			included.append(key)
			plan_docs.add(key)
	return included, kept


def _submitted_referrers(key: tuple[str, str]) -> list[tuple[str, str]]:
	"""Submitted records linking to `key`, as Frappe's cancel check sees them (minus ledger rows)."""
	from jewellery_erpnext.utils import get_submitted_linked_docs

	return [
		ref
		for ref in get_submitted_linked_docs(frappe.get_doc(*key))
		if ref[0] not in CANCELLED_WITH_VOUCHER and ref[0] not in LEDGER_ROWS
	]


def _creation_times(keys: list[tuple[str, str]]) -> dict[tuple[str, str], object]:
	by_doctype = {}
	for dt, dn in keys:
		by_doctype.setdefault(dt, []).append(dn)

	created = {}
	for dt, names in by_doctype.items():
		for row in frappe.get_all(
			dt, filters={"name": ["in", names]}, fields=["name", "creation"]
		):
			created[(dt, row.name)] = row.creation
	return created


def get_linked_records(plan: list[tuple[str, str]]) -> tuple[dict, list]:
	"""For every planned record, the submitted records linking to it: Frappe's own cancel check.

	Returns (`referrers`, `outside`): referrers[X] are the planned records that must be cancelled
	before X, and `outside` lists links from submitted records that are not part of this cancel,
	which would block it and are reported before anything is touched.
	"""
	from jewellery_erpnext.utils import get_submitted_links

	plan_docs = set(plan)
	released = _released_by_plan(plan)
	pmos = {dn for dt, dn in plan if dt == PMO}
	order_of = {}  # Work Order / PMO -> (its PMO, docstatus), shared by every record's rows

	by_doctype = {}
	for dt, dn in plan:
		by_doctype.setdefault(dt, []).append(dn)
	# One query per doctype and link field for all its planned records, not one per record.
	links = {dt: get_submitted_links(dt, names) for dt, names in by_doctype.items()}

	referrers, outside = {}, []
	for key in plan:
		doc = frappe.get_doc(*key)
		ignored = set(doc.get("ignore_linked_doctypes") or [])
		for ref in links[key[0]][key[1]]:
			if (
				ref[0] in ignored
				or ref[0] in CANCELLED_WITH_VOUCHER
				or ref[0] in LEDGER_ROWS
			):
				continue
			if ref in plan_docs:
				referrers.setdefault(key, set()).add(ref)
			elif (key, ref) in released:
				# Cleared by a planned record's own cancel, so that record goes first.
				referrers.setdefault(key, set()).add(released[(key, ref)])
			else:
				outside.append((key, ref))
		outside += [(key, ref) for ref in _other_orders_covered(doc, pmos, order_of)]
	return referrers, outside


def _other_orders_covered(
	doc, pmos: set, order_of: dict | None = None
) -> list[tuple[str, str]]:
	"""Work Orders / PMOs of *other* orders that this record's own rows point at.

	One Department IR, Employee IR or Stock Entry can carry rows for several orders' Work Orders, and
	its cancel acts on every row (moves those Work Orders back, cancels their Stock Entries). Nothing
	links *to* it from those orders, so the referrer check above misses it; this looks the other way.
	"""
	order_of = {} if order_of is None else order_of
	found = []
	for row in [doc, *doc.get_all_children()]:
		for df in row.meta.get_link_fields():
			if (
				df.options not in (PMO, "Manufacturing Work Order")
				or df.fieldname == "amended_from"
			):
				continue
			ref = (df.options, row.get(df.fieldname))
			if not ref[1] or ref in found:
				continue
			if ref not in order_of:
				if df.options == PMO:
					order_of[ref] = (
						ref[1],
						frappe.db.get_value(PMO, ref[1], "docstatus"),
					)
				else:
					order_of[ref] = frappe.db.get_value(
						*ref, ["manufacturing_order", "docstatus"]
					) or (None, None)
			pmo, docstatus = order_of[ref]
			# Already-cancelled ones are left out: this record's cancel cannot set them back further.
			if pmo not in pmos and docstatus != 2:
				found.append(ref)
	return found


def _released_by_plan(plan: list[tuple[str, str]]) -> dict:
	"""Links from outside the plan that a planned record's cancel removes: {(target, ref): record}.

	A Serial Number Creator points the shared Tracking BOM at the FG BOM it made and points it away
	again on cancel (release_tracking_bom_for_finished_goods), so that link does not block the BOM.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator import (
		_linked_tracking_bom,
	)

	released = {}
	for dt, dn in plan:
		if dt != "Serial Number Creator":
			continue
		snc = frappe.db.get_value(
			dt,
			dn,
			["fg_bom", "manufacturing_work_order", "parent_manufacturing_order"],
			as_dict=True,
		)
		tracking_bom = snc and snc.fg_bom and _linked_tracking_bom(snc)
		if tracking_bom:
			released[(("BOM", snc.fg_bom), ("Tracking Bom", tracking_bom))] = (dt, dn)
	return released


def _order_by_links(
	plan: list[tuple[str, str]], referrers: dict
) -> list[tuple[str, str]]:
	"""Records linking to X go before X. `plan` (newest first) decides among the free ones, and a
	loop (each side linking to the other) is entered at its newest record."""
	rank = {key: i for i, key in enumerate(plan)}
	remaining = set(plan)
	ordered = []
	while remaining:
		free = [k for k in remaining if not (referrers.get(k, set()) & remaining)]
		nxt = min(free or remaining, key=rank.__getitem__)
		ordered.append(nxt)
		remaining.discard(nxt)
	return ordered


def _outside_links_message(outside: list) -> str:
	return "<br>".join(
		_("{0} {1} is linked with {2} {3}, which is not part of this cancel").format(
			key[0], key[1], ref[0], ref[1]
		)
		for key, ref in outside
	)


@contextmanager
def _ignore_links_from(plan_docs: set[tuple[str, str]]):
	"""Let one cancel through when the only documents still linking to it are in this same plan.

	Used only to break a real loop (Serial Number Creator <-> BOM, Stock Entry <-> Material
	Request), where each side refuses to go first. The others in the loop are cancelled later in the
	same transaction, and any failure rolls back the whole run, so nothing ends up submitted while
	pointing at a cancelled document. Links from outside the plan still block, and only the Cancel
	check is relaxed: deletes (e.g. an IR removing a Manufacturing Operation) keep theirs.
	"""
	real_static, real_dynamic = (
		delete_doc.get_linked_docs,
		delete_doc.get_dynamic_linked_docs,
	)

	def outside_plan(real):
		def wrapper(doc, method="Delete"):
			links = real(doc, method)
			if method != "Cancel":
				return links
			return [
				link
				for link in links
				if (link["reference_doctype"], link["reference_docname"])
				not in plan_docs
			]

		return wrapper

	with (
		patch.object(delete_doc, "get_linked_docs", outside_plan(real_static)),
		patch.object(delete_doc, "get_dynamic_linked_docs", outside_plan(real_dynamic)),
	):
		yield


@frappe.whitelist()
def get_cancel_preview(pmo_name: str) -> dict:
	frappe.only_for("System Manager")
	# Just the list for the confirmation dialog. The link pre-check is the slow part and the job
	# runs it anyway before touching anything, so it is not repeated here while the form waits.
	plan, kept = get_cancel_scope(pmo_name)
	return {
		"docs": [{"doctype": dt, "name": dn} for dt, dn in plan],
		"kept": [
			{
				"doctype": k[0],
				"name": k[1],
				"used_by": [f"{dt} {dn}" for dt, dn in users],
			}
			for k, users in kept.items()
		],
	}


@frappe.whitelist()
def enqueue_cancel_all(pmo_name: str) -> None:
	frappe.only_for("System Manager")
	frappe.has_permission(PMO, "cancel", pmo_name, throw=True)
	# The list, the per-doctype Cancel permissions and the link pre-check are all done by the job
	# (as this user) before anything is cancelled; a failure there is sent back to the form.
	frappe.enqueue(
		cancel_all,
		queue="long",
		timeout=3600,
		job_id=f"pmo_cancel_all::{pmo_name}",
		deduplicate=True,
		pmo_name=pmo_name,
		user=frappe.session.user,
	)


def mark_workflow_cancelled(keys: list[tuple[str, str]]) -> None:
	"""Move cancelled records of workflow doctypes (Quotation, Material Request) to the workflow's
	cancelled state. doc.cancel() only sets docstatus; a workflow's own Cancel action is what moves
	workflow_state, so without this the form still shows e.g. "Submitted" on a cancelled Quotation."""
	from frappe.model.workflow import get_workflow_name, get_workflow_state_field

	by_doctype = {}
	for dt, dn in keys:
		by_doctype.setdefault(dt, []).append(dn)

	for dt, names in by_doctype.items():
		workflow = get_workflow_name(dt)
		if not workflow:
			continue
		state = frappe.db.get_value(
			"Workflow Document State", {"parent": workflow, "doc_status": "2"}, "state"
		)
		if not state:
			continue
		for name in frappe.get_all(
			dt, {"name": ["in", names], "docstatus": 2}, pluck="name"
		):
			frappe.db.set_value(
				dt,
				name,
				get_workflow_state_field(workflow),
				state,
				update_modified=False,
			)


# Each restart teaches one "cancel X before Y"; a plan this size needs a handful at most.
MAX_RESTARTS = 25


class _MustWaitFor(Exception):
	"""`doc`'s cancel touched a document that links to `blockers`, which this run already cancelled."""

	def __init__(self, doc, blockers):
		super().__init__(f"{doc} must be cancelled before {blockers}")
		self.doc = doc
		self.blockers = blockers


def _try_cancel(
	doctype: str, name: str, retryable, errors: dict, plan_docs=None
) -> bool:
	savepoint = f"pmo_cancel_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(savepoint)
	try:
		if plan_docs is None:
			frappe.get_doc(doctype, name).cancel()
		else:
			with _ignore_links_from(plan_docs):
				frappe.get_doc(doctype, name).cancel()
	except retryable as e:
		frappe.db.rollback(save_point=savepoint)
		_reset_after_failed_attempt(doctype, name)
		errors[(doctype, name)] = str(e)
		return False
	except frappe.CancelledLinkError as e:
		# e.g. a Quotation's on_cancel saves its Tracking Bom, whose BOM we already cancelled. Retrying
		# cannot help (the BOM stays cancelled), so tell the caller which one has to wait.
		blockers = [
			key
			for key in (plan_docs or _PlanDocs.value)
			if key != (doctype, name)
			and _names_in(key[1], str(e))
			and frappe.db.get_value(key[0], key[1], "docstatus") == 2
		]
		if not blockers:
			raise
		raise _MustWaitFor((doctype, name), blockers) from e
	except frappe.ValidationError as e:
		# e.g. the same Quotation hook saving a Tracking Bom this run already cancelled: "Cannot edit
		# cancelled document" names nothing, but the document being saved is on the stack.
		edited = _cancelled_doc_being_edited(e)
		if (
			not edited
			or edited == (doctype, name)
			or edited not in (plan_docs or _PlanDocs.value)
		):
			raise
		raise _MustWaitFor((doctype, name), [edited]) from e

	errors.pop((doctype, name), None)
	return True


def _names_in(name: str, text: str) -> bool:
	"""`name` appears in `text` as a whole name: BOM-0001 does not match inside BOM-00012."""
	return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) is not None


def _cancelled_doc_being_edited(exc: Exception) -> tuple[str, str] | None:
	tb = exc.__traceback__
	while tb:
		frame = tb.tb_frame
		if frame.f_code.co_name == "check_docstatus_transition":
			# The stored docstatus (the argument) is what makes it "cancelled", not the in-memory one.
			doc = frame.f_locals.get("self")
			if doc is not None and frame.f_locals.get("to_docstatus") == 2:
				return (doc.doctype, doc.name)
		tb = tb.tb_next
	return None


class _PlanDocs:
	"""The plan of the run in progress, so _try_cancel can name blockers without threading it through."""

	value: frozenset = frozenset()


def _still_submitted(keys) -> set:
	"""The records among `keys` whose docstatus is 1, one query per doctype."""
	by_doctype = {}
	for dt, dn in keys:
		by_doctype.setdefault(dt, []).append(dn)
	return {
		(dt, dn)
		for dt, names in by_doctype.items()
		for dn in frappe.get_all(
			dt, {"name": ["in", names], "docstatus": 1}, pluck="name", order_by=None
		)
	}


def _cancel_in_order(
	plan, waits_for, retryable, progress, cancelled_by: dict, referrers=None
) -> list:
	"""One attempt at the whole plan. Returns the documents let through to break a loop.

	Fills `cancelled_by` with the plan documents another cancel took down on its way (a Sales Order
	cancelling its Tracking Bom), so a "wait for X" rule can be moved onto the one that really acts.
	"""
	plan_docs = set(plan)
	pending = list(plan)
	errors = {}
	loop_breaks = []

	# Which planned records are still submitted, kept in memory: it only changes when a cancel here
	# succeeds (failed attempts roll back to their savepoint), and then it is re-read from the
	# database once per doctype -- that also catches records another cancel took down with it.
	standing = _still_submitted(plan)
	referrers = referrers or {}

	def is_submitted(key):
		return key in standing

	def must_wait(key):
		"""Checked before every cancel: a planned record still linking to it, or a learned rule."""
		return any(
			is_submitted(first)
			for first in (*referrers.get(key, ()), *waits_for.get(key, ()))
		)

	def cancelled(key):
		standing.discard(key)
		still = _still_submitted(standing)
		for other in standing - still:
			cancelled_by[other] = key
		standing.intersection_update(still)
		progress(key)

	while pending:
		still_pending = []
		for key in pending:
			# A parent's on_cancel may already have cancelled it (Department IR cancels its own
			# Stock Entries), so check the current state rather than the planned one.
			if not is_submitted(key):
				progress(key)
			elif must_wait(key):
				still_pending.append(key)
			elif _try_cancel(*key, retryable, errors):
				cancelled(key)
			else:
				still_pending.append(key)

		if still_pending and len(still_pending) == len(pending):
			# Stuck: everything left waits on something else left. Enter the loop at a record whose
			# only blockers are planned links (never a learned rule), ignoring those links once.
			candidates = [
				key
				for key in still_pending
				if not any(is_submitted(first) for first in waits_for.get(key, ()))
			]
			broken = next(
				(
					key
					for key in candidates
					if _try_cancel(*key, retryable, errors, plan_docs)
				),
				None,
			)
			if not broken:
				frappe.throw(
					"<br>".join(
						f"{dt} {dn}: {errors.get((dt, dn)) or _('waiting for another document')}"
						for dt, dn in still_pending
					),
					title=_("Could not cancel these documents"),
				)
			loop_breaks.append(broken)
			still_pending.remove(broken)
			cancelled(broken)
		pending = still_pending

	return loop_breaks


def cancel_all(pmo_name: str, user: str) -> None:
	"""Cancel the PMO and everything linked to it, all or nothing.

	Documents are tried newest first. One that is blocked by another document still standing (a
	submitted link, or stock a later entry still holds) is rolled back to its savepoint and retried
	on the next pass. When a whole pass makes no progress the rest are stuck in a loop, so one of
	them is cancelled ignoring links from the plan (see _ignore_links_from) and normal passes resume.

	Some cancels save another document on the way (a Quotation deactivates its Tracking Bom), which
	fails once something that document links to is already cancelled. Then the run is rolled back,
	"X before Y" is remembered, and it starts again; each restart learns one such rule.
	"""
	plan = []
	retryable = _retryable_errors()
	waits_for = {}
	learned = []
	done = set()

	def progress(key):
		done.add(key)
		frappe.publish_progress(
			len(done) / len(plan) * 100,
			title=_("Cancelling linked documents"),
			description=f"{key[0]} {key[1]}",
		)

	try:
		plan = get_cancel_plan(pmo_name)
		missing = sorted(
			{dt for dt, _dn in plan if not frappe.has_permission(dt, "cancel")}
		)
		if missing:
			frappe.throw(
				_("You do not have Cancel permission on: {0}").format(
					", ".join(missing)
				),
				frappe.PermissionError,
			)
		# Checked before anything is touched, so a blocker outside the plan never surfaces half way.
		referrers, outside = get_linked_records(plan)
		if outside:
			frappe.throw(
				_outside_links_message(outside),
				title=_("Linked records outside this cancel"),
			)
		plan = _order_by_links(plan, referrers)
		_PlanDocs.value = frozenset(plan)

		for _attempt in range(MAX_RESTARTS + 1):
			run_savepoint = f"pmo_cancel_run_{frappe.generate_hash(length=8)}"
			frappe.db.savepoint(run_savepoint)
			done.clear()
			cancelled_by = {}
			try:
				loop_breaks = _cancel_in_order(
					plan, waits_for, retryable, progress, cancelled_by, referrers
				)
				mark_workflow_cancelled(plan)
				break
			except _MustWaitFor as e:
				frappe.db.rollback(save_point=run_savepoint)
				_reset_after_failed_attempt(*e.doc)
				new_rules = []
				for blocker in e.blockers:
					# The blocker may have gone down inside another cancel; that one has to wait too,
					# or the rule never takes effect and every restart learns it again.
					chain, key = [], blocker
					while key is not None and key not in chain:
						chain.append(key)
						key = cancelled_by.get(key)
					for target in chain:
						if e.doc not in waits_for.get(target, ()):
							waits_for.setdefault(target, set()).add(e.doc)
							new_rules.append(target)
				if not new_rules:
					frappe.throw(
						_(
							"{0} still cannot be cancelled after {1}; cancel order learned so far: {2}"
						).format(e.doc, e.blockers, learned),
						title=_("Could not find a cancel order"),
					)
				learned.append((e.doc, new_rules))
		else:
			frappe.throw(
				_("Gave up after {0} restarts: {1}").format(MAX_RESTARTS, learned)
			)

	except Exception as e:
		frappe.log_error(title=f"PMO Cancel All failed: {pmo_name}")
		frappe.publish_realtime(
			DONE_EVENT,
			{"pmo": pmo_name, "status": "failed", "error": str(e)},
			user=user,
		)
		# Re-raise so the job runner rolls back; returning would make it commit the partial cancel.
		raise
	finally:
		_PlanDocs.value = frozenset()

	# after_commit: the job runner commits only once this returns, and the form reloads on this event.
	frappe.publish_realtime(
		DONE_EVENT,
		{
			"pmo": pmo_name,
			"status": "done",
			"count": len(plan),
			"loop_breaks": loop_breaks,
			"learned_order": learned,
		},
		user=user,
		after_commit=True,
	)
