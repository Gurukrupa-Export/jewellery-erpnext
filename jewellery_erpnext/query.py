import frappe
from erpnext.controllers.item_variant import create_variant, get_variant
from frappe import _


@frappe.whitelist()
def item_attribute_query(doctype, txt, searchfield, start, page_len, filters):
	args = {
		"item_attribute": filters.get("item_attribute"),
		"txt": "%{0}%".format(txt),
	}

	ItemAttributeValue = frappe.qb.DocType("Item Attribute Value")
	CustomerDiamondGrade = frappe.qb.DocType("Customer Diamond Grade")
	AttributeValue = frappe.qb.DocType("Attribute Value")

	if filters.get("parent_attribute_value"):
		args["parent_attribute_value"] = filters.get("parent_attribute_value")
		query = (
			frappe.qb.from_(ItemAttributeValue)
			.select(ItemAttributeValue.attribute_value)
			.where(
				(ItemAttributeValue.parent == args["item_attribute"])
				& (ItemAttributeValue.attribute_value.like(args["txt"]))
				& (ItemAttributeValue.parent_attribute_value == args["parent_attribute_value"])
			)
		)
		item_attribute = query.run()

	else:
		query = (
			frappe.qb.from_(ItemAttributeValue)
			.select(ItemAttributeValue.attribute_value)
			.where(
				(ItemAttributeValue.parent == filters.get("item_attribute"))
				& (ItemAttributeValue.attribute_value.like(args["txt"]))
			)
		)
		if filters.get("customer_code"):
			customer_code = filters.get("customer_code")
			subquery = (
				frappe.qb.from_(CustomerDiamondGrade)
				.select(CustomerDiamondGrade.diamond_quality)
				.where(CustomerDiamondGrade.parent == customer_code)
			)
			query = query.where(ItemAttributeValue.attribute_value.isin(subquery))

		if filters.get("metal_touch"):
			metal_touch = filters.get("metal_touch")
			subquery = (
				frappe.qb.from_(AttributeValue)
				.select(AttributeValue.name)
				.where(AttributeValue.metal_touch == metal_touch)
			)
			query = query.where(ItemAttributeValue.attribute_value.isin(subquery))
		item_attribute = query.run()

	return item_attribute if item_attribute else []


@frappe.whitelist()
def set_wo_items_grade(doctype, txt, searchfield, start, page_len, filters):
	bom_no = frappe.db.get_value("Sales Order Item", {"parent": filters.get("sales_order")}, "bom")
	frappe.logger("utils").debug(filters.get("sales_order"))
	diamond_quality = frappe.db.get_value("BOM Diamond Detail", {"parent": bom_no}, "quality")
	frappe.logger("utils").debug(diamond_quality)

	SalesOrder = frappe.qb.DocType("Sales Order")
	Customer = frappe.qb.DocType("Customer")
	CustomerDiamondGrade = frappe.qb.DocType("Customer Diamond Grade")

	query = (
		frappe.qb.from_(SalesOrder)
		.left_join(Customer)
		.on(Customer.name == SalesOrder.customer)
		.left_join(CustomerDiamondGrade)
		.on(CustomerDiamondGrade.parent == Customer.name)
		.select(
			CustomerDiamondGrade.diamond_grade_1,
			CustomerDiamondGrade.diamond_grade_2,
			CustomerDiamondGrade.diamond_grade_3,
			CustomerDiamondGrade.diamond_grade_4,
		)
		.where(
			(SalesOrder.name == filters.get("sales_order"))
			& (CustomerDiamondGrade.diamond_quality == diamond_quality)
		)
	)

	data = query.run()
	return tuple(zip(*data))


@frappe.whitelist()
def get_item_code(item_code, grade):
	variant_of = frappe.db.get_value("Item", item_code, "variant_of")
	if variant_of != "D":
		return item_code
	attr_val = frappe.db.get_list(
		"Item Variant Attribute", {"parent": item_code}, ["attribute", "attribute_value"]
	)
	args = {}
	for attr in attr_val:
		if attr.get("attribute") == "Diamond Grade":
			attr["attribute_value"] = grade
		args[attr.get("attribute")] = attr.get("attribute_value")

	variant = get_variant(variant_of, args)
	if not variant:
		variant = create_variant(variant_of, args)
		variant.save()
		return variant.name
	return variant


