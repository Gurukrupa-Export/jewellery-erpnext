import frappe
from frappe import _
from frappe.query_builder.custom import ConstantColumn
from frappe.utils import flt


def update_serial_details(self):
	if self.tag_no:
		sr_doc = frappe.get_doc("Serial No", self.tag_no)

		if not frappe.db.exists("Serial No Table", {"serial_no": self.tag_no, "bom": self.name}):

			sr_doc.append(
				"custom_serial_no_table",
				{
					"serial_no": self.tag_no,
					"item_code": self.get("item"),
					"company": self.get("company"),
					"bom": self.name,
					"purchase_document_no": sr_doc.get("purchase_document_no"),
				},
			)
			sr_doc.save()


def calculate_gst_rate(self):
	gold_gst_rate = frappe.db.get_single_value("Jewellery Settings", "gold_gst_rate")
	divide_by = 100 + int(gold_gst_rate)
	self.gold_rate = self.gold_rate_with_gst * 100 / divide_by


def set_bom_rate(self):
	"""
	Calculates BOM Rate for an Item
	Takes in BOM Document as a parameter
	"""
	fields = {
		"gold_bom_rate": get_gold_rate(self),
		"diamond_bom_rate": get_diamond_rate(self),
		"gemstone_bom_rate": get_gemstone_rate(self),
		"other_bom_rate": get_other_rate(self),
		"making_charge": get_making_charges(self) or 0,
	}
	# remove None values
	fields = {k: v for k, v in fields.items() if v is not None}
	bom_fields = {k.replace("rate", "amount"): v for k, v in fields.items() if v is not None}
	# update the self object
	self.db_set(bom_fields)

	del bom_fields["modified"]
	del bom_fields["modified_by"]
	# Update Quotation in which current BOM is present
	# frappe.db.set_value("Quotation Item", {"quotation_bom": self.name}, fields)

	# calculate and update the total bom amount
	self.total_bom_amount = sum(bom_fields.values())
	# frappe.db.set_value(
	# 	"Quotation Item",
	# 	{"quotation_bom": self.name},
	# 	{"bom_rate": self.total_bom_amount},
	# )

	self.making_fg_purchase = 0
	for row in self.metal_detail + self.finding_detail:
		self.making_fg_purchase += row.fg_purchase_amount if row.fg_purchase_amount else 0

	self.diamond_fg_purchase = 0
	for row in self.diamond_detail:
		self.diamond_fg_purchase += row.fg_purchase_amount if row.fg_purchase_amount else 0

	self.gemstone_fg_purchase = 0
	for row in self.gemstone_detail:
		self.gemstone_fg_purchase += row.fg_purchase_amount if row.fg_purchase_amount else 0

	# commit the changes
	# frappe.db.commit()


def get_gold_rate(self):
	# Get the metal purity from the self object or default to 0

	# Get the gold GST rate from the Jewellery Settings doctype
	gold_gst_rate = frappe.db.get_single_value("Jewellery Settings", "gold_gst_rate")

	# Initialize the amount variable
	amount = 0

	# Set rates in metal_detail and finding_detail child tables
	for item in self.metal_detail + self.finding_detail:
		item.quantity = flt(item.quantity, self.doc_pricision)
		metal_purity = frappe.db.get_value(
			"Metal Criteria", {"parent": self.customer, "metal_touch": item.metal_touch}, "metal_purity"
		)
		company_metal_purity = item.purity_percentage or 0
		if not metal_purity:
			# frappe.throw("Customer Metal Criteria is missing")
			metal_purity = company_metal_purity
		# Check if item is a customer item
		if item.is_customer_item:
			# Set rate and amount to 0 if it's a customer item
			item.rate = 0
			item.amount = 0
		else:
			# Calculate the amount using the gold rate, metal purity and GST rate
			item.rate = flt(self.gold_rate_with_gst) * flt(metal_purity) / (100 + int(gold_gst_rate))
			item.amount = flt(item.quantity) * item.rate

			if company_metal_purity != metal_purity:
				company_rate = (
					flt(self.gold_rate_with_gst) * flt(company_metal_purity) / (100 + int(gold_gst_rate))
				)
				company_amount = flt(item.quantity) * company_rate
				item.difference = item.amount - company_amount
			else:
				item.difference = 0
		# Add the current item's amount to the total amount
		amount += item.amount

	# Return the total amount
	return amount


