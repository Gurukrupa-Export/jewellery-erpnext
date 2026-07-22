# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order import (
	make_subcontracting_order,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mould.doc_events.utils import (
	get_mould_id_map,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
	create_mwo,
	make_manufacturing_order,
)


class ManufacturingPlan(Document):
	def on_cancel(self):
		frappe.db.sql(
			"""
            UPDATE `tabQuotation Item` qi
            JOIN `tabManufacturing Plan Table` mpt
                ON qi.name = mpt.quotation_item
            JOIN `tabManufacturing Plan` mp
                ON (mpt.parent = mp.name AND mp.name = %(mp_name)s)
            JOIN `tabQuotation` q
				ON (qi.parent = q.name AND q.docstatus = 1)
            SET
                qi.manufacturing_order_qty =
                    COALESCE(qi.manufacturing_order_qty, 0)
                    - COALESCE(mpt.manufacturing_order_qty, 0)
                    - COALESCE(mpt.subcontracting_qty, 0),
                q.modified = NOW(),
                q.modified_by = %(modified_by)s
        """,
			{
				"mp_name": self.name,
				"modified_by": frappe.session.user,
			},
		)

	def on_submit(self):
		is_subcontracting = False
		# customer_diamond_data removed for memory efficiency
		frappe.db.sql(
			"""
					UPDATE `tabQuotation Item` qi
					JOIN `tabManufacturing Plan Table` mpt
						ON qi.name = mpt.quotation_item
					JOIN `tabManufacturing Plan` mp
						ON (mpt.parent = mp.name AND mp.name = %(mp_name)s)
					JOIN `tabQuotation` q
						ON (qi.parent = q.name AND q.docstatus = 1)
					SET
						qi.manufacturing_order_qty =
							COALESCE(qi.manufacturing_order_qty, 0)
							+ COALESCE(mpt.manufacturing_order_qty, 0)
							+ COALESCE(mpt.subcontracting_qty, 0),
						q.modified = NOW(),
						q.modified_by = %(modified_by)s
				""",
			{
				"mp_name": self.name,
				"modified_by": frappe.session.user,
			},
		)

		# Bulk Fetching Data
		cache_data = self.get_manufacturing_plan_data()
		frappe.flags.is_manufactur_order_created = False
		frappe.flags.creating_from_manufacturing_plan = True
		frappe.flags._mp_tracking_bom_queue = []
		try:
			for row in self.manufacturing_plan_table:
				if row.quotation_item or row.mwo:
					create_manufacturing_order(self, row, cache_data)
					frappe.flags.is_manufactur_order_created = True
					if row.subcontracting:
						is_subcontracting = True
					if row.manufacturing_bom is None:
						frappe.throw(f"Row:{row.idx} Manufacturing Bom Missing")
		finally:
			queue = getattr(frappe.flags, "_mp_tracking_bom_queue", None) or []
			for tb_name, ref_docname in queue:
				frappe.db.set_value(
					"Tracking Bom",
					tb_name,
					{
						"reference_doctype": "Parent Manufacturing Order",
						"reference_docname": ref_docname,
					},
				)
			frappe.flags.creating_from_manufacturing_plan = False
			frappe.flags._mp_tracking_bom_queue = []

		if frappe.flags.is_manufactur_order_created:
			frappe.msgprint(_("Manufacturing Orders Created Successfully"))

		if is_subcontracting:
			create_subcontracting_order(self)

	def validate(self):
		self.validate_qty_with_bom_creation()
		self.refresh_mould_ids()

	def refresh_mould_ids(self):
		"""Keep every plan row's Mould List ID (the Mould docname) in sync with the
		current Mould per Item on every validate -- not only at item-fetch time -- so
		submitted/edited/manually added rows always reflect the current Mould. The
		field is read_only (system-managed), so overwriting is safe."""
		item_codes = {
			row.item_code for row in self.manufacturing_plan_table if row.item_code
		}
		if not item_codes:
			return
		mould_id_map = get_mould_id_map(item_codes)
		for row in self.manufacturing_plan_table:
			row.mould_id = mould_id_map.get(row.item_code)

	def validate_qty_with_bom_creation(self):
		total = 0
		for row in self.manufacturing_plan_table:
			# Validate Qty
			if row.custom_tracking_bom:
				frappe.db.set_value(
					"Tracking Bom",
					row.custom_tracking_bom,
					{"reference_doctype": self.doctype, "reference_docname": self.name},
				)
			if not row.subcontracting:
				row.subcontracting_qty = 0
				row.supplier = None
			if (row.manufacturing_order_qty + row.subcontracting_qty) > row.pending_qty:
				error_message = _(
					"Row #{0}: Total Order qty cannot be greater than {1}"
				).format(row.idx, row.pending_qty)
				frappe.throw(error_message)
			total += cint(row.manufacturing_order_qty) + cint(row.subcontracting_qty)
			if row.qty_per_manufacturing_order == 0:
				frappe.throw(_("Qty per Manufacturing Order Can not  be 0"))

			# Set Manufacturing BOM if not set
			if not row.manufacturing_bom:
				row.manufacturing_bom = row.bom
		self.total_planned_qty = total

	def get_manufacturing_plan_data(self):
		quotation_items = set()
		mwo_items = set()
		bom_names = set()
		customer_names = set()
		item_codes = set()
		customer_diamond_keys = set()

		for row in self.manufacturing_plan_table:
			if row.quotation_item:
				quotation_items.add(row.quotation_item)
			if row.mwo:
				mwo_items.add(row.mwo)
			if row.manufacturing_bom:
				bom_names.add(row.manufacturing_bom)
			if getattr(row, "bom", None):
				bom_names.add(row.bom)
			if row.serial_id_bom:
				bom_names.add(row.serial_id_bom)
			if row.customer:
				customer_names.add(row.customer)
			if row.item_code:
				item_codes.add(row.item_code)
			if row.customer and row.diamond_quality:
				customer_diamond_keys.add((row.customer, row.diamond_quality))

		quotation_data_map = fetch_doc_map(
			"Quotation Item",
			quotation_items,
			["name", "metal_type", "metal_touch", "metal_colour", "diamond_grade"],
		)

		mwo_data_map = fetch_doc_map(
			"Manufacturing Work Order",
			mwo_items,
			[
				"name",
				"metal_type",
				"metal_touch",
				"metal_colour",
				"master_bom",
				"custom_tracking_bom",
			],
		)

		bom_data_map = fetch_doc_map(
			"BOM", bom_names, ["name", "metal_type_", "metal_colour", "metal_touch"]
		)

		customer_data_map = fetch_doc_map(
			"Customer", customer_names, ["name", "is_internal_customer"]
		)

		# Fetch Customer Diamond Grades
		customer_diamond_grade_map = {}
		if customer_diamond_keys:
			# We filter by 'parent' IN customer_names.
			cust_grades = frappe.get_all(
				"Customer Diamond Grade",
				filters={"parent": ["in", list(customer_names)]},
				fields=[
					"parent",
					"diamond_quality",
					"diamond_grade_1",
					"diamond_grade_2",
					"diamond_grade_3",
					"diamond_grade_4",
				],
			)
			for cg in cust_grades:
				customer_diamond_grade_map[(cg.parent, cg.diamond_quality)] = cg

		item_data_map = fetch_doc_map("Item", item_codes, ["name", "has_batch_no"])

		# We can fetch all attribute values that are customer diamond qualities just in case.
		attr_values = frappe.get_all(
			"Attribute Value",
			filters={"is_customer_diamond_quality": 1},
			fields=["name"],
		)
		attribute_value_set = {d.name for d in attr_values}

		manufacturer = frappe.defaults.get_user_default("manufacturer")
		finding_default_department = None
		if manufacturer:
			finding_default_department = frappe.db.get_value(
				"Manufacturing Setting",
				{"manufacturer": manufacturer},
				"default_department",
			)

		return {
			"quotation_data": quotation_data_map,
			"mwo_data": mwo_data_map,
			"bom_data": bom_data_map,
			"customer_data": customer_data_map,
			"item_data": item_data_map,
			"customer_diamond_grade": customer_diamond_grade_map,
			"attribute_value_set": attribute_value_set,
			"mp_context": {
				"manufacturer": manufacturer,
				"finding_default_department": finding_default_department,
			},
		}

	@frappe.whitelist()
	def get_items_for_production(self):
		if self.select_manufacture_order in ["Manufacturing", "Repair"]:
			QuotationItem = frappe.qb.DocType("Quotation Item")
			Item = frappe.qb.DocType("Item")
			Quotation = frappe.qb.DocType("Quotation")

			query = (
				frappe.qb.from_(QuotationItem)
				.join(Item)
				.on(QuotationItem.item_code == Item.name)
				.join(Quotation)
				.on(QuotationItem.parent == Quotation.name)
				.select(
					QuotationItem.name.as_("quotation_item"),
					QuotationItem.parent.as_("quotation"),
					QuotationItem.item_code,
					QuotationItem.copy_bom.as_("bom"),
					QuotationItem.custom_tracking_bom,
					Quotation.party_name.as_("customer"),
					Item.master_bom.as_("master_bom"),
					QuotationItem.diamond_quality,
					QuotationItem.custom_customer_sample.as_("customer_sample"),
					QuotationItem.custom_customer_voucher_no.as_("customer_voucher_no"),
					QuotationItem.custom_customer_gold.as_("customer_gold"),
					QuotationItem.custom_customer_diamond.as_("customer_diamond"),
					QuotationItem.custom_customer_stone.as_("customer_stone"),
					QuotationItem.custom_customer_good.as_("customer_good"),
					QuotationItem.custom_customer_weight.as_("customer_weight"),
					(QuotationItem.qty - QuotationItem.manufacturing_order_qty).as_(
						"pending_qty"
					),
					QuotationItem.order_form_type,
					QuotationItem.custom_repair_type.as_("repair_type"),
					QuotationItem.custom_product_type.as_("product_type"),
					QuotationItem.serial_no,
					QuotationItem.custom_serial_id_bom.as_("serial_id_bom"),
				)
				.where(
					(QuotationItem.parent.isin(self.docs_to_append))
					& (QuotationItem.qty > QuotationItem.manufacturing_order_qty)
				)
			)

			if self.setting_type:
				query = query.where(QuotationItem.setting_type == self.setting_type)

			items = query.run(as_dict=True)
		else:
			items = []

		mwo_data = frappe.db.sql(
			"""
			SELECT
				item_code,
				SUM(qty) AS total_qty,
				MIN(name) AS mwo
			FROM `tabManufacturing Work Order`
			WHERE name IN %(docs)s
			GROUP BY item_code
		""",
			{"docs": tuple(self.docs_to_append)},
			as_dict=True,
		)

		mould_id_map = get_mould_id_map(
			[row["item_code"] for row in items] + [row["item_code"] for row in mwo_data]
		)

		self.manufacturing_plan_table = []
		for item_row in items:
			bom = item_row.get("bom") or item_row.get("master_bom")
			if not bom and (
				item_row.get("order_form_type") == "Repair Order"
				or self.select_manufacture_order == "Repair"
			):
				bom = item_row.get("serial_id_bom")
			if bom:
				item_row["manufacturing_order_qty"] = item_row.get("pending_qty")
				if self.is_subcontracting:
					item_row["subcontracting"] = self.is_subcontracting
					item_row["subcontracting_qty"] = item_row.get("pending_qty")
					item_row["supplier"] = self.supplier
					item_row["estimated_delivery_date"] = self.estimated_date
					item_row["purchase_type"] = self.purchase_type
					item_row["manufacturing_order_qty"] = 0

				item_row["qty_per_manufacturing_order"] = 1
				item_row["bom"] = bom
				item_row["order_form_type"] = item_row.get("order_form_type")
				item_row["mould_id"] = mould_id_map.get(item_row["item_code"])
				self.append("manufacturing_plan_table", item_row)
			else:
				item_code = item_row["item_code"]
				frappe.throw(
					_(
						f"Sales Order BOM Not Found.</br>Please Set Master BOM for <b>{item_code}</b> into Item Master"
					)
				)

		for row in mwo_data:
			qty = row["total_qty"]
			self.append(
				"manufacturing_plan_table",
				{
					"item_code": row["item_code"],
					"pending_qty": qty,
					"manufacturing_order_qty": qty,
					"qty_per_manufacturing_order": qty,
					"mwo": row["mwo"],
					"mould_id": mould_id_map.get(row["item_code"]),
				},
			)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_pending_ppo_quotation(doctype, txt, searchfield, start, page_len, filters):
	Quotation = frappe.qb.DocType("Quotation")
	QuotationItem = frappe.qb.DocType("Quotation Item")
	Item = frappe.qb.DocType("Item")

	conditions = (
		(QuotationItem.qty > QuotationItem.manufacturing_order_qty)
		& (QuotationItem.order_form_type != "Repair Order")
		& (Quotation.order_type != "Repair")
	)

	if txt:
		conditions &= Quotation.name.like(f"%{txt}%")

	if customer := filters.get("party_name"):
		conditions &= Quotation.party_name == customer

	if company := filters.get("company"):
		conditions &= Quotation.company == company

	if branch := filters.get("branch"):
		conditions &= Quotation.branch == branch

	if txn_date := filters.get("transaction_date"):
		conditions &= Quotation.transaction_date == txn_date

	query = (
		frappe.qb.from_(Quotation)
		.distinct()
		.from_(QuotationItem)
		.join(Item)
		.on(QuotationItem.item_code == Item.name)
		.select(
			Quotation.name,
			Quotation.transaction_date,
			Quotation.company,
			Quotation.party_name.as_("customer"),
		)
		.where(
			(Quotation.name == QuotationItem.parent)
			& (Quotation.docstatus == 1)
			& conditions
			& (Item.master_bom.isnotnull() | QuotationItem.copy_bom.isnotnull())
		)
		.orderby(Quotation.transaction_date, order=frappe.qb.desc)
		.limit(page_len)
		.offset(start)
	)
	quotation_data = query.run(as_dict=True)

	return quotation_data


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_repair_pending_ppo_quotation(
	doctype, txt, searchfield, start, page_len, filters
):
	Quotation = frappe.qb.DocType("Quotation")
	QuotationItem = frappe.qb.DocType("Quotation Item")

	conditions = (QuotationItem.qty > QuotationItem.manufacturing_order_qty) & (
		Quotation.order_type == "Repair"
	)

	if txt:
		conditions &= Quotation.name.like(f"%{txt}%")

	if customer := filters.get("party_name"):
		conditions &= Quotation.party_name == customer

	if company := filters.get("company"):
		conditions &= Quotation.company == company

	if branch := filters.get("branch"):
		conditions &= Quotation.branch == branch

	if txn_date := filters.get("transaction_date"):
		conditions &= Quotation.transaction_date == txn_date

	query = (
		frappe.qb.from_(Quotation)
		.distinct()
		.from_(QuotationItem)
		.select(
			Quotation.name,
			Quotation.transaction_date,
			Quotation.company,
			Quotation.party_name.as_("customer"),
		)
		.where(
			(Quotation.name == QuotationItem.parent)
			& (Quotation.docstatus == 1)
			& conditions
		)
		.orderby(Quotation.transaction_date, order=frappe.qb.desc)
		.limit(page_len)
		.offset(start)
	)
	quotation_data = query.run(as_dict=True)

	return quotation_data


