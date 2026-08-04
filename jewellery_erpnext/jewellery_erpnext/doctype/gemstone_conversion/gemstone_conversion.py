# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt
import json
from erpnext.controllers.queries import get_batch_no
import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime, time_diff_in_seconds

from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.doc_events.batch_utils import (
	update_fifo_batch,
)
from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
	get_item_loss_item,
)


class GemstoneConversion(Document):
	def before_validate(self):
		update_fifo_batch(self)
		self.validate_gemstone_type()
		self.validate_target_item()

	def on_submit(self):
		make_gemstone_stock_entry(self)
		if self.conversion_type == "Resize":
			make_target_item_transfer_stock_entry(self)
		if self.g_source_qty and self.g_source_qty > self.batch_avail_qty:
			frappe.throw(_("Source Qty greater then batch available qty"))

	def validate(self):
		loss_item = None
		if self.loss_type:
			loss_item = get_loss_item(self.company, self.g_source_item, self.loss_type)
		remove_list = []
		self.g_target_qty = 0
		for row in self.sc_target_table:
			if row.item_code == loss_item:
				remove_list.append(row)
				continue
			self.g_target_qty += row.qty

		for row in remove_list:
			self.remove(row)

		if self.workflow_state == "Draft":
			set_subcontractor_warehouse(self)

		should_auto_add_loss = (
			self.conversion_type == "Conversion" and self.workflow_state == "Draft"
		) or (
			self.conversion_type == "Resize"
			and self.workflow_state in ("Issue", "Receive")
		)
		if should_auto_add_loss:
			if loss_item and flt((self.g_source_qty or 0) - self.g_target_qty, 2) > 0:
				self.append(
					"sc_target_table",
					{
						"item_code": loss_item,
						"qty": ((self.g_source_qty or 0) - self.g_target_qty),
					},
				)
				self.g_loss_item = loss_item
		self.g_loss_qty = flt((self.g_source_qty or 0) - self.g_target_qty, 2)

		if self.g_source_qty is not None and self.g_target_qty > self.g_source_qty:
			frappe.throw(_("Target Qty does not match with Source Qty"))

		if self.g_loss_qty < 0:
			frappe.throw(_("Target Qty not allowed greater than Source Qty"))
		# if self.workflow_state != 'Issue':
		# 	if self.g_target_qty > self.batch_avail_qty:
		# 		frappe.throw(_("Target Qty not allowed greater than Batch Available Qty"))
		# 	if self.g_source_qty and self.g_source_qty > self.batch_avail_qty:
		# 		frappe.throw(
		# 			f"Conversion failed batch available qty not meet. </br><b>(Batch Qty = {self.batch_avail_qty})</b><br>select another batch."
		# 		)
		if (
			self.conversion_type == "Conversion" and self.workflow_state == "Submitted"
		) or (self.conversion_type == "Resize" and self.workflow_state == "Receive"):
			if (self.g_source_qty or 0) == 0 or self.g_target_qty == 0:
				frappe.throw(
					_(
						"Add at least one Target Item other than the Loss Item. Source Qty or Target Qty is not allowed to be Zero."
					)
				)
		if self.g_source_qty is not None and self.g_source_qty < 0:
			frappe.throw(_("Source Qty invalid"))

		validate_gemstone_item(self)
		if self.workflow_state == "Issue":
			# frappe.throw("HERE")
			make_warehouse_transfer_stock_entry(self)
			create_fg_subcontracting_po(self)

		if self.conversion_type == "Resize":
			stamp_resize_conversion_time(self)

		if self.g_source_item and self.batch and self.source_warehouse:
			self.source_valuation_rate = get_source_batch_valuation_rate(
				self.g_source_item, self.batch, self.source_warehouse
			)

	@frappe.whitelist()
	def get_detail_tab_value(self):
		errors = []
		dpt, branch = frappe.get_value(
			"Employee", self.employee, ["department", "branch"]
		)
		if not dpt:
			errors.append(
				f"Department Messing against <b>{self.employee} Employee Master</b>"
			)
		if not branch and self.company == "Gurukrupa Export Private Limited":
			errors.append(
				f"Branch Messing against <b>{self.employee} Employee Master</b>"
			)
		mnf = frappe.get_value("Department", dpt, "manufacturer")
		if not mnf:
			errors.append("Manufacturer Messing against <b>Department Master</b>")
		s_wh = frappe.get_value(
			"Warehouse",
			{"disabled": 0, "department": dpt, "warehouse_type": "Raw Material"},
			"name",
		)
		if not mnf:
			errors.append("Warehouse Missing Warehouse Master Department Not Set")
		if errors:
			frappe.throw("<br>".join(errors))
		if dpt and mnf and s_wh:
			self.department = dpt
			self.branch = branch
			self.manufacturer = mnf
			self.source_warehouse = s_wh
			self.target_warehouse = s_wh

	@frappe.whitelist()
	def get_batch_detail(self):
		bal_qty = None
		supplier = None
		customer = None
		inventory_type = None

		error = []
		if self.batch:
			bal_qty = get_batch_qty(
				batch_no=self.batch, warehouse=self.source_warehouse
			)
			reference_doctype, reference_name = frappe.get_value(
				"Batch", self.batch, ["reference_doctype", "reference_name"]
			)
			# if not bal_qty:
			# 	error.append("Batch Qty zero")
			if reference_doctype:
				if reference_doctype == "Purchase Receipt":
					# supplier = frappe.get_value(
					# 	reference_doctype, reference_name, "supplier"
					# )
					inventory_type = "Regular Stock"
				if reference_doctype == "Stock Entry":
					inventory_type = frappe.get_value(
						reference_doctype, reference_name, "inventory_type"
					)
					if not inventory_type:
						inventory_type = frappe.get_value(
							"Batch", self.batch, "custom_inventory_type"
						)
					if inventory_type == "Customer Goods":
						customer = frappe.get_value(
							reference_doctype, reference_name, "_customer"
						)
			if error:
				frappe.throw(", ".join(error))

		return bal_qty, supplier, customer, inventory_type

	def validate_gemstone_type(self):
		gemstone_type = frappe.db.get_value(
			"Item Variant Attribute",
			{"attribute": "Gemstone Type", "parent": self.g_source_item},
			"attribute_value",
		)
		variant_of = frappe.db.get_value("Item", self.g_source_item, "variant_of")

		for row in self.sc_target_table:
			t_variant_of = frappe.db.get_value("Item", row.item_code, "variant_of")

			if variant_of == t_variant_of:
				continue
			t_gemstone_type = frappe.db.get_value(
				"Item Variant Attribute",
				{"attribute": "Gemstone Type", "parent": row.item_code},
				"attribute_value",
			)

			if t_gemstone_type != gemstone_type:
				frappe.throw(
					_(
						"The gemstone type in <b>{0}</b> is different from that in <b>{1}</b>."
					).format(row.item_code, self.g_source_item)
				)

	def validate_target_item(self):
		attr_value = frappe.db.get_value(
			"Item Variant Attribute",
			{"attribute": "Gemstone Size", "parent": self.g_source_item},
			"attribute_value",
		)
		variant_of = frappe.db.get_value("Item", self.g_source_item, "variant_of")
		if not attr_value:
			return
		height, weight = frappe.db.get_value(
			"Attribute Value", attr_value, ["height", "weight"]
		)

		for row in self.sc_target_table:
			t_variant_of, is_customer_gemstone = frappe.db.get_value(
				"Item",
				row.item_code,
				["variant_of", "custom_inventory_type_can_be_customer_goods"],
			)

			if variant_of == t_variant_of:
				continue

			t_attr_value = frappe.db.get_value(
				"Item Variant Attribute",
				{"attribute": "Gemstone Size", "parent": row.item_code},
				"attribute_value",
			)

			t_height, t_weight = frappe.db.get_value(
				"Attribute Value", t_attr_value, ["height", "weight"]
			)

			if is_customer_gemstone:
				if t_height != height or weight != t_weight:
					frappe.throw(
						_(
							"The gemstone size in <b>{0}</b> is not equal size of <b>{1}</b>."
						).format(row.item_code, self.g_source_item)
					)

			if t_height > height or weight > t_weight:
				frappe.throw(
					_(
						"The gemstone size in <b>{0}</b> is not within the size range of <b>{1}</b>."
					).format(row.item_code, self.g_source_item)
				)


