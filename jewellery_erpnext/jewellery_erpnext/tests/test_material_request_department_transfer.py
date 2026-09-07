# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""The "Transfer to Department" route on a submitted Material Request.

``custom_operation_type`` picks which of the two final workflow actions is offered:
"Transfer to MOP" hands the material to a Manufacturing Operation, "Transfer to
Department" moves it from ``set_warehouse`` into ``custom_destination_warehouse``. The
workflow conditions make them mutually exclusive; this file covers the department half --
``make_department_transfer_stock_entry`` and the ``before_update_after_submit`` dispatch
that reaches it.

DB-free, per the app's test idiom: ``setUpClass`` is neutralised and every read is mocked.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.customization.material_request import (
	material_request as mr_custom,
)
from jewellery_erpnext.jewellery_erpnext.doc_events import material_request as mr_mod

_MR_CUSTOM = "jewellery_erpnext.jewellery_erpnext.customization.material_request.material_request"
_MR_EVENTS = "jewellery_erpnext.jewellery_erpnext.doc_events.material_request"

_COMPANY = "Gurukrupa Export Private Limited"
_SOURCE = "Diamond Bagging RSV - GEPL"
_DEST_WH = "Diamond Setting RSV - GEPL"
_DEST_DEPT = "Diamond Setting - GEPL"


class _MR:
	"""Stand-in for a submitted Material Request.

	Reads go through ``get()`` because that is how the maker reads it -- a real Document
	returns None for a field its meta does not carry, which a SimpleNamespace would raise
	on. ``db_set`` is a mock so the stamp can be asserted.
	"""

	def __init__(self, **kwargs):
		values = {
			"name": "MR-1",
			"company": _COMPANY,
			"workflow_state": "Material Transferred to Department",
			"set_warehouse": _SOURCE,
			"custom_destination_department": _DEST_DEPT,
			"custom_destination_warehouse": _DEST_WH,
			"custom_reserve_se": "SE-RESERVE",
			"custom_department_transfer_se": None,
		}
		values.update(kwargs)
		self.__dict__.update(values)
		self.db_set = MagicMock()

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


def _warehouse(department=_DEST_DEPT, company=_COMPANY, is_group=0):
	return frappe._dict(department=department, company=company, is_group=is_group)


def _submitted(workflow_state, previously):
	"""A submitted request that already existed in ``previously`` before this save.

	``before_update_after_submit`` fires on every Update, so the dispatch asks
	``get_doc_before_save`` whether the state actually moved. Passing the same value for both
	models a plain Update; a different one models a workflow action being applied.
	"""
	return SimpleNamespace(
		workflow_state=workflow_state,
		get_doc_before_save=lambda: frappe._dict(workflow_state=previously),
	)


# Distinguishes "the caller said nothing, use a valid warehouse" from "the caller wants
# frappe.db.get_value to come back empty", which None cannot.
_UNSET = object()


