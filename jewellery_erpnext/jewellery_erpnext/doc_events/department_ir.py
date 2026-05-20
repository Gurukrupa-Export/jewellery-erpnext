"""
Department IR -> Product Certification Stock Entry auto-creation.

Hooked from `hooks.doc_events["Department IR"]["on_submit"]`. Creates and
submits a "Material Transfer to Department" Stock Entry whenever a
Department IR moves material between Tagging and Product Certification:

  CASE 1 (Issue): Tagging -> Product Certification
  CASE 2 (Receive): the matching Receive against that Issue at PC.

Source-of-truth for item rows is MOP Log (the same source the EOD
material-transfer flow uses in `mop_eod_sync._build_transfer_rows_for_mop`).
Stock Reservation Entry presence is used only as a gate when its
manufacturing_work_order / manufacturing_operation custom fields exist on
the install.

All warehouse lookups are dynamic by `Warehouse.department`, matching the
existing pattern in
`jewellery_erpnext.doctype.department_ir.department_ir`. The literal names
named in the spec ("Tagging WO - GEPL", "Product Certification Transit
- GEPL", "Product Certification Raw Material - GEPL") are not hardcoded;
only the department names that drive routing are constants.

The reservation-fetching surface (`get_reserved_items_for_department_ir`)
is the only project-specific seam: replace its body if the live SRE / MOP
Log schema differs on a given site.
"""

import frappe
from frappe import _

from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
	_build_transfer_rows_for_mop,
)

TAGGING_DEPARTMENT = "Tagging - GEPL"
PRODUCT_CERTIFICATION_DEPARTMENT = "Product Certification - GEPL"
STOCK_ENTRY_TYPE = "Material Transfer to Department"
PURPOSE = "Material Transfer"
LOGGER_NAME = "department_ir_pc_stock_entry"


def create_pc_stock_entry_from_department_ir(doc, method=None):
	"""Entry point. Creates a Stock Entry for Tagging<->PC movements only."""
	if getattr(doc, "docstatus", 0) != 1:
		return

	if is_pc_issue(doc):
		flow = "issue"
	elif is_pc_receive(doc):
		flow = "receive"
	else:
		return

	if already_created_stock_entry(doc):
		frappe.logger(LOGGER_NAME).info(
			f"Stock Entry already exists for Department IR {doc.name}; skipping."
		)
		return

	try:
		s_wh = get_source_warehouse(doc, flow)
		t_wh = get_target_warehouse(doc, flow)
		validate_warehouse(s_wh, _("source warehouse"))
		validate_warehouse(t_wh, _("target warehouse"))

		items = get_reserved_items_for_department_ir(doc, flow, s_wh, t_wh)
		if not items:
			frappe.throw(
				_(
					"No reserved stock items found for Department IR {0}. "
					"Cannot create Product Certification Stock Entry."
				).format(doc.name)
			)

		se_name = create_stock_entry(doc, items, s_wh, t_wh)
		frappe.logger(LOGGER_NAME).info(
			f"Created Stock Entry {se_name} for Department IR {doc.name} ({flow})."
		)
	except frappe.ValidationError:
		raise
	except Exception:
		frappe.log_error(
			title=f"PC Stock Entry auto-create failed: {doc.name}",
			message=frappe.get_traceback(),
		)
		raise


# ---------------------------------------------------------------------------
# Branch detection
# ---------------------------------------------------------------------------


def is_pc_issue(doc):
	return (
		getattr(doc, "type", None) == "Issue"
		and getattr(doc, "transfer_type", None) == "Next Department"
		and getattr(doc, "current_department", None) == TAGGING_DEPARTMENT
		and getattr(doc, "next_department", None) == PRODUCT_CERTIFICATION_DEPARTMENT
	)


def is_pc_receive(doc):
	if getattr(doc, "type", None) != "Receive":
		return False
	if getattr(doc, "current_department", None) != PRODUCT_CERTIFICATION_DEPARTMENT:
		return False
	receive_against = getattr(doc, "receive_against", None)
	if not receive_against:
		return False
	issue = frappe.db.get_value(
		"Department IR",
		receive_against,
		["current_department", "next_department", "type", "docstatus"],
		as_dict=True,
	)
	if not issue:
		return False
	return (
		issue.type == "Issue"
		and issue.docstatus == 1
		and issue.current_department == TAGGING_DEPARTMENT
		and issue.next_department == PRODUCT_CERTIFICATION_DEPARTMENT
	)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def already_created_stock_entry(doc):
	return bool(
		frappe.db.exists(
			"Stock Entry",
			{
				"department_ir": doc.name,
				"docstatus": ["!=", 2],
				"stock_entry_type": STOCK_ENTRY_TYPE,
			},
		)
	)


# ---------------------------------------------------------------------------
# Warehouse resolution (dynamic by Warehouse.department)
# ---------------------------------------------------------------------------


