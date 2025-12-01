import json

import frappe
from frappe import _

from jewellery_erpnext.jewellery_erpnext.doc_events.bom_utils import (
	calculate_gst_rate,
	set_bom_item_details,
	set_bom_rate_in_quotation,
)
from jewellery_erpnext.jewellery_erpnext.customization.quotation.doc_events.utils import (
	update_si,
)

@frappe.whitelist()
def update_status(quotation_id):
	status = frappe.db.get_value("Quotation", quotation_id, "status")
	if status != "Closed":
		frappe.db.set_value("Quotation", quotation_id, "status", "Closed")
	else:
		frappe.db.set_value("Quotation", quotation_id, "status", "Open")


def validate(self, method):
	validate_gold_rate_with_gst(self)
	self.calculate_taxes_and_totals()
	# create_new_bom(self)
	if self.workflow_state == "Creating BOM":
		frappe.enqueue(create_new_bom, self=self, queue="long", timeout=10000)
	if self.docstatus == 0:
		calculate_gst_rate(self)
		if not self.get("__islocal"):
			set_bom_item_details(self)
			update_si(self)
		set_bom_rate_in_quotation(self)


@frappe.whitelist()
def generate_bom(name):
	self = frappe.get_doc("Quotation", name)
	self.flags.can_be_saved = True
	frappe.enqueue(
		create_new_bom, self=self, queue="long", timeout=1000, event="creating BOM for Quotation"
	)


def onload(self, method):
	return


def on_submit(self, method):
	submit_bom(self)


def on_cancel(self, method):
	cancel_bom(self)

def before_submit(self, method):
	validate_invoice_item(self)
	
def create_new_bom(self):
	"""
	Create Quotation Type BOM from Template/ Finished Goods Bom
	"""
	metal_criteria = (
		frappe.get_list(
			"Metal Criteria",
			{"parent": self.party_name},
			["metal_touch", "metal_purity"],
			ignore_permissions=1,
		)
		or {}
	)
	metal_criteria = {row.metal_touch: row.metal_purity for row in metal_criteria}
	error_logs = []
	self.custom_bom_creation_logs = None
	attribute_data = frappe._dict()
	item_bom_data = frappe._dict()
	bom_data = frappe._dict()
	for row in self.items:
		if item_bom_data.get(row.item_code):
			row.db_set("quotation_bom", item_bom_data.get(row.item_code))
		if bom_data.get(row.item_code):
			row.db_set("copy_bom", bom_data.get(row.item_code))
		if row.quotation_bom:
			continue
		bom = frappe.qb.DocType("BOM")
		query = (
			frappe.qb.from_(bom)
			.select(bom.name)
			.where(
				(bom.item == row.get("item_code"))
				& (
					(bom.tag_no == row.get("serial_no"))
					| ((bom.bom_type == "Finished Goods") & (bom.is_active == 1) & (bom.docstatus == 1))
					| ((bom.bom_type == "Template") & (bom.is_active == 1))
				)
			)
			.orderby(
				frappe.qb.terms.Case()
				.when(bom.tag_no == row.get("serial_no"), 1)
				.when(bom.bom_type == "Finished Goods", 2)
				.when(bom.bom_type == "Template", 3)
				.else_(0),
			)
			.orderby(bom.creation)
			.limit(1)
		)
		bom = query.run(as_dict=True)
		if row.order_form_type == "Order":
			mod_reason = frappe.db.get_value("Order", row.order_form_id, "mod_reason")

			if "F-G" in row.item_code or mod_reason == "Change in Metal Touch":
				bom = [{"name": frappe.db.get_value("Order", row.order_form_id, "new_bom")}]
		# query = """
		# 	SELECT name
		# 	FROM BOM
		# 	WHERE item = %(item_code)s
		# 	AND (
		# 		(tag_no = %(serial_no)s AND %(serial_no)s IS NOT NULL) OR
		# 		(bom_type = 'Finished Goods' AND is_active = 1 AND docstatus = 1) OR
		# 		(bom_type = 'Template' AND is_active = 1)
		# 	)
		# 	ORDER BY
		# 		CASE
		# 			WHEN tag_no = %(serial_no)s THEN 1
		# 			WHEN bom_type = 'Finished Goods' THEN 2
		# 			WHEN bom_type = 'Template' THEN 3
		# 		END,
		# 		creation ASC
		# 	LIMIT 1
		# """

		# params = {
		# 	"item_code": row.item_code,
		# 	"serial_no": row.get("serial_no")
		# }

		# bom = frappe.db.sql(query, params, as_dict=True)

		# serial_bom = None
		# Can use single query
		# if serial := row.get("serial_no"):
		# 	serial_bom = frappe.db.get_value("BOM", {"item": row.item_code, "tag_no": serial}, "name")
		# if serial_bom:
		# 	bom = serial_bom
		# # if Finished Goods BOM for an item is already present for item Copy from FINISHED GOODS BOM
		# elif fg_bom := frappe.db.get_value(
		# 	"BOM",
		# 	{"item": row.item_code, "is_active": 1, "docstatus": 1, "bom_type": "Finished Goods"},
		# 	"name",
		# 	order_by="creation asc",
		# ):
		# 	bom = fg_bom
		# if row.order_form_type == 'Order':
		# 		mod_reason = frappe.db.get_value("Order",row.order_form_id,"mod_reason")
		# 		if "F-G" in row.item_code or mod_reason == 'Change in Metal Touch':
		# 			bom = frappe.db.get_value("Order",row.order_form_id,"new_bom")
		# # if Finished Goods BOM for an item not present for item Copy from TEMPLATE BOM
		# elif temp_bom := frappe.db.get_value(
		# 	"BOM",
		# 	{"item": row.item_code, "is_active": 1, "bom_type": "Template"},
		# 	"name",
		# 	order_by="creation asc",
		# ):
		# 	bom = temp_bom
		# if row.order_form_type == 'Order':
		# 	mod_reason = frappe.db.get_value("Order",row.order_form_id,"mod_reason")
		# 	if "F-G" in row.item_code or mod_reason == 'Change in Metal Touch':
		# 		bom = frappe.db.get_value("Order",row.order_form_id,"new_bom")
		# else:
		# 	bom = None
		if bom:
			try:
				create_quotation_bom(
					self, row, bom[0].get("name"), attribute_data, metal_criteria, item_bom_data, bom_data
				)
			except Exception as e:
				frappe.log_error(title="Quotation Error", message=f"{e}")
				error_logs.append(f"Row {row.idx} : {e}")

	if error_logs:
		import html2text

		error_str = "<br>".join(error_logs)
		error_str = html2text.html2text(error_str)
		# self.custom_bom_creation_logs = error_str
		frappe.db.set_value(self.doctype, self.name, "custom_bom_creation_logs", error_str)
	else:
		if self.flags.can_be_saved:
			self.save()
		else:
			self.calculate_taxes_and_totals()
			self.db_update()
		frappe.db.set_value(self.doctype, self.name, "workflow_state", "BOM Created")
		frappe.db.set_value(self.doctype, self.name, "custom_bom_creation_logs", None)


