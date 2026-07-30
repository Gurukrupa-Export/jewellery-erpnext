# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt

import re

import frappe
from erpnext.controllers.queries import get_batch_no
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events.lanes import (
	REGULAR_STOCK,
	apportion,
	build_lanes,
	split_allocations,
	split_conversion,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events.melting_loss import (
	cancel_melting_loss_stock_entries,
	make_melting_loss_stock_entry,
	validate_melting_loss,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events.utils import (
	get_batch_lane_map,
	update_alloy_betch,
	update_batch_details,
	update_source_betch,
)

# Remark sentences offered by the Remarks dropdown. Add a sentence here and it shows up
# in the form with no other change; use "{percentage}" where the document's Percentage
# belongs, or leave it out for a sentence that carries no percentage.
REMARK_TEMPLATES = ("NR {percentage}% PLAIN ROUND BALLS LOSS BOOK",)

# Matches the number a rendered template put in place of "{percentage}".
_PERCENTAGE_PATTERN = "[-0-9.]+"


def render_remark_options(percentage, precision):
	"""Render every remark sentence with this document's Percentage substituted.

	Single source of truth for the Remarks dropdown: the client fills the Select from
	this (via ``MetalConversions.get_remark_options``) and ``set_remarks`` re-renders the
	stored value from it, so the text the user picked and the text we store cannot drift.
	"""
	value = f"{flt(percentage, precision):.{precision}f}"
	return [template.format(percentage=value) for template in REMARK_TEMPLATES]


def template_index(remark):
	"""Index of the REMARK_TEMPLATES entry a stored remark came from, else None.

	Matches on the fixed words only, so a remark rendered at one percentage is still
	recognised after the Percentage has been edited -- that is what lets ``set_remarks``
	re-render it instead of rejecting it.
	"""
	for idx, template in enumerate(REMARK_TEMPLATES):
		body = _PERCENTAGE_PATTERN.join(
			re.escape(part) for part in template.split("{percentage}")
		)
		if re.match("^" + body + "$", remark or ""):
			return idx
	return None


class MetalConversions(Document):
	def on_submit(self):
		if self.get("is_melting_loss"):
			# Pure loss-recording mode: book ONLY the Loss Qty as a Process Loss SE
			# (RM -> Scrap). No conversion Stock Entry is created.
			make_melting_loss_stock_entry(self)
			return
		if self.multiple_metal_converter == 0:
			if (
				self.target_item
				and self.target_item.startswith("M")
				and "24KT" in self.target_item
			):
				pass
			else:
				self.get_alloy_bailance()
			make_metal_stock_entry(self)
		if self.multiple_metal_converter == 1:
			if self.mc_source_table == []:
				frappe.throw(_("Source Item Missing"))
			if self.m_target_qty <= 0 or self.m_target_item is None:
				frappe.throw(_("Target Item or Target Qty Missing"))
			if self.alloy_qty <= 0 or self.alloy is None:
				frappe.throw(_("Alloy Item or Alloy Qty Missing"))
			self.get_alloy_bailance()
			make_multiple_metal_stock_entry(self)

		if self.multiple_metal_converter == 0:
			if self.target_qty <= 0 or self.source_qty <= 0:
				frappe.throw(
					_("Source Qty or Target Qty not allowed Zero to post transaction")
				)

	def before_validate(self):
		update_batch_details(self)

	def validate(self):
		# if not self.batch and self.multiple_metal_converter == 0:
		# 	frappe.throw(_("Batch Missing"))
		# Melting-loss guards + conversion-field clearing MUST run first so the
		# downstream alloy / batch helpers see the cleared state.
		validate_melting_loss(self)
		update_alloy_betch(self)
		update_source_betch(self)
		build_conversion_lanes(self)
		self.set_remarks()

	def set_remarks(self):
		"""Guard Remarks and keep it in step with Percentage.

		Remarks is a Select whose option TEXT carries the document's Percentage, so the
		option list is per-document and cannot live in the DocType JSON. The JSON
		therefore ships no ``options``, which makes frappe skip its own Select check
		(``base_document._validate_selects`` returns early on a falsy ``df.options``) --
		this method is the replacement guard.

		It also RE-RENDERS the stored sentence, so a Percentage edited after the remark
		was picked can never leave a stale number behind, whether the edit came from the
		form, the API or an import.
		"""
		if not self.remarks:
			return

		idx = template_index(self.remarks)
		if idx is None:
			frappe.throw(
				_(
					"Remarks must be chosen from the list. {0} is not a valid remark."
				).format(frappe.bold(self.remarks))
			)

		self.remarks = render_remark_options(
			self.percentage, self.precision("percentage")
		)[idx]

	@frappe.whitelist()
	def get_remark_options(self):
		"""Remark sentences for this document's Percentage -- fills the client dropdown."""
		return render_remark_options(self.percentage, self.precision("percentage"))

	def on_cancel(self):
		# Scoped cascade: cancels only auto-created Process Loss SEs owned by this
		# document; a no-op for conversion-mode documents.
		cancel_melting_loss_stock_entries(self)

	@frappe.whitelist()
	def clear_fields(self):
		for field in self.meta.fields:
			if field.fieldname not in (
				"name",
				"creation",
				"modified",
				"multiple_metal_converter",
				"employee",
				"company",
				"department",
				"manufacturer",
				"date",
				"source_warehouse",
				"target_warehouse",
				# Document-level header fields (Details tab), not mode-specific ones --
				# switching converter mode must not silently drop the operator's remark.
				"percentage",
				"remarks",
			):
				self.set(field.fieldname, None)

	@frappe.whitelist()
	def set_attribute_value(self):
		return frappe.db.get_value(
			"Item Variant Attribute", {"parent": self.source_item}, "attribute_value"
		)

	@frappe.whitelist()
	def get_batch_detail(self):
		bal_qty = ""
		supplier = ""
		customer = ""
		inventory_type = ""
		error = []
		if self.batch:
			bal_qty = get_batch_qty(
				batch_no=self.batch, warehouse=self.source_warehouse
			)
			reference_doctype, reference_name, inventory_type = frappe.get_value(
				"Batch",
				self.batch,
				["reference_doctype", "reference_name", "custom_inventory_type"],
			)
			if not bal_qty:
				error.append("Batch Qty zero")
			if reference_doctype:
				if reference_doctype == "Purchase Receipt":
					supplier = frappe.get_value(
						reference_doctype, reference_name, "supplier"
					)
					# inventory_type = "Regular Stock"
				if reference_doctype == "Stock Entry":
					inventory_type = frappe.get_value(
						reference_doctype, reference_name, "inventory_type"
					)
					if inventory_type == "Customer Goods":
						customer = frappe.get_value(
							"batch", self.batch, "custom_customer"
						)
			if error:
				frappe.throw(", ".join(error))

			return (
				bal_qty or None,
				supplier or None,
				customer or None,
				inventory_type or None,
			)

	@frappe.whitelist()
	def get_child_batch_detail(self, table_item, talble_source_warehouse, table_batch):
		bal_qty = None
		supplier = None
		customer = None
		inventory_type = None
		error = []
		if table_batch:
			bal_qty = get_batch_qty(
				batch_no=table_batch, warehouse=self.source_warehouse
			)
			reference_doctype, reference_name = frappe.get_value(
				"Batch", table_batch, ["reference_doctype", "reference_name"]
			)
			if not bal_qty:
				error.append("Batch Qty zero")
			if reference_doctype:
				if reference_doctype == "Purchase Receipt":
					supplier = frappe.get_value(
						reference_doctype, reference_name, "supplier"
					)
					inventory_type = "Regular Stock"
				if reference_doctype == "Stock Entry":
					inventory_type = frappe.get_value(
						reference_doctype, reference_name, "inventory_type"
					)
					if inventory_type == "Customer Goods":
						customer = frappe.get_value(
							reference_doctype, reference_name, "_customer"
						)
			if error:
				frappe.throw(", ".join(error))
		return (
			bal_qty or None,
			supplier or None,
			customer or None,
			inventory_type or None,
		)

	@frappe.whitelist()
	def get_detail_tab_value(self):
		errors = []
		company = frappe.get_value("Employee", self.employee, "company")
		dpt, branch = frappe.get_value(
			"Employee", self.employee, ["department", "branch"]
		)
		if not dpt:
			errors.append(
				f"Department Messing against <b>{self.employee} Employee Master</b>"
			)
		if company == "Gurukrupa Export Private Limited" and not branch:
			errors.append(
				f"Branch Messing against <b>{self.employee} Employee Master</b>"
			)
		mnf = frappe.get_value("Department", dpt, "manufacturer")
		if not mnf:
			errors.append("Manufacturer Messing against <b>Department Master</b>")
		s_wh = frappe.get_value(
			"Warehouse",
			{"disabled": 0, "department": dpt, "warehouse_type": "Raw Material"},
			"name",
		)
		if not mnf:
			errors.append("Warehouse Missing Warehouse Master Department Not Set")
		if errors:
			frappe.throw("<br>".join(errors))
		if dpt and mnf and s_wh:
			self.department = dpt
			self.branch = branch
			self.manufacturer = mnf
			self.source_warehouse = s_wh
			self.target_warehouse = s_wh

	@frappe.whitelist()
	def calculate_metal_conversion(self):
		source_item_purity = get_metal_purity_percentage(self.source_item)
		target_item_purity = get_metal_purity_percentage(self.target_item)

		if not source_item_purity:
			frappe.throw(
				_(
					"<b>Source Item</b> in Attribute Value doctype <b>Purity Percentage</b> Missing"
				)
			)
		if not target_item_purity:
			frappe.throw(
				_(
					"<b>Target Item</b> in Attribute Value doctype <b>Purity Percentage</b> Missing"
				)
			)

		if source_item_purity:
			if target_item_purity:
				if target_item_purity != 0:
					target_qty = float(
						(self.source_qty * source_item_purity) / target_item_purity
					)
					alloy_qty = round(float((target_qty - self.source_qty)), 3)
				else:
					frappe.throw(_("Error: Target Item Purity value is zero."))
			else:
				frappe.throw(_("Error: Target Item Purity not found."))
		else:
			frappe.throw(_("Error: Source Item Purity not found."))
		return target_qty, alloy_qty

	@frappe.whitelist()
	def calculate_Multiple_conversion(self):
		if not self.m_target_item:
			frappe.throw(_("Target Item Code Missing"))

		target_item_purity = get_metal_purity_percentage(self.m_target_item)
		if not target_item_purity:
			frappe.throw(
				_(
					"<b>Target Item</b> in Attribute Value doctype <b>Purity Percentage</b> Missing"
				)
			)

		sum_total = 0
		sum_source_qty = 0
		inventory_types_source = set()
		for row in self.mc_source_table:
			inventory_types_source.add(row.inventory_type)
			sum_total += row.total
			sum_source_qty += row.qty
		target_qty = round(sum_total / target_item_purity, 3)
		alloy_qty = round(float((target_qty - sum_source_qty)), 3)
		return target_qty, alloy_qty

	@frappe.whitelist()
	def get_alloy_bailance(self):
		if self.multiple_metal_converter == 0:
			_alloy_qty = self.source_alloy_qty or self.target_alloy_qty
			if _alloy_qty:
				_alloy = self.source_alloy or self.target_alloy
				if not _alloy:
					frappe.throw(_("Alloy Missing"))
				alloy_qty_bail = frappe.get_value(
					"Bin",
					{"warehouse": self.source_warehouse, "item_code": _alloy},
					"actual_qty",
				)

				if alloy_qty_bail:
					if flt(_alloy_qty) > alloy_qty_bail:
						frappe.throw(
							f"Alloy <b>{_alloy}</b> Bailance qty is {alloy_qty_bail}</br>We need {_alloy_qty} Respective <b>{self.source_warehouse}</b> Warehouse."
						)
				else:
					frappe.throw(
						f"Alloy <b>{_alloy}</b> Stock Not Available Respective <b>{self.source_warehouse}</b> Warehouse."
					)
		else:
			if self.alloy_qty:
				if not self.alloy:
					frappe.throw(_("Alloy Missing"))
				actual_qty = frappe.get_value(
					"Bin",
					{"warehouse": self.source_warehouse, "item_code": self.alloy},
					"actual_qty",
				)

				if actual_qty:
					if self.alloy_qty > actual_qty:
						frappe.throw(
							f"Alloy <b>{self.alloy}</b> Bailance qty is {actual_qty}</br>We need {self.alloy_qty} Respective <b>{self.source_warehouse}</b> Warehouse."
						)
				else:
					frappe.throw(
						f"Alloy <b>{self.alloy}</b> Stock Not Available Respective <b>{self.source_warehouse}</b> Warehouse."
					)

	@frappe.whitelist()
	def get_mc_table_purity(self, item_code, qty):
		if not item_code:
			frappe.throw(_("Item Code Missing"))

		source_item_purity = get_metal_purity_percentage(item_code)
		if not source_item_purity:
			frappe.throw(
				_(
					"<b>Source Item</b> in Attribute Value doctype <b>Purity Percentage</b> Missing"
				)
			)

		total = qty * source_item_purity
		return total, source_item_purity


def get_metal_purity_percentage(item_code):
	item_variant_attribute_value = frappe.get_all(
		"Item Variant Attribute",
		filters={"parent": item_code, "attribute": "Metal Purity"},
		fields=["parent", "attribute", "attribute_value"],
	)
	if not item_variant_attribute_value:
		frappe.throw(_("Attribute Value Missing"))
	target_purity = float(
		frappe.get_value(
			"Attribute Value",
			item_variant_attribute_value[0].get("attribute_value"),
			"purity_percentage",
		)
	)
	return target_purity


def make_metal_stock_entry(self):
	target_wh = self.target_warehouse
	source_wh = self.source_warehouse
	# Ownership is no longer a document-level property: every row takes its
	# inventory_type / customer from its own lane (see build_conversion_lanes).
	# RULE B (canonical lock order): pre-lock the source/target Bins in sorted order so
	# concurrent metal conversions acquire shared item+warehouse Bins in the same sequence
	# (breaks 1213 reverse-order cycles). Additive — does not change the Stock Entry built.
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_bins,
		preallocate_series_for_docs,
	)

	# RULE A (canonical lock order): pin the Stock Entry naming-series row (canonical
	# position 2) BEFORE the Bins so this flow acquires Series-then-Bin like every
	# conformant SE submit -- fixes the Bin-before-Series inversion behind F-002 1213
	# deadlock cycles. Additive: preallocate_series is SELECT ... FOR UPDATE only (no
	# increment), re-entrant with the real naming at insert. company/stock_entry_type
	# are set so the series prefix (and any future sharded/DNR counter) resolves.
	_series_stub = frappe.new_doc("Stock Entry")
	_series_stub.company = self.company
	_series_stub.stock_entry_type = "Repack-Metal Conversion"
	preallocate_series_for_docs(_series_stub)

	lock_bins(
		[
			(self.source_item, source_wh),
			(self.source_alloy, source_wh),
			(self.target_item, target_wh),
			(self.target_alloy, target_wh),
		]
	)
	lanes = list(self.conversion_lanes or [])
	if not lanes:
		frappe.throw(
			_(
				"No conversion lanes were resolved for this document. Please re-save it and try again."
			)
		)

	precision = self.precision("target_qty") or 3
	weights = [flt(lane.source_qty) for lane in lanes]

	# The alloy totals are apportioned from what is STORED on the document -- not
	# recomputed from the purities -- because those are the figures the operator saw
	# and the figures get_alloy_bailance checked against Bin. Apportioning guarantees
	# the lane shares sum back to the validated total exactly, so no lane can quietly
	# consume alloy that was never confirmed available.
	source_alloy_needs = target_alloy_qtys = [0.0 for _ in lanes]
	source_alloy_rows = [[] for _ in lanes]

	if self.source_alloy and flt(self.source_alloy_qty) > 0:
		source_alloy_needs = apportion(flt(self.source_alloy_qty), weights, precision)
		source_alloy_rows = split_allocations(
			self.alloy_batch_details or [], source_alloy_needs, precision
		)
	if self.target_alloy and flt(self.target_alloy_qty) > 0:
		target_alloy_qtys = apportion(flt(self.target_alloy_qty), weights, precision)

	# The header can only describe an unambiguous voucher. inventory_type is set only
	# for a single-lane draw; _customer opens create_child_batches' gate (which is now
	# row-aware) and is left blank when no lane is customer-owned so that path is
	# skipped entirely for an all-Regular conversion.
	single_lane = lanes[0].inventory_type if len(lanes) == 1 else None
	header_customer = next((lane.customer for lane in lanes if lane.customer), None)

	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Repack-Metal Conversion",
			"purpose": "Repack",
			"company": self.company,
			"custom_metal_conversion_reference": self.name,
			"inventory_type": single_lane,
			"_customer": header_customer,
			"auto_created": 1,
			"branch": self.branch,
		}
	)

	def _common():
		return {
			"department": self.department,
			"employee": self.employee,
			"manufacturer": self.manufacturer,
		}

	# Rows are emitted lane by lane -- each lane's sources immediately followed by its
	# target -- so the voucher reads source/target/source/target and every row carries
	# the lane it belongs to.
	booked_source = [0.0 for _ in lanes]
	booked_target = 0.0
	allocations_by_lane = _allocations_by_lane(self)

	for idx, lane in enumerate(lanes):
		tag = lane_tag(lane.inventory_type, lane.customer)
		lane_inv_type = lane.inventory_type or REGULAR_STOCK
		lane_allocations = allocations_by_lane.get(
			(lane_inv_type, lane.customer or None), []
		)

		for allocation in lane_allocations:
			se.append(
				"items",
				dict(
					_common(),
					item_code=self.source_item,
					qty=flt(allocation["qty"], precision),
					inventory_type=lane_inv_type,
					customer=lane.customer,
					batch_no=allocation["batch"],
					s_warehouse=source_wh,
					custom_conversion_lane=tag,
					use_serial_batch_fields=True,
				),
			)
			booked_source[idx] = flt(
				booked_source[idx] + flt(allocation["qty"], precision), precision
			)

		# Alloy stays "Regular Stock" -- it IS company stock being consumed -- but it is
		# tagged to the lane it funds so its origin entries and Batch Rate contribution
		# can be attributed to the right target batch.
		for allocation in source_alloy_rows[idx]:
			se.append(
				"items",
				dict(
					_common(),
					item_code=self.source_alloy,
					qty=flt(allocation["qty"], precision),
					inventory_type=REGULAR_STOCK,
					batch_no=allocation["batch"],
					s_warehouse=source_wh,
					custom_conversion_lane=tag,
					use_serial_batch_fields=True,
				),
			)

		se.append(
			"items",
			dict(
				_common(),
				item_code=self.target_item,
				qty=flt(lane.target_qty, precision),
				inventory_type=lane_inv_type,
				customer=lane.customer,
				t_warehouse=target_wh,
				custom_conversion_lane=tag,
			),
		)
		booked_target = flt(booked_target + flt(lane.target_qty, precision), precision)

		if flt(target_alloy_qtys[idx], precision) > 0:
			# Alloy freed by raising the purity belongs to the lane whose metal freed
			# it, customer included -- otherwise a customer's alloy would silently
			# become company stock.
			se.append(
				"items",
				dict(
					_common(),
					item_code=self.target_alloy,
					qty=flt(target_alloy_qtys[idx], precision),
					inventory_type=lane_inv_type,
					customer=lane.customer,
					t_warehouse=target_wh,
					custom_conversion_lane=tag,
				),
			)

	# Replaces the old "Inventory types in Source Table are not consistent" throw. That
	# guard existed only because this voucher used to be single-ownership by
	# construction; mixed ownership is now the point. What still must hold is that every
	# lane consumed exactly its own source qty and the lane targets sum to the document
	# target -- i.e. the split neither invented nor dropped metal.
	tolerance = 1.0 / (10 ** (precision + 1))
	for idx, lane in enumerate(lanes):
		if abs(booked_source[idx] - flt(lane.source_qty, precision)) > tolerance:
			frappe.throw(
				_(
					"Lane {0} allocated {1} but booked {2} on the Stock Entry. Please re-save the document."
				).format(
					frappe.bold(lane_tag(lane.inventory_type, lane.customer)),
					flt(lane.source_qty, precision),
					booked_source[idx],
				)
			)
	if abs(booked_target - flt(self.target_qty, precision)) > tolerance:
		frappe.throw(
			_(
				"Target Qty {0} does not match the {1} booked across conversion lanes. Please re-save the document."
			).format(flt(self.target_qty, precision), booked_target)
		)

	se.save()
	se.submit()
	self.stock_entry = se.name


