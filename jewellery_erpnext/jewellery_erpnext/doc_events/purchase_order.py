import json

import frappe
from erpnext.setup.utils import get_exchange_rate
from frappe.utils import flt
# from jewellery_erpnext.utils import update_existing


def validate(self, method):
	update_rate(self)
	set_gst_details(self)
	self.calculate_taxes_and_totals()
def set_gst_details(self):
    
    if self.purchase_type not in ("Finished Goods", "Subcontracting","Branch Purchase"):
        return
    
    customer_state = frappe.db.get_value("Address", self.supplier_address, "gst_state_number")
    company_state  = frappe.db.get_value("Address", self.company_address,  "gst_state_number")

    if not customer_state or not company_state:
        return

    self.tax_category = "In-State" if customer_state == company_state else "Out-State"

    item_template_map = {
        "Finished Goods": {
            "Gurukrupa Export Private Limited": "GST 3% - GEPL",
            "KG GK Jewellers Private Limited":  "GST 3% - KGJPL",
        },
        "Subcontracting": {
            "Gurukrupa Export Private Limited": "GST 5% - GEPL",
            "KG GK Jewellers Private Limited":  "GST 5% - KGJPL",
        },
        "Branch Purchase": {
            "Gurukrupa Export Private Limited": "GST 3% - GEPL",
            "KG GK Jewellers Private Limited":  "GST 3% - KGJPL",
        }
    }
    item_tax_template = item_template_map.get(self.purchase_type, {}).get(self.company)
    
    if not item_tax_template:
        return

    taxes_and_charges = frappe.db.get_value(
        "Purchase Taxes and Charges Template",
        {
            "company":      self.company,
            "tax_category": self.tax_category,
            "disabled":     0,
        },
        "name"
    )
    self.taxes_and_charges=taxes_and_charges
    if not taxes_and_charges:
        frappe.log_error(
            f"No Sales Taxes and Charges Template found for "
            f"Company: {self.company}, Tax Category: {self.tax_category}",
            "set_gst_details"
        )
        return
    # frappe.throw(f"{taxes_and_charges}")
    self.taxes_and_charges = taxes_and_charges

    template_rates = frappe.get_all(
        "Item Tax Template Detail",
        filters={"parent": item_tax_template},
        fields=["tax_type", "tax_rate"]
    )

    cgst_rate = sgst_rate = igst_rate = 0.0
    for r in template_rates:
        tax_type = r.tax_type or ""
        if "Output" not in tax_type or "RCM" in tax_type:
            continue
        if "CGST" in tax_type:
            cgst_rate = float(r.tax_rate)
        elif "SGST" in tax_type:
            sgst_rate = float(r.tax_rate)
        elif "IGST" in tax_type:
            igst_rate = float(r.tax_rate)

    account_rate_map = {}
    for r in template_rates:
        tax_type = r.tax_type or ""
        if "Output" not in tax_type or "RCM" in tax_type:
            continue
        account_rate_map[r.tax_type] = float(r.tax_rate)

    self.taxes = []
    
    tax_rows = frappe.get_all(
        "Purchase Taxes and Charges",
        filters={"parent": self.taxes_and_charges},
        fields=["charge_type", "account_head", "description", "rate", "cost_center"],
        order_by="idx asc"
    )
    import json
    # frappe.throw(f"hii{tax_rows}")
    item_tax_rate = {}

    if self.items and self.items[0].item_tax_rate:
        item_tax_rate = json.loads(self.items[0].item_tax_rate)
    for t in tax_rows:
        # correct_rate = account_rate_map.get(t.account_head, t.rate)
        t.rate = flt(item_tax_rate.get(t.account_head, t.rate))
        
        # correct_rate = flt(self.items[0].item_tax_rate.get(t.account_head, t.rate))
        # frappe.throw(f"{t.rate}")
        self.append("taxes", {
            "charge_type":  t.charge_type,
            "account_head": t.account_head,
            "description":  t.description,
            "rate":         t.rate,
            "cost_center":  t.cost_center,
            "tax_amount": self.total * t.rate / 100,
            "total":(self.total * t.rate / 100)+self.total,
            "category":'Total',
            "add_deduct_tax":'Add'
        })
        self.total_taxes_and_charges = (self.total * t.rate / 100)+self.total
        
        self.grand_total = self.total_taxes_and_charges
    for item in self.items:
        if not item.item_code:
            continue

        item.item_tax_template = item_tax_template
        item.gst_treatment     = "Taxable"
        item.cgst_rate         = 0.0
        item.sgst_rate         = 0.0
        item.igst_rate         = 0.0
        item.cgst_amount       = 0.0
        item.sgst_amount       = 0.0
        item.igst_amount       = 0.0

        taxable_value = float(item.taxable_value)

        if self.tax_category == "In-State":
            item.cgst_rate   = cgst_rate
            item.sgst_rate   = sgst_rate
            item.cgst_amount = float(taxable_value * cgst_rate / 100, 2)
            item.sgst_amount = float(taxable_value * sgst_rate / 100, 2)
        else:
            item.igst_rate   = igst_rate
            item.igst_amount = flt(taxable_value * igst_rate / 100, 2)