def get_diamond_rate(self):
	# Get the customer from the self object
	customer = self.customer

	# Get the diamond price list type for the customer
	cust_diamond_price_list_type = frappe.db.get_value("Customer", customer, "diamond_price_list")

	# Initialize the diamond amount variable
	diamond_amount = 0
	# create a dict having sieve size range with avg wt for rate acc to range
	ss_range = {}
	for diamond in self.diamond_detail:
		actual_qty = diamond.quantity
		diamond.quantity = flt(diamond.quantity, self.diamond_pricision)
		diamond.difference = actual_qty - diamond.quantity
		if not diamond.sieve_size_range:
			continue
		det = ss_range.get(diamond.sieve_size_range) or {}
		# det['pcs'] = flt(det.get("pcs")) + diamond.pcs
		det["pcs"] = (flt(det.get("pcs")) + flt(diamond.get("pcs"))) or 1
		det["quantity"] = flt(flt(det.get("quantity")) + diamond.quantity, 3)
		det["std_wt"] = flt(flt(det["quantity"], 2) / det["pcs"], 3)
		ss_range[diamond.sieve_size_range] = det

	# Iterate through the diamond_detail
	for diamond in self.diamond_detail:
		det = ss_range.get(diamond.sieve_size_range) or {}
		amount = _calculate_diamond_amount(self, diamond, cust_diamond_price_list_type, det)
		diamond_amount += amount
	return diamond_amount


def _calculate_diamond_amount(self, diamond, cust_diamond_price_list_type, range_det):
	"""
	Calculates Diamond Rate for a single diamond in BOM Diamond Detail.
	Takes a single row of BOM Diamond Detail as a parameter.
	"""
	# Get the sieve size range and diameter for the current diamond
	attribute_value = frappe.db.get_value(
		"Attribute Value",
		diamond.diamond_sieve_size,
		["sieve_size_range", "diameter"],
		as_dict=1,
	)
	sieve_size_range = attribute_value.get("sieve_size_range")
	size_in_mm = attribute_value.get("diameter")

	# Create filters for retrieving the Diamond Price List
	price_list_type = cust_diamond_price_list_type
	filters = {
		"price_list": self.selling_price_list,
		"diamond_type": diamond.diamond_type,
		"stone_shape": diamond.stone_shape,
		"diamond_quality": diamond.quality,
		"price_list_type": price_list_type,
		"customer": self.customer,
	}
	if price_list_type == "Weight (in cts)":
		filters.update(
			{
				"from_weight": ["<=", range_det.get("std_wt")],
				"to_weight": [">=", range_det.get("std_wt")],
			}
		)
	elif price_list_type == "Sieve Size Range":
		filters["sieve_size_range"] = sieve_size_range
	elif price_list_type == "Size (in mm)":
		if size_in_mm:
			filters["size_in_mm"] = size_in_mm
		if diamond.diamond_size_in_mm:
			filters["diamond_size_in_mm"] = diamond.diamond_size_in_mm
	else:
		frappe.msgprint(_("Price List Type Not Specified"))
		return 0

	# Retrieve the Diamond Price List and calculate the rate
	diamond_price_list = frappe.get_list(
		"Diamond Price List",
		filters=filters,
		fields=[
			"rate",
			"handling_rate",
			"supplier_fg_purchase_rate",
			"custom_outright_handling_charges_in_",
			"custom_outright_handling_charges_rate",
			"custom_outwork_handling_charges_in__",
			"custom_outwork_handling_charges_rate_",
		],
		order_by="effective_from desc",
		limit=1,
	)
	if not diamond_price_list:
		frappe.msgprint(
			f"Diamond Amount for Sieve Size - {diamond.diamond_sieve_size} is 0\n Please Check if Diamond Price Exists For {filters}"
		)
		return 0

	# Get Handling Rate of the Diamond if it is a cutomer provided Diamond
	rate = (
		diamond_price_list[0].get("handling_rate")
		+ (
			diamond_price_list[0].get("handling_rate")
			* (diamond_price_list[0].get("custom_outwork_handling_charges_in__") or 0)
		)
		+ (diamond_price_list[0].get("custom_outwork_handling_charges_rate_") or 0)
		if diamond.is_customer_item
		else diamond_price_list[0].get("rate")
		+ (
			diamond_price_list[0].get("rate")
			* (diamond_price_list[0].get("custom_outright_handling_charges_in_") or 0)
		)
		+ (diamond_price_list[0].get("custom_outright_handling_charges_rate") or 0)
	)

	# Set the rate and total rate for the diamond
	if price_list_type == "Weight (in cts)":
		diamond.std_wt = range_det.get("std_wt")
		range_det[
			"rate"
		] = rate  # just in case if need to calculate amount after round off quantity(weight)
	diamond.total_diamond_rate = rate
	diamond.diamond_rate_for_specified_quantity = int(rate) * diamond.quantity  # amount

	# FG Rate
	diamond.fg_purchase_rate = diamond_price_list[0].get("supplier_fg_purchase_rate")
	diamond.fg_purchase_amount = diamond.fg_purchase_rate * diamond.quantity

	return int(rate) * diamond.quantity


