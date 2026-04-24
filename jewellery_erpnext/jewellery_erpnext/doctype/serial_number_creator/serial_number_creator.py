# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt

import json
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import (
	cint,
	cstr,
	date_diff,
	flt,
	get_first_day,
	get_last_day,
	nowdate,
)

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	create_finished_goods_bom,
	create_manufacturing_entry,
	set_values_in_bulk,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	get_current_mop_balance_rows,
)


class SerialNumberCreator(Document):
	def validate(self):
		pass

	def on_submit(self):
		if cstr(getattr(self, "status", "")).strip() == "Pending RM Fetch":
			frappe.throw(_("Please fetch raw materials before submitting this SNC."))
		validate_qty(self)
		calulate_id_wise_sum_up(self)
		to_prepare_data_for_make_mnf_stock_entry(self)
		update_new_serial_no(self)

	@frappe.whitelist()
	def fetch_raw_materials(self):
		"""Fetch raw materials from latest MOP Log snapshot into FG tablle."""
		if self.docstatus != 0:
			frappe.throw(_("Only Draft documents can fetch raw materials."))

		mop_name = _resolve_snc_mop(self)
		if not mop_name:
			frappe.throw(
				_(
					"Manufacturing Operation is missing. Please ensure the Manufacturing Work Order has a Manufacturing Operation."
				)
			)

		mnf_qty = int(flt(_resolve_snc_mnf_qty(self)) or 0)
		if mnf_qty <= 0:
			frappe.throw(_("Invalid Manufacturing Qty on SNC."))

		# physicalize virtual movements from MWO sync if any
		_make_physical_transfer_for_synced_mop_logs(mop_name, self)

		# read current balance rows (latest per item+batch)
		balance_rows = get_current_mop_balance_rows(
			mop_name,
			include_fields=[
				"item_code",
				"batch_no",
				"qty_after_transaction_batch_based",
				"pcs_after_transaction_batch_based",
				"serial_and_batch_bundle",
				"voucher_type",
				"voucher_no",
				"row_name",
			],
		)

		stock_rows = _to_snc_stock_rows_from_mop_balance(balance_rows)
		if not stock_rows:
			frappe.throw(
				_(
					"No raw materials found in MOP Log for Manufacturing Operation {0}."
				).format(mop_name)
			)

		# reset and fill
		self.set("fg_details", [])
		self.set("source_table", [])
		_append_fg_rows_split(self, stock_rows, mnf_qty)

		# move to next status for submission
		self.status = "Ready to Submit"
		self.manufacturing_operation = mop_name
		self.save(ignore_permissions=True)
		return {"status": self.status, "items": len(self.fg_details)}

	@frappe.whitelist()
	def get_serial_summary(self):
		# Define the tables
		stock_entry = frappe.qb.DocType("Stock Entry")
		serial_no = frappe.qb.DocType("Serial No")
		bom = frappe.qb.DocType("BOM")

		# Build the query
		data = (
			frappe.qb.from_(stock_entry)
			.inner_join(serial_no)
			.on(stock_entry.name == serial_no.purchase_document_no)
			.inner_join(bom)
			.on(serial_no.name == bom.tag_no)
			.select(serial_no.purchase_document_no, serial_no.serial_no, bom.name)
			.where(stock_entry.custom_serial_number_creator == self.name)
		).run(as_dict=True)

		return frappe.render_template(
			"jewellery_erpnext/jewellery_erpnext/doctype/serial_number_creator/serial_summery.html",
			{"data": data},
		)

	@frappe.whitelist()
	def get_bom_summary(self):
		if self.design_id_bom:
			bom_data = frappe.get_doc("BOM", self.design_id_bom)
			item_records = []
			for bom_row in bom_data.items:
				item_record = {
					"item_code": bom_row.item_code,
					"qty": bom_row.qty,
					"uom": bom_row.uom,
				}
				item_records.append(item_record)
			return frappe.render_template(
				"jewellery_erpnext/jewellery_erpnext/doctype/serial_number_creator/bom_summery.html",
				{"data": item_records},
			)


