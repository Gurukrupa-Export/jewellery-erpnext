# Copyright (c) 2026, Nirali and Contributors
# See license.txt

"""Metal Conversions Chain Subcontracting (doc_events/chain_subcontracting.py).

Pure-logic suite: every master read and every Stock Entry / Purchase Order is stubbed, so it
runs on any site and says nothing about the stock ledger -- the desk walk-through in the PR
covers that. Stubs are keyed on what they answer (never a blanket ``frappe.db.get_value``),
and ``flt`` is replaced by a deterministic shim, because frappe's own returns 0.0 when the
rounding setting cannot be read.

Lock order is asserted by recording the calls into ``lock_order``; whether two real requests
deadlock is not something a stub can show.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import frappe
from frappe import _
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions import (
	metal_conversions as mc,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events import (
	chain_subcontracting as cs,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events import (
	lanes as lanes_mod,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events import (
	melting_loss,
)
from jewellery_erpnext.patches import add_metal_conversion_chain_sc as chain_sc_patch
from jewellery_erpnext.patches import add_metal_conversion_workflow as workflow_patch
from jewellery_erpnext.patches import (
	metal_conversion_receive_submits_directly as receive_patch,
)
from jewellery_erpnext.patches import (
	remove_unused_chain_sc_stock_entry_type as cleanup_patch,
)
from jewellery_erpnext.patches import seed_stock_entry_types as seed_patch

_REAL_GET_ALL = frappe.get_all
# _ChainCase stubs it; kept to test the real lookup.
_REAL_GET_EMPLOYEE_WAREHOUSE = cs.get_employee_warehouse

_LOCK_ORDER = "jewellery_erpnext.jewellery_erpnext.lock_order"
_SCRAP_WAREHOUSE = "jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion.get_scrap_warehouse"

EMPLOYEE_DEPT = {
	"EMP-1": "Chain - GK",
	# To Employees: Artists of the conversion's department, except where named.
	"EMP-2": "Chain - GK",
	"EMP-X": "Chain - GK",  # an Artist with no Raw Material warehouse
	"EMP-P": "Polish - GK",  # an Artist of another department
	"EMP-N": "Chain - GK",  # not an Artist
}
EMPLOYEE_DESIGNATION = {
	"EMP-2": "Artist",
	"EMP-X": "Artist",
	"EMP-P": "Artist",
	"EMP-N": "Goldsmith",
}
DEPT_RM = {"Chain - GK": "Chain RM - GK", "Polish - GK": "Polish RM - GK"}
SUPPLIER_RM = {"SUP-1": "SUP-1 MSL WH - GK"}
EMPLOYEE_WH = {"EMP-2": "EMP-2 RM WH - GK"}

#: 24KT 99.9 is mastered at 100%: what the supplier is given to alloy down to 22KT 91.75.
FINE_24KT = "M-G-24KT-99.9-Y"
ALLOYED_DOWN = {FINE_24KT: 100.0, "CH-22KT": 91.75}
SAME_PURITY = {"M-G-22KT-91.6-Y": 91.6, "CH-22KT": 91.6}
#: Metal Purity attribute values: Purity Percentage, and the Metal Touch each belongs to.
ATTRIBUTE_PURITY = {"91.75": 91.75, "91.6": 91.6, "75.4": 75.4}
PURITY_TOUCH = {"91.75": "22KT", "91.6": "22KT", "75.4": "18KT"}


def _det_flt(value, precision=None, rounding_method=None):
	try:
		num = float(value or 0)
	except (TypeError, ValueError):
		return 0.0
	return round(num, precision) if precision is not None else num


def _row(batch, qty):
	return frappe._dict(batch=batch, qty=qty)


class _Doc(frappe._dict):
	"""A Metal Conversions stand-in: ``_dict`` field access plus the Document calls used."""

	def get_doc_before_save(self):
		return self.get("_before")

	def is_new(self):
		return bool(self.get("_new"))

	def set(self, key, value):
		self[key] = value


def _chain_doc(**fields):
	doc = _Doc(
		name="MCON-CH-1",
		company="KG GK Jewellers Private Limited",
		branch="Main",
		employee="EMP-1",
		department="Chain - GK",
		manufacturer="Shubh",
		date="2026-10-12",
		source_warehouse="Chain RM - GK",
		target_warehouse="Chain RM - GK",
		is_chain_manufacturing="Yes",
		is_chain_subcontracting=1,
		chain_supplier="SUP-1",
		source_item="M-G-22KT-91.6-Y",
		source_qty=10.0,
		target_item=None,
		target_qty=0,
		multiple_metal_converter=0,
		is_melting_loss=0,
		source_batch_details=[_row("B-1", 6.0), _row("B-2", 4.0)],
		workflow_state="Draft",
	)
	doc.update(fields)
	doc.meta = SimpleNamespace(get_label=lambda f: f.replace("_", " ").title())
	doc.db_set = MagicMock()
	return doc


#: Is Chain Subcontracting unticked: the metal goes to the To Employee.
EMPLOYEE_MODE = {
	"is_chain_subcontracting": 0,
	"chain_supplier": None,
	"to_employee": "EMP-2",
}
#: The Target Metal Touch / Purity an employee chooses before Issue.
ALLOYED_TOUCH = {"chain_metal_touch": "22KT", "chain_metal_purity": "91.75"}
SAME_TOUCH = {"chain_metal_touch": "22KT", "chain_metal_purity": "91.6"}


def _employee_doc(**fields):
	"""A chain conversion given to an employee."""
	return _chain_doc(**{**EMPLOYEE_MODE, **fields})


def _get_value(doctype, name=None, fieldname=None, *args, **kwargs):
	"""Keyed answers for the master reads the chain module makes."""
	if doctype == "Employee" and fieldname == "department":
		return EMPLOYEE_DEPT.get(name)
	if doctype == "Employee" and fieldname == ["department", "designation"]:
		if name not in EMPLOYEE_DEPT:
			return None
		return EMPLOYEE_DEPT[name], EMPLOYEE_DESIGNATION.get(name)
	if doctype == "Department" and fieldname == "manufacturer":
		return "Shubh"
	if doctype == "Attribute Value" and fieldname == "metal_touch":
		return PURITY_TOUCH.get(name)
	raise AssertionError(f"unexpected get_value({doctype!r}, {name!r}, {fieldname!r})")


class _ChainCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		# The first _() reads Translation through get_all; warm it before any stub.
		_("warm")

	def setUp(self):
		# Metal Purity by item code; none unless a test sets it, so the purity rules skip.
		self.purity = {}
		self._patches = [
			patch.object(cs, "flt", _det_flt),
			patch.object(lanes_mod, "flt", _det_flt),
			patch.object(cs, "_precision", return_value=3),
			patch.object(cs.frappe.db, "get_value", side_effect=_get_value),
			patch.object(
				cs, "_metal_purity", side_effect=lambda item: self.purity.get(item)
			),
			patch.object(
				cs,
				"_purity_percentage",
				side_effect=lambda value: ATTRIBUTE_PURITY.get(value),
			),
			patch.object(
				cs, "get_department_rm_warehouse", side_effect=lambda d: DEPT_RM.get(d)
			),
			patch.object(
				cs,
				"get_supplier_rm_warehouse",
				side_effect=lambda s, company=None: SUPPLIER_RM.get(s),
			),
			patch.object(
				cs,
				"get_employee_warehouse",
				side_effect=lambda e, company=None: EMPLOYEE_WH.get(e),
			),
			patch.object(cs, "get_department_chain_flags", return_value=(True, True)),
		]
		for p in self._patches:
			p.start()

	def tearDown(self):
		for p in reversed(self._patches):
			p.stop()


class TestValidateChain(_ChainCase):
	def test_non_chain_doc_is_normalised_and_cleared(self):
		doc = _chain_doc(
			is_chain_manufacturing=None, is_chain_subcontracting=1, to_department="X"
		)
		cs.validate_chain(doc)
		self.assertEqual(doc.is_chain_manufacturing, "No")
		self.assertEqual(doc.is_chain_subcontracting, 0)
		for field in cs.CHAIN_FIELDS:
			self.assertIsNone(doc.get(field), field)
		self.assertEqual(doc.dept_is_chain_manufacturing, 1)

	def test_supplier_mode_resolves_its_warehouses_from_masters(self):
		doc = _chain_doc(source_warehouse="Hand Picked WH", target_qty=9.5)
		cs.validate_chain(doc)
		self.assertEqual(doc.source_warehouse, "Chain RM - GK")
		self.assertEqual(doc.target_warehouse, "Chain RM - GK")
		self.assertEqual(doc.chain_supplier_warehouse, "SUP-1 MSL WH - GK")
		self.assertEqual(cs.work_warehouse(doc), "SUP-1 MSL WH - GK")
		self.assertEqual(doc.chain_process_loss_qty, 0.5)

	def test_supplier_alloy_loss_is_the_fine_shortfall_never_negative(self):
		self.purity = dict(ALLOYED_DOWN)
		doc = _chain_doc(source_item=FINE_24KT, target_item="CH-22KT", target_qty=10.8)
		cs.validate_chain(doc)
		self.assertEqual(doc.chain_process_loss_qty, 0.091)

	def test_employee_mode_resolves_the_employees_own_warehouse(self):
		doc = _employee_doc(source_warehouse="Hand Picked WH")
		cs.validate_chain(doc)
		self.assertEqual(doc.source_warehouse, "Chain RM - GK")
		self.assertEqual(doc.to_department, "Chain - GK")
		self.assertEqual(doc.to_warehouse, "EMP-2 RM WH - GK")
		self.assertEqual(cs.work_warehouse(doc), "EMP-2 RM WH - GK")

	def test_one_party_only(self):
		# Ticked: a supplier, and any To Employee is dropped.
		doc = _chain_doc(to_employee="EMP-2", to_warehouse="EMP-2 RM WH - GK")
		cs.validate_chain(doc)
		for field in cs.EMPLOYEE_FIELDS:
			self.assertIsNone(doc.get(field), field)
		self.assertEqual(doc.chain_supplier, "SUP-1")
		# Unticked: the To Employee, and any Supplier is dropped.
		doc = _employee_doc(chain_supplier="SUP-1")
		cs.validate_chain(doc)
		for field in cs.SUPPLIER_FIELDS:
			self.assertIsNone(doc.get(field), field)
		self.assertEqual(doc.to_employee, "EMP-2")

	def test_department_follows_the_employee(self):
		doc = _chain_doc(department="Somewhere Else")
		cs.validate_chain(doc)
		self.assertEqual(doc.department, "Chain - GK")
		self.assertEqual(doc.manufacturer, "Shubh")

	def test_alloy_is_cleared(self):
		doc = _chain_doc(
			source_alloy="ALLOY", source_alloy_qty="1.2", target_alloy_qty=3
		)
		cs.validate_chain(doc)
		self.assertIsNone(doc.source_alloy)
		self.assertIsNone(doc.source_alloy_qty)
		self.assertEqual(doc.target_alloy_qty, 0)

	def test_ineligible_departments_are_refused(self):
		for flags in ((False, False), (True, False)):
			with patch.object(cs, "get_department_chain_flags", return_value=flags):
				with self.assertRaises(ValidationError, msg=str(flags)):
					cs.validate_chain(_chain_doc())
		with patch.object(
			cs, "get_department_chain_flags", return_value=(False, False)
		):
			with self.assertRaises(ValidationError):
				cs.validate_chain(_employee_doc())

	def test_employee_mode_needs_no_subcontracting_department(self):
		with patch.object(cs, "get_department_chain_flags", return_value=(True, False)):
			cs.validate_chain(_employee_doc())

	def test_other_modes_are_refused(self):
		for fields in ({"multiple_metal_converter": 1}, {"is_melting_loss": 1}):
			with self.assertRaises(ValidationError):
				cs.validate_chain(_chain_doc(**fields))

	def test_missing_warehouses_are_named(self):
		with self.assertRaisesRegex(ValidationError, "Raw Material"):
			cs.validate_chain(_chain_doc(chain_supplier="SUP-NO-WH"))
		with self.assertRaisesRegex(ValidationError, "No Raw Material warehouse"):
			cs.validate_chain(_employee_doc(to_employee="EMP-X"))

	def test_to_employee_is_an_artist_of_the_department(self):
		for employee in ("EMP-N", "EMP-P", "EMP-UNKNOWN"):
			with self.assertRaisesRegex(
				ValidationError, "must be an Artist", msg=employee
			):
				cs.validate_chain(_employee_doc(to_employee=employee))

	def test_the_employee_warehouse_is_raw_material(self):
		"""Issue hands the metal to the To Employee's Raw Material warehouse, not Manufacturing."""
		reads = []

		def get_value(doctype, name=None, fieldname=None, *args, **kwargs):
			if doctype == "Warehouse":
				reads.append(name)
				return "EMP-2 RM WH - GK"
			return _get_value(doctype, name, fieldname, *args, **kwargs)

		with patch.object(cs.frappe.db, "get_value", side_effect=get_value):
			warehouse = _REAL_GET_EMPLOYEE_WAREHOUSE("EMP-2", "KG GK")
		self.assertEqual(warehouse, "EMP-2 RM WH - GK")
		self.assertEqual(
			reads,
			[
				{
					"disabled": 0,
					"is_group": 0,
					"employee": "EMP-2",
					"warehouse_type": "Raw Material",
					"company": "KG GK",
				}
			],
		)


