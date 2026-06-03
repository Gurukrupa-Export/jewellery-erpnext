from collections import defaultdict

import frappe
from erpnext.stock.doctype.batch.batch import (
	get_batch_qty,
)
from frappe.utils import date_diff, flt, nowdate

from jewellery_erpnext.customer_subcontracting.report.subcontracting_report.subcontracting_report import (
	get_linked_batches,
)


def create_gold_repack(doc, method=None):
	create_company_gold_repack_automation(doc)
	create_customer_gold_repack_automation(doc)


def create_customer_gold_repack_automation(doc):
	if doc.doctype != "Stock Entry":
		return

	if doc.docstatus != 1:
		return

	if doc.stock_entry_type not in [
		"Customer Goods Received",
		"Customer Goods Transfer",
	]:
		return

	has_target_wh = False

	for d in doc.items:
		if d.t_warehouse == "Central RM - GEPL":
			has_target_wh = True
			break

	if not has_target_wh:
		return

	incoming_customer = doc._customer

	if not incoming_customer:
		return

	incoming_batches = []

	for d in doc.items:
		if not d.batch_no:
			continue

		if not d.item_code:
			continue

		if d.inventory_type != "Customer Goods":
			continue

		if not is_gold_item(d.item_code):
			continue

		incoming_batches.append(
			{
				"batch_no": d.batch_no,
				"qty": flt(d.qty),
				"remaining_qty": flt(d.qty),
				"inventory_type": d.inventory_type,
				"item_code": d.item_code,
				"warehouse": d.t_warehouse,
			}
		)

	if not incoming_batches:
		return

	pending_logs = frappe.get_all(
		"Subcontracting Log",
		filters={
			"settlement_required": 1,
			"settlement_status": ["in", ["Pending", "Partially Settled"]],
			"mwo_type": "Subcontracting",
			"usage_type": ["in", ["Different Customer Gold", "Company Gold"]],
			"customer": incoming_customer,
		},
		fields=["*"],
		order_by="creation asc",
	)

	if not pending_logs:
		return

	process_repack_settlement(
		incoming_batches=incoming_batches,
		pending_logs=pending_logs,
		incoming_customer=incoming_customer,
	)


def create_company_gold_repack_automation(doc):
	if doc.doctype != "Stock Entry":
		return

	if doc.docstatus != 1:
		return

	if doc.stock_entry_type != "Material Transfer (DEPARTMENT)":
		return

	has_target_wh = False

	for d in doc.items:
		if d.t_warehouse == "Central RM - GEPL":
			has_target_wh = True
			break

	if not has_target_wh:
		return

	incoming_batches = []

	for d in doc.items:
		if not d.batch_no:
			continue

		if not d.item_code:
			continue

		if d.inventory_type == "Customer Goods":
			continue

		if not is_gold_item(d.item_code):
			continue

		incoming_batches.append(
			{
				"batch_no": d.batch_no,
				"qty": flt(d.qty),
				"remaining_qty": flt(d.qty),
				"inventory_type": d.inventory_type,
				"item_code": d.item_code,
				"warehouse": d.t_warehouse,
			}
		)
	if not incoming_batches:
		return

	pending_logs = frappe.get_all(
		"Subcontracting Log",
		filters={
			"settlement_required": 1,
			"settlement_status": ["in", ["Pending", "Partially Settled"]],
			"mwo_type": "Regular",
			"usage_type": "Different Customer Gold",
		},
		fields=["*"],
		order_by="creation asc",
	)

	if not pending_logs:
		return

	process_repack_settlement(
		incoming_batches=incoming_batches,
		pending_logs=pending_logs,
		incoming_customer=None,
	)


def is_gold_item(item_code):
	return isinstance(item_code, str) and item_code.startswith("M-")


