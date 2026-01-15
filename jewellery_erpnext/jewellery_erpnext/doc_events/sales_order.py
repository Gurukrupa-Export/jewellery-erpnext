import frappe
from frappe import _
from frappe.model.mapper import get_mapped_doc
from jewellery_erpnext.jewellery_erpnext.customization.sales_order.doc_events.branch_utils import (
	create_branch_so,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.bom_utils import (
	calculate_gst_rate,
	set_bom_item_details,
	set_bom_rate,
)


def validate(self, method):
	validate_sales_type(self)
	# update_snc(self)
	# update_same_customer_snc(self)
	validate_quotation_item(self)
	# validate_customer_approval_invoice_items(self)
	# if self.sales_type != 'Branch Sales':
	create_new_bom(self)
	validate_serial_number(self)
	# validate_items(self)
	validate_item_dharm(self)
	# calculate_gst_rate(self)
	if not self.get("__islocal") and self.docstatus == 0:
		set_bom_item_details(self)


def on_submit(self, method):
	# submit_bom(self)
	# create_branch_so(self)
	validate_snc(self)


def on_cancel(self, method):
	cancel_bom(self)
	validate_snc(self)



def create_new_bom(self):
	"""
	This Function Creates Sales Order Type BOM from Quotation Bom
	"""
	# diamond_grade_data = frappe._dict()
	self.total=0
	for row in self.items:
		creation_no = frappe.get_value("Serial No",row.serial_no,"purchase_document_no")
		serial_no_creator=frappe.get_value("Stock Entry",creation_no,"custom_serial_number_creator")
		snc=frappe.get_value("Serial Number Creator",serial_no_creator,"parent_manufacturing_order")
		refrence_customer=frappe.get_value("Parent Manufacturing Order",snc,"ref_customer")
		if not refrence_customer:
			sales_order=frappe.get_value("Parent Manufacturing Order",snc,"sales_order")
			refrence_customer=frappe.db.get_value("Sales Order",sales_order,"ref_customer")
			
		refrence_customer_for_company_to_company = frappe.get_value("Sales Order",self.custom_parent_sales_order,"customer")
		exchange_rate = frappe.db.sql("""SELECT exchange_rate FROM `tabCurrency Exchange` WHERE for_selling = 1 
			ORDER BY modified DESC LIMIT 1""", pluck="exchange_rate")
		exchange_rate = exchange_rate[0] if exchange_rate else None 
		billing_currency=frappe.get_value("Customer",refrence_customer,"default_currency")
		

		if not row.quotation_bom:
			if self.sales_type != 'Branch Sales':
				create_serial_no_bom(self, row)
			
			if row.bom:
				if frappe.db.get_value("BOM",row.bom,"docstatus") == 1:
					frappe.db.set_value("BOM",row.bom,"docstatus","0")
				doc = frappe.get_doc("BOM",row.bom)
				
				customer_group = frappe.db.get_value('Customer', self.customer , 'customer_group')
				precision = frappe.db.get_value("Customer", self.customer, "custom_precision_variable")
				doc.metal_and_finding_weight = round(sum(row.quantity for row in doc.metal_detail),precision) + round(sum(row.quantity for row in doc.finding_detail),precision)
				diamond_pcs=doc.total_diamond_pcs
				if hasattr(doc, "gemstone_detail"):
					for gem in doc.gemstone_detail or []:

						gemstone_price_list_customer = frappe.db.get_value(
							"Customer",
							self.customer,
							"custom_gemstone_price_list_type"
						)
						if self.company=='Gurukrupa Export Private Limited' and customer_group == 'Internal':
							gem.total_gemstone_rate = gem.fg_purchase_rate
							gem.gemstone_rate_for_specified_quantity = (
								float(gem.total_gemstone_rate) / 100 * float(gem.quantity)
							)
							doc.total_gemstone_amount = sum(
								flt(r.gemstone_rate_for_specified_quantity)
								for r in doc.get("gemstone_detail", [])
							)
						elif self.company=='KG GK Jewellers Private Limited' and customer_group == 'Internal':
							if billing_currency == 'USD':
								
								gem.total_gemstone_rate = gem.se_rate*exchange_rate
							else:
								gem.total_gemstone_rate = gem.se_rate
							gem.gemstone_rate_for_specified_quantity = (
								float(gem.total_gemstone_rate) / 100 * float(gem.quantity)
							)
							# gem.fg_purchase_amount=gem.gemstone_rate_for_specified_quantity
							doc.total_gemstone_amount = sum(
								flt(r.gemstone_rate_for_specified_quantity)
								for r in doc.get("gemstone_detail", []))
						else:
							if gemstone_price_list_customer == "Fixed" and customer_group !="Retail":

								gpc = frappe.get_all(
									"Gemstone Price List",
									filters={
										"customer": self.customer,
										"price_list_type": gemstone_price_list_customer,
										"gemstone_grade": gem.get("gemstone_grade"),
										"cut_or_cab": gem.get("cut_or_cab"),
										"gemstone_type": gem.get("gemstone_type"),
										"stone_shape": gem.get("stone_shape")
									},
									fields=["name", "price_list_type", "rate", "handling_rate"],
								)
								if not gpc:
									frappe.throw("No Gemstone Price List found")
							

							elif customer_group=="Retail":
								gpc = frappe.get_all(
									"Gemstone Price List",
									filters={
										"is_retail_customer":1,
										"price_list_type": gemstone_price_list_customer,
										"gemstone_grade": gem.get("gemstone_grade"),
										"cut_or_cab": gem.get("cut_or_cab"),
										"gemstone_type": gem.get("gemstone_type"),
										"stone_shape": gem.get("stone_shape")
									},
									fields=["name", "price_list_type", "rate", "handling_rate"],
								)

								if not gpc:
									frappe.throw("No Retail Gemstone Price List found")
								if gem.is_customer_item:
									gem.total_gemstone_rate = gpc[0]["outwork_handling_charges_rate"]
								else:	
									gem.total_gemstone_rate = gpc[0]["rate"]
								gem.total_gemstone_rate =round(gem.total_gemstone_rate , 2)
								gem.gemstone_rate_for_specified_quantity = (
									float(gem.total_gemstone_rate) / 100 * float(gem.gemstone_pr)
								)
								gem.gemstone_rate_for_specified_quantity=round(gem.gemstone_rate_for_specified_quantity, 2)
								doc.total_gemstone_amount = sum(
									flt(r.gemstone_rate_for_specified_quantity)
									for r in doc.get("gemstone_detail", [])
								)

							elif gemstone_price_list_customer == "Diamond Range"and customer_group !="Retail":
								gpc = frappe.get_all(
									"Gemstone Price List",
									filters={
										"customer": self.customer,
										"price_list_type": gemstone_price_list_customer,
										"cut_or_cab": gem.get("cut_or_cab"),
										"gemstone_grade":gem.get("gemstone_grade"),
										"from_gemstone_pr_rate":["<=",gem.get("gemstone_pr")],
										"to_gemstone_pr_rate":[">=",gem.get("gemstone_pr")]
									},
									fields=["name", "price_list_type"],
								)
								if not gpc:
									frappe.throw(f"No Multiplier Price List found ")

								gpc_doc = frappe.get_doc("Gemstone Price List", gpc[0].name)
								multiplier_rows = gpc_doc.get("gemstone_multiplier")
								rate = 0
								for mul in multiplier_rows:
									if mul.gemstone_type == gem.gemstone_type and (flt(doc.diamond_weight)>=flt(mul.from_weight) and flt(doc.diamond_weight)<=flt(mul.to_weight)):
										if gem.is_customer_item:
											if gem.gemstone_quality == 'Precious':
												rate = mul.outwork_precious_percentage

											elif gem.gemstone_quality == 'Semi-Precious':
												rate = mul.outwork_semi_precious_percentage

											elif gem.gemstone_quality == 'Synthetic':
												rate = mul.outwork_synthetic_percentage
										else:
											if gem.gemstone_quality == 'Precious':
												rate = mul.precious_percentage

											elif gem.gemstone_quality == 'Semi-Precious':
												rate = mul.semi_precious_percentage

											elif gem.gemstone_quality == 'Synthetic':
												rate = mul.synthetic_percentage

									gem.total_gemstone_rate = round(rate, 2)

								gem.gemstone_rate_for_specified_quantity = (
									float(rate) / 100 * float(gem.gemstone_pr)
								)
								gem.gemstone_rate_for_specified_quantity =round(gem.gemstone_rate_for_specified_quantity , 2)
								gem.price_list_type='Diamond Range'
							##############
							elif gemstone_price_list_customer == "Diamond Range" and customer_group =="Retail":
								gpc = frappe.get_all(
									"Gemstone Price List",
									filters={
										"is_retail_customer":1,
										"price_list_type": gemstone_price_list_customer,
										"cut_or_cab": gem.get("cut_or_cab"),
										"gemstone_grade":gem.get("gemstone_grade"),
										"from_gemstone_pr_rate":["<=",gem.get("gemstone_pr")],
										"to_gemstone_pr_rate":[">=",gem.get("gemstone_pr")]
									},
									fields=["name", "price_list_type"],
								)

								if not gpc:
									frappe.throw("No Retail Multiplier Price List found")

								gpc_doc = frappe.get_doc("Gemstone Price List", gpc[0].name)
								multiplier_rows = gpc_doc.get("gemstone_multiplier")
								rate = 0
								for mul in multiplier_rows:
									if mul.gemstone_type == gem.gemstone_type and (flt(doc.diamond_weight)>=flt(mul.from_weight) and flt(doc.diamond_weight)<=flt(mul.to_weight)):
										if gem.gemstone_quality == 'Precious':
											rate = mul.precious_percentage

										elif gem.gemstone_quality == 'Semi-Precious':
											rate = mul.semi_precious_percentage

										elif gem.gemstone_quality == 'Synthetic':
											rate = mul.synthetic_percentage

									gem.total_gemstone_rate = round(rate, 2)

								gem.gemstone_rate_for_specified_quantity = (
									float(rate) / 100 * float(gem.gemstone_pr)
								)
								gem.gemstone_rate_for_specified_quantity =round(gem.gemstone_rate_for_specified_quantity , 2)
								gem.price_list_type='Diamond Range'
							doc.total_gemstone_amount = sum(
									flt(r.gemstone_rate_for_specified_quantity)
									for r in doc.get("gemstone_detail", [])
								)

				if hasattr(doc, "metal_detail"):
					for s in doc.metal_detail:
						filters={
							"customer": self.customer,
							"metal_type":doc.metal_type,
							"setting_type":doc.setting_type,
							"from_gold_rate": ["<=", self.gold_rate_with_gst],
							"to_gold_rate": [">=", self.gold_rate_with_gst],
							"metal_touch":s.metal_touch
						}
						if self.company=='KG GK Jewellers Private Limited' :
							filters["customer"] = refrence_customer
						if self.company=='Gurukrupa Export Private Limited' and customer_group == 'Internal':
							filters["customer"] =refrence_customer_for_company_to_company
						mc = frappe.get_all(
						"Making Charge Price",
						filters=filters,
						fields=["name"],
						limit=1
					)
						if not mc:
			
							frappe.throw(f"""Create a valid Making Charge Price for Customer: {filters["customer"] }, Metal Type:{doc.metal_touch} "Setting Type":{doc.setting_type} """)
						
						mc_name = mc[0]["name"]
						sub = frappe.db.get_all(
							"Making Charge Price Item Subcategory",
							filters={"parent": mc_name, "subcategory": doc.item_subcategory},
							fields=[
								"rate_per_gm",
								"rate_per_pc",
								"supplier_fg_purchase_rate",
								"wastage",
								"custom_subcontracting_rate",
								"custom_subcontracting_wastage",
								"to_diamond","from_diamond"
							],
							limit=1
						)
						sub_info = sub[0]
						threshold = 2 if sub_info.get("rate_per_gm_threshold") == 0 else sub_info.get("rate_per_gm_threshold")
						if doc.metal_and_finding_weight is not None and threshold is not None:
							if doc.metal_and_finding_weight < threshold:
								for row in sub:
									if row.from_diamond:
										if int(row.from_diamond) <= int(diamond_pcs) <= int(row.to_diamond):
											
											sub_info = row
						gold_gst_rate=frappe.db.get_single_value("Jewellery Settings", "gold_gst_rate")
						
						
						if self.company=='Gurukrupa Export Private Limited' and customer_group == 'Internal':
							# for s in doc.metal_detail:
							if s.is_customer_item:
								s.rate=0
								s.quantity=round(s.quantity, precision)
								s.amount=round(s.rate*s.quantity,2 )
								s.making_rate= sub_info.get("custom_subcontracting_rate", 0)
								s.making_amount = s.making_rate * s.quantity
							else:
								customer_metal_purity = frappe.db.sql(f"""select metal_purity from `tabMetal Criteria` where parent = '{self.customer}' and metal_type = '{s.metal_type}' and metal_touch = '{s.metal_touch}'""",as_dict=True)[0]['metal_purity']
								calculated_gold_rate = (float(customer_metal_purity) * self.gold_rate_with_gst) / (100 + int(gold_gst_rate))
								s.rate= round(calculated_gold_rate , 2)
								
								s.quantity=round(s.quantity, precision)
								s.amount=round(s.rate*s.quantity,2 )
								s.making_rate=sub_info.get("supplier_fg_purchase_rate", 0)
								s.wastage_rate = 0 
								s.wastage_amount =0
								s.making_amount=round(s.making_rate*s.quantity,2 )
								s.customer_metal_purity = customer_metal_purity

								
						elif self.company=='KG GK Jewellers Private Limited' and customer_group == 'Internal':
							# for s in doc.metal_detail:
							if s.is_customer_item:
								s.rate=0
								s.quantity=round(s.quantity, precision)
								s.amount=round(s.rate*s.quantity,2 )
								s.making_rate= sub_info.get("custom_subcontracting_rate", 0)
								s.making_amount =round( s.making_rate * s.quantity,2)
							else:
								if billing_currency == 'USD':
									s.se_rate=s.se_rate*exchange_rate
									s.rate = s.se_rate
									s.making_rate=sub_info.get("supplier_fg_purchase_rate",0)*exchange_rate
								else:
									s.rate= s.se_rate
									s.making_rate=sub_info.get("supplier_fg_purchase_rate",0)
								s.quantity=round(s.quantity, precision)
								s.amount=round(s.rate*s.quantity,2 )
								s.wastage_rate = 0 
								s.wastage_amount =0
								s.making_amount=round(s.making_rate*s.quantity,2)
								
								
						else:
							if not mc:
								frappe.throw(f"""Create a valid Making Charge Price for Customer: {self.customer}, Metal Type:{doc.metal_type} "Setting Type":{doc.setting_type} """)
							# mc_name = mc[0]["name"]
							# sub = frappe.db.get_all(
							# 	"Making Charge Price Item Subcategory",
							# 	filters={"parent": mc_name, "subcategory": doc.item_subcategory},
							# 	fields=[
							# 		"rate_per_gm",
							# 		"rate_per_pc",
							# 		"supplier_fg_purchase_rate",
							# 		"wastage",
							# 		"custom_subcontracting_rate",
							# 		"custom_subcontracting_wastage"
							# 	],
							# 	limit=1
							# )
							# sub_info = sub[0]
							if doc.metal_and_finding_weight is not None and threshold is not None:
								if doc.metal_and_finding_weight < threshold:
									# Use per piece rate, wastage might apply differently if needed
									making_rate = sub_info.get("rate_per_pc", 0)
									wastage_rate_value = 0  # or adjust if wastage applies for rate_per_pc
							else:
								# Use per gram rate along with wastage value
								making_rate = sub_info.get("rate_per_gm", 0)
								wastage_rate_value = sub_info.get("wastage", 0) / 100.0
							
							is_cust = getattr(doc, "is_customer_item", False)
							
							# for s in doc.metal_detail:
								
									
							if is_cust:
								wastage = sub_info.get("custom_subcontracting_wastage", 0) / 100.0
							else:
								# s.rate = self.gold_rate_with_gst
								wastage = wastage_rate_value
							if s.is_customer_item:
								s.rate=0
								s.amount=round(s.rate*s.quantity,2 )
								s.making_rate= sub_info.get("custom_subcontracting_rate", 0)
								s.making_amount = s.making_rate * s.quantity
							# gold_gst_rate=frappe.db.get_single_value("Jewellery Settings", "gold_gst_rate")
							# calculated_gold_rate = (float(s.metal_purity) * self.gold_rate_with_gst) / (100 + int(gold_gst_rate))
							else:
								customer_metal_purity = frappe.db.sql(f"""select metal_purity from `tabMetal Criteria` where parent = '{self.customer}' and metal_type = '{s.metal_type}' and metal_touch = '{s.metal_touch}'""",as_dict=True)[0]['metal_purity']
								
								s.customer_metal_purity = customer_metal_purity
								
								calculated_gold_rate = (float(customer_metal_purity) * self.gold_rate_with_gst) / (100 + int(gold_gst_rate))
								s.rate=round(calculated_gold_rate , 2)
								s.quantity=round(s.quantity, precision)
								s.amount=round(s.rate*s.quantity,2 )
								
								s.making_rate=making_rate
								if doc.metal_and_finding_weight < 2:
									s.making_amount = s.making_rate
								else:
									s.making_amount = s.making_rate * s.quantity
								s.wastage_rate=wastage
								s.wastage_amount=s.wastage_rate*s.amount
						
						# frappe.set_value(s.doctype, s.name, "rate", s.rate)
					doc.total_metal_amount = sum(flt((r.amount)) for r in doc.get("metal_detail", []))
					
					doc.total_wastage_amount = sum(flt((r.wastage_amount)) for r in doc.get("metal_detail", [])) 
					doc.total_making_amount = sum(flt((r.making_amount)) for r in doc.get("metal_detail", [])) 
					
				



				if hasattr(doc, "finding_detail") and doc.finding_detail:
					# Cache Finding Subcategory data to avoid repeated DB queries for same finding_type
					finding_cache = {}
					total_finding_amount = 0.0
					total_finding_making_amount = 0.0
					total_finding_wastage_amount = 0.0

					for f in doc.finding_detail:
						filters={
							"customer": self.customer,
							"metal_type":doc.metal_type,
							"setting_type":doc.setting_type,
							"from_gold_rate": ["<=", self.gold_rate_with_gst],
							"to_gold_rate": [">=", self.gold_rate_with_gst],
							"metal_touch":f.metal_touch
						}
						if self.company=='KG GK Jewellers Private Limited' :
							filters["customer"] = refrence_customer
						if self.company=='Gurukrupa Export Private Limited' and customer_group == 'Internal':
							filters["customer"] =refrence_customer_for_company_to_company
						mc = frappe.get_all(
						"Making Charge Price",
						filters=filters,
						fields=["name"],
						limit=1
					)
						if not mc:
			
							frappe.throw(f"""Create a valid Making Charge Price for Customer: {filters["customer"] }, Metal Type:{doc.metal_touch} "Setting Type":{doc.setting_type} """)
						
						mc_name = mc[0]["name"]
						sub = frappe.db.get_all(
							"Making Charge Price Item Subcategory",
							filters={"parent": mc_name, "subcategory": doc.item_subcategory},
							fields=[
								"rate_per_gm",
								"rate_per_pc",
								"supplier_fg_purchase_rate",
								"wastage",
								"custom_subcontracting_rate",
								"custom_subcontracting_wastage"
							],
							limit=1
						)
						sub_info = sub[0]
						finding_type = f.finding_type
						if finding_type not in finding_cache:
							find = frappe.db.get_all(
								"Making Charge Price Finding Subcategory",
								filters={
									"parent": mc_name,
									"subcategory": finding_type
								},
								fields=[
									"rate_per_gm",
									"rate_per_pc",
									"wastage",
									"supplier_fg_purchase_rate",
									"custom_subcontracting_rate",
									"custom_subcontracting_wastage"
								],
								limit=1
							)
							if not find:
								find = frappe.db.get_all(
									"Making Charge Price Item Subcategory",
									filters={"parent": mc_name, "subcategory": doc.item_subcategory},
									fields=[
										"rate_per_gm",
										"rate_per_pc",
										"supplier_fg_purchase_rate",
										"wastage",
										"custom_subcontracting_rate",
										"custom_subcontracting_wastage","name"
									],
									limit=1
								)
							find_data= find[0]
						gold_gst_rate=frappe.db.get_single_value("Jewellery Settings", "gold_gst_rate")
						calculated_gold_rate = (float(f.metal_purity) * self.gold_rate_with_gst) / (100 + int(gold_gst_rate))
						gold_gst_rate=frappe.db.get_single_value("Jewellery Settings", "gold_gst_rate")
						customer_metal_purity = frappe.db.sql(f"""select metal_purity from `tabMetal Criteria` where parent = '{self.customer}' and metal_type = '{f.metal_type}' and metal_touch = '{f.metal_touch}'""",as_dict=True)[0]['metal_purity']
						calculated_gold_rate = (float(customer_metal_purity) * self.gold_rate_with_gst) / (100 + int(gold_gst_rate))
						if f.is_customer_item:
							f.rate= 0
							f.quantity=round(f.quantity, precision)
							f.amount=0
							f.making_rate = find_data.get("custom_subcontracting_rate")
							f.wastage_rate = 0
							f.wastage_amount=0
							f.metal_purity = customer_metal_purity
							f.making_amount = round(f.making_rate*f.quantity,2 )
						else:
							if self.company=='Gurukrupa Export Private Limited' and customer_group == 'Internal':
								f.rate= round(calculated_gold_rate , 2)
								f.quantity=round(f.quantity, precision)
								f.amount=round(f.rate*f.quantity,2 )
								f.making_rate = find_data.get("supplier_fg_purchase_rate")
								f.wastage_rate = 0
								f.wastage_amount=0
								f.metal_purity = customer_metal_purity
								f.making_amount = round(f.making_rate*f.quantity,2 )
							elif self.company=='KG GK Jewellers Private Limited' and customer_group == 'Internal':
								if billing_currency == 'USD':
									f.se_rate=f.se_rate*exchange_rate
									f.rate= f.se_rate
									f.making_rate = find_data.get("supplier_fg_purchase_rate")*exchange_rate
								else:
									f.rate= f.se_rate
									f.making_rate = find_data.get("supplier_fg_purchase_rate")
								f.quantity=round(f.quantity, precision)
								f.amount=round(f.rate*f.quantity,2 )
								f.wastage_rate = 0
								f.wastage_amount =0
								f.metal_purity = customer_metal_purity
								f.making_amount = round(f.making_rate*f.quantity,2 )
							else:
								f.metal_purity = customer_metal_purity
								
								f.rate=round(calculated_gold_rate , 2)
								f.quantity=round(f.quantity, precision)
								f.amount = round(f.rate * f.quantity,  2)
								finding_weight = getattr(doc, "metal_and_finding_weight", None)

								if finding_weight is not None and finding_weight < 2:
									making_rate = find_data.get("rate_per_pc", 0)
									wastage_rate = 0 
									f.making_amount = making_rate 
								else:
									making_rate = find_data.get("rate_per_gm", 0)
									wastage_rate = find_data.get("wastage", 0) / 100.0
									f.making_amount = making_rate * f.quantity

								f.making_rate = making_rate
								f.wastage_rate = wastage_rate
								f.wastage_amount = wastage_rate * f.amount

						total_finding_amount += f.amount
						total_finding_making_amount += f.making_amount
						total_finding_wastage_amount += f.wastage_amount
					
					doc.total_finding_amount = total_finding_amount
					doc.total_finding_making_amount = total_finding_making_amount
					doc.total_finding_wastage_amount = total_finding_wastage_amount

			

				if hasattr(doc, "diamond_detail"):
					for d in doc.diamond_detail:
						# Fetch customer's diamond price list for the stone shape
						if self.company=='KG GK Jewellers Private Limited' and customer_group == 'Internal':
							customer_diamond_list = frappe.db.sql(
							f"""
							SELECT diamond_price_list FROM `tabDiamond Price List Table`
							WHERE parent = %s AND diamond_shape = %s
							""", (refrence_customer, d.stone_shape), as_dict=True)
						else:
							customer_diamond_list = frappe.db.sql(
								f"""
								SELECT diamond_price_list FROM `tabDiamond Price List Table`
								WHERE parent = %s AND diamond_shape = %s
								""", (self.customer, d.stone_shape), as_dict=True)
						rate = 0
						if customer_diamond_list:

							price_list_type = customer_diamond_list[0]["diamond_price_list"]

							# Prepare common filters for Diamond Price List query
							common_filters = {
								"price_list": "Standard Selling",
								"price_list_type": price_list_type,
								"customer": self.customer,
								"diamond_type": d.diamond_type,
								"stone_shape": d.stone_shape,
								"diamond_quality": d.quality
							}

							# d.weight_per_pcs = round(d.quantity/d.pcs,3)
							d.weight_per_pcs = round(d.quantity/d.pcs,2)
							if price_list_type == 'Sieve Size Range':
								sieve_filter = {**common_filters, "sieve_size_range": d.sieve_size_range}
								latest = frappe.db.get_value("Diamond Price List", sieve_filter,
															["rate",
															"outright_handling_charges_rate",
															"outright_handling_charges_in_percentage",
															"outwork_handling_charges_rate",
															"outwork_handling_charges_in_percentage"], as_dict=True)
							elif price_list_type == 'Weight (in cts)':
								common_conditions = " AND ".join([f"{k} = %s" for k in common_filters.keys()])
								rate_result =  frappe.db.sql(
												f"""
													SELECT 
														rate,
														outright_handling_charges_rate,
														outright_handling_charges_in_percentage,
														outwork_handling_charges_rate,
														outwork_handling_charges_in_percentage
													FROM `tabDiamond Price List`
													WHERE {common_conditions}
													AND %s BETWEEN from_weight AND to_weight
													LIMIT 1
												""",
												list(common_filters.values()) + [d.weight_per_pcs],
												as_dict=True
											)
								
								latest = rate_result[0] if rate_result else None
							elif price_list_type == 'Size (in mm)':
								
								latest = frappe.db.get_value("Diamond Price List", {**common_filters, "diamond_size_in_mm": d.diamond_sieve_size},
															["rate",
															"outright_handling_charges_rate",
															"outright_handling_charges_in_percentage",
															"outwork_handling_charges_rate",
															"outwork_handling_charges_in_percentage"], as_dict=True)
							else:
								latest = None
							if not latest:
								total_rate=0
								d.total_diamond_rate = 0
								d.diamond_rate_for_specified_quantity = 0
							if latest:

								base_rate = latest.get("rate", 0)
								fg_purchase_rate=latest.get("supplier_fg_purchase_rate", 0)
								out_rate = latest.get("outright_handling_charges_rate", 0)
								out_pct = latest.get("outright_handling_charges_in_percentage", 0)
								work_rate = latest.get("outwork_handling_charges_rate", 0)
								work_pct = latest.get("outwork_handling_charges_in_percentage", 0)
								is_cust = getattr(d, "is_customer_item", False)  # Or fetch from appropriate BOM row object if accessible

								# Determine multiplier based on price_list_type
								if price_list_type == "Size (in mm)":
									multiplier = d.quantity  # or appropriate field here
								elif price_list_type == "Sieve Size Range":
									multiplier = d.quantity  # Or sieve size dependent multiplier
								else:  # Weight in cts
									multiplier = d.quantity

								# Calculate total_rate based on is_customer_item flag
								if is_cust:
									if work_rate:
										total_rate = work_rate
									else:
										total_rate = base_rate * (work_pct / 100)
								else:
									if out_rate:
										total_rate = base_rate + out_rate
									else:
										total_rate = base_rate + (base_rate * (out_pct / 100))
							
							
							if self.company=='KG GK Jewellers Private Limited' and customer_group == 'Internal':
								if billing_currency == 'USD':
									d.se_rate = d.se_rate *exchange_rate
									d.total_diamond_rate=d.se_rate
								else:
									d.total_diamond_rate=d.se_rate
								d.quantity=round(d.quantity,precision )
								d.diamond_rate_for_specified_quantity = round(d.quantity * d.se_rate, 2)
							elif self.company=='Gurukrupa Export Private Limited' and customer_group == 'Internal':
								d.fg_purchase_rate = latest.get("supplier_fg_purchase_rate") if latest else 0
								d.total_diamond_rate=d.fg_purchase_rate
								d.quantity=round(d.quantity,precision )
								d.diamond_rate_for_specified_quantity = round(d.quantity * d.total_diamond_rate, 2)
							# Fetch the matching diamond price list entry
							else:
								
									d.total_diamond_rate = round(total_rate, 2)
									d.quantity=round(d.quantity,precision )
									d.diamond_rate_for_specified_quantity = round(d.quantity * total_rate, 2)
								
				doc.total_diamond_amount = sum(
					flt(r.diamond_rate_for_specified_quantity)
					for r in doc.get("diamond_detail", [])
				)
				
				doc.diamond_bom_amount=doc.total_diamond_amount
				doc.gold_bom_amount=doc.total_metal_amount
				doc.gemstone_bom_amount=doc.total_gemstone_amount
				doc.finding_bom_amount=doc.total_finding_amount
				doc.total_bom_amount=(doc.diamond_bom_amount +doc.gold_bom_amount+ doc.gemstone_bom_amount+doc.finding_bom_amount)
				doc.making_charge = sum(row.making_amount for row in doc.metal_detail) + sum(row.making_amount for row in doc.finding_detail)
				doc.total_metal_weight = sum(row.quantity for row in doc.metal_detail)
				doc.metal_weight = doc.total_metal_weight
				doc.diamond_weight = sum(row.quantity for row in doc.diamond_detail)
				doc.total_diamond_weight_in_gms = round(sum(row.quantity for row in doc.diamond_detail)/5,2)
				doc.total_gemstone_weight = sum(row.quantity for row in doc.gemstone_detail)
				doc.gemstone_weight = doc.total_gemstone_weight
				doc.total_gemstone_weight_in_gms = round(sum(row.quantity for row in doc.gemstone_detail)/5,2)
				doc.finding_weight = round(sum(row.quantity for row in doc.finding_detail),2)
				doc.finding_weight_ = doc.finding_weight
				doc.total_finding_weight_per_gram = doc.finding_weight
				doc.total_diamond_pcs = sum(flt(row.pcs) for row in doc.diamond_detail)
				doc.total_gemstone_pcs = sum(flt(row.pcs) for row in doc.gemstone_detail)
				doc.total_other_weight = sum(row.quantity for row in doc.other_detail)
				doc.other_weight = doc.total_other_weight
				# doc.total_diamond_amount  = sum(row.diamond_rate_for_specified_quantity for row in doc.diamond_detail)
				doc.total_diamond_amount = sum(row.diamond_rate_for_specified_quantity or 0.0 for row in doc.diamond_detail)
				# doc.diamond_bom_amount = sum(row.diamond_rate_for_specified_quantity for row in doc.diamond_detail)
				doc.diamond_bom_amount = sum(row.diamond_rate_for_specified_quantity or 0.0 for row in doc.diamond_detail)

				doc.metal_and_finding_weight = round(flt(doc.metal_weight) + flt(doc.finding_weight),2)
				doc.gold_to_diamond_ratio = (
					flt(doc.metal_and_finding_weight) / flt(doc.diamond_weight) if doc.diamond_weight else 0
				)
				doc.diamond_ratio = (
					flt(doc.diamond_weight) / flt(doc.total_diamond_pcs) if doc.total_diamond_pcs else 0
				)
				doc.gross_weight = round(
					flt(doc.metal_and_finding_weight)
					+ flt(doc.total_diamond_weight_in_gms)
					+ flt(doc.total_gemstone_weight_in_gms)
					+ flt(doc.total_other_weight)
				,2)
				doc.metal_to_diamond_ratio_excl_of_finding=(
					flt(doc.metal_weight) / flt(doc.diamond_weight) if doc.diamond_weight else 0
				)
				# Jay Added
				doc.custom_total_pure_weight = sum(
					row.quantity * (flt(row.metal_purity) / 100) for row in doc.metal_detail
				)
				doc.custom_total_pure_finding_weight = sum(
					row.quantity * (flt(row.metal_purity) / 100) for row in doc.finding_detail
				)
				doc.custom_net_pure_weight = (
					doc.custom_total_pure_weight + doc.custom_total_pure_finding_weight
				)
				# for row in self.items:
					# bom_doc = frappe.get_doc("BOM", row.bom)	
					# 
				total_amount = 	doc.total_bom_amount+ doc.making_charge + doc.certification_amount + doc.custom_duty_amount + doc.hallmarking_amount+ doc.freight_amount + doc.sale_amount
				row.amount=total_amount
				row.rate=row.amount/row.qty
				row.gold_bom_rate =doc.gold_bom_amount
				row.diamond_bom_rate =doc.diamond_bom_amount
				row.gemstone_bom_rate = doc.gemstone_bom_amount
				row.other_bom_rate = doc.other_bom_amount
				row.making_charge = doc.making_charge
				self.total=self.total + row.amount
				# frappe.msgprint(f"{self.total},{total_amount}")
				# self.total = self.total	
				# for row in self.taxes:
				# 	row.amount = self.total
					# row.rate = row.amount / row.qty
				
				doc.save(ignore_permissions=True)		
				frappe.db.commit()
		elif not row.bom and frappe.db.exists("BOM", row.quotation_bom):
			row.bom = row.quotation_bom
			#######################################################################
			# bom_doc = frappe.get_doc("BOM", row.bom)
			# if hasattr(bom_doc, "diamond_detail"):
			# 	for diamond in bom_doc.diamond_detail or []:
			# 		diamond.quality = self.custom_diamond_quality
			# 	bom_doc.save(ignore_permissions=True)
			# 	frappe.db.commit()
			# 	frappe.msgprint("hii")
			#####################################################################
			data_to_be_updated = {
				"bom_type": "Sales Order",
				"custom_creation_doctype": "Sales Order",
				"custom_creation_docname": self.name,
				"gold_rate_with_gst": self.gold_rate_with_gst,
			}
			frappe.db.set_value("BOM", row.quotation_bom, data_to_be_updated)
			doc = frappe.get_doc("BOM",row.quotation_bom)
			row.gold_bom_rate = doc.gold_bom_amount
			row.diamond_bom_rate = doc.diamond_bom_amount
			row.gemstone_bom_rate = doc.gemstone_bom_amount
			row.other_bom_rate = doc.other_bom_amount
			row.making_charge = doc.making_charge
			row.bom_rate = doc.total_bom_amount
			row.rate = doc.total_bom_amount

			# create_sales_order_bom(self, row, diamond_grade_data)


def create_serial_no_bom(self, row):
	serial_no_bom = frappe.db.get_value("Serial No", row.serial_no, "custom_bom_no")
	if not serial_no_bom:
		return
	bom_doc = frappe.get_doc("BOM", serial_no_bom)
	# if self.customer != bom_doc.customer:
	doc = frappe.copy_doc(bom_doc)
	doc.customer = self.customer
	doc.gold_rate_with_gst = self.gold_rate_with_gst
	if hasattr(doc, "diamond_detail"):
		for diamond in doc.diamond_detail or []:
			diamond.quality = self.custom_diamond_quality
		# for diamond in doc.diamond_detail:
	doc.save(ignore_permissions=True)
	row.bom = doc.name
	row.bom_no = doc.name


def create_sales_order_bom(self, row, diamond_grade_data):
	doc = frappe.copy_doc(frappe.get_doc("BOM", row.quotation_bom))
	# doc = get_mapped_doc(
	# 	"BOM",
	# 	row.quotation_bom,
	# 	{
	# 		"BOM": {
	# 			"doctype": "BOM",
	# 		}
	# 	},
	# 	ignore_permissions=True,
	# )
	try:
		doc.custom_creation_doctype = self.doctype
		doc.is_default = 0
		doc.is_active = 1
		doc.bom_type = "Sales Order"
		doc.gold_rate_with_gst = self.gold_rate_with_gst
		doc.customer = self.customer
		doc.selling_price_list = self.selling_price_list
		doc.reference_doctype = "Sales Order"
		doc.reference_docname = self.name
		doc.custom_creation_docname = None
		# doc.save(ignore_permissions=True)
		for diamond in doc.diamond_detail:
			if row.diamond_grade:
				diamond.diamond_grade = row.diamond_grade
				diamond.quality=self.custom_diamond_quality
				
			else:
				if not diamond_grade_data.get(row.diamond_quality):
					diamond_grade_data[row.diamond_quality] = frappe.db.get_value(
						"Customer Diamond Grade",
						{"parent": doc.customer, "diamond_quality": row.diamond_quality},
						"diamond_grade_1",
					)

				diamond.diamond_grade = diamond_grade_data.get(row.diamond_quality)
			if row.diamond_quality:
				diamond.quality = row.diamond_quality

		# This Save will Call before_save and validate method in BOM and Rates Will be Calculated as diamond_quality is calculated too
		doc.save(ignore_permissions=True)
		doc.db_set("custom_creation_docname", self.name)
		row.bom = doc.name
		row.gold_bom_rate = doc.gold_bom_amount
		row.diamond_bom_rate = doc.diamond_bom_amount
		row.gemstone_bom_rate = doc.gemstone_bom_amount
		row.other_bom_rate = doc.other_bom_amount
		row.making_charge = doc.making_charge
		row.bom_rate = doc.total_bom_amount
		row.rate = doc.total_bom_amount
		self.total = doc.total_bom_amount
	except Exception as e:
		frappe.logger("utils").exception(e)
		frappe.log_error(
			title=f"Error while creating Sales Order from {row.quotation_bom}", message=str(e)
		)
		frappe.throw(_("Row {0} {1}").format(row.idx, e))

def validate_snc(self):
	for row in self.items:
		if row.serial_no:
			if self.docstatus == 2:  
				frappe.db.set_value("Serial No", row.serial_no, "status", "Active")
			else:
				frappe.db.set_value("Serial No", row.serial_no, "status", "Reserved")

def submit_bom(self):
	for row in self.items:
		if row.bom:
			bom_doc = frappe.get_doc("BOM", row.bom)
			if bom_doc.docstatus == 0:
				bom_doc.submit()
			# frappe.enqueue(enqueue_submit_bom, job_name="Submitting SO BOM", bom=row.bom)


# def enqueue_submit_bom(bom):
# 	bom_doc = frappe.get_doc("BOM", bom)
# 	if bom_doc.docstatus == 0:
# 		bom_doc.submit()


def cancel_bom(self):
	for row in self.items:
		if row.bom:
			bom = frappe.get_doc("BOM", row.bom)
			bom.is_active = 0
			row.bom = ""

def validate_serial_number(self):
	if getattr(self, 'skip_serial_validation', False):
		return
	
	for row in self.items:
		if row.serial_no:
			# serial_nos = [s.strip() for s in row.serial_no.split('\n') if s.strip()]

			# for serial in serial_nos:
			existing = frappe.db.sql("""
				SELECT soi.name, soi.parent
				FROM `tabSales Order Item` soi
				JOIN `tabSales Order` so ON soi.parent = so.name
				WHERE so.docstatus = 1
					AND soi.serial_no = %s
				
			""", (row.serial_no), as_dict=True)
			# if existing:
			# 	so_name = existing[0].parent
			# 	frappe.throw(f"Serial No {row.serial_no} is already used in submitted Sales Order {so_name}.")



import frappe
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.styles import Font, Alignment
from io import BytesIO

@frappe.whitelist()
def xl_preview_sales_order(docname):
    doc = frappe.get_doc("Sales Order", docname)
    rows_diamond = []

    # Excel columns (Diamond Rate pehle, Diamond Amount baadme shift kiya hai)
    columns = [
        "Index","Item Code","Serial No","Item Name","Diamond Quality","PCS","Diamond Weight","Average",
        "Total Cts","Grams","Total Diamond Rate","Diamond Amount","Gross Weight","Gemstone Weight",
        "Other Weight","Gold Rate","Net Weight","Gold Amount","Customer Purity","Chain Weight",
        "Chain Amount","Chain Purity","Per Gram MC","Chain MC","Chain Wastage %","Chain Wastage Amount",
        "Jewellery Per Gram MC","Jewellery MC","Gold Wastage %","Jewellery Wastage","Gemstone Pcs",
        "Gemstone Cts","Gemstone Amount","Cert Charge","Hallmark Charge","Total Amt"
    ]

    # --- Populate rows_diamond ---
    for item in doc.items:
        #  New logic for BOM selection
        bom_name = None
        if item.quotation_bom:
            bom_name = item.quotation_bom
        elif hasattr(item, "bom") and item.bom:   # Check if BOM field exists in Sales Order Item
            bom_name = item.bom

        if not bom_name:
            continue  # Skip if no BOM found

        bom_doc = frappe.get_doc("BOM", bom_name)

        total_qty = sum([float(d.quantity or 0) for d in bom_doc.diamond_detail])
        grams = total_qty * 0.2

        gross_weight = float(bom_doc.gross_weight or 0)
        gross_weight = round(gross_weight, 2)

        gemstone_weight = float(bom_doc.total_gemstone_weight_in_gms or 0)
        other_weight = float(bom_doc.other_weight or 0)
        net_weight = float(bom_doc.metal_and_finding_weight or 0)

        gemstone_pcs_rows = [float(g.pcs or 0) for g in bom_doc.gemstone_detail] if bom_doc.gemstone_detail else []
        gemstone_cts_rows = [float(g.quantity or 0) for g in bom_doc.gemstone_detail] if bom_doc.gemstone_detail else []
        gemstone_amount_rows = [float(g.gemstone_rate_for_specified_quantity or 0) for g in bom_doc.gemstone_detail] if bom_doc.gemstone_detail else []

        chain_weight_val, chain_mc_val, chain_wastage_val = 0.0, 0.0, 0.0
        chain_weight, chain_amount, chain_mc, chain_wastage, chain_purity = 0, 0, 0, 0, 0
        per_gram_mc, chain_wastage_amount = 0, 0
        net_weight_from_findings = 0.0

        if bom_doc.finding_detail:
            for f in bom_doc.finding_detail:
                qty = float(f.quantity or 0)
                if f.finding_category and f.finding_category.lower() == "chains":
                    chain_weight_val += qty
                    chain_purity = float(f.customer_metal_purity or 0)
                    per_gram_mc = float(f.making_rate or 0)
                    chain_mc_val = float(f.making_amount or 0)
                    chain_wastage_val = float(f.wastage_rate or 0)
                else:
                    net_weight_from_findings += qty

        # --- FIXED CHAIN AMOUNT CALCULATION ---
        if chain_weight_val > 0:
            chain_weight = chain_weight_val
            quotation_gold_rate = float(doc.gold_rate or 0)
            chain_amount = (quotation_gold_rate * chain_purity / 100) * chain_weight
            chain_mc = chain_mc_val
            chain_wastage = chain_wastage_val
            chain_wastage_amount = (chain_amount * chain_wastage_val) if chain_wastage_val else 0

        net_weight_display = net_weight + net_weight_from_findings

        #  Net Weight se chain weight minus karna ---
        if chain_weight > 0:
            net_weight_display = net_weight_display - chain_weight

        if bom_doc.metal_detail:
            customer_metal_purity = float(bom_doc.metal_detail[0].customer_metal_purity or 0)
            gold_wastage = float(bom_doc.metal_detail[0].wastage_rate or 0)
            jewellery_per_gram_mc = float(bom_doc.metal_detail[0].making_rate or 0)
        else:
            customer_metal_purity, gold_wastage, jewellery_per_gram_mc = 0.0, 0, 0

        quotation_gold_rate = float(doc.gold_rate or 0)
        calculated_gold_rate = (quotation_gold_rate * customer_metal_purity) / 100
        calculated_gold_rate = float(f"{calculated_gold_rate:.2f}")   # Always 2 decimals

        cert_charge = float(bom_doc.certification_amount or 0)
        hallmark_charge = float(bom_doc.hallmarking_amount or 0)

        for i, diamond in enumerate(bom_doc.diamond_detail):
            pcs = float(diamond.pcs or 0)
            qty = float(diamond.quantity or 0)   #
            qty = float(f"{qty:.2f}")            #
            avg = (qty / pcs) if pcs else 0
            rate = float(diamond.total_diamond_rate or 0)
            diamond_amount = rate * qty

            gold_amount_val = calculated_gold_rate * net_weight_display if i == 0 else 0
            jewellery_wastage_val = gold_amount_val * (gold_wastage / 100) if i == 0 else 0

            gemstone_pcs_val = gemstone_pcs_rows[i] if i < len(gemstone_pcs_rows) else 0
            gemstone_cts_val = gemstone_cts_rows[i] if i < len(gemstone_cts_rows) else 0
            gemstone_amount_val = gemstone_amount_rows[i] if i < len(gemstone_amount_rows) else 0

            jewellery_mc_val = net_weight_display * jewellery_per_gram_mc if i == 0 else 0

            total_amt = (
                hallmark_charge + cert_charge + jewellery_mc_val +
                gemstone_amount_val + gold_amount_val +
                jewellery_wastage_val + diamond_amount
            )

            rows_diamond.append([
                item.idx if i == 0 else "",
                item.item_code if i == 0 else "",
                item.serial_no if i == 0 else "",
                item.item_name if i == 0 else "",
                item.diamond_quality,
                pcs,
                f"{qty:.2f}",
                f"{avg:.3f}",   # 
                round(total_qty, 2) if (i == 0 and total_qty != 0) else "",
                round(grams, 2) if (i == 0 and grams != 0) else "",
                round(rate, 2),   # 
                round(diamond_amount, 2),  # 
                round(gross_weight, 2) if (i == 0 and gross_weight != 0) else "",
                round(gemstone_weight, 2) if (i == 0 and gemstone_weight != 0) else "",
                round(other_weight, 2) if (i == 0 and other_weight != 0) else "",
                f"{calculated_gold_rate:.2f}" if i == 0 else "",
                round(net_weight_display, 2) if i == 0 else "",
                f"{gold_amount_val:.2f}" if i == 0 else "",
                customer_metal_purity if i == 0 else "",
                round(chain_weight, 2) if i == 0 else "",
                round(chain_amount, 2) if i == 0 else "",
                chain_purity if i == 0 else "",
                round(per_gram_mc, 2) if i == 0 else "",
                round(chain_mc, 2) if i == 0 else "",
                round(chain_wastage, 2) if i == 0 else "",
                round(chain_wastage_amount, 2) if i == 0 else "",
                round(jewellery_per_gram_mc, 2) if i == 0 else "",
                round(jewellery_mc_val, 2) if i == 0 else "",
                round(gold_wastage, 2) if i == 0 else "",
                round(jewellery_wastage_val, 2) if i == 0 else "",
                gemstone_pcs_val if gemstone_pcs_val != 0 else "",
                round(gemstone_cts_val, 2) if gemstone_cts_val != 0 else "",
                round(gemstone_amount_val, 2) if gemstone_amount_val != 0 else "",
                round(cert_charge, 2) if i == 0 else "",
                round(hallmark_charge, 2) if i == 0 else "",
                round(total_amt, 2)
            ])

    # --- SUM ROW ---
    sum_row = [""] * len(columns)
    sum_row[5]  = round(sum(float(r[5] or 0) for r in rows_diamond), 2)
    sum_row[6]  = round(sum(float(r[6] or 0) for r in rows_diamond), 2)
    sum_row[8]  = round(sum(float(r[8] or 0) for r in rows_diamond), 2)
    sum_row[10] = round(sum(float(r[10] or 0) for r in rows_diamond), 2)  # Total Diamond Rate
    sum_row[11] = round(sum(float(r[11] or 0) for r in rows_diamond), 2)  # Diamond Amount
    sum_row[12] = round(sum(float(r[12] or 0) for r in rows_diamond), 2)
    sum_row[13] = round(sum(float(r[13] or 0) for r in rows_diamond), 2)
    sum_row[14] = round(sum(float(r[14] or 0) for r in rows_diamond), 2)
    sum_row[16] = round(sum(float(r[16] or 0) for r in rows_diamond), 2)
    sum_row[17] = round(sum(float(r[17] or 0) for r in rows_diamond), 2)
    sum_row[19] = round(sum(float(r[19] or 0) for r in rows_diamond), 2)
    sum_row[20] = round(sum(float(r[20] or 0) for r in rows_diamond), 2)
    sum_row[23] = round(sum(float(r[23] or 0) for r in rows_diamond), 2)
    sum_row[25] = round(sum(float(r[25] or 0) for r in rows_diamond), 2)
    sum_row[27] = round(sum(float(r[27] or 0) for r in rows_diamond), 2)  #  Jewellery MC total
    sum_row[29] = round(sum(float(r[29] or 0) for r in rows_diamond), 2)
    sum_row[30] = round(sum(float(r[30] or 0) for r in rows_diamond), 2)
    sum_row[31] = round(sum(float(r[31] or 0) for r in rows_diamond), 2)
    sum_row[32] = round(sum(float(r[32] or 0) for r in rows_diamond), 2)
    sum_row[33] = round(sum(float(r[33] or 0) for r in rows_diamond), 2)
    sum_row[34] = round(sum(float(r[34] or 0) for r in rows_diamond), 2)
    sum_row[35] = round(sum(float(r[35] or 0) for r in rows_diamond), 2)

    rows_diamond.append(sum_row)

    # --- Create Workbook ---
    wb = Workbook()
    ws = wb.active
    ws.title = "Diamond Detail"

    # --- Add Company Name ---
    company_name = "M/S. GURUKRUPA EXPORT PVT LIMITED"
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(columns))
    cell = ws.cell(row=1, column=1, value=company_name)
    cell.font = Font(bold=True, size=15)
    cell.alignment = Alignment(horizontal="center", vertical="center")

    # --- Add Headers ---
    for col_num, column_title in enumerate(columns, 1):
        c = ws.cell(row=2, column=col_num, value=column_title)
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal="center", vertical="center")

    # --- Add Data Rows ---
    for row_num, row_data in enumerate(rows_diamond, 3):
        for col_num, cell_value in enumerate(row_data, 1):
            ws.cell(row=row_num, column=col_num, value=cell_value)

    # --- Auto column width ---
    for i, column in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(i)].width = 15

    # --- Save to BytesIO and Download ---
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    frappe.local.response.filecontent = output.read()
    frappe.local.response.filename = f"Diamond_Detail_SO_{docname}.xlsx"
    frappe.local.response.type = "download"


