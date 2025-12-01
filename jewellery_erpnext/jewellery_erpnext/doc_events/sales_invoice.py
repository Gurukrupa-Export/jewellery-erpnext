from datetime import datetime, timedelta

import frappe
from erpnext.setup.utils import get_exchange_rate
from frappe import _
from frappe.query_builder.custom import ConstantColumn
from frappe.utils import flt, get_last_day

from jewellery_erpnext.jewellery_erpnext.doc_events.bom_utils import _calculate_diamond_amount
from jewellery_erpnext.jewellery_erpnext.doc_events.quotation import update_totals


def before_validate(self, method):
	# copying Items Table to Invoice Item table
	if self.item_same_as_above:
		self.invoice_item = []
		for row in self.items:
			duplicate_row = {}
			for key in row.__dict__:
				duplicate_row[key] = row.get(key)
			duplicate_row["name"] = None

			if duplicate_row:
				self.append("invoice_item", duplicate_row)

	for row in self.items:
		if row.serial_no:
			row.bom = frappe.db.get_value("BOM", {"tag_no": row.serial_no}, "name")
	
	update_income_account(self)
	payment_terms_data = update_si_data(self)
	update_payment_terms(self, payment_terms_data)


def update_si_data(self):
	self.is_customer_metal = False
	self.is_customer_diamond = False
	invoice_data = {}
	payment_terms_data = {}
	is_branch_customer = frappe.db.get_value(
		"Sales Type Multiselect", {"parent": self.customer, "sales_type": "Branch"}
	)
	separate_hallmarking_invoice = frappe.db.get_value(
		"Customer", self.customer, "custom_separate_hallmarking_invoice"
	)
	for row in self.items:
		if row.bom and not self.item_same_as_above:
			exchange_rate = 1
			bom_doc = frappe.get_doc("BOM", row.bom)
			if bom_doc.currency != self.currency:
				exchange_rate = get_exchange_rate(
					bom_doc.currency, self.currency, transaction_date=self.posting_date
				)

			update_bom_details(self, row, bom_doc, is_branch_customer, invoice_data)

			if row.get("custom_freight_amount"):
				custom_item, hsn_code, uom = frappe.db.get_value(
					"E Invoice Item", {"is_for_freight": 1}, ["name", "hsn_code", "uom"]
				)
				if invoice_data.get(custom_item):
					invoice_data[custom_item]["qty"] += 1
					invoice_data[custom_item]["amount"] += row.custom_freight_amount
				else:
					invoice_data[custom_item] = {
						"qty": 1,
						"hsn_code": hsn_code,
						"uom": uom,
						"amount": row.custom_freight_amount,
						"income_account": row.income_account,
						"cost_center": row.cost_center,
					}
			if row.get("custom_hallmarking_amount") and not separate_hallmarking_invoice:
				custom_item, hsn_code, uom = frappe.db.get_value(
					"E Invoice Item", {"is_for_hallmarking": 1}, ["name", "hsn_code", "uom"]
				)
				if invoice_data.get(custom_item):
					invoice_data[custom_item]["qty"] += 1
					invoice_data[custom_item]["amount"] += row.custom_hallmarking_amount
				else:
					invoice_data[custom_item] = {
						"qty": 1,
						"hsn_code": hsn_code,
						"uom": uom,
						"amount": row.custom_hallmarking_amount,
						"income_account": row.income_account,
						"cost_center": row.cost_center,
					}
			# for i in invoice_data:
			# 	invoice_data[i]["amount"] *= exchange_rate

			update_einvoice_items(self, invoice_data, payment_terms_data)

			bom_doc = frappe.get_doc("BOM", row.bom)
			if self.gold_rate_with_gst > 0 and bom_doc.gold_rate_with_gst > 0:
				row.metal_amount = (
					bom_doc.gold_bom_amount / bom_doc.gold_rate_with_gst
				) * self.gold_rate_with_gst
			else:
				row.metal_amount = bom_doc.gold_bom_amount
			row.metal_amount *= exchange_rate
			row.making_amount = bom_doc.making_charge * exchange_rate
			row.finding_amount = bom_doc.finding_bom_amount * exchange_rate
			row.diamond_amount = bom_doc.diamond_bom_amount * exchange_rate
			row.gemstone_amount = bom_doc.gemstone_bom_amount * exchange_rate
			row.custom_certification_amount = bom_doc.certification_amount * exchange_rate
			row.custom_freight_amount = bom_doc.freight_amount * exchange_rate
			row.custom_hallmarking_amount = bom_doc.hallmarking_amount * exchange_rate
			row.custom_custom_duty_amount = bom_doc.custom_duty_amount * exchange_rate
			row.rate = flt(
				row.metal_amount
				+ row.making_amount
				+ row.diamond_amount
				+ row.finding_amount
				+ row.gemstone_amount
				+ row.custom_certification_amount
				+ row.custom_freight_amount
				+ row.custom_hallmarking_amount
				+ row.custom_custom_duty_amount,
				3,
			)
			row.amount = row.qty * row.rate
	return payment_terms_data


