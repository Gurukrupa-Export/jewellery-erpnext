import string
from datetime import datetime

import frappe
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting.report.subcontracting_report.subcontracting_report import (
	execute as get_report_data,
)
from jewellery_erpnext.customer_subcontracting.report.subcontracting_report.subcontracting_report import (
	get_linked_batches,
)


def create_parent_batches(doc, method=None):
	if doc.doctype == "Stock Entry":
		if getattr(doc, "stock_entry_type", None) not in [
			"Customer Goods Received",
			"Subcontracting Repack",
		]:
			return

	elif doc.doctype == "Purchase Receipt":
		if getattr(doc, "purchase_type", None) != "Subcontracting":
			return

	else:
		return

	for row in doc.items:
		if not row.item_code:
			continue

		if "24KT" not in row.item_code:
			continue

		if row.batch_no:
			continue

		customer = getattr(row, "customer", None) or getattr(doc, "_customer", None)

		if not customer:
			continue

		year_code = get_year_code()
		month = datetime.today().strftime("%m")

		item_code = row.item_code

		serial = get_next_serial(customer, year_code, month)

		batch_name = f"{customer}-{year_code}{month}-{item_code}-{serial}"

		frappe.flags.is_batch_autoname = True

		if not frappe.db.exists("Batch", batch_name):
			batch = frappe.new_doc("Batch")
			batch.batch_id = batch_name
			batch.item = item_code
			batch.reference_doctype = doc.doctype
			batch.reference_name = doc.name
			batch.custom_customer = doc._customer
			batch.custom_inventory_type = "Customer Goods"
			batch.custom_customer_voucher_type = "Customer Subcontracting"
			batch.insert()
		row.batch_no = batch_name


def get_year_code():
	year_dict = {
		"1": "A",
		"2": "B",
		"3": "C",
		"4": "D",
		"5": "E",
		"6": "F",
		"7": "G",
		"8": "H",
		"9": "I",
		"0": "J",
	}

	year = datetime.today().year
	last_two = str(year)[-2:]

	return last_two[0] + year_dict[last_two[1]]


def get_next_serial(customer, year_code, month):
	prefix = f"{customer}-{year_code}{month}"

	batch = frappe.db.sql(
		"""
        SELECT name
        FROM `tabBatch`
        WHERE name LIKE %s
        ORDER BY name DESC
        LIMIT 1
        """,
		(prefix + "%",),
		as_dict=True,
	)

	if batch:
		last_serial = batch[0].name.split("-")[-1]

		if last_serial.isdigit():
			next_serial = int(last_serial) + 1
			return str(next_serial).zfill(2)

	return "01"


def create_child_batches(doc, method=None):
	if doc.doctype != "Stock Entry":
		return

	parent_batch = None
	for r in doc.items:
		if r.s_warehouse and r.batch_no:
			parent_batch = r.batch_no
			break

	if not parent_batch:
		return

	parts = parent_batch.split("-")
	if len(parts) < 4:
		return

	parent_serial = parts[-1]

	for row in doc.items:
		if row.s_warehouse or row.batch_no:
			continue

		if not row.t_warehouse:
			continue

		item_code = row.item_code
		customer = getattr(row, "customer", None)

		if customer:
			prefix = f"{customer}-{parts[1]}"
		else:
			prefix = f"{parts[0]}-{parts[1]}"
		base_name = f"{prefix}-{item_code}-{parent_serial}"

		batches = frappe.db.sql(
			"""
            SELECT name
            FROM `tabBatch`
            WHERE name LIKE %s
            ORDER BY name DESC
            """,
			(base_name + "-%",),
			as_dict=True,
		)

		alphabet = "A"
		if batches:
			last_alpha = batches[0].name.split("-")[-1]
			if last_alpha in string.ascii_uppercase:
				idx = string.ascii_uppercase.index(last_alpha)
				alphabet = string.ascii_uppercase[idx + 1]

		batch_name = f"{base_name}-{alphabet}"

		frappe.flags.is_batch_autoname = True

		if not frappe.db.exists("Batch", batch_name):
			batch = frappe.new_doc("Batch")
			batch.batch_id = batch_name
			batch.item = item_code
			batch.reference_doctype = doc.doctype
			batch.reference_name = doc.name
			batch.custom_customer = doc._customer
			batch.custom_inventory_type = "Customer Goods"
			batch.custom_customer_voucher_type = "Customer Subcontracting"
			batch.insert()

		row.batch_no = batch_name