class TestFrozenAfterIssue(_ChainCase):
	def _issued(self, **changes):
		before = _chain_doc(chain_issue_stock_entry="SE-ISSUE-1")
		doc = _chain_doc(chain_issue_stock_entry="SE-ISSUE-1", _before=before)
		doc.update(changes)
		return doc

	def test_unchanged_issued_doc_saves(self):
		cs.validate_chain(self._issued(target_item="CH-22KT", target_qty=9.5))

	def test_stock_fields_cannot_change(self):
		for changes in (
			{"source_qty": 11.0},
			{"chain_supplier": "SUP-2"},
			{"source_item": "M-OTHER"},
			{"chain_issue_stock_entry": None},
		):
			with self.assertRaises(ValidationError, msg=str(changes)):
				cs.validate_chain(self._issued(**changes))

	def test_an_emptied_allocation_is_put_back(self):
		# The form's target_item handler used to clear the table when the Target Item was
		# typed in after Issue (MCON00800): the save must go through with the issued rows.
		doc = self._issued(
			source_batch_details=[], target_item="CH-22KT", target_qty=7.0
		)
		cs.validate_chain(doc)
		self.assertEqual(
			[(row["batch"], row["qty"]) for row in doc.source_batch_details],
			[("B-1", 6.0), ("B-2", 4.0)],
		)

	def test_a_changed_allocation_is_put_back(self):
		doc = self._issued(source_batch_details=[_row("B-9", 10.0)])
		cs.validate_chain(doc)
		self.assertEqual(
			[(row["batch"], row["qty"]) for row in doc.source_batch_details],
			[("B-1", 6.0), ("B-2", 4.0)],
		)

	def test_issued_to_employee_is_not_rejudged(self):
		"""An Issue made to an employee who is no longer an Artist here still receives."""
		fields = dict(
			chain_issue_stock_entry="SE-ISSUE-1",
			to_employee="EMP-P",
			to_department="Polish - GK",
			to_warehouse="EMP-P MFG WH - GK",
		)
		doc = _employee_doc(**fields, _before=_employee_doc(**fields))
		cs.validate_chain(doc)
		self.assertEqual(doc.to_warehouse, "EMP-P MFG WH - GK")

	def test_the_to_employee_cannot_change_after_issue(self):
		before = _employee_doc(
			chain_issue_stock_entry="SE-ISSUE-1", to_warehouse="EMP-2 RM WH - GK"
		)
		doc = _employee_doc(
			chain_issue_stock_entry="SE-ISSUE-1",
			to_warehouse="EMP-2 RM WH - GK",
			_before=before,
		)
		doc.to_employee = "EMP-9"
		with self.assertRaises(ValidationError):
			cs.validate_chain(doc)

	def test_issued_doc_skips_eligibility(self):
		# A department unticked after Issue must not strand the metal at the supplier.
		with patch.object(
			cs, "get_department_chain_flags", return_value=(False, False)
		):
			cs.validate_chain(self._issued())


class TestAfterValidate(_ChainCase):
	def test_issue_runs_once_in_the_issue_state(self):
		with patch.object(cs, "run_issue") as run_issue:
			cs.after_validate(_chain_doc(workflow_state="Issue"))
			cs.after_validate(
				_chain_doc(workflow_state="Issue", chain_issue_stock_entry="SE-1")
			)
			cs.after_validate(_chain_doc(workflow_state="Draft"))
		self.assertEqual(run_issue.call_count, 1)

	def test_batch_shows_the_first_allocation(self):
		doc = _chain_doc()
		with patch.object(cs, "run_issue"):
			cs.after_validate(doc)
		self.assertEqual(doc.batch, "B-1")

	def test_non_chain_is_untouched(self):
		doc = _chain_doc(
			is_chain_manufacturing="No", workflow_state="Issue", batch="KEEP"
		)
		with patch.object(cs, "run_issue") as run_issue:
			cs.after_validate(doc)
		run_issue.assert_not_called()
		self.assertEqual(doc.batch, "KEEP")

	def test_back_to_draft_with_an_issue_runs_cancel_issue(self):
		doc = _chain_doc(workflow_state="Draft", chain_issue_stock_entry="SE-ISSUE-1")
		with (
			patch.object(cs, "run_cancel_issue") as run_cancel_issue,
			patch.object(cs, "run_issue") as run_issue,
		):
			cs.after_validate(doc)
		run_cancel_issue.assert_called_once_with(doc)
		run_issue.assert_not_called()


class TestValidateIssue(_ChainCase):
	def setUp(self):
		super().setUp()
		self.batch_item = {"B-1": "M-G-22KT-91.6-Y", "B-2": "M-G-22KT-91.6-Y"}
		self.available = {"B-1": 6.0, "B-2": 4.0}
		self.po_uom = ("Gram", 0)

		def _get_all(doctype, *args, **kwargs):
			if doctype == "Batch":
				return [[name, item] for name, item in self.batch_item.items()]
			return _REAL_GET_ALL(doctype, *args, **kwargs)

		for p in (
			patch.object(cs.frappe, "get_all", side_effect=_get_all),
			patch.object(cs, "_po_item_uom", side_effect=lambda: self.po_uom),
			patch.object(
				cs,
				"get_batch_qty",
				side_effect=lambda batch_no, warehouse: self.available[batch_no],
			),
		):
			p.start()
			self._patches.append(p)

	def _ready(self, **fields):
		doc = _chain_doc(**fields)
		cs.validate_chain(doc)
		return doc

	def test_a_complete_doc_passes(self):
		cs.validate_issue(self._ready())

	def test_new_doc_is_refused(self):
		with self.assertRaises(ValidationError):
			cs.validate_issue(self._ready(_new=True))

	def test_batch_of_another_item_is_refused(self):
		self.batch_item["B-2"] = "M-OTHER"
		with self.assertRaises(ValidationError):
			cs.validate_issue(self._ready())

	def test_short_batch_is_refused(self):
		self.available["B-1"] = 5.0
		with self.assertRaises(ValidationError):
			cs.validate_issue(self._ready())

	def test_allocation_must_cover_source_qty(self):
		with self.assertRaises(ValidationError):
			cs.validate_issue(self._ready(source_qty=12.0))

	def test_missing_po_item_is_refused(self):
		self.po_uom = (None, 0)
		with self.assertRaisesRegex(ValidationError, "does not exist"):
			cs.validate_issue(self._ready())

	def _fractional(self):
		# 10.25 g, fully allocated, so only the UOM rule can refuse it.
		self.available["B-1"] = 6.25
		return self._ready(
			source_qty=10.25,
			source_batch_details=[_row("B-1", 6.25), _row("B-2", 4.0)],
		)

	def test_whole_number_uom_refuses_a_fractional_qty(self):
		self.po_uom = ("Nos", 1)
		with self.assertRaisesRegex(ValidationError, "whole-number"):
			cs.validate_issue(self._fractional())

	def test_gram_takes_a_fractional_qty(self):
		cs.validate_issue(self._fractional())

	def test_whole_number_uom_takes_a_whole_qty(self):
		self.po_uom = ("Nos", 1)
		cs.validate_issue(self._ready())

	def test_missing_inputs_are_reported(self):
		doc = self._ready()
		doc.chain_supplier = None
		doc.chain_supplier_warehouse = None
		with self.assertRaisesRegex(ValidationError, "Supplier is not selected"):
			cs.validate_issue(doc)

	def test_employee_mode_passes_without_a_po_item(self):
		# No supplier, so no Purchase Order and no service item to check.
		self.po_uom = (None, 0)
		cs.validate_issue(self._ready(**EMPLOYEE_MODE, **SAME_TOUCH))

	def test_employee_mode_needs_the_to_employee(self):
		doc = self._ready(**EMPLOYEE_MODE)
		doc.to_employee = None
		with self.assertRaisesRegex(ValidationError, "To Employee is not selected"):
			cs.validate_issue(doc)


