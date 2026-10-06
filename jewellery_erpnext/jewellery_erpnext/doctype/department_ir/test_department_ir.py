# # Copyright (c) 2026, Nirali and Contributors
# # See license.txt
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.types.frappedict import _dict as FrappeDict

from jewellery_erpnext.jewellery_erpnext.doc_events import (
	current_operation_guard as guard,
)
from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir import (
	DepartmentIR,
	add_time_log_optimize,
	department_receive_query,
	fetch_and_update,
	get_manufacturing_operations,
)
from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.department_ir_utils import (
	WEIGHT_FIELDS,
	validate_and_update_gross_wt_from_mop,
)
from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.product_tolerance import (
	get_tolerance_failures,
	validate_product_tolerance,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.test_manufacturing_operation import (
	dir_for_issue,
	dir_for_receive,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.test_manufacturing_work_order import (
	create_pmo,
)


class FakeDepartmentIR(FrappeDict):
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		if "department_ir_operation" not in self:
			self.department_ir_operation = []

		self._pmo_processed_for_dept = set()

	def append(self, key, value):
		self[key].append(FrappeDict(value))

	def on_submit_issue_new(self, cancel=False):
		DepartmentIR.on_submit_issue_new(self, cancel)

	def on_submit_receive(self, cancel=False):
		DepartmentIR.on_submit_receive(self, cancel)

	def validate_receive_lineage(self):
		DepartmentIR.validate_receive_lineage(self)


class TestDepartmentIR(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		cls.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.get_datetime",
		return_value="2026-01-01 12:00:00",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.create_operation_for_next_dept"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.create_mop_log_for_department_ir"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.add_time_log"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.set_value"
	)
	def test_on_submit_issue_creates_mop_log_and_transitions(
		self,
		mock_set_val,
		mock_add_time,
		mock_create_mop,
		mock_create_op,
		mock_get_doc,
		mock_get_val,
		mock_datetime,
	):
		doc = FakeDepartmentIR(
			doctype="Department IR",
			name="DIR-ISS-001",
			type="Issue",
			current_department="Dept A",
			next_department="Dept B",
		)

		doc.append(
			"department_ir_operation",
			{
				"manufacturing_operation": "MOP-CURRENT",
				"manufacturing_work_order": "MWO-1",
			},
		)

		# Mocking warehouse fetch
		def get_val_side_effect(dt, filters=None, fieldname=None, as_dict=False):
			if fieldname == "default_in_transit_warehouse":
				return "Transit-WH"
			return "Dept-WH"

		mock_get_val.side_effect = get_val_side_effect

		# Mock new operation
		mock_create_op.return_value = FrappeDict(name="MOP-NEW")

		doc.on_submit_issue_new(cancel=False)

		# Verify MOP transition
		mock_set_val.assert_any_call(
			"Manufacturing Operation", "MOP-CURRENT", "status", "Finished"
		)
		mock_create_op.assert_called_once_with(
			"DIR-ISS-001", "MWO-1", "MOP-CURRENT", "Dept B"
		)

		# Verify MOP Log Generation
		self.assertTrue(mock_create_mop.called)
		args, kwargs = mock_create_mop.call_args
		self.assertEqual(args[0].name, "DIR-ISS-001")
		self.assertEqual(args[2], "Transit-WH")  # In transit is source for issue log
		self.assertEqual(args[4], "MOP-NEW")

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.get_datetime",
		return_value="2026-01-01 12:00:00",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.get_value",
		return_value="Test WH",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.get_value",
		return_value="Test WH",
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.get_doc"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.create_mop_log_for_department_ir"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.set_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.add_time_log"
	)
	def test_on_submit_receive_marks_received_and_logs(
		self,
		mock_add_time,
		mock_set_val,
		mock_create_mop,
		mock_get_doc,
		mock_gv1,
		mock_gv2,
		mock_datetime,
	):
		doc = FakeDepartmentIR(
			doctype="Department IR",
			name="DIR-REC-001",
			type="Receive",
			current_department="Dept B",
			receive_against="DIR-ISS-001",
		)

		doc.append(
			"department_ir_operation",
			{
				"manufacturing_operation": "MOP-NEW",
				"manufacturing_work_order": "MWO-1",
			},
		)

		doc.on_submit_receive(cancel=False)

		# Verify status updates
		args, kwargs = mock_set_val.call_args_list[0]
		self.assertEqual(args[0], "Manufacturing Operation")
		self.assertEqual(args[1], "MOP-NEW")
		self.assertEqual(args[2]["department_receive_id"], "DIR-REC-001")
		self.assertEqual(args[2]["department_ir_status"], "Received")

		# Verify MOP Log Generation
		mock_create_mop.assert_called_once()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.get_value"
	)
	def test_validate_receive_lineage_blocks_invalid_parents(self, mock_get_value):
		doc = FakeDepartmentIR(
			doctype="Department IR",
			name="DIR-REC-001",
			type="Receive",
			receive_against="DIR-ISS-BAD",
		)

		doc.append("department_ir_operation", {"manufacturing_operation": "MOP-ORPHAN"})

		# Simulate a receive_against document that is in Draft (0) not Submitted (1)
		mock_get_value.return_value = FrappeDict(docstatus=0, type="Issue")

		with self.assertRaises(frappe.ValidationError) as context:
			doc.validate_receive_lineage()

		self.assertIn("must be a submitted Department IR", str(context.exception))

	def test_department_ir_scan(self):
		create_pmo(self)
		mo = mo_creation()
		dir_issue = dir_for_issue(
			"Manufacturing Plan & Management - T", "Waxing - T", mo
		)
		for row in dir_issue.department_ir_operation:
			self.assertEqual(row.manufacturing_work_order, mo.manufacturing_work_order)
			self.assertEqual(row.manufacturing_operation, mo.name)
			self.assertEqual(row.parent_manufacturing_order, mo.manufacturing_order)
		mo.reload()

		self.assertEqual("Finished", mo.status)

		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		self.assertIsNotNone(mo_wax.department_issue_id)
		self.assertEqual(mo_wax.department_issue_id, dir_issue.name)

		dir_receive = dir_for_receive(dir_issue)
		for row in dir_receive.department_ir_operation:
			self.assertEqual(row.gross_wt, mo.gross_wt)
			self.assertEqual(
				row.manufacturing_work_order, mo_wax.manufacturing_work_order
			)
			self.assertEqual(row.manufacturing_operation, mo_wax.name)
			self.assertEqual(row.parent_manufacturing_order, mo_wax.manufacturing_order)
		mo_wax.reload()
		self.assertIsNotNone(mo_wax.department_receive_id)
		self.assertEqual(mo_wax.department_receive_id, dir_receive.name)

	def test_department_ir_by_manufacturing_operation(self):
		create_pmo(self)
		mo = mo_creation()
		dir_issue = frappe.new_doc("Department IR")
		dir_issue.company = "Test_Company"
		dir_issue.manufacturer = "Shubh"
		dir_issue.current_department = "Manufacturing Plan & Management - T"
		dir_issue.next_department = "Waxing - T"
		dir_issue = get_manufacturing_operations(mo.name, dir_issue)
		dir_issue.save()
		dir_issue.submit()

		for row in dir_issue.department_ir_operation:
			self.assertEqual(row.manufacturing_work_order, mo.manufacturing_work_order)
			self.assertEqual(row.manufacturing_operation, mo.name)
			self.assertEqual(row.parent_manufacturing_order, mo.manufacturing_order)
		mo.reload()

		self.assertEqual("Finished", mo.status)

		mo_wax = frappe.get_last_doc("Manufacturing Operation")
		self.assertIsNotNone(mo_wax.department_issue_id)
		self.assertEqual(mo_wax.department_issue_id, dir_issue.name)

		dir_receive = dir_for_receive(dir_issue)
		for row in dir_receive.department_ir_operation:
			self.assertEqual(
				row.manufacturing_work_order, mo_wax.manufacturing_work_order
			)
			self.assertEqual(row.manufacturing_operation, mo_wax.name)
			self.assertEqual(row.parent_manufacturing_order, mo_wax.manufacturing_order)
		mo_wax.reload()
		self.assertIsNotNone(mo_wax.department_receive_id)
		self.assertEqual(mo_wax.department_receive_id, dir_receive.name)

	def test_department_receive_query_no_match_returns_empty(self):
		res = department_receive_query(
			"Department IR",
			"non-existent-xyz",
			"name",
			0,
			20,
			{"current_department": "", "next_department": ""},
		)
		self.assertEqual(res, [])

	def test_add_time_log_optimize_updates_and_inserts_time_log(self):
		mop = frappe.new_doc("Manufacturing Operation")
		mop.department = "Manufacturing Plan & Management - T"
		mop.insert()

		add_time_log_optimize(
			mop.name, {"status": "WIP", "start_time": frappe.utils.now()}
		)

		status = frappe.db.get_value("Manufacturing Operation", mop.name, "status")
		self.assertEqual(status, "WIP")

		started_time = frappe.db.get_value(
			"Manufacturing Operation", mop.name, "started_time"
		)
		self.assertIsNotNone(started_time, "started_time should be set")

		time_logs = frappe.db.get_all(
			"Manufacturing Operation Time Log",
			filters={"parent": mop.name},
			pluck="name",
		)
		self.assertTrue(len(time_logs) >= 1)

	def test_get_manufacturing_operations_does_not_duplicate(self):
		create_pmo(self)
		mo = mo_creation()
		dir_issue = frappe.new_doc("Department IR")
		dir_issue.manufacturer = "Shubh"
		dir_issue.current_department = "Manufacturing Plan & Management - T"
		dir_issue.next_department = "Waxing - GEPL"

		dir_issue.append(
			"department_ir_operation",
			{
				"manufacturing_operation": mo.name,
				"manufacturing_work_order": mo.manufacturing_work_order,
			},
		)

		updated = get_manufacturing_operations(mo.name, dir_issue)
		entries = [
			r
			for r in updated.department_ir_operation
			if r.manufacturing_work_order == mo.manufacturing_work_order
		]
		self.assertEqual(len(entries), 1)

	def test_fetch_and_update_returns_false_when_no_stock_entries(self):
		# mo = mo_creation()

		class Row:
			manufacturing_work_order = "NON-EXISTENT-MWO"

		res = fetch_and_update(frappe.new_doc("Department IR"), Row(), "MOP-UNKNOWN")
		self.assertFalse(res)

	def tearDown(self):
		return super().tearDown()

	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.db.get_value"
	)
	@patch(
		"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir.frappe.get_all"
	)
	def test_receive_rows_are_fetched_in_creation_order(
		self, mock_get_all, _mock_get_value
	):
		"""Manufacturing Operation sorts "modified DESC" by default, which had the Receive
		leg build its child table in a different order than the Issue processed its rows.
		Pinned to insertion order so both legs agree."""
		mock_get_all.return_value = []
		doc = FakeDepartmentIR(doctype="Department IR", name="DIR-ORDER-001")
		DepartmentIR.get_manufacturing_operations_from_department_ir(
			doc, "DIR-ISSUE-001"
		)

		self.assertEqual(mock_get_all.call_args.kwargs.get("order_by"), "creation asc")


