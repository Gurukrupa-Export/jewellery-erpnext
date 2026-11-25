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

from jewellery_erpnext.utils import bulk_map


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
# Fetch Items from Warehouse: several items (or all) at once; with Main Slip also employee warehouses
# ------------------------------------------------------------------------------------------------


def _has_field(doctype, fieldname):
	return frappe.get_meta(doctype).has_field(fieldname)


def _as_list(value):
	if not value:
		return []
	if isinstance(value, str):
		value = json.loads(value)
	return [cstr(v).strip() for v in value if cstr(v).strip()]


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
