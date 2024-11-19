# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from jewellery_erpnext.utils import get_item_from_attribute


class MainSlip(Document):
	def autoname(self):
		department = frappe.get_value(
			"Department", self.department, "custom_abbreviation"
		)  # self.department.split("-")[0]
		# initials = department.split(" ")
		if not department:
			frappe.throw(f"{self.department} please set department abbreviation")
		self.dep_abbr = department  # "".join([word[0] for word in initials if word])
		self.type_abbr = self.metal_type[0]
		if self.metal_colour:
			self.color_abbr = self.metal_colour[0]
		elif self.allowed_colours:
			self.color_abbr = str(self.allowed_colours).upper()
		else:
			self.color_abbr = None

	def validate(self):
		if not self.for_subcontracting:
			self.validate_metal_properties()
			self.warehouse = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"company": self.company,
					"employee": self.employee,
					"warehouse_type": "Manufacturing",
				},
			)
			self.raw_material_warehouse = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"company": self.company,
					"employee": self.employee,
					"warehouse_type": "Raw Material",
				},
			)
		else:
			self.warehouse = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"company": self.company,
					"subcontractor": self.subcontractor,
					"warehouse_type": "Manufacturing",
				},
			)

			self.raw_material_warehouse = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"company": self.company,
					"subcontractor": self.subcontractor,
					"warehouse_type": "Raw Material",
				},
			)
		if not self.warehouse:
			# frappe.throw(
			# 	_(
			# 		f"Please set warehouse for {'subcontractor' if self.for_subcontracting else 'employee'}: {self.subcontractor if self.for_subcontracting else self.employee}"
			# 	)
			# )
			frappe.throw(
				_("Please set warehouse for {0}: {1}").format(
					"subcontractor" if self.for_subcontracting else "employee",
					self.subcontractor if self.for_subcontracting else self.employee,
				)
			)
		field_map = {
			"10KT": "wax_to_gold_10",
			"14KT": "wax_to_gold_14",
			"18KT": "wax_to_gold_18",
			"22KT": "wax_to_gold_22",
			"24KT": "wax_to_gold_24",
		}
		if self.is_tree_reqd:
			ratio = frappe.db.get_value(
				"Manufacturing Setting", {"company": self.company}, field_map.get(self.metal_touch)
			)
			self.computed_gold_wt = flt(self.tree_wax_wt) * flt(ratio)
		if (
			not frappe.db.exists("Material Request", {"main_slip": self.name})
			and not self.is_new()
			and self.computed_gold_wt > 0
		):
			create_material_request(self)
		self.update_batch_details()

	def update_batch_details(self):
		batch_details = {}
		for row in self.stock_details:
			if frappe.db.get_value("Item", row.item_code, "variant_of") not in ["M", "F"]:
				continue

			key = (row.item_code, row.batch_no)
			if batch_details.get(key):
				batch_details[key]["mop_qty"] += row.mop_qty
				batch_details[key]["mop_consume_qty"] += row.mop_consume_qty
				batch_details[key]["qty"] += row.qty
				batch_details[key]["consume_qty"] += row.consume_qty
			else:
				batch_details[key] = {
					"qty": row.qty,
					"consume_qty": row.consume_qty,
					"mop_qty": row.mop_qty,
					"mop_consume_qty": row.mop_consume_qty,
					"inventory_type": row.inventory_type,
					"customer": row.customer,
				}

		self.batch_details = []
		loss_details = {}
		self.issue_metal = 0
		self.receive_metal = 0
		self.operation_issue = 0
		self.operation_receive = 0
		self.pending_metal = 0
		for row in batch_details:
			if batch_details[row]["consume_qty"] > batch_details[row]["qty"]:
				frappe.throw(_("Can not consume more material then assigned material for {0}").format(row))
			self.issue_metal += batch_details[row]["qty"]
			self.receive_metal += batch_details[row]["consume_qty"]
			self.operation_issue += batch_details[row]["mop_qty"]
			self.operation_receive += batch_details[row]["mop_consume_qty"]
			self.pending_metal = (self.issue_metal + self.operation_issue) - (
				self.receive_metal + self.operation_receive
			)
			self.append(
				"batch_details",
				{
					"item_code": row[0],
					"batch_no": row[1],
					"qty": batch_details[row]["qty"],
					"consume_qty": batch_details[row]["consume_qty"],
					"mop_qty": batch_details[row]["mop_qty"],
					"mop_consume_qty": batch_details[row]["mop_consume_qty"],
					"inventory_type": batch_details[row]["inventory_type"],
					"customer": batch_details[row].get("customer"),
				},
			)

			loss_details[row[0]] = loss_details.get(row[0], 0) + (
				(batch_details[row]["qty"] - batch_details[row]["consume_qty"])
				+ batch_details[row]["mop_qty"]
				- batch_details[row]["mop_consume_qty"]
			)

		existing_loss_data = {
			row.item_code: {"msl_qty": flt(row.msl_qty, 3), "received_qty": row.received_qty}
			for row in self.loss_details
		}

		self.loss_details = []
		for row in loss_details:
			if loss_details[row] > 0:
				received_qty = 0
				if existing_loss_data.get(row) and existing_loss_data[row]["msl_qty"] == flt(
					loss_details[row], 3
				):
					received_qty = existing_loss_data[row]["received_qty"]
				variant_of = frappe.db.get_value("Item", row, "variant_of")
				self.append(
					"loss_details",
					{
						"item_code": row,
						"variant_of": variant_of,
						"msl_qty": flt(loss_details[row], 3),
						"received_qty": received_qty,
					},
				)

	def on_submit(self):
		not_finished_mop = []
		for row in self.main_slip_operation:
			if (
				frappe.db.get_value("Manufacturing Operation", row.manufacturing_operation, "status")
				!= "Finished"
			):
				if row.manufacturing_work_order not in not_finished_mop:
					not_finished_mop.append(row.manufacturing_work_order)

		if not_finished_mop:
			frappe.throw(
				_("Below mentioned Manufacturing Operations are not finished yet.<br> {0}").format(
					",".join(not_finished_mop)
				)
			)
		for row in self.loss_details:
			create_loss_stock_entries(
				self, row.item_code, row.variant_of, row.received_qty, (row.msl_qty - row.received_qty)
			)

	def validate_metal_properties(self):
		for row in self.main_slip_operation:
			mwo = frappe.db.get_value(
				"Manufacturing Work Order",
				row.manufacturing_work_order,
				[
					"metal_type",
					"metal_touch",
					"metal_purity",
					"metal_colour",
					"multicolour",
					"allowed_colours",
				],
				as_dict=1,
			)
			# if mwo.multicolour == 1:
			# 	if self.multicolour == 0:
			# 		frappe.throw(
			# 			f"Select Multicolour Main Slip </br><b>Metal Properties are: (MT:{mwo.metal_type}, MTC:{mwo.metal_touch}, MP:{mwo.metal_purity}, MC:{mwo.allowed_colours})</b>"
			# 		)
			# 	mwo_allowed_colors = "".join(sorted(map(str.upper, mwo.allowed_colours)))
			# 	ms_allowed_colors = "".join(sorted(map(str.upper, self.allowed_colours)))
			# 	if mwo_allowed_colors and not ms_allowed_colors:
			# 		frappe.throw(
			# 			f"Metal properties in MWO: <b>{row.manufacturing_work_order}</b> do not match the main slip. </br><b>Metal Properties: (MT:{mwo.metal_type}, MTC:{mwo.metal_touch}, MP:{mwo.metal_purity}, MC:{mwo_allowed_colors})</b>"
			# 		)

			# colour_code = {"P": "Pink", "Y": "Yellow", "W": "White"}
			# colour_code = {"P": "P", "Y": "Y", "W": "W"}
			# color_matched = False	 # Flag to check if at least one color matches
			# for char in allowed_colors:
			# 	if char not in colour_code:
			# 		frappe.throw(f"Invalid color code <b>{char}</b> in MWO: <b>{row.manufacturing_work_order}</b>")
			# 	if self.check_color and colour_code[char] == self.allowed_colours:
			# 		color_matched = True	# Set the flag to True if color matches and exit loop
			# 		break
			# 	print(f"{char}{colour_code[char]}{color_matched}")				# Throw an error only if no color matches
			# if self.check_color and not color_matched:
			# 	frappe.throw(f"Metal properties in MWO: <b>{row.manufacturing_work_order}</b> do not match the main slip. </br><b>Metal Properties: (MT:{mwo.metal_type}, MTC:{mwo.metal_touch}, MP:{mwo.metal_purity}, MC:{allowed_colors})</b>")

			if mwo.multicolour == 0:
				if (
					mwo.metal_type != self.metal_type
					or mwo.metal_touch != self.metal_touch
					or mwo.metal_purity != self.metal_purity
					or (self.check_color and mwo.metal_colour != self.metal_colour)
				):
					frappe.throw(
						f"Metal properties in MWO: <b>{row.manufacturing_work_order}</b> do not match the main slip, </br><b>Metal Properties: (MT:{mwo.metal_type}, MTC:{mwo.metal_touch}, MP:{mwo.metal_purity}, MC:{mwo.allowed_colors})</b>"
					)

	def before_insert(self):
		if self.is_tree_reqd:
			self.tree_number = create_tree_number(self)


