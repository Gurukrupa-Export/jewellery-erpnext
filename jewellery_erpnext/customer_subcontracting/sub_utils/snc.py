import hashlib

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.utils import cint, flt, now_datetime

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	create_mr_wo_stock_entry,
	get_make_receive_entry_rows,
)

PURITY_PRIORITY = ("24KT", "22KT", "20KT", "18KT")


@frappe.whitelist()
def validate_button_visibility(mwo):
	mwo = _get_mwo(mwo)
	if mwo.docstatus != 1 or not mwo.manufacturing_order:
		return False
	if cint(getattr(mwo, "snc_done", 0)):
		return False

	transfer = _get_original_material_transfer(mwo.name)
	if not transfer:
		return False

	rows = _get_original_gold_rows(transfer)
	if not rows:
		return False

	pmo_is_customer_gold = _is_customer_gold(mwo)
	return any(_row_needs_settlement(mwo, row, pmo_is_customer_gold) for row in rows)


def _is_customer_gold(mwo):
	return cint(
		frappe.db.get_value(
			"Parent Manufacturing Order", mwo.manufacturing_order, "is_customer_gold"
		)
	)


def _row_needs_settlement(mwo, row, pmo_is_customer_gold):
	"""Decide whether an original transfer gold row must be settled by SNC.

	Subcontracting order: settle whenever the borrowed gold is not the order
	customer's own gold (covers other-customer gold and regular/company gold).
	Regular order: settle only when a customer's gold was borrowed.
	"""
	batch_customer = row.get("batch_customer")
	if pmo_is_customer_gold:
		return batch_customer != mwo.customer
	return bool(batch_customer)


def validate_snc_before_submit(doc, method=None):
	"""Block submit of the finished-goods (for_fg) MWO while any working MWO of the
	same PMO still needs its 'Create SNC' gold settlement done.

	The FG MWO carries no borrowed gold of its own -- the settlement (and the Create
	SNC button) live on the working sibling MWOs. So this fires once, at the FG MWO
	submit, and verifies the siblings, mirroring the per-PMO repack guard.
	"""
	if not cint(getattr(doc, "for_fg", 0)) or not doc.manufacturing_order:
		return

	siblings = frappe.get_all(
		"Manufacturing Work Order",
		filters={
			"manufacturing_order": doc.manufacturing_order,
			"docstatus": 1,
			"for_fg": 0,
			"has_split_mwo": 0,
			"snc_done": 0,
			"name": ["!=", doc.name],
		},
		fields=["name", "snc_requirement", "snc_done"],
	)

	pending = []
	visibility_cache = {}
	for mwo in siblings:
		if cint(mwo.snc_done):
			continue

		# Prefer the stamped field; fall back to live computation for MWOs created
		# before this field started being populated.
		if mwo.snc_requirement:
			needs = mwo.snc_requirement == "Need"
		else:
			needs = visibility_cache.get(mwo.name)
			if needs is None:
				needs = validate_button_visibility(mwo.name)
				visibility_cache[mwo.name] = needs

		if needs:
			pending.append(mwo.name)

	if pending:
		mwo_list = "<br>".join("- <b>{0}</b>".format(name) for name in pending)
		frappe.throw(
			_(
				"Gold Settlement Pending. Please click 'Create SNC' on the following "
				"Manufacturing Work Order(s) before submitting:<br>{0}"
			).format(mwo_list)
		)


def stamp_snc_requirement(doc, method=None):
	"""Stamp a working MWO's ``snc_requirement`` (Need / Not Need) when its original
	gold ``Material Transfer (WORK ORDER)`` is submitted, using the same 5-case logic
	that drives the Create SNC button visibility. Skips SNC's own settlement transfers.
	"""
	if doc.stock_entry_type != "Material Transfer (WORK ORDER)" or not doc.get(
		"manufacturing_work_order"
	):
		return
	if (doc.get("custom_request_id") or "").startswith("SNC-"):
		return

	needs = validate_button_visibility(doc.manufacturing_work_order)
	frappe.db.set_value(
		"Manufacturing Work Order",
		doc.manufacturing_work_order,
		"snc_requirement",
		"Need" if needs else "Not Need",
	)