@frappe.whitelist()
def set_metal_purity(sales_order):
	bom = frappe.db.get_value("Sales Order Item", {"parent": sales_order}, "bom")
	remark = frappe.db.get_value("Sales Order Item", {"parent": sales_order}, "remarks")
	metal_purity = frappe.db.get_value("BOM Metal Detail", {"parent": bom}, "purity_percentage")
	return {"metal_purity": metal_purity, "remark": remark}


@frappe.whitelist()
def get_scrap_items(doctype, txt, searchfield, start, page_len, filters):
	from pypika import functions as fn
	manufacturing_operation = filters.get("manufacturing_operation")

	StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")
	Item = frappe.qb.DocType("Item")

	query = (
		frappe.qb.from_(StockEntryDetail)
		.left_join(Item)
		.on(Item.name == StockEntryDetail.item_code)
		.select(StockEntryDetail.item_code)
		.distinct()
		.where(StockEntryDetail.manufacturing_operation.like(f"%{manufacturing_operation}%"))
	)

	data = query.run()
	return data


@frappe.whitelist()
def diamond_grades_query(doctype, txt, searchfield, start, page_len, filters):
	cond = ""
	args = {
		"customer": filters.get("customer"),
	}
	diamond_quality = None

	CustomerDiamondGrade = frappe.qb.DocType("Customer Diamond Grade")

	query = (
		frappe.qb.from_(CustomerDiamondGrade)
		.select(CustomerDiamondGrade.diamond_quality)
		.where(CustomerDiamondGrade.parent == args["customer"])
	)

	diamond_quality = query.run()

	if diamond_quality:
		return diamond_quality
	else:
		frappe.throw(
			_("Diamond Qulity not Found. Please define the Diamond quality in <strong>Customer</strong>")
		)


@frappe.whitelist()
def get_production_item(doctype, txt, searchfield, start, page_len, filters):
	Item = frappe.qb.DocType("Item")

	query = frappe.qb.from_(Item).select(Item.name).where(Item.item_group == "Designs")

	production_items = query.run()
	return production_items


@frappe.whitelist()
def set_warehouses(filters=None):
	js_values = frappe.db.get_single_value("Jewellery Settings", "*", as_dict=True)
	return js_values


@frappe.whitelist()
def get_wo_operations(doctype, txt, searchfield, start, page_len, filters):

	WorkOrderOperation = frappe.qb.DocType("Work Order Operation")

	query = (
		frappe.qb.from_(WorkOrderOperation)
		.select(WorkOrderOperation.operation)
		.where(WorkOrderOperation.parent == filters.get("work_order"))
	)
	work_order_operations = query.run()

	return work_order_operations


@frappe.whitelist()
def get_parcel_place(doctype, txt, searchfield, start, page_len, filters):
	ParcelPlaceList = frappe.qb.DocType("Parcel Place List")
	ParcelPlaceMultiSelect = frappe.qb.DocType("Parcel Place MultiSelect")

	query = frappe.qb.from_(ParcelPlaceList).select(ParcelPlaceList.parcel_place)

	if customer := filters.get("customer_code"):
		subquery = (
			frappe.qb.from_(ParcelPlaceMultiSelect)
			.select(ParcelPlaceMultiSelect.parcel_place)
			.where(ParcelPlaceMultiSelect.parent == customer)
		)
		query = query.where(ParcelPlaceList.name.isin(subquery))

	parcel_places = query.run()
	return parcel_places

@frappe.whitelist()
def get_customer_mtel_purity(customer,metal_type,metal_touch):
	metal_purity = frappe.db.sql(f"""select metal_purity from `tabMetal Criteria` where parent = '{customer}' and metal_type = '{metal_type}' and metal_touch = '{metal_touch}'""",as_dict=1)
	if not metal_purity:
		frappe.throw(f"Customer {customer} has no metal purity according to metal type {metal_type} and metal touch {metal_touch}")
	return metal_purity[0]['metal_purity']


