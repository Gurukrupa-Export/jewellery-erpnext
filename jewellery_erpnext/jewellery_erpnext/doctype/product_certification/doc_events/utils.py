import frappe


def get_item_for_certification(department, service_type):
	"""Charge item + rate for a certification service, from the Manufacturing Setting of
	the Department's Manufacturer.

	``Product Certification Details`` is a child of Manufacturing Setting, which is named
	after the Manufacturer — so the parent key is the Department's ``manufacturer``. A blank
	one degrades the filter to ``parent IS NULL`` and silently matches nothing, which is why
	callers must go through ``validate_po_configuration`` first for an accurate message.
	"""
	manufacturer = frappe.db.get_value("Department", department, "manufacturer")
	if not manufacturer:
		return None
	return frappe.db.get_value(
		"Product Certification Details",
		{
			"parent": manufacturer,
			"parenttype": "Manufacturing Setting",
			"certification_type": service_type,
		},
		["purchase_item", "rate"],
		as_dict=1,
	)


def po_is_expected(doc):
	"""Whether this document should produce a service Purchase Order.

	The single source of truth for the skip rules, shared by the submit-time validation and
	by create_po itself, so a guard can never disagree with the creator about whether a PO
	was due.
	"""
	if doc.type == "Receive":
		return False
	if doc.customer and frappe.db.get_value(
		"Customer", doc.customer, "custom_ignore_po_creation_for_certification"
	):
		return False
	return True


def resolve_po_supplier(doc):
	"""Supplier for the certification PO: the document's own, else the one named after the
	Type of Certification. Purchase Order.supplier is mandatory, so a blank one fails the
	insert."""
	if doc.supplier:
		return doc.supplier
	if doc.type_of_certification:
		return frappe.db.get_value(
			"Supplier", {"supplier_name": doc.type_of_certification}, "name"
		)
	return None


def validate_po_configuration(doc):
	"""Refuse the submit unless the service Purchase Order can actually be created.

	Everything create_po needs is checked here, at submit time, where the operator can act
	on it — rather than in the deferred job, which runs after the document has committed and
	surfaces only as a background traceback, leaving a submitted certification with no PO.

	Each failure names the field that is actually missing. The previous message always
	pointed at Manufacturing Setting even when the real gap was Department.manufacturer.

	Returns the resolved charge item so ``create_po`` can reuse it rather than resolving the
	whole chain (Department → Manufacturer → Manufacturing Setting → charge item) a second
	time immediately afterwards.
	"""
	if not po_is_expected(doc):
		return None

	if not doc.department:
		frappe.throw(
			frappe._(
				"Department is mandatory to create Purchase Order for certification."
			)
		)
	if not doc.service_type:
		frappe.throw(
			frappe._(
				"Service Type is mandatory to create Purchase Order for certification."
			)
		)

	if not resolve_po_supplier(doc):
		frappe.throw(
			frappe._(
				"Supplier could not be determined for this certification. Set Supplier on "
				"this document, or create a Supplier named after the Type of Certification "
				"{0}."
			).format(frappe.bold(doc.type_of_certification or "-"))
		)

	manufacturer = frappe.db.get_value("Department", doc.department, "manufacturer")
	if not manufacturer:
		frappe.throw(
			frappe._(
				"Department {0} has no Manufacturer set, so the certification charge item "
				"cannot be resolved. Set Manufacturer on the Department, then ensure its "
				"Manufacturing Setting has a Product Certification Details row for Service "
				"Type {1}."
			).format(frappe.bold(doc.department), frappe.bold(doc.service_type))
		)

	if not frappe.db.exists("Manufacturing Setting", {"manufacturer": manufacturer}):
		frappe.throw(
			frappe._(
				"No Manufacturing Setting exists for Manufacturer {0} (from Department "
				"{1}), so the certification charge item cannot be resolved."
			).format(frappe.bold(manufacturer), frappe.bold(doc.department))
		)

	item_data = get_item_for_certification(doc.department, doc.service_type)
	if not item_data or not item_data.get("purchase_item"):
		frappe.throw(
			frappe._(
				"Manufacturing Setting for Manufacturer {0} has no Product Certification "
				"Details row with a Purchase Item for Service Type {1}. Add it before "
				"submitting."
			).format(frappe.bold(manufacturer), frappe.bold(doc.service_type))
		)

	return item_data