def mo_creation():
	"""The current operation of a freshly submitted work order: the real lifecycle.

	Submitting a Manufacturing Work Order mints its first Manufacturing Operation (Not Started,
	in Manufacturing Setting.default_department) and points ``manufacturing_operation`` at it.
	Every Employee IR / Department IR row must name that pointer
	(doc_events/current_operation_guard.py), so this submits the producing work order of the
	Parent Manufacturing Order the calling test just built with ``create_pmo`` and returns its
	pointer.

	It used to mint an extra operation on the newest work order instead -- normally the FG one,
	still a draft -- which the guard rightly refuses: an operation of an unsubmitted work order
	that is not its current operation.
	"""
	pmo = frappe.get_last_doc("Parent Manufacturing Order", filters={"docstatus": 1})
	mwo_name = frappe.db.get_value(
		"Manufacturing Work Order",
		{
			"manufacturing_order": pmo.name,
			"docstatus": 0,
			"for_fg": 0,
			"is_finding_mwo": 0,
		},
		"name",
		order_by="creation desc",
	)
	if not mwo_name:
		frappe.throw(
			f"Parent Manufacturing Order {pmo.name} has no draft producing work order left; "
			"call create_pmo(self) before mo_creation()."
		)

	mwo = frappe.get_doc("Manufacturing Work Order", mwo_name)
	mwo.submit()

	pointer = frappe.db.get_value(
		"Manufacturing Work Order", mwo.name, "manufacturing_operation"
	)
	return frappe.get_doc("Manufacturing Operation", pointer)


_TOL_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.product_tolerance"
_UTILS_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.department_ir_utils"


def _dir_row(**kwargs):
	row = FrappeDict(
		manufacturing_operation="MOP-0001",
		manufacturing_work_order="MWO-0001",
		gross_wt=0.0,
		net_wt=0.0,
		finding_wt=0.0,
		diamond_wt=0.0,
		gemstone_wt=0.0,
	)
	row.update(kwargs)
	return row


def _mwo(**kwargs):
	mwo = FrappeDict(
		name="MWO-0001",
		manufacturing_order="PMO-0001",
		metal_type="Gold",
		is_finding_mwo=0,
		for_fg=0,
		qty=1,
	)
	mwo.update(kwargs)
	return mwo


def _metal_band(**kwargs):
	band = FrappeDict(
		parent="PMO-0001",
		metal_type="Gold",
		weight_type="Net Weight",
		from_tolerance_wt=13.95,
		to_tolerance_wt=16.05,
		standard_tolerance_wt=15.0,
	)
	band.update(kwargs)
	return band


class TestRowMirrorsTheOperation(UnitTestCase):
	"""A Department IR row carries the Manufacturing Operation's own weights.

	There is no previous-operation fallback. The `or` chain that used to provide one read
	a legitimate 0.0 as "unknown" and resurrected weight the operation did not hold --
	Department-IR-Labh-2026-02662 asked an operator to move 7.99 g out of MOP-G6L23, whose
	every bucket was 0, because its previous operation still read 7.99. The is_finding and
	is_mwo_refined carve-outs were the two honest zeroes already recognised; these tests
	pin that every honest zero is now treated the same way.
	"""

	MOP = "MOP-G6L23"
	MWO = "MWO-KGJPL-MU06633-001-9-91.75-Y-01"

	def _resolve(self, mop_data, rows=None):
		"""Run the resolver over one row, with everything but the weight read stubbed."""
		rows = (
			rows
			if rows is not None
			else [
				_dir_row(
					manufacturing_operation=self.MOP, manufacturing_work_order=self.MWO
				)
			]
		)
		doc = FrappeDict(
			type="Issue",
			next_department="Manufacturing Plan & Management - T",
			current_department="Central - T",
			department_ir_operation=rows,
		)
		with patch(f"{_UTILS_MODULE}.validate_duplicate"), patch(
			f"{_UTILS_MODULE}.validate_allowed_operation"
		), patch(f"{_UTILS_MODULE}.update_mop_balance"), patch(
			f"{_UTILS_MODULE}.update_previous_mop_data"
		), patch(
			f"{_UTILS_MODULE}.frappe.db.get_value", return_value=FrappeDict(mop_data)
		) as get_value:
			mwo_list = validate_and_update_gross_wt_from_mop(doc)
		return rows, mwo_list, get_value

	def test_zero_operation_gives_a_zero_row(self):
		"""The reported bug: every bucket 0 on the operation must stay 0 on the row."""
		rows, _mwo_list, _gv = self._resolve(dict.fromkeys(WEIGHT_FIELDS, 0.0))
		for field in WEIGHT_FIELDS:
			self.assertEqual(
				rows[0].get(field), 0, f"{field} should mirror the operation"
			)

	def test_the_previous_operation_is_never_read(self):
		"""One read, for the operation itself. A second read is the fallback coming back."""
		_rows, _mwo_list, get_value = self._resolve(dict.fromkeys(WEIGHT_FIELDS, 0.0))
		self.assertEqual(get_value.call_count, 1)
		self.assertEqual(get_value.call_args.args[1], self.MOP)

	def test_every_bucket_comes_from_the_operation(self):
		mop_data = {
			"gross_wt": 7.99,
			"net_wt": 6.984,
			"finding_wt": 0.684,
			"diamond_wt": 1.2,
			"diamond_pcs": 40,
			"gemstone_wt": 0.41,
			"gemstone_pcs": 1,
			"other_wt": 0.0,
		}
		rows, _mwo_list, _gv = self._resolve(mop_data)
		for field, expected in mop_data.items():
			self.assertEqual(rows[0].get(field), expected)

	def test_a_null_bucket_becomes_zero_not_none(self):
		"""diamond_pcs / gemstone_pcs are Data fields; None there would render as blank."""
		rows, _mwo_list, _gv = self._resolve(dict.fromkeys(WEIGHT_FIELDS, None))
		for field in WEIGHT_FIELDS:
			self.assertEqual(rows[0].get(field), 0)

	def test_a_hand_edited_row_is_overwritten(self):
		"""The grid allows bulk edit, and this runs on every draft save."""
		row = _dir_row(
			manufacturing_operation=self.MOP,
			manufacturing_work_order=self.MWO,
			gross_wt=99.0,
			net_wt=99.0,
		)
		rows, _mwo_list, _gv = self._resolve(
			dict.fromkeys(WEIGHT_FIELDS, 0.0), rows=[row]
		)
		self.assertEqual(rows[0].gross_wt, 0)
		self.assertEqual(rows[0].net_wt, 0)

	def test_every_row_reaches_the_repairing_check(self):
		"""mwo_list feeds valid_reparing_or_next_operation.

		It used to be reset at the top of each iteration and appended to only in the
		fallback branch, so it ended up holding the last row's work order -- or nothing at
		all when that row was a finding -- which made the Repairing decision depend on row
		order.
		"""
		rows = [
			_dir_row(manufacturing_operation="MOP-A", manufacturing_work_order="MWO-A"),
			_dir_row(manufacturing_operation="MOP-B", manufacturing_work_order="MWO-B"),
		]
		_rows, mwo_list, _gv = self._resolve(
			dict.fromkeys(WEIGHT_FIELDS, 0.0), rows=rows
		)
		self.assertEqual(mwo_list, ["MWO-A", "MWO-B"])