def create_quotation_bom(self, row, bom, attribute_data, metal_criteria, item_bom_data, bom_data):
	copy_bom = bom 

	#  If Order Form ID exists, try using its new_bom as copy_bom
	if row.order_form_id:
		order_form_bom = frappe.db.get_value("Order", row.order_form_id, "new_bom")
		if order_form_bom:
			copy_bom = order_form_bom

	row.db_set("copy_bom", copy_bom)
	doc = frappe.copy_doc(frappe.get_doc("BOM", copy_bom))
	doc.custom_creation_doctype = self.doctype
	doc.custom_creation_docname = self.name

	doc.is_default = 0
	doc.is_active = 0
	doc.bom_type = "Quotation"
	doc.gold_rate_with_gst = self.gold_rate_with_gst
	doc.customer = self.party_name
	doc.selling_price_list = self.selling_price_list
	doc.hallmarking_amount = row.custom_hallmarking_amount

	ref_customer = frappe.db.get_value("Quotation", self.name, "ref_customer")
	diamond_price_list_ref_customer = frappe.db.get_value("Customer", ref_customer, "diamond_price_list")
	gemstone_price_list_ref_customer = frappe.db.get_value("Customer", ref_customer, "custom_gemstone_price_list_type")
	diamond_price_list_customer = frappe.db.get_value("Customer", doc.customer, "diamond_price_list")
	gemstone_price_list_customer = frappe.db.get_value("Customer", doc.customer, "custom_gemstone_price_list_type")

	diamond_price_list = frappe.get_all(
		"Diamond Price List",
		filters={"customer": doc.customer, "price_list_type": diamond_price_list_customer},
		fields=["name", "price_list_type"],
	)
	gemstone_price_list = frappe.get_all(
		"Gemstone Price List",
		filters={"customer": doc.customer, "price_list_type": gemstone_price_list_customer},
		fields=["name", "price_list_type"],
	)

	if not attribute_data:
		attribute_data = frappe.db.get_all(
			"Attribute Value", {"custom_consider_as_gold_item": 1}, pluck="name"
		)

	if self.company == "KG GK Jewellers Private Limited":
		doc.company = self.company
		for diamond in doc.diamond_detail:
			diamond.rate = doc.gold_rate_with_gst
			if self.custom_customer_diamond == "Yes":
				diamond.is_customer_item = 1
			if diamond_price_list and any(dpl["price_list_type"] == diamond_price_list_ref_customer for dpl in diamond_price_list):
				if diamond_price_list_ref_customer == "Size (in mm)":
					size_entries = frappe.db.sql("""
						SELECT name, supplier_fg_purchase_rate, rate,outright_handling_charges_rate,outright_handling_charges_in_percentage,
						outwork_handling_charges_rate,outwork_handling_charges_in_percentage
						FROM `tabDiamond Price List` 
						WHERE customer = %s 
						AND price_list_type = %s 
						AND size_in_mm = %s
						ORDER BY creation DESC
						LIMIT 1
					""", (doc.customer, diamond_price_list_customer, diamond.size_in_mm), as_dict=True)

					if size_entries:
						entry = size_entries[0]
						
						# diamond.total_diamond_rate = entry.get("rate", 0)
						diamond.fg_purchase_rate = entry.get("supplier_fg_purchase_rate", 0)
						diamond.fg_purchase_amount = diamond.fg_purchase_rate * diamond.quantity
						if diamond.is_customer_item:
							diamond.total_diamond_rate = entry.get("outwork_handling_charges_rate", 0)
							diamond.diamond_rate_for_specified_quantity = diamond.total_diamond_rate * diamond.size_in_mm
							if entry.get("outwork_handling_charges_rate") == 0:
								percentage = entry.get("outwork_handling_charges_in_percentage", 0)
								amount = entry.get("rate", 0) * (percentage / 100)
								diamond.total_diamond_rate = amount 
								diamond.diamond_rate_for_specified_quantity = diamond.total_diamond_rate * diamond.size_in_mm
						else:
							diamond.total_diamond_rate = entry.get("rate", 0) + entry.get("outright_handling_charges_rate", 0)
							diamond.diamond_rate_for_specified_quantity =  diamond.total_diamond_rate * diamond.size_in_mm
							if entry.get("outright_handling_charges_rate") == 0:
								percentage = entry.get("outright_handling_charges_in_percentage", 0)
								rate = entry.get("rate", 0) * (percentage / 100)
								diamond.total_diamond_rate = rate + entry.get("rate", 0)
						

				if diamond_price_list_ref_customer == "Sieve Size Range":
					sieve_entries = frappe.db.sql("""
						SELECT name, supplier_fg_purchase_rate, rate,outright_handling_charges_rate,outright_handling_charges_in_percentage,
						outwork_handling_charges_rate,outwork_handling_charges_in_percentage 
						FROM `tabDiamond Price List` 
						WHERE customer = %s 
						AND price_list_type = %s 
						AND sieve_size_range = %s
						ORDER BY creation DESC
						LIMIT 1
					""", (ref_customer, diamond_price_list_ref_customer, diamond.sieve_size_range), as_dict=True)

					if sieve_entries:
						entry = sieve_entries[0]
						diamond.total_diamond_rate = entry.get("rate", 0)
						diamond.fg_purchase_rate = entry.get("supplier_fg_purchase_rate", 0)
						diamond.fg_purchase_amount = diamond.fg_purchase_rate * diamond.quantity

				if diamond_price_list_ref_customer == "Weight (in cts)":
					weight_entries = frappe.db.sql("""
						SELECT name, from_weight, to_weight, supplier_fg_purchase_rate, rate,outright_handling_charges_rate,outright_handling_charges_in_percentage,
						outwork_handling_charges_rate,outwork_handling_charges_in_percentage 
						FROM `tabDiamond Price List` 
						WHERE customer = %s 
						AND price_list_type = %s 
						AND %s BETWEEN from_weight AND to_weight
						ORDER BY creation DESC
						LIMIT 1
					""", (ref_customer, diamond_price_list_ref_customer, diamond.weight_per_pcs), as_dict=True)

					if weight_entries:
						entry = weight_entries[0]
						
						# diamond.total_diamond_rate = entry.get("rate", 0)
						diamond.fg_purchase_rate = entry.get("supplier_fg_purchase_rate", 0)
						diamond.fg_purchase_amount = diamond.fg_purchase_rate * diamond.quantity
						if diamond.is_customer_item:
							diamond.total_diamond_rate = entry.get("outwork_handling_charges_rate", 0)
							
							diamond.diamond_rate_for_specified_quantity =  diamond.total_diamond_rate * diamond.weight_per_pcs
							
							if entry.get("outwork_handling_charges_rate") == 0:
								percentage = entry.get("outwork_handling_charges_in_percentage", 0)
								amount = entry.get("rate", 0) * (percentage / 100)
								diamond.total_diamond_rate = amount 
								diamond.diamond_rate_for_specified_quantity =  diamond.total_diamond_rate * diamond.weight_per_pcs
								
						else:
							diamond.total_diamond_rate = entry.get("rate", 0) + entry.get("outright_handling_charges_rate", 0)
							diamond.diamond_rate_for_specified_quantity =  diamond.total_diamond_rate * diamond.weight_per_pcs
							if entry.get("outright_handling_charges_rate") == 0:
								percentage = entry.get("outright_handling_charges_in_percentage", 0)
								rate = entry.get("rate", 0) * (percentage / 100)
								diamond.total_diamond_rate = rate + entry.get("rate", 0)
								diamond.diamond_rate_for_specified_quantity =  diamond.total_diamond_rate * diamond.weight_per_pcs

		for find in doc.finding_detail:
			rate_per_gm = 0
			fg_purchase_rate = 0
			fg_purchase_amount = 0  
			wastage_rate = 0 
			if self.custom_customer_gold == "Yes":
				find.is_customer_item = 1
			making_charge_price_list = frappe.get_all(
				"Making Charge Price",
				filters={"customer": ref_customer, "setting_type": doc.setting_type},
				fields=["name"]
			)

			making_charge_price_list_with_gold_rate = frappe.get_all(
				"Making Charge Price",
				filters={
					"customer": ref_customer,
					"setting_type": doc.setting_type,
					"from_gold_rate": ["<=", doc.gold_rate_with_gst], 
					"to_gold_rate": [">=", doc.gold_rate_with_gst]
				},
				fields=["name"]
			)

			matching_subcategory = None  
			if making_charge_price_list:
				price_list_name = making_charge_price_list[0]["name"]
				subcategories = frappe.get_all(
					"Making Charge Price Item Subcategory",
					filters={"parent": price_list_name},
					fields=["subcategory", "rate_per_gm", "supplier_fg_purchase_rate", "wastage","custom_subcontracting_rate","custom_subcontracting_wastage"]
				)
				if subcategories:
					matching_subcategory = next(
						(row for row in subcategories if row.get("subcategory") == doc.item_subcategory),
						None
					)
				if matching_subcategory:
					rate_per_gm = matching_subcategory.get("rate_per_gm", 0)
					fg_purchase_rate = matching_subcategory.get("supplier_fg_purchase_rate", 0)
					fg_purchase_amount = fg_purchase_rate * find.quantity
					if find.is_customer_item:
						find.rate = matching_subcategory.get("custom_subcontracting_rate", 0)
						wastage_rate = matching_subcategory.get("custom_subcontracting_wastage", 0)
						fg_purchase_rate = 0
						fg_purchase_amount = 0
						rate_per_gm = 0
					else:
						find.rate = doc.gold_rate_with_gst
						wastage_rate = matching_subcategory.get("wastage", 0)/100



					# wastage_rate = matching_subcategory.get("wastage", 0) / 100.0
			find.wastage_rate = wastage_rate
			# find.rate = doc.gold_rate_with_gst
			find.amount = find.rate * find.quantity
			find.making_rate = rate_per_gm
			if making_charge_price_list_with_gold_rate:
				find.making_amount = find.making_rate * find.quantity

			find.fg_purchase_rate = fg_purchase_rate
			find.fg_purchase_amount = fg_purchase_amount
			find.wastage_amount = find.wastage_rate * find.amount

		for gem in doc.gemstone_detail:
			gem.rate = doc.gold_rate_with_gst
			if gemstone_price_list and any(dpl["price_list_type"] == gemstone_price_list_ref_customer for dpl in gemstone_price_list):
				if gemstone_price_list_ref_customer == "Diamond Range":
					query = frappe.db.sql("""
						SELECT gpl.name, gpl.cut_or_cab, gpl.gemstone_grade,
							gm.item_category, gm.precious, gm.semi_precious, gm.synthetic,
							sfm.precious AS supplier_precious, sfm.semi_precious AS supplier_semi_precious, sfm.synthetic AS supplier_synthetic
						FROM `tabGemstone Price List` gpl
						INNER JOIN `tabGemstone Multiplier` gm 
							ON gm.parent = gpl.name AND gm.item_category = %s AND gm.parentfield = 'gemstone_multiplier'
						LEFT JOIN `tabGemstone Multiplier` sfm 
							ON sfm.parent = gpl.name AND sfm.item_category = %s AND sfm.parentfield = 'supplier_fg_multiplier'
						WHERE gpl.customer = %s
						AND gpl.price_list_type = %s
						AND gpl.cut_or_cab = %s
						AND gpl.gemstone_grade = %s
						ORDER BY gpl.creation DESC
						LIMIT 1
					""", (doc.item_category, doc.item_category, ref_customer, gemstone_price_list_ref_customer, gem.cut_or_cab, gem.gemstone_grade), as_dict=True)

					if query:
						entry = query[0]
						gemstone_quality = row.get("gemstone_quality") 
						gemstone_pr = gem.gemstone_pr

						multiplier_value = entry.get("precious") if gemstone_quality == "Precious" else \
											entry.get("semi_precious") if gemstone_quality == "Semi Precious" else \
											entry.get("synthetic")

						supplier_value = entry.get("supplier_precious") if gemstone_quality == "Precious" else \
											entry.get("supplier_semi_precious") if gemstone_quality == "Semi Precious" else \
											entry.get("supplier_synthetic")

						if multiplier_value is not None:
							gem.total_gemstone_rate = multiplier_value
							gem.gemstone_rate_for_specified_quantity = gem.total_gemstone_rate * gemstone_pr

						if supplier_value is not None:
							gem.fg_purchase_rate = supplier_value
							gem.fg_purchase_amount = gem.fg_purchase_rate * gemstone_pr

		for metal in doc.metal_detail:
			rate_per_gm = 0
			fg_purchase_rate = 0
			fg_purchase_amount = 0 
			wastage_rate = 0
			if self.custom_customer_gold == "Yes":
				metal.is_customer_item = 1
			making_charge_price_list = frappe.get_all(
				"Making Charge Price",
				filters={"customer": ref_customer, "setting_type": doc.setting_type},
				fields=["name"]
			)
			making_charge_price_list_with_gold_rate = frappe.get_all(
				"Making Charge Price",
				filters={
					"customer": ref_customer,
					"setting_type": doc.setting_type,
					"from_gold_rate": ["<=", doc.gold_rate_with_gst],
					"to_gold_rate": [">=", doc.gold_rate_with_gst]
				},
				fields=["name"]
			)
			
			if making_charge_price_list:
				subcategories = frappe.get_all(
					"Making Charge Price Item Subcategory",
					filters={"parent": making_charge_price_list[0]["name"]},
					fields=["subcategory", "rate_per_gm", "supplier_fg_purchase_rate", "wastage","custom_subcontracting_rate","custom_subcontracting_wastage"]
				)
				
				if subcategories:
					match = next((row for row in subcategories if row.get("subcategory") == doc.item_subcategory), None)
					if match:
						rate_per_gm = match.get("rate_per_gm", 0)
						fg_purchase_rate = match.get("supplier_fg_purchase_rate", 0)
						fg_purchase_amount = fg_purchase_rate * metal.quantity
						if metal.is_customer_item:
							metal.rate = match.get("custom_subcontracting_rate", 0)
							wastage_rate = match.get("custom_subcontracting_wastage")
							fg_purchase_rate = 0
							fg_purchase_amount = 0
							rate_per_gm = 0
							
						else:
							metal.rate = doc.gold_rate_with_gst
							wastage_rate = match.get("wastage", 0)/100

						# wastage_rate = match.get("wastage", 0) / 100.0
					else:
						frappe.msgprint(f"No matching subcategory found for {doc.item_subcategory}")
			else:
				frappe.msgprint(f"No making charge price list found for customer {doc.customer} and setting type {doc.setting_type}")

			metal.wastage_rate = wastage_rate
			# metal.rate = doc.gold_rate_with_gst
			metal.amount = metal.rate * metal.quantity
			metal.wastage_amount = metal.wastage_rate * metal.amount 
			metal.fg_purchase_rate = fg_purchase_rate
			metal.fg_purchase_amount = fg_purchase_amount
			metal.making_rate = rate_per_gm
			metal.making_amount = metal.making_rate * metal.quantity

	else:    
		for item in doc.metal_detail + doc.finding_detail:
			if (
				row.custom_customer_finding == "Yes"
				and row.parentfield == "finding_detail"
				and row.finding_category in attribute_data
			):
				item.is_customer_item = 1

			if row.custom_customer_gold == "Yes":
				if row.parentfield == "finding_detail" and row.finding_category not in attribute_data:
					item.is_customer_item = 1
				elif row.parentfield != "finding_detail":
					item.is_customer_item = 1
			if item.metal_touch:
				item.metal_purity = metal_criteria.get(item.metal_touch)

		for metal in doc.metal_detail:
			rate_per_gm = 0
			fg_purchase_rate = 0
			fg_purchase_amount = 0 
			wastage_rate = 0  
			if self.custom_customer_gold == "Yes":
				metal.is_customer_item = 1
			making_charge_price_list = frappe.get_all(
				"Making Charge Price",
				filters={
					"customer": doc.customer,
					"setting_type": doc.setting_type,
				},
				fields=["name"]
			)

			making_charge_price_list_with_gold_rate = frappe.get_all(
				"Making Charge Price",
				filters={
					"customer": doc.customer,
					"setting_type": doc.setting_type,
					"from_gold_rate": ["<=", doc.gold_rate_with_gst],
					"to_gold_rate": [">=", doc.gold_rate_with_gst]
				},
				fields=["name"]
			)
			
			if making_charge_price_list:
				making_charge_price_subcategories = frappe.get_all(
					"Making Charge Price Item Subcategory",
					filters={"parent": making_charge_price_list[0]["name"]},
					fields=["subcategory", "rate_per_gm", "supplier_fg_purchase_rate", "wastage","custom_subcontracting_rate","custom_subcontracting_wastage"]
				)
				
				if making_charge_price_subcategories:
					matching_subcategory = next(
						(row for row in making_charge_price_subcategories if row.get("subcategory") == doc.item_subcategory),
						None
					)
					if matching_subcategory:
						rate_per_gm = matching_subcategory.get("rate_per_gm", 0)
						fg_purchase_rate = matching_subcategory.get("supplier_fg_purchase_rate", 0)
						fg_purchase_amount = fg_purchase_rate * metal.quantity
						if metal.is_customer_item:
							metal.rate = matching_subcategory.get("custom_subcontracting_rate", 0)
							wastage_rate = matching_subcategory.get("custom_subcontracting_wastage")
							fg_purchase_rate = 0
							fg_purchase_amount = 0
							rate_per_gm = 0
							
						else:
							metal.rate = doc.gold_rate_with_gst
							wastage_rate = matching_subcategory.get("wastage", 0)/100

						# wastage_rate = matching_subcategory.get("wastage", 0) / 100.0

					metal.wastage_rate = wastage_rate
					metal.fg_purchase_amount = fg_purchase_amount
					metal.fg_purchase_rate = fg_purchase_rate
					metal.amount = metal.rate * metal.quantity
					metal.wastage_amount = metal.wastage_rate * metal.amount
					metal.making_rate = rate_per_gm
					metal.making_amount = metal.making_rate * metal.quantity
					# metal.rate = doc.gold_rate_with_gst
					
				else:
					frappe.msgprint(f"No matching subcategory found for {doc.item_subcategory}")
			else:
				frappe.msgprint(f"No making charge price list found for customer {doc.customer} and setting type {doc.setting_type}")
		
		for find in doc.finding_detail:
			wastage_rate = 0  
			matching_subcategory = None
			if self.custom_customer_gold == "Yes":
				find.is_customer_item = 1
			making_charge_price_list = frappe.get_all(
				"Making Charge Price",
				filters={
					"customer": doc.customer,
					"setting_type": doc.setting_type,
				},
				fields=["name"]
			)
			making_charge_price_list_with_gold_rate = frappe.get_all(
				"Making Charge Price",
				filters={
					"customer": doc.customer,
					"setting_type": doc.setting_type,
					"from_gold_rate": ["<=", doc.gold_rate_with_gst], 
					"to_gold_rate": [">=", doc.gold_rate_with_gst]
				},
				fields=["name"]
			)
			if making_charge_price_list:
				making_charge_price_subcategories = frappe.get_all(
					"Making Charge Price Item Subcategory",
					filters={"parent": making_charge_price_list[0]["name"]},
					fields=["subcategory", "rate_per_gm", "supplier_fg_purchase_rate", "wastage","custom_subcontracting_rate","custom_subcontracting_wastage"]
				)
				if making_charge_price_subcategories:
					matching_subcategory = next(
						(row for row in making_charge_price_subcategories if row.get("subcategory") == doc.item_subcategory),
						None
					)
				if matching_subcategory:
					rate_per_gm = matching_subcategory.get("rate_per_gm", 0)
					fg_purchase_rate = matching_subcategory.get("supplier_fg_purchase_rate", 0)
					fg_purchase_amount = fg_purchase_rate * find.quantity
					if find.is_customer_item:
						find.rate = matching_subcategory.get("custom_subcontracting_rate", 0)
						wastage_rate = matching_subcategory.get("custom_subcontracting_wastage", 0)
						fg_purchase_rate = 0
						fg_purchase_amount = 0
						rate_per_gm = 0
					else:
						find.rate = doc.gold_rate_with_gst
						wastage_rate = matching_subcategory.get("wastage", 0)/100



					# wastage_rate = matching_subcategory.get("wastage", 0) / 100.0
				# find.rate = doc.gold_rate_with_gst 
				find.amount = find.rate * find.quantity
				find.wastage_rate = wastage_rate
				find.making_rate = rate_per_gm
				find.making_amount = find.making_rate * find.quantity
				find.fg_purchase_rate = fg_purchase_rate
				find.fg_purchase_amount = fg_purchase_amount
				find.wastage_amount = find.wastage_rate * find.amount

		for diamond in doc.diamond_detail:
			
			diamond.rate = doc.gold_rate_with_gst
			if self.custom_customer_diamond == "Yes":
				diamond.is_customer_item = 1
			if diamond_price_list and any(dpl["price_list_type"] == diamond_price_list_customer for dpl in diamond_price_list):
				latest_diamond_price_list_entry  = frappe.db.sql(
						"""
						SELECT name, from_weight, to_weight, supplier_fg_purchase_rate,rate,outright_handling_charges_rate,outright_handling_charges_in_percentage,
						outwork_handling_charges_rate,outwork_handling_charges_in_percentage
						FROM `tabDiamond Price List` 
						WHERE customer = %s 
						AND price_list_type = %s 
						AND %s BETWEEN from_weight AND to_weight
						ORDER BY creation DESC
						LIMIT 1 
						""",
						(doc.customer, diamond_price_list_customer,diamond.weight_per_pcs),
						as_dict=True
					)
				if latest_diamond_price_list_entry:
					latest_entry = latest_diamond_price_list_entry[0]
					
					# diamond.total_diamond_rate = latest_entry.get("rate", 0)
					diamond.fg_purchase_rate = latest_entry.get("supplier_fg_purchase_rate", 0)
					diamond.fg_purchase_amount = diamond.fg_purchase_rate * diamond.quantity
					# diamond.diamond_rate_for_specified_quantity = diamond.total_diamond_rate * diamond.quantity
					if diamond.is_customer_item:
						diamond.total_diamond_rate = latest_entry.get("outwork_handling_charges_rate", 0)
						diamond.diamond_rate_for_specified_quantity =  diamond.total_diamond_rate * diamond.weight_per_pcs
						
						if latest_entry.get("outwork_handling_charges_rate") == 0:
							percentage = latest_entry.get("outwork_handling_charges_in_percentage", 0)
							amount = latest_entry.get("rate", 0) * (percentage / 100)
							diamond.total_diamond_rate = amount 
							diamond.diamond_rate_for_specified_quantity =  diamond.total_diamond_rate * diamond.weight_per_pcs
							
					else:
						diamond.total_diamond_rate = latest_entry.get("rate", 0) + latest_entry.get("outright_handling_charges_rate", 0)
						diamond.diamond_rate_for_specified_quantity =  diamond.total_diamond_rate * diamond.weight_per_pcs
						if latest_entry.get("outright_handling_charges_rate") == 0:
							percentage = latest_entry.get("outright_handling_charges_in_percentage", 0)
							rate = latest_entry.get("rate", 0) * (percentage / 100)
							diamond.total_diamond_rate = rate + latest_entry.get("rate", 0)
							diamond.diamond_rate_for_specified_quantity =  diamond.total_diamond_rate * diamond.weight_per_pcs


				if diamond_price_list_customer == "Sieve Size Range":
					sieve_size_range_diamond_price_list_entry = frappe.db.sql(
						"""
						SELECT name, supplier_fg_purchase_rate,rate,outright_handling_charges_rate,outright_handling_charges_in_percentage,
						outwork_handling_charges_rate,outwork_handling_charges_in_percentage
						FROM `tabDiamond Price List` 
						WHERE customer = %s 
						AND price_list_type = %s 
						AND sieve_size_range = %s
						ORDER BY creation DESC
						LIMIT 1 
						""",
						(doc.customer, diamond_price_list_customer, diamond.sieve_size_range),
						as_dict=True
					)
					if sieve_size_range_diamond_price_list_entry:
						latest_entry = sieve_size_range_diamond_price_list_entry[0]  # Get the first entry
						diamond.total_diamond_rate = latest_entry.get("rate", 0)
						diamond.fg_purchase_rate = latest_entry.get("supplier_fg_purchase_rate", 0)
						diamond.fg_purchase_amount = diamond.fg_purchase_rate * diamond.quantity

				if diamond_price_list_customer == "Size (in mm)":
					
					size_in_mm_diamond_price_list_entry = frappe.db.sql(
						"""
						SELECT name, supplier_fg_purchase_rate,rate,outright_handling_charges_rate,outright_handling_charges_in_percentage,
						outwork_handling_charges_rate,outwork_handling_charges_in_percentage
						FROM `tabDiamond Price List` 
						WHERE customer = %s 
						AND price_list_type = %s 
						AND size_in_mm = %s
						ORDER BY creation DESC
						LIMIT 1 
						""",
						(doc.customer, diamond_price_list_customer, diamond.size_in_mm),
						as_dict=True
					)
					if size_in_mm_diamond_price_list_entry:
						latest_entry = size_in_mm_diamond_price_list_entry[0]  # Get the first entry
						diamond.total_diamond_rate = latest_entry.get("rate", 0)
						diamond.fg_purchase_rate = latest_entry.get("supplier_fg_purchase_rate", 0)
						diamond.fg_purchase_amount = diamond.fg_purchase_rate * diamond.quantity
						if diamond.is_customer_item:
							diamond.total_diamond_rate = latest_entry.get("outwork_handling_charges_rate", 0)
							diamond.diamond_rate_for_specified_quantity = diamond.total_diamond_rate * diamond.size_in_mm
							if latest_entry.get("outwork_handling_charges_rate") == 0:
								percentage = latest_entry.get("outwork_handling_charges_in_percentage", 0)
								amount = latest_entry.get("rate", 0) * (percentage / 100)
								diamond.total_diamond_rate = amount 
								diamond.diamond_rate_for_specified_quantity = diamond.total_diamond_rate * diamond.size_in_mm
						else:
							diamond.total_diamond_rate = latest_entry.get("rate", 0) + latest_entry.get("outright_handling_charges_rate", 0)
							diamond.diamond_rate_for_specified_quantity =  diamond.total_diamond_rate * diamond.size_in_mm
							if latest_entry.get("outright_handling_charges_rate") == 0:
								percentage = latest_entry.get("outright_handling_charges_in_percentage", 0)
								rate = latest_entry.get("rate", 0) * (percentage / 100)
								diamond.total_diamond_rate = rate + latest_entry.get("rate", 0)


			# if row.custom_customer_diamond == "Yes":
			# 	diamond.is_customer_item = 1

			if row.diamond_quality:
				diamond.quality = row.diamond_quality

		for gem in doc.gemstone_detail:
			gem.rate = doc.gold_rate_with_gst
			if gemstone_price_list and any(dpl["price_list_type"] == gemstone_price_list_customer for dpl in gemstone_price_list):
				if gemstone_price_list_customer == "Diamond Range":
					combined_query = frappe.db.sql(
							"""
							SELECT gpl.name, gpl.cut_or_cab, gpl.gemstone_grade,
								gm.item_category, gm.precious, gm.semi_precious, gm.synthetic,
								sfm.precious AS supplier_precious, sfm.semi_precious AS supplier_semi_precious, sfm.synthetic AS supplier_synthetic
							FROM `tabGemstone Price List` gpl
							INNER JOIN `tabGemstone Multiplier` gm 
								ON gm.parent = gpl.name AND gm.item_category = %s AND gm.parentfield = 'gemstone_multiplier'
							LEFT JOIN `tabGemstone Multiplier` sfm 
								ON sfm.parent = gpl.name AND sfm.item_category = %s AND sfm.parentfield = 'supplier_fg_multiplier'
							WHERE gpl.customer = %s
							AND gpl.price_list_type = %s
							AND gpl.cut_or_cab = %s
							AND gpl.gemstone_grade = %s
							ORDER BY gpl.creation DESC
							LIMIT 1
							""",
							(doc.item_category, doc.item_category, doc.customer, gemstone_price_list_customer, gem.cut_or_cab, gem.gemstone_grade),
							as_dict=True
						)
					if combined_query:
						entry = combined_query[0] 
						gemstone_quality = gem.gemstone_quality
						gemstone_pr = gem.gemstone_pr
						multiplier_selected_value = entry.get("precious") if gemstone_quality == "Precious" else \
														entry.get("semi_precious") if gemstone_quality == "Semi Precious" else \
														entry.get("synthetic") if gemstone_quality == "Synthetic" else None

						supplier_selected_value = entry.get("supplier_precious") if gemstone_quality == "Precious" else \
													entry.get("supplier_semi_precious") if gemstone_quality == "Semi Precious" else \
													entry.get("supplier_synthetic") if gemstone_quality == "Synthetic" else None

						if multiplier_selected_value is not None:
								gem.total_gemstone_rate = multiplier_selected_value
								gem.gemstone_rate_for_specified_quantity = gem.total_gemstone_rate * gemstone_pr

						if supplier_selected_value is not None:
								gem.fg_purchase_rate = supplier_selected_value
								gem.fg_purchase_amount = gem.fg_purchase_rate * gemstone_pr

			if row.custom_customer_stone == "Yes":
				gem.is_customer_item = 1

		for other in doc.other_detail:
			if row.custom_customer_good == "Yes":
				other.is_customer_item = 1
	for idx, find in enumerate(doc.finding_detail, start=1):
		if not find.metal_purity:
			touch = (find.metal_touch or "").strip()
			purity = metal_criteria.get(touch)

			if not purity and row.get("metal_purity"):
				purity = row.metal_purity

			if purity:
				find.metal_purity = purity
			else:
				frappe.throw(f"BOM Finding Detail Row #{idx}: Value missing for: Metal Purity for Metal Touch '{touch}'")


