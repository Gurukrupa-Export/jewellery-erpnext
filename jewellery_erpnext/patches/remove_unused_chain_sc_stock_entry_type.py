"""Remove the "Material Transfer (Chain SC)" Stock Entry Type where nothing live uses it.

The first Metal Conversions Chain Subcontracting release seeded this type for both of its
transfers. Chain Manufacturing now books the existing types instead -- "Material Transfer to
Employee" or "Material Transfer to Subcontractor" on Issue, plain "Material Transfer" for the
return -- so the type is no longer seeded (``seed_stock_entry_types``). A site that migrated
the first release still lists it in every Stock Entry Type picker.

Deleted only when no draft or submitted Stock Entry names it: such entries must still resolve
their type, and Frappe would refuse the delete anyway. Cancelled entries do not count -- the
same rule Frappe's own delete check applies (``check_if_doc_is_linked`` skips cancelled
linking documents) -- so once the conversions that used it are cancelled, a re-run removes
it. A Document Naming Rule the naming shard created for the type
(``shard_stock_entry_naming_by_type``) is left alone -- its record is part of that patch's
drift checks, and with the type gone its condition matches nothing.

Each outcome is printed, so migrate shows why the type stayed. Re-run it by hand after
cancelling, since the Patch Log already holds it::

    bench --site <site> execute jewellery_erpnext.patches.remove_unused_chain_sc_stock_entry_type.execute

Idempotent: a missing type is a no-op.
"""

import frappe

STOCK_ENTRY_TYPE = "Material Transfer (Chain SC)"


def execute():
	if not frappe.db.exists("Stock Entry Type", STOCK_ENTRY_TYPE):
		return

	live = frappe.db.count(
		"Stock Entry", {"stock_entry_type": STOCK_ENTRY_TYPE, "docstatus": ["<", 2]}
	)
	if live:
		_report(
			f"{STOCK_ENTRY_TYPE!r} kept: {live} draft or submitted Stock Entr"
			f"{'y uses' if live == 1 else 'ies use'} it. Cancel them, then re-run this patch."
		)
		return

	try:
		frappe.delete_doc("Stock Entry Type", STOCK_ENTRY_TYPE, ignore_permissions=True)
	except frappe.LinkExistsError:
		# Another record links it (a settings field, say): keep it rather than fail migrate.
		_report(f"{STOCK_ENTRY_TYPE!r} kept: another record links it.")
		return
	_report(f"deleted the unused Stock Entry Type {STOCK_ENTRY_TYPE!r}.")


def _report(message):
	message = f"remove_unused_chain_sc_stock_entry_type: {message}"
	print(message)
	frappe.logger().info(message)
