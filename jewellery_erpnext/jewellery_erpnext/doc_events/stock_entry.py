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

from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.se_utils import (
	create_repack_for_subcontracting,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.update_utils import (
	update_main_slip_se_details,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	create_mop_log_for_stock_transfer_to_mo as create_mop_log,
)
from jewellery_erpnext.utils import (
	get_item_from_attribute,
	get_variant_of_item,
	group_aggregate_with_concat,
)


def before_validate(self, method):
	validate_ir(self)
	if (
		not self.get("__islocal")
		and frappe.db.exists("Stock Entry", self.name)
		and self.docstatus == 0
	) or self.flags.throw_batch_error:
		self.update_batches()

	pure_item_purity = None

	dir_staus_cache = {}
	purity_cache = {}

	manufacturer = self.manufacturer or frappe.defaults.get_user_default("manufacturer")

	pure_item = frappe.db.get_value(
		"Manufacturing Setting",
		{"manufacturer": manufacturer},
		"pure_gold_item",
	)

	if not pure_item:
		frappe.throw(_("Select Manufacturer in session defaults or in Field"))

	pure_item_purity = get_purity_percentage(pure_item)

	for row in self.items:
		if (
			not self.auto_created
			and not row.batch_no
			and not row.serial_no
			and row.s_warehouse
		):
			frappe.throw(_("Please click Get FIFO Batch Button"))

		if not self.auto_created and row.manufacturing_operation:
			if row.manufacturing_operation not in dir_staus_cache:
				dir_staus_cache[row.manufacturing_operation] = frappe.db.get_value(
					"Manufacturing Operation",
					row.manufacturing_operation,
					"department_ir_status",
				)
			if dir_staus_cache[row.manufacturing_operation] == "In-Transit":
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
			if row.item_code not in purity_cache:
				purity_cache[row.item_code] = get_purity_percentage(row.item_code)

			item_purity = purity_cache[row.item_code]

			if not item_purity:
				continue

			if pure_item_purity == item_purity:
				row.custom_pure_qty = row.qty
				frappe.log_error(
					f"Item: {row.item_code}, Purity: {item_purity}, Pure Qty: {row.custom_pure_qty}",
					"Pure Qty Calculation",
				)
			else:
				row.custom_pure_qty = flt((item_purity * row.qty) / pure_item_purity, 3)

		if (
			self.stock_entry_type == "Material Receipt"
			and not row.inventory_type
			and not row.batch_no
		):
			row.inventory_type = "Regular Stock"

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
	if self.auto_created == 0:
		return

	if self.stock_entry_type not in [
		"Material Receive (WORK ORDER)",
		"Material Transfer (WORK ORDER)",
	]:
		return

	if not self.manufacturing_work_order:
		return

	mwo = self.manufacturing_work_order

	dept_ir = frappe.get_all(
		"Department IR Operation",
		filters={
			"manufacturing_work_order": mwo,
			"docstatus": 0,
		},
		pluck="parent",
	)

	if dept_ir:
		ir_names = ", ".join(f"'{row}'" for row in dept_ir)
		frappe.throw(
			f"{mwo} is already present in Draft :{ir_names} . Please submit or cancel them first."
		)

	emp_ir = frappe.get_all(
		"Employee IR Operation",
		filters={
			"manufacturing_work_order": mwo,
			"docstatus": 0,
		},
		pluck="parent",
	)

	if emp_ir:
		ir_names = ", ".join(f"'{row}'" for row in emp_ir)
		frappe.throw(
			f"{mwo} is already present in Draft :{ir_names} . Please submit or cancel them first."
		)


def validate_pcs(self):
	seen_mr_items = set()

	for row in self.items:
		mr_item = row.material_request_item
		if not mr_item:
			continue
		if mr_item in seen_mr_items:
			row.pcs = 0
		else:
			seen_mr_items.add(mr_item)

	self.flags.ignore_mandatory = True


def get_receive_work_order_batch(self):
	if not self.items:
		return

	keys = {
		(row.manufacturing_operation, row.item_code)
		for row in self.items
		if row.manufacturing_operation and row.item_code
	}

	mop_logs = frappe.get_all(
		"MOP Log",
		filters={
			"manufacturing_operation": ["in", [k[0] for k in keys]],
			"item_code": ["in", [k[1] for k in keys]],
			"is_cancelled": 0,
		},
		fields=[
			"manufacturing_operation",
			"item_code",
			"batch_no",
		],
		order_by="flow_index desc, creation desc",
	)

	batch_data = {}
	for row in mop_logs:
		key = (row.manufacturing_operation, row.item_code)
		if key not in batch_data and row.batch_no:
			batch_data[key] = row.batch_no

	for entry in self.items:
		key = (entry.manufacturing_operation, entry.item_code)
		if entry.batch_no:
			batch_data[key] = entry.batch_no
		if not entry.batch_no and key in batch_data:
			entry.batch_no = batch_data[key]


def on_update_after_submit(self, method):
	if (
		self.subcontracting
		and frappe.db.get_value("Subcontracting", self.subcontracting, "docstatus") == 0
	):
		frappe.get_doc("Subcontracting", self.subcontracting).submit()


def validate_metal_properties(doc):
	if not doc.items:
		return

	mwo_set = set()
	msl_set = set()
	item_codes = set()
	operations = set()
	msl_mop_dict = {}

	for row in doc.items:
		if row.inventory_type == "Customer Goods":
			row.allow_zero_valuation_rate = 1

		if row.custom_variant_of not in ["M", "F"]:
			continue

		main_slip = row.main_slip or row.to_main_slip

		if not (row.custom_manufacturing_work_order or main_slip):
			continue

		if row.custom_manufacturing_work_order:
			mwo_set.add(row.custom_manufacturing_work_order)

		if main_slip:
			msl_set.add(main_slip)

		if row.item_code:
			item_codes.add(row.item_code)

		if row.manufacturing_operation:
			operations.add(row.manufacturing_operation)
			msl_mop_dict[row.manufacturing_operation] = main_slip

	mwo_wise_data = {
		d.name: d
		for d in frappe.get_all(
			"Manufacturing Work Order",
			filters={"name": ["in", list(mwo_set)]},
			fields=[
				"name",
				"metal_type",
				"metal_touch",
				"metal_purity",
				"metal_colour",
				"multicolour",
				"allowed_colours",
			],
		)
	}

	msl_wise_data = {
		d.name: d
		for d in frappe.get_all(
			"Main Slip",
			filters={"name": ["in", list(msl_set)]},
			fields=[
				"name",
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
		)
	}

	attr_rows = frappe.get_all(
		"Item Variant Attribute",
		filters={
			"parent": ["in", list(item_codes)],
			"attribute": [
				"in",
				["Metal Type", "Metal Touch", "Metal Purity", "Metal Colour"],
			],
		},
		fields=["parent", "attribute", "attribute_value"],
	)

	item_attr_map = {}
	for r in attr_rows:
		item_attr_map.setdefault(r.parent, {})[scrub(r.attribute)] = r.attribute_value

	item_flags = {
		d.name: d.custom_is_manufacturing_item
		for d in frappe.get_all(
			"Item",
			filters={"name": ["in", list(item_codes)]},
			fields=["name", "custom_is_manufacturing_item"],
		)
	}

	operation_map = {}
	if operations:
		mop_rows = frappe.get_all(
			"Manufacturing Operation",
			filters={"name": ["in", list(operations)]},
			fields=["name", "operation"],
		)

		dept_ops = frappe.get_all(
			"Department Operation",
			filters={"name": ["in", [r.operation for r in mop_rows if r.operation]]},
			fields=[
				"name",
				"check_purity_in_main_slip as check_purity",
				"check_touch_in_main_slip as check_touch",
				"check_colour_in_main_slip as check_colour",
			],
		)

		dept_map = {d.name: d for d in dept_ops}

		for r in mop_rows:
			if r.operation in dept_map:
				operation_map[r.name] = dept_map[r.operation]

	manufacturer = frappe.defaults.get_user_default("manufacturer")
	company_validations = (
		frappe.db.get_value(
			"Manufacturing Setting",
			{"manufacturer": manufacturer},
			["check_purity", "check_colour", "check_touch"],
			as_dict=True,
		)
		or {}
	)

	item_data = {}

	for row in doc.items:
		if row.custom_variant_of not in ["M", "F"]:
			continue

		main_slip = row.main_slip or row.to_main_slip
		if not (row.custom_manufacturing_work_order or main_slip):
			continue

		if row.item_code not in item_data:
			attrs = item_attr_map.get(row.item_code, {})

			item_data[row.item_code] = frappe._dict(attrs)
			item_data[row.item_code]["mwo"] = []
			item_data[row.item_code]["mop"] = []
			item_data[row.item_code]["variant"] = row.custom_variant_of
			item_data[row.item_code]["ignore_touch_and_purity"] = item_flags.get(
				row.item_code
			)

		if row.custom_manufacturing_work_order:
			if (
				row.custom_manufacturing_work_order
				not in item_data[row.item_code]["mwo"]
			):
				item_data[row.item_code]["mwo"].append(
					row.custom_manufacturing_work_order
				)

		key = row.manufacturing_operation or main_slip
		if key and key not in item_data[row.item_code]["mop"]:
			item_data[row.item_code]["mop"].append(key)

	mwo_errors = {}
	msl_errors = {}

	for item, data in item_data.items():
		for mwo in data["mwo"]:
			mwo_data = mwo_wise_data.get(mwo)
			if not mwo_data:
				continue

			mwo_errors.setdefault(mwo, [])

			if mwo_data.metal_type != data.get("metal_type"):
				frappe.throw(
					_(
						"Only {0} Metal type allowed in Manufacturing Work Order {1}"
					).format(mwo_data.metal_type, mwo)
				)

			if (
				company_validations.get("check_touch")
				and not data.get("ignore_touch_and_purity")
				and company_validations.get("check_touch")
				in ["Both", data.get("variant")]
				and mwo_data.metal_touch != data.get("metal_touch")
			):
				mwo_errors[mwo].append("Metal Touch")

			if (
				company_validations.get("check_purity")
				and not data.get("ignore_touch_and_purity")
				and company_validations.get("check_purity")
				in ["Both", data.get("variant")]
				and mwo_data.metal_purity != data.get("metal_purity")
			):
				mwo_errors[mwo].append("Metal Purity")

			if (
				company_validations.get("check_colour")
				and company_validations.get("check_colour")
				in ["Both", data.get("variant")]
				and (mwo_data.metal_colour or "").lower()
				!= (data.get("metal_colour") or "").lower()
				and not item_flags.get(item)
			):
				mwo_errors[mwo].append("Metal Colour")

		for mop in data["mop"]:
			msl = mop if mop in msl_wise_data else msl_mop_dict.get(mop)
			if not msl:
				continue

			msl_data = msl_wise_data.get(msl)
			if not msl_data:
				continue

			if not msl_data.get("for_subcontracting"):
				msl_errors.setdefault(msl, [])

				if msl_data.metal_colour:
					if company_validations.get("check_touch") and not data.get(
						"ignore_touch_and_purity"
					):
						if msl_data.metal_touch != data.get("metal_touch"):
							msl_errors[msl].append("Metal Touch")

					if company_validations.get("check_purity") and not data.get(
						"ignore_touch_and_purity"
					):
						if msl_data.metal_purity != data.get("metal_purity"):
							msl_errors[msl].append("Metal Purity")

					if company_validations.get("check_colour"):
						if (msl_data.metal_colour or "").lower() != (
							data.get("metal_colour") or ""
						).lower() and msl_data.check_color:
							msl_errors[msl].append("Metal Colour")

	all_error_msg = []

	for k, v in mwo_errors.items():
		if v:
			all_error_msg.append(
				f"{', '.join(set(v))} do not match with Manufacturing Work Order: {k}"
			)

	for k, v in msl_errors.items():
		if v:
			all_error_msg.append(
				f"{', '.join(set(v))} do not match with Main Slip: {k}"
			)

	if all_error_msg:
		frappe.throw("<br>".join(all_error_msg))


def on_cancel(self, method=None):
	update_manufacturing_operation(self, True)
	update_main_slip(self, True)
	sync_mop_log_for_stock_entry(self, is_cancelled=True)


def before_submit(self, method):
	main_slip = self.to_main_slip or self.main_slip
	subcontractor = self.subcontractor or self.to_subcontractor
	if (
		not self.auto_created
		and self.stock_entry_type != "Manufacture"
		and (
			(
				main_slip
				and frappe.db.get_value("Main Slip", main_slip, "for_subcontracting")
			)
			or (self.manufacturing_operation and subcontractor)
		)
	):
		create_repack_for_subcontracting(self, self.subcontractor, main_slip)
	if self.stock_entry_type != "Manufacture":
		self.posting_time = frappe.utils.nowtime()


def onsubmit(self, method):
	validate_items(self)
	stock_reservation_entry_for_mwo(self)
	sync_mop_log_for_stock_entry(self)


def sync_mop_log_for_stock_entry(self, is_cancelled=False):
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

	for row in self.items:
		if not (row.get("manufacturing_operation") and row.item_code):
			continue
		if frappe.db.exists(
			"MOP Log",
			{
				"voucher_type": "Stock Entry",
				"voucher_no": self.name,
				"row_name": row.name,
				"manufacturing_operation": row.manufacturing_operation,
				"is_cancelled": 0,
			},
		):
			continue
		create_mop_log(self, row, is_synced=True)


def stock_reservation_entry_for_mwo(self):
	types_for_reservation = frappe.get_all(
		"Stock Entry Type To Reservation",
		filters={"parent": "MOP Settings"},
		pluck="stock_entry_type_to_reservation",
	)

	_eir_ref = getattr(self, "employee_ir", None)
	is_eir_injection = isinstance(_eir_ref, str) and bool(_eir_ref.strip())

	if not is_eir_injection and self.stock_entry_type not in types_for_reservation:
		return

	if not (self.manufacturing_order and self.manufacturing_work_order):
		frappe.throw(
			_("Parent Manufacturing Order and Manufacturing Work Order is required")
		)

	sales_order, sales_order_item, manufacturer = frappe.get_cached_value(
		"Parent Manufacturing Order",
		self.manufacturing_order,
		["sales_order", "sales_order_item", "manufacturer"],
	)

	base_mr_voucher_qty = frappe.db.sql(
		"""
        SELECT SUM(custom_total_quantity)
        FROM `tabMaterial Request`
        WHERE manufacturing_order=%s AND docstatus != 2
        """,
		(self.manufacturing_order,),
	)[0][0]

	if base_mr_voucher_qty:
		base_mr_voucher_qty = flt(base_mr_voucher_qty)

		tolerance = frappe.db.get_value(
			"Manufacturing Setting",
			self.manufacturer or manufacturer,
			"addition_maximum_item__tolerance_percentage",
		)

		if tolerance:
			base_mr_voucher_qty += base_mr_voucher_qty * (flt(tolerance) / 100)

	item_codes = {row.item_code for row in self.items if row.item_code}

	item_meta = {
		d.name: d
		for d in frappe.get_all(
			"Item",
			filters={"name": ["in", list(item_codes)]},
			fields=["name", "has_batch_no", "has_serial_no"],
		)
	}

	total_so_reserved = get_sre_reserved_qty_for_voucher_detail_no(
		"Sales Order", sales_order, sales_order_item
	)

	for row in self.items:
		if not row.t_warehouse:
			continue

		item_info = item_meta.get(row.item_code)
		if not item_info:
			continue

		has_batch_no = item_info.has_batch_no
		has_serial_no = item_info.has_serial_no

		if has_batch_no and row.get("batch_no"):
			available_qty = get_available_qty_to_reserve(
				row.item_code, row.t_warehouse, batch_no=row.batch_no
			)
		else:
			available_qty = get_available_qty_to_reserve(row.item_code, row.t_warehouse)

		qty_to_reserve = min(row.qty, available_qty)
		qty_to_reserve = flt(qty_to_reserve)

		if qty_to_reserve <= 0 and is_eir_injection and flt(row.qty) > 0:
			qty_to_reserve = flt(row.qty)

		if qty_to_reserve <= 0:
			continue

		effective_voucher_qty = flt(base_mr_voucher_qty) if base_mr_voucher_qty else 0

		if is_eir_injection:
			effective_voucher_qty = max(
				effective_voucher_qty,
				flt(total_so_reserved) + qty_to_reserve,
			)
		elif not effective_voucher_qty:
			effective_voucher_qty = flt(total_so_reserved) + qty_to_reserve

		doc_sre = frappe.new_doc("Stock Reservation Entry")

		doc_sre.update(
			{
				"voucher_type": "Sales Order",
				"voucher_no": sales_order,
				"item_code": row.item_code,
				"voucher_qty": effective_voucher_qty,
				"reserved_qty": qty_to_reserve,
				"company": self.company,
				"stock_uom": row.uom,
				"warehouse": row.t_warehouse,
				"manufacturing_work_order": self.manufacturing_work_order,
				"manufacturing_operation": row.manufacturing_operation,
				"voucher_detail_no": sales_order_item,
				"available_qty": max(available_qty, qty_to_reserve),
				"has_batch_no": cint(has_batch_no),
				"has_serial_no": cint(has_serial_no),
			}
		)

		if has_batch_no and row.get("batch_no"):
			doc_sre.reservation_based_on = "Serial and Batch"
			doc_sre.append(
				"sb_entries",
				{
					"batch_no": row.batch_no,
					"warehouse": row.t_warehouse,
					"qty": qty_to_reserve,
				},
			)
		else:
			doc_sre.reservation_based_on = "Qty"

		doc_sre.insert(ignore_links=True)
		doc_sre.submit()

		create_mop_log(self, row, is_synced=True)


def update_main_slip(doc, is_cancelled=False):
	msl = doc.to_main_slip or doc.main_slip
	if not msl:
		return

	ms_doc = frappe.get_doc("Main Slip", msl)

	manufacturer = doc.manufacturer or frappe.defaults.get_user_default("manufacturer")

	days = frappe.db.get_value(
		"Manufacturing Setting",
		{"manufacturer": manufacturer},
		"allowed_days_for_main_slip_issue",
	)

	if (
		not doc.auto_created
		and doc.to_main_slip
		and days is not None
		and abs(frappe.utils.date_diff(ms_doc.creation, frappe.utils.today())) > days
	):
		frappe.throw(_("Not allowed to transfer raw material in Main Slip"))

	warehouse_data = frappe._dict()

	if is_cancelled:
		se_item_names = [d.name for d in doc.items if d.name]

		existing_records = frappe.get_all(
			"Main Slip SE Details",
			filters={"se_item": ["in", se_item_names]},
			fields=["name", "se_item"],
		)

		se_map = {d.se_item: d.name for d in existing_records}

	for entry in doc.items:
		if is_cancelled:
			if entry.name in se_map:
				frappe.delete_doc("Main Slip SE Details", se_map[entry.name])

			continue

		if entry.main_slip and entry.to_main_slip:
			frappe.throw(_("Select either source or target main slip."))

		if not (entry.main_slip or entry.to_main_slip):
			continue

		entry.auto_created = doc.auto_created

		update_main_slip_se_details(
			ms_doc,
			doc.stock_entry_type,
			entry,
			warehouse_data,
			is_cancelled,
		)

	ms_doc.save()


def validate_items(self):
	if self.stock_entry_type != "Broken / Loss":
		return

	bom_items = set(
		frappe.get_all(
			"BOM Item",
			filters={"bom": self.bom_no},
			pluck="item_code",
		)
	)

	for row in self.items:
		if row.item_code not in bom_items:
			frappe.throw(f"Item {row.item_code} Not Present In BOM {self.bom_no}")


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
	if self.stock_entry_type != "Manufacture":
		return

	bom_doc = frappe.new_doc("BOM")

	items_to_manufacture = []
	raw_materials = []
	scrap_map = {}

	item_codes = [item.item_code for item in self.items]
	item_variants = {
		d.name: d.variant_of
		for d in frappe.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "variant_of"],
		)
	}

	for item in self.items:
		variant_of = item_variants.get(item.item_code)

		if not item.s_warehouse and item.t_warehouse:
			if not variant_of and item.item_code not in ["METAL LOSS", "FINDING LOSS"]:
				items_to_manufacture.append(item.item_code)
			else:
				scrap_map[item.item_code] = scrap_map.get(item.item_code, 0) + item.qty
		else:
			raw_materials.append({"item_code": item.item_code, "qty": item.qty})

	for rm in raw_materials:
		if rm["item_code"] in scrap_map:
			rm["qty"] -= scrap_map[rm["item_code"]]

	if not items_to_manufacture:
		frappe.throw("No valid item to manufacture")

	bom_doc.item = items_to_manufacture[0]

	diamond_quality = frappe.db.get_value(
		"BOM Diamond Detail", {"parent": self.bom_no}, "quality"
	)

	bom_meta = frappe.db.get_value(
		"BOM",
		self.bom_no,
		["customer", "gold_rate_with_gst", "tag_no"],
		as_dict=True,
	)

	for raw_item in raw_materials:
		qty = raw_item.get("qty") or 1

		set_item_details(
			raw_item["item_code"],
			bom_doc,
			qty,
			diamond_quality,
		)

	bom_doc.customer = bom_meta.get("customer")
	bom_doc.gold_rate_with_gst = bom_meta.get("gold_rate_with_gst")
	bom_doc.tag_no = bom_meta.get("tag_no")

	bom_doc.is_default = 0
	bom_doc.bom_type = "Finished Goods"
	bom_doc.reference_doctype = "Work Order"

	bom_doc.save(ignore_permissions=True)