@frappe.whitelist()
def get_customer_approval_data(customer_approval_data):
	doc = frappe.get_doc("Customer Approval", customer_approval_data)
	return doc


@frappe.whitelist()
def customer_approval_filter(doctype, txt, searchfield, start, page_len, filters):
	CustomerApproval = frappe.qb.DocType("Customer Approval")
	StockEntry = frappe.qb.DocType("Stock Entry")

	query = (
		frappe.qb.from_(CustomerApproval)
		.left_join(StockEntry)
		.on(CustomerApproval.name == StockEntry.custom_customer_approval_reference)
		.select(CustomerApproval.name)
		.where(
			(
				(StockEntry.custom_customer_approval_reference != CustomerApproval.name)
				| (StockEntry.custom_customer_approval_reference.isnull())
			)
			& (CustomerApproval.docstatus == 1)
			& (CustomerApproval[searchfield].like(f"%{txt}%"))
		)
	)

	if filters.get("date"):
		query = query.where(CustomerApproval.date == filters["date"])

	dialoge_filter = query.run(as_dict=True)

	return dialoge_filter




from frappe.utils import flt
def validate_items(self):
	# for row in self.custom_invoice_item:
		# frappe.throw(f"{row.rate}")
	allowed = ("Finished Goods", "Subcontracting", "Certification")
	if self.sales_type in allowed:
		customer_payment_term_doc = frappe.get_doc(
			"Customer Payment Terms",
			{"customer": self.customer}
		)
		
		e_invoice_items = []
	
		# Loop through all child table rows
		for row in customer_payment_term_doc.customer_payment_details:
			item_type = row.item_type
			e_invoice_item = frappe.get_doc("E Invoice Item", item_type)
			
			matched_sales_type_row = None
			for row in e_invoice_item.sales_type:
				if row.sales_type == self.sales_type:
					matched_sales_type_row = row
					break

			# Skip item if no matching sales_type and custom_sales_type is set
			if self.sales_type and not matched_sales_type_row:
				continue
			e_invoice_items.append({
				"item_type": item_type,
				"is_for_metal": e_invoice_item.is_for_metal,
				"is_for_labour": e_invoice_item.is_for_labour,
				"is_for_diamond": e_invoice_item.is_for_diamond,
				"diamond_type" : e_invoice_item.diamond_type,
				"is_for_making": e_invoice_item.is_for_making,
				"is_for_finding": e_invoice_item.is_for_finding,
				"is_for_finding_making": e_invoice_item.is_for_finding_making,
				"is_for_gemstone": e_invoice_item.is_for_gemstone,
				"metal_type": e_invoice_item.metal_type,
				"metal_purity": e_invoice_item.metal_purity,
				"uom": e_invoice_item.uom,
				"tax_rate": matched_sales_type_row.tax_rate if matched_sales_type_row else 0
			})
		self.set("custom_invoice_item", [])
		aggregated_metal_items = {}
		aggregated_metal_labour_items = {}
		aggregated_metal_making_items = {}
		aggregated_diamond_items = {}
		aggregated_finding_items = {}
		aggregated_finding_making_items = {}
		aggregated_gemstone_items = {}

		for item in self.items:
			if item.bom:
				bom_doc = frappe.get_doc("BOM", item.bom)
				for metal in bom_doc.metal_detail:
					if not metal.is_customer_item:
						for e_item in e_invoice_items:
							if (
								e_item["is_for_metal"] and
								metal.metal_type == e_item["metal_type"] and
								metal.metal_touch == e_item["metal_purity"] and
								metal.stock_uom == e_item["uom"]
							):
								key = (e_item["item_type"], e_item["uom"])
								if key not in aggregated_metal_items:
									aggregated_metal_items[key] = {
											"item_code": e_item["item_type"],
											"item_name": e_item["item_type"],
											"uom": e_item["uom"],
											"qty": 0,
											"rate": 0,
											"amount": 0,
											"tax_rate": e_item["tax_rate"],
											"tax_amount": 0,
											"amount_with_tax": 0
										}
						
								multiplied_qty = metal.quantity * item.qty
								
								metal_rate = metal.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else metal.rate
								metal_amount = metal_rate * multiplied_qty
								aggregated_metal_items[key]["rate"] += metal_rate
								# Update quantity and amount
								aggregated_metal_items[key]["qty"] = multiplied_qty
								aggregated_metal_items[key]["amount"] += metal_amount

								# Calculate tax amount
								tax_rate_decimal = aggregated_metal_items[key]["tax_rate"] / 100
								aggregated_metal_items[key]["tax_amount"] += metal_amount * tax_rate_decimal

								# Update amount with tax
								aggregated_metal_items[key]["amount_with_tax"] = (
									aggregated_metal_items[key]["amount"] +
									aggregated_metal_items[key]["tax_amount"]
								)
								aggregated_metal_items[key]["delivery_date"] = self.delivery_date


				for metal in bom_doc.metal_detail:
					for e_item in e_invoice_items:
						# New condition: if metal is a customer item and e_invoice is for labour
						if (
							metal.is_customer_item
							and e_item["is_for_labour"]
							and metal.stock_uom == e_item["uom"]
							and metal.metal_type == e_item["metal_type"]
							# and metal.metal_touch == e_item["metal_purity"]
						):
							key = (e_item["item_type"], e_item["uom"])
							if key not in aggregated_metal_labour_items:
								aggregated_metal_labour_items[key] = {
									"item_code": e_item["item_type"],
									"item_name": e_item["item_type"],
									"uom": e_item["uom"],
									"qty": 0,
									"rate": 0,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
									"tax_amount": 0,
									"amount_with_tax": 0
								}
							
							multiplied_qty = metal.quantity * item.qty
							metal_rate = metal.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else metal.rate
							metal_amount = metal_rate * multiplied_qty
							

							aggregated_metal_labour_items[key]["rate"] += metal_rate
							aggregated_metal_labour_items[key]["qty"] = multiplied_qty
							aggregated_metal_labour_items[key]["amount"] += metal_amount

							tax_rate_decimal = aggregated_metal_labour_items[key]["tax_rate"] / 100
							aggregated_metal_labour_items[key]["tax_amount"] += metal_amount * tax_rate_decimal
							aggregated_metal_labour_items[key]["amount_with_tax"] = (
								aggregated_metal_labour_items[key]["amount"] +
								aggregated_metal_labour_items[key]["tax_amount"]
							)
							aggregated_metal_labour_items[key]["delivery_date"] = self.delivery_date
		
			
				
				for making in bom_doc.metal_detail:
					if not metal.is_customer_item:
						for e_item in e_invoice_items:
							if (
								e_item["is_for_making"] and
								making.metal_type == e_item["metal_type"] and
								making.metal_touch == e_item["metal_purity"] and
								making.stock_uom == e_item["uom"]
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
							
								multiplied_qty = making.quantity * item.qty
								metal_making_amount = making.making_rate * multiplied_qty

								# Update quantity and amount
								aggregated_metal_making_items[key]["qty"] += multiplied_qty
								# aggregated_metal_making_items[key]["amount"] += metal_making_amount
								aggregated_metal_making_items[key]["amount"] += metal.making_amount
								# Calculate tax amount
								tax_rate_decimal = aggregated_metal_making_items[key]["tax_rate"] / 100
								aggregated_metal_making_items[key]["tax_amount"] += metal_making_amount * tax_rate_decimal

								# Update amount with tax
								aggregated_metal_making_items[key]["amount_with_tax"] = (
									aggregated_metal_making_items[key]["amount"] +
									aggregated_metal_making_items[key]["tax_amount"]
								)
								aggregated_metal_making_items[key]["delivery_date"] = self.delivery_date


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
									"rate": 0,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
									"tax_amount": 0,
									"amount_with_tax": 0
								}
								
							multiplied_qty = diamond.quantity * item.qty
							diamond_rate = diamond.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else diamond.total_diamond_rate
							# diamond_amount = sum(flt(diamond.diamond_rate_for_specified_quantity)
							# )
							diamond_amount = flt(diamond.diamond_rate_for_specified_quantity)
							
							# diamond_amount = diamond_rate * multiplied_qty
							# aggregated_diamond_items[key]["rate"] = diamond_rate
							# Update quantity and amount
							aggregated_diamond_items[key]["qty"] += multiplied_qty
							aggregated_diamond_items[key]["amount"] += diamond_amount
							aggregated_diamond_items[key]["rate"] = aggregated_diamond_items[key]["amount"] / aggregated_diamond_items[key]["qty"] 
							# Calculate tax amount
							tax_rate_decimal = aggregated_diamond_items[key]["tax_rate"] / 100
							aggregated_diamond_items[key]["tax_amount"] += diamond_amount * tax_rate_decimal

							# Update amount with tax
							aggregated_diamond_items[key]["amount_with_tax"] = (
								aggregated_diamond_items[key]["amount"] +
								aggregated_diamond_items[key]["tax_amount"]
							)
							aggregated_diamond_items[key]["delivery_date"] = self.delivery_date

		
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
										"rate": 0,
										"amount": 0,
										"tax_rate": e_item["tax_rate"],
										"tax_amount": 0,
										"amount_with_tax": 0
									}
								multiplied_qty = finding.quantity * item.qty
								finding_rate = finding.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else finding.rate
								finding_making_amount = finding_rate * multiplied_qty
								aggregated_finding_items[key]["rate"] = finding_rate
								# Update quantity and amount
								aggregated_finding_items[key]["qty"] += multiplied_qty
								aggregated_finding_items[key]["amount"] += finding_making_amount

								# Calculate tax amount
								tax_rate_decimal = aggregated_finding_items[key]["tax_rate"] / 100
								aggregated_finding_items[key]["tax_amount"] += finding_making_amount * tax_rate_decimal

								# Update amount with tax
								aggregated_finding_items[key]["amount_with_tax"] = (
									aggregated_finding_items[key]["amount"] +
									aggregated_finding_items[key]["tax_amount"]
								)
								aggregated_finding_items[key]["delivery_date"] = self.delivery_date


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
								aggregated_finding_making_items[key]["delivery_date"] = self.delivery_date
									# Gemstone aggregation


				for gemstone in bom_doc.gemstone_detail:	
					for e_item in e_invoice_items:
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
							multiplied_qty = gemstone.quantity * item.qty
							gemstone_rate = gemstone.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else gemstone.total_gemstone_rate
							# aggregated_gemstone_items[key]["rate"] = gemstone_rate
							# gemstone_amount = gemstone_rate * multiplied_qty
							gemstone_amount = flt(gemstone.gemstone_rate_for_specified_quantity)
							# Update quantity and amount
							aggregated_gemstone_items[key]["qty"] += multiplied_qty
							aggregated_gemstone_items[key]["amount"] += gemstone_amount
							aggregated_gemstone_items[key]["rate"] = aggregated_gemstone_items[key]["amount"] / aggregated_gemstone_items[key]["qty"]
							# Calculate tax amount
							tax_rate_decimal = aggregated_gemstone_items[key]["tax_rate"] / 100
							aggregated_gemstone_items[key]["tax_amount"] += gemstone_amount * tax_rate_decimal

							# Update amount with tax
							aggregated_gemstone_items[key]["amount_with_tax"] = (
								aggregated_gemstone_items[key]["amount"] +
								aggregated_gemstone_items[key]["tax_amount"]
							)
							aggregated_gemstone_items[key]["delivery_date"] = self.delivery_date
		



		for item in aggregated_diamond_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_metal_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_metal_labour_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_metal_making_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_finding_items.values():
			self.append("custom_invoice_item", item)

		
		for item in aggregated_finding_making_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_gemstone_items.values():
				self.append("custom_invoice_item", item)


