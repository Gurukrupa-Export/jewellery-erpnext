"""Refinery Work Order: a Manufacturing Work Order the Central (refinery) department uses to
collect a PMO's refinery material and recast it, apart from the regular manufacturing flow.

* Created from the PMO (``create_refinery_work_order``), flagged ``is_refinery_mwo`` and linked
  to the PMO through ``manufacturing_order``. Its header weights start and stay at 0.
* ``transfer_to_refinery_mwo`` moves the full current balance of chosen MWOs of the same PMO
  that sit in the refinery department into it. The source work order keeps its status and its
  current operation; only the material leaves it.
* It is left out of tagging and the FG weight roll-up, is never issued to an employee, and moves
  between departments only inside the refinery department (Department IR).

The refinery department is ``Manufacturer.custom_refinery_department``.
"""

import frappe
from frappe import _
from frappe.model.mapper import get_mapped_doc
from frappe.utils import flt, now

REFINERY_FLAG = "is_refinery_mwo"
REFINERY_DEPARTMENT_FIELD = "custom_refinery_department"

# Header weight fields a Refinery Work Order keeps at 0 (create and after every transfer).
REFINERY_ZERO_WEIGHT_FIELDS = (
	"gross_wt",
	"net_wt",
	"finding_wt",
	"diamond_wt",
	"gemstone_wt",
	"other_wt",
	"received_gross_wt",
	"received_net_wt",
	"loss_wt",
	"diamond_wt_in_gram",
	"diamond_pcs",
	"gemstone_pcs",
)


# --- lookups -----------------------------------------------------------------------------


def get_refinery_department(manufacturer):
	"""The department ``manufacturer`` runs its refinery process in, or None."""
	if not manufacturer:
		return None
	if not frappe.get_meta("Manufacturer").has_field(REFINERY_DEPARTMENT_FIELD):
		return None
	return frappe.db.get_value("Manufacturer", manufacturer, REFINERY_DEPARTMENT_FIELD)


def is_refinery_mwo(mwo):
	return bool(mwo) and bool(
		frappe.db.get_value("Manufacturing Work Order", mwo, REFINERY_FLAG)
	)


def refinery_mwos(names):
	"""The Refinery Work Orders among ``names``: {name: (department, manufacturer)}."""
	names = [n for n in set(names or ()) if n]
	if not names:
		return {}
	rows = frappe.get_all(
		"Manufacturing Work Order",
		filters={"name": ["in", names], REFINERY_FLAG: 1},
		fields=["name", "department", "manufacturer"],
	)
	return {r.name: (r.department, r.manufacturer) for r in rows}


def open_refinery_mwo(pmo):
	"""The PMO's Refinery Work Order still taking material (not cancelled/closed), or None."""
	return frappe.db.get_value(
		"Manufacturing Work Order",
		{
			"manufacturing_order": pmo,
			REFINERY_FLAG: 1,
			"docstatus": ["!=", 2],
			"status": ["not in", ("Cancelled", "Closed", "Completed")],
		},
		"name",
	)


def refinery_eligible_mwos(pmo):
	"""The PMO's work orders whose material can go to its Refinery Work Order: submitted,
	regular (not FG, not refinery), in the refinery department, not already transferred, and
	not Closed / on Hold."""
	from jewellery_erpnext.jewellery_erpnext.doc_events.mwo_closed_guard import (
		BLOCKED_MWO_STATUSES,
	)

	manufacturer = frappe.db.get_value(
		"Parent Manufacturing Order", pmo, "manufacturer"
	)
	department = get_refinery_department(manufacturer)
	if not department:
		return []
	done = set(
		frappe.get_all(
			"Refinery Source MWO",
			filters={"parenttype": "Manufacturing Work Order", "docstatus": ["!=", 2]},
			pluck="source_mwo",
		)
	)
	rows = frappe.get_all(
		"Manufacturing Work Order",
		filters={
			"manufacturing_order": pmo,
			"docstatus": 1,
			"for_fg": 0,
			REFINERY_FLAG: 0,
			"department": department,
			"status": ["not in", list(BLOCKED_MWO_STATUSES) + ["Cancelled"]],
		},
		fields=[
			"name",
			"manufacturing_operation",
			"metal_type",
			"metal_touch",
			"metal_colour",
			"status",
		],
		order_by="name asc",
	)
	return [r for r in rows if r.name not in done and r.manufacturing_operation]



@frappe.whitelist()
def get_refinery_transfer_candidates(refinery_mwo):
	"""The work orders the Material Transfer dialog of ``refinery_mwo`` offers."""
	refinery = frappe.get_doc("Manufacturing Work Order", refinery_mwo)
	refinery.check_permission("read")
	if not refinery.get(REFINERY_FLAG):
		frappe.throw(_("{0} is not a Refinery Work Order.").format(refinery_mwo))
	return refinery_eligible_mwos(refinery.manufacturing_order)

