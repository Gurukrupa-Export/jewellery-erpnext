import json

import frappe
from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import (
	StockReconciliation,
)
from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import (
	get_items as erpnext_get_items,
)
from frappe import _
from frappe.utils import cint, cstr, flt

from jewellery_erpnext.jewellery_erpnext.doc_events.supplier_metal_limit import (
	_metal_items,
	pure_weight,
)
from jewellery_erpnext.utils import bulk_map

PRECISION = 3

# Stock UOM -> grams. carat / 5 = gram (carat x 0.2). Other UOMs (Nos, ...) have no gram value.
GRAM_FACTORS = {"Gram": 1.0, "Carat": 0.2}

# Pending transit legs listed in the error, at most.
MAX_LISTED_LEGS = 10


def validate_department(self, method=None):
	if not self.set_warehouse:
		return

	if self.workflow_state not in ["In Progress", "Send for Approval"]:
		return

	department = frappe.db.get_value("Warehoouse", self.set_warehouse, "department")

	if not department:
		frappe.msgprint(_("Department not mentioned in warehouse"))
		return

	if frappe.db.get_all(
		"Manufacturig Operation",
		{"department": department, "department_ir_status": "In-Transit"},
	):
		frappe.throw(
			_(
				"Some Manufacturing Operations are in Transit mode, Complete Transit First then perform the action"
			)
		)


def validate_transit_warehouse_empty(self, method=None):
	"""Block save/submit unless every reconciled item has zero stock in the department's
	Transit warehouse.

	Reconciling a department while items still sit in its transit warehouse (mid-transfer)
	yields wrong balances. So: resolve the department from ``custom_department``, find its
	Transit warehouse (``Warehouse{department, warehouse_type="Transit", disabled=0}``), and
	require ``Bin.actual_qty`` there to be 0 for every distinct item on the reconciliation.

	``custom_department`` is read via ``.get()`` (it is a gke_customization fixture field and
	may be absent on un-synced sites) so a missing field degrades to "skip" rather than raising.
	A missing Bin row means no stock for that (item, warehouse) pair, i.e. 0 -- handled by
	``flt(None) == 0.0`` and ``on_hand.get(ic, 0.0)``.
	"""
	department = self.get("custom_department")
	if not department:
		return

	transit_warehouse = frappe.db.get_value(
		"Warehouse",
		{"department": department, "warehouse_type": "Transit", "disabled": 0},
		"name",
	)
	if not transit_warehouse:
		return  # department has no transit warehouse -> nothing can be in transit

	item_codes = {row.item_code for row in self.items if row.item_code}
	if not item_codes:
		return

	# One bulk read (matches the app's Bin bulk-prefetch idiom, main_slip_inject.py:858).
	on_hand = {
		b["item_code"]: flt(b["actual_qty"])
		for b in frappe.db.get_all(
			"Bin",
			filters={
				"warehouse": transit_warehouse,
				"item_code": ["in", list(item_codes)],
			},
			fields=["item_code", "actual_qty"],
		)
	}

	blocked = sorted(ic for ic in item_codes if on_hand.get(ic, 0.0) != 0)
	if blocked:
		rows = "".join(f"<li>{ic}: {on_hand[ic]}</li>" for ic in blocked)
		frappe.throw(
			_(
				"Stock Reconciliation cannot be saved: the following item(s) still have stock "
				"in the transit warehouse <b>{0}</b>. Clear the transit warehouse first."
			).format(transit_warehouse)
			+ f"<ul>{rows}</ul>",
			title=_("Transit Warehouse Not Empty"),
		)


# ------------------------------------------------------------------------------------------------
# Item Group summary, positive / negative difference, pure weight
# ------------------------------------------------------------------------------------------------


