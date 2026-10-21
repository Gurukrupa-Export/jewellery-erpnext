"""
Mock-level tests for the Parent Manufacturing Order Close / Open flow (PR #1491).

* a draft Department IR / Employee IR naming a Closed work order's operation is refused on save
  AND on submit (Frappe sets docstatus 1 before validate on every submit path);
* a draft work order closed along with its PMO cannot be submitted while the PMO is Closed;
* the Close locks its work orders (then their current operations) NOWAIT and decides from the
  locked rows, so an IR submit in flight cannot slip a live operation under a Closed work order;
* a Close Reason is stored only on a PMO the Close can start from.

Every DB read goes through a patched module helper or a doctype-keyed ``frappe.get_all`` stub that
passes other doctypes through (an unkeyed stub catches the translation cache's own read).

Run with:
  bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_pmo_close_open
"""

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

GUARD = "jewellery_erpnext.jewellery_erpnext.doc_events.mwo_closed_guard"
MWO_MOD = "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order"
CO = "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.close_open"


def _keyed_get_all(tables):
	"""``frappe.get_all`` that answers for the doctypes in ``tables`` and passes the rest on."""
	real = frappe.get_all

	def fake(doctype, *args, **kwargs):
		if doctype in tables:
			return tables[doctype]
		return real(doctype, *args, **kwargs)

	return fake


def _mop(name, mwo, status, department_ir_status=None):
	return frappe._dict(
		name=name,
		manufacturing_work_order=mwo,
		status=status,
		department_ir_status=department_ir_status,
	)


def _locked_mwo(name, status, docstatus=1, pointer=None):
	return frappe._dict(
		name=name, status=status, docstatus=docstatus, manufacturing_operation=pointer
	)


