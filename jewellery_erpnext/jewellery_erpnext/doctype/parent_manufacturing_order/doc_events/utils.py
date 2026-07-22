import frappe


def update_parent_details(self):
	if not self.quotation_item:
		return

	po_row = frappe.db.get_value(
		"Quotation Item", self.quotation_item, "custom_po_details"
	)
	if not po_row:
		return

	m_plan_row = frappe.db.get_value(
		"Purchase Order Item", po_row, "custom_m_plan_details"
	)

	if not m_plan_row:
		return

	mfg_plan_details = frappe.db.get_value(
		"Manufacturing Plan Table",
		m_plan_row,
		["parent", "quotation", "quotation_item"],
		as_dict=1,
	)

	if not mfg_plan_details:
		return

	self.parent_quotation = mfg_plan_details.get("quotation")
	self.parent_mp = mfg_plan_details.get("parent")
	self.ref_customer = frappe.db.get_value(
		"Quotation", self.parent_quotation, "party_name"
	)
