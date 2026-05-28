# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt

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

from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	get_current_mop_balance_rows,
)


class SerialNumberCreator(Document):
	def validate(self):
		pass

	def before_insert(self):
		validate_not_metal_only(self)
		self._render_fg_details()
		self._compute_total_weight()

	# 	if not self.fg_details:
	# 		self.load_raw_materials()

	def on_submit(self):
		validate_qty(self)
		calulate_id_wise_sum_up(self)
		to_prepare_data_for_make_mnf_stock_entry(self)
		update_new_serial_no(self)

	def _render_fg_details(self):
		"""Build source_table (batch-wise) and fg_details (aggregated) from MOP Log."""
		mop_name = self.manufacturing_operation
		mwo_name = self.manufacturing_work_order

		if not mop_name:
			if mwo_name:
				frappe.throw(
					_(
						f"Manufacturing Operation is required to render FG Details for MWO: {mwo_name}"
					)
				)
			return

		# Only auto-populate if both tables are empty (first save / draft)
		if self.fg_details or self.source_table:
			return

		# Resolve manufacturing qty (number of IDs to split across)
		mnf_qty = _resolve_snc_mnf_qty(self)
		if mnf_qty <= 0:
			return

		# Get batch-wise source rows from MOP Log
		source_rows = _get_source_raw_materials(mop_name, self)
		if not source_rows:
			return

		self.set("fg_details", [])
		self.set("source_table", [])

		# -- Source Table: batch-wise rows with full detail --
		for row in source_rows:
			self.append(
				"source_table",
				{
					"row_material": row.get("item_code"),
					"qty": row.get("qty"),
					"uom": row.get("uom"),
					"pcs": row.get("pcs"),
					"batch_no": row.get("batch_no"),
					"inventory_type": row.get("inventory_type"),
					"customer": row.get("customer"),
					"s_warehouse": row.get("s_warehouse"),
					"sub_setting_type": row.get("sub_setting_type"),
					"sed_item": row.get("sed_item"),
				},
			)

		# -- FG Details: aggregated by item_code, split across mnf_qty IDs --
		_append_fg_rows_aggregated(self, source_rows, mnf_qty)

	def _compute_total_weight(self):
		"""Auto-compute total_weight (product weight / gross weight) from fg_details."""
		total = 0
		for row in self.fg_details or []:
			if not row.row_material:
				continue
			first_char = row.row_material[0] if row.row_material else ""
			if first_char in ("D", "G"):
				# Carat items → convert to grams
				total += flt(row.qty) * 0.2
			else:
				total += flt(row.qty)
		self.total_weight = flt(total, 3)

	@frappe.whitelist()
	def get_serial_summary(self):
		# Define the tables
		stock_entry = frappe.qb.DocType("Stock Entry")
		serial_no = frappe.qb.DocType("Serial No")
		bom = frappe.qb.DocType("BOM")

		# Build the query
		data = (
			frappe.qb.from_(stock_entry)
			.left_join(serial_no)
			.on(
				(stock_entry.name == serial_no.purchase_document_no)
				| (stock_entry.name == serial_no.reference_name)
			)
			.left_join(bom)
			.on(serial_no.name == bom.tag_no)
			.select(
				stock_entry.name.as_("stock_entry"),
				serial_no.name.as_("serial_no"),
				bom.name.as_("bom_name"),
				serial_no.purchase_document_no,
				serial_no.reference_name,
			)
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
	"""Use source_table (batch-wise) for stock entry creation.

	source_table has one row per (item_code, batch_no) with all batch detail
	needed for the manufacturing stock entry (s_warehouse, inventory_type, etc.).
	fg_details is kept for BOM creation (aggregated item/qty/pcs).
	"""

	# Build row_data from source_table (batch-wise) for stock entry
	row_data = []
	for row in self.source_table:
		row_data.append(
			{
				"item_code": row.row_material,
				"qty": row.qty,
				"uom": row.uom,
				"id": 1,  # single FG item
				"inventory_type": row.inventory_type,
				"customer": row.customer,
				"batch_no": row.batch_no,
				"pcs": row.pcs,
				"s_warehouse": row.s_warehouse,
				"sub_setting_type": row.sub_setting_type,
			}
		)

	pmo = frappe.db.get_value(
		"Manufacturing Work Order",
		self.manufacturing_work_order,
		"manufacturing_order",
	)

	operation_data = frappe.get_all(
		"Manufacturing Operation",
		{"manufacturing_order": pmo, "docstatus": ["!=", 2]},
		["name as manufacturing_operation", "employee", "total_minutes", "operation"],
	)

	if row_data:
		for row in row_data:
			if row.get("s_warehouse"):
				pmo = frappe.db.get_value(
					"Manufacturing Work Order",
					self.manufacturing_work_order,
					"manufacturing_order",
				)
				# sales_order = frappe.db.get_value(
				# 	"Parent Manufacturing Order", pmo, "sales_order"
				# )

				sre_reserved_qty_total = 0.0

				# ── PRIORITY 1: Product Certification Receive ──
				# If PC happened before SNC, the SREs are already cancelled
				# and the stock sits at the PC Receive t_warehouse.
				# Check this FIRST as the authoritative source.
				pc_receive_data = frappe.db.sql(
					"""
					SELECT se_item.t_warehouse, se_item.qty
					FROM `tabStock Entry` se
					JOIN `tabStock Entry Detail` se_item ON se.name = se_item.parent
					JOIN `tabProduct Certification` pc ON se.product_certification = pc.name
					WHERE pc.type = 'Receive'
					  AND se.docstatus = 1
					  AND EXISTS(
					      SELECT 1 FROM `tabProduct Details` pd
					      WHERE pd.parent = pc.name
					        AND (pd.manufacturing_work_order = %(mwo)s
					             OR pd.parent_manufacturing_order = %(pmo)s)
					  )
					  AND se_item.item_code = %(item_code)s
					ORDER BY se.creation DESC LIMIT 1
				""",
					{
						"mwo": self.manufacturing_work_order,
						"pmo": pmo,
						"item_code": row["item_code"],
					},
					as_dict=1,
				)

				if pc_receive_data:
					sre_reserved_qty_total = flt(pc_receive_data[0].qty)
					row["s_warehouse"] = pc_receive_data[0].t_warehouse
					# Persist the corrected warehouse back to source_table
					for st_row in self.source_table:
						if st_row.row_material == row[
							"item_code"
						] and st_row.batch_no == row.get("batch_no"):
							st_row.s_warehouse = row["s_warehouse"]
							st_row.db_set(
								"s_warehouse",
								row["s_warehouse"],
							)
					frappe.logger().info(
						f"SNC {self.name}: PC Receive — "
						f"Item: {row['item_code']}, "
						f"Warehouse: {row['s_warehouse']}, "
						f"Qty: {sre_reserved_qty_total} "
						f"(Using Product Certification Receive as primary source)"
					)
				else:
					# ── PRIORITY 2: SRE cancellation ──
					# No PC exists — resolve via Stock Reservation Entries
					# First, find all MWOs that consumed this item/batch via MOP Log
					mwo_prefix = (
						self.manufacturing_work_order.rsplit("-", 1)[0] + "%"
						if self.manufacturing_work_order
						else "%"
					)
					consumed_mwos = frappe.db.sql(
						"""
						SELECT DISTINCT manufacturing_work_order
						FROM `tabMOP Log`
						WHERE item_code = %s AND batch_no = %s
						  AND (manufacturing_work_order = %s OR manufacturing_work_order LIKE %s)
						  AND is_cancelled = 0
					""",
						(
							row["item_code"],
							row.get("batch_no"),
							self.manufacturing_work_order,
							mwo_prefix,
						),
						as_list=1,
					)

					search_mwos = [self.manufacturing_work_order]
					for m in consumed_mwos:
						if m[0] and m[0] not in search_mwos:
							search_mwos.append(m[0])

					linked_sres = frappe.get_all(
						"Stock Reservation Entry",
						filters={
							"item_code": row["item_code"],
							"docstatus": 1,
							"manufacturing_work_order": ["in", search_mwos],
						},
						pluck="name",
					)

					# Deduplicate and cancel SREs while preventing TimestampMismatchError on Bins
					for sre_name in set(linked_sres):
						frappe.clear_document_cache("Bin")
						sre_doc = frappe.get_doc("Stock Reservation Entry", sre_name)
						sre_reserved_qty_total += flt(sre_doc.reserved_qty)

						# Capture warehouse from SRE if available
						if sre_doc.warehouse:
							row["s_warehouse"] = sre_doc.warehouse

						sre_doc.flags.ignore_permissions = True
						sre_doc.cancel()
						frappe.clear_document_cache("Bin")

					# Persist the corrected SRE warehouse back to source_table
					if linked_sres:
						for st_row in self.source_table:
							if st_row.row_material == row[
								"item_code"
							] and st_row.batch_no == row.get("batch_no"):
								st_row.s_warehouse = row["s_warehouse"]
								st_row.db_set(
									"s_warehouse",
									row["s_warehouse"],
								)

				loss_qty = sre_reserved_qty_total - flt(row["qty"])

				if loss_qty > 0:
					variant_of = frappe.db.get_value(
						"Item", row["item_code"], "variant_of"
					)
					loss_warehouse = None
					variant_loss_details = frappe.db.get_value(
						"Variant Loss Warehouse",
						{
							"parent": self.manufacturer,
							"variant": variant_of or row["item_code"],
						},
						[
							"loss_warehouse",
							"consider_department_warehouse",
							"warehouse_type",
						],
						as_dict=1,
					)

					if variant_loss_details:
						if variant_loss_details.get("loss_warehouse"):
							loss_warehouse = variant_loss_details.get("loss_warehouse")
						elif variant_loss_details.get(
							"consider_department_warehouse"
						) and variant_loss_details.get("warehouse_type"):
							loss_warehouse = frappe.db.get_value(
								"Warehouse",
								{
									"disabled": 0,
									"department": self.department,
									"warehouse_type": variant_loss_details.get(
										"warehouse_type"
									),
								},
							)

					if loss_warehouse:
						# Duplicate guard: skip if a Repack entry already exists for this SNC + item
						existing_loss_se = frappe.db.sql(
							"""
							SELECT se.name
							FROM `tabStock Entry` se
							JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
							WHERE se.custom_serial_number_creator = %s
							  AND se.stock_entry_type = 'Repack'
							  AND se.docstatus != 2
							  AND sed.item_code = %s
							LIMIT 1
							""",
							(self.name, row["item_code"]),
						)
						if existing_loss_se:
							frappe.msgprint(
								_(
									"Repack (Loss) Stock Entry already exists for {0}"
								).format(row["item_code"])
							)
						else:
							se_loss = frappe.new_doc("Stock Entry")
							se_loss.stock_entry_type = "Repack"
							se_loss.purpose = "Repack"
							se_loss.company = self.company
							se_loss.custom_serial_number_creator = self.name
							se_loss.append(
								"items",
								{
									"item_code": row["item_code"],
									"qty": loss_qty,
									"s_warehouse": row["s_warehouse"],
									"t_warehouse": loss_warehouse,
									"batch_no": row.get("batch_no"),
									"use_serial_batch_fields": 1,
								},
							)
							se_loss.insert(ignore_permissions=True)
							se_loss.submit()

				frappe.clear_cache()

		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			create_finished_goods_bom,
			create_manufacturing_entry,
		)

		se_name = create_manufacturing_entry(self, row_data, operation_data)

		self.fg_serial_no = se_name
		frappe.db.set_value(
			self.doctype,
			self.name,
			"fg_serial_no",
			se_name,
			update_modified=False,
		)
		create_finished_goods_bom(self, se_name, operation_data)
		submit_tracking_bom_for_finished_goods(self)

	if pmo:
		from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
			set_values_in_bulk,
		)

		wo_list = frappe.get_all(
			"Manufacturing Work Order", {"manufacturing_order": pmo}, pluck="name"
		)
		set_values_in_bulk("Manufacturing Work Order", wo_list, {"status": "Completed"})

		# Mark all relevant virtual logs as synced now that they are physically consumed
		mop_names = [
			d.manufacturing_operation
			for d in operation_data
			if d.manufacturing_operation
		]
		if mop_names:
			frappe.db.sql(
				"""
				UPDATE `tabMOP Log`
				SET is_synced = 1
				WHERE manufacturing_operation IN %s
				  AND is_cancelled = 0
				  AND is_synced = 0
			""",
				(tuple(mop_names),),
			)


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