def update_rate(self):
	if self.purchase_type == "FG Purchase" and not self.is_new():
		bom_data = frappe._dict()
		for row in self.items:
			if row.manufacturing_bom:
				# confirm with Rajnibhai on 8 Jan 2025
				# bom_doc = frappe.get_doc("BOM", row.manufacturing_bom)
				# bom_doc.gold_rate_with_gst = self.gold_rate_with_gst
				# bom_doc.validate()
				# bom_doc.save()

				field = [
					"making_fg_purchase",
					"finding_bom_amount",
					"diamond_fg_purchase",
					"gemstone_fg_purchase",
					"certification_amount",
					"freight_amount",
					"hallmarking_amount",
					"custom_duty_amount",
				]

				if not bom_data.get(row.manufacturing_bom):
					bom_data[row.manufacturing_bom] = frappe.db.get_value(
						"BOM", row.manufacturing_bom, field, as_dict=1
					)
				bom_doc = bom_data.get(row.manufacturing_bom)
				row.making_amount = bom_doc.making_fg_purchase
				row.finding_amount = bom_doc.finding_bom_amount
				row.diamond_amount = bom_doc.diamond_fg_purchase
				row.gemstone_amount = bom_doc.gemstone_fg_purchase
				row.custom_certification_amount = bom_doc.certification_amount
				row.custom_freight_amount = bom_doc.freight_amount
				row.custom_hallmarking_amount = bom_doc.hallmarking_amount
				row.custom_custom_duty_amount = bom_doc.custom_duty_amount

				row.rate = (
					row.metal_amount
					+ row.making_amount
					+ row.finding_amount
					+ row.diamond_amount
					+ row.gemstone_amount
					+ row.custom_certification_amount
					+ row.custom_freight_amount
					+ row.custom_hallmarking_amount
					+ row.custom_custom_duty_amount
				)


