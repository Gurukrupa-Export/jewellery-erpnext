from . import __version__ as app_version

app_name = "jewellery_erpnext"
app_title = "Jewellery Erpnext"
app_publisher = "Nirali"
app_description = "jewellery custom app"
app_icon = "octicon octicon-file-directory"
app_color = "grey"
app_email = "nirali@ascratech.com"
app_license = "MIT"

app_include_css = "/assets/jewellery_erpnext/css/jewellery.css"
app_include_js = "/assets/jewellery_erpnext/js/override/custom_multi_select_dialog.js"
after_migrate = "jewellery_erpnext.migrate.after_migrate"

doctype_js = {
	"Quotation": "public/js/doctype_js/quotation.js",
	"Customer": "public/js/doctype_js/customer.js",
	"BOM": "public/js/doctype_js/bom.js",
	"Work Order": "public/js/doctype_js/work_order.js",
	"Item": "public/js/doctype_js/item.js",
	"Stock Entry": "public/js/doctype_js/stock_entry.js",
	"Operation": "public/js/doctype_js/operation.js",
	"Job Card": "public/js/doctype_js/job_card.js",
	"Sales Order": "public/js/doctype_js/sales_order.js",
	"Manufacturer": "public/js/doctype_js/manufacturer.js",
	"Quality Inspection Template": "public/js/doctype_js/quality_inspection_template.js",
	"Supplier": "public/js/doctype_js/supplier.js",
	"Material Request": "public/js/doctype_js/material_request.js",
	"Sales Invoice": "public/js/doctype_js/sales_invoice.js",
	"Delivery Note": "public/js/doctype_js/delivery_note.js",
	"Purchase Order": "public/js/doctype_js/purchase_order.js",
	"Purchase Receipt": "public/js/doctype_js/purchase_receipt.js",
	"Purchase Invoice": "public/js/doctype_js/purchase_invoice.js",
	"Stock Reconciliation": "public/js/doctype_js/stock_reconciliation.js",
}

# from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry

# from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
# 	get_bom_scrap_material,
# 	get_scrap_items_from_job_card,
# )

# StockEntry.get_scrap_items_from_job_card = get_scrap_items_from_job_card
# StockEntry.get_bom_scrap_material = get_bom_scrap_material
# from erpnext.manufacturing.doctype.work_order.work_order import WorkOrder

# from jewellery_erpnext.jewellery_erpnext.doc_events.work_order import get_work_orders

# WorkOrder.get_work_orders = get_work_orders

doc_events = {
	"Quotation": {
		"before_validate": "jewellery_erpnext.jewellery_erpnext.customization.quotation.quotation.before_validate",
		"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.quotation.validate",
		"on_submit": "jewellery_erpnext.jewellery_erpnext.doc_events.quotation.on_submit",
		"before_submit": "jewellery_erpnext.jewellery_erpnext.doc_events.quotation.before_submit",
		"on_cancel": "jewellery_erpnext.jewellery_erpnext.doc_events.quotation.on_cancel",
		"onload": "jewellery_erpnext.jewellery_erpnext.doc_events.quotation.onload",
	},
	"Delivery Note": {
		"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.delivery_note.validate",
	},
	"Sales Order": {
		"before_validate": "jewellery_erpnext.jewellery_erpnext.customization.sales_order.sales_order.before_validate",
		"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.sales_order.validate",
		"on_submit": "jewellery_erpnext.jewellery_erpnext.doc_events.sales_order.on_submit",
		"on_cancel": "jewellery_erpnext.jewellery_erpnext.doc_events.sales_order.on_cancel",
		"on_update_after_submit": "jewellery_erpnext.jewellery_erpnext.customization.sales_order.sales_order.on_update_after_submit",
	},
	"BOM": {
		"before_validate": "jewellery_erpnext.jewellery_erpnext.doc_events.bom.before_validate",
		"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.bom.validate",
		"on_update": "jewellery_erpnext.jewellery_erpnext.doc_events.bom.on_update",
		"on_cancel": "jewellery_erpnext.jewellery_erpnext.doc_events.bom.on_cancel",
		"on_submit": "jewellery_erpnext.jewellery_erpnext.doc_events.bom.on_submit",
		"on_update_after_submit": "jewellery_erpnext.jewellery_erpnext.doc_events.bom.on_update_after_submit",
	},
	"Work Order": {
		"before_save": "jewellery_erpnext.jewellery_erpnext.doc_events.work_order.before_save",
		"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.work_order.validate",
	},
	"Item": {
		"before_validate": "jewellery_erpnext.jewellery_erpnext.doc_events.item.before_validate",
		"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.item.validate",
		"before_save": "jewellery_erpnext.jewellery_erpnext.doc_events.item.before_save",
		"on_trash": "jewellery_erpnext.jewellery_erpnext.doc_events.item.on_trash",
		"before_insert": "jewellery_erpnext.jewellery_erpnext.doc_events.item.before_insert",
	},
	"Item Attribute": {
		"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.item_attribute.validate"
	},
	"Stock Entry": {
		"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.validate",
		"before_validate": [
			"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.before_validate",
			"jewellery_erpnext.jewellery_erpnext.customization.stock_entry.stock_entry.before_validate",
		],
		"before_submit": "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.before_submit",
		"on_submit": [
			"jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.onsubmit",
			"jewellery_erpnext.jewellery_erpnext.customization.stock_entry.stock_entry.on_submit",
		],
		"on_cancel": "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.on_cancel",
		"on_update_after_submit": "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.on_update_after_submit",
	},
	"Job Card": {
		"onload": "jewellery_erpnext.jewellery_erpnext.doc_events.job_card.onload",
		"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.job_card.validate",
		"on_submit": "jewellery_erpnext.jewellery_erpnext.doc_events.job_card.onsubmit",
	},
	"Diamond Weight": {
		"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.diamond_weight.validate"
	},
	"Gemstone Weight": {
		"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.gemstone_weight.validate"
	},
	"Warehouse": {"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.warehouse.validate"},
	"Purchase Order": {
		"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.validate",
		"on_cancel": "jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.on_cancel",
	},
	"Sales Invoice": {
		"before_validate": [
			"jewellery_erpnext.jewellery_erpnext.doc_events.sales_invoice.before_validate",
			"jewellery_erpnext.jewellery_erpnext.customization.sales_invoice.sales_invoice.before_validate",
		],
		"on_submit": "jewellery_erpnext.jewellery_erpnext.customization.sales_invoice.sales_invoice.on_submit",
	},
	"Serial No": {
		"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.serial_no.update_table"
	},
	"Material Request": {
		"before_validate": "jewellery_erpnext.jewellery_erpnext.doc_events.material_request.before_validate",
		"before_update_after_submit": "jewellery_erpnext.jewellery_erpnext.doc_events.material_request.before_update_after_submit",
		"validate": "jewellery_erpnext.jewellery_erpnext.doc_events.material_request.create_stock_entry",
		"on_submit": "jewellery_erpnext.jewellery_erpnext.doc_events.material_request.on_submit",
	},
	"Serial and Batch Bundle": {
		"after_insert": "jewellery_erpnext.jewellery_erpnext.customization.serial_and_batch_bundle.serial_and_batch_bundle.after_insert"
	},
	"Purchase Receipt": {
		"before_validate": "jewellery_erpnext.jewellery_erpnext.customization.purchase_receipt.purchase_receipt.before_validate",
		"on_submit": "jewellery_erpnext.jewellery_erpnext.customization.purchase_receipt.purchase_receipt.on_submit",
	},
	"Batch": {"validate": "jewellery_erpnext.jewellery_erpnext.customization.batch.batch.validate"},
	"Stock Reconciliation": {
		"validate": "jewellery_erpnext.jewellery_erpnext.customization.stock_reconciliation.stock_reonciliation.validate_department"
	},
}

