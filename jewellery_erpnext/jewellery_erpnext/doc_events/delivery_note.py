import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doc_events.sales_invoice import set_gst_details


def validate(self, method):

	for row in self.items:
		if row.against_sales_order:
			if row.bom:
				bom_doc = frappe.get_doc("BOM", row.bom)
				row.custom_diamond_pcs = bom_doc.total_diamond_pcs
				row.custom_gemstone_pcs = bom_doc.total_gemstone_pcs
				row.custom_other_weight = bom_doc.total_other_weight
				row.custom_metal_weight = bom_doc.total_metal_weight
				row.custom_finding_weight = bom_doc.finding_weight
				row.custom_diamond_weight = bom_doc.total_diamond_weight_in_gms
				row.custom_gemstone_weight = bom_doc.total_gemstone_weight_in_gms
				row.custom_gross_weight = bom_doc.gross_weight
	self.custom_diamond_pcs = sum(int(r.custom_diamond_pcs or 0) for r in self.items)
	self.custom_gemstone_pcs = sum(float(r.custom_gemstone_pcs or 0) for r in self.items)
	self.custom_other_weight = sum(float(r.custom_other_weight or 0) for r in self.items)
	self.custom_metal_weight = sum(float(r.custom_metal_weight or 0) for r in self.items)
	self.custom_finding_weight = sum(float(r.custom_finding_weight or 0) for r in self.items)
	self.custom_diamond_weight = sum(float(r.custom_diamond_weight or 0) for r in self.items)
	self.custom_gemstone_weight = sum(float(r.custom_gemstone_weight or 0) for r in self.items)
	self.custom_gross_weight = sum(float(r.custom_gross_weight or 0) for r in self.items)

	# The e-invoice item table and GST used to be copied straight from the
	# Sales Order (a fixed snapshot at mapping time), so removing a row here
	# left stale amounts behind. Rebuild both from whatever items are
	# currently on this Delivery Note instead.
	update_dn_einvoice_items(self)
	self.total = flt(sum(flt(row.amount) for row in self.items))
	set_gst_details(self)
	self.calculate_taxes_and_totals()
	apply_einvoice_item_tax(self)


def _matching_e_invoice_item_parents(sales_type):
	return frappe.get_all(
		"Sales Type Multiselect",
		filters={"parenttype": "E Invoice Item", "sales_type": sales_type},
		pluck="parent",
	)