def process_repack_settlement(
	incoming_batches,
	pending_logs,
	incoming_customer=None,
):
	for log in pending_logs:
		if not log.batch_item or not is_gold_item(log.batch_item):
			continue

		required_pure_qty = flt(log.balance_pure_qty, 3)

		if required_pure_qty <= 0:
			continue

		target_batch = get_target_repack_batch(log.usage_batch)

		if not target_batch:
			continue

		target_purity = get_purity(log.batch_item)

		if not target_purity:
			frappe.log_error(
				f"Purity Not Found : {log.batch_item}",
				"Repack Automation",
			)
			continue

		for incoming in incoming_batches:
			if required_pure_qty <= 0:
				break

			if incoming["remaining_qty"] <= 0:
				continue

			source_batch = incoming["batch_no"]

			source_item_code = incoming["item_code"]

			if not is_gold_item(source_item_code):
				continue

			source_purity = get_purity(incoming["item_code"])

			if not source_purity:
				continue

			available_pure_qty = flt(
				incoming["remaining_qty"] * (source_purity / 100), 3
			)

			if available_pure_qty <= 0:
				continue

			consume_pure_qty = min(required_pure_qty, available_pure_qty)
			if consume_pure_qty <= 0:
				continue

			repack_entry = create_gold_repack_entry(
				source_batch=source_batch,
				target_batch=target_batch,
				qty=consume_pure_qty,
				source_customer=incoming_customer,
				reference_log=log.name,
				source_warehouse=incoming["warehouse"],
			)

			update_settlement_log(
				log_name=log.name,
				settled_pure_qty=consume_pure_qty,
				repack_entry=repack_entry,
				settlement_batch=source_batch,
			)

			incoming["remaining_qty"] = flt(
				incoming["remaining_qty"] - consume_pure_qty, 3
			)

			required_pure_qty = flt(required_pure_qty - consume_pure_qty, 3)


def create_gold_repack_entry(
	source_batch, target_batch, qty, source_customer, reference_log, source_warehouse
):
	source_batch_doc = frappe.get_doc("Batch", source_batch)

	target_batch_doc = frappe.get_doc("Batch", target_batch)
	target_qty = qty

	se = frappe.new_doc("Stock Entry")

	se.stock_entry_type = "Subcontracting Repack"

	se.purpose = "Repack"

	se.company = frappe.defaults.get_user_default("Company")

	se.custom_reference_log = reference_log

	se.append(
		"items",
		{
			"item_code": source_batch_doc.item,
			"batch_no": source_batch,
			"s_warehouse": source_warehouse,
			"qty": qty,
			"customer": source_customer,
			"inventory_type": source_batch_doc.custom_inventory_type,
			"is_finished_item": 0,
			"use_serial_batch_fields": 1,
		},
	)

	se.append(
		"items",
		{
			"item_code": target_batch_doc.item,
			"batch_no": target_batch,
			"t_warehouse": source_warehouse,
			"qty": target_qty,
			"customer": target_batch_doc.custom_customer,
			"inventory_type": target_batch_doc.custom_inventory_type,
			"is_finished_item": 1,
			"use_serial_batch_fields": 1,
		},
	)

	se.insert(ignore_permissions=True)
	se.submit()

	return se.name


def update_settlement_log(
	log_name,
	settled_pure_qty,
	repack_entry,
	settlement_batch,
):
	log = frappe.get_doc("Subcontracting Log", log_name)

	is_gold = is_gold_item(log.batch_item)

	log.settlement_batch = settlement_batch

	log.settled_by_repack = repack_entry

	if is_gold:
		log.settled_pure_qty = flt(log.settled_pure_qty + settled_pure_qty, 3)
		log.balance_pure_qty = flt(log.pending_pure_qty - log.settled_pure_qty, 3)
		balance_qty = log.balance_pure_qty

	else:
		log.settled_pure_qty = flt(log.settled_pure_qty + settled_pure_qty, 3)
		balance_qty = flt(log.quantity - log.settled_pure_qty, 3)

	if balance_qty <= 0:
		log.settlement_status = "Settled"

	else:
		log.settlement_status = "Partially Settled"

	log.save(ignore_permissions=True)


def get_purity(item_code):
	item = frappe.get_doc("Item", item_code)

	purity = 99.9

	for attr in item.attributes:
		if attr.attribute == "Metal Purity":
			purity = flt(attr.attribute_value)

	return purity


def get_target_repack_batch(batch_no):
	if not batch_no:
		return None

	batch_item = frappe.db.get_value("Batch", batch_no, "item")

	if not is_gold_item(batch_item):
		return batch_no

	linked_batches = get_linked_batches(batch_no)

	for linked_batch in linked_batches:
		item_code = frappe.db.get_value("Batch", linked_batch, "item")

		if not item_code:
			continue

		if "24KT" in item_code:
			return linked_batch

	return batch_no


