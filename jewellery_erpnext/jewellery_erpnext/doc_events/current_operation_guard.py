"""Work-order current-operation guard: the subset Settle needs on gurukrupa-uat.

Whether a Manufacturing Operation is still live work -- an open status, and not a reverted
operation kept only as history. Customer Goods Return Settle reads it before taking a batch back
from a work order whose IR moves wait for the end-of-day sync
(``cg_settle._pending_on_another_open_operation``).

kggk_uat's module of the same name (commit ``baeb15fa``) also holds the Employee IR / Department
IR current-operation guard, which gurukrupa-uat does not have. These definitions are copied from
it verbatim; if that commit is ever ported, take kggk_uat's full file over this one.
"""

from frappe.utils import cstr

OPEN_STATUSES = ("Not Started", "On Hold", "WIP", "QC Pending", "QC Completed")
REVERT = "Revert"


def is_reverted(status, department_ir_status=None):
	"""True for an operation kept only as history.

	kggk_uat does not delete the operation a cancelled Department Issue / Employee Receive minted:
	it marks it ``department_ir_status = "Revert"`` (status Not Started) and moves the work order
	back to the source. Every picker already skips those rows, and the audit
	(``mop_lineage_audit._co_reverted``) treats them the same way: never a live operation and
	never a successor.
	"""
	return cstr(status) == REVERT or cstr(department_ir_status) == REVERT


def is_open_operation(status, department_ir_status=None):
	"""True for an operation that is still live work: an open status and not reverted."""
	return cstr(status) in OPEN_STATUSES and not is_reverted(
		status, department_ir_status
	)
