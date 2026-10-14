# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Chain Subcontracting for Metal Conversions.

A chain conversion sends the source metal to a subcontractor and takes the converted
item back for another employee. It runs on the Gemstone Conversion resize workflow
(``patches/add_metal_conversion_workflow.py``)::

    Draft --Issue--> Issue --Receive--> Receive --Submit--> Submitted
                     Issue --Cancel Issue--> Draft          (System Manager)

**Issue** (a save, docstatus 0) -- ``run_issue`` from ``MetalConversions.validate``:

* "Material Transfer (Chain SC)": the FIFO-allocated source batches, employee's
  department RM warehouse -> supplier's RM warehouse;
* a draft Purchase Order for the "Semi FG Subcontracting" service, left for the buying
  team to price and submit.

**Receive** (the submit) -- ``run_receive`` from ``MetalConversions.on_submit``:

* "Repack-Metal Conversion" inside the supplier's RM warehouse: Target Qty of the source
  batches -> Target Qty of the Target Item, built with the regular conversion's lane
  helpers so pricing and batch provenance work exactly as for any conversion;
* "Process Loss" for Source Qty - Target Qty (skipped when zero): the remaining source
  batches -> the loss item in the issuing department's Scrap warehouse, through the
  melting-loss builder;
* "Material Transfer (Chain SC)": the batch the Repack minted, supplier RM warehouse ->
  the To Employee's department RM warehouse.

**Cancel Issue** (a save back to Draft) -- ``run_cancel_issue``: an Issue that has not been
received is undone -- its transfer cancelled, its draft PO deleted -- so a wrong supplier,
quantity or batch can be corrected and issued again.

The Repack consumes Target Qty of the source, not all of it: consuming Source Qty there
would leave the Process Loss entry nothing to consume. Repack plus Process Loss together
consume exactly the Source Qty that Issue moved. The target is typed in, so there is no
purity conversion to check it against; it may only not hold more fine metal than was issued.

Receive and Cancel each post several Stock Entries in one transaction, so every naming
counter and then every Bin they touch is locked once, up front, in the canonical order
(``lock_order`` RULE A/B) -- each entry pinning only its own would take a later counter
after earlier Bins.

Chain Subcontracting draws Regular Stock only (``update_source_betch``): the Process Loss
books its scrap as Regular Stock, so customer metal would change hands there.

Every warehouse is resolved here, never picked by the user, and the allocation is frozen
once Issue has moved the stock -- re-allocating against the department warehouse after
that would find nothing there.
"""

import frappe
from erpnext.stock.doctype.batch.batch import get_batch_qty
from frappe import _
from frappe.utils import cint, flt, nowdate

from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events.lanes import (
	build_lanes,
	split_allocations,
	split_conversion,
)
from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events.utils import (
	get_batch_lane_map,
)

CHAIN_SC_SE_TYPE = "Material Transfer (Chain SC)"
CHAIN_SC_PO_ITEM = "Semi FG Subcontracting"
CHAIN_SC_PURCHASE_TYPE = "Subcontracting"

DRAFT_STATE = "Draft"
ISSUE_STATE = "Issue"
RECEIVE_STATE = "Receive"

RAW_MATERIAL = "Raw Material"
REGULAR_STOCK = "Regular Stock"

DEPARTMENT_FLAGS = ("custom_is_chain_manufacturing", "custom_is_chain_subcontracting")

# What Issue moved stock for. Changing any of these afterwards would describe metal that
# is not where the document says it is.
FROZEN_AFTER_ISSUE = (
	"employee",
	"department",
	"source_warehouse",
	"source_item",
	"source_qty",
	"is_chain_manufacturing",
	"is_chain_subcontracting",
	"chain_supplier",
	"chain_supplier_warehouse",
	"chain_issue_stock_entry",
	"chain_purchase_order",
	"multiple_metal_converter",
	"is_melting_loss",
)

# Cleared whenever a document is not a chain conversion.
CHAIN_FIELDS = (
	"to_employee",
	"to_department",
	"to_warehouse",
	"chain_supplier",
	"chain_supplier_warehouse",
)


def is_chain_sc(doc):
	return doc.get("is_chain_manufacturing") == "Yes" and bool(
		cint(doc.get("is_chain_subcontracting"))
	)


def chain_issued(doc):
	return bool(doc.get("chain_issue_stock_entry"))


# ---------------------------------------------------------------------------
# Master lookups
# ---------------------------------------------------------------------------


def get_department_chain_flags(department):
	"""``(is_chain_manufacturing, is_chain_subcontracting)`` of a Department.

	Both False until ``add_metal_conversion_chain_sc`` has created the two fields -- a
	missing column must not break every save of the doctype.
	"""
	if not department:
		return False, False
	meta = frappe.get_meta("Department")
	if not all(meta.has_field(field) for field in DEPARTMENT_FLAGS):
		return False, False
	values = frappe.db.get_value(
		"Department", department, DEPARTMENT_FLAGS, as_dict=True
	)
	if not values:
		return False, False
	return (
		bool(cint(values.custom_is_chain_manufacturing)),
		bool(cint(values.custom_is_chain_subcontracting)),
	)


def get_department_rm_warehouse(department):
	"""The department's Raw Material warehouse -- the filter ``get_detail_tab_value`` uses."""
	if not department:
		return None
	return frappe.db.get_value(
		"Warehouse",
		{"disabled": 0, "department": department, "warehouse_type": RAW_MATERIAL},
		"name",
	)


