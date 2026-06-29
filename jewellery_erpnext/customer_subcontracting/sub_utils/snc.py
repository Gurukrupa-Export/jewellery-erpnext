import hashlib

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	create_mr_wo_stock_entry,
	get_make_receive_entry_rows,
)

PURITY_PRIORITY = ("24KT", "22KT", "20KT", "18KT")


@frappe.whitelist()
def validate_button_visibility(mwo):
	mwo = _get_mwo(mwo)
	if mwo.docstatus != 1 or not mwo.manufacturing_order or not mwo.customer:
		return False

	transfer = _get_original_material_transfer(mwo.name)
	if not transfer:
		return False

	batch_customer = _get_original_material_transfer_batch_customer(transfer)
	if not batch_customer:
		return False

	pmo_is_customer_gold = cint(
		frappe.db.get_value(
			"Parent Manufacturing Order", mwo.manufacturing_order, "is_customer_gold"
		)
	)
	if pmo_is_customer_gold:
		return mwo.customer != batch_customer
	return mwo.customer == batch_customer


@frappe.whitelist()
def create_snc(mwo):
	mwo = _get_mwo(mwo)
	if not validate_button_visibility(mwo.name):
		frappe.throw(
			_("Create SNC is not available for this Manufacturing Work Order.")
		)

	original_transfer = _get_original_material_transfer(mwo.name)
	original_rows = _get_original_customer_gold_rows(original_transfer)
	if not original_rows:
		frappe.throw(_("No original Material Transfer customer gold rows found."))

	created = {"make_receive": None, "conversions": [], "transfers": []}
	for row in original_rows:
		required_batch = find_customer_batch(mwo.customer, row["item_code"], row["qty"])
		if required_batch:
			target_warehouse = required_batch["warehouse"]
			make_receive = trigger_make_receive(mwo, target_warehouse)
			created["make_receive"] = created["make_receive"] or make_receive
			transfer = create_material_transfer_work_order(
				mwo=mwo,
				original_transfer=original_transfer,
				original_row=row,
				batch_no=required_batch["batch_no"],
				source_warehouse=target_warehouse,
			)
			created["transfers"].append(transfer)
			continue

		source_batch = find_customer_rm_warehouse(
			mwo.customer,
			row["item_code"],
			row["custom_pure_qty"],
			search_different_purity=True,
		)
		if not source_batch:
			frappe.throw(_("No available stock for Customer {0}.").format(mwo.customer))

		make_receive = trigger_make_receive(mwo, source_batch["warehouse"])
		created["make_receive"] = created["make_receive"] or make_receive
		conversion = create_repack_metal_conversion(
			mwo=mwo,
			original_transfer=original_transfer,
			source_batch=source_batch,
			required_item_code=row["item_code"],
			required_pure_qty=row["custom_pure_qty"],
			required_qty=row["qty"],
		)
		created["conversions"].append(conversion["stock_entry"])
		transfer = create_material_transfer_work_order(
			mwo=mwo,
			original_transfer=original_transfer,
			original_row=row,
			batch_no=conversion["target_batch"],
			source_warehouse=conversion["warehouse"],
		)
		created["transfers"].append(transfer)

	return created


def find_customer_batch(customer, item_code, required_qty):
	return find_customer_rm_warehouse(customer, item_code, required_qty)


def find_customer_rm_warehouse(
	customer, item_code, required_qty_or_pure_qty, search_different_purity=False
):
	if not search_different_purity:
		return _find_available_customer_batch(
			customer, item_code, required_qty_or_pure_qty
		)

	required_purity = _get_purity_label(item_code)
	for purity in PURITY_PRIORITY:
		if purity == required_purity:
			continue
		for candidate_item in _get_gold_items_for_purity(purity, item_code):
			source_purity = _get_item_purity(candidate_item)
			if not source_purity:
				continue
			source_qty = flt(flt(required_qty_or_pure_qty) / (source_purity / 100), 3)
			batch = _find_available_customer_batch(customer, candidate_item, source_qty)
			if batch:
				batch["qty"] = source_qty
				return batch
	return None


def create_repack_metal_conversion(
	mwo,
	original_transfer,
	source_batch,
	required_item_code,
	required_pure_qty,
	required_qty,
):
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
			"company": original_transfer.company,
			"branch": original_transfer.branch,
			"manufacturing_order": mwo.manufacturing_order,
			"manufacturing_work_order": mwo.name,
			"manufacturing_operation": mwo.manufacturing_operation,
			"from_warehouse": source_batch["warehouse"],
			"to_warehouse": source_batch["warehouse"],
			"inventory_type": "Customer Goods",
			"_customer": mwo.customer,
			"auto_created": 1,
		}
	)
	_append_item(
		se,
		{
			"item_code": source_batch["item_code"],
			"qty": source_batch["qty"],
			"batch_no": source_batch["batch_no"],
			"s_warehouse": source_batch["warehouse"],
			"inventory_type": "Customer Goods",
			"customer": mwo.customer,
		},
	)
	_append_item(
		se,
		{
			"item_code": required_item_code,
			"qty": target_qty,
			"t_warehouse": source_batch["warehouse"],
			"inventory_type": "Customer Goods",
			"customer": mwo.customer,
		},
	)
	se.insert(ignore_permissions=True)
	se.submit()

	target_batch = frappe.db.get_value(
		"Stock Entry Detail",
		{
			"parent": se.name,
			"item_code": required_item_code,
			"t_warehouse": source_batch["warehouse"],
		},
		"batch_no",
	)
	if not target_batch:
		frappe.throw(
			_("Repack-Metal Conversion {0} did not create a target batch.").format(
				se.name
			)
		)

	return {
		"stock_entry": se.name,
		"target_batch": target_batch,
		"warehouse": source_batch["warehouse"],
	}