def make_subcontracting_order(doc):
	default_item = frappe.db.get_single_value("Jewellery Settings", "service_item")
	supplier_wise_items = frappe._dict()
	for row in doc.manufacturing_plan_table:
		if not supplier_wise_items.get(row.supplier):
			supplier_wise_items.setdefault(row.supplier, {"items": []})

		supplier_wise_items[row.supplier]["ref_customer"] = row.get("customer", None)
		supplier_wise_items[row.supplier]["purchase_type"] = row.purchase_type
		supplier_wise_items[row.supplier]["custom_customer_po"] = row.customer_po

		if row.purchase_type == "FG Purchase":
			supplier_wise_items[row.supplier]["items"].append(
				{
					"item_code": (row.item_code),
					"qty": row.subcontracting_qty,
					"manufacturing_bom": row.manufacturing_bom,
					"diamond_quality": row.diamond_quality,
					"custom_manufacturing_plan": doc.name,
					"custom_m_plan_details": row.name,
					"custom_child_po_no": row.child_po,
				}
			)
		else:
			supplier_wise_items[row.supplier]["is_subcontracted"] = 1
			supplier_wise_items[row.supplier]["schedule_date"] = row.estimated_delivery_date
			supplier_wise_items[row.supplier]["items"].append(
				{
					"item_code": default_item,
					"qty": 1,
					"fg_item": row.item_code,
					"fg_item_qty": row.subcontracting_qty,
					"schedule_date": row.estimated_delivery_date,
					"custom_child_po_no": row.child_po,
					"custom_m_plan_details": row.name,
				}
			)

	for row in supplier_wise_items:
		po_doc = frappe.new_doc("Purchase Order")
		po_doc.supplier = row
		po_doc.company = doc.company
		po_doc.schedule_date = supplier_wise_items[row].get("schedule_date") or po_doc.transaction_date
		po_doc.purchase_type = supplier_wise_items[row].get("purchase_type")
		po_doc.ref_customer = supplier_wise_items[row].get("ref_customer")
		po_doc.manufacturing_plan = doc.name
		po_doc.custom_customer_po = supplier_wise_items[row].get("custom_customer_po")
		po_doc.is_subcontracted = supplier_wise_items[row].get("is_subcontracted")
		for item in supplier_wise_items[row]["items"]:
			po_doc.append("items", item)
		po_doc.save()


def on_cancel(doc, method=None):
	pass
	# update_existing("Manufacturing Plan Table", doc.rowname, "manufacturing_order_qty", f"manufacturing_order_qty - {doc.qty}")
	# update_existing("Sales Order Item", doc.sales_order_item, "manufacturing_order_qty", f"manufacturing_order_qty - {doc.qty}")


@frappe.whitelist()
def make_quotation(source_name, target_doc=None):
	def set_missing_values(source, target):
		from erpnext.controllers.accounts_controller import get_default_taxes_and_charges

		quotation = frappe.get_doc(target)
		company_currency = frappe.get_cached_value("Company", quotation.company, "default_currency")
		customer = frappe.db.get_value("Company", source.company, "customer_code")

		target.party_name = customer

		if company_currency == quotation.currency:
			exchange_rate = 1
		else:
			exchange_rate = get_exchange_rate(
				quotation.currency, company_currency, quotation.transaction_date, args="for_selling"
			)
		quotation.conversion_rate = exchange_rate
		# get default taxes
		taxes = get_default_taxes_and_charges(
			"Sales Taxes and Charges Template", company=quotation.company
		)
		if taxes.get("taxes"):
			quotation.update(taxes)
		quotation.run_method("set_missing_values")
		quotation.run_method("calculate_taxes_and_totals")

		quotation.quotation_to = "Customer"
		field_map = {
			"transaction_date": "transaction_date",
			"ref_customer": "ref_customer",
		}
		for target_field, source_field in field_map.items():
			quotation.set(target_field, source.get(source_field))

	if isinstance(target_doc, str):
		target_doc = json.loads(target_doc)
	if not target_doc:
		target_doc = frappe.new_doc("Quotation")
	else:
		target_doc = frappe.get_doc(target_doc)

	po_doc = frappe.get_doc("Purchase Order", source_name)

	target_doc.po_no = po_doc.custom_customer_po

	for row in po_doc.items:
		target_doc.append(
			"items",
			{
				"branch": row.get("branch"),
				"project": row.get("project"),
				"item_code": row.get("item_code"),
				"qty": row.get("qty"),
				"diamond_quality": row.get("diamond_quality"),
				"custom_customer_gold": "No",
				"rate": row.get("rate"),
				"custom_customer_diamond": "No",
				"custom_customer_stone": "No",
				"custom_customer_good": "No",
				"po_no": po_doc.get("name"),
				"custom_po_details": row.name,
			},
		)
	set_missing_values(po_doc, target_doc)

	return target_doc
