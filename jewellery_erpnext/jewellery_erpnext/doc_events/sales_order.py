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
	validate_quotation_item(self)
	validate_items(self)
	create_new_bom(self)
	validate_serial_number(self)
	# calculate_gst_rate(self)
	if not self.get("__islocal") and self.docstatus == 0:
		set_bom_item_details(self)


def on_submit(self, method):
	# submit_bom(self)
	create_branch_so(self)
	validate_snc(self)


def on_cancel(self, method):
	cancel_bom(self)
	validate_snc(self)


def create_new_bom(self):
	"""
	This Function Creates Sales Order Type BOM from Quotation Bom
	"""
	# diamond_grade_data = frappe._dict()
	for row in self.items:
		if not row.quotation_bom:
			create_serial_no_bom(self, row)
			if row.bom:
				doc = frappe.get_doc("BOM",row.bom)
				row.gold_bom_rate = doc.gold_bom_amount
				row.diamond_bom_rate = doc.diamond_bom_amount
				row.gemstone_bom_rate = doc.gemstone_bom_amount
				row.other_bom_rate = doc.other_bom_amount
				row.making_charge = doc.making_charge
				row.bom_rate = doc.total_bom_amount
				row.rate = doc.total_bom_amount
		elif not row.bom and frappe.db.exists("BOM", row.quotation_bom):
			row.bom = row.quotation_bom
			
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
	if row.bom:
		return
	serial_no_bom = frappe.db.get_value("Serial No", row.serial_no, "custom_bom_no")
	if not serial_no_bom:
		return
	bom_doc = frappe.get_doc("BOM", serial_no_bom)
	if self.customer != bom_doc.customer:
		doc = frappe.copy_doc(bom_doc)
		doc.customer = self.customer
		doc.gold_rate_with_gst = self.gold_rate_with_gst
		doc.save(ignore_permissions=True)
		row.bom = doc.name


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
			# frappe.throw(f"{existing}")
			if existing:
				so_name = existing[0].parent
				frappe.throw(f"Serial No {row.serial_no} is already used in submitted Sales Order {so_name}.")

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

