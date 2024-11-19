import frappe
from frappe.query_builder import Case
from frappe.query_builder.functions import Locate


@frappe.whitelist()
def get_diamond_grade(doctype, txt, searchfield, start, page_len, filters):
	data1 = frappe.db.get_all(
		"Customer Diamond Grade",
		{"parent": filters.get("customer")},
		["diamond_grade_1", "diamond_grade_2", "diamond_grade_3", "diamond_grade_4"],
	)

	lst = [tuple([row[i]]) for row in data1 for i in row if row.get(i)]

	return tuple(lst)