def create_material_request(doc):
	mr = frappe.new_doc("Material Request")
	mr.material_request_type = "Material Transfer"
	item = get_item_from_attribute(
		doc.metal_type, doc.metal_touch, doc.metal_purity, doc.metal_colour
	)
	if not item:
		return
	mr.schedule_date = frappe.utils.nowdate()
	mr.to_main_slip = doc.name
	mr.department = doc.department
	mr.append(
		"items",
		{
			"item_code": item,
			"qty": doc.computed_gold_wt,
			"warehouse": frappe.db.get_value(
				"Warehouse", {"disabled": 0, "department": doc.department}, "name"
			),
		},
	)
	mr.save()


# def create_tree_number():
# 	doc = frappe.get_doc({"doctype": "Tree Number"}).insert()
# 	return doc.name
def create_tree_number(self):
	doc = frappe.get_doc({"doctype": "Tree Number", "company": self.company}).insert()
	return doc.name


@frappe.whitelist()
def create_stock_entries(
	main_slip, actual_qty, metal_loss, metal_type, metal_touch, metal_purity, metal_colour=None
):
	item = get_item_from_attribute(metal_type, metal_touch, metal_purity, metal_colour)
	if not item:
		frappe.throw(_("No Item found for selected atrributes in main slip"))
	if flt(actual_qty) <= 0:
		return
	doc = frappe.get_doc("Main Slip", main_slip)

	batch_data = []
	for row in doc.batch_details:
		if row.qty != row.consume_qty and row.item_code == item:
			batch_data.append(
				{
					"batch_no": row.batch_no,
					"qty": row.qty - row.consume_qty,
					"inventory_type": row.inventory_type,
				}
			)

	variant_of = frappe.db.get_value("Item", item, "variant_of")

	create_metal_loss(doc, item, variant_of, flt(metal_loss), batch_data)
	stock_entry = frappe.new_doc("Stock Entry")
	stock_entry.stock_entry_type = "Material Transfer to Department"
	stock_entry.main_slip = doc.name
	stock_entry.subcontractor = doc.subcontractor

	for row in batch_data:
		if actual_qty > 0:
			if (row.consume_qty + actual_qty) <= row.qty:
				se_qty = actual_qty
				actual_qty = 0
			else:
				se_qty = row.qty - row.consume_qty
				actual_qty -= se_qty

		stock_entry.append(
			"items",
			{
				"item_code": item,
				"qty": flt(actual_qty),
				"s_warehouse": doc.warehouse,
				"t_warehouse": doc.loss_warehouse,
				"main_slip": main_slip,
				"to_department": doc.department,
				"manufacturer": doc.manufacturer,
				"inventory_type": row.inventory_type,
			},
		)
	stock_entry.save()
	stock_entry.submit()


