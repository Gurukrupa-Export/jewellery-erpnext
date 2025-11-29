import frappe
from frappe import _


def validate_po(self):
	allow_quotation = frappe.db.get_value(
		"Company", self.company, "custom_allow_quotation_from_po_only"
	)
	hallmarking_charge = (
		frappe.db.get_value("Customer Certification Price", self.party_name, "hallmarking_amount") or 0
	)
	po_data = frappe._dict()
	for row in self.items:
		update_customer_details(self, row)
		update_hallmarking_amount(hallmarking_charge, row)
		if allow_quotation and not row.po_no:
			frappe.throw(
				_("Row {0} : Quotation can be created from Purchase Order for this Company").format(row.idx)
			)
		elif row.po_no:
			if not po_data.get(row.po_no):
				po_data[row.po_no] = frappe.db.get_value("Purchase Order", row.po_no, "custom_quotation")
			if not po_data.get(row.po_no):
				frappe.db.set_value("Purchase Order", row.po_no, "custom_quotation", self.name)


def update_customer_details(self, row):
	if not row.custom_customer_gold:
		row.custom_customer_gold = self.custom_customer_gold
	if not row.custom_customer_diamond:
		row.custom_customer_diamond = self.custom_customer_diamond
	if not row.custom_customer_stone:
		row.custom_customer_stone = self.custom_customer_stone
	if not row.custom_customer_good:
		row.custom_customer_good = self.custom_customer_good
	if not row.custom_customer_finding:
		row.custom_customer_finding = self.custom_customer_finding


def update_hallmarking_amount(hallmarking_charge, row):
	row.custom_hallmarking_amount = hallmarking_charge * row.qty

# from jewellery_erpnext.jewellery_erpnext.doc_events.sales_invoice import (
# 	update_making_charges,
# )
from jewellery_erpnext.jewellery_erpnext.doc_events.bom_utils import _calculate_diamond_amount
from frappe.utils import flt

def update_si(self):
	invoice_data = {}
	is_branch_customer = frappe.db.get_value(
		"Sales Type Multiselect", {"parent": self.party_name, "sales_type": "Branch"}
	)
	
	for row in self.items:
		if row.quotation_bom :
			bom_doc = frappe.get_doc("BOM", row.quotation_bom)
	
			update_bom_details(self, row, bom_doc, is_branch_customer, invoice_data)
			# update_einvoice_items(self, invoice_data)


def update_einvoice_items(self, invoice_data):
	# self.custom_invoice_item = []
	for row in invoice_data:
		if invoice_data[row]["amount"] > 0:
			self.append(
				"custom_invoice_item",
				{
					"item_code": row,
					"item_name": row,
					"uom": invoice_data[row]["uom"] or "Nos",
					"gst_hsn_code": invoice_data[row]["hsn_code"],
					"conversion_factor": 1,
					"qty": invoice_data[row]["qty"],
					"rate": flt(invoice_data[row]["amount"] / invoice_data[row]["qty"], 3),
					"base_rate": flt(invoice_data[row]["amount"] / invoice_data[row]["qty"], 3),
					"amount": flt(invoice_data[row]["amount"], 3),
					"base_amount": invoice_data[row]["amount"],
					"diamond_quality": self.diamond_quality,
					# "income_account": invoice_data[row]["income_account"],
					# "cost_center": invoice_data[row]["cost_center"],
				},
			)


