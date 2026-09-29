# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt

import hashlib
import re

import frappe
from erpnext.controllers.queries import get_batch_no
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, nowtime

from jewellery_erpnext.customer_subcontracting.customer_gold_components import (
	get_company_component_qty,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock.batch_valuation_ledger import (
	capped_auto_batch_nos,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	CUSTOMER_INVENTORY_TYPES,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events.lanes import (
	REGULAR_STOCK,
	build_lanes,
	lane_key,
	split_allocations,
	split_conversion,
	split_with_alloy,
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

#: The Stock Entry Type every conversion voucher is booked under.
CONVERSION_SE_TYPE = "Repack-Metal Conversion"

#: ``Stock Entry Detail.custom_conversion_lane`` is a Data field.
LANE_TAG_LENGTH = 140

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
			self.validate_target_qty()
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
		_stamp_source_ownership(self)
		if self.docstatus == 0:
			# Only a submit writes this link, so a draft keeps none. (An amendment copies it --
			# Amend copies no-copy fields -- but Frappe checks links before this hook runs, so
			# the form drops that one on load: metal_conversions.js.)
			self.stock_entry = None

	def validate(self):
		# if not self.batch and self.multiple_metal_converter == 0:
		# 	frappe.throw(_("Batch Missing"))
		# Melting-loss guards + conversion-field clearing MUST run first so the
		# downstream alloy / batch helpers see the cleared state.
		validate_melting_loss(self)
		update_alloy_betch(self)
		update_source_betch(self)
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
		# Scoped cascade: the auto-created entries this document owns -- its Process Loss
		# (melting-loss mode) and its Repack-Metal Conversion -- are cancelled through the
		# standard controller, so a conversion whose output was already used cannot be
		# cancelled at all rather than half-cancelled.
		cancel_melting_loss_stock_entries(self)
		cancel_conversion_stock_entries(self)

	def validate_target_qty(self):
		"""Target Qty must be what the purity masters make of Source Qty, to one posting unit.

		The form computes it (``calculate_metal_conversion``) and clears it whenever the
		source changes, but the server never checked it, so an API caller -- or a draft
		left stale by a purity-master edit -- could post a target the source metal cannot
		produce. The builder then books Source Qty plus the stored alloy, and requires that
		to agree with this figure too (``_check_target_total``).
		"""
		if not (
			flt(self.source_qty)
			and flt(self.target_qty)
			and self.source_item
			and self.target_item
		):
			# Nothing to compare: the builder's own messages (no batches allocated, a zero
			# quantity) say what is missing.
			return
		precision = _qty_precision()
		expected, _alloy = self.calculate_metal_conversion()
		# Raw figures, one unit at posting precision apart at most: the form stores the target
		# unrounded, and rounding both sides here -- by a different method than the form's
		# round() -- refused documents whose figures sat on either side of a tie.
		if abs(flt(self.target_qty) - flt(expected)) > _qty_unit(precision):
			frappe.throw(
				_(
					"Target Qty {0} is not what {1} of {2} converts to at the configured purities ({3}). "
					"Select the Target Item again to recalculate."
				).format(
					flt(self.target_qty, precision),
					flt(self.source_qty, precision),
					self.source_item,
					flt(expected, precision),
				)
			)

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
		"""Balance and supplier for a hand-picked batch.

		Ownership is deliberately NOT returned: a conversion draws FIFO across
		whatever ownerships the warehouse holds, so a single batch cannot describe
		it. The Batch itself is the source of truth for who owns what, and the
		Stock Entry records what was booked.
		"""
		bal_qty = ""
		supplier = ""
		error = []
		if self.batch:
			bal_qty = get_batch_qty(
				batch_no=self.batch, warehouse=self.source_warehouse
			)
			reference_doctype, reference_name = frappe.get_value(
				"Batch", self.batch, ["reference_doctype", "reference_name"]
			)
			if not bal_qty:
				error.append("Batch Qty zero")
			if reference_doctype == "Purchase Receipt":
				supplier = frappe.get_value(
					reference_doctype, reference_name, "supplier"
				)
			if error:
				frappe.throw(", ".join(error))

			return (bal_qty or None, supplier or None)

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
	precision = _qty_precision()

	# The ownership split is derived here, from what the document already holds: the
	# FIFO allocation in source_batch_details plus each batch's own ownership on
	# tabBatch. Nothing about the lanes is stored -- the Batch stays the single source
	# of truth for who owns what, and the Stock Entry below records what was booked.
	# Every customer batch is a lane of its own (lanes.lane_key); company stock pools.
	lanes = build_lanes(
		self.source_batch_details or [],
		get_batch_lane_map([row.batch for row in (self.source_batch_details or [])]),
	)
	if not lanes:
		frappe.throw(
			_(
				"No source batches are allocated for this document. Please re-save it and try again."
			)
		)
	_check_lane_owners(lanes)

	# The alloy the operator confirmed (and the Bin check and update_alloy_betch sized) is
	# the stored figure, so it is shared out, not re-derived: each lane takes its part in
	# proportion to its source (one source item, one purity), and its target is exactly
	# its source plus that part. The target total therefore follows the stored alloy and
	# must agree with Target Qty to within one unit -- the most two correct figures,
	# rounded by different methods, can differ. Without an alloy item the targets are
	# shared out as before.
	consume = bool(self.source_alloy and flt(self.source_alloy_qty) > 0)
	release = bool(self.target_alloy and flt(self.target_alloy_qty) > 0)
	if consume and release:
		frappe.throw(
			_(
				"Source Alloy and Target Alloy are both set. Select the Target Item again to recalculate the conversion."
			)
		)
	source_qty = flt(sum(lane["source_qty"] for lane in lanes), precision)
	if consume or release:
		alloy_total = flt(
			self.source_alloy_qty if consume else self.target_alloy_qty, precision
		)
		_check_target_total(
			self.target_qty,
			flt(source_qty + (alloy_total if consume else -alloy_total), precision),
			precision,
		)
		split_with_alloy(
			lanes,
			alloy_total,
			[lane["source_qty"] for lane in lanes],
			precision,
			release=release,
		)
	else:
		split_conversion(lanes, flt(self.target_qty), precision)

	source_alloy_needs = [max(lane["alloy_qty"], 0.0) for lane in lanes]
	target_alloy_qtys = [
		max(-lane["alloy_qty"], 0.0) if release else 0.0 for lane in lanes
	]
	source_alloy_rows = [[] for _ in lanes]
	if consume:
		source_alloy_rows = split_allocations(
			self.alloy_batch_details or [], source_alloy_needs, precision
		)
		_check_alloy_covered(
			self.source_alloy, source_alloy_rows, source_alloy_needs, precision
		)

	se = frappe.get_doc(_conversion_header(self, lanes))
	common = {
		"department": self.department,
		"employee": self.employee,
		"manufacturer": self.manufacturer,
	}

	# Rows are emitted lane by lane -- each lane's sources, its alloy, then its target --
	# so every lane is one contiguous run: the lane pricer (loss_valuation) values each
	# run from its own consumed rows, and every row carries the lane it belongs to.
	booked_target = 0.0

	for idx, lane in enumerate(lanes):
		tag = lane_tag(lane["inventory_type"], lane["customer"], lane.get("batch"))
		lane_inv_type = lane["inventory_type"] or REGULAR_STOCK
		booked_target = flt(
			booked_target
			+ _append_lane_rows(
				se,
				lane,
				tag,
				common,
				source_item=self.source_item,
				source_wh=source_wh,
				alloy_item=self.source_alloy,
				alloy_rows=source_alloy_rows[idx],
				target_item=self.target_item,
				target_wh=target_wh,
				precision=precision,
			),
			precision,
		)

		released_alloy = flt(target_alloy_qtys[idx], precision)
		if released_alloy > 0:
			# Alloy freed by raising the purity belongs to the lane whose metal freed
			# it, customer included -- otherwise a customer's alloy would silently
			# become company stock.
			#
			# C09 CARVE-OUT. That rule is right for metal the customer supplied, and
			# wrong for the part of the melt the COMPANY supplied. When company alloy
			# was blended into a customer lane on an earlier conversion, raising the
			# purity again frees some of that same company alloy -- and handing all of
			# it back tagged to the customer converts company stock into customer
			# stock with no transaction and no counterparty.
			#
			# The split is taken from RECORDED components (customer_gold_components),
			# never re-derived from the source batches' current tags, which are mutable.
			# ``get_company_component_qty`` returns 0.0 for a batch with no recorded
			# components, so on a site with no component history this branch emits
			# exactly the single row it always did -- which is why shipping it changes
			# no existing behaviour and no existing test.
			company_alloy = 0.0
			if lane["customer"]:
				company_alloy = min(
					released_alloy,
					flt(
						get_company_component_qty(
							[allocation["batch"] for allocation in lane["batches"]]
						),
						precision,
					),
				)

			customer_alloy = flt(released_alloy - company_alloy, precision)

			if customer_alloy > 0:
				se.append(
					"items",
					dict(
						common,
						item_code=self.target_alloy,
						qty=customer_alloy,
						inventory_type=lane_inv_type,
						customer=lane["customer"],
						t_warehouse=target_wh,
						custom_conversion_lane=tag,
					),
				)

			if company_alloy > 0:
				# Same lane tag on purpose: it funded this lane and its Batch Rate
				# contribution still belongs to this lane's target batch. Only the
				# OWNERSHIP differs.
				se.append(
					"items",
					dict(
						common,
						item_code=self.target_alloy,
						qty=company_alloy,
						inventory_type=REGULAR_STOCK,
						customer=None,
						t_warehouse=target_wh,
						custom_conversion_lane=tag,
					),
				)

	# Replaces the old "Inventory types in Source Table are not consistent" throw. That
	# guard existed only because this voucher used to be single-ownership by
	# construction; mixed ownership is now the point. What must still hold is that the
	# lane targets sum back to the document's Target Qty -- i.e. apportioning across
	# lanes neither invented nor dropped metal. (Per-lane source qty needs no check: the
	# consume rows are emitted straight from the lane's own allocation.)
	if abs(booked_target - flt(self.target_qty)) > _qty_unit(precision):
		frappe.throw(
			_(
				"Target Qty {0} does not match the {1} booked across conversion lanes. Please re-save the document."
			).format(flt(self.target_qty, precision), booked_target)
		)

	se.save()
	se.submit()
	# db_set, not an attribute: on_submit runs after the document row is written, so a
	# plain assignment was never saved and every single-mode conversion lost this link.
	self.db_set("stock_entry", se.name)


def _conversion_header(doc, lanes):
	"""The Stock Entry header for a conversion voucher built from ``lanes``.

	The header can only describe an unambiguous voucher. ``inventory_type`` is set only
	when every lane has ONE ownership -- a customer's two batches are two lanes but still
	one owner. ``_customer`` opens create_child_batches' gate (which is row-aware) and is
	left blank when no lane is customer-owned, so that path is skipped entirely for an
	all-Regular conversion.
	"""
	ownerships = {(lane["inventory_type"], lane["customer"]) for lane in lanes}
	return {
		"doctype": "Stock Entry",
		"stock_entry_type": CONVERSION_SE_TYPE,
		"purpose": "Repack",
		"company": doc.company,
		"custom_metal_conversion_reference": doc.name,
		"inventory_type": lanes[0]["inventory_type"] if len(ownerships) == 1 else None,
		"_customer": next(
			(lane["customer"] for lane in lanes if lane["customer"]), None
		),
		"auto_created": 1,
		"branch": doc.branch,
	}


def _append_lane_rows(
	se,
	lane,
	tag,
	common,
	*,
	source_item,
	source_wh,
	alloy_item,
	alloy_rows,
	target_item,
	target_wh,
	precision,
):
	"""Append one lane's rows -- its sources, the alloy it consumes, its target -- and
	return the target qty booked.

	Every row carries ``tag``: provenance (``update_parent_batch_id``), child-batch naming
	(``create_child_batches``) and the Customer Gold ledger all read the lane from it, and
	the rows stay contiguous so the lane pricer values the lane on its own.
	"""
	lane_inv_type = lane["inventory_type"] or REGULAR_STOCK

	for allocation in lane["batches"]:
		se.append(
			"items",
			dict(
				common,
				item_code=allocation.get("item_code") or source_item,
				qty=flt(allocation["qty"], precision),
				inventory_type=lane_inv_type,
				customer=lane["customer"],
				batch_no=allocation["batch"],
				s_warehouse=source_wh,
				custom_conversion_lane=tag,
				use_serial_batch_fields=True,
			),
		)

	# Alloy stays "Regular Stock" -- it IS company stock being consumed -- but it is
	# tagged to the lane it funds so its origin entries and Batch Rate contribution
	# can be attributed to the right target batch.
	for allocation in alloy_rows:
		se.append(
			"items",
			dict(
				common,
				item_code=alloy_item,
				qty=flt(allocation["qty"], precision),
				inventory_type=REGULAR_STOCK,
				batch_no=allocation["batch"],
				s_warehouse=source_wh,
				custom_conversion_lane=tag,
				use_serial_batch_fields=True,
			),
		)

	target_qty = flt(lane["target_qty"], precision)
	se.append(
		"items",
		dict(
			common,
			item_code=target_item,
			qty=target_qty,
			inventory_type=lane_inv_type,
			customer=lane["customer"],
			t_warehouse=target_wh,
			custom_conversion_lane=tag,
		),
	)
	return target_qty


def _check_lane_owners(lanes):
	"""A customer-owned lane must name its customer.

	Its target batch is minted for that customer. With none, ``create_child_batches``
	falls back to the voucher's first customer and would hand this batch's metal to
	another owner, so the conversion is refused rather than guessed: the batch master
	needs its customer first.
	"""
	for lane in lanes:
		if lane["inventory_type"] in CUSTOMER_INVENTORY_TYPES and not lane["customer"]:
			frappe.throw(
				_(
					"Batch {0} is {1} but names no customer, so its converted metal would "
					"have no owner. Set the customer on the batch, then convert it."
				).format(frappe.bold(lane.get("batch") or ""), lane["inventory_type"])
			)


def _qty_precision():
	"""Decimals a conversion posts its quantities at: the Stock Entry row's ``transfer_qty``.

	The builders used the conversion's own float precision, which follows System Settings: at
	2 a 0.477 g source row became 0.48 g and overdrew its batch, while the row itself posts at
	``transfer_qty``'s 3 decimals on every site.
	"""
	return cint(frappe.get_precision("Stock Entry Detail", "transfer_qty")) or 3


def _qty_unit(precision):
	"""One unit at ``precision``, plus float slack: the most two correct figures -- the form's
	round() and the server's Banker's flt() -- can differ by on a rounding tie."""
	return 1.0 / (10**precision) + 1e-9


def _check_target_total(target_qty, booked_total, precision):
	"""The targets the stored alloy makes (source +/- alloy) must be Target Qty, to one unit.

	A larger gap means the stored alloy and the target disagree -- a stale draft or an edited
	figure -- and posting it would create or lose metal.
	"""
	if abs(flt(target_qty) - flt(booked_total)) > _qty_unit(precision):
		frappe.throw(
			_(
				"Target Qty {0} does not match the {1} that the source quantity and the stored alloy "
				"quantity make. Recalculate the conversion and try again."
			).format(flt(target_qty, precision), flt(booked_total, precision))
		)


def _check_alloy_covered(alloy_item, rows, needs, precision):
	"""Every lane's alloy must come from an allocated batch -- none may be booked short."""
	handed = flt(
		sum(flt(row["qty"]) for lane_rows in rows for row in lane_rows), precision
	)
	needed = flt(sum(needs), precision)
	if abs(handed - needed) > _qty_unit(precision):
		frappe.throw(
			_(
				"Alloy {0}: the allocated batches cover {1} but this conversion needs {2}. "
				"Please re-save the document."
			).format(alloy_item, handed, needed)
		)


def make_multiple_metal_stock_entry(self):
	lane_map = get_batch_lane_map([row.batch for row in self.mc_source_table])
	if _has_customer_owned_source(self.mc_source_table, lane_map):
		# The legacy build below groups by inventory type alone and appends its targets
		# without a customer, so customer metal came out ownerless and merged. A document
		# with no customer metal keeps exactly that build.
		return make_multiple_customer_metal_stock_entry(self, lane_map)

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


def _has_customer_owned_source(rows, lane_map):
	"""True when any source row is customer metal -- by its batch, which is the physical truth
	(``row_ownership`` rule 1); a row without a batch is judged by its own type."""
	for row in rows:
		inventory_type = (
			lane_map.get(row.batch, (REGULAR_STOCK, None))[0]
			if row.get("batch")
			else row.get("inventory_type")
		)
		if inventory_type in CUSTOMER_INVENTORY_TYPES:
			return True
	return False


def _stamp_source_ownership(doc):
	"""Show each MC Source Table row the ownership of the batch it names.

	The batch wins over the row (``row_ownership`` rule 1). The form fills a row's read-only
	type from the header of the entry that minted the batch, which is blank for a mixed-
	ownership conversion, so a customer's converted batch arrived typed "Regular Stock" and
	its owner could neither see nor correct it. A customer type names its customer; company
	stock names none (rule 2).
	"""
	rows = [row for row in (doc.get("mc_source_table") or []) if row.get("batch")]
	if not rows:
		return
	lane_map = get_batch_lane_map([row.batch for row in rows])
	for row in rows:
		inventory_type, customer = lane_map.get(row.batch, (REGULAR_STOCK, None))
		row.inventory_type = inventory_type
		row.customer = customer if inventory_type in CUSTOMER_INVENTORY_TYPES else None


def make_multiple_customer_metal_stock_entry(self, lane_map):
	"""Multiple-converter mode with customer metal: each customer batch converts on its own.

	Exactly as in single mode, every customer batch is a lane of its own and Regular
	Stock rows pool; ownership is the batch's. Rows of different purities are the point of
	this mode, so each lane NEEDS alloy in proportion to its own fine gold (fine / target
	purity - source): the stored Alloy Qty is shared by those needs, and each lane's target
	is its source plus its share -- a lane already at the target purity needs none and gets
	none. A lane that would need alloy taken OUT cannot be converted here: netting it
	against another lane's addition would move metal between owners, so it is refused.
	"""
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_bins,
		preallocate_series_for_docs,
	)

	source_wh = self.source_warehouse
	target_wh = self.get("target_warehouse") or source_wh
	precision = _qty_precision()

	# Same canonical lock order as single mode: the naming-series row, then the Bins --
	# the alloy's included, since its batches are drawn here.
	_series_stub = frappe.new_doc("Stock Entry")
	_series_stub.company = self.company
	_series_stub.stock_entry_type = CONVERSION_SE_TYPE
	preallocate_series_for_docs(_series_stub)
	lock_bins(
		[(row.item_code, source_wh) for row in self.mc_source_table]
		+ [(self.m_target_item, target_wh), (self.alloy, source_wh)]
	)

	target_purity = get_metal_purity_percentage(self.m_target_item)
	if not target_purity:
		frappe.throw(_("Error: Target Item Purity value is zero."))

	lanes = {}
	order = []
	for row in self.mc_source_table:
		if not row.batch:
			frappe.throw(
				_("Row {0}: select the batch to convert for {1}.").format(
					row.idx, row.item_code
				)
			)
		inventory_type, customer = lane_map.get(row.batch, (REGULAR_STOCK, None))

		key = lane_key(inventory_type, customer, row.batch)
		lane = lanes.get(key)
		if lane is None:
			lane = lanes[key] = {
				"inventory_type": key[0],
				"customer": key[1],
				"batch": key[2],
				"source_qty": 0.0,
				"fine": 0.0,
				"batches": [],
			}
			order.append(key)

		qty = flt(row.qty, precision)
		lane["source_qty"] = flt(lane["source_qty"] + qty, precision)
		lane["fine"] += qty * get_metal_purity_percentage(row.item_code)
		lane["batches"].append(
			{"batch": row.batch, "qty": qty, "item_code": row.item_code}
		)

	lanes = [lanes[key] for key in order]
	if not lanes:
		frappe.throw(_("Source Item Missing"))
	_check_lane_owners(lanes)

	# The server's own figure, not the client's ``total`` column: the target is what the
	# purity masters make of the source rows. Raw figures, one unit apart at most: the form
	# stores its round(..., 3), and rounding both sides again refused ties.
	expected = sum(lane["fine"] for lane in lanes) / target_purity
	if abs(flt(self.m_target_qty) - flt(expected)) > _qty_unit(precision):
		frappe.throw(
			_(
				"Target Qty {0} is not what the source rows convert to at the configured "
				"purities ({1}). Press Calculate again."
			).format(flt(self.m_target_qty, precision), flt(expected, precision))
		)

	needs = []
	for lane in lanes:
		need = lane["fine"] / target_purity - lane["source_qty"]
		if need < -1e-9:
			frappe.throw(
				_(
					"{0} would need alloy taken out to reach {1}, which the Multiple Metal "
					"Converter cannot do without mixing it with other owners' metal. "
					"Convert it on its own."
				).format(
					_("Batch {0}").format(frappe.bold(lane["batch"]))
					if lane["batch"]
					else _("The Regular Stock rows"),
					self.m_target_item,
				)
			)
		needs.append(max(need, 0.0))

	source_qty = flt(sum(lane["source_qty"] for lane in lanes), precision)
	alloy_total = flt(self.alloy_qty, precision) if sum(needs) > 1e-9 else 0.0
	if alloy_total and self.alloy_check:
		frappe.throw(
			_(
				"This conversion needs alloy added, but Alloy Check says alloy comes out. "
				"Press Calculate again."
			)
		)
	_check_target_total(
		self.m_target_qty, flt(source_qty + alloy_total, precision), precision
	)
	split_with_alloy(lanes, alloy_total, needs, precision)

	shares = [max(lane["alloy_qty"], 0.0) for lane in lanes]
	alloy_rows = [[] for _ in lanes]
	if alloy_total:
		alloy_rows = split_allocations(
			_alloy_pool(self, alloy_total), shares, precision
		)
		_check_alloy_covered(self.alloy, alloy_rows, shares, precision)

	se = frappe.get_doc(_conversion_header(self, lanes))
	common = {
		"department": self.department,
		"employee": self.employee,
		"manufacturer": self.manufacturer,
	}
	booked_target = 0.0
	for lane, lane_alloy_rows in zip(lanes, alloy_rows):
		booked_target = flt(
			booked_target
			+ _append_lane_rows(
				se,
				lane,
				lane_tag(lane["inventory_type"], lane["customer"], lane["batch"]),
				common,
				source_item=None,
				source_wh=source_wh,
				alloy_item=self.alloy,
				alloy_rows=lane_alloy_rows,
				target_item=self.m_target_item,
				target_wh=target_wh,
				precision=precision,
			),
			precision,
		)

	if abs(booked_target - flt(self.m_target_qty)) > _qty_unit(precision):
		frappe.throw(
			_(
				"Target Qty {0} does not match the {1} booked across conversion lanes. Please re-save the document."
			).format(flt(self.m_target_qty, precision), booked_target)
		)

	se.save()
	se.submit()
	frappe.db.set_value(self.doctype, self.name, "stock_entry", se.name)


def _alloy_pool(self, qty):
	"""The alloy batches this conversion draws: the picked batch, else FIFO."""
	if self.get("alloy_batch"):
		return [{"batch": self.alloy_batch, "qty": qty}]
	return [
		{"batch": row.batch_no, "qty": flt(row.qty)}
		for row in capped_auto_batch_nos(
			frappe._dict(
				{
					"posting_date": self.get("posting_date") or self.get("date"),
					"posting_time": self.get("posting_time") or nowtime(),
					"item_code": self.alloy,
					"warehouse": self.source_warehouse,
					"qty": qty,
				}
			)
		)
		or []
		if row.batch_no
	]


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_filtered_batches(doctype, txt, searchfield, start, page_len, filters):
	data = get_batch_no(doctype, txt, searchfield, start, page_len, filters)
	return data


def get_batch_details(batch):
	batch_details = frappe.get_doc("Batch", batch)
	return batch_details


def lane_tag(inventory_type, customer, batch=None):
	"""The value stamped onto ``Stock Entry Detail.custom_conversion_lane``.

	The lane a row belongs to cannot be re-derived downstream for every row: alloy
	consume rows are booked "Regular Stock" yet legitimately fund a customer lane,
	and no per-lane alloy proportion is stored anywhere else. So the builder writes
	the lane explicitly and ``create_child_batches`` /
	``update_parent_batch_id`` key off it.

	A customer batch is a lane of its own (``lanes.lane_key``), so its tag names the
	batch: ``"Customer Goods|<customer>|<batch>"``. Company stock keeps
	``"<inventory type>|"``. Every reader compares tags within one voucher and none
	parses them; a tag too long for the Data field names the batch by a stable digest.
	"""
	tag = f"{inventory_type or REGULAR_STOCK}|{customer or ''}"
	if not batch:
		return tag

	tagged = f"{tag}|{batch}"
	if len(tagged) <= LANE_TAG_LENGTH:
		return tagged
	digest = hashlib.sha1(batch.encode()).hexdigest()[:12]
	return f"{tag}|#{digest}"[:LANE_TAG_LENGTH]


def cancel_conversion_stock_entries(doc):
	"""Cancel the conversion Stock Entry this document generated, through its controller.

	Found by the reverse link every generated entry carries
	(``custom_metal_conversion_reference``), so conversions submitted before the forward
	``stock_entry`` link was saved are covered too. ERPNext refuses the cancel when an
	output batch has already been used, and that refusal blocks this cancellation as a
	whole: a conversion is never left half-cancelled.
	"""
	for se_name in frappe.db.get_all(
		"Stock Entry",
		{
			"custom_metal_conversion_reference": doc.name,
			"stock_entry_type": CONVERSION_SE_TYPE,
			"auto_created": 1,
			"docstatus": 1,
		},
		pluck="name",
	):
		frappe.get_doc("Stock Entry", se_name).cancel()
