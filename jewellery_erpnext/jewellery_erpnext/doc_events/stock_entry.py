import copy
import itertools
import json
from datetime import datetime

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	get_available_qty_to_reserve,
	get_sre_reserved_qty_for_voucher_detail_no,
)
from frappe import _, scrub
from frappe.model.mapper import get_mapped_doc
from frappe.query_builder.functions import Sum
from frappe.utils import cint, flt

# from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.se_utils import (
# 	create_repack_for_subcontracting,
# )
from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	validate_loss_ownership_carried,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	create_mop_log_for_stock_transfer_to_mo as create_mop_log,
)
from jewellery_erpnext.jewellery_erpnext.lock_order import (
	lock_bins_for_rows,
	lock_items,
	preallocate_series_for_docs,
	sorted_stock_rows,
)
from jewellery_erpnext.utils import (
	bulk_map,
	get_item_from_attribute,
	get_variant_of_item,
	group_aggregate_with_concat,
)

MANUFACTURER = frappe.defaults.get_user_default("manufacturer")


def before_validate(self, method):
	validate_ir(self)
	if self.docstatus == 0:
		# FIFO batch allocation now runs automatically for every draft (incl.
		# brand-new / unsaved docs) — this replaces the old "Get FIFO Batches"
		# button. update_batches() is internally guarded (not self.auto_created)
		# and idempotent (rows whose batch_no is still valid are kept as-is), and
		# only db_update()s when the SE already exists, so it is safe on unsaved
		# inserts: get_fifo_batches' row.db_set is a no-op in-memory set while
		# self.name is None, and the rebuilt items persist on insert.
		# flags.throw_batch_error still controls throw-vs-msgprint inside
		# get_fifo_batches.
		self.update_batches()

	pure_item_purity = None

	dir_staus_data = frappe._dict()

	# has_batch_no is read once per row below; prefetch it for the (post-update_batches)
	# item table so this is one query instead of O(rows). Its only reader sits behind
	# ``not self.auto_created``, so skip the query entirely on auto-created SEs.
	has_batch_map = {}
	if not self.auto_created:
		has_batch_map = bulk_map(
			"Item", [row.item_code for row in self.items], ["has_batch_no"]
		)

	for row in self.items:
		if (
			not self.auto_created
			and row.s_warehouse
			and not row.batch_no
			and not row.serial_no
			and (has_batch_map.get(row.item_code) or {}).get("has_batch_no")
		):
			# Allocation (update_batches) already ran above; a batch-tracked source
			# row still without a batch means there is genuinely no stock to draw
			# from. Non-batch-tracked source items are exempt.
			frappe.throw(
				_("No stock available to allocate a batch for {0} in {1}").format(
					row.item_code, row.s_warehouse
				)
			)

		if not self.auto_created and row.manufacturing_operation:
			if not dir_staus_data.get(row.manufacturing_operation):
				dir_staus_data[row.manufacturing_operation] = frappe.db.get_value(
					"Manufacturing Operation",
					row.manufacturing_operation,
					"department_ir_status",
				)
			if dir_staus_data[row.manufacturing_operation] == "In-Transit":
				frappe.throw(
					_("Stock Entry not allowed for {0} in between transit").format(
						row.manufacturing_operation
					)
				)
		if row.custom_variant_of in ["M", "F"] and self.stock_entry_type not in [
			"Customer Goods Transfer",
			"Customer Goods Issue",
			"Customer Goods Received",
		]:
			if not pure_item_purity:
				if self.stock_entry_type == "Material Transfer":
					manufacturer = None
					if self.to_main_slip:
						manufacturer = frappe.db.get_value(
							"Main Slip", self.to_main_slip, "manufacturer"
						)
					if self.main_slip:
						manufacturer = frappe.db.get_value(
							"Main Slip", self.main_slip, "manufacturer"
						)
					# Fallback so a Material Transfer SE with no Main Slip link (e.g. the Tree
					# Number Issue button) resolves a manufacturer instead of raising
					# UnboundLocalError.
					if not manufacturer:
						manufacturer = self.get("manufacturer") or MANUFACTURER
				elif self.manufacturing_order:
					manufacturer = frappe.db.get_value(
						"Parent Manufacturing Order",
						self.manufacturing_order,
						"manufacturer",
					)
				else:
					# Stock Entry.manufacturer is a custom field (gke_customization ships it
					# in fixtures/custom_field.json) and is therefore NOT guaranteed to be
					# installed: gk.site currently has no such Custom Field and no column,
					# only the Stock Entry Detail one. It is also set as a plain in-memory
					# attribute by callers that know it without persisting it (Refining
					# Entry carries its own onto the SEs it builds). Read it with .get() so
					# a Stock Entry that lacks the field falls back to the session default
					# instead of raising AttributeError mid-save.
					manufacturer = self.get("manufacturer") or MANUFACTURER

				pure_item = frappe.db.get_value(
					"Manufacturing Setting",
					{"manufacturer": manufacturer},
					"pure_gold_item",
				)

				# Manufacturing Setting is keyed by manufacturer, but the live records are
				# per-COMPANY (one row each, named after the company). A manufacturer that is
				# actually set -- on the document, or as a session default -- therefore
				# matches nothing, and this used to abort the save with a message telling the
				# user to set the very thing that broke it. Fall back to the company's own
				# setting, which is what the company-keyed lookup above did before it was
				# commented out.
				#
				# The fallback never GUESSES between manufacturers: it takes the company-wide
				# record (no manufacturer on it) when there is one, else the company's single
				# record. A company that genuinely keeps one setting per manufacturer keeps
				# throwing, because picking an arbitrary sibling there would silently cost the
				# metal off another manufacturer's pure_gold_item.
				if not pure_item and self.company:
					settings = frappe.get_all(
						"Manufacturing Setting",
						filters={"company": self.company},
						fields=["name", "manufacturer", "pure_gold_item"],
						order_by="name",
					)

					if len(settings) == 1:
						pure_item = settings[0].pure_gold_item
					else:
						company_wide = [s for s in settings if not s.manufacturer]
						if len(company_wide) == 1:
							pure_item = company_wide[0].pure_gold_item

				if not pure_item:
					frappe.throw(
						_(
							"Set Pure Gold Item in the Manufacturing Setting for manufacturer {0} or company {1}"
						).format(
							frappe.bold(manufacturer or _("(not set)")),
							frappe.bold(self.company),
						)
					)

				pure_item_purity = get_purity_percentage(pure_item)

			# get_purity_percentage is already @frappe.request_cache'd, so repeated
			# item codes across rows resolve from that cache — no local memo needed.
			item_purity = get_purity_percentage(row.item_code)

			if not item_purity:
				continue

			if pure_item_purity == item_purity:
				row.custom_pure_qty = row.qty

			else:
				row.custom_pure_qty = flt((item_purity * row.qty) / pure_item_purity, 3)

		# set default inventory type as regular stock for material receipt
		if (
			self.stock_entry_type == "Material Receipt"
			and not row.inventory_type
			and not row.batch_no
		):
			row.inventory_type = "Regular Stock"

	# Must run BEFORE the blanket default below, which is what erases the evidence.
	validate_loss_ownership_carried(self)

	# Ensure all items have a valid inventory_type to prevent None in Stock Ledger Entry
	for row in self.items:
		if not row.inventory_type:
			row.inventory_type = "Regular Stock"

	validate_pcs(self)
	if self.stock_entry_type == "Material Receive (WORK ORDER)":
		get_receive_work_order_batch(self)

	if self.purpose == "Material Transfer" and self.auto_created == 0:
		validate_metal_properties(self)
	else:
		allow_zero_valuation(self)


def validate_ir(self):
	# 	validate_inventory_dimention(self)

	if self.auto_created == 0:
		if self.stock_entry_type in [
			"Material Receive (WORK ORDER)",
			"Material Transfer (WORK ORDER)",
		]:
			if self.manufacturing_work_order:
				if self.manufacturing_work_order:
					dept_ir_mwo = frappe.get_all(
						"Department IR Operation",
						filters={
							"manufacturing_work_order": self.manufacturing_work_order,
							"docstatus": 0,
						},
						fields=["parent"],
					)

					if dept_ir_mwo:
						ir_names = ", ".join(
							f"'{row['parent']}'" for row in dept_ir_mwo
						)
						frappe.throw(
							f"{self.manufacturing_work_order} is already present in Draft :{ir_names} . Please submit or cancel them first."
						)

					emp_ir_mwo = frappe.get_all(
						"Employee IR Operation",
						filters={
							"manufacturing_work_order": self.manufacturing_work_order,
							"docstatus": 0,
						},
						fields=["parent"],
					)

					if emp_ir_mwo:
						ir_names = ", ".join(f"'{row['parent']}'" for row in emp_ir_mwo)
						frappe.throw(
							f"{self.manufacturing_work_order} is already present in Draft :{ir_names} . Please submit or cancel them first."
						)