@frappe.whitelist()
def create_snc(mwo):
	mwo = _get_mwo(mwo)
	if not validate_button_visibility(mwo.name):
		frappe.throw(
			_("Create SNC is not available for this Manufacturing Work Order.")
		)

	original_transfer = _get_original_material_transfer(mwo.name)
	pmo_is_customer_gold = _is_customer_gold(mwo)
	original_rows = [
		row
		for row in _get_original_gold_rows(original_transfer)
		if _row_needs_settlement(mwo, row, pmo_is_customer_gold)
	]
	if not original_rows:
		frappe.throw(_("No original Material Transfer gold rows to settle found."))

	# The order owner whose gold is brought in to replace the borrowed gold.
	# Subcontracting order -> the order customer; regular order -> Regular Stock.
	owner_customer = mwo.customer if pmo_is_customer_gold else None

	created = {"make_receive": None, "conversions": [], "transfers": []}
	for row in original_rows:
		required_batch = find_owner_batch(
			owner_customer, row["item_code"], row["qty"], company=mwo.company
		)
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
				owner_customer=owner_customer,
			)
			created["transfers"].append(transfer)
			continue

		source_batch = find_owner_rm_warehouse(
			owner_customer,
			row["item_code"],
			row["custom_pure_qty"],
			search_different_purity=True,
			company=mwo.company,
		)
		if not source_batch:
			frappe.throw(
				_("No available stock for {0}.").format(_owner_label(owner_customer))
			)

		make_receive = trigger_make_receive(mwo, source_batch["warehouse"])
		created["make_receive"] = created["make_receive"] or make_receive
		conversion = create_repack_metal_conversion(
			mwo=mwo,
			original_transfer=original_transfer,
			source_batch=source_batch,
			required_item_code=row["item_code"],
			required_pure_qty=row["custom_pure_qty"],
			required_qty=row["qty"],
			owner_customer=owner_customer,
		)
		created["conversions"].append(conversion["stock_entry"])
		# The owner conversion above is systematic only (no physical movement),
		# so mirror it on the borrowed usage batch in the SAME warehouse: convert
		# it back from the expected purity to the available/source purity, keeping
		# each purity physically balanced.
		usage_conversion = create_repack_metal_conversion(
			mwo=mwo,
			original_transfer=original_transfer,
			source_batch={
				"item_code": row["item_code"],
				"batch_no": row["batch_no"],
				"qty": row["qty"],
				"warehouse": source_batch["warehouse"],
			},
			required_item_code=source_batch["item_code"],
			required_pure_qty=row["custom_pure_qty"],
			required_qty=source_batch["qty"],
			owner_customer=row.get("batch_customer"),
		)
		created["conversions"].append(usage_conversion["stock_entry"])
		transfer = create_material_transfer_work_order(
			mwo=mwo,
			original_transfer=original_transfer,
			original_row=row,
			batch_no=conversion["target_batch"],
			source_warehouse=conversion["warehouse"],
			owner_customer=owner_customer,
		)
		created["transfers"].append(transfer)

	frappe.db.set_value("Manufacturing Work Order", mwo.name, "snc_done", 1)
	return created


def _owner_label(owner_customer):
	return "Customer {0}".format(owner_customer) if owner_customer else "Regular Stock"


def find_owner_batch(owner_customer, item_code, required_qty, company=None):
	return find_owner_rm_warehouse(
		owner_customer, item_code, required_qty, company=company
	)


def find_owner_rm_warehouse(
	owner_customer,
	item_code,
	required_qty_or_pure_qty,
	search_different_purity=False,
	company=None,
):
	if not search_different_purity:
		return _find_available_owner_batch(
			owner_customer, item_code, required_qty_or_pure_qty, company
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
			batch = _find_available_owner_batch(
				owner_customer, candidate_item, source_qty, company
			)
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
	owner_customer=None,
):
	required_purity = _get_item_purity(required_item_code)
	if not required_purity:
		frappe.throw(
			_("Could not determine purity for item {0}.").format(required_item_code)
		)

	target_qty = flt(flt(required_pure_qty) / (required_purity / 100), 3)
	if target_qty <= 0:
		target_qty = flt(required_qty, 3)

	inventory_type = _owner_inventory_type(owner_customer)
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
			"inventory_type": inventory_type,
			"_customer": owner_customer,
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
			"inventory_type": inventory_type,
			"customer": owner_customer,
		},
	)
	_append_item(
		se,
		{
			"item_code": required_item_code,
			"qty": target_qty,
			"t_warehouse": source_batch["warehouse"],
			"inventory_type": inventory_type,
			"customer": owner_customer,
		},
	)
	se.insert(ignore_permissions=True)
	_submit_consuming_stock_entry(se)

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
	mwo,
	original_transfer,
	original_row,
	batch_no,
	source_warehouse,
	owner_customer=None,
):
	target_warehouse = original_row.get("t_warehouse") or original_transfer.to_warehouse
	inventory_type = _owner_inventory_type(owner_customer)
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
			"inventory_type": inventory_type,
			"_customer": owner_customer,
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
			"inventory_type": inventory_type,
			"customer": owner_customer,
			"custom_manufacturing_work_order": mwo.name,
			"custom_parent_manufacturing_order": mwo.manufacturing_order,
			"manufacturing_operation": mwo.manufacturing_operation,
		},
	)
	se.insert(ignore_permissions=True)
	_submit_consuming_stock_entry(se)
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
		# SNC only settles gold; receive gold rows only, never diamond/findings.
		if not (row.get("item_code") or "").startswith("M-G-"):
			continue
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


def _owner_inventory_type(owner_customer):
	return "Customer Goods" if owner_customer else "Regular Stock"


