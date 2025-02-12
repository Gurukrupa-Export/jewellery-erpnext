import frappe
from erpnext.stock.doctype.item_price.item_price import ItemPriceDuplicateItem
from frappe import _
from frappe.query_builder.functions import IsNull


def check_duplicates(self):
	ItemPrice = frappe.qb.DocType("Item Price")
	conditions = [
		(ItemPrice.item_code == self.item_code),
		(ItemPrice.price_list == self.price_list),
		(ItemPrice.name != self.name),
	]
	for field in [
		"uom",
		"valid_from",
		"valid_upto",
		"packing_unit",
		"customer",
		"supplier",
		"batch_no",
		"bom_no",
	]:
		if self.get(field):
			conditions.append(ItemPrice[field] == self.get(field))
		else:
			conditions.append(IsNull(ItemPrice[field]) | (ItemPrice[field] == ""))

	query = frappe.qb.from_(ItemPrice).select(ItemPrice.price_list_rate)

	for condition in conditions:
		query = query.where(condition)

	price_list_rate = query.run(as_dict=self.as_dict())

	if price_list_rate:
		frappe.throw(
			_(
				"Item Price appears multiple times based on Price List, Supplier/Customer, Currency, Item, Batch, UOM, Qty, and Dates."
			),
			ItemPriceDuplicateItem,
		)
