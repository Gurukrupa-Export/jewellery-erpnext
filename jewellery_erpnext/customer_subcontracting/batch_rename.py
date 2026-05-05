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

		while frappe.db.exists("Batch", batch_name):
			serial = str(int(serial) + 1).zfill(2)
			batch_name = f"{customer}-{year_code}{month}-{item_code}-{serial}"

		frappe.flags.is_batch_autoname = True

		batch = frappe.new_doc("Batch")
		batch.batch_id = batch_name
		batch.item = item_code
		batch.reference_doctype = doc.doctype
		batch.reference_name = doc.name
		batch.custom_customer = doc._customer
		batch.custom_inventory_type = "Customer Goods"
		batch.custom_customer_voucher_type = "Customer Subcontracting"
		batch.insert(ignore_permissions=True)
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

		while frappe.db.exists("Batch", batch_name):
			if alphabet == "Z":
				break

			alphabet = chr(ord(alphabet) + 1)
			batch_name = f"{base_name}-{alphabet}"

		frappe.flags.is_batch_autoname = True

		batch = frappe.new_doc("Batch")
		batch.batch_id = batch_name
		batch.item = item_code
		batch.reference_doctype = doc.doctype
		batch.reference_name = doc.name
		batch.custom_customer = doc._customer
		batch.custom_inventory_type = "Customer Goods"
		batch.custom_customer_voucher_type = "Customer Subcontracting"
		batch.insert(ignore_permissions=True)

		row.batch_no = batch_name


def get_purity(item_code):
	item = frappe.get_doc("Item", item_code)
	purity = 100
	for attr in item.attributes:
		if attr.attribute == "Metal Purity":
			purity = flt(attr.attribute_value)
	return purity


def get_already_repacked_qty(parent_batch, customer):
	result = frappe.db.sql(
		"""
        SELECT IFNULL(SUM(sed_target.qty), 0)
        FROM `tabStock Entry` se
        JOIN `tabStock Entry Detail` sed_target
            ON sed_target.parent = se.name AND sed_target.is_finished_item = 1
        WHERE se.stock_entry_type = 'Subcontracting Repack'
        AND sed_target.batch_no = %s
        AND sed_target.customer = %s
        AND se.docstatus = 1
        """,
		(parent_batch, customer),
	)

	return flt(result[0][0]) if result else 0


def create_repack_for_used_other(doc, method=None):
	if doc.doctype not in ["Stock Entry", "Purchase Receipt"]:
		return

	warehouse_field = "t_warehouse" if doc.doctype == "Stock Entry" else "warehouse"

	if not any(
		getattr(d, warehouse_field, None) == "Central RM - GEPL" for d in doc.items
	):
		return

	source_customer = next((d.customer for d in doc.items if d.customer), None)

	if source_customer:
		return create_forward_repack(doc, source_customer)

	if not any(d.inventory_type == "Regular Stock" for d in doc.items):
		return create_reverse_repack(doc)


