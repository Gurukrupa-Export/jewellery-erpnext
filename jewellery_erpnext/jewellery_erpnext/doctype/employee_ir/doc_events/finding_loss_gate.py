"""Per-operation, per-finding-category gate on Employee IR loss booking.

Department Operation carries a ``finding_loss_booking`` table of
``(finding_category, loss_booking)`` rows. A finding item whose category is
listed there with ``loss_booking`` unticked must not have process loss booked
against it for that operation — a purchased chain or clasp is expected back at
exactly its issued weight, so any shortfall belongs to the metal, not to it.

**Fail-open contract.** A category that is not listed, or an operation with an
empty table, books loss exactly as before. This is deliberate: it means shipping
this module changes nothing on any existing site until someone adds a row.
``is_loss_booking_blocked`` returns True only when the item is a finding AND its
category is listed AND the flag is off.

Where the gate applies:

  * ``EmployeeIR.book_metal_loss`` skips blocked rows while building the
    proportional pool. Because the skip happens before ``total_qty`` is summed,
    the survivors absorb the blocked row's share and the booked total still
    equals ``gross_wt - received_gross_wt`` — so the balance validators in
    ``validation_utils`` need no changes.
  * ``validate_loss_rows_against_gate`` throws on any loss row (auto or manual)
    that sits on a blocked category. It runs on validate AND on submit; the
    submit call is what catches a draft saved before an admin flipped the flag,
    since ``validate_process_loss`` early-returns once ``docstatus != 0``.

Blocked rows are thrown on rather than silently dropped at Stock Entry creation:
``get_employee_ir_loss_map`` reads the child tables directly, so suppressing only
the Stock Entry line would book a MOP Log loss with no matching stock movement.
"""

import frappe
from frappe import _
from frappe.utils import cint

FINDING_PREFIX = "F"
FINDING_CATEGORY_ATTRIBUTE = "Finding Category"


def get_loss_booking_map(operation):
	"""``{finding_category: books_loss}`` for a Department Operation.

	An absent key means "books loss" — see the fail-open contract above. Returns
	an empty dict when the operation is unset or lists no categories, which makes
	every ``is_loss_booking_blocked`` call short-circuit to False.
	"""
	if not operation:
		return {}

	rows = frappe.get_all(
		"Finding Category Loss Booking",
		filters={"parent": operation, "parenttype": "Department Operation"},
		fields=["finding_category", "loss_booking"],
	)
	return {
		row.finding_category: cint(row.loss_booking)
		for row in rows
		if row.finding_category
	}


def get_finding_category_map(item_codes):
	"""Bulk ``{item_code: finding_category}`` for finding items.

	One query for the whole batch of item codes rather than a lookup per loss
	row, following the app's prefetch-map convention. Non-finding codes are
	dropped up front: only ``F``-prefixed items carry a Finding Category
	attribute, so querying for the rest would be wasted work.
	"""
	codes = sorted({c for c in (item_codes or []) if c and c[0] == FINDING_PREFIX})
	if not codes:
		return {}

	rows = frappe.get_all(
		"Item Variant Attribute",
		filters={"parent": ["in", codes], "attribute": FINDING_CATEGORY_ATTRIBUTE},
		fields=["parent", "attribute_value"],
	)
	return {row.parent: row.attribute_value for row in rows if row.attribute_value}


def is_loss_booking_blocked(item_code, booking_map, category_map):
	"""True only for a finding whose category is listed AND flagged off."""
	if not booking_map:
		return False
	if not item_code or item_code[0] != FINDING_PREFIX:
		return False

	category = (category_map or {}).get(item_code)
	if not category or category not in booking_map:
		# Fail open: an unlisted category books loss exactly as before.
		return False
	return not booking_map[category]


def validate_loss_rows_against_gate(doc):
	"""Throw if any booked loss row sits on a blocked finding category.

	Covers both loss tables. ``employee_loss_details`` is normally already clean
	because ``book_metal_loss`` excludes blocked rows while building it — this
	catches the case where the Department Operation flag was flipped after the
	draft was saved, since neither ``validate_process_loss`` nor
	``validate_manually_book_loss_details`` rebuilds anything once
	``docstatus != 0``. ``manually_book_loss_details`` is operator-entered, so
	this is its only gate.

	Not gated on ``docstatus``: the on_submit caller needs it to run when
	``docstatus`` is already 1, matching ``validate_loss_tables_required``.
	"""
	if getattr(doc, "type", None) != "Receive":
		return

	booking_map = get_loss_booking_map(getattr(doc, "operation", None))
	if not booking_map:
		return

	tables = (
		("Employee Loss Details", getattr(doc, "employee_loss_details", None) or []),
		(
			"Manually Book Loss Details",
			getattr(doc, "manually_book_loss_details", None) or [],
		),
	)
	item_codes = [row.item_code for _label, rows in tables for row in rows]
	category_map = get_finding_category_map(item_codes)

	for label, rows in tables:
		for row in rows:
			if not is_loss_booking_blocked(row.item_code, booking_map, category_map):
				continue
			frappe.throw(
				_(
					"{0} row #{1}: Loss Booking is turned off for Finding Category "
					"<b>{2}</b> on operation <b>{3}</b>, so no loss can be booked against "
					"<b>{4}</b>. Remove the row (re-save the document to rebuild the "
					"automatic loss), or tick Loss Booking for that category on the "
					"Department Operation."
				).format(
					label,
					row.idx,
					category_map.get(row.item_code),
					doc.operation,
					row.item_code,
				)
			)