def validate_not_metal_only(doc):
	"""Prevent SNC submission when only metal items exist in source_table.

	A finished jewellery piece must contain additional materials (diamond,
	gemstone, finding, etc.) beyond just metal. This validation ensures
	incomplete compositions are caught before stock entries are created.
	"""
	has_metal = False
	has_non_metal = False
	for row in doc.source_table:
		if not row.row_material:
			continue
		qty = flt(row.qty)
		if qty <= 0:
			continue
		item_group = frappe.db.get_value("Item", row.row_material, "item_group") or ""
		if "Metal" in item_group:
			has_metal = True
		else:
			has_non_metal = True

	if has_metal and not has_non_metal:
		frappe.throw(
			_(
				"Submission not allowed. Only metal details are available. "
				"Additional manufacturing details (diamond, gemstone, finding) "
				"are required before submission."
			)
		)


def validate_qty(self):
	for row in self.source_table:
		if row.qty <= 0:
			frappe.throw(_("Source Table Quantity Zero or Negative Not Allowed"))
	for row in self.fg_details:
		if row.qty == 0:
			frappe.throw(_("FG Details Table Quantity Zero Not Allowed"))


@frappe.whitelist()
def get_operation_details(
	mwo,
	pmo,
	docname=None,
	company=None,
	mnf=None,
	dpt=None,
	for_fg=None,
	design_id_bom=None,
):
	exist_snc_doc = frappe.get_all(
		"Serial Number Creator",
		filters={"manufacturing_operation": docname, "docstatus": ["!=", 2]},
		fields=["name"],
	)
	if exist_snc_doc:
		frappe.throw(f"Document Already Created...! {exist_snc_doc[0]['name']}")
	snc_doc = frappe.new_doc("Serial Number Creator")
	snc_doc.type = "Manufacturing"
	snc_doc.manufacturing_work_order = mwo
	snc_doc.manufacturing_operation = docname
	snc_doc.parent_manufacturing_order = pmo
	snc_doc.company = company
	snc_doc.manufacturer = mnf
	snc_doc.department = dpt
	snc_doc.for_fg = for_fg
	snc_doc.design_id_bom = design_id_bom

	snc_doc.save(ignore_permissions=True)

	frappe.msgprint(
		f"<b>Serial Number Creator</b> Document Created...! <b>Doc NO:</b> {snc_doc.name}"
	)
	return snc_doc.name