class TestProductToleranceGate(UnitTestCase):
	"""Department IR Issue is refused when the weight leaves the PMO's band."""

	def _failures(self, rows, mwos, bands, dept="Tagging - T"):
		doc = FrappeDict(
			type="Issue",
			is_finding=0,
			next_department=dept,
			current_department="Final Polish - T",
			department_ir_operation=rows,
		)
		with patch(
			f"{_TOL_MODULE}.frappe.get_all",
			side_effect=lambda doctype, **kw: mwos
			if doctype == "Manufacturing Work Order"
			else bands.get(doctype, []),
		), patch(f"{_TOL_MODULE}.frappe.get_meta") as meta:
			meta.return_value.has_field.return_value = True
			return get_tolerance_failures(doc, rows)

	def _validate(self, doc, flag=1):
		with patch(
			f"{_TOL_MODULE}.frappe.get_cached_value", return_value=flag
		) as cached:
			with patch(
				f"{_TOL_MODULE}.get_tolerance_failures", return_value=[]
			) as calc:
				validate_product_tolerance(doc)
		return cached, calc

	# ---- gating ----

	def test_receive_leg_is_never_checked(self):
		doc = FrappeDict(
			type="Receive",
			is_finding=0,
			next_department=None,
			department_ir_operation=[_dir_row()],
		)
		cached, calc = self._validate(doc)
		cached.assert_not_called()
		calc.assert_not_called()

	def test_finding_department_is_still_checked(self):
		"""is_finding is fetched from next_department.custom_is_finding.

		Tagging, Final Polish and Central all carry that flag, so treating it as
		"this transfer is a finding run" would switch the check off for exactly the
		hand-offs it exists to guard.
		"""
		doc = FrappeDict(
			type="Issue",
			is_finding=1,
			next_department="Tagging - T",
			department_ir_operation=[_dir_row()],
		)
		cached, calc = self._validate(doc)
		cached.assert_called_once()
		calc.assert_called_once()

	def test_unflagged_department_issues_no_tolerance_query(self):
		doc = FrappeDict(
			type="Issue",
			is_finding=0,
			next_department="Tagging - T",
			department_ir_operation=[_dir_row()],
		)
		_cached, calc = self._validate(doc, flag=0)
		calc.assert_not_called()

	def test_missing_custom_field_degrades_to_off(self):
		doc = FrappeDict(
			type="Issue",
			is_finding=0,
			next_department="Tagging - T",
			department_ir_operation=[_dir_row()],
		)
		_cached, calc = self._validate(doc, flag=None)
		calc.assert_not_called()

	# ---- boundaries ----

	def test_weight_above_band_fails_with_a_named_message(self):
		failures = self._failures(
			[_dir_row(net_wt=16.9)],
			[_mwo()],
			{"Metal Product Tolerance": [_metal_band()]},
		)
		self.assertEqual(len(failures), 1)
		for token in (
			"PMO-0001",
			"MWO-0001",
			"MOP-0001",
			"Tagging - T",
			"16.9",
			"13.95",
			"16.05",
		):
			self.assertIn(token, failures[0])

	def test_weight_below_band_fails(self):
		failures = self._failures(
			[_dir_row(net_wt=13.0)],
			[_mwo()],
			{"Metal Product Tolerance": [_metal_band()]},
		)
		self.assertEqual(len(failures), 1)

	def test_float_noise_at_the_boundary_passes(self):
		failures = self._failures(
			[_dir_row(net_wt=16.0500001)],
			[_mwo()],
			{"Metal Product Tolerance": [_metal_band()]},
		)
		self.assertEqual(failures, [])

	def test_exact_bounds_pass(self):
		for weight in (13.95, 16.05):
			self.assertEqual(
				self._failures(
					[_dir_row(net_wt=weight)],
					[_mwo()],
					{"Metal Product Tolerance": [_metal_band()]},
				),
				[],
			)

	def test_net_band_compares_net_plus_finding(self):
		"""MOP net_wt is metal only; the band's source includes findings."""
		failures = self._failures(
			[_dir_row(net_wt=15.0, finding_wt=2.0)],
			[_mwo()],
			{"Metal Product Tolerance": [_metal_band()]},
		)
		self.assertEqual(len(failures), 1)

	def test_gross_band_compares_gross(self):
		failures = self._failures(
			[_dir_row(gross_wt=15.0, net_wt=99.0)],
			[_mwo()],
			{"Metal Product Tolerance": [_metal_band(weight_type="Gross Weight")]},
		)
		self.assertEqual(failures, [])

	def test_zero_weight_is_not_a_violation(self):
		failures = self._failures(
			[_dir_row()], [_mwo()], {"Metal Product Tolerance": [_metal_band()]}
		)
		self.assertEqual(failures, [])

	def test_pmo_without_bands_passes(self):
		failures = self._failures([_dir_row(net_wt=999.0)], [_mwo()], {})
		self.assertEqual(failures, [])

	# ---- selection and bucketing ----

	def test_band_for_another_metal_is_ignored(self):
		failures = self._failures(
			[_dir_row(net_wt=16.9)],
			[_mwo(metal_type="Gold")],
			{
				"Metal Product Tolerance": [
					_metal_band(metal_type="Gold"),
					_metal_band(
						metal_type="Silver", from_tolerance_wt=0, to_tolerance_wt=100
					),
				]
			},
		)
		self.assertEqual(len(failures), 1)

	def test_producing_work_orders_of_one_pmo_are_summed(self):
		failures = self._failures(
			[
				_dir_row(
					manufacturing_work_order="MWO-A",
					manufacturing_operation="MOP-A",
					net_wt=8.0,
				),
				_dir_row(
					manufacturing_work_order="MWO-B",
					manufacturing_operation="MOP-B",
					net_wt=7.0,
				),
			],
			[_mwo(name="MWO-A"), _mwo(name="MWO-B")],
			{"Metal Product Tolerance": [_metal_band()]},
		)
		self.assertEqual(failures, [])

	def test_fg_row_is_dropped_when_producing_rows_are_present(self):
		failures = self._failures(
			[
				_dir_row(manufacturing_work_order="MWO-A", net_wt=15.0),
				_dir_row(manufacturing_work_order="MWO-FG", net_wt=15.0),
			],
			[_mwo(name="MWO-A"), _mwo(name="MWO-FG", for_fg=1)],
			{"Metal Product Tolerance": [_metal_band()]},
		)
		self.assertEqual(failures, [])

	def test_finding_work_orders_are_excluded(self):
		failures = self._failures(
			[_dir_row(net_wt=999.0)],
			[_mwo(is_finding_mwo=1)],
			{"Metal Product Tolerance": [_metal_band()]},
		)
		self.assertEqual(failures, [])

	def test_band_is_scaled_by_quantity(self):
		self.assertEqual(
			self._failures(
				[_dir_row(net_wt=45.0)],
				[_mwo(qty=3)],
				{"Metal Product Tolerance": [_metal_band()]},
			),
			[],
		)
		self.assertEqual(
			len(
				self._failures(
					[_dir_row(net_wt=15.0)],
					[_mwo(qty=3)],
					{"Metal Product Tolerance": [_metal_band()]},
				)
			),
			1,
		)

	# ---- stones ----

	def test_diamond_band_is_checked_in_carats(self):
		failures = self._failures(
			[_dir_row(diamond_wt=2.5)],
			[_mwo()],
			{
				"Diamond Product Tolerance": [
					FrappeDict(
						parent="PMO-0001",
						weight_type="Weight wise",
						from_tolerance_wt=2.0,
						to_tolerance_wt=2.2,
						standard_tolerance_wt=2.1,
					)
				]
			},
		)
		self.assertEqual(len(failures), 1)
		self.assertIn("cts", failures[0])

	def test_per_sieve_diamond_bands_are_not_compared_to_a_total(self):
		failures = self._failures(
			[_dir_row(diamond_wt=9.9)],
			[_mwo()],
			{
				"Diamond Product Tolerance": [
					FrappeDict(
						parent="PMO-0001",
						weight_type="MM Size wise",
						from_tolerance_wt=2.0,
						to_tolerance_wt=2.2,
						standard_tolerance_wt=2.1,
					)
				]
			},
		)
		self.assertEqual(failures, [])

	def test_several_failures_are_reported_together(self):
		doc = FrappeDict(
			type="Issue",
			is_finding=0,
			next_department="Tagging - T",
			department_ir_operation=[],
		)
		with patch(f"{_TOL_MODULE}.frappe.get_cached_value", return_value=1), patch(
			f"{_TOL_MODULE}.get_tolerance_failures", return_value=["first", "second"]
		):
			doc.department_ir_operation = [_dir_row()]
			with self.assertRaises(frappe.ValidationError):
				validate_product_tolerance(doc)