def update_einvoice_items(self, invoice_data, payment_terms_data):
	self.invoice_item = []
	for row in invoice_data:
		if invoice_data[row]["amount"] > 0:
			if payment_terms_data.get(row):
				payment_terms_data[row] += flt(invoice_data[row]["amount"], 3)
			else:
				payment_terms_data[row] = flt(invoice_data[row]["amount"], 3)
			self.append(
				"invoice_item",
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
					"income_account": invoice_data[row]["income_account"],
					"cost_center": invoice_data[row]["cost_center"],
				},
			)

	if self.invoice_item:
		self.taxes_and_charges = None
		self.taxes = []
		out_state = 0
		if frappe.db.get_value("Address", self.company_address, "state") != frappe.db.get_value(
			"Address", self.customer_address, "state"
		):
			out_state = 1

		self.taxes_and_charges = frappe.db.get_value(
			"Sales Taxes and Charges Template",
			{"sales_type": self.sales_type, "out_state": out_state, "company": self.company},
		)


def update_bom_details(self, row, bom_doc, is_branch_customer, invoice_data):
	gold_item = None
	gold_making_item = None
	bom_doc.customer = self.customer
	for i in bom_doc.metal_detail:
		amount = i.amount
		if is_branch_customer:
			amount = i.se_rate * i.quantity
		if not i.is_customer_item:
			update_making_charges(row, bom_doc, i, self.gold_rate_with_gst)

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
					"income_account": row.income_account,
					"cost_center": row.cost_center,
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
					"income_account": row.income_account,
					"cost_center": row.cost_center,
				}

			if i.making_amount > 0:
				invoice_data[gold_making_item]["qty"] += i.quantity

		if i.is_customer_item:
			self.is_customer_metal = True

	einvoice_item = None
	making_item = None
	for i in bom_doc.finding_detail:
		amount = i.amount
		if is_branch_customer:
			amount = i.se_rate * i.quantity
		if not i.is_customer_item:
			update_making_charges(row, bom_doc, i, self.gold_rate_with_gst)
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
		
		# if not i.not_finding_rate:
		# 	if gold_item:
		# 		if invoice_data.get(gold_item):
		# 			invoice_data[gold_item]["amount"] += amount
		# 		else:
		# 			invoice_data[gold_item] = {
		# 				"amount": amount,
		# 				"hsn_code": hsn_code,
		# 				"qty": 0,
		# 				"uom": gold_uom,
		# 				"income_account": row.income_account,
		# 				"cost_center": row.cost_center,
		# 			}

		# 		if amount > 0:
		# 			invoice_data[gold_item]["qty"] += i.quantity

		# 	if gold_making_item and not is_branch_customer:
		# 		if invoice_data.get(gold_making_item):
		# 			invoice_data[gold_making_item]["amount"] += i.making_amount
		# 		else:
		# 			invoice_data[gold_making_item] = {
		# 				"amount": i.making_amount,
		# 				"hsn_code": making_hsn_code,
		# 				"qty": 0,
		# 				"uom": gold_making_uom,
		# 				"income_account": row.income_account,
		# 				"cost_center": row.cost_center,
		# 			}

		# 		if i.making_amount > 0:
		# 			invoice_data[gold_making_item]["qty"] += i.quantity

		# else:
		if einvoice_item:
			if invoice_data.get(einvoice_item):
				invoice_data[einvoice_item]["amount"] += amount
			else:
				invoice_data[einvoice_item] = {
					"amount": amount,
					"hsn_code": hsn_code,
					"qty": 0,
					"uom": uom,
					"income_account": row.income_account,
					"cost_center": row.cost_center,
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
					"income_account": row.income_account,
					"cost_center": row.cost_center,
				}

			if i.making_amount > 0:
				invoice_data[making_item]["qty"] += i.quantity

		if i.is_customer_item:
			self.is_customer_metal = True

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
					"income_account": row.income_account,
					"cost_center": row.cost_center,
				}

			if amount > 0:
				invoice_data[einvoice_item_name]["qty"] += i.quantity

		if custom_diamond_quality := row.custom_diamond_quality or self.custom_diamond_quality:
			i.quality = custom_diamond_quality

		if i.is_customer_item:
			self.is_customer_diamond = True

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
		# if i.price_list_type == "Weight (in cts)":
		# 	filters.update(
		# 		{
		# 			"gemstone_type": i.gemstone_type,
		# 			"stone_shape": i.stone_shape,
		# 			"gemstone_quality": i.gemstone_quality,
		# 			"from_weight": ["<=", gemstone_weight_per_pcs],
		# 			"to_weight": [">=", gemstone_weight_per_pcs],
		# 		}
		# 	)
		if i.price_list_type == "Diamond Range" and i.gemstone_size:
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
		if i.price_list_type == "Diamond Range":
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
		if i.price_list_type == "Diamond Range":
			rate = multiplier * pr
		else:
			rate = (
				gemstone_price_list[0].get("handling_charges_rate")
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
					"income_account": row.income_account,
					"cost_center": row.cost_center,
				}

			if amount > 0:
				invoice_data[f"{einvoice_item}"]["qty"] += i.quantity

	bom_doc.doc_pricision = (
		2 if frappe.db.get_value("Customer", bom_doc.customer, "custom_consider_2_digit_for_bom") else 3
	)
	bom_doc.diamond_pricision = (
		2
		if frappe.db.get_value("Customer", bom_doc.customer, "custom_consider_2_digit_for_diamond")
		else 3
	)
	bom_doc.gemstone_pricision = (
		2
		if frappe.db.get_value("Customer", bom_doc.customer, "custom_consider_2_digit_for_gemstone")
		else 3
	)
	bom_doc.gold_rate_with_gst = self.gold_rate_with_gst
	bom_doc.validate()
	bom_doc.save()
	update_totals("BOM", bom_doc.name)


