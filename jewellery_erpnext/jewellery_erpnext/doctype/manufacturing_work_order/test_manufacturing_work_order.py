# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order import (
	manufacturing_work_order as mwo_mod,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.test_parent_manufacturing_order import (
	create_man_plan,
)


class TestManufacturingWorkOrder(IntegrationTestCase):
	@classmethod
	def setUpClass(clas):
		clas.department = frappe.get_value(
			"Department", {"department_name": "Test_Department"}, "name"
		)
		clas.branch = frappe.get_value("Branch", {"branch_name": "Test Branch"}, "name")

		clas.warehouse = frappe.get_value(
			"Warehouse", {"warehouse_name": "Test_Warehouse"}, "name"
		)

	def test_submit_creates_manufacturing_operation_and_validates_pending_work_orders(
		self,
	):
		create_pmo(self)
		pmo = frappe.get_last_doc("Parent Manufacturing Order")
		mwo_list = frappe.get_all(
			"Manufacturing Work Order",
			filters={
				"manufacturing_order": pmo.name,
			},
			fields=["name", "department"],
		)

		for mwo_name in mwo_list:
			mwo = frappe.get_doc("Manufacturing Work Order", mwo_name.name)
			if mwo.department != "Serial Number - T":
				mwo.submit()

				mo = frappe.get_last_doc("Manufacturing Operation")
				self.assertEqual(mwo.name, mo.manufacturing_work_order)
				self.assertEqual(mwo.manufacturing_operation, mo.name)
				self.assertEqual(mwo.manufacturing_order, mo.manufacturing_order)
				self.assertEqual(mwo.manufacturing_plan, mo.manufacturing_plan)
				self.assertEqual(mwo.item_code, mo.item_code)
				self.assertEqual(mwo.master_bom, mo.design_id_bom)
				self.assertEqual(mwo.metal_type, mo.metal_type)
				self.assertEqual(mwo.metal_touch, mo.metal_touch)
				self.assertEqual(mwo.metal_colour, mo.metal_colour)
				self.assertEqual(mwo.metal_purity, mo.metal_purity)

			else:
				with self.assertRaises(frappe.ValidationError) as context:
					mwo.submit()

				self.assertIn(
					"Cannot submit. The following linked MWO(s) are not yet in",
					str(context.exception),
				)

	def test_cancel_sets_status_cancelled(self):
		create_pmo(self)
		mwo = frappe.get_last_doc(
			"Manufacturing Work Order",
			filters={"department": ["not in", ["Serial Number - T"]]},
		)
		if mwo.docstatus == 0:
			mwo.submit()

		mwo.cancel()
		mwo.reload()
		self.assertEqual(mwo.status, "Cancelled")

	def test_transfer_to_mwo_delegates_to_stock_transfer_entry(self):
		create_pmo(self)
		mwo = frappe.get_last_doc(
			"Manufacturing Work Order",
			filters={"department": ["not in", ["Serial Number - T"]]},
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.create_stock_transfer_entry"
		) as mock_transfer:
			mwo.transfer_to_mwo()
			mock_transfer.assert_called_once_with(mwo)

	def test_create_mfg_entry_delegates_to_create_se_entry(self):
		create_pmo(self)
		mwo = frappe.get_last_doc(
			"Manufacturing Work Order",
			filters={"department": ["not in", ["Serial Number - T"]]},
		)

		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_work_order.manufacturing_work_order.create_se_entry"
		) as mock_create_se:
			mwo.create_mfg_entry()
			mock_create_se.assert_called_once_with(mwo)