def build_item_group_summary(rows):
	"""Total qty per (Item Group, stock UOM) of the rows that have an item, with its gram value.

	Returns ``(summary_rows, grand_total_gram)``. Carat is converted at carat x 0.2 (carat / 5);
	UOMs without a gram value (Nos, ...) get 0 and stay out of the grand total.
	"""
	totals = {}
	for row in rows:
		if not row.get("item_code"):
			continue
		key = (cstr(row.get("item_group")), cstr(row.get("stock_uom")))
		totals[key] = totals.get(key, 0.0) + flt(row.get("qty"))

	summary, grand_total = [], 0.0
	for (item_group, uom), qty in sorted(totals.items()):
		factor = GRAM_FACTORS.get(uom)
		in_gram = flt(qty * factor, PRECISION) if factor else 0.0
		grand_total += in_gram
		summary.append(
			{
				"item_group": item_group or None,
				"uom": uom or None,
				"total_qty": flt(qty, PRECISION),
				"total_qty_in_gram": in_gram,
			}
		)
	return summary, flt(grand_total, PRECISION)


def difference_summary(rows):
	"""Set each Stock Comparison row's signed ``diff_qty`` (Stock Reconciliation qty - Child Stock
	Reconciliation qty) and return ``(positive_total, negative_total)``; the negative total is a
	magnitude."""
	positive = negative = 0.0
	for row in rows:
		diff = flt(flt(row.get("currenty_qty")) - flt(row.get("qty")), PRECISION)
		row.diff_qty = diff
		if diff > 0:
			positive += diff
		elif diff < 0:
			negative -= diff
	return flt(positive, PRECISION), flt(negative, PRECISION)


def _has_field(doctype, fieldname):
	return frappe.get_meta(doctype).has_field(fieldname)


def _fill_item_details(rows):
	"""Fill a missing item_group / stock_uom from Item (one read)."""
	missing = [
		r.item_code for r in rows if r.item_code and not (r.item_group and r.stock_uom)
	]
	if not missing:
		return
	items = bulk_map("Item", missing, ["item_group", "stock_uom"])
	for row in rows:
		details = items.get(row.item_code) or {}
		row.item_group = row.item_group or details.get("item_group")
		row.stock_uom = row.stock_uom or details.get("stock_uom")


def _summary_key(rows):
	return [
		(
			cstr(row.get("item_group")),
			cstr(row.get("uom")),
			flt(row.get("total_qty"), PRECISION),
			flt(row.get("total_qty_in_gram"), PRECISION),
		)
		for row in rows
	]


def set_reconciliation_summaries(self, method=None):
	"""validate: Item Group summary, pure weight (rows + total) and the signed Stock Comparison
	difference with its positive / negative totals. Each field is set only when it exists, so code
	deployed ahead of ``add_stock_reconciliation_summary_fields`` does not break the save."""
	items = self.get("items") or []
	comparison = self.get("custom_stock_comparison") or []
	purities = _metal_items(
		[r.item_code for r in items] + [r.item_code for r in comparison]
	)

	if _has_field("Stock Reconciliation Item", "custom_pure_weight"):
		total_pure = 0.0
		for row in items:
			row.custom_pure_weight = (
				pure_weight(row.qty, purities[row.item_code])
				if row.item_code in purities
				else 0.0
			)
			total_pure += row.custom_pure_weight
		if self.meta.has_field("custom_total_pure_weight"):
			self.custom_total_pure_weight = flt(total_pure, PRECISION)

	if self.meta.has_field("custom_item_group_summary"):
		_fill_item_details(items)
		summary, grand_total = build_item_group_summary(items)
		if _summary_key(self.get("custom_item_group_summary") or []) != _summary_key(
			summary
		):
			# Rebuilt only when it changed, so an unchanged save keeps the same rows.
			self.set("custom_item_group_summary", summary)
		if self.meta.has_field("custom_grand_total_gram"):
			self.custom_grand_total_gram = grand_total

	positive, negative = difference_summary(comparison)
	if _has_field("Stock Comparison", "pure_weight"):
		for row in comparison:
			row.pure_weight = (
				pure_weight(row.diff_qty, purities[row.item_code])
				if row.item_code in purities
				else 0.0
			)
	if self.meta.has_field("custom_positive_difference_qty"):
		self.custom_positive_difference_qty = positive
		self.custom_negative_difference_qty = negative


# ------------------------------------------------------------------------------------------------
# Pending transit receipt
# ------------------------------------------------------------------------------------------------


