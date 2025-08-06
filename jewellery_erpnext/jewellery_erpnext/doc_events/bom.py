import frappe
from erpnext.controllers.item_variant import create_variant, get_variant, make_variant_item_code
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.bom.doc_events.utils import (
	product_ratio,
	update_specifications,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.bom_utils import (
	calculate_gst_rate,
	set_bom_item_details,
	set_bom_rate,
	update_serial_details,
)


def before_validate(self, method):
	validate_rating(self)
	validate_weight(self)
	if self.bom_type == "Quotation" or self.docstatus == 1:
		precision_data = frappe.db.get_value(
			"Customer",
			self.customer,
			[
				"custom_consider_2_digit_for_bom",
				"custom_consider_2_digit_for_diamond",
				"custom_consider_2_digit_for_gemstone",
				"custom_gemstone_price_list_type",
			],
			as_dict=1,
		)

		gemstone_price_list_type = None

		if self.customer:
			self.doc_pricision = 2 if precision_data.get("custom_consider_2_digit_for_bom") else 3
			self.diamond_pricision = 2 if precision_data.get("custom_consider_2_digit_for_diamond") else 3
			self.gemstone_pricision = 2 if precision_data.get("custom_consider_2_digit_for_gemstone") else 3
			gemstone_price_list_type = precision_data.get("custom_gemstone_price_list_type")
		else:
			self.doc_pricision = 3
			self.diamond_pricision = 3
			self.gemstone_pricision = 3

		if self.customer and not gemstone_price_list_type:
			frappe.throw(
				_("Gemstone Price list type not mentioned into customer {0}").format(self.customer)
			)

		if gemstone_price_list_type:
			for row in self.gemstone_detail:
				row.price_list_type = gemstone_price_list_type

		system_item_validation(self)
		set_item_variant(self)
		set_bom_items(self)
		update_specifications(self)


def validate(self, method):
	if self.bom_type not in ["Quotation"]:
		return

	calculate_metal_qty(self)
	calculate_diamond_qty(self)
	calculate_total(self)
	set_bom_rate(self)
	product_ratio(self)
	# set_sepecifications(self)
	calculate_rates(self)
	if frappe.db.exists("BOM", self.name):
		update_serial_details(self)

	# for row in self.finding_detail:
	# 	if row.item_variant and frappe.db.exists("Item", row.item_variant):
	# 		row.ignore_work_order = frappe.db.get_value(
	# 			"Item", row.item_variant, "custom_ignore_work_order"
	# 		)


def after_insert(self, method):
	update_serial_details(self)


def on_update(self, method):
	pass


def on_update_after_submit(self, method):
	pricision = 3
	if frappe.db.get_value("Customer", self.customer, "custom_consider_2_digit_for_bom"):
		pricision = 2
		self.db_set("doc_pricision", pricision)

	if frappe.db.get_value("Customer", self.customer, "custom_consider_2_digit_for_diamond"):
		pricision = 2
		self.db_set("diamond_pricision", pricision)

	if frappe.db.get_value("Customer", self.customer, "custom_consider_2_digit_for_gemstone"):
		pricision = 2
		self.db_set("gemstone_pricision", pricision)

	set_bom_rate(self)
	calculate_total(self)


def on_cancel(self, method):
	pass


def on_submit(self, method):
	if self.bom_type == "Template":
		return frappe.throw(_("Template BOM Can't Be Submitted"))


def system_item_validation(self):
	if frappe.db.get_value("Item", self.item, "is_system_item"):
		frappe.throw(_("Cannot create BOM for system item {0}.").format(self.item))


def set_item_variant(self):
	if self.bom_type in ["Template", "Quotation", "Sales Order"]:
		return
	bom_tables = ["metal_detail", "diamond_detail", "gemstone_detail", "finding_detail"]

	attributes = {}

	item_variant_data = frappe._dict()

	item_name_data = frappe._dict()

	for bom_table in bom_tables:
		if not self.get(bom_table):
			continue

		for row in self.get(bom_table):
			if not item_variant_data.get(row.item):
				item_variant_data[row.item] = frappe.db.get_all(
					"Item Variant Attribute", {"parent": row.item}, pluck="attribute", order_by="idx"
				)
			item_attribute_data = item_variant_data.get(row.item)

			if row.item not in attributes:
				attributes[row.item] = item_attribute_data
				# attributes[row.item] = [
				# 	attr.attribute for attr in item_attribure_details
				# ]

			if not item_name_data.get(row.item):
				item_name_data[row.item] = frappe.db.get_value("Item", row.item, "item_name")

			item_name = item_name_data.get(row.item)

			args = {}
			for attr in attributes[row.item]:
				key = attr
				normalized_attr = frappe.scrub(key)
				if attr == "Finding Sub-Category":
					key = "Finding Sub-Category"
					normalized_attr = "finding_type"

				if row.get(normalized_attr):
					args[key] = row.get(normalized_attr)

			if bom_table != "gemstone_detail":
				temp_variant = frappe._dict()

				variant_attributes = []

				for d in item_attribute_data:
					variant_attributes.append(frappe._dict({"attribute": d, "attribute_value": args.get(d)}))

				temp_variant.setdefault("attributes", variant_attributes)

				make_variant_item_code(row.item, item_name, temp_variant)

				if not temp_variant.item_code or not frappe.db.exists("Item", temp_variant.item_code):
					frappe.throw(_("{0} does not exists for {1}").format(temp_variant.item_code, self.name))

				row.item_variant = temp_variant.item_code

			else:
				variant = get_variant(
					row.item, args
				)  # Get the variant for the current item and attribute values
				if variant:
					row.item_variant = variant
				else:
					# Create a new variant
					variant = create_variant(row.item, args)
					variant_item_group = frappe.db.get_value(
						"Variant Item Group", {"parent": self.company, "item_variant": row.item}, "item_group"
					)
					if variant_item_group:
						variant.item_group = variant_item_group
					variant.flags.ignore_permissions = True
					variant.save()
					row.item_variant = variant.name


def set_bom_items(self):
	"""
	Sets BOM Items Based On METAL, DIAMOND, GEMSTONE, FINDING Child Tables.
	If BOM Type is TEMPLATE or QUOTATION set defualt Item from Jewellery Settings to avoid garbage items creation.
	"""
	# Place a dummy Item if Bom type is Template or Quotation
	default_item = frappe.db.get_single_value("Jewellery Settings", "defualt_item")
	uom = frappe.db.get_value("Item", default_item, "stock_uom")
	if self.bom_type in ["Template", "Quotation"]:
		self.items = []
		self.append(
			"items",
			{
				"item_code": default_item,
				"qty": 1,
				"uom": uom,
				"rate": 0,
			},
		)
	else:
		# Set Item Based On Child Tables
		_set_bom_items_by_child_tables(self, default_item)


def _set_bom_items_by_child_tables(self, default_item):
	bom_items = {}
	bom_items.update({row.item_variant: row.quantity for row in self.metal_detail if row.quantity})
	bom_items.update({row.item_variant: row.quantity for row in self.diamond_detail if row.quantity})
	bom_items.update({row.item_variant: row.quantity for row in self.gemstone_detail if row.quantity})
	bom_items.update({row.item_variant: row.quantity for row in self.finding_detail if row.quantity})

	if bom_items:
		# defualt_item = frappe.db.get_single_value("Jewellery Settings", "defualt_item")
		items = frappe.get_all("Jewellery System Item", {"parent": "Jewellery Settings"}, "item_code")
		item_list = [row.get("item_code") for row in items]
		to_remove = [
			d
			for d in self.items
			if (self.bom_type not in ["Template", "Quotation"] and d.item_code == default_item)
			or frappe.db.get_value("Item", d.item_code, "variant_of") in item_list
		]
		for d in to_remove:
			self.remove(d)

		uom_dict = frappe._dict()
		for item_code, qty in bom_items.items():
			if not uom_dict.get(item_code):
				uom_dict[item_code] = frappe.db.get_value("Item", item_code, "stock_uom")
			self.append(
				"items",
				{
					"item_code": item_code,
					"qty": qty,
					"uom": uom_dict.get(item_code),
					"rate": 0,
				},
			)

	if len(self.other_detail) > 0:
		for row in self.other_detail:
			self.append(
				"items",
				{
					"item_code": row.item_code,
					"qty": row.quantity,
					"uom": frappe.db.get_value("Item", item_code, "stock_uom"),
					"rate": 0,
				},
			)


def update_item_price(self):
	if frappe.db.exists(
		"Item Price", {"item_code": self.item, "price_list": self.buying_price_list, "bom_no": self.name}
	):
		name = frappe.db.get_value(
			"Item Price",
			{"item_code": self.item, "price_list": self.buying_price_list, "bom_no": self.name},
			"name",
		)
		item_doc = frappe.get_doc("Item Price", name)
		item_doc.db_set("price_list_rate", self.total_cost)
		item_doc.db_update()
	else:
		_create_new_price_list(self)


def _create_new_price_list(self):
	item_price = frappe.new_doc("Item Price")
	item_price.price_list = self.buying_price_list
	item_price.item_code = self.item
	item_price.bom_no = self.name
	item_price.price_list_rate = self.total_cost
	item_price.save()

def validate_rating(self):
    # Exit quietly if ratio not provided
    if not self.gold_to_diamond_ratio:
        return

    try:
        ratio_value = float(self.gold_to_diamond_ratio)
    except (ValueError, TypeError):
        # If it's not a valid float, skip logic
        return

    gc_ratio_master = frappe.get_single("GC Ratio Master")
    for row in gc_ratio_master.gc_ratio:
        range_text = row.metal_to_gold_ratio_group.strip()
        try:
            if '-' in range_text:
                lower, upper = [float(x.strip()) for x in range_text.split('-')]
                if lower <= ratio_value <= upper:
                    self.custom_rating = row.rating
                    break

            elif 'Above' in range_text:
                threshold = float(range_text.replace('Above', '').strip())
                if ratio_value > threshold:
                    self.custom_rating = row.rating
                    break

            elif 'Below' in range_text:
                threshold = float(range_text.replace('Below', '').strip())
                if ratio_value < threshold:
                    self.custom_rating = row.rating
                    break

        except ValueError:
            frappe.msgprint(f"Skipping invalid range value: {range_text}")

def validate_weight(self):
	total_metal_quantity = 0.0
	total_diamond_weight = 0.0
	total_finding_weight = 0.0
	total_gemstone_weight = 0.0
	# Sum of metal_detail quantities
	for row in self.metal_detail:
		if row.quantity:
			total_metal_quantity += row.quantity

	# Sum of diamond_detail (quantity * pcs)
	for row in self.diamond_detail:
		if row.quantity and row.pcs:
			total_diamond_weight += row.quantity

	for row in self.finding_detail:
		if row.quantity and row.qty:
			total_finding_weight += row.qty * row.quantity


	for row in self.gemstone_detail:
		if row.quantity and row.pcs:
			qty = float(row.quantity)
			pcs = float(row.pcs)
			total_gemstone_weight += pcs * qty

	self.metal_weight = total_metal_quantity
	self.diamond_weight = total_diamond_weight
	self.finding_weight_ = total_finding_weight
	self.gemstone_weight  = total_gemstone_weight
	self.total_diamond_weight_in_gms  = self.diamond_weight / 5
	self.total_gemstone_weight_in_gms = self.gemstone_weight / 5
	# frappe.throw(f"{self.finding_weight_}")

def calculate_metal_qty(self):
	if self.metal_detail:
		color = []
		for row in self.metal_detail:
			if row.metal_colour and row.metal_colour not in color:
				color.append(row.metal_colour)

			if row.cad_weight and row.cad_to_finish_ratio:
				row.quantity = flt(row.cad_weight * row.cad_to_finish_ratio / 100)
		if color:
			AV = frappe.qb.DocType("Attribute Value")
			conditions = []
			for value in color:
				condition = AV.name.like(f"%{value}%")
				conditions.append(condition)
			query = frappe.qb.from_(AV).select(AV.name).where((AV.is_metal_colour == 1))
			for cond in conditions:
				query = query.where(cond)

			metal_colours = query.run()

			metal_colour = [i[0] for i in metal_colours if len(i[0].split("+")) == len(color)]
			if metal_colour:
				self.metal_colour = metal_colour[0]


def calculate_diamond_qty(self):
	for row in self.diamond_detail + self.gemstone_detail:
		row.weight_in_gms = flt(flt(row.quantity) / 5, 3)


def calculate_total(self):
	"""Calculate the total weight of metal, diamond, gemstone, and finding.
	Also calculate the gold to diamond ratio, and the diamond ratio.
	"""
	self.total_metal_weight = sum(row.quantity for row in self.metal_detail)
	self.metal_weight = self.total_metal_weight
	self.diamond_weight = sum(row.quantity for row in self.diamond_detail)
	self.total_diamond_weight_in_gms = sum(row.weight_in_gms for row in self.diamond_detail)
	self.total_gemstone_weight = sum(row.quantity for row in self.gemstone_detail)
	self.gemstone_weight = self.total_gemstone_weight
	self.total_gemstone_weight_in_gms = sum(row.weight_in_gms for row in self.gemstone_detail)
	self.finding_weight = sum(row.quantity for row in self.finding_detail)
	self.finding_weight_ = self.finding_weight
	self.total_diamond_pcs = sum(flt(row.pcs) for row in self.diamond_detail)
	self.total_gemstone_pcs = sum(flt(row.pcs) for row in self.gemstone_detail)
	self.total_other_weight = sum(row.quantity for row in self.other_detail)
	self.other_weight = self.total_other_weight
	# self.total_diamond_amount  = sum(row.diamond_rate_for_specified_quantity for row in self.diamond_detail)
	self.total_diamond_amount = sum(row.diamond_rate_for_specified_quantity or 0.0 for row in self.diamond_detail)
	# self.diamond_bom_amount = sum(row.diamond_rate_for_specified_quantity for row in self.diamond_detail)
	self.diamond_bom_amount = sum(row.diamond_rate_for_specified_quantity or 0.0 for row in self.diamond_detail)

	self.metal_and_finding_weight = flt(self.metal_weight) + flt(self.finding_weight)
	self.gold_to_diamond_ratio = (
		flt(self.metal_and_finding_weight) / flt(self.diamond_weight) if self.diamond_weight else 0
	)
	self.diamond_ratio = (
		flt(self.diamond_weight) / flt(self.total_diamond_pcs) if self.total_diamond_pcs else 0
	)
	self.gross_weight = (
		flt(self.metal_and_finding_weight)
		+ flt(self.total_diamond_weight_in_gms)
		+ flt(self.total_gemstone_weight_in_gms)
		+ flt(self.total_other_weight)
	)

	# Jay Added
	self.custom_total_pure_weight = sum(
		row.quantity * (flt(row.metal_purity) / 100) for row in self.metal_detail
	)
	self.custom_total_pure_finding_weight = sum(
		row.quantity * (flt(row.metal_purity) / 100) for row in self.finding_detail
	)
	self.custom_net_pure_weight = (
		self.custom_total_pure_weight + self.custom_total_pure_finding_weight
	)
	# -----


def set_sepecifications(self):
	"""
	Sets Defualt Specifications(For Template BOM) and Modified Specifications FOR BOM
	"""
	fields_list = [
		"item_category",
		"item_subcategory",
		"product_size",
		"metal_target",
		"diamond_target",
		"metal_colour",
		"enamal",
		"rhodium",
		"gemstone_type",
		"gemstone_quality",
		"back_belt_patti",
		"black_beed",
		"black_beed_line",
		"two_in_one",
		"chain",
		"chain_type",
		"chain_length",
		"customer_chain",
		"chain_weight",
		"detachable",
		"total_length",
		"back_chain",
		"back_chain_size",
		"back_side_size",
		"chain_size",
		"kadi_to_mugappu",
		"space_between_mugappu",
		"breadth",
		"width",
		"back_belt_length",
	]

	# Set Defualt Specification For Template BOM
	if self.bom_type == "Template":
		bom = frappe.db.get_list("BOM", {"name": self.name}, "*")
		if bom:
			specifications = "".join(
				f"{key} - {val} \n" for key, val in bom[0].items() if key in fields_list and val != None
			)
			self.defualt_specifications = specifications
	else:
		set_specifications_for_modified_bom(self, fields_list)


def set_specifications_for_modified_bom(self, fields_list):
	"""
	Set Modified Specifications Based On Values Changed From Defualt BOM.
	Defualt Specifications and Modified Specifications are `TEXT` fields.
	"""
	temp_bom = frappe.db.get_list("BOM", {"item": self.item, "bom_type": "Template"}, "*")
	temp_bom_dict = {}
	if temp_bom:
		for key, val in temp_bom[0].items():
			if key in fields_list:
				temp_bom_dict[key] = val
	modified_specifications = ""
	if self.is_new():
		self.defualt_specifications = "".join(f"{key} - {val} \n" for key, val in temp_bom_dict.items())
		return

	new_fields = [
		{"item_category": self.item_category},
		{"item_subcategory": self.item_subcategory},
		{"product_size": self.product_size},
		{"metal_target": self.metal_target},
		{"diamond_target": self.diamond_target},
		{"metal_colour": self.metal_colour},
		{"enamal": self.enamal},
		{"rhodium": self.rhodium},
		{"gemstone_type": self.gemstone_type},
		{"gemstone_quality": self.gemstone_quality},
		{"back_belt_patti": self.back_belt_patti},
		{"black_beed": self.black_beed},
		{"black_beed_line": self.black_beed_line},
		{"chain": self.chain},
		{"chain_type": self.chain_type},
		{"chain_length": self.chain_length},
		{"customer_chain": self.customer_chain},
		{"chain_weight": self.chain_weight},
		{"detachable": self.detachable},
		{"total_length": self.total_length},
		{"back_chain": self.back_chain},
		{"back_chain_size": self.back_chain_size},
		{"back_side_size": self.back_side_size},
		{"chain_size": self.chain_size},
		{"kadi_to_mugappu": self.kadi_to_mugappu},
		{"space_between_mugappu": self.space_between_mugappu},
		{"width": self.width},
		{"back_belt_length": self.back_belt_length},
	]
	new_dict = {}
	for i in new_fields:
		for key, val in i.items():
			new_dict[key] = val

	for key, val in new_dict.items():
		if temp_bom_dict.get(key):
			temp_bom_val = temp_bom_dict[key]
			if val != temp_bom_val and val:
				modified_specifications += f"{key} - {val} \n"
	self.defualt_specifications = "".join(
		f"{key} - {val} \n" for key, val in temp_bom_dict.items() if val != None
	)
	self.modified_specifications = modified_specifications


@frappe.whitelist()
def check_diamond_sieve_size_tolerance_value_exist(filters):
	result = frappe.get_all(
		"Attribute Value Diamond Sieve Size",
		fields=["diamond_quality", "diamond_shape", "from_weight", "to_weight", "for_universal_value"],
		filters=filters,
	)
	return result


@frappe.whitelist()
def get_weight_in_cts_from_attribute_value(filters):
	result = frappe.get_all("Attribute Value", fields=["weight_in_cts"], filters=filters)
	return result


@frappe.whitelist()
def get_quality_diamond_sieve_size_tolerance_value(filters):
	result = frappe.get_all(
		"Attribute Value Diamond Sieve Size",
		fields=["diamond_quality", "diamond_shape", "from_weight", "to_weight", "for_universal_value"],
		filters=filters,
	)
	return result


@frappe.whitelist()
def get_records_universal_attribute_value(filters):
	result = frappe.get_all(
		"Attribute Value Diamond Sieve Size",
		fields=["diamond_quality", "diamond_shape", "from_weight", "to_weight"],
		filters=filters,
	)
	return result


@frappe.whitelist()
def get_bom_details(serial_no, customer):
	try:
		return frappe.get_doc("BOM", {"tag_no": serial_no, "is_active": 1})
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error(e)


def calculate_rates(self):
	gross_weight = self.gross_weight if self.get("gross_weight") else 0
	gemstone_weight = self.gemstone_weight if self.get("gemstone_weight") else 0
	finding_weight = self.finding_weight if self.get("finding_weight") else 0
	other_weight = self.other_weight if self.get("other_weight") else 0
	diamond_weight = self.diamond_weight if self.get("diamond_weight") else 0
	self.diamond_inclusive = flt(gross_weight - gemstone_weight - finding_weight - other_weight, 3)
	self.net_wt = flt(
		gross_weight - (diamond_weight / 5) - gemstone_weight - finding_weight - other_weight, 3
	)


def update_totals(parent_doctype, parent_doctype_name):
	self = frappe.get_doc(parent_doctype, parent_doctype_name)
	self.doc_pricision = (
		2 if frappe.db.get_value("Customer", self.customer, "custom_consider_2_digit_for_bom") else 3
	)
	gold_gst_rate = frappe.db.get_single_value("Jewellery Settings", "gold_gst_rate")

	self.db_set("gold_bom_amount", 0)
	self.db_set("making_charge", 0)
	self.db_set("total_making_amount", 0)
	self.db_set("making_fg_purchase", 0)
	for row in self.metal_detail + self.finding_detail:
		metal_purity = frappe.db.get_value(
			"Metal Criteria", {"parent": self.customer, "metal_touch": row.metal_touch}, "metal_purity"
		)
		company_metal_purity = row.purity_percentage or 0
		if not metal_purity:
			metal_purity = company_metal_purity

		row.db_set("amount", (flt(row.quantity) * row.rate))
		row.db_set("fg_purchase_amount", (flt(row.quantity) * row.fg_purchase_rate))
		row.db_set("making_amount", (row.making_rate * row.quantity))
		row.db_set("wastage_amount", (row.wastage_rate * row.amount / 100))

		self.db_set("making_charge", self.making_charge + row.making_amount)
		self.db_set("total_making_amount", (self.total_making_amount + row.making_amount))
		self.db_set("gold_bom_amount", (self.gold_bom_amount + row.amount))
		self.db_set("making_fg_purchase", (self.making_fg_purchase + row.fg_purchase_amount))

		if flt(company_metal_purity, 3) != flt(metal_purity, 3):
			# company_rate = (
			# 	flt(self.gold_rate_with_gst) * flt(company_metal_purity) / (100 + int(gold_gst_rate))
			# )
			company_rate = flt(row.rate) * flt(company_metal_purity) / (100 + int(gold_gst_rate))
			company_amount = flt(row.quantity) * company_rate
			row.db_set("difference", (row.amount - company_amount))
		else:
			row.db_set("difference", 0)

	self.db_set("diamond_bom_amount", 0)
	for row in self.diamond_detail:
		row.db_set("diamond_rate_for_specified_quantity", (row.total_diamond_rate * row.quantity))
		self.db_set(
			"diamond_bom_amount", (self.diamond_bom_amount + row.diamond_rate_for_specified_quantity)
		)

	self.db_set("gemstone_bom_amount", 0)
	for row in self.gemstone_detail:
		row.db_set("gemstone_rate_for_specified_quantity", (row.total_gemstone_rate * row.quantity))
		self.db_set(
			"gemstone_bom_amount", (self.gemstone_bom_amount + row.gemstone_rate_for_specified_quantity)
		)