def _department_manufacturing_wh(department):
	return frappe.db.get_value(
		"Warehouse",
		{
			"disabled": 0,
			"department": department,
			"warehouse_type": "Manufacturing",
		},
	)


def _department_transit_wh(department):
	return frappe.db.get_value(
		"Warehouse",
		{
			"disabled": 0,
			"department": department,
			"warehouse_type": "Manufacturing",
		},
		"default_in_transit_warehouse",
	)


def get_source_warehouse(doc, flow):
	if flow == "issue":
		wh = _department_manufacturing_wh(TAGGING_DEPARTMENT)
		if not wh:
			frappe.throw(
				_("Please set the Manufacturing warehouse for department {0}.").format(
					TAGGING_DEPARTMENT
				)
			)
		return wh
	# receive: source is the PC transit warehouse used by the Issue
	wh = _department_transit_wh(PRODUCT_CERTIFICATION_DEPARTMENT)
	if not wh:
		frappe.throw(
			_("Please set the default in-transit warehouse for department {0}.").format(
				PRODUCT_CERTIFICATION_DEPARTMENT
			)
		)
	return wh


def get_target_warehouse(doc, flow):
	if flow == "issue":
		wh = _department_transit_wh(PRODUCT_CERTIFICATION_DEPARTMENT)
		if not wh:
			frappe.throw(
				_(
					"Please set the default in-transit warehouse for department {0}."
				).format(PRODUCT_CERTIFICATION_DEPARTMENT)
			)
		return wh
	# receive: land in the PC manufacturing (raw-material) warehouse
	wh = _department_manufacturing_wh(PRODUCT_CERTIFICATION_DEPARTMENT)
	if not wh:
		frappe.throw(
			_("Please set the Manufacturing warehouse for department {0}.").format(
				PRODUCT_CERTIFICATION_DEPARTMENT
			)
		)
	return wh


def validate_warehouse(warehouse, label):
	if not warehouse or not frappe.db.exists("Warehouse", warehouse):
		frappe.throw(_("Warehouse {0} does not exist ({1}).").format(warehouse, label))


# ---------------------------------------------------------------------------
# Reservation / row source (the project-specific seam)
# ---------------------------------------------------------------------------


def get_reserved_items_for_department_ir(doc, flow, s_wh, t_wh):
	"""
	Returns a list of Stock Entry item dicts.

	Strategy (this is the only function that should change if the
	reservation source-of-truth on the live site differs):

	  1. (Issue + Receive) Gate via Stock Reservation Entry when the
	         manufacturing_work_order / manufacturing_operation custom fields
	         are present on the SRE doctype.
	  2. (Receive) Prefer mirroring the Issue's Stock Entry rows, swapping
	         the source/target warehouses. Falls through if missing.
	  3. (Issue, or Receive fallback) Build rows from MOP Log per
	         Manufacturing Operation, reusing _build_transfer_rows_for_mop.
	"""
	_gate_with_stock_reservation_entry(doc)

	if flow == "receive":
		rows = _mirror_from_issue_stock_entry(doc, s_wh, t_wh)
		if rows:
			return _enrich_rows(rows, doc, flow)

	rows = _build_rows_from_mop_log(doc, s_wh, t_wh)
	return _enrich_rows(rows, doc, flow)


def _gate_with_stock_reservation_entry(doc):
	"""
	Log a warning when no Stock Reservation Entry exists for an MWO/MOP pair.

	This is informational only: in this codebase, Stock Reservation Entries are
	produced **by** the Stock Entry submission flow (see
	`doc_events.stock_entry.stock_reservation_entry_for_mwo`), not as a
	precondition for it. The empty-items check at the entry point is the real
	safety net against creating a Stock Entry with no source data.
	"""
	try:
		meta = frappe.get_meta("Stock Reservation Entry")
	except frappe.DoesNotExistError:
		return
	has_mwo = meta.has_field("manufacturing_work_order")
	has_mop = meta.has_field("manufacturing_operation")
	if not (has_mwo or has_mop):
		return

	logger = frappe.logger(LOGGER_NAME)
	for row in doc.department_ir_operation:
		filters = {"docstatus": 1}
		if has_mwo and row.manufacturing_work_order:
			filters["manufacturing_work_order"] = row.manufacturing_work_order
		if has_mop and row.manufacturing_operation:
			filters["manufacturing_operation"] = row.manufacturing_operation
		if len(filters) == 1:
			continue
		if not frappe.db.exists("Stock Reservation Entry", filters):
			logger.info(
				f"No SRE for MWO {row.manufacturing_work_order} / "
				f"MOP {row.manufacturing_operation} on Department IR {doc.name}"
			)