def set_item_details(item_code, bom_doc, qty, diamond_quality):
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
	update_mop_details(doc, is_cancelled)


def update_mop_details(se_doc, is_cancelled=False):
	se_employee = se_doc.to_employee or se_doc.employee
	se_subcontractor = se_doc.to_subcontractor or se_doc.subcontractor

	mop_data = frappe._dict()
	warehouse_data = frappe._dict()
	batch_data = frappe._dict()

	validate_batches = se_doc.purpose != "Manufacture"

	if frappe.flags.is_finding_transfer:
		validate_batches = False

	mop_list = list(
		{
			row.manufacturing_operation
			for row in se_doc.items
			if row.manufacturing_operation
		}
	)

	if not mop_list:
		return

	mop_base_data = frappe.get_all(
		"MOP Log",
		filters={
			"manufacturing_operation": ["in", mop_list],
			"is_cancelled": 0,
		},
		fields=["manufacturing_operation", "item_code", "batch_no"],
		order_by="flow_index desc, creation desc",
	)

	for row in mop_base_data:
		key = (row.manufacturing_operation, row.item_code)
		batch_data.setdefault(key, [])
		if row.batch_no and row.batch_no not in batch_data[key]:
			batch_data[key].append(row.batch_no)

	mop_basic_details = {
		d.name: d
		for d in frappe.get_all(
			"Manufacturing Operation",
			filters={"name": ["in", mop_list]},
			fields=["name", "company", "department", "employee", "subcontractor"],
		)
	}

	if is_cancelled:
		se_names = [d.name for d in se_doc.items if d.name]

		delete_map = {}
		for doctype in [
			"Department Source Table",
			"Department Target Table",
			"Employee Source Table",
			"Employee Target Table",
		]:
			records = frappe.get_all(
				doctype,
				filters={"sed_item": ["in", se_names]},
				fields=["name", "sed_item"],
			)
			for r in records:
				delete_map.setdefault(r.sed_item, []).append((doctype, r.name))

	for entry in se_doc.items:
		mop_name = entry.manufacturing_operation
		if not mop_name:
			continue

		mop_data.setdefault(
			mop_name,
			{
				"department_source_table": [],
				"department_target_table": [],
				"employee_source_table": [],
				"employee_target_table": [],
			},
		)

		if is_cancelled:
			for doctype, docname in delete_map.get(entry.name, []):
				frappe.delete_doc(doctype, docname)
			continue

		mop_info = mop_basic_details.get(mop_name)
		if not mop_info:
			continue

		d_wh, e_wh = get_warehouse_details(
			mop_info, warehouse_data, se_employee, se_subcontractor
		)

		validated_batches = False

		temp_raw = entry.as_dict(no_nulls=True)

		if entry.s_warehouse == d_wh:
			if validate_batches and entry.batch_no:
				validate_duplicate_batches(entry, batch_data)
				validated_batches = True

			if entry.t_warehouse != entry.s_warehouse:
				mop_data[mop_name]["department_source_table"].append(temp_raw)

			if frappe.flags.is_finding_transfer:
				mop_data[mop_name]["department_target_table"].append(temp_raw)

		elif entry.t_warehouse == d_wh:
			mop_data[mop_name]["department_target_table"].append(temp_raw)

		emp_raw = temp_raw

		if entry.s_warehouse == e_wh:
			if validate_batches and entry.batch_no and not validated_batches:
				validate_duplicate_batches(entry, batch_data)

			mop_data[mop_name]["employee_source_table"].append(emp_raw)

		elif entry.t_warehouse == e_wh:
			mop_data[mop_name]["employee_target_table"].append(emp_raw)

	if (
		se_doc.stock_entry_type == "Material Transfer (WORK ORDER)"
		and not se_doc.auto_created
	):
		frappe.flags.update_pcs = 1

	update_balance_table(mop_data)


