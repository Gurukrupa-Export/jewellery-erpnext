import json

import frappe
from frappe.utils import cint

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

	for grade in row:
		if not grade:
			continue
		is_customer_grade = frappe.db.get_value(
			"Attribute Value", grade, "is_customer_diamond_quality"
		)
		if bool(cint(is_customer_diamond)) == bool(is_customer_grade):
			return grade

	return None


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

	customer = filters.get("customer")
	diamond_quality = filters.get("diamond_quality")
	use_custom = filters.get("use_custom_diamond_grade")
	is_customer_diamond = int(filters.get("is_customer_diamond") or 0)

	data = frappe.db.get_all(
		"Customer Diamond Grade",
		{"parent": customer, "diamond_quality": diamond_quality},
		["diamond_grade_1", "diamond_grade_2", "diamond_grade_3", "diamond_grade_4"],
	)

	if not data:
		return []

	grade_fields = [
		"diamond_grade_1",
		"diamond_grade_2",
		"diamond_grade_3",
		"diamond_grade_4",
	]

	if use_custom:
		# Return all unique non-empty grades
		grades = set()
		for row in data:
			for key in grade_fields:
				if row.get(key):
					grades.add(row[key])
		return [(g,) for g in sorted(grades)]

	# When not using custom grade, pick the correct grade based on is_customer_diamond
	all_grades = []
	for row in data:
		for key in grade_fields:
			grade = row.get(key)
			if grade and grade not in all_grades:
				all_grades.append(grade)

	for grade in all_grades:
		is_customer_grade = frappe.db.get_value(
			"Attribute Value", grade, "is_customer_diamond_quality"
		)
		if is_customer_diamond and is_customer_grade:
			return [(grade,)]
		elif not is_customer_diamond and not is_customer_grade:
			return [(grade,)]

	# Fallback: return first available grade
	if all_grades:
		return [(all_grades[0],)]
	return []