def validate_pcs(self):
	# A single Material Request Item's qty can be split across several batch
	# rows (see get_fifo_batches). The MR Item's PCS is the authoritative total
	# for that item; it must be carried by exactly one row's worth of count so
	# the per-item PCS is not multiplied by the number of batches.
	#
	# Historically every row after the first was zeroed — but a batch row that
	# physically holds stock should never read 0 PCS. So each extra batch row
	# now defaults to 1, and the first row absorbs the deduction. The per-item
	# total is preserved: e.g. 71 split across two batches → 70 + 1 (not 71 + 0).
	#
	# The total is read from the MR Item (not the rows) so this stays idempotent
	# when re-run on already-split rows — e.g. the MOP Stock Entry is a copy of
	# the reserve Stock Entry and runs before_validate again.
	rows_by_mri = {}
	for row in self.items:
		if row.material_request_item:
			rows_by_mri.setdefault(row.material_request_item, []).append(row)

	for mri, rows in rows_by_mri.items():
		if len(rows) <= 1:
			continue
		total = cint(frappe.db.get_value("Material Request Item", mri, "pcs"))
		if total <= 0:
			# No authoritative total — leave the rows untouched.
			continue
		first, extras = rows[0], rows[1:]
		if total > len(extras):
			# Enough to keep at least 1 on every row.
			for r in extras:
				r.pcs = 1
			first.pcs = total - len(extras)
		else:
			# Not enough PCS to spread one-each; keep the whole count on the
			# first row (prior behaviour) rather than driving it below 1.
			for r in extras:
				r.pcs = 0
			first.pcs = total
	self.flags.ignore_mandatory = True


def get_receive_work_order_batch(self):
	batch_data = {}
	for entry in self.items:
		key = (entry.manufacturing_operation, entry.item_code)

		if entry.batch_no:
			batch_data[key] = entry.batch_no

		if not batch_data.get(key):
			batch_data[key] = frappe.db.get_value(
				"MOP Log",
				{
					"manufacturing_operation": entry.manufacturing_operation,
					"item_code": entry.item_code,
					"is_cancelled": 0,
				},
				"batch_no",
				order_by="flow_index desc, creation desc",
			)

		if entry.batch_no not in batch_data.get(key, []):
			entry.batch_no = batch_data[key]


def on_update_after_submit(self, method):
	if (
		self.subcontracting
		and frappe.db.get_value("Subcontracting", self.subcontracting, "docstatus") == 0
	):
		frappe.get_doc("Subcontracting", self.subcontracting).submit()


def validate_main_slip_warehouse(doc):
	for row in doc.items:
		main_slip = row.main_slip or row.to_main_slip
		if not main_slip:
			return
		warehouse = frappe.db.get_value("Main Slip", main_slip, "warehouse")

		if doc.auto_created == 0:
			warehouse = frappe.db.get_value(
				"Main Slip", main_slip, "raw_material_warehouse"
			)

		if (row.main_slip and row.s_warehouse != warehouse) or (
			row.to_main_slip and row.t_warehouse != warehouse
		):
			frappe.throw(
				_("Selected warehouse does not belongs to main slip {0}").format(
					main_slip
				)
			)


def validate_metal_properties(doc):
	mwo_wise_data = frappe._dict()
	msl_wise_data = frappe._dict()
	item_data = frappe._dict()
	operation_data = frappe._dict()
	msl_mop_dict = frappe._dict()
	if doc.manufacturing_work_order:
		mwo_wise_data[doc.manufacturing_work_order] = frappe.db.get_value(
			"Manufacturing Work Order",
			doc.manufacturing_work_order,
			[
				"metal_type",
				"metal_touch",
				"metal_purity",
				"metal_colour",
				"multicolour",
				"allowed_colours",
			],
			as_dict=1,
		)

	for row in doc.items:
		# allow_zero_valuation Start
		if row.inventory_type == "Customer Goods":
			row.allow_zero_valuation_rate = 1
		# allow_zero_valuation End

		main_slip = row.main_slip or row.to_main_slip

		if not (
			row.custom_manufacturing_work_order or main_slip
		) or row.custom_variant_of not in [
			"M",
			"F",
		]:
			continue

		if row.custom_manufacturing_work_order and not mwo_wise_data.get(
			row.custom_manufacturing_work_order
		):
			mwo_wise_data[row.custom_manufacturing_work_order] = frappe.db.get_value(
				"Manufacturing Work Order",
				row.custom_manufacturing_work_order,
				[
					"metal_type",
					"metal_touch",
					"metal_purity",
					"metal_colour",
					"multicolour",
					"allowed_colours",
				],
				as_dict=1,
			)

		if main_slip and not msl_wise_data.get(main_slip):
			msl_wise_data[main_slip] = frappe.db.get_value(
				"Main Slip",
				main_slip,
				[
					"metal_type",
					"metal_touch",
					"metal_purity",
					"metal_colour",
					"check_color",
					"for_subcontracting",
					"multicolour",
					"allowed_colours",
					"raw_material_warehouse",
				],
				as_dict=1,
			)

		if not item_data.get(row.item_code):
			attribute_det = frappe.db.get_values(
				"Item Variant Attribute",
				{
					"parent": row.item_code,
					"attribute": [
						"in",
						["Metal Type", "Metal Touch", "Metal Purity", "Metal Colour"],
					],
				},
				["attribute", "attribute_value"],
				as_dict=1,
			)

			item_data[row.item_code] = frappe._dict(
				{scrub(row.attribute): row.attribute_value for row in attribute_det}
			)
			item_data[row.item_code]["mwo"] = (
				[row.custom_manufacturing_work_order]
				if row.custom_manufacturing_work_order
				else []
			)
			key = row.manufacturing_operation or main_slip
			item_data[row.item_code]["mop"] = [key] if key else []
			item_data[row.item_code]["variant"] = row.custom_variant_of
			# Fold the two Item reads for this row (also read as custom_ignore_work_order
			# in the mwo colour check below) into a single fetch.
			itm = (
				frappe.db.get_value(
					"Item",
					row.item_code,
					["custom_is_manufacturing_item", "custom_ignore_work_order"],
					as_dict=True,
				)
				or {}
			)
			item_data[row.item_code]["ignore_touch_and_purity"] = itm.get(
				"custom_is_manufacturing_item"
			)
			item_data[row.item_code]["ignore_work_order"] = itm.get(
				"custom_ignore_work_order"
			)
		else:
			if (
				row.custom_manufacturing_work_order
				and row.custom_manufacturing_work_order
				not in item_data[row.item_code]["mwo"]
			):
				item_data[row.item_code]["mwo"].append(
					row.custom_manufacturing_work_order
				)

			key = row.manufacturing_operation or main_slip
			if key and key not in item_data[row.item_code]["mop"]:
				item_data[row.item_code]["mop"].append(key)

		msl_mop_dict.update({row.manufacturing_operation: main_slip})

		if row.manufacturing_operation and not operation_data.get(
			row.manufacturing_operation
		):
			operation = frappe.db.get_value(
				"Manufacturing Operation", row.manufacturing_operation, "operation"
			)
			if operation:
				operation_data[row.manufacturing_operation] = frappe.db.get_value(
					"Department Operation",
					operation,
					[
						"check_purity_in_main_slip as check_purity",
						"check_touch_in_main_slip as check_touch",
						"check_colour_in_main_slip as check_colour",
					],
					as_dict=True,
				)

	manufacturer = MANUFACTURER or doc.manufacturer
	company_validations = (
		frappe.db.get_value(
			"Manufacturing Setting",
			{"manufacturer": manufacturer},
			["check_purity", "check_colour", "check_touch"],
			as_dict=True,
		)
		or {}
	)
	if (
		(not company_validations.get("check_purity"))
		or (not company_validations.get("check_colour"))
		or (not company_validations.get("check_touch"))
	):
		frappe.throw(
			_(
				"Please set all validation options in Manufacturing Settings for manufacturer {0}"
			).format(manufacturer)
		)

	mwo_erros = {}
	msl_erros = {}

	for item in item_data:
		for mwo in item_data[item]["mwo"]:
			mwo_data = mwo_wise_data.get(mwo)
			mwo_erros.setdefault(mwo, [])

			if mwo_data.metal_type != item_data[item].metal_type:
				frappe.throw(
					_(
						"Only {0} Metal type allowed in Manufacturing Work Order {1}"
					).format(mwo_data.metal_type, mwo)
				)

			if (
				company_validations.get("check_touch")
				and not item_data[item].ignore_touch_and_purity
				and (
					company_validations.get("check_touch")
					in ["Both", item_data[item].variant]
				)
				and mwo_data.metal_touch != item_data[item].metal_touch
			):
				mwo_erros[mwo].append("Metal Touch")

			if (
				company_validations.get("check_purity")
				and not item_data[item].ignore_touch_and_purity
				and (
					company_validations.get("check_purity")
					in ["Both", item_data[item].variant]
				)
				and mwo_data.metal_purity != item_data[item].metal_purity
			):
				mwo_erros[mwo].append("Metal Purity")

			if (
				company_validations.get("check_colour")
				and (
					company_validations.get("check_colour")
					in ["Both", item_data[item].variant]
				)
				and mwo_data.metal_colour.lower()
				!= item_data[item].metal_colour.lower()
				and item_data[item]["ignore_work_order"] == 0
			):
				mwo_erros[mwo].append("Metal Colour")

		for mop in item_data[item]["mop"]:
			if msl_wise_data.get(mop):
				msl = mop
				msl_data = msl_wise_data.get(mop)
			else:
				msl = msl_mop_dict.get(mop)
				if not msl:
					continue
				msl_data = msl_wise_data.get(msl)
			if not msl_data.get("for_subcontracting"):
				msl_erros.setdefault(msl, [])

				if msl_data.metal_colour:
					if (
						company_validations.get("check_touch")
						and not item_data[item].ignore_touch_and_purity
					):
						if msl_data.metal_touch != item_data[item].metal_touch:
							msl_erros[msl].append("Metal Touch")
					if (
						company_validations.get("check_purity")
						and not item_data[item].ignore_touch_and_purity
					):
						if msl_data.metal_purity != item_data[item].metal_purity:
							msl_erros[msl].append("Metal Purity")
					if company_validations.get("check_colour"):
						if (
							msl_data.metal_colour.lower()
							!= item_data[item].metal_colour.lower()
							and msl_data.check_color
						):
							msl_erros[msl].append("Metal Colour")

			if msl_data.allowed_colours:
				if msl_data.multicolour == 1:
					allowed_colors = "".join(
						sorted([color.upper() for color in msl_data.allowed_colours])
					)
					colour_code = {"P": "Pink", "Y": "Yellow", "W": "White"}
					color_matched = False
					for char in allowed_colors:
						if char not in colour_code:
							frappe.throw(
								_(
									"Invalid color code <b>{0}</b> in MSL: <b>{1}</b>"
								).format(char, msl)
							)
						if (
							msl_data.check_color
							and colour_code[char] == item_data[item].metal_colour
						):
							color_matched = True
							break

					if msl_data.check_color and not color_matched:
						frappe.throw(
							f"Metal properties in MSL: <b>{msl}</b> do not match the Item. </br><b>Metal Properties are: (MT:{msl_data.metal_type}, MTC:{msl_data.metal_touch}, MP:{msl_data.metal_purity}, MC:{allowed_colors})</b>"
						)

	all_error_msg = []

	for row in mwo_erros:
		combine_components = ", ".join(set(mwo_erros[row]))
		if combine_components:
			all_error_msg.append(
				"{0} do not match with the selected Manufacturing Work Order : {1}".format(
					combine_components, row
				)
			)

	for row in msl_erros:
		combine_components = ", ".join(set(msl_erros[row]))
		if combine_components:
			all_error_msg.append(
				"{0} do not match with the selected Main Slip : {1}".format(
					combine_components, row
				)
			)

	combined_error_msg = "<br>".join(all_error_msg)
	if combined_error_msg:
		frappe.throw(_("{0}").format(combined_error_msg))


