import frappe,json
from frappe.query_builder import Case
from frappe.query_builder.functions import Locate


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

	data = frappe.db.get_all(
		"Customer Diamond Grade",
		{
			"parent": customer,
			"diamond_quality": diamond_quality
		},
		["diamond_grade_1", "diamond_grade_2", "diamond_grade_3", "diamond_grade_4"]
	)

	if not data:
		return []

	# Always get diamond_grade_1 to pre-set
	diamond_grade_1 = data[0].get("diamond_grade_1")

	# If use_custom is false, return only diamond_grade_1
	if not use_custom:
		return [(diamond_grade_1,)]

	# Else return all unique non-empty grades
	grades = set()
	for row in data:
		for key in ["diamond_grade_1", "diamond_grade_2", "diamond_grade_3", "diamond_grade_4"]:
			if row.get(key):
				grades.add(row[key])

	return [(g,) for g in sorted(grades)]