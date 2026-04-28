import frappe


def create_test_data():
	def create_attribute_value():
		def create(data):
			if not frappe.db.exists(data["doctype"], data["attribute_value"]):
				frappe.get_doc(data).insert(ignore_permissions=True)

		create(
			{
				"doctype": "Attribute Value",
				"attribute_value": "Open",
				"is_setting_type": 1,
			}
		)

		create(
			{
				"doctype": "Attribute Value",
				"attribute_value": "Close-Open Setting",
				"is_sub_setting_type": 1,
				"parent_attribute_value": "Open",
			}
		)

		create(
			{
				"doctype": "Attribute Value",
				"attribute_value": "Close",
				"is_setting_type": 1,
			}
		)

		create(
			{
				"doctype": "Attribute Value",
				"attribute_value": "Close Setting",
				"is_sub_setting_type": 1,
				"parent_attribute_value": "Close",
			}
		)

	def create_item_attribute():
		def create(data):
			if not frappe.db.exists(data["doctype"], data["attribute_name"]):
				frappe.get_doc(data).insert(ignore_permissions=True)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Item Category",
				"item_attribute_values": [{"attribute_value": "Mugappu", "abbr": "M"}],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Item Subcategory",
				"item_attribute_values": [
					{"attribute_value": "Casual Mugappu", "abbr": "CM"}
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Metal Type",
				"item_attribute_values": [
					{"attribute_value": "Gold", "abbr": "G"},
					{"attribute_value": "Platinum", "abbr": "P"},
					{"attribute_value": "Silver", "abbr": "S"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Metal Colour",
				"item_attribute_values": [
					{"attribute_value": "White", "abbr": "W"},
					{"attribute_value": "Pink", "abbr": "P"},
					{"attribute_value": "Yellow", "abbr": "Y"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Metal Touch",
				"item_attribute_values": [
					{"attribute_value": "22KT", "abbr": "22KT"},
					{"attribute_value": "18KT", "abbr": "18KT"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Sizer Type",
				"item_attribute_values": [
					{"attribute_value": "Scale", "abbr": "S"},
					{"attribute_value": "Rod", "abbr": "R"},
					{"attribute_value": "None", "abbr": "None"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Gemstone Type",
				"item_attribute_values": [
					{"attribute_value": "Rose Quartz", "abbr": "Rose Quartz"},
					{"attribute_value": "Ruby", "abbr": "RB"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Stone Changeable",
				"item_attribute_values": [
					{"attribute_value": "Yes", "abbr": "Yes"},
					{"attribute_value": "No", "abbr": "No"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Gemstone Size",
				"item_attribute_values": [
					{"attribute_value": "2.70*1.80 MM", "abbr": "2.70*1.80 MM"}
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Two in One",
				"item_attribute_values": [
					{"attribute_value": "Yes", "abbr": "Yes"},
					{"attribute_value": "No", "abbr": "No"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "2 in 1",
				"item_attribute_values": [
					{"attribute_value": "Yes", "abbr": "Yes"},
					{"attribute_value": "No", "abbr": "No"},
				],
			}
		)
		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Age Group",
				"item_attribute_values": [
					{"attribute_value": "25-44", "abbr": "25-44"},
					{"attribute_value": "44 & above", "abbr": "44 & above"},
				],
			}
		)
		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Gender",
				"item_attribute_values": [
					{"attribute_value": "Men", "abbr": "Men"},
					{"attribute_value": "Women", "abbr": "Women"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Occasion",
				"item_attribute_values": [
					{"attribute_value": "Diwali", "abbr": "DI"},
					{"attribute_value": "Wedding", "abbr": "WE"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Rhodium",
				"item_attribute_values": [
					{"attribute_value": "Black", "abbr": "Black"},
					{"attribute_value": "None", "abbr": "None"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Setting Type",
				"item_attribute_values": [
					{"attribute_value": "Open", "abbr": "OP"},
					{"attribute_value": "Close", "abbr": "CL"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Sub Setting Type1",
				"item_attribute_values": [
					{"attribute_value": "Close Setting", "abbr": "CLS"},
					{"attribute_value": "Close-Open Setting", "abbr": "CES"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Sub Setting Type2",
				"item_attribute_values": [
					{"attribute_value": "Close Setting", "abbr": "CLS"},
					{"attribute_value": "Close-Open Setting", "abbr": "CES"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Design Type",
				"item_attribute_values": [
					{"attribute_value": "New Design", "abbr": "New Design"},
					{"attribute_value": "Sketch Design", "abbr": "Sketch Design"},
					{
						"attribute_value": "Mod - Old Stylebio & Tag No",
						"abbr": "Mod - Old Stylebio & Tag No",
					},
					{"attribute_value": "As Per Serial No", "abbr": "As Per Serial No"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Diamond Type",
				"item_attribute_values": [{"attribute_value": "Natural", "abbr": "NT"}],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Detachable",
				"item_attribute_values": [{"attribute_value": "No", "abbr": "No"}],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Feature",
				"item_attribute_values": [
					{"attribute_value": "Lever Back", "abbr": "Lever Back"}
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Chain Type",
				"item_attribute_values": [
					{"attribute_value": "Hollow Pipes", "abbr": "HWP"}
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Chain From",
				"item_attribute_values": [
					{"attribute_value": "Customer", "abbr": "CU"}
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Cap/Ganthan",
				"item_attribute_values": [
					{"attribute_value": "Metal Cap", "abbr": "Metal Cap"},
					{"attribute_value": "Diamond Cap", "abbr": "Diamond Cap"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Enamal",
				"item_attribute_values": [
					{"attribute_value": "Golden", "abbr": "Golden"},
					{"attribute_value": "No", "abbr": "No"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Gemstone Quality",
				"item_attribute_values": [
					{"attribute_value": "Synthetic", "abbr": "SYN"}
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Diamond Target",
				"numeric_values": 1,
				"from_range": 0,
				"to_range": 1000,
				"increment": 0.001,
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Metal Target",
				"numeric_values": 1,
				"from_range": 0,
				"to_range": 1000,
				"increment": 0.001,
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Product Size",
				"numeric_values": 1,
				"from_range": 0,
				"to_range": 1000,
				"increment": 0.001,
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Distance Between Kadi To Mugappu",
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Space between Mugappu",
				"numeric_values": 1,
				"from_range": 0,
				"to_range": 1000,
				"increment": 0.001,
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Back Side Size",
				"numeric_values": 1,
				"from_range": 0,
				"to_range": 1000,
				"increment": 0.01,
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Number of Ant",
				"numeric_values": 1,
				"from_range": 0,
				"to_range": 1000,
				"increment": 1,
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Chain Length",
				"numeric_values": 1,
				"from_range": 0,
				"to_range": 1000,
				"increment": 0.001,
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Count of Spiral Turns",
				"numeric_values": 1,
				"from_range": 0,
				"to_range": 1000,
				"increment": 0.001,
			}
		)

		create({"doctype": "Item Attribute", "attribute_name": "Gemstone Type1"})

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Chain Thickness",
				"numeric_values": 1,
				"from_range": 0,
				"to_range": 1000,
				"increment": 0.001,
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Chain Weight",
				"numeric_values": 1,
				"from_range": 0,
				"to_range": 1000,
				"increment": 0.001,
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Metal Purity",
				"item_attribute_values": [
					{"attribute_value": "91.9", "abbr": "91.9"},
					{"attribute_value": "91.6", "abbr": "91.6"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Lock Type",
				"item_attribute_values": [{"attribute_value": "No", "abbr": "No"}],
			}
		)

		create({"doctype": "Item Attribute", "attribute_name": "Qty"})

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Chain",
				"item_attribute_values": [
					{"attribute_value": "Yes", "abbr": "Y"},
					{"attribute_value": "No", "abbr": "N"},
				],
			}
		)

	def create_users_data():
		if not frappe.db.exists("Gender", "Other"):
			frappe.get_doc({"doctype": "Gender", "gender": "Other"}).insert(
				ignore_permissions=True
			)

		if not frappe.db.exists("Salutation", "Mx"):
			frappe.get_doc({"doctype": "Salutation", "salutation": "Mx"}).insert(
				ignore_permissions=True
			)

		if not frappe.db.exists("Designation", "Software Tester L1"):
			frappe.get_doc(
				{"doctype": "Designation", "designation_name": "Software Tester L1"}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Warehouse Type", "Transit"):
			frappe.get_doc(
				{"doctype": "Warehouse Type", "__newname": "Transit"}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Company", "Test_Company"):
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": "Test_Company",
					"country": "India",
					"default_currency": "INR",
					"chart_of_accounts": "Standard",
					"enable_perpetual_inventory": 0,
					"gstin": "24AAQCA8719H1ZC",
					"gst_category": "Registered Regular",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Customer", "Test_Customer_External"):
			customer = frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": "Test_Customer_External",
					"customer_type": "Individual",
					"custom_sketch_workflow_state": "External",
				}
			)
			customer.append(
				"diamond_grades",
				{
					"diamond_quality": "EF-VVS",
					"diamond_grade_1": "6B",
					"diamond_grade_2": "4",
				},
			)
			customer.insert(ignore_permissions=True)

		if not frappe.db.exists("Customer", "Test_Customer_Internal"):
			customer = frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": "Test_Customer_Internal",
					"customer_type": "Individual",
					"custom_sketch_workflow_state": "Internal",
				}
			)
			customer.append(
				"diamond_grades",
				{
					"diamond_quality": "EF-VVS",
					"diamond_grade_1": "6B",
					"diamond_grade_2": "4",
				},
			)
			customer.insert(ignore_permissions=True)

		if not frappe.db.exists("Supplier", "Test_Supplier"):
			frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": "Test_Supplier"}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Department", {"department_name": "Test_Department"}):
			frappe.get_doc(
				{
					"doctype": "Department",
					"department_name": "Test_Department",
					"company": "Test_Company",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Branch", {"branch_name": "Test Branch"}):
			frappe.get_doc(
				{
					"doctype": "Branch",
					"branch": "Test Branch",
					"branch_name": "Test Branch",
					"company": "Test_Company",
					"custom_is_central_branch": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Sales Person", "Test_Sales_Person"):
			frappe.get_doc(
				{"doctype": "Sales Person", "sales_person_name": "Test_Sales_Person"}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Employment Type", "Off-Role"):
			frappe.get_doc(
				{"doctype": "Employment Type", "employee_type_name": "Off-Role"}
			).insert(ignore_permissions=True)
		if not frappe.db.exists(
			"Employee", {"employee_name": "Test Designer Employee"}
		):
			frappe.get_doc(
				{
					"doctype": "Employee",
					"first_name": "Test",
					"middle_name": "Designer",
					"last_name": "Employee",
					"company": "Test_Company",
					"gender": "Other",
					"date_of_birth": "2000-01-01",
					"salutation": "Mx",
					"date_of_joining": "2024-04-01",
					"old_employee_code": "GF02867",
					"old_punch_id": "2867",
					"designation": "Software Tester L1",
					"branch": frappe.get_value(
						"Branch", {"branch_name": "Test Branch"}, "name"
					),
					"department": frappe.get_value(
						"Department", {"department_name": "Test_Department"}, "name"
					),
					"final_confirmation_date": "2024-04-01",
					"custom_notice_dayes": "30",
					"cell_number": "9876543210",
					"personal_email": "test@gmail.com",
					"current_address": "Coimbatore",
					"permanent_address": "Coimbatore",
					"attendance_device_id": "2867",
				}
			).insert(ignore_permissions=True, ignore_mandatory=True)

		if not frappe.db.exists("Address", {"name": "Test_Company-Billing"}):
			address = frappe.new_doc("Address")
			address.address_title = "Test_Company"
			address.address_line1 = "Test_Address"
			address.city = "Test_City"
			address.state = "Gujarat"
			address.country = "India"
			address.pincode = "380015"
			address.gst_category = "Registered Regular"
			address.gstin = "24AAKCG8950G1ZD"
			address.is_your_company_address = 1
			address.append(
				"links", {"link_doctype": "Company", "link_name": "Test_Company"}
			)
			address.append(
				"links",
				{
					"link_doctype": "Customer",
					"link_name": "Test_Customer_External",
				},
			)
			address.insert(ignore_permissions=True)

		if not frappe.db.exists("Item Group", "Test_Item_Group"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "Test_Item_Group",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item Group", "All Item Groups"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "All Item Groups",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item Group", "Expenses"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "Expenses",
					"parent_item_group": "All Item Groups",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item Group", "Utility Expense"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "Utility Expense",
					"parent_item_group": "Expenses",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item Group", "Designs"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "Designs",
					"parent_item_group": "All Item Groups",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item Group", "Design Template"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "Design Template",
					"parent_item_group": "Designs",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item Group", "Design Variant"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "Design Variant",
					"parent_item_group": "Designs",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item Group", "Mugappu - T"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "Mugappu - T",
					"parent_item_group": "Design Template",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item Group", "Casual Mugappu - T"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "Casual Mugappu - T",
					"parent_item_group": "Mugappu - T",
				}
			).insert(ignore_mandatory=True)

		if not frappe.db.exists("Item Group", "Mugappu - V"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "Mugappu - V",
					"parent_item_group": "Design Template",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item Group", "Casual Mugappu - V"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "Casual Mugappu - V",
					"parent_item_group": "Mugappu - V",
				}
			).insert(ignore_mandatory=True)

		if not frappe.db.exists("UOM", "Nos"):
			frappe.get_doc(
				{"doctype": "UOM", "uom_name": "Nos", "must_be_whole_number": 1}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("UOM", "Gram"):
			frappe.get_doc(
				{
					"doctype": "UOM",
					"uom_name": "Gram",
				}
			).insert(ignore_permissions=True)

		stock = frappe.get_doc("Stock Settings")
		stock.stock_uom = "Nos"
		stock.save()

		if not frappe.db.exists("Item", "ITEM-001"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "ITEM-001",
					"item_name": "ITEM-001",
					"stock_uom": "Nos",
					"designer": "Administrator",
					"is_design_code": 0,
					"item_group": "Test_Item_Group",
					"valuation_rate": 555,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "ITEM-002"):
			itm = frappe.get_doc(
				{
					"doctype": "Item",
					"item_name": "ITEM-002",
					"item_code": "ITEM-002",
					"stock_uom": "Nos",
					"designer": "Administrator",
					"is_design_code": 0,
					"item_group": "Test_Item_Group",
					"has_variants": 1,
				}
			)
			row = [
				{"attribute": "Gemstone Type"},
				{"attribute": "Metal Colour"},
				{"attribute": "Metal Touch"},
				{"attribute": "Setting Type"},
				{"attribute": "Sizer Type"},
				{"attribute": "Stone Changeable"},
			]
			for r in row:
				itm.append("attributes", r)
			itm.insert(ignore_permissions=True)

		if not frappe.db.exists("Default Charges", "Making Charges"):
			frappe.get_doc(
				{"doctype": "Default Charges", "charge_type": "Making Charges"}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Sales Type", "Finished Goods"):
			frappe.get_doc({"doctype": "Sales Type", "type": "Finished Goods"}).insert(
				ignore_permissions=True
			)

		if not frappe.db.exists("E Invoice Item", "18KT Gold Jewellery Making Charges"):
			e_invoice = frappe.get_doc(
				{
					"doctype": "E Invoice Item",
					"item_name": "18KT Gold Jewellery Making Charges",
					"is_for_making": 1,
					"metal_type": "Gold",
					"metal_touch": "18KT",
					"uom": "Gram",
					"hsn_code": 711319,
					"charge_type": "Making Charges",
				}
			)
			e_invoice.append("sales_type", {"sales_type": "Finished Goods"})
			e_invoice.insert(ignore_permissions=True)

		if not frappe.db.exists("Payment Term", "2"):
			frappe.get_doc(
				{
					"doctype": "Payment Term",
					"payment_term_name": 2,
					"invoice_portion": 100,
					"due_date_based_on": "Day(s) after invoice date",
					"credit_days": 2,
					"discount_type": "Percentage",
				}
			).insert(ignore_permissions=True)

		settings = frappe.get_single("Jewellery Settings")
		settings.gold_gst_rate = "3"
		settings.default_item = "ITEM-001"
		settings.save()

		itm_varient_setting = frappe.get_single("Item Variant Settings")
		itm_varient_setting.allow_rename_attribute_value = 1
		itm_varient_setting.save()

		if not frappe.db.exists("Currency", "INR"):
			frappe.get_doc(
				{
					"doctype": "Currency",
					"currency_name": "INR",
					"fraction": "Paisa",
					"fraction_units": 100,
					"symbol": "₹",
					"number_format": "#,##,###.##",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Price List", "Standard Selling"):
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": "Standard Selling",
					"currency": "INR",
					"selling": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Price List", "Standard Buying"):
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": "Standard Buying",
					"currency": "INR",
					"buying": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Warehouse", "All Warehouse - T"):
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": "All Warehouse",
					"company": "Test_Company",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Warehouse", "Test_Warehouse - T"):
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": "Test_Warehouse",
					"parent_warehouse": "All Warehouse - T",
					"account": "Stock in Hand - T",
					"company": "Test_Company",
					"branch": frappe.get_value(
						"Branch", {"branch_name": "Test Branch"}, "name"
					),
				}
			).insert(ignore_permissions=True)

	create_attribute_value()
	create_users_data()
	create_item_attribute()
