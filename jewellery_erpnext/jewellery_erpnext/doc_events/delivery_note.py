import frappe
from frappe import _

def validate(self, method):
    if self.is_return:
        return
    # frappe.throw("hii")
    # self.set("custom_invoice_item", [])
    # added_items = set()  
    for row in self.items:
        if row.against_sales_order:
            if row.bom:
                bom_doc = frappe.get_doc("BOM", row.bom)
                row.custom_diamond_pcs=bom_doc.total_diamond_pcs
                row.custom_gemstone_pcs=bom_doc.total_gemstone_pcs
                row.custom_other_weight = bom_doc.total_other_weight
                row.custom_metal_weight=bom_doc.total_metal_weight
                row.custom_finding_weight=bom_doc.finding_weight
                row.custom_diamond_weight=bom_doc.total_diamond_weight_in_gms
                row.custom_gemstone_weight=bom_doc.total_gemstone_weight_in_gms
                row.custom_gross_weight=bom_doc.gross_weight
    self.custom_diamond_pcs = sum(int(r.custom_diamond_pcs or 0) for r in self.items)
    self.custom_gemstone_pcs = sum(float(r.custom_gemstone_pcs or 0) for r in self.items)
    self.custom_other_weight = sum(float(r.custom_other_weight) for r in self.items)
    self.custom_metal_weight = sum(float(r.custom_metal_weight) for r in self.items)
    self.custom_finding_weight = sum(float(r.custom_finding_weight) for r in self.items)
    self.custom_diamond_weight = sum(float(r.custom_diamond_weight) for r in self.items)
    self.custom_gemstone_weight = sum(float(r.custom_gemstone_weight) for r in self.items)
    self.custom_gross_weight = sum(float(r.custom_gross_weight) for r in self.items)
            # sales_order_id = row.against_sales_order
            
    #         invoice_items = frappe.get_all(
    #             'Sales Order E Invoice Item',
    #             filters={'parent': sales_order_id},
    #             fields=['item_code', 'item_name', 'uom', 'qty', 'rate', 'amount',"tax_amount","amount_with_tax","tax_rate"]
    #         )
            
    #         for invoice_item in invoice_items:
    #             item_key = (
    #                 invoice_item.item_code,
    #                 invoice_item.item_name,
    #                 invoice_item.uom,
    #                 invoice_item.qty,
    #                 invoice_item.rate,
    #                 invoice_item.amount
    #             )
                
    #             if item_key not in added_items:
    #                 added_items.add(item_key)
    #                 self.append('custom_invoice_item', {
    #                     'item_code': invoice_item.item_code,
    #                     'item_name': invoice_item.item_name,
    #                     'uom': invoice_item.uom,
    #                     'qty': invoice_item.qty,
    #                     'rate': invoice_item.rate,
    #                     'amount': invoice_item.amount,
    #                     "tax_amount":invoice_item.tax_amount,
    #                     "amount_with_tax":invoice_item.amount_with_tax,
    #                     "tax_rate":invoice_item.tax_rate
    #                 })

    #     if row.serial_no:
    #         source_warehouse=frappe.db.get_value('Serial No',row.serial_no,'warehouse')
    #         self.set_warehouse=source_warehouse
    #         row.warehouse=source_warehouse
        
    # for r in self.items:
    #     if r.serial_no:
    #         source_warehouse=frappe.db.get_value('Serial No',r.serial_no,'warehouse')
    #         # frappe.throw(f"{row}")
    #         self.set_warehouse=source_warehouse
    #         r.warehouse=source_warehouse


# def validate(self, method):
#     self.set("custom_invoice_item", [])
#     added_items = set()
#     source_warehouse = None

#     for row in self.items:
#         if row.against_sales_order:
#             invoice_items = frappe.get_all(
#                 'Sales Order E Invoice Item',
#                 filters={'parent': row.against_sales_order},
#                 fields=['item_code', 'item_name', 'uom', 'qty', 'rate', 'amount',
#                         'tax_amount', 'amount_with_tax', 'tax_rate']
#             )

#             for invoice_item in invoice_items:
#                 item_key = (
#                     invoice_item.item_code,
#                     invoice_item.item_name,
#                     invoice_item.uom,
#                     invoice_item.qty,
#                     invoice_item.rate,
#                     invoice_item.amount
#                 )
#                 if item_key not in added_items:
#                     added_items.add(item_key)
#                     self.append('custom_invoice_item', invoice_item)  # ✅ Pass dict directly

#         if row.serial_no and source_warehouse is None:
#             source_warehouse = frappe.db.get_value('Serial No', row.serial_no, 'warehouse')

#         if row.serial_no:
#             row.warehouse = source_warehouse
    
#     if source_warehouse:
#         self.set_warehouse = source_warehouse
        