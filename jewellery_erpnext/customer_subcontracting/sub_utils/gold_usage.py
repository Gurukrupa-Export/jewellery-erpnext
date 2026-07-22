import frappe


def is_gold_item(item_code):
	return item_code.startswith("M-")


def classify_gold_usage(doc, item):
	order_customer = get_order_customer(doc)

	item_customer = item.customer

	ownership = (
		"Customer Gold" if item.inventory_type == "Customer Goods" else "Company Gold"
	)

	mwo_type = get_mwo_type(doc)

	if not is_gold_item(item.item_code):
		return {
			"mwo_type": mwo_type,
			"usage_type": ownership,
			"used_as_fallback": 0,
			"settlement_required": 0,
			"settlement_status": None,
			"settlement_type": None,
			"settlement_customer": None,
		}

	# CASE 1 ==> Subcontracting + Same Customer Gold
	if (
		mwo_type == "Subcontracting"
		and ownership == "Customer Gold"
		and order_customer == item_customer
	):
		return {
			"mwo_type": "Subcontracting",
			"usage_type": "Same Customer Gold",
			"used_as_fallback": 0,
			"settlement_required": 0,
			"settlement_status": None,
			"settlement_type": None,
			"settlement_customer": None,
		}

	# CASE 2 ==> Subcontracting + Different Customer Gold
	if (
		mwo_type == "Subcontracting"
		and ownership == "Customer Gold"
		and order_customer != item_customer
	):
		return {
			"mwo_type": "Subcontracting",
			"usage_type": "Different Customer Gold",
			"used_as_fallback": 0,
			"settlement_required": 0,
			"settlement_status": "Pending",
			"settlement_type": "Customer Needs Gold",
			"settlement_customer": item_customer,
		}

	# CASE 3 ==> Subcontracting + Company Gold
	if mwo_type == "Subcontracting" and ownership == "Company Gold":
		return {
			"mwo_type": "Subcontracting",
			"usage_type": "Company Gold",
			"used_as_fallback": 0,
			"settlement_required": 0,
			"settlement_status": "Pending",
			"settlement_type": "Company Needs Gold",
			"settlement_customer": order_customer,
		}

	# CASE 4 ==> Regular + Company Gold
	if mwo_type == "Regular" and ownership == "Company Gold":
		return {
			"mwo_type": "Regular",
			"usage_type": "Company Gold",
			"used_as_fallback": 0,
			"settlement_required": 0,
			"settlement_status": None,
			"settlement_type": None,
			"settlement_customer": None,
		}

	# CASE 5 ==> Regular + Different Customer Gold
	if mwo_type == "Regular" and ownership == "Customer Gold":
		return {
			"mwo_type": "Regular",
			"usage_type": (
				"Same Customer Gold"
				if order_customer == item_customer
				else "Different Customer Gold"
			),
			"used_as_fallback": 1,
			"settlement_required": 0,
			"settlement_status": "Pending",
			"settlement_type": "Customer Needs Gold",
			"settlement_customer": item_customer,
		}

	return {}


def get_order_customer(doc):
	from jewellery_erpnext.utils import resolve_pmo_demand_anchor

	pmo = getattr(doc, "manufacturing_order", None)
	if not pmo:
		return None

	# New records derive the customer from the Quotation; legacy records from the Sales Order.
	voucher_type, voucher_no, _detail_no = resolve_pmo_demand_anchor(pmo)
	if not voucher_no:
		return None
	if voucher_type == "Quotation":
		return frappe.db.get_value("Quotation", voucher_no, "party_name")
	return frappe.db.get_value("Sales Order", voucher_no, "customer")


def get_sales_order(doc):
	pmo = getattr(doc, "manufacturing_order", None)
	if not pmo:
		return None

	return frappe.db.get_value("Parent Manufacturing Order", pmo, "sales_order")


def get_mwo_type(doc):
	pmo = getattr(doc, "manufacturing_order", None)
	if not pmo:
		return "Regular"

	is_customer_gold = frappe.db.get_value(
		"Parent Manufacturing Order", pmo, "is_customer_gold"
	)
	if is_customer_gold:
		return "Subcontracting"

	return "Regular"


def get_inventory_data(doc, item, config):
	customer = item.customer or getattr(doc, "_customer", None)
	ownership = (
		"Customer Gold" if item.inventory_type == "Customer Goods" else "Company Gold"
	)

	return {
		"doctype": "Subcontracting Log",
		"reference_doctype": doc.doctype,
		"reference_docname": doc.name,
		"transaction_type": config["transaction_type"],
		"customer": customer,
		"batch": item.batch_no,
		"item": item.item_code,
		"quantity": item.qty,
		"pure_qty": item.custom_pure_qty or 0,
		"inventory_type": item.inventory_type,
		"ownership": ownership,
		"source_warehouse": item.s_warehouse,
		"target_warehouse": item.t_warehouse,
	}


def find_pending_settlements(item):
	ownership = (
		"Customer Gold" if item.inventory_type == "Customer Goods" else "Company Gold"
	)
	filters = {"settlement_required": 1, "settlement_status": ["!=", "Settled"]}

	# customer gold came
	if ownership == "Customer Gold":
		filters.update(
			{
				"settlement_type": "Company Needs Gold",
				"settlement_customer": item.customer,
			}
		)

	# company gold came
	else:
		filters.update({"settlement_type": "Customer Needs Gold"})

	return frappe.get_all(
		"Subcontracting Log", filters=filters, fields=["*"], order_by="creation asc"
	)


def update_pending_settlement(log_name, settled_qty, repack_entry, batch_no):
	log = frappe.get_doc("Subcontracting Log", log_name)
	log.settled_pure_qty += settled_qty
	log.balance_pure_qty = log.pending_pure_qty - log.settled_pure_qty
	log.settlement_batch = batch_no
	log.settled_by_repack = repack_entry
	if log.balance_pure_qty <= 0:
		log.settlement_status = "Settled"
	else:
		log.settlement_status = "Partially Settled"

	log.save(ignore_permissions=True)