def validate_material_request_warehouses(self, method=None):
	"""A Stock Entry mapped from a Material Request does not own its warehouse routing.

	make_stock_entry's update_item copies the MR row's warehouses onto the Stock Entry
	row, and make_in_transit_stock_entry then re-points every t_warehouse at the target
	warehouse's transit warehouse. Either way the routing is the decision recorded on the
	Material Request, so a row that no longer agrees with it was edited afterwards.

	The in-transit form also locks the two grid columns
	(toggle_mr_transit_warehouse_lock in public/js/doctype_js/stock_entry.js); that lock
	is cosmetic, this is the enforcement, and it covers API and script writes too.

	Runs as a `validate` doc_event, so it sees the rows after update_batches has rebuilt
	them (a FIFO split keeps material_request_item and both warehouses on every split row)
	and after ERPNext's validate_warehouse has filled or nulled blanks by purpose.
	"""
	if not self.get("custom_material_request_reference"):
		return

	rows = [
		row for row in self.items if row.material_request and row.material_request_item
	]
	if not rows:
		return

	mr_items = bulk_map(
		"Material Request Item",
		[row.material_request_item for row in rows],
		["parent", "warehouse", "from_warehouse"],
	)
	mr_types = bulk_map(
		"Material Request",
		[row.material_request for row in rows],
		["material_request_type"],
	)
	# Only the target side has a second accepted value, so the transit lookup is limited
	# to the expected targets this entry actually references.
	transit_map = bulk_map(
		"Warehouse",
		[
			(mr_items.get(row.material_request_item) or {}).get("warehouse")
			for row in rows
		],
		["default_in_transit_warehouse"],
	)

	for row in rows:
		# material_request / material_request_item are written by the mapper but are
		# API-writable, so they are treated as untrusted: a row whose provenance cannot be
		# resolved, or whose item belongs to some other request, must not reach the
		# warehouse comparison -- it would enforce routing taken from an unrelated
		# Material Request. Deliberately per row: "Get Items From -> Material Request" is
		# multi-select, so one entry may legitimately carry rows from several requests and
		# custom_material_request_reference keeps only the last of them.
		mr_item = mr_items.get(row.material_request_item)
		if not mr_item:
			frappe.throw(
				_(
					"Row #{0}: Material Request Item {1} no longer exists. Its Material Request link must be corrected before this entry can be saved."
				).format(row.idx, frappe.bold(row.material_request_item))
			)

		if mr_item.parent != row.material_request:
			frappe.throw(
				_(
					"Row #{0}: Material Request Item {1} belongs to Material Request {2}, but the row is linked to {3}."
				).format(
					row.idx,
					frappe.bold(row.material_request_item),
					frappe.bold(mr_item.parent),
					frappe.bold(row.material_request),
				)
			)

		# Mirrors update_item in doc_events/material_request.py, which decides which side
		# of the row it fills from the Material Request type.
		mr_type = (mr_types.get(row.material_request) or {}).get(
			"material_request_type"
		)
		if mr_type == "Material Issue":
			expected_source, expected_target = mr_item.warehouse, None
		elif mr_type == "Customer Provided":
			expected_source, expected_target = None, mr_item.warehouse
		else:
			expected_source, expected_target = mr_item.from_warehouse, mr_item.warehouse

		# Neither a blank expectation nor a blank row value is an assertion that the side
		# is wrong. validate_warehouse has already run (controller methods precede
		# doc_event handlers) and it nulls whichever side the purpose does not use --
		# a Material Transfer request received as Customer Goods Received legitimately
		# ends up with no s_warehouse. Where the purpose *does* need the side, that same
		# ERPNext check rejects a blank before this handler is reached.
		if expected_source and row.s_warehouse and row.s_warehouse != expected_source:
			_throw_warehouse_mismatch(
				row, _("Source Warehouse"), expected_source, row.s_warehouse
			)

		if (
			not expected_target
			or not row.t_warehouse
			or row.t_warehouse == expected_target
		):
			continue

		# The in-transit button routes through the target's transit warehouse rather than
		# the target itself, so both readings are "as per the Material Request".
		transit = (transit_map.get(expected_target) or {}).get(
			"default_in_transit_warehouse"
		)
		if transit and row.t_warehouse == transit:
			continue

		_throw_warehouse_mismatch(
			row,
			_("Target Warehouse"),
			" / ".join(w for w in (expected_target, transit) if w),
			row.t_warehouse,
		)


