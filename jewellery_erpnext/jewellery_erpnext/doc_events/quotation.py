import json

import frappe
from frappe import _
from frappe.model.mapper import get_mapped_doc

from jewellery_erpnext.jewellery_erpnext.doc_events.bom_utils import (
	calculate_gst_rate,
	set_bom_item_details,
	set_bom_rate_in_quotation,
)


@frappe.whitelist()
def update_status(quotation_id):
	status = frappe.db.get_value("Quotation", quotation_id, "status")
	if status != "Closed":
		frappe.db.set_value("Quotation", quotation_id, "status", "Closed")
	else:
		frappe.db.set_value("Quotation", quotation_id, "status", "Open")


def validate(self, method):
	create_new_bom(self)
	if self.docstatus == 0:
		calculate_gst_rate(self)
		if not self.get("__islocal"):
			set_bom_item_details(self)
		set_bom_rate_in_quotation(self)


def onload(self, method):
	return


def on_submit(self, method):
	submit_bom(self)


def on_cancel(self, method):
	cancel_bom(self)


def create_new_bom(self):
	"""
	Create Quotation Type BOM from Template/ Finished Goods Bom
	"""
	for row in self.items:
		if row.quotation_bom:
			continue
		serial_bom = None
		if serial := row.get("serial_no"):
			serial_bom = frappe.db.get_value("BOM", {"item": row.item_code, "tag_no": serial}, "name")
		if serial_bom:
			bom = serial_bom
		# if Finished Goods BOM for an item is already present for item Copy from FINISHED GOODS BOM
		elif fg_bom := frappe.db.get_value(
			"BOM",
			{"item": row.item_code, "is_active": 1, "docstatus": 1, "bom_type": "Finished Goods"},
			"name",
			order_by="creation asc",
		):
			bom = fg_bom
		# if Finished Goods BOM for an item not present for item Copy from TEMPLATE BOM
		elif temp_bom := frappe.db.get_value(
			"BOM",
			{"item": row.item_code, "is_active": 1, "bom_type": "Template"},
			"name",
			order_by="creation asc",
		):
			bom = temp_bom
		else:
			bom = None
		if bom:
			create_quotation_bom(self, row, bom)


def create_quotation_bom(self, row, bom):
	row.copy_bom = bom
	doc = get_mapped_doc(
		"BOM",
		{"name": bom},
		{
			"BOM": {
				"doctype": "BOM",
			}
		},
		ignore_permissions=True,
	)
	doc.custom_creation_doctype = self.doctype
	doc.is_default = 0
	doc.is_active = 0
	doc.bom_type = "Quotation"
	doc.gold_rate_with_gst = self.gold_rate_with_gst
	doc.customer = self.party_name
	doc.selling_price_list = self.selling_price_list
	metal_criteria = (
		frappe.get_list(
			"Metal Criteria",
			{"parent": doc.customer},
			["metal_touch", "metal_purity"],
			ignore_permissions=1,
		)
		or {}
	)
	metal_criteria = {row.metal_touch: row.metal_purity for row in metal_criteria}
	for item in doc.metal_detail:
		if row.custom_customer_gold == "Yes":
			item.is_customer_item = 1
		if item.metal_touch:
			item.metal_purity = metal_criteria.get(item.metal_touch)
	for item in doc.finding_detail:
		if row.custom_customer_gold == "Yes":
			item.is_customer_item = 1
		if item.metal_touch:
			item.metal_purity = metal_criteria.get(item.metal_touch)

	# doc.save(ignore_permissions = True) # This Save will Call before_save and validate method in BOM
	for diamond in doc.diamond_detail:
		if row.custom_customer_diamond == "Yes":
			diamond.is_customer_item = 1
		diamond_grade_1 = frappe.db.get_value(
			"Customer Diamond Grade",
			{"parent": doc.customer, "diamond_quality": row.diamond_quality},
			"diamond_grade_1",
		)

		if diamond_grade_1:
			diamond.diamond_grade = diamond_grade_1
		if row.diamond_quality:
			diamond.quality = row.diamond_quality

	for gem in doc.gemstone_detail:
		if row.custom_customer_stone == "Yes":
			gem.is_customer_item = 1

	for other in doc.other_detail:
		if row.custom_customer_good == "Yes":
			other.is_customer_item = 1

	# This Save will Call before_save and validate method in BOM and Rates Will be Calculated as diamond_quality is calculated too
	doc.save(ignore_permissions=True)
	doc.db_set("custom_creation_docname", self.name)
	row.quotation_bom = doc.name
	row.gold_bom_rate = doc.gold_bom_amount
	row.diamond_bom_rate = doc.diamond_bom_amount
	row.gemstone_bom_rate = doc.gemstone_bom_amount
	row.other_bom_rate = doc.other_bom_amount
	row.making_charge = doc.making_charge
	row.bom_rate = doc.total_bom_amount
	row.rate = doc.total_bom_amount
	self.total = doc.total_bom_amount


def submit_bom(self):
	for row in self.items:
		if row.quotation_bom:
			bom = frappe.get_doc("BOM", row.quotation_bom)
			bom.submit()


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
	for d in metal_data:
		update_table(parent, "BOM Metal Detail", "metal_detail", d)


def set_diamond_detail(parent, diamond_detail):
	diamond_data = json.loads(diamond_detail)
	for d in diamond_data:
		update_table(parent, "BOM Diamond Detail", "diamond_detail", d)


def set_gemstone_detail(parent, gemstone_detail):
	gemstone_data = json.loads(gemstone_detail)
	for d in gemstone_data:
		update_table(parent, "BOM Gemstone Detail", "gemstone_detail", d)


def set_finding_detail(parent, finding_detail):
	finding_data = json.loads(finding_detail)
	for d in finding_data:
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
