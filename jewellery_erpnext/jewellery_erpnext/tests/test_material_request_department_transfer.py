# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""The "Transfer to Department" route on a submitted Material Request.

``custom_operation_type`` picks which of the two final workflow actions is offered:
"Transfer to MOP" hands the material to a Manufacturing Operation, "Transfer to
Department" sends it from ``set_warehouse`` through the transit warehouse of
``custom_destination_warehouse``, where the receiving department's End Transit lands it. The
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
_DEST_TRANSIT = "Diamond Setting Transit - GEPL"
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
			# The deferred From Reserve entry has already brought the material into
			# set_warehouse -- the precondition validate_from_reserve_done enforces.
			"custom_transfer_se": "SE-XFER",
			"custom_transfer_se_state": "Done",
			"custom_department_transfer_se": None,
		}
		values.update(kwargs)
		self.__dict__.update(values)
		self.db_set = MagicMock()

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


def _warehouse(
	department=_DEST_DEPT,
	company=_COMPANY,
	is_group=0,
	warehouse_type="Reserve",
	default_in_transit_warehouse=_DEST_TRANSIT,
):
	return frappe._dict(
		department=department,
		company=company,
		is_group=is_group,
		warehouse_type=warehouse_type,
		default_in_transit_warehouse=default_in_transit_warehouse,
	)


def _submitted(
	workflow_state,
	previously,
	material_request_type="Manufacture",
	custom_operation_type="Transfer to MOP",
	custom_manufacturing_operation=None,
	warehouse="WH-Setting",
):
	"""A submitted request that already existed in ``previously`` before this save.

	``before_update_after_submit`` fires on every Update, so the dispatch asks
	``get_doc_before_save`` whether the state actually moved. Passing the same value for both
	models a plain Update; a different one models a workflow action being applied.

	The last four are stated rather than left off because the hook now also carries the
	save-time department gate, whose predicate reads all of them. A document missing them
	skips that gate by accident, which would make every noop assertion below vacuous.
	"""
	return SimpleNamespace(
		workflow_state=workflow_state,
		material_request_type=material_request_type,
		custom_operation_type=custom_operation_type,
		custom_manufacturing_operation=custom_manufacturing_operation,
		items=[SimpleNamespace(warehouse=warehouse)],
		get_doc_before_save=lambda: frappe._dict(workflow_state=previously),
	)


# Distinguishes "the caller said nothing, use a valid warehouse" from "the caller wants
# frappe.db.get_value to come back empty", which None cannot.
_UNSET = object()


