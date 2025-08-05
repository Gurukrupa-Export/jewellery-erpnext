import json

import frappe
from frappe import _
from frappe.model.mapper import get_mapped_doc


@frappe.whitelist()
def make_mop_stock_entry(self, **kwargs):
	try:
		if isinstance(self, str):
			self = json.loads(self)
		if not self.get("custom_reserve_se"):
			return

		se_doc = frappe.get_doc("Stock Entry", self.get("custom_reserve_se"))
		mop_data = frappe.db.get_value(
			"Manufacturing Operation",
			kwargs.get("mop"),
			["department", "status", "employee", "department_ir_status"],
			as_dict=1,
		)
		if mop_data.get("department_ir_status") == "In-Transit":
			frappe.throw(
				_("{0} Manufacturing Operation not allowd becuase it is in-transit status.").format(
					kwargs.get("mop")
				)
			)
		new_se_doc = frappe.copy_doc(se_doc)

		new_se_doc.stock_entry_type = "Material Transfer (WORK ORDER)"
		new_se_doc.manufacturing_operation = kwargs.get("mop")
		new_se_doc.auto_created = 1
		new_se_doc.to_department = mop_data.get("department")
		new_se_doc.add_to_transit = 0
		warehouse_data = frappe._dict()
		t_warehouse = frappe.db.get_value(
			"Warehouse",
			{"department": mop_data.get("department"), "warehouse_type": "Manufacturing"},
			"name",
		)
		if mop_data.get("status") == "WIP" and mop_data.get("employee"):
			t_warehouse = frappe.db.get_value(
				"Warehouse", {"employee": mop_data.get("employee"), "warehouse_type": "Manufacturing"}, "name"
			)
		for row in new_se_doc.items:
			if not warehouse_data.get(row.material_request_item):
				warehouse_data[row.material_request_item] = frappe.db.get_value(
					"Material Request Item", row.material_request_item, "warehouse"
				)
			s_warehouse = warehouse_data.get(row.material_request_item)

			row.s_warehouse = s_warehouse
			row.t_warehouse = t_warehouse
			row.to_department = mop_data.get("department")
			row.manufacturing_operation = kwargs.get("mop")
			row.serial_and_batch_bundle = None

		new_se_doc.save()
		new_se_doc.submit()
		frappe.msgprint(_("Stock Entry Created"))
		self.db_set("custom_mop_se", new_se_doc.name)
		# frappe.db.set_value("Material Request", self.get("name"), "custom_mop_se", new_se_doc.name)

		return new_se_doc.name

	except Exception as e:
		frappe.log_error("data Error", e)
		frappe.throw(str(e))
		return e

@frappe.whitelist()
def make_department_stock_entry(self, **kwargs):
	# try:
	if isinstance(self, str):
		self = json.loads(self)
	if not self.get("custom_reserve_se"):
		return
	
	se_doc = frappe.get_doc("Stock Entry", self.get("custom_reserve_se"))
	new_se_doc = frappe.copy_doc(se_doc)
	new_se_doc.stock_entry_type = "Material Transfered to Department"
	new_se_doc.auto_created = 1
	new_se_doc.to_department = self.get("department")
	new_se_doc.add_to_transit = 0
	warehouse_data = frappe._dict()
	t_warehouse = frappe.db.get_value("Warehouse",{"department": self.get("custom_department"), "warehouse_type": "Reserve"},"name")
	if not t_warehouse:
		frappe.throw("No warehouse for Selected Department ")

	new_se_doc.to_warehouse = t_warehouse
	for row in new_se_doc.items:
		if not warehouse_data.get(row.material_request_item):
			warehouse_data[row.material_request_item] = frappe.db.get_value(
				"Material Request Item", row.material_request_item, "warehouse"
			)
		s_warehouse = warehouse_data.get(row.material_request_item)
		row.to_department = self.get("custom_department")
		row.s_warehouse = s_warehouse
		row.t_warehouse = t_warehouse
		row.serial_and_batch_bundle = None

	new_se_doc.save()
	new_se_doc.submit()
	frappe.msgprint(_("Stock Entry Created"))
	self.db_set("custom_mop_se", new_se_doc.name)
	# frappe.db.set_value("Material Request", self.get("name"), "custom_mop_se", new_se_doc.name)

	return new_se_doc.name

	# except Exception as e:
	# 	frappe.log_error("data Error", e)
	# 	frappe.throw(str(e))
	# 	return e