def update_making_charges(row, bom_doc, bom_row, gold_rate):
	bom_doc.set_additional_rate = False
	item_details = frappe.db.get_value(
		"Item", row.item_code, ["item_subcategory", "setting_type"], as_dict=True
	)
	sub_category, setting_type = item_details.get("item_subcategory"), item_details.get(
		"setting_type"
	)

	MCP = frappe.qb.DocType("Making Charge Price")
	MCPIS = frappe.qb.DocType("Making Charge Price Item Subcategory")
	MCPFS = frappe.qb.DocType("Making Charge Price Finding Subcategory")
	if bom_row.parentfield == "metal_detail":
		child_table = MCPIS
	else:
		child_table = MCPFS
	subcat_subcategory = (
		sub_category if not bom_row.get("finding_type") else bom_row.get("finding_type")
	)
	query = (
		frappe.qb.from_(MCP)
		.left_join(child_table)
		.on(child_table.parent == MCP.name)
		.select(
			# MCP.metal_purity,
			child_table.rate_per_gm,
			child_table.rate_per_pc,
			child_table.rate_per_gm_threshold,
			child_table.wastage,
			child_table.supplier_fg_purchase_rate,
		)
		.where(
			(MCP.customer == bom_doc.customer)
			& (MCP.setting_type == setting_type)
			& (MCP.metal_type == bom_row.metal_type)
			& (MCP.from_gold_rate <= gold_rate)
			& (MCP.to_gold_rate >= gold_rate)
		)
	)

	subquery = (
		frappe.qb.from_(child_table)
		.left_join(MCP)
		.on(child_table.parent == MCP.name)
		.select(child_table.name)
		.where((child_table.parent == MCP.name))
		.run()
	)
	if subquery:
		query = query.where(child_table.subcategory == subcat_subcategory)
	else:
		query = query.where((child_table.subcategory.isnull()) | (child_table.subcategory == ""))

	if bom_row.parentfield != "metal_detail":
		query = query.where(child_table.metal_touch == bom_row.metal_touch)

	query = query.limit(1)
	making_charge_details = query.run(as_dict=True)

	if not making_charge_details and bom_row.parentfield != "metal_detail":
		# Subquery to check for existence
		subquery = (
			frappe.qb.from_(MCPIS)
			.left_join(MCP)
			.on(MCPIS.parent == MCP.name)
			.select(MCPIS.name)
			.where((MCPIS.parent == MCP.name) & (MCPIS.subcategory == sub_category))
			.run()
		)
		query = (
			frappe.qb.from_(MCP)
			.left_join(MCPIS)
			.on(MCPIS.parent == MCP.name)
			.select(
				# MCP.metal_purity,
				MCPIS.rate_per_gm,
				MCPIS.rate_per_pc,
				MCPIS.rate_per_gm_threshold,
				MCPIS.wastage,
				MCPIS.subcategory,
				MCPIS.supplier_fg_purchase_rate,
				ConstantColumn(1).as_("non_finding_rate"),
			)
			.where(
				(MCP.customer == bom_doc.customer)
				& (MCP.setting_type == setting_type)
				& (MCP.metal_type == bom_row.metal_type)
				& (MCP.from_gold_rate <= gold_rate)
				& (MCP.to_gold_rate >= gold_rate)
			)
			.limit(1)
		)
		if subquery:
			query = query.where(MCPIS.subcategory == sub_category)
		else:
			query = query.where((MCPIS.subcategory.isnull()) | (MCPIS.subcategory == ""))

		making_charge_details = query.run(as_dict=True)

	if len(making_charge_details) > 0:
		making_charges = making_charge_details[0]

		bom_row.making_rate = flt(making_charges.get("rate_per_gm"))

		additional_net_weight = 0
		if not bom_doc.set_additional_rate and bom_row.parentfield == "metal_detail":
			if frappe.db.get_value(
				"Customer", bom_doc.customer, "compute_making_charges_on"
			) == "Diamond Inclusive" and flt(bom_row.metal_purity) == flt(bom_doc.metal_purity):
				if not bom_doc.total_diamond_weight_per_gram:
					bom_doc.total_diamond_weight_per_gram = flt(flt(bom_doc.total_diamond_weight) / 5, 3)
				bom_row.additional_net_weight = bom_doc.total_diamond_weight_per_gram
				additional_net_weight = bom_row.additional_net_weight
				bom_doc.set_additional_rate = True

		if bom_doc.metal_and_finding_weight < (making_charges.get("rate_per_gm_threshold") or 0):
			metal_making_charges = making_charges.get("rate_per_pc")
		else:
			metal_making_charges = bom_row.making_rate * (bom_row.quantity + additional_net_weight)

		bom_row.making_amount = metal_making_charges

		bom_row.wastage_rate = flt(making_charges.get("wastage"))

		# Add the wastage percentage to the making charges
		bom_row.wastage_amount = bom_row.wastage_rate * bom_row.amount / 100

		bom_row.fg_purchase_rate = flt(making_charges.get("supplier_fg_purchase_rate"))
		bom_row.fg_purchase_amount = bom_row.fg_purchase_rate * (
			bom_row.quantity + additional_net_weight
		)