# doc.save(ignore_permissions=True)

	# This Save will Call before_save and validate method in BOM and Rates Will be Calculated as diamond_quality is calculated too
	doc.save(ignore_permissions=True)
	doc.total_diamond_amount = sum(d.diamond_rate_for_specified_quantity for d in doc.diamond_detail if d.diamond_rate_for_specified_quantity)
	doc.total_diamond_weight = sum(d.quantity for d in doc.diamond_detail if d.quantity)
	doc.diamond_bom_amount = sum(d.diamond_rate_for_specified_quantity for d in doc.diamond_detail if d.diamond_rate_for_specified_quantity)
	doc.total_metal_amount = sum(d.amount for d in doc.metal_detail if d.amount)
	doc.making_charge = sum(d.making_amount for d in doc.metal_detail if d.making_amount)
	doc.finding_bom_amount = sum(d.amount for d in doc.finding_detail if d.amount)
	doc.gemstone_bom_amount = sum(d.gemstone_rate_for_specified_quantity for d in doc.gemstone_detail if d.gemstone_rate_for_specified_quantity)
	doc.total_bom_amount = (
    doc.diamond_bom_amount
    + doc.total_metal_amount
    + doc.making_charge
    + doc.finding_bom_amount
    + doc.gemstone_bom_amount
	)
	item_bom_data[row.item_code] = doc.name
	bom_data[row.item_code] = bom
	doc.db_set("custom_creation_docname", self.name)
	row.db_set("quotation_bom", doc.name)
	row.gold_bom_rate = doc.gold_bom_amount
	row.diamond_bom_rate = doc.diamond_bom_amount
	row.gemstone_bom_rate = doc.gemstone_bom_amount
	row.other_bom_rate = doc.other_bom_amount
	row.making_charge = doc.making_charge
	row.bom_rate = doc.total_bom_amount
	row.rate = doc.total_bom_amount
	