def get_purity(item_code):
	item = frappe.get_doc("Item", item_code)
	purity = 100
	for attr in item.attributes:
		if attr.attribute == "Metal Purity":
			purity = flt(attr.attribute_value)

	return purity


def create_repack_for_used_other(doc, method=None):
	if doc.doctype != "Stock Entry":
		return

	if doc.stock_entry_type != "Customer Goods Received":
		return

	for item in doc.items:
		if item.t_warehouse != "Central RM - GEPL":
			return

	source_customer = (
		getattr(doc, "_customer", None)
		or getattr(doc, "customer", None)
		or next(row.customer for row in doc.items if getattr(row, "customer", None))
	)

	if not source_customer:
		return []

	columns, report_data = get_report_data(filters={"other_customer": source_customer})

	if not report_data:
		return []

	matched_rows = []

	for row in report_data:
		try:
			bacth_no = row[0]
			owner = row[1]
			item = row[2]
			opening_qty = row[3]
			used_other = row[5]
			other_customer = row[6]
		except Exception:
			continue

		if not other_customer or used_other <= 0:
			continue

		if source_customer in (other_customer or ""):
			matched_rows.append(
				{
					"batch_no": bacth_no,
					"owner": owner,
					"item": item,
					"opening_qty": opening_qty,
					"used_other": used_other,
					"other_customer": other_customer,
				}
			)

	if not matched_rows:
		return []

	last_row = matched_rows[-1]

	child_batch = last_row["batch_no"]
	item_code = last_row["item"]
	used_other = last_row["used_other"]
	owner = last_row["owner"]

	linked_batches = get_linked_batches(child_batch)

	parent_batch = None

	for b in linked_batches:
		try:
			batch_doc = frappe.get_doc("Batch", b)
			item = batch_doc.item

			if item and "24KT" in item:
				parent_batch = b
				break
		except Exception as e:
			frappe.log_error(title="Batch Error", message=str(e))

	if not parent_batch:
		return []

	purity = get_purity(item_code)
	converted_qty = used_other * (purity / 100)

	parent_item = frappe.get_value("Batch", parent_batch, "item")
	for doc_row in doc.items:
		if not doc_row.batch_no:
			continue

		source_batch = doc_row.batch_no
		source_qty = flt(doc_row.qty)

		exists = frappe.db.exists(
			"Stock Entry Detail",
			{
				"batch_no": parent_batch,
				"is_finished_item": 1,
				"docstatus": 1,
			},
		)

		if exists:
			return

		try:
			se = frappe.new_doc("Stock Entry")
			se.stock_entry_type = "Subcontracting Repack"
			se.purpose = "Repack"
			se.company = doc.company

			# SOurce
			se.append(
				"items",
				{
					"item_code": parent_item,
					"batch_no": source_batch,
					"qty": converted_qty,
					"s_warehouse": "Central RM - GEPL",
					"customer": source_customer,
					"inventory_type": "Regular Stock",
					"is_finished_item": 0,
					"use_serial_batch_fields": 1,
				},
			)

			se.append(
				"items",
				{
					"item_code": parent_item,
					"batch_no": parent_batch,
					"qty": converted_qty,
					"t_warehouse": "RM Procurement - GEPL",
					"customer": owner,
					"inventory_type": "Regular Stock",
					"is_finished_item": 1,
					"use_serial_batch_fields": 1,
				},
			)

			se.insert()
			se.submit()

		except Exception as e:
			frappe.log_error(title="Repack Error", message=str(e))