class TestProductToleranceBandSelection(UnitTestCase):
	"""Regressions found by review: each weight basis must be judged on its own."""

	def _failures(self, rows, mwos, bands):
		doc = FrappeDict(
			type="Issue",
			is_finding=0,
			next_department="Tagging - T",
			current_department="Final Polish - T",
			department_ir_operation=rows,
		)
		with patch(
			f"{_TOL_MODULE}.frappe.get_all",
			side_effect=lambda doctype, **kw: mwos
			if doctype == "Manufacturing Work Order"
			else bands.get(doctype, []),
		), patch(f"{_TOL_MODULE}.frappe.get_meta") as meta:
			meta.return_value.has_field.return_value = True
			return get_tolerance_failures(doc, rows)

	def test_passing_net_band_does_not_excuse_a_gross_overrun(self):
		"""Gross and Net measure different things and are judged separately."""
		failures = self._failures(
			[_dir_row(gross_wt=30.0, net_wt=15.0)],
			[_mwo()],
			{
				"Metal Product Tolerance": [
					_metal_band(weight_type="Net Weight"),
					_metal_band(
						weight_type="Gross Weight",
						from_tolerance_wt=19.8,
						to_tolerance_wt=22.0,
					),
				]
			},
		)
		self.assertEqual(len(failures), 1)
		self.assertIn("Gross Weight", failures[0])
		self.assertIn("30.0", failures[0])

	def test_both_bases_can_fail_at_once(self):
		failures = self._failures(
			[_dir_row(gross_wt=30.0, net_wt=99.0)],
			[_mwo()],
			{
				"Metal Product Tolerance": [
					_metal_band(weight_type="Net Weight"),
					_metal_band(
						weight_type="Gross Weight",
						from_tolerance_wt=19.8,
						to_tolerance_wt=22.0,
					),
				]
			},
		)
		self.assertEqual(len(failures), 2)

	def test_duplicate_bands_of_one_basis_still_fail_open(self):
		"""Legacy PMOs carry a whole schedule for one basis; any match passes."""
		failures = self._failures(
			[_dir_row(net_wt=15.0)],
			[_mwo()],
			{
				"Metal Product Tolerance": [
					_metal_band(from_tolerance_wt=1.0, to_tolerance_wt=2.0),
					_metal_band(),
				]
			},
		)
		self.assertEqual(failures, [])

	def test_foreign_metal_band_is_not_applied(self):
		"""A Gold-only schedule must not police a Platinum work order."""
		failures = self._failures(
			[_dir_row(net_wt=40.0)],
			[_mwo(metal_type="Platinum")],
			{"Metal Product Tolerance": [_metal_band(metal_type="Gold")]},
		)
		self.assertEqual(failures, [])

	def test_universal_diamond_band_is_enforced(self):
		"""set_diamond_tolerance_table stamps Universal from the whole BOM aggregate,
		so it is a product total and must be compared like one."""
		failures = self._failures(
			[_dir_row(diamond_wt=9.9)],
			[_mwo()],
			{
				"Diamond Product Tolerance": [
					FrappeDict(
						parent="PMO-0001",
						weight_type="Universal",
						from_tolerance_wt=2.0,
						to_tolerance_wt=2.2,
						standard_tolerance_wt=2.1,
					)
				]
			},
		)
		self.assertEqual(len(failures), 1)
		self.assertIn("cts", failures[0])


class TestScopedStoneBandsAreNotEnforced(UnitTestCase):
	"""A type- or shape-scoped stone band is a subtotal; the row carries a total."""

	def _failures(self, rows, mwos, bands):
		doc = FrappeDict(
			type="Issue",
			is_finding=0,
			next_department="Tagging - T",
			current_department="Final Polish - T",
			department_ir_operation=rows,
		)
		with patch(
			f"{_TOL_MODULE}.frappe.get_all",
			side_effect=lambda doctype, **kw: mwos
			if doctype == "Manufacturing Work Order"
			else bands.get(doctype, []),
		), patch(f"{_TOL_MODULE}.frappe.get_meta") as meta:
			meta.return_value.has_field.return_value = True
			return get_tolerance_failures(doc, rows)

	def _diamond(self, **kwargs):
		band = FrappeDict(
			parent="PMO-0001",
			weight_type="Weight wise",
			diamond_type=None,
			from_tolerance_wt=2.0,
			to_tolerance_wt=2.2,
			standard_tolerance_wt=2.1,
		)
		band.update(kwargs)
		return band

	def test_type_scoped_diamond_band_is_not_enforced(self):
		failures = self._failures(
			[_dir_row(diamond_wt=9.9)],
			[_mwo()],
			{"Diamond Product Tolerance": [self._diamond(diamond_type="Natural")]},
		)
		self.assertEqual(failures, [])

	def test_unscoped_diamond_band_is_still_enforced(self):
		failures = self._failures(
			[_dir_row(diamond_wt=9.9)],
			[_mwo()],
			{"Diamond Product Tolerance": [self._diamond()]},
		)
		self.assertEqual(len(failures), 1)

	def test_scoped_band_does_not_mask_an_unscoped_one(self):
		"""A loose type-scoped band must not excuse a breach of the whole-product band."""
		failures = self._failures(
			[_dir_row(diamond_wt=9.9)],
			[_mwo()],
			{
				"Diamond Product Tolerance": [
					self._diamond(
						diamond_type="Natural",
						from_tolerance_wt=0.0,
						to_tolerance_wt=100.0,
					),
					self._diamond(),
				]
			},
		)
		self.assertEqual(len(failures), 1)

	def test_type_scoped_gemstone_band_is_not_enforced(self):
		failures = self._failures(
			[_dir_row(gemstone_wt=9.9)],
			[_mwo()],
			{
				"Gemstone Product Tolerance": [
					FrappeDict(
						parent="PMO-0001",
						weight_type="Weight wise",
						gemstone_type="Ruby",
						gemstone_shape=None,
						from_tolerance_wt=2.0,
						to_tolerance_wt=2.2,
						standard_tolerance_wt=2.1,
					)
				]
			},
		)
		self.assertEqual(failures, [])


# =============================================================================================
# Work-order current-operation guard: Department IR rules, draft block, cancel guard and wiring
# (doc_events/current_operation_guard.py). DB-free like the guard classes in
# test_employee_ir.py, so they run in CI through `run-tests --doctype "Department IR"`.
# =============================================================================================

_DIR_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir"
_PC_TAGGING_SYNC = (
	"jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.pc_tagging_stock_sync"
	".process_pc_tagging_stock_sync"
)


class _GuardStop(Exception):
	"""Raised by a patched step to end a controller method at a known point."""


def _warm_guard_caches():
	"""Load what frappe._ / frappe.format / flt read lazily before a test patches frappe.db."""
	frappe._("Work Order Current Operation")
	frappe.get_system_settings("float_precision")
	frappe.format(frappe.utils.now_datetime(), {"fieldtype": "Datetime"})
	frappe.utils.flt(1.23456, 3)


def _record(calls, label, result=None, raises=None):
	"""Side effect that records ``label`` in ``calls``, then returns ``result`` or raises."""

	def side_effect(*args, **kwargs):
		calls.append(label)
		if raises is not None:
			raise raises
		return result

	return side_effect


class _DIRGuardDoc(FrappeDict):
	"""The slice of a Department IR the guard and the controller touch.

	(The Employee IR twin lives in test_employee_ir.py, which imports this module, so it cannot
	be imported from here.)
	"""

	def is_new(self):
		return bool(self.get("_is_new"))

	def get_doc_before_save(self):
		return self.get("_before")


def _guard_dir(
	kind="Issue",
	rows=(("MOP-1", "MWO-1"),),
	*,
	name="DIR-T-0001",
	docstatus=0,
	new=False,
	before=None,
	**header,
):
	"""Issue: Dept-A -> Dept-B. Receive: into Dept-B from Dept-A against DIR-I-0001."""
	issue = kind == "Issue"
	doc = _DIRGuardDoc(
		doctype="Department IR",
		name=name,
		type=kind,
		docstatus=docstatus,
		company="Co-T",
		current_department="Dept-A" if issue else "Dept-B",
		next_department="Dept-B" if issue else None,
		previous_department=None if issue else "Dept-A",
		receive_against=None if issue else "DIR-I-0001",
		department_ir_operation=[
			FrappeDict(
				idx=idx, manufacturing_operation=mop, manufacturing_work_order=mwo
			)
			for idx, (mop, mwo) in enumerate(rows, start=1)
		],
		flags=FrappeDict(),
		_is_new=new,
		_before=before,
	)
	doc.update(header)
	return doc


def _guard_op(name, mwo="MWO-1", **fields):
	"""A Manufacturing Operation row as lock_manufacturing_operations returns it."""
	row = FrappeDict(
		name=name,
		manufacturing_work_order=mwo,
		company="Co-T",
		department="Dept-A",
		status="Not Started",
		department_ir_status="Received",
		operation=None,
		employee=None,
		subcontractor=None,
		for_subcontracting=0,
		department_issue_id=None,
		department_receive_id=None,
		employee_ir=None,
		previous_mop=None,
	)
	row.update(fields)
	return row


