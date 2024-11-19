import frappe

from jewellery_erpnext.jewellery_erpnext.customization.sales_order.doc_events.branch_utils import (
	create_branch_so,
)
from jewellery_erpnext.jewellery_erpnext.customization.sales_order.doc_events.utils import (
	update_delivery_date,
	validate_duplicate_so,
)


def on_update_after_submit(self, method):
	update_delivery_date(self)


def before_validate(self, method):
	validate_duplicate_so(self)


def on_submit(self, method):
	create_branch_so(self)
