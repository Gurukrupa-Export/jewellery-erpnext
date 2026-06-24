import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	create_mr_wo_stock_entry,
	get_make_receive_entry_rows,
)

SOURCE_WAREHOUSE = "Central RM - GEPL"
TARGET_WAREHOUSE = "MPM WO - GEPL"
PURITY_PRIORITY = ("24KT", "22KT", "20KT", "18KT")


@frappe.whitelist()
def create_snc(stock_entry):
	if isinstance(stock_entry, str):
		stock_entry = frappe.get_doc("Stock Entry", stock_entry)

	ctx = validate_snc(stock_entry)
	make_receive = trigger_make_receive(stock_entry)
	created = {"make_receive": make_receive, "transfers": [], "conversions": []}

	for row in _get_customer_gold_rows(stock_entry, ctx["mwo"].customer):
		batch = get_customer_batch(ctx["mwo"].customer, row["item_code"], row["qty"])
		if not batch:
			conversion = create_metal_conversion(
				source_batch=_get_conversion_source_batch(
					ctx["mwo"].customer, row["item_code"], row["custom_pure_qty"]
				),
				required_item_code=row["item_code"],
				required_pure_qty=row["custom_pure_qty"],
				required_qty=row["qty"],
				source_entry=stock_entry,
				mwo=ctx["mwo"],
			)
			created["conversions"].append(conversion["stock_entry"])
			batch = conversion["target_batch"]

		transfer = create_work_order_transfer(
			source_entry=stock_entry,
			source_row=row,
			batch_no=batch["batch_no"],
			qty=row["qty"],
			mwo=ctx["mwo"],
		)
		created["transfers"].append(transfer)

	return created


def validate_snc(stock_entry):
	if (
		stock_entry.stock_entry_type != "Material Transfer (WORK ORDER)"
		or stock_entry.docstatus != 1
	):
		frappe.throw(
			_("SNC can be created only from submitted Material Transfer (WORK ORDER).")
		)

	if not stock_entry.manufacturing_work_order:
		frappe.throw(_("Manufacturing Work Order is required for SNC."))

	mwo = frappe.get_doc(
		"Manufacturing Work Order", stock_entry.manufacturing_work_order
	)
	if cint(mwo.get("is_finding_mwo")):
		frappe.throw(
			_("SNC is allowed only for Finished Goods Manufacturing Work Orders.")
		)

	if not mwo.customer:
		frappe.throw(
			_("Customer is required on Manufacturing Work Order {0}.").format(mwo.name)
		)

	if not mwo.manufacturing_order:
		frappe.throw(
			_(
				"Parent Manufacturing Order is required on Manufacturing Work Order {0}."
			).format(mwo.name)
		)

	pmo = frappe.get_doc("Parent Manufacturing Order", mwo.manufacturing_order)
	if cint(pmo.is_customer_gold) != 1:
		frappe.throw(
			_("SNC is allowed only when Parent Manufacturing Order is Customer Gold.")
		)

	rows = _get_customer_gold_rows(stock_entry, mwo.customer)
	if not rows:
		frappe.throw(
			_(
				"No finished-goods Customer Gold rows with a different batch customer were found."
			)
		)

	return {"mwo": mwo, "pmo": pmo, "rows": rows}


def trigger_make_receive(stock_entry):
	if not stock_entry.manufacturing_operation:
		frappe.throw(_("Manufacturing Operation is required to trigger Make Receive."))

	rows = (
		get_make_receive_entry_rows(stock_entry.manufacturing_operation).get("rows")
		or []
	)
	receive_items = []
	for row in rows:
		qty = flt(row.get("available_to_receive_qty"))
		if qty <= 0:
			continue
		receive_items.append(
			{
				"stock_reservation_entry": row.get("stock_reservation_entry"),
				"stock_reservation_entry_detail": row.get(
					"stock_reservation_entry_detail"
				),
				"item_code": row.get("item_code"),
				"batch_no": row.get("batch_no"),
				"qty": qty,
				"pcs": cint(row.get("available_to_receive_pcs") or 0),
				"inventory_type": row.get("inventory_type"),
				"customer": row.get("customer"),
			}
		)

	if not receive_items:
		frappe.throw(_("No Make Receive rows are available for SNC."))

	request_id = "SNC-{0}".format(stock_entry.name)
	return create_mr_wo_stock_entry(
		{
			"manufacturing_operation": stock_entry.manufacturing_operation,
			"receive_items": receive_items,
		},
		request_id=request_id,
	)