def get_gemstone_rate(self):
	gemstone_amount = 0
	for stone in self.gemstone_detail:
		actual_qty = stone.quantity
		stone.quantity = flt(stone.quantity, self.gemstone_pricision)
		stone.difference = actual_qty - stone.quantity
		# Calculate the weight per piece
		stone.pcs = int(stone.pcs) or 1
		gemstone_weight_per_pcs = stone.quantity / stone.pcs

		# Create filters for retrieving the Gemstone Price List
		filters = {
			"price_list": self.selling_price_list,
			"price_list_type": stone.price_list_type,
			"customer": self.customer,
			"cut_or_cab": stone.cut_or_cab,
			"gemstone_grade": stone.gemstone_grade,
			"gemstone_pr": stone.gemstone_pr,
			"per_pc_or_per_carat": stone.per_pc_or_per_carat,
		}
		if stone.price_list_type == "Weight (in cts)":
			filters.update(
				{
					"gemstone_type": stone.gemstone_type,
					"stone_shape": stone.stone_shape,
					"gemstone_quality": stone.gemstone_quality,
					"from_weight": ["<=", gemstone_weight_per_pcs],
					"to_weight": [">=", gemstone_weight_per_pcs],
				}
			)
		elif stone.price_list_type == "Multiplier" and stone.gemstone_size:
			filters.update(
				{
					"to_stone_size": [">=", stone.gemstone_size],
					"from_stone_size": ["<=", stone.gemstone_size],
				}
			)
		else:
			filters["gemstone_type"] = stone.gemstone_type
			filters["stone_shape"] = stone.stone_shape
			filters["gemstone_quality"] = stone.gemstone_quality
			filters["gemstone_quality"] = stone.gemstone_quality
			filters["gemstone_size"] = stone.gemstone_size

		# Retrieve the Gemstone Price List and calculate the rate
		gemstone_price_list = frappe.get_list(
			"Gemstone Price List",
			filters=filters,
			fields=[
				"name",
				"rate",
				"handling_rate",
				"supplier_fg_purchase_rate",
				"custom_outright_handling_charges_in_",
				"custom_outright_handling_charges_rate",
				"custom_outwork_handling_charges_in__",
				"custom_outwork_handling_charges_rate_",
			],
			order_by="effective_from desc",
			limit=1,
		)

		multiplier = 0
		item_category = frappe.db.get_value("Item", self.item, "item_category")
		if stone.price_list_type == "Multiplier":
			for row in gemstone_price_list:
				multiplier = (
					frappe.db.get_value(
						"Gemstone Multiplier",
						{"parent": row.name, "item_category": item_category, "parentfield": "gemstone_multiplier"},
						frappe.scrub(stone.gemstone_quality),
					)
					or 0
				)
				fg_multiplier = (
					frappe.db.get_value(
						"Gemstone Multiplier",
						{
							"parent": row.name,
							"item_category": item_category,
							"parentfield": "supplier_fg_multiplier",
						},
						frappe.scrub(stone.gemstone_quality),
					)
					or 0
				)

		if not gemstone_price_list:
			frappe.msgprint(
				f"Gemstone Amount for {stone.gemstone_type} is 0\n Please Check if Gemstone Price Exists For {filters}"
			)
			return 0

		# Get Handling Rate of the Diamond if it is a cutomer provided Diamond
		pr = int(stone.gemstone_pr)
		if stone.price_list_type == "Multiplier":
			rate = multiplier * pr
		else:
			rate = (
				gemstone_price_list[0].get("handling_rate")
				+ (
					gemstone_price_list[0].get("handling_rate")
					+ gemstone_price_list[0].get("custom_outwork_handling_charges_in__")
				)
				+ gemstone_price_list[0].get("custom_outwork_handling_charges_rate_")
				if stone.is_customer_item
				else gemstone_price_list[0].get("rate")
				+ (
					gemstone_price_list[0].get("rate")
					+ gemstone_price_list[0].get("custom_outright_handling_charges_in_")
				)
				+ gemstone_price_list[0].get("custom_outright_handling_charges_rate")
			)
		stone.total_gemstone_rate = rate
		stone.gemstone_rate_for_specified_quantity = int(rate) * stone.quantity
		gemstone_amount += int(rate) * stone.quantity

		if stone.price_list_type != "Multiplier":
			stone.fg_purchase_rate = gemstone_price_list[0].get("supplier_fg_purchase_rate")
			stone.fg_purchase_amount = stone.quantity * stone.fg_purchase_rate
		else:
			stone.fg_purchase_rate = pr * fg_multiplier
			stone.fg_purchase_amount = stone.quantity * stone.fg_purchase_rate

	return gemstone_amount


