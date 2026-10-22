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
	except Exception:
		# Importing ERPNext can fail for other reasons outside a bench (e.g. frappe's logger
		# opening ../logs from the working directory); the app import must not fail with it.
		import logging

		logging.getLogger(__name__).warning(
			"Item-wise tax rounding patch not applied", exc_info=True
		)


_apply_erpnext_patches()