class TestValidateReceive(_ChainCase):
	def setUp(self):
		super().setUp()
		self.supplier_stock = {"B-1": 6.0, "B-2": 4.0}
		self.issue_status = 1
		# A supplier's Receive needs both purities; same purity unless a test sets others,
		# so the conversion bound is the 10 g issued.
		self.purity = dict(SAME_PURITY)

		def _receive_get_value(doctype, name=None, fieldname=None, *args, **kwargs):
			if doctype == "Stock Entry" and fieldname == "docstatus":
				return self.issue_status
			return _get_value(doctype, name, fieldname, *args, **kwargs)

		for p in (
			patch.object(cs.frappe.db, "get_value", side_effect=_receive_get_value),
			patch.object(
				cs,
				"get_batch_qty",
				side_effect=lambda batch_no, warehouse: self.supplier_stock[batch_no],
			),
			patch.object(
				cs, "_metal_purity", side_effect=lambda item: self.purity.get(item)
			),
		):
			p.start()
			self._patches.append(p)

	def _received(self, **fields):
		# An issued document keeps the supplier warehouse Issue resolved; it is frozen.
		doc = _chain_doc(
			workflow_state="Submitted",
			chain_issue_stock_entry="SE-ISSUE-1",
			chain_supplier_warehouse="SUP-1 MSL WH - GK",
			target_item="CH-22KT",
			target_qty=9.5,
		)
		cs.validate_chain(doc)
		doc.update(fields)
		return doc

	def test_a_complete_receive_passes(self):
		cs.validate_receive(self._received())

	def test_only_the_receive_action_submits(self):
		# Receive lands on Submitted, as a plain .submit() does: what tells them apart is
		# the Issue, which only the Issue state -- whose one submit is Receive -- carries.
		with self.assertRaisesRegex(ValidationError, "submitted by its Receive action"):
			cs.validate_receive(self._received(chain_issue_stock_entry=None))

	def test_issue_must_be_submitted(self):
		self.issue_status = 2
		with self.assertRaises(ValidationError):
			cs.validate_receive(self._received())

	def test_target_rules(self):
		for fields in (
			{"target_item": None},
			{"target_qty": 0},
			{"target_qty": 10.5},
		):
			with self.assertRaises(ValidationError, msg=str(fields)):
				cs.validate_receive(self._received(**fields))

	def test_full_target_is_allowed(self):
		cs.validate_receive(self._received(target_qty=10.0))

	def test_metal_must_still_be_at_the_supplier(self):
		self.supplier_stock["B-2"] = 1.0
		with self.assertRaises(ValidationError):
			cs.validate_receive(self._received())

	def test_same_purity_passes(self):
		self.purity = {"M-G-22KT-91.6-Y": 91.6, "CH-22KT": 91.6}
		cs.validate_receive(self._received())

	def test_target_cannot_hold_more_fine_metal_than_was_issued(self):
		# 10 g at 75% is 7.5 g fine; 9.5 g at 91.6% would be 8.702 g. A supplier is held to
		# the conversion (8.188 g), an employee to the fine metal.
		self.purity = {"M-G-22KT-91.6-Y": 75.0, "CH-22KT": 91.6}
		with self.assertRaisesRegex(ValidationError, "cannot be greater than 8.188"):
			cs.validate_receive(self._received())
		with self.assertRaisesRegex(ValidationError, "fine metal"):
			cs.validate_receive(self._received_from_employee())

	def test_a_supplier_may_return_the_alloy_they_added(self):
		# 10 g of 24KT (100%) converts to 10.899 g of 22KT 91.75: heavier than the source.
		self.purity = dict(ALLOYED_DOWN)
		for target_qty in (10.5, 10.899, 10.9):
			cs.validate_receive(
				self._received(source_item=FINE_24KT, target_qty=target_qty)
			)

	def test_a_supplier_return_is_capped_at_the_conversion(self):
		self.purity = dict(ALLOYED_DOWN)
		with self.assertRaisesRegex(ValidationError, "cannot be greater than 10.899"):
			cs.validate_receive(
				self._received(source_item=FINE_24KT, target_qty=10.901)
			)

	def test_an_employee_return_keeps_the_weight_rule(self):
		# Same purity: no alloy, so an employee returns at most the weight issued.
		with self.assertRaisesRegex(ValidationError, "greater than Source Qty"):
			cs.validate_receive(
				self._received_from_employee(
					source_item="M-G-22KT-91.6-Y", target_qty=10.5
				)
			)

	def test_an_employee_lower_purity_target_needs_the_alloy_issued(self):
		# 24KT -> 22KT 91.75 chosen only after Issue: the 0.899 of alloy never went out.
		self.purity = dict(ALLOYED_DOWN)
		with self.assertRaisesRegex(
			ValidationError, "needs 0.899 of alloy, which was not issued"
		):
			cs.validate_receive(
				self._received_from_employee(source_item=FINE_24KT, target_qty=9.5)
			)

	def test_a_supplier_receive_needs_both_purities(self):
		# No purity, no conversion to bound the target by: refused, naming the item -- never
		# by the weight rule, which a supplier is not held to.
		self.purity = {FINE_24KT: 100.0}
		with self.assertRaisesRegex(
			ValidationError, "Metal Purity is missing on .*CH-22KT"
		):
			cs.validate_receive(self._received(source_item=FINE_24KT, target_qty=9.5))

	def test_a_supplier_is_never_held_to_the_weight_rule(self):
		self.purity = dict(ALLOYED_DOWN)
		for target_qty in (10.5, 11.0):
			try:
				cs.validate_receive(
					self._received(source_item=FINE_24KT, target_qty=target_qty)
				)
			except ValidationError as error:
				self.assertNotIn("Source Qty", str(error))

	def test_missing_purity_skips_the_fine_metal_check(self):
		# An employee is held to the weight issued; with no purity there is nothing more.
		self.purity = {"M-G-22KT-91.6-Y": 75.0}
		cs.validate_receive(self._received_from_employee())

	def _received_from_employee(self, **fields):
		# An issued document keeps the To Employee's warehouse Issue resolved; it is frozen.
		doc = _employee_doc(
			workflow_state="Submitted",
			chain_issue_stock_entry="SE-ISSUE-1",
			to_warehouse="EMP-2 RM WH - GK",
			target_item="CH-22KT",
			target_qty=9.5,
		)
		cs.validate_chain(doc)
		doc.update(fields)
		return doc

	def test_an_employee_receive_passes(self):
		cs.validate_receive(self._received_from_employee())

	def test_an_employee_receive_needs_their_warehouse(self):
		with self.assertRaisesRegex(ValidationError, "Raw Material warehouse"):
			cs.validate_receive(self._received_from_employee(to_warehouse=None))


class TestRunReceive(_ChainCase):
	def _run(self, target_qty, doc=None):
		doc = doc or _chain_doc(
			workflow_state="Submitted",
			chain_issue_stock_entry="SE-ISSUE-1",
			chain_supplier_warehouse="SUP-1 MSL WH - GK",
			target_item="CH-22KT",
			target_qty=target_qty,
		)
		with (
			# Lock order has its own suite (TestReceiveLockOrder).
			patch.object(cs, "_lock_receive"),
			patch.object(
				cs, "make_repack_stock_entry", return_value="SE-REPACK-1"
			) as repack,
			patch.object(
				melting_loss, "make_process_loss_entry", return_value="SE-LOSS-1"
			) as loss,
			patch.object(
				cs, "make_final_transfer", return_value="SE-TRANSFER-1"
			) as transfer,
		):
			cs.run_receive(doc)
		return doc, repack, loss, transfer

	def test_repack_takes_target_qty_and_loss_the_rest(self):
		doc, repack, loss, transfer = self._run(9.5)
		repack_rows = repack.call_args.args[1]
		self.assertEqual(sum(r["qty"] for r in repack_rows), 9.5)
		self.assertEqual(repack.call_args.args[2], 9.5)
		loss_kwargs = loss.call_args.kwargs
		self.assertEqual(loss_kwargs["loss_qty"], 0.5)
		self.assertEqual(sum(r["qty"] for r in loss_kwargs["rows"]), 0.5)
		self.assertEqual(loss_kwargs["source_wh"], "SUP-1 MSL WH - GK")
		# FIFO: the repack drains B-1 first, the loss comes from the end of B-2.
		self.assertEqual(loss_kwargs["rows"], [{"batch": "B-2", "qty": 0.5}])
		transfer.assert_called_once_with(doc, "SE-REPACK-1", 9.5)
		doc.db_set.assert_has_calls(
			[
				call(
					{
						"stock_entry": "SE-REPACK-1",
						"chain_repack_stock_entry": "SE-REPACK-1",
					}
				),
				call("chain_loss_stock_entry", "SE-LOSS-1"),
				call("chain_transfer_stock_entry", "SE-TRANSFER-1"),
			]
		)

	def test_zero_loss_skips_the_process_loss_entry(self):
		doc, repack, loss, transfer = self._run(10.0)
		loss.assert_not_called()
		transfer.assert_called_once()

	def _alloyed_down(self, target_qty):
		self.purity = dict(ALLOYED_DOWN)
		return _chain_doc(
			workflow_state="Submitted",
			chain_issue_stock_entry="SE-ISSUE-1",
			chain_supplier_warehouse="SUP-1 MSL WH - GK",
			source_item=FINE_24KT,
			target_item="CH-22KT",
			target_qty=target_qty,
		)

	def test_supplier_alloy_repacks_the_fine_metal_and_loses_the_shortfall(self):
		# 10.8 g of 22KT 91.75 holds 9.909 g fine: that much 24KT is repacked into it, and the
		# 0.091 g of 24KT not returned is the loss.
		doc, repack, loss, transfer = self._run(10.8, doc=self._alloyed_down(10.8))
		self.assertAlmostEqual(sum(r["qty"] for r in repack.call_args.args[1]), 9.909)
		self.assertEqual(repack.call_args.args[2], 10.8)
		self.assertEqual(loss.call_args.kwargs["loss_qty"], 0.091)
		self.assertEqual(
			loss.call_args.kwargs["rows"], [{"batch": "B-2", "qty": 0.091}]
		)
		transfer.assert_called_once_with(doc, "SE-REPACK-1", 10.8)

	def test_the_full_conversion_back_repacks_everything_with_no_loss(self):
		doc, repack, loss, transfer = self._run(10.899, doc=self._alloyed_down(10.899))
		self.assertAlmostEqual(sum(r["qty"] for r in repack.call_args.args[1]), 10.0)
		self.assertEqual(repack.call_args.args[2], 10.899)
		loss.assert_not_called()
		transfer.assert_called_once_with(doc, "SE-REPACK-1", 10.899)

	def test_an_employee_loss_comes_from_their_warehouse(self):
		doc = _employee_doc(
			workflow_state="Submitted",
			chain_issue_stock_entry="SE-ISSUE-1",
			to_warehouse="EMP-2 RM WH - GK",
			target_item="CH-22KT",
			target_qty=9.5,
		)
		_doc, _repack, loss, _transfer = self._run(9.5, doc=doc)
		self.assertEqual(loss.call_args.kwargs["source_wh"], "EMP-2 RM WH - GK")