def submit_bom(self):
	pass
	# for row in self.items:
	# 	if row.quotation_bom:
	# 		bom = frappe.get_doc("BOM", row.quotation_bom)
	# 		bom.submit()


def cancel_bom(self):
	for row in self.items:
		if row.quotation_bom:
			bom = frappe.get_doc("BOM", row.quotation_bom)
			bom.is_active = 0
			# bom.cancel()
			bom.save()
			# frappe.delete_doc("BOM", bom.name, force=1)
			row.quotation_bom = None


from jewellery_erpnext.jewellery_erpnext.doc_events.bom import update_totals


@frappe.whitelist()
def update_bom_detail(
	parent_doctype,
	parent_doctype_name,
	metal_detail,
	diamond_detail,
	gemstone_detail,
	finding_detail,
	other_detail,
):
	parent = frappe.get_doc(parent_doctype, parent_doctype_name)

	set_metal_detail(parent, metal_detail)
	set_diamond_detail(parent, diamond_detail)
	set_gemstone_detail(parent, gemstone_detail)
	set_finding_detail(parent, finding_detail)
	set_other_detail(parent, other_detail)

	parent.reload()
	parent.ignore_validate_update_after_submit = True
	parent.save()

	update_totals(parent_doctype, parent_doctype_name)
	return "BOM Updated"


