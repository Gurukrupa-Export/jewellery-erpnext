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
			# s_warehouse = frappe.db.sql(f"""WITH last_se AS (
			# 	SELECT sei.parent AS stock_entry_name
			# 	FROM `tabStock Entry Detail` sei
			# 	WHERE sei.material_request = '{self.name}'
			# 	ORDER BY sei.creation DESC
			# 	LIMIT 1
			# 	)
			# 	SELECT sei.t_warehouse
			# 	FROM `tabStock Entry Detail` sei
			# 	JOIN last_se ON sei.parent = last_se.stock_entry_name
			# 	GROUP BY sei.t_warehouse
			# 	HAVING COUNT(DISTINCT sei.t_warehouse) = 1
			# 	""",as_dict=1)
			# if s_warehouse:
			# 	s_warehouse = s_warehouse[0]['t_warehouse']
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
	# c = (
    # self.custom_material_request_department_transfer[-2].department
    # if len(self.custom_material_request_department_transfer) > 1
    # else self.custom_department
	# )

	if isinstance(self, str):
		self = json.loads(self)
	if not self.get("custom_reserve_se"):
		return
	
	se_doc = frappe.get_doc("Stock Entry", self.get("custom_reserve_se"))


	new_se_doc = frappe.copy_doc(se_doc)
	new_se_doc.stock_entry_type = "Material Transfered to Department"
	new_se_doc.auto_created = 1
	new_se_doc.to_department = self.get("custom_department")
	new_se_doc.add_to_transit = 0
	warehouse_data = frappe._dict()
	t_warehouse = frappe.db.get_value("Warehouse",{"department": self.get("custom_department"), "warehouse_type": "Reserve"},"name")
	if not t_warehouse:
		# self.custom_custom_counter -= 1
		# self.db_set("custom_custom_counter", self.custom_custom_counter)
		# frappe.db.commit()  # force commit
		self.custom_material_request_department_transfer.pop(-1)
		frappe.throw("No warehouse for Selected Department ")


	s_warehouse = frappe.db.sql(f"""WITH last_se AS (
		SELECT sei.parent AS stock_entry_name
		FROM `tabStock Entry Detail` sei
		WHERE sei.material_request = '{self.name}'
		ORDER BY sei.creation DESC
		LIMIT 1
		)
		SELECT sei.t_warehouse
		FROM `tabStock Entry Detail` sei
		JOIN last_se ON sei.parent = last_se.stock_entry_name
		GROUP BY sei.t_warehouse
		HAVING COUNT(DISTINCT sei.t_warehouse) = 1
		""",as_dict=1)
	if s_warehouse:
		s_warehouse = s_warehouse[0]['t_warehouse']
	new_se_doc.to_warehouse = t_warehouse
	for row in new_se_doc.items:
		# 	 = frappe.db.get_value("Warehouse",{"department": c, "warehouse_type":"Reserve"},"name")
		# if not warehouse_data.get(row.material_request_item):
		# 	warehouse_data[row.material_request_item] = frappe.db.get_value(
		# 		"Material Request Item", row.material_request_item, "warehouse"
		# 	)
		# s_warehouse = warehouse_data.get(row.material_request_item)
		row.to_department = self.get("custom_department")
		row.s_warehouse = s_warehouse
		row.t_warehouse = t_warehouse
		row.serial_and_batch_bundle = None


	new_se_doc.save()
	new_se_doc.submit()
	frappe.msgprint(_("Stock Entry Created"))

	if self.custom_material_request_department_transfer:
		last_row = self.custom_material_request_department_transfer[-1]
		last_row.db_set("stock_entry_created", 1)   # update DB
		last_row.stock_entry_created = 1            # update in memory (so UI shows it)

	# self.db_set("custom_mop_se", new_se_doc.name)
	frappe.db.set_value("Material Request", self.get("name"), "custom_reserve_se", new_se_doc.name)

	
	return new_se_doc.name

	# except Exception as e:
	# 	frappe.log_error("data Error", e)
	# 	frappe.throw(str(e))
	# 	return e

@frappe.whitelist()
def update_department_and_create_stock_entry(material_request_name, new_department):
	doc = frappe.get_doc("Material Request", material_request_name)
	current_department = doc.custom_department

	# 1. If department is same, throw
	if new_department == current_department:
		frappe.throw("Raw material is already in this department.")

	# 2. Check if last Stock Entry (Material Transfer) for this MR is already from current to target department
	# Get last Stock Entry for this Material Request
	last_stock_entry = frappe.db.sql("""
		SELECT 
			se.name, sed.s_warehouse, sed.t_warehouse
		FROM 
			`tabStock Entry` se 
			JOIN `tabStock Entry Detail` sed ON se.name = sed.parent
		WHERE 
			se.docstatus = 1
			AND sed.material_request = %s
		ORDER BY se.creation DESC
		LIMIT 1
	""", (material_request_name,), as_dict=1)

	# Set up expected s_warehouse and t_warehouse for new entry
	s_warehouse = None
	t_warehouse = frappe.db.get_value("Warehouse",{"department": new_department, "warehouse_type": "Reserve"},"name")
	if not t_warehouse:
		frappe.throw("No warehouse for Selected Department")

	if last_stock_entry:
		s_warehouse_last = last_stock_entry[0].get("s_warehouse")
		t_warehouse_last = last_stock_entry[0].get("t_warehouse")
		# Your logic for what the new s_warehouse should be
		# s_warehouse = ...
		# Example logic: if equal, throw
		if t_warehouse_last == t_warehouse:
			frappe.throw("Raw material is already in this department.")

	# 3. Update department, reset counter
	doc.db_set("custom_department", new_department)
	doc.db_set("custom_custom_counter", 1)
	doc.db_set("workflow_state", "Material Transferred to Department")
	doc.db_set("custom_operation_type", "Transfer to Department")

    # 4. Call to create Stock Entry
	new_se_name = make_department_stock_entry(doc.as_dict())  # Pass document as dict or as needed
	return new_se_name

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

		s_warehouse = ''
		s_warehouse = frappe.db.sql(f"""WITH last_se AS (
			SELECT sei.parent AS stock_entry_name
			FROM `tabStock Entry Detail` sei
			WHERE sei.material_request = '{self.name}'
			ORDER BY sei.creation DESC
			LIMIT 1
			)
			SELECT sei.t_warehouse
			FROM `tabStock Entry Detail` sei
			JOIN last_se ON sei.parent = last_se.stock_entry_name
			GROUP BY sei.t_warehouse
			HAVING COUNT(DISTINCT sei.t_warehouse) = 1
			""",as_dict=1)
		if s_warehouse:
			s_warehouse = s_warehouse[0]['t_warehouse']
		
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