def create_loss_stock_entries(self, item, variant_of, actual_qty, metal_loss):

	if not item:
		frappe.throw(_("No Item found for selected atrributes in main slip"))
	if flt(actual_qty) <= 0 and metal_loss == 0:
		return

	batch_data = []
	for row in self.batch_details:
		if (row.qty != row.consume_qty) and row.item_code == item:
			batch_data.append(
				{
					"batch_no": row.batch_no,
					"qty": flt(row.qty - row.consume_qty, 3),
					"inventory_type": row.inventory_type,
				}
			)
		elif (row.mop_qty != row.mop_consume_qty) and row.item_code == item:
			batch_data.append(
				{
					"batch_no": row.batch_no,
					"mop_qty": flt(row.mop_qty - row.mop_consume_qty, 3),
					"inventory_type": row.inventory_type,
				}
			)

	create_metal_loss(self, item, variant_of, flt(metal_loss, 3), batch_data)
	if actual_qty > 0:
		stock_entry = frappe.new_doc("Stock Entry")
		stock_entry.stock_entry_type = "Material Transfer to Department"
		stock_entry.main_slip = self.name
		stock_entry.subcontractor = self.subcontractor
		stock_entry.auto_created = 1
		dep_warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"company": self.company,
				"disabled": 0,
				"department": self.department,
				"warehouse_type": "Raw Material",
			},
		)
		for row in batch_data:
			if row.get("qty"):
				warehouse = self.raw_material_warehouse
				qty = row.get("qty")
			elif row.get("mop_qty"):
				warehouse = self.warehouse
				qty = row.get("mop_qty")
			if actual_qty > 0:
				if actual_qty <= qty:
					se_qty = actual_qty
					actual_qty = 0
				else:
					se_qty = qty
					actual_qty -= se_qty

				stock_entry.append(
					"items",
					{
						"item_code": item,
						"qty": flt(se_qty, 3),
						"s_warehouse": warehouse,
						"t_warehouse": dep_warehouse,  # self.loss_warehouse changes by Rajnibhai
						"main_slip": self.name,
						"to_department": self.department,
						"manufacturer": self.manufacturer,
						"inventory_type": row["inventory_type"],
						"batch_no": row["batch_no"],
						"use_serial_batch_fields": 1,
					},
				)

		stock_entry.save()
		stock_entry.submit()