@frappe.whitelist()
def get_details_to_append(source_names, target_doc=None):
	if not target_doc:
		target_doc = frappe.new_doc("Manufacturing Plan")
	elif isinstance(target_doc, str):
		target_doc = frappe.get_doc(json.loads(target_doc))
	target_doc.docs_to_append = json.loads(source_names)
	target_doc.get_items_for_production()
	return target_doc


@frappe.whitelist()
def make_manufacturing_plan(source_name, target_doc=None):
	"""Quotation-side entry point: build a Manufacturing Plan pre-filled from a Quotation.

	Reuses ``get_items_for_production`` so the row-population logic stays in one place.
	"""
	quotation = frappe.db.get_value(
		"Quotation", source_name, ["company", "branch", "order_type"], as_dict=True
	)
	if not quotation:
		frappe.throw(_("Quotation {0} not found").format(source_name))

	target = frappe.new_doc("Manufacturing Plan")
	target.company = quotation.company
	target.branch = quotation.branch
	target.select_manufacture_order = (
		"Repair" if quotation.order_type == "Repair" else "Manufacturing"
	)
	target.docs_to_append = [source_name]
	target.get_items_for_production()
	return target


@frappe.whitelist()
def map_docs(method, source_names, target_doc, args=None):
	method = frappe.get_attr(frappe.override_whitelisted_method(method))
	if method not in frappe.whitelisted:
		raise frappe.PermissionError
	_args = (
		(source_names, target_doc, json.loads(args))
		if args
		else (source_names, target_doc)
	)
	target_doc = method(*_args)
	return target_doc


