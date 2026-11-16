"""Indexes for the two slow lookups in an Item insert on GK.

1. ``Item (item_category_code, sequence)``. Server Scripts ``GK Item Naming`` (Item Before
   Insert) and ``GK Item Naming API`` (called by the Item form when Item Category changes) find
   the next design sequence with ``MAX(CAST(sequence AS INTEGER)) ... WHERE item_category_code
   = %s``. With no index, each call scanned all of tabItem: about 0.78 s on 100k Items, 0.08 s
   with this index. It has to cover ``sequence``: the biggest category holds about 30% of the
   Items, and on an index of ``item_category_code`` alone MariaDB still picks the full scan.

2. ``Item Variant Attribute (attribute, attribute_value, parent)``. ERPNext's duplicate-variant
   check (``find_variant`` -> ``get_item_codes_by_attributes``) runs one query per attribute,
   ``WHERE attribute = %s AND attribute_value = %s`` plus an EXISTS on the template. Only
   ``attribute`` was indexed, so each query walked every row of that attribute (up to 100k of
   1.2M). Creating one variant of template F spent about 3 s there; with this index, 0.8 s.

Both are composite, which Frappe's schema sync leaves alone (it only drops single-column indexes
on fields without ``search_index``).
"""

from jewellery_erpnext.patches.add_stock_entry_idempotency_indexes import (
	_add_index_if_missing,
)


def execute():
	_add_index_if_missing(
		"tabItem", "item_category_code_sequence_idx", ("item_category_code", "sequence")
	)
	_add_index_if_missing(
		"tabItem Variant Attribute",
		"iva_attr_value_parent_idx",
		("attribute", "attribute_value", "parent"),
	)