def _get_original_gold_rows(transfer):
	"""Return all gold (M-G-) rows of the original Material Transfer, tagging each
	with the borrowed batch's customer (``None`` for regular/company stock)."""
	rows = []
	for row in transfer.items:
		if not row.batch_no or not (row.item_code or "").startswith("M-G-"):
			continue
		batch_customer = frappe.db.get_value("Batch", row.batch_no, "custom_customer")
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


def _find_available_owner_batch(owner_customer, item_code, required_qty, company=None):
	if owner_customer:
		batch_filters = {
			"item": item_code,
			"custom_customer": owner_customer,
			"disabled": 0,
		}
	else:
		batch_filters = {
			"item": item_code,
			"custom_inventory_type": "Regular Stock",
			"disabled": 0,
		}
	batches = frappe.get_all(
		"Batch", filters=batch_filters, pluck="name", order_by="creation asc"
	)
	warehouses = _get_raw_material_warehouses(company)
	if not batches or not warehouses:
		return None

	required = flt(required_qty, 3)
	# One grouped query for the batch-wise consumable balance the submit-time
	# negative-stock check (erpnext BatchNoValuation) enforces. get_batch_qty also
	# counts the legacy SLE.batch_no ledger and future-dated rows the validator
	# ignores, so a warehouse can look available here yet go negative on transfer.
	consumable = _consumable_batch_qty_map(batches, item_code, warehouses)

	for batch_no in batches:
		for warehouse in warehouses:
			if consumable.get((batch_no, warehouse), 0) < required:
				continue
			# Consumable stock exists here; confirm it is not fully reserved
			# (get_batch_qty nets Stock Reservation Entries) before committing.
			available_qty = flt(
				get_batch_qty(
					batch_no=batch_no, warehouse=warehouse, item_code=item_code
				),
				3,
			)
			if available_qty < required:
				continue
			return {
				"batch_no": batch_no,
				"item_code": item_code,
				"warehouse": warehouse,
				"available_qty": consumable[(batch_no, warehouse)],
				"qty": required,
			}
	return None


def _consumable_batch_qty(batch_no, item_code, warehouse):
	"""Consumable balance for one (batch, warehouse); see _consumable_batch_qty_map."""
	return _consumable_batch_qty_map([batch_no], item_code, [warehouse]).get(
		(batch_no, warehouse), 0.0
	)


def _consumable_batch_qty_map(batch_nos, item_code, warehouses):
	"""Map {(batch_no, warehouse): qty} of the balance the submit-time negative-stock
	check enforces: submitted Serial and Batch Entry rows up to now only -- no legacy
	SLE.batch_no ledger, no future-dated rows. Mirrors get_batch_stock_before_date."""
	result = {}
	if not batch_nos or not warehouses:
		return result
	now_dt = now_datetime()
	wh_ph = ", ".join(["%s"] * len(warehouses))
	for start in range(0, len(batch_nos), 500):
		chunk = batch_nos[start : start + 500]
		b_ph = ", ".join(["%s"] * len(chunk))
		rows = frappe.db.sql(
			f"""
			select batch_no, warehouse, coalesce(sum(qty), 0) as qty
			from `tabSerial and Batch Entry`
			where batch_no in ({b_ph}) and item_code = %s
				and warehouse in ({wh_ph}) and docstatus = 1
				and type_of_transaction in ('Inward', 'Outward')
				and posting_datetime <= %s
			group by batch_no, warehouse
			""",
			(*chunk, item_code, *warehouses, now_dt),
			as_dict=True,
		)
		for row in rows:
			result[(row.batch_no, row.warehouse)] = flt(row.qty, 3)
	return result


def _submit_consuming_stock_entry(se):
	"""Submit an SNC Stock Entry after asserting every outward line has the consumable
	batch balance the submit-time check enforces, so residual edge cases fail with a
	clear message and full rollback instead of a deep BatchNegativeStockError."""
	needed = {}
	for item in se.items:
		if not item.get("s_warehouse") or not item.get("batch_no"):
			continue
		key = (item.item_code, item.batch_no, item.s_warehouse)
		needed[key] = flt(needed.get(key, 0)) + flt(item.qty, 3)
	for (item_code, batch_no, warehouse), qty in needed.items():
		available = _consumable_batch_qty(batch_no, item_code, warehouse)
		if available < qty:
			frappe.throw(
				_(
					"Not enough stock of batch {0} ({1}) in {2}: need {3}, have {4}."
				).format(batch_no, item_code, warehouse, qty, available)
			)
	se.submit()


def _get_raw_material_warehouses(company=None):
	filters = {"warehouse_type": "Raw Material", "disabled": 0}
	if company:
		# Scope to the SNC's company; otherwise a batch found in another company's
		# Raw Material warehouse would build a Stock Entry stamped with this company
		# and fail validate_warehouse_company at submit.
		filters["company"] = company
	return frappe.get_all(
		"Warehouse",
		filters=filters,
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
