# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.query_builder import DocType
from frappe.utils import get_link_to_form


class SketchOrderForm(Document):
	def on_submit(self):
		create_sketch_order(self)
		if self.supplier:
			create_po(self)

	# def on_cancel(self):
	# 	delete_auto_created_sketch_order(self)
	# 	frappe.db.set_value("Sketch Order Form", self.name, "workflow_state", "Cancelled")

	def validate(self):
		self.validate_category_subcaegory()

	def validate_category_subcaegory(self):
		tablename = "order_details"
		for row in self.get(tablename):
			if row.subcategory:
				parent = frappe.db.get_value("Attribute Value", row.subcategory, "parent_attribute_value")
				if row.category != parent:
					# frappe.throw(_(f"Category & Sub Category mismatched in row #{row.idx}"))
					frappe.throw(_("Category & Sub Category mismatched in row #{0}").format(row.idx))


def create_sketch_order(self):
	# if self.design_by in ["Customer Design", "Concept by Designer"]:
	# 	order_details = self.order_details
	# 	doctype = "Sketch Order Form Detail"
	# else:
	# 	return
	order_details = self.order_details
	doctype = "Sketch Order Form Detail"
	doclist = []
	for row in order_details:
		docname = make_sketch_order(doctype, row.name, self)
		doclist.append(get_link_to_form("Sketch Order", docname))

	if doclist:
		msg = _("The following {0} were created: {1}").format(
			frappe.bold(_("Sketch Orders")), "<br>" + ", ".join(doclist)
		)
		frappe.msgprint(msg)


def delete_auto_created_sketch_order(self):
	for row in frappe.get_all("Sketch Order", filters={"sketch_order_form": self.name}):
		frappe.delete_doc("Sketch Order", row.name)


def make_sketch_order(doctype, source_name, parent_doc=None, target_doc=None):
	def set_missing_values(source, target):
		target.sketch_order_form_detail = source.name
		target.sketch_order_form = source.parent
		target.sketch_order_form_index = source.idx
		set_fields_from_parent(source, target)

	def set_fields_from_parent(source, target, parent=parent_doc):
		target.company = parent.company
		target.remark = parent.remarks

		# new code start
		target.age_group = parent.age_group
		target.alphabetnumber = parent.alphabetnumber
		target.animalbirds = parent.animalbirds
		target.collection_1 = parent.collection_1
		target.design_style = parent.design_style
		target.gender = parent.gender
		target.lines_rows = parent.lines_rows
		target.language = parent.language
		target.occasion = parent.occasion
		target.religious = parent.religious
		target.shapes = parent.shapes
		target.zodiac = parent.zodiac
		target.rhodium = parent.rhodium
		# nwe code end

		# target.stepping = parent.stepping
		# target.fusion = parent.fusion
		# target.drops = parent.drops
		# target.coin = parent.coin
		# target.gold_wire = parent.gold_wire
		# target.gold_ball = parent.gold_ball
		# target.flows = parent.flows
		# target.nagas = parent.nagas

		target.india = parent.india
		target.india_states = parent.india_states
		target.usa = parent.usa
		target.usa_states = parent.usa_states
		# if parent_doc.design_by == "Concept by Designer":
		# 	fields = [
		# 		"market",
		# 		"age",
		# 		"gender",
		# 		"function",
		# 		"concept_type",
		# 		"nature",
		# 		"setting_style",
		# 		"animal",
		# 		"god",
		# 		"temple",
		# 		"birds",
		# 		"shape",
		# 		"creativity_type",
		# 		"stepping",
		# 		"fusion",
		# 		"drops",
		# 		"coin",
		# 		"gold_wire",
		# 		"gold_ball",
		# 		"flows",
		# 		"nagas",
		# 	]
		# 	for field in fields:
		# 		target.set(field, parent_doc.get(field))

	doc = get_mapped_doc(
		doctype,
		source_name,
		{doctype: {"doctype": "Sketch Order"}},
		target_doc,
		set_missing_values,
	)

	doc.save()
	return doc.name


@frappe.whitelist()
def get_customer_orderType(customer_code):
	OrderType = DocType("Order Type")
	order_type = (
		frappe.qb.from_(OrderType)
		.select(OrderType.order_type)
		.where(OrderType.parent == customer_code)
		.run(as_dict=True)
	)

	return order_type


def create_po(self):
	total_qty = 0
	for i in self.order_details:
		total_qty += i.qty
	po_doc = frappe.new_doc("Purchase Order")
	po_doc.supplier = self.supplier
	po_doc.company = self.company
	po_doc.branch = self.branch
	po_doc.project = self.project
	po_doc.custom_form = "Sketch Order Form"
	po_doc.custom_form_id = self.name
	po_doc.purchase_type = "Subcontracting"
	po_doc.custom_sketch_order_form = self.name
	po_doc.schedule_date = self.delivery_date
	po_item_log = po_doc.append("items", {})
	po_item_log.item_code = "Design Expness"
	po_item_log.schedule_date = self.delivery_date
	po_item_log.qty = total_qty
	po_doc.save()
	po_name = po_doc.name
	msg = _("The following {0} is created: {1}").format(
		frappe.bold(_("Purchase Order")), "<br>" + get_link_to_form("Purchase Order", po_name)
	)
	frappe.msgprint(msg)
