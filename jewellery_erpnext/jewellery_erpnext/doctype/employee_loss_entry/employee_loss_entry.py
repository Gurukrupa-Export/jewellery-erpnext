# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Employee Loss Entry (req #8).

Standalone employee loss booking. The user picks an employee; the employee's Raw
Material (MSL) warehouse is resolved automatically; the user enters the loss
items/qty. On submit exactly ONE "Process Loss" (Repack) Stock Entry consumes the
loss qty from the MSL warehouse (FIFO batch-wise) and produces the mapped
loss-variant item into the department Scrap warehouse. The Employee Warehouse
Tracking report then reflects the new Loss (and recalculates Pending) with no
extra wiring, since it classifies Loss by the "Process Loss" stock entry type.

Mirrors the Metal Conversions melting-loss engine
(``doctype/metal_conversions/doc_events/melting_loss.py``).
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, nowtime, today

LOSS_SE_TYPE = "Process Loss"
LOSS_TYPE = "Loss"


def _loss_precision():
	# ERPNext recomputes flt(qty * conversion_factor, precision("transfer_qty"));
	# read the live precision instead of hardcoding 3.
	return frappe.get_precision("Stock Entry Detail", "transfer_qty") or 3


class EmployeeLossEntry(Document):
	def validate(self):
		if not self.posting_date:
			self.posting_date = today()
		if not self.posting_time:
			self.posting_time = nowtime()
		self.msl_warehouse = _resolve_msl_warehouse(self)
		self.scrap_warehouse = _resolve_scrap_warehouse(self)
		if not self.manufacturer and self.department:
			self.manufacturer = frappe.db.get_value(
				"Department", self.department, "manufacturer"
			)
		self._validate_items()

	def on_submit(self):
		make_employee_loss_stock_entry(self)
		_refresh_msl_tracking(self.msl_warehouse)

	def on_cancel(self):
		cancel_employee_loss_stock_entries(self)
		_refresh_msl_tracking(self.msl_warehouse)

	def _validate_items(self):
		if not self.items:
			frappe.throw(_("Add at least one loss item."))
		prec = _loss_precision()
		for row in self.items:
			if flt(row.qty, prec) <= 0:
				frappe.throw(
					_("Row {0}: Loss Qty must be greater than zero.").format(row.idx)
				)


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------


def _resolve_msl_warehouse(doc):
	if not doc.employee:
		frappe.throw(_("Employee is required."))
	wh = frappe.db.get_value(
		"Warehouse",
		{"disabled": 0, "employee": doc.employee, "warehouse_type": "Raw Material"},
	)
	if not wh:
		frappe.throw(
			_(
				"No active Raw Material (MSL) warehouse found for employee {0}. "
				"Create one from the Warehouse list first."
			).format(doc.employee)
		)
	return wh


def _resolve_scrap_warehouse(doc):
	if not doc.department:
		frappe.throw(_("Department is required to resolve the Scrap warehouse."))
	from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import (
		get_scrap_warehouse,
	)

	return get_scrap_warehouse(doc.department)


def _resolve_loss_item(doc, item_code):
	"""Resolve the mapped loss-variant item for a metal/finding item (req #8)."""
	variant_of = frappe.db.get_value("Item", item_code, "variant_of")
	if not variant_of:
		frappe.throw(
			_("Item {0} has no variant template; cannot resolve a loss item.").format(
				item_code
			)
		)
	if not doc.manufacturer:
		frappe.throw(
			_(
				"Manufacturer is required to look up the loss item. Set it on the "
				"Department or on this Employee Loss Entry."
			)
		)

	from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
		get_item_loss_item,
	)

	loss_item = get_item_loss_item(doc.company, item_code, variant_of, LOSS_TYPE)
	if not loss_item:
		frappe.throw(
			_(
				"No loss item configured for {0} (variant {1}, loss type {2}). "
				"Configure the Variant Loss Table on the Manufacturer."
			).format(item_code, variant_of, LOSS_TYPE)
		)
	return loss_item


def _fifo_batches(doc, item_code, warehouse, qty):
	"""FIFO-allocate ``qty`` across the batches of ``item_code`` in ``warehouse``.

	Uses the app's capped FIFO helper (never offers more than the authoritative
	Serial-and-Batch balance). Returns a list of ``frappe._dict(batch_no, qty)``.
	Non-batch items return a single unbatched row.
	"""
	prec = _loss_precision()
	need = flt(qty, prec)
	if not frappe.get_cached_value("Item", item_code, "has_batch_no"):
		return [frappe._dict(batch_no=None, qty=need)]

	from jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger import (
		capped_auto_batch_nos,
	)

	kwargs = frappe._dict(
		posting_date=doc.posting_date,
		posting_time=doc.posting_time,
		item_code=item_code,
		warehouse=warehouse,
		qty=need,
		for_stock_levels=False,
		consider_negative_batches=False,
	)
	batches = capped_auto_batch_nos(kwargs) or []
	allocated = flt(sum(flt(b.qty) for b in batches), prec)
	if allocated < need:
		frappe.throw(
			_(
				"Insufficient stock of {0} in MSL warehouse {1}: need {2}, "
				"available {3}."
			).format(item_code, warehouse, need, allocated)
		)
	return batches