def _throw_warehouse_mismatch(row, label, expected, actual):
	frappe.throw(
		_("Row #{0}: {1} must be {2} as per Material Request {3}, not {4}").format(
			row.idx,
			label,
			frappe.bold(expected),
			frappe.bold(row.material_request),
			frappe.bold(actual or _("(blank)")),
		)
	)


def on_cancel(self, method=None):
	update_manufacturing_operation(self, True)
	sync_mop_log_for_stock_entry(self, is_cancelled=True)


def prelock_bins(self, method=None):
	"""RULE B (canonical lock order): before ERPNext posts SLEs / updates Bins, acquire a
	FOR UPDATE lock on every (item_code, warehouse) Bin this Stock Entry will touch, in
	sorted order. Concurrent SE submits then acquire shared Bins in the SAME sequence,
	which removes the reverse-order cycles that cause 1213 deadlocks. Applied to ALL Stock
	Entries (one place covers MR / repack / metal-conversion / main-slip injection / EOD /
	loss / SNC etc.). Additive — ERPNext locks these same Bins during posting anyway; this
	only fixes the acquisition order."""
	# EXPERIMENTAL (F-004, opt-in, OFF by default): canonical position 0 -- serialize
	# same-item submits so ERPNext core's cross-voucher repost locks can't deadlock. Broadest
	# scope, so acquired FIRST to avoid inverting against Series/Bin. Real throughput cost;
	# enable per site with site_config "serialize_stock_submit_by_item": 1 and A/B measure.
	if frappe.conf.get("serialize_stock_submit_by_item"):
		lock_items([r.item_code for r in self.items])
	# Canonical position 2: pin this SE's naming-series counter (tabSeries) FOR UPDATE
	# *before* any Bin lock, so a transaction can never hold a Bin while waiting on the
	# series row another transaction holds while waiting on that Bin. Re-entrant with the
	# getseries() call already made at insert (same row, same txn) — purely additive.
	preallocate_series_for_docs(self)
	lock_bins_for_rows(self.items, "s_warehouse", "t_warehouse")


def prelock_bins_on_cancel(self, method=None):
	"""Canonical Bin ordering on the CANCEL path (F-002 / F-012 fix).

	ERPNext's StockEntry.on_cancel reverses SLEs and re-acquires the same (item,
	warehouse) Bin rows, but in lazy row-iteration order -- no canonical sort -- so a
	cancel racing a concurrent conformant submit could take Bins in the opposite order
	and deadlock (1213). before_cancel previously carried only the EOD-lock validator,
	so nothing pre-ordered the cancel's Bins. Pin them up front, sorted, matching every
	other flow. No series pre-lock: a cancel mints no new document name. Additive --
	these are the same Bins core's update_stock_ledger() takes anyway, just earlier and
	in canonical order; lock_bins_for_rows skips any (item, warehouse) with no Bin."""
	lock_bins_for_rows(self.items, "s_warehouse", "t_warehouse")


def before_submit(self, method):
	# validation_for_stock_entry_submission(self)
	if self.stock_entry_type != "Manufacture":
		self.posting_time = frappe.utils.nowtime()

	# group_se_items_and_update_mop_items(self, method)


def onsubmit(self, method):
	validate_items(self)
	# Hard skip reservation logic for Product Certification Receive
	# (Fire Assy Service / XRF Services), regardless of reservation settings.
	pc_name = getattr(self, "product_certification", None)
	if pc_name:
		pc = frappe.db.get_value(
			"Product Certification",
			pc_name,
			["service_type", "type"],
			as_dict=True,
		)
		if (
			pc
			and pc.type == "Receive"
			and pc.service_type
			in [
				"Fire Assy Service",
				"XRF Services",
			]
		):
			return

	types_for_reservation = frappe.db.get_all(
		"Stock Entry Type To Reservation",
		filters={"parent": "MOP Settings"},
		pluck="stock_entry_type_to_reservation",
	)
	if self.stock_entry_type in types_for_reservation:
		if self.stock_entry_type == "Repack" and (
			not (self.manufacturing_order and self.manufacturing_work_order)
		):
			return
		stock_reservation_entry_for_mwo(self)
		sync_mop_log_for_stock_entry(self)


def sync_mop_log_for_stock_entry(self, is_cancelled=False):
	"""Bridge Stock Entry lines onto MOP Log so the virtual ledger sees diamond /
	gemstone / metal items moved by Material Request and other work-order transfers.

	Stock has already moved physically at this point, so rows are written with
	``is_synced=True`` to keep MOP EOD Sync from materializing a duplicate Stock
	Entry on top of the existing one. Idempotent on ``(voucher_no, row_name,
	manufacturing_operation)`` so the reservation path's existing writes and any
	resubmit / replay do not create duplicates.
	"""
	if is_cancelled:
		frappe.db.sql(
			"""
			UPDATE `tabMOP Log`
			SET is_cancelled = 1
			WHERE voucher_type = 'Stock Entry'
			  AND voucher_no = %s
			  AND is_cancelled = 0
			""",
			(self.name,),
		)
		return

	# Nothing to sync unless at least one row is MOP-bound; skip the snapshot query
	# entirely rather than scanning tabMOP Log for a voucher that cannot match.
	if not any(
		row.get("manufacturing_operation") and row.item_code for row in self.items
	):
		return

	# Prefetch the existing (row_name, manufacturing_operation) pairs for this voucher
	# in one query instead of a per-row exists() check. row.name is unique per row, so
	# a create_mop_log earlier in this loop can never satisfy a later row's check — the
	# pre-loop snapshot is equivalent to re-querying each iteration.
	existing_mop_logs = {
		(r.row_name, r.manufacturing_operation)
		for r in frappe.get_all(
			"MOP Log",
			filters={
				"voucher_type": "Stock Entry",
				"voucher_no": self.name,
				"is_cancelled": 0,
			},
			fields=["row_name", "manufacturing_operation"],
		)
	}
	for row in self.items:
		if not (row.get("manufacturing_operation") and row.item_code):
			continue
		if (row.name, row.manufacturing_operation) in existing_mop_logs:
			continue
		create_mop_log(self, row, is_synced=True)


