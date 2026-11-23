"""Create the ``Subcontracting Charges`` Item that Hybrid Sales Orders depend on.

``add_hybrid_outwork_row`` (Sales Order ``before_validate``) moves the customer-supplied
portion of a Hybrid order into one aggregated row for this item, so the real rows keep the
Outright (3%) template and this row carries the Outwork (5%) one. The item was never created
by the app, so a site without it fails the save with ``Item Subcontracting Charges not found``.

KGGK only: skipped on any site that does not have the KG GK Jewellers company, so other
deployments of the app are untouched. Idempotent: does nothing if the item already exists. Run ad-hoc with::

    bench --site <site> execute jewellery_erpnext.patches.create_hybrid_outwork_item.execute
"""

import frappe

ITEM = "Subcontracting Charges"
HSN_CODE = "9988"  # job-work / manufacturing services on goods owned by others
KGGK_COMPANY = "KG GK Jewellers Private Limited"
# Outwork (5%) Item Tax Template, same name as SALES_TYPE_ITEM_TAX_TEMPLATE["Outwork"][KGGK_COMPANY]
OUTWORK_TEMPLATES = ("GST 5% - KGJPL",)


def execute():
	if not frappe.db.exists("Company", KGGK_COMPANY) or frappe.db.exists("Item", ITEM):
		return

	item_group = (
		"Services" if frappe.db.exists("Item Group", "Services") else "All Item Groups"
	)
	doc = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": ITEM,
			"item_name": ITEM,
			"description": ITEM,
			"item_group": item_group,
			"stock_uom": "Nos",
			"is_stock_item": 0,
			"is_purchase_item": 0,
			"is_sales_item": 1,
			"include_item_in_manufacturing": 0,
			"gst_hsn_code": HSN_CODE
			if frappe.db.exists("GST HSN Code", HSN_CODE)
			else None,
		}
	)
	for template in OUTWORK_TEMPLATES:
		if frappe.db.exists("Item Tax Template", template):
			doc.append("taxes", {"item_tax_template": template})
	doc.insert(ignore_permissions=True)