def create_snc_from_mwo_submit(mwo_name: str) -> str:
	"""Automatically create SNC when a Work Order is submitted for the Serial No department."""
	mwo = frappe.get_doc("Manufacturing Work Order", mwo_name)
	if not cint(getattr(mwo, "for_fg", 0)):
		return ""

	# Check if SNC already exists for this MWO
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

	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_current_mop_balance_rows,
	)

	balance_rows = get_current_mop_balance_rows(
		mop_name,
		include_fields=[
			"item_code",
			"qty_after_transaction_batch_based",
			"pcs_after_transaction_batch_based",
		],
	)

	has_metal = False
	has_non_metal = False

	if balance_rows:
		for r in balance_rows:
			item_code = r.get("item_code")
			qty = flt(r.get("qty_after_transaction_batch_based") or 0)
			pcs = flt(r.get("pcs_after_transaction_batch_based") or 0)
			if qty <= 0 and pcs <= 0:
				continue
			item_group = frappe.db.get_value("Item", item_code, "item_group") or ""
			if "Metal" in item_group:
				has_metal = True
			else:
				has_non_metal = True

		if has_metal and not has_non_metal:
			frappe.throw(
				_(
					"Only metal details available. Cannot create SNC because metal definitely combines with any of other items like diamond, gemstone, finding."
				)
			)

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

	snc.flags.ignore_mandatory = True
	snc.insert(ignore_permissions=True)

	return snc.name


