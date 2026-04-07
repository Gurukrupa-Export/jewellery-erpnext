"""
MOP Log-based stock utilities for Employee IR Receive (v2 architecture).

This module replaces Main Slip-based warehouse/stock logic with
MOP Log event-driven equivalents. No direct Stock Entry creation —
all movements are recorded as MOP Log events for EOD reconciliation.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from jewellery_erpnext.utils import get_item_from_attribute


# ---------------------------------------------------------------------------
# 1. Warehouse Resolution
# ---------------------------------------------------------------------------

def resolve_employee_warehouse(employee, subcontractor=None, subcontracting="No"):
	"""Return the Manufacturing warehouse for the employee or subcontractor.

	Replaces Main Slip warehouse resolution.
	"""
	if subcontracting == "Yes":
		wh = frappe.db.get_value(
			"Warehouse",
			{"disabled": 0, "subcontractor": subcontractor, "warehouse_type": "Manufacturing"},
		)
		if not wh:
			frappe.throw(
				_("No Manufacturing warehouse found for subcontractor {0}").format(subcontractor)
			)
		return wh

	wh = frappe.db.get_value(
		"Warehouse",
		{"disabled": 0, "employee": employee, "warehouse_type": "Manufacturing"},
	)
	if not wh:
		frappe.throw(
			_("No Manufacturing warehouse found for employee {0}").format(employee)
		)
	return wh


def resolve_department_warehouse(department):
	"""Return the Manufacturing warehouse for the department."""
	wh = frappe.db.get_value(
		"Warehouse",
		{"disabled": 0, "department": department, "warehouse_type": "Manufacturing"},
	)
	if not wh:
		frappe.throw(
			_("No Manufacturing warehouse found for department {0}").format(department)
		)
	return wh


# ---------------------------------------------------------------------------
# 2. Item Identification
# ---------------------------------------------------------------------------

_item_cache = {}


def identify_metal_item(metal_type, metal_touch, metal_purity, metal_colour):
	"""Return the Item code matching the given metal attributes.

	Results are cached per-request to avoid repeated DB hits when
	processing multiple rows with the same MWO attributes.
	"""
	key = (metal_type, metal_touch, metal_purity, metal_colour)
	if key not in _item_cache:
		_item_cache[key] = get_item_from_attribute(
			metal_type, metal_touch, metal_purity, metal_colour
		)
	return _item_cache.get(key)


# ---------------------------------------------------------------------------
# 3. MOP-Log-based stock query (replaces get_stock_data_new)
# ---------------------------------------------------------------------------

def get_mop_stock(manufacturing_operation):
	"""Query MOP Log for the current stock picture of a Manufacturing Operation.

	Returns a dict keyed by ``(item_code, batch_no)`` with aggregated
	``qty`` and ``pcs`` across all non-cancelled logs for this MOP.
	"""
	logs = frappe.db.sql(
		"""
		SELECT
			item_code,
			batch_no,
			SUM(qty_change)  AS qty,
			SUM(pcs_change)  AS pcs
		FROM `tabMOP Log`
		WHERE manufacturing_operation = %s
		  AND is_cancelled = 0
		GROUP BY item_code, batch_no
		HAVING SUM(qty_change) != 0
		""",
		(manufacturing_operation,),
		as_dict=True,
	)
	return {(r.item_code, r.batch_no): r for r in logs}


# ---------------------------------------------------------------------------
# 4. Receive MOP Event Creation
# ---------------------------------------------------------------------------

def create_receive_mop_events(doc, row, department_wh, employee_wh, difference_wt, loss_items):
	"""Create MOP Log events for an Employee IR Receive.

	This is the core function that replaces ``create_stock_entry()`` in the
	old architecture. Instead of building Stock Entry rows, it creates
	MOP Log entries that the EOD reconciliation engine will later process.

	Parameters
	----------
	doc : EmployeeIR
		The parent document.
	row : EmployeeIROperation (child row)
		The operation being received.
	department_wh : str
		Department Manufacturing warehouse (target).
	employee_wh : str
		Employee Manufacturing warehouse (source).
	difference_wt : float
		``received_gross_wt - gross_wt``.  Negative = loss, positive = addition.
	loss_items : list[dict]
		Loss details from ``manually_book_loss_details + employee_loss_details``
		filtered for this MWO.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		create_mop_log_for_stock_transfer_to_mo,
	)

	precision = cint(frappe.db.get_single_value("System Settings", "float_precision"))

	# Get current stock picture from MOP Log for this operation
	mop_stock = get_mop_stock(row.manufacturing_operation)

	if not mop_stock:
		# Nothing was issued to this MOP — skip
		return

	# ── Transfer all items back: employee_wh → department_wh ──
	for (item_code, batch_no), stock_data in mop_stock.items():
		qty = flt(stock_data.qty, precision)
		pcs = cint(stock_data.pcs)

		if qty == 0 and pcs == 0:
			continue

		# Check if this item has loss booked against it
		item_loss = 0
		for li in loss_items:
			if li.get("item_code") == item_code and li.get("batch_no") == batch_no:
				item_loss += flt(li.get("loss_qty"), precision)

		transfer_qty = flt(qty - item_loss, precision)
		if transfer_qty <= 0:
			continue

		# Create the MOP Log event: employee → department
		log_row = frappe._dict(
			{
				"item_code": item_code,
				"batch_no": batch_no,
				"qty": transfer_qty,
				"pcs": pcs,
				"s_warehouse": employee_wh,
				"t_warehouse": department_wh,
				"manufacturing_operation": row.manufacturing_operation,
				"serial_and_batch_bundle": None,
			}
		)

		# Wrap in a doc-like object for the MOP log creator
		log_doc = frappe._dict(
			{
				"name": doc.name,
				"doctype": "Employee IR",
				"manufacturing_work_order": row.manufacturing_work_order,
			}
		)

		create_mop_log_for_stock_transfer_to_mo(log_doc, log_row, is_synced=False, voucher_type="Employee IR")

	# ── Loss events (explicit negative qty entries) ──
	for li in loss_items:
		loss_qty = flt(li.get("loss_qty"), precision)
		if loss_qty <= 0:
			continue

		loss_row = frappe._dict(
			{
				"item_code": li.get("item_code"),
				"batch_no": li.get("batch_no"),
				"qty": -loss_qty,  # negative = loss
				"pcs": -cint(li.get("pcs", 0)),
				"s_warehouse": employee_wh,
				"t_warehouse": None,  # consumed / lost
				"manufacturing_operation": li.get("manufacturing_operation") or row.manufacturing_operation,
				"serial_and_batch_bundle": None,
			}
		)

		loss_doc = frappe._dict(
			{
				"name": doc.name,
				"doctype": "Employee IR",
				"manufacturing_work_order": li.get("manufacturing_work_order") or row.manufacturing_work_order,
			}
		)

		create_mop_log_for_stock_transfer_to_mo(loss_doc, loss_row, is_synced=False, voucher_type="Employee IR")

	# ── Addition events (employee added metal, difference_wt > 0) ──
	if flt(difference_wt, precision) > 0:
		mwo_data = frappe.db.get_value(
			"Manufacturing Work Order",
			row.manufacturing_work_order,
			["metal_type", "metal_touch", "metal_purity", "metal_colour"],
			as_dict=True,
		)
		if mwo_data:
			metal_item = identify_metal_item(
				mwo_data.metal_type,
				mwo_data.metal_touch,
				mwo_data.metal_purity,
				mwo_data.metal_colour,
			)
			if metal_item:
				add_row = frappe._dict(
					{
						"item_code": metal_item,
						"batch_no": None,
						"qty": flt(difference_wt, precision),
						"pcs": 1,
						"s_warehouse": department_wh,
						"t_warehouse": employee_wh,
						"manufacturing_operation": row.manufacturing_operation,
						"serial_and_batch_bundle": None,
					}
				)

				add_doc = frappe._dict(
					{
						"name": doc.name,
						"doctype": "Employee IR",
						"manufacturing_work_order": row.manufacturing_work_order,
					}
				)

				create_mop_log_for_stock_transfer_to_mo(add_doc, add_row, is_synced=False, voucher_type="Employee IR")

				# Step 2: Receive the added metal back to the department with the rest of the jewelry
				add_receive_row = frappe._dict(
					{
						"item_code": metal_item,
						"batch_no": None,
						"qty": flt(difference_wt, precision),
						"pcs": 1,
						"s_warehouse": employee_wh,
						"t_warehouse": department_wh,
						"manufacturing_operation": row.manufacturing_operation,
						"serial_and_batch_bundle": None,
					}
				)
				create_mop_log_for_stock_transfer_to_mo(add_doc, add_receive_row, is_synced=False, voucher_type="Employee IR")


