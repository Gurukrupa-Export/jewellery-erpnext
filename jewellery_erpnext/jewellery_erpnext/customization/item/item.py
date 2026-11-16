import frappe
from erpnext.stock.doctype.item.item import Item, check_stock_uom_with_bin
from frappe import _


class CustomItem(Item):
	def validate_uom(self):
		"""ERPNext's check, without one query per variant on a template save.

		Core calls ``check_stock_uom_with_bin(variant, template_uom)`` for every variant, and that
		function returns at once when the variant already has the template's UOM. Saving template F
		(about 11k variants) spent most of a 60 s save there. Only the variants whose UOM differs
		can fail or need their Bin updated, so only those are checked. Items that are not
		templates go straight to core.
		"""
		if not self.has_variants:
			return super().validate_uom()

		if not self.is_new():
			check_stock_uom_with_bin(self.name, self.stock_uom)

		for name in frappe.db.sql_list(
			"""select name from `tabItem`
			where variant_of = %s and (stock_uom is null or stock_uom != %s)""",
			(self.name, self.stock_uom),
		):
			check_stock_uom_with_bin(name, self.stock_uom)

	def validate_variant_based_on_change(self):
		"""Core's check with an existence test in place of loading every variant of a template.

		Core uses ``frappe.get_all("Item", {"variant_of": self.name})`` only for its truth value;
		on template F that read about 11k rows (1.2 s) on every save.
		"""
		if not self.is_new() and (
			self.variant_of
			or (self.has_variants and frappe.db.exists("Item", {"variant_of": self.name}))
		):
			if self.variant_based_on != frappe.db.get_value("Item", self.name, "variant_based_on"):
				frappe.throw(_("Variant Based On cannot be changed"))
