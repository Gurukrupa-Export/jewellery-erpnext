# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Which Stock Entry types an Employee IR Receive submit can still mint (C-01).

``EmployeeIR.on_submit_receive`` pins the naming counter of every type returned here before
it locks Bins (lock_order RULE F), and nothing else. The answer must therefore be a SUPERSET
of what the cascade mints; a type it misses is caught by the Stock Entry ``before_naming``
guard, which takes that counter NOWAIT. The mint sites and their gates:

* ``finding_repack.create_finding_repack_for_row`` -> **Repack**, when the operation repacks
  findings (``is_finding_repack_eir``) and the row has a complete finding slot
  (``finding_pairs``). Its produced rows then ride a **Material Transfer (WORK ORDER)**.
* ``main_slip_inject.inject_extra_metal_for_eir_receive`` -> **Material Transfer (WORK ORDER)**
  and, when the employee's MSL warehouse is short of the alloy, a purity **Repack**. Runs only
  for a gain (``is_raw_material`` and received gross above gross) or for finding rows. The
  transfer-or-Repack choice is made from an MSL Bin read inside the row loop, after the Bins
  are locked and after earlier rows' postings, so it cannot be predicted here: a gain plans
  both types.
* ``loss_stock_entry.create_loss_stock_entries`` -> **Process Loss**, when a row of
  ``employee_loss_details`` / ``manually_book_loss_details`` still has a loss at the Stock Entry
  Detail ``transfer_qty`` precision -- or, for a document still being inserted (kggk_uat plans
  in ``before_insert``, before validate builds those rows), when any operation row was received
  lighter than issued.

Every test reads the same in-memory values the cascade reads, with the cascade's own
expression. Where validate's 3-dp rounding (``precision.round_employee_ir_weights_to_precision``)
could change an answer, the test is widened so it holds on either side of validate.
"""

import frappe
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.finding_repack import (
	finding_pairs,
	is_finding_repack_eir,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry import (
	PROCESS_LOSS_SE_TYPE,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject import (
	MATERIAL_TRANSFER_STOCK_ENTRY_TYPE,
	REPACK_STOCK_ENTRY_TYPE,
)

# precision.round_employee_ir_weights_to_precision(doc, precision=3), run by validate.
VALIDATE_WEIGHT_PRECISION = 3


def receive_mint_types(eir):
	"""Sorted tuple of the Stock Entry types ``eir``'s receive cascade can mint; ``()`` if none."""
	if eir.get("type") != "Receive":
		return ()

	rows = eir.get("employee_ir_operations") or []
	types = set()
	if _has_gain(eir, rows) or _has_finding(eir, rows):
		types.update((MATERIAL_TRANSFER_STOCK_ENTRY_TYPE, REPACK_STOCK_ENTRY_TYPE))
	if _has_representable_loss(eir) or (
		_not_yet_validated(eir) and _has_row_loss(rows)
	):
		types.add(PROCESS_LOSS_SE_TYPE)
	return tuple(sorted(types))


def _has_gain(eir, rows):
	"""``inject_extra_metal_for_eir_receive``'s ``has_gain``, for any row.

	Unrounded, exactly as the cascade compares. Rounding is monotonic, so a row that still
	gains after validate's rounding also gains before it: the raw test is the wider one.
	"""
	if not cint(eir.get("is_raw_material")):
		return False
	return any(
		(flt(r.get("received_gross_wt")) - flt(r.get("gross_wt"))) > 0 for r in rows
	)


def _has_finding(eir, rows):
	"""``create_finding_repack_for_row``'s gate, for any row (the type was checked by the caller).

	``finding_pairs`` first: it is in memory, while ``is_finding_repack_eir`` costs a query.
	``finding_wt1`` / ``finding_wt2`` are not rounded by validate.
	"""
	return any(finding_pairs(r) for r in rows) and is_finding_repack_eir(eir)


def _not_yet_validated(eir):
	"""A document being inserted (kggk_uat's REST insert-and-submit, pre-locked in
	``before_insert``): validate has not built its loss rows yet. ``__islocal`` is what
	``Document.is_new`` reads; ``insert`` sets it before ``check_if_latest`` / ``before_insert``.
	"""
	return bool(eir.get("__islocal"))


def _has_row_loss(rows):
	"""Any row received lighter than it was issued.

	kggk_uat takes the receive pre-locks early (``take_receive_prelocks``, from
	``check_if_latest`` / ``before_insert``). On an insert-and-submit that is before validate's
	``validate_process_loss`` builds the loss rows, which are derived from exactly this
	difference, so planning Process Loss on it keeps the plan a superset there. It can
	over-plan (a loss that books no Process Loss), which only holds MAT-STE- the way every
	receive did before C-01. A saved draft already carries the loss rows its last save built,
	so it is planned from those, exactly as on kggk_prod.
	"""
	return any(
		flt(r.get("received_gross_wt"))
		and flt(r.get("gross_wt")) > flt(r.get("received_gross_wt"))
		for r in rows
	)


def _has_representable_loss(eir):
	"""``loss_stock_entry._prepare_loss_row``'s gate, for any loss row.

	The cascade reads ``flt(proportionally_loss, transfer_qty precision) > 0``. Validate rounds
	``proportionally_loss`` to 3 dp first, and rounding twice can keep a loss that rounding once
	drops (0.0049 -> 0.005 -> 0.01 at precision 2), so both forms are tested.
	"""
	loss_rows = list(eir.get("employee_loss_details") or []) + list(
		eir.get("manually_book_loss_details") or []
	)
	if not loss_rows:
		return False
	se_precision = frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3
	for row in loss_rows:
		loss = row.get("proportionally_loss")
		if flt(loss, se_precision) > 0:
			return True
		if flt(flt(loss, VALIDATE_WEIGHT_PRECISION), se_precision) > 0:
			return True
	return False
