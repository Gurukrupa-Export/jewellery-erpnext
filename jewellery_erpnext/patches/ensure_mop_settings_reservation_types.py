import frappe

_REQUIRED_TYPES = ("Repack", "Material Transfer (WORK ORDER)")


def execute():
	if not frappe.db.exists("DocType", "MOP Settings"):
		return
	existing = set(
		frappe.db.get_all(
			"Stock Entry Type To Reservation",
			filters={"parent": "MOP Settings"},
			pluck="stock_entry_type_to_reservation",
		)
	)
	doc = frappe.get_doc("MOP Settings", "MOP Settings")
	changed = False
	for se_type in _REQUIRED_TYPES:
		if se_type not in existing:
			doc.append(
				"stock_entry_type_to_reservation",
				{"stock_entry_type_to_reservation": se_type},
			)
			changed = True
	if changed:
		doc.save(ignore_permissions=True)