def validate_and_repack_on_mwo_submit(doc, method=None):
	validate_pending_repack_before_submit(doc)
	validate_category_waiting_days(doc)


def validate_pending_repack_before_submit(doc):
	if doc.department != "Serial Number - GEPL":
		return

	pending_logs = frappe.get_all(
		"Subcontracting Log",
		filters={
			"customer": doc.customer,
			"settlement_required": 1,
			"parent_manufacturing_order_name": doc.manufacturing_order,
			"settlement_status": ["!=", "Settled"],
		},
		fields=["name"],
	)

	if pending_logs:
		frappe.throw(
			"Customer Gold Repack Pending.Please click 'Create Repack' before submitting the MWO."
		)


def process_pending_repack_for_mwo(doc_name):
	doc = frappe.get_doc("Manufacturing Work Order", doc_name)

	customer = doc.customer

	department = doc.department

	if department != "Serial Number - GEPL":
		return

	if not customer:
		return

	mwo_type = get_mwo_type_from_pmo(doc.manufacturing_order)

	if mwo_type != "Subcontracting":
		frappe.throw("Create Repack is allowed only for Subcontracting Orders.")

	pending_logs = frappe.get_all(
		"Subcontracting Log",
		filters={
			"customer": customer,
			"settlement_required": 1,
			"settlement_status": ["!=", "Settled"],
			"parent_manufacturing_order_name": doc.manufacturing_order,
		},
		fields=["*"],
		order_by="creation asc",
	)
	if not pending_logs:
		return

	gold_sources = get_flat_available_gold(customer)

	if not gold_sources:
		frappe.throw(f"Customer Gold Not Available. Customer : {customer}")

	pending_message = []

	for log in pending_logs:
		if not log.batch_item or not is_gold_item(log.batch_item):
			continue
		required_pure_qty = flt(log.balance_pure_qty, 3)

		if required_pure_qty <= 0:
			continue

		target_batch = get_target_repack_batch(log.usage_batch)

		if not target_batch:
			continue
		target_item = frappe.db.get_value("Batch", target_batch, "item")
		target_purity = get_purity(target_item)
		gold_sources.sort(
			key=lambda d: (
				get_purity(d["item_code"]) != target_purity,
				-flt(d["purity"]),
			)
		)

		for source in gold_sources:
			source_item = source["item_code"]

			if not is_gold_item(source_item):
				continue

			if required_pure_qty <= 0:
				break

			available_qty = flt(source["qty"], 3)

			if available_qty <= 0:
				continue

			purity = flt(source["purity"], 3)

			available_pure_qty = flt(available_qty * (purity / 100), 3)

			if available_pure_qty <= 0:
				continue

			consume_pure_qty = min(required_pure_qty, available_pure_qty)

			repack_entry = create_gold_repack_entry(
				source_batch=source["batch_no"],
				target_batch=target_batch,
				qty=consume_pure_qty,
				source_customer=customer,
				reference_log=log.name,
				source_warehouse=source["warehouse"],
			)

			update_settlement_log(
				log_name=log.name,
				settled_pure_qty=consume_pure_qty,
				repack_entry=repack_entry,
				settlement_batch=source["batch_no"],
			)

			source["qty"] = flt(source["qty"] - consume_pure_qty, 3)

			required_pure_qty = flt(required_pure_qty - consume_pure_qty, 3)

		if required_pure_qty > 0:
			pending_message.append(
				f"Log : {log.name},Usage Batch : {log.usage_batch},Pending Pure Qty : {required_pure_qty}"
			)

	if pending_message:
		frappe.throw(
			"Partial Repack Completed.Remaining Qty Not Available."
			+ "<br>".join(pending_message)
		)


def get_flat_available_gold(customer):
	available_gold = get_customer_available_gold(customer)

	return flatten_gold_sources(available_gold)


def purity_priority(source):
	purity = flt(source["purity"])

	if purity >= 99:
		return 1

	if purity >= 91:
		return 2

	return 3