def set_metal_detail(parent, metal_detail):
	metal_data = json.loads(metal_detail)
	tolerance = frappe.db.get_value("Company", parent.company, "custom_metal_tolerance")
	for d in metal_data:
		validate_rate(parent, tolerance, d, "Metal")
		update_table(parent, "BOM Metal Detail", "metal_detail", d)


def set_diamond_detail(parent, diamond_detail):
	diamond_data = json.loads(diamond_detail)
	tolerance = frappe.db.get_value("Company", parent.company, "custom_diamond_tolerance")
	for d in diamond_data:
		validate_rate(parent, tolerance, d, "Diamond")
		update_table(parent, "BOM Diamond Detail", "diamond_detail", d)


def set_gemstone_detail(parent, gemstone_detail):
	gemstone_data = json.loads(gemstone_detail)
	tolerance = frappe.db.get_value("Company", parent.company, "custom_gemstone_tolerance")
	for d in gemstone_data:
		validate_rate(parent, tolerance, d, "Gemstone")
		update_table(parent, "BOM Gemstone Detail", "gemstone_detail", d)


def set_finding_detail(parent, finding_detail):
	finding_data = json.loads(finding_detail)
	tolerance = frappe.db.get_value("Company", parent.company, "custom_metal_tolerance")
	for d in finding_data:
		validate_rate(parent, tolerance, d, "Metal")
		update_table(parent, "BOM Finding Detail", "finding_detail", d)