def can_create_refinery_work_order(pmo_doc):
	"""Show Create Refinery Work Order on a submitted PMO whose Manufacturer has a refinery
	department and that has at least one work order in it."""
	if pmo_doc.docstatus != 1:
		return False
	department = get_refinery_department(pmo_doc.manufacturer)
	if not department:
		return False
	return bool(
		frappe.db.exists(
			"Manufacturing Work Order",
			{
				"manufacturing_order": pmo_doc.name,
				"docstatus": 1,
				"for_fg": 0,
				REFINERY_FLAG: 0,
				"department": department,
			},
		)
	)


# --- create --------------------------------------------------------------------------------


@frappe.whitelist()
def create_refinery_work_order(pmo):
	"""Create (or return the open) Refinery Work Order of ``pmo``."""
	from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.close_open import (
		refuse_while_blocked,
	)

	frappe.has_permission("Manufacturing Work Order", "create", throw=True)
	refuse_while_blocked(pmo, _("No new work order can be created for it."))
	pmo_doc = frappe.get_doc("Parent Manufacturing Order", pmo)
	if pmo_doc.docstatus != 1:
		frappe.throw(_("Submit the Parent Manufacturing Order first."))
	department = get_refinery_department(pmo_doc.manufacturer)
	if not department:
		frappe.throw(
			_("Set the Refinery Department on Manufacturer {0} first.").format(
				frappe.bold(pmo_doc.manufacturer)
			)
		)
	if not can_create_refinery_work_order(pmo_doc):
		frappe.throw(
			_(
				"No work order of {0} is in the refinery department {1}, so there is nothing to refine."
			).format(frappe.bold(pmo), frappe.bold(department))
		)

	existing = open_refinery_mwo(pmo)
	if existing:
		frappe.msgprint(
			_("Refinery Work Order {0} is already open for {1}.").format(
				frappe.bold(existing), frappe.bold(pmo)
			)
		)
		return existing

	mwo = get_mapped_doc(
		"Parent Manufacturing Order",
		pmo,
		{
			"Parent Manufacturing Order": {
				"doctype": "Manufacturing Work Order",
				"field_map": {"name": "manufacturing_order"},
			}
		},
		ignore_permissions=True,
	)
	mwo.seq = int(pmo.split("-")[-1])
	mwo.department = department
	mwo.metal_touch = pmo_doc.metal_touch
	mwo.metal_type = pmo_doc.metal_type
	mwo.metal_purity = pmo_doc.metal_purity
	mwo.metal_colour = pmo_doc.metal_colour
	mwo.set(REFINERY_FLAG, 1)
	# The PMO's Tracking BOM stays pointed at its regular work orders (after_insert would
	# re-point it here).
	if mwo.meta.has_field("custom_tracking_bom"):
		mwo.custom_tracking_bom = None
	mwo.for_fg = 0
	mwo.auto_created = 1
	for field in REFINERY_ZERO_WEIGHT_FIELDS:
		if mwo.meta.has_field(field):
			mwo.set(field, 0)
	mwo.flags.ignore_permissions = True
	mwo.insert()
	mwo.submit()
	frappe.msgprint(
		_("Refinery Work Order {0} created.").format(
			frappe.utils.get_link_to_form("Manufacturing Work Order", mwo.name)
		),
		alert=True,
		indicator="green",
	)
	return mwo.name


def set_refinery_operation_department(doc, mop):
	"""create_manufacturing_operation hook: a Refinery Work Order's first operation starts in
	its refinery department with no operation (as a split child's), and at zero weight."""
	if not doc.get(REFINERY_FLAG):
		return False
	mop.department = doc.department
	mop.operation = None
	mop.status = "Not Started"
	return True


def keep_refinery_weights_zero(mwo):
	values = {
		f: 0
		for f in REFINERY_ZERO_WEIGHT_FIELDS
		if frappe.get_meta("Manufacturing Work Order").has_field(f)
	}
	frappe.db.set_value("Manufacturing Work Order", mwo, values, update_modified=False)


# --- material transfer --------------------------------------------------------------------


def _refinery_target_warehouse(department):
	warehouse = frappe.db.get_value(
		"Warehouse",
		{"disabled": 0, "department": department, "warehouse_type": "Manufacturing"},
	)
	if not warehouse:
		frappe.throw(
			_("No Manufacturing warehouse is set for the refinery department {0}.").format(
				frappe.bold(department)
			)
		)
	return warehouse