class TestMakeDepartmentTransferStockEntry(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, doc, warehouse=_UNSET, from_reserve_se="SE-XFER"):
		"""Run the maker with the Warehouse read and both Stock Entry calls stubbed.

		``from_reserve_se`` is what the From Reserve lookup finds for the request and the
		entry named in ``custom_transfer_se`` (None: no such submitted entry).

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
		) as copy_doc, patch(f"{_MR_CUSTOM}.frappe.msgprint"), patch(
			f"{_MR_CUSTOM}.get_submitted_from_reserve_se", return_value=from_reserve_se
		) as from_reserve_lookup:
			self._copy_doc = copy_doc
			self._from_reserve_lookup = from_reserve_lookup
			mr_custom.make_department_transfer_stock_entry(doc)

		return se

	def _run_expecting_throw(self, doc, warehouse=_UNSET, from_reserve_se="SE-XFER"):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run(doc, warehouse, from_reserve_se)
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

	def test_transit_destination_warehouse_throws(self):
		"""The material goes through the destination's own transit warehouse; the
		destination itself must be where the receipt lands it."""
		msg = self._run_expecting_throw(
			_MR(), warehouse=_warehouse(warehouse_type="Transit")
		)
		self.assertIn("Transit warehouse", msg)
		self._copy_doc.assert_not_called()

	def test_destination_without_transit_warehouse_throws(self):
		msg = self._run_expecting_throw(
			_MR(), warehouse=_warehouse(default_in_transit_warehouse=None)
		)
		self.assertIn("Transit warehouse is not mentioned", msg)
		self.assertIn(_DEST_WH, msg)
		self._copy_doc.assert_not_called()

	# --- the From Reserve precondition -----------------------------------

	def test_pending_from_reserve_entry_throws(self):
		msg = self._run_expecting_throw(
			_MR(custom_transfer_se=None, custom_transfer_se_state="Pending")
		)
		self.assertIn("still being created", msg)
		self._copy_doc.assert_not_called()

	def test_failed_from_reserve_entry_throws_with_its_error(self):
		msg = self._run_expecting_throw(
			_MR(
				custom_transfer_se=None,
				custom_transfer_se_state="Failed",
				custom_transfer_se_error="cannot import name '_get_incoming_rate'",
			)
		)
		self.assertIn("failed", msg)
		self.assertIn("_get_incoming_rate", msg)
		self.assertIn(_SOURCE, msg)
		self._copy_doc.assert_not_called()

	def test_missing_from_reserve_entry_throws(self):
		msg = self._run_expecting_throw(
			_MR(custom_transfer_se=None, custom_transfer_se_state=None)
		)
		self.assertIn("No submitted Material Transfer From Reserve", msg)
		self._copy_doc.assert_not_called()

	def test_stale_from_reserve_stamp_throws(self):
		"""A request split off another carries the parent's "Done" stamp, naming an entry
		whose rows moved the parent's material -- the lookup, not the stamp, decides."""
		msg = self._run_expecting_throw(_MR(), from_reserve_se=None)
		self.assertIn("No submitted Material Transfer From Reserve", msg)
		self._from_reserve_lookup.assert_called_once_with("MR-1", "SE-XFER")
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
		self.assertEqual(se.to_warehouse, _DEST_TRANSIT)
		se.save.assert_called_once()
		se.submit.assert_called_once()

	def test_sends_the_material_into_transit(self):
		"""ERPNext v16.36.0 accepts Add to Transit only into a Transit warehouse, so the
		entry goes to the destination's transit warehouse with the flag on -- set here, not
		left to the type's fetch -- and is never held out of transit."""
		se = self._run(_MR())

		self.assertEqual(se.add_to_transit, 1)
		self.assertIsNot(se.flags.no_transit, True)

	def test_routes_every_row_into_the_destination_transit_warehouse(self):
		se = self._run(_MR())

		for row in se.items:
			self.assertEqual(row.s_warehouse, _SOURCE)
			self.assertEqual(row.t_warehouse, _DEST_TRANSIT)
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


class TestValidateDepartmentTransferReceived(IntegrationTestCase):
	"""Transfer to MOP reads the material from custom_destination_warehouse, which it only
	reaches once the receiving department ends the Transfer to Department's transit."""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, transfer, non_transit=False, doc=None):
		"""Run the guard with the department transfer's Stock Entry read answered by
		``transfer``; every other ``get_value`` falls through to the real one."""
		real_get_value = frappe.db.get_value
		reads = []

		def _get_value(doctype, filters=None, fieldname="name", *args, **kwargs):
			if doctype != "Stock Entry":
				return real_get_value(doctype, filters, fieldname, *args, **kwargs)
			reads.append(filters)
			return transfer

		doc = doc or _MR(custom_department_transfer_se="SE-DEPT-1")
		with patch.object(
			mr_custom.frappe.db, "get_value", side_effect=_get_value
		), patch.object(
			mr_custom, "has_non_transit_target", return_value=non_transit
		) as non_transit_check:
			mr_custom.validate_department_transfer_received(doc)
		return reads, non_transit_check

	def _run_expecting_throw(self, transfer, non_transit=False):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run(transfer, non_transit)
		return str(ctx.exception)

	def test_material_still_in_transit_throws(self):
		msg = self._run_expecting_throw(
			frappe._dict(add_to_transit=1, per_transferred=0)
		)
		self.assertIn("still in transit", msg)
		self.assertIn("SE-DEPT-1", msg)
		self.assertIn(_DEST_WH, msg)

	def test_partly_received_transfer_throws(self):
		msg = self._run_expecting_throw(
			frappe._dict(add_to_transit=1, per_transferred=40)
		)
		self.assertIn("still in transit", msg)

	def test_received_transfer_passes(self):
		_, non_transit_check = self._run(
			frappe._dict(add_to_transit=1, per_transferred=100)
		)
		non_transit_check.assert_not_called()

	def test_old_direct_transfer_is_not_waited_on(self):
		"""Entries made before this became a transit leg carry add_to_transit too, but
		moved the material straight into the destination."""
		self._run(frappe._dict(add_to_transit=1, per_transferred=0), non_transit=True)

	def test_one_shot_transfer_passes(self):
		_, non_transit_check = self._run(
			frappe._dict(add_to_transit=0, per_transferred=0)
		)
		non_transit_check.assert_not_called()

	def test_request_without_a_department_transfer_reads_nothing(self):
		reads, _ = self._run(None, doc=_MR())
		self.assertEqual(reads, [])