def get_customer_batch(customer, item_code, required_qty=None):
	for batch in _get_customer_batches(customer, item_code):
		available_qty = _get_available_batch_qty(batch.name, item_code)
		if required_qty is None or available_qty >= flt(required_qty):
			return {"batch_no": batch.name, "available_qty": available_qty}
	return None


def create_metal_conversion(
	source_batch, required_item_code, required_pure_qty, required_qty, source_entry, mwo
):
	if not source_batch:
		frappe.throw(_("No customer-owned source batch found for metal conversion."))

	required_purity = _get_item_purity(required_item_code)
	if not required_purity:
		frappe.throw(
			_("Could not determine purity for item {0}.").format(required_item_code)
		)

	target_qty = flt(flt(required_pure_qty) / (required_purity / 100), 3)
	if target_qty <= 0:
		target_qty = flt(required_qty, 3)

	se = frappe.new_doc("Stock Entry")
	se.update(
		{
			"stock_entry_type": "Repack-Metal Conversion",
			"purpose": "Repack",
			"company": source_entry.company,
			"branch": source_entry.branch,
			"manufacturing_order": source_entry.manufacturing_order,
			"manufacturing_work_order": source_entry.manufacturing_work_order,
			"manufacturing_operation": source_entry.manufacturing_operation,
			"from_warehouse": SOURCE_WAREHOUSE,
			"to_warehouse": SOURCE_WAREHOUSE,
			"inventory_type": "Customer Goods",
			"_customer": mwo.customer,
			"auto_created": 1,
		}
	)
	_append_item(
		se,
		{
			"item_code": source_batch["item_code"],
			"qty": flt(source_batch["qty"], 3),
			"batch_no": source_batch["batch_no"],
			"s_warehouse": SOURCE_WAREHOUSE,
			"inventory_type": "Customer Goods",
			"customer": mwo.customer,
		},
	)
	_append_item(
		se,
		{
			"item_code": required_item_code,
			"qty": target_qty,
			"t_warehouse": SOURCE_WAREHOUSE,
			"inventory_type": "Customer Goods",
			"customer": mwo.customer,
		},
	)
	se.insert(ignore_permissions=True)
	se.submit()

	target_batch = _get_target_batch_from_stock_entry(se.name, required_item_code)
	if not target_batch:
		frappe.throw(
			_("Repack-Metal Conversion {0} did not create a target batch.").format(
				se.name
			)
		)

	return {
		"stock_entry": se.name,
		"target_batch": {"batch_no": target_batch, "available_qty": target_qty},
	}


def create_work_order_transfer(source_entry, source_row, batch_no, qty, mwo):
	se = frappe.new_doc("Stock Entry")
	se.update(
		{
			"stock_entry_type": "Material Transfer (WORK ORDER)",
			"purpose": "Material Transfer",
			"company": source_entry.company,
			"branch": source_entry.branch,
			"manufacturing_order": source_entry.manufacturing_order,
			"manufacturing_work_order": source_entry.manufacturing_work_order,
			"manufacturing_operation": source_entry.manufacturing_operation,
			"from_warehouse": SOURCE_WAREHOUSE,
			"to_warehouse": TARGET_WAREHOUSE,
			"inventory_type": "Customer Goods",
			"_customer": mwo.customer,
		}
	)
	_append_item(
		se,
		{
			"item_code": source_row["item_code"],
			"qty": flt(qty, 3),
			"custom_pure_qty": source_row.get("custom_pure_qty"),
			"batch_no": batch_no,
			"s_warehouse": SOURCE_WAREHOUSE,
			"t_warehouse": TARGET_WAREHOUSE,
			"inventory_type": "Customer Goods",
			"customer": mwo.customer,
			"custom_manufacturing_work_order": source_entry.manufacturing_work_order,
			"custom_parent_manufacturing_order": source_entry.manufacturing_order,
			"manufacturing_operation": source_entry.manufacturing_operation,
		},
	)
	se.insert(ignore_permissions=True)
	se.submit()
	return se.name


