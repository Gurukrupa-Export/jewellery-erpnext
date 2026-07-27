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
	# Already settled: nothing to do. This also short-circuits the heavier live
	# Make Receive computation below for the common already-done case.
	if cint(getattr(mwo, "snc_done", 0)):
		return False
	return _mwo_needs_settlement(mwo)


def _mwo_needs_settlement(mwo):
	"""Live check: does this MWO's operation currently hold borrowed gold that must
	be settled?

	Reads the same live receivable rows ``create_snc`` actually settles
	(``_get_receivable_gold_rows``), so it is correct no matter how many
	``Material Transfer (WORK ORDER)`` documents have fed the operation. The old
	implementation inspected only the *earliest* transfer, so a later transfer that
	borrowed another customer's gold was never detected and the MWO stayed
	"Not Need".

	Does NOT consult ``snc_done`` -- that gate lives in ``validate_button_visibility``.
	``stamp_snc_requirement`` needs the raw live position so a fresh borrow can
	re-open a previously completed settlement.
	"""
	mwo = _get_mwo(mwo)
	if (
		mwo.docstatus != 1
		or not mwo.manufacturing_order
		or not mwo.manufacturing_operation
	):
		return False
	pmo_is_customer_gold = _is_customer_gold(mwo)
	return any(
		_row_needs_settlement(mwo, row, pmo_is_customer_gold)
		for row in _get_receivable_gold_rows(mwo)
	)


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
	"""Stamp a working MWO's ``snc_requirement`` (Need / Not Need) whenever a
	``Material Transfer (WORK ORDER)`` feeding it is submitted, using the live
	held-gold position (the same source ``create_snc`` settles). Skips SNC's own
	settlement transfers.

	A single MWO can be fed by several transfers from different customers, so the
	requirement is recomputed from what the operation holds *now*, not from the
	earliest transfer. When fresh borrowed gold arrives after a prior settlement,
	this also resets ``snc_done`` so the MWO re-enters the Create SNC flow.
	"""
	if doc.stock_entry_type != "Material Transfer (WORK ORDER)" or not doc.get(
		"manufacturing_work_order"
	):
		return
	if (doc.get("custom_request_id") or "").startswith("SNC-"):
		return

	needs = _mwo_needs_settlement(doc.manufacturing_work_order)
	values = {"snc_requirement": "Need" if needs else "Not Need"}
	if needs:
		# A previously completed settlement no longer covers this new borrow.
		values["snc_done"] = 0
	frappe.db.set_value(
		"Manufacturing Work Order", doc.manufacturing_work_order, values
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
	# The order owner whose gold is brought in to replace the borrowed gold.
	# Subcontracting order -> the order customer; regular order -> Regular Stock.
	owner_customer = mwo.customer if pmo_is_customer_gold else None

	# Settle the gold the operation ACTUALLY holds now (the same live source the
	# Make Receive uses -- loss-adjusted, per current batch), so the replacement
	# transfer mirrors the received rows row-for-row instead of copying the stale
	# earliest Material Transfer.
	settle_rows = [
		row
		for row in _get_receivable_gold_rows(mwo)
		if _row_needs_settlement(mwo, row, pmo_is_customer_gold)
	]
	if not settle_rows:
		frappe.throw(_("No borrowed gold rows to settle found."))

	created = {"make_receive": None, "conversions": [], "transfers": []}
	transfer_rows = []
	# One allocation map for the whole settlement: the owner-batch finders take no DB
	# hold, so without it two settle rows can be handed the same batch and over-draw it.
	allocated = {}
	for row in settle_rows:
		# Reserves: consumed by the ONE Material Transfer built at the very end, so
		# nothing reduces the ledger before the next row looks.
		required_batch = find_owner_batch(
			owner_customer,
			row["item_code"],
			row["qty"],
			company=mwo.company,
			allocated=allocated,
		)
		if required_batch:
			# Receive the borrowed gold once, then bring the owner's own gold back
			# to the warehouse this row was received from.
			if created["make_receive"] is None:
				created["make_receive"] = trigger_make_receive(
					mwo, required_batch["warehouse"], receive_items=settle_rows
				)
			transfer_rows.append(
				{
					"item_code": row["item_code"],
					"qty": row["qty"],
					"custom_pure_qty": row["custom_pure_qty"],
					"batch_no": required_batch["batch_no"],
					"s_warehouse": required_batch["warehouse"],
					"t_warehouse": row["s_warehouse"],
				}
			)
			continue

		# Reads the map but does NOT reserve: the conversion below submits immediately,
		# so the ledger is already reduced before the next row looks. Reserving as well
		# would double-count the same source quantity.
		source_batch = find_owner_rm_warehouse(
			owner_customer,
			row["item_code"],
			row["custom_pure_qty"],
			search_different_purity=True,
			company=mwo.company,
			allocated=allocated,
			reserve=False,
		)
		if not source_batch:
			frappe.throw(
				_("No available stock for {0}.").format(_owner_label(owner_customer))
			)

		if created["make_receive"] is None:
			created["make_receive"] = trigger_make_receive(
				mwo, source_batch["warehouse"], receive_items=settle_rows
			)
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
		# Claim the conversion OUTPUT. This is the claim that actually fixes the
		# original crash: the output is fresh owner stock that no finder returned, so
		# without recording it here a later row's find_owner_batch re-discovers the
		# batch this row just minted and both rows draw on the same 1.0 g.
		output_key = (conversion["target_batch"], conversion["warehouse"])
		allocated[output_key] = flt(allocated.get(output_key, 0), 3) + flt(
			row["qty"], 3
		)
		transfer_rows.append(
			{
				"item_code": row["item_code"],
				"qty": row["qty"],
				"custom_pure_qty": row["custom_pure_qty"],
				"batch_no": conversion["target_batch"],
				"s_warehouse": conversion["warehouse"],
				"t_warehouse": row["s_warehouse"],
			}
		)

	created["transfers"].append(
		create_material_transfer_work_order(
			mwo, original_transfer, transfer_rows, owner_customer
		)
	)

	frappe.db.set_value("Manufacturing Work Order", mwo.name, "snc_done", 1)
	return created


def _owner_label(owner_customer):
	return "Customer {0}".format(owner_customer) if owner_customer else "Regular Stock"


def find_owner_batch(
	owner_customer,
	item_code,
	required_qty,
	company=None,
	warehouses=None,
	allocated=None,
	reserve=True,
):
	return find_owner_rm_warehouse(
		owner_customer,
		item_code,
		required_qty,
		company=company,
		warehouses=warehouses,
		allocated=allocated,
		reserve=reserve,
	)


def find_owner_rm_warehouse(
	owner_customer,
	item_code,
	required_qty_or_pure_qty,
	search_different_purity=False,
	company=None,
	warehouses=None,
	allocated=None,
	reserve=True,
):
	"""Locate a batch of ``owner_customer``'s metal big enough for the requirement.

	``warehouses`` narrows the search (defaults to every Raw Material warehouse in the
	company); ``allocated`` is a caller-owned ``{(batch_no, warehouse): qty}`` map that
	makes a multi-row run self-consistent -- see :func:`_find_available_owner_batch`.
	``reserve`` decides whether a hit is *recorded* in that map or merely read against.
	"""
	if not search_different_purity:
		return _find_available_owner_batch(
			owner_customer,
			item_code,
			required_qty_or_pure_qty,
			company,
			warehouses=warehouses,
			allocated=allocated,
			reserve=reserve,
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
				owner_customer,
				candidate_item,
				source_qty,
				company,
				warehouses=warehouses,
				allocated=allocated,
				reserve=reserve,
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
	mwo, original_transfer, rows, owner_customer=None
):
	"""Build ONE 'Material Transfer (WORK ORDER)' that mirrors the Make Receive:
	one row per received borrowed-gold row, each bringing the owner's replacement
	gold back into the warehouse that borrowed gold was received from."""
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
			"from_warehouse": rows[0]["s_warehouse"],
			"to_warehouse": rows[0]["t_warehouse"],
			"inventory_type": inventory_type,
			"_customer": owner_customer,
		}
	)
	for row in rows:
		_append_item(
			se,
			{
				"item_code": row["item_code"],
				"qty": row["qty"],
				"custom_pure_qty": row["custom_pure_qty"],
				"batch_no": row["batch_no"],
				"s_warehouse": row["s_warehouse"],
				"t_warehouse": row["t_warehouse"],
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


def _get_receivable_gold_rows(mwo, target_warehouse=None):
	"""Gold (M-G-) rows the operation ACTUALLY holds now, from the same live Make
	Receive source (loss-adjusted SRE remaining), shaped like ``create_mr_wo_stock_entry``
	receive_items plus the ``batch_customer`` / ``custom_pure_qty`` / ``s_warehouse``
	the settlement transfer needs.

	The Make Receive rows carry neither ``inventory_type``/``customer`` nor
	``custom_pure_qty``: ownership is read off the Batch master and pure qty is
	recomputed from item purity. The ``target_warehouse`` only labels the receive
	destination -- it does not affect which rows or quantities are returned.
	"""
	if not mwo.manufacturing_operation:
		frappe.throw(_("Manufacturing Operation is required to trigger Make Receive."))

	if not target_warehouse:
		rm_warehouses = _get_raw_material_warehouses(mwo.company)
		target_warehouse = rm_warehouses[0] if rm_warehouses else None

	rows = (
		get_make_receive_entry_rows(
			mwo.manufacturing_operation, target_warehouse=target_warehouse
		).get("rows")
		or []
	)
	receive_items = []
	for row in rows:
		item_code = row.get("item_code") or ""
		# SNC only settles gold; receive gold rows only, never diamond/findings.
		if not item_code.startswith("M-G-"):
			continue
		qty = flt(row.get("available_to_receive_qty"), 3)
		if qty <= 0:
			continue
		batch_no = row.get("batch_no")
		inventory_type = batch_customer = None
		if batch_no:
			inventory_type, batch_customer = frappe.db.get_value(
				"Batch", batch_no, ["custom_inventory_type", "custom_customer"]
			) or (None, None)
		receive_items.append(
			{
				"stock_reservation_entry": row.get("stock_reservation_entry"),
				"stock_reservation_entry_detail": row.get(
					"stock_reservation_entry_detail"
				),
				"item_code": item_code,
				"batch_no": batch_no,
				"qty": qty,
				"pcs": cint(row.get("available_to_receive_pcs") or 0),
				"inventory_type": inventory_type,
				"customer": batch_customer,
				"batch_customer": batch_customer,
				"custom_pure_qty": flt(qty * _get_item_purity(item_code) / 100, 3),
				"s_warehouse": row.get("s_warehouse"),
			}
		)
	return receive_items


def trigger_make_receive(mwo, target_warehouse, receive_items=None):
	if not mwo.manufacturing_operation:
		frappe.throw(_("Manufacturing Operation is required to trigger Make Receive."))

	if receive_items is None:
		receive_items = _get_receivable_gold_rows(mwo, target_warehouse)
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


def _find_available_owner_batch(
	owner_customer,
	item_code,
	required_qty,
	company=None,
	warehouses=None,
	allocated=None,
	reserve=True,
):
	"""Find one (batch, warehouse) holding at least ``required_qty`` of the owner's metal.

	``allocated`` -- when the caller passes a ``{(batch_no, warehouse): qty}`` dict, the
	running claim is subtracted from BOTH availability checks. The finder takes no
	database hold, so without that map a loop over several rows can be handed the SAME
	batch twice and over-draw it at submit ("need X have Y").

	``reserve`` -- whether a hit is recorded in the map, and it depends on WHEN the
	caller's consumer submits:

	* ``True`` (default) for stock consumed by a Stock Entry built later in the run --
	  nothing has reduced the ledger yet, so the claim must be held in the map.
	* ``False`` when the caller submits its consuming entry IMMEDIATELY after the find.
	  The ledger is already reduced before the next lookup, so also reserving would
	  double-count the same quantity.

	Stock that a run *creates* (e.g. a conversion's output batch) is never returned by a
	finder, so it can only be claimed by the caller writing to the map directly.
	"""
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
	if warehouses is None:
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
			# Net out whatever earlier rows of this same run already claimed here.
			taken = (
				flt(allocated.get((batch_no, warehouse), 0), 3) if allocated else 0.0
			)
			if flt(consumable.get((batch_no, warehouse), 0), 3) - taken < required:
				continue
			# Consumable stock exists here; confirm it is not fully reserved
			# (get_batch_qty nets Stock Reservation Entries) before committing.
			available_qty = flt(
				get_batch_qty(
					batch_no=batch_no, warehouse=warehouse, item_code=item_code
				),
				3,
			)
			if available_qty - taken < required:
				continue
			if allocated is not None and reserve:
				allocated[(batch_no, warehouse)] = taken + required
			return {
				"batch_no": batch_no,
				"item_code": item_code,
				"warehouse": warehouse,
				"available_qty": flt(consumable[(batch_no, warehouse)] - taken, 3),
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
