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
	row.db_set("copy_bom", bom)
	doc = frappe.copy_doc(frappe.get_doc("BOM", bom))
	doc.custom_creation_doctype = self.doctype
	doc.custom_creation_docname = self.name
	doc.is_default = 0
	doc.is_active = 0
	doc.bom_type = "Quotation"
	doc.gold_rate_with_gst = self.gold_rate_with_gst
	doc.customer = self.party_name
	doc.selling_price_list = self.selling_price_list
	doc.hallmarking_amount = row.custom_hallmarking_amount

	if not attribute_data:
		attribute_data = frappe.db.get_all(
			"Attribute Value", {"custom_consider_as_gold_item": 1}, pluck="name"
		)

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
	# for item in doc.finding_detail:
	# 	if row.custom_customer_gold == "Yes":
	# 		item.is_customer_item = 1
	# 	if item.metal_touch:
	# 		item.metal_purity = metal_criteria.get(item.metal_touch)

	# doc.save(ignore_permissions = True) # This Save will Call before_save and validate method in BOM
	for diamond in doc.diamond_detail:
		if row.custom_customer_diamond == "Yes":
			diamond.is_customer_item = 1
		# if not diamond_grade_data.get(row.diamond_quality):
		# 	diamond_grade_data[row.diamond_quality] = frappe.db.get_value(
		# 		"Customer Diamond Grade",
		# 		{"parent": doc.customer, "diamond_quality": row.diamond_quality},
		# 		"diamond_grade_1",
		# 	)

		# diamond.diamond_grade = diamond_grade_data.get(row.diamond_quality)

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
	# self.total = doc.total_bom_amount


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