def _get_customer_gold_rows(stock_entry, required_customer):
	rows = []
	for row in stock_entry.items:
		if row.inventory_type != "Customer Goods":
			continue
		if not (row.item_code or "").startswith("M-G-"):
			continue
		if not row.batch_no:
			continue
		batch_customer = frappe.db.get_value("Batch", row.batch_no, "custom_customer")
		if not batch_customer or batch_customer == required_customer:
			continue
		rows.append(
			{
				"name": row.name,
				"item_code": row.item_code,
				"batch_no": row.batch_no,
				"qty": flt(row.qty, 3),
				"custom_pure_qty": flt(row.custom_pure_qty, 3),
				"inventory_type": row.inventory_type,
				"used_customer": batch_customer,
				"required_customer": required_customer,
			}
		)
	return rows


def _get_customer_batches(customer, item_code):
	return frappe.get_all(
		"Batch",
		filters={"item": item_code, "custom_customer": customer, "disabled": 0},
		fields=["name", "item", "creation"],
		order_by="creation asc",
	)


def _get_conversion_source_batch(customer, required_item_code, required_pure_qty):
	required_purity_label = _get_purity_label(required_item_code)
	for purity in PURITY_PRIORITY:
		if purity == required_purity_label:
			continue
		for item_code in _get_gold_items_for_purity(purity, required_item_code):
			source_purity = _get_item_purity(item_code)
			if not source_purity:
				continue
			source_qty = flt(flt(required_pure_qty) / (source_purity / 100), 3)
			for batch in _get_customer_batches(customer, item_code):
				available_qty = _get_available_batch_qty(batch.name, item_code)
				if available_qty >= source_qty:
					return {
						"batch_no": batch.name,
						"item_code": item_code,
						"qty": source_qty,
					}
	return None


def _get_gold_items_for_purity(purity, required_item_code):
	colour = (required_item_code or "").split("-")[-1]
	filters = {"disabled": 0}
	if colour:
		filters["name"] = ["like", "%-{0}".format(colour)]

	items = frappe.get_all("Item", filters=filters, pluck="name")
	return [
		item
		for item in items
		if _get_purity_label(item) == purity and item.startswith("M-G-")
	]


def _get_available_batch_qty(batch_no, item_code):
	return flt(
		get_batch_qty(
			batch_no=batch_no,
			warehouse=SOURCE_WAREHOUSE,
			item_code=item_code,
			ignore_reserved_stock=True,
		),
		3,
	)


def _get_target_batch_from_stock_entry(stock_entry, item_code):
	return frappe.db.get_value(
		"Stock Entry Detail",
		{
			"parent": stock_entry,
			"item_code": item_code,
			"t_warehouse": SOURCE_WAREHOUSE,
		},
		"batch_no",
	)


def _get_item_purity(item_code):
	purity = frappe.db.get_value("Item", item_code, "metal_purity")
	if purity:
		attr_purity = frappe.db.get_value(
			"Attribute Value", purity, "custom_purity_percentage"
		)
		if attr_purity:
			return flt(attr_purity)
		try:
			return flt(float(purity))
		except (TypeError, ValueError):
			pass
	try:
		return flt(float((item_code or "").split("-")[-2]))
	except (TypeError, ValueError, IndexError):
		return 0


def _get_purity_label(item_code):
	parts = (item_code or "").split("-")
	return parts[2] if len(parts) > 2 else None


def _append_item(se, values):
	row = se.append("items", {})
	row.update(values)
	row.use_serial_batch_fields = 1
	row.allow_zero_valuation_rate = 1
	if not row.get("pcs"):
		row.pcs = 1
	return row