class TestReceiveLockOrder(_ChainCase):
	"""Receive pins every counter, then every Bin, before its first Stock Entry (F3)."""

	def _run(self, target_qty, doc=None):
		events = []
		doc = doc or _chain_doc(
			workflow_state="Submitted",
			chain_issue_stock_entry="SE-ISSUE-1",
			chain_supplier_warehouse="SUP-1 MSL WH - GK",
			target_item="CH-22KT",
			target_qty=target_qty,
		)
		with (
			patch(
				f"{_LOCK_ORDER}.series_stubs",
				side_effect=lambda company, *types: list(types),
			),
			patch(
				f"{_LOCK_ORDER}.preallocate_series_for_docs",
				side_effect=lambda *stubs: events.append(("counters", set(stubs))),
			),
			patch(
				f"{_LOCK_ORDER}.lock_bins",
				side_effect=lambda pairs: events.append(("bins", set(pairs))),
			),
			patch.object(
				melting_loss, "_resolve_loss_item", return_value="ML-22KT"
			) as resolve_loss_item,
			patch(_SCRAP_WAREHOUSE, return_value="Chain Scrap - GK"),
			patch.object(
				cs,
				"make_repack_stock_entry",
				side_effect=lambda *a: events.append("repack") or "SE-REPACK-1",
			),
			patch.object(
				melting_loss,
				"make_process_loss_entry",
				side_effect=lambda *a, **k: events.append("loss") or "SE-LOSS-1",
			),
			patch.object(
				cs,
				"make_final_transfer",
				side_effect=lambda *a: events.append("transfer") or "SE-TRANSFER-1",
			),
		):
			cs.run_receive(doc)
		return events, resolve_loss_item

	def test_counters_then_bins_then_the_entries(self):
		events, _resolve = self._run(9.5)
		self.assertEqual(
			events[0],
			(
				"counters",
				{
					mc.CONVERSION_SE_TYPE,
					cs.RETURN_SE_TYPE,
					melting_loss.MELTING_LOSS_SE_TYPE,
				},
			),
		)
		self.assertEqual(
			events[1],
			(
				"bins",
				{
					("M-G-22KT-91.6-Y", "SUP-1 MSL WH - GK"),
					("CH-22KT", "SUP-1 MSL WH - GK"),
					# The return lands in the issuing department's RM warehouse.
					("CH-22KT", "Chain RM - GK"),
					("ML-22KT", "Chain Scrap - GK"),
				},
			),
		)
		self.assertEqual(events[2:], ["repack", "loss", "transfer"])

	def test_zero_loss_pins_no_loss_counter_or_bin(self):
		events, resolve_loss_item = self._run(10.0)
		self.assertEqual(
			events[0], ("counters", {mc.CONVERSION_SE_TYPE, cs.RETURN_SE_TYPE})
		)
		self.assertEqual(
			events[1],
			(
				"bins",
				{
					("M-G-22KT-91.6-Y", "SUP-1 MSL WH - GK"),
					("CH-22KT", "SUP-1 MSL WH - GK"),
					("CH-22KT", "Chain RM - GK"),
				},
			),
		)
		self.assertEqual(events[2:], ["repack", "transfer"])
		resolve_loss_item.assert_not_called()

	def test_an_employee_receive_locks_their_warehouse(self):
		doc = _employee_doc(
			workflow_state="Submitted",
			chain_issue_stock_entry="SE-ISSUE-1",
			to_warehouse="EMP-2 RM WH - GK",
			target_item="CH-22KT",
			target_qty=10.0,
		)
		events, _resolve = self._run(10.0, doc=doc)
		self.assertEqual(
			events[1],
			(
				"bins",
				{
					("M-G-22KT-91.6-Y", "EMP-2 RM WH - GK"),
					("CH-22KT", "EMP-2 RM WH - GK"),
					("CH-22KT", "Chain RM - GK"),
				},
			),
		)


class _FakeDoc(SimpleNamespace):
	def __init__(self, payload=None, **kwargs):
		super().__init__(**kwargs)
		self.payload = payload or {}
		self.items = []
		self.flags = frappe._dict()
		self.saved = self.submitted = False
		self.name = kwargs.get("name", "SE-NEW-1")

	def append(self, table, row):
		self.items.append(row)

	def save(self):
		self.saved = True

	def submit(self):
		self.submitted = True


class _BuilderCase(_ChainCase):
	def setUp(self):
		super().setUp()
		self.captured = []

		def _get_doc(payload, *args, **kwargs):
			doc = _FakeDoc(payload)
			self.captured.append(doc)
			return doc

		for p in (
			patch.object(cs.frappe, "get_doc", side_effect=_get_doc),
			patch.object(cs.frappe, "new_doc", side_effect=lambda dt: _FakeDoc()),
			patch("jewellery_erpnext.jewellery_erpnext.lock_order.lock_bins"),
			patch(
				"jewellery_erpnext.jewellery_erpnext.lock_order.preallocate_series_for_docs"
			),
			patch(
				"jewellery_erpnext.jewellery_erpnext.lock_order.stock_lock_key",
				side_effect=lambda i, w, b=None: (i or "", w or "", b or ""),
			),
		):
			p.start()
			self._patches.append(p)

	def _doc(self, **fields):
		doc = _chain_doc(
			chain_supplier_warehouse="SUP-1 MSL WH - GK",
			target_item="CH-22KT",
			target_qty=9.5,
		)
		doc.update(fields)
		return doc

	def _employee(self, **fields):
		return _employee_doc(
			to_warehouse="EMP-2 RM WH - GK",
			target_item="CH-22KT",
			target_qty=9.5,
			**fields,
		)


class TestStockEntryBuilders(_BuilderCase):
	def test_issue_moves_each_batch_department_to_supplier(self):
		cs.make_issue_stock_entry(self._doc())
		se = self.captured[-1]
		self.assertEqual(
			se.payload["stock_entry_type"], "Material Transfer to Subcontractor"
		)
		self.assertEqual(se.payload["subcontractor"], "SUP-1")
		self.assertNotIn("to_employee", se.payload)
		self.assertEqual(se.payload["purpose"], "Material Transfer")
		self.assertEqual(se.payload["custom_metal_conversion_reference"], "MCON-CH-1")
		self.assertEqual(se.payload["auto_created"], 1)
		self.assertEqual(
			[(r["batch_no"], r["qty"]) for r in se.items], [("B-1", 6.0), ("B-2", 4.0)]
		)
		for row in se.items:
			self.assertEqual(row["s_warehouse"], "Chain RM - GK")
			self.assertEqual(row["t_warehouse"], "SUP-1 MSL WH - GK")
			self.assertEqual(row["inventory_type"], "Regular Stock")
		self.assertTrue(se.saved and se.submitted)

	def test_issue_to_an_employee_goes_to_their_warehouse(self):
		cs.make_issue_stock_entry(self._employee())
		se = self.captured[-1]
		self.assertEqual(
			se.payload["stock_entry_type"], "Material Transfer to Employee"
		)
		self.assertEqual(se.payload["to_employee"], "EMP-2")
		self.assertNotIn("subcontractor", se.payload)
		for row in se.items:
			self.assertEqual(row["s_warehouse"], "Chain RM - GK")
			self.assertEqual(row["t_warehouse"], "EMP-2 RM WH - GK")

	def test_repack_stays_inside_the_supplier_warehouse(self):
		with patch.object(
			cs,
			"get_batch_lane_map",
			return_value={
				"B-1": ("Regular Stock", None),
				"B-2": ("Regular Stock", None),
			},
		), patch.object(mc, "flt", _det_flt):
			cs.make_repack_stock_entry(
				self._doc(),
				[{"batch": "B-1", "qty": 6.0}, {"batch": "B-2", "qty": 3.5}],
				9.5,
			)
		se = self.captured[-1]
		self.assertEqual(se.payload["stock_entry_type"], mc.CONVERSION_SE_TYPE)
		consume = [r for r in se.items if r.get("s_warehouse")]
		produce = [r for r in se.items if r.get("t_warehouse")]
		self.assertEqual(sum(r["qty"] for r in consume), 9.5)
		self.assertEqual({r["s_warehouse"] for r in consume}, {"SUP-1 MSL WH - GK"})
		self.assertEqual(len(produce), 1)
		self.assertEqual(produce[0]["item_code"], "CH-22KT")
		self.assertEqual(produce[0]["qty"], 9.5)
		self.assertEqual(produce[0]["t_warehouse"], "SUP-1 MSL WH - GK")

	def test_final_transfer_carries_the_minted_batch(self):
		with patch.object(
			cs, "_produced_batches", return_value=[{"batch": "TB-1", "qty": 9.5}]
		):
			cs.make_final_transfer(self._doc(), "SE-REPACK-1", 9.5)
		se = self.captured[-1]
		self.assertEqual(se.payload["stock_entry_type"], "Material Transfer")
		self.assertEqual(len(se.items), 1)
		row = se.items[0]
		self.assertEqual(row["batch_no"], "TB-1")
		self.assertEqual(row["item_code"], "CH-22KT")
		self.assertEqual(row["s_warehouse"], "SUP-1 MSL WH - GK")
		# Back to the issuing department, as Gemstone Conversion returns a resize.
		self.assertEqual(row["t_warehouse"], "Chain RM - GK")

	def test_an_employee_return_leaves_their_warehouse(self):
		with patch.object(
			cs, "_produced_batches", return_value=[{"batch": "TB-1", "qty": 9.5}]
		) as produced:
			cs.make_final_transfer(self._employee(), "SE-REPACK-1", 9.5)
		produced.assert_called_once_with("SE-REPACK-1", "CH-22KT", "EMP-2 RM WH - GK")
		row = self.captured[-1].items[0]
		self.assertEqual(row["s_warehouse"], "EMP-2 RM WH - GK")
		self.assertEqual(row["t_warehouse"], "Chain RM - GK")

	def test_final_transfer_refuses_a_short_repack(self):
		with patch.object(
			cs, "_produced_batches", return_value=[{"batch": "TB-1", "qty": 9.0}]
		):
			with self.assertRaises(ValidationError):
				cs.make_final_transfer(self._doc(), "SE-REPACK-1", 9.5)


class TestSubcontractingPO(_BuilderCase):
	def test_draft_po_for_the_service_item(self):
		po = _FakeDoc(name="PO-1")
		with (
			patch.object(cs.frappe, "new_doc", return_value=po),
			patch.object(
				cs,
				"_linked_address",
				side_effect=lambda dt, name: {
					"Company": "ADDR-CO",
					"Supplier": "ADDR-SUP",
				}[dt],
			),
			patch.object(cs, "nowdate", return_value="2026-10-12"),
			patch.object(cs, "_po_item_uom", return_value=("Gram", 0)),
		):
			name = cs.make_subcontracting_po(self._doc(date="2026-09-01"))
		self.assertEqual(name, "PO-1")
		self.assertEqual(po.supplier, "SUP-1")
		self.assertEqual(po.purchase_type, cs.CHAIN_SC_PURCHASE_TYPE)
		self.assertEqual(po.custom_metal_conversion, "MCON-CH-1")
		self.assertEqual(po.billing_address, "ADDR-CO")
		self.assertEqual(po.supplier_address, "ADDR-SUP")
		# A back-dated conversion must not produce a Required By date before today.
		self.assertEqual(po.schedule_date, "2026-10-12")
		self.assertEqual(
			po.items,
			[
				{
					"item_code": cs.CHAIN_SC_PO_ITEM,
					"qty": 10.0,
					# The item's own stock UOM, so no purchase-UOM conversion applies.
					"uom": "Gram",
					"conversion_factor": 1,
					"schedule_date": "2026-10-12",
				}
			],
		)
		self.assertTrue(po.flags.ignore_permissions)
		self.assertTrue(po.saved)
		self.assertFalse(po.submitted)


class TestProcessLossEntry(IntegrationTestCase):
	"""The extracted builder takes split_allocations dicts as well as table rows."""

	@classmethod
	def setUpClass(cls):
		_("warm")

	def test_returns_the_existing_entry(self):
		doc = _chain_doc()
		with patch.object(melting_loss.frappe.db, "exists", return_value="SE-LOSS-9"):
			self.assertEqual(
				melting_loss.make_process_loss_entry(
					doc, rows=[], loss_qty=0.5, source_wh="X"
				),
				"SE-LOSS-9",
			)

	def test_dict_rows_from_the_supplier_warehouse(self):
		doc = _chain_doc()
		captured = {}

		def _get_doc(payload, *args, **kwargs):
			captured["se"] = _FakeDoc(payload, name="SE-LOSS-1")
			return captured["se"]

		with (
			patch.object(melting_loss.frappe.db, "exists", return_value=None),
			patch.object(melting_loss, "_loss_precision", return_value=3),
			patch.object(melting_loss, "flt", _det_flt),
			patch.object(melting_loss.frappe, "get_doc", side_effect=_get_doc),
			patch.object(
				melting_loss.frappe, "new_doc", side_effect=lambda dt: _FakeDoc()
			),
			patch.object(melting_loss, "_resolve_loss_item", return_value="ML-22KT"),
			patch(
				"jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion.get_scrap_warehouse",
				return_value="Chain Scrap - GK",
			),
			patch("jewellery_erpnext.jewellery_erpnext.lock_order.lock_bins"),
			patch(
				"jewellery_erpnext.jewellery_erpnext.lock_order.preallocate_series_for_docs"
			),
		):
			name = melting_loss.make_process_loss_entry(
				doc,
				rows=[{"batch": "B-2", "qty": 0.5}],
				loss_qty=0.5,
				source_wh="SUP-1 MSL WH - GK",
			)
		self.assertEqual(name, "SE-LOSS-1")
		consume, produce = captured["se"].items
		self.assertEqual(consume["batch_no"], "B-2")
		self.assertEqual(consume["s_warehouse"], "SUP-1 MSL WH - GK")
		self.assertEqual(produce["t_warehouse"], "Chain Scrap - GK")
		self.assertEqual(produce["qty"], 0.5)
		doc.db_set.assert_not_called()