def validate_item_dharm(self):
	allowed = ("Finished Goods", "Subcontracting", "Certification","Branch Sales")
	if self.sales_type in allowed:
		customer_payment_term_doc = frappe.get_doc(
			"Customer Payment Terms",
			{"customer": self.customer}
		)
		
		e_invoice_items = []

		for row in self.items:
			gross_weighh = frappe.get_value("BOM", row.bom, "gross_weight")
			row.custom_gross_weight = gross_weighh
			
		# Prepare invoice items as before
		for row in customer_payment_term_doc.customer_payment_details:
			item_type = row.item_type
			e_invoice_item = frappe.get_doc("E Invoice Item", item_type)
			matched_sales_type_row = None
			for st_row in e_invoice_item.sales_type:
				if st_row.sales_type == self.sales_type:
					matched_sales_type_row = st_row
					break

			if self.sales_type and not matched_sales_type_row:
				continue

			e_invoice_items.append({
				"item_type": item_type,
				"is_for_metal": e_invoice_item.is_for_metal,
				"is_for_hallmarking":e_invoice_item.is_for_hallmarking,
				"is_for_labour": e_invoice_item.is_for_labour,
				"is_for_diamond": e_invoice_item.is_for_diamond,
				"diamond_type": e_invoice_item.diamond_type,
				"is_for_making": e_invoice_item.is_for_making,
				"is_for_finding": e_invoice_item.is_for_finding,
				"is_for_finding_making": e_invoice_item.is_for_finding_making,
				"is_for_gemstone": e_invoice_item.is_for_gemstone,
				"metal_type": e_invoice_item.metal_type,
				"metal_purity": e_invoice_item.metal_purity,
				"uom": e_invoice_item.uom,
				"finding_category":e_invoice_item.finding_category,
				"tax_rate": matched_sales_type_row.tax_rate if matched_sales_type_row else 0
			})

		self.set("custom_invoice_item", [])
		aggregated_metal_items = {}
		aggregated_metal_labour_items = {}
		aggregated_metal_making_items = {}
		aggregated_hallmarking_items = {}
		aggregated_diamond_items = {}
		aggregated_gemstone_items = {}
		aggregated_finding_items = {}
		aggregated_finding_making_items = {}
		for item in self.items:
			if item.bom:
				bom_doc = frappe.get_doc("BOM", item.bom)
				if bom_doc.hallmarking_amount:
					for e_item in e_invoice_items:
						if (
							e_item["is_for_hallmarking"]
						):
							key = (e_item["item_type"], e_item["uom"])
							if key not in aggregated_hallmarking_items:
								aggregated_hallmarking_items[key] = {
									"item_code": e_item["item_type"],
									"item_name": e_item["item_type"],
									"uom": e_item["uom"],
									"qty": 0,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
									"tax_amount": 0,
									"amount_with_tax": 0,
									"delivery_date": self.delivery_date
								}
							aggregated_hallmarking_items[key]["amount"] += bom_doc.hallmarking_amount
							aggregated_hallmarking_items[key]["qty"] +=1

				for metal in bom_doc.metal_detail:
					
					if not metal.is_customer_item:
						for e_item in e_invoice_items:
							if (
								e_item["is_for_metal"] and
								metal.metal_type == e_item["metal_type"] and
								metal.metal_touch == e_item["metal_purity"] and
								metal.stock_uom == e_item["uom"]
							):
								key = (e_item["item_type"], e_item["uom"])
								
								if key not in aggregated_metal_items:
									aggregated_metal_items[key] = {
										"item_code": e_item["item_type"],
										"item_name": e_item["item_type"],
										"uom": e_item["uom"],
										"qty": 0,
										"amount": 0,
										"tax_rate": e_item["tax_rate"],
										"tax_amount": 0,
										"amount_with_tax": 0,
										"delivery_date": self.delivery_date
									}

								multiplied_qty = metal.quantity * item.qty
								metal_rate = metal.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else metal.rate
								# making_amount=metal.making_amount
								metal_amount = (metal_rate * multiplied_qty)
								
								# Sum quantities and amounts
								aggregated_metal_items[key]["qty"] += multiplied_qty
								aggregated_metal_items[key]["amount"] += metal_amount

								# Calculate tax amount
								tax_rate_decimal = aggregated_metal_items[key]["tax_rate"] / 100
								aggregated_metal_items[key]["tax_amount"] += metal_amount * tax_rate_decimal

								aggregated_metal_items[key]["amount_with_tax"] = (
									aggregated_metal_items[key]["amount"] +
									aggregated_metal_items[key]["tax_amount"]
								)
								break
								
						for e_item in e_invoice_items:
							if (
								e_item["is_for_making"] and
								metal.metal_type == e_item["metal_type"] and
								metal.metal_touch == e_item["metal_purity"] and
								metal.stock_uom == e_item["uom"]
							):
								key = (e_item["item_type"], e_item["uom"])

								if key not in aggregated_metal_making_items:
									aggregated_metal_making_items[key] = {
										"item_code": e_item["item_type"],
										"item_name": e_item["item_type"],
										"uom": e_item["uom"],
										"qty": 0,
										"rate": metal.making_rate,  # initial rate, will be overwritten with average later
										"amount": 0,
										"tax_rate": e_item["tax_rate"],
										"tax_amount": 0,
										"amount_with_tax": 0,
										"delivery_date": self.delivery_date
									}

								multiplied_qty = metal.quantity * item.qty
								metal_making_amount = metal.making_rate * multiplied_qty
								aggregated_metal_making_items[key]["qty"] += multiplied_qty
								aggregated_metal_making_items[key]["amount"] += metal_making_amount

								tax_rate_decimal = aggregated_metal_making_items[key]["tax_rate"] / 100
								aggregated_metal_making_items[key]["tax_amount"] += metal_making_amount * tax_rate_decimal

								aggregated_metal_making_items[key]["amount_with_tax"] = (
										aggregated_metal_making_items[key]["amount"] +
									aggregated_metal_making_items[key]["tax_amount"]
								)
								break
					else:
						for e_item in e_invoice_items:
							
							if (
								e_item["is_for_labour"]
								# and metal.stock_uom == e_item["uom"]
								# and metal.metal_type == e_item["metal_type"]
								# and metal.metal_touch == e_item["metal_purity"]
							):
								key = (e_item["item_type"], e_item["uom"])
								if key not in aggregated_metal_labour_items:
									aggregated_metal_labour_items[key] = {
										"item_code": e_item["item_type"],
										"item_name": e_item["item_type"],
										"uom": e_item["uom"],
										"qty": 0,
										"amount": 0,
										"tax_rate": e_item["tax_rate"],
										"tax_amount": 0,
										"amount_with_tax": 0,
										"delivery_date": self.delivery_date
									}

								multiplied_qty = metal.quantity * item.qty
								# metal_rate = metal.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else metal.making_rate
								metal_rate =metal.making_rate
								metal_amount = metal_rate * multiplied_qty

								aggregated_metal_labour_items[key]["qty"] += multiplied_qty
								aggregated_metal_labour_items[key]["amount"] += metal_amount
								tax_rate_decimal = aggregated_metal_labour_items[key]["tax_rate"] / 100
								aggregated_metal_labour_items[key]["tax_amount"] += metal_amount * tax_rate_decimal
								aggregated_metal_labour_items[key]["amount_with_tax"] = (
									aggregated_metal_labour_items[key]["amount"] +
									aggregated_metal_labour_items[key]["tax_amount"]
								)
								
						

				for diamond in bom_doc.diamond_detail:
					if not diamond.is_customer_item:
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
										"rate": 0,
										"amount": 0,
										"tax_rate": e_item["tax_rate"],
										"tax_amount": 0,
										"amount_with_tax": 0,
										"delivery_date": self.delivery_date
									}

								multiplied_qty = diamond.quantity * item.qty
								diamond_rate = diamond.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else diamond.total_diamond_rate
								diamond_amount = flt(diamond.diamond_rate_for_specified_quantity)

								aggregated_diamond_items[key]["qty"] += multiplied_qty
								aggregated_diamond_items[key]["amount"] += diamond_amount

								# Calculate average rate after accumulation
								if aggregated_diamond_items[key]["qty"] > 0:
									aggregated_diamond_items[key]["rate"] = aggregated_diamond_items[key]["amount"] / aggregated_diamond_items[key]["qty"]
								else:
									aggregated_diamond_items[key]["rate"] = 0

								tax_rate_decimal = aggregated_diamond_items[key]["tax_rate"] / 100
								aggregated_diamond_items[key]["tax_amount"] += diamond_amount * tax_rate_decimal

								aggregated_diamond_items[key]["amount_with_tax"] = (
									aggregated_diamond_items[key]["amount"] +
									aggregated_diamond_items[key]["tax_amount"]
								)
					else:
						for e_item in e_invoice_items:
							if (
								e_item["is_for_labour"]
								# and e_item["diamond_type"] == diamond.diamond_type
								# and e_item["uom"] == diamond.stock_uom
							):
								key = (e_item["item_type"], e_item["uom"])

								if key not in aggregated_metal_labour_items:
									aggregated_metal_labour_items[key] = {
										"item_code": e_item["item_type"],
										"item_name": e_item["item_type"],
										"uom": e_item["uom"],
										"qty": 0,
										"rate": 0,
										"amount": 0,
										"tax_rate": e_item["tax_rate"],
										"tax_amount": 0,
										"amount_with_tax": 0,
										"delivery_date": self.delivery_date
									}

								multiplied_qty = diamond.quantity * item.qty
								diamond_rate = diamond.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else diamond.total_diamond_rate
								diamond_amount = flt(diamond.diamond_rate_for_specified_quantity)

								aggregated_metal_labour_items[key]["qty"] += multiplied_qty/5
								aggregated_metal_labour_items[key]["amount"] += diamond_amount
								# Calculate average rate after accumulation
								if aggregated_metal_labour_items[key]["qty"] > 0:
									aggregated_metal_labour_items[key]["rate"] = aggregated_metal_labour_items[key]["amount"] / aggregated_metal_labour_items[key]["qty"]
								else:
									aggregated_metal_labour_items[key]["rate"] = 0

								tax_rate_decimal = aggregated_metal_labour_items[key]["tax_rate"] / 100
								aggregated_metal_labour_items[key]["tax_amount"] += diamond_amount * tax_rate_decimal

								aggregated_metal_labour_items[key]["amount_with_tax"] = (
									aggregated_metal_labour_items[key]["amount"] +
									aggregated_metal_labour_items[key]["tax_amount"]
								)		

				for gemstone in bom_doc.gemstone_detail:
					for e_item in e_invoice_items:
						if not gemstone.is_customer_item:
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
										"rate": gemstone.total_gemstone_rate,  # initial rate; average will be calculated later
										"amount": 0,
										"tax_rate": e_item["tax_rate"],
										"tax_amount": 0,
										"amount_with_tax": 0,
										"delivery_date": self.delivery_date
									}

								multiplied_qty = gemstone.quantity * item.qty
								gemstone_rate = gemstone.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else gemstone.total_gemstone_rate
								gemstone_amount = flt(gemstone.gemstone_rate_for_specified_quantity)

								aggregated_gemstone_items[key]["qty"] += multiplied_qty
								aggregated_gemstone_items[key]["amount"] += gemstone_amount

								# Calculate average rate after accumulation
								if aggregated_gemstone_items[key]["qty"] > 0:
									aggregated_gemstone_items[key]["rate"] = aggregated_gemstone_items[key]["amount"] / aggregated_gemstone_items[key]["qty"]
								else:
									aggregated_gemstone_items[key]["rate"] = 0

								tax_rate_decimal = aggregated_gemstone_items[key]["tax_rate"] / 100
								aggregated_gemstone_items[key]["tax_amount"] += gemstone_amount * tax_rate_decimal

								aggregated_gemstone_items[key]["amount_with_tax"] = (
									aggregated_gemstone_items[key]["amount"] +
									aggregated_gemstone_items[key]["tax_amount"]
								)
						else:
							if (
							e_item["is_for_labour"]
							and e_item["uom"] == gemstone.stock_uom
						):
								key = (e_item["item_type"], e_item["uom"])

								if key not in aggregated_metal_labour_items:
									aggregated_metal_labour_items[key] = {
										"item_code": e_item["item_type"],
										"item_name": e_item["item_type"],
										"uom": e_item["uom"],
										"qty": 0,
										"rate": gemstone.total_gemstone_rate,  # initial rate; average will be calculated later
										"amount": 0,
										"tax_rate": e_item["tax_rate"],
										"tax_amount": 0,
										"amount_with_tax": 0,
										"delivery_date": self.delivery_date
									}

								multiplied_qty = gemstone.quantity * item.qty
								gemstone_rate = gemstone.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else gemstone.total_gemstone_rate
								gemstone_amount = flt(gemstone.gemstone_rate_for_specified_quantity)

								aggregated_metal_labour_items[key]["qty"] += multiplied_qty/5
								aggregated_metal_labour_items[key]["amount"] += gemstone_amount
								# Calculate average rate after accumulation
								if aggregated_metal_labour_items[key]["qty"] > 0:
									aggregated_metal_labour_items[key]["rate"] = aggregated_metal_labour_items[key]["amount"] / aggregated_metal_labour_items[key]["qty"]
								else:
									aggregated_metal_labour_items[key]["rate"] = 0

								tax_rate_decimal = aggregated_metal_labour_items[key]["tax_rate"] / 100
								aggregated_metal_labour_items[key]["tax_amount"] += gemstone_amount * tax_rate_decimal

								aggregated_metal_labour_items[key]["amount_with_tax"] = (
									aggregated_metal_labour_items[key]["amount"] +
									aggregated_metal_labour_items[key]["tax_amount"]
								)
				for finding in bom_doc.finding_detail:
					if not finding.is_customer_item:
						finding_handled = False
						for e_item in e_invoice_items:
							if (e_item["is_for_finding"] and e_item["metal_type"] == finding.metal_type and e_item["metal_purity"] == finding.metal_touch and e_item["uom"] == finding.stock_uom and e_item["finding_category"] == finding.finding_category):
								finding_handled = True
								key = (e_item["item_type"], e_item["uom"])
								if key not in aggregated_finding_items:
									aggregated_finding_items[key] = {
										"item_code": e_item["item_type"],
										"item_name": e_item["item_type"],
										"uom": e_item["uom"],
										"qty": 0,
										"rate": 0,
										"amount": 0,
										"tax_rate": e_item["tax_rate"],
										"tax_amount": 0,
										"amount_with_tax": 0,
										"delivery_date": self.delivery_date
									}
								multiplied_qty = finding.quantity * item.qty
								making_amount = finding.making_amount
								finding_rate = 0 
								if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009":
									finding_rate = finding.se_rate 
								elif self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009":
									finding_rate = finding.se_rate
								elif self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009":
									finding_rate = finding.se_rate 
								finding_making_amount = (finding.rate * multiplied_qty)
								aggregated_finding_items[key]["qty"] += multiplied_qty
								aggregated_finding_items[key]["amount"] += finding_making_amount
								aggregated_finding_items[key]["rate"] = finding_rate
								
								tax_rate_decimal = aggregated_finding_items[key]["tax_rate"] / 100
								aggregated_finding_items[key]["tax_amount"] += finding_making_amount * tax_rate_decimal

								aggregated_finding_items[key]["amount_with_tax"] = (
									aggregated_finding_items[key]["amount"] +
									aggregated_finding_items[key]["tax_amount"]
								)
								break

						if not finding_handled:
							for e_item in e_invoice_items:
								if (e_item["is_for_metal"] and finding.metal_type == e_item["metal_type"] and finding.metal_touch == e_item["metal_purity"] and finding.stock_uom == e_item["uom"] and e_item["finding_category"] is None):
									key = (e_item["item_type"], e_item["uom"])
									if key not in aggregated_metal_items:
										aggregated_metal_items[key] = {
											"item_code": e_item["item_type"],
											"item_name": e_item["item_type"],
											"uom": e_item["uom"],
											"qty": 0,
											"amount": 0,
											"tax_rate": e_item["tax_rate"],
											"tax_amount": 0,
											"amount_with_tax": 0,
											"delivery_date": self.delivery_date,
											"rate": 0
										}
									
									finding_rate = finding.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else finding.rate
									multiplied_qty = finding.quantity * item.qty
									making_amount = finding.making_amount
									finding_making_amount = (finding.rate * multiplied_qty)
									
									aggregated_metal_items[key]["qty"] += multiplied_qty
									aggregated_metal_items[key]["amount"] += finding_making_amount
									aggregated_metal_items[key]["rate"] = finding_rate
									
									tax_rate_decimal = aggregated_metal_items[key]["tax_rate"] / 100
									aggregated_metal_items[key]["tax_amount"] += finding_making_amount * tax_rate_decimal
									aggregated_metal_items[key]["amount_with_tax"] = (
										aggregated_metal_items[key]["amount"] + 
										aggregated_metal_items[key]["tax_amount"]
									)
									break

						
						finding_making_handled = False
						for e_item in e_invoice_items:
							if (e_item["is_for_finding_making"] and e_item["metal_type"] == finding.metal_type and e_item["metal_purity"] == finding.metal_touch and e_item["uom"] == finding.stock_uom and e_item["finding_category"] == finding.finding_category):
								finding_making_handled = True
								key = (e_item["item_type"], e_item["uom"])
								if key not in aggregated_finding_making_items:
									aggregated_finding_making_items[key] = {
										"item_code": e_item["item_type"],
										"item_name": e_item["item_type"],
										"uom": e_item["uom"],
										"qty": 0,
										"rate": finding.making_rate,
										"amount": 0,
										"tax_rate": e_item["tax_rate"],
										"tax_amount": 0,
										"amount_with_tax": 0,
										"delivery_date": self.delivery_date
									}
								
								multiplied_qty = finding.quantity * item.qty
								making_amount = finding.making_amount
								finding_making_amount = (finding.making_rate * multiplied_qty)
								
								aggregated_finding_making_items[key]["qty"] += multiplied_qty
								aggregated_finding_making_items[key]["amount"] += finding_making_amount
								if aggregated_finding_making_items[key]["qty"] > 0:
									aggregated_finding_making_items[key]["rate"] = aggregated_finding_making_items[key]["amount"] / aggregated_finding_making_items[key]["qty"]
								else:
									aggregated_finding_making_items[key]["rate"] = 0
								
								tax_rate_decimal = aggregated_finding_making_items[key]["tax_rate"] / 100
								aggregated_finding_making_items[key]["tax_amount"] += finding_making_amount * tax_rate_decimal
								aggregated_finding_making_items[key]["amount_with_tax"] = (
									aggregated_finding_making_items[key]["amount"] +
									aggregated_finding_making_items[key]["tax_amount"]
								)
								break
						
						if not finding_making_handled:
							for e_item in e_invoice_items:
								if (e_item["is_for_making"] and e_item["metal_type"] == finding.metal_type and e_item["metal_purity"] == finding.metal_touch and e_item["uom"] == finding.stock_uom):
									key = (e_item["item_type"], e_item["uom"])
									if key not in aggregated_metal_making_items:
										aggregated_metal_making_items[key] = {
											"item_code": e_item["item_type"],
											"item_name": e_item["item_type"],
											"uom": e_item["uom"],
											"qty": 0,
											"rate": finding.making_rate,
											"amount": 0,
											"tax_rate": e_item["tax_rate"],
											"tax_amount": 0,
											"amount_with_tax": 0,
											"delivery_date": self.delivery_date
										}
									
									multiplied_qty = finding.quantity * item.qty
									making_amount = finding.making_amount
									finding_making_amount = (finding.making_rate * multiplied_qty)
									aggregated_metal_making_items[key]["qty"] += multiplied_qty
									aggregated_metal_making_items[key]["amount"] += finding_making_amount

									if aggregated_metal_making_items[key]["qty"] > 0:
										aggregated_metal_making_items[key]["rate"] = aggregated_metal_making_items[key]["amount"] / aggregated_metal_making_items[key]["qty"]
									else:
										aggregated_metal_making_items[key]["rate"] = 0
									
									tax_rate_decimal = aggregated_metal_making_items[key]["tax_rate"] / 100
									aggregated_metal_making_items[key]["tax_amount"] += finding_making_amount * tax_rate_decimal
									aggregated_metal_making_items[key]["amount_with_tax"] = (
										aggregated_metal_making_items[key]["amount"] +
										aggregated_metal_making_items[key]["tax_amount"]
									)
									break
					else:
						for e_item in e_invoice_items:
							if (e_item["is_for_labour"] ):
								key = (e_item["item_type"], e_item["uom"])
								if key not in aggregated_metal_labour_items:
									aggregated_metal_labour_items[key] = {
										"item_code": e_item["item_type"],
										"item_name": e_item["item_type"],
										"uom": e_item["uom"],
										"qty": 0,
										"rate": finding.making_rate,
										"amount": 0,
										"tax_rate": e_item["tax_rate"],
										"tax_amount": 0,
										"amount_with_tax": 0,
										"delivery_date": self.delivery_date
									}
								
								multiplied_qty = finding.quantity * item.qty
								making_amount = finding.making_amount
								finding_making_amount = (finding.making_rate * multiplied_qty)
								aggregated_metal_labour_items[key]["qty"] += multiplied_qty
								aggregated_metal_labour_items[key]["amount"] += finding_making_amount

								if aggregated_metal_labour_items[key]["qty"] > 0:
									aggregated_metal_labour_items[key]["rate"] = aggregated_metal_labour_items[key]["amount"] / aggregated_metal_labour_items[key]["qty"]
								else:
									aggregated_metal_labour_items[key]["rate"] = 0
								
								tax_rate_decimal = aggregated_metal_labour_items[key]["tax_rate"] / 100
								aggregated_metal_labour_items[key]["tax_amount"] += finding_making_amount * tax_rate_decimal
								aggregated_metal_labour_items[key]["amount_with_tax"] = (
									aggregated_metal_labour_items[key]["amount"] +
									aggregated_metal_labour_items[key]["tax_amount"]
								)
								break

		
		# After aggregation, calculate average rate = total amount / total qty per key
		for key, val in aggregated_metal_items.items():
			if val["qty"] > 0:
				
				average_rate = val["amount"] / val["qty"]
			else:
				average_rate = 0
			val["rate"] = average_rate
			self.append("custom_invoice_item", val)

		for key, val in aggregated_hallmarking_items.items():
			val["rate"] = val["amount"] / val["qty"] if val["qty"] else 0
			self.append("custom_invoice_item", val)
		

		for key, val in aggregated_metal_labour_items.items():
			val["rate"] = val["amount"] / val["qty"] if val["qty"] else 0
			val["qty"] = round(val["qty"],2)
			self.append("custom_invoice_item", val)
		
		for key, val in aggregated_metal_making_items.items():
			val["rate"] = val["amount"] / val["qty"] if val["qty"] else 0
			self.append("custom_invoice_item", val)
		
		for key, val in aggregated_diamond_items.items():
			val["rate"] = val["amount"] / val["qty"] if val["qty"] else 0
			self.append("custom_invoice_item", val)

		for key, val in aggregated_gemstone_items.items():
			val["rate"] = val["amount"] / val["qty"] if val["qty"] else 0
			self.append("custom_invoice_item", val)
		
		for key, val in aggregated_finding_items.items():
			val["rate"] = val["amount"] / val["qty"] if val["qty"] else 0
			self.append("custom_invoice_item", val)

	
		for key, val in aggregated_finding_making_items.items():
			val["rate"] = val["amount"] / val["qty"] if val["qty"] else 0
			self.append("custom_invoice_item", val)




