import frappe


def update_parent_details(self):
	chain = resolve_parent_chains([self.sales_order_item]).get(self.sales_order_item)
	if not chain:
		return

	_apply_parent_chain(self, chain)


def _fetch_map(doctype, names, fields):
	names = {n for n in names if n}
	if not names:
		return {}

	return {
		d.name: d
		for d in frappe.get_all(
			doctype, filters={"name": ["in", list(names)]}, fields=fields
		)
	}


def resolve_parent_chains(sales_order_items):
	"""Walk every line's parent chain at once and pick each one's Ref Customer.

	THE single implementation of that walk. It used to exist twice -- once here for the PMO and
	once inside Manufacturing Plan's grade pre-resolution -- and the copies disagreed about which
	line to climb from, so a plan could grade a row against one customer and the PMO it created
	against another.

	The climb is not obvious: a line's ``custom_po_details`` leads to the Purchase Order Item
	raised by the PREVIOUS manufacturing plan, whose ``custom_m_plan_details`` points at that
	plan's row and so at ITS sales order line. The quotation that records the customer behind an
	internal order is the parent line's, not this line's -- this line's own quotation is only the
	last resort. Resolving from the current line alone, as the Manufacturing Plan copy did,
	silently used the bottom rung as if it were the top.

	Returns {sales_order_item: chain}, where a chain carries the links the walk reached plus the
	resolved ``ref_customer``. Every stage is one query for the whole batch, so a plan with a
	thousand rows costs the same seven queries as a single PMO save.
	"""
	# This line: the Purchase Order link to climb, and its own quotation (the last rung, which
	# the PMO also holds as its fetch_from `quotation` field).
	lines = _fetch_map(
		"Sales Order Item",
		sales_order_items,
		["name", "custom_po_details", "prevdoc_docname"],
	)

	# The Purchase Order behind the line, and the manufacturing plan row behind that.
	po_items = _fetch_map(
		"Purchase Order Item",
		[d.custom_po_details for d in lines.values()],
		["name", "parent", "custom_m_plan_details"],
	)

	# The parent plan row: its plan, its sales order, and the line it was raised for.
	mp_rows = _fetch_map(
		"Manufacturing Plan Table",
		[d.custom_m_plan_details for d in po_items.values()],
		["name", "parent", "sales_order", "docname"],
	)

	# The parent line's quotation.
	parent_lines = _fetch_map(
		"Sales Order Item",
		[d.docname for d in mp_rows.values()],
		["name", "prevdoc_docname"],
	)

	chains = {}
	for name in lines:
		chains[name] = _build_chain(name, lines, po_items, mp_rows, parent_lines)

	quotation_refs = _fetch_map(
		"Quotation",
		[c.quotation for c in chains.values()]
		+ [c.own_quotation for c in chains.values()],
		["name", "ref_customer"],
	)
	sales_order_customers = _fetch_map(
		"Sales Order", [c.sales_order for c in chains.values()], ["name", "customer"]
	)
	purchase_order_refs = _fetch_map(
		"Purchase Order",
		[c.purchase_order for c in chains.values()],
		["name", "ref_customer"],
	)

	for chain in chains.values():
		chain.ref_customer = _pick_ref_customer(
			chain, quotation_refs, sales_order_customers, purchase_order_refs
		)

	return chains


def _build_chain(name, lines, po_items, mp_rows, parent_lines):
	"""The links one line's walk reached, blank on every link it did not.

	``reached_*`` records that a stage was walked, separately from what it held: the PMO writes
	parent_sales_order even when the plan row carried none, and collapsing the two would turn a
	blank into "leave whatever an earlier save stored".
	"""
	line = lines.get(name) or frappe._dict()
	chain = frappe._dict(
		purchase_order=None,
		quotation=None,
		sales_order=None,
		mp=None,
		own_quotation=line.get("prevdoc_docname"),
		reached_mp_row=False,
		reached_parent_line=False,
		ref_customer=None,
	)

	po_item = po_items.get(line.get("custom_po_details")) or frappe._dict()
	chain.purchase_order = po_item.get("parent")

	mp_row = mp_rows.get(po_item.get("custom_m_plan_details"))
	if not mp_row:
		return chain

	chain.reached_mp_row = True
	chain.sales_order = mp_row.get("sales_order")
	chain.mp = mp_row.get("parent")

	if mp_row.get("docname"):
		chain.reached_parent_line = True
		parent_line = parent_lines.get(mp_row["docname"]) or frappe._dict()
		chain.quotation = parent_line.get("prevdoc_docname")

	return chain


def _pick_ref_customer(
	chain, quotation_refs, sales_order_customers, purchase_order_refs
):
	"""Take Ref Customer from the nearest source the walk reached.

	Ref Customer belongs to the quotation the parent sales order line was raised against -- that
	is where the real customer behind an internal order is recorded. The rungs below it are
	progressively coarser: a Purchase Order or a Quotation can cover rows for more than one
	customer, and carries only one value. So the per-line sources always win, and the coarse ones
	only speak where the walk found nothing at all.
	"""
	if chain.quotation:
		ref = (quotation_refs.get(chain.quotation) or frappe._dict()).get(
			"ref_customer"
		)
		if ref:
			return ref

	if chain.sales_order:
		ref = (sales_order_customers.get(chain.sales_order) or frappe._dict()).get(
			"customer"
		)
		if ref:
			return ref

	# The walk reached the Purchase Order but not the manufacturing plan row behind it -- Purchase
	# Order Items raised before custom_m_plan_details existed have no link to follow.
	if chain.purchase_order:
		ref = (purchase_order_refs.get(chain.purchase_order) or frappe._dict()).get(
			"ref_customer"
		)
		if ref:
			return ref

	# Last resort: this line's own quotation. Reached when the line was never tied back to a
	# Purchase Order Item, so the walk stopped at its first guard.
	if chain.own_quotation:
		ref = (quotation_refs.get(chain.own_quotation) or frappe._dict()).get(
			"ref_customer"
		)
		if ref:
			return ref

	return None


def _apply_parent_chain(self, chain):
	"""Write the walk's findings onto the document.

	Assigns only what the walk reached. Leaving a blank alone rather than writing None keeps a
	save from wiping a Ref Customer someone set by hand -- the field is not read-only -- and keeps
	the parent links a previous save stored when this walk exits early.
	"""
	if chain.reached_parent_line:
		self.parent_quotation = chain.quotation

	if chain.reached_mp_row:
		self.parent_sales_order = chain.sales_order
		self.parent_mp = chain.mp

	if chain.ref_customer:
		self.ref_customer = chain.ref_customer