class TestCreateMrForSplitWorkOrder(UnitTestCase):
	"""Unit coverage for ``create_mr_for_split_work_order`` -- F-04/F-06 in the PR #1236
	review: the split-MR stage-stamp reset and MOP inheritance had no test coverage, and
	the function's behaviour when the source MWO has no ``manufacturing_operation`` yet
	was unconfirmed.

	Mocked rather than built on ``create_pmo``, and ``UnitTestCase`` rather than
	``IntegrationTestCase``: the function's own DB calls (``get_value``, ``count``,
	``get_doc``, ``copy_doc``) are simple enough to stub directly, and doing so avoids both
	the full Parent Manufacturing Order/BOM fixture chain ``create_pmo`` needs and
	``IntegrationTestCase.setUpClass``'s auto-generated test records for "Manufacturing Work
	Order" (which cascades into Company and fails on a site without that base fixture) --
	this test never touches the database at all.
	"""

	def _run(self, mwo_operation="MOP-NEW"):
		item_a = MagicMock(qty=5, pcs=3)
		item_b = MagicMock(qty=2, pcs=1)
		old_mr = MagicMock(title="MRD-LH-(TEST-001)-2")
		new_mr = MagicMock(title="MRD-LH-(TEST-001)-2", items=[item_a, item_b])
		new_mr.flags = SimpleNamespace()

		def _gv(doctype, filters, fieldname=None):
			if (
				doctype == "Manufacturing Work Order"
				and fieldname == "manufacturing_order"
			):
				return "PMO-1"
			if (
				doctype == "Manufacturing Work Order"
				and fieldname == "manufacturing_operation"
			):
				return mwo_operation
			if doctype == "Material Request":
				return "OLD-MR-1"
			return None

		with (
			patch("frappe.db.get_value", side_effect=_gv),
			patch("frappe.db.count", return_value=1),
			patch("frappe.get_doc", return_value=old_mr),
			patch("frappe.copy_doc", return_value=new_mr),
			patch("frappe.msgprint"),
		):
			mwo_mod.create_mr_for_split_work_order(
				"MWO-CHILD-1", "Test_Company", "Test_Manufacturer"
			)

		return new_mr, item_a, item_b

	def test_stage_stamps_are_cleared(self):
		"""The bug this closed: copy_doc otherwise carries the old (about-to-be-cancelled)
		MR's Reserve/MOP/Department Transfer Stock Entry links onto the new split MR."""
		new_mr, *_ = self._run()
		self.assertIsNone(new_mr.custom_reserve_se)
		self.assertIsNone(new_mr.custom_mop_se)
		self.assertIsNone(new_mr.custom_department_transfer_se)

	def test_manufacturing_operation_left_blank_for_manual_selection(self):
		"""custom_manufacturing_operation is a manual field the user picks from the dropdown
		themselves -- it must always start blank on the new split MR, regardless of what the
		source MWO's own manufacturing_operation currently is, and regardless of whatever
		copy_doc would otherwise have carried over from old_mr."""
		new_mr, *_ = self._run(mwo_operation="MOP-XYZ")
		self.assertIsNone(new_mr.custom_manufacturing_operation)

	def test_manufacturing_work_order_linked(self):
		new_mr, *_ = self._run()
		self.assertEqual(new_mr.custom_manufacturing_work_order, "MWO-CHILD-1")

	def test_item_quantities_reset_to_zero(self):
		_, item_a, item_b = self._run()
		self.assertEqual(item_a.qty, 0)
		self.assertEqual(item_a.pcs, 0)
		self.assertEqual(item_b.qty, 0)
		self.assertEqual(item_b.pcs, 0)

	def test_workflow_state_reset_to_draft(self):
		new_mr, *_ = self._run()
		self.assertEqual(new_mr.workflow_state, "Draft")

	def test_saved_with_ignore_mandatory_and_validate(self):
		new_mr, *_ = self._run()
		self.assertTrue(new_mr.flags.ignore_mandatory)
		self.assertTrue(new_mr.flags.ignore_validate)
		new_mr.save.assert_called_once()


class _ReachedOpenOperationsCheck(Exception):
	"""Raised by the stubbed frappe.get_all: reaching it means the eligibility gate and the
	split-count check both passed and create_split_work_order moved on to its open-operations
	query, before anything was written."""


MPM_LINK = "Manufacturing Plan & Management  - KGJPL"