def update_balance_table(mop_data):
	if not mop_data:
		return

	mop_names = list(mop_data.keys())

	mop_docs = {
		doc.name: doc
		for doc in frappe.get_all(
			"Manufacturing Operation",
			filters={"name": ["in", mop_names]},
			fields=["name"],
		)
	}

	mop_docs = {
		name: frappe.get_doc("Manufacturing Operation", name) for name in mop_docs
	}

	for mop, tables in mop_data.items():
		mop_doc = mop_docs.get(mop)
		if not mop_doc:
			continue

		has_updates = False

		for table, details in tables.items():
			if not details:
				continue

			for row in details:
				new_row = row.copy()
				new_row.update(
					{
						"sed_item": row.get("name"),
						"idx": None,
						"name": None,
					}
				)

				mop_doc.append(table, new_row)
				has_updates = True

		if has_updates:
			mop_doc.save()


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
			},
		},
		target_doc,
		set_missing_values,
	)

	return doclist


def convert_metal_purity(from_item: dict, to_item: dict, s_warehouse, t_warehouse):
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


@frappe.whitelist()
def create_material_receipt_for_sales_person(source_name):
	source_doctype = "Stock Entry"
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

	return target_doc


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

	item_codes = {item.item_code for item in doc.items if item.item_code}

	item_map = {
		d.name: d.has_serial_no
		for d in frappe.get_all(
			"Item",
			filters={"name": ["in", list(item_codes)]},
			fields=["name", "has_serial_no"],
		)
	}

	serial_item = {}

	for item in doc.items:
		if not item_map.get(item.item_code):
			continue

		if not item.serial_no:
			continue

		serials = [s.strip() for s in item.serial_no.split("\n") if s.strip()]

		if serials:
			serial_item[item.item_code] = serials

	return serial_item


@frappe.whitelist()
def set_filter_for_main_slip(doctype, txt, searchfield, start, page_len, filters):
	mnf = filters.get("mnf")
	metal_purity = frappe.db.get_value(
		"Manufacturing Work Order", {mnf}, "metal_purity"
	)
	return metal_purity


def group_se_items_and_update_mop_items(doc, method):
	if not doc.items:
		return

	mop_items = []

	for row in doc.items:
		mop_row = row.as_dict(no_nulls=True)

		mop_row.update(
			{
				"name": None,
				"idx": None,
				"doctype": "Stock Entry MOP Item",
			}
		)

		mop_items.append(mop_row)

	doc.set("custom_mop_items", mop_items)

	doc.update_child_table("custom_mop_items")

	if doc.auto_created:
		grouped_se_items = group_se_items(mop_items)

		if grouped_se_items and len(grouped_se_items) < len(doc.items):
			new_items = []

			for row in grouped_se_items:
				new_items.append(
					{
						**row,
						"name": None,
						"idx": None,
						"doctype": "Stock Entry Detail",
					}
				)

			doc.set("items", new_items)

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
