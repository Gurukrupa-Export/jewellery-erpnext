# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class CustomerApproval(Document):
	def before_save(self):
		stock_entry_reference = self.stock_entry_reference
		quantity = quantity_calculation(stock_entry_reference)
		for item in self.items:
			for qty in quantity:
				if qty[0] == item.item_code:
					if qty[1] < item.quantity:
						frappe.throw(_("Error: Quantity cannot be greater than the remaining quantity."))

			serial_item = []
			if item.serial_no:
				serial_item.extend(item.serial_no.split("\n"))

			if len(serial_item) > item.quantity:
				frappe.throw(_("Error: There is mismatch in Quantity and Serial Nos, Please recheck it"))

			elif len(serial_item) < item.quantity:
				frappe.throw(_("Error: There are less serial no. Please add"))


@frappe.whitelist()
def get_stock_entry_data(stock_entry_reference):
	doc = frappe.get_doc("Stock Entry", stock_entry_reference)

	quantities = dict(quantity_calculation(stock_entry_reference))
	serial_numbers = serial_no_filter(stock_entry_reference)

	for item in doc.items:
		item_code = item.item_code
		if item_code in quantities:
			item.qty = quantities[item_code]
			for serial_no in serial_numbers:
				if item_code == serial_no["item_code"]:
					item.serial_no = serial_no["serial_no"]
					break
		else:
			item.qty = 0
	return {"items": doc.items, "supporting_staff": doc.custom_supporting_staff}


@frappe.whitelist()
def get_items_filter(doctype, txt, searchfield, start, page_len, filters):
	stock_entry_reference = filters["stock_entry_reference"]
	result = quantity_calculation(stock_entry_reference)
	return result


@frappe.whitelist()
def quantity_calculation(stock_entry_reference):
	StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")
	StockEntry = frappe.qb.DocType("Stock Entry")

	query = (
		frappe.qb.from_(StockEntryDetail)
		.left_join(StockEntry)
		.on(StockEntryDetail.parent == StockEntry.name)
		.select(StockEntryDetail.item_code, StockEntryDetail.qty)
		.where(StockEntry.name.like(stock_entry_reference))
	)
	issue_item = query.run(as_dict=True)

	issue_item = [
		{"item_code": entry["item_code"], "quantity": entry.get("quantity", entry.get("qty"))}
		for entry in issue_item
	]

	query = (
		frappe.qb.from_(StockEntryDetail)
		.left_join(StockEntry)
		.on(StockEntryDetail.parent == StockEntry.name)
		.select(StockEntryDetail.item_code, StockEntryDetail.qty)
		.where(
			(StockEntry.custom_material_return_receipt_number.like(stock_entry_reference))
			& (StockEntry.custom_customer_approval_reference.isnull())
		)
	)
	returned_item = query.run(as_dict=True)

	returned_item = [
		{"item_code": entry["item_code"], "quantity": entry.get("quantity", entry.get("qty"))}
		for entry in returned_item
	]

	SalesOrderItemChild = frappe.qb.DocType("Sales Order Item Child")
	CustomerApproval = frappe.qb.DocType("Customer Approval")

	query = (
		frappe.qb.from_(SalesOrderItemChild)
		.left_join(CustomerApproval)
		.on(SalesOrderItemChild.parent == CustomerApproval.name)
		.select(SalesOrderItemChild.item_code, SalesOrderItemChild.quantity)
		.where(CustomerApproval.stock_entry_reference.like(stock_entry_reference))
	)
	customer_approved_item = query.run(as_dict=True)

	total_item_occupied = returned_item + customer_approved_item

	summed_quantities = {}
	for entry in total_item_occupied:
		item_code = entry["item_code"]
		quantity = entry["quantity"]
		summed_quantities.setdefault(item_code, 0)
		summed_quantities[item_code] += quantity

	total_quantity_dict = {item["item_code"]: item["quantity"] for item in issue_item}

	for item in total_item_occupied:
		item_code = item["item_code"]
		if item_code in total_quantity_dict:
			total_quantity_dict[item_code] -= item["quantity"]

	result = [
		[item_code, quantity] for item_code, quantity in total_quantity_dict.items() if quantity > 0
	]

	return result