def source_balance_rows(mop):
	"""The current balance of operation ``mop``: the latest MOP Log row per item+batch
	(``get_current_mop_balance_rows``), positive quantities only, with the warehouse the
	material sits in."""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_current_mop_balance_rows,
	)

	balance = []
	for r in get_current_mop_balance_rows(
		mop,
		include_fields=[
			"item_code",
			"batch_no",
			"from_warehouse",
			"to_warehouse",
			"qty_after_transaction_batch_based",
			"pcs_after_transaction_batch_based",
		],
	):
		qty = flt(r.get("qty_after_transaction_batch_based"), 3)
		warehouse = r.get("to_warehouse") or r.get("from_warehouse")
		if qty > 0 and warehouse:
			balance.append(
				frappe._dict(
					item_code=r.get("item_code"),
					batch_no=r.get("batch_no"),
					qty=qty,
					pcs=r.get("pcs_after_transaction_batch_based") or 0,
					warehouse=warehouse,
				)
			)
	return balance


def build_transfer_rows(balance, source_mop, target_mop, target_mwo, target_warehouse):
	return [
		{
			"item_code": r.item_code,
			"qty": flt(r.qty, 3),
			"pcs": r.get("pcs") or 0,
			"s_warehouse": r.warehouse,
			"t_warehouse": target_warehouse,
			"batch_no": r.batch_no,
			"use_serial_batch_fields": 1,
			"manufacturing_operation": target_mop,
			"custom_manufacturing_work_order": target_mwo,
		}
		for r in balance
	]


def validate_transfer_source(refinery, source):
	"""Refuse a source work order that may not feed ``refinery``."""
	if source.name == refinery.name:
		frappe.throw(_("A Refinery Work Order cannot transfer to itself."))
	if source.manufacturing_order != refinery.manufacturing_order:
		frappe.throw(
			_("{0} belongs to {1}, not to {2}.").format(
				frappe.bold(source.name),
				frappe.bold(source.manufacturing_order),
				frappe.bold(refinery.manufacturing_order),
			)
		)
	if source.docstatus != 1 or source.get(REFINERY_FLAG) or source.for_fg:
		frappe.throw(
			_("{0} is not a submitted regular work order.").format(frappe.bold(source.name))
		)
	if source.department != refinery.department:
		frappe.throw(
			_("{0} is in {1}; only work orders in the refinery department {2} can be transferred.").format(
				frappe.bold(source.name),
				frappe.bold(source.department),
				frappe.bold(refinery.department),
			)
		)
	if not source.manufacturing_operation:
		frappe.throw(_("{0} has no current operation.").format(frappe.bold(source.name)))
	if (
		frappe.db.get_value(
			"Manufacturing Operation", source.manufacturing_operation, "department_ir_status"
		)
		== "In-Transit"
	):
		frappe.throw(
			_("The operation {0} of {1} is In-Transit.").format(
				frappe.bold(source.manufacturing_operation), frappe.bold(source.name)
			)
		)


def _post_source_outward_logs(se, source):
	"""Take the moved material off the source operation's MOP Log, so the original work order
	no longer shows it. Its status and current operation stay as they are."""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		create_mop_log_for_stock_transfer_to_mo,
	)

	voucher = frappe._dict(
		doctype=se.doctype,
		name=se.name,
		stock_entry_type=se.stock_entry_type,
		manufacturing_work_order=source.name,
	)
	for row in se.get("items") or []:
		out = frappe._dict(row.as_dict())
		out.manufacturing_operation = source.manufacturing_operation
		out.t_warehouse = None
		create_mop_log_for_stock_transfer_to_mo(voucher, out, is_synced=True, outward=True)