#: The rows of the four entries a received chain conversion owns.
_ENTRY_ROWS = [
	frappe._dict(item_code="CH-22KT", s_warehouse="SUP", t_warehouse="TO"),
	frappe._dict(item_code="M-G-22KT-91.6-Y", s_warehouse="SUP", t_warehouse=None),
	frappe._dict(item_code="ML-22KT", s_warehouse=None, t_warehouse="SCRAP"),
	frappe._dict(item_code="CH-22KT", s_warehouse=None, t_warehouse="SUP"),
	frappe._dict(item_code="M-G-22KT-91.6-Y", s_warehouse="DEPT", t_warehouse="SUP"),
]


class TestCancelChain(_ChainCase):
	def _cancel(
		self, po_status, entries=("SE-TRANSFER-1", "SE-REPACK-1", "SE-ISSUE-1")
	):
		order = []
		doc = _chain_doc(
			chain_issue_stock_entry="SE-ISSUE-1",
			chain_transfer_stock_entry="SE-TRANSFER-1",
			chain_purchase_order="PO-1",
		)

		def _get_value(doctype, name=None, fieldname=None, *args, **kwargs):
			if doctype == "Purchase Order":
				return po_status
			raise AssertionError(doctype)

		def _get_all(doctype, *args, **kwargs):
			if doctype == "Stock Entry":
				return list(entries)
			if doctype == "Stock Entry Detail":
				return _ENTRY_ROWS
			return _REAL_GET_ALL(doctype, *args, **kwargs)

		with (
			patch.object(cs.frappe.db, "get_value", side_effect=_get_value),
			patch.object(cs.frappe, "get_all", side_effect=_get_all),
			patch(
				f"{_LOCK_ORDER}.lock_bins",
				side_effect=lambda pairs: order.append(("lock", set(pairs))),
			),
			patch.object(
				cs, "_cancel_stock_entry", side_effect=lambda name: order.append(name)
			),
			patch.object(
				melting_loss,
				"cancel_melting_loss_stock_entries",
				side_effect=lambda d: order.append("loss"),
			),
			patch.object(
				mc,
				"cancel_conversion_stock_entries",
				side_effect=lambda d: order.append("repack"),
			),
			patch.object(cs.frappe, "delete_doc") as delete_doc,
		):
			cs.cancel_chain(doc)
		return order, delete_doc, doc

	def test_reverse_order_and_draft_po_deleted(self):
		order, delete_doc, doc = self._cancel(po_status=0)
		self.assertEqual(order[1:], ["SE-TRANSFER-1", "loss", "repack", "SE-ISSUE-1"])
		delete_doc.assert_called_once_with(
			"Purchase Order", "PO-1", ignore_permissions=True
		)
		doc.db_set.assert_called_once_with("chain_purchase_order", None)

	def test_every_bin_is_locked_before_the_first_cancel(self):
		order, _delete_doc, _doc = self._cancel(po_status=0)
		self.assertEqual(
			order[0],
			(
				"lock",
				{
					("CH-22KT", "SUP"),
					("CH-22KT", "TO"),
					("M-G-22KT-91.6-Y", "SUP"),
					("M-G-22KT-91.6-Y", "DEPT"),
					("ML-22KT", "SCRAP"),
				},
			),
		)

	def test_nothing_to_lock_without_entries(self):
		order, _delete_doc, _doc = self._cancel(po_status=0, entries=())
		self.assertNotIn(
			"lock", [event[0] for event in order if isinstance(event, tuple)]
		)

	def test_submitted_po_stops_everything(self):
		with self.assertRaises(ValidationError):
			self._cancel(po_status=1)


class TestCancelIssue(_ChainCase):
	"""Issue -> Draft (Cancel Issue) undoes an Issue that has not been received (F2)."""

	def _cancel_issue(self, po_status, po="PO-1"):
		# Kept on the test so a refusal can still be checked for what ran before it.
		self.cancelled = cancelled = []
		doc = _chain_doc(
			workflow_state="Draft",
			chain_issue_stock_entry="SE-ISSUE-1",
			chain_purchase_order=po,
			target_item="CH-22KT",
			target_qty=9.5,
			chain_process_loss_qty=0.5,
		)

		def _get_value(doctype, name=None, fieldname=None, *args, **kwargs):
			if doctype == "Purchase Order" and fieldname == "docstatus":
				return po_status
			raise AssertionError(f"unexpected get_value({doctype!r}, {name!r})")

		with (
			patch.object(cs.frappe.db, "get_value", side_effect=_get_value),
			patch.object(
				cs,
				"_cancel_stock_entry",
				side_effect=lambda name: cancelled.append(name),
			),
			patch.object(cs.frappe, "delete_doc") as delete_doc,
		):
			cs.run_cancel_issue(doc)
		return doc, cancelled, delete_doc

	def test_cancels_the_issue_and_deletes_the_draft_po(self):
		doc, cancelled, delete_doc = self._cancel_issue(po_status=0)
		self.assertEqual(cancelled, ["SE-ISSUE-1"])
		delete_doc.assert_called_once_with(
			"Purchase Order", "PO-1", force=True, ignore_permissions=True
		)
		self.assertIsNone(doc.chain_issue_stock_entry)
		self.assertIsNone(doc.chain_purchase_order)
		# The receipt typed in the Issue state will not happen now.
		self.assertIsNone(doc.target_item)
		self.assertEqual(doc.target_qty, 0)
		self.assertEqual(doc.chain_process_loss_qty, 0)
		# The allocation stays: the metal is back in the department warehouse.
		self.assertEqual(
			[row.batch for row in doc.source_batch_details], ["B-1", "B-2"]
		)

	def test_a_submitted_po_stops_it_before_anything_is_cancelled(self):
		with self.assertRaisesRegex(ValidationError, "PO-1"):
			self._cancel_issue(po_status=1)
		self.assertEqual(self.cancelled, [])

	def test_a_cancelled_po_is_left_alone(self):
		doc, cancelled, delete_doc = self._cancel_issue(po_status=2)
		self.assertEqual(cancelled, ["SE-ISSUE-1"])
		delete_doc.assert_not_called()
		self.assertIsNone(doc.chain_purchase_order)

	def test_no_po_reads_no_po(self):
		doc, cancelled, delete_doc = self._cancel_issue(po_status=None, po=None)
		self.assertEqual(cancelled, ["SE-ISSUE-1"])
		delete_doc.assert_not_called()

	def test_an_entry_cancelled_by_hand_is_not_cancelled_again(self):
		def _get_value(doctype, name=None, fieldname=None, *args, **kwargs):
			if doctype == "Stock Entry" and fieldname == "docstatus":
				return 2
			raise AssertionError(doctype)

		with (
			patch.object(cs.frappe.db, "get_value", side_effect=_get_value),
			patch.object(cs.frappe, "get_doc") as get_doc,
		):
			cs._cancel_stock_entry("SE-ISSUE-1")
		get_doc.assert_not_called()


class TestGuardTrash(_ChainCase):
	"""Delete is refused only while the Issue keeps metal at the supplier (F2)."""

	def _guard(self, issue_status, **fields):
		def _get_value(doctype, name=None, fieldname=None, *args, **kwargs):
			if doctype == "Stock Entry" and fieldname == "docstatus":
				return issue_status
			raise AssertionError(doctype)

		with patch.object(cs.frappe.db, "get_value", side_effect=_get_value):
			cs.guard_trash(_chain_doc(**fields))

	def test_a_live_issue_cannot_be_deleted(self):
		with self.assertRaisesRegex(ValidationError, "Cancel Issue"):
			self._guard(1, chain_issue_stock_entry="SE-ISSUE-1")

	def test_a_cancelled_conversion_can_be_deleted(self):
		self._guard(None, docstatus=2, chain_issue_stock_entry="SE-ISSUE-1")

	def test_an_issue_cancelled_by_hand_can_be_deleted(self):
		self._guard(2, chain_issue_stock_entry="SE-ISSUE-1")

	def test_a_never_issued_conversion_can_be_deleted(self):
		self._guard(None)


class TestChainReceiveQtys(_ChainCase):
	"""``get_chain_receive_qtys``: the Target Qty the form fills, and the loss it shows."""

	def _qtys(self, doc):
		with (
			patch.object(mc, "flt", _det_flt),
			patch.object(mc, "_qty_precision", return_value=3),
		):
			return mc.MetalConversions.get_chain_receive_qtys(doc)

	def test_a_supplier_conversion_fills_what_the_source_converts_to(self):
		# MCON00812: 2 g of 24KT 99.9 (100%) -> 22KT 91.75 is 2.180 g.
		self.purity = dict(ALLOYED_DOWN)
		doc = _chain_doc(source_item=FINE_24KT, source_qty=2.0, target_item="CH-22KT")
		qtys = self._qtys(doc)
		self.assertAlmostEqual(qtys["max_target_qty"], 2.17984, places=5)
		self.assertEqual(qtys["loss_qty"], 0)

		doc.target_qty = 2.15
		self.assertEqual(self._qtys(doc)["loss_qty"], 0.027)

	def test_no_fill_without_purity_or_for_an_employee_needing_no_alloy(self):
		doc = _chain_doc(source_item=FINE_24KT, target_item="CH-22KT")
		self.assertIsNone(self._qtys(doc)["max_target_qty"])
		self.purity = dict(SAME_PURITY)
		doc = _employee_doc(**SAME_TOUCH)
		qtys = self._qtys(doc)
		self.assertIsNone(qtys["max_target_qty"])
		self.assertEqual(qtys["alloy_qty"], 0)

	def test_an_employee_making_a_lower_purity_gets_source_plus_alloy(self):
		self.purity = dict(ALLOYED_DOWN)
		# Draft: no Target Item yet; the chosen purity sizes the alloy.
		doc = _employee_doc(source_item=FINE_24KT, source_qty=2.0, **ALLOYED_TOUCH)
		with patch.object(mc, "chain_issued", return_value=False):
			qtys = self._qtys(doc)
		self.assertEqual(qtys["alloy_qty"], 0.18)
		self.assertEqual(qtys["max_target_qty"], 2.18)

		# After Issue: what was issued, not a recomputation.
		doc.update(
			chain_issue_stock_entry="SE-ISSUE-1",
			target_item="CH-22KT",
			source_alloy="ALLOY-1",
			source_alloy_qty="0.180",
		)
		qtys = self._qtys(doc)
		self.assertEqual(qtys["alloy_qty"], 0.18)
		self.assertEqual(qtys["max_target_qty"], 2.18)