# ---------------------------------------------------------------------------
# Stock Entry creation / cancellation
# ---------------------------------------------------------------------------


def make_employee_loss_stock_entry(doc):
	"""Build + submit ONE "Process Loss" Repack SE moving each loss item from the
	employee MSL (Raw Material) warehouse to the department Scrap warehouse.
	Idempotent via the ``stock_entry`` link stored back on the document.
	"""
	if doc.stock_entry and frappe.db.exists(
		"Stock Entry", {"name": doc.stock_entry, "docstatus": ["!=", 2]}
	):
		return

	prec = _loss_precision()
	msl_wh = doc.msl_warehouse or _resolve_msl_warehouse(doc)
	scrap_wh = doc.scrap_warehouse or _resolve_scrap_warehouse(doc)

	# Resolve consume batches + produce loss item per row up front.
	plan = []
	for row in doc.items:
		qty = flt(row.qty, prec)
		loss_item = _resolve_loss_item(doc, row.item_code)
		if row.batch_no:
			batches = [frappe._dict(batch_no=row.batch_no, qty=qty)]
		else:
			batches = _fifo_batches(doc, row.item_code, msl_wh, qty)
		plan.append(
			frappe._dict(
				item_code=row.item_code, qty=qty, loss_item=loss_item, batches=batches
			)
		)

	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_bins,
		preallocate_series_for_docs,
		stock_lock_key,
	)

	# RULE A — deterministic consume order (sorted) avoids 1213 lock cycles.
	plan.sort(key=lambda p: stock_lock_key(p.item_code, msl_wh, None))
	# Canonical lock order: pin the Stock Entry naming-series row BEFORE the Bins so
	# this flow is Series-then-Bin like every conformant SE submit.
	_series_stub = frappe.new_doc("Stock Entry")
	_series_stub.company = doc.company
	_series_stub.stock_entry_type = LOSS_SE_TYPE
	preallocate_series_for_docs(_series_stub)
	# RULE B — pre-lock consume (item, MSL) + produce (loss_item, Scrap) Bins.
	bin_pairs = [(p.item_code, msl_wh) for p in plan]
	bin_pairs += [(p.loss_item, scrap_wh) for p in plan]
	lock_bins(bin_pairs)

	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": LOSS_SE_TYPE,
			"purpose": "Repack",
			"company": doc.company,
			"branch": doc.branch,
			"department": doc.department,
			# Header manufacturer is required: the global M/F pure-metal block
			# (doc_events/stock_entry.py) runs on the consume rows at submit and
			# reads self.manufacturer.
			"manufacturer": doc.manufacturer,
			"inventory_type": "Regular Stock",
			"posting_date": doc.posting_date,
			"posting_time": doc.posting_time,
			"set_posting_time": 1,
			"auto_created": 1,
		}
	)

	for p in plan:
		for b in p.batches:
			se.append(
				"items",
				{
					"item_code": p.item_code,
					"qty": flt(b.qty, prec),
					"s_warehouse": msl_wh,
					"batch_no": b.batch_no,
					"inventory_type": "Regular Stock",
					"department": doc.department,
					"employee": doc.employee,
					"manufacturer": doc.manufacturer,
					"use_serial_batch_fields": 1,
				},
			)
		# ONE produce row into Scrap per input item. No batch_no: the loss variant
		# has create_new_batch = 1, so the Serial and Batch Bundle mints a NEW batch.
		se.append(
			"items",
			{
				"item_code": p.loss_item,
				"qty": p.qty,
				"t_warehouse": scrap_wh,
				"is_finished_item": 1,
				"set_basic_rate_manually": 1,
				"inventory_type": "Regular Stock",
				"department": doc.department,
				"employee": doc.employee,
				"manufacturer": doc.manufacturer,
				"use_serial_batch_fields": 1,
			},
		)

	se.flags.ignore_permissions = True
	se.save()
	se.submit()
	# db_set persists post-save; a plain attribute assignment would not.
	doc.db_set("stock_entry", se.name)


def cancel_employee_loss_stock_entries(doc):
	"""Cancel the auto-created Process Loss SE owned by this document."""
	if not doc.stock_entry:
		return
	se = frappe.db.get_value(
		"Stock Entry", doc.stock_entry, ["name", "docstatus"], as_dict=True
	)
	if se and se.docstatus == 1:
		frappe.get_doc("Stock Entry", se.name).cancel()


def _refresh_msl_tracking(warehouse):
	"""Recompute the warehouse's maintained tracking table after a loss booking
	(req #8: "Loss quantity is updated in warehouse tracking records. Pending
	quantity is recalculated automatically"). A tracking-refresh failure must not
	roll back the loss booking, so failures are logged, not raised.
	"""
	if not warehouse:
		return
	from jewellery_erpnext.jewellery_erpnext.doc_events.warehouse_tracking import (
		recalculate_msl_tracking,
	)

	try:
		recalculate_msl_tracking(warehouse)
	except Exception:
		frappe.log_error(
			title="Employee Loss Entry: MSL tracking refresh failed",
			message=frappe.get_traceback(),
		)