def get_supplier_rm_warehouse(supplier, company=None):
	"""The subcontractor's Raw Material warehouse (``Warehouse.subcontractor``)."""
	if not supplier:
		return None
	filters = {
		"disabled": 0,
		"is_group": 0,
		"subcontractor": supplier,
		"warehouse_type": RAW_MATERIAL,
	}
	if company:
		filters["company"] = company
	return frappe.db.get_value("Warehouse", filters, "name")


# ---------------------------------------------------------------------------
# Save-time: field resolution and guards
# ---------------------------------------------------------------------------


def validate_chain(doc):
	"""Resolve every chain field from its masters and hold the chain rules.

	Runs on every save. A non-chain document only gets its department flags refreshed and
	the chain fields cleared.
	"""
	issued = chain_issued(doc) or _issued_before_save(doc)
	if issued:
		_check_issue_frozen(doc)

	if doc.get("is_chain_manufacturing") == "Yes" and not issued and doc.employee:
		# The Department follows the Employee; it is never picked by hand for a chain.
		employee_department = frappe.db.get_value(
			"Employee", doc.employee, "department"
		)
		if not employee_department:
			frappe.throw(
				_("Department is missing against Employee {0}.").format(
					frappe.bold(doc.employee)
				)
			)
		if employee_department != doc.department:
			doc.department = employee_department
			doc.manufacturer = (
				frappe.db.get_value("Department", employee_department, "manufacturer")
				or doc.manufacturer
			)

	manufacturing, subcontracting = get_department_chain_flags(doc.department)
	doc.dept_is_chain_manufacturing = cint(manufacturing)
	doc.dept_is_chain_subcontracting = cint(subcontracting)

	if doc.get("is_chain_manufacturing") != "Yes":
		doc.is_chain_manufacturing = "No"
		doc.is_chain_subcontracting = 0
		for field in CHAIN_FIELDS:
			doc.set(field, None)
		doc.chain_process_loss_qty = 0
		return

	if not issued:
		_check_eligibility(doc, manufacturing, subcontracting)
		if cint(doc.multiple_metal_converter):
			frappe.throw(
				_(
					"Chain Manufacturing converts a single Source Item. Uncheck Multiple Metal "
					"Converter."
				)
			)
		if cint(doc.get("is_melting_loss")):
			frappe.throw(
				_(
					"Chain Manufacturing books its Process Loss at Receive. Uncheck Is Melting "
					"Loss."
				)
			)
		_clear_alloy(doc)
		set_issue_warehouses(doc)

	set_to_warehouse(doc)
	doc.chain_process_loss_qty = (
		flt(flt(doc.source_qty) - flt(doc.target_qty), _precision())
		if flt(doc.target_qty)
		else 0
	)


def _check_eligibility(doc, manufacturing, subcontracting):
	if not manufacturing:
		frappe.throw(
			_(
				"Department {0} is not set up for Chain Manufacturing. Set Is Chain "
				"Manufacturing to No, or tick Is Chain Manufacturing on the Department."
			).format(frappe.bold(doc.department))
		)
	if not subcontracting:
		frappe.throw(
			_(
				"Department {0} is not set up for Chain Subcontracting, and Chain Manufacturing "
				"is only available through a subcontractor. Set Is Chain Manufacturing to No."
			).format(frappe.bold(doc.department))
		)
	if not cint(doc.is_chain_subcontracting):
		frappe.throw(
			_(
				"Tick Is Chain Subcontracting and select the Supplier, or set Is Chain "
				"Manufacturing to No."
			)
		)


def set_issue_warehouses(doc):
	"""Department RM -> supplier RM: both resolved from their masters."""
	department_wh = get_department_rm_warehouse(doc.department)
	if doc.department and not department_wh:
		frappe.throw(
			_(
				"No Raw Material warehouse found for Department {0}. Configure a Warehouse "
				"with Warehouse Type = Raw Material and Department = {0}."
			).format(frappe.bold(doc.department))
		)
	if department_wh:
		doc.source_warehouse = department_wh
		doc.target_warehouse = department_wh

	doc.chain_supplier_warehouse = None
	if doc.chain_supplier:
		doc.chain_supplier_warehouse = get_supplier_rm_warehouse(
			doc.chain_supplier, doc.company
		)
		if not doc.chain_supplier_warehouse:
			frappe.throw(
				_(
					"No Raw Material warehouse found for Supplier {0}. Configure a Warehouse "
					"with Warehouse Type = Raw Material and Subcontractor = {0}."
				).format(frappe.bold(doc.chain_supplier))
			)


