# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Tests for the CAD / CAM receive details (doc_events/cad_cam_receive.py) and the
Item Master sync they feed (ManufacturingOperation.attach_cad_cam_file_into_item_master).

Nothing here touches the database: the Department Operation flag lookup, Item reads and the
Cam Weight Detail writes are patched, like the other mocked classes in test_employee_ir.py.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.types.frappedict import _dict as FrappeDict

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	cad_cam_receive as mod,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	ManufacturingOperation,
)

MODULE = (
	"jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.cad_cam_receive"
)

CAD_ROW = {
	"cad_file": "/files/a.dxf",
	"cad_numbering_file": "/files/n.dxf",
	"estimated_rpt_wt": 1.25,
	"ref_name": "REF-1",
	"estimated_casting_wt": 3.5,
}
CAM_ROW = {"support_cam_file": "/files/s.stl", "platform_wt": 2.5}


def _doc(type_="Receive", operation="RPT Digital File Generation", *rows):
	"""An Employee IR stand-in with one FrappeDict per row (idx counts from 1)."""
	return FrappeDict(
		{
			"type": type_,
			"operation": operation,
			"receive_details_type": None,
			"employee_ir_operations": [
				FrappeDict({"idx": i, **row}) for i, row in enumerate(rows, start=1)
			],
		}
	)


