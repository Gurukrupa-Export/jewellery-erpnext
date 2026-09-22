"""Missing-index fix found by profiling a slow "Material Transfer (DEPARTMENT)" Stock
Entry submission with the Optimus profiler (``tabOptimus Session`` p66cuot6oi /
af91f794ffbc6a50, 2026-09-22): both tables are searched by ``voucher_no`` during Stock
Entry submission's batch/serial validation, with no index on that column -- costing
~100-230ms per submit and only growing as the tables do.

``frappe.db.add_index`` is idempotent (``ADD INDEX IF NOT EXISTS``), so re-running this
patch is a no-op.
"""

import frappe


def execute():
	frappe.db.add_index("Serial and Batch Entry", ["voucher_no"])
	frappe.db.add_index("Serial and Batch Bundle", ["voucher_no"])