def _reconciliation_departments(doc):
	departments = {doc.get("custom_department")} - {None, ""}
	warehouses = {row.warehouse for row in doc.get("items") or [] if row.warehouse}
	if doc.get("set_warehouse"):
		warehouses.add(doc.set_warehouse)
	for details in bulk_map("Warehouse", warehouses, ["department"]).values():
		if details.get("department"):
			departments.add(details["department"])
	return departments


def get_pending_transit_legs():
	"""Transfers into a Transit warehouse whose stock is still sitting there, with ``qty`` = the
	part of that leg still in transit.

	``per_transferred`` alone cannot say that: ERPNext raises it only from a receipt linked through
	``outgoing_stock_entry``, so a transfer made without Add to Transit stays below 100 forever, and
	an item's transit stock may come from a newer entry than the one listed. So the stock now in
	each (item, transit warehouse) Bin is attributed newest-first to the not-fully-received entries
	that brought it in (window ``newer_qty`` = qty of those newer legs); a leg whose stock has
	already left transit drops out, and the block lifts as soon as the transit stock is moved on.

	Driven from the small side, in this order (``STRAIGHT_JOIN``): Transit warehouses -> their
	Bins holding stock (``Bin.warehouse`` index) -> the incoming ledger entries of that item in that
	warehouse (``item_code, warehouse, ...`` index) -> the Stock Entry row (``voucher_detail_no``,
	primary key) and header (primary key). Joining Stock Entry Detail on ``t_warehouse`` (no
	index) or on ``item_code`` (a gold item has hundreds of thousands of rows) took 9-34 s.
	"""
	return frappe.db.sql(
		"""
		select name, item_code, s_warehouse, t_warehouse, source_department, transit_department,
			least(leg_qty, in_transit - newer_qty) as qty
		from (
			select straight_join se.name, sle.item_code, sed.s_warehouse, sle.warehouse as t_warehouse,
				src.department as source_department, tw.department as transit_department,
				bin.actual_qty as in_transit, sle.actual_qty as leg_qty,
				sum(sle.actual_qty) over (
					partition by sle.item_code, sle.warehouse
					order by sle.posting_datetime desc, sle.creation desc
				) - sle.actual_qty as newer_qty
			from `tabWarehouse` tw
			join `tabBin` bin
				on bin.warehouse = tw.name and bin.actual_qty > 0
			join `tabStock Ledger Entry` sle
				on sle.item_code = bin.item_code and sle.warehouse = tw.name
				and sle.actual_qty > 0 and sle.is_cancelled = 0
				and sle.voucher_type = 'Stock Entry'
			join `tabStock Entry` se
				on se.name = sle.voucher_no and se.docstatus = 1
				and ifnull(se.per_transferred, 0) < 100
			join `tabStock Entry Detail` sed
				on sed.name = sle.voucher_detail_no
			left join `tabWarehouse` src on src.name = sed.s_warehouse
			where tw.warehouse_type = 'Transit'
		) legs
		where newer_qty < in_transit
		""",
		as_dict=True,
	)


def blocked_departments(legs, departments):
	"""The legs that touch ``departments`` -- through the source warehouse's department (sending
	side) or the transit warehouse's department (receiving side)."""
	return [
		leg
		for leg in legs
		if leg.get("source_department") in departments
		or leg.get("transit_department") in departments
	]


def validate_no_pending_transit(self, method=None):
	"""before_submit: refuse while a transit transfer into or out of one of this reconciliation's
	departments is still waiting for its receipt (e.g. Central RM -> Waxing Transit not yet
	received blocks both Central and Waxing)."""
	departments = _reconciliation_departments(self)
	if not departments:
		return

	blocking = blocked_departments(get_pending_transit_legs(), departments)
	if not blocking:
		return

	lines = sorted(
		{
			(
				leg.name,
				cstr(leg.get("item_code")),
				flt(leg.get("qty"), PRECISION),
				cstr(leg.t_warehouse),
			)
			for leg in blocking
		}
	)
	listed = "".join(
		"<li>{0}</li>".format(
			_("{0}: {1} - {2} still in {3}").format(name, item_code, qty, warehouse)
		)
		for name, item_code, qty, warehouse in lines[:MAX_LISTED_LEGS]
	)
	more = (
		_("<li>... and {0} more</li>").format(len(lines) - MAX_LISTED_LEGS)
		if len(lines) > MAX_LISTED_LEGS
		else ""
	)
	frappe.throw(
		_(
			"Stock Reconciliation cannot be submitted because a Transit Stock Entry is pending "
			"receipt for this department. Please complete the pending material receipt before "
			"proceeding with Stock Reconciliation."
		)
		+ f"<ul>{listed}{more}</ul>",
		title=_("Pending Transit Receipt"),
	)


