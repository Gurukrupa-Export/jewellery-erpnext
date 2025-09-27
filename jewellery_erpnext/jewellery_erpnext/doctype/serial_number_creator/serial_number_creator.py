# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt

import json
from copy import deepcopy

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import date_diff, flt, get_first_day, get_last_day, nowdate, time_diff

from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	create_finished_goods_bom,
	create_manufacturing_entry,
	set_values_in_bulk,
)


class SerialNumberCreator(Document):
	def validate(self):
		pass

	def on_submit(self):
		validate_qty(self)
		calulate_id_wise_sum_up(self)
		to_prepare_data_for_make_mnf_stock_entry(self)
		update_new_serial_no(self)

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
				item_record = {"item_code": bom_row.item_code, "qty": bom_row.qty, "uom": bom_row.uom}
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
			"Manufacturing Work Order", self.manufacturing_work_order, "manufacturing_order"
		)

		wo = frappe.get_all("Manufacturing Work Order", {"manufacturing_order": pmo}, pluck="name")
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
	shift_hours = frappe.utils.flt(frappe.db.get_value("Shift Type", shift, "shift_hours")) or 10

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
def get_operation_details(data, docname, mwo, pmo, company, mnf, dpt, for_fg, design_id_bom):
	exist_snc_doc = frappe.get_all(
		"Serial Number Creator",
		filters={"manufacturing_operation": docname, "docstatus": ["!=", 2]},
		fields=["name"],
	)
	if exist_snc_doc:
		frappe.throw(f"Document Already Created...! {exist_snc_doc[0]['name']}")
	snc_doc = frappe.new_doc("Serial Number Creator")
	mnf_op_doc = frappe.get_doc("Manufacturing Operation", docname)
	# data_dict = json.loads(data)
	# New Code
	try:
		data_dict = json.loads(data)
	except:
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
						"qty": _qty,  # data_entry["qty"],
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
					"pcs": data_entry.get("pcs")
					# "id": mnf_id,
					# "batch_no": data_entry["batch_no"],
					# "gross_wt": data_entry["gross_wt"],
				},
			)
	snc_doc.type = "Manufacturing"
	# snc_doc.manufacturing_operation = mnf_op_doc.name
	snc_doc.manufacturing_work_order = mwo
	snc_doc.parent_manufacturing_order = pmo
	snc_doc.company = company
	snc_doc.manufacturer = mnf
	snc_doc.department = dpt
	snc_doc.for_fg = for_fg
	snc_doc.design_id_bom = design_id_bom
	snc_doc.total_weight = total_qty
	snc_doc.save()
	# mnf_op_doc.status = "Finished"
	# mnf_op_doc.save()
	frappe.msgprint(
		f"<b>Serial Number Creator</b> Document Created...! <b>Doc NO:</b> {snc_doc.name}"
	)


from decimal import ROUND_HALF_UP, Decimal


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
		if source_data.get(row_material) and flt(qty_sum, 3) != flt(source_data.get(row_material), 3):
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
			["purchase_document_no", "item_code", "custom_repair_type", "custom_product_type"],
			as_dict=1,
		)

		huid_details = ""
		certificate_details = ""
		for row in frappe.db.get_all("HUID Detail", {"parent": self.serial_no}, ["*"]):
			if row.huid:
				huid_details += """
								{0} - {1}""".format(
					row.huid, row.date
				)
			if row.certification_no:
				certificate_details += """
								{0} - {1}""".format(
					row.certification_no, row.certification_date
				)

		for row in frappe.db.get_all("Serial No Table", {"parent": self.serial_no}, ["*"]):
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
