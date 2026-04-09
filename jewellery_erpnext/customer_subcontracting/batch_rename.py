import string
from datetime import datetime

import frappe
from frappe.utils import flt

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

	prefix = f"{parts[0]}-{parts[1]}"
	parent_serial = parts[-1]

	for row in doc.items:
		if row.s_warehouse or row.batch_no:
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
			batch = frappe.new_doc("Batch")
			batch.batch_id = batch_name
			batch.item = item_code
			batch.reference_doctype = doc.doctype
			batch.reference_name = doc.name
			batch.insert(ignore_permissions=True)

		row.batch_no = batch_name


def create_repack_for_used_other(doc, method=None):
	if doc.stock_entry_type != "Customer Goods Received":
		return

	main_batches = {}
	for row in doc.items:
		if not row.batch_no:
			continue

		batch_no = row.batch_no
		if batch_no in main_batches:
			continue

		customer = (
			getattr(doc, "_customer", None)
			or getattr(doc, "customer", None)
			or row.customer
		)
		main_batches[batch_no] = {
			"item_code": row.item_code,
			"customer": customer,
		}

	if not main_batches:
		return

	batch_to_main = {}
	for batch_no in main_batches:
		linked = get_linked_batches(batch_no)
		for linked_batch in linked:
			if linked_batch not in batch_to_main:
				batch_to_main[linked_batch] = batch_no

	if not batch_to_main:
		return

	try:
		usage_rows = frappe.db.sql(
			"""
			SELECT
				sed.batch_no,
				SUM(sed.qty) AS qty,
				COALESCE(NULLIF(sed.customer, ''), NULLIF(se._customer, ''), NULLIF(se.customer, '')) AS batch_customer,
				mwo.customer AS mwo_customer,
				sed.item_code
			FROM `tabStock Entry Detail` sed
			JOIN `tabStock Entry` se ON se.name = sed.parent
			LEFT JOIN `tabManufacturing Work Order` mwo
				ON mwo.name = se.manufacturing_work_order
			WHERE se.docstatus = 1
				AND se.stock_entry_type LIKE 'Material Transfer%'
				AND sed.s_warehouse IS NOT NULL
				AND sed.batch_no IN %(batch_nos)s
			GROUP BY sed.batch_no,
				COALESCE(NULLIF(sed.customer, ''), NULLIF(se._customer, ''), NULLIF(se.customer, '')),
				mwo.customer,
				sed.item_code
			""",
			{"batch_nos": tuple(batch_to_main.keys())},
			as_dict=1,
		)

		used_other_qty = {}
		for row in usage_rows:
			main_batch = batch_to_main.get(row.batch_no)
			if not main_batch:
				continue

			batch_customer = row.batch_customer or ""
			mwo_customer = row.mwo_customer or ""
			if batch_customer != mwo_customer:
				used_other_qty.setdefault(main_batch, 0)
				used_other_qty[main_batch] += flt(row.qty)

		for batch_no, qty in used_other_qty.items():
			if flt(qty) <= 0:
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
				frappe.logger.info(
					f"Repack already exists for batch {batch_no}; skipping creation."
				)
				continue

			item_code = main_batches[batch_no]["item_code"]
			customer = main_batches[batch_no]["customer"]

			new_se = frappe.new_doc("Stock Entry")
			new_se.stock_entry_type = "Subcontracting Repack"
			new_se.purpose = "Repack"
			new_se.company = doc.company
			new_se.auto_created = 1

			new_se.append(
				"items",
				{
					"item_code": item_code,
					"batch_no": batch_no,
					"qty": qty,
					"s_warehouse": "Central RM - GEPL",
					"customer": customer,
					"is_finished_item": 0,
				},
			)
			new_se.append(
				"items",
				{
					"item_code": item_code,
					"batch_no": batch_no,
					"qty": qty,
					"t_warehouse": "Central RM - GEPL",
					"customer": customer,
					"is_finished_item": 1,
				},
			)

			new_se.save()
			new_se.submit()
			frappe.logger.info(
				f"Created Subcontracting Repack {new_se.name} for CGR batch {batch_no} with qty {qty}"
			)

	except Exception:
		frappe.logger.exception(
			"Failed to create Subcontracting Repack for used-other customer material."
		)
		raise