def set_to_warehouse(doc):
	"""To Employee -> their Department -> its RM warehouse, the final destination."""
	doc.to_department = None
	doc.to_warehouse = None
	if not doc.to_employee:
		return
	doc.to_department = frappe.db.get_value("Employee", doc.to_employee, "department")
	if not doc.to_department:
		frappe.throw(
			_("Department is missing against To Employee {0}.").format(
				frappe.bold(doc.to_employee)
			)
		)
	doc.to_warehouse = get_department_rm_warehouse(doc.to_department)
	if not doc.to_warehouse:
		frappe.throw(
			_(
				"No Raw Material warehouse found for Department {0} of To Employee {1}. "
				"Configure a Warehouse with Warehouse Type = Raw Material and Department = {0}."
			).format(frappe.bold(doc.to_department), frappe.bold(doc.to_employee))
		)


def _issued_before_save(doc):
	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	return bool(before and before.get("chain_issue_stock_entry"))


def _check_issue_frozen(doc):
	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	if not before:
		return

	changed = []
	for field in FROZEN_AFTER_ISSUE:
		old, new = before.get(field), doc.get(field)
		if field == "source_qty":
			same = flt(old, _precision()) == flt(new, _precision())
		elif field in (
			"is_chain_subcontracting",
			"multiple_metal_converter",
			"is_melting_loss",
		):
			same = cint(old) == cint(new)
		else:
			same = (old or None) == (new or None)
		if not same:
			changed.append(_(doc.meta.get_label(field)))

	if _allocation_key(before) != _allocation_key(doc):
		changed.append(_(doc.meta.get_label("source_batch_details")))

	if changed:
		frappe.throw(
			_(
				"{0} cannot be changed after Issue: the metal has already been transferred to "
				"the supplier in {1}."
			).format(
				", ".join(changed), frappe.bold(before.get("chain_issue_stock_entry"))
			),
			title=_("Already Issued"),
		)


def _allocation_key(doc):
	return [
		(row.get("batch"), flt(row.get("qty"), _precision()))
		for row in _source_rows(doc)
	]


def _clear_alloy(doc):
	"""A chain conversion carries no alloy; the API does not clear hidden fields."""
	doc.source_alloy_check = 0
	doc.source_alloy = None
	doc.source_alloy_qty = None
	doc.source_alloy_batch = None
	doc.target_alloy_check = 0
	doc.target_alloy = None
	doc.target_alloy_qty = 0
	doc.alloy_batch_details = []


def after_validate(doc):
	"""Last step of ``validate``: show the allocated batch and run Issue when it is due."""
	if not is_chain_sc(doc):
		return
	if doc.get("workflow_state") == DRAFT_STATE and chain_issued(doc):
		# Back to Draft with an Issue still linked: only the Cancel Issue transition
		# leads here (validate_workflow refuses any other Issue -> Draft change).
		run_cancel_issue(doc)
		return
	rows = _source_rows(doc)
	if not chain_issued(doc):
		doc.batch = rows[0].batch if rows else None
	if doc.get("workflow_state") == ISSUE_STATE and not chain_issued(doc):
		run_issue(doc)


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------


def run_issue(doc):
	validate_issue(doc)
	doc.chain_issue_stock_entry = make_issue_stock_entry(doc)
	doc.chain_purchase_order = make_subcontracting_po(doc)


def run_cancel_issue(doc):
	"""Undo an Issue that has not been received (the Cancel Issue action).

	The Issue transfer is cancelled through its controller, so ERPNext refuses when the
	metal has already moved on from the supplier's warehouse; a draft PO is deleted. The
	document is a Draft again -- its allocation still holds, the metal being back in the
	department -- to be corrected and issued afresh. A submitted PO belongs to the buying
	team and stops it, as it stops a cancel after Receive.
	"""
	po = doc.get("chain_purchase_order")
	po_status = frappe.db.get_value("Purchase Order", po, "docstatus") if po else None
	if po_status == 1:
		frappe.throw(
			_("Cancel Purchase Order {0} before cancelling the Issue.").format(
				frappe.bold(po)
			),
			title=_("Purchase Order Submitted"),
		)

	# Already cancelled by hand is fine: _cancel_stock_entry only cancels a live entry.
	_cancel_stock_entry(doc.chain_issue_stock_entry)
	if po_status == 0:
		# force: this conversion still links the PO until the save in progress is written.
		frappe.delete_doc("Purchase Order", po, force=True, ignore_permissions=True)

	doc.chain_issue_stock_entry = None
	doc.chain_purchase_order = None
	# A target typed in the Issue state described a receipt that will not happen now.
	doc.target_item = None
	doc.target_qty = 0
	doc.chain_process_loss_qty = 0


