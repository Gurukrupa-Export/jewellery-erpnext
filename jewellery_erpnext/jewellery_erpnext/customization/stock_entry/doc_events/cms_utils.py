import frappe

def create_customer_main_slip(item_code,t_warehouse,batch_no,company):
    # Only process Material Transfer type
    if not t_warehouse or not batch_no or not item_code or "24KT" not in item_code:
        return

    is_cms_warehouse = frappe.db.get_value(
            "Warehouse", t_warehouse, "custom_is_customer_main_slip"
        )
    if not is_cms_warehouse:
        return
    
    ref_data = frappe.db.get_value(
        "Batch",batch_no,
        ["reference_doctype", "reference_name", "custom_customer"],
          as_dict=True,
          )
    if not ref_data:
        frappe.msgprint(f"No Batch data found for {batch_no}")
        return

    ref_doctype = ref_data.reference_doctype
    ref_name = ref_data.reference_name
    batch_customer = ref_data.custom_customer

        
    if frappe.db.exists("Customer Metal Main Slip", {"batch_no":batch_no}):
        frappe.throw(f"CMS already exists for Batch {batch_no}")

    customer = None
        
    if ref_doctype == "Stock Entry":
        stock_data = frappe.db.get_value(
            "Stock Entry",
            ref_name,
            ["stock_entry_type", "customer_voucher_type", "_customer"],
            as_dict=True,
        )
        if (
            stock_data
            and stock_data.stock_entry_type == "Customer Goods Received"
            and stock_data.customer_voucher_type == "Customer Subcontracting"
        ):
            customer = stock_data._customer

        
    elif ref_doctype == "Purchase Receipt":
        customer = batch_customer

    if not customer:
        frappe.throw(f"Customer not found for Batch {batch_no}")

        # Create CMS record
    cms = frappe.new_doc("Customer Metal Main Slip")
    cms.company = company
    cms.warehouse = t_warehouse
    cms.customer = customer
    cms.batch_no = batch_no
    cms.voucher_type = ref_doctype
    cms.voucher_no = ref_name
    cms.insert()

    frappe.msgprint(f"Customer Metal Main Slip {cms.name} created successfully")