class _Base(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		# Build the translation cache before any frappe.get_all stub is active.
		frappe._("Work Order Closed")


class TestIRGuardChecksSubmitToo(_Base):
	def _ir(self, docstatus):
		return frappe._dict(
			docstatus=docstatus,
			department_ir_operation=[
				frappe._dict(idx=1, manufacturing_operation="MOP-1")
			],
		)

	@patch(f"{GUARD}.closed_mwo_operations", return_value={"MOP-1": "MWO-1"})
	def test_a_draft_save_is_refused(self, _closed):
		with self.assertRaisesRegex(frappe.ValidationError, "which is Closed"):
			block_closed_mwo_operations(self._ir(0), "department_ir_operation")

	@patch(f"{GUARD}.closed_mwo_operations", return_value={"MOP-1": "MWO-1"})
	def test_a_submit_is_refused(self, _closed):
		"""A submit reaches validate with docstatus 1 already set."""
		with self.assertRaisesRegex(frappe.ValidationError, "which is Closed"):
			block_closed_mwo_operations(self._ir(1), "department_ir_operation")

	@patch(f"{GUARD}.closed_mwo_operations", return_value={"MOP-1": "MWO-1"})
	def test_a_cancelled_document_is_left_alone(self, closed):
		block_closed_mwo_operations(self._ir(2), "department_ir_operation")
		closed.assert_not_called()

	@patch(f"{GUARD}.closed_mwo_operations", return_value={})
	def test_an_open_work_order_passes(self, _closed):
		block_closed_mwo_operations(self._ir(1), "department_ir_operation")


class TestWorkOrderSubmitUnderClosedPmo(_Base):
	def _mwo(self):
		return frappe._dict(
			manufacturing_order="PMO-1",
			is_without_so=0,
			validate_photoshop_images=MagicMock(),
		)

	@patch(f"{MWO_MOD}.is_pmo_closed", return_value=True)
	def test_submit_is_refused_while_the_pmo_is_closed(self, closed):
		mwo = self._mwo()
		with self.assertRaisesRegex(frappe.ValidationError, "PMO-1.*is Closed"):
			ManufacturingWorkOrder.before_submit(mwo)
		closed.assert_called_once_with("PMO-1")
		mwo.validate_photoshop_images.assert_not_called()

	@patch(f"{MWO_MOD}.is_pmo_closed", return_value=False)
	def test_submit_goes_on_when_the_pmo_is_open(self, _closed):
		mwo = self._mwo()
		ManufacturingWorkOrder.before_submit(mwo)
		mwo.validate_photoshop_images.assert_called_once_with()


class TestIsPmoClosed(_Base):
	"""Both site states: with the Close/Open workflow (and its workflow_state field) and without
	it (a fresh install only marks the workflow patch as done)."""

	@patch(f"{CO}._stored_state")
	@patch(f"{CO}._has_workflow_state", return_value=False)
	def test_a_site_without_the_workflow_has_no_closed_pmo(self, _has, stored):
		self.assertFalse(close_open.is_pmo_closed("PMO-1"))
		stored.assert_not_called()

	@patch(f"{CO}._stored_state")
	@patch(f"{CO}._has_workflow_state", return_value=True)
	def test_reads_the_stored_state(self, _has, stored):
		cases = [
			(frappe._dict(docstatus=1, workflow_state="Closed"), True),
			(frappe._dict(docstatus=1, workflow_state="Open"), False),
			(frappe._dict(docstatus=1, workflow_state="Submitted"), False),
			(None, False),
		]
		for state, expected in cases:
			with self.subTest(state=state):
				stored.return_value = state
				self.assertIs(close_open.is_pmo_closed("PMO-1"), expected)

	@patch(f"{CO}._stored_state")
	@patch(f"{CO}._has_workflow_state", return_value=True)
	def test_a_work_order_without_a_pmo_is_not_looked_up(self, _has, stored):
		self.assertFalse(close_open.is_pmo_closed(None))
		stored.assert_not_called()


@patch("frappe.get_roles", return_value=["System Manager"])
class TestSaveCloseReason(_Base):
	def test_refused_unless_the_close_can_start_from_the_state(self, _roles):
		for docstatus, state in ((0, "Draft"), (1, "Closed"), (2, "Cancelled")):
			with self.subTest(state=state):
				with (
					patch(
						f"{CO}._stored_state",
						return_value=frappe._dict(
							docstatus=docstatus, workflow_state=state
						),
					),
					patch(f"{CO}.frappe.db.set_value") as set_value,
				):
					with self.assertRaisesRegex(
						frappe.ValidationError, "not already Closed"
					):
						close_open.save_close_reason("PMO-1", "Customer cancelled")
					set_value.assert_not_called()

	def test_stored_on_a_state_the_close_starts_from(self, _roles):
		for state in ("Submitted", "Open"):
			with self.subTest(state=state):
				with (
					patch(
						f"{CO}._stored_state",
						return_value=frappe._dict(docstatus=1, workflow_state=state),
					),
					patch(f"{CO}.frappe.db.set_value") as set_value,
				):
					close_open.save_close_reason("PMO-1", "  Customer cancelled ")
					set_value.assert_called_once_with(
						"Parent Manufacturing Order",
						"PMO-1",
						"close_reason",
						"Customer cancelled",
						update_modified=False,
					)

	@patch(f"{CO}._stored_state", return_value=None)
	def test_a_missing_pmo_is_refused(self, _stored, _roles):
		with self.assertRaises(frappe.DoesNotExistError):
			close_open.save_close_reason("PMO-X", "Customer cancelled")

	@patch(f"{CO}._stored_state")
	def test_a_blank_reason_is_refused_first(self, stored, _roles):
		with self.assertRaisesRegex(frappe.ValidationError, "Close Reason is required"):
			close_open.save_close_reason("PMO-1", "   ")
		stored.assert_not_called()

	def test_only_a_system_manager(self, roles):
		roles.return_value = ["Manufacturing User"]
		with self.assertRaises(frappe.PermissionError):
			close_open.save_close_reason("PMO-1", "Customer cancelled")


class TestCloseLocksBeforeItDecides(_Base):
	SNAPSHOT = [
		frappe._dict(name="MWO-1", manufacturing_operation="MOP-1"),
		frappe._dict(name="MWO-2", manufacturing_operation=None),
	]

	def _busy(self, cls, row):
		exc = cls("busy")
		exc.lock_row_name = row
		return exc

	def test_locks_work_orders_then_both_pointers_nowait(self):
		locked = {
			"MWO-1": _locked_mwo("MWO-1", "In Process", pointer="MOP-NEW"),
			"MWO-2": _locked_mwo("MWO-2", "Draft", docstatus=0),
		}
		order = MagicMock()
		order.lock_work_orders.return_value = locked
		order.lock_manufacturing_operations.return_value = {}
		with (
			patch(
				"frappe.get_all",
				side_effect=_keyed_get_all({"Manufacturing Work Order": self.SNAPSHOT}),
			),
			patch(f"{CO}.lock_work_orders", order.lock_work_orders),
			patch(
				f"{CO}.lock_manufacturing_operations",
				order.lock_manufacturing_operations,
			),
		):
			self.assertEqual(close_open._lock_for_close("PMO-1"), (locked, {}))
		self.assertEqual(
			order.mock_calls,
			[
				call.lock_work_orders(["MWO-1", "MWO-2"], wait=0),
				call.lock_manufacturing_operations({"MOP-1", "MOP-NEW"}, wait=0),
			],
		)

	def test_a_busy_row_refuses_the_close(self):
		for target, cls, row in (
			("lock_work_orders", frappe.QueryTimeoutError, "MWO-2"),
			("lock_manufacturing_operations", frappe.QueryDeadlockError, "MOP-1"),
		):
			with self.subTest(target=target):
				with (
					patch(
						"frappe.get_all",
						side_effect=_keyed_get_all(
							{"Manufacturing Work Order": self.SNAPSHOT}
						),
					),
					patch(f"{CO}.lock_work_orders", return_value={}),
					patch(f"{CO}.lock_manufacturing_operations", return_value={}),
					patch(f"{CO}.{target}", side_effect=self._busy(cls, row)),
				):
					with self.assertRaisesRegex(
						frappe.ValidationError, f"{row}.*Try closing again"
					):
						close_open._lock_for_close("PMO-1")


class TestInProgressCheck(_Base):
	def _check(self, plain_open, fresh=None):
		with patch(
			"frappe.get_all",
			side_effect=_keyed_get_all(
				{
					"Manufacturing Work Order": ["MWO-1"],
					"Manufacturing Operation": plain_open,
				}
			),
		):
			close_open.block_close_while_operations_in_progress("PMO-1", fresh)

	def test_a_fresh_wip_pointer_overrides_a_stale_not_started(self):
		with self.assertRaisesRegex(frappe.ValidationError, "MOP-1 \\(MWO-1\\)"):
			self._check([], {"MOP-1": _mop("MOP-1", "MWO-1", "WIP")})

	def test_a_fresh_in_transit_pointer_blocks(self):
		with self.assertRaisesRegex(frappe.ValidationError, "MOP-1"):
			self._check([], {"MOP-1": _mop("MOP-1", "MWO-1", "Finished", "In-Transit")})

	def test_a_fresh_finished_pointer_clears_a_stale_wip(self):
		self._check(
			[_mop("MOP-1", "MWO-1", "WIP")],
			{"MOP-1": _mop("MOP-1", "MWO-1", "Finished", "Received")},
		)

	def test_a_fresh_row_of_another_work_order_is_ignored(self):
		self._check([], {"MOP-9": _mop("MOP-9", "MWO-OTHER", "WIP")})

	def test_the_dry_run_decides_from_the_plain_read(self):
		with self.assertRaisesRegex(frappe.ValidationError, "MOP-1"):
			self._check([_mop("MOP-1", "MWO-1", "WIP")])

	def test_nothing_open_passes(self):
		self._check([], {"MOP-1": _mop("MOP-1", "MWO-1", "Not Started")})


class TestCloseWritesFromLockedRows(_Base):
	@patch(f"{CO}.frappe.db.set_value")
	def test_keeps_the_locked_status_and_skips_finished_work_orders(self, set_value):
		close_open.close_related_mwos(
			{
				"MWO-1": _locked_mwo("MWO-1", "In Process"),
				"MWO-2": _locked_mwo("MWO-2", "Draft", docstatus=0),
				"MWO-3": _locked_mwo("MWO-3", "Completed"),
				"MWO-4": _locked_mwo("MWO-4", "Not Started", docstatus=2),
			}
		)
		self.assertEqual(
			set_value.call_args_list,
			[
				call(
					"Manufacturing Work Order",
					"MWO-1",
					{"status_before_close": "In Process", "status": "Closed"},
					update_modified=False,
				),
				call(
					"Manufacturing Work Order",
					"MWO-2",
					{"status_before_close": "Draft", "status": "Closed"},
					update_modified=False,
				),
			],
		)


class TestCloseOrder(_Base):
	def _pmo(self, reason="Customer cancelled"):
		return frappe._dict(
			name="PMO-1",
			close_reason=reason,
			workflow_state="Closed",
			get_doc_before_save=lambda: frappe._dict(workflow_state="Submitted"),
		)

	def test_locks_then_checks_then_writes(self):
		flow = MagicMock()
		flow._lock_for_close.return_value = ("MWO-ROWS", "MOP-ROWS")
		with (
			patch(f"{CO}._require_system_manager"),
			patch(f"{CO}._lock_for_close", flow._lock_for_close),
			patch(
				f"{CO}.block_close_while_operations_in_progress",
				flow.block_close_while_operations_in_progress,
			),
			patch(f"{CO}.close_related_mwos", flow.close_related_mwos),
		):
			close_open.on_workflow_state_change(self._pmo())
		self.assertEqual(
			flow.mock_calls,
			[
				call._lock_for_close("PMO-1"),
				call.block_close_while_operations_in_progress(
					"PMO-1", fresh_operations="MOP-ROWS"
				),
				call.close_related_mwos("MWO-ROWS"),
			],
		)

	def test_a_missing_reason_is_refused_before_any_lock(self):
		with (
			patch(f"{CO}._require_system_manager"),
			patch(f"{CO}._lock_for_close") as lock,
		):
			with self.assertRaisesRegex(
				frappe.ValidationError, "Close Reason is required"
			):
				close_open.on_workflow_state_change(self._pmo(reason=" "))
		lock.assert_not_called()