def get_making_charges(self):
	"""
	Calculates Making Charges IN BOM
	Takes BOM document as a parameter
	"""

	# If Customer Provided Metal/Finding, User will update the Making Rate Manually
	for metal in self.metal_detail:
		if metal.is_customer_item:
			metal.making_amount = metal.making_rate * metal.quantity

	for finding in self.finding_detail:
		if finding.is_customer_item and finding.get("making_rate"):
			finding.making_amount = finding.making_rate * finding.quantity

	item_details = frappe.db.get_value(
		"Item", self.item, ["item_subcategory", "setting_type"], as_dict=True
	)
	sub_category, setting_type = item_details.get("item_subcategory"), item_details.get(
		"setting_type"
	)
	return get_metal_and_finding_making_rate(self, sub_category, setting_type)


def get_metal_and_finding_making_rate(self, sub_category, setting_type):
	# Get Making Charge From Making Charge Price Master for mentioned Combinations
	self.set_additional_rate = False
	for row in self.metal_detail + self.finding_detail:

		MCP = frappe.qb.DocType("Making Charge Price")
		MCPIS = frappe.qb.DocType("Making Charge Price Item Subcategory")
		MCPFS = frappe.qb.DocType("Making Charge Price Finding Subcategory")

		if row.parentfield == "metal_detail":
			child_table = MCPIS
		else:
			child_table = MCPFS
		subcat_subcategory = sub_category if not row.get("finding_type") else row.get("finding_type")
		# Build query
		query = (
			frappe.qb.from_(MCP)
			.left_join(child_table)
			.on(child_table.parent == MCP.name)
			.select(
				child_table.rate_per_gm,
				child_table.rate_per_pc,
				child_table.rate_per_gm_threshold,
				child_table.wastage,
				child_table.supplier_fg_purchase_rate,
			)
			.where(
				(MCP.customer == self.customer)
				& (MCP.setting_type == setting_type)
				& (MCP.metal_type == row.metal_type)
			)
		)
		# Subquery to check for existence
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

		# Add dynamic conditions
		if row.parentfield != "metal_detail":
			query = query.where(child_table.metal_touch == row.metal_touch)

		query = query.limit(1)
		making_charge_details = query.run(as_dict=True)

		# AND mcp.metal_purity = '{row.metal_purity}'
		if not making_charge_details and row.parentfield != "metal_detail":
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
					(MCP.customer == self.customer)
					& (MCP.setting_type == setting_type)
					& (MCP.metal_type == row.metal_type)
				)
				.limit(1)
			)
			if subquery:
				query = query.where(MCPIS.subcategory == sub_category)
			else:
				query = query.where((MCPIS.subcategory.isnull()) | (MCPIS.subcategory == ""))

			making_charge_details = query.run(as_dict=True)

			# AND mcp.metal_purity = '{row.metal_purity}'

		_set_total_making_charges(self, row, making_charge_details or [])

	amount = sum(flt(metal.making_amount) for metal in self.metal_detail)
	amount += sum(flt(metal.making_amount) for metal in self.finding_detail)

	return amount