def create_forward_repack(doc, source_customer):
	print(f"Creating forward for source customer: {source_customer}")

	columns, report_data = get_report_data(filters={"other_customer": source_customer})
	if not report_data:
		return
	print(f"Report data fetched for forward repack: {report_data}")
	matched_rows = []

	for row in report_data:
		try:
			batch_no = row[0]
			owner = row[1]
			item = row[2]
			used_other = flt(row[5])
			other_customer = row[6] or ""
		except Exception:
			continue

		if not other_customer or used_other <= 0 or not owner:
			continue

		other_customers = [c.strip() for c in other_customer.split(",") if c.strip()]

		if source_customer in other_customers:
			matched_rows.append(
				{
					"batch_no": batch_no,
					"owner": owner,
					"item": item,
					"used_other": flt(used_other),
				}
			)
	print(f"Matched rows for forward repack: {matched_rows}")
	if not matched_rows:
		return

	matched_rows.sort(key=lambda x: x["batch_no"])

	source_batch = next((d.batch_no for d in doc.items if d.batch_no), None)
	if not source_batch:
		return

	total_source_qty = sum(flt(d.qty) for d in doc.items if d.batch_no)
	source_item = next((d.item_code for d in doc.items if d.batch_no), None)

	source_purity = get_purity(source_item)
	total_source_24kt = total_source_qty * (source_purity / 100)
	print(
		f"Total source quantity: {total_source_qty}, source purity: {source_purity}, total source 24kt: {total_source_24kt}"
	)

	remaining_qty = total_source_24kt
	print(f"Initial remaining quantity for repack: {remaining_qty}")

	for row in matched_rows:
		if remaining_qty <= 0:
			break

		child_batch = row["batch_no"]
		used_other = row["used_other"]
		owner = row["owner"]

		if owner == source_customer:
			continue

		linked_batches = get_linked_batches(child_batch)

		parent_batch = None
		for b in linked_batches:
			item = frappe.get_value("Batch", b, "item")
			if item and "24KT" in item:
				parent_batch = b
				break
		print(
			f"Processing child batch {child_batch}, linked batches: {linked_batches}, identified parent batch: {parent_batch}"
		)

		if not parent_batch:
			continue

		parent_item = frappe.get_value("Batch", parent_batch, "item")

		parent_rows = [
			r
			for r in report_data
			if r[0] == parent_batch and (r[6] or "").strip() == owner
		]

		parent_received_back = sum(flt(r[7]) for r in parent_rows) if parent_rows else 0
		print(f"Parent Rows: {parent_rows}")

		print(
			f"Child batch: {child_batch}, Used Other: {used_other}, Parent Batch: {parent_batch}, Parent Received ABck(Same Customer): {parent_received_back}"
		)
		already_repacked = get_already_repacked_qty(parent_batch, owner)

		print(f"DB repacked for {parent_batch}/{owner}: {already_repacked}")

		effective_received = max(parent_received_back, already_repacked)

		if abs(effective_received - used_other) < 0.0001:
			print("Already Balance")
			continue

		if effective_received > used_other:
			print("Over Qty")
			continue

		pending_qty = used_other - effective_received

		if pending_qty <= 0:
			continue

		process_qty = min(pending_qty, remaining_qty)

		converted_qty = process_qty

		create_repack_entry(
			doc,
			source_batch,
			parent_batch,
			source_customer,
			owner,
			parent_item,
			converted_qty,
			is_reverse=False,
		)

		remaining_qty -= process_qty
		print(
			f"Remaining quantity after processing child batch {child_batch}: {remaining_qty}"
		)