def _guard_wo(name="MWO-1", pointer="MOP-1", docstatus=1):
	"""A Manufacturing Work Order row as lock_work_orders returns it."""
	return FrappeDict(
		name=name,
		docstatus=docstatus,
		company="Co-T",
		manufacturing_operation=pointer,
		department="Dept-A",
	)


def _guard_family(*ops):
	family = {}
	for op in ops:
		family.setdefault(op.manufacturing_work_order, []).append(op)
	return family


def _guard_problems(doc, ops, wos, family=None, phase="save"):
	return guard._row_problems(
		doc,
		guard.profile(doc),
		phase,
		{op.name: op for op in ops},
		{wo.name: wo for wo in wos},
		_guard_family(*ops) if family is None else family,
	)


def _single_problem(test, problems, exc):
	test.assertEqual(len(problems), 1, problems)
	test.assertIs(problems[0][0], exc, problems)
	return problems[0][1]


def _check_call(check):
	"""``(phase, locking)`` of the single ``_check`` call, however it was spelled."""
	check.assert_called_once()
	args, kwargs = check.call_args
	locking = (
		kwargs["locking"]
		if "locking" in kwargs
		else (args[2] if len(args) > 2 else False)
	)
	return args[1], locking


class TestCurrentOperationGuardDepartmentIssueRule(IntegrationTestCase):
	"""Department Issue: the current operation must be Not Started, with no employee or
	subcontractor, received in the sending department -- the department_ir.js scan / Get
	Operations filters, server-side."""

	@classmethod
	def setUpClass(cls):
		_warm_guard_caches()

	def _issue(self, doc=None, **op_fields):
		return _guard_problems(
			doc or _guard_dir("Issue"), [_guard_op("MOP-1", **op_fields)], [_guard_wo()]
		)

	def _refused(self, doc=None, **op_fields):
		message = _single_problem(
			self, self._issue(doc, **op_fields), guard.CurrentOperationError
		)
		self.assertIn("a Department Issue needs", message)
		return message

	def test_not_started_unassigned_operation_in_the_sending_department_passes(self):
		self.assertEqual(self._issue(), [])
		self.assertEqual(self._issue(department_ir_status=None), [])

	def test_backward_rework_transfer_is_allowed(self):
		"""Repairing moves send work back to an earlier department; only the source matters."""
		self.assertEqual(
			self._issue(_guard_dir("Issue", next_department="Model Making - T")), []
		)

	def test_same_current_and_next_department_is_refused(self):
		problems = self._issue(_guard_dir("Issue", next_department=" dept-a "))
		message = _single_problem(self, problems, guard.CurrentOperationError)
		self.assertEqual(message, "Current and next department cannot be the same.")

	def test_every_status_but_not_started_is_refused(self):
		for status in (
			"WIP",
			"On Hold",
			"QC Pending",
			"QC Completed",
			"Finished",
			"Revert",
			None,
		):
			with self.subTest(status=status):
				self._refused(status=status)

	def test_operation_in_another_department_is_refused(self):
		self.assertIn(
			"received in <strong>Dept-A</strong>", self._refused(department="Dept-X")
		)

	def test_operation_in_transit_or_reverted_is_refused(self):
		for transit in ("In-Transit", "Revert"):
			with self.subTest(transit=transit):
				self._refused(department_ir_status=transit)

	def test_operation_with_an_employee_or_subcontractor_is_refused(self):
		"""Unlike an Employee Issue, both holders must be unset."""
		for field, value in (("employee", "EMP-1"), ("subcontractor", "SUP-1")):
			with self.subTest(field=field):
				self._refused(**{field: value})

	def test_same_work_order_twice_is_refused(self):
		"""Family B of the audit: duplicate rows of one transfer minted twin operations."""
		doc = _guard_dir("Issue", rows=(("MOP-1", "MWO-1"), ("MOP-1B", "MWO-1")))
		problems = _guard_problems(
			doc, [_guard_op("MOP-1"), _guard_op("MOP-1B")], [_guard_wo()]
		)
		message = _single_problem(self, problems, guard.CurrentOperationError)
		self.assertIn("work order <strong>MWO-1</strong> is already on row 1", message)

	def test_operation_the_work_order_has_moved_past_is_refused(self):
		problems = _guard_problems(
			_guard_dir("Issue"),
			[_guard_op("MOP-1", status="Finished")],
			[_guard_wo(pointer="MOP-2")],
			family={},
		)
		self.assertIn(
			"MOP-2", _single_problem(self, problems, guard.StaleOperationError)
		)

	def test_transfer_again_after_a_reverted_transfer_is_allowed(self):
		"""kggk_uat: a cancelled Department Issue leaves its minted operation as Revert history
		(previous_mop = the restored source). Re-issuing the work order must go through; a live
		later operation still makes the work order ambiguous."""
		source = _guard_op("MOP-1")
		for marks in (
			{"department_ir_status": "Revert", "status": "Not Started"},
			{"status": "Revert"},
		):
			with self.subTest(marks=marks):
				reverted = _guard_op(
					"MOP-2", previous_mop="MOP-1", department="Dept-B", **marks
				)
				self.assertEqual(
					_guard_problems(
						_guard_dir("Issue"),
						[source],
						[_guard_wo()],
						family=_guard_family(source, reverted),
					),
					[],
				)
		live = _guard_op(
			"MOP-2",
			previous_mop="MOP-1",
			department="Dept-B",
			department_ir_status="In-Transit",
		)
		problems = _guard_problems(
			_guard_dir("Issue"),
			[source],
			[_guard_wo()],
			family=_guard_family(source, live),
		)
		_single_problem(self, problems, guard.AmbiguousOperationError)


class TestCurrentOperationGuardDepartmentReceiveRule(IntegrationTestCase):
	"""Department Receive: only the in-flight transfer of this Issue, into this department."""

	@classmethod
	def setUpClass(cls):
		_warm_guard_caches()

	def _receive(self, **op_fields):
		fields = {
			"department": "Dept-B",
			"department_ir_status": "In-Transit",
			"department_issue_id": "DIR-I-0001",
		}
		fields.update(op_fields)
		doc = _guard_dir("Receive", rows=(("MOP-2", "MWO-1"),))
		return _guard_problems(
			doc, [_guard_op("MOP-2", **fields)], [_guard_wo(pointer="MOP-2")]
		)

	def _refused(self, **op_fields):
		return _single_problem(
			self, self._receive(**op_fields), guard.CurrentOperationError
		)

	def _mismatch(self, **op_fields):
		"""In transit, but not as this Receive needs it: the message names what differs."""
		message = self._refused(**op_fields)
		for token in (
			"MOP-2",
			"MWO-1",
			"is in transit but does not match this Receive",
		):
			self.assertIn(token, message)
		self.assertNotIn("no longer in transit", message)
		return message

	def test_in_flight_transfer_of_this_issue_passes(self):
		self.assertEqual(self._receive(), [])

	def test_transfer_already_received_is_refused(self):
		message = self._refused(department_ir_status="Received")
		self.assertIn("is no longer in transit", message)
		for token in ("MOP-2", "MWO-1", "DIR-I-0001", "Dept-B", "(Received)"):
			self.assertIn(token, message)

	def test_operation_never_in_transit_says_so(self):
		message = self._refused(department_ir_status=None)
		self.assertIn("is no longer in transit", message)
		self.assertIn("(not in transit)", message)

	def test_operation_already_started_is_refused(self):
		self.assertIn("(status WIP)", self._mismatch(status="WIP"))

	def test_transfer_of_another_issue_is_refused(self):
		message = self._mismatch(department_issue_id="DIR-I-OTHER")
		self.assertIn("sent by <strong>DIR-I-OTHER</strong>", message)

	def test_transfer_into_another_department_is_refused(self):
		self.assertIn(
			"going to <strong>Dept-C</strong>", self._mismatch(department="Dept-C")
		)

	def test_a_transfer_closed_by_hand_while_in_transit_names_its_status(self):
		"""Production had 46 such current operations (Finished while still In-Transit, closed
		at the desk after a receive was cancelled): the refusal used to claim they were "no
		longer in transit ... (In-Transit)". Removing the row lets the others be received."""
		message = self._mismatch(status="Finished")
		self.assertIn("(status Finished)", message)
		self.assertIn("closed while still in transit", message)
		self.assertIn("current-operation audit", message)
		self.assertIn("remove this row", message)
		self.assertNotIn("(In-Transit)", message)

	def test_every_mismatch_is_listed(self):
		message = self._mismatch(
			status="WIP", department_issue_id=None, department="Dept-C"
		)
		self.assertIn(
			"(status WIP, sent by no Department Issue, going to <strong>Dept-C</strong>)",
			message,
		)