#: 10 g of 24KT (100%) to 22KT 91.75 needs 10 x 100 / 91.75 - 10 = 0.899 of alloy.
EMPLOYEE_ALLOY = 0.899
EMPLOYEE_TARGET = 10.899


class TestEmployeeAlloy(_ChainCase):
	"""An employee making a lower purity is given our alloy with the metal (item 4)."""

	def setUp(self):
		super().setUp()
		self.purity = dict(ALLOYED_DOWN)
		self.batch_item = {"B-1": FINE_24KT, "B-2": FINE_24KT, "AB-1": "ALLOY-1"}
		self.stock = {"B-1": 6.0, "B-2": 4.0, "AB-1": 5.0}
		self.issue_status = 1
		# The Target Item's Metal Touch / Purity attributes, checked at Receive.
		self.attributes = {"CH-22KT": {"Metal Touch": "22KT", "Metal Purity": "91.75"}}

		def _get_all(doctype, *args, **kwargs):
			if doctype == "Batch":
				return [[name, item] for name, item in self.batch_item.items()]
			if doctype == "Item Variant Attribute":
				item = kwargs["filters"]["parent"]
				return [list(pair) for pair in self.attributes.get(item, {}).items()]
			return _REAL_GET_ALL(doctype, *args, **kwargs)

		def _alloy_get_value(doctype, name=None, fieldname=None, *args, **kwargs):
			if doctype == "Stock Entry" and fieldname == "docstatus":
				return self.issue_status
			return _get_value(doctype, name, fieldname, *args, **kwargs)

		for p in (
			patch.object(cs.frappe, "get_all", side_effect=_get_all),
			patch.object(cs.frappe.db, "get_value", side_effect=_alloy_get_value),
			patch.object(
				cs,
				"get_batch_qty",
				side_effect=lambda batch_no, warehouse: self.stock[batch_no],
			),
		):
			p.start()
			self._patches.append(p)

	def _draft(self, **fields):
		# No Target Item before Issue: the chosen touch and purity size the alloy.
		doc = _employee_doc(**{"source_item": FINE_24KT, **ALLOYED_TOUCH, **fields})
		cs.validate_chain(doc)
		return doc

	def _issued(self, **fields):
		doc = _employee_doc(
			workflow_state="Submitted",
			chain_issue_stock_entry="SE-ISSUE-1",
			to_warehouse="EMP-2 RM WH - GK",
			source_item=FINE_24KT,
			target_item="CH-22KT",
			target_qty=EMPLOYEE_TARGET,
			source_alloy_check=1,
			source_alloy="ALLOY-1",
			source_alloy_qty=str(EMPLOYEE_ALLOY),
			alloy_batch_details=[_row("AB-1", EMPLOYEE_ALLOY)],
			**ALLOYED_TOUCH,
		)
		doc.update(fields)
		return doc

	# -- Draft ---------------------------------------------------------------------------

	def test_draft_sizes_the_alloy_as_the_normal_conversion(self):
		doc = self._draft(source_alloy="ALLOY-1", source_alloy_qty="5")
		self.assertIsNone(doc.target_item)
		self.assertEqual(doc.source_alloy_check, 1)
		self.assertEqual(doc.source_alloy_qty, EMPLOYEE_ALLOY)
		self.assertEqual(doc.source_alloy, "ALLOY-1")
		self.assertEqual(doc.target_alloy_qty, 0)

	def test_draft_leaves_the_target_qty_to_receive(self):
		self.assertFalse(self._draft().target_qty)
		self.assertEqual(self._draft(target_qty=3).target_qty, 3)

	def test_draft_ignores_a_target_item_without_a_chosen_purity(self):
		# The Target Item is picked at Receive; before Issue only the purity sizes alloy.
		doc = self._draft(
			chain_metal_touch=None,
			chain_metal_purity=None,
			target_item="CH-22KT",
			source_alloy="ALLOY-1",
			source_alloy_qty="0.899",
		)
		self.assertIsNone(doc.source_alloy)
		self.assertIsNone(doc.source_alloy_qty)

	def test_a_supplier_or_a_same_purity_employee_carries_no_alloy(self):
		doc = _chain_doc(
			source_item=FINE_24KT,
			target_item="CH-22KT",
			source_alloy="ALLOY-1",
			source_alloy_qty="0.899",
			**ALLOYED_TOUCH,
		)
		cs.validate_chain(doc)
		self.assertIsNone(doc.source_alloy)
		self.assertIsNone(doc.source_alloy_qty)
		# A supplier never chooses a touch / purity: the fields are cleared.
		self.assertIsNone(doc.chain_metal_touch)
		self.assertIsNone(doc.chain_metal_purity)

		self.purity = dict(SAME_PURITY)
		doc = _employee_doc(source_alloy="ALLOY-1", source_alloy_qty="1", **SAME_TOUCH)
		cs.validate_chain(doc)
		self.assertIsNone(doc.source_alloy)
		self.assertIsNone(doc.source_alloy_qty)

	# -- Issue ---------------------------------------------------------------------------

	def test_issue_needs_the_touch_and_purity(self):
		with self.assertRaisesRegex(
			ValidationError,
			"Target Metal Touch and Target Metal Purity are mandatory",
		):
			cs.validate_issue(self._draft(chain_metal_purity=None))
		with self.assertRaisesRegex(
			ValidationError, "91.75.*belongs to Metal Touch .*22KT.*, not .*18KT"
		):
			cs.validate_issue(self._draft(chain_metal_touch="18KT"))

	def test_issue_needs_the_source_alloy(self):
		with self.assertRaisesRegex(ValidationError, "Source Alloy is mandatory"):
			cs.validate_issue(self._draft())

	def test_issue_passes_with_the_alloy_allocated(self):
		cs.validate_issue(
			self._draft(
				source_alloy="ALLOY-1",
				alloy_batch_details=[_row("AB-1", EMPLOYEE_ALLOY)],
			)
		)

	def test_issue_refuses_short_or_foreign_alloy_batches(self):
		with self.assertRaisesRegex(
			ValidationError, "hold 0.5, not the Alloy Qty 0.899"
		):
			cs.validate_issue(
				self._draft(
					source_alloy="ALLOY-1", alloy_batch_details=[_row("AB-1", 0.5)]
				)
			)
		self.stock["AB-1"] = 0.4
		with self.assertRaisesRegex(ValidationError, "AB-1.*has only 0.4"):
			cs.validate_issue(
				self._draft(
					source_alloy="ALLOY-1",
					alloy_batch_details=[_row("AB-1", EMPLOYEE_ALLOY)],
				)
			)
		self.batch_item["AB-1"] = "ALLOY-2"
		with self.assertRaisesRegex(ValidationError, "does not belong to Source Alloy"):
			cs.validate_issue(
				self._draft(
					source_alloy="ALLOY-1",
					alloy_batch_details=[_row("AB-1", EMPLOYEE_ALLOY)],
				)
			)

	# -- Receive -------------------------------------------------------------------------

	def test_receive_may_bring_back_source_plus_alloy(self):
		cs.validate_receive(self._issued())
		cs.validate_receive(self._issued(target_qty=10.5))
		with self.assertRaisesRegex(
			ValidationError,
			"Target Qty 10.901 cannot be greater than 10.899: the 10.0 of .* and 0.899 of "
			"ALLOY-1 issued to EMP-2",
		):
			cs.validate_receive(self._issued(target_qty=10.901))

	def test_receive_needs_a_target_of_the_chosen_purity(self):
		self.attributes["CH-22KT"]["Metal Purity"] = "91.6"
		with self.assertRaisesRegex(
			ValidationError,
			"CH-22KT.* is 22KT 91.6, but the metal was issued to make 22KT 91.75",
		):
			cs.validate_receive(self._issued())

	def test_receive_accepts_a_target_without_purity_attributes(self):
		# Nothing to compare: the weight issued (source + alloy) still bounds it.
		self.attributes = {}
		self.purity = {FINE_24KT: 100.0}
		cs.validate_receive(self._issued())
		with self.assertRaisesRegex(ValidationError, "cannot be greater than 10.899"):
			cs.validate_receive(self._issued(target_qty=10.95))

	def test_receive_refuses_a_lower_purity_issued_without_alloy(self):
		# Issued before the touch / purity existed: no alloy went with the metal.
		doc = self._issued(
			chain_metal_touch=None,
			chain_metal_purity=None,
			source_alloy_check=0,
			source_alloy=None,
			source_alloy_qty=None,
			alloy_batch_details=[],
			target_qty=10.0,
		)
		with self.assertRaisesRegex(
			ValidationError,
			"needs 0.899 of alloy, which was not issued .*Cancel Issue, set the Target Metal "
			"Touch",
		):
			cs.validate_receive(doc)

	def test_receive_needs_the_alloy_in_the_employee_warehouse(self):
		self.stock["AB-1"] = 0.5
		with self.assertRaisesRegex(ValidationError, "AB-1.*has only 0.5 of ALLOY-1"):
			cs.validate_receive(self._issued())

	def test_receive_split_loses_the_fine_shortfall_of_source(self):
		# 10.8 g of 22KT 91.75 holds 9.909 g fine: that much 24KT is repacked with all the
		# alloy, and the 0.091 g of 24KT not returned is the loss.
		self.assertEqual(
			cs.receive_split(self._issued(target_qty=10.8), 3), (9.909, 0.091)
		)
		self.assertEqual(cs.receive_split(self._issued(), 3), (10.0, 0.0))

	def test_run_receive_repacks_the_fine_metal(self):
		with (
			patch.object(cs, "_lock_receive"),
			patch.object(
				cs, "make_repack_stock_entry", return_value="SE-REPACK-1"
			) as repack,
			patch.object(
				melting_loss, "make_process_loss_entry", return_value="SE-LOSS-1"
			) as loss,
			patch.object(cs, "make_final_transfer", return_value="SE-TRANSFER-1"),
		):
			cs.run_receive(self._issued(target_qty=10.8))
		self.assertAlmostEqual(sum(r["qty"] for r in repack.call_args.args[1]), 9.909)
		self.assertEqual(repack.call_args.args[2], 10.8)
		self.assertEqual(loss.call_args.kwargs["loss_qty"], 0.091)
		self.assertEqual(loss.call_args.kwargs["source_wh"], "EMP-2 RM WH - GK")

	def test_receive_locks_the_alloy_bin(self):
		with (
			patch(f"{_LOCK_ORDER}.lock_bins") as lock_bins,
			patch(f"{_LOCK_ORDER}.preallocate_series_for_docs"),
			patch(f"{_LOCK_ORDER}.series_stubs", return_value=[]),
		):
			cs._lock_receive(self._issued(), 0)
		self.assertIn(("ALLOY-1", "EMP-2 RM WH - GK"), lock_bins.call_args.args[0])

	# -- Cancel Issue / frozen -------------------------------------------------------------

	def test_alloy_touch_and_purity_are_frozen_after_issue(self):
		before = self._issued(workflow_state="Issue", target_qty=EMPLOYEE_TARGET)
		for changes in (
			{"source_alloy": "ALLOY-2"},
			{"source_alloy_qty": "0.5"},
			{"chain_metal_touch": "18KT"},
			{"chain_metal_purity": "91.6"},
		):
			doc = self._issued(workflow_state="Issue", _before=before)
			doc.update(changes)
			with self.assertRaises(ValidationError, msg=str(changes)):
				cs.validate_chain(doc)

		doc = self._issued(
			workflow_state="Issue", _before=before, alloy_batch_details=[]
		)
		cs.validate_chain(doc)
		self.assertEqual(
			[(row["batch"], row["qty"]) for row in doc.alloy_batch_details],
			[("AB-1", EMPLOYEE_ALLOY)],
		)

	def test_the_target_item_is_still_picked_at_receive(self):
		before = self._issued(workflow_state="Issue", target_item=None)
		doc = self._issued(workflow_state="Issue", _before=before)
		cs.validate_chain(doc)
		self.assertEqual(doc.target_item, "CH-22KT")

	def test_a_target_without_alloy_issued_may_still_change(self):
		# The weight rule's flow: no alloy went out, so the target is picked at Receive.
		self.purity = dict(SAME_PURITY)
		before = _employee_doc(chain_issue_stock_entry="SE-ISSUE-1")
		doc = _employee_doc(
			chain_issue_stock_entry="SE-ISSUE-1", _before=before, target_item="CH-22KT"
		)
		cs.validate_chain(doc)

	def test_cancel_issue_clears_the_alloy(self):
		doc = self._issued(workflow_state="Draft", chain_purchase_order=None)
		with patch.object(cs, "_cancel_stock_entry") as cancelled:
			cs.run_cancel_issue(doc)
		cancelled.assert_called_once_with("SE-ISSUE-1")
		self.assertIsNone(doc.source_alloy)
		self.assertIsNone(doc.source_alloy_qty)
		self.assertEqual(doc.alloy_batch_details, [])


