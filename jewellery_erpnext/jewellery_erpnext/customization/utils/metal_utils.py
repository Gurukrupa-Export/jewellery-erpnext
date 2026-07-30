import frappe


@frappe.request_cache
def get_purity_percentage(item):
	"""Metal purity % for an item variant.

	Request/job-scoped cache: an item's Metal Purity attribute cannot change mid-request, and
	the Stock Entry ``before_validate`` hook calls this **once per metal/finding row**. A
	consolidated EOD transfer carries ~9,954 such rows against only ~496 distinct item codes,
	so the uncached version issued that three-way join ~20x more often than it needed to.
	``frappe.request_cache`` is cleared per request and per background job, so a purity edit
	is picked up by the next one.
	"""
	if not item:
		return

	IVA = frappe.qb.DocType("Item Variant Attribute")
	ITEM = frappe.qb.DocType("Item")
	AV = frappe.qb.DocType("Attribute Value")

	purity_percentage = (
		frappe.qb.from_(IVA)
		.join(ITEM)
		.on(ITEM.name == IVA.parent)
		.join(AV)
		.on(IVA.attribute_value == AV.name)
		.select(AV.purity_percentage)
		.where((IVA.attribute == "Metal Purity") & (ITEM.name == item))
	).run()

	if not purity_percentage:
		return

	return purity_percentage[0][0]
