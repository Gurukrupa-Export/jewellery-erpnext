import frappe

from jewellery_erpnext.jewellery_erpnext.utils.safe_submit import submit_with_retry


# def get_item_for_certification(company, service_type):
# 	return frappe.db.get_value(
# 		"Product Certification Details",
# 		{"parent": company, "certification_type": service_type},
# 		["purchase_item", "rate"],
# 		as_dict=1,
# 	)
def get_item_for_certification(department, service_type):
	manufacturer = frappe.db.get_value("Department", department, "manufacturer")
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
		supplier = frappe.db.get_value(
			"Supplier", {"supplier_name": self.type_of_certification}, "name"
		)

	if not self.department:
		frappe.throw(
			frappe._(
				"Department is mandatory to create Purchase Order for certification."
			)
		)

	if not self.service_type:
		frappe.throw(
			frappe._(
				"Service Type is mandatory to create Purchase Order for certification."
			)
		)

	po_doc.company = self.company

	# item_data = get_item_for_certification(self.company, self.service_type)
	item_data = get_item_for_certification(self.department, self.service_type)

	if not item_data:
		manufacturer = frappe.db.get_value(
			"Department", self.department, "manufacturer"
		)
		frappe.throw(
			frappe._(
				"Please configure Product Certification Details for Manufacturer {0} and Service Type {1} in Manufacturing Setting"
			).format(manufacturer or "associated with department", self.service_type)
		)

	rate = 0
	if self.service_type == "Diamond Certificate service":
		rate = frappe.db.get_value(
			"Customer", self.customer, "custom_certification_charges"
		)
	elif self.service_type == "Hall Marking Service":
		rate = frappe.db.get_value(
			"Supplier", self.supplier, "custom_certification_charges"
		)

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
	# D-002 idempotency: the Receive path (Fire Assy / XRF) calls this on
	# every PC submit. Without a guard, a retried submit creates a duplicate
	# Repack SE per Main Slip. Mirror the Issue-path pattern: short-circuit
	# if a submitted Repack SE already exists for this Product Certification.
	existing_repack = frappe.db.get_value(
		"Stock Entry",
		{
			"product_certification": self.name,
			"stock_entry_type": "Repack",
			"docstatus": 1,
		},
		"name",
	)
	if existing_repack:
		frappe.msgprint(
			frappe._(
				"Repack Stock Entry {0} already created for {1}; skipping duplicate."
			).format(existing_repack, self.name)
		)
		return

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
	if not s_warehouse:
		s_warehouse = frappe.db.exists(
			"Warehouse", {"department": self.department, "disabled": 0}
		)

	t_warehouse = frappe.db.exists(
		"Warehouse",
		{
			"subcontractor": self.supplier,
			"warehouse_type": "Raw Material",
			"disabled": 0,
		},
	)
	if not t_warehouse:
		t_warehouse = frappe.db.exists(
			"Warehouse", {"subcontractor": self.supplier, "disabled": 0}
		)

	for item in main_slip_dict:
		se_doc = frappe.new_doc("Stock Entry")
		se_doc.stock_entry_type = "Repack"
		se_doc.company = self.company
		se_doc.product_certification = self.name
		se_doc.auto_created = 1
		# EG-014 guard: rows below pass serial_and_batch_bundle=None which
		# means ERPNext auto-constructs the bundle, calling
		# combine(posting_date, posting_time). Populate both fields up-front
		# so the auto-construction never sees None.
		se_doc.posting_date = se_doc.posting_date or frappe.utils.today()
		se_doc.posting_time = se_doc.posting_time or frappe.utils.nowtime()
		se_doc.set_posting_time = 1
		items = []
		for row in self.product_details:
			if row.main_slip == item:
				msl_item_gw = gross_wt_dict.get(
					(row.main_slip, row.pure_item)
				) + gross_wt_dict.get((row.main_slip, row.loss_item))
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
							"gross_weight": gross_wt_dict.get(
								(row.main_slip, row.pure_item)
							),
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
							"gross_weight": gross_wt_dict.get(
								(row.main_slip, row.loss_item)
							),
						}
					)

		for item in items:
			se_doc.append("items", item)
		se_doc.save()
		# D-002: retry on 1205 only (idempotency upstream is the
		# existing_repack short-circuit at the top of this function).
		submit_with_retry(se_doc)

		for row in self.exploded_product_details:
			item_conversion_repack(self, row, s_warehouse, t_warehouse)


def item_conversion_repack(self, row, s_warehouse, t_warehouse):
	# D-002 idempotency: a retried Receive submit must not create a second
	# per-row Repack SE. We track which exploded-product-detail row has
	# already been repacked by looking for an existing submitted Repack SE
	# whose first item matches this row's item_code. Coarser than a row
	# ID but sufficient: the same row's item_code+gross_weight is the only
	# input to the SE, so a matching submitted SE means this row is done.
	existing = frappe.db.sql(
		"""
		SELECT se.name FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.product_certification = %(pc)s
		  AND se.stock_entry_type = 'Repack'
		  AND se.auto_created = 1
		  AND se.docstatus = 1
		  AND sed.item_code = %(item)s
		  AND ABS(sed.qty - %(qty)s) < 0.0001
		LIMIT 1
		""",
		{"pc": self.name, "item": row.item_code, "qty": row.gross_weight},
	)
	if existing:
		return
	se_doc = frappe.new_doc("Stock Entry")
	se_doc.stock_entry_type = "Repack"
	se_doc.company = self.company
	se_doc.product_certification = self.name
	se_doc.auto_created = 1
	# EG-014 guard: ensure posting_date/posting_time so the auto-constructed
	# Serial and Batch Bundle does not hit combine(None, None).
	se_doc.posting_date = se_doc.posting_date or frappe.utils.today()
	se_doc.posting_time = se_doc.posting_time or frappe.utils.nowtime()
	se_doc.set_posting_time = 1
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
	# D-002: retry on 1205 only (idempotency upstream is the existing-row
	# JOIN check at the top of this function).
	submit_with_retry(se_doc)