def validate_issue(doc):
	"""Spec 11: everything the Issue transfer and the PO need, reported together."""
	if doc.is_new():
		frappe.throw(_("Save the conversion before issuing it."))

	errors = []
	manufacturing, subcontracting = get_department_chain_flags(doc.department)
	precision = _precision()

	if not doc.employee:
		errors.append(_("Employee is not selected."))
	if not doc.department:
		errors.append(_("The Employee's Department could not be determined."))
	elif not (manufacturing and subcontracting):
		errors.append(
			_("Department {0} is not eligible for Chain Subcontracting.").format(
				frappe.bold(doc.department)
			)
		)
	if doc.is_chain_manufacturing != "Yes":
		errors.append(_("Is Chain Manufacturing must be Yes."))
	if not doc.to_employee:
		errors.append(_("To Employee is not selected."))
	elif not doc.to_department:
		errors.append(_("The To Employee's Department could not be determined."))
	if not doc.chain_supplier:
		errors.append(_("Supplier is not selected."))
	if not doc.source_item:
		errors.append(_("Source Item is not selected."))
	if flt(doc.source_qty, precision) <= 0:
		errors.append(_("Source Qty must be greater than zero."))
	if not doc.source_warehouse:
		errors.append(_("The Employee's Department has no Raw Material warehouse."))
	if not doc.chain_supplier_warehouse:
		errors.append(_("The Supplier has no Raw Material warehouse."))
	if not doc.to_warehouse:
		errors.append(_("The To Employee's Department has no Raw Material warehouse."))
	po_uom, whole_number = _po_item_uom()
	if not po_uom:
		errors.append(
			_(
				"Item {0}, which the Purchase Order is raised for, does not exist."
			).format(frappe.bold(CHAIN_SC_PO_ITEM))
		)
	elif whole_number and not float(flt(doc.source_qty, precision)).is_integer():
		# PurchaseOrder.validate refuses a fraction in a whole-number UOM -- after the
		# Issue transfer was already booked, so the whole Issue would roll back there.
		errors.append(
			_(
				"Item {0} is in {1}, a whole-number UOM, so Source Qty {2} cannot go on "
				"its Purchase Order. Set the item's Stock UOM to Gram."
			).format(
				frappe.bold(CHAIN_SC_PO_ITEM),
				frappe.bold(po_uom),
				flt(doc.source_qty, precision),
			)
		)

	rows = _source_rows(doc)
	if not rows:
		errors.append(_("No batch is allocated for the Source Item."))
	elif doc.source_item and doc.source_warehouse:
		allocated = flt(sum(flt(row.qty) for row in rows), precision)
		if abs(allocated - flt(doc.source_qty, precision)) > _qty_unit(precision):
			errors.append(
				_(
					"The allocated batches hold {0}, not the Source Qty {1}. Save again."
				).format(allocated, flt(doc.source_qty, precision))
			)
		batch_items = dict(
			frappe.get_all(
				"Batch",
				filters={"name": ["in", list({row.batch for row in rows})]},
				fields=["name", "item"],
				as_list=True,
			)
		)
		for row in rows:
			if batch_items.get(row.batch) != doc.source_item:
				errors.append(
					_("Batch {0} does not belong to Source Item {1}.").format(
						frappe.bold(row.batch), frappe.bold(doc.source_item)
					)
				)
				continue
			available = flt(
				get_batch_qty(batch_no=row.batch, warehouse=doc.source_warehouse)
			)
			if flt(row.qty, precision) - available > _qty_unit(precision):
				errors.append(
					_("Batch {0} has only {1} in {2}; {3} is needed.").format(
						frappe.bold(row.batch),
						flt(available, precision),
						doc.source_warehouse,
						flt(row.qty, precision),
					)
				)

	if errors:
		frappe.throw("<br>".join(errors), title=_("Cannot Issue"))


def make_issue_stock_entry(doc):
	"""Department RM -> supplier RM, one row per allocated batch."""
	rows = _sorted_allocations(
		doc.source_item, doc.source_warehouse, _source_allocations(doc)
	)
	return _make_transfer(
		doc,
		item_code=doc.source_item,
		rows=rows,
		from_wh=doc.source_warehouse,
		to_wh=doc.chain_supplier_warehouse,
	)