def stamp_resize_conversion_time(self):
	if self.workflow_state == "Issue" and not self.issue_time:
		self.issue_time = now_datetime()

	if self.workflow_state == "Receive":
		if not self.receive_time:
			self.receive_time = now_datetime()
		if self.issue_time and self.receive_time < self.issue_time:
			frappe.throw(_("Receive Time cannot be before Issue Time"))

		if self.is_subcontracting:
			self.conversion_cost = get_subcontracting_pi_amount(
				self.fg_subcontracting_po
			)
		else:
			self.conversion_cost = get_employee_conversion_cost(
				self.to_employee, self.issue_time, self.receive_time
			)


def get_subcontracting_pi_amount(fg_subcontracting_po):
	pi_name = frappe.db.get_value(
		"Purchase Invoice Item",
		{"purchase_order": fg_subcontracting_po, "docstatus": 1},
		"parent",
	)
	if not pi_name:
		frappe.throw(
			_(
				"Submit a Purchase Invoice against FG Subcontracting PO <b>{0}</b> before Receive."
			).format(fg_subcontracting_po)
		)
	return flt(frappe.db.get_value("Purchase Invoice", pi_name, "grand_total"))


def get_employee_conversion_cost(to_employee, issue_time, receive_time):
	if not (issue_time and receive_time):
		frappe.throw(
			_("Issue Time and Receive Time are required to calculate conversion cost.")
		)

	hour_rate = frappe.db.get_value(
		"Workstation", {"employee": to_employee}, "hour_rate"
	)
	if not hour_rate:
		frappe.throw(
			_(
				"No Workstation found for Employee <b>{0}</b>. Configure a Workstation with Employee = {0} and set its Hour Rate."
			).format(to_employee)
		)

	minutes_worked = time_diff_in_seconds(receive_time, issue_time) / 60
	return flt(hour_rate) * minutes_worked / 60


