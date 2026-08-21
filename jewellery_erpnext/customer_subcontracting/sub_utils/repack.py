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
		if d.t_warehouse and d.t_warehouse.startswith("Central RM"):
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
		if d.t_warehouse and d.t_warehouse.startswith("Central RM"):
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
			"usage_type": ["in", ["Different Customer Gold", "Same Customer Gold"]],
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
	return isinstance(item_code, str) and item_code.startswith("M-G-")


def process_repack_settlement(
	incoming_batches,
	pending_logs,
	incoming_customer=None,
):
	# RULE B (canonical lock order): pre-lock the source Bins this settlement run will touch,
	# in sorted (item_code, warehouse) order, so two concurrent repack-settlement runs acquire
	# the shared gold batches in the same sequence — breaks 1213 reverse-order deadlock cycles
	# across the per-log nested SE submits below. Additive: does not change what is created.
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_bins,
		preallocate_series_for_docs,
	)

	# Canonical lock order: pin the Stock Entry naming-series row (position 2) BEFORE the
	# Bins so the per-log nested SE submits below are Series-then-Bin like every conformant
	# SE submit -- fixes the Bin-before-Series inversion behind F-002 1213 cycles. The
	# stub mirrors create_gold_repack_entry()'s naming inputs (type + user-default company)
	# so the pinned series prefix matches the row the nested inserts will lock. Additive:
	# SELECT ... FOR UPDATE only, re-entrant with the real naming at insert.
	_series_stub = frappe.new_doc("Stock Entry")
	_series_stub.company = frappe.defaults.get_user_default("Company")
	_series_stub.stock_entry_type = "Subcontracting Repack"
	preallocate_series_for_docs(_series_stub)

	lock_bins(
		[(b.get("item_code"), b.get("warehouse")) for b in (incoming_batches or [])]
	)

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
				incoming["remaining_qty"] * source_purity / 100,
				3,
			)

			consume_pure_qty = min(
				required_pure_qty,
				available_pure_qty,
			)

			consume_gross_qty = flt(
				consume_pure_qty * 100 / source_purity,
				3,
			)

			if available_pure_qty <= 0:
				continue

			if consume_pure_qty <= 0:
				continue

			repack_entry = create_gold_repack_entry(
				source_batch=source_batch,
				target_batch=target_batch,
				qty=consume_gross_qty,
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
				incoming["remaining_qty"] - consume_gross_qty, 3
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
	if not doc.department or not doc.department.startswith("Serial Number"):
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

	if not department or not department.startswith("Serial Number"):
		return

	if not customer:
		return

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

	mwo_type = get_mwo_type_from_pmo(doc.manufacturing_order)

	if mwo_type == "Subcontracting":
		gold_sources = get_flat_available_gold(customer)
	else:
		gold_sources = flatten_gold_sources(get_company_available_gold())

	if not gold_sources:
		frappe.throw(f"Customer Gold Not Available. Customer : {customer}")

	repack_plan = build_mwo_repack_plan(pending_logs, gold_sources)

	if not repack_plan:
		frappe.throw("No Raw Material customer gold is available for repack.")

	for row in repack_plan:
		repack_entry = create_gold_repack_entry(
			source_batch=row["source_batch"],
			target_batch=row["target_batch"],
			qty=row["qty"],
			source_customer=customer,
			reference_log=row["log_name"],
			source_warehouse=row["source_warehouse"],
		)

		update_settlement_log(
			log_name=row["log_name"],
			settled_pure_qty=row["settled_pure_qty"],
			repack_entry=repack_entry,
			settlement_batch=row["source_batch"],
		)


def build_mwo_repack_plan(pending_logs, gold_sources):
	"""Build one complete plan before creating stock entries.

	The preferred plan uses the exact 24KT item of each linked target batch.
	If even one log cannot be fully allocated, all logs fall back to the
	customer's original MTWO item and quantity without purity conversion.
	"""
	logs = [
		log
		for log in pending_logs
		if log.batch_item
		and is_gold_item(log.batch_item)
		and flt(log.balance_pure_qty, 3) > 0
	]

	if not logs:
		return []

	purity_plan = build_24kt_repack_plan(logs, gold_sources)
	if purity_plan:
		return purity_plan

	return build_same_item_repack_plan(logs, gold_sources)


def build_24kt_repack_plan(pending_logs, gold_sources):
	requirements = []

	for log in pending_logs:
		target_batch = get_target_repack_batch(log.usage_batch)
		target_item = frappe.db.get_value("Batch", target_batch, "item")

		if not target_item or "24KT" not in target_item:
			return []

		requirements.append(
			{
				"log": log,
				"target_batch": target_batch,
				"target_item": target_item,
				"qty": flt(log.balance_pure_qty, 3),
				"settled_pure_qty": flt(log.balance_pure_qty, 3),
			}
		)

	return allocate_repack_requirements(requirements, gold_sources)


def build_same_item_repack_plan(pending_logs, gold_sources):
	requirements = []

	for log in pending_logs:
		remaining_qty = get_remaining_log_qty(log)

		if remaining_qty <= 0:
			continue

		requirements.append(
			{
				"log": log,
				"target_batch": log.usage_batch,
				"target_item": log.batch_item,
				"qty": remaining_qty,
				"settled_pure_qty": flt(log.balance_pure_qty, 3),
			}
		)

	return allocate_repack_requirements(requirements, gold_sources)


def get_remaining_log_qty(log):
	quantity = flt(log.quantity, 3)
	pending_pure_qty = flt(log.pending_pure_qty, 3)
	balance_pure_qty = flt(log.balance_pure_qty, 3)

	if pending_pure_qty <= 0:
		return quantity

	return flt(quantity * balance_pure_qty / pending_pure_qty, 3)


def allocate_repack_requirements(requirements, gold_sources):
	available_sources = [
		{
			**source,
			"qty": flt(source["qty"], 3),
		}
		for source in gold_sources
	]

	plan = []

	for requirement in requirements:
		required_qty = flt(requirement["qty"], 3)
		allocated_qty = 0

		requirement_rows = []

		for source in available_sources:
			if source["item_code"] != requirement["target_item"]:
				continue

			if source["qty"] <= 0:
				continue

			consume_qty = min(
				required_qty - allocated_qty,
				source["qty"],
			)

			if consume_qty <= 0:
				continue

			requirement_rows.append(
				{
					"log_name": requirement["log"].name,
					"source_batch": source["batch_no"],
					"source_warehouse": source["warehouse"],
					"target_batch": requirement["target_batch"],
					"qty": flt(consume_qty, 3),
				}
			)

			source["qty"] = flt(
				source["qty"] - consume_qty,
				3,
			)

			allocated_qty = flt(
				allocated_qty + consume_qty,
				3,
			)

			if allocated_qty >= required_qty:
				break

		# No stock at all
		if allocated_qty <= 0:
			continue

		if allocated_qty < required_qty:
			return []

		total_settle = flt(
			requirement["settled_pure_qty"] * allocated_qty / required_qty,
			3,
		)

		remaining_settle = total_settle

		for index, row in enumerate(requirement_rows):
			if index == len(requirement_rows) - 1:
				row["settled_pure_qty"] = remaining_settle

			else:
				settled = flt(
					total_settle * row["qty"] / allocated_qty,
					3,
				)

				row["settled_pure_qty"] = settled

				remaining_settle = flt(
					remaining_settle - settled,
					3,
				)

		plan.extend(requirement_rows)

	return plan


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

			warehouse_type = frappe.db.get_value(
				"Warehouse",
				warehouse,
				"warehouse_type",
			)

			# Only Raw Material warehouse can be used for repack
			if warehouse_type != "Raw Material":
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
	if not department or not department.startswith("Serial Number"):
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
			"item": ["like", "M-G-%"],
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
