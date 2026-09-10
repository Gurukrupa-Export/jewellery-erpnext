# Copyright (c) 2023, Nirali and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

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

	def test_manufacturing_operation_inherited_from_mwo(self):
		new_mr, *_ = self._run(mwo_operation="MOP-XYZ")
		self.assertEqual(new_mr.custom_manufacturing_operation, "MOP-XYZ")

	def test_manufacturing_operation_is_none_when_mwo_has_none(self):
		"""F-06: the source MWO's manufacturing_operation being unset must not raise --
		the field is simply left blank, for before_validate to derive later if it can."""
		new_mr, *_ = self._run(mwo_operation=None)
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


def create_pmo(self):
	create_man_plan(self)
	pmo = frappe.get_last_doc("Parent Manufacturing Order")
	pmo.diamond_department = "Diamond Setting - T"
	pmo.gemstone_department = "Diamond Setting - T"
	pmo.manufacturer = "Shubh"
	pmo.save()
	pmo.submit()
	return pmo