class TestEmployeeAlloyEntries(_BuilderCase):
	def setUp(self):
		super().setUp()
		self.purity = dict(ALLOYED_DOWN)

	def _issued(self, **fields):
		doc = self._employee(
			source_item=FINE_24KT,
			source_alloy_check=1,
			source_alloy="ALLOY-1",
			source_alloy_qty=str(EMPLOYEE_ALLOY),
			alloy_batch_details=[_row("AB-1", EMPLOYEE_ALLOY)],
			**ALLOYED_TOUCH,
		)
		doc.update(fields)
		return doc

	def test_issue_moves_the_alloy_with_the_metal(self):
		cs.make_issue_stock_entry(self._issued())
		se = self.captured[-1]
		self.assertEqual(
			[(r["item_code"], r["batch_no"], r["qty"]) for r in se.items],
			[
				(FINE_24KT, "B-1", 6.0),
				(FINE_24KT, "B-2", 4.0),
				("ALLOY-1", "AB-1", EMPLOYEE_ALLOY),
			],
		)
		for row in se.items:
			self.assertEqual(row["s_warehouse"], "Chain RM - GK")
			self.assertEqual(row["t_warehouse"], "EMP-2 RM WH - GK")

	def test_issue_without_alloy_moves_only_the_metal(self):
		self.purity = dict(SAME_PURITY)
		cs.make_issue_stock_entry(self._employee())
		self.assertEqual(
			{r["item_code"] for r in self.captured[-1].items}, {"M-G-22KT-91.6-Y"}
		)

	def test_repack_consumes_all_the_alloy(self):
		with patch.object(
			cs,
			"get_batch_lane_map",
			return_value={
				"B-1": ("Regular Stock", None),
				"B-2": ("Regular Stock", None),
			},
		), patch.object(mc, "flt", _det_flt):
			cs.make_repack_stock_entry(
				self._issued(target_qty=10.8),
				[{"batch": "B-1", "qty": 6.0}, {"batch": "B-2", "qty": 3.909}],
				10.8,
			)
		se = self.captured[-1]
		consume = [r for r in se.items if r.get("s_warehouse")]
		alloy = [r for r in consume if r["item_code"] == "ALLOY-1"]
		self.assertEqual([(r["batch_no"], r["qty"]) for r in alloy], [("AB-1", 0.899)])
		self.assertEqual({r["s_warehouse"] for r in consume}, {"EMP-2 RM WH - GK"})
		produce = [r for r in se.items if r.get("t_warehouse")]
		self.assertEqual(
			[(r["item_code"], r["qty"]) for r in produce], [("CH-22KT", 10.8)]
		)

	def test_repack_refuses_an_alloy_allocation_short_of_the_issue(self):
		with patch.object(
			cs,
			"get_batch_lane_map",
			return_value={
				"B-1": ("Regular Stock", None),
				"B-2": ("Regular Stock", None),
			},
		), patch.object(mc, "flt", _det_flt):
			with self.assertRaisesRegex(ValidationError, "alloy batches cover 0.5"):
				cs.make_repack_stock_entry(
					self._issued(alloy_batch_details=[_row("AB-1", 0.5)]),
					[{"batch": "B-1", "qty": 6.0}, {"batch": "B-2", "qty": 4.0}],
					10.899,
				)