def set_subcontractor_warehouse(self):
	if self.conversion_type != "Resize":
		return

	if self.is_subcontracting and self.supplier:
		warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"subcontractor": self.supplier,
				"warehouse_type": "Manufacturing",
				"disabled": 0,
			},
			"name",
		)
		if not warehouse:
			frappe.throw(
				_(
					"No Manufacturing Warehouse found for Subcontractor <b>{0}</b>. Configure a Warehouse with Warehouse Type = Manufacturing and Subcontractor = {0}."
				).format(self.supplier)
			)

	elif not self.is_subcontracting and self.to_employee:
		warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"employee": self.to_employee,
				"warehouse_type": "Manufacturing",
				"disabled": 0,
			},
			"name",
		)
		if not warehouse:
			frappe.throw(
				_(
					"No Manufacturing Warehouse found for Employee <b>{0}</b>. Configure a Warehouse with Warehouse Type = Manufacturing and Employee = {0}."
				).format(self.to_employee)
			)

	else:
		return

	self.target_warehouse = warehouse


def make_gemstone_stock_entry(self):
	target_wh = self.target_warehouse
	inventory_type = self.inventory_type
	batch_no = self.batch
	loss_item = get_loss_item(self.company, self.g_source_item, self.loss_type)
	is_resize = self.conversion_type == "Resize"
	# Resize: the source item was moved to target_warehouse at Issue and never brought
	# back - repack consumes it directly from there instead of a separate transfer first.
	source_wh = self.target_warehouse if is_resize else self.source_warehouse

	if is_resize:
		scrap_warehouse = None
	else:
		scrap_warehouse = get_scrap_warehouse(self.department)
	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Repack-Gemstone Conversion",
			"purpose": "Repack",
			"company": self.company,
			"custom_gemstone_conversion": self.name,
			"inventory_type": inventory_type,
			"_customer": self.customer,
			"auto_created": 1,
			"branch": self.branch,
		}
	)
	source_item = []
	target_item = []
	source_item.append(
		{
			"item_code": self.g_source_item,
			"qty": self.g_source_qty or 0,
			"inventory_type": inventory_type,
			"batch_no": batch_no,
			"department": self.department,
			"employee": self.employee,
			"manufacturer": self.manufacturer,
			"s_warehouse": source_wh,
		}
	)
	if is_resize:
		target_basic_rate = flt(self.source_valuation_rate) + flt(self.conversion_cost)
	else:
		target_basic_rate = flt(self.source_valuation_rate) * (
			flt(self.rate_factor) or 1
		)
	for row in self.sc_target_table:
		if row.item_code == loss_item:
			if is_resize:
				# Resize: loss item is not tracked as scrap stock - no Stock Entry row for it.
				continue
			t_wh = scrap_warehouse
		else:
			t_wh = target_wh
		target_item.append(
			{
				"item_code": row.item_code,
				"qty": row.qty,
				"inventory_type": inventory_type,
				"department": self.department,
				"employee": self.employee,
				"manufacturer": self.manufacturer,
				"t_warehouse": t_wh,
				"basic_rate": target_basic_rate,
			}
		)
	for row in source_item:
		se.append(
			"items",
			{
				"item_code": row["item_code"],
				"qty": row["qty"],
				"inventory_type": row["inventory_type"],
				"batch_no": row["batch_no"],
				"department": row["department"],
				"employee": row["employee"],
				"manufacturer": row["manufacturer"],
				"s_warehouse": row["s_warehouse"],
				"use_serial_batch_fields": True,
				"serial_and_batch_bundle": None,
			},
		)
	for row in target_item:
		se.append(
			"items",
			{
				"item_code": row["item_code"],
				"qty": row["qty"],
				"inventory_type": row["inventory_type"],
				"department": row["department"],
				"employee": row["employee"],
				"manufacturer": row["manufacturer"],
				"t_warehouse": row["t_warehouse"],
				"basic_rate": row["basic_rate"],
				"basic_amount": flt(row["basic_rate"] * row["qty"]),
				"set_basic_rate_manually": 1,
			},
		)
	se.save()
	se.submit()
	self.stock_entry = se.name

	if is_resize:
		se.reload()
		self.flags.repack_target_batch_map = {
			row.item_code: row.batch_no for row in se.items if row.t_warehouse
		}


