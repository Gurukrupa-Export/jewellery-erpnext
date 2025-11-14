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

	def on_update_after_submit(self):
		if self.updated_delivery_date:
			sketch_order_names = frappe.get_all(
				"Sketch Order",
				filters={"sketch_order_form": self.name},
				pluck="name"
			)

			for sketch_order_names in sketch_order_names:
				frappe.db.set_value("Sketch Order", sketch_order_names, "update_delivery_date", self.updated_delivery_date)

				
	def on_cancel(self):
		sketch_orders = frappe.db.get_list("Sketch Order", filters={"sketch_order_form": self.name}, fields="name")
		if sketch_orders:
			for order in sketch_orders:
				frappe.db.set_value("Sketch Order", order["name"], "workflow_state", "Cancelled")
		
		frappe.db.set_value("Sketch Order Form", self.name, "workflow_state", "Cancelled")
		self.reload()
		
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


# def create_sketch_order(self):
# 	# if self.design_by in ["Customer Design", "Concept by Designer"]:
# 	# 	order_details = self.order_details
# 	# 	doctype = "Sketch Order Form Detail"
# 	# else:
# 	# 	return
# 	order_details = self.order_details
# 	doctype = "Sketch Order Form Detail"
# 	doclist = []
# 	for row in order_details:
# 		docname = make_sketch_order(doctype, row.name, self)
# 		doclist.append(get_link_to_form("Sketch Order", docname))

# 	if doclist:
# 		msg = _("The following {0} were created: {1}").format(
# 			frappe.bold(_("Sketch Orders")), "<br>" + ", ".join(doclist)
# 		)
# 		frappe.msgprint(msg)

from frappe.utils import getdate, get_datetime, now_datetime, add_days, get_link_to_form, get_time
from datetime import datetime, timedelta
import frappe
from frappe import _

def create_sketch_order(self):
    order_details = self.order_details
    doctype = "Sketch Order Form Detail"
    doclist = []

    order_criteria = frappe.get_single("Order Criteria")

    for row in order_details:
        docname = make_sketch_order(doctype, row.name, self)
        sketch_order_doc = frappe.get_doc("Sketch Order", docname)

        if self.order_date:
            parent_date = getdate(self.order_date)
            current_dt = now_datetime()
            final_order_datetime = datetime.combine(parent_date, current_dt.time())
            sketch_order_doc.order_date = final_order_datetime

       
            for criteria_row in order_criteria.order:
                if criteria_row.disable:
                    continue

                department_match = False
                shift_start_time = None
                shift_end_time = None

                for dept_row in order_criteria.department_shift:
                    if dept_row.disable:
                        continue
                    if dept_row.department == self.department:
                        shift_start_time = get_time(dept_row.shift_start_time)
                        shift_end_time = get_time(dept_row.shift_end_time)
                        department_match = True
                        break

                if not department_match:
                    continue

                # Sketch approval date and time
                sketch_approval_days = criteria_row.sketch_approval_day or 0
                submission_time = get_time(criteria_row.sketch_submission_time) or datetime.strptime("09:00:00", "%H:%M:%S").time()
                approval_date = add_days(parent_date, sketch_approval_days)
                final_submission_datetime = datetime.combine(approval_date, submission_time)
                sketch_order_doc.sketch_delivery_date = final_submission_datetime

                # IBM delivery calculation
                ibm_time_value = criteria_row.skecth_approval_timefrom_ibm_team or 0

                # Convert to hours
                if isinstance(ibm_time_value, timedelta):
                    ibm_time_hours = ibm_time_value.total_seconds() / 3600
                else:
                    try:
                        ibm_time_hours = float(ibm_time_value)
                    except (ValueError, TypeError):
                        ibm_time_hours = 0

                # Calculate IBM delivery datetime
                if shift_start_time and shift_end_time:
                    sketch_dt = final_submission_datetime
                    shift_end_dt = datetime.combine(final_submission_datetime.date(), shift_end_time)
                    remaining_shift_hours = max(0, (shift_end_dt - sketch_dt).total_seconds() / 3600)

                    if ibm_time_hours <= remaining_shift_hours:
                        ibm_delivery_datetime = final_submission_datetime + timedelta(hours=ibm_time_hours)
                    else:
                        extra_hours = ibm_time_hours - remaining_shift_hours
                        next_day = final_submission_datetime.date() + timedelta(days=1)
                        ibm_delivery_datetime = datetime.combine(next_day, shift_start_time) + timedelta(hours=extra_hours)
                else:
                    ibm_delivery_datetime = final_submission_datetime + timedelta(hours=ibm_time_hours)

                sketch_order_doc.ibm_delivery_date = ibm_delivery_datetime
                break  

        if self.delivery_date:
            parent_delivery_dt = get_datetime(self.delivery_date)
            sketch_order_doc.delivery_date = parent_delivery_dt

        sketch_order_doc.save(ignore_permissions=True)
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