@frappe.whitelist()
def create_process_loss(
	main_slip, mop, item, qty, consume_qty, metal_loss, batch_no, inventory_type, customer=None
):

	qty = flt(qty, 3)
	consume_qty = flt(qty, 3)
	metal_loss = flt(metal_loss, 3)

	doc = frappe.get_doc("Main Slip", main_slip)

	se_doc = frappe.new_doc("Stock Entry")
	se_doc.stock_entry_type = "Process Loss"
	se_doc.purpose = "Repack"
	se_doc.manufacturing_operation = mop
	se_doc.department = doc.department
	se_doc.to_department = doc.department
	se_doc.employee = doc.employee
	se_doc.subcontractor = doc.subcontractor
	se_doc.auto_created = 1

	dust_item = get_item_loss_item(
		doc.company,
		item,
		item[0],
	)
	variant_of = item[0]

	variant_loss_details = frappe.db.get_value(
		"Variant Loss Warehouse",
		{"parent": doc.manufacturer, "variant": variant_of},
		["loss_warehouse", "consider_department_warehouse", "warehouse_type"],
		as_dict=1,
	)

	loss_warehouse = None
	if variant_loss_details:
		if variant_loss_details.get("loss_warehouse"):
			loss_warehouse = variant_loss_details.get("loss_warehouse")

		elif variant_loss_details.get("consider_department_warehouse") and variant_loss_details.get(
			"warehouse_type"
		):
			loss_warehouse = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"department": doc.department,
					"warehouse_type": variant_loss_details.get("warehouse_type"),
				},
			)

	if not loss_warehouse:
		frappe.throw(_("Default loss warehouse is not set in Manufacturer loss table"))

	se_doc.append(
		"items",
		{
			"item_code": item,
			"s_warehouse": doc.raw_material_warehouse,
			"t_warehouse": None,
			"to_employee": None,
			"employee": doc.employee,
			"to_subcontractor": None,
			"use_serial_batch_fields": True,
			"serial_and_batch_bundle": None,
			"subcontractor": doc.subcontractor,
			"main_slip": doc.name,
			"qty": abs(metal_loss),
			"manufacturing_operation": mop,
			"department": doc.department,
			"to_department": doc.department,
			"manufacturer": doc.manufacturer,
			"material_request": None,
			"material_request_item": None,
			"inventory_type": inventory_type,
			"batch_no": batch_no,
		},
	)
	if frappe.db.get_value("Item", dust_item, "valuation_rate") == 0:
		frappe.db.set_value("Item", dust_item, "valuation_rate", se_doc.items[0].get("basic_rate") or 1)
	se_doc.append(
		"items",
		{
			"item_code": dust_item,
			"s_warehouse": None,
			"t_warehouse": loss_warehouse,
			"to_employee": None,
			"employee": doc.employee,
			"to_subcontractor": None,
			"use_serial_batch_fields": True,
			"serial_and_batch_bundle": None,
			"subcontractor": doc.subcontractor,
			"to_main_slip": None,
			"qty": abs(metal_loss),
			"department": doc.department,
			"to_department": doc.department,
			"manufacturer": doc.manufacturer,
			"material_request": None,
			"material_request_item": None,
			"inventory_type": inventory_type,
		},
	)

	se_doc.save()
	se_doc.submit()

	return se_doc.name