def create_material_transfer_work_order(
	mwo, original_transfer, original_row, batch_no, source_warehouse
):
	target_warehouse = original_row.get("t_warehouse") or original_transfer.to_warehouse
	se = frappe.new_doc("Stock Entry")
	se.update(
		{
			"stock_entry_type": "Material Transfer (WORK ORDER)",
			"purpose": "Material Transfer",
			"company": original_transfer.company,
			"branch": original_transfer.branch,
			"manufacturing_order": mwo.manufacturing_order,
			"manufacturing_work_order": mwo.name,
			"manufacturing_operation": mwo.manufacturing_operation,
			"from_warehouse": source_warehouse,
			"to_warehouse": target_warehouse,
			"inventory_type": "Customer Goods",
			"_customer": mwo.customer,
		}
	)
	_append_item(
		se,
		{
			"item_code": original_row["item_code"],
			"qty": original_row["qty"],
			"custom_pure_qty": original_row["custom_pure_qty"],
			"batch_no": batch_no,
			"s_warehouse": source_warehouse,
			"t_warehouse": target_warehouse,
			"inventory_type": "Customer Goods",
			"customer": mwo.customer,
			"custom_manufacturing_work_order": mwo.name,
			"custom_parent_manufacturing_order": mwo.manufacturing_order,
			"manufacturing_operation": mwo.manufacturing_operation,
		},
	)
	se.insert(ignore_permissions=True)
	se.submit()
	return se.name


def trigger_make_receive(mwo, target_warehouse):
	if not mwo.manufacturing_operation:
		frappe.throw(_("Manufacturing Operation is required to trigger Make Receive."))

	rows = (
		get_make_receive_entry_rows(
			mwo.manufacturing_operation, target_warehouse=target_warehouse
		).get("rows")
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

	return create_mr_wo_stock_entry(
		{
			"manufacturing_operation": mwo.manufacturing_operation,
			"receive_items": receive_items,
		},
		request_id="SNC-"
		+ hashlib.md5(f"{mwo.name}-{target_warehouse}".encode()).hexdigest()[:10],
		target_warehouse=target_warehouse,
	)


def _get_mwo(mwo):
	if isinstance(mwo, str):
		return frappe.get_doc("Manufacturing Work Order", mwo)
	return mwo


def _get_original_material_transfer(mwo_name):
	name = frappe.db.get_value(
		"Stock Entry",
		{
			"stock_entry_type": "Material Transfer (WORK ORDER)",
			"manufacturing_work_order": mwo_name,
			"docstatus": 1,
		},
		"name",
		order_by="creation asc",
	)
	return frappe.get_doc("Stock Entry", name) if name else None


def _get_original_material_transfer_batch_customer(transfer):
	for row in _get_original_customer_gold_rows(transfer):
		if row.get("batch_customer"):
			return row["batch_customer"]
	return None


def _get_original_customer_gold_rows(transfer):
	rows = []
	for row in transfer.items:
		if row.inventory_type != "Customer Goods":
			continue
		if not row.batch_no or not (row.item_code or "").startswith("M-G-"):
			continue
		batch_customer = frappe.db.get_value("Batch", row.batch_no, "custom_customer")
		if not batch_customer:
			continue
		rows.append(
			{
				"item_code": row.item_code,
				"batch_no": row.batch_no,
				"batch_customer": batch_customer,
				"qty": flt(row.qty, 3),
				"custom_pure_qty": flt(row.custom_pure_qty, 3),
				"t_warehouse": row.t_warehouse,
			}
		)
	return rows


def _find_available_customer_batch(customer, item_code, required_qty):
	for batch in frappe.get_all(
		"Batch",
		filters={"item": item_code, "custom_customer": customer, "disabled": 0},
		fields=["name", "creation"],
		order_by="creation asc",
	):
		for warehouse in _get_raw_material_warehouses():
			available_qty = flt(
				get_batch_qty(
					batch_no=batch.name,
					warehouse=warehouse,
					item_code=item_code,
				),
				3,
			)
			if available_qty >= flt(required_qty, 3):
				return {
					"batch_no": batch.name,
					"item_code": item_code,
					"warehouse": warehouse,
					"available_qty": available_qty,
					"qty": flt(required_qty, 3),
				}
	return None


def _get_raw_material_warehouses():
	return frappe.get_all(
		"Warehouse",
		filters={"warehouse_type": "Raw Material", "disabled": 0},
		pluck="name",
	)


def _get_gold_items_for_purity(purity, required_item_code):
	colour = (required_item_code or "").split("-")[-1]
	filters = {"disabled": 0}
	if colour:
		filters["name"] = ["like", "%-{0}".format(colour)]

	items = frappe.get_all("Item", filters=filters, pluck="name")
	return [
		item
		for item in items
		if item.startswith("M-G-") and _get_purity_label(item) == purity
	]


def _get_item_purity(item_code):
	item = frappe.get_doc("Item", item_code)

	for row in item.attributes:
		if row.attribute == "Metal Purity":
			try:
				return flt(row.attribute_value)
			except (TypeError, ValueError):
				pass

			purity = frappe.db.get_value(
				"Attribute Value",
				row.attribute_value,
				"custom_purity_percentage",
			)
			if purity:
				return flt(purity)

	try:
		return flt((item_code or "").split("-")[-2])
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