def make_subcontracting_po(doc):
	"""A draft PO for the subcontractor's service, priced and submitted by buying.

	The shape of Gemstone Conversion's ``create_fg_subcontracting_po``, with dates that
	cannot precede today (a back-dated conversion would fail ERPNext's Required By check)
	and the "Subcontracting" purchase type, which gives it the job-work GST template
	(``doc_events.purchase_order.set_gst_details``).
	"""
	today = nowdate()
	po = frappe.new_doc("Purchase Order")
	po.supplier = doc.chain_supplier
	po.company = doc.company
	po.transaction_date = today
	po.schedule_date = today
	po.purchase_type = CHAIN_SC_PURCHASE_TYPE
	po.branch = doc.branch
	po.custom_branch = doc.branch
	po.custom_metal_conversion = doc.name

	company_address = _linked_address("Company", doc.company)
	supplier_address = _linked_address("Supplier", doc.chain_supplier)
	if company_address:
		# Purchase Order has no company_address (Gemstone's assignment is a no-op);
		# billing_address is the field set_gst_details reads.
		po.billing_address = company_address
	if supplier_address:
		po.supplier_address = supplier_address

	po.append(
		"items",
		{
			"item_code": CHAIN_SC_PO_ITEM,
			"qty": flt(doc.source_qty, _precision()),
			# Billed per gram issued, in the item's own stock UOM: a purchase UOM would
			# need a conversion row and could be a whole-number one.
			"uom": _po_item_uom()[0],
			"conversion_factor": 1,
			"schedule_date": today,
		},
	)
	# System-generated: the operator issuing metal need not hold buying rights.
	po.flags.ignore_permissions = True
	po.save()
	return po.name


def _po_item_uom():
	"""``(stock_uom, must_be_whole_number)`` of the PO service item; ``(None, 0)`` if missing.

	ERPNext ships "Nos" as whole-number, which is why ``add_metal_conversion_chain_sc``
	creates the item in Gram; a site that has it in a whole-number UOM is told so at Issue.
	"""
	uom = frappe.db.get_value("Item", CHAIN_SC_PO_ITEM, "stock_uom")
	if not uom:
		return None, 0
	return uom, cint(frappe.db.get_value("UOM", uom, "must_be_whole_number"))


def _linked_address(link_doctype, link_name):
	return frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": link_doctype, "link_name": link_name, "parenttype": "Address"},
		"parent",
	)


# ---------------------------------------------------------------------------
# Receive
# ---------------------------------------------------------------------------


def validate_receive(doc):
	"""Spec 16, from ``before_submit``: a chain conversion is submitted only by Receive.

	The Purchase Order is deliberately not read: buying prices and submits it on its own
	(spec 13) and spec 16 lists no PO check. So Receive neither waits for the PO or its
	invoice nor adds the job-work charge to the received item -- unlike Gemstone
	Conversion's subcontracted Resize (``get_subcontracting_pi_amount``). Decision recorded
	on PR #1433.
	"""
	if doc.get("workflow_state") != RECEIVE_STATE:
		frappe.throw(
			_(
				"A Chain Subcontracting conversion is submitted by its Receive action, after "
				"Issue."
			),
			title=_("Cannot Submit"),
		)

	errors = []
	precision = _precision()
	source_qty = flt(doc.source_qty, precision)
	target_qty = flt(doc.target_qty, precision)

	if not chain_issued(doc):
		errors.append(_("Issue has not been completed."))
	elif (
		frappe.db.get_value("Stock Entry", doc.chain_issue_stock_entry, "docstatus")
		!= 1
	):
		errors.append(
			_("Issue Stock Entry {0} is not submitted.").format(
				frappe.bold(doc.chain_issue_stock_entry)
			)
		)
	if source_qty <= 0:
		errors.append(_("Source Qty is not available."))
	if not doc.target_item:
		errors.append(_("Target Item is not selected."))
	if target_qty <= 0:
		errors.append(_("Target Qty must be greater than zero."))
	elif target_qty - source_qty > _qty_unit(precision):
		errors.append(
			_("Target Qty {0} cannot be greater than Source Qty {1}.").format(
				target_qty, source_qty
			)
		)
	else:
		errors.extend(_fine_metal_errors(doc, source_qty, target_qty, precision))
	if not doc.chain_supplier_warehouse:
		errors.append(_("The Supplier has no Raw Material warehouse."))
	if not doc.to_employee:
		errors.append(_("To Employee is not selected."))
	if not doc.to_warehouse:
		errors.append(_("The To Employee's Department has no Raw Material warehouse."))

	if doc.chain_supplier_warehouse:
		for row in _source_rows(doc):
			available = flt(
				get_batch_qty(
					batch_no=row.batch, warehouse=doc.chain_supplier_warehouse
				)
			)
			if flt(row.qty, precision) - available > _qty_unit(precision):
				errors.append(
					_("Batch {0} has only {1} of {2} in {3}; {4} was issued.").format(
						frappe.bold(row.batch),
						flt(available, precision),
						doc.source_item,
						doc.chain_supplier_warehouse,
						flt(row.qty, precision),
					)
				)

	if errors:
		frappe.throw("<br>".join(errors), title=_("Cannot Receive"))