class TestGetReceiveDetailsType(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _lookup(self, stored, operation="Anything"):
		with patch(f"{MODULE}.frappe.db.get_value", return_value=stored) as gv:
			result = mod.get_receive_details_type(operation)
		return result, gv

	def test_cad_and_cam_are_returned(self):
		self.assertEqual(self._lookup("CAD")[0], "CAD")
		self.assertEqual(self._lookup("CAM")[0], "CAM")

	def test_blank_is_none(self):
		self.assertIsNone(self._lookup("")[0])
		self.assertIsNone(self._lookup(None)[0])

	def test_unknown_flag_value_is_treated_as_no_flag(self):
		self.assertIsNone(self._lookup("Foo")[0])

	def test_no_operation_skips_the_lookup(self):
		with patch(f"{MODULE}.frappe.db.get_value") as gv:
			self.assertIsNone(mod.get_receive_details_type(None))
		gv.assert_not_called()


@patch(f"{MODULE}.get_receive_details_type")
class TestValidateCadCamReceiveDetails(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_cad_receive_with_all_fields_passes(self, flag):
		flag.return_value = "CAD"
		doc = _doc("Receive", "RPT Digital File Generation", CAD_ROW)
		mod.validate_cad_cam_receive_details(doc)
		self.assertEqual(doc.receive_details_type, "CAD")

	def test_cad_receive_with_nothing_filled_lists_all_five(self, flag):
		flag.return_value = "CAD"
		with self.assertRaises(frappe.ValidationError) as ctx:
			mod.validate_cad_cam_receive_details(
				_doc("Receive", "RPT Digital File Generation", {})
			)
		message = str(ctx.exception)
		for label in mod.CAD_FIELDS.values():
			self.assertIn(label, message)

	def test_cad_receive_with_zero_estimated_rpt_wt_is_refused(self, flag):
		flag.return_value = "CAD"
		row = {**CAD_ROW, "estimated_rpt_wt": 0}
		with self.assertRaises(frappe.ValidationError) as ctx:
			mod.validate_cad_cam_receive_details(
				_doc("Receive", "RPT Digital File Generation", row)
			)
		self.assertIn("Estimated RPT WT", str(ctx.exception))

	def test_cad_receive_carrying_a_cam_field_is_refused(self, flag):
		flag.return_value = "CAD"
		row = {**CAD_ROW, "platform_wt": 1}
		with self.assertRaises(frappe.ValidationError) as ctx:
			mod.validate_cad_cam_receive_details(
				_doc("Receive", "RPT Digital File Generation", row)
			)
		self.assertIn("not applicable", str(ctx.exception))
		self.assertIn("Platform WT", str(ctx.exception))

	def test_cam_receive_with_both_fields_passes(self, flag):
		flag.return_value = "CAM"
		doc = _doc("Receive", "RPT Support Generation", CAM_ROW)
		mod.validate_cad_cam_receive_details(doc)
		self.assertEqual(doc.receive_details_type, "CAM")

	def test_cam_receive_with_platform_wt_missing_is_refused(self, flag):
		flag.return_value = "CAM"
		with self.assertRaises(frappe.ValidationError) as ctx:
			mod.validate_cad_cam_receive_details(
				_doc(
					"Receive",
					"RPT Support Generation",
					{"support_cam_file": "/files/s.stl"},
				)
			)
		self.assertIn("Platform WT", str(ctx.exception))

	def test_cam_receive_carrying_a_cad_field_is_refused(self, flag):
		flag.return_value = "CAM"
		row = {**CAM_ROW, "cad_file": "/files/a.dxf"}
		with self.assertRaises(frappe.ValidationError) as ctx:
			mod.validate_cad_cam_receive_details(
				_doc("Receive", "RPT Support Generation", row)
			)
		self.assertIn("not applicable", str(ctx.exception))
		self.assertIn("CAD File", str(ctx.exception))

	def test_unflagged_receive_with_no_values_passes(self, flag):
		flag.return_value = None
		doc = _doc("Receive", "Casting", {})
		mod.validate_cad_cam_receive_details(doc)
		self.assertIsNone(doc.receive_details_type)

	def test_unflagged_receive_with_a_posted_value_is_refused(self, flag):
		flag.return_value = None
		with self.assertRaises(frappe.ValidationError) as ctx:
			mod.validate_cad_cam_receive_details(
				_doc("Receive", "Casting", {"ref_name": "x"})
			)
		self.assertIn("not applicable", str(ctx.exception))

	def test_issue_on_a_cad_operation_is_not_looked_up_and_needs_nothing(self, flag):
		flag.return_value = "CAD"  # must not be consulted for an Issue
		doc = _doc("Issue", "RPT Digital File Generation", {})
		mod.validate_cad_cam_receive_details(doc)
		flag.assert_not_called()
		self.assertIsNone(doc.receive_details_type)

	def test_issue_on_a_cad_operation_with_cad_values_is_refused(self, flag):
		flag.return_value = "CAD"
		with self.assertRaises(frappe.ValidationError):
			mod.validate_cad_cam_receive_details(
				_doc("Issue", "RPT Digital File Generation", CAD_ROW)
			)

	def test_error_names_the_offending_row(self, flag):
		flag.return_value = "CAD"
		bad = {**CAD_ROW, "ref_name": ""}
		with self.assertRaises(frappe.ValidationError) as ctx:
			mod.validate_cad_cam_receive_details(
				_doc("Receive", "RPT Digital File Generation", CAD_ROW, bad)
			)
		self.assertIn("Row 2", str(ctx.exception))
		self.assertNotIn("Row 1", str(ctx.exception))


@patch(f"{MODULE}.get_receive_details_type")
class TestCadCamValuesForOperation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_cad_submit_returns_the_row_values_and_the_flag(self, flag):
		flag.return_value = "CAD"
		values = mod.cad_cam_values_for_operation(
			FrappeDict({"type": "Receive", "operation": "x"}), FrappeDict(CAD_ROW)
		)
		self.assertEqual(values, {"receive_details_type": "CAD", **CAD_ROW})

	def test_cam_submit_returns_only_the_cam_values(self, flag):
		flag.return_value = "CAM"
		values = mod.cad_cam_values_for_operation(
			FrappeDict({"type": "Receive", "operation": "x"}),
			FrappeDict({**CAM_ROW, "ref_name": "ignored"}),
		)
		self.assertEqual(values, {"receive_details_type": "CAM", **CAM_ROW})

	def test_cancel_blanks_files_and_text_but_zeroes_weights(self, flag):
		"""Float columns are NOT NULL: writing None to one crashed the cancel."""
		flag.return_value = "CAD"
		values = mod.cad_cam_values_for_operation(
			FrappeDict({"type": "Receive", "operation": "x"}),
			FrappeDict(CAD_ROW),
			cancel=True,
		)
		self.assertIsNone(values["cad_file"])
		self.assertIsNone(values["cad_numbering_file"])
		self.assertIsNone(values["ref_name"])
		self.assertEqual(values["estimated_rpt_wt"], 0)
		self.assertEqual(values["estimated_casting_wt"], 0)

	def test_unflagged_operation_writes_nothing(self, flag):
		flag.return_value = None
		self.assertEqual(
			mod.cad_cam_values_for_operation(
				FrappeDict({"type": "Receive", "operation": "x"}), FrappeDict(CAD_ROW)
			),
			{},
		)

	def test_issue_writes_nothing(self, flag):
		flag.return_value = "CAD"
		self.assertEqual(
			mod.cad_cam_values_for_operation(
				FrappeDict({"type": "Issue", "operation": "x"}), FrappeDict(CAD_ROW)
			),
			{},
		)


class TestGetEstimatedRptWt(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, rows, ratio=4, item="ITEM-1"):
		with (
			patch(f"{MODULE}.frappe.db.get_value", return_value=item),
			patch(f"{MODULE}.frappe.has_permission", return_value=True),
			patch(
				f"{MODULE}.frappe.get_all", return_value=[FrappeDict(r) for r in rows]
			),
			patch(f"{MODULE}.frappe.db.get_single_value", return_value=ratio),
			# flt(x, 3) loads System Settings (into a cache) on first use. Left alone, that
			# load would run against the patched db.get_value above and cache garbage.
			patch("frappe.get_system_settings", return_value="Banker's Rounding"),
		):
			return mod.get_estimated_rpt_wt("MOP-1")

	def test_stored_value_wins(self):
		self.assertEqual(
			self._run([{"cad_weight": 9, "estimated_rpt_wt": 2.346}]), 2.346
		)

	def test_works_it_out_from_cad_weight_when_none_is_stored(self):
		self.assertEqual(
			self._run([{"cad_weight": 10, "estimated_rpt_wt": 0}], ratio=4), 2.5
		)

	def test_skips_rows_without_a_weight(self):
		rows = [
			{"cad_weight": 0, "estimated_rpt_wt": 0},
			{"cad_weight": 8, "estimated_rpt_wt": 0},
		]
		self.assertEqual(self._run(rows, ratio=4), 2.0)

	def test_zero_ratio_gives_zero_not_a_division_error(self):
		self.assertEqual(
			self._run([{"cad_weight": 10, "estimated_rpt_wt": 0}], ratio=0), 0
		)

	def test_item_with_no_rows_gives_zero(self):
		self.assertEqual(self._run([]), 0)

	def test_operation_with_no_item_gives_zero(self):
		self.assertEqual(
			self._run([{"cad_weight": 10, "estimated_rpt_wt": 0}], item=None), 0
		)

	def test_item_read_permission_is_checked(self):
		with (
			patch(f"{MODULE}.frappe.db.get_value", return_value="ITEM-1"),
			patch(
				f"{MODULE}.frappe.has_permission", side_effect=frappe.PermissionError
			) as perm,
			patch(f"{MODULE}.frappe.get_all") as get_all,
		):
			with self.assertRaises(frappe.PermissionError):
				mod.get_estimated_rpt_wt("MOP-1")
		perm.assert_called_once()
		get_all.assert_not_called()


class TestAttachCadCamFileIntoItemMaster(IntegrationTestCase):
	"""The Item Master sync. Called unbound on a stand-in, so no Manufacturing Operation is built."""

	@classmethod
	def setUpClass(cls):
		pass

	@staticmethod
	def _mop(existing_child=None, **fields):
		values = {
			"name": "MOP-1",
			"item_code": "ITEM-1",
			"manufacturer": "Shubh",
			"operation": "Casting",
			"receive_details_type": None,
			"cad_file": None,
			"cad_numbering_file": None,
			"estimated_rpt_wt": 0,
			"ref_name": None,
			"estimated_casting_wt": 0,
			"support_cam_file": None,
			"platform_wt": 0,
			"rpt_wt_issue": 0,
			"rpt_wt_receive": 0,
			"rpt_wt_loss": 0,
			**fields,
		}
		mop = MagicMock()
		mop.configure_mock(**values)
		mop.get.side_effect = values.get
		mop.get_existing_child.return_value = existing_child
		return mop

	def _sync(self, mop, mapping_operations=()):
		rows = [FrappeDict({"operation": op}) for op in mapping_operations]
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_all",
			return_value=rows,
		):
			ManufacturingOperation.attach_cad_cam_file_into_item_master(mop)

	def test_flagged_operation_with_details_adds_one_row_with_all_fields(self):
		mop = self._mop(receive_details_type="CAD", **CAD_ROW)
		self._sync(mop)
		mop.add_child_record.assert_called_once()
		_parent_dt, parent, child_dt, fields = mop.add_child_record.call_args.args
		self.assertEqual((parent, child_dt), ("ITEM-1", "Cam Weight Detail"))
		self.assertEqual(fields["mop_reference"], "MOP-1")
		for fieldname, value in CAD_ROW.items():
			self.assertEqual(fields[fieldname], value)

	def test_unflagged_operation_not_in_the_mapping_adds_nothing(self):
		mop = self._mop(**CAD_ROW)
		self._sync(mop)
		mop.add_child_record.assert_not_called()

	def test_unflagged_operation_in_the_mapping_still_adds_a_row(self):
		"""The CAM Weight Details Mapping path is unchanged."""
		mop = self._mop(operation="Mould Filling", platform_wt=2)
		self._sync(mop, mapping_operations=["Mould Filling"])
		mop.add_child_record.assert_called_once()

	def test_existing_row_is_updated_not_duplicated(self):
		child = MagicMock()
		mop = self._mop(existing_child=child, receive_details_type="CAD", **CAD_ROW)
		self._sync(mop)
		mop.add_child_record.assert_not_called()
		updated = child.update.call_args.args[0]
		self.assertEqual(updated["cad_file"], "/files/a.dxf")
		self.assertEqual(updated["ref_name"], "REF-1")
		self.assertEqual(updated["estimated_casting_wt"], 3.5)
		child.save.assert_called_once()

	def test_row_is_looked_up_by_the_operation_only(self):
		"""Not by ref_name: it is typed by the CAD user and used to hide the row."""
		mop = self._mop(receive_details_type="CAD", **CAD_ROW)
		self._sync(mop)
		mop.get_existing_child.assert_called_once_with(
			"Item", "ITEM-1", "Cam Weight Detail", "MOP-1"
		)

	def test_get_existing_child_filters_on_mop_reference_not_ref_name(self):
		"""mop_series holds the operation's name, so also filtering it by ref_name never matched
		once ref_name was typed by the user, and every save added a duplicate row."""
		mop = self._mop(ref_name="typed-by-the-user")
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_all",
			return_value=[],
		) as get_all:
			result = ManufacturingOperation.get_existing_child(
				mop, "Item", "ITEM-1", "Cam Weight Detail", "MOP-1"
			)
		self.assertIsNone(result)
		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["mop_reference"], "MOP-1")
		self.assertNotIn("mop_series", filters)

	def test_new_row_is_inserted(self):
		child = MagicMock()
		with patch(
			"jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation.frappe.get_doc",
			return_value=child,
		):
			ManufacturingOperation.add_child_record(
				MagicMock(),
				"Item",
				"ITEM-1",
				"Cam Weight Detail",
				{"cad_file": "/files/a.dxf"},
			)
		child.insert.assert_called_once()
