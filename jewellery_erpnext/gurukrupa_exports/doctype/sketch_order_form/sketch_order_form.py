# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.query_builder import DocType
from frappe.utils import getdate, get_datetime, now_datetime, add_days, get_link_to_form, get_time
from datetime import datetime, timedelta

class SketchOrderForm(Document):

	def validate(self):
		self.validate_category_subcategory()

	def on_submit(self):
		self.handle_submit()
	
	def on_update_after_submit(self):
		self.update_delivery_dates()
	
	def on_cancel(self):
		self.cancel_linked_sketch_orders()
	
	def validate_category_subcategory(self):
		for row in self.order_details:
			if row.subcategory:
				parent_category = frappe.db.get_value("Attribute Value", row.subcategory, "parent_attribute_value")
				if row.category != parent_category:
					frappe.throw(_("Category & Sub Category mismatched in row #{0}").format(row.idx))
	
	def handle_submit(self):
		create_sketch_order(self)
		if self.supplier:
			create_purchase_order(self)

	def update_delivery_dates(self):
		if self.updated_delivery_date:
			sketch_order_names = frappe.get_all(
				"Sketch Order",
				filters={"sketch_order_form": self.name},
				pluck="name"
			)

			for sketch_order_names in sketch_order_names:
				frappe.db.set_value("Sketch Order", sketch_order_names, "update_delivery_date", self.updated_delivery_date)
	
	def cancel_linked_sketch_orders(self):
		sketch_orders = frappe.db.get_list("Sketch Order", filters={"sketch_order_form": self.name}, fields="name")
		for order in sketch_orders:
			frappe.db.set_value(
				"Sketch Order", 
				order["name"], 
				"workflow_state", 
				"Cancelled")
		
		frappe.db.set_value("Sketch Order Form", self.name, "workflow_state", "Cancelled")
		self.reload()


def create_sketch_order(doc):
	order_criteria = frappe.get_single("Order Criteria")
	created_orders = []

	for row in doc.order_details:
		order_name = make_sketch_order(
			"Sketch Order Form Detail",
			row.name,
			doc
		)

		sketch_order = frappe.get_doc("Sketch Order", order_name)

		apply_parent_dates(doc, sketch_order)
		apply_order_criteria_dates(doc, sketch_order, order_criteria)

		sketch_order.save(ignore_permissions=True)
		created_orders.append(
			get_link_to_form("Sketch Order", order_name)
		)

	if created_orders:
		frappe.msgprint(
			_("The following {0} were created: {1}")
			.format(
				frappe.bold(_("Sketch Orders")),
				"<br>" + ", ".join(created_orders)
			)
		)


def apply_parent_dates(parent, sketch_order):
	if parent.order_date:
		parent_date = getdate(parent.order_date)
		current_time = now_datetime().time()
		sketch_order.order_date = datetime.combine(parent_date, current_time)

	if parent.delivery_date:
		sketch_order.delivery_date = get_datetime(parent.delivery_date)


def apply_order_criteria_dates(parent, sketch_order, order_criteria):
	if not parent.order_date:
		return

	parent_date = getdate(parent.order_date)

	department_shifts = {
		row.department: (
			get_time(row.shift_start_time),
			get_time(row.shift_end_time)
		)
		for row in order_criteria.department_shift
		if not row.disable
	}

	for criteria in order_criteria.order:
		if criteria.disable:
			continue

		if parent.department not in department_shifts:
			continue

		shift_start, shift_end = department_shifts[parent.department]

		sketch_delivery = calculate_sketch_delivery(
			parent_date,
			criteria
		)
		sketch_order.sketch_delivery_date = sketch_delivery

		ibm_delivery = calculate_ibm_delivery(
			sketch_delivery,
			criteria.skecth_approval_timefrom_ibm_team,
			shift_start,
			shift_end
		)
		sketch_order.ibm_delivery_date = ibm_delivery
		break


def calculate_sketch_delivery(parent_date, criteria):
	approval_days = criteria.sketch_approval_day or 0
	submission_time = (
		get_time(criteria.sketch_submission_time)
		or datetime.strptime("09:00:00", "%H:%M:%S").time()
	)

	approval_date = add_days(parent_date, approval_days)
	return datetime.combine(approval_date, submission_time)


def calculate_ibm_delivery(base_datetime, ibm_time_value, shift_start, shift_end):
	if isinstance(ibm_time_value, timedelta):
		ibm_hours = ibm_time_value.total_seconds() / 3600
	else:
		try:
			ibm_hours = float(ibm_time_value)
		except (TypeError, ValueError):
			ibm_hours = 0

	if not shift_start or not shift_end:
		return base_datetime + timedelta(hours=ibm_hours)

	shift_end_dt = datetime.combine(base_datetime.date(), shift_end)
	remaining_hours = max(
		0,
		(shift_end_dt - base_datetime).total_seconds() / 3600
	)

	if ibm_hours <= remaining_hours:
		return base_datetime + timedelta(hours=ibm_hours)

	next_day = base_datetime.date() + timedelta(days=1)
	extra_hours = ibm_hours - remaining_hours
	return datetime.combine(next_day, shift_start) + timedelta(hours=extra_hours)

def make_sketch_order(doctype, source_name, parent_doc, target_doc=None):

	def set_missing_values(source, target):
		target.sketch_order_form_detail = source.name
		target.sketch_order_form = source.parent
		target.sketch_order_form_index = source.idx
		copy_parent_fields(parent_doc, target)

	doc = get_mapped_doc(
		doctype,
		source_name,
		{doctype: {"doctype": "Sketch Order"}},
		target_doc,
		set_missing_values
	)

	doc.save()
	return doc.name

def copy_parent_fields(parent, target):
	target.company = parent.company
	target.remark = parent.remarks

	fields = [
		"age_group", "alphabetnumber", "animalbirds",
		"collection_1", "design_style", "gender",
		"lines_rows", "language", "occasion",
		"religious", "shapes", "zodiac", "rhodium",
		"india", "india_states", "usa", "usa_states"
	]

	for field in fields:
		target.set(field, parent.get(field))


def create_purchase_order(doc):
	total_qty = sum(row.qty for row in doc.order_details)

	po = frappe.new_doc("Purchase Order")
	po.update({
		"supplier": doc.supplier,
		"company": doc.company,
		"branch": doc.branch,
		"project": doc.project,
		"custom_form": "Sketch Order Form",
		"custom_form_id": doc.name,
		"purchase_type": "Subcontracting",
		"custom_sketch_order_form": doc.name,
		"schedule_date": doc.delivery_date
	})

	item = po.append("items", {})
	item.item_code = "Design Expness"
	item.qty = total_qty
	item.schedule_date = doc.delivery_date

	po.save()

	frappe.msgprint(
		_("The following {0} is created: {1}")
		.format(
			frappe.bold(_("Purchase Order")),
			"<br>" + get_link_to_form("Purchase Order", po.name)
		)
	)



@frappe.whitelist()
def get_customer_orderType(customer_goods):
	order_type = Doctype("Order Type")
	return (
			frappe.qb.form_(OrderType)
			.select(OrderType.order_type)
			.where(OrderType.parent == customer_code)
			.run(as_dict=True)
		)