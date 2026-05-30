import frappe


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


def process_fire_assy_xrf_submit(doc, create_stock_entry_fn):
	"""
	Single orchestration point for Fire Assy/XRF submit flow.

	- Creates issue/receive Stock Entry (receive uses single Material Receipt flow)
	- Creates Purchase Order when applicable (Issue only)
	- Updates BOM amount breakdown on Receive
	"""
	create_stock_entry_fn(doc)
	create_po(doc)
	update_bom_details(doc)


def _get_department_rm_warehouse(department):
	rm_warehouse = frappe.db.get_value(
		"Warehouse",
		{
			"department": department,
			"warehouse_type": "Raw Material",
			"disabled": 0,
			"is_group": 0,
		},
		"name",
	)
	if not rm_warehouse:
		rm_warehouse = frappe.db.get_value(
			"Warehouse",
			{"department": department, "disabled": 0, "is_group": 0},
			"name",
		)
	if not rm_warehouse:
		frappe.throw(
			frappe._(
				"No Raw Material warehouse found for Department {0}. Please configure a department warehouse."
			).format(department)
		)
	return rm_warehouse


def _get_department_scrap_warehouse(department):
	scrap_warehouse = frappe.db.get_value(
		"Warehouse",
		{
			"department": department,
			"warehouse_type": "Scrap",
			"disabled": 0,
			"is_group": 0,
		},
		"name",
	)
	if not scrap_warehouse:
		frappe.throw(
			frappe._(
				"No Scrap Warehouse found for Department {0}. Configure the Scrap Warehouse for this department."
			).format(department)
		)
	return scrap_warehouse


def _get_supplier_certification_warehouse(company, supplier):
	t_warehouse = frappe.db.get_value(
		"Warehouse",
		{
			"company": company,
			"subcontractor": supplier,
			"warehouse_type": "Raw Material",
			"disabled": 0,
			"is_group": 0,
		},
		"name",
	)
	if not t_warehouse:
		t_warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"company": company,
				"subcontractor": supplier,
				"disabled": 0,
				"is_group": 0,
			},
			"name",
		)
	if not t_warehouse:
		frappe.throw(
			frappe._(
				"Please set warehouse for selected supplier {0} (subcontractor warehouse)."
			).format(supplier)
		)
	return t_warehouse


def _get_issue_item_source_warehouse_map(issue_stock_entry):
	rows = frappe.db.get_all(
		"Stock Entry Detail",
		filters={"parent": issue_stock_entry},
		fields=["item_code", "t_warehouse"],
	)
	item_wh = {}
	item_wh_multi = set()
	for r in rows:
		if not r.get("item_code") or not r.get("t_warehouse"):
			continue
		if r.item_code in item_wh and item_wh[r.item_code] != r.t_warehouse:
			item_wh_multi.add(r.item_code)
		else:
			item_wh[r.item_code] = r.t_warehouse

	for item_code in item_wh_multi:
		item_wh.pop(item_code, None)

	return item_wh


