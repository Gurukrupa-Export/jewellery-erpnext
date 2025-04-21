import frappe
from frappe import _
from frappe.utils import flt
from erpnext.stock.stock_ledger import (
	validate_cancellation,
	set_as_cancel,
	validate_serial_no,
	get_args_for_future_sle,
	make_entry,
	get_incoming_outgoing_rate_for_cancel,
	repost_current_voucher,
	update_bin_qty
)

from erpnext.stock.utils import (
	get_combine_datetime,
	get_incoming_outgoing_rate_for_cancel,
	get_or_make_bin,
)

def make_sl_entries(sl_entries, allow_negative_stock=False, via_landed_cost_voucher=False):
	"""Create SL entries from SL entry dicts

	args:
			- allow_negative_stock: disable negative stock valiations if true
			- via_landed_cost_voucher: landed cost voucher cancels and reposts
			entries of purchase document. This flag is used to identify if
			cancellation and repost is happening via landed cost voucher, in
			such cases certain validations need to be ignored (like negative
							stock)
	"""
	from erpnext.controllers.stock_controller import future_sle_exists

	if sl_entries:
		cancel = sl_entries[0].get("is_cancelled")
		if cancel:
			validate_cancellation(sl_entries)
			set_as_cancel(sl_entries[0].get("voucher_type"), sl_entries[0].get("voucher_no"))

		args = get_args_for_future_sle(sl_entries[0])
		future_sle_exists(args, sl_entries)

		repo = getattr(frappe.local, "batch_valuation_ledger", None)

		for sle in sl_entries:
			if sle.serial_no and not via_landed_cost_voucher:
				validate_serial_no(sle)

			if cancel:
				sle["actual_qty"] = -flt(sle.get("actual_qty"))

				if sle["actual_qty"] < 0 and not sle.get("outgoing_rate"):
					sle["outgoing_rate"] = get_incoming_outgoing_rate_for_cancel(
						sle.item_code, sle.voucher_type, sle.voucher_no, sle.voucher_detail_no
					)
					sle["incoming_rate"] = 0.0

				if sle["actual_qty"] > 0 and not sle.get("incoming_rate"):
					sle["incoming_rate"] = get_incoming_outgoing_rate_for_cancel(
						sle.item_code, sle.voucher_type, sle.voucher_no, sle.voucher_detail_no
					)
					sle["outgoing_rate"] = 0.0

			# Update ledger_state after SLE submission (triggers on_submit)
			if sle.get("actual_qty") or sle.get("voucher_type") == "Stock Reconciliation":
				sle_doc = make_entry(sle, allow_negative_stock, via_landed_cost_voucher)

				if repo and sle.get("serial_and_batch_bundle") and sle.actual_qty < 0:
					bundle_entries = frappe.get_all(
						"Serial and Batch Entry",
						filters={"parent": sle.serial_and_batch_bundle},
						fields=["batch_no", "stock_value_difference", "qty"]
					)
					repo.update(sle, bundle_entries)

			args = sle_doc.as_dict()
			args["posting_datetime"] = get_combine_datetime(args.posting_date, args.posting_time)

			if sle.get("voucher_type") == "Stock Reconciliation":
				# preserve previous_qty_after_transaction for qty reposting
				args.previous_qty_after_transaction = sle.get("previous_qty_after_transaction")

			is_stock_item = frappe.get_cached_value("Item", args.get("item_code"), "is_stock_item")
			if is_stock_item:
				bin_name = get_or_make_bin(args.get("item_code"), args.get("warehouse"))
				args.reserved_stock = flt(frappe.db.get_value("Bin", bin_name, "reserved_stock"))
				repost_current_voucher(args, allow_negative_stock, via_landed_cost_voucher)
				update_bin_qty(bin_name, args)
			else:
				frappe.msgprint(
					_("Item {0} ignored since it is not a stock item").format(args.get("item_code"))
				)