def create_reverse_repack(doc, method=None):
	print("Company gold Detected")

	columns, report_data = get_report_data()

	print(f"Report Data: {report_data}")

	if not report_data:
		return

	filtered_rows = []
	count = 0

	for row in report_data:
		try:
			used_other = flt(row[5])
			received_back = flt(row[7])
		except Exception:
			continue

		pending_qty = used_other - received_back

		if used_other > 0 and pending_qty > 0:
			count += 1
			filtered_rows.append(row)
	print(f"Filtered Rows: {filtered_rows}, Count: {count}")

	if not filtered_rows:
		print("No pending used_other found → skipping")
		return

	source_batch = next(
		(
			d.batch_no
			for d in doc.items
			if d.inventory_type == "Regular Stock" and d.batch_no
		),
		None,
	)

	print(f"Source batch: {source_batch}")

	total_incoming_qty = sum(
		flt(d.qty) for d in doc.items if d.inventory_type == "Regular Stock"
	)
	remaining_qty = total_incoming_qty

	print(f"Incoming Qty: {total_incoming_qty}")

	for row in filtered_rows:
		if remaining_qty <= 0:
			break

		try:
			child_batch = row[0]
			owner = row[1]
			used_other = flt(row[5])
			received_back = flt(row[7])
		except Exception:
			continue

		pending_qty = used_other - received_back

		print(
			f"\n➡ Processing: {child_batch} | Owner: {owner} | Pending: {pending_qty}"
		)

		entries = frappe.get_all(
			"Stock Entry Detail",
			filters={"batch_no": child_batch, "docstatus": 1},
			fields=["parent", "s_warehouse", "custom_manufacturing_work_order"],
		)

		mwo = None

		for e in entries:
			if e.s_warehouse and e.custom_manufacturing_work_order:
				mwo = e.custom_manufacturing_work_order
				break

		print(f"Manufacturing Work Order: {mwo}")

		if not mwo:
			continue

		pmo = frappe.db.get_value(
			"Manufacturing Work Order", mwo, "manufacturing_order"
		)

		print(f"PMO: {pmo}")

		if not pmo:
			continue

		is_customer_gold = frappe.db.get_value(
			"Parent Manufacturing Order", pmo, "is_customer_gold"
		)

		print(
			f"Checking:   Child batch:{child_batch}, Owner:{owner}, Pending:{pending_qty}, PMO is_customer_gold: {is_customer_gold}"
		)

		if is_customer_gold:
			continue

		linked_batches = get_linked_batches(child_batch)

		print(f"Linked Batches: {linked_batches}")

		parent_batch = None

		for b in linked_batches:
			item = frappe.get_value("Batch", b, "item")
			if item and "24KT" in item:
				parent_batch = b
				break

		print(f"Parent batch found: {parent_batch}")

		if not parent_batch:
			continue

		parent_item = frappe.get_value("Batch", parent_batch, "item")

		already_repacked = (
			frappe.db.sql(
				"""
            SELECT IFNULL(SUM(sed.qty), 0)
            FROM `tabStock Entry` se
            JOIN `tabStock Entry Detail` sed
                ON sed.parent = se.name AND sed.is_finished_item = 1
            WHERE se.stock_entry_type = 'Subcontracting Repack'
            AND sed.batch_no = %s
            AND se.docstatus = 1
        """,
				(parent_batch,),
			)[0][0]
			or 0
		)

		print(f"Already repacked: {already_repacked}")

		if already_repacked >= used_other:
			print("Over repack")
			continue

		pending_qty = used_other - already_repacked

		process_qty = min(pending_qty, remaining_qty)

		if process_qty <= 0:
			continue

		print(
			f"Repacking {process_qty} from {source_batch} to {parent_batch} for owner {owner}"
		)

		create_repack_entry(
			doc,
			source_batch,
			parent_batch,
			None,
			owner,
			parent_item,
			process_qty,
			is_reverse=True,
		)

		remaining_qty -= process_qty
		print(
			f"Remaining quantity after processing child batch {child_batch}: {remaining_qty}"
		)


def create_repack_entry(
	doc,
	source_batch,
	target_batch,
	source_customer,
	owner,
	item_code,
	qty,
	is_reverse=False,
):
	try:
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Subcontracting Repack"
		se.purpose = "Repack"
		se.company = doc.company

		se.append(
			"items",
			{
				"item_code": item_code,
				"batch_no": source_batch,
				"qty": qty,
				"s_warehouse": "Central RM - GEPL",
				"customer": source_customer if not is_reverse else None,
				"inventory_type": "Regular Stock",
				"is_finished_item": 0,
				"use_serial_batch_fields": 1,
			},
		)

		se.append(
			"items",
			{
				"item_code": item_code,
				"batch_no": target_batch,
				"qty": qty,
				"t_warehouse": "Central RM - GEPL",
				"customer": owner,
				"inventory_type": "Regular Stock",
				"is_finished_item": 1,
				"use_serial_batch_fields": 1,
			},
		)

		se.insert(ignore_permissions=True)
		se.submit()

	except Exception:
		frappe.log_error(frappe.get_traceback(), "Repack Error")
