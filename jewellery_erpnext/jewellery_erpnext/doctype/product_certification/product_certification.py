# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.utils import cint

from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import get_item_loss_item
from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.doc_events.utils import (
	create_po,
	create_repack_entry,
	update_bom_details,
)


class ProductCertification(Document):
	def validate(self):
		if self.department and not frappe.db.exists(
			"Warehouse", {"disabled": 0, "department": self.department}
		):
			frappe.throw(_("Please set warehouse for selected Department"))

		if self.supplier and not frappe.db.exists(
			"Warehouse", {"disabled": 0, "company": self.company, "subcontractor": self.supplier}
		):
			frappe.throw(_("Please set warehouse for selected supplier"))

		self.validate_items()
		self.update_bom()
		self.get_exploded_table()
		self.distribute_amount()

	def validate_items(self):
		if self.type == "Issue":
			return
		for row in self.product_details:
			if not frappe.db.get_value(
				"Product Details",
				{
					"parent": self.receive_against,
					"serial_no": row.get("serial_no") if row.get("serial_no") else None,
					"item_code": row.item_code,
					"manufacturing_work_order": row.get("manufacturing_work_order")
					if row.get("manufacturing_work_order")
					else None,
					"parent_manufacturing_order": row.get("parent_manufacturing_order")
					if row.get("parent_manufacturing_order")
					else None,
					"tree_no": row.get("tree_no") if row.get("tree_no") else None,
				},
			):
				# frappe.throw(_(f"Row #{row.idx}: item not found in {self.receive_against}"))
				frappe.throw(_("Row #{0}: item not found in {1}").format(row.idx, self.receive_against))

	def update_bom(self):
		if self.service_type in ["Hall Marking Service", "Diamond Certificate service"]:
			for row in self.product_details:
				if not (row.serial_no or row.manufacturing_work_order or row.parent_manufacturing_order):
					# frappe.throw(_(f"Row #{row.idx}: Either select serial no or manufacturing work order"))
					frappe.throw(
						_(
							"Row #{0}: Either select serial no or manufacturing work order or Parent Manufacturing Order"
						).format(row.idx)
					)
				if row.bom:
					continue
				if row.serial_no:
					row.bom = frappe.db.get_value("BOM", {"tag_no": row.serial_no}, "name")
				if not row.bom:
					row.bom = frappe.db.get_value("Item", row.item_code, "master_bom")
				if not row.bom:
					# frappe.throw(_(f"Row #{row.idx}: BOM not found for item or serial no"))
					frappe.throw(_("Row #{0}: BOM not found for item or serial no").format(row.idx))

	def distribute_amount(self):
		if not self.exploded_product_details:
			return
		length = len(self.exploded_product_details)
		if self.type == "Issue":
			self.total_amount = 0
		amt = self.total_amount / length

		qty_data = {}
		for row in self.product_details:
			common_order = row.parent_manufacturing_order or row.manufacturing_work_order
			if qty_data.get((common_order, row.serial_no)):
				qty_data[(common_order, row.serial_no)] += row.total_weight
			else:
				qty_data[(common_order, row.serial_no)] = row.total_weight

		for row in self.exploded_product_details:
			if qty_data.get((common_order, row.serial_no)):
				if row.gross_weight == 0 or not row.gross_weight:
					row.gross_weight = qty_data[(common_order, row.serial_no)]
					qty_data[(common_order, row.serial_no)] = 0
				else:
					qty_data[(common_order, row.serial_no)] -= row.gross_weight
			row.amount = amt

	def on_submit(self):
		create_stock_entry(self)
		self.update_huid()
		create_po(self)
		update_bom_details(self)

	def update_huid(self):
		for row in self.exploded_product_details:
			if row.serial_no:
				add_to_serial_no(row.serial_no, self, row)
			elif row.manufacturing_work_order or row.parent_manufacturing_order:
				if row.huid or row.certification:
					if row.parent_manufacturing_order:
						pmo = row.parent_manufacturing_order
					else:
						pmo = frappe.db.get_value(
							"Manufacturing Work Order", row.manufacturing_work_order, "manufacturing_order"
						)

					pmo_doc = frappe.get_doc("Parent Manufacturing Order", pmo)
					pmo_doc.append(
						"product_certification_details",
						{
							"huid": row.huid,
							"certification_no": row.certification,
							"date": self.date if row.huid else None,
							"certification_date": self.certification_date if row.certification else None,
						},
					)
					pmo_doc.save()

	def get_exploded_table(self):
		exploded_product_details = []
		if self.service_type in ["Hall Marking Service", "Diamond Certificate service"]:
			cat_det = frappe.get_all(
				"Certification Settings", {"parent": "Jewellery Settings"}, ["category", "count"]
			)
			custom_cat = {row.category: row.count for row in cat_det}
			metal_det = None
			for row in self.product_details:
				metal_touch = ""
				metal_colour = frappe.db.get_value("BOM", row.bom, "metal_colour")
				count = 1
				if row.manufacturing_work_order:
					mwo = frappe.db.get_value(
						"Manufacturing Work Order",
						row.manufacturing_work_order,
						["department", "qty", "metal_touch", "metal_colour"],
						as_dict=1,
					)
					if self.department != mwo.department:
						# frappe.throw(_(f"Manufacturing Work Order should be in '{self.department}' department"))
						frappe.throw(
							_("Row {0}: Manufacturing Work Order should be in {1} department").format(
								row.idx, self.department
							)
						)
					count *= cint(mwo.get("qty"))
					metal_touch = mwo.get("metal_touch")
					metal_colour = mwo.get("metal_colour")
				elif row.parent_manufacturing_order:
					departments = frappe.db.get_all(
						"Manufacturing Work Order",
						{"docstatus": 1, "manufacturing_order": row.parent_manufacturing_order, "is_finding_mwo": 0},
						pluck="department",
					)
					department = list(set(departments))

					if len(department) != 1:
						frappe.throw(_("All Manufacturing Work Order should be in same Depratment"))

					if departments and departments[0] != self.department:
						frappe.throw(
							_("Row {0}: Manufacturing Work Order should be in {1} department").format(
								row.idx, self.department
							)
						)
					pmo_data = frappe.db.get_value(
						"Parent Manufacturing Order",
						row.parent_manufacturing_order,
						["qty", "metal_touch", "metal_colour"],
						as_dict=1,
					)
					count *= cint(pmo_data.get("qty"))
					metal_touch = pmo_data.get("metal_touch")
					metal_colour = pmo_data.get("metal_colour")
				else:
					metal_det = frappe.db.get_all("BOM Metal Detail", {"parent": row.bom}, "DISTINCT metal_touch")
					count *= cint(len(metal_det))

				if row.category in custom_cat:
					count *= custom_cat.get(row.category, 1)

				existing = []
				for i in self.exploded_product_details:
					common_order = row.parent_manufacturing_order or row.manufacturing_work_order
					if (
						(row.item_code == i.item_code or row.item_code == "" or not row.item_code)
						and (row.serial_no == i.serial_no or row.serial_no == "" or not row.serial_no)
						and (
							common_order == (i.parent_manufacturing_order or i.manufacturing_work_order)
							or common_order == ""
							or not common_order
						)
					):
						existing.append(i)
				# existing = self.get(
				# 	"exploded_product_details",
				# 	{
				# 		"item_code": row.item_code,
				# 		"serial_no": row.serial_no,
				# 		"manufacturing_work_order": row.manufacturing_work_order,
				# 	},
				# )
				if existing and len(existing) == count:
					continue

				pmo_weights = frappe._dict()

				if row.parent_manufacturing_order or row.manufacturing_work_order:
					pmo_weights = frappe.db.get_value(
						"Parent Manufacturing Order",
						row.parent_manufacturing_order or row.manufacturing_work_order,
						[
							"gross_weight",
							"net_weight",
							"diamond_weight",
							"gemstone_weight",
							"finding_weight",
							"other_weight",
						],
						as_dict=1,
					)
				bom_weights = frappe.db.get_value(
					"BOM",
					row.bom,
					[
						"gross_weight",
						"metal_and_finding_weight",
						"diamond_weight",
						"gemstone_weight",
						"finding_weight_",
						"other_weight",
					],
					as_dict=1,
				)
				for i in range(0, count):
					if metal_det:
						if count == 2 and len(metal_det) < count:
							metal_touch = metal_det[0].get("metal_touch")
						else:
							metal_touch = metal_det[i].get("metal_touch")
					if existing and metal_touch in [a.get("metal_touch") for a in existing]:
						continue
					exploded_product_details.append(
						{
							"item_code": row.item_code,
							"serial_no": row.serial_no,
							"bom": row.bom,
							"gross_weight": pmo_weights.get("gross_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["gross_weight"] / count,
							"gold_weight": pmo_weights.get("net_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["metal_and_finding_weight"] / count,
							"chain_weight": pmo_weights.get("finding_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["finding_weight_"] / count,
							"other_weight": pmo_weights.get("other_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["other_weight"] / count,
							"stone_weight": pmo_weights.get("gemstone_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["gemstone_weight"] / count,
							"diamond_weight": pmo_weights.get("diamond_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["diamond_weight"] / count,
							"parent_manufacturing_order": row.parent_manufacturing_order,
							"manufacturing_work_order": row.manufacturing_work_order,
							"supply_raw_material": bool(row.parent_manufacturing_order or row.manufacturing_work_order),
							"metal_touch": metal_touch,
							"metal_colour": metal_colour,
							"category": row.category,
							"sub_category": row.sub_category,
						}
					)

		elif self.service_type in ["Fire Assy Service", "XRF Services"]:
			if self.manufacturer:
				manufacturer = self.manufacturer
			else:
				manufacturer = frappe.defaults.get_user_default("manufacturer")
			if not manufacturer:	
				frappe.throw("Set manufacturer in session defaults")
			# pure_item = frappe.db.get_value("Manufacturing Setting", self.company, "pure_gold_item")
			pure_item = frappe.db.get_value("Manufacturing Setting", {"manufacturer":self.manufacturer}, "pure_gold_item")
			if not pure_item:
				# frappe.throw(_("Please mention Pure Item in Manufacturing Setting"))
				frappe.throw(_("Select Manufacturer in session defaults or in Filed"))

			existing_data = []
			for row in self.exploded_product_details:
				existing_data.append([row.main_slip, row.tree_no])

			for row in self.product_details:
				if [row.main_slip, row.tree_no] not in existing_data:
					exploded_product_details.append(
						{"item_code": row.item_code, "main_slip": row.main_slip, "tree_no": row.tree_no}
					)
					if self.service_type == "Fire Assy Service":
						exploded_product_details.append(
							{"item_code": pure_item, "main_slip": row.main_slip, "tree_no": row.tree_no}
						)
					loss_item = get_item_loss_item(self.company, row.item_code, "M")
					exploded_product_details.append(
						{"item_code": loss_item, "main_slip": row.main_slip, "tree_no": row.tree_no}
					)
					row.loss_item = loss_item
				row.pure_item = pure_item

		for row in exploded_product_details:
			self.append("exploded_product_details", row)

	@frappe.whitelist()
	def get_item_from_main_slip(self, tree_no):
		metal = frappe.db.get_value(
			"Main Slip",
			{"tree_number": tree_no},
			["metal_type", "metal_touch", "metal_purity", "metal_colour", "name"],
			as_dict=1,
		)
		from jewellery_erpnext.utils import get_item_from_attribute

		return {
			"main_slip": metal.name,
			"item_code": get_item_from_attribute(
				metal.metal_type, metal.metal_touch, metal.metal_purity, metal.metal_colour
			),
		}


def create_stock_entry(doc):
	if doc.type == "Issue" or doc.service_type in [
		"Hall Marking Service",
		"Diamond Certificate service",
	]:

		se_doc = frappe.new_doc("Stock Entry")
		se_doc.stock_entry_type = get_stock_entry_type(doc.service_type, doc.type)
		se_doc.company = doc.company
		se_doc.product_certification = doc.name
		warehouse_type = "Manufacturing"
		# if doc.service_type in ["Fire Assy Service", "XRF Services"]:
		# 	warehouse_type = "Raw Material"
		s_warehouse = frappe.db.exists(
			"Warehouse",
			{
				"department": doc.department,
				"warehouse_type": "Raw Material"
				if doc.service_type in ["Fire Assy Service", "XRF Services"]
				else warehouse_type,
				"disabled": 0,
			},
		)
		t_warehouse = frappe.db.exists(
			"Warehouse",
			{
				"company": doc.company,
				"subcontractor": doc.supplier,
				"warehouse_type": warehouse_type,
				"disabled": 0,
			},
		)

		added_mwo = []
		added_serial = []
		for row in doc.exploded_product_details:
			common_order = row.parent_manufacturing_order or row.manufacturing_work_order
			if row.supply_raw_material and common_order not in added_mwo:
				get_stock_item_against_mwo(se_doc, doc, row, s_warehouse, t_warehouse)
				added_mwo.append(common_order)
			else:
				if (not row.serial_no or row.serial_no in added_serial) and not row.tree_no:
					continue
				added_serial.append(row.serial_no)
				if row.gross_weight > 0:
					se_doc.append(
						"items",
						{
							"item_code": row.item_code,
							"serial_no": row.serial_no,
							"qty": 1 if row.serial_no else row.gross_weight,
							"s_warehouse": s_warehouse if doc.type == "Issue" else t_warehouse,
							"t_warehouse": t_warehouse if doc.type == "Issue" else s_warehouse,
							"Inventory_type": "Regular Stock",
							"reference_doctype": "Serial No",
							"reference_docname": row.serial_no,
							"serial_and_batch_bundle": None,
							"use_serial_batch_fields": True,
							"gross_weight": row.gross_weight,
						},
					)
		if not se_doc.items:
			frappe.throw(_("No item found for Repack"))
		se_doc.flags.throw_batch_error = True
		se_doc.inventory_type = "Regular Stock"
		se_doc.save()
		se_doc.submit()
		frappe.msgprint(_("Stock Entry created"))
	elif doc.type == "Receive" and doc.service_type in ["Fire Assy Service", "XRF Services"]:
		create_repack_entry(doc)


def get_stock_entry_type(txn_type, purpose):
	if purpose == "Issue":
		if txn_type == "Hall Marking Service":
			return "Material Issue for Hallmarking"
		else:
			return "Material Issue for Certification"
	else:
		if txn_type == "Hall Marking Service":
			return "Material Receipt for Hallmarking"
		else:
			return "Material Receipt for Certification"


# def get_stock_item_against_mwo(se_doc, doc, row, s_warehouse, t_warehouse):
# 	if doc.type == "Issue":
# 		target_wh = frappe.get_value(
# 			"Warehouse",
# 			{"disabled": 0, "department": doc.department, "warehouse_type": "Manufacturing"},
# 			"name",
# 		)
# 		filters = [
# 			["Stock Entry MOP Item", "manufacturing_operation", "is", "set"],
# 			["Stock Entry MOP Item", "t_warehouse", "=", target_wh],
# 			["Stock Entry MOP Item", "employee", "is", "not set"],
# 		]
# 		if row.manufacturing_work_order:
# 			filters += (
# 				["Stock Entry MOP Item", "custom_manufacturing_work_order", "=", row.manufacturing_work_order],
# 			)
# 			latest_mop = frappe.db.get_value(
# 				"Manufacturing Work Order", row.manufacturing_work_order, "manufacturing_operation"
# 			)
# 			if latest_mop:
# 				filters += [
# 					["Stock Entry MOP Item", "manufacturing_operation", "=", latest_mop],
# 				]
# 		elif row.parent_manufacturing_order:
# 			filters += (
# 				[
# 					"Stock Entry MOP Item",
# 					"custom_parent_manufacturing_order",
# 					"=",
# 					row.parent_manufacturing_order,
# 				],
# 			)
# 			mwo = frappe.db.get_value(
# 				"Manufacturing Work Order",
# 				{"manufacturing_order": row.parent_manufacturing_order, "is_finding_mwo": 0, "docstatus": 1},
# 			)
# 			if mwo:
# 				latest_mop = frappe.db.get_value("Manufacturing Work Order", mwo, "manufacturing_operation")
# 				if latest_mop:
# 					filters += [
# 						["Stock Entry MOP Item", "manufacturing_operation", "=", latest_mop],
# 					]
# 	else:
# 		filters = [["Stock Entry", "product_certification", "=", doc.receive_against]]
# 		if row.manufacturing_work_order:
# 			filters += [
# 				["Stock Entry MOP Item", "reference_docname", "=", row.manufacturing_work_order],
# 				["Stock Entry MOP Item", "reference_doctype", "=", "Manufacturing Work Order"],
# 			]
# 		elif row.parent_manufacturing_order:
# 			filters += [
# 				["Stock Entry MOP Item", "reference_docname", "=", row.parent_manufacturing_order],
# 				["Stock Entry MOP Item", "reference_doctype", "=", "Parent Manufacturing Order"],
# 			]
# 	stock_entries = frappe.get_all(
# 		"Stock Entry",
# 		filters=filters,
# 		fields=[
# 			"`tabStock Entry MOP Item`.item_code",
# 			"`tabStock Entry MOP Item`.qty",
# 			"`tabStock Entry MOP Item`.batch_no",
# 		],
# 		join="right join",
# 	)
# 	if len(stock_entries) < 1:
# 		frappe.msgprint(_("Row {0} : No Stock entry Found against the Order").format(row.idx))

# 	for item in stock_entries:
# 		se_doc.append(
# 			"items",
# 			{
# 				"item_code": item.item_code,
# 				"qty": item.qty,
# 				"s_warehouse": s_warehouse if doc.type == "Issue" else t_warehouse,
# 				"t_warehouse": t_warehouse if doc.type == "Issue" else s_warehouse,
# 				"Inventory_type": "Regular Stock",
# 				"reference_doctype": "Manufacturing Work Order"
# 				if row.manufacturing_work_order
# 				else "Parent Manufacturing Order",
# 				"reference_docname": row.manufacturing_work_order
# 				if row.manufacturing_work_order
# 				else row.parent_manufacturing_order,
# 				"use_serial_batch_fields": True,
# 				"batch_no": item.get("batch_no"),
# 			},
# 		)

def get_stock_item_against_mwo(se_doc, doc, row, s_warehouse, t_warehouse):
	if doc.type == "Issue":
		target_wh = frappe.get_value(
			"Warehouse",
			{"disabled": 0, "department": doc.department, "warehouse_type": "Manufacturing"},
			"name",
		)

		# Prepare dynamic WHERE clauses
		conditions = [
			"mop_item.t_warehouse = %s",
			"mop_item.manufacturing_operation IS NOT NULL",
			"mop_item.employee IS NULL"
		]
		params = [target_wh]

		or_clauses = []

		# Include conditions for either MWO or Parent MWO
		if row.manufacturing_work_order:
			or_clauses.append("mop_item.custom_manufacturing_work_order = %s")
			params.append(row.manufacturing_work_order)

			latest_mop = frappe.db.get_value(
				"Manufacturing Work Order", row.manufacturing_work_order, "manufacturing_operation"
			)
			if latest_mop:
				or_clauses.append("mop_item.manufacturing_operation = %s")
				params.append(latest_mop)

		if row.parent_manufacturing_order:
			or_clauses.append("mop_item.custom_parent_manufacturing_order = %s")
			params.append(row.parent_manufacturing_order)

			mwo = frappe.db.get_value(
				"Manufacturing Work Order",
				{"manufacturing_order": row.parent_manufacturing_order, "is_finding_mwo": 0, "docstatus": 1},
			)
			if mwo:
				latest_mop = frappe.db.get_value("Manufacturing Work Order", mwo, "manufacturing_operation")
				if latest_mop:
					or_clauses.append("mop_item.manufacturing_operation = %s")
					params.append(latest_mop)

		if or_clauses:
			conditions.append(f"({' OR '.join(or_clauses)})")

	else:
		# For "Receive" type
		conditions = ["se.product_certification = %s"]
		params = [doc.receive_against]

		if row.manufacturing_work_order:
			conditions.append("mop_item.reference_docname = %s")
			params.append(row.manufacturing_work_order)
			conditions.append("mop_item.reference_doctype = 'Manufacturing Work Order'")

		elif row.parent_manufacturing_order:
			conditions.append("mop_item.reference_docname = %s")
			params.append(row.parent_manufacturing_order)
			conditions.append("mop_item.reference_doctype = 'Parent Manufacturing Order'")


	sql = f"""
		SELECT
			mop_item.item_code,
			mop_item.qty,
			mop_item.batch_no
		FROM `tabStock Entry Detail` mop_item
		LEFT JOIN `tabStock Entry` se ON mop_item.parent = se.name
		WHERE {" AND ".join(conditions)}
	"""

	stock_entries = frappe.db.sql(sql, tuple(params), as_dict=True)

	if not stock_entries:
		frappe.msgprint(_("Row {0} : No Stock entry Found against the Order").format(row.idx))

	for item in stock_entries:
		se_doc.append(
			"items",
			{
				"item_code": item.item_code,
				"qty": item.qty,
				"s_warehouse": s_warehouse if doc.type == "Issue" else t_warehouse,
				"t_warehouse": t_warehouse if doc.type == "Issue" else s_warehouse,
				"Inventory_type": "Regular Stock",
				"reference_doctype": "Manufacturing Work Order"
				if row.manufacturing_work_order
				else "Parent Manufacturing Order",
				"reference_docname": row.manufacturing_work_order
				if row.manufacturing_work_order
				else row.parent_manufacturing_order,
				"use_serial_batch_fields": True,
				"batch_no": item.get("batch_no"),
			},
		)


@frappe.whitelist()
def create_product_certification_receive(source_name, target_doc=None):
	def set_missing_values(source, target):
		target.type = "Receive"

	doc = get_mapped_doc(
		"Product Certification",
		source_name,
		{
			"Product Certification": {
				"doctype": "Product Certification",
				"field_map": {"name": "receive_against"},
				"field_no_map": ["date"],
			},
		},
		target_doc,
		set_missing_values,
		ignore_permissions=True,
	)

	return doc


def add_to_serial_no(serial_no, doc, row):
	serial_doc = frappe.get_doc("Serial No", serial_no)
	existing_data = [huild.huid for huild in serial_doc.huid]
	if row.huid and row.huid not in existing_data:
		serial_doc.append("huid", {"huid": row.huid, "date": doc.date})
	serial_doc.save()