class TestCurrentOperationGuardDepartmentDraftBlock(IntegrationTestCase):
	"""An outstanding Employee Issue draft blocks Department IR saves and submits on its work
	order: on 2026-10-05 Department IRs moved the work orders on while EMP-IR-Labh-2026-43405
	sat as a draft, which is what made that draft stale."""

	@classmethod
	def setUpClass(cls):
		_warm_guard_caches()

	def tearDown(self):
		frappe.clear_messages()
		return super().tearDown()

	def _check(self, doc, phase, ops, wos, drafts=(), previous=()):
		"""``previous``: what _previous_operations reports for the row operations."""
		self.reads = []
		self.locked = []

		def draft_read(mwos, exclude=None, locking=False):
			self.reads.append((sorted(mwos), exclude))
			return [FrappeDict(d) for d in drafts]

		rows = ({op.name: op for op in ops}, {wo.name: wo for wo in wos})

		def lock_block(mops, mwos, doc=None):
			self.locked.append((sorted(mops), sorted(mwos)))
			return rows

		with (
			patch.object(guard, "_lock_block", side_effect=lock_block),
			patch.object(guard, "_after_locks"),
			patch.object(guard, "_family", return_value=_guard_family(*ops)),
			patch.object(guard, "_confirm_successors", side_effect=lambda f, m: f),
			patch.object(guard, "_confirm_drafts", side_effect=lambda d, m: d),
			patch.object(
				guard, "_previous_operations", return_value=list(previous)
			) as previous_read,
			patch.object(guard, "outstanding_issue_drafts", side_effect=draft_read),
			patch.object(guard.frappe, "msgprint"),
			patch.object(guard, "_refuse_if_snapshot_older"),
		):
			self.previous_read = previous_read
			guard._check(doc, phase, True)

	@staticmethod
	def _draft():
		return dict(
			draft="EMP-IR-T-0001",
			mwo="MWO-1",
			idx=1,
			owner="maker@example.com",
			creation=frappe.utils.now_datetime(),
		)

	def test_department_issue_submit_is_blocked_by_an_employee_issue_draft(self):
		doc = _guard_dir("Issue", docstatus=1, before=_guard_dir("Issue"))
		with self.assertRaises(guard.OutstandingDraftError) as cm:
			self._check(
				doc,
				"submit",
				[_guard_op("MOP-1")],
				[_guard_wo()],
				drafts=[self._draft()],
			)
		self.assertIn("EMP-IR-T-0001", str(cm.exception))
		self.assertEqual(self.reads, [(["MWO-1"], "DIR-T-0001")])

	def test_new_department_receive_is_blocked_too(self):
		doc = _guard_dir("Receive", rows=(("MOP-2", "MWO-1"),), new=True)
		op = _guard_op(
			"MOP-2",
			department="Dept-B",
			department_ir_status="In-Transit",
			department_issue_id="DIR-I-0001",
		)
		with self.assertRaises(guard.OutstandingDraftError):
			self._check(
				doc, "save", [op], [_guard_wo(pointer="MOP-2")], drafts=[self._draft()]
			)
		self.assertEqual(self.reads, [(["MWO-1"], None)])

	def test_department_issue_without_drafts_passes_and_is_remembered(self):
		doc = _guard_dir("Issue", docstatus=1, before=_guard_dir("Issue"))
		self._check(doc, "submit", [_guard_op("MOP-1")], [_guard_wo()])
		self.assertEqual(
			doc.flags.current_operation_guarded,
			{"phase": "submit", "docstatus": 1, "mwos": ["MWO-1"]},
		)

	def test_a_save_locks_the_previous_operations_in_the_operation_phase(self):
		"""validate_and_update_gross_wt_from_mop -> update_previous_mop_data writes the row
		operations' previous operations later in the save. Locking them only then -- after the
		work-order lock -- deadlocked against Stock Entries on those operations (race suite,
		31 deadlocks in 60 s): they join the one sorted operation set instead."""
		for kind, rows, op in (
			("Issue", (("MOP-5", "MWO-1"),), _guard_op("MOP-5")),
			(
				"Receive",
				(("MOP-5", "MWO-1"),),
				_guard_op(
					"MOP-5",
					department="Dept-B",
					department_ir_status="In-Transit",
					department_issue_id="DIR-I-0001",
				),
			),
		):
			with self.subTest(kind=kind):
				doc = _guard_dir(kind, rows=rows, new=True)
				self._check(
					doc, "save", [op], [_guard_wo(pointer="MOP-5")], previous=["MOP-4"]
				)
				self.previous_read.assert_called_once_with(["MOP-5"])
				self.assertEqual(self.locked, [(["MOP-4", "MOP-5"], ["MWO-1"])])

	def test_a_submit_locks_only_the_row_operations(self):
		"""Nothing writes a previous operation at submit."""
		doc = _guard_dir("Issue", docstatus=1, before=_guard_dir("Issue"))
		self._check(
			doc, "submit", [_guard_op("MOP-1")], [_guard_wo()], previous=["MOP-0"]
		)
		self.previous_read.assert_not_called()
		self.assertEqual(self.locked, [(["MOP-1"], ["MWO-1"])])

	def test_previous_operations_come_from_one_plain_read(self):
		with patch.object(
			guard.frappe,
			"get_all",
			return_value=[
				FrappeDict(previous_mop="MOP-0"),
				FrappeDict(previous_mop=None),
				FrappeDict(previous_mop="MOP-1"),
			],
		) as get_all:
			self.assertEqual(
				guard._previous_operations(["MOP-2", "MOP-1", "", "MOP-2"]), ["MOP-0"]
			)
		get_all.assert_called_once()
		self.assertEqual(
			get_all.call_args.kwargs["filters"], {"name": ["in", ["MOP-1", "MOP-2"]]}
		)
		with patch.object(guard.frappe, "get_all") as get_all:
			self.assertEqual(guard._previous_operations(["", None]), [])
		get_all.assert_not_called()


