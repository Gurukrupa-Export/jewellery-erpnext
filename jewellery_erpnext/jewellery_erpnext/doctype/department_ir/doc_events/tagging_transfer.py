# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	get_current_mop_balance_rows,
)

PRODUCT_CERT_DEPT = "Product Certification"
TAGGING_DEPT = "Tagging"

_TOLERANCE = 1e-9


def should_handle_tagging_issue(doc):
	return (
		doc.type == "Issue"
		and PRODUCT_CERT_DEPT in doc.current_department
		and TAGGING_DEPT in doc.next_department
	)


def should_handle_tagging_receive(doc):
	return (
		doc.type == "Receive"
		and PRODUCT_CERT_DEPT in doc.previous_department
		and TAGGING_DEPT in doc.current_department
	)


def handle_tagging_issue(doc, row, new_operation_name, t_warehouse):
	"""Create Material Transfer SE + manage SREs when Product Certification issues to Tagging.

	Called inside on_submit_issue_new loop, AFTER create_operation_for_next_dept.

	Parameters:
	  doc               : Department IR document
	  row               : Department IR Operation child row
	  new_operation_name: Tagging MOP just created by create_operation_for_next_dept
	  t_warehouse       : in_transit_wh already resolved in on_submit_issue_new
	                      = default_in_transit_warehouse of Tagging MFG WH ("Tagging Transit - GEPL")
	"""
	if not t_warehouse:
		frappe.throw(
			_(
				"Cannot process Tagging transfer: transit warehouse not found for department {0}"
			).format(doc.next_department)
		)

	mop_name = row.manufacturing_operation
	mwo_name = row.manufacturing_work_order

	# Idempotency guard
	existing_se = frappe.db.get_value(
		"Stock Entry",
		{
			"department_ir": doc.name,
			"manufacturing_work_order": mwo_name,
			"stock_entry_type": "Material Transfer to Department",
			"docstatus": ["!=", 2],
		},
	)
	if existing_se:
		frappe.msgprint(
			_(
				"Stock Entry {0} already exists for Department IR {1} / MWO {2}. Skipping."
			).format(existing_se, doc.name, mwo_name)
		)
		return

	mop_balance_rows = get_current_mop_balance_rows(mop_name)
	if not mop_balance_rows:
		frappe.throw(
			_(
				"No MOP Log balance found for Manufacturing Operation {0}. Cannot create Material Transfer."
			).format(mop_name)
		)

	sre_list = _get_active_sres(mwo_name, mop_name)
	if not sre_list:
		frappe.throw(
			_(
				"No active Stock Reservation Entry found for MWO {0} / MOP {1}. Cannot determine source warehouse."
			).format(mwo_name, mop_name)
		)

	se_items = _build_se_items_from_mop_balance(
		mop_balance_rows, sre_list, t_warehouse, mop_name
	)
	if not se_items:
		frappe.throw(
			_(
				"No valid items found in MOP balance for MOP {0}. Cannot create Material Transfer."
			).format(mop_name)
		)

	cancelled_sres = _cancel_sres(sre_list)

	try:
		_create_and_submit_se(
			doc,
			se_items,
			stock_entry_type="Material Transfer to Department",
			mwo_name=mwo_name,
			mop_name=mop_name,
		)
		# New SRE points to transit warehouse under the new Tagging MOP
		_recreate_sres(
			cancelled_sres, new_warehouse=t_warehouse, new_mop_name=new_operation_name
		)
	except Exception:
		frappe.log_error(
			title="Tagging Transfer SE failed for Department IR {0}".format(doc.name),
			message=frappe.get_traceback(),
		)
		raise


