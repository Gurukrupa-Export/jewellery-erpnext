import json

import frappe
from frappe.utils import cint, cstr

# from frappe.query_builder import Case
# from frappe.query_builder.functions import Locate

# @frappe.whitelist()
# def get_diamond_grade(doctype, txt, searchfield, start, page_len, filters):
# 	data1 = frappe.db.get_all(
# 		"Customer Diamond Grade",
# 		{"parent": filters.get("customer")},
# 		["diamond_grade_1", "diamond_grade_2", "diamond_grade_3", "diamond_grade_4"],
# 	)

# 	lst = [tuple([row[i]]) for row in data1 for i in row if row.get(i)]

# 	return tuple(lst)


GRADE_FIELDS = [
	"diamond_grade_1",
	"diamond_grade_2",
	"diamond_grade_3",
	"diamond_grade_4",
]

# Sales Type -> the is_customer_diamond it implies. Outright is a company-owned sale, Outwork a
# customer-supplied one, so each admits only one kind of grade. Hybrid, Branch Sales,
# Certification and Repairing are deliberately absent: they admit either kind.
SALES_TYPE_CUSTOMER_DIAMOND = {"Outright": 0, "Outwork": 1}


def is_customer_diamond_flag(value):
	"""Read the Sales Order Item's Yes/No text as the PMO's is_customer_diamond checkbox.

	The two sides used to disagree on case -- Manufacturing Plan lowercased the string while
	the PMO compared it to "Yes" -- so a row saved as "yes" resolved its grade one way at plan
	time and the other way on the PMO. One reading of the value keeps them in step.
	"""
	return 1 if cstr(value).strip().lower() == "yes" else 0


def _grade_flags(grades):
	"""is_customer_diamond_quality for every grade name, in one query."""
	names = [g for g in grades if g]
	if not names:
		return {}

	return {
		d.name: cint(d.is_customer_diamond_quality)
		for d in frappe.get_all(
			"Attribute Value",
			filters={"name": ["in", names]},
			fields=["name", "is_customer_diamond_quality"],
		)
	}


def pick_diamond_grade(grades, is_customer_diamond, flags=None):
	"""Choose one grade out of an ordered diamond_grade_1..4 sequence.

	Customer diamond: only an Attribute Value flagged is_customer_diamond_quality will do, so an
	unflagged grade is never a substitute. Returning None here is what makes the PMO throw
	instead of silently stamping the wrong grade onto the tracking BOM and the item variant.

	Not customer diamond: either kind is acceptable. Plain grades are preferred so a correctly
	configured customer keeps resolving to the grade it always has; the flagged one is only
	taken when nothing else is configured for the quality.

	``flags`` maps grade name -> truthy is_customer_diamond_quality. Callers that already hold
	those values pass them in; leaving it None looks them up.
	"""
	grades = [g for g in grades if g]
	if not grades:
		return None

	if flags is None:
		flags = _grade_flags(grades)

	if cint(is_customer_diamond):
		for grade in grades:
			if flags.get(grade):
				return grade
		return None

	for grade in grades:
		if not flags.get(grade):
			return grade

	return grades[0]


def custom_override_grades(all_grades, sales_type, is_customer_diamond, flags=None):
	"""The grades the manual override (use_custom_diamond_grade) may offer.

	Sales Type and the checkbox have to agree, because Sales Type is what sets
	custom_customer_diamond on the quotation in the first place (public/js/doctype_js/quotation.js)
	and that is what becomes is_customer_diamond here. A PMO where the two disagree has been
	edited into a state neither answer fits, so the list is empty on purpose: that sends the user
	back to fix one of them rather than letting them pick a grade the sale does not support.

	``flags`` is the same optional grade -> is_customer_diamond_quality map pick_diamond_grade
	takes, for callers that already hold those values.
	"""
	expected = SALES_TYPE_CUSTOMER_DIAMOND.get(sales_type)
	if expected is None:
		# No Outright/Outwork rule to apply -- including a PMO whose read-only sales_type never
		# fetched, which is every PMO created before the field existed.
		return all_grades

	if expected != cint(is_customer_diamond):
		return []

	if flags is None:
		flags = _grade_flags(all_grades)

	return [g for g in all_grades if cint(bool(flags.get(g))) == expected]


def sales_type_expects(sales_type):
	"""The is_customer_diamond this Sales Type requires, or None when it dictates nothing."""
	return SALES_TYPE_CUSTOMER_DIAMOND.get(sales_type)


def customer_grades(customer, diamond_quality):
	"""Every grade configured for a customer/quality pair, in diamond_grade_1..4 order."""
	if not (customer and diamond_quality):
		return []

	row = frappe.db.get_value(
		"Customer Diamond Grade",
		{"parent": customer, "diamond_quality": diamond_quality},
		GRADE_FIELDS,
	)
	if not row:
		return []

	grades = []
	for grade in row:
		if grade and grade not in grades:
			grades.append(grade)

	return grades


@frappe.whitelist()
def get_allowed_custom_diamond_grades(
	customer=None,
	ref_customer=None,
	diamond_quality=None,
	sales_type=None,
	is_customer_diamond=0,
):
	"""The grades a manual override may legitimately hold.

	The same answer the link query gives the dropdown and the controller enforces on save, so
	the form can tell whether a value already in the field has just become invalid.
	"""
	return custom_override_grades(
		customer_grades(ref_customer or customer, diamond_quality),
		sales_type,
		is_customer_diamond,
	)


def resolve_diamond_grade(customer, diamond_quality, is_customer_diamond):
	"""Pick the grade to auto-apply for a customer/quality pair.

	Both the PMO controller (on save) and the form (on field change) must resolve the grade
	through here. When the two sides disagree, the form overwrites the stored grade every time
	the record is opened, which marks it dirty without any user edit.
	"""
	if not (customer and diamond_quality):
		return None

	row = frappe.db.get_value(
		"Customer Diamond Grade",
		{"parent": customer, "diamond_quality": diamond_quality},
		GRADE_FIELDS,
	)
	if not row:
		return None

	return pick_diamond_grade(row, is_customer_diamond)


@frappe.whitelist()
def get_auto_diamond_grade(
	customer=None, ref_customer=None, diamond_quality=None, is_customer_diamond=0
):
	"""Form-side preview of the grade the PMO controller will store on save."""
	return resolve_diamond_grade(
		ref_customer or customer, diamond_quality, is_customer_diamond
	)


@frappe.whitelist()
def get_diamond_grade(doctype, txt, searchfield, start, page_len, filters):
	if isinstance(filters, str):
		filters = json.loads(filters)

	# Same customer the controller resolves against: a PMO with a Ref Customer stores that
	# customer's grade, so listing the ordering customer's grades here offers values the save
	# would immediately replace.
	customer = filters.get("ref_customer") or filters.get("customer")
	diamond_quality = filters.get("diamond_quality")
	use_custom = filters.get("use_custom_diamond_grade")
	is_customer_diamond = cint(filters.get("is_customer_diamond"))
	sales_type = filters.get("sales_type")

	all_grades = customer_grades(customer, diamond_quality)
	if not all_grades:
		return []

	if use_custom:
		# Manual override: offer the grades this sale admits, so a user can pick one the
		# automatic rule would not -- but not one Outright/Outwork rules out.
		return [
			(g,)
			for g in sorted(
				custom_override_grades(all_grades, sales_type, is_customer_diamond)
			)
		]

	grade = pick_diamond_grade(all_grades, is_customer_diamond)

	return [(grade,)] if grade else []
