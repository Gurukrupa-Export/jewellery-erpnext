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
		self, name="PMO-1", state="Submitted", before=None, snapshot=None, **fields
	):
		super().__init__(
			name=name,
			workflow_state=state,
			hold_snapshot=json.dumps(snapshot) if snapshot else None,
			**fields,
		)
		self.db_set = MagicMock()
		self.add_comment = MagicMock()
		self.reload = MagicMock()
		self.get_doc_before_save = lambda: before


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

	@patch(f"{MWO_MOD}.pmo_blocking_state", return_value=None)
	def test_submit_goes_on_when_the_pmo_is_open(self, _blocking):
		mwo = self._mwo()
		ManufacturingWorkOrder.before_submit(mwo)
		mwo.validate_photoshop_images.assert_called_once_with()


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
	PAIRS = (
		("Submitted", "Hold", "_hold"),
		("Draft", "Hold (Draft)", "_hold"),
		("Submitted", "Closed", "_close"),
		("Hold", "Closed", "_close"),
		("Draft", "Closed (Draft)", "_close"),
		("Hold (Draft)", "Closed (Draft)", "_close"),
		("Hold", "Submitted", "_open"),
		("Hold (Draft)", "Submitted", "_open"),
		("Draft", "Submitted", None),
		("Submitted", "Submitted", None),
	)

	def test_each_transition_runs_its_action(self):
		for previous, current, expected in self.PAIRS:
			with self.subTest(previous=previous, current=current):
				pmo = _FakePMO(
					state=current, before=frappe._dict(workflow_state=previous)
				)
				with (
					patch(f"{CO}._hold") as hold,
					patch(f"{CO}._close") as close,
					patch(f"{CO}._open") as open_,
				):
					close_open.on_workflow_state_change(pmo)
				ran = {"_hold": hold, "_close": close, "_open": open_}
				for name, mock in ran.items():
					if name == expected:
						mock.assert_called_once_with(pmo)
					else:
						mock.assert_not_called()

	def test_an_insert_does_nothing(self):
		pmo = _FakePMO(state="Draft", before=None)
		with patch(f"{CO}._pmo_documents") as documents:
			close_open.on_workflow_state_change(pmo)
		documents.assert_not_called()


class TestHold(_Base):
	def _hold(self, docs):
		pmo = _FakePMO(state="Hold")
		with (
			patch(f"{CO}._pmo_documents", return_value=docs) as documents,
			patch(f"{CO}._refuse_unless_ready") as refuse,
			patch(f"{CO}.set_values_in_bulk") as bulk,
		):
			close_open._hold(pmo)
		documents.assert_called_once_with("PMO-1", "Hold")
		refuse.assert_called_once_with("PMO-1", "Hold", docs)
		return pmo, bulk

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
		):
			close_open._open(pmo)
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


class TestClose(_Base):
	def test_a_reason_is_required_before_anything_is_locked(self):
		pmo = _FakePMO(state="Closed", close_reason=" ")
		with patch(f"{CO}._pmo_documents") as documents:
			with self.assertRaisesRegex(
				frappe.ValidationError, "Close Reason is required"
			):
				close_open._close(pmo)
		documents.assert_not_called()

	def test_closes_work_orders_finishes_waiting_operations_and_closes_requests(self):
		pmo = _FakePMO(
			state="Closed", close_reason="Customer cancelled", snapshot={"mwo": {}}
		)
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
		):
			close_open._close(pmo)
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
		self.assertIn("Customer cancelled", pmo.add_comment.call_args.args[1])