def _fine_metal_errors(doc, source_qty, target_qty, precision):
	"""A supplier cannot add gold: the target may not hold more fine metal than was issued.

	Fine metal is qty x Metal Purity. Skipped when either item has no purity on its masters
	-- the target is typed in, so this is the only physical bound there is. Whether the
	loss itself needs a limit or an approval is a business rule not yet set.
	"""
	if not doc.target_item:
		return []
	source_purity = _metal_purity(doc.source_item)
	target_purity = _metal_purity(doc.target_item)
	if not (source_purity and target_purity):
		return []
	issued_fine = flt(source_qty * source_purity / 100, precision)
	received_fine = flt(target_qty * target_purity / 100, precision)
	if received_fine - issued_fine <= _qty_unit(precision):
		return []
	return [
		_(
			"The target holds {0} of fine metal ({1} at {2}%) but only {3} was issued "
			"({4} at {5}%). Check the Target Item and Target Qty."
		).format(
			received_fine,
			target_qty,
			target_purity,
			issued_fine,
			source_qty,
			source_purity,
		)
	]


def _metal_purity(item_code):
	"""An item's Metal Purity in percent, or None when its masters carry none.

	The lookup ``metal_conversions.get_metal_purity_percentage`` makes, without its throw:
	a chain target need not be a metal variant.
	"""
	if not item_code:
		return None
	value = frappe.db.get_value(
		"Item Variant Attribute",
		{"parent": item_code, "attribute": "Metal Purity"},
		"attribute_value",
	)
	if not value:
		return None
	return (
		flt(frappe.db.get_value("Attribute Value", value, "purity_percentage")) or None
	)


def run_receive(doc):
	"""Repack -> Process Loss (when there is one) -> transfer to the To Employee."""
	precision = _precision()
	target_qty = flt(doc.target_qty, precision)
	loss_qty = flt(flt(doc.source_qty, precision) - target_qty, precision)

	repack_rows, loss_rows = split_allocations(
		_source_allocations(doc), [target_qty, max(loss_qty, 0)], precision
	)
	_check_covered(repack_rows, target_qty, precision)
	_check_covered(loss_rows, max(loss_qty, 0), precision)

	# Before the first entry: each builder below still pins its own counter and Bins, which
	# are then re-entrant no-ops.
	_lock_receive(doc, loss_qty)

	repack = make_repack_stock_entry(doc, repack_rows, target_qty)
	# stock_entry too, so reports reading the conversion's own entry keep working.
	doc.db_set({"stock_entry": repack, "chain_repack_stock_entry": repack})

	if loss_qty > 0:
		from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events.melting_loss import (
			make_process_loss_entry,
		)

		loss = make_process_loss_entry(
			doc,
			rows=loss_rows,
			loss_qty=loss_qty,
			source_wh=doc.chain_supplier_warehouse,
		)
		doc.db_set("chain_loss_stock_entry", loss)

	transfer = make_final_transfer(doc, repack, target_qty)
	doc.db_set("chain_transfer_stock_entry", transfer)


