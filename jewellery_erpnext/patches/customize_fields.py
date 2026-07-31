"""
Add per-Sales-Order precision-override custom fields to Sales Order.

These two Check fields let a Sales Order override the customer-level precision
defaults used when the BOM is (re)built during Sales Order validation
(_get_bom_context in doc_events/sales_order.py):
  - custom_precision            -> when ticked, forces metal_precision = 2
  - custom_precision_for_stone  -> when ticked, forces stone_precision = 2

They are declared only in custom_fields/sales_order.json, but this app's
custom_fields/*.json are dead config on real and CI sites: the after_migrate
hook that would sync them is disabled (hooks.py), and install-app marks patches
"complete" without running them on fresh sites. So the fields were referenced in
code but never created in the DB, causing every Sales Order save to crash with:
  AttributeError: 'SalesOrder' object has no attribute 'custom_precision'

This patch provisions them via the canonical helper. Idempotent (keyed on
(dt, fieldname)). Ad-hoc entry point:
  bench --site <site> execute jewellery_erpnext.patches.add_sales_order_precision_fields.execute
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {
		"Sales Order": [
			{

   "fieldname": "custom_metal_weight",
   "fieldtype": "Float",
   "insert_after": "items",
   "label": "Metal Weight",
   "module": "Jewellery Erpnext"
  },
  {
   
   "fieldname": "custom_finding_weight",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "custom_metal_weight",
   "label": "Finding Weight",
   "module": "Jewellery Erpnext"
  },
  {
  
   "fieldname": "custom_gemstone_weight",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "custom_finding_weight",
   "label": "Gemstone Weight",
   "module": "Jewellery Erpnext"
  },
  {

   "fieldname": "custom_diamond_weight",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "custom_gemstone_weight",
   "label": "Diamond Weight",
   "module": "Jewellery Erpnext"
  },
  {
  
   "fieldname": "custom_other_weight",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "custom_diamond_weight",
   "label": "Other Weight",
   "module": "Jewellery Erpnext"
  },
  {

   "fieldname": "custom_diamond_pcs",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "custom_other_weight",
   "label": "Diamond Pcs",
   "module": "Jewellery Erpnext"
  },
  {
  
   "fieldname": "custom_gemstone_pcs",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "custom_diamond_pcs",
   "label": "Gemstone Pcs",
   "module": "Jewellery Erpnext"
  },
  {

   "fieldname": "custom_gross_weight",
   "fieldtype": "Float",
   "insert_after": "custom_gemstone_pcs",
   "label": "Gross Weight",
   "module": "Jewellery Erpnext"
  }
		],
        "Delivery Note":[
       {
    
   "fieldname": "custom_metal_weight",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "items",
   "label": "Metal Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_finding_weight",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "custom_metal_weight",
   "label": "Finding Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_gemstone_weight",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "custom_finding_weight",
   "label": "Gemstone Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_diamond_weight",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "custom_gemstone_weight",
   "label": "Diamond Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_other_weight",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "custom_diamond_weight",
   "label": "Other Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_diamond_pcs",
   "fieldtype": "Int",
   "is_system_generated": 1,
   "insert_after": "custom_other_weight",
   "label": "Diamond Pcs",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_gemstone_pcs",
   "fieldtype": "Int",
   "is_system_generated": 1,
   "insert_after": "custom_diamond_pcs",
   "label": "Gemstone Pcs",
   "module": "Jewellery Erpnext"
  },
  {
   
   "fieldname": "custom_gross_weight",
   "fieldtype": "Float",
   "insert_after": "custom_gemstone_pcs",
   "label": "Gross Weight",
   "module": "Jewellery Erpnext"
  }
        ],
        "Delivery Note Item":[
{
    
   "fieldname": "custom_metal_weight",
   "fieldtype": "Float",
   "insert_after": "taxable_value",
   "label": "Metal Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_finding_weight",
   "fieldtype": "Float",
   "insert_after": "custom_metal_weight",
   "label": "Finding Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_gemstone_weight",
   "fieldtype": "Float",
   "insert_after": "custom_finding_weight",
   "label": "Gemstone Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_diamond_weight",
   "fieldtype": "Float",
   "insert_after": "custom_gemstone_weight",
   "label": "Diamond Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_other_weight",
   "fieldtype": "Float",
   "insert_after": "custom_diamond_weight",
   "label": "Other Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_diamond_pcs",
   "fieldtype": "Int",
   "insert_after": "custom_other_weight",
   "label": "Diamond Pcs",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_gemstone_pcs",
   "fieldtype": "Int",
   "insert_after": "custom_diamond_pcs",
   "label": "Gemstone Pcs",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_gross_weight",
   "fieldtype": "Float",
   "insert_after": "custom_gemstone_pcs",
   "label": "Gross Weight",
   "module": "Jewellery Erpnext"
  }
        ],
      "Sales Invoice":[
		  {
    
   "fieldname": "custom_metal_weight",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "items",
   "label": "Metal Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_finding_weight",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "custom_metal_weight",
   "label": "Finding Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_gemstone_weight",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "custom_finding_weight",
   "label": "Gemstone Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_diamond_weight",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "custom_gemstone_weight",
   "label": "Diamond Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_other_weight",
   "fieldtype": "Float",
   "is_system_generated": 1,
   "insert_after": "custom_diamond_weight",
   "label": "Other Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_diamond_pcs",
   "fieldtype": "Int",
   "is_system_generated": 1,
   "insert_after": "custom_other_weight",
   "label": "Diamond Pcs",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_gemstone_pcs",
   "fieldtype": "Int",
   "is_system_generated": 1,
   "insert_after": "custom_diamond_pcs",
   "label": "Gemstone Pcs",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_gross_weight",
   "fieldtype": "Float",
   "insert_after": "custom_gemstone_pcs",
   "label": "Gross Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_product_return_form_ref",
   "fieldtype": "Link",
   "insert_after": "due_date",
   "label": "Product Return Form",
   "module": "Jewellery Erpnext",
   "options": "Product Return Order Form"
  }
      ],
      "Sales Invoice Item":[
        {
    
   "fieldname": "custom_metal_weight",
   "fieldtype": "Float",
   "insert_after": "taxable_value",
   "label": "Metal Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_finding_weight",
   "fieldtype": "Float",
   "insert_after": "custom_metal_weight",
   "label": "Finding Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_gemstone_weight",
   "fieldtype": "Float",
   "insert_after": "custom_finding_weight",
   "label": "Gemstone Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_diamond_weight",
   "fieldtype": "Float",
   "insert_after": "custom_gemstone_weight",
   "label": "Diamond Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_other_weight",
   "fieldtype": "Float",
   "insert_after": "custom_diamond_weight",
   "label": "Other Weight",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_diamond_pcs",
   "fieldtype": "Int",
   "insert_after": "custom_other_weight",
   "label": "Diamond Pcs",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_gemstone_pcs",
   "fieldtype": "Int",
   "insert_after": "custom_diamond_pcs",
   "label": "Gemstone Pcs",
   "module": "Jewellery Erpnext"
  },
  {
    
   "fieldname": "custom_gross_weight",
   "fieldtype": "Float",
   "insert_after": "custom_gemstone_pcs",
   "label": "Gross Weight",
   "module": "Jewellery Erpnext"
  }
      ]
	}
    
	create_custom_fields(custom_fields, ignore_validate=True)
	# frappe.logger().info(
	# 	"add_sales_order_precision_fields: custom fields created/updated on Sales Order"
	# )