def to_prepare_data_for_make_mnf_stock_entry(self):
	id_wise_data_split = {}
	for row in self.fg_details:
		if row.id:
			key = row.id
			if key not in id_wise_data_split:
				id_wise_data_split[key] = []
				id_wise_data_split[key].append(
					{
						"item_code": row.row_material,
						"qty": row.qty,
						"uom": row.uom,
						"id": row.id,
						"inventory_type": row.inventory_type,
						"customer": row.customer,
						"batch_no": row.batch_no,
						"pcs": row.pcs,
					}
				)
			else:
				id_wise_data_split[key].append(
					{
						"item_code": row.row_material,
						"qty": row.qty,
						"uom": row.uom,
						"id": row.id,
						"inventory_type": row.inventory_type,
						"customer": row.customer,
						"batch_no": row.batch_no,
						"pcs": row.pcs,
					}
				)
	for key, row_data in id_wise_data_split.items():
		pmo = frappe.db.get_value(
			"Manufacturing Work Order",
			self.manufacturing_work_order,
			"manufacturing_order",
		)

		wo = frappe.get_all(
			"Manufacturing Work Order", {"manufacturing_order": pmo}, pluck="name"
		)
		set_values_in_bulk("Manufacturing Work Order", wo, {"status": "Completed"})

		operation_data = frappe.db.get_all(
			"PMO Operation Cost",
			{"parent": pmo},
			[
				"expense_account",
				"amount",
				"exchange_rate",
				"description",
				"workstation",
				"manufacturing_operation",
				"total_minutes",
			],
		)

		se_name = create_manufacturing_entry(self, row_data, operation_data)
		self.fg_serial_no = se_name
		create_finished_goods_bom(self, se_name, operation_data)
		submit_tracking_bom_for_finished_goods(self)


def get_shift(employee, start_date, end_date):
	Attendance = frappe.qb.DocType("Attendance")

	shift = (
		frappe.qb.from_(Attendance)
		.select(Attendance.shift)
		.distinct()
		.where(
			(Attendance.employee == employee)
			& (Attendance.attendance_date.between(start_date, end_date))
			& (Attendance.shift.notnull())
		)
	).run(pluck=True)

	if shift:
		return shift[0]

	return ""


def get_hourly_rate(employee):
	hourly_rate = 0
	start_date, end_date = get_first_day(nowdate()), get_last_day(nowdate())
	shift = get_shift(employee, start_date, end_date)
	shift_hours = (
		frappe.utils.flt(frappe.db.get_value("Shift Type", shift, "shift_hours")) or 10
	)

	base = frappe.db.get_value("Employee", employee, "ctc")

	holidays = get_holidays_for_employee(employee, start_date, end_date)
	working_days = date_diff(end_date, start_date) + 1

	working_days -= len(holidays)

	total_working_days = working_days
	target_working_hours = frappe.utils.flt(shift_hours * total_working_days)

	if target_working_hours:
		hourly_rate = frappe.utils.flt(base / target_working_hours)

	return hourly_rate


def get_holidays_for_employee(employee, start_date, end_date):
	from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee
	from hrms.utils.holiday_list import get_holiday_dates_between

	HOLIDAYS_BETWEEN_DATES = "holidays_between_dates"

	holiday_list = get_holiday_list_for_employee(employee)
	key = f"{holiday_list}:{start_date}:{end_date}"
	holiday_dates = frappe.cache().hget(HOLIDAYS_BETWEEN_DATES, key)

	if not holiday_dates:
		holiday_dates = get_holiday_dates_between(holiday_list, start_date, end_date)
		frappe.cache().hset(HOLIDAYS_BETWEEN_DATES, key, holiday_dates)

	return holiday_dates


def validate_qty(self):
	for row in self.fg_details:
		if row.qty == 0:
			frappe.throw(_("FG Details Table Quantity Zero Not Allowed"))