def calulate_id_wise_sum_up(self):
	"""Validate that fg_details totals per item match source_table totals per item.

	fg_details has aggregated qty per item_code (no batch split).
	source_table has batch-wise qty per (item_code, batch_no).
	The sum of qty per item_code in both tables must match.
	"""
	# Sum qty per item in fg_details
	fg_qty_sum = {}
	for row in self.fg_details:
		if row.row_material:
			key = row.row_material
			if key not in fg_qty_sum:
				fg_qty_sum[key] = float(Decimal("0.000"))
			fg_qty_sum[key] += float(
				Decimal(str(row.qty)).quantize(Decimal("0.000"), rounding=ROUND_HALF_UP)
			)
	fg_qty_sum = {key: round(float(value), 3) for key, value in fg_qty_sum.items()}

	# Sum qty per item in source_table (batch-wise rows aggregated by item)
	source_data = frappe._dict()
	for row in self.source_table:
		source_data.setdefault(row.get("row_material"), 0)
		source_data[row.row_material] += row.qty

	for row_material, qty_sum in fg_qty_sum.items():
		src_qty = flt(source_data.get(row_material), 3)
		if src_qty and flt(qty_sum, 3) != src_qty:
			frappe.throw(
				f"Row Material in FG Details <b>{row_material}</b> does not match </br></br>"
				f"FG Details SUM: <b>{round(qty_sum, 3)}</b></br>"
				f"Source Table SUM: <b>{src_qty}</b>"
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


# def _resolve_mwo_qty(mwo):
# 	# MWO.qty is the number of pieces / manufacturing qty used for SNC ID splits.
# 	return getattr(mwo, "qty", None)


# def _resolve_snc_mnf_qty(snc_doc):
# 	# Prefer MWO qty if possible
# 	mwo_name = cstr(getattr(snc_doc, "manufacturing_work_order", None) or "").strip()
# 	if mwo_name:
# 		qty = frappe.db.get_value("Manufacturing Work Order", mwo_name, "qty")
# 		if qty is not None:
# 			return qty

# 	ids = {cstr(r.get("id")) for r in (snc_doc.get("fg_details") or []) if r.get("id")}
# 	return len(ids) or 1


# def _resolve_snc_mop(snc_doc):
# 	# Prefer explicit field if present, else derive from MWO
# 	mop_name = cstr(getattr(snc_doc, "manufacturing_operation", None) or "").strip()
# 	if mop_name:
# 		return mop_name
# 	mwo_name = cstr(getattr(snc_doc, "manufacturing_work_order", None) or "").strip()
# 	if not mwo_name:
# 		return ""
# 	return cstr(
# 		frappe.db.get_value(
# 			"Manufacturing Work Order", mwo_name, "manufacturing_operation"
# 		)
# 		or ""
# 	).strip()


# def _get_mop_is_sync(mop_name: str) -> int:
# 	"""Check if there are any non-cancelled logs for this MOP that are marked as 'is_synced'."""
# 	if not mop_name:
# 		return 0
# 	return (
# 		1
# 		if frappe.db.exists(
# 			"MOP Log",
# 			{"manufacturing_operation": mop_name, "is_synced": 1, "is_cancelled": 0},
# 		)
# 		else 0
# 	)


def _get_source_raw_materials(mop_name, snc_doc):
	"""Get batch-wise source raw materials from MOP Log for a Manufacturing Operation.

	Monitors all MOP Log flow_index entries to capture intermediate Stock Entry
	additions. Checks Stock Reservation Entry for Sales Order warehouse.

	Returns a list of dicts with: item_code, batch_no, qty, uom, pcs,
	inventory_type, customer, s_warehouse, sub_setting_type, sed_item.
	"""
	if not mop_name:
		return []

	# Get current balance rows from MOP Log (latest per item/batch)
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
			"from_warehouse",
			"to_warehouse",
			"manufacturing_work_order",
			"flow_index",
		],
	)
	if not balance_rows:
		return []

	# Resolve PMO and Sales Order for SRE lookup
	mwo_name = cstr(getattr(snc_doc, "manufacturing_work_order", None) or "").strip()
	pmo = None
	sales_order = None
	if mwo_name:
		pmo = frappe.db.get_value(
			"Manufacturing Work Order", mwo_name, "manufacturing_order"
		)
	if pmo:
		sales_order = frappe.db.get_value(
			"Parent Manufacturing Order", pmo, "sales_order"
		)

	# Get all MWOs for the PMO (for physical warehouse fallback)
	# all_mwos = []
	# if pmo:
	# 	all_mwos = frappe.get_all(
	# 		"Manufacturing Work Order",
	# 		{"manufacturing_order": pmo, "docstatus": 1},
	# 		pluck="name",
	# 	)

	out = []
	for r in balance_rows:
		item_code = r.get("item_code")
		batch_no = r.get("batch_no")
		qty = flt(r.get("qty_after_transaction_batch_based") or 0)
		pcs = flt(r.get("pcs_after_transaction_batch_based") or 0)
		if qty <= 0 and pcs <= 0:
			continue

		uom = frappe.db.get_value("Item", item_code, "stock_uom") if item_code else None

		# Fetch attributes from source Stock Entry Detail if available
		sub_setting_type = None
		inventory_type = None
		customer = None
		if r.get("voucher_type") == "Stock Entry" and r.get("row_name"):
			sed_data = frappe.db.get_value(
				"Stock Entry Detail",
				r.get("row_name"),
				["inventory_type", "custom_sub_setting_type", "customer"],
				as_dict=1,
			)
			if sed_data and r.get("voucher_type") == "Stock Entry":
				sub_setting_type = sed_data.custom_sub_setting_type
				inventory_type = sed_data.inventory_type
				customer = sed_data.customer

		s_wh = resolve_and_validate(
			item_code=item_code,
			qty=qty,
			batch_no=batch_no,
			sales_order=sales_order,
			mwo=mwo_name,
			mop=mop_name,
		)

		if not s_wh:
			s_wh = r.get("to_warehouse")

		if not s_wh:
			s_wh = r.get("to_warehouse")

		out.append(
			{
				"item_code": item_code,
				"batch_no": batch_no,
				"qty": qty,
				"uom": uom,
				"pcs": pcs,
				"inventory_type": inventory_type,
				"customer": customer,
				"sub_setting_type": sub_setting_type,
				"sed_item": r.get("row_name")
				if r.get("voucher_type") == "Stock Entry"
				else None,
				"s_warehouse": s_wh or r.get("to_warehouse"),
				"serial_and_batch_bundle": r.get("serial_and_batch_bundle"),
			}
		)
	return out