def _allocations_by_lane(self):
	"""``{lane key: [{batch, qty}]}`` for the whole allocation, in FIFO order.

	The lane rows carry their batches as a display string only, so the authoritative
	allocation is re-read from ``source_batch_details`` and grouped with the same
	``get_batch_lane_map`` the lanes were built from -- the two therefore cannot
	disagree. Built once per voucher rather than once per lane, since it costs a
	query.
	"""
	rows = list(self.source_batch_details or [])
	lane_map = get_batch_lane_map([row.batch for row in rows])

	grouped = {}
	for row in rows:
		inventory_type, customer = lane_map.get(row.batch, (REGULAR_STOCK, None))
		key = (inventory_type or REGULAR_STOCK, customer or None)
		grouped.setdefault(key, []).append({"batch": row.batch, "qty": flt(row.qty)})

	return grouped


def make_multiple_metal_stock_entry(self):
	source_wh = self.source_warehouse
	# RULE B (canonical lock order): pre-lock source + target Bins in sorted order so
	# concurrent conversions acquire shared item+warehouse Bins in the same sequence.
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_bins,
		preallocate_series_for_docs,
	)

	# RULE A (canonical lock order): pin the Stock Entry naming-series row BEFORE the
	# Bins (Series-then-Bin) -- fixes the Bin-before-Series inversion behind F-002
	# 1213 cycles. Additive: SELECT ... FOR UPDATE only, re-entrant with insert naming.
	_series_stub = frappe.new_doc("Stock Entry")
	_series_stub.company = self.company
	_series_stub.stock_entry_type = "Repack-Metal Conversion"
	preallocate_series_for_docs(_series_stub)

	_prelock = [(r.item_code, source_wh) for r in self.mc_source_table]
	_prelock.append((self.get("m_target_item"), self.get("target_warehouse")))
	lock_bins(_prelock)
	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Repack-Metal Conversion",
			"purpose": "Repack",
			"company": self.company,
			"custom_metal_conversion_reference": self.name,
			# "inventory_type": inventory_type,
			# "_customer": self.customer,
			"auto_created": 1,
			"branch": self.branch,
		}
	)
	se.branch = self.branch
	inventory_types_source = set()
	source_item = []
	target_item = []
	inventory_wise_data = {}
	for row in self.mc_source_table:
		if inventory_wise_data.get(row.inventory_type):
			inventory_wise_data[row.inventory_type]["qty"] += row.total
		else:
			inventory_wise_data[row.inventory_type] = {
				"customer": row.get("customer"),
				"qty": row.total,
			}
		source_item.append(
			{
				"item_code": row.item_code,
				"qty": row.qty,
				"inventory_type": row.inventory_type or "Regular Stock",
				"batch_no": row.batch,
				"department": self.department,
				"employee": self.employee,
				"manufacturer": self.manufacturer,
				"s_warehouse": source_wh,
			},
		)
		se.inventory_type = row.inventory_type or "Regular Stock"
		inventory_types_source.add(row.inventory_type or "Regular Stock")
	if len(inventory_types_source) > 1:
		frappe.throw(
			_(
				"Inventory types in <b>Source Table</b> are not consistent. Please check."
			)
		)

	for row in inventory_wise_data:
		qty, purity_percentage = self.get_mc_table_purity(
			self.m_target_item, self.m_target_qty
		)
		target_item.append(
			{
				"item_code": self.m_target_item,
				"qty": (inventory_wise_data[row]["qty"] / purity_percentage),
				"inventory_type": row,
				"customer": inventory_wise_data[row].get("customer"),
				"department": self.department,
				"employee": self.employee,
				"manufacturer": self.manufacturer,
				"t_warehouse": source_wh,
			}
		)
	if self.alloy and self.alloy_qty > 0:
		if self.alloy_check == 0:
			source_item.append(
				{
					"item_code": self.alloy,
					"qty": self.alloy_qty,
					"inventory_type": se.inventory_type or "Regular Stock",
					"batch_no": self.alloy_batch,
					"department": self.department,
					"employee": self.employee,
					"manufacturer": self.manufacturer,
					"s_warehouse": source_wh,
				}
			)
		if self.alloy_check == 1:
			target_item.append(
				{
					"item_code": self.alloy,
					"qty": self.alloy_qty,
					"inventory_type": se.inventory_type or "Regular Stock",
					"department": self.department,
					"employee": self.employee,
					"manufacturer": self.manufacturer,
					"t_warehouse": source_wh,
				}
			)
	for row in source_item:
		se.append(
			"items",
			{
				"item_code": row["item_code"],
				"qty": row["qty"],
				"inventory_type": row["inventory_type"] or "Regular Stock",
				"batch_no": row["batch_no"],
				"department": row["department"],
				"employee": row["employee"],
				"manufacturer": row["manufacturer"],
				"s_warehouse": row["s_warehouse"],
				"use_serial_batch_fields": True,
			},
		)
	for row in target_item:
		se.append(
			"items",
			{
				"item_code": row["item_code"],
				"qty": row["qty"],
				"inventory_type": row["inventory_type"] or "Regular Stock",
				"department": row["department"],
				"employee": row["employee"],
				"manufacturer": row["manufacturer"],
				"t_warehouse": row["t_warehouse"],
			},
		)

	se.save()
	se.submit()
	frappe.db.set_value(self.doctype, self.name, "stock_entry", se.name)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_filtered_batches(doctype, txt, searchfield, start, page_len, filters):
	data = get_batch_no(doctype, txt, searchfield, start, page_len, filters)
	return data