override_whitelisted_methods = {
	"erpnext.manufacturing.doctype.job_card.job_card.make_stock_entry": "jewellery_erpnext.jewellery_erpnext.doc_events.job_card.make_stock_entry",
	"erpnext.stock.doctype.material_request.material_request.make_stock_entry": "jewellery_erpnext.jewellery_erpnext.doc_events.material_request.make_stock_entry",
	"erpnext.stock.doctype.stock_entry.stock_entry.make_stock_in_entry": "jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry.make_stock_in_entry",
	"frappe.desk.doctype.bulk_update.bulk_update.submit_cancel_or_update_docs": "jewellery_erpnext.jewellery_erpnext.doc_events.bulk_update.custom_submit_cancel_or_update_docs",
}

override_doctype_class = {
	"Stock Entry": "jewellery_erpnext.jewellery_erpnext.customization.stock_entry.stock_entry.CustomStockEntry",
	"Stock Reconciliation": "jewellery_erpnext.jewellery_erpnext.doctype.stock_reconciliation_template.stock_reconciliation_template_utils.CustomStockReconciliation",
	"Stock Ledger Entry": "jewellery_erpnext.jewellery_erpnext.customization.stock_ledger_entry.stock_ledger_entry.CustomStockLedgerEntry",
	"Serial and Batch Bundle": "jewellery_erpnext.jewellery_erpnext.customization.serial_and_batch_bundle.serial_and_batch_bundle.CustomSerialandBatchBundle",
}


scheduler_events = {
	"hourly": [
		"jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.se_utils.rename_stock_entry_docs"
	],
}

# from erpnext.stock import get_item_details
# from jewellery_erpnext.erpnext_override import get_price_list_rate_for
# get_item_details.get_price_list_rate_for = get_price_list_rate_for

# from erpnext.stock.doctype.item_price.item_price import ItemPrice
# from jewellery_erpnext.jewellery_erpnext.doc_events.item_price import check_duplicates
# ItemPrice.check_duplicates = check_duplicates

# from gst_india.gst_india.utils.transaction_data import GSTTransactionData

# from jewellery_erpnext.gurukrupa_exports.overrides.einvoice_override import (
# 	custom_get_all_item_details,
# )

# GSTTransactionData.get_all_item_details = custom_get_all_item_details

# User Data Protection
# --------------------

user_data_fields = [
	{
		"doctype": "{doctype_1}",
		"filter_by": "{filter_by}",
		"redact_fields": ["{field_1}", "{field_2}"],
		"partial": 1,
	},
	{
		"doctype": "{doctype_2}",
		"filter_by": "{filter_by}",
		"partial": 1,
	},
	{
		"doctype": "{doctype_3}",
		"strict": False,
	},
	{"doctype": "{doctype_4}"},
]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"jewellery_erpnext.auth.validate"
# ]

# fixtures = [
#     {
#          "dt": "Custom Field",
#          "filters":["name", "in", [
#              'Stock Entry Detail-custom_sub_setting_type',
#              'Stock Entry Detail-custom_is_customer_item',
#              'Material Request Item-custom_sub_setting_type',
#              'Material Request Item-custom_is_customer_item'
# 		]]
#       }
#     ]
