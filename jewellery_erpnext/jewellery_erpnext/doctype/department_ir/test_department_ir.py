# # Copyright (c) 2026, Nirali and Contributors
# # See license.txt
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.types.frappedict import _dict as FrappeDict

from jewellery_erpnext.jewellery_erpnext.doctype.department_ir.department_ir import (
	DepartmentIR,
	add_time_log_optimize,
	department_receive_query,
	fetch_and_update,
	get_manufacturing_operations,
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
	mwo = frappe.get_last_doc("Manufacturing Work Order")
	mo = frappe.new_doc("Manufacturing Operation")
	mo.department = "Manufacturing Plan & Management - T"
	mo.manufacturer = "Shubh"
	mo.manufacturing_work_order = mwo.name
	mo.manufacturing_order = mwo.manufacturing_order
	mo.manufacturing_plan = mwo.manufacturing_plan
	mo.type = "Manufacturing Work Order"
	mo.operation = "Manufacturing Plan & Management"
	mo.item_code = mwo.item_code
	mo.design_id_bom = mwo.master_bom
	mo.metal_type = mwo.metal_type
	mo.metal_touch = mwo.metal_touch
	mo.metal_colour = mwo.metal_colour
	mo.meatal_purity = mwo.metal_purity

	mo.save()

	return mo


_TOL_MODULE = "jewellery_erpnext.jewellery_erpnext.doctype.department_ir.doc_events.product_tolerance"


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
