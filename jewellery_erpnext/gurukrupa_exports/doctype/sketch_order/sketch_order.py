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
		self._move_approved_to_cmo("final_sketch_hold", "hold")
		self._move_approved_to_cmo("final_sketch_rejected", "reject")

	def _move_approved_to_cmo(self, source_field, count_field):
		"""Move approved rows from source child table to final_sketch_approval_cmo.

		Collects approved rows first, then removes them after iteration to avoid
		modifying the list during iteration.
		"""
		source_table = self.get(source_field, [])
		approved_rows = [row for row in source_table if row.is_approved]

		if not approved_rows:
			return

		for row in approved_rows:
			self.append(
				"final_sketch_approval_cmo",
				{
					"designer": row.designer,
					"sketch_image": row.sketch_image,
					"designer_name": row.designer_name,
					"qc_person": row.qc_person,
					"diamond_wt_approx": row.diamond_wt_approx,
					"setting_type": row.setting_type,
					"sub_category": row.sub_category,
					"category": row.category,
					"image_rough": row.image_rough,
					"final_image": row.final_image,
				},
			)

		approved_set = set(id(row) for row in approved_rows)
		self.set(
			source_field, [row for row in source_table if id(row) not in approved_set]
		)

		for s in self.final_sketch_approval:
			s.approved = len(self.final_sketch_approval_cmo)
			setattr(s, count_field, len(self.get(source_field, [])))

		label = "Hold" if count_field == "hold" else "Rejected"
		frappe.msgprint(_(f"{label} Image is approved"))

	def on_submit(self):
		self.make_items()

	def make_items(self):
		if self.order_type != "Purchase":
			for row in self.final_sketch_approval_cmo:
				# item_template = create_item_template_from_sketch_order(self, row.name)
				# update_item_template(self, item_template)
				# frappe.db.set_value(row.doctype, row.name, "item", item_template)
				# frappe.msgprint(
				# 	_("New Item Created: {0}").format(
				# 		get_link_to_form("Item", item_template)
				# 	)
				# )
				if row.item_remark == "Copy Paste Item":
					frappe.db.set_value("Item",row.item,"order_form_type","Sketch Order")
					frappe.db.set_value("Item",row.item,"custom_sketch_order_form_id",self.sketch_order_form)
					frappe.db.set_value("Item",row.item,"custom_sketch_order_id",self.name)
				else:
					item_template = create_item_template_from_sketch_order(self, row.name)
					update_item_template(self, item_template)
					# item_variant = create_item_from_sketch_order(self, item_template, row.name)
					# update_item_variant(self, item_variant, item_template)
					frappe.db.set_value(row.doctype, row.name, "item", item_template)
					frappe.msgprint(_("New Item Created: {0}").format(get_link_to_form("Item", item_template)))
		if self.order_type == "Purchase":
			item_template = create_item_for_po(self, self.name)
			update_item_template(self, item_template)
			frappe.db.set_value("Sketch Order", self.name, "item_code", item_template)
			frappe.msgprint(
				_("New Item Created: {0}").format(
					get_link_to_form("Item", item_template)
				)
			)


def update_item_template(self, item_template):
	frappe.db.set_value(
		"Item", item_template, {"is_design_code": 0, "item_code": item_template}
	)

from frappe.utils import get_datetime, get_time
from datetime import datetime, timedelta, time
import frappe

def update_sketch_delivery_date(self):
    if not self.sketch_update_delivery_date:
        return

    order_criteria = frappe.get_single("Order Criteria")

    # ----------------------------
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

    # ----------------------------
    # Convert sketch_approval_time_ibm to hours
    if isinstance(sketch_approval_time_ibm, timedelta):
        remaining_hours = sketch_approval_time_ibm.total_seconds() / 3600
    elif isinstance(sketch_approval_time_ibm, datetime.time):
        remaining_hours = sketch_approval_time_ibm.hour + sketch_approval_time_ibm.minute / 60 + sketch_approval_time_ibm.second / 3600
    elif isinstance(sketch_approval_time_ibm, (int, float)):
        remaining_hours = float(sketch_approval_time_ibm)
    else:
        frappe.throw("skecth_approval_timefrom_ibm_team must be a time, timedelta, or numeric value")

    # ----------------------------
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

    # ----------------------------
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
		for row in rough_sketch_approval:
			self.append("rough_sketch_approval", row)
		for row in final_sketch_approval:
			self.append("final_sketch_approval", row)
		for row in final_sketch_approval_cmo:
			self.append("final_sketch_approval_cmo", row)
	if self.workflow_state == "Requires Update":
		total_approved = 0
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
				self.append(
					"final_sketch_approval_cmo",
					{
						"designer": j["designer"],
						"designer_name": frappe.db.get_value(
							"Employee", j["designer"], "employee_name"
						),
						"category": self.category,
					},
				)
			designer.append(j["designer"])


def check_count(self, designer):
	count = 0
	if self.final_sketch_approval_cmo:
		for i in self.final_sketch_approval_cmo:
			if designer == i.designer:
				count += 1

	return count


def create_item_template_from_sketch_order(self, source_name, target_doc=None):
	def post_process(source, target):
		sub_category, designer = frappe.db.get_value(
			"Final Sketch Approval CMO", source_name, ["sub_category", "designer"]
		)

		target.update(
			{
				"is_design_code": 1,
				"has_variants": 1,
				"india": self.india,
				"india_states": self.india_states,
				"usa": self.usa,
				"usa_states": self.usa_states,
				"custom_sketch_order_id": self.name,
				"custom_sketch_order_form_id": self.sketch_order_form,
				"item_group": f"{sub_category} - T",
				"designer": designer,
				"subcategory": sub_category,
				"item_subcategory": sub_category,
			}
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
		target.update(
			{
				"is_design_code": 1,
				"has_variants": 1,
				"india": self.india,
				"india_states": self.india_states,
				"usa": self.usa,
				"usa_states": self.usa_states,
				"designer": frappe.db.get_value(
					"Employee", {"user_id": frappe.session.user}, "name"
				)
				or frappe.session.user,
				"custom_sketch_order_id": self.name,
				"custom_sketch_order_form_id": self.sketch_order_form,
				"item_group": f"{self.subcategory} - T",
				"item_category": self.category,
				"item_subcategory": self.subcategory,
			}
		)

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
