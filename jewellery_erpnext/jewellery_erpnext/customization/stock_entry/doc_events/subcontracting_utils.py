import frappe


def create_subcontracting_doc(
	self, subcontractor, department, repack_raws, main_slip=None, receive=False
):
	if not frappe.db.exists("Subcontracting", {"parent_stock_entry": self.name, "docstatus": 0}):
		sub_doc = frappe.new_doc("Subcontracting")
		sub_doc.supplier = subcontractor
		sub_doc.department = department
		sub_doc.date = frappe.utils.today()
		sub_doc.main_slip = main_slip
		sub_doc.company = self.company
		sub_doc.work_order = self.manufacturing_work_order
		sub_doc.manufacturing_order = self.manufacturing_order
		sub_doc.operation = self.manufacturing_operation

		# sub_doc.finish_item = frappe.db.get_value(
		# 	"Manufacturing Setting", self.company, "pure_gold_item"
		# )
		sub_doc.finish_item = frappe.db.get_value(
			"Manufacturing Setting", {"manufacturer":self.manufacturer}, "pure_gold_item"
		)

		if self.manufacturing_operation:
			metal_data = frappe.db.get_value(
				"Manufacturing Operation",
				self.manufacturing_operation,
				["metal_type", "metal_touch", "metal_purity", "metal_colour"],
				as_dict=True,
			)
		elif main_slip:
			metal_data = frappe.db.get_value(
				"Main Slip",
				main_slip,
				["metal_type", "metal_touch", "metal_purity", "metal_colour"],
				as_dict=True,
			)

		sub_doc.metal_type = metal_data.metal_type
		sub_doc.metal_touch = metal_data.metal_touch
		sub_doc.metal_purity = metal_data.metal_purity
		sub_doc.metal_colour = metal_data.metal_colour

		for row in repack_raws:
			if row["item_code"] == sub_doc.finish_item:
				continue
			temp_warehouse = row["s_warehouse"]
			row["s_warehouse"] = row["t_warehouse"] if receive else None
			row["t_warehouse"] = temp_warehouse if not receive else None
			sub_doc.append("source_table", row)

		sub_doc.transaction_type = "Receive" if receive else "Issue"
		sub_doc.parent_stock_entry = self.name
		if not sub_doc.source_table:
			return
		sub_doc.save()
	else:
		sub_doc = frappe.get_doc("Subcontracting", {"parent_stock_entry": self.name, "docstatus": 0})
	if receive:
		frappe.enqueue(
			udpate_stock_entry,
			queue="short",
			event="Update Stock Entry",
			enqueue_after_commit=True,
			docname=self.name,
			subcontracting=sub_doc.name,
		)
	else:
		sub_doc.submit()


def udpate_stock_entry(docname, subcontracting):
	doc = frappe.get_doc("Stock Entry", docname)
	doc.subcontracting = subcontracting
	doc.save()
