import json

import frappe
from erpnext.controllers.item_variant import create_variant, get_variant
from frappe.desk.reportview import get_filters_cond, get_match_cond
from frappe.query_builder import CustomFunction
from frappe.query_builder.functions import Locate
from frappe.utils import now


@frappe.whitelist()
def set_items_from_attribute(item_template, item_template_attribute):
	if isinstance(item_template_attribute, str):
		item_template_attribute = json.loads(item_template_attribute)
	args = {}
	for row in item_template_attribute:
		if not row.get("attribute_value"):
			frappe.throw(
				f"Row: {row.get('idx')} Please select attribute value for {row.get('item_attribute')}."
			)
		args.update({row.get("item_attribute"): row.get("attribute_value")})
	variant = get_variant(item_template, args)
	if variant:
		return frappe.get_doc("Item", variant)
	else:
		variant = create_variant(item_template, args)
		variant.save()
		return variant


@frappe.whitelist()
def get_item_from_attribute(metal_type, metal_touch, metal_purity, metal_colour=None):
	# items are created without metal_touch as attribute so not considering it in condition for now
	ItemVariantAttribute = frappe.qb.DocType("Item Variant Attribute")
	Item = frappe.qb.DocType("Item")

	# Subqueries for each attribute
	mtp = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(ItemVariantAttribute.parent, ItemVariantAttribute.attribute_value.as_("metal_type"))
		.where(ItemVariantAttribute.attribute == "Metal Type")
	).as_("mtp")

	mt = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(ItemVariantAttribute.parent, ItemVariantAttribute.attribute_value.as_("metal_touch"))
		.where(ItemVariantAttribute.attribute == "Metal Touch")
	).as_("mt")

	mp = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(ItemVariantAttribute.parent, ItemVariantAttribute.attribute_value.as_("metal_purity"))
		.where(ItemVariantAttribute.attribute == "Metal Purity")
	).as_("mp")

	mc = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(ItemVariantAttribute.parent, ItemVariantAttribute.attribute_value.as_("metal_colour"))
		.where(ItemVariantAttribute.attribute == "Metal Colour")
	).as_("mc")

	# Main query with joins and conditions
	query = (
		frappe.qb.from_(mtp)
		.join(mt)
		.on(mt.parent == mtp.parent)
		.join(mp)
		.on(mp.parent == mtp.parent)
		.join(mc)
		.on(mc.parent == mtp.parent)
		.join(Item)
		.on(Item.name == mtp.parent)
		.select(mtp.parent.as_("item_code"))
		.where(
			(Item.variant_of == "M")
			& (mtp.metal_type == metal_type)
			& (mt.metal_touch == metal_touch)
			& (mp.metal_purity == metal_purity)
		)
	)

	if metal_colour:
		query = query.where(mc.metal_colour == metal_colour)

	data = query.run()
	if data:
		return data[0][0]
	return None


@frappe.whitelist()
def get_item_from_attribute_full(metal_type, metal_touch, metal_purity, metal_colour=None):
	# items are created without metal_touch as attribute so not considering it in condition for now
	ItemVariantAttribute = frappe.qb.DocType("Item Variant Attribute")
	Item = frappe.qb.DocType("Item")

	# Subqueries for each attribute
	mtp = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(ItemVariantAttribute.parent, ItemVariantAttribute.attribute_value.as_("metal_type"))
		.where(ItemVariantAttribute.attribute == "Metal Type")
	).as_("mtp")

	mt = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(ItemVariantAttribute.parent, ItemVariantAttribute.attribute_value.as_("metal_touch"))
		.where(ItemVariantAttribute.attribute == "Metal Touch")
	).as_("mt")

	mp = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(ItemVariantAttribute.parent, ItemVariantAttribute.attribute_value.as_("metal_purity"))
		.where(ItemVariantAttribute.attribute == "Metal Purity")
	).as_("mp")

	mc = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(ItemVariantAttribute.parent, ItemVariantAttribute.attribute_value.as_("metal_colour"))
		.where(ItemVariantAttribute.attribute == "Metal Colour")
	).as_("mc")

	# Main query with left joins and conditions
	query = (
		frappe.qb.from_(mtp)
		.left_join(mt)
		.on(mt.parent == mtp.parent)
		.left_join(mp)
		.on(mp.parent == mtp.parent)
		.left_join(mc)
		.on(mc.parent == mtp.parent)
		.right_join(Item)
		.on(Item.name == mtp.parent)
		.select(mtp.parent.as_("item_code"))
		.where(
			(Item.variant_of == "M")
			& (mtp.metal_type == metal_type)
			& (mt.metal_touch == metal_touch)
			& (mp.metal_purity == metal_purity)
		)
	)
	if metal_colour:
		query = query.where(mc.metal_colour == metal_colour)

	data = query.run()

	if data:
		return data
	return None