def stock_reservation_entry_for_mwo(self):
	# EIR injection: main_slip_inject.py stamps employee_ir on every auto-created
	# SE header.  These SEs MUST always reserve — they are legitimate MWO-linked
	# movements whose stock must be protected.  Bypassing the config gate here
	# ensures a missing "Repack" row in MOP Settings cannot silently skip reservation.
	_eir_ref = getattr(self, "employee_ir", None)
	is_eir_injection = isinstance(_eir_ref, str) and bool(_eir_ref.strip())

	if not (self.manufacturing_order and self.manufacturing_work_order):
		frappe.throw(
			_(
				"Parent Manufacturing Order and Manufacturing Work Order is required to create Stock Reservation Entry"
			)
		)
	sales_order, sales_order_item, manufacturer = frappe.get_cached_value(
		"Parent Manufacturing Order",
		self.manufacturing_order,
		["sales_order", "sales_order_item", "manufacturer"],
	)
	voucher_qty_row = frappe.db.sql(
		"""
        SELECT sum(custom_total_quantity) FROM `tabMaterial Request`
        WHERE manufacturing_order=%s AND docstatus!=2
        """,
		(self.manufacturing_order,),
	)
	base_mr_voucher_qty = None
	if voucher_qty_row and voucher_qty_row[0] and voucher_qty_row[0][0] is not None:
		base_mr_voucher_qty = flt(voucher_qty_row[0][0])
		addition_maximum_item__tolerance_percentage = frappe.db.get_value(
			"Manufacturing Setting",
			self.manufacturer or manufacturer,
			"addition_maximum_item__tolerance_percentage",
		)
		if addition_maximum_item__tolerance_percentage:
			base_mr_voucher_qty = base_mr_voucher_qty + (
				base_mr_voucher_qty
				* (flt(addition_maximum_item__tolerance_percentage) / 100)
			)

	# RULE B (canonical lock order): pre-lock every inbound Bin this reservation will
	# touch, in sorted (item_code, warehouse) order, so concurrent Stock Entry submits
	# acquire shared Bins in the same sequence — breaks 1213 reverse-order and
	# Series<->Bin deadlock cycles. Skipped for Material Receive (WORK ORDER), which
	# only writes MOP Logs and reserves nothing.
	if self.stock_entry_type != "Material Receive (WORK ORDER)":
		lock_bins_for_rows(
			[r for r in self.items if r.get("t_warehouse")], "t_warehouse"
		)
	# RULE A: iterate in canonical (item_code, warehouse, batch_no) order. A stable sort
	# keeps rows competing for the same key in their original relative order, so the
	# reserved quantity each row receives is unchanged.
	for row in sorted_stock_rows(self.items, warehouse_attr="t_warehouse"):
		if self.stock_entry_type == "Material Receive (WORK ORDER)":
			create_mop_log(self, row, is_synced=True)
			continue
		# Repack / issue rows only have s_warehouse; reserve against inbound stock only.
		if not row.get("t_warehouse"):
			continue
		has_batch_no, has_serial_no = frappe.get_cached_value(
			"Item", row.item_code, ["has_batch_no", "has_serial_no"]
		)
		# ERPNext's SRE before_submit (validate_with_allowed_qty) re-checks availability at the
		# WAREHOUSE ledger level (get_available_qty_to_reserve without batch_no -> get_stock_balance
		# / qty_after_transaction), NOT the batch bundle. That figure is the hard ceiling on what
		# this reservation may claim, so read it up front for every row.
		wh_available_qty = get_available_qty_to_reserve(row.item_code, row.t_warehouse)
		if has_batch_no and row.get("batch_no"):
			available_qty_to_reserve = get_available_qty_to_reserve(
				row.item_code, row.t_warehouse, batch_no=row.batch_no
			)
		else:
			available_qty_to_reserve = wh_available_qty
		qty_to_be_reserved = flt(min(flt(row.qty), available_qty_to_reserve))
		# A batch booked by this same transaction reads 0 until it settles (and Employee IR
		# extra-metal injections land stock whose availability lags the same way). Fall back to the
		# inbound line qty rather than refusing to reserve stock that demonstrably just landed.
		if qty_to_be_reserved <= 0 and flt(row.qty) > 0:
			qty_to_be_reserved = flt(row.qty)
		# Apply the ledger ceiling LAST, so neither the fallback above nor a precision-3 batch qty
		# can claim more than ERPNext's re-check will allow ("Cannot reserve more than Allowed
		# Qty"). A 0 ledger means "cannot tell yet", not "nothing there" -- it must not zero out a
		# reservation the fallback just rescued.
		if wh_available_qty > 0:
			qty_to_be_reserved = flt(min(qty_to_be_reserved, wh_available_qty))
		if qty_to_be_reserved <= 0:
			frappe.throw(
				_(
					"No available stock to reserve for Item {0} in Warehouse {1} in batch {2} available: {3}"
				).format(
					row.item_code,
					row.t_warehouse,
					row.batch_no,
					available_qty_to_reserve,
				)
			)

		total_so_reserved = get_sre_reserved_qty_for_voucher_detail_no(
			row.item_code, "Sales Order", sales_order, sales_order_item
		)
		effective_voucher_qty = (
			flt(base_mr_voucher_qty) if base_mr_voucher_qty is not None else 0
		)
		if is_eir_injection:
			effective_voucher_qty = max(
				effective_voucher_qty,
				flt(total_so_reserved) + qty_to_be_reserved,
			)
		elif not effective_voucher_qty and base_mr_voucher_qty is None:
			effective_voucher_qty = flt(total_so_reserved) + qty_to_be_reserved

		new_stock_reservation_entries_mwo = frappe.new_doc("Stock Reservation Entry")
		new_stock_reservation_entries_mwo.voucher_type = "Sales Order"
		new_stock_reservation_entries_mwo.voucher_no = sales_order
		new_stock_reservation_entries_mwo.item_code = row.item_code
		new_stock_reservation_entries_mwo.voucher_qty = effective_voucher_qty
		new_stock_reservation_entries_mwo.reserved_qty = qty_to_be_reserved
		new_stock_reservation_entries_mwo.company = self.company
		new_stock_reservation_entries_mwo.stock_uom = row.uom

		new_stock_reservation_entries_mwo.warehouse = row.t_warehouse
		new_stock_reservation_entries_mwo.manufacturing_work_order = (
			self.manufacturing_work_order
		)
		new_stock_reservation_entries_mwo.manufacturing_operation = (
			row.manufacturing_operation
		)
		new_stock_reservation_entries_mwo.voucher_detail_no = sales_order_item
		new_stock_reservation_entries_mwo.available_qty = max(
			available_qty_to_reserve, qty_to_be_reserved
		)
		new_stock_reservation_entries_mwo.has_batch_no = cint(has_batch_no)
		new_stock_reservation_entries_mwo.has_serial_no = cint(has_serial_no)
		if has_batch_no and row.get("batch_no"):
			new_stock_reservation_entries_mwo.reservation_based_on = "Serial and Batch"
			new_stock_reservation_entries_mwo.append(
				"sb_entries",
				{
					"batch_no": row.batch_no,
					"warehouse": row.t_warehouse,
					"qty": qty_to_be_reserved,
				},
			)
		else:
			new_stock_reservation_entries_mwo.reservation_based_on = "Qty"
		new_stock_reservation_entries_mwo.insert(ignore_links=1)
		new_stock_reservation_entries_mwo.submit()
		create_mop_log(self, row, is_synced=True)


def validate_items(self):
	if self.stock_entry_type != "Broken / Loss":
		return
	bom_item_codes = set(
		frappe.get_all("BOM Item", filters={"parent": self.bom_no}, pluck="item_code")
	)
	for i in self.items:
		if i.get("item_code") not in bom_item_codes:
			return frappe.throw(
				f"Item {i.get('item_code')} Not Present In BOM {self.bom_no}"
			)


def allow_zero_valuation(self):
	for row in self.items:
		if row.inventory_type == "Customer Goods":
			row.allow_zero_valuation_rate = 1


def update_material_request_status(self):
	try:
		if self.purpose != "Material Transfer for Manufacture":
			return
		mr_doc = frappe.db.get_value(
			"Material Request", {"docstatus": 0, "job_card": self.job_card}, "name"
		)
		frappe.msgprint(mr_doc)
		if mr_doc:
			mr_doc = frappe.get_doc(
				"Material Request", {"docstatus": 0, "job_card": self.job_card}, "name"
			)
			mr_doc.per_ordered = 100
			mr_doc.status = "Transferred"
			mr_doc.save()
			mr_doc.submit()
	except Exception as e:
		frappe.logger("utils").exception(e)


def create_finished_bom(self):
	"""
	-> This function creates a Finieshed Goods BOM based on the items in a stock entry
	-> It separates the items into manufactured items, raw materials and scrap items
	-> Subtracts the scrap quantity from the raw materials quantity
	-> Sets the properties of the BOM document before saving it,
	                                and retrieves properties from the Work Order BOM and assigns them to the newly created BOM
	"""
	if self.stock_entry_type != "Manufacture":
		return
	bom_doc = frappe.new_doc("BOM")
	items_to_manufacture = []
	raw_materials = []
	scrap_item = []
	# Seperate Items Into Items To Manufacture, Raw Materials and Scrap Items
	for item in self.items:
		if not item.s_warehouse and item.t_warehouse:
			variant_of = frappe.db.get_value("Item", item.item_code, "variant_of")
			if not variant_of and item.item_code not in ["METAL LOSS", "FINDING LOSS"]:
				items_to_manufacture.append(item.item_code)
			else:
				scrap_item.append({"item_code": item.item_code, "qty": item.qty})
		else:
			raw_materials.append({"item_code": item.item_code, "qty": item.qty})

	# Subtract Scrap Quantity from actual quantity
	for scrap, rm in itertools.product(scrap_item, raw_materials):
		variant_of = get_variant_of_item(rm.get("item_code"))
		if scrap.get("item_code") == rm.get("item_code"):
			rm["qty"] = rm["qty"] - scrap["qty"]

	bom_doc.item = items_to_manufacture[0]
	# Loop-invariant (depends only on self.bom_no) — fetch once, not once per raw item.
	diamond_quality = frappe.db.get_value(
		"BOM Diamond Detail", {"parent": self.bom_no}, "quality"
	)
	for raw_item in raw_materials:
		qty = raw_item.get("qty") or 1
		# Set all the items into respective Child Tables For BOM rate Calculation
		updated_bom = set_item_details(
			raw_item.get("item_code"), bom_doc, qty, diamond_quality
		)
	bom_info = (
		frappe.db.get_value(
			"BOM",
			self.bom_no,
			["customer", "gold_rate_with_gst", "tag_no"],
			as_dict=True,
		)
		or {}
	)
	updated_bom.customer = bom_info.get("customer")
	updated_bom.gold_rate_with_gst = bom_info.get("gold_rate_with_gst")
	updated_bom.is_default = 0
	updated_bom.tag_no = bom_info.get("tag_no")
	updated_bom.bom_type = "Finished Goods"
	updated_bom.reference_doctype = "Work Order"
	updated_bom.save(ignore_permissions=True)


