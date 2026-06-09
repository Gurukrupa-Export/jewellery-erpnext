import json

import frappe

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
