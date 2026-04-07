"""
EOD (End of Day) MOP Log Sync Engine.

Processes unsynced MOP Log events, aggregates net movements per
Manufacturing Operation, and creates Stock Entries to update the
stock ledger. This is the final step in the event-driven MOP
architecture where intra-day operations are buffered and only
materialized at EOD.

The sync can be triggered:
  1. Manually via **MOP Settings → Sync MOP Log** button
  2. Automatically via Frappe scheduler (daily event)
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, now


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@frappe.whitelist()
def sync_mop_logs():
	"""Entry point for EOD sync. Called from MOP Settings button or scheduler.

	Algorithm:
	  1. Fetch all unsynced, non-cancelled MOP Logs
	  2. Group by manufacturing_operation
	  3. For each MOP, aggregate net qty per (item_code, batch_no)
	  4. Create Stock Entry (Material Transfer) for net movements
	  5. Mark processed logs as is_synced = 1
	  6. Return summary
	"""
	unsynced = _get_unsynced_logs()
	if not unsynced:
		frappe.msgprint(_("No unsynced MOP Logs found."), indicator="green")
		return {"processed": 0, "stock_entries": []}

	# Group logs by manufacturing_operation
	grouped = _group_by_mop(unsynced)

	created_entries = []
	total_processed = 0

	for mop_name, logs in grouped.items():
		try:
			se_name = _process_mop_group(mop_name, logs)
			if se_name:
				created_entries.append(se_name)

			# Mark all logs in this group as synced
			log_names = [log.name for log in logs]
			_mark_synced(log_names)
			total_processed += len(log_names)

		except Exception:
			frappe.log_error(
				title=f"EOD Sync Error for MOP {mop_name}",
				message=frappe.get_traceback(),
			)
			# Continue with other MOPs — don't let one failure block all
			continue

	frappe.db.commit()

	summary = {
		"processed": total_processed,
		"stock_entries": created_entries,
	}

	frappe.msgprint(
		_("EOD Sync complete: {0} logs processed, {1} Stock Entries created.").format(
			total_processed, len(created_entries)
		),
		indicator="green",
	)

	return summary


# ---------------------------------------------------------------------------
# Internal Functions
# ---------------------------------------------------------------------------

def _get_unsynced_logs():
	"""Fetch all MOP Logs with is_synced=0 and is_cancelled=0."""
	return frappe.get_all(
		"MOP Log",
		filters={"is_synced": 0, "is_cancelled": 0},
		fields=[
			"name",
			"item_code",
			"batch_no",
			"qty_change",
			"pcs_change",
			"from_warehouse",
			"to_warehouse",
			"manufacturing_operation",
			"manufacturing_work_order",
			"voucher_type",
			"voucher_no",
			"serial_and_batch_bundle",
		],
		order_by="creation asc",
		limit_page_length=0,
	)


def _group_by_mop(logs):
	"""Group MOP Logs by manufacturing_operation.

	Returns dict: {mop_name: [log1, log2, ...]}
	"""
	grouped = {}
	for log in logs:
		grouped.setdefault(log.manufacturing_operation, []).append(log)
	return grouped


def _process_mop_group(mop_name, logs):
	"""Process a group of MOP Logs for a single Manufacturing Operation.

	Aggregates net movements per (item_code, batch_no, from_wh, to_wh)
	and creates a Stock Entry if there are non-zero net movements.

	Returns Stock Entry name or None.
	"""
	precision = cint(frappe.db.get_single_value("System Settings", "float_precision"))

	# Aggregate net qty per movement key
	# Key: (item_code, batch_no, from_warehouse, to_warehouse)
	net_movements = {}
	mwo = None

	for log in logs:
		mwo = mwo or log.manufacturing_work_order

		key = (
			log.item_code,
			log.batch_no,
			log.from_warehouse,
			log.to_warehouse,
		)

		if key not in net_movements:
			net_movements[key] = frappe._dict({
				"qty": 0,
				"pcs": 0,
				"sbb": log.serial_and_batch_bundle,
			})

		net_movements[key].qty += flt(log.qty_change, precision)
		net_movements[key].pcs += cint(log.pcs_change)

	# Filter out zero-net movements
	non_zero = {k: v for k, v in net_movements.items() if flt(v.qty, precision) != 0}

	if not non_zero:
		# All movements cancelled out — nothing to do
		return None

	# Get MOP meta for the Stock Entry
	mop_data = frappe.db.get_value(
		"Manufacturing Operation",
		mop_name,
		["department", "manufacturing_order", "employee"],
		as_dict=True,
	)

	if not mop_data:
		return None

	department = mop_data.department
	company = frappe.db.get_value("Department", department, "company") if department else None

	if not company:
		company = frappe.defaults.get_defaults().get("company")

	# ── Create Stock Entry ──
	se = frappe.new_doc("Stock Entry")
	se.company = company
	se.stock_entry_type = "Material Transfer to Department"
	se.purpose = "Material Transfer"
	se.department = department
	se.to_department = department
	se.auto_created = 1
	se.set_posting_time = 1
	se.posting_date = frappe.utils.today()
	se.posting_time = frappe.utils.nowtime()

	# Add a comment linking to the EOD sync
	se.remarks = f"EOD Sync — MOP: {mop_name}"

	has_items = False

	for (item_code, batch_no, from_wh, to_wh), movement in non_zero.items():
		qty = flt(movement.qty, precision)

		if qty > 0:
			# Positive net = material moved FROM → TO
			s_warehouse = from_wh
			t_warehouse = to_wh
		else:
			# Negative net = reverse direction
			s_warehouse = to_wh
			t_warehouse = from_wh
			qty = abs(qty)

		if not s_warehouse and not t_warehouse:
			# Loss event — create as Process Loss (source only, no target)
			# Use department warehouse as source for loss accounting
			dept_wh = frappe.db.get_value(
				"Warehouse",
				{"disabled": 0, "department": department, "warehouse_type": "Manufacturing"},
			)
			s_warehouse = dept_wh
			t_warehouse = None

		se.append("items", {
			"item_code": item_code,
			"qty": qty,
			"pcs": abs(cint(movement.pcs)),
			"s_warehouse": s_warehouse,
			"t_warehouse": t_warehouse,
			"batch_no": batch_no,
			"use_serial_batch_fields": True,
			"serial_and_batch_bundle": movement.sbb,
			"manufacturing_operation": mop_name,
			"custom_manufacturing_work_order": mwo,
			"department": department,
			"to_department": department,
		})
		has_items = True

	if not has_items:
		return None

	se.flags.ignore_permissions = True
	se.save()
	se.submit()

	return se.name


def _mark_synced(log_names):
	"""Bulk-mark MOP Logs as synced.

	Uses a single SQL UPDATE for performance.
	"""
	if not log_names:
		return

	# Batch in chunks of 500 to avoid overly large IN clauses
	chunk_size = 500
	for i in range(0, len(log_names), chunk_size):
		chunk = log_names[i : i + chunk_size]
		placeholders = ", ".join(["%s"] * len(chunk))
		frappe.db.sql(
			f"""
			UPDATE `tabMOP Log`
			SET is_synced = 1, modified = %s
			WHERE name IN ({placeholders})
			""",
			[now()] + chunk,
		)


# ---------------------------------------------------------------------------
# Scheduler Hook
# ---------------------------------------------------------------------------

def daily_sync_mop_logs():
	"""Called by Frappe scheduler at end of day.

	Wraps sync_mop_logs() with error handling and logging.
	"""
	try:
		result = sync_mop_logs()
		frappe.logger().info(
			f"EOD MOP Sync: {result.get('processed', 0)} logs processed, "
			f"{len(result.get('stock_entries', []))} SEs created"
		)
	except Exception:
		frappe.log_error(
			title="EOD MOP Sync Failed",
			message=frappe.get_traceback(),
		)