def _resolve_snc_mnf_qty(snc_doc):
	"""Resolve the manufacturing quantity for SNC ID splits.

	Prefers MWO qty if available, otherwise defaults to 1.
	"""
	mwo_name = cstr(getattr(snc_doc, "manufacturing_work_order", None) or "").strip()
	if mwo_name:
		qty = frappe.db.get_value("Manufacturing Work Order", mwo_name, "qty")
		if qty is not None:
			return int(flt(qty)) or 1
	return 1


def _append_fg_rows_aggregated(snc_doc, source_rows, mnf_qty: int):
	"""Append fg_details rows aggregated by item_code (no batch splitting).

	Each unique item_code gets one row per mnf_id with qty/pcs split evenly.
	The last ID gets the remainder to avoid rounding errors.
	"""
	# Aggregate by item_code
	item_agg = {}
	for row in source_rows:
		key = row.get("item_code")
		if key not in item_agg:
			item_agg[key] = {
				"qty": 0,
				"pcs": 0,
				"uom": row.get("uom"),
				"sub_setting_type": row.get("sub_setting_type"),
			}
		item_agg[key]["qty"] += flt(row.get("qty") or 0)

		# Diamond/Gemstone items (prefix D or G): each batch is a distinct physical
		# stone, so pcs should be summed across batches.
		# Metal and all other items: multiple batches are weight splits of the SAME
		# physical piece, so use max() to avoid double-counting the pcs.
		first_char = (key or "")[0].upper() if key else ""
		if first_char in ("D", "G"):
			item_agg[key]["pcs"] += flt(row.get("pcs") or 0)
		else:
			item_agg[key]["pcs"] = max(item_agg[key]["pcs"], flt(row.get("pcs") or 0))

	# Split across mnf_qty IDs
	for mnf_id in range(1, int(mnf_qty) + 1):
		for item_code, agg in item_agg.items():
			total_qty = flt(agg["qty"])
			total_pcs = flt(agg["pcs"])

			_qty = flt(total_qty / mnf_qty, 3)
			_pcs = flt(total_pcs / mnf_qty, 3)

			if mnf_id == mnf_qty:
				# Last ID gets remainder
				already_allocated_qty = flt(_qty * (mnf_qty - 1), 3)
				already_allocated_pcs = flt(_pcs * (mnf_qty - 1), 3)
				_qty = flt(total_qty - already_allocated_qty, 3)
				_pcs = flt(total_pcs - already_allocated_pcs, 3)

			snc_doc.append(
				"fg_details",
				{
					"row_material": item_code,
					"id": mnf_id,
					"qty": _qty,
					"uom": agg["uom"],
					"pcs": _pcs,
					"sub_setting_type": agg.get("sub_setting_type"),
				},
			)


