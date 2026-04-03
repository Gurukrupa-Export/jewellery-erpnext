import string
from datetime import datetime

import frappe


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

		if row.t_warehouse:
			continue

		customer = getattr(doc, "_customer", None) or getattr(row, "customer", None)

		if not customer:
			continue

		year_code = get_year_code()
		month = datetime.today().strftime("%m")

		item_code = row.item_code

		serial = get_next_serial(customer, year_code, month)

		batch_name = f"{customer}-{year_code}{month}-{item_code}-{serial}"

		frappe.flags.is_batch_autoname = True

		if not frappe.db.exists("Batch", batch_name):
			batch = frappe.get_doc(
				{
					"doctype": "Batch",
					"batch_id": batch_name,
					"item": item_code,
					"reference_doctype": doc.doctype,
					"reference_name": doc.name,
				}
			)

			batch.save()
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

	prefix = f"{parts[0]}-{parts[1]}"
	parent_serial = parts[-1]

	for row in doc.items:
		if row.s_warehouse and row.batch_no:
			continue

		if not row.t_warehouse:
			continue

		item_code = row.item_code
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
			batch = frappe.get_doc(
				{
					"doctype": "Batch",
					"batch_id": batch_name,
					"item": item_code,
					"reference_doctype": doc.doctype,
					"reference_name": doc.name,
				}
			)
			batch.insert(ignore_permissions=True)

		row.batch_no = batch_name