def handle_tagging_receive(doc, row, t_warehouse):
	"""Create Material Transfer SE + manage SREs when Tagging receives from Product Certification.

	Called inside on_submit_receive loop.

	Parameters:
	  doc        : Department IR document
	  row        : Department IR Operation child row (manufacturing_operation = Tagging MOP)
	  t_warehouse: department_wh already resolved in on_submit_receive
	               = Tagging Manufacturing WH ("Tagging WO - GEPL")
	"""
	if not t_warehouse:
		frappe.throw(
			_(
				"Cannot process Tagging receive: manufacturing warehouse not found for department {0}"
			).format(doc.current_department)
		)

	mop_name = row.manufacturing_operation
	mwo_name = row.manufacturing_work_order

	existing_se = frappe.db.get_value(
		"Stock Entry",
		{
			"department_ir": doc.name,
			"manufacturing_work_order": mwo_name,
			"stock_entry_type": "Material Transfer to Department",
			"docstatus": ["!=", 2],
		},
	)
	if existing_se:
		frappe.msgprint(
			_(
				"Stock Entry {0} already exists for Department IR Receive {1}. Skipping."
			).format(existing_se, doc.name)
		)
		return

	mop_balance_rows = get_current_mop_balance_rows(mop_name)
	if not mop_balance_rows:
		frappe.throw(
			_("No MOP Log balance found for Manufacturing Operation {0}.").format(
				mop_name
			)
		)

	sre_list = _get_active_sres(mwo_name, mop_name)
	if not sre_list:
		frappe.throw(
			_("No active Stock Reservation Entry found for MWO {0} / MOP {1}.").format(
				mwo_name, mop_name
			)
		)

	se_items = _build_se_items_from_mop_balance(
		mop_balance_rows, sre_list, t_warehouse, mop_name
	)
	if not se_items:
		frappe.throw(
			_("No valid items found in MOP balance for MOP {0}.").format(mop_name)
		)

	cancelled_sres = _cancel_sres(sre_list)

	try:
		_create_and_submit_se(
			doc,
			se_items,
			stock_entry_type="Material Transfer to Department",
			mwo_name=mwo_name,
			mop_name=mop_name,
		)
		# Receive: same MOP, warehouse updated to Tagging WO
		_recreate_sres(cancelled_sres, new_warehouse=t_warehouse, new_mop_name=mop_name)
	except Exception:
		frappe.log_error(
			title="Tagging Receive SE failed for Department IR {0}".format(doc.name),
			message=frappe.get_traceback(),
		)
		raise


# ── Private helpers ────────────────────────────────────────────────────────────


def _get_active_sres(mwo_name, mop_name):
	"""Return submitted SREs filtered by MWO and MOP."""
	sre_cols = frappe.db.get_table_columns("Stock Reservation Entry")
	filters = {"docstatus": 1}
	if "manufacturing_work_order" in sre_cols:
		filters["manufacturing_work_order"] = mwo_name
	if "manufacturing_operation" in sre_cols:
		filters["manufacturing_operation"] = mop_name
	return frappe.db.get_all(
		"Stock Reservation Entry",
		filters=filters,
		fields=[
			"name",
			"item_code",
			"warehouse",
			"reserved_qty",
			"delivered_qty",
			"reservation_based_on",
			"manufacturing_work_order",
			"manufacturing_operation",
			"voucher_type",
			"voucher_no",
			"voucher_detail_no",
			"voucher_qty",
			"company",
			"stock_uom",
		],
	)


def _build_se_items_from_mop_balance(mop_balance_rows, sre_list, t_warehouse, mop_name):
	"""Build Stock Entry item dicts from MOP Log balance.

	s_warehouse is taken from the first SRE — all SREs for the same MOP should
	point to the same warehouse.
	"""
	s_warehouse = sre_list[0]["warehouse"] if sre_list else None
	if not s_warehouse:
		frappe.throw(
			_(
				"Could not resolve source warehouse from Stock Reservation Entry for MOP {0}"
			).format(mop_name)
		)

	se_items = []
	for balance_row in mop_balance_rows:
		item_code = balance_row.get("item_code")
		qty = flt(
			balance_row.get("qty_after_transaction_batch_based")
			or balance_row.get("qty_after_transaction")
			or 0
		)
		batch_no = balance_row.get("batch_no")

		if not item_code or qty <= _TOLERANCE:
			continue

		item_dict = {
			"item_code": item_code,
			"qty": qty,
			"s_warehouse": s_warehouse,
			"t_warehouse": t_warehouse,
			"batch_no": batch_no,
			"use_serial_batch_fields": True,
			"manufacturing_operation": mop_name,
		}

		# pcs for diamond (D*) and gemstone (G*) items
		if item_code[0] in ("D", "G"):
			pcs = balance_row.get("pcs_after_transaction_batch_based")
			if pcs is not None:
				item_dict["pcs"] = pcs

		se_items.append(item_dict)

	return se_items


def _cancel_sres(sre_list):
	"""Cancel all submitted SREs. Returns list of cancelled SRE docs."""
	cancelled = []
	for sre_row in sre_list:
		try:
			sre_doc = frappe.get_doc("Stock Reservation Entry", sre_row["name"])
			if sre_doc.docstatus == 1:
				sre_doc.ignore_permissions = True
				sre_doc.cancel()
				cancelled.append(sre_doc)
		except Exception:
			frappe.log_error(
				title="Failed to cancel SRE {0}".format(sre_row["name"]),
				message=frappe.get_traceback(),
			)
			raise
	return cancelled