def set_item_details(item_code, bom_doc, qty, diamond_quality):
	"""
	-> This function takes in an item_code, a bom_doc, a quantity and diamond_quality as its inputs,
	-> It then adds the item attributes and details in the corresponding child table of BOM document.
	-> It returns the updated BOM document.
	"""
	variant_of = get_variant_of_item(item_code)
	item_doc = frappe.get_doc("Item", item_code)
	attr_dict = {"item_variant": item_code, "quantity": qty}
	for attr in item_doc.attributes:
		attr_doc = frappe.as_json(attr)
		attr_doc = json.loads(attr_doc)
		for key, val in attr_doc.items():
			if key == "attribute":
				attr_dict[attr_doc[key].replace(" ", "_").lower()] = attr_doc[
					"attribute_value"
				]
	# Determine child table name based on variant
	child_table_name = ""
	if variant_of == "M":
		child_table_name = "metal_detail"
	elif variant_of == "D":
		child_table_name = "diamond_detail"
		weight_per_pcs = frappe.db.get_value(
			"Attribute Value", attr_dict.get("diamond_sieve_size"), "weight_in_cts"
		)
		attr_dict["weight_per_pcs"] = weight_per_pcs
		attr_dict["quality"] = diamond_quality
		attr_dict["pcs"] = qty / weight_per_pcs
	elif variant_of == "G":
		child_table_name = "gemstone_detail"
	elif variant_of == "F":
		child_table_name = "finding_detail"
	else:
		return
	bom_doc.append(child_table_name, attr_dict)
	return bom_doc


def custom_get_scrap_items_from_job_card(self):
	if not self.pro_doc:
		self.set_work_order_details()

	JobCard = frappe.qb.DocType("Job Card")
	JobCardScrapItem = frappe.qb.DocType("Job Card Scrap Item")

	query = (
		frappe.qb.from_(JobCardScrapItem)
		.join(JobCard)
		.on(JobCardScrapItem.parent == JobCard.name)
		.select(
			JobCardScrapItem.item_code,
			JobCardScrapItem.item_name,
			Sum(JobCardScrapItem.stock_qty).as_("stock_qty"),
			JobCardScrapItem.stock_uom,
			JobCardScrapItem.description,
			JobCard.wip_warehouse,
		)
		.where(
			(JobCard.docstatus == 1)
			& (JobCardScrapItem.item_code.isnotnull())
			& (JobCard.work_order == self.work_order)
		)
		.groupby(JobCardScrapItem.item_code)
	)

	scrap_items = query.run(as_dict=1)
	# custom change in query JC.wip_warehouse

	pending_qty = flt(self.pro_doc.qty) - flt(self.pro_doc.produced_qty)
	if pending_qty <= 0:
		return []

	used_scrap_items = self.get_used_scrap_items()
	for row in scrap_items:
		row.stock_qty -= flt(used_scrap_items.get(row.item_code))
		row.stock_qty = (row.stock_qty) * flt(self.fg_completed_qty) / flt(pending_qty)

		if used_scrap_items.get(row.item_code):
			used_scrap_items[row.item_code] -= row.stock_qty

		if cint(frappe.get_cached_value("UOM", row.stock_uom, "must_be_whole_number")):
			row.stock_qty = frappe.utils.ceil(row.stock_qty)

	return scrap_items


def custom_get_bom_scrap_material(self, qty):
	from erpnext.manufacturing.doctype.bom.bom import get_bom_items_as_dict

	# item dict = { item_code: {qty, description, stock_uom} }
	item_dict = (
		get_bom_items_as_dict(
			self.bom_no, self.company, qty=qty, fetch_exploded=0, fetch_scrap_items=1
		)
		or {}
	)

	for row in self.get_scrap_items_from_job_card():
		if row.stock_qty <= 0:
			continue

		item_row = item_dict.get(row.item_code)
		if not item_row:
			item_row = frappe._dict({})

		item_row.update(
			{
				"uom": row.stock_uom,
				"from_warehouse": "",
				"qty": row.stock_qty + flt(item_row.stock_qty),
				"converison_factor": 1,
				"is_scrap_item": 1,
				"item_name": row.item_name,
				"description": row.description,
				"allow_zero_valuation_rate": 1,
				"to_warehouse": row.wip_warehouse,  # custom change
			}
		)

		item_dict[row.item_code] = item_row

	return item_dict


def update_manufacturing_operation(doc, is_cancelled=False):
	pass


def validate_duplicate_batches(entry, batch_data):
	key = (entry.manufacturing_operation, entry.item_code)
	if not batch_data.get(key):
		batch_data[key] = frappe.db.get_all(
			"MOP Log",
			filters={
				"manufacturing_operation": entry.manufacturing_operation,
				"item_code": entry.item_code,
				"is_cancelled": 0,
			},
			pluck="batch_no",
			order_by="flow_index desc, creation desc",
		)

	if entry.batch_no not in batch_data[key]:
		frappe.throw(
			_(
				"Row {0}: Selected Item {1} Batch <b>{2}</b> does not belong to <b>{3}</b><br><br><b>Allowed Batches:</b> {4}"
			).format(
				entry.idx,
				entry.item_code,
				entry.batch_no,
				entry.manufacturing_operation,
				", ".join(str(b) for b in batch_data[key] if b),
			)
		)


def get_warehouse_details(
	mop_doc, warehouse_data, se_employee=None, se_subcontractor=None
):
	d_warehouse = None
	e_warehouse = None
	if mop_doc.department and not warehouse_data.get(mop_doc.department):
		warehouse_data[mop_doc.department] = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"department": mop_doc.department,
				"warehouse_type": "Manufacturing",
			},
		)
	d_warehouse = warehouse_data.get(mop_doc.department)
	mop_employee = mop_doc.employee or se_employee
	if mop_employee:
		if not warehouse_data.get(mop_employee):
			warehouse_data[mop_employee] = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"company": mop_doc.company,
					"employee": mop_employee,
					"warehouse_type": "Manufacturing",
				},
			)

		e_warehouse = warehouse_data[mop_employee]

	if not mop_employee:
		mop_subcontractor = mop_doc.subcontractor or se_subcontractor
		if not warehouse_data.get(mop_subcontractor):
			warehouse_data[mop_subcontractor] = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"company": mop_doc.company,
					"subcontractor": mop_subcontractor,
					"warehouse_type": "Manufacturing",
				},
			)
		e_warehouse = warehouse_data[mop_subcontractor]

	return d_warehouse, e_warehouse


@frappe.whitelist()
def make_stock_in_entry(source_name, target_doc=None):
	def set_missing_values(source, target):
		if target.stock_entry_type == "Customer Goods Received":
			target.stock_entry_type = "Customer Goods Issue"
			target.purpose = "Material Issue"
			target.custom_cg_issue_against = source.name
		elif target.stock_entry_type == "Customer Goods Issue":
			target.stock_entry_type = "Customer Goods Received"
			target.purpose = "Material Receipt"
		elif source.stock_entry_type == "Customer Goods Transfer":
			target.stock_entry_type = "Customer Goods Transfer"
			target.purpose = "Material Transfer"
		target.set_missing_values()

	def update_item(source_doc, target_doc, source_parent):
		target_doc.t_warehouse = ""
		# getting target warehouse on end transit
		target_wh = ""
		if source_parent.custom_material_request_reference:
			ref_mr = frappe.get_doc(
				"Material Request", source_parent.custom_material_request_reference
			)
			for wh in ref_mr.items:
				if wh.item_code == source_doc.item_code:
					target_wh = wh.warehouse
			target_doc.t_warehouse = target_wh

		target_doc.s_warehouse = source_doc.t_warehouse
		target_doc.qty = source_doc.qty

	doclist = get_mapped_doc(
		"Stock Entry",
		source_name,
		{
			"Stock Entry": {
				"doctype": "Stock Entry",
				"field_map": {"name": "outgoing_stock_entry"},
				"validation": {"docstatus": ["=", 1]},
			},
			"Stock Entry Detail": {
				"doctype": "Stock Entry Detail",
				"field_map": {
					"name": "ste_detail",
					"parent": "against_stock_entry",
					"serial_no": "serial_no",
					"batch_no": "batch_no",
				},
				"postprocess": update_item,
				# "condition": lambda doc: flt(doc.qty) - flt(doc.transferred_qty) > 0.01,
			},
		},
		target_doc,
		set_missing_values,
	)

	return doclist


