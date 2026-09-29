# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CustomerGoldItem(Document):
	"""RETIRED -- no DocType links to this table any more, and nothing reads it.

	It was the row type of ``Subcontracting Settings.customer_gold_items``, a second list of
	items a Customer Gold receipt would accept. That list was removed on 2026-09-28: the Item's
	own ``custom_inventory_type_can_be_customer_goods`` flag is now the only eligibility source
	(``customer_subcontracting/customer_goods_eligibility.py``).

	The controller is kept for one release only so that a site running this code before its
	migrate can still load a cached Settings document that carries these rows (the pickled
	child needs this module, and until ``sync_all`` runs the field is still in the meta).
	Delete this folder once every site, production included, has migrated past that change;
	``remove_orphan_doctypes`` then drops the metadata. The ``tabCustomer Gold Item`` rows are
	historical and are left in place -- see ``patches/audit_customer_gold_items_removal.py``.
	"""