def set_other_detail(parent, other_material):
	other_material = json.loads(other_material)
	for d in other_material:
		update_table(parent, "BOM Other Detail", "other_detail", d)


def update_table(parent, table, table_field, doc):
	if not doc.get("docname"):
		child_doc = parent.append(table_field, {})
	else:
		child_doc = frappe.get_doc(table, doc.get("docname"))
	doc.pop("docname", "")
	doc.pop("name", "")
	child_doc.update(doc)
	child_doc.flags.ignore_validate_update_after_submit = True
	child_doc.save()


def validate_rate(parent, tolerance, doc, table):
	table_dic = {
		"Metal": ["rate", "actual_rate"],
		"Gemstone": ["total_gemstone_rate", "actual_total_gemstone_rate"],
		"Diamond": ["total_diamond_rate", "actual_total_diamond_rate"],
	}
	if doc.get(table_dic.get(table)[0]) and doc.get(table_dic.get(table)[1]):
		tolerance_range = (doc.get(table_dic.get(table)[1]) * tolerance) / 100

		if (
			doc.get(table_dic.get(table)[1]) - tolerance_range
			<= doc.get(table_dic.get(table)[0])
			<= doc.get(table_dic.get(table)[1]) + tolerance_range
		):
			pass
		else:
			frappe.throw("Enter the rate within the tolerance range.")