class TestCurrentOperationGuardDepartmentCancel(IntegrationTestCase):
	"""guard_cancel: a Department IR cancel may only undo a transfer that is still current."""

	@classmethod
	def setUpClass(cls):
		_warm_guard_caches()

	def _cancel(self, doc, ops, wos, minted=None, family=None, holding=None):
		"""Run guard_cancel; ``minted`` maps a work order to the operation the Issue minted;
		``holding`` is the submitted Employee Issue a message names for a held operation."""
		self.locked = []
		self.minted_filters = []

		def lock_block(mops, mwos, doc=None):
			self.locked.append((sorted(mops), sorted(mwos)))
			return {op.name: op for op in ops}, {wo.name: wo for wo in wos}

		def get_value(doctype, filters=None, *args, **kwargs):
			if doctype == "Manufacturing Operation" and isinstance(filters, dict):
				self.minted_filters.append(filters)
				return (minted or {}).get(filters.get("manufacturing_work_order"))
			return None

		with (
			patch.object(guard, "_lock_block", side_effect=lock_block),
			patch.object(guard, "_after_locks"),
			patch.object(
				guard,
				"_family",
				return_value=_guard_family(*ops) if family is None else family,
			),
			patch.object(guard, "_confirm_successors", side_effect=lambda f, m: f),
			patch.object(guard, "_holding_issue", return_value=holding),
			patch.object(guard.frappe.db, "get_value", side_effect=get_value),
			patch.object(
				guard, "other_submitted_issues", side_effect=AssertionError("EIR only")
			),
			patch.object(guard, "_refuse_if_snapshot_older"),
		):
			guard.guard_cancel(doc)

	def _issue(self):
		return _guard_dir("Issue", name="DIR-I-0001", docstatus=2)

	def _transfer(self, **fields):
		values = {
			"department": "Dept-B",
			"department_ir_status": "In-Transit",
			"department_issue_id": "DIR-I-0001",
			"previous_mop": "MOP-1",
		}
		values.update(fields)
		return _guard_op("MOP-2", **values)

	def test_issue_cancel_is_allowed_while_the_transfer_is_in_flight(self):
		source = _guard_op("MOP-1", status="Finished")
		self._cancel(
			self._issue(),
			[source, self._transfer()],
			[_guard_wo(pointer="MOP-2")],
			minted={"MWO-1": "MOP-2"},
		)
		self.assertEqual(
			self.minted_filters,
			[
				{
					"department_issue_id": "DIR-I-0001",
					"manufacturing_work_order": "MWO-1",
				}
			],
		)
		self.assertEqual(self.locked, [(["MOP-1", "MOP-2"], ["MWO-1"])])

	def test_issue_cancel_is_refused_once_the_transfer_was_received(self):
		source = _guard_op("MOP-1", status="Finished")
		received = self._transfer(
			department_ir_status="Received", department_receive_id="DIR-R-0001"
		)
		with self.assertRaises(guard.HistoryRewriteError) as cm:
			self._cancel(
				self._issue(),
				[source, received],
				[_guard_wo(pointer="MOP-2")],
				minted={"MWO-1": "MOP-2"},
			)
		message = str(cm.exception)
		# The work order did not "move on": its transfer was received. The cancel would delete
		# MOP-2 and reopen the SOURCE, MOP-1.
		self.assertIn(
			"its transfer <strong>MOP-2</strong> of work order <strong>MWO-1</strong> was "
			"already received by <strong>DIR-R-0001</strong>",
			message,
		)
		self.assertIn("would reopen <strong>MOP-1</strong>", message)
		self.assertNotIn("has moved on", message)
		self.assertNotIn("would reopen <strong>MOP-2</strong>", message)

	def test_issue_cancel_ignores_a_reverted_successor_of_its_transfer(self):
		"""kggk_uat LIFO: what happened in the receiving department was undone, its minted
		operation kept as Revert history (previous_mop = this transfer's operation)."""
		source = _guard_op("MOP-1", status="Finished")
		transfer = self._transfer()
		reverted = _guard_op(
			"MOP-3",
			previous_mop="MOP-2",
			department="Dept-B",
			department_ir_status="Revert",
		)
		self._cancel(
			self._issue(),
			[source, transfer],
			[_guard_wo(pointer="MOP-2")],
			minted={"MWO-1": "MOP-2"},
			family=_guard_family(source, transfer, reverted),
		)

	def test_issue_cancel_is_refused_for_a_transfer_stamped_with_a_receipt(self):
		"""A receipt id on the minted operation is enough, whatever its transit status says."""
		source = _guard_op("MOP-1", status="Finished")
		stamped = self._transfer(department_receive_id="DIR-R-0001")
		with self.assertRaises(guard.HistoryRewriteError):
			self._cancel(
				self._issue(),
				[source, stamped],
				[_guard_wo(pointer="MOP-2")],
				minted={"MWO-1": "MOP-2"},
			)

	def test_issue_cancel_is_refused_once_the_work_order_moved_further(self):
		source = _guard_op("MOP-1", status="Finished")
		transfer = self._transfer(
			status="Finished",
			department_ir_status="Received",
			department_receive_id="DIR-R-0001",
		)
		later = _guard_op(
			"MOP-3", previous_mop="MOP-2", department="Dept-B", status="WIP"
		)
		with self.assertRaises(guard.HistoryRewriteError) as cm:
			self._cancel(
				self._issue(),
				[source, transfer],
				[_guard_wo(pointer="MOP-3")],
				minted={"MWO-1": "MOP-2"},
				family=_guard_family(source, transfer, later),
			)
		self.assertIn("MOP-3", str(cm.exception))

	def test_issue_cancel_is_refused_without_the_minted_operation(self):
		with self.assertRaises(guard.HistoryRewriteError):
			self._cancel(
				self._issue(),
				[_guard_op("MOP-1", status="Finished")],
				[_guard_wo()],
				minted={},
			)

	def _receive(self):
		return _guard_dir(
			"Receive", rows=(("MOP-2", "MWO-1"),), name="DIR-R-0001", docstatus=2
		)

	def _received(self, **fields):
		values = {
			"department": "Dept-B",
			"department_ir_status": "Received",
			"department_issue_id": "DIR-I-0001",
			"department_receive_id": "DIR-R-0001",
			"previous_mop": "MOP-1",
		}
		values.update(fields)
		return _guard_op("MOP-2", **values)

	def test_receive_cancel_is_allowed_while_nothing_happened_in_the_department(self):
		self._cancel(self._receive(), [self._received()], [_guard_wo(pointer="MOP-2")])
		self.assertEqual(self.minted_filters, [])
		self.assertEqual(self.locked, [(["MOP-2"], ["MWO-1"])])

	def test_receive_cancel_is_refused_once_the_work_order_moved_on(self):
		"""Family A of the audit: 10-01 Receive cancels reopened work orders already moved on.
		The message names what blocks the cancel -- the Employee IR holding the operation, not a
		"move" to the very operation the cancel names."""
		for label, fields, pointer, expected in (
			(
				"issued to an employee",
				{"status": "WIP", "operation": "Op-T", "employee": "EMP-1"},
				"MOP-2",
				(
					"<strong>MOP-2</strong> of work order <strong>MWO-1</strong> is WIP with "
					"<strong>EMP-1</strong> for <strong>Op-T</strong> (Employee IR "
					"<strong>EIR-I-0007</strong>)",
					"would send <strong>MOP-2</strong> back in transit while it is WIP",
				),
			),
			(
				"received by another document",
				{"department_receive_id": "DIR-R-OTHER"},
				"MOP-2",
				("was received by <strong>DIR-R-OTHER</strong>",),
			),
			(
				"sent onwards",
				{"status": "Finished"},
				"MOP-3",
				(
					"work order <strong>MWO-1</strong> has moved on to <strong>MOP-3</strong>",
					"would send <strong>MOP-2</strong> back in transit.",
				),
			),
		):
			with self.subTest(label=label):
				with self.assertRaises(guard.HistoryRewriteError) as cm:
					self._cancel(
						self._receive(),
						[self._received(**fields)],
						[_guard_wo(pointer=pointer)],
						holding="EIR-I-0007",
					)
				message = str(cm.exception)
				for token in expected:
					self.assertIn(token, message)
				self.assertNotIn("would reopen", message)

	def test_receive_cancel_ignores_a_reverted_successor(self):
		self._cancel(
			self._receive(),
			[self._received()],
			[_guard_wo(pointer="MOP-2")],
			family=_guard_family(
				self._received(),
				_guard_op(
					"MOP-3",
					previous_mop="MOP-2",
					status="Not Started",
					department_ir_status="Revert",
				),
			),
		)

	def test_cancel_guard_ignores_warn_mode(self):
		with (
			patch.dict(frappe.local.conf, {"current_operation_guard": "warn"}),
			patch.object(guard.frappe, "log_error") as log_error,
		):
			with self.assertRaises(guard.HistoryRewriteError):
				self._cancel(
					self._receive(),
					[self._received(status="WIP", operation="Op-T")],
					[_guard_wo(pointer="MOP-2")],
				)
		log_error.assert_not_called()

	def test_reviewed_repair_bypasses_the_rule(self):
		allowed = {guard.REVIEWED_REPAIR_FLAG: {("Department IR", "DIR-R-0001")}}
		with patch.dict(frappe.local.flags, allowed):
			self._cancel(
				self._receive(),
				[self._received(status="WIP", operation="Op-T")],
				[_guard_wo(pointer="MOP-2")],
			)
		self.assertEqual(self.locked, [(["MOP-2"], ["MWO-1"])])


