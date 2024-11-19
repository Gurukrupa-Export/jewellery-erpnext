import frappe


def get_purity_percentage(item):
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
