import frappe
from frappe import _

def validate(self, method):
    self.set("custom_invoice_item", [])
    added_items = set()  

    for row in self.items:
        if row.against_sales_order:
            sales_order_id = row.against_sales_order
            
            invoice_items = frappe.get_all(
                'Sales Order E Invoice Item',
                filters={'parent': sales_order_id},
                fields=['item_code', 'item_name', 'uom', 'qty', 'rate', 'amount',"tax_amount","amount_with_tax","tax_rate"]
            )
            
            for invoice_item in invoice_items:
                item_key = (
                    invoice_item.item_code,
                    invoice_item.item_name,
                    invoice_item.uom,
                    invoice_item.qty,
                    invoice_item.rate,
                    invoice_item.amount
                )
                
                if item_key not in added_items:
                    added_items.add(item_key)
                    self.append('custom_invoice_item', {
                        'item_code': invoice_item.item_code,
                        'item_name': invoice_item.item_name,
                        'uom': invoice_item.uom,
                        'qty': invoice_item.qty,
                        'rate': invoice_item.rate,
                        'amount': invoice_item.amount,
                        "tax_amount":invoice_item.tax_amount,
                        "amount_with_tax":invoice_item.amount_with_tax,
                        "tax_rate":invoice_item.tax_rate
                    })

    for r in self.items:
        if r.serial_no:
            source_warehouse=frappe.db.get_value('Serial No',r.serial_no,'warehouse')
            # frappe.throw(f"{row}")
            self.set_warehouse=source_warehouse
            r.warehouse=source_warehouse

    