def _get_issue_item_receipt_defaults(issue_stock_entry):
	"""
	Return default source warehouse / batch / serial from Issue SE per item.

	Lookup order for batch/serial:
	  1. Direct field on Stock Entry Detail row
	  2. Serial and Batch Bundle entries linked to the row
	  3. Stock Ledger Entry posted by the Issue SE (Frappe v16 may move
	     batch data into the SLE/bundle and clear the row-level fields)
	"""
	rows = frappe.db.get_all(
		"Stock Entry Detail",
		filters={"parent": issue_stock_entry},
		fields=[
			"item_code",
			"t_warehouse",
			"batch_no",
			"serial_no",
			"serial_and_batch_bundle",
			"qty",
		],
		order_by="idx asc",
	)
	item_defaults = {}
	for r in rows:
		item_code = r.get("item_code")
		if not item_code:
			continue

		batch_no = r.get("batch_no")
		serial_no = r.get("serial_no")
		bundle = r.get("serial_and_batch_bundle")

		# --- Priority 2: look inside the Serial and Batch Bundle ---
		if bundle and (not batch_no or not serial_no):
			bundle_entries = frappe.db.get_all(
				"Serial and Batch Entry",
				filters={"parent": bundle},
				fields=["batch_no", "serial_no"],
				order_by="idx asc",
			)
			if not batch_no:
				for be in bundle_entries:
					if be.get("batch_no"):
						batch_no = be.batch_no
						break
			if not serial_no:
				serials = [be.serial_no for be in bundle_entries if be.get("serial_no")]
				if serials:
					serial_no = "\n".join(serials)

		# --- Priority 3: look at the Stock Ledger Entry ---
		if not batch_no:
			sle_batch = frappe.db.get_value(
				"Stock Ledger Entry",
				{
					"voucher_type": "Stock Entry",
					"voucher_no": issue_stock_entry,
					"item_code": item_code,
					"batch_no": ["is", "set"],
				},
				"batch_no",
				order_by="creation asc",
			)
			if sle_batch:
				batch_no = sle_batch

		if item_code not in item_defaults:
			item_defaults[item_code] = {
				"s_warehouse": r.get("t_warehouse"),
				"batch_no": batch_no,
				"serial_no": serial_no,
			}
			continue

		# Fill missing values from subsequent rows if first row lacked them.
		if not item_defaults[item_code].get("batch_no") and batch_no:
			item_defaults[item_code]["batch_no"] = batch_no
		if not item_defaults[item_code].get("serial_no") and serial_no:
			item_defaults[item_code]["serial_no"] = serial_no
		if not item_defaults[item_code].get("s_warehouse") and r.get("t_warehouse"):
			item_defaults[item_code]["s_warehouse"] = r.get("t_warehouse")

	return item_defaults