def validate_items(self):
	# if self.sales_type == "Finished Goods":
	# Get the Customer Payment Terms document for the given customer
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
		for row in e_invoice_item.sales_type_table:
			if row.sales_type == self.sales_type:
				matched_sales_type_row = row
				break

		# Skip item if no matching sales_type and custom_sales_type is set
		if self.sales_type and not matched_sales_type_row:
			continue
		e_invoice_items.append({
			"item_type": item_type,
			"is_for_metal": e_invoice_item.is_for_metal,
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
	aggregated_metal_making_items = {}
	aggregated_diamond_items = {}
	aggregated_finding_items = {}
	aggregated_finding_making_items = {}
	aggregated_gemstone_items = {}

	for item in self.items:
		if item.bom:
			bom_doc = frappe.get_doc("BOM", item.bom)
			for metal in bom_doc.metal_detail:
				for e_item in e_invoice_items:
					# frappe.throw(f"Matching E-Invoice Item Found: {e_invoice_items}")
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
						# multiplied_qty = metal.quantity * item.qty
						# metal_rate = metal.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else metal.rate
						# metal_amount = metal_rate * multiplied_qty
						# aggregated_metal_items[key]["rate"] = metal_rate
						# # metal_amount =  metal.rate * multiplied_qty
						# aggregated_metal_items[key]["qty"] += multiplied_qty
						# aggregated_metal_items[key]["amount"] += metal_amount
						multiplied_qty = metal.quantity * item.qty
						metal_rate = metal.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else metal.rate
						metal_amount = metal_rate * multiplied_qty

						# Update quantity and amount
						aggregated_metal_items[key]["qty"] += multiplied_qty
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

			for making in bom_doc.metal_detail:
				for e_item in e_invoice_items:
					# frappe.throw(f"Matching E-Invoice Item Found: {e_invoice_items}")
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
						# multiplied_qty = making.quantity * item.qty
						# metal_making_amount = making.making_rate * multiplied_qty
						# aggregated_metal_making_items[key]["qty"] += multiplied_qty
						# aggregated_metal_making_items[key]["amount"] += metal_making_amount

						multiplied_qty = making.quantity * item.qty
						metal_making_amount = making.making_rate * multiplied_qty

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
						aggregated_metal_making_items[key]["delivery_date"] = self.delivery_date


			for diamond in bom_doc.diamond_detail:
				for e_item in e_invoice_items:
					# frappe.throw(f"{diamond.stock_uom}")
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
							
						# multiplied_qty = diamond.quantity * item.qty	
						# diamond_rate = diamond.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else diamond.total_diamond_rate
						# diamond_amount = diamond_rate * multiplied_qty
						# aggregated_diamond_items[key]["qty"] += multiplied_qty
						# aggregated_diamond_items[key]["rate"] = diamond_rate  
						# aggregated_diamond_items[key]["amount"] += diamond_amount
						multiplied_qty = diamond.quantity * item.qty
						diamond_rate = diamond.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else diamond.total_diamond_rate
						diamond_amount = diamond_rate * multiplied_qty

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
							# multiplied_qty = finding.quantity * item.qty
							# finding_rate = finding.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else finding.rate
							# finding_amount = finding_rate * multiplied_qty
							# aggregated_finding_items[key]["qty"] += multiplied_qty
							# aggregated_finding_items[key]["rate"] = finding_rate 
							# aggregated_finding_items[key]["amount"] += finding_amount
							multiplied_qty = finding.quantity * item.qty
							finding_rate = finding.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else finding.rate
							finding_making_amount = finding_rate * multiplied_qty

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
							# multiplied_qty = finding_making.quantity * item.qty
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
							aggregated_finding_making_items[key]["delivery_date"] = self.delivery_date
								# Gemstone aggregation
			for gemstone in bom_doc.gemstone_detail:	
				for e_item in e_invoice_items:
					# frappe.throw(f"{gemstone.stock_uom}")
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
						# gemstone_rate = gemstone.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else gemstone.total_gemstone_rate
						# gemstone_amount = gemstone_rate * multiplied_qty
						# aggregated_gemstone_items[key]["qty"] += multiplied_qty
						# aggregated_gemstone_items[key]["rate"] = gemstone_rate  
						# aggregated_gemstone_items[key]["amount"] += gemstone_amount
						multiplied_qty = gemstone.quantity * item.qty
						gemstone_rate = gemstone.se_rate if self.company == "KG GK Jewellers Private Limited" and self.customer == "GJCU0009" else gemstone.total_gemstone_rate
						gemstone_amount = gemstone_rate * multiplied_qty

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
						aggregated_gemstone_items[key]["delivery_date"] = self.delivery_date

	for item in aggregated_diamond_items.values():
		self.append("custom_invoice_item", item)

	for item in aggregated_metal_items.values():
		self.append("custom_invoice_item", item)

	for item in aggregated_metal_making_items.values():
		self.append("custom_invoice_item", item)

	for item in aggregated_finding_items.values():
		self.append("custom_invoice_item", item)

	
	for item in aggregated_finding_making_items.values():
		self.append("custom_invoice_item", item)

	for item in aggregated_gemstone_items.values():
			self.append("custom_invoice_item", item)

	
		

		
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
		if r.prevdoc_docname:
			quotation_sales_type = frappe.db.get_value('Quotation', r.prevdoc_docname, 'custom_sales_type')
			if quotation_sales_type:  
				self.sales_type = quotation_sales_type
	if not self.sales_type :
		frappe.throw("Sales Type is mandatory.")
	if not self.gold_rate_with_gst:
		frappe.throw("Metal rate  with GST is mandatory.")



