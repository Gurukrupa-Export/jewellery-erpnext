import frappe
from frappe.utils import flt


def set_tracking_bom_rate_in_quotation(self):
	"""
	Fetch Tracking BOM Rates and replace the quotation item rate with Tracking BOM rate.
	Works like set_bom_rate_in_quotation but reads from Tracking Bom instead of BOM.
	"""
	for row in self.items:
		if row.get("custom_tracking_bom"):
			field_list = [
				"gold_rate_with_gst",
				"gold_bom_amount",
				"making_charge",
				"finding_bom_amount",
				"diamond_bom_amount",
				"gemstone_bom_amount",
				"other_bom_amount",
				"total_bom_amount",
				"hallmarking_amount",
			]
			tb_data = frappe.db.get_value(
				"Tracking Bom", row.custom_tracking_bom, field_list, as_dict=1
			)
			if not tb_data:
				continue

			if self.gold_rate_with_gst > 0 and tb_data.gold_rate_with_gst > 0:
				row.metal_amount = (
					tb_data.gold_bom_amount / tb_data.gold_rate_with_gst
				) * self.gold_rate_with_gst
			else:
				row.metal_amount = tb_data.gold_bom_amount

			row.making_amount = tb_data.making_charge
			row.finding_amount = tb_data.finding_bom_amount
			row.diamond_amount = tb_data.diamond_bom_amount
			row.gemstone_amount = tb_data.gemstone_bom_amount
			row.custom_hallmarking_amount = tb_data.hallmarking_amount

			row.rate = flt(
				row.metal_amount
				+ row.making_amount
				+ row.finding_amount
				+ row.diamond_amount
				+ row.gemstone_amount
				+ (row.custom_hallmarking_amount or 0),
				3,
			)