def _set_total_making_charges(self, metal, making_charge_details):
	charges_details = {row.metal_purity: row for row in making_charge_details}
	# Calculate the making charges for each metal and finding
	# making_charges = charges_details.get(metal.metal_purity) or {}
	if len(making_charge_details) > 0:
		making_charges = making_charge_details[0]
		if not metal.is_customer_item:
			# Set the rate per gram
			metal.making_rate = flt(making_charges.get("rate_per_gm"))

			# set additional_net_weigth
			additional_net_weight = 0
			if not self.set_additional_rate and metal.parentfield == "metal_detail":
				if frappe.db.get_value(
					"Customer", self.customer, "compute_making_charges_on"
				) == "Diamond Inclusive" and flt(metal.metal_purity) == flt(self.metal_purity):
					if not self.total_diamond_weight_per_gram:
						self.total_diamond_weight_per_gram = flt(flt(self.total_diamond_weight) / 5, 3)
					metal.additional_net_weight = self.total_diamond_weight_per_gram
					additional_net_weight = metal.additional_net_weight
					self.set_additional_rate = True
			# Calculate the making charges
			if self.metal_and_finding_weight < (making_charges.get("rate_per_gm_threshold") or 0):
				metal_making_charges = making_charges.get("rate_per_pc")
			else:
				metal_making_charges = metal.making_rate * (metal.quantity + additional_net_weight)

			# For E-Invoicing purpose
			if making_charges.get("non_finding_rate"):
				metal.non_finding_rate = 1

			# Set the making amount on the metal or finding
			metal.making_amount = metal_making_charges

			# Set wastage rate
			metal.wastage_rate = flt(making_charges.get("wastage"))

			# Add the wastage percentage to the making charges
			metal.wastage_amount = metal.wastage_rate * metal.amount / 100

			# set FG Purcahse rate and amount

			metal.fg_purchase_rate = flt(making_charges.get("supplier_fg_purchase_rate"))
			metal.fg_purchase_amount = metal.fg_purchase_rate * (metal.quantity + additional_net_weight)


def get_doctype_name(self):
	if "QTN" in self.name:
		return "Quotation"
	return "Sales Order" if "ORD" in self.name else None


def get_other_rate(self):
	self.igi_charges = self.igi_charges or 0
	self.dhc_charges = self.dhc_charges or 0
	self.sgl_charges = self.sgl_charges or 0
	self.hallmark_charges = self.hallmark_charges or 0
	other_details = [
		self.igi_charges,
		self.dhc_charges,
		self.sgl_charges,
		self.hallmark_charges,
	]
	return sum(other_details)