def _lock_receive(doc, loss_qty):
	"""Every counter, then every Bin, the Receive cascade will touch -- once, up front.

	Receive mints up to three Stock Entry types. Pinned one entry at a time, the Process
	Loss and Chain SC counters were taken after the Repack's Bins: with per-type counters
	(``shard_stock_entry_naming_by_type``), a Receive holding a supplier Bin and waiting for
	the Chain SC counter could cycle with an Issue holding that counter and waiting for the
	Bin. ``preallocate_series_for_docs`` and ``lock_bins`` both sort what they are given.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions import (
		CONVERSION_SE_TYPE,
	)
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_bins,
		preallocate_series_for_docs,
		series_stubs,
	)

	supplier_wh = doc.chain_supplier_warehouse
	types = [CONVERSION_SE_TYPE, CHAIN_SC_SE_TYPE]
	pairs = [
		(doc.source_item, supplier_wh),
		(doc.target_item, supplier_wh),
		(doc.target_item, doc.to_warehouse),
	]
	if loss_qty > 0:
		from jewellery_erpnext.jewellery_erpnext.doctype.gemstone_conversion.gemstone_conversion import (
			get_scrap_warehouse,
		)
		from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events.melting_loss import (
			MELTING_LOSS_SE_TYPE,
			_resolve_loss_item,
		)

		types.append(MELTING_LOSS_SE_TYPE)
		pairs.append((_resolve_loss_item(doc), get_scrap_warehouse(doc.department)))

	preallocate_series_for_docs(*series_stubs(doc.company, *types))
	lock_bins(pairs)


def make_repack_stock_entry(doc, rows, target_qty):
	"""Source batches -> Target Item, both inside the supplier's RM warehouse.

	Built from the regular conversion's lane helpers, so the entry is a
	"Repack-Metal Conversion" like any other: lane-tagged rows, priced by the lane pricer.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions import (
		CONVERSION_SE_TYPE,
		_append_lane_rows,
		_check_lane_owners,
		_conversion_header,
		lane_tag,
	)
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_bins,
		preallocate_series_for_docs,
	)

	precision = _precision()
	supplier_wh = doc.chain_supplier_warehouse

	lanes = build_lanes(rows, get_batch_lane_map([row["batch"] for row in rows]))
	if not lanes:
		frappe.throw(_("No source batches are allocated for this document."))
	_check_lane_owners(lanes)
	split_conversion(lanes, target_qty, precision)

	_series_stub = frappe.new_doc("Stock Entry")
	_series_stub.company = doc.company
	_series_stub.stock_entry_type = CONVERSION_SE_TYPE
	preallocate_series_for_docs(_series_stub)
	lock_bins([(doc.source_item, supplier_wh), (doc.target_item, supplier_wh)])

	se = frappe.get_doc(_conversion_header(doc, lanes))
	common = {
		"department": doc.department,
		"employee": doc.employee,
		"manufacturer": doc.manufacturer,
	}
	booked = 0.0
	for lane in lanes:
		booked = flt(
			booked
			+ _append_lane_rows(
				se,
				lane,
				lane_tag(lane["inventory_type"], lane["customer"], lane.get("batch")),
				common,
				source_item=doc.source_item,
				source_wh=supplier_wh,
				alloy_item=None,
				alloy_rows=[],
				target_item=doc.target_item,
				target_wh=supplier_wh,
				precision=precision,
			),
			precision,
		)
	if abs(booked - target_qty) > _qty_unit(precision):
		frappe.throw(
			_("Target Qty {0} does not match the {1} booked in the Repack.").format(
				target_qty, booked
			)
		)

	se.save()
	se.submit()
	return se.name


def make_final_transfer(doc, repack, target_qty):
	"""Supplier RM -> To Employee's department RM, carrying the batch the Repack minted."""
	precision = _precision()
	rows = _produced_batches(repack, doc.target_item, doc.chain_supplier_warehouse)
	if not rows:
		rows = [{"batch": None, "qty": target_qty}]
	booked = flt(sum(flt(row["qty"]) for row in rows), precision)
	if abs(booked - target_qty) > _qty_unit(precision):
		frappe.throw(
			_("The Repack produced {0} of {1}, not the Target Qty {2}.").format(
				booked, doc.target_item, target_qty
			)
		)
	return _make_transfer(
		doc,
		item_code=doc.target_item,
		rows=_sorted_allocations(doc.target_item, doc.chain_supplier_warehouse, rows),
		from_wh=doc.chain_supplier_warehouse,
		to_wh=doc.to_warehouse,
	)


def _produced_batches(stock_entry, item_code, warehouse):
	"""``[{batch, qty}]`` the entry put into ``warehouse``, read from its bundles."""
	out = []
	for row in frappe.get_all(
		"Stock Entry Detail",
		filters={
			"parent": stock_entry,
			"item_code": item_code,
			"t_warehouse": warehouse,
		},
		fields=["transfer_qty", "batch_no", "serial_and_batch_bundle"],
		order_by="idx",
	):
		if row.serial_and_batch_bundle:
			entries = frappe.get_all(
				"Serial and Batch Entry",
				filters={"parent": row.serial_and_batch_bundle},
				fields=["batch_no", "qty"],
				order_by="idx",
			)
			if entries:
				out.extend(
					{"batch": entry.batch_no, "qty": abs(flt(entry.qty))}
					for entry in entries
					if entry.batch_no
				)
				continue
		out.append({"batch": row.batch_no, "qty": flt(row.transfer_qty)})
	return out


def _make_transfer(doc, *, item_code, rows, from_wh, to_wh):
	from jewellery_erpnext.jewellery_erpnext.lock_order import (
		lock_bins,
		preallocate_series_for_docs,
	)

	precision = _precision()
	_series_stub = frappe.new_doc("Stock Entry")
	_series_stub.company = doc.company
	_series_stub.stock_entry_type = CHAIN_SC_SE_TYPE
	preallocate_series_for_docs(_series_stub)
	lock_bins([(item_code, from_wh), (item_code, to_wh)])

	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": CHAIN_SC_SE_TYPE,
			"purpose": "Material Transfer",
			"company": doc.company,
			"branch": doc.branch,
			"department": doc.department,
			"manufacturer": doc.manufacturer,
			"custom_metal_conversion_reference": doc.name,
			"inventory_type": REGULAR_STOCK,
			"auto_created": 1,
		}
	)
	for row in rows:
		item = {
			"item_code": item_code,
			"qty": flt(row["qty"], precision),
			"s_warehouse": from_wh,
			"t_warehouse": to_wh,
			"inventory_type": REGULAR_STOCK,
			"department": doc.department,
			"employee": doc.employee,
			"manufacturer": doc.manufacturer,
		}
		if row.get("batch"):
			item.update({"batch_no": row["batch"], "use_serial_batch_fields": 1})
		se.append("items", item)

	se.save()
	se.submit()
	return se.name