def update_dn_einvoice_items(self):
	invoice_data = {}
	is_branch_customer = frappe.db.get_value(
		"Sales Type Multiselect", {"parent": self.customer, "sales_type": "Branch"}
	)
	matching_parents = _matching_e_invoice_item_parents(self.sales_type)

	def add(item_code, amount, qty, rate, hsn, uom):
		if not item_code:
			return
		if item_code in invoice_data:
			invoice_data[item_code]["amount"] += amount
			invoice_data[item_code]["qty"] += qty
			invoice_data[item_code]["rate"] = rate
		else:
			invoice_data[item_code] = {
				"amount": amount,
				"qty": qty,
				"rate": rate,
				"hsn_code": hsn,
				"uom": uom,
			}

	for row in self.items:
		if not row.bom:
			continue
		bom_doc = frappe.get_doc("BOM", row.bom)

		for i in bom_doc.metal_detail:
			# frappe.throw("jiij")
			if i.is_customer_item:
				continue
			einvoice_item, hsn_code, uom = frappe.db.get_value(
				"E Invoice Item",
				{
					"is_for_metal": 1,
					"metal_type": i.metal_type,
					"metal_purity": i.metal_touch,
					"name": ["in", matching_parents],
				},
				["name", "hsn_code", "uom"],
			) or (None, None, None)
			add(einvoice_item, flt(i.amount), flt(i.quantity), flt(i.rate), hsn_code, uom)

			if not is_branch_customer:
				filter_value = "is_for_labour" if i.is_customer_item else "is_for_making"
				making_item, making_hsn, making_uom = frappe.db.get_value(
					"E Invoice Item",
					{
						filter_value: 1,
						"metal_type": i.metal_type,
						"metal_purity": i.metal_touch,
						"name": ["in", matching_parents],
					},
					["name", "hsn_code", "uom"],
				) or (None, None, None)
				making_amount = flt(i.making_amount) + flt(i.wastage_amount)
				# frappe.throw(f"{making_amount}")
				add(making_item, making_amount, flt(i.quantity), flt(i.making_rate), making_hsn, making_uom)

		for i in bom_doc.finding_detail:
			if i.is_customer_item:
				continue
			einvoice_item, hsn_code, uom = frappe.db.get_value(
				"E Invoice Item",
				{
					"is_for_finding": 1,
					"metal_type": i.metal_type,
					"metal_purity": i.metal_touch,
					"finding_category": i.finding_category,
					"name": ["in", matching_parents],
				},
				["name", "hsn_code", "uom"],
			) or (None, None, None)
			if not einvoice_item:
				result = frappe.db.get_value(
					"E Invoice Item",
					{
						"is_for_metal": 1,
						"metal_type": i.metal_type,
						"metal_purity": i.metal_touch,
						"finding_category": ["is", "not set"],
						"name": ["in", matching_parents],
					},
					["name", "hsn_code", "uom"],
				)
				if result:
					einvoice_item, hsn_code, uom = result
			add(einvoice_item, flt(i.amount), flt(i.quantity), flt(i.rate), hsn_code, uom)

			making_amount = flt(i.making_amount) + flt(i.wastage_amount)
			filter_value = "is_for_labour" if i.is_customer_item else "is_for_finding_making"
			result = frappe.db.get_value(
				"E Invoice Item",
				{
					filter_value: 1,
					"metal_type": i.metal_type,
					"metal_purity": i.metal_touch,
					"finding_category": i.finding_category,
					"name": ["in", matching_parents],
				},
				["name", "hsn_code", "uom"],
			)
			if not result:
				fallback_filter_value = "is_for_labour" if i.is_customer_item else "is_for_making"
				result = frappe.db.get_value(
					"E Invoice Item",
					{
						fallback_filter_value: 1,
						"metal_type": i.metal_type,
						"metal_purity": i.metal_touch,
						"name": ["in", matching_parents],
					},
					["name", "hsn_code", "uom"],
				)
			if result:
				making_item, making_hsn, making_uom = result
				add(making_item, making_amount, flt(i.quantity), flt(i.making_rate), making_hsn, making_uom)

		for i in bom_doc.diamond_detail:
			if i.is_customer_item:
				continue
			result = frappe.db.get_value(
				"E Invoice Item",
				{"is_for_diamond": 1, "diamond_type": i.diamond_type, "name": ["in", matching_parents]},
				["name", "hsn_code", "uom"],
			)
			if not result:
				continue
			einvoice_item, hsn_code, uom = result
			amount = flt(i.diamond_rate_for_specified_quantity)
			qty = flt(i.quantity)
			rate = amount / qty if qty else 0
			add(einvoice_item, amount, qty, rate, hsn_code, uom)

		for i in bom_doc.gemstone_detail:
			if i.is_customer_item:
				continue
			result = frappe.db.get_value(
				"E Invoice Item",
				{"is_for_gemstone": 1, "name": ["in", matching_parents]},
				["name", "hsn_code", "uom"],
			)
			if not result:
				continue
			einvoice_item, hsn_code, uom = result
			if is_branch_customer:
				amount = flt(i.se_rate) * flt(i.quantity)
				rate = flt(i.se_rate)
			else:
				amount = flt(i.gemstone_rate_for_specified_quantity)
				rate = flt(i.total_gemstone_rate)
			add(einvoice_item, amount, flt(i.quantity), rate, hsn_code, uom)

		if bom_doc.hallmarking_amount:
			result = frappe.db.get_value("E Invoice Item", {"is_for_hallmarking": 1}, ["name", "hsn_code", "uom"])
			if result:
				einvoice_item, hsn_code, uom = result
				add(einvoice_item, flt(bom_doc.hallmarking_amount), 1, flt(bom_doc.hallmarking_amount), hsn_code, uom)

		if bom_doc.certification_amount:
			result = frappe.db.get_value("E Invoice Item", {"is_for_certification": 1}, ["name", "hsn_code", "uom"])
			if result:
				einvoice_item, hsn_code, uom = result
				add(
					einvoice_item,
					flt(bom_doc.certification_amount),
					1,
					flt(bom_doc.certification_amount),
					hsn_code,
					uom,
				)

	self.set("custom_invoice_item", [])
	for item_code, data in invoice_data.items():
		self.append(
			"custom_invoice_item",
			{
				"item_code": item_code,
				"item_name": item_code,
				"uom": data["uom"] or "Nos",
				"qty": -abs(data["qty"]) if self.is_return else data["qty"],
				"rate": data["rate"],
				"amount": flt(data["amount"], 3),
			},
		)


def apply_einvoice_item_tax(self):
	"""set_gst_details() already stamped cgst_rate/sgst_rate/igst_rate on
	self.items (all rows share the same rate) - reuse that same rate on the
	custom_invoice_item rows, which don't go through calculate_taxes_and_totals."""
	if not self.get("custom_invoice_item"):
		return
	overall_rate = 0
	if self.items:
		first = self.items[0]
		overall_rate = flt(first.igst_rate) or (flt(first.cgst_rate) + flt(first.sgst_rate))
	for row in self.custom_invoice_item:
		row.tax_rate = overall_rate
		row.tax_amount = flt(flt(row.amount) * overall_rate / 100, 2)
		row.amount_with_tax = flt(row.amount) + row.tax_amount
		