# ---------------------------------------------------------------------------
# 5. Repack Conversion (MOP Log events, no Stock Entry)
# ---------------------------------------------------------------------------

def handle_repack_conversion(
	doc, row, metal_item, employee_wh, department_wh, remaining_wt, mwo_data
):
	"""Handle repack conversion when the required alloy item is not available.

	Checks if pure gold is available in the department warehouse and records
	MOP Log events for the conversion (pure → alloy).

	Returns the remaining weight that could NOT be fulfilled.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		create_mop_log_for_stock_transfer_to_mo,
	)

	precision = cint(frappe.db.get_single_value("System Settings", "float_precision"))

	pure_gold_item = frappe.db.get_value(
		"Manufacturing Setting",
		{"manufacturer": doc.manufacturer},
		"pure_gold_item",
	)
	if not pure_gold_item:
		return remaining_wt

	# Get purity percentage for conversion
	purity = flt(
		frappe.db.get_value("Attribute Value", mwo_data.metal_purity, "purity_percentage")
	)
	if purity <= 0:
		return remaining_wt

	# How much pure gold do we need?
	# Formula: pure_qty = (remaining_wt * purity) / 100
	pure_qty_needed = flt((remaining_wt * purity) / 100, precision)

	if pure_qty_needed <= 0:
		return remaining_wt

	# ── Log: consume pure gold (negative event for pure item) ──
	consume_row = frappe._dict(
		{
			"item_code": pure_gold_item,
			"batch_no": None,
			"qty": -pure_qty_needed,
			"pcs": 0,
			"s_warehouse": department_wh,
			"t_warehouse": None,
			"manufacturing_operation": row.manufacturing_operation,
			"serial_and_batch_bundle": None,
		}
	)

	log_doc = frappe._dict(
		{
			"name": doc.name,
			"doctype": "Employee IR",
			"manufacturing_work_order": row.manufacturing_work_order,
		}
	)

	create_mop_log_for_stock_transfer_to_mo(log_doc, consume_row, is_synced=False)

	# ── Log: produce alloy item (positive event for target item) ──
	produce_row = frappe._dict(
		{
			"item_code": metal_item,
			"batch_no": None,
			"qty": flt(remaining_wt, precision),
			"pcs": 1,
			"s_warehouse": None,
			"t_warehouse": department_wh,
			"manufacturing_operation": row.manufacturing_operation,
			"serial_and_batch_bundle": None,
		}
	)

	create_mop_log_for_stock_transfer_to_mo(log_doc, produce_row, is_synced=False)

	return 0  # fully fulfilled