class TestMakeDepartmentTransferStockEntry(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, doc, warehouse=_UNSET):
		"""Run the maker with the Warehouse read and both Stock Entry calls stubbed.

		Returns the copied Stock Entry mock so the caller can assert what was built.
		"""
		se = MagicMock()
		se.name = "SE-DEPT-1"
		se.items = [
			MagicMock(material_request_item="MRI-1"),
			MagicMock(material_request_item="MRI-2"),
		]

		with patch(
			f"{_MR_CUSTOM}.frappe.db.get_value",
			return_value=_warehouse() if warehouse is _UNSET else warehouse,
		), patch(f"{_MR_CUSTOM}.frappe.get_doc"), patch(
			f"{_MR_CUSTOM}.frappe.copy_doc", return_value=se
		) as copy_doc, patch(f"{_MR_CUSTOM}.frappe.msgprint"):
			self._copy_doc = copy_doc
			mr_custom.make_department_transfer_stock_entry(doc)

		return se

	def _run_expecting_throw(self, doc, warehouse=_UNSET):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run(doc, warehouse)
		return str(ctx.exception)

	# --- guards ----------------------------------------------------------

	def test_missing_destination_department_throws(self):
		msg = self._run_expecting_throw(_MR(custom_destination_department=None))
		self.assertIn("Destination Department", msg)
		self._copy_doc.assert_not_called()

	def test_missing_destination_warehouse_throws(self):
		msg = self._run_expecting_throw(_MR(custom_destination_warehouse=None))
		self.assertIn("Destination Warehouse", msg)
		self._copy_doc.assert_not_called()

	def test_missing_set_warehouse_throws(self):
		msg = self._run_expecting_throw(_MR(set_warehouse=None))
		self.assertIn("Target Warehouse is not set", msg)
		self._copy_doc.assert_not_called()

	def test_source_equal_to_destination_throws(self):
		msg = self._run_expecting_throw(_MR(set_warehouse=_DEST_WH))
		self.assertIn("cannot be the same", msg)
		self._copy_doc.assert_not_called()

	def test_unknown_destination_warehouse_throws(self):
		msg = self._run_expecting_throw(_MR(), warehouse=None)
		self.assertIn("not found", msg)
		self._copy_doc.assert_not_called()

	def test_group_destination_warehouse_throws(self):
		msg = self._run_expecting_throw(_MR(), warehouse=_warehouse(is_group=1))
		self.assertIn("group warehouse", msg)
		self._copy_doc.assert_not_called()

	def test_warehouse_in_another_department_throws(self):
		msg = self._run_expecting_throw(
			_MR(), warehouse=_warehouse(department="Pre Polish - GEPL")
		)
		self.assertIn("Pre Polish - GEPL", msg)
		self.assertIn(_DEST_DEPT, msg)
		self._copy_doc.assert_not_called()

	def test_warehouse_department_unset_throws(self):
		msg = self._run_expecting_throw(_MR(), warehouse=_warehouse(department=None))
		self.assertIn("(not set)", msg)
		self._copy_doc.assert_not_called()

	def test_warehouse_in_another_company_throws(self):
		msg = self._run_expecting_throw(
			_MR(), warehouse=_warehouse(company="KG GK Jewellers Private Limited")
		)
		self.assertIn("KG GK Jewellers Private Limited", msg)
		self._copy_doc.assert_not_called()

	def test_missing_reserve_se_throws(self):
		msg = self._run_expecting_throw(_MR(custom_reserve_se=None))
		self.assertIn("no Reserve Stock Entry", msg)
		self._copy_doc.assert_not_called()

	# --- the stock entry -------------------------------------------------

	def test_builds_and_submits_a_department_transfer_entry(self):
		doc = _MR()
		se = self._run(doc)

		self.assertEqual(se.stock_entry_type, "Material Transfer (DEPARTMENT)")
		self.assertEqual(se.purpose, "Material Transfer")
		self.assertEqual(se.auto_created, 1)
		self.assertEqual(se.to_department, _DEST_DEPT)
		self.assertEqual(se.from_warehouse, _SOURCE)
		self.assertEqual(se.to_warehouse, _DEST_WH)
		se.save.assert_called_once()
		se.submit.assert_called_once()

	def test_routes_every_row_source_to_destination(self):
		se = self._run(_MR())

		for row in se.items:
			self.assertEqual(row.s_warehouse, _SOURCE)
			self.assertEqual(row.t_warehouse, _DEST_WH)
			self.assertEqual(row.to_department, _DEST_DEPT)
			self.assertIsNone(row.serial_and_batch_bundle)

	def test_clears_manufacturing_operation_everywhere(self):
		"""A department move must never be pulled into the MOP ledger by a copied value."""
		se = self._run(_MR())

		self.assertIsNone(se.manufacturing_operation)
		for row in se.items:
			self.assertIsNone(row.manufacturing_operation)

	def test_stamps_the_created_entry_on_the_request(self):
		doc = _MR()
		se = self._run(doc)

		doc.db_set.assert_called_once_with("custom_department_transfer_se", se.name)

	def test_leaves_the_material_request_reference_on_the_rows(self):
		"""The MR link is the row-level one; the header field stays blank on purpose --
		setting it would arm validate_material_request_warehouses, which asserts the very
		routing this transfer departs from."""
		se = self._run(_MR())

		self.assertEqual(
			[row.material_request_item for row in se.items], ["MRI-1", "MRI-2"]
		)

	# --- idempotency -----------------------------------------------------

	def test_second_run_is_a_noop(self):
		doc = _MR(custom_department_transfer_se="SE-DEPT-1")
		self._run(doc)

		self._copy_doc.assert_not_called()
		doc.db_set.assert_not_called()