class TestSplitWorkOrderEligibility(UnitTestCase):
	"""``create_split_work_order`` may only split a submitted, not-yet-split MWO that sits in
	Manufacturing Plan & Management with zero gross weight on both its header and its
	current Manufacturing Operation.

	Mock-only for the same reason as TestCreateMrForSplitWorkOrder. frappe.db.get_value is
	stubbed with a keyed side_effect (never a blanket return, which would also answer
	DocType meta reads), and flt's rounding method is pinned: without a bound site
	flt(x, 3) returns 0.0, which would make every "weight blocks" case pass vacuously.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# The first _() in a process builds the merged translation cache, and that build
		# reads the Translation table through frappe.get_all under suppress(Exception).
		# Left to happen inside _run, it would hit the stubbed get_all and cache an
		# incomplete map for the rest of the test run -- so build it here, unpatched.
		frappe._("Work Order")

	def _run(
		self,
		*,
		docstatus=1,
		has_split_mwo=0,
		department=MPM_LINK,
		department_name="Manufacturing Plan & Management",
		gross_wt=0,
		mop="MOP-1",
		mop_gross_wt=0,
		count=1,
		limit=0,
	):
		calls = []

		def _gv(doctype, filters=None, fieldname="name", *args, **kwargs):
			calls.append((doctype, filters, fieldname))
			if doctype == "Manufacturing Work Order" and isinstance(fieldname, list):
				return frappe._dict(
					docstatus=docstatus,
					has_split_mwo=has_split_mwo,
					department=department,
					gross_wt=gross_wt,
					manufacturing_operation=mop,
				)
			if doctype == "Department" and fieldname == "department_name":
				return department_name
			if doctype == "Manufacturing Operation" and fieldname == "gross_wt":
				return mop_gross_wt
			if doctype == "Manufacturing Setting" and fieldname == "wo_split_limit":
				return limit
			return None

		open_operation_queries = []
		real_get_all = frappe.get_all

		def _ga(doctype, *args, **kwargs):
			# Only the open-operations query is the sentinel; anything the framework itself
			# reads (e.g. Translation) goes to the real get_all.
			if doctype == "Manufacturing Operation":
				open_operation_queries.append(kwargs)
				raise _ReachedOpenOperationsCheck
			return real_get_all(doctype, *args, **kwargs)

		with (
			patch("frappe.db.get_value", side_effect=_gv),
			patch.object(
				frappe, "get_system_settings", return_value="Banker's Rounding"
			),
			patch("frappe.get_all", side_effect=_ga),
			patch("frappe.db.set_value") as set_value,
			patch.object(mwo_mod, "get_mapped_doc") as mapped,
		):
			try:
				mwo_mod.create_split_work_order("MWO-1", "Test_Company", "Labh", count)
			finally:
				set_value.assert_not_called()
				mapped.assert_not_called()
				self.calls = calls
				self.open_operation_queries = open_operation_queries

	def assert_allowed(self, **kwargs):
		with self.assertRaises(_ReachedOpenOperationsCheck):
			self._run(**kwargs)

	def assert_blocked(self, pattern, **kwargs):
		with self.assertRaisesRegex(frappe.ValidationError, pattern):
			self._run(**kwargs)
		self.assertFalse(self.open_operation_queries)

	def test_00_flt_actually_rounds_under_the_pin(self):
		with patch.object(
			frappe, "get_system_settings", return_value="Banker's Rounding"
		):
			self.assertAlmostEqual(flt(0.034, 3), 0.034, places=3)
			self.assertAlmostEqual(flt(0.1234, 3), 0.123, places=3)

	def test_mpm_with_zero_weight_is_allowed(self):
		self.assert_allowed()
		self.assertIn(("Department", MPM_LINK, "department_name"), self.calls)
		self.assertIn(("Manufacturing Operation", "MOP-1", "gross_wt"), self.calls)

	def test_existing_split_count_check_still_runs(self):
		self.assert_blocked("Invalid split count", count=0)

	def test_other_department_is_blocked_before_split_count_check(self):
		self.assert_blocked(
			"can be split only in",
			department="Waxing - KGJPL",
			department_name="Waxing",
			count=0,
		)
		self.assertFalse([c for c in self.calls if c[0] == "Manufacturing Setting"])

	def test_department_matched_on_department_name_not_link(self):
		self.assert_blocked(
			"can be split only in",
			department="Manufacturing Plan & Management - KGJPL",
			department_name="Casting",
		)

	def test_missing_department_is_blocked(self):
		self.assert_blocked("no department", department=None, department_name=None)
		self.assertFalse([c for c in self.calls if c[0] == "Department"])

	def test_header_gross_wt_blocks(self):
		self.assert_blocked("Gross Wt is 0", gross_wt=0.034)

	def test_mop_gross_wt_blocks(self):
		"""The live case: 0.171 ct of diamond issued onto an MPM operation while the MWO
		header still reads 0."""
		self.assert_blocked(
			"MOP-2609-ER55V4", mop="MOP-2609-ER55V4", mop_gross_wt=0.034
		)

	def test_smallest_rounded_weight_blocks(self):
		self.assert_blocked("Gross Wt is 0", mop_gross_wt=0.001)

	def test_negative_weight_blocks(self):
		self.assert_blocked("Gross Wt is 0", mop_gross_wt=-0.01)

	def test_sub_precision_weight_is_treated_as_zero(self):
		self.assert_allowed(gross_wt=1e-7, mop_gross_wt=0.0004)

	def test_missing_operation_is_treated_as_zero(self):
		self.assert_allowed(mop=None)
		self.assertFalse([c for c in self.calls if c[0] == "Manufacturing Operation"])

	def test_already_split_is_blocked(self):
		self.assert_blocked("already been split", has_split_mwo=1)

	def test_draft_is_blocked(self):
		self.assert_blocked("must be submitted", docstatus=0)


def create_pmo(self):
	create_man_plan(self)
	pmo = frappe.get_last_doc("Parent Manufacturing Order")
	pmo.diamond_department = "Diamond Setting - T"
	pmo.gemstone_department = "Diamond Setting - T"
	pmo.manufacturer = "Shubh"
	pmo.save()
	pmo.submit()
	return pmo