@frappe.whitelist()
def make_department_mop_stock_entry(self, **kwargs):
	try:
		if isinstance(self, str):
			self = json.loads(self)
		if not self.get("custom_reserve_se"):
			return
		
		se_doc = frappe.get_doc("Stock Entry", self.get("custom_reserve_se"))
		mop_data = frappe.db.get_value(
			"Manufacturing Operation",
			kwargs.get("mop"),
			["department", "status", "employee", "department_ir_status"],
			as_dict=1,
		)
		if mop_data.get("department_ir_status") == "In-Transit":
			frappe.throw(
				_("{0} Manufacturing Operation not allowd becuase it is in-transit status.").format(
					kwargs.get("mop")
				)
			)
		new_se_doc = frappe.copy_doc(se_doc)

		new_se_doc.stock_entry_type = "Material Transfer (WORK ORDER)"
		new_se_doc.manufacturing_operation = kwargs.get("mop")
		new_se_doc.auto_created = 1
		new_se_doc.to_department = self.get("custom_department")
		new_se_doc.add_to_transit = 0
		t_warehouse = frappe.db.get_value(
			"Warehouse",
			{"department": mop_data.get("department"), "warehouse_type": "Manufacturing"},
			"name",
		)
		if mop_data.get("status") == "WIP" and mop_data.get("employee"):
			t_warehouse = frappe.db.get_value(
				"Warehouse", {"employee": mop_data.get("employee"), "warehouse_type": "Manufacturing"}, "name"
			)
		for row in new_se_doc.items:
			s_warehouse = frappe.db.get_value("Warehouse",{"department":self.custom_department,"warehouse_type":"Reserve"},"name")
			row.s_warehouse = s_warehouse
			row.t_warehouse = t_warehouse
			row.manufacturing_operation = kwargs.get("mop")
			row.serial_and_batch_bundle = None

		new_se_doc.save()
		new_se_doc.submit()
		frappe.msgprint(_("Stock Entry Created"))
		self.db_set("custom_mop_se", new_se_doc.name)

		return new_se_doc.name

	except Exception as e:
		frappe.log_error("data Error", e)
		frappe.throw(str(e))
		return e

@frappe.whitelist()
def get_pmo_data(source_name, target_doc=None):
	def set_missing_values(source, target):

		MR = frappe.qb.DocType("Stock Entry")
		MRI = frappe.qb.DocType("Stock Entry Detail")

		materail_data = (
			frappe.qb.from_(MR)
			.join(MRI)
			.on(MR.name == MRI.parent)
			.select(
				MRI.item_code,
				MRI.qty,
				MRI.uom,
				MRI.basic_rate,
				MRI.inventory_type,
				MRI.customer,
				MRI.conversion_factor,
				MRI.t_warehouse,
				MRI.s_warehouse,
				MRI.batch_no,
			)
			.where(MRI.custom_parent_manufacturing_order == source_name)
			.where(MR.docstatus == 1)
			.where(MR.stock_entry_type == "Material Transfer From Reserve")
		)

		if target.custom_item_type:
			variant_of_dict = {"Gemstone": "G", "Diamond": "D"}
			if variant_of_dict.get(target.custom_item_type):
				materail_data = materail_data.where(
					MRI.custom_variant_of == variant_of_dict.get(target.custom_item_type)
				)

		materail_data = materail_data.run(as_dict=True)

		for row in materail_data:
			target.append(
				"items",
				{
					"warehouse": row.t_warehouse,
					"from_warehouse": row.s_warehouse,
					"item_code": row.item_code,
					"qty": row.qty,
					"uom": row.uom,
					"conversion_factor": row.conversion_factor,
					"rate": row.rate,
					"inventory_type": row.inventory_type,
					"customer": row.get("customer"),
					"batch_no": row.get("batch_no"),
				},
			)

		target.manufacturing_order = source_name

		target.set_missing_values()

	doclist = get_mapped_doc(
		"Parent Manufacturing Order",
		source_name,
		{
			"Parent Manufacturing Order": {
				"validation": {"docstatus": ["=", 1]},
			},
		},
		target_doc,
		set_missing_values,
	)

	return doclist