class TestBeforeUpdateAfterSubmitDispatch(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _dispatch(self, doc):
		with patch.object(
			mr_mod, "make_department_transfer_stock_entry"
		) as dept_transfer, patch.object(
			mr_mod, "make_mop_stock_entry"
		) as mop, patch.object(mr_mod, "make_department_mop_stock_entry") as dept_mop:
			mr_mod.before_update_after_submit(doc, None)
		return dept_transfer, mop, dept_mop

	def test_department_state_calls_the_department_maker_only(self):
		doc = SimpleNamespace(workflow_state="Material Transferred to Department")
		dept_transfer, mop, dept_mop = self._dispatch(doc)

		dept_transfer.assert_called_once_with(doc)
		mop.assert_not_called()
		dept_mop.assert_not_called()

	def test_mop_state_never_calls_the_department_maker(self):
		doc = SimpleNamespace(
			workflow_state="Material Transferred to MOP",
			custom_manufacturing_operation="MOP-001",
			custom_department=None,
			items=[SimpleNamespace(warehouse="WH-Setting")],
		)

		def _gv(doctype, name, fieldname=None, **kwargs):
			if doctype == "Manufacturing Operation":
				return frappe._dict(
					status="Not Started", department=None, previous_mop=None
				)
			return None

		with patch(f"{_MR_EVENTS}.frappe.db.get_value", side_effect=_gv):
			dept_transfer, mop, _dept_mop = self._dispatch(doc)

		dept_transfer.assert_not_called()
		mop.assert_called_once_with(doc, mop="MOP-001")

	def test_material_transferred_state_calls_nothing(self):
		doc = SimpleNamespace(workflow_state="Material Transferred")
		dept_transfer, mop, dept_mop = self._dispatch(doc)

		dept_transfer.assert_not_called()
		mop.assert_not_called()
		dept_mop.assert_not_called()

	# --- only the save that applies the action does anything -------------

	def test_plain_update_in_the_mop_state_is_a_noop(self):
		"""The guards belong to the action, not to every Update that follows it.

		Re-running them here would compare the operation against a warehouse the material
		has already left, and the operator would have no way to save the document again.
		"""
		doc = _submitted(
			"Material Transferred to MOP", previously="Material Transferred to MOP"
		)
		dept_transfer, mop, dept_mop = self._dispatch(doc)

		dept_transfer.assert_not_called()
		mop.assert_not_called()
		dept_mop.assert_not_called()

	def test_plain_update_in_the_department_state_is_a_noop(self):
		doc = _submitted(
			"Material Transferred to Department",
			previously="Material Transferred to Department",
		)
		dept_transfer, mop, dept_mop = self._dispatch(doc)

		dept_transfer.assert_not_called()
		mop.assert_not_called()
		dept_mop.assert_not_called()

	def test_the_transition_save_still_dispatches(self):
		doc = _submitted(
			"Material Transferred to Department", previously="Material Transferred"
		)
		dept_transfer, mop, dept_mop = self._dispatch(doc)

		dept_transfer.assert_called_once_with(doc)
		mop.assert_not_called()
		dept_mop.assert_not_called()
