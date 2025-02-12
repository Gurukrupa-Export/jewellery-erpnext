import frappe


def update_table(self, method):
	# serial_numbers = frappe.get_all("Serial No",filters={"name": self.name},fields={"*"})
	existing_serial_record = frappe.get_all(
		"Serial No Table",
		filters={"parent": self.name, "purchase_document_no": self.purchase_document_no},
	)
	if existing_serial_record:
		pass
		# frappe.db.set_value("Serial No Table", existing_serial_record[0].name,"serial_no",self.name)
		# frappe.db.set_value("Serial No Table", existing_serial_record[0].name,"warranty_period",self.warranty_period)

	else:
		# frappe.throw(f"{existing_serial_record}")
		self.append(
			"custom_serial_no_table",
			{
				"parent": self.name,
				"parenttype": "Serial No",
				"parentfield": "custom_serial_no_table",
				"serial_no": self.get("serial_no"),
				"item_code": self.get("item_code"),
				"company": self.get("company"),
				"purchase_document_no": self.get("purchase_document_no"),
			},
		)