def create_po(self):
	if not po_is_expected(self):
		return

	# Idempotent: the deferred job is deduplicated by job_id, but a retry or a job queued
	# before PO creation moved into on_submit must not mint a second PO.
	existing = frappe.db.exists(
		"Purchase Order", {"product_certification": self.name, "docstatus": ["<", 2]}
	)
	if existing:
		return

	total_gross_wt = sum(row.gross_weight for row in self.exploded_product_details)
	total_qty = len(self.exploded_product_details)
	po_doc = frappe.new_doc("Purchase Order")

	po_doc.product_certification = self.name
	supplier = resolve_po_supplier(self)

	# Re-checked rather than assumed: create_po is also reachable from the deferred job and
	# from a direct call, not only through before_submit. Its answer is reused below — it
	# resolves the very charge item this used to look up again on the next line.
	item_data = validate_po_configuration(self)

	po_doc.company = self.company

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
	- Creates Purchase Order when applicable (Issue only), synchronously, so a failure
	  rolls the submit back instead of stranding a submitted certification with no PO
	- Updates BOM amount breakdown on Receive (deferred)
	"""
	create_stock_entry_fn(doc)
	create_po(doc)
	frappe.enqueue(
		"jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification.deferred_po_bom",
		pc_name=doc.name,
		enqueue_after_commit=True,
		job_id=f"pc_po_bom::{doc.name}",
		deduplicate=True,
	)


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


def _get_issue_stock_entry_details(issue_stock_entry):
	"""Read the Issue Stock Entry's lines once and derive BOTH per-item maps from them.

	``_get_issue_item_source_warehouse_map`` and ``_get_issue_item_receipt_defaults`` used to
	be two functions issuing the same ``Stock Entry Detail`` query back to back, and the
	second then issued a ``Serial and Batch Entry`` read and a ``Stock Ledger Entry`` read
	*per line*. All of it is now four queries for the whole document.

	Returns ``(item_wh, item_defaults)``:

	* ``item_wh`` — item to target warehouse, dropped when the item went to more than one.
	* ``item_defaults`` — default source warehouse / batch / serial per item, resolved in
	  the same priority order as before:
	    1. Direct field on the Stock Entry Detail row
	    2. Serial and Batch Bundle entries linked to the row
	    3. Stock Ledger Entry posted by the Issue SE (Frappe v16 may move batch data into
	       the SLE/bundle and clear the row-level fields)
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

	# --- Priority 2 source, for every bundle on the entry at once ---
	bundles = {
		r.serial_and_batch_bundle for r in rows if r.get("serial_and_batch_bundle")
	}
	bundle_entries = {}
	if bundles:
		for entry in frappe.db.get_all(
			"Serial and Batch Entry",
			filters={"parent": ("in", list(bundles))},
			fields=["parent", "batch_no", "serial_no"],
			order_by="parent asc, idx asc",
		):
			bundle_entries.setdefault(entry.parent, []).append(entry)

	# Resolve each line's batch/serial (priorities 1 and 2) first, so we know which items
	# still need the ledger before asking for it.
	resolved = []
	needs_sle_batch = set()
	for r in rows:
		item_code = r.get("item_code")
		batch_no = r.get("batch_no")
		serial_no = r.get("serial_no")
		bundle = r.get("serial_and_batch_bundle")

		if bundle and (not batch_no or not serial_no):
			entries = bundle_entries.get(bundle, [])
			if not batch_no:
				for be in entries:
					if be.get("batch_no"):
						batch_no = be.batch_no
						break
			if not serial_no:
				serials = [be.serial_no for be in entries if be.get("serial_no")]
				if serials:
					serial_no = "\n".join(serials)

		resolved.append((r, batch_no, serial_no))
		if item_code and not batch_no:
			needs_sle_batch.add(item_code)

	# --- Priority 3, for every item still short of a batch, in one read ---
	# The per-line query this replaces carried the same filters and ordering for every line
	# of an item, so it always returned this same value.
	sle_batch = {}
	if needs_sle_batch:
		for sle in frappe.db.get_all(
			"Stock Ledger Entry",
			filters={
				"voucher_type": "Stock Entry",
				"voucher_no": issue_stock_entry,
				"item_code": ("in", list(needs_sle_batch)),
				"batch_no": ["is", "set"],
			},
			fields=["item_code", "batch_no"],
			order_by="creation asc",
		):
			sle_batch.setdefault(sle.item_code, sle.batch_no)

	item_wh = {}
	item_wh_multi = set()
	item_defaults = {}

	for r, batch_no, serial_no in resolved:
		item_code = r.get("item_code")

		if item_code and r.get("t_warehouse"):
			if item_code in item_wh and item_wh[item_code] != r.t_warehouse:
				item_wh_multi.add(item_code)
			else:
				item_wh[item_code] = r.t_warehouse

		if not item_code:
			continue

		if not batch_no:
			batch_no = sle_batch.get(item_code)

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

	for item_code in item_wh_multi:
		item_wh.pop(item_code, None)

	return item_wh, item_defaults