@frappe.whitelist()
def get_loss_item(company, souce_item, loss_type=None):
	return get_item_loss_item(company, souce_item, "G", loss_type)


def get_source_batch_valuation_rate(item_code, batch_no, warehouse):
	if not (item_code and batch_no and warehouse):
		return 0

	if frappe.db.get_value("Batch", batch_no, "use_batchwise_valuation"):
		data = frappe.db.sql(
			"""
			SELECT sbe.stock_value_difference AS stock_value, sbe.qty AS qty
			FROM `tabSerial and Batch Bundle` sb
			INNER JOIN `tabSerial and Batch Entry` sbe ON sb.name = sbe.parent
			WHERE sbe.batch_no = %(batch_no)s
				AND sb.warehouse = %(warehouse)s
				AND sb.item_code = %(item_code)s
				AND sb.docstatus = 1
				AND sb.is_cancelled = 0
				AND sbe.qty > 0
			ORDER BY sb.posting_date ASC, sb.posting_time ASC, sb.creation ASC
			LIMIT 1
			""",
			{"batch_no": batch_no, "warehouse": warehouse, "item_code": item_code},
			as_dict=True,
		)
		if data and data[0].qty:
			return flt(data[0].stock_value) / flt(data[0].qty)
		return 0

	return flt(
		frappe.db.get_value(
			"Bin", {"item_code": item_code, "warehouse": warehouse}, "valuation_rate"
		)
	)


def make_warehouse_transfer_stock_entry(self):
	if self.issue_stock_entry:
		return self.issue_stock_entry

	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Transfer",
			"purpose": "Material Transfer",
			"company": self.company,
			"custom_gemstone_conversion": self.name,
			"inventory_type": self.inventory_type,
			"_customer": self.customer,
			"auto_created": 1,
			"branch": self.branch,
		}
	)
	se.append(
		"items",
		{
			"item_code": self.g_source_item,
			"qty": self.g_source_qty or 0,
			"inventory_type": self.inventory_type,
			"batch_no": self.batch,
			"department": self.department,
			"employee": self.employee,
			"manufacturer": self.manufacturer,
			"s_warehouse": self.source_warehouse,
			"t_warehouse": self.target_warehouse,
			"use_serial_batch_fields": True,
			"serial_and_batch_bundle": None,
		},
	)
	se.save()
	se.submit()
	self.issue_stock_entry = se.name
	return se.name