@frappe.whitelist()
def get_operation_details(
	data, docname, mwo, pmo, company, mnf, dpt, for_fg, design_id_bom
):
	exist_snc_doc = frappe.get_all(
		"Serial Number Creator",
		filters={"manufacturing_operation": docname, "docstatus": ["!=", 2]},
		fields=["name"],
	)
	if exist_snc_doc:
		frappe.throw(f"Document Already Created...! {exist_snc_doc[0]['name']}")
	snc_doc = frappe.new_doc("Serial Number Creator")
	try:
		data_dict = json.loads(data)
	except (ValueError, TypeError):
		data_dict = data
	stock_data = data_dict[0]
	mnf_qty = int(data_dict[2])

	total_qty = data_dict[3]

	existing_se_item = {}
	item_qty = {}
	for mnf_id in range(1, mnf_qty + 1):
		for data_entry in stock_data:
			key = (data_entry["item_code"], data_entry["batch_no"])
			if not item_qty.get(key):
				item_qty.setdefault(key, 0)
				item_qty[key] += data_entry["qty"]
			_qty = flt(data_entry["qty"] / mnf_qty, 3)
			if mnf_id == mnf_qty:
				_qty = flt(item_qty[key], 3)
				item_qty[key] = 0
			else:
				item_qty[key] -= _qty
			existing_se_item.setdefault(mnf_id, [])
			if data_entry["name"] not in existing_se_item[mnf_id]:
				existing_se_item[mnf_id].append(data_entry["name"])

				snc_doc.append(
					"fg_details",
					{
						"row_material": data_entry["item_code"],
						"id": mnf_id,
						"batch_no": data_entry["batch_no"],
						"qty": _qty,
						"uom": data_entry["uom"],
						"gross_wt": data_entry["gross_wt"],
						"inventory_type": data_entry["inventory_type"],
						"sub_setting_type": data_entry.get("custom_sub_setting_type"),
						"sed_item": data_entry["name"],
						"pcs": data_entry.get("pcs"),
					},
				)

	if mnf_qty > 1:
		for data_entry in stock_data:
			snc_doc.append(
				"source_table",
				{
					"row_material": data_entry["item_code"],
					"qty": data_entry["qty"],
					"uom": data_entry["uom"],
					"pcs": data_entry.get("pcs"),
				},
			)
	snc_doc.type = "Manufacturing"
	snc_doc.manufacturing_work_order = mwo
	snc_doc.parent_manufacturing_order = pmo
	snc_doc.company = company
	snc_doc.manufacturer = mnf
	snc_doc.department = dpt
	snc_doc.for_fg = for_fg
	snc_doc.design_id_bom = design_id_bom
	snc_doc.total_weight = total_qty
	snc_doc.save()
	frappe.msgprint(
		f"<b>Serial Number Creator</b> Document Created...! <b>Doc NO:</b> {snc_doc.name}"
	)


def create_snc_from_mwo_submit(mwo_name: str) -> str:
	"""Create an SNC (Serial Number Creator) in draft when a FG MWO is submitted.

	If the linked MOP has `is_sync=1` (when such a field exists), raw materials are
	fetched immediately from latest MOP Log snapshot and the SNC becomes ready to submit.
	Otherwise, SNC is created in a pending state and the user must click Fetch Raw Materials.
	"""
	mwo = frappe.get_doc("Manufacturing Work Order", mwo_name)
	if not cint(getattr(mwo, "for_fg", 0)):
		return ""

	mop_name = cstr(getattr(mwo, "manufacturing_operation", None) or "").strip()
	if not mop_name:
		return ""

	exist_snc = frappe.db.get_value(
		"Serial Number Creator",
		{"manufacturing_work_order": mwo_name, "docstatus": ["!=", 2]},
		"name",
	)
	if exist_snc:
		return exist_snc

	pmo = frappe.db.get_value(
		"Manufacturing Work Order", mwo_name, "manufacturing_order"
	)
	snc = frappe.new_doc("Serial Number Creator")
	snc.type = "Manufacturing"
	snc.manufacturing_operation = mop_name
	snc.manufacturing_work_order = mwo_name
	snc.parent_manufacturing_order = pmo
	snc.company = mwo.company
	snc.manufacturer = mwo.manufacturer
	snc.department = mwo.department
	snc.for_fg = mwo.for_fg
	snc.design_id_bom = mwo.master_bom
	snc.total_weight = 0

	# Create the SNC in draft first
	snc.flags.ignore_mandatory = True
	snc.insert(ignore_permissions=True)

	try:
		# fetch_raw_materials handles internal saving and sets status to "Ready to Submit"
		snc.fetch_raw_materials()
	except Exception as e:
		frappe.log_error(
			title="SNC Auto Fetch Error", message=f"MWO: {mwo_name}, Error: {e}"
		)
		# If auto-fetch fails, at least ensure it's in a known state
		snc.db_set("status", "Pending RM Fetch")

	return snc.name