class TestDepartmentIRCurrentOperationWiring(IntegrationTestCase):
	"""Where DepartmentIR calls the guard, and the two cancel restorations that ship with it."""

	@classmethod
	def setUpClass(cls):
		_warm_guard_caches()

	def test_before_insert_takes_the_lock_block_for_every_new_document(self):
		for kind in ("Issue", "Receive"):
			for docstatus, phase in ((0, "save"), (1, "submit")):
				with self.subTest(kind=kind, docstatus=docstatus):
					with patch.object(guard, "_check") as check:
						guard.on_before_insert(
							_guard_dir(kind, new=True, docstatus=docstatus)
						)
					self.assertEqual(_check_call(check), (phase, True))

	def test_every_department_ir_submit_is_checked_under_locks(self):
		for kind in ("Issue", "Receive"):
			with self.subTest(kind=kind):
				with patch.object(guard, "_check") as check:
					guard.on_before_validate(
						_guard_dir(kind, docstatus=1, before=_guard_dir(kind))
					)
				self.assertEqual(_check_call(check), ("submit", True))

	def test_controller_before_insert_hands_the_document_to_the_guard(self):
		"""begin_attempt (the EOD / reconciliation-window refusal, the budget reset) first."""
		calls = []
		doc = _guard_dir(new=True)
		with (
			patch.object(
				guard, "begin_attempt", side_effect=_record(calls, "begin attempt")
			),
			patch.object(
				guard, "on_before_insert", side_effect=_record(calls, "guard")
			),
		):
			DepartmentIR.before_insert(doc)
		self.assertEqual(calls, ["begin attempt", "guard"])

	def test_a_frozen_save_or_submit_is_refused_before_any_lock(self):
		"""F2: during the EOD sync the refusal comes before the block -- for a saved draft in
		check_if_latest, for a new document in before_insert."""
		from frappe.model.document import Document

		eod = "jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.eod_lock.is_eod_sync_locked"
		saved = frappe.get_doc(
			{
				"doctype": "Department IR",
				"name": "DIR-T-0099",
				"type": "Issue",
				"docstatus": 1,
				"current_department": "Dept-A",
			}
		)
		calls = []
		with (
			patch(eod, return_value=True),
			patch.object(frappe.db, "get_value", return_value=0),
			patch.object(
				guard, "lock_before_own_rows", side_effect=_record(calls, "block")
			),
			patch.object(
				guard, "on_before_insert", side_effect=_record(calls, "new block")
			),
			patch.object(
				Document, "check_if_latest", side_effect=_record(calls, "own rows")
			),
		):
			with self.assertRaisesRegex(
				frappe.ValidationError, "EOD sync is in progress"
			):
				saved.check_if_latest()
			with self.assertRaisesRegex(
				frappe.ValidationError, "EOD sync is in progress"
			):
				DepartmentIR.before_insert(_guard_dir(new=True))
		self.assertEqual(calls, [])

	def test_check_if_latest_takes_the_block_before_frappe_locks_the_own_rows(self):
		"""Frappe's check_if_latest loads the saved document's rows FOR UPDATE (with the gap
		after them): waiting for the block only after that deadlocked with a new Department IR of
		the same work order inserting its rows into that gap (race suite)."""
		from frappe.model.document import Document

		for label, doc, expected in (
			(
				"saved document",
				frappe.get_doc(
					{
						"doctype": "Department IR",
						"name": "DIR-T-0099",
						"type": "Receive",
						"docstatus": 1,
					}
				),
				[("begin attempt", 0), "block", "own rows (Frappe)"],
			),
			("new document", frappe.new_doc("Department IR"), ["own rows (Frappe)"]),
		):
			with self.subTest(label):
				calls = []
				with (
					patch.object(frappe.db, "get_value", return_value=0),
					patch.object(
						guard,
						"begin_attempt",
						side_effect=lambda d, stored=None, calls=calls: calls.append(
							("begin attempt", stored)
						),
					),
					patch.object(
						guard,
						"lock_before_own_rows",
						side_effect=lambda d, calls=calls: calls.append("block"),
					),
					patch.object(
						Document,
						"check_if_latest",
						side_effect=lambda *a, calls=calls, **k: calls.append(
							"own rows (Frappe)"
						),
					),
				):
					doc.check_if_latest()
				self.assertEqual(calls, expected)

	def test_before_validate_guards_a_submit_although_the_checks_below_are_save_only(
		self,
	):
		calls = []
		with (
			patch.object(
				guard, "on_before_validate", side_effect=_record(calls, "guard")
			),
			patch.object(
				frappe.db, "get_value", side_effect=_record(calls, "save-only read")
			),
			patch(
				f"{_DIR_MODULE}.validate_mwo",
				side_effect=_record(calls, "validate_mwo"),
			),
		):
			DepartmentIR.before_validate(_guard_dir(docstatus=1))
		self.assertEqual(calls, ["guard", "validate_mwo"])

	def test_before_validate_guards_a_draft_save_before_its_first_read(self):
		calls = []
		with (
			patch.object(
				guard, "on_before_validate", side_effect=_record(calls, "guard")
			),
			patch.object(
				frappe.db,
				"get_value",
				side_effect=_record(
					calls, "department company read", raises=_GuardStop()
				),
			),
		):
			with self.assertRaises(_GuardStop):
				DepartmentIR.before_validate(_guard_dir())
		self.assertEqual(calls, ["guard", "department company read"])

	def test_on_update_runs_the_terminal_draft_check_for_draft_saves_only(self):
		for docstatus, expected in ((0, 1), (1, 0)):
			with self.subTest(docstatus=docstatus):
				with patch.object(guard, "final_draft_check") as final:
					DepartmentIR.on_update(_guard_dir(docstatus=docstatus))
				self.assertEqual(final.call_count, expected)

	def test_submit_ends_with_the_terminal_draft_check(self):
		for kind, expected in (
			("Issue", ["issue", "final draft check"]),
			("Receive", ["receive", "final draft check"]),
		):
			with self.subTest(kind=kind):
				calls = []
				doc = _guard_dir(kind, docstatus=1)
				doc.on_submit_issue_new = _record(calls, "issue")
				doc.on_submit_receive = _record(calls, "receive")
				with patch.object(
					guard,
					"final_draft_check",
					side_effect=_record(calls, "final draft check"),
				):
					DepartmentIR.on_submit(doc)
				self.assertEqual(calls, expected)

	def test_cancel_is_guarded_before_any_reversal(self):
		for kind, reversal in (
			("Issue", "reverse issue"),
			("Receive", "reverse receive"),
		):
			with self.subTest(kind=kind):
				calls = []
				doc = _guard_dir(kind, docstatus=2)
				doc.on_submit_issue_new = lambda cancel=False: calls.append(
					("reverse issue", cancel)
				)
				doc.on_submit_receive = lambda cancel=False: calls.append(
					("reverse receive", cancel)
				)
				with patch.object(
					guard, "guard_cancel", side_effect=_record(calls, "guard cancel")
				):
					DepartmentIR.on_cancel(doc)
				self.assertEqual(calls, ["guard cancel", (reversal, True)])

	def test_refused_cancel_reverses_nothing(self):
		calls = []
		doc = _guard_dir("Receive", docstatus=2)
		doc.on_submit_receive = lambda cancel=False: calls.append(
			("reverse receive", cancel)
		)
		with patch.object(
			guard, "guard_cancel", side_effect=guard.HistoryRewriteError("moved on")
		):
			with self.assertRaises(guard.HistoryRewriteError):
				DepartmentIR.on_cancel(doc)
		self.assertEqual(calls, [])

	def test_discard_marks_child_rows_through_the_guard(self):
		doc = _guard_dir()
		with patch.object(guard, "on_discard") as hook:
			DepartmentIR.on_discard(doc)
		hook.assert_called_once_with(doc)

	def test_issue_cancel_restores_the_source_operation_to_not_started(self):
		"""A Department Issue only takes a Not Started operation; its cancel used to leave WIP,
		which no picker accepts and which reads as employee custody."""
		set_values = []

		def get_value(doctype, filters=None, *args, **kwargs):
			if doctype == "Manufacturing Operation" and isinstance(filters, dict):
				return "MOP-NEW"
			return None

		with (
			patch(f"{_DIR_MODULE}.get_datetime", return_value="2026-01-01 12:00:00"),
			patch.object(frappe.db, "sql", return_value=[]),
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(frappe.db, "get_list", return_value=[]),
			patch.object(
				frappe.db, "set_value", side_effect=lambda *a, **k: set_values.append(a)
			),
			patch.object(
				frappe,
				"get_doc",
				side_effect=lambda doctype, name=None, *a, **k: FrappeDict(name=name),
			),
			patch.object(frappe, "delete_doc") as delete_doc,
			patch(_PC_TAGGING_SYNC),
		):
			DepartmentIR.on_submit_issue_new(
				_guard_dir("Issue", name="DIR-I-0001", docstatus=2), cancel=True
			)
		self.assertIn(
			("Manufacturing Operation", "MOP-1", "status", "Not Started"), set_values
		)
		self.assertNotIn(
			("Manufacturing Operation", "MOP-1", "status", "WIP"), set_values
		)
		self.assertIn(
			("Manufacturing Work Order", "MWO-1", "manufacturing_operation", "MOP-1"),
			set_values,
		)
		delete_doc.assert_called_once_with(
			"Manufacturing Operation", "MOP-NEW", ignore_permissions=1
		)

	def _receive_writes(self, doc, cancel):
		set_values = []

		def get_value(doctype, *args, **kwargs):
			# No Parent Manufacturing Order: the PMO / tagging block stays out of the way.
			return "WH-T" if doctype == "Warehouse" else None

		with (
			patch(f"{_DIR_MODULE}.get_datetime", return_value="2026-01-01 12:00:00"),
			patch.object(frappe.db, "sql", return_value=[]),
			patch.object(frappe.db, "get_value", side_effect=get_value),
			patch.object(frappe, "get_value", return_value="WH-T"),
			patch.object(
				frappe.db, "set_value", side_effect=lambda *a, **k: set_values.append(a)
			),
			patch.object(frappe, "get_doc", return_value=MagicMock()),
			patch(f"{_DIR_MODULE}.add_time_log"),
			patch(f"{_DIR_MODULE}.create_mop_log_for_department_ir"),
			patch(_PC_TAGGING_SYNC),
		):
			DepartmentIR.on_submit_receive(doc, cancel=cancel)
		return set_values

	def test_receive_cancel_returns_the_work_order_to_the_sending_department(self):
		doc = _guard_dir(
			"Receive", rows=(("MOP-2", "MWO-1"),), name="DIR-R-0001", docstatus=2
		)
		writes = self._receive_writes(doc, cancel=True)
		self.assertIn(
			("Manufacturing Work Order", "MWO-1", "department", "Dept-A"), writes
		)
		self.assertNotIn(
			("Manufacturing Work Order", "MWO-1", "department", "Dept-B"), writes
		)

	def test_receive_cancel_without_a_previous_department_keeps_the_current_one(self):
		doc = _guard_dir(
			"Receive", rows=(("MOP-2", "MWO-1"),), docstatus=2, previous_department=None
		)
		writes = self._receive_writes(doc, cancel=True)
		self.assertIn(
			("Manufacturing Work Order", "MWO-1", "department", "Dept-B"), writes
		)

	def test_receive_submit_moves_the_work_order_to_the_receiving_department(self):
		doc = _guard_dir("Receive", rows=(("MOP-2", "MWO-1"),), docstatus=1)
		writes = self._receive_writes(doc, cancel=False)
		self.assertIn(
			("Manufacturing Work Order", "MWO-1", "department", "Dept-B"), writes
		)