def convert_metal_purity(from_item: dict, to_item: dict, s_warehouse, t_warehouse):
	"""Create and submit a Repack Stock Entry between two attribute-resolved items.

	Not used by Employee IR injection (see ``main_slip_inject``). **Unsafe for
	batch-tracked metal** as written: ``before_validate`` requires ``batch_no`` on
	outgoing rows unless serialised; this helper does not run FIFO batch allocation.
	Parameters are typed as ``dict`` but the implementation uses attribute access
	(``from_item.metal_type``, …)—pass ``SimpleNamespace`` / ``frappe._dict`` or
	refactor to subscripting. Prefer EIR/MOP injection builders + shared FIFO helpers
	for production metal flows.
	"""
	f_item = get_item_from_attribute(
		from_item.metal_type,
		from_item.metal_touch,
		from_item.metal_purity,
		from_item.metal_colour,
	)
	t_item = get_item_from_attribute(
		to_item.metal_type,
		to_item.metal_touch,
		to_item.metal_purity,
		to_item.metal_colour,
	)
	doc = frappe.new_doc("Stock Entry")
	doc.stock_entry_type = "Repack"
	doc.purpose = "Repack"
	doc.inventory_type = "Regular Stock"
	doc.auto_created = True
	doc.append(
		"items",
		{
			"item_code": f_item,
			"s_warehouse": s_warehouse,
			"t_warehouse": None,
			"qty": from_item.qty,
			"inventory_type": "Regular Stock",
		},
	)
	doc.append(
		"items",
		{
			"item_code": t_item,
			"s_warehouse": None,
			"t_warehouse": t_warehouse,
			"qty": to_item.qty,
			"inventory_type": "Regular Stock",
		},
	)
	doc.save()
	doc.submit()


@frappe.whitelist()
def make_mr_on_return(source_name, target_doc=None):
	def set_missing_values(source, target):
		itm_batch = []
		dict = {}
		for i in source.items:
			dict.update(
				{
					"item": i.item_code,
					"batch": i.batch_no,
					"serial": i.serial_no,
					"idx": i.idx,
				}
			)
			itm_batch.append(dict)

		for itm in target.items:
			for b in itm_batch:
				if itm.item_code == b.get("item") and itm.idx == b.get("idx"):
					itm.custom_batch_no = b.get("batch")
					itm.custom_serial_no = b.get("serial")

		if source.stock_entry_type == "Customer Goods Transfer":
			target.material_request_type = "Material Transfer"
		target.set_missing_values()

	def update_item(source_doc, target_doc, source_parent):
		target_doc.from_warehouse = source_doc.t_warehouse
		target_wh = ""
		if source_parent.outgoing_stock_entry:
			ref_se = frappe.get_doc("Stock Entry", source_parent.outgoing_stock_entry)
			for wh in ref_se.items:
				if wh.item_code == source_doc.item_code:
					target_wh = wh.s_warehouse

		timestamp_obj = datetime.strptime(
			str(source_doc.creation), "%Y-%m-%d %H:%M:%S.%f"
		)

		date = timestamp_obj.strftime("%Y-%m-%d")
		time = timestamp_obj.strftime("%H:%M:%S.%f")

		wh_qty = get_batch_qty(
			batch_no=source_doc.batch_no,
			warehouse=source_doc.t_warehouse,
			item_code=source_doc.item_code,
			posting_date=date,
			posting_time=time,
		)

		target_doc.warehouse = target_wh
		target_doc.qty = wh_qty

	doclist = get_mapped_doc(
		"Stock Entry",
		source_name,
		{
			"Stock Entry": {
				"doctype": "Material Request",
			},
			"Stock Entry Detail": {
				"doctype": "Material Request Item",
				"field_map": {
					"custom_serial_no": "serial_no",
					"custom_batch_no": "batch_no",
				},
				"postprocess": update_item,
			},
		},
		target_doc,
		set_missing_values,
	)

	return doclist


"""
create_material_receipt_for_sales_person function
creates a return receipt for items issued. i.e. Stock Enty to Stock Entry.
"""


@frappe.whitelist()
def create_material_receipt_for_sales_person(source_name):
	source_doctype = "Stock Entry"
	# target_doctype = "Stock Entry"
	source_doc = frappe.get_doc("Stock Entry", source_name)
	target_doc = frappe.new_doc(source_doctype)
	target_doc.update(source_doc.as_dict())

	StockEntry = frappe.qb.DocType("Stock Entry")
	StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")

	query = (
		frappe.qb.from_(StockEntry)
		.left_join(StockEntryDetail)
		.on(StockEntryDetail.parent == StockEntry.name)
		.select(
			StockEntry.name,
			StockEntryDetail.item_code,
			Sum(StockEntryDetail.qty).as_("quantity"),
		)
		.where(StockEntry.custom_material_return_receipt_number == source_doc.name)
		.groupby(StockEntry.name, StockEntryDetail.item_code)
	)

	material_receipts = query.run(as_dict=True)

	item_qty_material_receipt = {}
	for row in material_receipts:
		if row.item_code not in item_qty_material_receipt:
			item_qty_material_receipt[row.item_code] = row.quantity
		else:
			item_qty_material_receipt[row.item_code] += row.quantity

	target_doc.stock_entry_type = "Material Receipt - Sales Person"
	target_doc.docstatus = 0
	target_doc.posting_date = frappe.utils.nowdate()
	target_doc.posting_time = frappe.utils.nowtime()

	CustomerApproval = frappe.qb.DocType("Customer Approval")
	SalesOrderItemChild = frappe.qb.DocType("Sales Order Item Child")

	query = (
		frappe.qb.from_(CustomerApproval)
		.left_join(SalesOrderItemChild)
		.on(SalesOrderItemChild.parent == CustomerApproval.name)
		.select(SalesOrderItemChild.item_code, Sum(SalesOrderItemChild.quantity))
		.where(CustomerApproval.stock_entry_reference.like(source_name))
		.groupby(SalesOrderItemChild.item_code)
	)
	items_quantity_ca = query.run(as_dict=True)

	items_quantity_ca = {
		item["item_code"]: flt(item["sum(soic.quantity)"]) for item in items_quantity_ca
	}
	items_quantity = item_qty_material_receipt.copy()
	for item_code in items_quantity_ca:
		if item_code in items_quantity:
			items_quantity[item_code] += items_quantity_ca[item_code]
		else:
			items_quantity[item_code] = items_quantity_ca[item_code]

	filtered_items = []
	for item in target_doc.items:
		if item.item_code not in items_quantity:
			filtered_items.append(item)
		elif item.item_code in items_quantity:
			if item.qty != items_quantity[item.item_code]:
				item.qty -= items_quantity[item.item_code]
				filtered_items.append(item)

	serial_and_batch_items = {}
	for item in source_doc.items:
		serial_and_batch_items[item.item_code] = [item.serial_no, item.batch_no]
	target_doc.items = filtered_items
	target_doc.stock_entry_type = "Material Receipt - Sales Person"
	target_doc.custom_material_return_receipt_number = source_doc.name
	for item in target_doc.items:
		if item.item_code in serial_and_batch_items:
			item.serial_no = serial_and_batch_items[item.item_code][0]
			item.batch_no = serial_and_batch_items[item.item_code][1]
		item.s_warehouse, item.t_warehouse = item.t_warehouse, item.s_warehouse
	target_doc.insert()
	# total_return_receipt_for_issue = {}

	return target_doc


"""
create_material_receipt_for_customer_approval function
creates a return receipt for items issued. i.e. Customer Approval to Stock Entry.
"""