def _mirror_from_issue_stock_entry(doc, s_wh, t_wh):
	"""For the Receive flow, copy the Issue's Stock Entry items with swapped warehouses."""
	if not getattr(doc, "receive_against", None):
		return []
	issue_se_name = frappe.db.get_value(
		"Stock Entry",
		{
			"department_ir": doc.receive_against,
			"docstatus": 1,
			"stock_entry_type": STOCK_ENTRY_TYPE,
		},
		"name",
	)
	if not issue_se_name:
		return []

	issue_se = frappe.get_doc("Stock Entry", issue_se_name)
	rows = []
	sed_meta = frappe.get_meta("Stock Entry Detail")
	carry = [
		"item_code",
		"qty",
		"transfer_qty",
		"uom",
		"stock_uom",
		"conversion_factor",
		"batch_no",
		"serial_and_batch_bundle",
		"serial_no",
		"use_serial_batch_fields",
	]
	custom_carry = [
		"manufacturing_operation",
		"custom_manufacturing_work_order",
		"manufacturer",
		"inventory_type",
		"to_inventory_type",
		"branch",
		"cost_center",
		"expense_account",
		"custom_variant_of",
		"custom_pure_qty",
		"pcs",
	]
	for it in issue_se.items:
		row = {"s_warehouse": s_wh, "t_warehouse": t_wh}
		for f in carry:
			val = getattr(it, f, None)
			if val not in (None, ""):
				row[f] = val
		for f in custom_carry:
			if sed_meta.has_field(f):
				val = getattr(it, f, None)
				if val not in (None, ""):
					row[f] = val
		rows.append(row)
	return rows


def _build_rows_from_mop_log(doc, s_wh, t_wh):
	"""Build Stock Entry items from MOP Log per Manufacturing Operation."""
	rows = []
	seen_mops = set()
	for op_row in doc.department_ir_operation:
		mop_name = op_row.manufacturing_operation
		if not mop_name or mop_name in seen_mops:
			continue
		seen_mops.add(mop_name)

		logs = frappe.get_all(
			"MOP Log",
			filters={"manufacturing_operation": mop_name, "is_cancelled": 0},
			fields=[
				"name",
				"item_code",
				"qty_after_transaction_batch_based",
				"batch_no",
				"serial_no",
				"flow_index",
				"manufacturing_operation",
			],
			order_by="flow_index asc",
		)
		if not logs:
			continue

		mop_doc = frappe.get_cached_doc("Manufacturing Operation", mop_name)
		rows.extend(
			_build_transfer_rows_for_mop(
				{
					"mop_name": mop_name,
					"mop_doc": mop_doc,
					"logs": [frappe._dict(l) for l in logs],
				},
				s_wh,
				t_wh,
			)
		)
	return rows


def _enrich_rows(rows, doc, flow):
	"""Add Department IR-derived custom fields (guarded by Stock Entry Detail meta)."""
	if not rows:
		return rows
	sed_meta = frappe.get_meta("Stock Entry Detail")

	if flow == "issue":
		from_dept = doc.current_department
		to_dept = doc.next_department or PRODUCT_CERTIFICATION_DEPARTMENT
	else:
		# receive: material is moving within the PC department's warehouses
		from_dept = PRODUCT_CERTIFICATION_DEPARTMENT
		to_dept = PRODUCT_CERTIFICATION_DEPARTMENT

	for row in rows:
		if sed_meta.has_field("department") and "department" not in row:
			row["department"] = from_dept
		if sed_meta.has_field("to_department") and "to_department" not in row:
			row["to_department"] = to_dept
		if sed_meta.has_field("manufacturer") and getattr(doc, "manufacturer", None):
			row.setdefault("manufacturer", doc.manufacturer)
		if sed_meta.has_field("inventory_type") and not row.get("inventory_type"):
			row["inventory_type"] = "Regular Stock"
	return rows


# ---------------------------------------------------------------------------
# Stock Entry construction
# ---------------------------------------------------------------------------


def create_stock_entry(doc, items, s_wh, t_wh):
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = STOCK_ENTRY_TYPE
	se.purpose = PURPOSE
	se.company = doc.company

	se_meta = frappe.get_meta("Stock Entry")
	if se_meta.has_field("department_ir"):
		se.department_ir = doc.name
	if se_meta.has_field("auto_created"):
		se.auto_created = 1
	if se_meta.has_field("manufacturer") and getattr(doc, "manufacturer", None):
		se.manufacturer = doc.manufacturer
	if se_meta.has_field("add_to_transit"):
		se.add_to_transit = 0

	se.from_warehouse = s_wh
	se.to_warehouse = t_wh

	for it in items:
		row = dict(it)
		row.setdefault("s_warehouse", s_wh)
		row.setdefault("t_warehouse", t_wh)
		se.append("items", row)

	se.flags.ignore_permissions = True
	se.insert(ignore_permissions=True)
	se.submit()
	return se.name
