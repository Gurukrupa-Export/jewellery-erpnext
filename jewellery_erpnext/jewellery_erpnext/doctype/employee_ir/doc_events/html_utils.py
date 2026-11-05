import json

import frappe
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
	# Received Gross Wt is shown as received_gross_wt + finding_wt1 + finding_wt2,
	# so it is summed separately from the plain per-field totals above.
	received_gross_wt = 0
	for row in doc.get("employee_ir_operations"):
		for i in data[0]:
			if row.get(i):
				value = row.get(i)
				if i in ["diamond_pcs", "gemstone_pcs"] and row.get(i):
					value = int(row.get(i))
				data[0][i] += flt(value, 3)
			data[0][i] = flt(data[0][i], 3)
		received_gross_wt += (
			flt(row.get("received_gross_wt"), 3)
			+ flt(row.get("finding_wt1"), 3)
			+ flt(row.get("finding_wt2"), 3)
		)

	data[0]["received_gross_wt"] = flt(received_gross_wt, 3)

	return data