@frappe.whitelist()
def create_material_receipt_for_customer_approval(source_name, cust_name):
	CustomerApproval = frappe.qb.DocType("Customer Approval")
	SalesOrderItemChild = frappe.qb.DocType("Sales Order Item Child")

	query = (
		frappe.qb.from_(CustomerApproval)
		.left_join(SalesOrderItemChild)
		.on(SalesOrderItemChild.parent == CustomerApproval.name)
		.select(
			SalesOrderItemChild.item_code,
			Sum(SalesOrderItemChild.quantity).as_("total_quantity"),
			SalesOrderItemChild.serial_no,
		)
		.where(
			(CustomerApproval.stock_entry_reference.like(source_name))
			& (CustomerApproval.name == cust_name)
		)
		.groupby(SalesOrderItemChild.item_code, SalesOrderItemChild.serial_no)
	)
	items_quantity_ca = query.run(as_dict=True)

	item_qty = {
		item["item_code"]: {
			"total_quantity": item["total_quantity"],
			"serial_no": item["serial_no"],
		}
		for item in items_quantity_ca
	}

	target_doc = frappe.new_doc("Stock Entry")

	target_doc.update(frappe.get_doc("Stock Entry", source_name).as_dict())
	target_doc.docstatus = 0

	target_doc.items = []
	for item in frappe.get_all(
		"Stock Entry Detail", filters={"parent": source_name}, fields=["*"]
	):
		se_item = frappe.new_doc("Stock Entry Detail")
		item.serial_and_batch_bundle = None
		se_item.update(item)
		se_item.qty = item_qty.get(item.item_code, {}).get("total_quantity", 0)
		se_item.serial_no = item_qty.get(item.item_code, {}).get("serial_no", "")
		target_doc.append("items", se_item)

	target_doc.stock_entry_type = "Material Receipt - Sales Person"
	target_doc.custom_material_return_receipt_number = source_name
	target_doc.custom_customer_approval_reference = cust_name

	for item in target_doc.items:
		item.s_warehouse, item.t_warehouse = item.t_warehouse, item.s_warehouse

	target_doc.insert()
	return target_doc.name


"""
create_material_receipt_for_customer_approval
validates serial items entered are equal to quantity or not if not appropriate errors received

"""


@frappe.whitelist()
def make_stock_in_entry_on_transit_entry(source_name, target_doc=None):
	def set_missing_values(source, target):
		target.stock_entry_type = source.stock_entry_type
		target.set_missing_values()

	def update_item(source_doc, target_doc, source_parent):
		target_doc.t_warehouse = ""

		if source_doc.material_request_item and source_doc.material_request:
			add_to_transit = frappe.db.get_value(
				"Stock Entry", source_name, "add_to_transit"
			)
			if add_to_transit:
				warehouse = frappe.get_value(
					"Material Request Item",
					source_doc.material_request_item,
					"warehouse",
				)
				target_doc.t_warehouse = warehouse

		target_doc.s_warehouse = source_doc.t_warehouse
		target_doc.qty = source_doc.qty - source_doc.transferred_qty

	doclist = get_mapped_doc(
		"Stock Entry",
		source_name,
		{
			"Stock Entry": {
				"doctype": "Stock Entry",
				"field_map": {"name": "outgoing_stock_entry"},
				"validation": {"docstatus": ["=", 1]},
			},
			"Stock Entry Detail": {
				"doctype": "Stock Entry Detail",
				"field_map": {
					"name": "ste_detail",
					"parent": "against_stock_entry",
					"serial_no": "serial_no",
					"batch_no": "batch_no",
				},
				"postprocess": update_item,
				"condition": lambda doc: flt(doc.qty) - flt(doc.transferred_qty) > 0.01,
			},
		},
		target_doc,
		set_missing_values,
	)

	return doclist


@frappe.whitelist()
def validation_of_serial_item(issue_doc):
	doc = frappe.get_doc("Stock Entry", issue_doc)
	serial_item = {}
	for item in doc.items:
		check_serial_no = frappe.db.get_list(
			"Item", filters={"item_code": item.item_code}, fields=["has_serial_no"]
		)
		if check_serial_no[0]["has_serial_no"] == 1:
			serial_item[item.item_code] = item.serial_no.split("\n")
	return serial_item


@frappe.whitelist()
def set_filter_for_main_slip(doctype, txt, searchfield, start, page_len, filters):
	mnf = filters.get("mnf")
	metal_purity = frappe.db.get_value(
		"Manufacturing Work Order", {mnf}, "metal_purity"
	)
	# frappe.throw(str(metal_purity))
	return metal_purity


def group_se_items_and_update_mop_items(doc, method):
	if not doc.items:
		return

	doc.set("custom_mop_items", [])

	for row in doc.items:
		mop_row = copy.deepcopy(row.as_dict())
		mop_row["name"] = None
		mop_row["idx"] = None

		if row.get("doctype") == "Stock Entry MOP Item":
			row.doctype = "Stock Entry Detail"
		else:
			mop_row["doctype"] = "Stock Entry MOP Item"

		doc.append("custom_mop_items", mop_row)

	doc.update_child_table("items")
	doc.update_child_table("custom_mop_items")

	if doc.auto_created:
		doc_dict = doc.as_dict()
		grouped_se_items = group_se_items(doc_dict.get("custom_mop_items"))

		if grouped_se_items and len(grouped_se_items) < len(doc.items):
			doc.set("items", [])

			for row in grouped_se_items:
				row["name"] = None
				row["idx"] = None
				row["doctype"] = "Stock Entry Detail"
				doc.append("items", row)

	doc.calculate_rate_and_amount()
	doc.update_child_table("items")


def group_se_items(se_items: list):
	if not se_items:
		return

	group_keys = ["item_code", "batch_no"]
	sum_keys = ["qty", "transfer_qty", "pcs"]
	concat_keys = [
		"custom_parent_manufacturing_order",
		"custom_manufacturing_work_order",
		"manufacturing_operation",
	]
	exclude_keys = [
		"name",
		"idx",
		"valuation_rate",
		"basic_rate",
		"amount",
		"basic_amount",
		"taxable_value",
		"actual_qty",
	]
	grouped_items = group_aggregate_with_concat(
		se_items, group_keys, sum_keys, concat_keys, exclude_keys
	)

	return grouped_items


def get_last_mwo_wh_based_on_index(mwo):
	filters = {"manufacturing_work_order": mwo, "is_cancelled": 0}
	last_index, last_log_name, to_warehouse = frappe.db.get_value(
		"MOP Log", filters, ["max(flow_index) as flow_index", "name", "to_warehouse"]
	)
	return last_index, last_log_name, to_warehouse


def consume_stock_reservation_entry(sre_doc, update_bin=True):
	"""Mark a Stock Reservation Entry as consumed/delivered instead of cancelling it.

	Sets ``delivered_qty = reserved_qty`` and updates the status to "Delivered".
	This keeps the SRE record intact for audit / tracking while properly releasing
	the Bin's reserved-stock counter (ERPNext's formula:
	``reserved = reserved_qty - delivered_qty - transferred_qty - consumed_qty``).

	If the SRE has Serial and Batch child entries (``sb_entries``), each child row's
	``delivered_qty`` is also set to match its ``qty`` so the batch-level reservation
	is released as well.

	Call ``frappe.clear_document_cache("Bin")`` before/after as needed — just like
	the old cancel flow did.
	"""
	from erpnext.stock.utils import get_or_make_bin

	sre_doc.flags.ignore_permissions = True

	# Update batch-level child entries if present
	if sre_doc.reservation_based_on == "Serial and Batch" and sre_doc.sb_entries:
		for entry in sre_doc.sb_entries:
			entry.delivered_qty = flt(entry.qty)
			entry.db_update()

	# Set delivered_qty = reserved_qty → triggers status "Delivered"
	sre_doc.db_set("delivered_qty", flt(sre_doc.reserved_qty), update_modified=True)
	sre_doc.delivered_qty = flt(sre_doc.reserved_qty)

	# Explicitly set status to "Delivered"
	sre_doc.update_status(status="Delivered")

	# Refresh bin reserved stock so the physical stock becomes available
	if update_bin:
		bin_name = get_or_make_bin(sre_doc.item_code, sre_doc.warehouse)
		bin_doc = frappe.get_cached_doc("Bin", bin_name)
		bin_doc.update_reserved_stock()