def fetch_doc_map(doctype, names, fields, key_field="name"):
	if not names:
		return {}

	data = frappe.get_all(
		doctype,
		filters={"name": ["in", list(names)]},
		fields=fields,
	)

	return {d[key_field]: d for d in data}


def create_manufacturing_order(doc, row, cache_data=None):
	if cache_data is None:
		cache_data = {}

	cnt = int(row.manufacturing_order_qty / row.qty_per_manufacturing_order)

	if not cnt:
		return

	quotation_data_map = cache_data.get("quotation_data", {})
	mwo_data_map = cache_data.get("mwo_data", {})
	bom_data_map = cache_data.get("bom_data", {})
	customer_data_map = cache_data.get("customer_data", {})
	item_data_map = cache_data.get("item_data", {})
	attribute_value_set = cache_data.get("attribute_value_set", set())
	customer_diamond_grade_map = cache_data.get("customer_diamond_grade", {})

	so_det = {}
	# Use plain dict copy instead of frappe._dict for memory/speed
	if row.quotation and row.quotation_item in quotation_data_map:
		so_det = quotation_data_map[row.quotation_item].copy()
	elif row.mwo and row.mwo in mwo_data_map:
		so_det = mwo_data_map[row.mwo].copy()
	else:
		# Fallback
		doc_type, docname = (
			("Quotation Item", row.quotation_item)
			if row.quotation
			else ("Manufacturing Work Order", row.mwo)
		)
		fields = ["metal_type", "metal_touch", "metal_colour"]
		if row.mwo:
			fields.append("master_bom")

		fetched_val = frappe.get_value(doc_type, docname, fields, as_dict=1)
		if fetched_val:
			so_det = fetched_val

	master_bom = None
	if doc.select_manufacture_order == "Manufacturing":
		master_bom = row.manufacturing_bom
	elif doc.select_manufacture_order == "Repair":
		master_bom = row.serial_id_bom

	if master_bom:
		# caching check
		bom_details = bom_data_map.get(master_bom)
		if not bom_details:
			bom_details = frappe.db.get_value(
				"BOM",
				master_bom,
				["metal_type_", "metal_colour", "metal_touch"],
				as_dict=1,
			)

		if bom_details:
			# Update dictionary values using subscript notation
			so_det["metal_type"] = bom_details.get("metal_type_") or so_det.get(
				"metal_type_"
			)
			so_det["metal_colour"] = bom_details.get("metal_colour") or so_det.get(
				"metal_colour"
			)
			so_det["metal_touch"] = bom_details.get("metal_touch") or so_det.get(
				"metal_touch"
			)

	# Check for internal customer
	customer_info = customer_data_map.get(row.customer)
	is_internal_customer = (
		customer_info.get("is_internal_customer")
		if customer_info
		else frappe.db.get_value("Customer", row.customer, "is_internal_customer")
	)

	if row.diamond_quality and not is_internal_customer:
		key = (row.customer, row.diamond_quality)
		diamond_grade = None

		diamond_grade_data = customer_diamond_grade_map.get(key)
		if diamond_grade_data:
			grades_to_check = [
				diamond_grade_data.get("diamond_grade_1"),
				diamond_grade_data.get("diamond_grade_2"),
				diamond_grade_data.get("diamond_grade_3"),
				diamond_grade_data.get("diamond_grade_4"),
			]

			from frappe import cstr

			customer_diamond = cstr(row.customer_diamond).strip().lower()

			if customer_diamond == "yes":
				for grade in grades_to_check:
					if grade and grade in attribute_value_set:
						diamond_grade = grade
						break
			else:
				for grade in grades_to_check:
					if grade and grade not in attribute_value_set:
						diamond_grade = grade
						break

		if not diamond_grade:
			if diamond_grade_data:
				for grade in grades_to_check:
					if grade:
						diamond_grade = grade
						break
			if not diamond_grade:
				# Minimal fallback
				diamond_grade = frappe.db.get_value(
					"Customer Diamond Grade",
					{"parent": row.customer, "diamond_quality": row.diamond_quality},
					"diamond_grade_1",
				)

		so_det["diamond_grade"] = diamond_grade

		has_batch_no = False
		item_info = item_data_map.get(row.item_code)
		if item_info:
			has_batch_no = item_info.get("has_batch_no")
		else:
			has_batch_no = frappe.db.get_value("Item", row.item_code, "has_batch_no")

		if not so_det.get("diamond_grade") and not has_batch_no:
			frappe.throw(
				_("Diamond Grade is not mentioned in customer {0}").format(row.customer)
			)

	mp_context = cache_data.get("mp_context") if cache_data else None
	for i in range(0, cnt):
		make_manufacturing_order(
			doc,
			row,
			master_bom=master_bom,
			so_det=so_det,
			mp_context=mp_context,
		)


