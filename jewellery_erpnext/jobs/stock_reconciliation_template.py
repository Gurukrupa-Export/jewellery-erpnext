from jewellery_erpnext.jewellery_erpnext.doctype.stock_reconciliation_template.stock_reconciliation_template_utils import create_stock_reconciliation_from_template

def create_stock_reconciliation():
    """
    Trigger the stock reconciliation process based on the defined templates.
    This function will be called every minute by the scheduler.
    """
    create_stock_reconciliation_from_template()