import json

import frappe
from frappe import _

from jewellery_erpnext.jewellery_erpnext.customization.quotation.doc_events.utils import (
	update_si,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.bom_utils import (
	calculate_gst_rate,
	set_bom_item_details,
	set_bom_rate_in_quotation,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.quotation_pricing import (
	_apply_kg_gk_pricing,
	_apply_standard_pricing,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.tracking_bom_utils import (
	set_tracking_bom_rate_in_quotation,
)


@frappe.whitelist()
def update_status(quotation_id):
	status = frappe.db.get_value("Quotation", quotation_id, "status")
	if status != "Closed":
		frappe.db.set_value("Quotation", quotation_id, "status", "Closed")
	else:
		frappe.db.set_value("Quotation", quotation_id, "status", "Open")


def validate(self, method):
	validate_gold_rate_with_gst(self)
	self.calculate_taxes_and_totals()
	if self.workflow_state == "Creating BOM":
		frappe.enqueue(
			create_bom_sceintifically, self=self, queue="long", timeout=10000
		)
	if self.docstatus == 0:
		calculate_gst_rate(self)
		if not self.get("__islocal"):
			set_bom_item_details(self)
			update_si(self)
		set_tracking_bom_rate_in_quotation(self)


def create_bom_sceintifically(self):
	create_tracking_bom_directly(self)


@frappe.whitelist()
def generate_bom(name):
	self = frappe.get_doc("Quotation", name)
	self.flags.can_be_saved = True
	frappe.enqueue(create_bom_sceintifically, self=self, queue="long", timeout=10000)


def onload(self, method):
	return


def on_submit(self, method):
	submit_bom(self)


def on_cancel(self, method):
	cancel_bom(self)


def before_submit(self, method):
	validate_invoice_item(self)


def submit_bom(self):
	pass


def cancel_bom(self):
	for row in self.items:
		if row.custom_tracking_bom:
			bom = frappe.get_doc("Tracking Bom", row.custom_tracking_bom)
			bom.is_active = 0
			bom.save()
			row.custom_tracking_bom = None


from jewellery_erpnext.jewellery_erpnext.doc_events.bom import update_totals


@frappe.whitelist()
def update_bom_detail(
	parent_doctype,
	parent_doctype_name,
	metal_detail,
	diamond_detail,
	gemstone_detail,
	finding_detail,
	other_detail,
):
	parent = frappe.get_doc(parent_doctype, parent_doctype_name)

	set_metal_detail(parent, metal_detail)
	set_diamond_detail(parent, diamond_detail)
	set_gemstone_detail(parent, gemstone_detail)
	set_finding_detail(parent, finding_detail)
	set_other_detail(parent, other_detail)

	parent.reload()
	parent.ignore_validate_update_after_submit = True
	parent.save()

	update_totals(parent_doctype, parent_doctype_name)
	return "BOM Updated"


def set_metal_detail(parent, metal_detail):
	metal_data = json.loads(metal_detail)
	tolerance = frappe.db.get_value("Company", parent.company, "custom_metal_tolerance")
	for d in metal_data:
		validate_rate(parent, tolerance, d, "Metal")
		update_table(parent, "BOM Metal Detail", "metal_detail", d)


def set_diamond_detail(parent, diamond_detail):
	diamond_data = json.loads(diamond_detail)
	tolerance = frappe.db.get_value(
		"Company", parent.company, "custom_diamond_tolerance"
	)
	for d in diamond_data:
		validate_rate(parent, tolerance, d, "Diamond")
		update_table(parent, "BOM Diamond Detail", "diamond_detail", d)


def set_gemstone_detail(parent, gemstone_detail):
	gemstone_data = json.loads(gemstone_detail)
	tolerance = frappe.db.get_value(
		"Company", parent.company, "custom_gemstone_tolerance"
	)
	for d in gemstone_data:
		validate_rate(parent, tolerance, d, "Gemstone")
		update_table(parent, "BOM Gemstone Detail", "gemstone_detail", d)


def set_finding_detail(parent, finding_detail):
	finding_data = json.loads(finding_detail)
	tolerance = frappe.db.get_value("Company", parent.company, "custom_metal_tolerance")
	for d in finding_data:
		validate_rate(parent, tolerance, d, "Metal")
		update_table(parent, "BOM Finding Detail", "finding_detail", d)


def set_other_detail(parent, other_material):
	other_material = json.loads(other_material)
	for d in other_material:
		update_table(parent, "BOM Other Detail", "other_detail", d)


def update_table(parent, table, table_field, doc):
	if not doc.get("docname"):
		child_doc = parent.append(table_field, {})
	else:
		child_doc = frappe.get_doc(table, doc.get("docname"))
	doc.pop("docname", "")
	doc.pop("name", "")
	child_doc.update(doc)
	child_doc.flags.ignore_validate_update_after_submit = True
	child_doc.save()


def validate_rate(parent, tolerance, doc, table):
	table_dic = {
		"Metal": ["rate", "actual_rate"],
		"Gemstone": ["total_gemstone_rate", "actual_total_gemstone_rate"],
		"Diamond": ["total_diamond_rate", "actual_total_diamond_rate"],
	}
	if doc.get(table_dic.get(table)[0]) and doc.get(table_dic.get(table)[1]):
		tolerance_range = (doc.get(table_dic.get(table)[1]) * tolerance) / 100

		if (
			doc.get(table_dic.get(table)[1]) - tolerance_range
			<= doc.get(table_dic.get(table)[0])
			<= doc.get(table_dic.get(table)[1]) + tolerance_range
		):
			pass
		else:
			frappe.throw("Enter the rate within the tolerance range.")


def new_finding_item(parent_doc, child_doctype, child_docname, finding_item):
	child_item = frappe.new_doc(child_doctype, parent_doc, child_docname)
	child_item.item = "F"
	child_item.metal_type = finding_item.get("metal_type")
	child_item.finding_category = finding_item.get("finding_category")
	child_item.finding_type = finding_item.get("finding_type")
	child_item.finding_size = finding_item.get("finding_size")
	child_item.metal_purity = finding_item.get("metal_purity")
	child_item.metal_colour = finding_item.get("metal_colour")
	child_item.quantity = finding_item.get("quantity")
	return child_item


@frappe.whitelist()
def get_gold_rate(party_name=None, currency=None):
	if not party_name:
		return
	cust_terr = frappe.db.get_value("Customer", party_name, "territory")
	gold_rate_with_gst = frappe.db.get_value(
		"Gold Price List",
		{"territory": cust_terr, "currency": currency},
		"rate",
		order_by="effective_from desc",
	)
	if not gold_rate_with_gst:
		frappe.msgprint(f"Gold Price List Not Found For {cust_terr}, {currency}")
	return gold_rate_with_gst


def validate_invoice_item(self):
	self.set("custom_invoice_item", [])
	if not self.custom_invoice_item:
		customer_payment_term_doc = frappe.get_doc(
			"Customer Payment Terms", {"customer": self.customer}
		)

		e_invoice_items = []
		for item_detail in customer_payment_term_doc.customer_payment_details:
			item_type = item_detail.item_type
			if item_type:
				e_invoice_item_doc = frappe.get_doc("E Invoice Item", item_type)
				# Match specific sales_type
				matched_sales_type_row = None
				for row in e_invoice_item_doc.sales_type:
					if row.sales_type == self.custom_sales_type:
						matched_sales_type_row = row
						break

				# Skip item if no matching sales_type and custom_sales_type is set
				if self.custom_sales_type and not matched_sales_type_row:
					continue
				e_invoice_items.append(
					{
						"item_type": item_type,
						"metal_purity": e_invoice_item_doc.metal_purity or "N/A",
						"is_for_metal": e_invoice_item_doc.is_for_metal,
						"metal_type": e_invoice_item_doc.metal_type or "N/A",
						"is_for_diamond": e_invoice_item_doc.is_for_diamond,
						"is_for_finding": e_invoice_item_doc.is_for_finding,
						"diamond_type": e_invoice_item_doc.diamond_type or "N/A",
						"is_for_gemstone": e_invoice_item_doc.is_for_gemstone,
						"is_for_making": e_invoice_item_doc.is_for_making,
						"is_for_finding_making": e_invoice_item_doc.is_for_finding_making,
						"uom": e_invoice_item_doc.uom or "N/A",
						"tax_rate": matched_sales_type_row.tax_rate
						if matched_sales_type_row
						else 0,
					}
				)

		self.set("custom_invoice_item", [])

		aggregated_diamond_items = {}
		aggregated_metal_making_items = {}
		aggregated_metal_amount_items = {}
		aggregated_finding_items = {}
		aggregated_finding_making_items = {}
		aggregated_gemstone_items = {}

		for item in self.items:
			if item.quotation_bom:
				bom_doc = frappe.get_doc("BOM", item.quotation_bom)
				for diamond in bom_doc.diamond_detail:
					for e_item in e_invoice_items:
						if (
							e_item["is_for_diamond"]
							and e_item["diamond_type"] == diamond.diamond_type
							and e_item["uom"] == diamond.stock_uom
						):
							key = (e_item["item_type"], e_item["uom"])
							if key not in aggregated_diamond_items:
								aggregated_diamond_items[key] = {
									"item_code": e_item["item_type"],
									"item_name": e_item["item_type"],
									"uom": e_item["uom"],
									"qty": 0,
									"rate": diamond.total_diamond_rate,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
									"tax_amount": 0,
									"amount_with_tax": 0,
								}
							# multiplied_qty = diamond.quantity * item.qty

							# aggregated_diamond_items[key]["qty"] += multiplied_qty
							# diamond_amount = diamond.total_diamond_rate * multiplied_qty
							# aggregated_diamond_items[key]["amount"] += diamond_amount
							multiplied_qty = diamond.quantity * item.qty
							diamond_amount = diamond.total_diamond_rate * multiplied_qty

							# Update quantity and amount
							aggregated_diamond_items[key]["qty"] += multiplied_qty
							aggregated_diamond_items[key]["amount"] += diamond_amount

							# Calculate tax amount
							tax_rate_decimal = (
								aggregated_diamond_items[key]["tax_rate"] / 100
							)
							aggregated_diamond_items[key]["tax_amount"] += (
								diamond_amount * tax_rate_decimal
							)

							# Update amount with tax
							aggregated_diamond_items[key]["amount_with_tax"] = (
								aggregated_diamond_items[key]["amount"]
								+ aggregated_diamond_items[key]["tax_amount"]
							)

				# Metal making aggregation
				for metal in bom_doc.metal_detail:
					for e_item in e_invoice_items:
						if (
							e_item["is_for_making"]
							and e_item["metal_type"] == metal.metal_type
							and e_item["metal_purity"] == metal.metal_touch
							and e_item["uom"] == metal.stock_uom
						):
							key = (e_item["item_type"], e_item["uom"])
							if key not in aggregated_metal_making_items:
								aggregated_metal_making_items[key] = {
									"item_code": e_item["item_type"],
									"item_name": e_item["item_type"],
									"uom": e_item["uom"],
									"qty": 0,
									"rate": metal.making_rate,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
									"tax_amount": 0,
									"amount_with_tax": 0,
								}
							# multiplied_qty = metal.quantity * item.qty
							# aggregated_metal_making_items[key]["qty"] += multiplied_qty
							# metal_making_amount = metal.making_rate * multiplied_qty

							# aggregated_metal_making_items[key]["amount"] += metal_making_amount

							multiplied_qty = metal.quantity * item.qty
							metal_making_amount = metal.making_rate * multiplied_qty

							# Update quantity and amount
							aggregated_metal_making_items[key]["qty"] += multiplied_qty
							aggregated_metal_making_items[key][
								"amount"
							] += metal_making_amount

							# Calculate tax amount
							tax_rate_decimal = (
								aggregated_metal_making_items[key]["tax_rate"] / 100
							)
							aggregated_metal_making_items[key]["tax_amount"] += (
								metal_making_amount * tax_rate_decimal
							)

							# Update amount with tax
							aggregated_metal_making_items[key]["amount_with_tax"] = (
								aggregated_metal_making_items[key]["amount"]
								+ aggregated_metal_making_items[key]["tax_amount"]
							)

				# Metal amount aggregation
				for metal in bom_doc.metal_detail:
					for e_item in e_invoice_items:
						if (
							e_item["is_for_metal"]
							and e_item["metal_type"] == metal.metal_type
							and e_item["metal_purity"] == metal.metal_touch
							and e_item["uom"] == metal.stock_uom
						):
							key = (e_item["item_type"], e_item["uom"])
							if key not in aggregated_metal_amount_items:
								aggregated_metal_amount_items[key] = {
									"item_code": e_item["item_type"],
									"item_name": e_item["item_type"],
									"uom": e_item["uom"],
									"qty": 0,
									"rate": metal.rate,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
									"tax_amount": 0,
									"amount_with_tax": 0,
								}
							# multiplied_qty = metal.quantity * item.qty
							# aggregated_metal_amount_items[key]["qty"] += multiplied_qty
							# metal_amount = metal.rate * multiplied_qty
							# aggregated_metal_amount_items[key]["amount"] += metal_amount

							multiplied_qty = metal.quantity * item.qty
							metal_amount = metal.rate * multiplied_qty

							# Update quantity and amount
							aggregated_metal_amount_items[key]["qty"] += multiplied_qty
							aggregated_metal_amount_items[key]["amount"] += metal_amount

							# Calculate tax amount
							tax_rate_decimal = (
								aggregated_metal_amount_items[key]["tax_rate"] / 100
							)
							aggregated_metal_amount_items[key]["tax_amount"] += (
								metal_amount * tax_rate_decimal
							)

							# Update amount with tax
							aggregated_metal_amount_items[key]["amount_with_tax"] = (
								aggregated_metal_amount_items[key]["amount"]
								+ aggregated_metal_amount_items[key]["tax_amount"]
							)

				# Finding aggregation
				for finding in bom_doc.finding_detail:
					for e_item in e_invoice_items:
						if (
							e_item["is_for_finding"]
							and e_item["metal_type"] == finding.metal_type
							and e_item["metal_purity"] == finding.metal_touch
							and e_item["uom"] == finding.stock_uom
						):
							key = (e_item["item_type"], e_item["uom"])
							if key not in aggregated_finding_items:
								aggregated_finding_items[key] = {
									"item_code": e_item["item_type"],
									"item_name": e_item["item_type"],
									"uom": e_item["uom"],
									"qty": 0,
									"rate": finding.rate,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
									"tax_amount": 0,
									"amount_with_tax": 0,
								}

							multiplied_qty = finding.quantity * item.qty
							finding_amount = finding.rate * multiplied_qty

							# Update quantity and amount
							aggregated_finding_items[key]["qty"] += multiplied_qty
							aggregated_finding_items[key]["amount"] += finding_amount

							# Calculate tax amount
							tax_rate_decimal = (
								aggregated_finding_items[key]["tax_rate"] / 100
							)
							aggregated_finding_items[key]["tax_amount"] += (
								finding_amount * tax_rate_decimal
							)

							# Update amount with tax
							aggregated_finding_items[key]["amount_with_tax"] = (
								aggregated_finding_items[key]["amount"]
								+ aggregated_finding_items[key]["tax_amount"]
							)

				# Finding making aggregation
				for finding_making in bom_doc.finding_detail:
					for e_item in e_invoice_items:
						if (
							e_item["is_for_finding_making"]
							and e_item["metal_type"] == finding_making.metal_type
							and e_item["metal_purity"] == finding_making.metal_touch
							and e_item["uom"] == finding_making.stock_uom
						):
							key = (e_item["item_type"], e_item["uom"])
							if key not in aggregated_finding_making_items:
								aggregated_finding_making_items[key] = {
									"item_code": e_item["item_type"],
									"item_name": e_item["item_type"],
									"uom": e_item["uom"],
									"qty": 0,
									"rate": finding_making.making_rate,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
									"tax_amount": 0,
									"amount_with_tax": 0,
								}
							# multiplied_qty = finding.quantity * item.qty
							# finding_making_amount = finding_making.making_rate * multiplied_qty
							# aggregated_finding_making_items[key]["qty"] += multiplied_qty
							# aggregated_finding_making_items[key]["amount"] += finding_making_amount

							multiplied_qty = finding.quantity * item.qty
							finding_making_amount = (
								finding_making.making_rate * multiplied_qty
							)

							# Update quantity and amount
							aggregated_finding_making_items[key][
								"qty"
							] += multiplied_qty
							aggregated_finding_making_items[key][
								"amount"
							] += finding_making_amount

							# Calculate tax amount
							tax_rate_decimal = (
								aggregated_finding_making_items[key]["tax_rate"] / 100
							)
							aggregated_finding_making_items[key]["tax_amount"] += (
								finding_making_amount * tax_rate_decimal
							)

							# Update amount with tax
							aggregated_finding_making_items[key]["amount_with_tax"] = (
								aggregated_finding_making_items[key]["amount"]
								+ aggregated_finding_making_items[key]["tax_amount"]
							)
				# Gemstone aggregation
				for gemstone in bom_doc.gemstone_detail:
					for e_item in e_invoice_items:
						# frappe.throw(f"{gemstone.uom}")
						if (
							e_item["is_for_gemstone"]
							and e_item["uom"] == gemstone.stock_uom
						):
							key = (e_item["item_type"], e_item["uom"])
							if key not in aggregated_gemstone_items:
								aggregated_gemstone_items[key] = {
									"item_code": e_item["item_type"],
									"item_name": e_item["item_type"],
									"uom": e_item["uom"],
									"qty": 0,
									"rate": gemstone.total_gemstone_rate,
									"amount": 0,
									"tax_rate": e_item["tax_rate"],
									"tax_amount": 0,
									"amount_with_tax": 0,
								}
							# multiplied_qty = gemstone.quantity * item.qty
							# gemstone_amount = gemstone.total_gemstone_rate * multiplied_qty
							# aggregated_gemstone_items[key]["qty"] += multiplied_qty
							# aggregated_gemstone_items[key]["amount"] += gemstone_amount

							multiplied_qty = gemstone.quantity * item.qty
							gemstone_amount = (
								gemstone.total_gemstone_rate * multiplied_qty
							)

							# Update quantity and amount
							aggregated_gemstone_items[key]["qty"] += multiplied_qty
							aggregated_gemstone_items[key]["amount"] += gemstone_amount
							# Calculate tax amount
							tax_rate_decimal = (
								aggregated_gemstone_items[key]["tax_rate"] / 100
							)
							aggregated_gemstone_items[key]["tax_amount"] += (
								gemstone_amount * tax_rate_decimal
							)

							# Update amount with tax
							aggregated_gemstone_items[key]["amount_with_tax"] = (
								aggregated_gemstone_items[key]["amount"]
								+ aggregated_gemstone_items[key]["tax_amount"]
							)
		for item in aggregated_diamond_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_metal_making_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_metal_amount_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_finding_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_finding_making_items.values():
			self.append("custom_invoice_item", item)

		for item in aggregated_gemstone_items.values():
			self.append("custom_invoice_item", item)


def validate_gold_rate_with_gst(self):
	for i in self.items:
		if i.order_form_id:
			order_qty = frappe.db.get_value("Order", i.order_form_id, "qty")
			if order_qty is not None and i.qty > order_qty:
				frappe.throw(
					_(
						"Row {0} : Quotation Item Qty ({1}) cannot be greater than Order Form Qty ({2})"
					).format(i.idx, i.qty, order_qty)
				)
	if not self.gold_rate_with_gst:
		frappe.throw(_("Gold Rate with GST is mandatory."))


def create_tracking_bom_directly(self):
	"""Create Tracking BOM directly from Template BOM."""
	item_codes = tuple([row.item_code for row in self.items if row.item_code])
	if item_codes:
		frappe.db.sql(
			"""
            UPDATE `tabItem` i
            INNER JOIN `tabQuotation Item` qi
                ON qi.item_code = i.name
            LEFT JOIN `tabOrder` o
                ON o.name = i.custom_cad_order_id
            LEFT JOIN `tabOrder Form` ofm
                ON ofm.name = i.custom_cad_order_form_id
            SET
                i.custom_cad_order_id = qi.order_form_id,
                i.custom_cad_order_form_id = NULL
            WHERE
                i.name IN %(items)s
                AND qi.parent = %(quotation)s
                AND o.docstatus = 2
                AND ofm.docstatus = 2
        """,
			{"items": item_codes, "quotation": self.name},
		)

	metal_criteria = (
		frappe.get_list(
			"Metal Criteria",
			{"parent": self.party_name},
			["metal_touch", "metal_purity"],
			ignore_permissions=1,
		)
		or {}
	)
	metal_criteria = {row.metal_touch: row.metal_purity for row in metal_criteria}
	error_logs = []
	self.custom_bom_creation_logs = None
	attribute_data = frappe._dict()
	item_tracking_data = frappe._dict()
	bom_data = frappe._dict()

	tracking_boms_to_insert = []
	tb_dicts = []
	child_tables_map = {
		"metal_detail": ("BOM Metal Detail", []),
		"diamond_detail": ("BOM Diamond Detail", []),
		"gemstone_detail": ("BOM Gemstone Detail", []),
		"finding_detail": ("BOM Finding Detail", []),
		"other_detail": ("BOM Other Detail", []),
		"raw_materials": ("Raw Materials", []),
		"operations": ("BOM Operation", []),
	}

	for row in self.items:
		if item_tracking_data.get(row.item_code):
			row.db_set("custom_tracking_bom", item_tracking_data.get(row.item_code))

		if bom_data.get(row.item_code):
			row.db_set("copy_bom", bom_data.get(row.item_code))

		if row.custom_tracking_bom:
			continue

		bom = frappe.qb.DocType("BOM")
		query = (
			frappe.qb.from_(bom)
			.select(bom.name)
			.where(
				(bom.item == row.get("item_code"))
				& (
					(bom.tag_no == row.get("serial_no"))
					| (
						(bom.bom_type == "Finished Goods")
						& (bom.is_active == 1)
						& (bom.docstatus == 1)
					)
					| ((bom.bom_type == "Template") & (bom.is_active == 1))
				)
			)
			.orderby(
				frappe.qb.terms.Case()
				.when(bom.tag_no == row.get("serial_no"), 1)
				.when(bom.bom_type == "Finished Goods", 2)
				.when(bom.bom_type == "Template", 3)
				.else_(0),
			)
			.orderby(bom.creation)
			.limit(1)
		)
		bom_result = query.run(as_dict=True)

		if row.order_form_type == "Order":
			mod_reason = frappe.db.get_value("Order", row.order_form_id, "mod_reason")
			if "F-G" in row.item_code or mod_reason == "Change in Metal Touch":
				bom_result = [
					{"name": frappe.db.get_value("Order", row.order_form_id, "new_bom")}
				]

		if bom_result:
			try:
				tb_doc = _create_single_tracking_bom(
					self,
					row,
					bom_result[0].get("name"),
					attribute_data,
					metal_criteria,
					item_tracking_data,
					bom_data,
				)
				if tb_doc:
					tracking_boms_to_insert.append(tb_doc)
			except Exception as e:
				frappe.log_error(title="Quotation Tracking BOM Error", message=f"{e}")
				error_logs.append(f"Row {row.idx} : {e}")

	if tracking_boms_to_insert:
		for tb_doc in tracking_boms_to_insert:
			try:
				tb_doc.insert(ignore_permissions=True)
			except Exception as e:
				frappe.log_error("Tracking BOM Bulk Insert Error", str(e))
				error_logs.append(f"Failed to insert Tracking BOM {tb_doc.name}: {e}")

	if error_logs:
		import html2text

		error_str = "<br>".join(error_logs)
		error_str = html2text.html2text(error_str)
		frappe.db.set_value(
			self.doctype, self.name, "custom_bom_creation_logs", error_str
		)
	else:
		if self.flags.can_be_saved:
			self.save()
		else:
			self.calculate_taxes_and_totals()
			self.db_update()
		frappe.db.set_value(self.doctype, self.name, "workflow_state", "BOM Created")
		frappe.db.set_value(self.doctype, self.name, "custom_bom_creation_logs", None)

	frappe.msgprint("Tracking BOM(s) created")


def _create_single_tracking_bom(
	self,
	row,
	source_bom_name,
	attribute_data,
	metal_criteria,
	item_tracking_data,
	bom_data,
):
	"""Create a single Tracking BOM from a source BOM template/FG, with price optimization."""
	copy_bom = source_bom_name
	if row.order_form_id:
		order_form_bom = frappe.db.get_value("Order", row.order_form_id, "new_bom")
		if order_form_bom:
			copy_bom = order_form_bom

	row.db_set("copy_bom", copy_bom)
	source_bom = frappe.get_doc("BOM", copy_bom)

	# Create Tracking BOM
	tracking_bom = frappe.new_doc("Tracking Bom")
	tracking_bom.item = source_bom.item
	tracking_bom.company = self.company
	tracking_bom.item_uom = source_bom.uom
	tracking_bom.quantity = source_bom.quantity
	tracking_bom.bom_type = "Quotation"
	tracking_bom.reference_doctype = "Quotation"
	tracking_bom.reference_docname = self.name
	tracking_bom.customer = self.party_name
	tracking_bom.selling_price_list = self.selling_price_list
	tracking_bom.gold_rate_with_gst = self.gold_rate_with_gst
	tracking_bom.hallmarking_amount = row.custom_hallmarking_amount
	tracking_bom.setting_type = source_bom.setting_type
	tracking_bom.item_subcategory = source_bom.item_subcategory
	tracking_bom.item_category = source_bom.item_category

	# Copy child tables from source BOM
	_copy_bom_child_tables(source_bom, tracking_bom)

	# Apply customer-specific pricing
	_apply_customer_pricing(
		self, row, tracking_bom, source_bom, attribute_data, metal_criteria
	)

	# Copy operations and raw materials
	for bom_op in source_bom.operations:
		tracking_bom.append(
			"operations",
			{
				"operation": bom_op.operation,
				"workstation": bom_op.workstation,
				"time_in_mins": bom_op.time_in_mins,
				"hour_rate": bom_op.hour_rate,
				"cost": bom_op.cost,
			},
		)

	# Validate calculates all rates and wait for bulk insert
	tracking_bom.set_new_name()
	tracking_bom.before_validate()
	tracking_bom.validate()

	# Update quotation item maps
	item_tracking_data[row.item_code] = tracking_bom.name
	bom_data[row.item_code] = source_bom_name
	row.db_set("custom_tracking_bom", tracking_bom.name)

	row.gold_bom_rate = tracking_bom.gold_bom_amount
	row.diamond_bom_rate = tracking_bom.diamond_bom_amount
	row.gemstone_bom_rate = tracking_bom.gemstone_bom_amount
	row.other_bom_rate = tracking_bom.other_bom_amount
	row.making_charge = tracking_bom.making_charge
	row.bom_rate = tracking_bom.total_bom_amount
	row.rate = tracking_bom.total_bom_amount

	return tracking_bom


def _copy_bom_child_tables(source_bom, tracking_bom):
	"""Copy metal, diamond, gemstone, finding, other detail from source BOM to Tracking BOM."""

	for raw in source_bom.items:
		d = tracking_bom.append("items", {})
		for field in raw.as_dict():
			if field not in (
				"name",
				"parent",
				"parenttype",
				"parentfield",
				"idx",
				"doctype",
				"docstatus",
				"owner",
				"creation",
				"modified",
				"modified_by",
			):
				d.set(field, raw.get(field))

	for metal in source_bom.metal_detail:
		d = tracking_bom.append("metal_detail", {})
		for field in metal.as_dict():
			if field not in (
				"name",
				"parent",
				"parenttype",
				"parentfield",
				"idx",
				"doctype",
				"docstatus",
				"owner",
				"creation",
				"modified",
				"modified_by",
			):
				d.set(field, metal.get(field))

	for diamond in source_bom.diamond_detail:
		d = tracking_bom.append("diamond_detail", {})
		for field in diamond.as_dict():
			if field not in (
				"name",
				"parent",
				"parenttype",
				"parentfield",
				"idx",
				"doctype",
				"docstatus",
				"owner",
				"creation",
				"modified",
				"modified_by",
			):
				d.set(field, diamond.get(field))

	for gem in source_bom.gemstone_detail:
		d = tracking_bom.append("gemstone_detail", {})
		for field in gem.as_dict():
			if field not in (
				"name",
				"parent",
				"parenttype",
				"parentfield",
				"idx",
				"doctype",
				"docstatus",
				"owner",
				"creation",
				"modified",
				"modified_by",
			):
				d.set(field, gem.get(field))

	for finding in source_bom.finding_detail:
		d = tracking_bom.append("finding_detail", {})
		for field in finding.as_dict():
			if field not in (
				"name",
				"parent",
				"parenttype",
				"parentfield",
				"idx",
				"doctype",
				"docstatus",
				"owner",
				"creation",
				"modified",
				"modified_by",
			):
				d.set(field, finding.get(field))

	for other in source_bom.other_detail:
		d = tracking_bom.append("other_detail", {})
		for field in other.as_dict():
			if field not in (
				"name",
				"parent",
				"parenttype",
				"parentfield",
				"idx",
				"doctype",
				"docstatus",
				"owner",
				"creation",
				"modified",
				"modified_by",
			):
				d.set(field, other.get(field))


def _apply_customer_pricing(
	self, row, tracking_bom, source_bom, attribute_data, metal_criteria
):
	"""Apply customer-specific pricing to Tracking BOM child tables.
	Handles KG GK company-specific logic and standard company logic."""
	ref_customer = frappe.db.get_value("Quotation", self.name, "ref_customer")
	diamond_price_list_ref_customer = frappe.db.get_value(
		"Customer", ref_customer, "diamond_price_list"
	)
	gemstone_price_list_ref_customer = frappe.db.get_value(
		"Customer", ref_customer, "custom_gemstone_price_list_type"
	)
	diamond_price_list_customer = frappe.db.get_value(
		"Customer", tracking_bom.customer, "diamond_price_list"
	)
	gemstone_price_list_customer = frappe.db.get_value(
		"Customer", tracking_bom.customer, "custom_gemstone_price_list_type"
	)

	diamond_price_list = frappe.get_all(
		"Diamond Price List",
		filters={
			"customer": tracking_bom.customer,
			"price_list_type": diamond_price_list_customer,
		},
		fields=["name", "price_list_type"],
	)
	gemstone_price_list = frappe.get_all(
		"Gemstone Price List",
		filters={
			"customer": tracking_bom.customer,
			"price_list_type": gemstone_price_list_customer,
		},
		fields=["name", "price_list_type"],
	)

	if not attribute_data:
		attribute_data.update(
			{
				v: True
				for v in frappe.db.get_all(
					"Attribute Value", {"custom_consider_as_gold_item": 1}, pluck="name"
				)
			}
		)

	if self.company == "KG GK Jewellers Private Limited":
		_apply_kg_gk_pricing(
			self,
			row,
			tracking_bom,
			ref_customer,
			diamond_price_list_ref_customer,
			gemstone_price_list_ref_customer,
			diamond_price_list_customer,
			gemstone_price_list_customer,
			diamond_price_list,
			gemstone_price_list,
		)
	else:
		_apply_standard_pricing(
			self,
			row,
			tracking_bom,
			attribute_data,
			metal_criteria,
			diamond_price_list_customer,
			gemstone_price_list_customer,
			diamond_price_list,
			gemstone_price_list,
		)

	# Apply finding metal purity
	for idx, find in enumerate(tracking_bom.finding_detail, start=1):
		if not find.metal_purity:
			touch = (find.metal_touch or "").strip()
			purity = metal_criteria.get(touch)
			if not purity and row.get("metal_purity"):
				purity = row.metal_purity
			if purity:
				find.metal_purity = purity
			else:
				frappe.throw(
					f"Tracking BOM Finding Detail Row #{idx}: Value missing for: Metal Purity for Metal Touch '{touch}'"
				)
