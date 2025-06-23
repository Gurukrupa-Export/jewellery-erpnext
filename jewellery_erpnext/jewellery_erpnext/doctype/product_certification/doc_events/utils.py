import frappe


# def get_item_for_certification(company, service_type):
# 	return frappe.db.get_value(
# 		"Product Certification Details",
# 		{"parent": company, "certification_type": service_type},
# 		["purchase_item", "rate"],
# 		as_dict=1,
# 	)
def get_item_for_certification(department, service_type):
	manufacturer = frappe.db.get_value("Department",department,"manufacturer")
	return frappe.db.get_value(
		"Product Certification Details",
		{"parent": manufacturer, "certification_type": service_type},
		["purchase_item", "rate"],
		as_dict=1,
	)


def create_po(self):
	if self.type == "Receive":
		return

	elif self.customer and frappe.db.get_value(
		"Customer", self.customer, "custom_ignore_po_creation_for_certification"
	):
		return

	total_gross_wt = 0
	total_qty = 0
	for row in self.exploded_product_details:
		total_gross_wt += row.gross_weight
		total_qty += 1
	po_doc = frappe.new_doc("Purchase Order")

	po_doc.product_certification = self.name
	supplier = self.supplier

	if not supplier and self.type_of_certification:
		supplier = frappe.db.get_value("Supplier", {"supplier_name": self.type_of_certification}, "name")

	po_doc.company = self.company

	# item_data = get_item_for_certification(self.company, self.service_type)
	item_data = get_item_for_certification(self.department, self.service_type)

	rate = 0
	if self.service_type == "Diamond Certificate service":
		rate = frappe.db.get_value("Customer", self.customer, "custom_certification_charges")
	elif self.service_type == "Hall Marking Service":
		rate = frappe.db.get_value("Supplier", self.supplier, "custom_certification_charges")

	po_doc.supplier = supplier
	po_doc.transaction_date = self.date
	po_doc.purchase_type = "Service"
	po_doc.append(
		"items",
		{
			"item_code": item_data.get("purchase_item"),
			"qty": total_qty,
			"rate": rate or item_data.get("rate"),
			"schedule_date": self.date,
			"custom_gross_wt": total_gross_wt,
		},
	)
	po_doc.save()


def update_bom_details(self):
	if self.type == "Issue":
		return

	bom_amount_dict = {}
	for row in self.exploded_product_details:
		bom_amount_dict[row.bom] = bom_amount_dict.get(row.bom, 0) + row.amount

	field = (
		"certification_amount"
		if self.service_type == "Diamond Certificate service"
		else "hallmarking_amount"
	)

	for row in bom_amount_dict:
		frappe.db.set_value("BOM", row, field, bom_amount_dict[row])


def create_repack_entry(self):
	main_slip_dict = {}
	for row in self.product_details:
		if not main_slip_dict.get(row.main_slip):
			main_slip_dict[row.main_slip] = row.item_code

	gross_wt_dict = {}
	for row in self.exploded_product_details:
		if not gross_wt_dict.get((row.main_slip, row.item_code)):
			gross_wt_dict[(row.main_slip, row.item_code)] = row.gross_weight

	s_warehouse = frappe.db.exists(
		"Warehouse",
		{
			"department": self.department,
			"warehouse_type": "Raw Material",
			"disabled": 0,
		},
	)
	t_warehouse = frappe.db.exists(
		"Warehouse",
		{
			"subcontractor": self.supplier,
			"warehouse_type": "Raw Material",
			"disabled": 0,
		},
	)

	for item in main_slip_dict:
		se_doc = frappe.new_doc("Stock Entry")
		se_doc.stock_entry_type = "Repack"
		se_doc.company = self.company
		se_doc.product_certification = self.name
		items = []
		for row in self.product_details:
			if row.main_slip == item:
				msl_item_gw = gross_wt_dict.get((row.main_slip, row.pure_item)) + gross_wt_dict.get(
					(row.main_slip, row.loss_item)
				)
				if msl_item_gw:
					items.append(
						{
							"item_code": row.item_code,
							"qty": msl_item_gw,
							"s_warehouse": t_warehouse,
							# "s_warehouse": s_warehouse,
							"t_warehouse": None,
							"Inventory_type": row.inventory_type,
							"serial_and_batch_bundle": None,
							"use_serial_batch_fields": True,
							"gross_weight": msl_item_gw,
						}
					)
					items.append(
						{
							"item_code": row.pure_item,
							"is_finished_item": 1,
							"qty": gross_wt_dict.get((row.main_slip, row.pure_item)),
							"s_warehouse": None,
							"t_warehouse": t_warehouse,
							"Inventory_type": row.inventory_type,
							"serial_and_batch_bundle": None,
							"use_serial_batch_fields": True,
							"gross_weight": gross_wt_dict.get((row.main_slip, row.pure_item)),
						}
					)
					items.append(
						{
							"item_code": row.loss_item,
							"is_finished_item": 1,
							"qty": gross_wt_dict.get((row.main_slip, row.loss_item)),
							"s_warehouse": None,
							"t_warehouse": t_warehouse,
							"Inventory_type": row.inventory_type,
							"serial_and_batch_bundle": None,
							"use_serial_batch_fields": True,
							"gross_weight": gross_wt_dict.get((row.main_slip, row.loss_item)),
						}
					)

		for item in items:
			se_doc.append("items", item)
		se_doc.save()
		se_doc.submit()

		for row in self.exploded_product_details:
			item_conversion_repack(self, row, s_warehouse, t_warehouse)


def item_conversion_repack(self, row, s_warehouse, t_warehouse):
	se_doc = frappe.new_doc("Stock Entry")
	se_doc.stock_entry_type = "Repack"
	se_doc.company = self.company
	se_doc.product_certification = self.name
	items = []
	items.append(
		{
			"item_code": row.item_code,
			"qty": row.gross_weight,
			"s_warehouse": t_warehouse,
			"t_warehouse": None,
			"Inventory_type": row.inventory_type,
			"serial_and_batch_bundle": None,
			"use_serial_batch_fields": True,
			"gross_weight": row.gross_weight,
		}
	)
	items.append(
		{
			"item_code": row.item_code,
			"qty": row.gross_weight,
			"s_warehouse": None,
			"t_warehouse": s_warehouse,
			"Inventory_type": row.inventory_type,
			"serial_and_batch_bundle": None,
			"use_serial_batch_fields": True,
			"gross_weight": row.gross_weight,
		}
	)
	for item in items:
		se_doc.append("items", item)
	se_doc.save()
	se_doc.submit()