def calulate_id_wise_sum_up(self):
	id_qty_sum = {}  # Dictionary to store the sum of 'qty' for each 'id'
	for row in self.fg_details:
		if row.id and row.row_material:
			key = row.row_material
			if key not in id_qty_sum:
				id_qty_sum[key] = float(Decimal("0.000"))  # round(0,3)

			# if row.uom == "cts":
			# 	id_qty_sum[key] += round(row.qty * 0.2,3)
			# else:
			# id_qty_sum[key] += round(row.qty,3)
			id_qty_sum[key] += float(
				Decimal(str(row.qty)).quantize(Decimal("0.000"), rounding=ROUND_HALF_UP)
			)
	id_qty_sum = {key: round(float(value), 3) for key, value in id_qty_sum.items()}

	source_data = frappe._dict()

	for row in self.source_table:
		source_data.setdefault(row.get("row_material"), 0)
		source_data[row.row_material] += row.qty

	for (row_material), qty_sum in id_qty_sum.items():
		if source_data.get(row_material) and flt(qty_sum, 3) != flt(
			source_data.get(row_material), 3
		):
			frappe.throw(
				f"Row Material in FG Details <b>{row_material}</b> does not match </br></br>ID Wise Row Material SUM: <b>{round(qty_sum, 3)}</b></br>Must be equal of row <b>#{row.get('idx')}</b> in source table<b>: {source_data.get(row_material)}</b>"
			)


def update_new_serial_no(self):
	new_sn_doc = frappe.get_doc("Serial No", self.fg_serial_no)
	existing_huid = []
	existing_certification = []

	for row in new_sn_doc.huid:
		if row.huid and row.huid not in existing_huid:
			existing_huid.append(row.huid)

		if row.certification_no and row.certification_no not in existing_certification:
			existing_certification.append(row.certification_no)

	pmo_data = frappe.db.get_all(
		"HUID Detail",
		{"parent": self.parent_manufacturing_order},
		["huid", "date", "certification_no", "certification_date"],
	)

	item_to_add = []
	for row in pmo_data:
		if row.huid and row.huid not in existing_huid:
			duplicate_row = deepcopy(row)
			duplicate_row["name"] = None
			item_to_add.append(duplicate_row)

	for row in item_to_add:
		new_sn_doc.append(
			"huid",
			{
				"huid": row.huid,
				"date": row.date,
				"certification_no": row.certification_no,
				"certification_date": row.certification_date,
			},
		)
	new_sn_doc.save()

	if self.serial_no and self.fg_details:
		serial_doc = frappe.get_doc("Serial No", self.fg_details[0].serial_no)
		previos_sr = frappe.db.get_value(
			"Serial No",
			self.serial_no,
			[
				"purchase_document_no",
				"item_code",
				"custom_repair_type",
				"custom_product_type",
			],
			as_dict=1,
		)

		huid_details = ""
		certificate_details = ""
		for row in frappe.db.get_all("HUID Detail", {"parent": self.serial_no}, ["*"]):
			if row.huid:
				huid_details += """
								{0} - {1}""".format(row.huid, row.date)
			if row.certification_no:
				certificate_details += """
								{0} - {1}""".format(
					row.certification_no, row.certification_date
				)

		for row in frappe.db.get_all(
			"Serial No Table", {"parent": self.serial_no}, ["*"]
		):
			temp_row = deepcopy(row)
			temp_row["name"] = None
			serial_doc.append("custom_serial_no_table", temp_row)

		serial_doc.append(
			"custom_serial_no_table",
			{
				"serial_no": self.serial_no,
				"item_code": previos_sr.item_code,
				"purchase_document_no": previos_sr.purchase_document_no,
				"pmo": self.parent_manufacturing_order,
				"mwo": self.manufacturing_work_order,
				"bom": self.design_id_bom,
				"huid_details": huid_details,
				"certification_details": certificate_details,
				"repair_type": previos_sr.get("repair_type"),
				"product_type": previos_sr.get("product_type"),
			},
		)
		serial_doc.save()


