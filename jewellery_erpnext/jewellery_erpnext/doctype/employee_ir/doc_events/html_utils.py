import frappe
import json
from frappe.utils import flt

@frappe.whitelist()
def get_summary_data(doc):
	if isinstance(doc, str):
		doc = json.loads(doc)
	data = [
		{
			"gross_wt": 0,
			"net_wt": 0,
			"finding_wt": 0,
			"diamond_wt": 0,
			"gemstone_wt": 0,
			"other_wt": 0,
			"diamond_pcs": 0,
			"gemstone_pcs": 0,
		}
	]
	for row in doc.get("employee_ir_operations"):
		for i in data[0]:
			if row.get(i):
				value = row.get(i)
				if i in ["diamond_pcs", "gemstone_pcs"] and row.get(i):
					value = int(row.get(i))
				data[0][i] += flt(value, 3)
			data[0][i] = flt(data[0][i], 3)

	return data