def _check_covered(rows, need, precision):
	handed = flt(sum(flt(row["qty"]) for row in rows), precision)
	if abs(handed - flt(need, precision)) > _qty_unit(precision):
		frappe.throw(
			_(
				"The issued batches cover {0}, but {1} is needed. The Source Batch Details no "
				"longer match the Source Qty."
			).format(handed, flt(need, precision))
		)


# ---------------------------------------------------------------------------
# Cancel / delete
# ---------------------------------------------------------------------------


def cancel_chain(doc):
	"""Reverse a received chain conversion, last entry first.

	The final transfer consumes what the Repack made, so it goes first; the Repack and the
	Process Loss are found by the same back-reference the regular conversion's cancel uses;
	the Issue transfer goes last. A submitted Purchase Order belongs to the buying team and
	stops the whole cancel; a draft one is deleted.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.doc_events.melting_loss import (
		cancel_melting_loss_stock_entries,
	)
	from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions import (
		cancel_conversion_stock_entries,
	)

	po = doc.get("chain_purchase_order")
	po_status = frappe.db.get_value("Purchase Order", po, "docstatus") if po else None
	if po_status == 1:
		frappe.throw(
			_("Cancel Purchase Order {0} before cancelling this conversion.").format(
				frappe.bold(po)
			),
			title=_("Purchase Order Submitted"),
		)

	_lock_chain_entries(doc)
	_cancel_stock_entry(doc.get("chain_transfer_stock_entry"))
	cancel_melting_loss_stock_entries(doc)
	cancel_conversion_stock_entries(doc)
	_cancel_stock_entry(doc.get("chain_issue_stock_entry"))

	if po_status == 0:
		frappe.delete_doc("Purchase Order", po, ignore_permissions=True)
		doc.db_set("chain_purchase_order", None)


def _lock_chain_entries(doc):
	"""Every Bin the cancel cascade will touch, sorted, before the first entry is cancelled.

	Each entry's own cancel orders only its own Bins, so across the four the sequence was
	unsorted: a Cancel holding the supplier Bin and reaching for the department Bin could
	cycle with an Issue of the same item going the other way. Cancel mints no names, so
	there is no counter to pin.
	"""
	from jewellery_erpnext.jewellery_erpnext.lock_order import lock_bins

	entries = frappe.get_all(
		"Stock Entry",
		filters={"custom_metal_conversion_reference": doc.name, "docstatus": 1},
		pluck="name",
	)
	if not entries:
		return
	rows = frappe.get_all(
		"Stock Entry Detail",
		filters={"parent": ["in", entries]},
		fields=["item_code", "s_warehouse", "t_warehouse"],
	)
	lock_bins(
		[
			(row.item_code, warehouse)
			for row in rows
			for warehouse in (row.s_warehouse, row.t_warehouse)
			if warehouse
		]
	)


def _cancel_stock_entry(name):
	if name and frappe.db.get_value("Stock Entry", name, "docstatus") == 1:
		frappe.get_doc("Stock Entry", name).cancel()


def guard_trash(doc):
	"""Refuse to delete a conversion while its Issue keeps metal at the supplier.

	A cancelled conversion, and one whose Issue entry is no longer submitted, may go.
	"""
	if doc.docstatus == 2 or not chain_issued(doc):
		return
	if (
		frappe.db.get_value("Stock Entry", doc.chain_issue_stock_entry, "docstatus")
		!= 1
	):
		return
	frappe.throw(
		_(
			"This conversion has issued metal to the supplier in {0}. Receive it, or ask a "
			"System Manager to use Cancel Issue, before deleting it."
		).format(frappe.bold(doc.chain_issue_stock_entry)),
		title=_("Already Issued"),
	)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source_rows(doc):
	return [
		row
		for row in (doc.get("source_batch_details") or [])
		if row.get("batch") and flt(row.get("qty")) > 0
	]


def _source_allocations(doc):
	return [{"batch": row.batch, "qty": flt(row.qty)} for row in _source_rows(doc)]


def _sorted_allocations(item_code, warehouse, rows):
	from jewellery_erpnext.jewellery_erpnext.lock_order import stock_lock_key

	return sorted(
		rows, key=lambda row: stock_lock_key(item_code, warehouse, row.get("batch"))
	)


def _precision():
	from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions import (
		_qty_precision,
	)

	return _qty_precision()


def _qty_unit(precision):
	from jewellery_erpnext.jewellery_erpnext.doctype.metal_conversions.metal_conversions import (
		_qty_unit as unit,
	)

	return unit(precision)