def create_subcontracting_order(doc):
	make_subcontracting_order(doc)


@frappe.whitelist()
def get_cad_eligible_items(manufacturing_plan):
	"""Item codes in this plan whose earliest Parent Manufacturing Order has no CAD/CAM MWO yet."""
	item_codes = frappe.db.sql(
		"""
		SELECT DISTINCT item_code
		FROM `tabManufacturing Plan Table`
		WHERE parent = %s AND item_code IS NOT NULL AND item_code != ''
		""",
		(manufacturing_plan,),
	)

	eligible = []
	for (item_code,) in item_codes:
		pmo = frappe.db.get_value(
			"Parent Manufacturing Order",
			{"manufacturing_plan": manufacturing_plan, "item_code": item_code},
			"name",
			order_by="creation asc",
		)
		if not pmo:
			continue
		if frappe.db.get_value(
			"Manufacturing Work Order", {"manufacturing_order": pmo, "for_cad_cam": 1}
		):
			continue
		eligible.append(item_code)
	return eligible


@frappe.whitelist()
def create_cad_mwo(manufacturing_plan, item_code, reason=None):
	"""Create a CAD/CAM Manufacturing Work Order for an item in this plan.

	Attaches to the earliest Parent Manufacturing Order for (manufacturing_plan, item_code),
	since CAD/CAM design work happens once per item, not once per unit.
	"""
	pmo = frappe.db.get_value(
		"Parent Manufacturing Order",
		{"manufacturing_plan": manufacturing_plan, "item_code": item_code},
		"name",
		order_by="creation asc",
	)
	if not pmo:
		frappe.throw(
			_(
				"No Parent Manufacturing Order found for Item {0} in Manufacturing Plan {1}"
			).format(item_code, manufacturing_plan)
		)
	return create_mwo(pmo, None, reason)


@frappe.whitelist()
def create_cad_mwo_bulk(manufacturing_plan, item_codes, reason=None):
	"""Create CAD/CAM Manufacturing Work Orders for multiple items in this plan.

	Each item is committed as soon as it succeeds, so one item lacking a Parent
	Manufacturing Order doesn't roll back the items already created before it.
	"""
	if isinstance(item_codes, str):
		item_codes = json.loads(item_codes)

	failed = []
	for item_code in item_codes:
		try:
			create_cad_mwo(manufacturing_plan, item_code, reason)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="CAD MWO bulk creation failed")
			failed.append(item_code)

	if failed:
		frappe.msgprint(
			_("Could not create CAD MWO for: {0}").format(", ".join(failed)),
			indicator="red",
		)
