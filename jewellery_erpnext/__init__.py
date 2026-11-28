__version__ = "0.0.1"


def _apply_erpnext_patches():
	# Scales ERPNext's item-wise tax rounding tolerance with the item count, so large
	# documents (1,000+ items) don't fail "Item Wise Tax Details do not match".
	try:
		from jewellery_erpnext.item_wise_tax_rounding import apply

		apply()
	except ImportError:
		# frappe / ERPNext not importable (e.g. while the app is being built or installed).
		pass


_apply_erpnext_patches()