def get_variant_of_item(item_code):
	return frappe.db.get_value("Item", item_code, "variant_of")


def update_existing(doctype, name, field, value=None, debug=False):
	modified = frappe.utils.now()
	modified_by = frappe.session.user
	Doc = frappe.qb.DocType(doctype)

	query = (
		frappe.qb.update(Doc)
		.set(Doc.modified, modified)
		.set(Doc.modified_by, modified_by)
		.where(Doc.name == name)
	)

	if isinstance(field, dict):
		# If field is a dictionary, prepare multiple field updates
		for key, _value in field.items():
			if isinstance(_value, str) and ("+" in _value or "-" in _value):
				operation = _value.split()
				if (
					len(operation) == 3
					and operation[0] == key
					and operation[2].lstrip("-").replace(".", "", 1).isdigit()
				):
					query = query.set(getattr(Doc, key), getattr(Doc, key) + float(operation[2]))
				else:
					query = query.set(getattr(Doc, key), _value)
			else:
				query = query.set(getattr(Doc, key), _value)
	else:
		# Single field update
		if isinstance(value, str) and ("+" in value or "-" in value):
			operation = value.split()
			if (
				len(operation) == 3
				and operation[0] == field
				and operation[2].lstrip("-").replace(".", "", 1).isdigit()
			):
				query = query.set(getattr(Doc, field), getattr(Doc, field) + float(operation[2]))
			else:
				query = query.set(getattr(Doc, field), value)
		else:
			query = query.set(getattr(Doc, field), value)

	query.run(debug=debug)


def set_values_in_bulk(doctype, doclist, values):
	Doc = frappe.qb.DocType(doctype)
	query = frappe.qb.update(Doc)

	for key, val in values.items():
		query = query.set(key, val)

	query = query.where(Doc.name.isin(doclist))
	query = query.run()


def get_value(doctype, filters, fields, default=None, debug=0):
	Doc = frappe.qb.DocType(doctype)

	fields = fields if isinstance(fields, list) else [fields]

	query = frappe.qb.from_(Doc).select(*fields)

	conditions = []
	for key, value in filters.items():
		if isinstance(value, str):
			value = frappe.db.escape(value)
		conditions.append(Doc[key] == value)

	for condition in conditions:
		query = query.where(condition)

	res = query.run(debug=debug)

	if res:
		return res[0][0] or default

	return default


@frappe.whitelist()
def db_get_value(doctype, docname, fields):
	# this is created to bypass permission issue during db call from client script
	import json

	fields = json.loads(fields)
	return frappe.db.get_value(doctype, docname, fields, as_dict=1)