# ------------------------------------------------------------------------------------------------
# Form helpers
# ------------------------------------------------------------------------------------------------


def _as_list(value):
	if not value:
		return []
	if isinstance(value, str):
		value = json.loads(value)
	return [cstr(v).strip() for v in value if cstr(v).strip()]


@frappe.whitelist()
def get_purity_percentages(item_codes):
	"""``{item_code: purity %}`` for the metal / finding items among ``item_codes``."""
	frappe.has_permission("Item", "read", throw=True)
	return _metal_items(_as_list(item_codes))


def employee_warehouses(warehouse):
	"""The employee warehouses that belong to department ``warehouse``: enabled, non-group,
	``employee`` set, the same ``warehouse_type`` and company, and ``custom_parent_department_warehouse``
	= its department. Only the ones the user can read (``get_list``), since ERPNext's ``get_items``
	throws on an unreadable warehouse."""
	if not _has_field("Warehouse", "custom_parent_department_warehouse"):
		return []
	details = frappe.db.get_value(
		"Warehouse",
		warehouse,
		["department", "warehouse_type", "company"],
		as_dict=True,
	)
	if not details or not details.department or not details.warehouse_type:
		return []
	return frappe.get_list(
		"Warehouse",
		filters={
			"disabled": 0,
			"is_group": 0,
			"employee": ["is", "set"],
			"warehouse_type": details.warehouse_type,
			"company": details.company,
			"custom_parent_department_warehouse": details.department,
			"name": ["!=", warehouse],
		},
		pluck="name",
		order_by="name asc",
	)


def _employee_warehouse_bins(warehouses, ignore_empty_stock):
	"""``(item_code, warehouse)`` of the Bins in ``warehouses`` for the items ERPNext's whole-
	warehouse fetch would take (enabled, stock, no variants), in warehouse then item order. With
	``ignore_empty_stock`` only Bins with stock now (``actual_qty``), so a back-dated reconciliation
	can miss an item that held stock on the posting date but is empty today."""
	if not warehouses:
		return []
	bin_ = frappe.qb.DocType("Bin")
	item = frappe.qb.DocType("Item")
	query = (
		frappe.qb.from_(bin_)
		.join(item)
		.on(item.name == bin_.item_code)
		.select(bin_.item_code, bin_.warehouse)
		.where(bin_.warehouse.isin(warehouses))
		.where(item.disabled == 0)
		.where(item.is_stock_item == 1)
		.where(item.has_variants == 0)
		.orderby(bin_.warehouse)
		.orderby(bin_.item_code)
	)
	if cint(ignore_empty_stock):
		query = query.where(bin_.actual_qty != 0)
	return [(row[0], row[1]) for row in query.run()]


def _fetch_warehouses(warehouse, main_slip):
	"""``warehouse`` and, when the form's Main Slip box is ticked, its employee warehouses."""
	if not cint(main_slip):
		return [warehouse]
	return [warehouse, *employee_warehouses(warehouse)]


@frappe.whitelist()
def get_warehouse_items(warehouse, txt=None, main_slip=0):
	"""Options for the Get Items multi-select: items with a Bin in ``warehouse`` (and, with Main
	Slip ticked, in its employee warehouses)."""
	frappe.has_permission("Warehouse", "read", doc=warehouse, throw=True)
	frappe.has_permission("Item", "read", throw=True)
	filters = {"warehouse": ["in", _fetch_warehouses(warehouse, main_slip)]}
	if txt:
		filters["item_code"] = ["like", f"%{txt}%"]
	item_codes = frappe.get_all(
		"Bin",
		filters=filters,
		pluck="item_code",
		group_by="item_code",
		order_by="item_code asc",
		limit_page_length=100,
	)
	names = bulk_map("Item", item_codes, ["item_name"])
	return [
		{
			"value": item_code,
			"description": (names.get(item_code) or {}).get("item_name") or "",
		}
		for item_code in item_codes
	]


