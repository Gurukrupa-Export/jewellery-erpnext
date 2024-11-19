import frappe
from frappe.query_builder import Case
from frappe.query_builder.functions import Locate


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def item_query_filters(doctype, txt, searchfield, start, page_len, filters):

	Item = frappe.qb.DocType("Item")

	loss_variants = frappe.db.get_list("Item", {"custom_is_loss_item": 1}, pluck="name")

	query = (
		frappe.qb.from_(Item)
		.select(Item.name, Item.item_name, Item.item_group)
		.where(Item.is_stock_item == 1)
		.where(Item.has_variants == 0)
	)

	if loss_variants:
		query = query.where(Item.variant_of.notin(loss_variants))

	# Construct the query with search conditions
	query = (
		query.where(
			(Item[searchfield].like(f"%{txt}%"))
			| (Item.item_name.like(f"%{txt}%"))
			| (Item.item_group.like(f"%{txt}%"))
		)
		.orderby(Case().when(Locate(txt, Item.name) > 0, Locate(txt, Item.name)).else_(99999))
		.orderby(Case().when(Locate(txt, Item.item_name) > 0, Locate(txt, Item.item_name)).else_(99999))
		.orderby(
			Case().when(Locate(txt, Item.item_group) > 0, Locate(txt, Item.item_group)).else_(99999)
		)
		.orderby(Item.idx, order=frappe.qb.desc)
		.orderby(Item.name)
		.orderby(Item.item_name)
		.orderby(Item.item_group)
		.limit(page_len)
		.offset(start)
	)
	data = query.run()
	return data


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def warehouse_query_filters(doctype, txt, searchfield, start, page_len, filters):
	filters["department"] = frappe.db.get_value(
		"Employee", {"user_id": frappe.session.user}, "department"
	)

	conditions = get_filters_cond(filters)

	Warehouse = frappe.qb.DocType("Warehouse")

	query = (
		frappe.qb.from_(Warehouse)
		.select(Warehouse.name, Warehouse.warehouse_name)
		.where(Warehouse.is_group == 0)
		.where(Warehouse.company == filters["company"])
	)
	# Add the dynamic conditions
	for condition in conditions:
		query = query.where(condition)

	# Construct the query with search conditions
	query = (
		query.where(
			(Warehouse[searchfield].like(f"%{txt}%")) | (Warehouse.warehouse_name.like(f"%{txt}%"))
		)
		.orderby(Case().when(Locate(txt, Warehouse.name) > 0, Locate(txt, Warehouse.name)).else_(99999))
		.orderby(
			Case()
			.when(Locate(txt, Warehouse.warehouse_name) > 0, Locate(txt, Warehouse.warehouse_name))
			.else_(99999)
		)
		.orderby(Warehouse.idx, order=frappe.qb.desc)
		.orderby(Warehouse.name)
		.orderby(Warehouse.warehouse_name)
		.limit(page_len)
		.offset(start)
	)
	data = query.run()
	return data


def get_filters_cond(filters):
	Warehouse = frappe.qb.DocType("Warehouse")
	conditions = []

	if filters["stock_entry_type"] == "Material Transfer (DEPARTMENT)" and filters.get("department"):
		raw_department = frappe.db.get_value(
			"Warehouse",
			{"disabled": 0, "warehouse_type": "Raw Material", "department": filters.get("department")},
		)
		if raw_department:
			conditions.append((Warehouse.warehouse_type == "Transit") | (Warehouse.name == raw_department))
		else:
			conditions.append(Warehouse.warehouse_type == "Transit")

	elif filters["stock_entry_type"] in (
		"Material Transfer (MAIN SLIP)",
		"Material Transfer (Subcontracting Work Order)",
	) and filters.get("department"):
		raw_department = frappe.db.get_value(
			"Warehouse",
			{"disabled": 0, "warehouse_type": "Raw Material", "department": filters.get("department")},
		)

		if filters["stock_entry_type"] == "Material Transfer (MAIN SLIP)":
			conditions.append((Warehouse.employee != "") | (Warehouse.employee.isnull().negate()))
		else:
			conditions.append((Warehouse.subcontracter != "") | (Warehouse.subcontracter.isnull().negate()))

		if raw_department and filters["stock_entry_type"] == "Material Transfer (MAIN SLIP)":
			conditions.append(
				(Warehouse.employee != "")
				| (Warehouse.employee.isnull().negate())
				| (Warehouse.name == raw_department) & (Warehouse.warehouse_type == "Raw Material")
			)
		elif raw_department:
			conditions.append(
				(Warehouse.subcontracter != "")
				| (Warehouse.subcontracter.isnull().negate())
				| (Warehouse.name == raw_department) & (Warehouse.warehouse_type == "Raw Material")
			)
		else:
			conditions.append((Warehouse.subcontracter != "") | (Warehouse.subcontracter.isnull().negate()))

	elif filters["stock_entry_type"] == "Material Transfer (WORK ORDER)" and filters.get(
		"department"
	):
		raw_department = frappe.db.get_value(
			"Warehouse",
			{"disabled": 0, "warehouse_type": "Raw Material", "department": filters.get("department")},
		)
		condition = (Warehouse.warehouse_type == "Manufacturing") & (
			((Warehouse.employee != "") | (Warehouse.employee.isnull().negate()))
			| ((Warehouse.department != "") | (Warehouse.department.isnull().negate()))
		)
		if raw_department:
			conditions.append(condition | (Warehouse.name == raw_department))
		else:
			conditions.append(condition)

	return conditions