class TestControllerRouting(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		_("warm")

	def test_chain_submit_goes_to_receive_only(self):
		doc = _chain_doc(workflow_state="Submitted")
		with (
			patch.object(mc, "validate_receive") as validate_receive,
			patch.object(mc, "run_receive") as run_receive,
			patch.object(mc, "make_metal_stock_entry") as conversion,
		):
			mc.MetalConversions.before_submit(doc)
			mc.MetalConversions.on_submit(doc)
		validate_receive.assert_called_once_with(doc)
		run_receive.assert_called_once_with(doc)
		conversion.assert_not_called()

	def test_non_chain_submit_is_unchanged(self):
		doc = _chain_doc(is_chain_manufacturing="No", is_chain_subcontracting=0)
		with patch.object(mc, "validate_receive") as validate_receive:
			mc.MetalConversions.before_submit(doc)
		validate_receive.assert_not_called()

	def test_chain_cancel_goes_through_cancel_chain(self):
		doc = _chain_doc()
		with (
			patch.object(mc, "cancel_chain") as cancel_chain,
			patch.object(mc, "cancel_conversion_stock_entries") as conversion,
		):
			mc.MetalConversions.on_cancel(doc)
		cancel_chain.assert_called_once_with(doc)
		conversion.assert_not_called()


class TestWorkflowPatch(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_submitted_is_the_first_submitted_state(self):
		"""Frappe back-fills and programmatic submits take the first docstatus-1 state."""
		first_submitted = next(
			s for s, status in workflow_patch.STATES if status == "1"
		)
		self.assertEqual(first_submitted, workflow_patch.SUBMITTED)
		self.assertEqual(workflow_patch.STATES[0], (workflow_patch.DRAFT, "0"))

	def test_draft_offers_issue_or_submit_never_both(self):
		from_draft = {
			action: condition
			for state, action, _next, condition, _allowed in workflow_patch.TRANSITIONS
			if state == workflow_patch.DRAFT
		}
		self.assertEqual(
			from_draft[workflow_patch.ISSUE_ACTION], workflow_patch.IS_CHAIN
		)
		self.assertEqual(
			from_draft[workflow_patch.SUBMIT_ACTION], workflow_patch.NOT_CHAIN
		)
		# Either mode of Chain Manufacturing issues; a document saved before the field
		# existed (None) keeps the plain Submit.
		for value, expected in (("Yes", "Issue"), ("No", "Submit"), (None, "Submit")):
			doc = frappe._dict(is_chain_manufacturing=value)
			offered = [
				action
				for action, condition in from_draft.items()
				if frappe.safe_eval(condition, None, {"doc": doc})
			]
			self.assertEqual(offered, [expected], value)

	def test_issue_is_a_save_and_receive_submits_directly(self):
		status = dict(workflow_patch.STATES)
		self.assertEqual(status[workflow_patch.ISSUE], "0")
		self.assertNotIn("Receive", status)
		on_receive = [
			row[2]
			for row in workflow_patch.TRANSITIONS
			if row[:2] == (workflow_patch.ISSUE, workflow_patch.RECEIVE_ACTION)
		]
		self.assertEqual(on_receive, [workflow_patch.SUBMITTED])
		self.assertEqual(status[workflow_patch.SUBMITTED], "1")
		# Submitted is the end: no second, stock-less Submit after Receive.
		self.assertFalse(
			[
				row
				for row in workflow_patch.TRANSITIONS
				if row[0] == workflow_patch.SUBMITTED
			]
		)

	def test_cancel_issue_is_system_manager_only(self):
		self.assertIn(
			(
				workflow_patch.ISSUE,
				workflow_patch.CANCEL_ISSUE_ACTION,
				workflow_patch.DRAFT,
				None,
				workflow_patch.SYSTEM_MANAGER,
			),
			workflow_patch.TRANSITIONS,
		)
		others = [
			row[4] for row in workflow_patch.TRANSITIONS if row[1] != "Cancel Issue"
		]
		self.assertEqual(set(others), {workflow_patch.ALL})

	def _workflow_before_cancel_issue(self):
		"""The workflow as the first version of the patch created it."""
		workflow = _FakeWorkflow(
			states=[state for state, _doc_status in workflow_patch.STATES],
			transitions=[
				(row[0], row[1])
				for row in workflow_patch.TRANSITIONS
				if row[1] != workflow_patch.CANCEL_ISSUE_ACTION
			],
		)
		return workflow

	def test_an_existing_workflow_gets_only_cancel_issue(self):
		workflow = self._workflow_before_cancel_issue()
		before = list(workflow.transitions)
		with patch.object(workflow_patch.frappe, "logger"):
			workflow_patch._add_missing(workflow)
		self.assertEqual(workflow.transitions[: len(before)], before)
		added = workflow.transitions[len(before) :]
		self.assertEqual(len(added), 1)
		self.assertEqual(
			(added[0].state, added[0].action, added[0].next_state, added[0].allowed),
			("Issue", "Cancel Issue", "Draft", "System Manager"),
		)
		workflow.save.assert_called_once_with(ignore_permissions=True)

	def _workflow_with_conditions(self, **conditions):
		"""Every transition present, the two Draft rows carrying ``conditions``."""
		return _FakeWorkflow(
			states=[state for state, _doc_status in workflow_patch.STATES],
			transitions=[
				(
					row[0],
					row[1],
					conditions.get(row[1], row[3])
					if row[0] == workflow_patch.DRAFT
					else row[3],
				)
				for row in workflow_patch.TRANSITIONS
			],
		)

	def _draft_conditions(self, workflow):
		return {
			row.action: row.condition
			for row in workflow.transitions
			if row.state == workflow_patch.DRAFT
		}

	def test_first_release_conditions_move_to_chain_manufacturing(self):
		workflow = self._workflow_with_conditions(
			Submit="doc.is_chain_subcontracting != 1",
			Issue="doc.is_chain_subcontracting == 1",
		)
		with patch.object(workflow_patch.frappe, "logger"):
			workflow_patch._add_missing(workflow)
		self.assertEqual(
			self._draft_conditions(workflow),
			{"Submit": workflow_patch.NOT_CHAIN, "Issue": workflow_patch.IS_CHAIN},
		)
		workflow.save.assert_called_once_with(ignore_permissions=True)

	def test_a_condition_an_administrator_changed_is_left_alone(self):
		workflow = self._workflow_with_conditions(Issue="doc.custom_ready == 1")
		with patch.object(workflow_patch.frappe, "logger"):
			workflow_patch._add_missing(workflow)
		self.assertEqual(
			self._draft_conditions(workflow)["Issue"], "doc.custom_ready == 1"
		)
		workflow.save.assert_not_called()

	def test_a_complete_workflow_is_not_saved(self):
		workflow = self._workflow_before_cancel_issue()
		with patch.object(workflow_patch.frappe, "logger"):
			workflow_patch._add_missing(workflow)
			workflow_patch._add_missing(workflow)
		workflow.save.assert_called_once()

	def test_execute_amends_an_existing_workflow_and_creates_a_missing_one(self):
		workflow = self._workflow_before_cancel_issue()
		for exists, add_calls, create_calls in ((True, 1, 0), (False, 0, 1)):
			with (
				patch.object(workflow_patch, "_ensure_masters"),
				patch.object(workflow_patch.frappe.db, "exists", return_value=exists),
				patch.object(workflow_patch.frappe.db, "get_value", return_value=None),
				patch.object(workflow_patch.frappe, "get_doc", return_value=workflow),
				patch.object(workflow_patch, "_add_missing") as add_missing,
				patch.object(workflow_patch, "_create_workflow") as create_workflow,
			):
				workflow_patch.execute()
			self.assertEqual(add_missing.call_count, add_calls, exists)
			self.assertEqual(create_workflow.call_count, create_calls, exists)


class TestReceiveSubmitsDirectlyPatch(IntegrationTestCase):
	"""Existing sites move from Issue -> Receive -> Submit -> Submitted to Issue -> Submitted."""

	@classmethod
	def setUpClass(cls):
		pass

	LEGACY = [
		("Draft", "Submit", "Submitted"),
		("Draft", "Issue", "Issue"),
		("Issue", "Receive", "Receive"),
		("Issue", "Cancel Issue", "Draft"),
		("Receive", "Submit", "Submitted"),
	]

	def _workflow(self, transitions, states=("Draft", "Issue", "Submitted", "Receive")):
		return SimpleNamespace(
			name="Metal Conversions",
			states=[frappe._dict(state=s, idx=i) for i, s in enumerate(states, 1)],
			transitions=[
				frappe._dict(state=a, action=b, next_state=c, idx=i)
				for i, (a, b, c) in enumerate(transitions, 1)
			],
		)

	def _run(self, workflow, in_receive=2):
		real_count, real_set_value = frappe.db.count, frappe.db.set_value
		moved = []

		def count(doctype, *args, **kwargs):
			if doctype == receive_patch.DOCTYPE:
				return in_receive
			return real_count(doctype, *args, **kwargs)

		def set_value(doctype, *args, **kwargs):
			if doctype == receive_patch.DOCTYPE:
				moved.append(args)
				return None
			return real_set_value(doctype, *args, **kwargs)

		with (
			patch.object(receive_patch.frappe.db, "count", side_effect=count),
			patch.object(receive_patch.frappe.db, "set_value", side_effect=set_value),
		):
			changes = receive_patch.submit_on_receive(workflow)
		return changes, moved

	def _shape(self, workflow):
		return (
			[row.state for row in workflow.states],
			[(r.state, r.action, r.next_state) for r in workflow.transitions],
		)

	def test_legacy_workflow_moves_to_the_new_shape(self):
		workflow = self._workflow(self.LEGACY)
		changes, moved = self._run(workflow)
		self.assertTrue(changes)
		self.assertEqual(
			self._shape(workflow),
			(
				["Draft", "Issue", "Submitted"],
				[
					("Draft", "Submit", "Submitted"),
					("Draft", "Issue", "Issue"),
					("Issue", "Receive", "Submitted"),
					("Issue", "Cancel Issue", "Draft"),
				],
			),
		)
		self.assertEqual([r.idx for r in workflow.transitions], [1, 2, 3, 4])
		# The submitted conversions waiting in Receive are moved, not stranded.
		self.assertEqual(
			moved,
			[
				(
					{"workflow_state": "Receive", "docstatus": 1},
					"workflow_state",
					"Submitted",
				)
			],
		)

	def test_the_new_shape_matches_what_a_fresh_site_gets(self):
		workflow = self._workflow(self.LEGACY)
		self._run(workflow)
		fresh = (
			[state for state, _status in workflow_patch.STATES],
			[row[:3] for row in workflow_patch.TRANSITIONS],
		)
		self.assertEqual(self._shape(workflow), fresh)

	def test_a_second_run_changes_nothing(self):
		workflow = self._workflow(self.LEGACY)
		self._run(workflow)
		changes, moved = self._run(workflow)
		self.assertEqual((changes, moved), ([], []))

	def test_receive_kept_while_an_administrator_routes_through_it(self):
		"""A transition of their own still uses Receive: the state and its docs stay."""
		workflow = self._workflow(self.LEGACY + [("Receive", "Close", "Submitted")])
		_changes, moved = self._run(workflow)
		self.assertIn("Receive", [row.state for row in workflow.states])
		self.assertEqual(moved, [])

	def test_execute_saves_only_when_something_changed(self):
		for transitions, saves in ((self.LEGACY, 1), (workflow_patch.TRANSITIONS, 0)):
			workflow = self._workflow(
				[row[:3] for row in transitions],
				states=[s for s, _ in workflow_patch.STATES]
				if saves == 0
				else ("Draft", "Issue", "Submitted", "Receive"),
			)
			workflow.save = MagicMock()
			with (
				patch.object(receive_patch.frappe.db, "exists", return_value=True),
				patch.object(receive_patch.frappe, "get_doc", return_value=workflow),
				patch.object(receive_patch.frappe, "logger"),
				patch.object(receive_patch.frappe.db, "count", return_value=0),
			):
				receive_patch.execute()
			self.assertEqual(workflow.save.call_count, saves)


class _FakeWorkflow(SimpleNamespace):
	"""``transitions`` rows are ``(state, action)`` or ``(state, action, condition)``."""

	def __init__(self, states, transitions):
		super().__init__(
			name="Metal Conversions",
			states=[frappe._dict(state=state) for state in states],
			transitions=[
				frappe._dict(
					state=row[0],
					action=row[1],
					condition=row[2] if len(row) > 2 else None,
				)
				for row in transitions
			],
			save=MagicMock(),
		)

	def append(self, table, row):
		getattr(self, table).append(frappe._dict(row))


class TestEnsurePoItem(IntegrationTestCase):
	"""The PO service item is created in Gram, not the template's "Nos" (F1)."""

	@classmethod
	def setUpClass(cls):
		pass

	def _ensure(self, existing=False, template=True, gram=True):
		template_doc = SimpleNamespace(name=chain_sc_patch.TEMPLATE_ITEM)
		copy = SimpleNamespace(
			stock_uom="Nos",
			purchase_uom="Nos",
			sales_uom="Nos",
			uoms=[frappe._dict(uom="Nos", conversion_factor=1)],
			insert=MagicMock(),
		)
		answers = {
			("Item", chain_sc_patch.CHAIN_SC_PO_ITEM): existing,
			("Item", chain_sc_patch.TEMPLATE_ITEM): template,
			("UOM", chain_sc_patch.PO_ITEM_UOM): gram,
		}

		def _exists(doctype, name=None, *args, **kwargs):
			return answers[(doctype, name)]

		with (
			patch.object(chain_sc_patch.frappe.db, "exists", side_effect=_exists),
			patch.object(chain_sc_patch.frappe, "get_doc", return_value=template_doc),
			patch.object(
				chain_sc_patch.frappe, "copy_doc", return_value=copy
			) as copy_doc,
			patch.object(chain_sc_patch.frappe, "logger"),
		):
			chain_sc_patch._ensure_po_item()
		return copy, copy_doc

	def test_created_in_gram(self):
		item, _copy_doc = self._ensure()
		self.assertEqual(item.item_code, chain_sc_patch.CHAIN_SC_PO_ITEM)
		self.assertEqual(item.stock_uom, "Gram")
		self.assertIsNone(item.purchase_uom)
		self.assertIsNone(item.sales_uom)
		# ERPNext adds the Gram x 1 row itself on save.
		self.assertEqual(item.uoms, [])
		item.insert.assert_called_once_with(ignore_permissions=True)

	def test_keeps_the_template_uom_when_gram_is_missing(self):
		item, _copy_doc = self._ensure(gram=False)
		self.assertEqual(item.stock_uom, "Nos")
		self.assertEqual(len(item.uoms), 1)
		item.insert.assert_called_once()

	def test_an_existing_item_is_left_alone(self):
		_item, copy_doc = self._ensure(existing=True)
		copy_doc.assert_not_called()

	def test_no_template_no_item(self):
		_item, copy_doc = self._ensure(template=False)
		copy_doc.assert_not_called()


class TestNoStockEntryTypeOfItsOwn(IntegrationTestCase):
	"""Chain Manufacturing books the existing transfer types; it seeds none of its own."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_the_seed_list_has_no_chain_sc_type_and_has_the_issue_types(self):
		seeded = {row[0] for row in seed_patch.STOCK_ENTRY_TYPES}
		self.assertNotIn(cleanup_patch.STOCK_ENTRY_TYPE, seeded)
		self.assertIn(cs.ISSUE_TO_EMPLOYEE_SE_TYPE, seeded)
		self.assertIn(cs.ISSUE_TO_SUPPLIER_SE_TYPE, seeded)

	def _cleanup(self, exists=True, entries=(), delete_side_effect=None):
		"""Run the patch over a type whose Stock Entries have the docstatuses ``entries``."""
		self.count_filters = []

		def _exists(doctype, *args, **kwargs):
			self.assertEqual(doctype, "Stock Entry Type")
			return exists

		def _count(doctype, filters=None, *args, **kwargs):
			self.assertEqual(doctype, "Stock Entry")
			self.count_filters.append(filters)
			# Apply the patch's own docstatus filter, so a test proves it is there.
			operator, bound = filters["docstatus"]
			self.assertEqual(operator, "<")
			return sum(1 for docstatus in entries if docstatus < bound)

		with (
			patch.object(cleanup_patch.frappe.db, "exists", side_effect=_exists),
			patch.object(cleanup_patch.frappe.db, "count", side_effect=_count),
			patch.object(
				cleanup_patch.frappe, "delete_doc", side_effect=delete_side_effect
			) as delete_doc,
			patch.object(cleanup_patch.frappe, "logger"),
			patch.object(cleanup_patch, "print", create=True) as printed,
		):
			cleanup_patch.execute()
		self.printed = [c.args[0] for c in printed.call_args_list]
		return delete_doc

	def test_an_unused_type_is_deleted(self):
		delete_doc = self._cleanup()
		delete_doc.assert_called_once_with(
			"Stock Entry Type", cleanup_patch.STOCK_ENTRY_TYPE, ignore_permissions=True
		)
		self.assertIn("deleted", self.printed[0])

	def test_a_submitted_or_draft_entry_keeps_it_and_says_why(self):
		for entries in ((1,), (0,), (1, 1, 2)):
			self._cleanup(entries=entries).assert_not_called()
			self.assertIn("kept", self.printed[0])
			self.assertEqual(
				self.count_filters[0]["stock_entry_type"],
				cleanup_patch.STOCK_ENTRY_TYPE,
			)

	def test_only_cancelled_entries_do_not_keep_it(self):
		# As Frappe's own delete check: a cancelled entry does not hold a master.
		self._cleanup(entries=(2, 2, 2)).assert_called_once()

	def test_a_missing_type_is_a_no_op(self):
		self._cleanup(exists=None).assert_not_called()
		self.assertEqual(self.count_filters, [])

	def test_a_type_linked_elsewhere_is_kept_without_failing_migrate(self):
		self._cleanup(delete_side_effect=frappe.LinkExistsError)
		self.assertIn("another record links it", self.printed[0])