def get_correct_source_warehouse(
	item_code, batch_no=None, sales_order=None, mwo=None, mop=None
):
	"""Priority-based warehouse resolution from SREs."""

	# Priority 1: SRE for Sales Order (Submitted)
	if sales_order:
		if batch_no:
			wh = frappe.db.sql(
				"""
				SELECT sre.warehouse
				FROM `tabSerial and Batch Entry` sbe
				JOIN `tabStock Reservation Entry` sre ON sre.name = sbe.parent
				WHERE sbe.parenttype = 'Stock Reservation Entry'
				  AND sbe.batch_no = %s
				  AND sre.item_code = %s
				  AND sre.voucher_no = %s
				  AND sre.docstatus = 1
				LIMIT 1
			""",
				(batch_no, item_code, sales_order),
			)
			if wh:
				return wh[0][0], "SRE"

		wh = frappe.db.get_value(
			"Stock Reservation Entry",
			{"item_code": item_code, "voucher_no": sales_order, "docstatus": 1},
			"warehouse",
		)
		if wh:
			return wh, "SRE"

	# Priority 2: Other specific links (MWO, MOP) (Submitted)
	for field, val in [
		("manufacturing_work_order", mwo),
		("manufacturing_operation", mop),
	]:
		if not val:
			continue
		if batch_no:
			wh = frappe.db.sql(
				f"""
				SELECT sre.warehouse
				FROM `tabSerial and Batch Entry` sbe
				JOIN `tabStock Reservation Entry` sre ON sre.name = sbe.parent
				WHERE sbe.parenttype = 'Stock Reservation Entry'
				  AND sbe.batch_no = %s
				  AND sre.item_code = %s
				  AND sre.{field} = %s
				  AND sre.docstatus = 1
				LIMIT 1
			""",
				(batch_no, item_code, val),
			)
			if wh:
				return wh[0][0], "SRE"

		wh = frappe.db.get_value(
			"Stock Reservation Entry",
			{"item_code": item_code, field: val, "docstatus": 1},
			"warehouse",
		)
		if wh:
			return wh, "SRE"

	# Priority 2.5: Product Certification Receive warehouse
	# When PC happens before SNC, SREs are cancelled during PC Issue and
	# stock is moved to a WIP warehouse, then back to the department
	# warehouse via PC Receive. The PC Receive t_warehouse is the
	# definitive location of the stock after certification.
	if mwo:
		pmo_for_pc = frappe.db.get_value(
			"Manufacturing Work Order", mwo, "manufacturing_order"
		)
		if pmo_for_pc:
			pc_wh = frappe.db.sql(
				"""
				SELECT se_item.t_warehouse
				FROM `tabStock Entry` se
				JOIN `tabStock Entry Detail` se_item ON se.name = se_item.parent
				JOIN `tabProduct Certification` pc ON se.product_certification = pc.name
				WHERE pc.type = 'Receive'
				  AND se.docstatus = 1
				  AND EXISTS(
				      SELECT 1 FROM `tabProduct Details` pd
				      WHERE pd.parent = pc.name
				        AND (pd.manufacturing_work_order = %s
				             OR pd.parent_manufacturing_order = %s)
				  )
				  AND se_item.item_code = %s
				ORDER BY se.creation DESC LIMIT 1
				""",
				(mwo, pmo_for_pc, item_code),
			)
			if pc_wh:
				return pc_wh[0][0], "PC_RECEIVE"

	# Priority 3: Cancelled SRE trace (Recently released stock)
	if sales_order:
		if batch_no:
			wh = frappe.db.sql(
				"""
				SELECT sre.warehouse
				FROM `tabSerial and Batch Entry` sbe
				JOIN `tabStock Reservation Entry` sre ON sre.name = sbe.parent
				WHERE sbe.parenttype = 'Stock Reservation Entry'
				  AND sbe.batch_no = %s
				  AND sre.item_code = %s
				  AND sre.voucher_no = %s
				  AND sre.docstatus = 2
				LIMIT 1
			""",
				(batch_no, item_code, sales_order),
			)
			if wh:
				return wh[0][0], "SRE"

		wh = frappe.db.get_value(
			"Stock Reservation Entry",
			{"item_code": item_code, "voucher_no": sales_order, "docstatus": 2},
			"warehouse",
		)
		if wh:
			return wh, "SRE"

	# Priority 4: Latest Stock Movement (Fallback if no reservation exists)
	sle_wh = frappe.db.sql(
		"""SELECT warehouse FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND (batch_no=%s OR %s IS NULL) AND is_cancelled=0
		ORDER BY posting_date DESC, posting_time DESC, creation DESC LIMIT 1""",
		(item_code, batch_no, batch_no),
	)
	if sle_wh:
		return sle_wh[0][0], "SLE"

	return None, None