# searches for customers with Sales Type
@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def customer_query(doctype, txt, searchfield, start, page_len, filters):
	"""query to filter customers with sales type"""

	Customer = frappe.qb.DocType("Customer")
	SalesType = frappe.qb.DocType("Sales Type")

	txt = f"%{txt}%"
	_txt = txt.replace("%", "")

	IF = CustomFunction("IF", ["condition", "true_expr", "false_expr"])

	sales_type_subquery = (
		frappe.qb.from_(SalesType)
		.select(SalesType.parent)
		.where(SalesType.sales_type == filters["sales_type"])
	)

	query = (
		frappe.qb.from_(Customer)
		.select(Customer.name, Customer.customer_name, Customer.customer_group, Customer.territory)
		.where(
			(Customer.docstatus < 2)
			& (Customer.name.isin(sales_type_subquery))
			& (frappe.qb.Field(searchfield).like(txt))
			| (Customer.customer_name.like(txt))
			| (Customer.territory.like(txt))
			| (Customer.customer_group.like(txt))
		)
		.limit(page_len)
		.offset(start)
	)
	# Add match conditions
	match_cond = get_match_cond(doctype)
	if match_cond:
		query = query.where(match_cond)

	# Add ordering conditions
	order_by_conditions = [
		IF(Locate(_txt, Customer.name), Locate(_txt, Customer.name), 99999),
		IF(Locate(_txt, Customer.customer_name), Locate(_txt, Customer.customer_name), 99999),
		IF(Locate(_txt, Customer.customer_group), Locate(_txt, Customer.customer_group), 99999),
		IF(Locate(_txt, Customer.territory), Locate(_txt, Customer.territory), 99999),
		Customer.customer_name,
		Customer.name,
	]
	query = query.orderby(*order_by_conditions)

	customers = query.run()

	return customers


@frappe.whitelist()
def get_sales_invoice_items(sales_invoices):
	"""
	method to get sales invoice item code, qty, rate and serial no
	args:
	                sales_invoices: list of names of sales invoices
	return:
	                List of item details
	"""
	if isinstance(sales_invoices, str):
		sales_invoices = json.loads(sales_invoices)
	return frappe.get_all(
		"Sales Invoice Item",
		{"parent": ["in", sales_invoices]},
		["item_code", "qty", "rate", "serial_no", "bom"],
	)


# searches for suppliers with purchase Type
@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def supplier_query(doctype, txt, searchfield, start, page_len, filters):
	"""query to filter suppliers with purchase type"""
	Supplier = frappe.qb.DocType("Supplier")
	PurchaseType = frappe.qb.DocType("Purchase Type")

	txt = f"%{txt}%"
	_txt = txt.replace("%", "")

	IF = CustomFunction("IF", ["condition", "true_expr", "false_expr"])
	Locate = CustomFunction("LOCATE", ["substr", "str"])

	query_filters = None
	if filters and filters.get("purchase_type"):
		# Subquery to filter suppliers with the specified purchase type
		purchase_type_subquery = (
			frappe.qb.from_(PurchaseType)
			.select(PurchaseType.parent)
			.where(PurchaseType.purchase_type == filters["purchase_type"])
		)
		query_filters = Supplier.name.isin(purchase_type_subquery)

	query = (
		frappe.qb.from_(Supplier)
		.select(Supplier.name, Supplier.supplier_name, Supplier.supplier_group)
		.where(
			(Supplier.docstatus < 2)
			& (
				frappe.qb.Field(searchfield).like(txt)
				| Supplier.supplier_name.like(txt)
				| Supplier.supplier_group.like(txt)
			)
		)
	)

	if query_filters:
		query = query.where(query_filters)

	match_cond = get_match_cond(doctype)
	if match_cond:
		query = query.where(match_cond)

	# Add ordering conditions
	order_by_conditions = [
		IF(Locate(_txt, Supplier.name), Locate(_txt, Supplier.name), 99999),
		IF(Locate(_txt, Supplier.supplier_name), Locate(_txt, Supplier.supplier_name), 99999),
		IF(Locate(_txt, Supplier.supplier_group), Locate(_txt, Supplier.supplier_group), 99999),
		Supplier.supplier_name,
		Supplier.name,
	]
	query = query.orderby(*order_by_conditions).limit(page_len).offset(start)

	suppliers = query.run()
	return suppliers


@frappe.whitelist()
def get_type_of_party(doc, parent, field):
	return frappe.db.get_value(doc, {"parent": parent}, field)