def create_metal_loss(doc, item, variant_of, metal_loss, batch_data, mop=None):
	variant_loss_details = frappe.db.get_value(
		"Variant Loss Warehouse",
		{"parent": doc.manufacturer, "variant": variant_of},
		["loss_warehouse", "consider_department_warehouse", "warehouse_type"],
		as_dict=1,
	)

	if variant_loss_details and variant_loss_details.get("loss_warehouse"):
		loss_warehouse = variant_loss_details.get("loss_warehouse")

	elif variant_loss_details.get("consider_department_warehouse") and variant_loss_details.get(
		"warehouse_type"
	):
		loss_warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"department": doc.department,
				"warehouse_type": variant_loss_details.get("warehouse_type"),
			},
		)

	if not loss_warehouse:
		frappe.throw(_("Default loss warehouse is not set in Manufacturer loss table"))

	if metal_loss <= 0:
		return
	metal_loss_item = get_item_loss_item(doc.company, item, variant_of)

	if not item:
		frappe.msgprint(
			_("Please set item for metal loss in Manufacturing Setting for selected company")
		)
		return
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Repack"
	se.main_slip = doc.name
	se.subcontractor = doc.subcontractor
	se.auto_created = 1
	se.manufacturing_opeartion = mop

	repack_qty = metal_loss
	for row in batch_data:
		if row.get("qty"):
			warehouse = doc.raw_material_warehouse
			qty = row.get("qty")
		elif row.get("mop_qty"):
			warehouse = doc.warehouse
			qty = row.get("mop_qty")
		if metal_loss > 0:
			if metal_loss <= qty:
				se_qty = metal_loss
				metal_loss = 0
			else:
				se_qty = qty
				metal_loss -= se_qty
			if row.get("qty"):
				row["qty"] -= se_qty
			elif row.get("mop_qty"):
				row["mop_qty"] -= se_qty
			se.append(
				"items",
				{
					"item_code": item,
					"qty": se_qty,
					"s_warehouse": warehouse,
					"t_warehouse": None,
					"main_slip": doc.name,
					"to_department": doc.department,
					"manufacturer": doc.manufacturer,
					"inventory_type": row["inventory_type"],
					"batch_no": row["batch_no"],
					"use_serial_batch_fields": True,
				},
			)
	se.append(
		"items",
		{
			"item_code": metal_loss_item,
			"qty": repack_qty,
			"s_warehouse": None,
			"t_warehouse": loss_warehouse or warehouse,
			"main_slip": doc.name,
			"to_department": doc.department,
			"manufacturer": doc.manufacturer,
			"inventory_type": "Regular Stock",
		},
	)

	se.save()
	se.submit()


