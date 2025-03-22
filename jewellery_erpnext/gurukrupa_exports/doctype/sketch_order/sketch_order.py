# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.utils import get_link_to_form


class SketchOrder(Document):
	def validate(self):
		populate_child_table(self)

	def on_submit(self):
		self.make_items()

	def make_items(self):
		# if self.workflow_state == "Items Updated":
		if self.order_type != 'Purchase':
			for row in self.final_sketch_approval_cmo:
				# if row.item or not (row.design_status == "Approved" and row.design_status_cpo == "Approved"):
				# 	continue
				item_template = create_item_template_from_sketch_order(self, row.name)
				updatet_item_template(self, item_template)
				# item_variant = create_item_from_sketch_order(self, item_template, row.name)
				# update_item_variant(self, item_variant, item_template)
				frappe.db.set_value(row.doctype, row.name, "item", item_template)
				frappe.msgprint(_("New Item Created: {0}").format(get_link_to_form("Item", item_template)))
		if self.order_type == 'Purchase':
			item_template = create_item_for_po(self,self.name)
			updatet_item_template(self, item_template)
			frappe.db.set_value("Sketch Order", self.name, "item_code", item_template)
			frappe.msgprint(_("New Item Created: {0}").format(get_link_to_form("Item", item_template)))

def updatet_item_template(self, item_template):
	frappe.db.set_value("Item", item_template, {"is_design_code": 0, "item_code": item_template})

def update_item_variant(self, item_variant, item_template):
	frappe.db.set_value("Item", item_variant, {"is_design_code": 1, "variant_of": item_template})

def populate_child_table(self):
	if self.workflow_state == "Assigned":
		self.rough_sketch_approval = []
		self.final_sketch_approval = []
		self.final_sketch_approval_cmo = []
		rough_sketch_approval = []
		final_sketch_approval = []
		final_sketch_approval_cmo = []
		for designer in self.designer_assignment:
			r_s_row = self.get(
				"rough_sketch_approvalz",
				{
					"designer": designer.designer,
					"designer_name": designer.designer_name,
				},
			)
			if not r_s_row:
				rough_sketch_approval.append(
					{
						"designer": designer.designer,
						"designer_name": designer.designer_name,
					},
				)
			final_sketch_approval.append(
				{
					"designer": designer.designer,
					"designer_name": designer.designer_name,
				},
			)
			# self.append(
			# 	"final_sketch_approval",
			# 	{
			# 		"designer": designer.designer,
			# 		"designer_name": designer.designer_name,
			# 	},
			# )
			# final_sketch_approval_cmo.append(
			# 	{
			# 		"designer": designer.designer,
			# 		"designer_name": designer.designer_name,
			# 	},
			# )
			# self.append(
			# 	"final_sketch_approval_cmo",
			# 	{
			# 		"designer": designer.designer,
			# 		"designer_name": designer.designer_name,
			# 	},
			# )

			# hod_name = frappe.db.get_value("User", {"email": frappe.session.user}, "full_name")
			# subject = "Sketch Design Assigned"
			# context = f"Mr. {hod_name} has assigned you a task"
			# user_id = frappe.db.get_value("Employee", designer.designer, "user_id")
			# if user_id:
			# 	create_system_notification(self, subject, context, [user_id])
		# create_system_notification(self, subject, context, recipients)
		# doc.final_sketch_approval_cmo and frappe.db.get_value("Final Sketch Approval HOD",{"parent":doc.name},"cmo_count as cnt", order_by="cnt asc")
		#  and frappe.db.get_value("Final Sketch Approval CMO",{"parent":doc.name},"sub_category")
		# and frappe.db.get_value("Final Sketch Approval CMO",{"parent":doc.name},"category") and frappe.db.get_value("Final Sketch Approval CMO",{"parent":doc.name},"setting_type")
		for row in rough_sketch_approval:
			self.append("rough_sketch_approval", row)
		for row in final_sketch_approval:
			self.append("final_sketch_approval", row)
		for row in final_sketch_approval_cmo:
			self.append("final_sketch_approval_cmo", row)
	if self.workflow_state == "Requires Update":
		total_approved = 0
		# if len(self.final_sketch_approval_cmo) != f_total:
		designer_with_approved_qty = []
		final_sketch_approval_cmo = []

		for i in self.final_sketch_approval:
			total_approved += i.approved
			designer_with_approved_qty.append(
				{"designer": i.designer, "qty": i.approved},
			)

		designer = []
		for j in designer_with_approved_qty:
			if j["designer"] in designer:
				continue
			for k in range(j["qty"]):
				count = check_count(self, j["designer"])
				if count == j["qty"]:
					continue
				self.append("final_sketch_approval_cmo", 
								{
									"designer": j["designer"],
									"designer_name":frappe.db.get_value("Employee",j["designer"],"employee_name"),
									"category":self.category
								}
							)
			designer.append(j["designer"])

def check_count(self, designer):
	count = 0
	if self.final_sketch_approval_cmo:
		for i in self.final_sketch_approval_cmo:
			if designer == i.designer:
				count += 1

	return count

def create_system_notification(self, subject, context, recipients):
	if not recipients:
		return
	notification_doc = {
		"type": "Alert",
		"document_type": self.doctype,
		"document_name": self.name,
		"subject": subject,
		"from_user": frappe.session.user,
		"email_content": context,
	}
	for user in recipients:
		notification = frappe.new_doc("Notification Log")
		notification.update(notification_doc)

		notification.for_user = user
		if (
			notification.for_user != notification.from_user
			or notification_doc.get("type") == "Energy Point"
			or notification_doc.get("type") == "Alert"
		):
			notification.insert(ignore_permissions=True)

