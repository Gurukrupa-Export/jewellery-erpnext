"""Add to Transit helpers shared by the Stock Entry and Material Request customizations.

Kept free of any other import on purpose: the Material Request side must not pull in the
Stock Entry doc_events, which read user defaults at import time.
"""

import frappe


def has_non_transit_target(stock_entry):
	"""Whether any row of ``stock_entry`` landed outside a Transit warehouse."""
	return bool(
		frappe.db.sql(
			"""
			SELECT 1 FROM `tabStock Entry Detail` sed
			LEFT JOIN `tabWarehouse` w ON w.name = sed.t_warehouse
			WHERE sed.parent = %s AND IFNULL(w.warehouse_type, '') != 'Transit'
			LIMIT 1
			""",
			(stock_entry,),
		)
	)
