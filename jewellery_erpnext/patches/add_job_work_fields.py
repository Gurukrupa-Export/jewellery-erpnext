"""Fields for the Gurukrupa -> KGGK job-work flow (``jewellery_erpnext.job_work``).

Sales Order Item (KGGK, spec step 8)
	``custom_parent_manufacturing_order`` -- the KGGK PMO each Subcontracting Income row bills.
	``custom_design_code`` -- the design (FG item) that row bills job work for.

Sales Invoice Item (KGGK, spec step 14)
	``custom_parent_manufacturing_order`` and ``custom_design_code`` -- same fieldnames as on
	Sales Order Item, so ERPNext's Sales Order -> Sales Invoice mapping copies them on its own.
	``custom_job_work_mwo`` -- Gurukrupa's Manufacturing Work Order the job work was for.

Landed Cost Taxes and Charges (Stock Entry Additional Costs, Gurukrupa, spec step 15)
	The Purchase Invoice, KGGK Sales Invoice, PMO and MWO behind each job-work cost row, as
	links instead of only inside the description, so they can be filtered and reported on.

Item "Subcontracting Income" (KGGK, spec step 8)
	The service item the Manufacturing Plan bills job work on. Created only when missing, as a
	plain non-stock sales item under SAC 998892 (Jewellery manufacturing services) -- HSN
	validation is on, so it cannot be created without one. The item tax template and income
	account are left to accounts: they differ per company, and an existing item is never
	touched, so their changes survive every migrate.

All fields read-only: the job-work code fills them. Wired to ``after_migrate`` as well as
``patches.txt`` because ``bench install-app`` marks every patch done without running it
(see ``add_material_request_total_pcs_field``), which is exactly the CI sequence.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from jewellery_erpnext.jewellery_erpnext.job_work import SUBCONTRACTING_INCOME_ITEM

# SAC for "Jewellery manufacturing services" (India Compliance's GST HSN Code master).
JOB_WORK_SAC = "998892"

FIELDS = {
	"Sales Order Item": [
		{
			# The KGGK PMO a Subcontracting Income row bills. Manufacturing Plan writes it when
			# it creates the income Sales Order and reads it back to avoid creating that Sales
			# Order twice; no app defined it on Sales Order Item, so both used to fail.
			"fieldname": "custom_parent_manufacturing_order",
			"label": "Parent Manufacturing Order",
			"fieldtype": "Link",
			"options": "Parent Manufacturing Order",
			"insert_after": "delivery_date",
			"read_only": 1,
		},
		{
			# The design (FG item) a Subcontracting Income row bills job work for. The row's own
			# item is the service item, so without this the order does not show which design.
			"fieldname": "custom_design_code",
			"label": "Design Code",
			"fieldtype": "Link",
			"options": "Item",
			"insert_after": "item_code",
			"read_only": 1,
			"in_list_view": 1,
		},
	],
	"Sales Invoice Item": [
		{
			"fieldname": "custom_parent_manufacturing_order",
			"label": "Parent Manufacturing Order",
			"fieldtype": "Link",
			"options": "Parent Manufacturing Order",
			"insert_after": "sales_order",
			"read_only": 1,
		},
		{
			# Same fieldname as on Sales Order Item, so the invoice row carries the design too.
			"fieldname": "custom_design_code",
			"label": "Design Code",
			"fieldtype": "Link",
			"options": "Item",
			"insert_after": "item_code",
			"read_only": 1,
			"in_list_view": 1,
		},
		{
			"fieldname": "custom_job_work_mwo",
			"label": "Job Work MWO",
			"fieldtype": "Link",
			"options": "Manufacturing Work Order",
			"insert_after": "custom_parent_manufacturing_order",
			"read_only": 1,
		},
	],
	"Landed Cost Taxes and Charges": [
		{
			"fieldname": "custom_purchase_invoice",
			"label": "Purchase Invoice",
			"fieldtype": "Link",
			"options": "Purchase Invoice",
			"insert_after": "description",
			"read_only": 1,
		},
		{
			"fieldname": "custom_sales_invoice",
			"label": "Sales Invoice",
			"fieldtype": "Link",
			"options": "Sales Invoice",
			"insert_after": "custom_purchase_invoice",
			"read_only": 1,
		},
		{
			"fieldname": "custom_parent_manufacturing_order",
			"label": "Parent Manufacturing Order",
			"fieldtype": "Link",
			"options": "Parent Manufacturing Order",
			"insert_after": "custom_sales_invoice",
			"read_only": 1,
		},
		{
			"fieldname": "custom_manufacturing_work_order",
			"label": "Manufacturing Work Order",
			"fieldtype": "Link",
			"options": "Manufacturing Work Order",
			"insert_after": "custom_parent_manufacturing_order",
			"read_only": 1,
		},
	],
}


def execute():
	create_custom_fields(FIELDS, update=True)
	ensure_subcontracting_income_item()


def ensure_subcontracting_income_item(item_code=SUBCONTRACTING_INCOME_ITEM):
	"""Create the job-work income item if this site does not have it yet."""
	if frappe.db.exists("Item", item_code):
		return
	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"description": "Job work charge billed by KGGK to Gurukrupa",
			# Same group as the existing service item "FG Subcontracting".
			"item_group": "All Item Groups",
			"stock_uom": "Nos",
			"is_stock_item": 0,
			"include_item_in_manufacturing": 0,
			"is_sales_item": 1,
			"is_purchase_item": 0,
			"gst_hsn_code": JOB_WORK_SAC
			if frappe.db.exists("GST HSN Code", JOB_WORK_SAC)
			else None,
		}
	)
	item.flags.ignore_permissions = True
	item.insert()


def after_migrate():
	# Non-fatal like the other after_migrate hook: a site that cannot create the item (for
	# example a missing UOM or HSN master) must still migrate; the failure is logged and
	# printed so it is seen.
	try:
		execute()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Job work fields / Subcontracting Income item")
		print("add_job_work_fields.after_migrate failed -- see Error Log")