def _create_and_submit_se(doc, se_items, stock_entry_type, mwo_name, mop_name):
	"""Create, save, and submit a Stock Entry."""
	se_doc = frappe.new_doc("Stock Entry")
	se_doc.stock_entry_type = stock_entry_type
	se_doc.company = doc.company
	se_doc.department_ir = doc.name
	se_doc.manufacturing_work_order = mwo_name
	se_doc.manufacturing_operation = mop_name
	se_doc.auto_created = 1
	se_doc.add_to_transit = 0

	for item in se_items:
		se_doc.append("items", item)

	se_doc.flags.ignore_permissions = True
	se_doc.save()
	se_doc.submit()
	return se_doc


def _recreate_sres(cancelled_sre_docs, new_warehouse, new_mop_name):
	"""Recreate SREs with updated warehouse and manufacturing_operation."""
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_available_qty_to_reserve,
	)

	for original_sre in cancelled_sre_docs:
		reserved_qty = flt(original_sre.reserved_qty) - flt(original_sre.delivered_qty)
		if reserved_qty <= _TOLERANCE:
			continue

		has_batch_no, has_serial_no = frappe.get_cached_value(
			"Item", original_sre.item_code, ["has_batch_no", "has_serial_no"]
		)

		# Rebuild batch rows from the old SRE
		sb_entries = []
		if cint(has_batch_no):
			old_sb = frappe.get_all(
				"Serial and Batch Entry",
				filters={"parent": original_sre.name},
				fields=["batch_no", "qty", "delivered_qty"],
			)
			for sb in old_sb:
				remaining_sb = flt(sb.qty) - flt(sb.delivered_qty)
				if remaining_sb > _TOLERANCE:
					sb_entries.append({"batch_no": sb.batch_no, "qty": remaining_sb})
			if not sb_entries:
				continue  # batch item with no remaining batch rows — skip

		# Compute available_qty for the new warehouse
		if sb_entries and cint(has_batch_no):
			available_qty = get_available_qty_to_reserve(
				original_sre.item_code,
				new_warehouse,
				batch_no=sb_entries[0]["batch_no"],
			)
		else:
			available_qty = get_available_qty_to_reserve(
				original_sre.item_code, new_warehouse
			)

		new_sre = frappe.new_doc("Stock Reservation Entry")
		new_sre.voucher_type = original_sre.voucher_type
		new_sre.voucher_no = original_sre.voucher_no
		new_sre.voucher_detail_no = original_sre.voucher_detail_no
		new_sre.voucher_qty = original_sre.voucher_qty
		new_sre.item_code = original_sre.item_code
		new_sre.warehouse = new_warehouse
		new_sre.reserved_qty = reserved_qty
		new_sre.company = original_sre.company
		new_sre.stock_uom = original_sre.stock_uom
		new_sre.reservation_based_on = original_sre.reservation_based_on
		new_sre.has_batch_no = cint(has_batch_no)
		new_sre.has_serial_no = cint(has_serial_no)
		new_sre.available_qty = max(flt(available_qty), reserved_qty)
		new_sre.manufacturing_work_order = original_sre.manufacturing_work_order
		new_sre.manufacturing_operation = new_mop_name

		for sb in sb_entries:
			new_sre.append("sb_entries", {"batch_no": sb["batch_no"], "qty": sb["qty"]})

		new_sre.flags.ignore_permissions = True
		new_sre.insert(ignore_links=1)
		new_sre.submit()


# ---------------------------------------------------------------------------
# Public API aliases used by DepartmentIR controller
# ---------------------------------------------------------------------------


def should_process_department_transfer(doc, transfer_type, warehouse=None):
	"""Unified gate check called by Department IR controller for Issue and Receive."""
	if transfer_type == "Issue":
		return should_handle_tagging_issue(doc)
	if transfer_type == "Receive":
		return should_handle_tagging_receive(doc)
	return False


def handle_department_transfer_issue(doc, row, new_operation_name, t_warehouse):
	return handle_tagging_issue(doc, row, new_operation_name, t_warehouse)


def handle_department_transfer_receive(doc, row, t_warehouse):
	return handle_tagging_receive(doc, row, t_warehouse)
