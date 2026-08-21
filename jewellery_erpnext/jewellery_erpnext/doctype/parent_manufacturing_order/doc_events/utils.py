import frappe


def update_parent_details(self):
	parents = _set_parent_chain(self)
	# Deliberately outside the walk: every guard in _set_parent_chain returns early, and
	# ref_customer must survive a break in any of them.
	_resolve_ref_customer(self, parents)


def _set_parent_chain(self):
	"""Fill parent_quotation / parent_sales_order / parent_mp on the document.

	Return a dict of what THIS walk established, blank on every link it did not reach:

	        purchase_order  the Purchase Order behind the sales order line
	        quotation       mirrors self.parent_quotation
	        sales_order     mirrors self.parent_sales_order

	It is not a full picture of the walk -- parent_mp is set on the document only, because no
	Ref Customer is derived from it. Anything added here should be added for a reader of the
	return value, not to make the two lists match.

	The return value exists so _resolve_ref_customer never re-reads these off the document: all
	three parent fields keep whatever an earlier save stored when the walk exits early, and none
	of them is read-only, so a stale or hand-typed link must not outrank what this walk found.
	"""
	parents = frappe._dict(purchase_order=None, quotation=None, sales_order=None)

	if not self.sales_order_item:
		return parents

	po_row = frappe.db.get_value(
		"Sales Order Item", self.sales_order_item, "custom_po_details"
	)
	if not po_row:
		return parents

	# Both fields in one read: the parent is only needed on the fallback path, but fetching it
	# here costs nothing and saves a round trip when the m-plan link turns out to be missing.
	po_item = (
		frappe.db.get_value(
			"Purchase Order Item",
			po_row,
			["parent", "custom_m_plan_details"],
			as_dict=True,
		)
		or frappe._dict()
	)
	parents.purchase_order = po_item.get("parent")

	m_plan_row = po_item.get("custom_m_plan_details")
	if not m_plan_row:
		return parents

	mfg_plan_details = frappe.db.get_value(
		"Manufacturing Plan Table",
		m_plan_row,
		["parent", "sales_order", "docname"],
		as_dict=1,
	)

	if not mfg_plan_details:
		return parents

	if mfg_plan_details.get("docname"):
		quotation = frappe.db.get_value(
			"Sales Order Item", mfg_plan_details["docname"], "prevdoc_docname"
		)
		self.parent_quotation = quotation
		parents.quotation = quotation

	self.parent_sales_order = mfg_plan_details.get("sales_order")
	self.parent_mp = mfg_plan_details.get("parent")
	parents.sales_order = self.parent_sales_order

	return parents


def _resolve_ref_customer(self, parents):
	"""Take Ref Customer from the nearest source this walk reached.

	Ref Customer belongs to the quotation the parent sales order line was raised against -- that is
	where the real customer behind an internal order is recorded. The rungs below it are
	progressively coarser: a Purchase Order or a Quotation can cover rows for more than one
	customer, and carries only one value. So the per-line sources always win, and the coarse ones
	only speak where the walk found nothing at all.

	Reads ``parents``, never self.parent_*: those fields keep whatever an earlier save stored when
	the walk exits early, and a stale one must not outrank a link the current walk did find.

	Assigns only when a rung produces a value. Leaving a blank alone rather than writing None keeps
	a save from wiping a Ref Customer someone set by hand -- the field is not read-only.
	"""
	ref_customer = None

	if parents.quotation:
		ref_customer = frappe.db.get_value(
			"Quotation", parents.quotation, "ref_customer"
		)

	if not ref_customer and parents.sales_order:
		ref_customer = frappe.db.get_value(
			"Sales Order", parents.sales_order, "customer"
		)

	# The walk reached the Purchase Order but not the manufacturing plan row behind it -- Purchase
	# Order Items raised before custom_m_plan_details existed have no link to follow.
	if not ref_customer and parents.purchase_order:
		ref_customer = frappe.db.get_value(
			"Purchase Order", parents.purchase_order, "ref_customer"
		)

	# Last resort: this order's own quotation, which the framework fetches from
	# sales_order_item.prevdoc_docname. Reached when the sales order line was never tied back to a
	# Purchase Order Item, so the walk stopped at its first guard. Read off self on purpose -- it is
	# this document's own field, moving with sales_order_item, not a parent link the walk derives,
	# so it is not exposed to the staleness the rungs above guard against.
	if not ref_customer and self.get("quotation"):
		ref_customer = frappe.db.get_value("Quotation", self.quotation, "ref_customer")

	if ref_customer:
		self.ref_customer = ref_customer