def submit_tracking_bom_for_finished_goods(doc):
	"""Update and submit linked Tracking BOM when SNC creates FG BOM."""
	if not doc.get("fg_bom"):
		return

	tracking_bom_name = frappe.db.get_value(
		"Manufacturing Work Order", doc.manufacturing_work_order, "custom_tracking_bom"
	)
	if not tracking_bom_name and doc.get("parent_manufacturing_order"):
		tracking_bom_name = frappe.db.get_value(
			"Parent Manufacturing Order",
			doc.parent_manufacturing_order,
			"custom_tracking_bom",
		)
	if not tracking_bom_name:
		return

	tracking_bom = frappe.get_doc("Tracking Bom", tracking_bom_name)
	if tracking_bom.docstatus == 0:
		tracking_bom.bom_type = "Finished Goods"
		tracking_bom.reference_doctype = "BOM"
		tracking_bom.reference_docname = doc.fg_bom
		tracking_bom.flags.ignore_validate_update_after_submit = True
		tracking_bom.save(ignore_permissions=True)
		tracking_bom.submit()
	else:
		frappe.db.set_value(
			"Tracking Bom",
			tracking_bom_name,
			{
				"bom_type": "Finished Goods",
				"reference_doctype": "BOM",
				"reference_docname": doc.fg_bom,
			},
			update_modified=True,
		)


def _resolve_mwo_qty(mwo):
	# MWO.qty is the number of pieces / manufacturing qty used for SNC ID splits.
	return getattr(mwo, "qty", None)


def _resolve_snc_mnf_qty(snc_doc):
	# Prefer MWO qty if possible
	mwo_name = cstr(getattr(snc_doc, "manufacturing_work_order", None) or "").strip()
	if mwo_name:
		qty = frappe.db.get_value("Manufacturing Work Order", mwo_name, "qty")
		if qty is not None:
			return qty
	# fallback: count unique ids already in fg_details, else 1
	ids = {cstr(r.get("id")) for r in (snc_doc.get("fg_details") or []) if r.get("id")}
	return len(ids) or 1


def _resolve_snc_mop(snc_doc):
	# Prefer explicit field if present, else derive from MWO
	mop_name = cstr(getattr(snc_doc, "manufacturing_operation", None) or "").strip()
	if mop_name:
		return mop_name
	mwo_name = cstr(getattr(snc_doc, "manufacturing_work_order", None) or "").strip()
	if not mwo_name:
		return ""
	return cstr(
		frappe.db.get_value(
			"Manufacturing Work Order", mwo_name, "manufacturing_operation"
		)
		or ""
	).strip()


def _get_mop_is_sync(mop_name: str) -> int:
	"""Check if there are any non-cancelled logs for this MOP that are marked as 'is_synced'."""
	if not mop_name:
		return 0
	return (
		1
		if frappe.db.exists(
			"MOP Log",
			{"manufacturing_operation": mop_name, "is_synced": 1, "is_cancelled": 0},
		)
		else 0
	)


def _to_snc_stock_rows_from_mop_balance(balance_rows):
	"""Convert MOP balance snapshot rows into SNC stock rows format."""
	out = []
	for r in balance_rows or []:
		item_code = r.get("item_code")
		batch_no = r.get("batch_no")
		qty = flt(r.get("qty_after_transaction_batch_based") or 0)
		pcs = flt(r.get("pcs_after_transaction_batch_based") or 0)
		if qty <= 0 and pcs <= 0:
			continue

		uom = frappe.db.get_value("Item", item_code, "stock_uom") if item_code else None

		# Fetch attributes from source SED if available
		inventory_type = None
		sub_setting_type = None
		if r.get("voucher_type") == "Stock Entry" and r.get("row_name"):
			sed_data = frappe.db.get_value(
				"Stock Entry Detail",
				r.get("row_name"),
				["inventory_type", "custom_sub_setting_type"],
				as_dict=1,
			)
			if sed_data:
				inventory_type = sed_data.inventory_type
				sub_setting_type = sed_data.custom_sub_setting_type

		# Calculate gross_wt (consistent with manufacturing_operation.py)
		uom_lower = (uom or "").lower()
		is_carat = uom_lower in ["carat", "cts", "ct"]
		gross_wt = flt(qty * 0.2, 3) if is_carat else flt(qty, 3)

		out.append(
			{
				"item_code": item_code,
				"batch_no": batch_no,
				"qty": qty,
				"uom": uom,
				"pcs": pcs,
				"gross_wt": gross_wt,
				"serial_and_batch_bundle": r.get("serial_and_batch_bundle"),
				"inventory_type": inventory_type,
				"sub_setting_type": sub_setting_type,
				"sed_item": r.get("row_name"),
			}
		)
	return out