def make_target_item_transfer_stock_entry(self):
	if self.target_transfer_stock_entry:
		return self.target_transfer_stock_entry

	if self.target_warehouse == self.source_warehouse:
		return

	loss_item = get_loss_item(self.company, self.g_source_item, self.loss_type)
	target_batch_map = self.flags.repack_target_batch_map or {}

	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Transfer",
			"purpose": "Material Transfer",
			"company": self.company,
			"custom_gemstone_conversion": self.name,
			"inventory_type": self.inventory_type,
			"_customer": self.customer,
			"auto_created": 1,
			"branch": self.branch,
		}
	)
	for row in self.sc_target_table:
		if row.item_code == loss_item:
			continue
		se.append(
			"items",
			{
				"item_code": row.item_code,
				"qty": row.qty,
				"inventory_type": self.inventory_type,
				# Carry forward the exact batch Repack just created instead of
				# letting the incoming leg auto-create a new one.
				"batch_no": target_batch_map.get(row.item_code),
				"department": self.department,
				"employee": self.employee,
				"manufacturer": self.manufacturer,
				"s_warehouse": self.target_warehouse,
				"t_warehouse": self.source_warehouse,
				"use_serial_batch_fields": True,
				"serial_and_batch_bundle": None,
			},
		)

	if not se.items:
		return

	se.save()
	se.submit()
	self.target_transfer_stock_entry = se.name
	return se.name


FG_SUBCONTRACTING_ITEM = "FG Subcontracting"


def create_fg_subcontracting_po(self):
	if self.fg_subcontracting_po:
		return
	if (
		self.workflow_state != "Issue"
		or not self.is_subcontracting
		or not self.supplier
	):
		return

	company_address = frappe.db.get_value(
		"Dynamic Link",
		{
			"link_doctype": "Company",
			"link_name": self.company,
			"parenttype": "Address",
		},
		"parent",
	)
	supplier_address = frappe.db.get_value(
		"Dynamic Link",
		{
			"link_doctype": "Supplier",
			"link_name": self.supplier,
			"parenttype": "Address",
		},
		"parent",
	)

	po = frappe.new_doc("Purchase Order")
	po.supplier = self.supplier
	po.company = self.company
	po.schedule_date = self.date
	po.branch = self.branch
	po.custom_branch = self.branch
	po.custom_gemstone_conversion = self.name
	if company_address:
		po.company_address = company_address
	if supplier_address:
		po.supplier_address = supplier_address
	po.append(
		"items",
		{
			"item_code": FG_SUBCONTRACTING_ITEM,
			"qty": 1,
			"rate": 200,
			"schedule_date": self.date,
		},
	)
	po.save()
	self.fg_subcontracting_po = po.name


def get_scrap_warehouse(department):
	scrap_wareouse = frappe.db.get_value(
		"Warehouse",
		{"department": department, "warehouse_type": "Scrap", "disabled": 0},
		"name",
	)

	if not scrap_wareouse:
		frappe.throw(
			_(
				"No Scrap Warehouse found for department {0}, Configure the Scrap Warehouse for this department."
			).format(department)
		)
	return scrap_wareouse


def validate_gemstone_item(self):
	src_item = json.loads(
		frappe.db.sql(
			"""
			SELECT
				JSON_OBJECTAGG(attribute, attribute_value) AS final_dict
			FROM `tabItem Variant Attribute`
			WHERE parent = %s
			""",
			(self.g_source_item,),
			as_dict=True,
		)[0]["final_dict"]
	)
	s_gemstone_size = src_item.get("Gemstone Size")
	s_gemstone_size = eval("*".join(s_gemstone_size.replace(" MM", "").split("*")))

	for target_row in self.sc_target_table:
		if target_row.item_code == self.g_source_item:
			frappe.throw("Same Item should not allow")

		if target_row.item_code == self.g_loss_item:
			continue

		item = json.loads(
			frappe.db.sql(
				"""
			SELECT
				JSON_OBJECTAGG(attribute, attribute_value) AS final_dict
			FROM `tabItem Variant Attribute`
			WHERE parent = %s
			""",
				(target_row.item_code,),
				as_dict=True,
			)[0]["final_dict"]
		)

		if len(src_item) != len(item):
			frappe.throw(f"Item Missmatch {target_row.item_code}")

		for attribute in src_item:
			if attribute == "Stone Shape" or attribute == "Gemstone PR":
				continue

			elif attribute == "Gemstone Size":
				t_gemstone_size = item.get(attribute)
				t_gemstone_size = eval(
					"*".join(t_gemstone_size.replace(" MM", "").split("*"))
				)
				if s_gemstone_size < t_gemstone_size:
					frappe.throw(
						f"Gemstone Size for this item {target_row.item_code} should not bigger than source item"
					)

			else:
				if src_item.get(attribute) == item.get(attribute):
					continue
				else:
					frappe.throw(
						f"{attribute} Missmatch for this item {target_row.item_code}"
					)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_filtered_batches(doctype, txt, searchfield, start, page_len, filters):
	data = get_batch_no(doctype, txt, searchfield, start, page_len, filters)
	return data