def validate_quotation_item(self):
	if not self.custom_invoice_item:
		for row in self.items:
			if row.prevdoc_docname:
				quotation_id = row.prevdoc_docname
				invoice_items = frappe.get_all(
					'Quotation E Invoice Item',
					filters={'parent': quotation_id},  
					fields=['item_code', 'item_name', 'uom', 'qty', 'rate', 'amount']
				)
				if invoice_items:
					for invoice_item in invoice_items:
						self.append('custom_invoice_item', {
							'item_code': invoice_item.item_code,
							'item_name': invoice_item.item_name,
							'uom': invoice_item.uom,
							'qty': invoice_item.qty,
							'rate': invoice_item.rate,
							'amount': invoice_item.amount
						})



def validate_sales_type(self):
	for r in self.items:
	# 	if r.prevdoc_docname:
	# 		quotation_sales_type = frappe.db.get_value('Quotation', r.prevdoc_docname, 'custom_sales_type')
	# 		if quotation_sales_type:  
	# 			self.sales_type = quotation_sales_type
	# 	if self.company == "Gurukrupa Export Private Limited":
	# 		# Throw only if BOTH are missing
		if not r.prevdoc_docname and not r.custom_customer_approval:
			pass
			# frappe.msgprint(
			# 	_("Row {0} : Sales Order can be created only from Quotation or Customer Approval for this Company").format(r.idx)
			# )
	if not self.sales_type :
		frappe.throw("Sales Type is mandatory.")
	# if not self.gold_rate_with_gst and self.company != 'Sadguru Diamond':
	# 	frappe.throw("Metal rate  with GST is mandatory.")