def get_item_loss_item(company, item, variant_of="M", loss_type=None):

	if loss_type:
		variant_name = frappe.db.get_value(
			"Variant Loss Table", {"variant": variant_of, "loss_type": loss_type}, "loss_variant"
		)
	else:
		variant_name = frappe.db.get_value("Variant Loss Table", {"variant": variant_of}, "loss_variant")

	item_attr_dict = {}
	for row in frappe.db.get_all(
		"Item Variant Attribute", {"parent": item}, ["attribute", "attribute_value"]
	):
		item_attr_dict.update({row.attribute: row.attribute_value})
	from jewellery_erpnext.utils import set_items_from_attribute

	# loss_item = get_any_item_from_attribute(variant_name, item_attr_dict)
	loss_item = set_items_from_attribute(
		variant_name,
		frappe.db.get_all(
			"Item Variant Attribute", {"parent": item}, ["attribute as item_attribute", "attribute_value"]
		),
	)

	if loss_item:
		loss_item.has_variants = 0
		loss_item.is_stock_item = 1
		loss_item.save()
		return loss_item.name
	else:
		return create_loss_item(variant_name, item_attr_dict)


def get_main_slip_item(main_slip):
	ms = frappe.db.get_value(
		"Main Slip", main_slip, ["metal_type", "metal_touch", "metal_purity", "metal_colour"], as_dict=1
	)
	item = get_item_from_attribute(ms.metal_type, ms.metal_touch, ms.metal_purity, ms.metal_colour)
	return item


def create_loss_item(item, item_attr_dict):
	from erpnext.controllers.item_variant import create_variant

	variant = create_variant(item, item_attr_dict)
	variant.is_stock_item = 1
	variant.save()

	frappe.throw(f"{variant.is_stock_item} - {variant.is_fixed_asset}")
	return variant.name


@frappe.whitelist()
def get_any_item_from_attribute(variant_of, attributes):
	Item = frappe.qb.DocType("Item")
	ItemVariantAttribute = frappe.qb.DocType("Item Variant Attribute")
	conditions = []
	join_tables = []

	for row in attributes:
		table_name = frappe.scrub(row)
		alias = ItemVariantAttribute.as_(table_name)
		join_tables.append(alias)
		conditions.append((alias.attribute == row) & (alias.attribute_value == attributes[row]))

	# Construct the main query
	query = (
		frappe.qb.from_(Item).select(Item.name.as_("item_code")).where(Item.variant_of == variant_of)
	)
	# Add joins
	for alias in join_tables:
		query = query.left_join(alias).on(Item.name == alias.parent)

	# Apply conditions
	for condition in conditions:
		query = query.where(condition)

	query = query.groupby(Item.name)
	# Execute query
	data = query.run()

	if data:
		return data[0][0]
	return None
