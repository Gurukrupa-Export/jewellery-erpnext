import frappe


def set_stamping_no(self, method):
	"""Generate unique sequential stamping number based on creation year and sequence"""
	if not self.custom_stamping_no:  # Only set if not already set
		from datetime import datetime

		# Get current year and convert to letter code (A=2021, B=2022, ... F=2026, G=2027)
		current_year = datetime.now().year
		year_code = chr(65 + (current_year - 2021))  # A starts at 2021

		# Get the count of Serial No created in current year
		from frappe.utils import get_datetime

		year_start = get_datetime(f"{current_year}-01-01")

		sequence = (
			frappe.db.count(
				"Serial No",
				filters={
					"creation": [">=", year_start],
					"custom_stamping_no": ["is", "not set"],
				},
			)
			+ 1
		)

		# Format as "2" + year_code + 4-digit sequence
		self.custom_stamping_no = f"2{year_code}{sequence:04d}"


def update_table(self, method):
	# serial_numbers = frappe.get_all("Serial No",filters={"name": self.name},fields={"*"})
	existing_serial_record = frappe.get_all(
		"Serial No Table",
		filters={
			"parent": self.name,
			"purchase_document_no": self.purchase_document_no,
		},
	)
	if existing_serial_record:
		pass
		# frappe.db.set_value("Serial No Table", existing_serial_record[0].name,"serial_no",self.name)
		# frappe.db.set_value("Serial No Table", existing_serial_record[0].name,"warranty_period",self.warranty_period)

	else:
		# frappe.throw(f"{existing_serial_record}")
		# if self.get("purchase_document_no"):
		# serial_number_creator = frappe.db.get_value(
		# 	"Stock Entry",
		# 	self.get("purchase_document_no"),
		# 	"custom_serial_number_creator",
		# )
		# pmo = frappe.db.get_value(
		# 	"Serial Number Creator",
		# 	serial_number_creator,
		# 	"parent_manufacturing_order",
		# )
		# mwo = frappe.db.get_value(
		# 	"Serial Number Creator",
		# 	serial_number_creator,
		# 	"manufacturing_work_order",
		# )
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