def update_bom_details(self, row, bom_doc, is_branch_customer, invoice_data):
	gold_item = None
	gold_making_item = None
	bom_doc.customer = self.party_name
	for i in bom_doc.metal_detail:
		amount = i.amount
		if is_branch_customer:
			amount = i.se_rate * i.quantity
		# if not i.is_customer_item:
		# 	update_making_charges(row, bom_doc, i, self.gold_rate_with_gst)

		filter_value = "is_for_making"
		if i.is_customer_item:
			filter_value = "is_for_labour"
		if not gold_item:
			gold_item, hsn_code, gold_uom = frappe.db.get_value(
				"E Invoice Item",
				{"is_for_metal": 1, "metal_type": i.metal_type, "metal_purity": i.metal_touch},
				["name", "hsn_code", "uom"],
			)
		if not gold_making_item:
			gold_making_item, making_hsn_code, gold_making_uom = frappe.db.get_value(
				"E Invoice Item",
				{filter_value: 1, "metal_type": i.metal_type, "metal_purity": i.metal_touch},
				["name", "hsn_code", "uom"],
			)
		if gold_item:
			if invoice_data.get(gold_item):
				invoice_data[gold_item]["amount"] += amount
			else:
				invoice_data[gold_item] = {
					"amount": amount,
					"hsn_code": hsn_code,
					"qty": 0,
					"uom": gold_uom,
					# "income_account": row.income_account,
					# "cost_center": row.cost_center,
				}

			if amount > 0:
				invoice_data[gold_item]["qty"] += i.quantity

		if gold_making_item and not is_branch_customer:
			if invoice_data.get(gold_making_item):
				invoice_data[gold_making_item]["amount"] += i.making_amount
			else:
				invoice_data[gold_making_item] = {
					"amount": i.making_amount,
					"hsn_code": making_hsn_code,
					"qty": 0,
					"uom": gold_making_uom,
					# "income_account": row.income_account,
					# "cost_center": row.cost_center,
				}

			if i.making_amount > 0:
				invoice_data[gold_making_item]["qty"] += i.quantity

	einvoice_item = None
	making_item = None
	for i in bom_doc.finding_detail:
		amount = i.amount
		if is_branch_customer:
			amount = i.se_rate * i.quantity
		# if not i.is_customer_item:
		# 	update_making_charges(row, bom_doc, i, self.gold_rate_with_gst)
		filter_value = "is_for_finding_making"
		if i.is_customer_item:
			filter_value = "is_for_labour"

		if not einvoice_item:
			einvoice_item, hsn_code, uom = frappe.db.get_value(
				"E Invoice Item",
				{"is_for_finding": 1, "metal_type": i.metal_type, "metal_purity": i.metal_touch},
				["name", "hsn_code", "uom"],
			)
		
		if not making_item:
			making_item, making_hsn_code, making_uom = frappe.db.get_value(
				"E Invoice Item",
				{filter_value: 1, "metal_type": i.metal_type, "metal_purity": i.metal_touch},
				["name", "hsn_code", "uom"],
			)
		
	
		if einvoice_item:
			if invoice_data.get(einvoice_item):
				invoice_data[einvoice_item]["amount"] += amount
			else:
				invoice_data[einvoice_item] = {
					"amount": amount,
					"hsn_code": hsn_code,
					"qty": 0,
					"uom": uom,
					# "income_account": row.income_account,
					# "cost_center": row.cost_center,
				}

			if amount > 0:
				invoice_data[einvoice_item]["qty"] += i.quantity

		if making_item:
			if invoice_data.get(making_item):
				invoice_data[making_item]["amount"] += i.making_amount
			else:
				invoice_data[making_item] = {
					"amount": i.making_amount,
					"hsn_code": making_hsn_code,
					"qty": 0,
					"uom": making_uom,
					# "income_account": row.income_account,
					# "cost_center": row.cost_center,
				}

			if i.making_amount > 0:
				invoice_data[making_item]["qty"] += i.quantity

	if is_branch_customer and bom_doc.get("operation_cost"):
		invoice_data[gold_making_item] = {"amount": bom_doc.operation_cost, "qty": 1}

	einvoice_item = None
	ss_range = {}
	for diamond in bom_doc.diamond_detail:
		actual_qty = diamond.quantity
		diamond.quantity = flt(diamond.quantity, bom_doc.diamond_pricision)
		diamond.difference = actual_qty - diamond.quantity
		if not diamond.sieve_size_range:
			continue
		det = ss_range.get(diamond.sieve_size_range) or {}
		# det['pcs'] = flt(det.get("pcs")) + diamond.pcs
		det["pcs"] = (flt(det.get("pcs")) + flt(diamond.get("pcs"))) or 1
		det["quantity"] = flt(flt(det.get("quantity")) + diamond.quantity, 3)
		det["std_wt"] = flt(flt(det["quantity"], 2) / det["pcs"], 3)
		ss_range[diamond.sieve_size_range] = det

	cust_diamond_price_list_type = frappe.db.get_value(
		"Customer", bom_doc.customer, "diamond_price_list"
	)

	diamond_price_list_data = frappe._dict()

	for i in bom_doc.diamond_detail:
		bom_doc.cust_diamond_price_list_type = cust_diamond_price_list_type
		det = ss_range.get(diamond.sieve_size_range) or {}
		# amount = 0
		amount = _calculate_diamond_amount(bom_doc, i, det, diamond_price_list_data)
		# amount = i.diamond_rate_for_specified_quantity
		if is_branch_customer:
			amount = i.se_rate * i.quantity
		if not einvoice_item:
			einvoice_item, hsn_code, uom = frappe.db.get_value(
				"E Invoice Item",
				{"is_for_diamond": 1, "diamond_type": i.diamond_type},
				["name", "hsn_code", "uom"],
			)

		if einvoice_item:
			einvoice_item_name = einvoice_item
			# if i.diamond_type != "Real":
			# 	einvoice_item_name += f" {i.diamond_type}"

			if invoice_data.get(einvoice_item_name):
				invoice_data[einvoice_item_name]["amount"] += amount
			else:
				invoice_data[einvoice_item_name] = {
					"amount": amount,
					"hsn_code": hsn_code,
					"qty": 0,
					"uom": uom,
					# "income_account": row.income_account,
					# "cost_center": row.cost_center,
				}

			if amount > 0:
				invoice_data[einvoice_item_name]["qty"] += i.quantity

		# if custom_diamond_quality := row.custom_diamond_quality or self.custom_diamond_quality:
		# 	i.quality = custom_diamond_quality

	einvoice_item = None
	for i in bom_doc.gemstone_detail:
		actual_qty = i.quantity
		i.quantity = flt(i.quantity, bom_doc.gemstone_pricision)
		i.difference = actual_qty - i.quantity
		# Calculate the weight per piece
		i.pcs = int(i.pcs) or 1
		gemstone_weight_per_pcs = i.quantity / i.pcs

		# Create filters for retrieving the Gemstone Price List
		filters = {
			"price_list": self.selling_price_list,
			"price_list_type": i.price_list_type,
			"customer": self.customer,
			"cut_or_cab": i.cut_or_cab,
			"gemstone_grade": i.gemstone_grade,
		}
		if i.price_list_type == "Weight (in cts)":
			filters.update(
				{
					"gemstone_type": i.gemstone_type,
					"stone_shape": i.stone_shape,
					"gemstone_quality": i.gemstone_quality,
					"from_weight": ["<=", gemstone_weight_per_pcs],
					"to_weight": [">=", gemstone_weight_per_pcs],
				}
			)
		elif i.price_list_type == "Multiplier" and i.gemstone_size:
			filters.update(
				{
					"to_stone_size": [">=", i.gemstone_size],
					"from_stone_size": ["<=", i.gemstone_size],
				}
			)
		else:
			filters["gemstone_type"] = i.gemstone_type
			filters["stone_shape"] = i.stone_shape
			filters["gemstone_quality"] = i.gemstone_quality
			filters["gemstone_quality"] = i.gemstone_quality
			filters["gemstone_size"] = i.gemstone_size

		# Retrieve the Gemstone Price List and calculate the rate
		gemstone_price_list = frappe.get_list(
			"Gemstone Price List",
			filters=filters,
			fields=["name", "rate", "handling_charges_rate", "supplier_fg_purchase_rate"],
			order_by="effective_from desc",
			limit=1,
		)

		multiplier = 0
		item_category = frappe.db.get_value("Item", bom_doc.item, "item_category")
		if i.price_list_type == "Multiplier":
			for gr in gemstone_price_list:
				multiplier = (
					frappe.db.get_value(
						"Gemstone Multiplier",
						{"parent": gr.name, "item_category": item_category, "parentfield": "gemstone_multiplier"},
						frappe.scrub(i.gemstone_quality),
					)
					or 0
				)
				fg_multiplier = (
					frappe.db.get_value(
						"Gemstone Multiplier",
						{
							"parent": gr.name,
							"item_category": item_category,
							"parentfield": "supplier_fg_multiplier",
						},
						frappe.scrub(i.gemstone_quality),
					)
					or 0
				)

		if not gemstone_price_list:
			frappe.msgprint(
				f"Gemstone Amount for {i.gemstone_type} is 0\n Please Check if Gemstone Price Exists For {filters}"
			)
			return 0

		# Get Handling Rate of the Diamond if it is a cutomer provided Diamond
		pr = int(i.gemstone_pr)
		if i.price_list_type == "Multiplier":
			rate = multiplier * pr
		else:
			rate = (
				gemstone_price_list[0].get("handlihandling_charges_rateng_rate")
				if i.is_customer_item
				else gemstone_price_list[0].get("rate")
			)
		i.total_gemstone_rate = rate
		i.gemstone_rate_for_specified_quantity = int(rate) * i.quantity

		amount = i.gemstone_rate_for_specified_quantity
		if is_branch_customer:
			amount = i.se_rate * i.quantity
		if not einvoice_item:
			einvoice_item, hsn_code, uom = frappe.db.get_value(
				"E Invoice Item", {"is_for_gemstone": 1}, ["name", "hsn_code", "uom"]
			)

		if einvoice_item:
			if invoice_data.get(f"{einvoice_item}"):
				invoice_data[f"{einvoice_item}"]["amount"] += amount
			else:
				invoice_data[f"{einvoice_item}"] = {
					"amount": amount,
					"hsn_code": hsn_code,
					"qty": 0,
					"uom": uom,
					# "income_account": row.income_account,
					# "cost_center": row.cost_center,
				}

			if amount > 0:
				invoice_data[f"{einvoice_item}"]["qty"] += i.quantity