class TestDepartmentTransferIsFrozen(IntegrationTestCase):
	"""Once the Transfer to Department entry exists, where it is going must not move.

	The destination fields are allow_on_submit and locked only by the form's
	``read_only_depends_on``, so an Update through the API or ``set_value`` still reached the
	database -- and End Transit then landed stock already in transit wherever the request
	pointed by then.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	_SENT = {
		"workflow_state": "Material Transferred to Department",
		"custom_operation_type": "Transfer to Department",
		"custom_destination_department": _DEST_DEPT,
		"custom_destination_warehouse": _DEST_WH,
		"custom_department_transfer_se": "SE-DEPT-1",
	}

	def _doc(self, before=None, **changes):
		"""The request as this save carries it; ``before`` is its previous version,
		which defaults to the one the transfer entry was made from."""
		values = dict(self._SENT, **changes)
		doc = SimpleNamespace(**values)
		previous = frappe._dict(self._SENT if before is None else before)
		doc.get_doc_before_save = lambda: previous
		return doc

	def _run_expecting_throw(self, doc):
		with self.assertRaises(frappe.ValidationError) as ctx:
			mr_mod.validate_department_transfer_frozen(doc)
		return str(ctx.exception)

	def test_changing_the_destination_warehouse_throws(self):
		msg = self._run_expecting_throw(
			self._doc(custom_destination_warehouse="Pre Polish RSV - GEPL")
		)
		self.assertIn("Destination Warehouse", msg)
		self.assertIn("SE-DEPT-1", msg)
		self.assertIn(_DEST_WH, msg)

	def test_changing_the_destination_department_throws(self):
		msg = self._run_expecting_throw(
			self._doc(custom_destination_department="Pre Polish - GEPL")
		)
		self.assertIn("Destination Department", msg)

	def test_clearing_the_transfer_link_throws(self):
		"""Otherwise one save could clear it and the next move the destination."""
		msg = self._run_expecting_throw(self._doc(custom_department_transfer_se=None))
		self.assertIn("Department Transfer SE", msg)

	def test_unchanged_request_passes(self):
		mr_mod.validate_department_transfer_frozen(self._doc())

	def test_other_fields_may_still_change(self):
		"""The request goes on to Transfer to MOP from this state."""
		mr_mod.validate_department_transfer_frozen(
			self._doc(
				custom_operation_type="Transfer to MOP",
				custom_manufacturing_operation="MOP-1",
			)
		)

	def test_blank_and_unset_count_as_unchanged(self):
		before = dict(self._SENT, custom_destination_department="")
		mr_mod.validate_department_transfer_frozen(
			self._doc(before=before, custom_destination_department=None)
		)

	def test_the_save_that_makes_the_transfer_passes(self):
		"""The maker stamps the entry with db_set, so the save that runs it -- where the
		operator picked the destination -- starts from a version without it."""
		before = dict(
			self._SENT,
			workflow_state="Material Transferred",
			custom_destination_warehouse=None,
			custom_department_transfer_se=None,
		)
		mr_mod.validate_department_transfer_frozen(self._doc(before=before))

	def test_document_without_a_previous_version_passes(self):
		mr_mod.validate_department_transfer_frozen(SimpleNamespace(**self._SENT))

	def test_update_after_submit_checks_before_anything_else(self):
		"""A plain Update or API save reaches before_update_after_submit too; the lock runs
		first, so a refused save never reaches a Stock Entry maker."""
		doc = _submitted(
			"Material Transferred to Department",
			"Material Transferred",
			custom_operation_type="Transfer to Department",
		)
		with patch.object(
			mr_mod,
			"validate_department_transfer_frozen",
			side_effect=frappe.ValidationError("frozen"),
		) as frozen, patch.object(
			mr_mod, "make_department_transfer_stock_entry"
		) as maker:
			with self.assertRaises(frappe.ValidationError):
				mr_mod.before_update_after_submit(doc, None)

		frozen.assert_called_once_with(doc)
		maker.assert_not_called()


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
		doc = SimpleNamespace(
			workflow_state="Material Transferred to Department",
			material_request_type="Manufacture",
			custom_operation_type="Transfer to Department",
		)
		dept_transfer, mop, dept_mop = self._dispatch(doc)

		dept_transfer.assert_called_once_with(doc)
		mop.assert_not_called()
		dept_mop.assert_not_called()

	def test_mop_state_never_calls_the_department_maker(self):
		doc = SimpleNamespace(
			workflow_state="Material Transferred to MOP",
			material_request_type="Manufacture",
			custom_operation_type="Transfer to MOP",
			custom_manufacturing_operation="MOP-001",
			custom_department=None,
			items=[SimpleNamespace(warehouse="WH-Setting")],
		)

		def _gv(doctype, name, fieldname=None, **kwargs):
			if doctype == "Manufacturing Operation":
				return frappe._dict(status="Not Started", department=None)
			return None

		with patch(f"{_MR_EVENTS}.frappe.db.get_value", side_effect=_gv):
			dept_transfer, mop, _dept_mop = self._dispatch(doc)

		dept_transfer.assert_not_called()
		# Both departments come back None, which the rule treats as a match.
		mop.assert_called_once_with(doc, mop="MOP-001")

	def test_material_transferred_state_makes_no_stock_entry(self):
		"""Not "does nothing" any more -- a plain Update in this state does run the
		department gate. It just never mints a Stock Entry."""
		doc = SimpleNamespace(
			workflow_state="Material Transferred",
			material_request_type="Manufacture",
			custom_operation_type="Transfer to MOP",
			custom_manufacturing_operation=None,
		)
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

	def test_plain_update_in_the_department_state_makes_no_stock_entry(self):
		"""With no operation selected the department gate has nothing to assert, so this
		save reaches neither half of the hook."""
		doc = _submitted(
			"Material Transferred to Department",
			previously="Material Transferred to Department",
			custom_manufacturing_operation=None,
		)
		dept_transfer, mop, dept_mop = self._dispatch(doc)

		dept_transfer.assert_not_called()
		mop.assert_not_called()
		dept_mop.assert_not_called()

	def test_the_transition_save_still_dispatches(self):
		doc = _submitted(
			"Material Transferred to Department",
			previously="Material Transferred",
			custom_operation_type="Transfer to Department",
		)
		dept_transfer, mop, dept_mop = self._dispatch(doc)

		dept_transfer.assert_called_once_with(doc)
		mop.assert_not_called()
		dept_mop.assert_not_called()

	def test_the_department_check_runs_above_the_transition_gate(self):
		"""The cross-file companion to TestMopDepartmentCheckOnSave.

		A plain Update -- same state before and after, so the dispatch below returns early
		-- still has to reject an operation in the wrong department. If the check sat below
		that gate this save would pass silently, which is the bug being fixed.
		"""
		doc = _submitted(
			"Material Transferred",
			previously="Material Transferred",
			custom_manufacturing_operation="MOP-001",
			warehouse="WH-Setting",
		)

		def _gv(doctype, name, fieldname=None, **kwargs):
			if doctype == "Manufacturing Operation":
				return "Pre Polish - GEPL"
			if doctype == "Warehouse":
				return "Diamond Setting - GEPL"
			return None

		with patch(f"{_MR_EVENTS}.frappe.db.get_value", side_effect=_gv):
			with self.assertRaises(frappe.ValidationError) as ctx:
				self._dispatch(doc)

		self.assertIn("Diamond Setting - GEPL", str(ctx.exception))
		self.assertIn("Pre Polish - GEPL", str(ctx.exception))