@frappe.whitelist()
def serial_no_filter(stock_entry_reference):
	StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")
	StockEntry = frappe.qb.DocType("Stock Entry")

	issue_item_serial_no = (
		frappe.qb.from_(StockEntryDetail)
		.left_join(StockEntry)
		.on(StockEntryDetail.parent == StockEntry.name)
		.select(StockEntryDetail.item_code, StockEntryDetail.serial_no)
		.where((StockEntry.name.like(stock_entry_reference)) & (StockEntryDetail.serial_no.isnotnull()))
	).run(as_dict=True)

	SalesOrderItemChild = frappe.qb.DocType("Sales Order Item Child")
	CustomerApproval = frappe.qb.DocType("Customer Approval")

	customer_approval_item_serial_no = (
		frappe.qb.from_(SalesOrderItemChild)
		.left_join(CustomerApproval)
		.on(SalesOrderItemChild.parent == CustomerApproval.name)
		.select(SalesOrderItemChild.item_code, SalesOrderItemChild.serial_no)
		.where(
			(CustomerApproval.stock_entry_reference.like(stock_entry_reference))
			& (SalesOrderItemChild.serial_no.isnotnull())
		)
	).run(as_dict=True)

	combined_data_ca_serial_no = {}

	for entry in customer_approval_item_serial_no:
		item_code = entry["item_code"]
		serial_no = entry["serial_no"]
		if item_code in combined_data_ca_serial_no:
			combined_data_ca_serial_no[item_code]["serial_no"] += "\n" + serial_no
		else:
			combined_data_ca_serial_no[item_code] = {"item_code": item_code, "serial_no": serial_no}
	customer_approval_item_serial_no = list(combined_data_ca_serial_no.values())

	return_reciept_serial_no = (
		frappe.qb.from_(StockEntryDetail)
		.left_join(StockEntry)
		.on(StockEntryDetail.parent == StockEntry.name)
		.select(StockEntryDetail.item_code, StockEntryDetail.serial_no)
		.where(
			(StockEntry.custom_material_return_receipt_number.like(stock_entry_reference))
			& (StockEntryDetail.serial_no.isnotnull())
		)
	).run(as_dict=True)

	combined_data_rr_serial_no = {}

	for entry in return_reciept_serial_no:
		item_code = entry["item_code"]
		serial_no = entry["serial_no"]
		if item_code in combined_data_rr_serial_no:
			combined_data_rr_serial_no[item_code]["serial_no"] += "\n" + serial_no
		else:
			combined_data_rr_serial_no[item_code] = {"item_code": item_code, "serial_no": serial_no}

	return_reciept_serial_no = list(combined_data_rr_serial_no.values())

	result = []

	for dict_a in issue_item_serial_no:
		item_code = dict_a["item_code"]
		serial_a = set(dict_a["serial_no"].split("\n")) if dict_a["serial_no"] else set()

		dict_b = next((d for d in customer_approval_item_serial_no if d["item_code"] == item_code), None)
		serial_b = set(dict_b["serial_no"].split("\n")) if dict_b and dict_b["serial_no"] else set()

		dict_c = next((d for d in return_reciept_serial_no if d["item_code"] == item_code), None)
		serial_c = set(dict_c["serial_no"].split("\n")) if dict_c and dict_c["serial_no"] else set()

		remaining_serials = serial_a - serial_b - serial_c

		result_dict = {"item_code": item_code, "serial_no": "\n".join(sorted(remaining_serials))}
		result.append(result_dict)
	return result


@frappe.whitelist()
def get_bom_no(serial_no):
	result = frappe.get_value("BOM", {"tag_no": serial_no}, ["name", "gross_weight"])
	if result:
		name, gross_weight = result
	else:
		name, gross_weight = "", ""
	return {"name": name, "gross_weight": gross_weight}
