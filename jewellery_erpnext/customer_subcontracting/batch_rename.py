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
			batch.insert(ignore_permissions=True)

		row.batch_no = batch_name


def create_repack_for_used_other(doc, method=None):
	if doc.docstatus != 1:
		return

	if getattr(doc, "customer_voucher_type", None) != "Customer Subcontracting":
		return

	customer_goods_rows = [
		row
		for row in doc.items
		if getattr(row, "inventory_type", None) == "Customer Goods"
	]

	if not customer_goods_rows:
		return

	if (
		not any(getattr(row, "customer", None) for row in doc.items)
		and not getattr(doc, "customer", None)
		and not getattr(doc, "_customer", None)
	):
		return

	central_rm_rows = [
		row
		for row in doc.items
		if getattr(row, "t_warehouse", None) == "Central RM - GEPL"
	]
	if not central_rm_rows:
		return

	customer = (
		getattr(doc, "_customer", None)
		or getattr(doc, "customer", None)
		or next(
			(row.customer for row in doc.items if getattr(row, "customer", None)), None
		)
	)

	if not customer:
		return

	try:
		columns, report_data = get_report_data(filters={"customer": customer})

		if not report_data:
			return

		main_batches = {}
		for row in doc.items:
			if row.batch_no:
				linked = get_linked_batches(row.batch_no)
				for batch in linked:
					main_batches[batch] = {
						"item_code": row.item_code,
						"customer": customer,
						"batch_no": row.batch_no,
					}

		repack_created_count = 0
		for report_row in report_data:
			batch_no = report_row[0]
			owner = report_row[1]
			item_code = report_row[2]
			used_other = flt(report_row[5])

			if used_other <= 0:
				continue

			if batch_no not in main_batches:
				continue

			existing = frappe.db.sql(
				"""
				SELECT se.name
				FROM `tabStock Entry` se
				JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
				WHERE se.stock_entry_type = 'Subcontracting Repack'
					AND se.docstatus = 1
					AND sed.batch_no = %s
				LIMIT 1
				""",
				(batch_no,),
				as_dict=1,
			)
			if existing:
				continue

			new_se = frappe.new_doc("Stock Entry")
			new_se.stock_entry_type = "Subcontracting Repack"
			new_se.purpose = "Repack"
			new_se.company = doc.company
			new_se.append(
				"items",
				{
					"item_code": item_code,
					"batch_no": batch_no,
					"qty": used_other,
					"s_warehouse": "Central RM - GEPL",
					"customer": owner,
					"is_finished_item": 0,
				},
			)
			new_se.append(
				"items",
				{
					"item_code": item_code,
					"batch_no": batch_no,
					"qty": used_other,
					"t_warehouse": "Central RM - GEPL",
					"customer": owner,
					"is_finished_item": 1,
				},
			)

			new_se.save()
			new_se.submit()
			repack_created_count += 1
			frappe.log_error(
				f"Created Subcontracting Repack {new_se.name} for batch {batch_no} with qty {used_other}"
			)

		frappe.log_error(
			f"Repack creation completed: {repack_created_count} repack entries created for customer {customer}"
		)

	except Exception as e:
		frappe.log_error(
			f"Failed to create Subcontracting Repack for used-other customer material: {str(e)}"
		)
		raise
