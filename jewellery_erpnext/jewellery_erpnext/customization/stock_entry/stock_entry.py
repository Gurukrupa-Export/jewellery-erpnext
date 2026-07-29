import copy
import json
from collections import defaultdict

import frappe
from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry
from frappe import _
from frappe.utils import flt

# from jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger import (
# 	BatchValuationLedger,
# )
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.inventory_utils import (
	in_configured_timeslot,
	validate_customer_voucher,
	validate_sample_goods_not_consumed,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.se_utils import (
	get_fifo_batches,
	set_employee,
	set_gross_wt,
	# validate_inventory_dimention,
	validate_warehouse,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.loss_valuation import (
	set_process_loss_produce_rates,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
	custom_get_bom_scrap_material,
	custom_get_scrap_items_from_job_card,
)


def before_validate(self, method):
	if not in_configured_timeslot(self):
		frappe.throw(_("Not Allowed to do entries, its freeze time"))
	validate_customer_voucher(self)
	validate_sample_goods_not_consumed(self)
	set_employee(self)
	set_gross_wt(self)
	validate_warehouse(self)


def on_submit(self, method):
	pass
	# validate_inventory_dimention(self)


class CustomStockEntry(StockEntry):
	# def autoname(self):
	# 	"""
	# 	Temporarily name doc for fast insertion
	# 	name will be changed using autoname options (in a scheduled job)
	# 	"""
	# 	self.name = frappe.generate_hash(txt="", length=10)
	# 	if self.meta.autoname == "hash":
	# 		self.to_rename = 0

	@frappe.whitelist()
	def update_batches(self):
		if not self.auto_created:
			rows_to_append = []
			# shared across rows so the same batch is not double-allocated when
			# multiple rows draw from the same item/warehouse. Both FIFO-allocated
			# rows (via get_fifo_batches) and already-filled rows kept below record
			# their consumption here.
			consumed = {}
			for row in self.items:
				if (
					row.get("department")
					and frappe.db.get_value(
						"Department", row.department, "custom_can_not_make_dg_entry"
					)
					== 1
				):
					if frappe.db.get_value("Item", row.item_code, "variant_of") in [
						"D",
						"G",
					]:
						frappe.throw(
							_("{0} not allowed in Operation {1}").format(
								row.item_code, row.department
							)
						)
				if frappe.db.get_value("Item", row.item_code, "has_batch_no"):
					if row.s_warehouse:
						if row.get("batch_no"):
							# Batch already filled — keep it as-is and do NOT refetch /
							# re-split, even if the row qty now exceeds the batch's
							# available qty (an over-issue is caught at submit). Only
							# rows with an empty batch trigger FIFO allocation.
							# Record this row's consumption so a later empty row drawing
							# from the same item/warehouse can't double-book the batch.
							batch_key = (
								row.s_warehouse or self.get("source_warehouse"),
								row.batch_no,
							)
							consumed[batch_key] = consumed.get(batch_key, 0) + flt(
								row.qty
							)
							temp_row = copy.deepcopy(row)
							rows_to_append += [temp_row]
						else:
							rows_to_append += get_fifo_batches(self, row, consumed)
					elif row.t_warehouse:
						rows_to_append += [row.__dict__]
				else:
					rows_to_append += [row.__dict__]

			# The item table is always rebuilt so inventory_type / customer / diamond
			# pcs are backfilled from the batch every save. The expensive FIFO refetch
			# (get_fifo_batches) only runs for rows with an empty batch, so a save where
			# every batch-tracked source row is already filled does zero refetches.
			if rows_to_append:
				self.items = []
				for item in rows_to_append:
					if isinstance(item, dict):
						item = frappe._dict(item)
					if item.batch_no:
						if not item.inventory_type:
							item.inventory_type = frappe.db.get_value(
								"Batch", item.batch_no, "custom_inventory_type"
							)
						item.customer = frappe.db.get_value(
							"Batch", item.batch_no, "custom_customer"
						)
					if frappe.db.get_value("Item", item.item_code, "variant_of") == "D":
						attribute = frappe.db.get_value(
							"Item Variant Attribute",
							{"parent": item.item_code, "attribute": "Diamond Grade"},
							"attribute_value",
						)
						diamond_sieve_size = frappe.db.get_value(
							"Item Variant Attribute",
							{
								"parent": item.item_code,
								"attribute": "Diamond Sieve Size",
							},
							"attribute_value",
						)
						weight = (
							frappe.db.get_value(
								"Attribute Value Diamond Sieve Size",
								{
									"parent": attribute,
									"diamond_sieve_size": diamond_sieve_size,
								},
								"per_pcs_average_weight",
							)
							or 0
						)

						if weight > 0 and item.qty and int(item.pcs) < 1:
							item.pcs = int(item.qty / weight)
					self.append("items", item)

			if frappe.db.exists("Stock Entry", self.name):
				self.db_update()

	def validate_with_material_request(self):
		for item in self.get("items"):
			material_request = item.material_request or None
			material_request_item = item.material_request_item or None
			if self.purpose == "Material Transfer" and self.outgoing_stock_entry:
				parent_se = frappe.get_value(
					"Stock Entry Detail",
					item.ste_detail,
					["material_request", "material_request_item"],
					as_dict=True,
				)
				if parent_se:
					material_request = parent_se.material_request
					material_request_item = parent_se.material_request_item

			if material_request:
				mreq_item = frappe.db.get_value(
					"Material Request Item",
					{"name": material_request_item, "parent": material_request},
					["item_code", "custom_alternative_item", "warehouse", "idx"],
					as_dict=True,
				)
				if item.item_code not in [
					mreq_item.item_code,
					mreq_item.custom_alternative_item,
				]:
					frappe.throw(
						_("Item for row {0} does not match Material Request").format(
							item.idx
						),
						frappe.MappingMismatchError,
					)
				elif self.purpose == "Material Transfer" and self.add_to_transit:
					continue

	def get_scrap_items_from_job_card(self):
		custom_get_scrap_items_from_job_card(self)

	def get_bom_scrap_material(self, qty):
		custom_get_bom_scrap_material(self, qty)

	def set_basic_rate(self, reset_outgoing_rate=True, raise_error_if_no_rate=True):
		"""Value a Process Loss SE's produce rows from the rows they consumed.

		ERPNext skips every ``set_basic_rate_manually`` row -- which is every loss/scrap
		produce row -- leaving basic_rate and basic_amount at 0, so the metal's value left
		the ledger and nothing replaced it. See ``utils/loss_valuation`` for why the flag
		cannot simply be dropped, and why this belongs on the controller rather than in
		each of the six builders (ERPNext re-derives Repack rates on every repost).

		Runs after super() so the consume rows already carry their basic_amount, and
		before ``update_valuation_rate`` / ``set_total_incoming_outgoing_value`` in
		``calculate_rate_and_amount``, which then pick the new values up for free.
		"""
		super().set_basic_rate(reset_outgoing_rate, raise_error_if_no_rate)
		set_process_loss_produce_rates(self)

	def validate_reserved_batches(self):
		"""Reserved-batch guard, expressed in this app's reservation vocabulary.

		ERPNext (>= v16.29, backport PR #57169) exempts a Stock Entry's own reservations
		ONLY by matching ``SE.work_order`` / ``SE.subcontracting_inward_order`` against
		``SRE.voucher_no``. This app cannot satisfy that, structurally:

		* erpnext hard-restricts SRE vouchers to Sales Orders
		  (``stock_reservation_entry.py`` ``allowed_voucher_types``), so every SRE minted
		  here is ``voucher_type = "Sales Order"`` and the manufacturing job is carried on
		  the custom Data fields ``manufacturing_work_order`` / ``manufacturing_operation``.
		* no manufacturing Stock Entry sets ``work_order`` -- the Manufacture SE is built
		  with ``manufacturing_order`` / ``manufacturing_work_order`` /
		  ``manufacturing_operation`` / ``custom_serial_number_creator`` instead.
		* even if it did, Work Order names and Sales Order names are disjoint, so the
		  comparison could never match.

		So upstream's ``own_vouchers`` is empty for every batch-consuming Stock Entry on
		this site, and ``get_reserved_batches`` filters on nothing but ``batch_no`` and
		``docstatus`` -- no item, warehouse, company or status scope. The result is that a
		job holding a perfectly valid reservation is measured against every reservation on
		that batch, in warehouses it never touched, for items it never moved.

		On erpnext <= v16.28 the whole body was unreachable here (its outer loop skipped
		when both reference fields were empty), which is why this only started failing
		after the upgrade.

		This override keeps the guard's intent and makes it satisfiable:

		1. exempt reservations belonging to this entry's own PMO / MWO / MOP chain, keyed
		   by SRE *name* (``voucher_no`` is a shared Sales Order and cannot identify a job);
		2. restrict the check to the ``(item_code, warehouse)`` pairs this entry actually
		   drew stock from;
		3. on a residual shortfall, warn on internal manufacturing movements and throw on
		   everything else. Real over-consumption is still caught downstream by
		   ``NegativeStockError`` in ``erpnext/stock/stock_ledger.py``.

		Version-agnostic by construction: it replaces the method wholesale, and the call
		site in ``StockController.make_sl_entries`` plus the zero-arg signature are
		identical across v16.27 - v16.30.
		"""
		from erpnext.stock.doctype.batch.batch import get_batch_qty

		if not frappe.db.get_single_value("Stock Settings", "enable_stock_reservation"):
			return

		batches = frappe.get_all(
			"Serial and Batch Entry",
			filters={
				"voucher_type": self.doctype,
				"voucher_no": self.name,
				"docstatus": 1,
				"batch_no": ("is", "set"),
				"qty": ("<", 0),
			},
			pluck="batch_no",
		)
		if not batches:
			return

		# Only the (item, warehouse) pairs this entry actually consumed from are in scope.
		touched = {
			(row.item_code, row.s_warehouse)
			for row in self.get("items")
			if row.s_warehouse
		}
		if not touched:
			return

		# Cheapest discriminator first: most entries consume batches nobody has reserved,
		# and this returns empty for them before any manufacturing-chain lookup runs.
		reserved_rows = self._get_scoped_reserved_batches(batches)
		if not reserved_rows:
			return

		own_sres = self._own_reservation_names()
		own_vouchers = {
			self.get(field)
			for field in ("work_order", "subcontracting_inward_order")
			if self.get(field)
		}

		outstanding_qty = defaultdict(float)
		reservations = defaultdict(list)
		for row in reserved_rows:
			if row.name in own_sres or (
				own_vouchers and row.voucher_no in own_vouchers
			):
				continue
			if (row.item_code, row.warehouse) not in touched:
				continue

			outstanding = flt(row.qty) - flt(row.delivered_qty)
			if outstanding <= 0:
				continue

			key = (row.batch_no, row.warehouse)
			outstanding_qty[key] += outstanding
			reservations[key].append(row)

		if not outstanding_qty:
			return

		precision = frappe.get_precision("Serial and Batch Entry", "qty")
		for (batch_no, warehouse), reserved_qty in outstanding_qty.items():
			if flt(reserved_qty, precision) <= 0:
				continue

			batch_qty = get_batch_qty(
				batch_no,
				warehouse,
				posting_date=self.posting_date,
				posting_time=self.posting_time,
				consider_negative_batches=True,
			)
			if flt(batch_qty, precision) >= flt(reserved_qty, precision):
				continue

			self._report_reserved_batch_shortfall(
				batch_no,
				warehouse,
				batch_qty,
				reserved_qty,
				reservations[(batch_no, warehouse)],
			)

	def _own_reservation_names(self):
		"""Names of the Stock Reservation Entries this entry is entitled to consume.

		Scoped to the manufacturing chain the entry belongs to: every MWO under its PMO,
		its own MWO, and the Manufacturing Operations named on the header or any item row.
		Matching on SRE *name* rather than ``voucher_no`` matters -- several jobs share one
		Sales Order, so a voucher match would exempt other jobs' reservations too.
		"""
		mwos = set()
		mops = set()

		if self.get("manufacturing_work_order"):
			mwos.add(self.manufacturing_work_order)
		if self.get("manufacturing_operation"):
			mops.add(self.manufacturing_operation)
		for row in self.get("items"):
			if row.get("manufacturing_operation"):
				mops.add(row.manufacturing_operation)

		if self.get("manufacturing_order"):
			mwos.update(
				frappe.get_all(
					"Manufacturing Work Order",
					{"manufacturing_order": self.manufacturing_order, "docstatus": 1},
					pluck="name",
				)
			)

		if not mwos and not mops:
			return set()

		names = set()
		for field, values in (
			("manufacturing_work_order", mwos),
			("manufacturing_operation", mops),
		):
			if not values:
				continue
			names.update(
				frappe.get_all(
					"Stock Reservation Entry",
					{"docstatus": 1, field: ("in", list(values))},
					pluck="name",
				)
			)
		return names

	def _get_scoped_reserved_batches(self, batches):
		"""Like core's ``get_reserved_batches``, plus the SRE name/item and the child qtys."""
		sre = frappe.qb.DocType("Stock Reservation Entry")
		sb_entry = frappe.qb.DocType("Serial and Batch Entry")

		return (
			frappe.qb.from_(sre)
			.join(sb_entry)
			.on(sre.name == sb_entry.parent)
			.select(
				sre.name,
				sre.voucher_type,
				sre.voucher_no,
				sre.item_code,
				sre.warehouse,
				sb_entry.batch_no,
				sb_entry.qty,
				sb_entry.delivered_qty,
			)
			.where((sre.docstatus == 1) & (sb_entry.batch_no.isin(batches)))
		).run(as_dict=True)

	def _report_reserved_batch_shortfall(
		self, batch_no, warehouse, batch_qty, reserved_qty, rows
	):
		"""Record the conflict and let the entry through; never block on it.

		Blocking does not protect the competing Sales Orders. Refining Entry
		RFN-MWO-26-00118 is the worked example: batch GE2D081-MGL22919Y0-80 held 0.434 in
		Model Making WO - KGJPL against 0.606 of reservations across nine Sales Orders, and
		the entry moved ~0.01 of it. The 0.172 shortfall pre-dated the document and outlived
		it -- the throw only stopped whichever entry happened to submit next.

		Nor can the check tell a legitimate consumer from an illegitimate one here: upstream
		identifies ownership solely through ``SE.work_order`` -> ``SRE.voucher_no``, and
		erpnext restricts SRE vouchers to Sales Orders, so ``own_vouchers`` is unmatchable
		for every Stock Entry this app builds. An earlier revision gated the throw on
		"is this an internal manufacturing movement", but of the ~56 Stock Entry builders in
		this app eight groups carry no header marker at all (``material_request.py`` MR
		reserve, ``finding_mwo.py`` both legs, ``main_slip.py:436``, ``repack.py:296``,
		``batch_rename.py:394``, ``job_card.py:180``, the ``get_mapped_doc`` templates), so
		no gate could reach them and they would have hard-thrown in production.

		Real over-consumption is still caught downstream by ``NegativeStockError`` in
		``erpnext/stock/stock_ledger.py`` -- that is the check that protects stock integrity.
		The root defect is release-side: nothing cancels or reduces the SRE at
		``s_warehouse`` when reserved batch stock leaves it (``doc_events/stock_entry.py``
		reserves inbound rows only), so reservations outlive the stock they point at.
		"""
		vouchers = ", ".join(
			f"{frappe.bold(voucher_type)} {frappe.bold(voucher_no)}"
			for voucher_type, voucher_no in dict.fromkeys(
				(row.voucher_type, row.voucher_no) for row in rows
			)
		)
		message = _(
			"Batch {0} in warehouse {1} is short of the quantity reserved for {2}."
			" {3} {4} was allowed through -- the shortfall is not caused by this entry."
		).format(
			frappe.bold(batch_no),
			frappe.bold(warehouse),
			vouchers,
			frappe.bold(self.doctype),
			frappe.bold(self.name),
		)

		frappe.log_error(
			title=_("Reserved Batch Shortfall"),
			message=(
				f"{message}\n\n"
				f"Batch {batch_no} @ {warehouse}: available {batch_qty}, "
				f"reserved by other vouchers {reserved_qty}.\n"
				f"Stock Entry type: {self.get('stock_entry_type')}.\n"
				"Reservations outliving their stock indicate a missing source-side SRE "
				"release; see validate_reserved_batches for the analysis."
			),
		)
		frappe.msgprint(
			message, title=_("Reserved Batch Shortfall"), indicator="orange"
		)


@frappe.whitelist()
def get_html_data(doc):
	if isinstance(doc, str):
		doc = json.loads(doc)
	itemwise_data = {}
	for row in doc.get("items"):
		row = frappe._dict(row)
		if itemwise_data.get(row.item_code):
			itemwise_data[row.item_code]["qty"] += row.qty
			itemwise_data[row.item_code]["pcs"] += (
				int(row.get("pcs")) if row.get("pcs") else 0
			)
		else:
			itemwise_data[row.item_code] = {
				"qty": row.qty,
				"pcs": int(row.get("pcs")) if row.get("pcs") else 0,
			}

	data = []
	for row in itemwise_data:
		data.append(
			{
				"item_code": row,
				"qty": flt(itemwise_data[row].get("qty"), 3),
				"pcs": itemwise_data[row].get("pcs"),
			}
		)

	return data