def get_batch_details(batch):
	batch_details = frappe.get_doc("Batch", batch)
	return batch_details


def lane_tag(inventory_type, customer):
	"""The value stamped onto ``Stock Entry Detail.custom_conversion_lane``.

	The lane a row belongs to cannot be re-derived downstream for every row: alloy
	consume rows are booked "Regular Stock" yet legitimately fund a customer lane,
	and no per-lane alloy proportion is stored anywhere else. So the builder writes
	the lane explicitly and ``create_child_batches`` /
	``update_parent_batch_id`` key off it.
	"""
	return f"{inventory_type or REGULAR_STOCK}|{customer or ''}"


def build_conversion_lanes(self):
	"""Split the FIFO allocation into ownership lanes and publish them on the doc.

	Replaces the old ``get_inventory_type``, which derived ONE ``inventory_type``
	for the whole document from whichever batch happened to be allocated first.
	That only worked while the allocator was restricted to a single ownership; now
	a 20 g draw can legitimately span 8 g Regular + 12 g Customer Goods, and each
	ownership has to convert and land separately.

	``self.inventory_type`` and ``self.customer`` survive as read-only *summaries*:
	they are filled only when the draw is unambiguous (a single lane / a single
	customer) and left blank otherwise, with ``conversion_lanes`` carrying the full
	picture. Both are still read by the gke reports.
	"""
	if self.get("multiple_metal_converter"):
		# Multiple-converter mode has its own per-row table (mc_source_table) and is
		# still single-ownership by construction; it does not use lanes.
		return

	precision = self.precision("target_qty") or 3
	batch_nos = [
		row.get("batch") if hasattr(row, "get") else getattr(row, "batch", None)
		for row in (self.source_batch_details or [])
	]
	lanes = build_lanes(self.source_batch_details or [], get_batch_lane_map(batch_nos))

	# In melting-loss mode no conversion Stock Entry is built, so there is nothing
	# to apportion -- the lanes are informational only (and the allocator has already
	# restricted them to Regular Stock).
	if not self.get("is_melting_loss"):
		split_conversion(lanes, flt(self.target_qty), precision)

	self.conversion_lanes = []
	for lane in lanes:
		self.append(
			"conversion_lanes",
			{
				"inventory_type": lane["inventory_type"],
				"customer": lane["customer"],
				"source_qty": lane["source_qty"],
				"target_qty": lane.get("target_qty") or 0.0,
				"alloy_qty": lane.get("alloy_qty") or 0.0,
				"batches": ", ".join(
					f"{b['batch']}:{flt(b['qty'], precision)}" for b in lane["batches"]
				),
			},
		)

	self.inventory_type = lanes[0]["inventory_type"] if len(lanes) == 1 else None

	customers = {lane["customer"] for lane in lanes if lane["customer"]}
	self.customer = customers.pop() if len(customers) == 1 else None