# @frappe.whitelist()
def create_item_template_from_sketch_order(self, source_name, target_doc=None):
	def post_process(source, target):

		target.is_design_code = 1
		target.has_variants = 1
		target.india = self.india
		target.india_states = self.india_states
		target.usa = self.usa
		target.usa_states = self.usa_states
		target.custom_sketch_order_id = self.name
		target.custom_sketch_order_form_id = self.sketch_order_form
		sub_category = frappe.db.get_value("Final Sketch Approval CMO", source_name, "sub_category")
		designer = frappe.db.get_value("Final Sketch Approval CMO", source_name, "designer")
		target.item_group = sub_category + " - T"
		target.designer = designer
		target.subcategory = sub_category
		target.item_subcategory = sub_category

	doc = get_mapped_doc(
		"Final Sketch Approval CMO",
		source_name,
		{
			"Final Sketch Approval CMO": {
				"doctype": "Item",
				"field_map": {
					"category": "item_category",
					"sub_category":"item_subcategory"
				},
			}
		},
		target_doc,
		post_process,
	)
	doc.save()
	return doc.name


def create_item_from_sketch_order(self, item_template, source_name, target_doc=None):
	def post_process(source, target):
		target.item_code = f"{item_template}-001"
		target.india = self.india
		target.india_states = self.india_states
		target.usa = self.usa
		target.usa_states = self.usa_states

		# new code start
		target.custom_sketch_order_id = self.name
		target.custom_sketch_order_form_id = self.sketch_order_form
		sub_category = frappe.db.get_value("Final Sketch Approval CMO", source_name, "sub_category")
		designer = frappe.db.get_value("Final Sketch Approval CMO", source_name, "designer")
		target.item_group = sub_category + " - V"
		target.designer = designer
		# new code end

		target.order_form_type = "Sketch Order"
		target.custom_sketch_order_id = self.name
		target.sequence = int(item_template[2:7])

		for row in self.age_group:
			target.append("custom_age_group", {"design_attribute": row.design_attribute})

		for row in self.alphabetnumber:
			target.append("custom_alphabetnumber", {"design_attribute": row.design_attribute})

		for row in self.animalbirds:
			target.append("custom_animalbirds", {"design_attribute": row.design_attribute})

		for row in self.collection_1:
			target.append("custom_collection", {"design_attribute": row.design_attribute})

		for row in self.design_style:
			target.append("custom_design_style", {"design_attribute": row.design_attribute})

		for row in self.gender:
			target.append("custom_gender", {"design_attribute": row.design_attribute})

		for row in self.lines_rows:
			target.append("custom_lines__rows", {"design_attribute": row.design_attribute})

		for row in self.language:
			target.append("custom_language", {"design_attribute": row.design_attribute})

		for row in self.occasion:
			target.append("custom_occasion", {"design_attribute": row.design_attribute})

		for row in self.religious:
			target.append("custom_religious", {"design_attribute": row.design_attribute})

		for row in self.shapes:
			target.append("custom_shapes", {"design_attribute": row.design_attribute})

		for row in self.zodiac:
			target.append("custom_zodiac", {"design_attribute": row.design_attribute})

		for row in self.rhodium:
			target.append("custom_rhodium", {"design_attribute": row.design_attribute})

		# attribute_value_for_name = []

		for i in frappe.get_all(
			"Attribute Value Item Attribute Detail",
			{
				"parent": self.final_sketch_approval_cmo[0].sub_category,
				"in_item_variant": 1,
			},
			"item_attribute",
			order_by="idx asc",
		):
			attribute_with = i.item_attribute.lower().replace(" ", "_")
			attribute_value = None

			field_data = frappe.get_meta("Sketch Order").get_field(attribute_with)
			if field_data and field_data.fieldtype != "Table MultiSelect":
				attribute_value = frappe.db.get_value("Sketch Order", self.name, attribute_with)

			target.append(
				"attributes",
				{
					"attribute": i.item_attribute,
					"variant_of": item_template,
					"attribute_value": attribute_value,
				},
			)

	doc = get_mapped_doc(
		"Final Sketch Approval CMO",
		source_name,
		{
			"Final Sketch Approval CMO": {
				"doctype": "Item",
				"field_map": {
					"category": "item_category",
					"sub_category": "item_subcategory",
					"gold_wt_approx": "approx_gold",
					"diamond_wt_approx": "approx_diamond",
					"designer": "designer",
				},
			}
		},
		target_doc,
		post_process,
	)
	doc.save()
	return doc.name


def create_item_for_po(self, source_name, target_doc=None):
	def post_process(source, target):

		target.is_design_code = 1
		target.has_variants = 1
		target.india = self.india
		target.india_states = self.india_states
		target.usa = self.usa
		target.usa_states = self.usa_states
		if frappe.db.get_value('Employee',{'user_id':frappe.session.user},'name'):
			target.designer = frappe.db.get_value('Employee',{'user_id':frappe.session.user},'name')
		else:
			target.designer = frappe.db.get_value('User',frappe.session.user,'full_name')
		target.custom_sketch_order_id = self.name
		target.custom_sketch_order_form_id = self.sketch_order_form
		target.item_group = self.subcategory + " - T"
		target.item_category = self.category
		target.item_subcategory = self.subcategory

	doc = get_mapped_doc(
		"Sketch Order",
		self.name,
		{
			"Sketch Order": {
				"doctype": "Item",
				"field_map": {
					"category": "item_category",
				},
			}
		},
		target_doc,
		post_process,
	)
	doc.save()
	return doc.name

