"""One-off: deactivate Serial Nos by cancelling the Stock Entry that created them.

ERPNext sets a Serial No to ``Inactive`` and clears its warehouse when the voucher it
references (``reference_name``) is cancelled (``SerialBatchBundle.
update_serial_no_status_warehouse``), so cancelling the creating entry is the supported
way to retire a tag. The defaults are the four KG GK tags reported on 2026-11-04, each
made by its own Serial Number Creator ``Manufacture`` entry on 2026-09-19.

An entry is cancelled only when, for every serial on it:

  1. The Serial No is ``Active`` and was created by a submitted Stock Entry.
  2. The serial has not moved since: its only live Serial and Batch Bundle is the inward
     on that entry. A piece that was delivered, invoiced or transferred is refused,
     because cancelling would leave the later voucher without its stock.
  3. Every serial the entry creates was requested, so a cancel never retires a tag
     nobody asked for.

Anything that fails a check is reported and left alone.

Only the entry is reversed. The Serial Number Creator, Manufacturing Work Order and
Manufacturing Operation stay as they are, and the consumed material returns to the WIP
warehouses it came from. The entries are backdated, so ERPNext queues Repost Item
Valuation entries for the scheduler; the gold metal one is large.

Run outside the EOD sync window: Stock Entry ``before_cancel`` refuses while it runs.

NOT registered in patches.txt; run it manually. Dry-run first:

    bench --site <site> execute \
        jewellery_erpnext.patches.deactivate_serial_nos.execute

Apply:

    bench --site <site> execute \
        jewellery_erpnext.patches.deactivate_serial_nos.execute \
        --kwargs "{'dry_run': False}"

Other serials: ``--kwargs "{'serial_nos': ['SN-1', 'SN-2'], 'dry_run': False}"``.
"""

import frappe

DEFAULT_SERIAL_NOS = ("KLHG42F0552", "KLHGX62F2614", "KLHGX62F2613", "KLHG42F0551")


def _live_vouchers(serial_no):
	"""``{(voucher_type, voucher_no)}`` of every live bundle that carries the serial."""
	rows = frappe.db.sql(
		"""
		select b.voucher_type, b.voucher_no
		from `tabSerial and Batch Entry` e
		join `tabSerial and Batch Bundle` b on b.name = e.parent
		where e.serial_no = %s and b.docstatus = 1 and b.is_cancelled = 0
		""",
		(serial_no,),
	)
	return {(voucher_type, voucher_no) for voucher_type, voucher_no in rows}


def _serials_created_by(stock_entry):
	"""Every serial the entry brings in, by the inward bundles and by ``reference_name``."""
	from_bundles = frappe.db.sql_list(
		"""
		select e.serial_no
		from `tabSerial and Batch Entry` e
		join `tabSerial and Batch Bundle` b on b.name = e.parent
		where b.voucher_type = 'Stock Entry' and b.voucher_no = %s
			and b.type_of_transaction = 'Inward' and b.docstatus = 1 and b.is_cancelled = 0
			and ifnull(e.serial_no, '') != ''
		""",
		(stock_entry,),
	)
	referencing = frappe.get_all(
		"Serial No",
		filters={"reference_doctype": "Stock Entry", "reference_name": stock_entry},
		pluck="name",
	)
	return set(from_bundles) | set(referencing)


def _classify(serial_nos):
	"""Return (to_cancel, refused): ``{stock_entry: [serial_no, ...]}``, ``{serial_no: reason}``."""
	to_cancel, refused = {}, {}

	for serial_no in serial_nos:
		row = frappe.db.get_value(
			"Serial No",
			serial_no,
			["status", "reference_doctype", "reference_name"],
			as_dict=True,
		)
		if not row:
			refused[serial_no] = "Serial No not found"
			continue
		if row.status != "Active":
			refused[serial_no] = f"status is {row.status or '(blank)'}, not Active"
			continue
		if row.reference_doctype != "Stock Entry" or not row.reference_name:
			refused[serial_no] = (
				f"created by {row.reference_doctype or '(nothing)'} "
				f"{row.reference_name or ''}, not a Stock Entry"
			).strip()
			continue

		stock_entry = row.reference_name
		if frappe.db.get_value("Stock Entry", stock_entry, "docstatus") != 1:
			refused[serial_no] = f"{stock_entry} is not submitted"
			continue

		vouchers = _live_vouchers(serial_no)
		if vouchers != {("Stock Entry", stock_entry)}:
			others = sorted(
				f"{vt} {vn}" for vt, vn in vouchers - {("Stock Entry", stock_entry)}
			)
			refused[serial_no] = (
				f"moved after {stock_entry}: {', '.join(others)}"
				if others
				else f"no live Serial and Batch Bundle on {stock_entry}"
			)
			continue

		to_cancel.setdefault(stock_entry, []).append(serial_no)

	requested = set(serial_nos)
	for stock_entry in list(to_cancel):
		unrequested = _serials_created_by(stock_entry) - requested
		if unrequested:
			for serial_no in to_cancel.pop(stock_entry):
				refused[serial_no] = (
					f"{stock_entry} also creates {', '.join(sorted(unrequested))}, "
					"which was not requested"
				)

	return to_cancel, refused


def execute(serial_nos=None, dry_run=True):
	serial_nos = list(dict.fromkeys(serial_nos or DEFAULT_SERIAL_NOS))
	to_cancel, refused = _classify(serial_nos)

	print(f"[deactivate-serial-nos] Requested serials: {len(serial_nos)}")
	print(f"  Stock Entries to cancel: {len(to_cancel)}")
	for stock_entry, serials in to_cancel.items():
		print(f"    {stock_entry}: {', '.join(serials)}")
	print(f"  Refused serials (left as they are): {len(refused)}")
	for serial_no, reason in refused.items():
		print(f"    {serial_no}: {reason}")

	if dry_run:
		print(
			"[deactivate-serial-nos] DRY RUN — no changes written. "
			"Re-run with dry_run=False to apply."
		)
		return

	if not to_cancel:
		print("[deactivate-serial-nos] Nothing to cancel.")
		return

	# Newest first, one commit each, so a failure leaves every earlier cancel in place
	# and nothing half-done.
	ordered = frappe.get_all(
		"Stock Entry",
		filters={"name": ["in", list(to_cancel)]},
		order_by="posting_date desc, posting_time desc, creation desc",
		pluck="name",
	)
	cancelled = []
	for stock_entry in ordered:
		try:
			frappe.get_doc("Stock Entry", stock_entry).cancel()
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			print(
				f"[deactivate-serial-nos] FAILED to cancel {stock_entry}; stopped here."
			)
			print(frappe.get_traceback())
			break
		cancelled.append(stock_entry)
		print(f"[deactivate-serial-nos] Cancelled {stock_entry}")

	for stock_entry in cancelled:
		for serial_no in to_cancel[stock_entry]:
			status, warehouse = frappe.db.get_value(
				"Serial No", serial_no, ["status", "warehouse"]
			)
			print(f"    {serial_no}: {status} (warehouse: {warehouse or 'none'})")

	print(
		f"[deactivate-serial-nos] Cancelled {len(cancelled)} of {len(ordered)} Stock Entries. "
		f"Repost Item Valuation entries queued on the site: "
		f"{frappe.db.count('Repost Item Valuation', {'docstatus': 1, 'status': 'Queued'})}"
	)
