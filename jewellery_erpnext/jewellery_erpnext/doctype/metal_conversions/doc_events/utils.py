import copy

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.utils import flt, nowtime

from jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger import (
	capped_auto_batch_nos,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.se_utils import (
	get_fifo_batches,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.sample_goods import (
	get_sample_batches,
)


def update_batch_details(self):
	rows_to_append = []
	self.flags.only_regular_stock_allowed = True

	if self.doctype == "Diamond Conversion":
		child_table = self.sc_source_table
	else:
		child_table = self.mc_source_table

	# shared across rows so the same batch is not double-allocated when multiple
	# rows draw from the same item/warehouse (see get_fifo_batches)
	consumed = {}
	for row in child_table:
		warehouse = row.get("s_warehouse") or self.get("source_warehouse")
		if row.get("batch") and get_batch_qty(row.batch, warehouse) >= row.qty:
			temp_row = copy.deepcopy(row)
			temp_row.batch_no = temp_row.batch
			rows_to_append += [temp_row]
		else:
			rows_to_append += get_fifo_batches(self, row, consumed)

	if rows_to_append:
		if self.doctype == "Diamond Conversion":
			self.sc_source_table = []
		else:
			self.mc_source_table = []

	for item in rows_to_append:
		if isinstance(item, dict):
			item = frappe._dict(item)
		item.name = None
		if item.batch_no:
			item.batch = item.batch_no
		batch = item.batch_no or item.batch
		if batch:
			if not item.inventory_type:
				item.inventory_type = frappe.db.get_value(
					"Batch", batch, "custom_inventory_type"
				)
			item.customer = frappe.db.get_value("Batch", batch, "custom_customer")
		if self.doctype == "Diamond Conversion":
			self.append("sc_source_table", item)
		else:
			self.append("mc_source_table", item)


def update_alloy_betch(self):
	if flt(self.source_alloy_qty) <= 0:
		return
	if not self.source_alloy and flt(self.source_alloy_qty) > 0:
		frappe.throw(_("Please Select The Alloy"))
	if (
		self.source_alloy_batch
		and self.source_alloy_qty
		and get_batch_qty(self.source_alloy_batch, self.source_warehouse)
		< flt(self.source_alloy_qty, 3)
	):
		frappe.msgprint(
			_("Selected batch does not have sufficient qty for transaction")
		)
	else:
		batch_data = capped_auto_batch_nos(
			frappe._dict(
				{
					"posting_date": self.get("posting_date") or self.get("date"),
					"posting_time": self.get("posting_time") or nowtime(),
					"item_code": self.source_alloy,
					"warehouse": self.source_warehouse,
					"qty": self.source_alloy_qty,
				}
			)
		)

		if not batch_data:
			frappe.throw(_("No batch available for given warehouse"))
		self.alloy_batch_details = []
		# batch_data = batch_data[0]
		if batch_data:
			remaining_qty = 0
			total_qty = 0
			for i in batch_data:
				qty = 0
				if flt(self.source_alloy_qty) > i.qty:
					qty = i.qty
					remaining_qty += i.qty
					total_qty += qty
				else:
					qty = flt(self.source_alloy_qty) - remaining_qty
					total_qty += qty
				self.append("alloy_batch_details", {"qty": qty, "batch": i.batch_no})
			if total_qty != flt(self.source_alloy_qty):
				frappe.throw(
					_(
						"The source quantity is not available for the given warehouse. The available quantity is {}.".format(
							total_qty
						)
					)
				)
		# if flt(self.source_alloy_qty) > batch_data.qty:
		# 	frappe.msgprint(
		# 		_("{0} missing for transaction in Batch {1}").format(
		# 			(self.source_alloy_qty - batch_data.qty), batch_data.batch_no
		# 		)
		# 	)

		# self.source_alloy_batch = batch_data.batch_no


def get_batch_lane_map(batch_nos):
	"""``{batch_no: (inventory_type, customer)}`` for every batch in ``batch_nos``.

	One bulk read instead of a ``get_value`` per candidate batch -- the house
	bulk-prefetch convention, and this runs inside ``validate`` on every save.

	A NULL ``custom_inventory_type`` is normalised to "Regular Stock": untyped
	batches are company stock, and treating them as a distinct ownership made
	them silently unallocatable (``None != "Regular Stock"``), which surfaced as
	a bogus "source quantity is not available" throw rather than a diagnosable one.
	"""
	batch_nos = {b for b in batch_nos if b}
	if not batch_nos:
		return {}

	return {
		row.name: (row.custom_inventory_type or "Regular Stock", row.custom_customer)
		for row in frappe.get_all(
			"Batch",
			filters={"name": ["in", list(batch_nos)]},
			fields=["name", "custom_inventory_type", "custom_customer"],
		)
	}


def update_source_betch(self):
	# Cap each batch at its authoritative Serial-and-Batch balance so an orphan
	# bundle (docstatus=1, no SLE) can't inflate availability and seed a phantom
	# row that later throws BatchNegativeStockError at SE submit. qty is omitted
	# deliberately -> capped_auto_batch_nos returns ALL real batches so the
	# filtering below runs against the full set.
	batch_data = capped_auto_batch_nos(
		frappe._dict(
			{
				"posting_date": self.get("posting_date") or self.get("date"),
				"posting_time": self.get("posting_time") or nowtime(),
				"item_code": self.source_item,
				"warehouse": self.source_warehouse,
				# "qty": self.source_qty,
			}
		)
	)

	if not batch_data:
		frappe.throw(_("No batch available for given warehouse"))
	self.source_batch_details = []
	# In melting-loss mode only the Loss Qty is consumed (RM -> Scrap); the
	# remainder is untouched. Conversion mode allocates source_qty.
	is_melting_loss = bool(self.get("is_melting_loss"))
	required_qty = flt(self.loss_qty) if is_melting_loss else flt(self.source_qty)

	lane_map = get_batch_lane_map([i.batch_no for i in batch_data])

	# Conversion mode draws FIFO across every ownership present in the warehouse and
	# splits the result into per-(inventory_type, customer) lanes downstream -- the
	# operator no longer declares the ownership up front, so there is no
	# inventory-type or customer filter here.
	#
	# Melting loss stays single-lane on purpose: make_melting_loss_stock_entry books
	# ONE scrap row force-typed "Regular Stock", so admitting a customer's batch here
	# would silently convert customer metal into company scrap. Restrict it instead of
	# mis-booking it; lane-splitting the loss flow is separate work.
	#
	# Customer Sample Goods is excluded in both modes. Sample stock may only appear as
	# a source row on the three Customer Goods movements -- "Repack-Metal Conversion"
	# is not in SAMPLE_ALLOWED_SE_TYPES -- so allocating one here would make the doc
	# un-submittable at validate_sample_goods_not_consumed. Mirrors the FIFO skip in
	# customization/stock_entry/doc_events/se_utils.py.
	sample_batches = get_sample_batches([i.batch_no for i in batch_data])

	remaining_qty = 0
	total_qty = 0

	for i in batch_data:
		if i.batch_no in sample_batches:
			continue

		inventory_type, _customer = lane_map.get(i.batch_no, ("Regular Stock", None))
		if is_melting_loss and inventory_type != "Regular Stock":
			continue

		if total_qty != required_qty:
			# If the current batch has more quantity than needed, use the difference
			if required_qty > remaining_qty + i.qty:
				qty = i.qty
				remaining_qty += i.qty
			else:
				qty = required_qty - remaining_qty
				remaining_qty = required_qty  # Ensure remaining_qty equals required_qty
			total_qty += qty

			# Append details to source_batch_details, preserving FIFO order
			self.append("source_batch_details", {"qty": qty, "batch": i.batch_no})

		if remaining_qty >= required_qty:
			break  # Stop if we have filled the required quantity

	if total_qty != required_qty:
		frappe.throw(
			_(
				"The source quantity is not available for the given warehouse. The available quantity is {}.".format(
					total_qty
				)
			)
		)