@frappe.whitelist()
def get_items_for_reconciliation(
	warehouse,
	posting_date,
	posting_time,
	company,
	item_codes=None,
	select_all=0,
	ignore_empty_stock=1,
	main_slip=0,
):
	"""ERPNext's ``get_items`` for several items at once: every item of ``warehouse`` when
	``select_all``, else each of ``item_codes``. With Main Slip ticked the same is done for each of
	its employee warehouses; every row keeps its own warehouse. Permission checks stay ERPNext's."""
	args = {
		"posting_date": posting_date,
		"posting_time": posting_time,
		"company": company,
		"ignore_empty_stock": cint(ignore_empty_stock),
	}
	extra_warehouses = _fetch_warehouses(warehouse, main_slip)[1:]

	if cint(select_all):
		rows = list(erpnext_get_items(warehouse=warehouse, **args))
		# Not one whole-warehouse get_items per employee warehouse: each runs the full Batch-Wise
		# Balance History report (~200 s on a big ledger, whatever the warehouse holds). Item-wise
		# calls for the items it holds are each well under a second.
		for item_code, employee_warehouse in _employee_warehouse_bins(
			extra_warehouses, args["ignore_empty_stock"]
		):
			rows.extend(
				erpnext_get_items(
					warehouse=employee_warehouse, item_code=item_code, **args
				)
			)
		return rows

	codes = list(dict.fromkeys(_as_list(item_codes)))
	if not codes:
		frappe.throw(_("Select at least one item, or tick Select All Items."))

	# An employee warehouse only gets rows for the selected items it holds (has a Bin for), so
	# unticking Ignore Empty Stock doesn't add a zero row per item per employee warehouse.
	held = set()
	if extra_warehouses:
		held = {
			(b.item_code, b.warehouse)
			for b in frappe.get_all(
				"Bin",
				filters={
					"item_code": ["in", codes],
					"warehouse": ["in", extra_warehouses],
				},
				fields=["item_code", "warehouse"],
			)
		}
	rows = []
	for item_code in codes:
		rows.extend(erpnext_get_items(warehouse=warehouse, item_code=item_code, **args))
		for employee_warehouse in extra_warehouses:
			if (item_code, employee_warehouse) in held:
				rows.extend(
					erpnext_get_items(
						warehouse=employee_warehouse, item_code=item_code, **args
					)
				)
	return rows


class CustomStockReconciliation(StockReconciliation):
	def has_custom_mwo(self):
		"""Return True if any row has custom_manufacturing_work_order checked.

		Read via ``.get()`` (not raw attribute access): ``custom_manufacturing_work_order``
		is a gke_customization fixture field on Stock Reconciliation Item and is absent
		on sites where that fixture hasn't been synced, where ``row.custom_manufacturing_work_order``
		raises AttributeError and aborts every Stock Reconciliation save. ``.get()``
		returns None when the field is missing, so the flow degrades to standard ERPNext
		validation.
		"""
		for row in self.items:
			if row.get("custom_manufacturing_work_order"):
				return True
		return False

	def validate(self):
		# If condition is not met, run standard ERPNext validate
		if not self.has_custom_mwo():
			return super().validate()

		# If condition is met, allow saving draft without item_code
		if self.docstatus == 0:
			return

		# If not draft, run standard validations
		return super().validate()

	def on_submit(self):
		# If condition is not met, run standard ERPNext submit
		if not self.has_custom_mwo():
			return super().on_submit()

		# If condition is met, block submit if item_code missing
		for row in self.items:
			if not row.item_code:
				frappe.throw(
					_("You cannot submit Stock Reconciliation without Item Code.")
				)

		return super().on_submit()

	def remove_items_with_no_change(self):
		# disable validation
		return
