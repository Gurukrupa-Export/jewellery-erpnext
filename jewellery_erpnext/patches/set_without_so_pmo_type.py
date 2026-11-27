"""Give Without-SO Parent Manufacturing Orders their plan's mode as Type.

make_finding_without_so_order used to stamp "Finding Manufacturing" on every PMO a
"Finding Manufacturing - Without SO" plan raised, because PMO Type had no option for that mode.
Only those PMOs are touched (is_without_so), so Finding Manufacturing PMOs raised from Work
Orders keep their Type. Idempotent.
"""

import frappe


def execute():
	PMO = frappe.qb.DocType("Parent Manufacturing Order")
	(
		frappe.qb.update(PMO)
		.set(PMO.type, "Finding Manufacturing - Without SO")
		.where((PMO.is_without_so == 1) & (PMO.type == "Finding Manufacturing"))
	).run()