@frappe.whitelist()
def transfer_to_refinery_mwo(refinery_mwo, source_mwos):
	"""Move the full current balance of each of ``source_mwos`` into ``refinery_mwo``: one
	Material Transfer to Department per source. Returns the Stock Entry names."""
	if isinstance(source_mwos, str):
		source_mwos = frappe.parse_json(source_mwos)
	source_mwos = [s for s in dict.fromkeys(source_mwos or []) if s]
	if not source_mwos:
		frappe.throw(_("Select at least one work order to transfer."))

	refinery = frappe.get_doc("Manufacturing Work Order", refinery_mwo)
	refinery.check_permission("write")
	if not refinery.get(REFINERY_FLAG) or refinery.docstatus != 1:
		frappe.throw(_("{0} is not a submitted Refinery Work Order.").format(refinery_mwo))
	if not refinery.manufacturing_operation:
		frappe.throw(_("{0} has no operation to receive into.").format(refinery_mwo))
	target_warehouse = _refinery_target_warehouse(refinery.department)
	already = {r.source_mwo for r in refinery.get("refinery_source_mwos") or []}

	entries = []
	for name in source_mwos:
		if name in already:
			frappe.throw(
				_("{0} has already been transferred to {1}.").format(
					frappe.bold(name), frappe.bold(refinery.name)
				)
			)
		source = frappe.get_doc("Manufacturing Work Order", name)
		validate_transfer_source(refinery, source)
		balance = source_balance_rows(source.manufacturing_operation)
		if not balance:
			frappe.throw(
				_("{0} has no material to transfer.").format(frappe.bold(source.name))
			)

		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Transfer to Department"
		se.company = refinery.company
		se.manufacturing_order = refinery.manufacturing_order
		se.manufacturing_work_order = refinery.name
		se.manufacturing_operation = refinery.manufacturing_operation
		se.auto_created = 1
		se.remarks = _("Refinery transfer from {0} to {1}").format(source.name, refinery.name)
		for row in build_transfer_rows(
			balance,
			source.manufacturing_operation,
			refinery.manufacturing_operation,
			refinery.name,
			target_warehouse,
		):
			se.append("items", row)
		se.flags.ignore_permissions = True
		se.save()
		se.submit()
		_post_source_outward_logs(se, source)

		row = refinery.append(
			"refinery_source_mwos",
			{
				"source_mwo": source.name,
				"source_operation": source.manufacturing_operation,
				"stock_entry": se.name,
				"transferred_on": now(),
			},
		)
		row.docstatus = 1
		row.db_insert()
		entries.append(se.name)

	keep_refinery_weights_zero(refinery.name)
	return entries


# --- guards on the regular flows ----------------------------------------------------------


def exclude_refinery_filter(filters):
	"""Add the "not a Refinery Work Order" condition to a Manufacturing Work Order filter dict."""
	if frappe.get_meta("Manufacturing Work Order").has_field(REFINERY_FLAG):
		filters[REFINERY_FLAG] = 0
	return filters


def without_refinery_mwos(names):
	"""``names`` minus the Refinery Work Orders."""
	names = list(names or [])
	refinery = refinery_mwos(names)
	return [n for n in names if n not in refinery]


def _row_mwos(doc, table):
	rows = doc.get(table) or []
	ops = [r.get("manufacturing_operation") for r in rows if r.get("manufacturing_operation")]
	op_mwo = (
		dict(
			frappe.get_all(
				"Manufacturing Operation",
				filters={"name": ["in", ops]},
				fields=["name", "manufacturing_work_order"],
				as_list=True,
			)
		)
		if ops
		else {}
	)
	return [
		(r, r.get("manufacturing_work_order") or op_mwo.get(r.get("manufacturing_operation")))
		for r in rows
	]


def block_refinery_in_employee_ir(doc, table="employee_ir_operations"):
	"""A Refinery Work Order is never issued to or received from an employee."""
	if doc.docstatus == 2:
		return
	pairs = _row_mwos(doc, table)
	refinery = refinery_mwos(m for _r, m in pairs)
	bad = [
		_("Row {0}: {1}").format(r.idx, frappe.bold(m)) for r, m in pairs if m in refinery
	]
	if bad:
		frappe.throw(
			"<br>".join(bad)
			+ "<br>"
			+ _("Refinery Work Orders cannot be transferred through Employee IR."),
			title=_("Refinery Work Order"),
		)


def validate_refinery_department_ir(doc, table="department_ir_operation"):
	"""A Refinery Work Order moves by Department IR only inside its refinery department:
	both the current and the next department must be the refinery department."""
	if doc.docstatus == 2:
		return
	pairs = _row_mwos(doc, table)
	refinery = refinery_mwos(m for _r, m in pairs)
	if not refinery:
		return
	bad = []
	for r, m in pairs:
		if m not in refinery:
			continue
		_dept, manufacturer = refinery[m]
		refinery_dept = get_refinery_department(manufacturer)
		departments = {doc.get("current_department"), doc.get("next_department")}
		departments.discard(None)
		departments.discard("")
		if not refinery_dept or departments != {refinery_dept}:
			bad.append(_("Row {0}: {1}").format(r.idx, frappe.bold(m)))
	if bad:
		frappe.throw(
			"<br>".join(bad)
			+ "<br>"
			+ _(
				"Refinery Work Orders move by Department IR only within the refinery department."
			),
			title=_("Refinery Work Order"),
		)


def refuse_refinery_mwo(mwo, action):
	"""Throw when ``mwo`` is a Refinery Work Order; ``action`` names what was refused."""
	if is_refinery_mwo(mwo):
		frappe.throw(
			_("{0} is a Refinery Work Order and cannot be used for {1}.").format(
				frappe.bold(mwo), action
			),
			title=_("Refinery Work Order"),
		)