def create_material_receipt_for_certification(self):
	"""Create TWO stock entries for Fire Assy / XRF Receive:

	1. Repack-Metal Conversion — for non-main items (other metals) AND
	   scrap/loss items.  Source warehouse = supplier certification WH
	   (where the Issue entry sent stock).  Target warehouse = dept RM
	   for non-loss rows, dept Scrap for loss rows.
	2. Material Receipt for Certification — only the main item.
	   Source warehouse = supplier certification WH.
	   Target warehouse = dept RM warehouse.
	"""
	if self.type != "Receive" or self.service_type not in [
		"Fire Assy Service",
		"XRF Services",
	]:
		return

	# Local import: product_certification.py imports this module at load time.
	from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification import (
		_slip_key,
	)

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
	issue_item_wh_map, issue_item_defaults = _get_issue_stock_entry_details(issue_se)

	# Keyed on the full (main_slip, tree_no) pair, not main_slip alone: trees minted since the
	# casting rework carry no Main Slip, so keying on the slip collapses every tree in the
	# document onto one entry and hands the wrong main/loss item to rows of the other trees.
	loss_item_by_slip = {}
	main_item_by_slip = {}
	all_loss_items = set()
	all_main_items = set()
	for pd in self.product_details:
		key = _slip_key(pd)
		if any(key):
			main_item_by_slip[key] = pd.item_code
			if pd.get("loss_item"):
				loss_item_by_slip[key] = pd.loss_item
		all_main_items.add(pd.item_code)
		if pd.get("loss_item"):
			all_loss_items.add(pd.loss_item)

	# ── Classify rows into main vs repack (other + scrap) ──
	main_rows = []
	repack_rows = []

	# Unwrapped once: the loop below used to rebuild a throwaway list from each set on
	# every exploded row just to read its only element.
	sole_loss_item = next(iter(all_loss_items)) if len(all_loss_items) == 1 else None
	sole_main_item = next(iter(all_main_items)) if len(all_main_items) == 1 else None

	# The two per-row fallbacks below, memoised on what they actually key off. An exploded
	# table repeats the same item across slips and trees, and each repeat re-ran the same
	# ledger scan and the same serial scan.
	sle_batch_cache = {}
	serial_cache = {}

	for row in self.exploded_product_details:
		qty = row.get("conversion_quantity") or row.get("gross_weight") or 0
		if qty <= 0:
			continue

		row_key = _slip_key(row)

		loss_item = loss_item_by_slip.get(row_key) or sole_loss_item
		main_item = main_item_by_slip.get(row_key) or sole_main_item
		is_loss_row = bool(loss_item and row.item_code == loss_item)
		if not is_loss_row and row.item_code in all_loss_items:
			is_loss_row = True

		is_main_item = (main_item and row.item_code == main_item) or (
			not main_item and row.item_code in all_main_items
		)

		# Batch/serial resolution
		item_defaults = issue_item_defaults.get(row.item_code, {})
		main_defaults = issue_item_defaults.get(main_item, {}) if main_item else {}

		# Source warehouse — always use supplier certification WH for all rows
		s_wh = (
			item_defaults.get("s_warehouse")
			or main_defaults.get("s_warehouse")
			or issue_item_wh_map.get(row.item_code)
			or (issue_item_wh_map.get(main_item) if main_item else None)
			or default_supplier_wh
		)

		t_wh = scrap_wh if is_loss_row else rm_wh

		# One read of the Item for all three flags; this used to ask twice per row.
		has_batch_no, has_serial_no, create_new_batch = frappe.get_cached_value(
			"Item",
			row.item_code,
			["has_batch_no", "has_serial_no", "create_new_batch"],
		)
		batch_no = item_defaults.get("batch_no")
		serial_no = item_defaults.get("serial_no")

		# Batch fallback via SLE
		if has_batch_no and not batch_no:
			sle_key = (row.item_code, s_wh)
			if sle_key not in sle_batch_cache:
				sle_batch_cache[sle_key] = frappe.db.get_value(
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
			sle_batch = sle_batch_cache[sle_key]
			if sle_batch:
				batch_no = sle_batch

		# Serial fallback
		if has_serial_no and not serial_no:
			try:
				qty_int = int(qty) if float(qty).is_integer() else 0
			except Exception:
				qty_int = 0
			if qty_int > 0:
				# Keyed on qty too, because the limit is part of the answer. Two rows of
				# the same item are still handed the same serials -- that is what this
				# already did, one query at a time.
				serial_key = (row.item_code, s_wh, qty_int)
				if serial_key not in serial_cache:
					serial_cache[serial_key] = frappe.db.get_all(
						"Serial No",
						filters={
							"item_code": row.item_code,
							"warehouse": s_wh,
							"status": ["not in", ["Delivered", "Inactive"]],
						},
						pluck="name",
						limit=qty_int,
					)
				available_serials = serial_cache[serial_key]
				if available_serials and len(available_serials) >= qty_int:
					serial_no = "\n".join(available_serials)

		# Auto-create batch for non-main items if needed
		if has_batch_no and not batch_no and not serial_no:
			if create_new_batch:
				from erpnext.stock.doctype.batch.batch import make_batch

				batch_no = make_batch(frappe._dict({"item": row.item_code}))

		row_dict = {
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
		}

		if is_main_item and not is_loss_row:
			main_rows.append(row_dict)
		else:
			# Source row for repack (consume the main item)
			if main_item:
				main_s_wh = (
					main_defaults.get("s_warehouse")
					or issue_item_wh_map.get(main_item)
					or default_supplier_wh
				)
				repack_rows.append(
					{
						"item_code": main_item,
						"qty": qty,
						"s_warehouse": main_s_wh,
						"t_warehouse": "",
						"batch_no": main_defaults.get("batch_no"),
						"serial_no": main_defaults.get("serial_no"),
						"is_scrap_item": 0,
						"use_serial_batch_fields": True,
						"serial_and_batch_bundle": None,
						"Inventory_type": row.get("inventory_type") or "Regular Stock",
						"gross_weight": qty,
					}
				)

			# Target row for repack
			row_dict["s_warehouse"] = ""
			row_dict["is_finished_item"] = 1
			repack_rows.append(row_dict)

	if not main_rows and not repack_rows:
		frappe.throw(frappe._("No receipt items found with Gross Weight."))

	def bypass_validate_warehouse(*args, **kwargs):
		pass

	# Canonical lock order: both Stock Entries below draw from the same supplier-
	# certification source warehouse for overlapping items. Submitting them sequentially
	# meant the transaction held SE#1's Bins while SE#2 waited on Bins a concurrent Product
	# Certification held — a 1213/1205 cross-cycle. Pin the Stock Entry series, then pre-lock
	# the UNION of every Bin both SEs touch, in sorted order, before either submit, so
	# concurrent PCs acquire the shared Bins in the identical sequence.
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_bins,
		preallocate_series_for_docs,
		series_stubs,
	)

	_pc_pairs = [
		(rd.get("item_code"), rd.get(wh))
		for rd in (main_rows + repack_rows)
		for wh in ("s_warehouse", "t_warehouse")
	]
	# Pin the naming counter of BOTH nested SE types built below (the per-(company x
	# type) Document Naming Rule counter post-reshard, or the tabSeries fallback).
	# A blank stub matches no rule and would pin the wrong (shared MAT-STE-) row.
	preallocate_series_for_docs(
		*series_stubs(self.company, "Material Receipt for Certification", "Repack")
	)
	lock_bins(_pc_pairs)

	# ── 1. Material Receipt for Certification — main item only ──
	if main_rows:
		se_receipt = frappe.new_doc("Stock Entry")
		se_receipt.stock_entry_type = "Material Receipt for Certification"
		se_receipt.company = self.company
		se_receipt.product_certification = self.name
		se_receipt.auto_created = 1
		se_receipt.inventory_type = "Regular Stock"
		for rd in main_rows:
			se_receipt.append("items", rd)
		se_receipt.validate_warehouse = bypass_validate_warehouse
		se_receipt.flags.ignore_permissions = True
		se_receipt.save(ignore_permissions=True)
		se_receipt.submit()

	# ── 2. Repack-Metal Conversion for other items + scrap ──
	if repack_rows:
		se_repack = frappe.new_doc("Stock Entry")
		se_repack.stock_entry_type = "Repack"
		se_repack.company = self.company
		se_repack.product_certification = self.name
		se_repack.auto_created = 1
		se_repack.inventory_type = "Regular Stock"
		for rd in repack_rows:
			se_repack.append("items", rd)
		se_repack.validate_warehouse = bypass_validate_warehouse
		se_repack.validate_finished_goods = bypass_validate_warehouse
		se_repack.validate_repack_entry = bypass_validate_warehouse
		se_repack.flags.ignore_permissions = True
		se_repack.save(ignore_permissions=True)
		se_repack.submit()
