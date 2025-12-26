# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.utils import get_link_to_form


class SketchOrder(Document):
	def validate(self):
		update_sketch_delivery_date(self)
		populate_child_table(self)
		rows_remove = []
		for r in self.final_sketch_hold:
			if r.is_approved:
				self.append('final_sketch_approval_cmo',{
					'designer':r.designer,
					'sketch_image':r.sketch_image,
					'category':self.category,
					'designer_name': r.designer_name,
					'qc_person':r.qc_person,
					'diamond_wt_approx':r.diamond_wt_approx,
					'diamond_wt_approx':r.diamond_wt_approx,
					'setting_type':r.setting_type,
					'sub_category':r.sub_category,
                    'category':r.category,
					'image_rough':r.image_rough,
					'final_image':r.final_image
					
				})
				rows_remove.append(r)
				for r in rows_remove:
					self.final_sketch_hold.remove(r)
			
				for s in self.final_sketch_approval:
					s.approved=len(self.final_sketch_approval_cmo)
					s.hold=len(self.final_sketch_hold)
			frappe.msgprint("Hold Image is approved")	
		rows_to_remove = []
		for r in self.final_sketch_rejected:
			if r.is_approved:
				self.append('final_sketch_approval_cmo',{
					'designer':r.designer,
					'sketch_image':r.sketch_image,
					'category':self.category,
					'designer_name': r.designer_name,
					'qc_person':r.qc_person,
					'diamond_wt_approx':r.diamond_wt_approx,
					'diamond_wt_approx':r.diamond_wt_approx,
					'setting_type':r.setting_type,
					'sub_category':r.sub_category,
                    'category':r.category,
					'image_rough':r.image_rough,
					'final_image':r.final_image
					
				})
				rows_to_remove.append(r)
				for r in rows_to_remove:
					self.final_sketch_rejected.remove(r)
			
				for s in self.final_sketch_approval:
					s.approved=len(self.final_sketch_approval_cmo)
					s.reject=len(self.final_sketch_rejected)
				frappe.msgprint("Rejected image is approved ")	

	def on_submit(self):
		self.make_items()
	def on_cancel(self):
		self.workflow_state = "Cancelled"
    
	def make_items(self):
		# if self.workflow_state == "Items Updated":
		if self.order_type != 'Purchase':
			for row in self.final_sketch_approval_cmo:
				# if row.item or not (row.design_status == "Approved" and row.design_status_cpo == "Approved"):
				# 	continue
				if row.item_remark == "Copy Paste Item":
					frappe.db.set_value("Item",row.item,"order_form_type","Sketch Order")
					frappe.db.set_value("Item",row.item,"custom_sketch_order_form_id",self.sketch_order_form)
					frappe.db.set_value("Item",row.item,"custom_sketch_order_id",self.name)
				else:
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


from frappe.utils import get_datetime, get_time
from datetime import datetime, timedelta, time
import frappe

def update_sketch_delivery_date(self):
    if not self.sketch_update_delivery_date:
        return

    order_criteria = frappe.get_single("Order Criteria")

    # Latest active order row
    valid_order_rows = [row for row in order_criteria.order if not row.disable]
    if not valid_order_rows:
        frappe.throw("No active (enabled) rows found in Order Criteria 'order' table.")

    latest_order_row = valid_order_rows[-1]

    sketch_time = latest_order_row.sketch_submission_time
    sketch_approval_time_ibm = latest_order_row.skecth_approval_timefrom_ibm_team

    if not sketch_time:
        frappe.throw("Sketch Submission Time not set in the latest active row of Order Criteria 'order' table.")

    if not sketch_approval_time_ibm:
        frappe.throw("Sketch Approval Time from IBM Team not set in the latest active row.")

    # Combine date and sketch_submission_time
    selected_datetime = get_datetime(self.sketch_update_delivery_date)
    sketch_datetime = datetime.combine(selected_datetime.date(), get_time(sketch_time))
    self.sketch_update_delivery_date = sketch_datetime

    # Convert sketch_approval_time_ibm to hours
    if isinstance(sketch_approval_time_ibm, timedelta):
        remaining_hours = sketch_approval_time_ibm.total_seconds() / 3600
    elif isinstance(sketch_approval_time_ibm, datetime.time):
        remaining_hours = sketch_approval_time_ibm.hour + sketch_approval_time_ibm.minute / 60 + sketch_approval_time_ibm.second / 3600
    elif isinstance(sketch_approval_time_ibm, (int, float)):
        remaining_hours = float(sketch_approval_time_ibm)
    else:
        frappe.throw("skecth_approval_timefrom_ibm_team must be a time, timedelta, or numeric value")

    # Latest department shift
    valid_shift_rows = [row for row in order_criteria.department_shift if not row.disable]
    if not valid_shift_rows:
        frappe.throw("No active (enabled) rows found in Order Criteria 'department_shift' table.")

    latest_shift_row = valid_shift_rows[-1]

    # Convert shift_start_time and shift_end_time to datetime.time
    def to_time(value):
        if isinstance(value, timedelta):
            total_seconds = value.total_seconds()
            hours = int(total_seconds // 3600)
            minutes = int((total_seconds % 3600) // 60)
            seconds = int(total_seconds % 60)
            return time(hour=hours, minute=minutes, second=seconds)
        elif isinstance(value, time):
            return value
        elif isinstance(value, str):
            parts = value.split(":")
            return time(hour=int(parts[0]), minute=int(parts[1]), second=int(parts[2]) if len(parts) > 2 else 0)
        else:
            frappe.throw("Shift times must be timedelta, time, or string in HH:MM:SS format")

    shift_start_time = to_time(latest_shift_row.shift_start_time)
    shift_end_time = to_time(latest_shift_row.shift_end_time)

    # Calculate IBM delivery respecting shift hours
    current_datetime = sketch_datetime

    while remaining_hours > 0:
        # Today's shift
        shift_start_datetime = datetime.combine(current_datetime.date(), shift_start_time)
        shift_end_datetime = datetime.combine(current_datetime.date(), shift_end_time)

        # If current time is after shift end, jump to next day's shift start
        if current_datetime >= shift_end_datetime:
            current_datetime = shift_start_datetime + timedelta(days=1)
            shift_start_datetime += timedelta(days=1)
            shift_end_datetime += timedelta(days=1)

        # If current time is before shift start, jump to shift start
        if current_datetime < shift_start_datetime:
            current_datetime = shift_start_datetime

        # Available hours in current shift
        available_hours = (shift_end_datetime - current_datetime).total_seconds() / 3600

        if remaining_hours <= available_hours:
            # Finish within current shift
            current_datetime += timedelta(hours=remaining_hours)
            remaining_hours = 0
        else:
            # Use up shift hours, move to next day
            remaining_hours -= available_hours
            current_datetime = shift_start_datetime + timedelta(days=1)

    # Save IBM delivery date
    self.ibm_delivery_date = current_datetime

    
    



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