def get_customer_available_gold(customer):
	result = defaultdict(lambda: defaultdict(list))

	customer_batches = frappe.get_all(
		"Batch",
		filters={
			"custom_customer": customer,
			"custom_inventory_type": "Customer Goods",
			"disabled": 0,
		},
		fields=["name", "item"],
	)

	for batch in customer_batches:
		if not is_gold_item(batch.item):
			continue
		stock_rows = get_batch_qty(batch_no=batch.name)

		if not stock_rows:
			continue

		for row in stock_rows:
			qty = flt(row.get("qty"))

			if qty <= 0:
				continue

			warehouse = row.get("warehouse")

			if not warehouse:
				continue

			result[batch.item][warehouse].append(
				{
					"batch_no": batch.name,
					"warehouse": warehouse,
					"qty": qty,
					"item_code": batch.item,
				}
			)

	return dict(result)


def validate_category_waiting_days(doc, method=None):
	is_customer_gold = frappe.get_value(
		"Parent Manufacturing Order", doc.manufacturing_order, "is_customer_gold"
	)

	if not is_customer_gold:
		return

	item_category = doc.item_category

	if not item_category:
		return

	settings = frappe.get_single("Subcontracting Settings")

	allowed_days = None

	for row in settings.category:
		if row.item_category == item_category:
			allowed_days = row.days
			break

	if allowed_days is None:
		return

	customer = doc.customer
	department = doc.department
	if department != "Serial Number - GEPL":
		return

	if not customer:
		return

	gold_received_date = frappe.db.sql(
		"""
		SELECT
			MIN(se.posting_date)
		FROM `tabStock Entry` se
		INNER JOIN `tabStock Entry Detail` sed
			ON sed.parent = se.name
		WHERE
			se.docstatus = 1
			AND se.stock_entry_type IN (
				'Customer Goods Received',
				'Customer Goods Transfer'
			)
			AND sed.customer = %s
		""",
		(customer,),
	)[0][0]

	if not gold_received_date:
		frappe.throw(f"Customer Gold Not Received for Customer :{customer}")

	current_date = nowdate()

	diff_days = date_diff(current_date, gold_received_date)

	if diff_days < allowed_days:
		frappe.throw(
			f"Manufacturing Waiting Days Not Completed.Item Category : {item_category} Allowed Days : {allowed_days} Current Days :{diff_days} Gold Received Date :{gold_received_date}"
		)


@frappe.whitelist()
def create_pending_repack(mwo_name):
	process_pending_repack_for_mwo(mwo_name)
	return "Success"


def get_mwo_type_from_pmo(manufacturing_order):
	is_customer_gold = frappe.db.get_value(
		"Parent Manufacturing Order", manufacturing_order, "is_customer_gold"
	)

	if is_customer_gold:
		return "Subcontracting"

	return "Regular"


def get_company_available_gold():
	result = defaultdict(lambda: defaultdict(list))

	company_batches = frappe.get_all(
		"Batch",
		filters={
			"custom_inventory_type": ["!=", "Customer Goods"],
			"disabled": 0,
		},
		fields=["name", "item"],
	)

	for batch in company_batches:
		stock_rows = get_batch_qty(batch_no=batch.name)

		if not stock_rows:
			continue

		for row in stock_rows:
			qty = flt(row.get("qty"))

			if qty <= 0:
				continue

			warehouse = row.get("warehouse")

			if not warehouse:
				continue

			result[batch.item][warehouse].append(
				{
					"batch_no": batch.name,
					"warehouse": warehouse,
					"qty": qty,
					"item_code": batch.item,
				}
			)

	return dict(result)


def flatten_gold_sources(available_gold):
	flat_sources = []

	for item_code, warehouse_data in available_gold.items():
		for warehouse, batches in warehouse_data.items():
			source_purity = get_purity(item_code)

			for batch_data in batches:
				qty = flt(batch_data["qty"], 3)

				if qty <= 0:
					continue

				flat_sources.append(
					{
						"item_code": item_code,
						"warehouse": warehouse,
						"batch_no": batch_data["batch_no"],
						"qty": qty,
						"purity": source_purity,
					}
				)

	flat_sources.sort(key=purity_priority)

	return flat_sources
