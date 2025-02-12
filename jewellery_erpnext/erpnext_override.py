import frappe
from erpnext.stock.get_item_details import check_packing_list
from frappe.query_builder.functions import IfNull, Sum
from frappe.utils import flt


def get_price_list_rate_for(args, item_code):
	"""
	:param customer: link to Customer DocType
	:param supplier: link to Supplier DocType
	:param price_list: str (Standard Buying or Standard Selling)
	:param item_code: str, Item Doctype field item_code
	:param qty: Desired Qty
	:param transaction_date: Date of the price
	"""
	item_price_args = {
		"item_code": item_code,
		"price_list": args.get("price_list"),
		"customer": args.get("customer"),
		"supplier": args.get("supplier"),
		"uom": args.get("uom"),
		"transaction_date": args.get("transaction_date"),
		"posting_date": args.get("posting_date"),
		"batch_no": args.get("batch_no"),
		"bom_no": args.get("bom_no"),  # jewellery change
	}

	item_price_data = 0
	price_list_rate = get_item_price(item_price_args, item_code)
	if price_list_rate:
		desired_qty = args.get("qty")
		if desired_qty and check_packing_list(price_list_rate[0][0], desired_qty, item_code):
			item_price_data = price_list_rate
	else:
		for field in ["customer", "supplier"]:
			del item_price_args[field]

		general_price_list_rate = get_item_price(
			item_price_args, item_code, ignore_party=args.get("ignore_party")
		)

		if not general_price_list_rate and args.get("uom") != args.get("stock_uom"):
			item_price_args["uom"] = args.get("stock_uom")
			general_price_list_rate = get_item_price(
				item_price_args, item_code, ignore_party=args.get("ignore_party")
			)

		if general_price_list_rate:
			item_price_data = general_price_list_rate

	if item_price_data:
		if item_price_data[0][2] == args.get("uom"):
			return item_price_data[0][1]
		elif not args.get("price_list_uom_dependant"):
			return flt(item_price_data[0][1] * flt(args.get("conversion_factor", 1)))
		else:
			return item_price_data[0][1]


def get_item_price(args, item_code, ignore_party=False):
	"""
	Get name, price_list_rate from Item Price based on conditions
	        Check if the desired qty is within the increment of the packing list.
	:param args: dict (or frappe._dict) with mandatory fields price_list, uom
	        optional fields transaction_date, customer, supplier
	:param item_code: str, Item Doctype field item_code
	"""

	ip = frappe.qb.DocType("Item Price")
	query = (
		frappe.qb.from_(ip)
		.select(ip.name, ip.price_list_rate, ip.uom)
		.where(
			(ip.item_code == item_code)
			& (ip.price_list == args.get("price_list"))
			& (IfNull(ip.uom, "").isin(["", args.get("uom")]))
			& (IfNull(ip.batch_no, "").isin(["", args.get("batch_no")]))
		)
		.orderby(ip.valid_from, order=frappe.qb.desc)
		.orderby(IfNull(ip.batch_no, ""), order=frappe.qb.desc)
		.orderby(ip.uom, order=frappe.qb.desc)
	)

	if not ignore_party:
		if args.get("customer"):
			query = query.where(ip.customer == args.get("customer"))
		elif args.get("supplier"):
			query = query.where(ip.supplier == args.get("supplier"))
		else:
			query = query.where((IfNull(ip.customer, "") == "") & (IfNull(ip.supplier, "") == ""))

	if args.get("transaction_date"):
		query = query.where(
			(IfNull(ip.valid_from, "2000-01-01") <= args["transaction_date"])
			& (IfNull(ip.valid_upto, "2500-12-31") >= args["transaction_date"])
		)

	return query.run()