def _append_fg_rows_split(snc_doc, stock_rows, mnf_qty: int):
	"""Append `fg_details` rows by splitting each RM across 1..mnf_qty.

	Mimics existing `get_operation_details()` split behavior so downstream SNC submit
	(stock entry + FG BOM) stays consistent.
	"""
	item_qty = {}
	item_pcs = {}
	item_gross_wt = {}
	for mnf_id in range(1, int(mnf_qty) + 1):
		for data_entry in stock_rows:
			key = (data_entry.get("item_code"), data_entry.get("batch_no"))
			if key not in item_qty:
				item_qty[key] = flt(data_entry.get("qty") or 0)
				item_pcs[key] = flt(data_entry.get("pcs") or 0)
				item_gross_wt[key] = flt(data_entry.get("gross_wt") or 0)

			_qty = flt((data_entry.get("qty") or 0) / mnf_qty, 3)
			_pcs = flt((data_entry.get("pcs") or 0) / mnf_qty, 3)
			_gross_wt = flt((data_entry.get("gross_wt") or 0) / mnf_qty, 3)

			if mnf_id == mnf_qty:
				_qty = flt(item_qty[key], 3)
				_pcs = flt(item_pcs[key], 3)
				_gross_wt = flt(item_gross_wt[key], 3)
				item_qty[key] = 0
				item_pcs[key] = 0
				item_gross_wt[key] = 0
			else:
				item_qty[key] = flt(item_qty[key] - _qty, 3)
				item_pcs[key] = flt(item_pcs[key] - _pcs, 3)
				item_gross_wt[key] = flt(item_gross_wt[key] - _gross_wt, 3)

			snc_doc.append(
				"fg_details",
				{
					"row_material": data_entry.get("item_code"),
					"id": mnf_id,
					"batch_no": data_entry.get("batch_no"),
					"qty": _qty,
					"uom": data_entry.get("uom"),
					"pcs": _pcs,
					"gross_wt": _gross_wt,
					"inventory_type": data_entry.get("inventory_type"),
					"sub_setting_type": data_entry.get("sub_setting_type"),
					"sed_item": data_entry.get("sed_item"),
				},
			)

	# Always keep a source snapshot (same as existing behaviour when mnf_qty > 1)
	for data_entry in stock_rows:
		snc_doc.append(
			"source_table",
			{
				"row_material": data_entry.get("item_code"),
				"qty": data_entry.get("qty"),
				"uom": data_entry.get("uom"),
				"pcs": data_entry.get("pcs"),
			},
		)


