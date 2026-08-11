import frappe


def update_parent_details(self):
	if not self.sales_order_item:
		return

	po_row = frappe.db.get_value(
		"Sales Order Item", self.sales_order_item, "custom_po_details"
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
		["parent", "sales_order", "docname"],
		as_dict=1,
	)

	if not mfg_plan_details:
		return

	if mfg_plan_details.get("docname"):
		quotation = frappe.db.get_value(
			"Sales Order Item", mfg_plan_details["docname"], "prevdoc_docname"
		)
		self.parent_quotation = quotation

	self.parent_sales_order = mfg_plan_details.get("sales_order")
	self.parent_mp = mfg_plan_details.get("parent")

	# Ref Customer belongs to the quotation the parent sales order line was raised against --
	# that is where the real customer behind an internal order is recorded. Fall back to the
	# parent sales order's customer when the quotation carries none, which is what the
	# ref_customer field's fetch_from resolves to anyway.
	quotation_ref_customer = None
	if self.parent_quotation:
		quotation_ref_customer = frappe.db.get_value(
			"Quotation", self.parent_quotation, "ref_customer"
		)

	self.ref_customer = quotation_ref_customer or frappe.db.get_value(
		"Sales Order", self.parent_sales_order, "customer"
	)
