"""
Mock-level tests for the Parent Manufacturing Order Hold / Open / Close flow.

* Hold puts the PMO's open work orders, their current operations and its Material Requests on
  Hold and keeps what each one was; Open puts back what is still on Hold; Close closes the
  work orders, finishes their waiting operations and closes the requests -- each only after
  the PMO's work orders pass the planning checks (department, nothing in progress, no
  transfer job pending, and for Close no material);
* rows are locked NOWAIT (requests, operations, work orders) and decided from those reads;
* a Close whose requests moved stock is queued: the PMO goes on Hold, a job re-opens /
  cancels one entry per transaction and applies the Close, and a failure keeps the Hold;
* every Hold, Open and Close takes one of the reasons MOP Settings lists for it and adds a row
  to the PMO's Hold / Open / Close Reasons with the action; a Submit (a held draft's too) and
  the Hold a queued Close puts in between take none;
* nothing moves for a held or closed PMO: IR rows, work order submits, Material Request saves,
  Stock Entries and operation status edits are refused;
* a pure workflow move of the PMO skips the PMO's own validation.

Every DB read goes through a patched module helper or a doctype-keyed stub that passes other
doctypes through (an unkeyed frappe.get_all stub catches the translation cache's own read; a
blanket frappe.db.get_value stub hijacks DocType meta loading).

Run with:
  bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_pmo_close_open
"""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events.mwo_closed_guard import (
	block_closed_mwo_operations,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
	ManufacturingWorkOrder,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events import (
	close_open,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	ParentManufacturingOrder,
)

GUARD = "jewellery_erpnext.jewellery_erpnext.doc_events.mwo_closed_guard"
MWO_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order"
CO = "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.close_open"
MPM = "Manufacturing Plan & Management - T"


def _keyed_get_all(tables):
	"""``frappe.get_all`` answering for the doctypes in ``tables`` (a value or a callable taking
	the call's arguments) and passing every other doctype on."""
	real = frappe.get_all

	def fake(doctype, *args, **kwargs):
		if doctype in tables:
			value = tables[doctype]
			return value(*args, **kwargs) if callable(value) else value
		return real(doctype, *args, **kwargs)

	return fake


def _keyed_get_value(values):
	"""``frappe.db.get_value`` answering for ``(doctype, name, field)`` keys and forwarding every
	other call verbatim."""
	real = frappe.db.get_value

	def fake(*args, **kwargs):
		key = tuple(args[:3])
		if key in values:
			return values[key]
		return real(*args, **kwargs)

	return fake


def _mwo(
	name, status="Not Started", docstatus=1, pointer=None, department=MPM, gross_wt=0
):
	return frappe._dict(
		name=name,
		status=status,
		docstatus=docstatus,
		manufacturing_operation=pointer,
		department=department,
		gross_wt=gross_wt,
	)


def _mop(name, mwo, status="Not Started", department_ir_status="", gross_wt=0):
	return frappe._dict(
		name=name,
		manufacturing_work_order=mwo,
		status=status,
		department_ir_status=department_ir_status,
		gross_wt=gross_wt,
	)


def _mr(name, state="Draft", docstatus=0, transfer=None):
	return frappe._dict(
		name=name,
		docstatus=docstatus,
		workflow_state=state,
		custom_transfer_se_state=transfer,
	)


def _docs(mrs=(), mops=(), mwos=()):
	return close_open._documents(
		{r.name: r for r in mrs}, {r.name: r for r in mops}, {r.name: r for r in mwos}
	)


class _FakePMO(frappe._dict):
	"""A PMO document: state, snapshot and the calls the flow makes on it."""

	def __init__(
		self,
		name="PMO-1",
		state="Submitted",
		before=None,
		snapshot=None,
		flags=None,
		**fields,
	):
		super().__init__(
			name=name,
			workflow_state=state,
			hold_snapshot=json.dumps(snapshot) if snapshot else None,
			**fields,
		)
		self.flags = frappe._dict(flags or {})
		self.db_set = MagicMock()
		self.add_comment = MagicMock()
		self.reload = MagicMock()
		self.get_doc_before_save = lambda: before

	def append(self, fieldname, values):
		"""Document.append: the new child row, which the flow inserts with db_insert."""
		row = frappe._dict(values, db_insert=MagicMock())
		self.setdefault(fieldname, []).append(row)
		return row


def _stored_snapshot(pmo):
	"""The snapshot the flow wrote with db_set (None when it cleared it)."""
	args = pmo.db_set.call_args
	assert args.args[0] == "hold_snapshot", args
	return json.loads(args.args[1]) if args.args[1] else None


class _Base(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		# Build the translation cache before any frappe.get_all stub is active.
		frappe._("Work Order Closed or on Hold")


class TestIRGuardChecksSubmitToo(_Base):
	def _ir(self, docstatus):
		return frappe._dict(
			docstatus=docstatus,
			department_ir_operation=[
				frappe._dict(idx=1, manufacturing_operation="MOP-1")
			],
		)

	@patch(
		f"{GUARD}.closed_mwo_operations", return_value={"MOP-1": ("MWO-1", "Closed")}
	)
	def test_a_draft_save_is_refused(self, _closed):
		with self.assertRaisesRegex(frappe.ValidationError, "which is Closed"):
			block_closed_mwo_operations(self._ir(0), "department_ir_operation")

	@patch(
		f"{GUARD}.closed_mwo_operations", return_value={"MOP-1": ("MWO-1", "Closed")}
	)
	def test_a_submit_is_refused(self, _closed):
		"""A submit reaches validate with docstatus 1 already set."""
		with self.assertRaisesRegex(frappe.ValidationError, "which is Closed"):
			block_closed_mwo_operations(self._ir(1), "department_ir_operation")

	@patch(f"{GUARD}.closed_mwo_operations", return_value={"MOP-1": ("MWO-1", "Hold")})
	def test_a_held_work_order_is_refused_too(self, _closed):
		with self.assertRaisesRegex(frappe.ValidationError, "which is on Hold"):
			block_closed_mwo_operations(self._ir(1), "department_ir_operation")

	@patch(
		f"{GUARD}.closed_mwo_operations", return_value={"MOP-1": ("MWO-1", "Closed")}
	)
	def test_a_cancelled_document_is_left_alone(self, closed):
		block_closed_mwo_operations(self._ir(2), "department_ir_operation")
		closed.assert_not_called()

	@patch(f"{GUARD}.closed_mwo_operations", return_value={})
	def test_an_open_work_order_passes(self, _closed):
		block_closed_mwo_operations(self._ir(1), "department_ir_operation")


class TestWorkOrderSubmitUnderBlockedPmo(_Base):
	def _mwo(self):
		return frappe._dict(
			manufacturing_order="PMO-1",
			is_without_so=0,
			validate_photoshop_images=MagicMock(),
		)

	def test_submit_is_refused_while_the_pmo_is_held_or_closed(self):
		for state, text in (("Hold", "is on Hold"), ("Closed", "is Closed")):
			with self.subTest(state=state):
				mwo = self._mwo()
				with patch(
					f"{MWO_MOD}.pmo_blocking_state", return_value=state
				) as blocking:
					with self.assertRaisesRegex(
						frappe.ValidationError, f"PMO-1.*{text}"
					):
						ManufacturingWorkOrder.before_submit(mwo)
				blocking.assert_called_once_with("PMO-1")
				mwo.validate_photoshop_images.assert_not_called()

	@patch(f"{MWO_MOD}.pmo_request_state", return_value=None)
	@patch(f"{MWO_MOD}.pmo_blocking_state", return_value=None)
	def test_submit_goes_on_when_the_pmo_is_open(self, _blocking, _requested):
		mwo = self._mwo()
		ManufacturingWorkOrder.before_submit(mwo)
		mwo.validate_photoshop_images.assert_called_once_with()

	@patch(f"{MWO_MOD}.pmo_blocking_state", return_value=None)
	def test_submit_is_refused_while_the_pmo_has_a_request(self, _blocking):
		for state, text in (
			("Close Requested", "Close request"),
			("Hold Requested", "Hold request"),
		):
			with self.subTest(state=state):
				mwo = self._mwo()
				with patch(f"{MWO_MOD}.pmo_request_state", return_value=state):
					with self.assertRaisesRegex(
						frappe.ValidationError, f"PMO-1.*{text}.*cannot be submitted"
					):
						ManufacturingWorkOrder.before_submit(mwo)
				mwo.validate_photoshop_images.assert_not_called()

	def test_split_is_refused_while_the_pmo_has_a_request(self):
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order import (
			_validate_split_basics,
		)

		row = frappe._dict(
			docstatus=1,
			status="Not Started",
			has_split_mwo=0,
			department=MPM,
			gross_wt=0,
			manufacturing_operation=None,
			manufacturing_order="PMO-1",
		)
		real = frappe.db.get_value

		def _get_value(*args, **kwargs):
			if args[:2] == ("Manufacturing Work Order", "MWO-1"):
				return row
			return real(*args, **kwargs)

		with (
			patch(f"{MWO_MOD}.frappe.db.get_value", side_effect=_get_value),
			patch(
				f"{MWO_MOD}.pmo_request_state", return_value="Close Requested"
			) as requested,
		):
			with self.assertRaisesRegex(
				frappe.ValidationError, "PMO-1.*Close request.*MWO-1 cannot be split"
			):
				_validate_split_basics("MWO-1")
		requested.assert_called_once_with("PMO-1")


class TestPmoBlockingState(_Base):
	"""Both site states: with the PMO workflow (and its workflow_state field) and without it (a
	fresh install only marks the workflow patch as done)."""

	@patch(f"{CO}._has_workflow_state", return_value=False)
	def test_a_site_without_the_workflow_has_no_blocked_pmo(self, _has):
		with (
			patch(
				"frappe.db.get_value", side_effect=AssertionError("no read expected")
			),
			patch("frappe.get_all", side_effect=_keyed_get_all({})) as get_all,
		):
			self.assertIsNone(close_open.pmo_blocking_state("PMO-1"))
			self.assertEqual(close_open.blocked_pmos(["PMO-1"]), {})
		get_all.assert_not_called()

	@patch(f"{CO}._has_workflow_state", return_value=True)
	def test_reads_the_stored_state(self, _has):
		cases = [
			("Hold", "Hold"),
			("Hold (Draft)", "Hold (Draft)"),
			("Closed", "Closed"),
			("Closed (Draft)", "Closed (Draft)"),
			("Submitted", None),
			("Draft", None),
			(None, None),
		]
		for stored, expected in cases:
			with self.subTest(stored=stored):
				with patch(
					"frappe.db.get_value",
					side_effect=_keyed_get_value(
						{
							(
								"Parent Manufacturing Order",
								"PMO-1",
								"workflow_state",
							): stored
						}
					),
				):
					self.assertEqual(close_open.pmo_blocking_state("PMO-1"), expected)

	@patch(f"{CO}._has_workflow_state", return_value=True)
	def test_a_work_order_without_a_pmo_is_not_looked_up(self, _has):
		with patch(
			"frappe.db.get_value", side_effect=AssertionError("no read expected")
		):
			self.assertIsNone(close_open.pmo_blocking_state(None))


class TestDispatch(_Base):
	REASON = "Customer request"
	# previous state, new state, the handler that runs, the action whose reason is recorded
	PAIRS = (
		("Submitted", "Hold", "_hold", "Hold"),
		("Draft", "Hold (Draft)", "_hold", "Hold"),
		("Submitted", "Closed", "_close", "Close"),
		("Hold", "Closed", "_close", "Close"),
		("Draft", "Closed (Draft)", "_close", "Close"),
		("Hold (Draft)", "Closed (Draft)", "_close", "Close"),
		("Hold", "Submitted", "_open", "Open"),
		# a held draft's submit puts back what its Hold set, but it is a Submit
		("Hold (Draft)", "Submitted", "_open", None),
		("Draft", "Submitted", None, None),
		("Submitted", "Submitted", None, None),
	)

	def _dispatch(self, previous, current, flags=None, refused=None):
		pmo = _FakePMO(
			state=current, before=frappe._dict(workflow_state=previous), flags=flags
		)
		with (
			patch(f"{CO}._hold") as hold,
			patch(f"{CO}._close") as close,
			patch(f"{CO}._open") as open_,
			patch(f"{CO}._record_reason") as record,
		):
			if refused:
				with self.assertRaisesRegex(frappe.ValidationError, refused):
					close_open.on_workflow_state_change(pmo)
			else:
				close_open.on_workflow_state_change(pmo)
		return pmo, {"_hold": hold, "_close": close, "_open": open_}, record

	def test_each_transition_runs_its_action_and_records_its_reason(self):
		for previous, current, expected, action in self.PAIRS:
			with self.subTest(previous=previous, current=current):
				pmo, ran, record = self._dispatch(
					previous, current, flags={close_open.REASON_FLAG: self.REASON}
				)
				reason = self.REASON if action else None
				for name, mock in ran.items():
					if name == expected:
						# _hold also gets the state it came from (a Close request is kept).
						args = (previous,) if name == "_hold" else ()
						mock.assert_called_once_with(pmo, reason, *args)
					else:
						mock.assert_not_called()
				if action:
					record.assert_called_once_with(pmo, action, self.REASON)
				else:
					record.assert_not_called()

	def test_an_action_without_a_reason_is_refused_before_anything_moves(self):
		"""An apply_workflow that did not come through apply_action (the API, a list view bulk
		action) carries no reason."""
		for previous, current, _handler, action in self.PAIRS:
			if not action:
				continue
			for flags in (None, {close_open.REASON_FLAG: "  "}):
				with self.subTest(previous=previous, current=current, flags=flags):
					_pmo, ran, record = self._dispatch(
						previous, current, flags=flags, refused="A reason is needed"
					)
					for mock in (*ran.values(), record):
						mock.assert_not_called()

	def test_a_submit_needs_no_reason(self):
		for previous in ("Draft", "Hold (Draft)"):
			with self.subTest(previous=previous):
				_pmo, _ran, record = self._dispatch(previous, "Submitted")
				record.assert_not_called()

	def test_the_hold_a_queued_close_puts_in_between_records_nothing(self):
		pmo, ran, record = self._dispatch(
			"Submitted", "Hold", flags={close_open.INTERNAL_FLAG: True}
		)
		ran["_hold"].assert_called_once_with(pmo, None, "Submitted")
		record.assert_not_called()

	def test_the_reason_is_trimmed(self):
		pmo, ran, record = self._dispatch(
			"Submitted", "Hold", flags={close_open.REASON_FLAG: "  Customer request "}
		)
		ran["_hold"].assert_called_once_with(pmo, "Customer request", "Submitted")
		record.assert_called_once_with(pmo, "Hold", "Customer request")

	def test_a_close_request_resumes_after_its_hold(self):
		"""FRD 3.5: Open of a Hold made during a Close request, and Withdraw of a Hold request
		made during one, go back to Close Requested -- neither is a new request."""
		cases = (
			("Hold", "_open", "Open", {"resumed": True}),
			("Hold Requested", "_withdraw", "Withdraw Request", {"resumed": True}),
		)
		for previous, handler, action, kwargs in cases:
			with self.subTest(previous=previous):
				pmo = _FakePMO(
					state="Close Requested",
					before=frappe._dict(workflow_state=previous),
					flags={close_open.REASON_FLAG: self.REASON},
				)
				with (
					patch(f"{CO}._open") as open_,
					patch(f"{CO}._withdraw") as withdraw,
					patch(f"{CO}._request") as request,
					patch(f"{CO}._record_reason") as record,
				):
					close_open.on_workflow_state_change(pmo)
				ran = {"_open": open_, "_withdraw": withdraw}[handler]
				args = (
					(pmo, self.REASON)
					if handler == "_open"
					else (pmo, previous, self.REASON)
				)
				ran.assert_called_once_with(*args, **kwargs)
				request.assert_not_called()
				record.assert_called_once_with(pmo, action, self.REASON)

	def test_a_hold_request_during_a_close_request_passes_the_close_request_on(self):
		pmo = _FakePMO(
			state="Hold Requested",
			before=frappe._dict(workflow_state="Close Requested"),
			flags={close_open.REASON_FLAG: self.REASON},
		)
		with (
			patch(f"{CO}._request") as request,
			patch(f"{CO}._record_reason") as record,
		):
			close_open.on_workflow_state_change(pmo)
		request.assert_called_once_with(
			pmo, "Request Hold", self.REASON, "Close Requested"
		)
		record.assert_called_once_with(pmo, "Request Hold", self.REASON)

	def test_a_refused_action_records_no_reason(self):
		pmo = _FakePMO(
			state="Hold",
			before=frappe._dict(workflow_state="Submitted"),
			flags={close_open.REASON_FLAG: self.REASON},
		)
		with (
			patch(f"{CO}._hold", side_effect=frappe.ValidationError("not in planning")),
			patch(f"{CO}._record_reason") as record,
		):
			with self.assertRaises(frappe.ValidationError):
				close_open.on_workflow_state_change(pmo)
		record.assert_not_called()

	def test_hold_open_hold_close_adds_four_rows_in_order(self):
		"""Each action is its own apply_workflow; the rows build up on the PMO."""
		pmo = _FakePMO(state="Submitted", docstatus=1)
		steps = (
			("Submitted", "Hold", "Awaiting customer approval"),
			("Hold", "Submitted", "Customer approved"),
			("Submitted", "Hold", "Design change"),
			("Hold", "Closed", "Order cancelled"),
		)
		with (
			patch(f"{CO}._hold"),
			patch(f"{CO}._open"),
			patch(f"{CO}._close"),
			patch(f"{CO}.now_datetime", return_value="2026-10-21 10:00:00"),
		):
			for previous, current, reason in steps:
				pmo.workflow_state = current
				pmo.get_doc_before_save = MagicMock(
					return_value=frappe._dict(workflow_state=previous)
				)
				pmo.flags = frappe._dict({close_open.REASON_FLAG: reason})
				close_open.on_workflow_state_change(pmo)
		self.assertEqual(
			[(row.status, row.reason) for row in pmo.status_reasons],
			[
				("Hold", "Awaiting customer approval"),
				("Open", "Customer approved"),
				("Hold", "Design change"),
				("Close", "Order cancelled"),
			],
		)
		for row in pmo.status_reasons:
			self.assertEqual(row.action_by, frappe.session.user)
			self.assertEqual(row.action_on, "2026-10-21 10:00:00")
			self.assertEqual(row.docstatus, 1)
			row.db_insert.assert_called_once_with()

	def test_a_drafts_row_is_a_draft_row(self):
		pmo = _FakePMO(state="Hold (Draft)", docstatus=0)
		with patch(f"{CO}.now_datetime", return_value="2026-10-21 10:00:00"):
			close_open._record_reason(pmo, "Hold", "Customer request")
		(row,) = pmo.status_reasons
		self.assertEqual(row.docstatus, 0)
		row.db_insert.assert_called_once_with()

	def test_an_insert_does_nothing(self):
		pmo = _FakePMO(state="Draft", before=None)
		with patch(f"{CO}._pmo_documents") as documents:
			close_open.on_workflow_state_change(pmo)
		documents.assert_not_called()


class TestHold(_Base):
	def _hold(self, docs, reason="Awaiting customer approval", previous=None, pmo=None):
		pmo = pmo or _FakePMO(state="Hold")
		with (
			patch(f"{CO}._pmo_documents", return_value=docs) as documents,
			patch(f"{CO}._refuse_unless_ready") as refuse,
			patch(f"{CO}.set_values_in_bulk") as bulk,
			patch(f"{CO}._notify_pmo") as notify,
		):
			close_open._hold(pmo, reason, previous)
		documents.assert_called_once_with("PMO-1", "Hold")
		refuse.assert_called_once_with("PMO-1", "Hold", docs)
		self.notify = notify
		return pmo, bulk

	def test_the_departments_holding_its_work_orders_are_alerted(self):
		docs = _docs(mwos=[_mwo("MWO-1"), _mwo("MWO-2", department="Waxing - T")])
		pmo, _bulk = self._hold(docs)
		self.notify.assert_called_once()
		args = self.notify.call_args.args
		self.assertIs(args[0], pmo)
		self.assertIn("PMO-1 has been put on Hold", args[1])
		self.assertEqual(args[2], [MPM, "Waxing - T"])
		self.assertEqual(args[3], "Awaiting customer approval")

	def test_a_hold_during_a_close_request_keeps_the_close_request(self):
		pmo, _bulk = self._hold(_docs(mwos=[_mwo("MWO-1")]), previous="Close Requested")
		self.assertEqual(_stored_snapshot(pmo)[close_open.RESUME_CLOSE_KEY], 1)
		self.assertIn("Close request carries on", pmo.add_comment.call_args.args[1])

	def test_the_hold_of_a_queued_close_keeps_no_request_and_alerts_nobody(self):
		pmo = _FakePMO(
			state="Hold",
			snapshot={close_open.RESUME_CLOSE_KEY: 1},
			flags={close_open.INTERNAL_FLAG: True},
		)
		self._hold(_docs(mwos=[_mwo("MWO-1")]), None, "Close Requested", pmo=pmo)
		self.assertNotIn(close_open.RESUME_CLOSE_KEY, _stored_snapshot(pmo))
		self.notify.assert_not_called()

	def test_holds_work_orders_current_operations_and_requests_and_keeps_what_they_were(
		self,
	):
		docs = _docs(
			mrs=[
				_mr("MR-1", "Draft"),
				_mr("MR-2", "Material Transferred to MOP", docstatus=1),
				_mr("MR-3", "Closed"),
			],
			mops=[
				_mop("MOP-1", "MWO-1"),
				_mop(
					"MOP-5",
					"MWO-5",
					status="Not Started",
					department_ir_status="Revert",
				),
			],
			mwos=[
				_mwo("MWO-1", pointer="MOP-1"),
				_mwo("MWO-2", status="Draft", docstatus=0),
				_mwo("MWO-5", pointer="MOP-5"),
			],
		)
		pmo, bulk = self._hold(docs)
		self.assertCountEqual(
			bulk.call_args_list,
			[
				call(
					"Manufacturing Work Order",
					["MWO-1", "MWO-2", "MWO-5"],
					{"status": "Hold"},
				),
				call("Manufacturing Operation", ["MOP-1"], {"status": "Hold"}),
				call("Material Request", ["MR-1", "MR-2"], {"workflow_state": "Hold"}),
			],
		)
		self.assertEqual(
			_stored_snapshot(pmo),
			{
				"mwo": {
					"MWO-1": "Not Started",
					"MWO-2": "Draft",
					"MWO-5": "Not Started",
				},
				"mop": {"MOP-1": "Not Started"},
				"mr": {"MR-1": "Draft", "MR-2": "Material Transferred to MOP"},
			},
		)
		pmo.add_comment.assert_called_once()
		self.assertIn(
			"Reason: Awaiting customer approval", pmo.add_comment.call_args.args[1]
		)

	def test_a_site_without_the_request_workflow_holds_no_request(self):
		docs = _docs(mrs=[frappe._dict(name="MR-1", docstatus=0)], mwos=[])
		_pmo, bulk = self._hold(docs)
		self.assertNotIn("Material Request", [c.args[0] for c in bulk.call_args_list])

	def test_a_refusal_writes_nothing(self):
		pmo = _FakePMO(state="Hold")
		with (
			patch(f"{CO}._pmo_documents", return_value=_docs(mwos=[_mwo("MWO-1")])),
			patch(
				f"{CO}._refuse_unless_ready", side_effect=frappe.ValidationError("no")
			),
			patch(f"{CO}.set_values_in_bulk") as bulk,
		):
			with self.assertRaises(frappe.ValidationError):
				close_open._hold(pmo)
		bulk.assert_not_called()
		pmo.db_set.assert_not_called()


class TestOpen(_Base):
	SNAPSHOT = {
		"mwo": {"MWO-1": "Not Started", "MWO-2": "Draft", "MWO-3": "Not Started"},
		"mop": {"MOP-1": "Not Started"},
		"mr": {"MR-1": "Draft", "MR-2": "Material Reserved"},
	}

	def test_puts_back_only_what_is_still_on_hold(self):
		pmo = _FakePMO(state="Submitted", snapshot=self.SNAPSHOT)
		docs = _docs(
			mrs=[_mr("MR-1", "Hold"), _mr("MR-2", "Hold", docstatus=2)],
			mops=[_mop("MOP-1", "MWO-1", status="Hold")],
			mwos=[
				_mwo("MWO-1", status="Hold", pointer="MOP-1"),
				_mwo("MWO-2", status="Hold", docstatus=0),
				# MWO-3 was cancelled during the Hold: not among the open work orders any more
			],
		)
		with (
			patch(f"{CO}._pmo_documents", return_value=docs) as documents,
			patch(f"{CO}.is_job_enqueued", return_value=False),
			patch(f"{CO}.set_values_in_bulk") as bulk,
			patch(f"{CO}._notify_pmo") as notify,
		):
			close_open._open(pmo)
		# No reason: a held draft's submit, which alerts nobody.
		notify.assert_not_called()
		documents.assert_called_once_with(
			"PMO-1", "Open", extra_operations=self.SNAPSHOT["mop"]
		)
		self.assertCountEqual(
			bulk.call_args_list,
			[
				call("Manufacturing Work Order", ["MWO-1"], {"status": "Not Started"}),
				call("Manufacturing Work Order", ["MWO-2"], {"status": "Draft"}),
				call("Manufacturing Operation", ["MOP-1"], {"status": "Not Started"}),
				call("Material Request", ["MR-1"], {"workflow_state": "Draft"}),
			],
		)
		self.assertIsNone(_stored_snapshot(pmo))

	def test_refused_once_a_close_has_cancelled_anything(self):
		snapshot = dict(self.SNAPSHOT, close={"cancelled": ["SE-2"], "reopened": []})
		pmo = _FakePMO(state="Submitted", snapshot=snapshot)
		with (
			patch(f"{CO}._pmo_documents") as documents,
			patch(f"{CO}.set_values_in_bulk") as bulk,
		):
			with self.assertRaisesRegex(frappe.ValidationError, "SE-2"):
				close_open._open(pmo)
		documents.assert_not_called()
		bulk.assert_not_called()

	def test_refused_while_a_close_job_runs(self):
		pmo = _FakePMO(state="Submitted", snapshot=self.SNAPSHOT)
		with (
			patch(f"{CO}.is_job_enqueued", return_value=True),
			patch(f"{CO}._pmo_documents") as documents,
		):
			with self.assertRaisesRegex(frappe.ValidationError, "being closed"):
				close_open._open(pmo)
		documents.assert_not_called()

	def test_an_open_with_nothing_to_put_back_still_comments_its_reason(self):
		"""A held draft's submit (no reason) and nothing to put back leaves no comment."""
		for reason in ("Customer approved", None):
			with self.subTest(reason=reason):
				pmo = _FakePMO(state="Submitted", snapshot={"mwo": {}})
				with (
					patch(f"{CO}._pmo_documents", return_value=_docs()),
					patch(f"{CO}.is_job_enqueued", return_value=False),
					patch(f"{CO}.set_values_in_bulk") as bulk,
					patch(f"{CO}._notify_pmo") as notify,
				):
					close_open._open(pmo, reason)
				bulk.assert_not_called()
				if reason:
					self.assertIn(
						"Reason: Customer approved", pmo.add_comment.call_args.args[1]
					)
					self.assertIn("has been opened", notify.call_args.args[1])
				else:
					pmo.add_comment.assert_not_called()
					notify.assert_not_called()

	def test_an_open_that_resumes_a_close_request_says_so(self):
		pmo = _FakePMO(
			state="Close Requested",
			snapshot={close_open.RESUME_CLOSE_KEY: 1, "mwo": {}},
		)
		docs = _docs(mwos=[_mwo("MWO-1", department="Returns - T")])
		with (
			patch(f"{CO}._pmo_documents", return_value=docs),
			patch(f"{CO}.is_job_enqueued", return_value=False),
			patch(f"{CO}.set_values_in_bulk"),
			patch(f"{CO}._notify_pmo") as notify,
		):
			close_open._open(pmo, "Hold cleared", resumed=True)
		self.assertIsNone(_stored_snapshot(pmo))
		self.assertIn("Close request carries on", pmo.add_comment.call_args.args[1])
		subject, departments = notify.call_args.args[1:3]
		self.assertIn("Close request carries on", subject)
		self.assertEqual(departments, ["Returns - T"])


class TestClose(_Base):
	def test_closes_work_orders_finishes_waiting_operations_and_closes_requests(self):
		pmo = _FakePMO(state="Closed", snapshot={"mwo": {}})
		docs = _docs(
			mrs=[_mr("MR-1", "Hold"), _mr("MR-2", "Draft"), _mr("MR-3", "Cancelled")],
			mops=[
				_mop("MOP-1", "MWO-1", status="Hold"),
				_mop("MOP-2", "MWO-1", status="Not Started"),
				_mop("MOP-3", "MWO-1", status="Finished"),
				_mop(
					"MOP-4",
					"MWO-1",
					status="Not Started",
					department_ir_status="Revert",
				),
				# a current operation that moved to a work order that completed meanwhile
				_mop("MOP-9", "MWO-9", status="Not Started"),
			],
			mwos=[
				_mwo("MWO-1", status="Hold", pointer="MOP-1"),
				_mwo("MWO-2", status="Draft", docstatus=0),
				_mwo("MWO-9", status="Completed", pointer="MOP-9"),
			],
		)
		with (
			patch(f"{CO}._pmo_documents", return_value=docs) as documents,
			patch(f"{CO}._refuse_unless_ready") as refuse,
			patch(f"{CO}.set_values_in_bulk") as bulk,
			patch(f"{CO}._notify_pmo") as notify,
		):
			close_open._close(pmo, "Customer cancelled")
		notify.assert_called_once_with(
			pmo,
			"Parent Manufacturing Order PMO-1 has been closed: 2 work orders closed.",
			[MPM],
			"Customer cancelled",
		)
		documents.assert_called_once_with("PMO-1", "Close")
		refuse.assert_called_once_with("PMO-1", "Close", docs)
		self.assertCountEqual(
			bulk.call_args_list,
			[
				call(
					"Manufacturing Work Order", ["MWO-1", "MWO-2"], {"status": "Closed"}
				),
				call(
					"Manufacturing Operation",
					["MOP-1", "MOP-2"],
					{"status": "Finished"},
				),
				call(
					"Material Request", ["MR-1", "MR-2"], {"workflow_state": "Closed"}
				),
			],
		)
		self.assertIsNone(_stored_snapshot(pmo))
		self.assertIn("Reason: Customer cancelled", pmo.add_comment.call_args.args[1])


class TestProblems(_Base):
	DEPARTMENTS = [
		(MPM, "Manufacturing Plan & Management"),
		("Waxing - T", "Waxing"),
	]

	def _problems(
		self,
		action,
		docs,
		busy=(),
		moved=(),
		stock=True,
		departments=(),
		drafts=(),
		kinds=False,
	):
		def _stock_entry(*args, **kwargs):
			if "outgoing_stock_entry" in (kwargs.get("filters") or {}):
				return []
			return sorted(kwargs["filters"]["name"][1], reverse=True)

		with (
			# flt(x, 3) rounds through the system settings: without a bound site it reads 0.0,
			# which would let every weight pass.
			patch.object(
				frappe, "get_system_settings", return_value="Banker's Rounding"
			),
			patch(f"{CO}.close_hold_departments", return_value=list(departments)),
			patch(f"{CO}._is_planning_department", return_value=False),
			patch(f"{CO}._draft_transactions", return_value=list(drafts)),
			patch(
				"frappe.get_all",
				side_effect=_keyed_get_all(
					{
						"Department": self.DEPARTMENTS,
						"Manufacturing Operation": [frappe._dict(row) for row in busy],
						"Stock Entry Detail": [frappe._dict(row) for row in moved],
						"Stock Entry": _stock_entry,
						"Material Request": [],
					}
				),
			),
		):
			if kinds:
				return close_open._problem_list("PMO-1", action, docs, stock=stock)
			return close_open._problems("PMO-1", action, docs, stock=stock)

	def test_a_work_order_outside_planning_blocks_hold_and_close(self):
		docs = _docs(mwos=[_mwo("MWO-1", department="Waxing - T"), _mwo("MWO-2")])
		for action, verb in (("Hold", "placed on Hold"), ("Close", "closed")):
			with self.subTest(action=action):
				problems = self._problems(action, docs)
				self.assertEqual(len(problems), 1)
				self.assertIn(
					f"PMO cannot be {verb} because all linked Manufacturing Work Orders must be in the Manufacturing Plan and Management Department.",
					problems[0],
				)
				self.assertIn("MWO-1", problems[0])
				self.assertNotIn("MWO-2", problems[0])

	def test_draft_work_orders_are_not_checked(self):
		docs = _docs(mwos=[_mwo("MWO-1", status="Draft", docstatus=0, department=None)])
		self.assertEqual(self._problems("Close", docs), [])

	def test_an_operation_in_progress_blocks_and_the_locked_row_overrides_the_snapshot(
		self,
	):
		docs = _docs(
			mops=[_mop("MOP-1", "MWO-1", status="WIP")],
			mwos=[_mwo("MWO-1", pointer="MOP-1")],
		)
		problems = self._problems("Hold", docs)
		self.assertTrue(any("MOP-1 (MWO-1)" in p for p in problems), problems)
		# The plain read still says WIP, the locked row says Finished: not in progress.
		docs = _docs(
			mops=[_mop("MOP-1", "MWO-1", status="Finished")],
			mwos=[_mwo("MWO-1", pointer="MOP-1")],
		)
		busy = [{"name": "MOP-1", "manufacturing_work_order": "MWO-1"}]
		self.assertEqual(self._problems("Hold", docs, busy=busy), [])

	def test_a_held_operation_is_not_in_progress_but_in_transit_is(self):
		docs = _docs(
			mops=[_mop("MOP-1", "MWO-1", status="Hold")],
			mwos=[_mwo("MWO-1", status="Hold", pointer="MOP-1")],
		)
		self.assertEqual(self._problems("Close", docs), [])
		docs = _docs(
			mops=[_mop("MOP-1", "MWO-1", department_ir_status="In-Transit")],
			mwos=[_mwo("MWO-1", pointer="MOP-1")],
		)
		self.assertTrue(self._problems("Hold", docs))

	def test_a_pending_transfer_job_blocks(self):
		docs = _docs(
			mrs=[_mr("MR-1", "Material Transferred", docstatus=1, transfer="Pending")]
		)
		problems = self._problems("Hold", docs)
		self.assertTrue(
			any("MR-1" in p and "from reserve" in p for p in problems), problems
		)

	def test_weight_blocks_close_only(self):
		docs = _docs(
			mops=[_mop("MOP-1", "MWO-1", gross_wt=1.25)],
			mwos=[_mwo("MWO-1", pointer="MOP-1"), _mwo("MWO-2", gross_wt=0.5)],
		)
		self.assertEqual(self._problems("Hold", docs), [])
		problems = self._problems("Close", docs)
		self.assertEqual(len(problems), 1)
		self.assertIn(
			"PMO cannot be closed because the Gross Weight of every linked Manufacturing Work Order must be 0.",
			problems[0],
		)
		self.assertIn("MWO-1 (1.25)", problems[0])
		self.assertIn("MWO-2 (0.5)", problems[0])

	def test_stock_left_on_the_requests_blocks_the_synchronous_close(self):
		docs = _docs(mrs=[_mr("MR-1", "Material Reserved")])
		moved = [{"material_request": "MR-1", "parent": "SE-1"}]
		problems = self._problems("Close", docs, moved=moved)
		self.assertTrue(any("SE-1" in p for p in problems), problems)
		self.assertEqual(self._problems("Close", docs, moved=moved, stock=False), [])


class TestLocks(_Base):
	def _documents(self, action, locked_mwos, extra=None):
		order = MagicMock()
		order.lock_material_requests.return_value = {"MR-1": _mr("MR-1")}
		order.lock_manufacturing_operations.side_effect = lambda names, wait: {
			n: _mop(n, "MWO-1") for n in names
		}
		order.lock_work_orders.return_value = locked_mwos
		with (
			patch(
				"frappe.get_all",
				side_effect=_keyed_get_all(
					{
						"Manufacturing Work Order": [
							frappe._dict(name="MWO-1", manufacturing_operation="MOP-1")
						],
						"Manufacturing Operation": ["MOP-0"],
						"Material Request": ["MR-1"],
					}
				),
			),
			patch(f"{CO}._material_request_fields", return_value=("name", "docstatus")),
			patch(f"{CO}.lock_material_requests", order.lock_material_requests),
			patch(
				f"{CO}.lock_manufacturing_operations",
				order.lock_manufacturing_operations,
			),
			patch(f"{CO}.lock_work_orders", order.lock_work_orders),
		):
			docs = close_open._pmo_documents("PMO-1", action, extra_operations=extra)
		return order, docs

	def test_requests_then_operations_then_work_orders_nowait(self):
		order, docs = self._documents("Hold", {"MWO-1": _mwo("MWO-1", pointer="MOP-1")})
		self.assertEqual(
			order.mock_calls,
			[
				call.lock_material_requests(
					["MR-1"], wait=0, fields=("name", "docstatus")
				),
				call.lock_manufacturing_operations({"MOP-1"}, wait=0),
				call.lock_work_orders(["MWO-1"], wait=0),
			],
		)
		self.assertEqual(docs.pointers, {"MOP-1"})

	def test_close_also_takes_the_waiting_operations(self):
		order, _docs_ = self._documents(
			"Close", {"MWO-1": _mwo("MWO-1", pointer="MOP-1")}
		)
		self.assertIn(
			call.lock_manufacturing_operations({"MOP-1", "MOP-0"}, wait=0),
			order.mock_calls,
		)

	def test_a_moved_current_operation_is_taken_too(self):
		order, docs = self._documents(
			"Hold", {"MWO-1": _mwo("MWO-1", pointer="MOP-NEW")}
		)
		self.assertEqual(
			order.mock_calls[-1],
			call.lock_manufacturing_operations({"MOP-NEW"}, wait=0),
		)
		self.assertEqual(docs.pointers, {"MOP-NEW"})

	def test_a_busy_row_refuses_with_its_name(self):
		for target, cls in (
			("lock_material_requests", frappe.QueryTimeoutError),
			("lock_manufacturing_operations", frappe.QueryDeadlockError),
			("lock_work_orders", frappe.QueryTimeoutError),
		):
			with self.subTest(target=target):
				exc = cls("busy")
				exc.lock_row_name = "ROW-9"
				with (
					patch(
						"frappe.get_all",
						side_effect=_keyed_get_all(
							{
								"Manufacturing Work Order": [
									frappe._dict(
										name="MWO-1", manufacturing_operation="MOP-1"
									)
								],
								"Material Request": ["MR-1"],
							}
						),
					),
					patch(
						f"{CO}._material_request_fields",
						return_value=("name", "docstatus"),
					),
					patch(f"{CO}.lock_material_requests", return_value={}),
					patch(f"{CO}.lock_manufacturing_operations", return_value={}),
					patch(f"{CO}.lock_work_orders", return_value={}),
					patch(f"{CO}.{target}", side_effect=exc),
				):
					with self.assertRaisesRegex(
						frappe.ValidationError, "ROW-9.*Try again"
					):
						close_open._pmo_documents("PMO-1", "Hold")


class TestTransitionPermission(_Base):
	def test_an_action_the_workflow_does_not_offer_is_refused(self):
		doc = _FakePMO(state="Submitted")
		with patch(f"{CO}.get_transitions", return_value=[{"action": "Hold"}]):
			close_open._require_action(doc, "Hold")
			with self.assertRaises(frappe.PermissionError):
				close_open._require_action(doc, "Open")


class TestReasons(_Base):
	"""The reasons MOP Settings lists for each action (its table of status and reason)."""

	def test_reads_the_actions_reasons_in_table_order(self):
		asked = {}

		def _reasons(*args, **kwargs):
			asked.update(kwargs)
			return ["Awaiting customer approval", "Design change"]

		with patch(
			"frappe.get_all",
			side_effect=_keyed_get_all({"PMO Action Reason": _reasons}),
		):
			reasons = close_open.action_reasons("Hold")
		self.assertEqual(reasons, ["Awaiting customer approval", "Design change"])
		self.assertEqual(
			asked,
			{
				"filters": {
					"parenttype": "MOP Settings",
					"parent": "MOP Settings",
					"parentfield": "pmo_action_reasons",
					"status": "Hold",
				},
				"pluck": "reason",
				"order_by": "idx asc",
			},
		)

	@patch(f"{CO}.action_reasons", return_value=["Customer approved"])
	def test_only_a_listed_reason_is_taken(self, listed):
		self.assertEqual(
			close_open._valid_reason("Open", "  Customer approved "),
			"Customer approved",
		)
		listed.assert_called_with("Open")
		for reason in ("Customer cancelled", "", None):
			with self.subTest(reason=reason):
				with self.assertRaisesRegex(
					frappe.ValidationError,
					"Choose one of the reasons MOP Settings lists",
				):
					close_open._valid_reason("Open", reason)

	@patch(f"{CO}.action_reasons", return_value=[])
	def test_an_action_mop_settings_has_no_reasons_for_is_refused(self, _listed):
		with self.assertRaisesRegex(frappe.ValidationError, "add them in MOP Settings"):
			close_open._valid_reason("Close", "Customer cancelled")

	def test_check_action_offers_the_actions_reasons(self):
		for state, action, reasons in (
			("Submitted", "Hold", ["Awaiting customer approval", "Design change"]),
			("Hold", "Open", ["Customer approved"]),
		):
			with self.subTest(action=action):
				with (
					patch("frappe.get_doc", return_value=_FakePMO(state=state)),
					patch(f"{CO}.get_transitions", return_value=[{"action": action}]),
					patch(f"{CO}.is_job_enqueued", return_value=False),
					patch(f"{CO}._pmo_documents", return_value=_docs()),
					patch(f"{CO}._refuse_unless_ready"),
					patch(f"{CO}.action_reasons", return_value=reasons) as listed,
				):
					self.assertEqual(
						close_open.check_action("PMO-1", action), {"reasons": reasons}
					)
				listed.assert_called_once_with(action)

	def test_check_action_refuses_first_when_there_are_no_reasons(self):
		with (
			patch("frappe.get_doc", return_value=_FakePMO(state="Submitted")),
			patch(f"{CO}.get_transitions", return_value=[{"action": "Hold"}]),
			patch(f"{CO}.action_reasons", return_value=[]),
			patch(f"{CO}._pmo_documents") as documents,
		):
			with self.assertRaisesRegex(
				frappe.ValidationError, "There are no reasons for"
			):
				close_open.check_action("PMO-1", "Hold")
		documents.assert_not_called()

	def test_a_submit_reads_no_reasons(self):
		with (
			patch("frappe.get_doc", return_value=_FakePMO(state="Hold (Draft)")),
			patch(f"{CO}.get_transitions", return_value=[{"action": "Submit"}]),
			patch(f"{CO}.action_reasons") as listed,
		):
			self.assertEqual(close_open.check_action("PMO-1", "Submit"), {})
		listed.assert_not_called()


class TestApplyAction(_Base):
	"""apply_action runs the workflow action with the reason chosen for it."""

	@contextmanager
	def _patched(self, transitions=("Hold",), job=False):
		applied = []

		def _apply_workflow(doc, action):
			applied.append((action, doc.flags.get(close_open.REASON_FLAG)))

		with (
			patch("frappe.get_doc", return_value=_FakePMO(state="Submitted")),
			patch(
				f"{CO}.get_transitions",
				return_value=[{"action": a} for a in transitions],
			),
			patch(f"{CO}.action_reasons", return_value=["Awaiting customer approval"]),
			patch(f"{CO}.is_job_enqueued", return_value=job),
			patch(f"{CO}.apply_workflow", side_effect=_apply_workflow),
		):
			yield applied

	def test_the_reason_goes_with_the_workflow_action(self):
		with self._patched() as applied:
			close_open.apply_action("PMO-1", "Hold", " Awaiting customer approval ")
		self.assertEqual(applied, [("Hold", "Awaiting customer approval")])

	def test_only_hold_open_and_close_take_a_reason(self):
		with self._patched(transitions=("Submit",)) as applied:
			with self.assertRaisesRegex(
				frappe.ValidationError, "is not done with a reason"
			):
				close_open.apply_action("PMO-1", "Submit", "Awaiting customer approval")
		self.assertEqual(applied, [])

	def test_an_action_the_workflow_does_not_offer_is_refused(self):
		with self._patched() as applied:
			with self.assertRaises(frappe.PermissionError):
				close_open.apply_action("PMO-1", "Open", "Awaiting customer approval")
		self.assertEqual(applied, [])

	def test_a_reason_mop_settings_does_not_list_is_refused(self):
		with self._patched() as applied:
			with self.assertRaisesRegex(
				frappe.ValidationError, "Choose one of the reasons"
			):
				close_open.apply_action("PMO-1", "Hold", "Typed in by hand")
		self.assertEqual(applied, [])

	def test_refused_while_a_close_job_runs(self):
		with self._patched(job=True) as applied:
			with self.assertRaisesRegex(
				frappe.ValidationError, "being closed in the background"
			):
				close_open.apply_action("PMO-1", "Hold", "Awaiting customer approval")
		self.assertEqual(applied, [])


class TestCheckActionAndEnqueue(_Base):
	PLAN = frappe._dict(entries=["SE-3", "SE-2"], stopped=["MR-2"])

	def _patches(self, doc, plan):
		return (
			patch("frappe.get_doc", return_value=doc),
			patch(
				f"{CO}.get_transitions",
				return_value=[{"action": "Close"}, {"action": "Hold"}],
			),
			patch(f"{CO}._refuse_while_job_runs"),
			patch(f"{CO}._pmo_documents", return_value=_docs(mrs=[_mr("MR-2")])),
			patch(f"{CO}._refuse_unless_ready"),
			patch(f"{CO}._close_stock_plan", return_value=plan),
			patch(f"{CO}.action_reasons", return_value=["Customer cancelled"]),
		)

	def test_check_close_lists_the_entries_to_cancel(self):
		doc = _FakePMO(state="Submitted")
		details = [
			frappe._dict(
				name="SE-2",
				stock_entry_type="Material transfer to Reserve",
				posting_date="2026-10-01",
			),
			frappe._dict(
				name="SE-3",
				stock_entry_type="Material Transfer From Reserve",
				posting_date="2026-10-02",
			),
		]
		p = self._patches(doc, self.PLAN)
		with p[0], p[1], p[2], p[3], p[4] as refuse, p[5], p[6], patch(
			"frappe.get_all", side_effect=_keyed_get_all({"Stock Entry": details})
		):
			result = close_open.check_action("PMO-1", "Close")
		refuse.assert_called_once()
		self.assertEqual(refuse.call_args.kwargs, {"stock": False})
		self.assertEqual(result["reasons"], ["Customer cancelled"])
		self.assertEqual([e.name for e in result["entries"]], ["SE-3", "SE-2"])
		self.assertEqual(result["stopped"], ["MR-2"])

	def test_enqueue_close_refuses_when_there_is_nothing_to_cancel(self):
		doc = _FakePMO(state="Submitted")
		p = self._patches(doc, frappe._dict(entries=[], stopped=[]))
		with p[0], p[1], p[2], p[3], p[4], p[5], p[6], patch(
			"frappe.enqueue"
		) as enqueue:
			with self.assertRaisesRegex(
				frappe.ValidationError, "no Stock Entries to cancel"
			):
				close_open.enqueue_close("PMO-1", "Customer cancelled")
		enqueue.assert_not_called()

	def test_enqueue_close_refuses_a_reason_mop_settings_does_not_list(self):
		doc = _FakePMO(state="Submitted")
		p = self._patches(doc, self.PLAN)
		with p[0] as get_doc, p[1], p[2], p[3], p[4], p[5], p[6], patch(
			"frappe.enqueue"
		) as enqueue:
			with self.assertRaisesRegex(
				frappe.ValidationError, "Choose one of the reasons"
			):
				close_open.enqueue_close("PMO-1", "Typed in by hand")
		get_doc.assert_not_called()
		enqueue.assert_not_called()

	def test_enqueue_close_holds_a_submitted_pmo_and_queues_the_job(self):
		doc = _FakePMO(state="Submitted")
		p = self._patches(doc, self.PLAN)
		internal = []
		with (
			p[0],
			p[1],
			p[2],
			p[3],
			p[4],
			p[5],
			p[6],
			patch(f"{CO}._check_close_rights") as rights,
			patch(
				f"{CO}.apply_workflow",
				side_effect=lambda d, a: internal.append(
					d.flags.get(close_open.INTERNAL_FLAG)
				),
			) as apply,
			patch("frappe.enqueue") as enqueue,
			patch("frappe.msgprint"),
		):
			close_open.enqueue_close("PMO-1", " Customer cancelled ")
		rights.assert_called_once_with(self.PLAN)
		# Its own Hold is part of the Close: marked internal, so it takes no reason or row.
		apply.assert_called_once_with(doc, "Hold")
		self.assertEqual(internal, [True])
		self.assertNotIn(close_open.INTERNAL_FLAG, doc.flags)
		close = _stored_snapshot(doc)["close"]
		self.assertEqual(close["reason"], "Customer cancelled")
		self.assertIn("Reason: Customer cancelled", doc.add_comment.call_args.args[1])
		self.assertEqual(close["entries"], ["SE-3", "SE-2"])
		self.assertEqual(close["reopen"], ["MR-2"])
		self.assertEqual((close["cancelled"], close["reopened"]), ([], []))
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(enqueue.call_args.args[0], close_open._close_with_stock)
		self.assertEqual(kwargs["job_id"], "pmo_close::PMO-1")
		self.assertEqual(kwargs["queue"], "long")
		self.assertTrue(kwargs["deduplicate"] and kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["pmo"], "PMO-1")

	def test_enqueue_close_keeps_the_progress_of_an_earlier_attempt_on_hold(self):
		doc = _FakePMO(
			state="Hold",
			snapshot={"mwo": {}, "close": {"cancelled": ["SE-9"], "reopened": []}},
		)
		p = self._patches(doc, self.PLAN)
		with (
			p[0],
			p[1],
			p[2],
			p[3],
			p[4],
			p[5],
			p[6],
			patch(f"{CO}._check_close_rights"),
			patch(f"{CO}.apply_workflow") as apply,
			patch("frappe.enqueue"),
			patch("frappe.msgprint"),
		):
			close_open.enqueue_close("PMO-1", "Customer cancelled")
		apply.assert_not_called()
		self.assertEqual(_stored_snapshot(doc)["close"]["cancelled"], ["SE-9"])


class TestCloseJob(_Base):
	def _run(self, doc, steps, cancel_error=None):
		request, entry = MagicMock(), MagicMock()
		if cancel_error:
			entry.cancel.side_effect = cancel_error
		docs = {
			"Parent Manufacturing Order": doc,
			"Material Request": request,
			"Stock Entry": entry,
		}
		with (
			patch("frappe.set_user") as set_user,
			patch("frappe.get_doc", side_effect=lambda doctype, *a, **k: docs[doctype]),
			patch(f"{CO}._refuse_unless_ready"),
			patch(f"{CO}._pmo_documents"),
			patch(f"{CO}._next_close_step", side_effect=steps),
			patch(f"{CO}._record_close_step") as record,
			patch(f"{CO}.apply_workflow") as apply,
			patch(f"{CO}._report_close") as report,
			patch("frappe.db.commit") as commit,
			patch("frappe.db.rollback") as rollback,
			patch("frappe.log_error"),
		):
			close_open._close_with_stock("PMO-1", "planner@example.com")
		set_user.assert_called_once_with("planner@example.com")
		return frappe._dict(
			request=request,
			entry=entry,
			record=record,
			apply=apply,
			report=report,
			commit=commit,
			rollback=rollback,
		)

	def test_reopens_then_cancels_one_per_transaction_then_closes(self):
		doc = _FakePMO(
			state="Hold", snapshot={"close": {"reason": "Customer cancelled"}}
		)
		r = self._run(doc, [("reopen", "MR-2"), ("cancel", "SE-3"), None])
		r.request.check_permission.assert_called_once_with("write")
		r.request.update_status.assert_called_once_with("Submitted")
		r.entry.cancel.assert_called_once_with()
		self.assertEqual(
			r.record.call_args_list,
			[call(doc, "reopen", "MR-2"), call(doc, "cancel", "SE-3")],
		)
		self.assertEqual(r.commit.call_count, 3)
		r.apply.assert_called_once_with(doc, "Close")
		# the reason chosen when the Close was queued: the Close records it as it happens
		self.assertEqual(doc.flags[close_open.REASON_FLAG], "Customer cancelled")
		r.rollback.assert_not_called()
		message = r.report.call_args.args[2]
		self.assertIn("MR-2, SE-3", message)
		self.assertTrue(r.report.call_args.kwargs["ok"])

	def test_a_failed_cancel_rolls_back_that_step_keeps_the_hold_and_says_what_was_done(
		self,
	):
		doc = _FakePMO(state="Hold")
		r = self._run(
			doc,
			[("reopen", "MR-2"), ("cancel", "SE-3")],
			cancel_error=frappe.ValidationError("Insufficient stock<br>\nfor D-1"),
		)
		r.rollback.assert_called_once()
		r.apply.assert_not_called()
		self.assertEqual(r.commit.call_count, 1)
		message = r.report.call_args.args[2]
		self.assertIn("stopped at SE-3: Insufficient stock for D-1", message)
		self.assertIn("Already done: MR-2", message)
		self.assertFalse(r.report.call_args.kwargs["ok"])

	def test_an_already_closed_pmo_ends_the_job(self):
		doc = _FakePMO(state="Closed")
		r = self._run(doc, [])
		r.apply.assert_not_called()
		r.request.update_status.assert_not_called()
		self.assertTrue(r.report.call_args.kwargs["ok"])


class TestNextCloseStep(_Base):
	def _step(self, doc, plan):
		with (
			patch(
				"frappe.get_all",
				side_effect=_keyed_get_all({"Material Request": ["MR-2"]}),
			),
			patch(f"{CO}._close_stock_plan", return_value=plan),
		):
			return close_open._next_close_step(doc)

	def _doc(self, state="Hold", close=None):
		return _FakePMO(
			state=state, snapshot={"close": close} if close is not None else None
		)

	CLOSE = {
		"entries": ["SE-3", "SE-2"],
		"reopen": ["MR-2"],
		"cancelled": [],
		"reopened": [],
	}

	def test_reopens_the_confirmed_stopped_requests_first(self):
		plan = frappe._dict(entries=["SE-3"], stopped=["MR-2"])
		self.assertEqual(
			self._step(self._doc(close=self.CLOSE), plan), ("reopen", "MR-2")
		)

	def test_then_cancels_the_latest_confirmed_entry(self):
		plan = frappe._dict(entries=["SE-3", "SE-2"], stopped=[])
		self.assertEqual(
			self._step(self._doc(close=self.CLOSE), plan), ("cancel", "SE-3")
		)

	def test_nothing_left_means_only_the_close(self):
		self.assertIsNone(
			self._step(
				self._doc(close=self.CLOSE), frappe._dict(entries=[], stopped=[])
			)
		)

	def test_refuses_what_was_not_confirmed(self):
		for plan, text in (
			(frappe._dict(entries=["SE-7"], stopped=[]), "SE-7"),
			(frappe._dict(entries=["SE-3"], stopped=["MR-8"]), "MR-8"),
		):
			with self.subTest(text=text):
				with self.assertRaisesRegex(frappe.ValidationError, text):
					self._step(self._doc(close=self.CLOSE), plan)

	def test_refuses_once_the_pmo_left_hold_or_the_close_was_dropped(self):
		for doc in (
			self._doc(state="Submitted", close=self.CLOSE),
			self._doc(close=None),
		):
			with self.subTest(state=doc.workflow_state):
				with self.assertRaisesRegex(
					frappe.ValidationError, "no longer on Hold"
				):
					self._step(doc, frappe._dict(entries=[], stopped=[]))


class TestGuards(_Base):
	def test_a_material_request_of_a_blocked_pmo_is_refused(self):
		doc = frappe._dict(name="MR-1", manufacturing_order="PMO-1")
		with patch(f"{CO}.pmo_blocking_state", return_value="Hold"):
			with self.assertRaisesRegex(frappe.ValidationError, "PMO-1.*on Hold.*MR-1"):
				close_open.refuse_material_request_while_blocked(doc)
		with (
			patch(f"{CO}.pmo_blocking_state", return_value=None),
			patch(f"{CO}.pmo_request_state", return_value=None),
		):
			close_open.refuse_material_request_while_blocked(doc)

	def _stock_entry(self, **fields):
		return frappe._dict(
			{
				"manufacturing_order": None,
				"manufacturing_work_order": None,
				"manufacturing_operation": None,
				"outgoing_stock_entry": None,
				"items": [],
				"flags": frappe._dict(),
				**fields,
			}
		)

	def test_a_stock_entry_for_a_blocked_pmo_is_refused_from_any_reference(self):
		doc = self._stock_entry(
			manufacturing_work_order="MWO-9",
			outgoing_stock_entry="SE-OUT",
			items=[
				frappe._dict(material_request="MR-1", manufacturing_operation="MOP-1")
			],
		)
		asked = {}

		def _pmo(*args, **kwargs):
			asked["names"] = kwargs["filters"]["name"][1]
			return [("PMO-1", "Hold")]

		with (
			patch(f"{CO}._has_workflow_state", return_value=True),
			patch(
				"frappe.db.get_value",
				side_effect=_keyed_get_value(
					{
						(
							"Manufacturing Work Order",
							"MWO-9",
							"manufacturing_order",
						): "PMO-9"
					}
				),
			),
			patch(
				"frappe.get_all",
				side_effect=_keyed_get_all(
					{
						"Stock Entry Detail": ["MR-2"],
						"Material Request": ["PMO-1"],
						"Manufacturing Operation": ["PMO-1"],
						"Parent Manufacturing Order": _pmo,
					}
				),
			),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "PMO-1.*on Hold"):
				close_open.refuse_stock_entry_while_blocked(doc)
		self.assertEqual(asked["names"], ["PMO-1", "PMO-9"])

	def test_a_stock_entry_with_no_pmo_reference_reads_nothing(self):
		with (
			patch(f"{CO}._has_workflow_state", return_value=True),
			patch("frappe.get_all", side_effect=_keyed_get_all({})) as get_all,
		):
			close_open.refuse_stock_entry_while_blocked(self._stock_entry())
		get_all.assert_not_called()

	def test_only_the_pmo_flow_sets_or_clears_an_operation_hold(self):
		def _op(status, before):
			return frappe._dict(
				name="MOP-1",
				status=status,
				get_doc_before_save=lambda: frappe._dict(status=before)
				if before
				else None,
			)

		for status, before in (
			("Hold", "Not Started"),
			("Not Started", "Hold"),
			("Hold", None),
		):
			with self.subTest(status=status, before=before):
				with self.assertRaisesRegex(frappe.ValidationError, "MOP-1"):
					close_open.validate_operation_hold(_op(status, before))
		for status, before in (
			("Hold", "Hold"),
			("WIP", "Not Started"),
			("On Hold", "WIP"),
		):
			with self.subTest(status=status, before=before):
				close_open.validate_operation_hold(_op(status, before))


class TestPmoController(_Base):
	FIELDS = [
		frappe._dict(fieldname="workflow_state", fieldtype="Link"),
		frappe._dict(fieldname="status_reasons", fieldtype="Table"),
		frappe._dict(fieldname="item_code", fieldtype="Link"),
		frappe._dict(fieldname="section", fieldtype="Section Break"),
		frappe._dict(fieldname="metal_product_tolerance", fieldtype="Table"),
	]

	def _pmo(
		self, action="save", before_state="Draft", state="Hold (Draft)", **changes
	):
		before = frappe._dict(
			workflow_state=before_state,
			status_reasons=[],
			item_code="ITEM-1",
			metal_product_tolerance=[frappe._dict(name="row-1")],
		)
		values = dict(before, workflow_state=state, **changes)
		return frappe._dict(
			_action=action,
			meta=frappe._dict(fields=self.FIELDS),
			get_doc_before_save=lambda: before,
			**values,
		)

	def test_a_pure_state_move_is_recognised(self):
		self.assertTrue(ParentManufacturingOrder._state_change_only(self._pmo()))
		# the action's reason row is part of the move
		self.assertTrue(
			ParentManufacturingOrder._state_change_only(
				self._pmo(
					action="update_after_submit",
					before_state="Submitted",
					state="Hold",
					status_reasons=[frappe._dict(name="reason-1")],
				)
			)
		)

	def test_anything_else_is_not(self):
		cases = {
			"field edit": self._pmo(item_code="ITEM-2"),
			"row change": self._pmo(
				metal_product_tolerance=[frappe._dict(name="row-2")]
			),
			"submit": self._pmo(action="submit", state="Submitted"),
			"same state": self._pmo(state="Draft"),
		}
		for label, doc in cases.items():
			with self.subTest(label):
				self.assertFalse(ParentManufacturingOrder._state_change_only(doc))

	def test_a_held_or_closed_draft_is_not_submitted_directly(self):
		for state, text in (("Hold (Draft)", "on Hold"), ("Closed (Draft)", "Closed")):
			with self.subTest(state=state):
				with self.assertRaisesRegex(frappe.ValidationError, text):
					ParentManufacturingOrder.before_submit(
						frappe._dict(name="PMO-1", workflow_state=state)
					)

	def test_a_held_or_closed_pmo_is_not_cancelled(self):
		for state in ("Hold", "Closed"):
			with self.subTest(state=state):
				with self.assertRaises(frappe.ValidationError):
					ParentManufacturingOrder.before_cancel(
						frappe._dict(name="PMO-1", workflow_state=state)
					)
		for state in ("Submitted", "Cancelled"):
			ParentManufacturingOrder.before_cancel(
				frappe._dict(name="PMO-1", workflow_state=state)
			)

	def test_a_requested_pmo_is_not_cancelled_until_withdrawn(self):
		for state in ("Close Requested", "Hold Requested"):
			with self.subTest(state=state):
				with self.assertRaisesRegex(
					frappe.ValidationError, "withdraw the request"
				):
					ParentManufacturingOrder.before_cancel(
						frappe._dict(name="PMO-1", workflow_state=state)
					)


class TestRequestTransitions(_Base):
	"""Close Requested / Hold Requested: which handler a transition runs and which row it adds."""

	PAIRS = (
		("Submitted", "Close Requested", "_request", "Request Close"),
		("Submitted", "Hold Requested", "_request", "Request Hold"),
		("Close Requested", "Submitted", "_withdraw", "Withdraw Request"),
		("Hold Requested", "Submitted", "_withdraw", "Withdraw Request"),
		# the request is done with the normal action and its checks
		("Close Requested", "Closed", "_close", "Close"),
		("Close Requested", "Hold", "_hold", "Hold"),
		("Hold Requested", "Hold", "_hold", "Hold"),
	)

	def test_each_transition_runs_its_handler_and_records_its_reason(self):
		for previous, current, handler, action in self.PAIRS:
			with self.subTest(previous=previous, current=current):
				pmo = _FakePMO(
					state=current,
					before=frappe._dict(workflow_state=previous),
					flags={close_open.REASON_FLAG: "Customer request"},
				)
				names = ("_hold", "_close", "_open", "_request", "_withdraw")
				with (
					patch(f"{CO}._hold") as hold,
					patch(f"{CO}._close") as close,
					patch(f"{CO}._open") as open_,
					patch(f"{CO}._request") as request,
					patch(f"{CO}._withdraw") as withdraw,
					patch(f"{CO}._record_reason") as record,
				):
					close_open.on_workflow_state_change(pmo)
				mocks = dict(zip(names, (hold, close, open_, request, withdraw)))
				for name, mock in mocks.items():
					if name == handler:
						mock.assert_called_once()
					else:
						mock.assert_not_called()
				record.assert_called_once_with(pmo, action, "Customer request")

	def test_a_request_needs_a_reason(self):
		pmo = _FakePMO(
			state="Close Requested", before=frappe._dict(workflow_state="Submitted")
		)
		with patch(f"{CO}._request") as request:
			with self.assertRaisesRegex(frappe.ValidationError, "A reason is needed"):
				close_open.on_workflow_state_change(pmo)
		request.assert_not_called()

	def test_requests_take_the_reasons_of_what_they_wait_for(self):
		asked = []

		def _reasons(*args, **kwargs):
			asked.append(kwargs["filters"]["status"])
			return ["Customer request"]

		with patch(
			"frappe.get_all",
			side_effect=_keyed_get_all({"PMO Action Reason": _reasons}),
		):
			for action in ("Request Close", "Request Hold", "Withdraw Request", "Hold"):
				close_open.action_reasons(action)
		self.assertEqual(asked, ["Close", "Hold", "Open", "Hold"])


class TestRequestReadiness(_Base):
	"""What a request waits for, and when a refused Close / Hold offers one."""

	DEPARTMENTS = TestProblems.DEPARTMENTS
	_problems = TestProblems._problems

	def test_the_close_hold_department_counts_as_back(self):
		docs = _docs(mwos=[_mwo("MWO-1", department="Returns - T")])
		self.assertEqual(self._problems("Hold", docs, departments=["Returns - T"]), [])
		problems = self._problems("Hold", docs)
		self.assertIn("MWO-1 (Returns - T)", problems[0])

	def test_a_work_order_in_any_request_department_counts_as_back(self):
		docs = _docs(
			mwos=[
				_mwo("MWO-1", department="Returns - T"),
				_mwo("MWO-2", department="Repair - T"),
			]
		)
		both = ["Returns - T", "Repair - T"]
		self.assertEqual(self._problems("Hold", docs, departments=both), [])
		(problem,) = self._problems("Hold", docs, departments=["Returns - T"])
		self.assertIn("MWO-2 (Repair - T)", problem)
		self.assertNotIn("MWO-1", problem)
		self.assertIn("accepted too: <strong>Returns - T</strong>", problem)

	def test_draft_transactions_block_a_close_only(self):
		docs = _docs(mwos=[_mwo("MWO-1")])
		drafts = ["SE-9 (Stock Entry)", "DIR-1 (Department IR)"]
		self.assertEqual(self._problems("Hold", docs, drafts=drafts), [])
		((kind, message),) = self._problems("Close", docs, drafts=drafts, kinds=True)
		self.assertEqual(kind, close_open.PROBLEM_DRAFTS)
		self.assertIn("SE-9 (Stock Entry)", message)
		self.assertIn("DIR-1 (Department IR)", message)

	def test_the_weight_refusal_states_the_outstanding_metal(self):
		docs = _docs(
			mops=[_mop("MOP-1", "MWO-1", gross_wt=1.25)],
			mwos=[_mwo("MWO-1", pointer="MOP-1"), _mwo("MWO-2", gross_wt=0.5)],
		)
		(message,) = self._problems("Close", docs)
		self.assertIn("Outstanding metal: 1.75 g", message)

	def test_problem_kinds(self):
		docs = _docs(
			mrs=[_mr("MR-1", "Material Transferred", docstatus=1, transfer="Pending")],
			mwos=[_mwo("MWO-1", department="Waxing - T")],
		)
		kinds = [k for k, _m in self._problems("Close", docs, kinds=True)]
		self.assertEqual(
			kinds, [close_open.PROBLEM_DEPARTMENT, close_open.PROBLEM_TRANSFER]
		)

	def _offer(
		self, problems, actions=("Close", "Request Close"), departments=("Returns - T",)
	):
		doc = _FakePMO(state="Submitted")
		with (
			patch(
				f"{CO}.get_transitions",
				return_value=[{"action": a} for a in actions],
			),
			patch(f"{CO}.close_hold_departments", return_value=list(departments)),
			patch(f"{CO}._problem_list", return_value=problems),
			patch(f"{CO}.action_reasons", return_value=["Customer request"]),
			patch(
				f"{CO}._work_orders_to_return",
				return_value=[frappe._dict(work_order="MWO-1")],
			),
		):
			return close_open._request_offer(doc, "Close", _docs())

	def test_a_close_refused_only_for_the_work_orders_offers_a_request(self):
		offer = self._offer([(close_open.PROBLEM_DEPARTMENT, "elsewhere")])
		self.assertEqual(offer["action"], "Request Close")
		self.assertEqual(offer["problems"], ["elsewhere"])
		self.assertEqual(offer["reasons"], ["Customer request"])
		self.assertEqual(offer["departments"], ["Returns - T"])
		self.assertEqual(offer["pending"][0].work_order, "MWO-1")

	def test_no_offer_when_it_cannot_be_requested(self):
		cases = {
			"hard problem": dict(
				problems=[
					(close_open.PROBLEM_DEPARTMENT, "elsewhere"),
					(close_open.PROBLEM_TRANSFER, "pending"),
				]
			),
			"request not offered to this user": dict(
				problems=[(close_open.PROBLEM_WEIGHT, "metal")], actions=("Close",)
			),
			"no request department": dict(
				problems=[(close_open.PROBLEM_WEIGHT, "metal")], departments=()
			),
			"nothing to wait for": dict(problems=[]),
		}
		for label, case in cases.items():
			with self.subTest(label):
				self.assertIsNone(self._offer(**case))


class TestRequestAction(_Base):
	"""_request: the departments holding the work orders are alerted, nothing is written."""

	def _docs(self):
		return _docs(
			mops=[
				frappe._dict(
					name="MOP-1",
					manufacturing_work_order="MWO-1",
					status="WIP",
					department="Waxing - T",
					employee="EMP-1",
					department_ir_status="Received",
					gross_wt=2.5,
				),
				frappe._dict(
					name="MOP-2",
					manufacturing_work_order="MWO-2",
					status="Not Started",
					department="Returns - T",
					department_ir_status="Received",
					gross_wt=0,
				),
			],
			mwos=[
				_mwo("MWO-1", pointer="MOP-1", department="Waxing - T"),
				_mwo("MWO-2", pointer="MOP-2", department="Returns - T"),
			],
		)

	def _request(self, departments=("Returns - T",), users=None):
		pmo = _FakePMO(state="Close Requested")
		with (
			patch.object(
				frappe, "get_system_settings", return_value="Banker's Rounding"
			),
			patch(f"{CO}.close_hold_departments", return_value=list(departments)),
			patch(f"{CO}._pmo_documents", return_value=self._docs()),
			patch(f"{CO}._problem_list", return_value=[]),
			patch(
				"frappe.get_all",
				side_effect=_keyed_get_all(
					{
						"Department": [
							("Waxing - T", "Waxing"),
							("Returns - T", "Returns"),
						]
					}
				),
			),
			patch(
				f"{CO}._department_users",
				return_value=users
				if users is not None
				else {"Waxing - T": {"waxer@example.com"}},
			),
			patch(f"{CO}.enqueue_create_notification") as notify,
		):
			close_open._request(pmo, "Request Close", "Customer cancelled")
		return pmo, notify

	def test_only_the_department_holding_a_work_order_is_alerted(self):
		pmo, notify = self._request()
		department_call, requester_call = notify.call_args_list
		self.assertEqual(department_call.args[0], ["waxer@example.com"])
		alert = department_call.args[1]
		self.assertEqual(alert["document_name"], "PMO-1")
		self.assertIn("Waxing - T", alert["subject"])
		self.assertIn("Returns - T", alert["subject"])
		self.assertIn("MWO-1 (2.5 g)", alert["email_content"])
		self.assertNotIn("MWO-2", alert["email_content"])
		self.assertIn("Customer cancelled", alert["email_content"])
		self.assertEqual(requester_call.args[0], frappe.session.user)
		self.assertIn("Waxing - T", requester_call.args[1]["subject"])
		pmo.db_set.assert_not_called()
		self.assertIn("Customer cancelled", pmo.add_comment.call_args.args[1])

	def test_a_department_without_users_is_skipped(self):
		_pmo, notify = self._request(users={})
		(requester_call,) = notify.call_args_list
		self.assertIn("no department", requester_call.args[1]["subject"])

	def test_refused_without_a_request_department(self):
		with self.assertRaisesRegex(
			frappe.ValidationError, "Close / Hold Request Department"
		):
			self._request(departments=())

	def test_refused_while_a_transfer_from_reserve_runs(self):
		pmo = _FakePMO(state="Close Requested")
		with (
			patch(f"{CO}.close_hold_departments", return_value=["Returns - T"]),
			patch(f"{CO}._pmo_documents", return_value=_docs()),
			patch(
				f"{CO}._problem_list",
				return_value=[(close_open.PROBLEM_TRANSFER, "still transferring")],
			),
			patch(f"{CO}.enqueue_create_notification") as notify,
		):
			with self.assertRaisesRegex(frappe.ValidationError, "still transferring"):
				close_open._request(pmo, "Request Close", "Customer cancelled")
		notify.assert_not_called()


class TestRequestRestrictions(_Base):
	"""While requested, work orders only come back and no raw material moves to them."""

	def _ir(self, type_, next_department=None, table="department_ir_operation"):
		return frappe._dict(
			{
				"type": type_,
				"docstatus": 0,
				"next_department": next_department,
				table: [frappe._dict(manufacturing_operation="MOP-1")],
			}
		)

	def _patched(self, state="Close Requested", departments=("Returns - T",)):
		return (
			patch(f"{CO}._has_workflow_state", return_value=True),
			patch(f"{CO}.close_hold_departments", return_value=list(departments)),
			patch(
				"frappe.get_all",
				side_effect=_keyed_get_all(
					{
						"Manufacturing Operation": ["PMO-1"],
						"Parent Manufacturing Order": [("PMO-1", state)]
						if state
						else [],
					}
				),
			),
		)

	def test_department_ir_issue_only_to_the_request_department(self):
		p = self._patched()
		with p[0], p[1], p[2]:
			close_open.refuse_department_ir_while_requested(
				self._ir("Issue", "Returns - T")
			)
			with self.assertRaisesRegex(
				frappe.ValidationError, "PMO-1.*Close request.*only to.*Returns - T"
			):
				close_open.refuse_department_ir_while_requested(
					self._ir("Issue", "Waxing - T")
				)
			close_open.refuse_department_ir_while_requested(self._ir("Receive"))

	def test_department_ir_issue_to_any_of_several_request_departments(self):
		p = self._patched(departments=("Returns - T", "Repair - T"))
		with p[0], p[1], p[2]:
			close_open.refuse_department_ir_while_requested(
				self._ir("Issue", "Repair - T")
			)
			for next_department in ("Waxing - T", None):
				with self.subTest(next_department=next_department):
					with self.assertRaisesRegex(
						frappe.ValidationError,
						"only to <strong>Returns - T</strong> or <strong>Repair - T</strong>",
					):
						close_open.refuse_department_ir_while_requested(
							self._ir("Issue", next_department)
						)

	def test_department_ir_issue_is_refused_without_request_departments(self):
		p = self._patched(departments=())
		with p[0], p[1], p[2]:
			with self.assertRaisesRegex(
				frappe.ValidationError, "only to.*a Close / Hold Request Department"
			):
				close_open.refuse_department_ir_while_requested(
					self._ir("Issue", "Returns - T")
				)

	def test_employee_ir_refusal_names_every_request_department(self):
		p = self._patched(departments=("Returns - T", "Repair - T"))
		with p[0], p[1], p[2]:
			with self.assertRaisesRegex(
				frappe.ValidationError,
				"transfer them to <strong>Returns - T</strong> or <strong>Repair - T</strong>",
			):
				close_open.refuse_employee_ir_while_requested(
					self._ir("Issue", table="employee_ir_operations")
				)

	def test_the_request_departments_are_read_in_their_order(self):
		with patch(
			"frappe.get_all", return_value=["Returns - T", "Repair - T"]
		) as get_all:
			self.assertEqual(
				close_open.close_hold_departments(), ["Returns - T", "Repair - T"]
			)
		get_all.assert_called_once_with(
			"PMO Close Hold Department",
			filters={
				"parenttype": "MOP Settings",
				"parent": "MOP Settings",
				"parentfield": "pmo_close_hold_departments",
			},
			order_by="idx asc",
			pluck="department",
		)

	def test_department_ir_of_a_pmo_without_a_request_goes_through(self):
		p = self._patched(state=None)
		with p[0], p[1], p[2]:
			close_open.refuse_department_ir_while_requested(
				self._ir("Issue", "Waxing - T")
			)

	def test_employee_ir_issue_is_refused_receive_goes_through(self):
		p = self._patched(state="Hold Requested")
		with p[0], p[1], p[2]:
			with self.assertRaisesRegex(
				frappe.ValidationError, "PMO-1.*Hold request.*employee"
			):
				close_open.refuse_employee_ir_while_requested(
					self._ir("Issue", table="employee_ir_operations")
				)
			close_open.refuse_employee_ir_while_requested(
				self._ir("Receive", table="employee_ir_operations")
			)

	def test_material_request_of_a_requested_pmo_is_refused(self):
		doc = frappe._dict(name="MR-1", manufacturing_order="PMO-1")
		with (
			patch(f"{CO}.pmo_blocking_state", return_value=None),
			patch(f"{CO}.pmo_request_state", return_value="Close Requested"),
		):
			with self.assertRaisesRegex(
				frappe.ValidationError, "PMO-1.*Close request.*MR-1.*raw material"
			):
				close_open.refuse_material_request_while_blocked(doc)

	def _stock_entry(self, stock_entry_type, **fields):
		return frappe._dict(
			{
				"stock_entry_type": stock_entry_type,
				"manufacturing_order": "PMO-1",
				"manufacturing_work_order": None,
				"manufacturing_operation": None,
				"outgoing_stock_entry": None,
				"items": [],
				"flags": frappe._dict(),
				**fields,
			}
		)

	def _guard_stock_entry(self, doc):
		with (
			patch(f"{CO}._has_workflow_state", return_value=True),
			patch(
				f"{CO}.blocked_request_stock_entry_types",
				return_value={"Material Transfer (WORK ORDER)"},
			),
			patch(
				"frappe.get_all",
				side_effect=_keyed_get_all(
					{
						"Parent Manufacturing Order": [("PMO-1", "Close Requested")],
						"Stock Entry Detail": [],
					}
				),
			),
		):
			close_open.refuse_stock_entry_while_blocked(doc)

	def test_a_blocked_stock_entry_type_is_refused(self):
		with self.assertRaisesRegex(
			frappe.ValidationError,
			"PMO-1.*Close request.*Material Transfer \\(WORK ORDER\\)",
		):
			self._guard_stock_entry(self._stock_entry("Material Transfer (WORK ORDER)"))

	def test_other_types_and_end_transit_receipts_go_through(self):
		self._guard_stock_entry(self._stock_entry("Material Transfer to Department"))
		self._guard_stock_entry(
			self._stock_entry(
				"Material Transfer (WORK ORDER)", outgoing_stock_entry="SE-OUT"
			)
		)

	def test_the_requested_pmos_found_at_validate_are_kept_for_on_submit(self):
		doc = self._stock_entry("Material Transfer to Department")
		self._guard_stock_entry(doc)
		self.assertEqual(doc.flags[close_open.REQUESTED_PMOS_FLAG], ["PMO-1"])
		with patch(f"{CO}.enqueue_ready_check") as ready:
			close_open.queue_ready_check_after_stock_entry(doc)
		ready.assert_called_once_with(["PMO-1"])


class TestHoldForClose(_Base):
	"""The Hold a queued Close starts with, for a user who may Close but not Hold."""

	def test_through_the_workflow_when_the_user_may_hold(self):
		doc = _FakePMO(state="Close Requested")
		with (
			patch(f"{CO}.get_transitions", return_value=[{"action": "Hold"}]),
			patch(f"{CO}.apply_workflow") as apply,
			patch(f"{CO}._hold") as hold,
		):
			close_open._hold_for_close(doc)
		apply.assert_called_once_with(doc, "Hold")
		hold.assert_not_called()
		self.assertNotIn(close_open.INTERNAL_FLAG, doc.flags)

	def test_applied_directly_for_the_order_department(self):
		doc = _FakePMO(state="Close Requested")
		with (
			patch(f"{CO}.get_transitions", return_value=[{"action": "Close"}]),
			patch(f"{CO}.apply_workflow") as apply,
			patch(f"{CO}._hold") as hold,
		):
			close_open._hold_for_close(doc)
		apply.assert_not_called()
		doc.db_set.assert_called_once_with("workflow_state", "Hold")
		hold.assert_called_once_with(doc)
		self.assertEqual(doc.add_comment.call_args.args[0], "Workflow")
		doc.reload.assert_called_once_with()
		self.assertNotIn(close_open.INTERNAL_FLAG, doc.flags)


class TestRequestNotifications(_Base):
	"""Hold, Open, Close and withdrawals alert the departments and the requester (FRD 6)."""

	def test_departments_and_the_requester_are_alerted_never_the_actor(self):
		me = frappe.session.user
		with (
			patch(
				f"{CO}._department_users",
				return_value={"Waxing - T": {"waxer@example.com", me}},
			) as users,
			patch(
				f"{CO}._latest_request",
				return_value=frappe._dict(action_by="order@example.com"),
			),
			patch(f"{CO}.enqueue_create_notification") as notify,
		):
			close_open._notify_pmo(
				_FakePMO(), "PMO-1 is on Hold.", ["Waxing - T"], "Why"
			)
		users.assert_called_once_with(["Waxing - T"])
		recipients, alert = notify.call_args.args
		self.assertEqual(recipients, ["order@example.com", "waxer@example.com"])
		self.assertEqual(alert["subject"], "PMO-1 is on Hold. Reason: Why")
		self.assertEqual(alert["document_name"], "PMO-1")
		self.assertEqual(alert["from_user"], me)

	def test_nobody_to_alert_sends_nothing(self):
		with (
			patch(f"{CO}._department_users", return_value={}),
			patch(f"{CO}._latest_request", return_value=None),
			patch(f"{CO}.enqueue_create_notification") as notify,
		):
			close_open._notify_pmo(_FakePMO(), "PMO-1 is on Hold.", [])
		notify.assert_not_called()

	def test_the_latest_request_row_is_read(self):
		row = frappe._dict(status="Request Close", action_by="order@example.com")
		with patch("frappe.get_all", return_value=[row]) as get_all:
			self.assertIs(close_open._latest_request("PMO-1"), row)
		self.assertEqual(get_all.call_args.args[0], "PMO Status Reason")
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{
				"parenttype": "Parent Manufacturing Order",
				"parent": "PMO-1",
				"parentfield": "status_reasons",
				"status": ["in", ("Request Close", "Request Hold")],
			},
		)
		self.assertEqual(get_all.call_args.kwargs["order_by"], "idx desc")

	def test_holding_departments_come_from_the_current_operations(self):
		docs = _docs(
			mops=[frappe._dict(name="MOP-1", department="Polish - T")],
			mwos=[
				_mwo("MWO-1", pointer="MOP-1", department="Waxing - T"),
				_mwo("MWO-2", department="Waxing - T"),
				_mwo("MWO-3", docstatus=2, department="Gone - T"),
			],
		)
		self.assertEqual(
			close_open._holding_departments(docs), ["Polish - T", "Waxing - T"]
		)

	def _withdraw(self, snapshot, resumed=False):
		pmo = _FakePMO(state="Submitted", snapshot=snapshot)
		docs = _docs(mwos=[_mwo("MWO-1", department="Waxing - T")])
		with (
			patch(f"{CO}._pmo_documents", return_value=docs) as documents,
			patch(f"{CO}._notify_pmo") as notify,
		):
			close_open._withdraw(
				pmo, "Hold Requested", "Customer changed mind", resumed
			)
		documents.assert_called_once_with("PMO-1", "Hold", lock=False)
		return pmo, notify

	def test_a_withdrawal_alerts_and_frees_the_work_orders(self):
		pmo, notify = self._withdraw(None)
		pmo.db_set.assert_not_called()
		subject, departments, reason = notify.call_args.args[1:]
		self.assertIn("can move freely again", subject)
		self.assertEqual(departments, ["Waxing - T"])
		self.assertEqual(reason, "Customer changed mind")

	def test_withdrawing_a_hold_request_resumes_the_close_request(self):
		pmo, notify = self._withdraw({close_open.RESUME_CLOSE_KEY: 1}, resumed=True)
		self.assertIsNone(_stored_snapshot(pmo))
		self.assertIn("Close request continues", notify.call_args.args[1])
		self.assertIn("Close request continues", pmo.add_comment.call_args.args[1])

	def test_a_hold_request_during_a_close_request_keeps_it(self):
		pmo = _FakePMO(state="Hold Requested")
		with (
			patch(
				f"{CO}._request_plan",
				return_value={"departments": ["Returns - T"], "pending": []},
			),
			patch(f"{CO}._notify_request"),
		):
			close_open._request(
				pmo, "Request Hold", "Stone shortage", "Close Requested"
			)
		self.assertEqual(_stored_snapshot(pmo), {close_open.RESUME_CLOSE_KEY: 1})

	def test_a_plain_request_stores_nothing(self):
		pmo = _FakePMO(state="Close Requested")
		with (
			patch(
				f"{CO}._request_plan",
				return_value={"departments": ["Returns - T"], "pending": []},
			),
			patch(f"{CO}._notify_request"),
		):
			close_open._request(pmo, "Request Close", "Customer cancelled", "Submitted")
		pmo.db_set.assert_not_called()


class TestReadyCheck(_Base):
	"""The requester hears when a requested PMO's work is back (FRD 6, transfer completed)."""

	def _notify_ready(self, problems, request):
		with (
			patch(
				f"{CO}.requested_pmos",
				return_value={"PMO-1": "Close Requested", "PMO-2": "Hold Requested"},
			),
			patch(f"{CO}._pmo_documents", return_value=_docs()) as documents,
			patch(f"{CO}._problem_list", side_effect=lambda n, *a, **k: problems[n]),
			patch(f"{CO}._latest_request", return_value=request),
			patch(f"{CO}.enqueue_create_notification") as notify,
		):
			close_open.notify_ready_requests(["PMO-1", "PMO-2"])
		return documents, notify

	def test_only_a_pmo_with_nothing_left_alerts_its_requester_once(self):
		request = frappe._dict(
			action_by="order@example.com", action_on="2026-10-29 12:00:00"
		)
		documents, notify = self._notify_ready(
			{"PMO-1": [], "PMO-2": [("department", "elsewhere")]}, request
		)
		self.assertEqual(
			documents.call_args_list,
			[call("PMO-1", "Close", lock=False), call("PMO-2", "Hold", lock=False)],
		)
		(alert_call,) = notify.call_args_list
		self.assertEqual(alert_call.args[0], "order@example.com")
		alert = alert_call.args[1]
		self.assertEqual(alert["document_name"], "PMO-1")
		self.assertIn("PMO-1 is ready to Close", alert["subject"])
		self.assertIn("2026-10-29 12:00:00", alert["subject"])
		self.assertEqual(
			alert_call.kwargs["dedupe_on"],
			["document_type", "document_name", "subject"],
		)

	def test_no_requester_no_alert(self):
		_documents, notify = self._notify_ready({"PMO-1": [], "PMO-2": []}, None)
		notify.assert_not_called()

	def test_queued_after_commit_once_per_pmo(self):
		with patch("frappe.enqueue") as enqueue:
			close_open.enqueue_ready_check([None, "PMO-1", "", "PMO-1"])
			close_open.enqueue_ready_check([])
		enqueue.assert_called_once_with(
			close_open.notify_ready_requests,
			queue="short",
			enqueue_after_commit=True,
			pmo_names=["PMO-1"],
		)

	def test_an_ir_submit_queues_its_requested_pmos(self):
		doc = frappe._dict(department_ir_operation=[])
		with (
			patch(f"{CO}._row_pmos", return_value={"PMO-1", "PMO-9"}) as rows,
			patch(
				f"{CO}.requested_pmos", return_value={"PMO-1": "Close Requested"}
			) as requested,
			patch(f"{CO}.enqueue_ready_check") as ready,
		):
			close_open.queue_ready_check_after_transfer(doc, "department_ir_operation")
		rows.assert_called_once_with(doc, "department_ir_operation")
		requested.assert_called_once_with({"PMO-1", "PMO-9"})
		ready.assert_called_once_with({"PMO-1": "Close Requested"})


class TestWorkflowPatch(_Base):
	"""The workflow patch's transitions agree with the flow (FRD 3.5 conditions)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from jewellery_erpnext.patches import create_pmo_close_open_workflow

		cls.wf = create_pmo_close_open_workflow

	def test_every_reason_action_is_recognised_by_the_flow(self):
		for state, action, next_state, _allowed, *_condition in self.wf.TRANSITIONS:
			if action not in close_open.REASON_ACTIONS:
				continue
			with self.subTest(state=state, action=action, next_state=next_state):
				self.assertEqual(close_open._action_of(state, next_state), action)

	def test_exactly_one_open_and_withdraw_passes_for_each_snapshot(self):
		self.assertEqual(self.wf.RESUME_KEY, close_open.RESUME_CLOSE_KEY)
		snapshots = {
			"Close Requested": json.dumps(
				{close_open.RESUME_CLOSE_KEY: 1, "mwo": {}}, sort_keys=True
			),
			"Submitted": json.dumps({"mwo": {"MWO-1": "Not Started"}}, sort_keys=True),
			"Submitted ": None,
		}
		for state, action in (("Hold", "Open"), ("Hold Requested", "Withdraw Request")):
			rows = [
				t
				for t in self.wf.TRANSITIONS
				if t[0] == state and t[1] == action and t[3] == self.wf.HOLD_TEAM
			]
			for expected, snapshot in snapshots.items():
				with self.subTest(state=state, snapshot=snapshot):
					doc = frappe._dict(hold_snapshot=snapshot)
					passing = [
						t[2] for t in rows if frappe.safe_eval(t[4], None, {"doc": doc})
					]
					self.assertEqual(passing, [expected.strip()])

	def test_the_hold_team_may_request_hold_during_a_close_request(self):
		self.assertIn(
			("Close Requested", "Request Hold", "Hold Requested", self.wf.HOLD_TEAM),
			self.wf.TRANSITIONS,
		)