def resolve_and_validate(
	item_code, qty, batch_no=None, sales_order=None, mwo=None, mop=None
):
	"""Combined resolution + stock validation with auto-recovery."""
	wh, source_type = get_correct_source_warehouse(
		item_code, batch_no, sales_order, mwo, mop
	)

	if not wh:
		return None

	# If resolved via SRE or PC Receive, we trust it as the source warehouse
	if source_type in ("SRE", "PC_RECEIVE"):
		return wh

	def get_available_qty(w):
		bin_data = frappe.db.get_value(
			"Bin",
			{"item_code": item_code, "warehouse": w},
			["actual_qty", "reserved_stock"],
			as_dict=1,
		)
		if not bin_data:
			return 0

		return flt(bin_data.actual_qty) - flt(bin_data.reserved_stock)

	if get_available_qty(wh) >= flt(qty):
		return wh

	# Search for any warehouse that has enough AVAILABLE stock of this BATCH
	if batch_no:
		alt_batch = frappe.db.sql(
			"""
			SELECT warehouse, SUM(actual_qty) as total_qty
			FROM `tabStock Ledger Entry`
			WHERE item_code = %s AND batch_no = %s AND is_cancelled = 0
			GROUP BY warehouse
			HAVING total_qty >= %s
			ORDER BY total_qty DESC
			LIMIT 1
		""",
			(item_code, batch_no, qty),
			as_dict=True,
		)
		if alt_batch:
			return alt_batch[0].warehouse

	# Fallback to any warehouse with available ITEM stock
	alt = frappe.db.sql(
		"""SELECT warehouse FROM `tabBin`
		WHERE item_code=%s AND (actual_qty - reserved_stock) >= %s
		ORDER BY (actual_qty - reserved_stock) DESC LIMIT 1""",
		(item_code, qty),
	)
	if alt:
		return alt[0][0]

	return wh  # Fallback to original even if short