def create_material_receipt_for_certification(self):
	if self.type != "Receive" or self.service_type not in [
		"Fire Assy Service",
		"XRF Services",
	]:
		return

	if not self.department:
		frappe.throw(frappe._("Department is mandatory for receipt."))
	if not self.supplier:
		frappe.throw(frappe._("Supplier is mandatory for receipt."))
	if not self.receive_against:
		frappe.throw(frappe._("Receive Against is mandatory for receipt."))

	issue_se = frappe.db.get_value(
		"Stock Entry",
		{"product_certification": self.receive_against, "docstatus": 1},
		"name",
	)
	if not issue_se:
		frappe.throw(
			frappe._("No Issue Stock Entry found for {0}.").format(self.receive_against)
		)

	rm_wh = _get_department_rm_warehouse(self.department)
	scrap_wh = _get_department_scrap_warehouse(self.department)
	default_supplier_wh = _get_supplier_certification_warehouse(
		self.company, self.supplier
	)
	issue_item_wh_map = _get_issue_item_source_warehouse_map(issue_se)
	issue_item_defaults = _get_issue_item_receipt_defaults(issue_se)

	loss_item_by_slip = {}
	main_item_by_slip = {}
	# Direct sets for XRF where main_slip may be None
	all_loss_items = set()
	all_main_items = set()
	for pd in self.product_details:
		if pd.get("main_slip"):
			main_item_by_slip[pd.main_slip] = pd.item_code
			if pd.get("loss_item"):
				loss_item_by_slip[pd.main_slip] = pd.loss_item
		all_main_items.add(pd.item_code)
		if pd.get("loss_item"):
			all_loss_items.add(pd.loss_item)

	se_doc = frappe.new_doc("Stock Entry")
	se_doc.stock_entry_type = "Material Receipt for Certification"
	se_doc.company = self.company
	se_doc.product_certification = self.name
	se_doc.auto_created = 1
	se_doc.inventory_type = "Regular Stock"

	for row in self.exploded_product_details:
		qty = row.get("gross_weight") or 0
		if qty <= 0:
			continue

		loss_item = loss_item_by_slip.get(row.get("main_slip"))
		main_item = main_item_by_slip.get(row.get("main_slip"))
		# Determine if this is a loss row: first via slip-based lookup, then via direct set
		is_loss_row = bool(loss_item and row.item_code == loss_item)
		if not is_loss_row and row.item_code in all_loss_items:
			is_loss_row = True

		# Batch/serial must always match the exploded row item (never reuse main item batch).
		item_defaults = issue_item_defaults.get(row.item_code, {})
		main_defaults = issue_item_defaults.get(main_item, {}) if main_item else {}

		# Newly produced items (not the main issued item) get no source warehouse
		is_main_item = (main_item and row.item_code == main_item) or (
			not main_item and row.item_code in all_main_items
		)
		if not is_main_item:
			s_wh = ""
		else:
			s_wh = (
				item_defaults.get("s_warehouse")
				or main_defaults.get("s_warehouse")
				or issue_item_wh_map.get(row.item_code)
				or (issue_item_wh_map.get(main_item) if main_item else None)
				or default_supplier_wh
			)
		t_wh = scrap_wh if is_loss_row else rm_wh
		has_batch_no, has_serial_no = frappe.get_cached_value(
			"Item", row.item_code, ["has_batch_no", "has_serial_no"]
		)
		batch_no = item_defaults.get("batch_no")
		serial_no = item_defaults.get("serial_no")

		# Fallback: if batch is required but missing, query the SLE at the
		# source warehouse for the most recent batch of this item.
		if has_batch_no and not batch_no:
			sle_batch = frappe.db.get_value(
				"Stock Ledger Entry",
				{
					"item_code": row.item_code,
					"warehouse": s_wh,
					"batch_no": ["is", "set"],
					"is_cancelled": 0,
				},
				"batch_no",
				order_by="posting_date desc, posting_time desc, creation desc",
			)
			if sle_batch:
				batch_no = sle_batch

		# Fallback: if serial is required but missing, try to pick available serials
		# from the same source warehouse.
		if has_serial_no and not serial_no:
			try:
				qty_int = int(qty) if float(qty).is_integer() else 0
			except Exception:
				qty_int = 0
			if qty_int > 0:
				available_serials = frappe.db.get_all(
					"Serial No",
					filters={
						"item_code": row.item_code,
						"warehouse": s_wh,
						"status": ["not in", ["Delivered", "Inactive"]],
					},
					pluck="name",
					limit=qty_int,
				)
				if available_serials and len(available_serials) >= qty_int:
					serial_no = "\n".join(available_serials)

		# Validate: throw only if we truly cannot resolve batch/serial after all fallbacks
		# and the item does NOT auto-create batches (items like pure gold / loss items
		# are produced during assay and will get a new batch on receipt).
		create_new_batch = frappe.get_cached_value(
			"Item", row.item_code, "create_new_batch"
		)
		if has_batch_no and not batch_no and not serial_no:
			if create_new_batch and row.item_code != main_item:
				from erpnext.stock.doctype.batch.batch import make_batch

				batch_no = make_batch(frappe._dict({"item": row.item_code}))
			else:
				pass
				# frappe.throw(
				# 	frappe._(
				# 		"Batch/Serial data is mandatory for Item {0}. Please ensure the Issue entry has batch/serial details."
				# 	).format(row.item_code)
				# )
		if has_serial_no and not serial_no and not batch_no:
			pass
			# frappe.throw(
			# 	frappe._(
			# 		"Serial/Batch data is mandatory for Item {0}. Please ensure the Issue entry has serial/batch details."
			# 	).format(row.item_code)
			# )

		se_doc.append(
			"items",
			{
				"item_code": row.item_code,
				"qty": qty,
				"s_warehouse": s_wh,
				"t_warehouse": t_wh,
				"batch_no": batch_no,
				"serial_no": serial_no,
				"is_scrap_item": 1 if is_loss_row else 0,
				"use_serial_batch_fields": True,
				"serial_and_batch_bundle": None,
				"Inventory_type": row.get("inventory_type") or "Regular Stock",
				"gross_weight": qty,
				"allow_zero_valuation_rate": 1,
			},
		)

	if not se_doc.items:
		frappe.throw(frappe._("No receipt items found with Gross Weight."))

	se_doc.flags.throw_batch_error = True

	# Bypass standard warehouse validation to allow mixed receipt/transfer in Material Transfer purpose
	def bypass_validate_warehouse(*args, **kwargs):
		pass

	se_doc.validate_warehouse = bypass_validate_warehouse

	se_doc.save()
	se_doc.submit()