def update_payment_terms(self, payment_terms_data=None):
	# frappe.throw(str(payment_terms_data))
	if self.payment_terms_template:
		return
	
	if not self.grand_total:
		return
	# custom_term = None
	# if frappe.db.exists("Customer Payment Terms", {"customer": self.customer}):
	# 	custom_term = frappe.get_doc("Customer Payment Terms", {"customer": self.customer})

	payment_term_dict = {}
	due_date_list = []
	item_to_append = []

	remaining_terms = []
	for row in payment_terms_data:
		payment_terms = frappe.db.get_value(
			"Customer Payment Terms Details",
			{"parent": self.customer, "item_type": row},
			["payment_term", "due_date_based_on", "due_days"],
			as_dict=1,
		)
		if payment_terms:
			if not payment_term_dict.get(payment_terms.payment_term):
				payment_term_dict.update(
					{
						payment_terms.payment_term: {
							"item_type": [row],
							"due_days": payment_terms.due_days,
							"due_date_based_on": payment_terms.due_date_based_on,
						}
					}
				)
			else:
				payment_term_dict[payment_terms.payment_term]["item_type"].append(row)

		else:
			remaining_terms.append(row)

	if remaining_terms:
		frappe.throw(
			_("Following Items not mentioned in Customer Payment Terms. <br><b>{0}</b>").format(
				"<br>".join(remaining_terms)
			)
		)
	# if custom_term:
	# 	for row in custom_term.customer_payment_details:
	# 		if not payment_term_dict.get(row.payment_term):
	# 			payment_term_dict.update(
	# 				{
	# 					row.payment_term: {
	# 						"item_type": [row.item_type],
	# 						"due_days": row.due_days,
	# 						"due_date_based_on": row.due_date_based_on,
	# 					}
	# 				}
	# 			)
	# 		else:
	# 			payment_term_dict[row.payment_term]["item_type"].append(row.item_type)

	total_metal_amount = 0
	total_making_amount = 0
	total_finding_amount = 0
	total_diamond_amount = 0
	total_gemstone_amount = 0

	if payment_term_dict:
		for row in self.items:
			if row.bom:
				total_metal_amount += (
					row.metal_amount
					+ row.custom_custom_duty_amount
					+ row.custom_hallmarking_amount
					+ row.custom_freight_amount
					+ row.custom_certification_amount
				)
				total_making_amount += row.making_amount
				total_finding_amount += row.finding_amount
				total_diamond_amount += row.diamond_amount
				total_gemstone_amount += row.gemstone_amount
	self.payment_schedule = []
	if payment_term_dict:
		due_date = None
		self.payment_terms_template = None
		for row in payment_term_dict:
			payment_amount = 0

			description = []
			for item_type in payment_term_dict[row]["item_type"]:
				charge_type = frappe.db.get_value("E Invoice Item", item_type, "charge_type")
				if charge_type in ["Making Charges", "Labour Charges"] and total_making_amount > 0:
					if charge_type == "Making Charges" and not self.is_customer_metal:
						
						# payment_amount += total_making_amount
						payment_amount += payment_terms_data.get(item_type)
						# payment_amount += self.total_taxes_and_charges
						description.append(item_type)
					elif charge_type != "Making Charges" and self.is_customer_metal:
						payment_amount += total_making_amount
						# payment_amount += self.total_taxes_and_charges
						description.append(item_type)
				elif charge_type == "Studded Metal" and total_metal_amount > 0:
					payment_amount += payment_terms_data.get(item_type)
					description.append(item_type)
				elif charge_type in ["Studded Diamond", "Handling Charges"] and total_diamond_amount > 0:
					if charge_type == "Studded Diamond" and not self.is_customer_diamond:
						payment_amount += payment_terms_data.get(item_type)
						# payment_amount += total_diamond_amount
						description.append(item_type)
					elif self.is_customer_diamond and charge_type != "Studded Diamond":
						payment_amount += total_diamond_amount
						description.append(item_type)
				elif charge_type == "Studded Gemstone" and total_gemstone_amount > 0:
					payment_amount += total_gemstone_amount
					description.append(item_type)

			if payment_term_dict[row]["due_date_based_on"] == "Day(s) after invoice date":
				due_date = datetime.strptime(self.posting_date, "%Y-%m-%d") + timedelta(
					days=int(payment_term_dict[row]["due_days"])
				)

			elif payment_term_dict[row]["due_date_based_on"] == "Day(s) after the end of the invoice month":
				posting_date = get_last_day(self.posting_date)
				due_date = datetime.strptime(posting_date, "%Y-%m-%d") + timedelta(
					days=int(payment_term_dict[row]["due_days"])
				)

			due_date_list.append(due_date)
			if payment_amount > 0:
				# if self.disable_rounded_total == 0:
				payment_amount = flt(payment_amount, 3)

				item_to_append.append(
					{
						"due_date": due_date,
						"description": ", ".join(item_type for item_type in description),
						"payment_term": row,
						"payment_amount": payment_amount,
						"custom_invoice_portion": flt((payment_amount / self.grand_total) * 100)
						if self.grand_total > 0
						else 0,
					}
				)
		self.payment_schedule = []
		self.due_date = max(due_date_list)
		self.extend("payment_schedule", item_to_append)

def update_income_account(self):
	if self.is_opening == "No":
		income_account = frappe.db.get_value(
			"Account", {"company": self.company, "custom_sales_type": self.sales_type}, "name"
		)
		if income_account:
			for row in self.items:
				row.income_account = income_account