def _make_physical_transfer_for_synced_mop_logs(mop_name, snc_doc):
	"""Identify MOP Log rows that were virtually synced but lack physical Stock Ledger entries, and move them.
	Uses the last flow index per item/batch to decide source and target warehouses."""
	# get_current_mop_balance_rows now orders by flow_index desc, creation desc
	balance_rows = get_current_mop_balance_rows(
		mop_name,
		include_fields=[
			"item_code",
			"batch_no",
			"qty_after_transaction_batch_based",
			"pcs_after_transaction_batch_based",
			"from_warehouse",
			"to_warehouse",
			"voucher_type",
			"serial_and_batch_bundle",
			"flow_index",
		],
	)

	# Physical target should be the department's manufacturing warehouse
	target_wh = frappe.db.get_value(
		"Warehouse",
		{
			"disabled": 0,
			"department": snc_doc.department,
			"warehouse_type": "Manufacturing",
		},
	)

	# Find all MWOs for the same Parent Manufacturing Order to search for physical movements
	pmo = snc_doc.parent_manufacturing_order
	all_mwos = []
	if pmo:
		all_mwos = frappe.get_all(
			"Manufacturing Work Order",
			{"manufacturing_order": pmo, "docstatus": 1},
			pluck="name",
		)

	items_to_transfer = []
	for row in balance_rows:
		# Use the latest flow index tier to decide status.
		# If it's a virtual sync log, we need a physical movement.
		if row.get("voucher_type") != "Manufacturing Work Order":
			continue

		qty = flt(row.get("qty_after_transaction_batch_based"))
		pcs = flt(row.get("pcs_after_transaction_batch_based"))
		if qty <= 0 and pcs <= 0:
			continue

		item_code = row.get("item_code")
		batch_no = row.get("batch_no")
		bundle_name = row.get("serial_and_batch_bundle")

		# INITIAL GUESS for source warehouse
		s_wh = row.get("from_warehouse")

		# SEARCH FOR PHYSICAL REALITY: Look for the last Stock Entry log for this batch in this PMO
		if all_mwos:
			physical_log = frappe.db.get_value(
				"MOP Log",
				{
					"manufacturing_work_order": ["in", all_mwos],
					"item_code": item_code,
					"batch_no": batch_no,
					"voucher_type": "Stock Entry",
					"is_cancelled": 0,
				},
				"to_warehouse",  # Where it was last physically moved TO
				order_by="flow_index desc, creation desc",
			)
			if physical_log:
				s_wh = physical_log

		# Fallback: if bundle exists, it MIGHT be right (though user error suggests it was stale)
		if (
			not s_wh
			and bundle_name
			and frappe.db.exists("Serial and Batch Bundle", bundle_name)
		):
			s_wh = frappe.db.get_value(
				"Serial and Batch Bundle", bundle_name, "warehouse"
			)

		t_wh = target_wh or row.get("to_warehouse")

		if s_wh and t_wh and s_wh != t_wh:
			row["s_wh"] = s_wh
			row["t_wh"] = t_wh
			items_to_transfer.append(row)

	if not items_to_transfer:
		return

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Material Transfer"
	se.purpose = "Material Transfer"
	se.company = snc_doc.company
	se.custom_serial_number_creator = snc_doc.name
	se.manufacturing_work_order = snc_doc.manufacturing_work_order
	se.manufacturing_operation = mop_name
	se.auto_created = 1

	for row in items_to_transfer:
		item_code = row.get("item_code")
		bundle_name = row.get("serial_and_batch_bundle")
		s_wh = row.get("s_wh")
		t_wh = row.get("t_wh")

		# Create a fresh bundle document to avoid "already used" validation errors
		new_bundle_name = None
		if bundle_name:
			try:
				old_bundle = frappe.get_doc("Serial and Batch Bundle", bundle_name)
				new_bundle = frappe.new_doc("Serial and Batch Bundle")
				new_bundle.item_code = old_bundle.item_code
				new_bundle.warehouse = s_wh
				new_bundle.type = "Outward"
				new_bundle.type_of_transaction = "Outward"
				new_bundle.voucher_type = "Stock Entry"
				new_bundle.company = old_bundle.company
				for entry in old_bundle.entries:
					new_bundle.append(
						"entries",
						{
							"batch_no": entry.batch_no,
							"qty": entry.qty,
							"serial_no": entry.serial_no,
						},
					)
				new_bundle.save()
				new_bundle_name = new_bundle.name
			except Exception as e:
				frappe.log_error(
					title="SNC Bundle Clone Error",
					message=f"MOP: {mop_name}, Item: {item_code}, Error: {e}",
				)
				new_bundle_name = None

		se.append(
			"items",
			{
				"item_code": item_code,
				"qty": flt(row.get("qty_after_transaction_batch_based")),
				"uom": frappe.db.get_value("Item", item_code, "stock_uom"),
				"s_warehouse": s_wh,
				"t_warehouse": t_wh,
				"batch_no": row.get("batch_no"),
				"serial_and_batch_bundle": new_bundle_name,
				"use_serial_batch_fields": 1,
				"manufacturing_operation": mop_name,
				"custom_manufacturing_work_order": snc_doc.manufacturing_work_order,
			},
		)

	se.flags.ignore_permissions = True
	se.save()
	se.submit()

	# Mark the virtual logs we just physicalized as synced
	mop_list = frappe.get_all(
		"Manufacturing Work Order",
		{"manufacturing_order": snc_doc.parent_manufacturing_order},
		pluck="manufacturing_operation",
	)
	if mop_list:
		frappe.db.sql(
			"""
			UPDATE `tabMOP Log`
			SET is_synced = 1
			WHERE manufacturing_operation IN %s
			  AND is_cancelled = 0
			  AND is_synced = 0
		""",
			(tuple(mop_list),),
		)

	frappe.msgprint(_("Physical Stock Transfer created: {0}").format(se.name))
	return se.name