def prettify_filter(filters):
	prettified_filter = [
		f"{str(filter[1]).upper()} {str(filter[2]).upper()} {str(filter[3]).upper()}<br>"
		for filter in filters
	]
	return "".join(prettified_filter)


def set_bom_item_details(self):
	"""
	This method is called on Save of Quotation/Sales Order/ Sales Invoice before save
	This Functions checks if any specific modifications is provided in Quotation Items and updates BOM rate accordingly
	`self` parameter in this function is quotation/sales_order document.
	"""
	doctype = get_doctype_name(self)
	for item in self.items:
		remark = ""
		if item.diamond_quality:
			remark += f"Diamond Quality: {item.diamond_quality} \n"
		if item.metal_colour:
			remark += f"Colour: {item.metal_colour}"
		if item.quotation_bom:
			bom_doc = (
				frappe.get_doc("BOM", item.quotation_bom)
				if doctype == "Quotation"
				else frappe.get_doc("BOM", item.bom)
			)
			# Set Metal Details Fields
			for metal in bom_doc.metal_detail + bom_doc.finding_detail:
				if item.metal_colour:
					metal.metal_colour = item.metal_colour

			# Set Diamond Detail Fields
			for diamond in bom_doc.diamond_detail:
				set_diamond_fields(diamond, item)

			bom_doc.save()
			# Set Gemstone Fields
			# for stone in self.gemstone_detail:
			# 	set_gemstone_fields(stone, item)

		item.remarks = remark


def set_diamond_fields(diamond, item):
	# doctype = get_doctype_name(self)
	# customer = self.party_name if doctype == "Quotation" else self.customer
	# if not item.get("diamond_grade"):
	# 	diamond_grade_1 = frappe.db.get_value(
	# 		"Customer Diamond Grade",
	# 		{"parent": customer, "diamond_quality": item.diamond_quality},
	# 		"diamond_grade_1",
	# 	)
	# 	if diamond_grade_1:
	# 		diamond.diamond_grade = diamond_grade_1
	# else:
	# 	diamond.diamond_grade = item.diamond_grade
	if item.get("diamond_quality"):
		diamond.quality = item.diamond_quality
	return


def set_gemstone_fields(stone, item):
	if item.gemstone_type:
		stone.gemstone_type = item.gemstone_type
	if item.gemstone_quality:
		stone.gemstone_quality = item.gemstone_quality
	if item.gemstone_grade:
		stone.gemstone_grade = item.gemstone_grade
	if item.gemstone_cut_or_cab:
		stone.cut_or_cab = item.gemstone_cut_or_cab


def set_bom_rate_in_quotation(self):
	"""
	Fetch BOM Rates FROM BOM and replace the rate with BOM RATE
	"""
	for row in self.items:
		if row.quotation_bom:
			bom_doc = frappe.get_doc("BOM", row.quotation_bom)
			if self.gold_rate_with_gst > 0 and bom_doc.gold_rate_with_gst > 0:
				row.metal_amount = (
					bom_doc.gold_bom_amount / bom_doc.gold_rate_with_gst
				) * self.gold_rate_with_gst
			else:
				row.metal_amount = bom_doc.gold_bom_amount
			row.making_amount = bom_doc.making_charge
			row.finding_amount = bom_doc.finding_bom_amount
			row.diamond_amount = bom_doc.diamond_bom_amount
			row.gemstone_amount = bom_doc.gemstone_bom_amount
			row.custom_certification_amount = bom_doc.certification_amount
			row.custom_freight_amount = bom_doc.freight_amount
			row.custom_hallmarking_amount = bom_doc.hallmarking_amount
			row.custom_custom_duty_amount = bom_doc.custom_duty_amount

			row.rate = flt(
				row.metal_amount
				+ row.making_amount
				+ row.finding_amount
				+ row.diamond_amount
				+ row.gemstone_amount
				+ row.custom_certification_amount
				+ row.custom_freight_amount
				+ row.custom_hallmarking_amount
				+ row.custom_custom_duty_amount,
				3,
			)