class TestProblems(_Base):
	DEPARTMENTS = [
		(MPM, "Manufacturing Plan & Management"),
		("Waxing - T", "Waxing"),
	]

	def _problems(self, action, docs, busy=(), moved=(), stock=True):
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

	def test_save_close_reason_needs_the_close_transition(self):
		doc = _FakePMO(state="Closed")
		with (
			patch("frappe.get_doc", return_value=doc),
			patch(f"{CO}.get_transitions", return_value=[]),
			patch("frappe.db.set_value") as set_value,
		):
			with self.assertRaises(frappe.PermissionError):
				close_open.save_close_reason("PMO-1", "Customer cancelled")
		set_value.assert_not_called()

	def test_save_close_reason_stores_a_stripped_reason(self):
		doc = _FakePMO(state="Submitted")
		with (
			patch("frappe.get_doc", return_value=doc),
			patch(f"{CO}.get_transitions", return_value=[{"action": "Close"}]),
			patch("frappe.db.set_value") as set_value,
		):
			close_open.save_close_reason("PMO-1", "  Customer cancelled ")
		set_value.assert_called_once_with(
			"Parent Manufacturing Order",
			"PMO-1",
			"close_reason",
			"Customer cancelled",
			update_modified=False,
		)

	def test_a_blank_reason_is_refused_first(self):
		with patch("frappe.get_doc") as get_doc:
			with self.assertRaisesRegex(
				frappe.ValidationError, "Close Reason is required"
			):
				close_open.save_close_reason("PMO-1", "  ")
		get_doc.assert_not_called()


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
		with p[0], p[1], p[2], p[3], p[4] as refuse, p[5], patch(
			"frappe.get_all", side_effect=_keyed_get_all({"Stock Entry": details})
		):
			result = close_open.check_action("PMO-1", "Close")
		refuse.assert_called_once()
		self.assertEqual(refuse.call_args.kwargs, {"stock": False})
		self.assertEqual([e.name for e in result["entries"]], ["SE-3", "SE-2"])
		self.assertEqual(result["stopped"], ["MR-2"])

	def test_enqueue_close_refuses_when_there_is_nothing_to_cancel(self):
		doc = _FakePMO(state="Submitted")
		p = self._patches(doc, frappe._dict(entries=[], stopped=[]))
		with p[0], p[1], p[2], p[3], p[4], p[5], patch("frappe.enqueue") as enqueue:
			with self.assertRaisesRegex(
				frappe.ValidationError, "no Stock Entries to cancel"
			):
				close_open.enqueue_close("PMO-1", "Customer cancelled")
		enqueue.assert_not_called()

	def test_enqueue_close_holds_a_submitted_pmo_and_queues_the_job(self):
		doc = _FakePMO(state="Submitted")
		p = self._patches(doc, self.PLAN)
		with (
			p[0],
			p[1],
			p[2],
			p[3],
			p[4],
			p[5],
			patch(f"{CO}._check_close_rights") as rights,
			patch(f"{CO}.apply_workflow") as apply,
			patch("frappe.db.set_value") as set_value,
			patch("frappe.enqueue") as enqueue,
			patch("frappe.msgprint"),
		):
			close_open.enqueue_close("PMO-1", " Customer cancelled ")
		rights.assert_called_once_with(self.PLAN)
		set_value.assert_called_once_with(
			"Parent Manufacturing Order",
			"PMO-1",
			"close_reason",
			"Customer cancelled",
			update_modified=False,
		)
		apply.assert_called_once_with(doc, "Hold")
		close = _stored_snapshot(doc)["close"]
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
			patch(f"{CO}._check_close_rights"),
			patch(f"{CO}.apply_workflow") as apply,
			patch("frappe.db.set_value"),
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
		doc = _FakePMO(state="Hold")
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
		with patch(f"{CO}.pmo_blocking_state", return_value=None):
			close_open.refuse_material_request_while_blocked(doc)

	def _stock_entry(self, **fields):
		return frappe._dict(
			{
				"manufacturing_order": None,
				"manufacturing_work_order": None,
				"manufacturing_operation": None,
				"outgoing_stock_entry": None,
				"items": [],
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
		frappe._dict(fieldname="close_reason", fieldtype="Small Text"),
		frappe._dict(fieldname="item_code", fieldtype="Link"),
		frappe._dict(fieldname="section", fieldtype="Section Break"),
		frappe._dict(fieldname="metal_product_tolerance", fieldtype="Table"),
	]

	def _pmo(
		self, action="save", before_state="Draft", state="Hold (Draft)", **changes
	):
		before = frappe._dict(
			workflow_state=before_state,
			close_reason=None,
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
		self.assertTrue(
			ParentManufacturingOrder._state_change_only(
				self._pmo(
					action="update_after_submit",
					before_state="Submitted",
					state="Hold",
					close_reason="x",
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