def new_finding_item(parent_doc, child_doctype, child_docname, finding_item):
	child_item = frappe.new_doc(child_doctype, parent_doc, child_docname)
	child_item.item = "F"
	child_item.metal_type = finding_item.get("metal_type")
	child_item.finding_category = finding_item.get("finding_category")
	child_item.finding_type = finding_item.get("finding_type")
	child_item.finding_size = finding_item.get("finding_size")
	child_item.metal_purity = finding_item.get("metal_purity")
	child_item.metal_colour = finding_item.get("metal_colour")
	child_item.quantity = finding_item.get("quantity")
	return child_item


@frappe.whitelist()
def get_gold_rate(party_name=None, currency=None):
	if not party_name:
		return
	cust_terr = frappe.db.get_value("Customer", party_name, "territory")
	gold_rate_with_gst = frappe.db.get_value(
		"Gold Price List",
		{"territory": cust_terr, "currency": currency},
		"rate",
		order_by="effective_from desc",
	)
	if not gold_rate_with_gst:
		frappe.msgprint(f"Gold Price List Not Found For {cust_terr}, {currency}")
	return gold_rate_with_gst

def validate_invoice_item(self):
	self.set("custom_invoice_item", [])
	if not self.custom_invoice_item:
		
		customer_payment_term_doc = frappe.get_doc(
			"Customer Payment Terms",
			{"customer": self.customer}
		)
		
		e_invoice_items = []
		for item_detail in customer_payment_term_doc.customer_payment_details:
			item_type = item_detail.item_type
			if item_type:
				e_invoice_item_doc = frappe.get_doc("E Invoice Item", item_type)
					# Match specific sales_type
				matched_sales_type_row = None
				for row in e_invoice_item_doc.sales_type:
					if row.sales_type == self.custom_sales_type:
						matched_sales_type_row = row
						break

				# Skip item if no matching sales_type and custom_sales_type is set
				if self.custom_sales_type and not matched_sales_type_row:
					continue
				e_invoice_items.append({
					"item_type": item_type,
					"metal_purity": e_invoice_item_doc.metal_purity or "N/A",
					"is_for_metal": e_invoice_item_doc.is_for_metal,
					"metal_type": e_invoice_item_doc.metal_type or "N/A",
					"is_for_diamond": e_invoice_item_doc.is_for_diamond,
					"is_for_finding": e_invoice_item_doc.is_for_finding,
					"diamond_type": e_invoice_item_doc.diamond_type or "N/A",
					"is_for_gemstone": e_invoice_item_doc.is_for_gemstone,
					"is_for_making": e_invoice_item_doc.is_for_making,
					"is_for_finding_making": e_invoice_item_doc.is_for_finding_making,
					"uom": e_invoice_item_doc.uom or "N/A",
					"tax_rate": matched_sales_type_row.tax_rate if matched_sales_type_row else 0
				})

		self.set("custom_invoice_item", [])

		aggregated_diamond_items = {}
		aggregated_metal_making_items = {}
		aggregated_metal_amount_items = {}
		aggregated_finding_items = {}
		aggregated_finding_making_items = {}
		aggregated_gemstone_items = {}

		for item in self.items:
			if item.quotation_bom:
				bom_doc = frappe.get_doc("BOM", item.quotation_bom)
				for diamond in bom_doc.diamond_detail:
					for e_item in e_invoice_items:
						if (
							e_item["is_for_diamond"]
							and e_item["diamond_type"] == diamond.diamond_type
							and e_item["uom"] == diamond.stock_uom
						):
							key = (e_item["item_type"], e_item["uom"])
							if key not in aggregated_diamond_items:
								aggregated_diamond_items[key] = {
									"item_code": e_item["item_type"],
									"item_name": e_item["item_type"],
									"uom": e_item["uom"],
									"qty": 0,
									"rate": diamond.total_diamond_rate,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
                                    "tax_amount": 0,
                                    "amount_with_tax": 0
									
								}
							# multiplied_qty = diamond.quantity * item.qty

							# aggregated_diamond_items[key]["qty"] += multiplied_qty
							# diamond_amount = diamond.total_diamond_rate * multiplied_qty
							# aggregated_diamond_items[key]["amount"] += diamond_amount
							multiplied_qty = diamond.quantity * item.qty
							diamond_amount = diamond.total_diamond_rate * multiplied_qty

							# Update quantity and amount
							aggregated_diamond_items[key]["qty"] += multiplied_qty
							aggregated_diamond_items[key]["amount"] += diamond_amount

							# Calculate tax amount
							tax_rate_decimal = aggregated_diamond_items[key]["tax_rate"] / 100
							aggregated_diamond_items[key]["tax_amount"] += diamond_amount * tax_rate_decimal

							# Update amount with tax
							aggregated_diamond_items[key]["amount_with_tax"] = (
								aggregated_diamond_items[key]["amount"] +
								aggregated_diamond_items[key]["tax_amount"]
							)

				# Metal making aggregation
				for metal in bom_doc.metal_detail:
					for e_item in e_invoice_items:
						
						if (
							e_item["is_for_making"]
							and e_item["metal_type"] == metal.metal_type
							and e_item["metal_purity"] == metal.metal_touch
							and e_item["uom"] == metal.stock_uom
						):
							key = (e_item["item_type"], e_item["uom"])
							if key not in aggregated_metal_making_items:
								aggregated_metal_making_items[key] = {
									"item_code": e_item["item_type"],
									"item_name": e_item["item_type"],
									"uom": e_item["uom"],
									"qty": 0,
									"rate": metal.making_rate,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
                                    "tax_amount": 0,
                                    "amount_with_tax": 0
								}
							# multiplied_qty = metal.quantity * item.qty
							# aggregated_metal_making_items[key]["qty"] += multiplied_qty
							# metal_making_amount = metal.making_rate * multiplied_qty
							
							# aggregated_metal_making_items[key]["amount"] += metal_making_amount

							multiplied_qty = metal.quantity * item.qty
							metal_making_amount = metal.making_rate * multiplied_qty

							# Update quantity and amount
							aggregated_metal_making_items[key]["qty"] += multiplied_qty
							aggregated_metal_making_items[key]["amount"] += metal_making_amount

							# Calculate tax amount
							tax_rate_decimal = aggregated_metal_making_items[key]["tax_rate"] / 100
							aggregated_metal_making_items[key]["tax_amount"] += metal_making_amount * tax_rate_decimal

							# Update amount with tax
							aggregated_metal_making_items[key]["amount_with_tax"] = (
								aggregated_metal_making_items[key]["amount"] +
								aggregated_metal_making_items[key]["tax_amount"]
							)

				# Metal amount aggregation
				for metal in bom_doc.metal_detail:
					for e_item in e_invoice_items:
						
						if (
							e_item["is_for_metal"]
							and e_item["metal_type"] == metal.metal_type
							and e_item["metal_purity"] == metal.metal_touch
							and e_item["uom"] == metal.stock_uom
						):
							key = (e_item["item_type"], e_item["uom"])
							if key not in aggregated_metal_amount_items:
								aggregated_metal_amount_items[key] = {
									"item_code": e_item["item_type"],
									"item_name": e_item["item_type"],
									"uom": e_item["uom"],
									"qty": 0,
									"rate": metal.rate,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
                                    "tax_amount": 0,
                                    "amount_with_tax": 0
								}
							# multiplied_qty = metal.quantity * item.qty
							# aggregated_metal_amount_items[key]["qty"] += multiplied_qty
							# metal_amount = metal.rate * multiplied_qty
							# aggregated_metal_amount_items[key]["amount"] += metal_amount

							multiplied_qty = metal.quantity * item.qty
							metal_amount = metal.rate * multiplied_qty

							# Update quantity and amount
							aggregated_metal_amount_items[key]["qty"] += multiplied_qty
							aggregated_metal_amount_items[key]["amount"] += metal_amount

							# Calculate tax amount
							tax_rate_decimal = aggregated_metal_amount_items[key]["tax_rate"] / 100
							aggregated_metal_amount_items[key]["tax_amount"] += metal_amount * tax_rate_decimal

							# Update amount with tax
							aggregated_metal_amount_items[key]["amount_with_tax"] = (
								aggregated_metal_amount_items[key]["amount"] +
								aggregated_metal_amount_items[key]["tax_amount"]
							)

				# Finding aggregation
				for finding in bom_doc.finding_detail:
					for e_item in e_invoice_items:
						if (
							e_item["is_for_finding"]
							and e_item["metal_type"] == finding.metal_type
							and e_item["metal_purity"] == finding.metal_touch
							and e_item["uom"] == finding.stock_uom
						):
							key = (e_item["item_type"], e_item["uom"])
							if key not in aggregated_finding_items:
								aggregated_finding_items[key] = {
									"item_code": e_item["item_type"],
									"item_name": e_item["item_type"],
									"uom": e_item["uom"],
									"qty": 0,
									"rate": finding.rate,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
                                    "tax_amount": 0,
                                    "amount_with_tax": 0
								}
							
							multiplied_qty = finding.quantity * item.qty
							finding_amount = finding.rate * multiplied_qty

							# Update quantity and amount
							aggregated_finding_items[key]["qty"] += multiplied_qty
							aggregated_finding_items[key]["amount"] += finding_amount


							# Calculate tax amount
							tax_rate_decimal = aggregated_finding_items[key]["tax_rate"] / 100
							aggregated_finding_items[key]["tax_amount"] += finding_amount * tax_rate_decimal

							# Update amount with tax
							aggregated_finding_items[key]["amount_with_tax"] = (
								aggregated_finding_items[key]["amount"] +
								aggregated_finding_items[key]["tax_amount"]
							)


				# Finding making aggregation
				for finding_making in bom_doc.finding_detail:
					for e_item in e_invoice_items:
						if (
							e_item["is_for_finding_making"]
							and e_item["metal_type"] == finding_making.metal_type
							and e_item["metal_purity"] == finding_making.metal_touch
							and e_item["uom"] == finding_making.stock_uom
						):
							key = (e_item["item_type"], e_item["uom"])
							if key not in aggregated_finding_making_items:
								aggregated_finding_making_items[key] = {
									"item_code": e_item["item_type"],
									"item_name": e_item["item_type"],
									"uom": e_item["uom"],
									"qty": 0,
									"rate": finding_making.making_rate,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
                                    "tax_amount": 0,
                                    "amount_with_tax": 0
								}
							# multiplied_qty = finding.quantity * item.qty
							# finding_making_amount = finding_making.making_rate * multiplied_qty
							# aggregated_finding_making_items[key]["qty"] += multiplied_qty
							# aggregated_finding_making_items[key]["amount"] += finding_making_amount

							multiplied_qty = finding.quantity * item.qty
							finding_making_amount = finding_making.making_rate * multiplied_qty

							# Update quantity and amount
							aggregated_finding_making_items[key]["qty"] += multiplied_qty
							aggregated_finding_making_items[key]["amount"] += finding_making_amount

							# Calculate tax amount
							tax_rate_decimal = aggregated_finding_making_items[key]["tax_rate"] / 100
							aggregated_finding_making_items[key]["tax_amount"] += finding_making_amount * tax_rate_decimal

							# Update amount with tax
							aggregated_finding_making_items[key]["amount_with_tax"] = (
								aggregated_finding_making_items[key]["amount"] +
								aggregated_finding_making_items[key]["tax_amount"]
							)
				# Gemstone aggregation
				for gemstone in bom_doc.gemstone_detail:
					
					for e_item in e_invoice_items:
						# frappe.throw(f"{gemstone.uom}")
						if (
							e_item["is_for_gemstone"]
							and e_item["uom"] == gemstone.stock_uom
						):
							key = (e_item["item_type"], e_item["uom"])
							if key not in aggregated_gemstone_items:
								aggregated_gemstone_items[key] = {
									"item_code": e_item["item_type"],
									"item_name": e_item["item_type"],
									"uom": e_item["uom"],
									"qty": 0,
									"rate": gemstone.total_gemstone_rate,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
                                    "tax_amount": 0,
                                    "amount_with_tax": 0
								}
							# multiplied_qty = gemstone.quantity * item.qty
							# gemstone_amount = gemstone.total_gemstone_rate * multiplied_qty
							# aggregated_gemstone_items[key]["qty"] += multiplied_qty
							# aggregated_gemstone_items[key]["amount"] += gemstone_amount

							multiplied_qty = gemstone.quantity * item.qty
							gemstone_amount = gemstone.total_gemstone_rate * multiplied_qty

							# Update quantity and amount
							aggregated_gemstone_items[key]["qty"] += multiplied_qty
							aggregated_gemstone_items[key]["amount"] += gemstone_amount
							# Calculate tax amount
							tax_rate_decimal = aggregated_gemstone_items[key]["tax_rate"] / 100
							aggregated_gemstone_items[key]["tax_amount"] += gemstone_amount * tax_rate_decimal

							# Update amount with tax
							aggregated_gemstone_items[key]["amount_with_tax"] = (
								aggregated_gemstone_items[key]["amount"] +
								aggregated_gemstone_items[key]["tax_amount"]
							)
		for item in aggregated_diamond_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_metal_making_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_metal_amount_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_finding_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_finding_making_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_gemstone_items.values():
			self.append("custom_invoice_item", item)

def validate_gold_rate_with_gst(self):

	for i in self.items:
		if i.order_form_id:
			order_qty = frappe.db.get_value("Order",i.order_form_id,"qty")
			if order_qty is not None:
				if i.qty > order_qty:
					frappe.throw(
						_("Row {0} : Quotation Item Qty ({1}) cannot be greater than Order Form Qty ({2})").format(
							i.idx, i.qty, order_qty
						)
					)
	if not self.gold_rate_with_gst:
		frappe.throw("Gold Rate with GST is mandatory.")