import json
@frappe.whitelist()

def make_sales_order_batch(sales_orders, target_doc=None):

	if isinstance(sales_orders, str):
		sales_orders = json.loads(sales_orders)

	if target_doc:
		if isinstance(target_doc, str):
			target_doc = json.loads(target_doc)

		target_doc = frappe.get_doc(target_doc)
	else:
		target_doc = frappe.new_doc("Sales Order")


	target_doc.items = []

	for so_name in sales_orders:
		so = frappe.db.get_value("Sales Order", so_name, "*", as_dict=True)
		if not so:
			continue
		target_doc.custom_diamond_quality = so.custom_diamond_quality
		target_doc.order_type = so.order_type
		target_doc.custom_parent_sales_order = so.name
		items = frappe.get_all(
			"Sales Order Item",
			filters={"parent": so_name},
			fields="*"
		)
		
		
		
		for it in items:
			snc_list = frappe.db.get_list("Serial Number Creator", 
				filters={"sales_order_id": so_name}, 
				fields=["name"]
			)
			
			stock_entries = []
			for snc in snc_list:
				stock_entry = frappe.db.get_value("Stock Entry", 
					{"custom_serial_number_creator": snc.name}, "name")
				if stock_entry:
					stock_entries.append(stock_entry)
			
			available_serials = []
			for stock_entry in stock_entries:
				serial_no = frappe.db.sql(f"""
					SELECT sed.serial_no, sed.item_code
					FROM `tabStock Entry Detail` sed
					WHERE sed.parent = '{stock_entry}'
					AND sed.item_code = '{it.item_code}'
					ORDER BY sed.idx DESC
					LIMIT 1
				""", as_dict=1)
				
				if serial_no and serial_no[0]['item_code'] == it.item_code:
					available_serials.append(serial_no[0]['serial_no'])
			
			if not available_serials:
				continue
			
			serial_count = 0
			for s_no in available_serials:
				if serial_count < it.qty:
					target_doc.append("items", {
						"item_code": it.item_code,
						"item_name": it.item_name,
						"serial_no": s_no,
						"bom": frappe.db.get_value("Serial No", s_no, "custom_bom_no"),
						"diamond_quality": so.custom_diamond_quality,
						"description": it.description,
						"qty": 1,
						"rate": it.rate,
						"warehouse": it.warehouse,
						"against_sales_order": so_name,
						"uom": it.uom
					})
					serial_count += 1
				else:
					break


	first_so = frappe.db.get_value("Sales Order", sales_orders[0], "*", as_dict=True)


	return target_doc
