import os

import frappe
from frappe.modules.import_file import import_file_by_path


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
					{"attribute_value": "24KT", "abbr": "24KT"},
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
					{"attribute_value": "75.4", "abbr": "75.4"},
					{"attribute_value": "99.9", "abbr": "99.9"},
					{"attribute_value": "92.0", "abbr": "92.0"},
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

		create(
			{"doctype": "Item Attribute", "attribute_name": "Diamond Certificate No"}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Diamond Sieve Size Range",
				"item_attribute_values": [{"attribute_value": "+0-2", "abbr": "+0-2"}],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Diamond Sieve Size",
				"item_attribute_values": [
					{"attribute_value": "+1-1.5", "abbr": "+1-1.5"},
					{"attribute_value": "+9-9.5", "abbr": "+9-9.5"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Diamond Grade",
				"item_attribute_values": [
					{"attribute_value": "4", "abbr": "4"},
					{"attribute_value": "6B", "abbr": "6B"},
					{"attribute_value": "7", "abbr": "7"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Stone Shape",
				"item_attribute_values": [{"attribute_value": "Round", "abbr": "RO"}],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Finding Category",
				"item_attribute_values": [{"attribute_value": "Chains", "abbr": "CHA"}],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Finding Sub-Category",
				"item_attribute_values": [
					{"attribute_value": "Kodi Chain", "abbr": "KC"}
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Finding Size",
				"item_attribute_values": [
					{"attribute_value": "2.50 MM", "abbr": "2.50 MM"}
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Gemstone Grade",
				"item_attribute_values": [
					{"attribute_value": "Real Treated", "abbr": "RT"}
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Gemstone PR",
				"item_attribute_values": [{"attribute_value": "10", "abbr": "10"}],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Cut or Cab",
				"item_attribute_values": [
					{"attribute_value": "Faceted", "abbr": "FC"},
					{"attribute_value": "Cabochon", "abbr": "CC"},
				],
			}
		)

		create(
			{
				"doctype": "Item Attribute",
				"attribute_name": "Per Pc or Per Carat",
				"item_attribute_values": [
					{"attribute_value": "Per Pc", "abbr": "PP"},
					{"attribute_value": "Per Carat", "abbr": "PC"},
				],
			}
		)

	def create_users_data():
		frappe.db.set_value(
			"Company",
			"Test_Company",
			"default_operating_cost_account",
			"Stock Expenses - T",
		)
		frappe.db.set_value(
			"Attribute Value", "Gold", "custom_batch_abbreviation", "GL"
		)
		frappe.db.set_value(
			"Attribute Value", "24KT", "custom_batch_abbreviation", "24"
		)
		frappe.db.set_value(
			"Attribute Value", "22KT", "custom_batch_abbreviation", "22"
		)
		frappe.db.set_value(
			"Attribute Value", "18KT", "custom_batch_abbreviation", "18"
		)
		frappe.db.set_value(
			"Attribute Value", "99.9", "custom_batch_abbreviation", "1000"
		)
		frappe.db.set_value(
			"Attribute Value", "91.9", "custom_batch_abbreviation", "919"
		)
		frappe.db.set_value(
			"Attribute Value", "91.6", "custom_batch_abbreviation", "916"
		)
		frappe.db.set_value(
			"Attribute Value", "Yellow", "custom_batch_abbreviation", "Y0"
		)
		frappe.db.set_value(
			"Attribute Value", "Natural", "custom_batch_abbreviation", "NT"
		)
		frappe.db.set_value(
			"Attribute Value", "Round", "custom_batch_abbreviation", "RO"
		)
		frappe.db.set_value("Attribute Value", "6B", "custom_batch_abbreviation", "X6")
		frappe.db.set_value(
			"Attribute Value", "+9-9.5", "custom_batch_abbreviation", "100105"
		)
		frappe.db.set_value(
			"Attribute Value", "Chains", "custom_batch_abbreviation", "CHA"
		)
		frappe.db.set_value(
			"Attribute Value", "Kodi Chain", "custom_batch_abbreviation", "KC"
		)
		frappe.db.set_value(
			"Attribute Value", "2.50 MM", "custom_batch_abbreviation", "B50MM0"
		)
		frappe.db.set_value(
			"Attribute Value", "Pink", "custom_batch_abbreviation", "P0"
		)
		frappe.db.set_value(
			"Attribute Value", "92.0", "custom_batch_abbreviation", "920"
		)
		frappe.db.set_value(
			"Attribute Value", "75.4", "custom_batch_abbreviation", "754"
		)

		if not frappe.db.exists("Item Group", "Test_Item_Group"):
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "Test_Item_Group",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)

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
					"valuation_rate": 555,
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

			bom = frappe.new_doc("BOM")
			bom.item = "ITEM-002"
			bom.company = "Test_Company"
			bom.append("items", {"item_code": "ITEM-002", "qty": 1, "rate": 555})
			bom.save()

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
					"metal_purity": "18KT",
					"uom": "Gram",
					"hsn_code": 711319,
					"charge_type": "Making Charges",
				}
			)
			e_invoice.append("sales_type", {"sales_type": "Finished Goods"})
			e_invoice.insert(ignore_permissions=True)

		if not frappe.db.exists("E Invoice Item", "18KT Gold Chain Making Charges"):
			e_invoice = frappe.get_doc(
				{
					"doctype": "E Invoice Item",
					"item_name": "18KT Gold Chain Making Charges",
					"is_for_finding": 1,
					"metal_type": "Gold",
					"metal_purity": "18KT",
					"uom": "Gram",
					"hsn_code": 711319,
					"finding_category": "Chains",
				}
			)
			e_invoice.append("sales_type", {"sales_type": "Finished Goods"})
			e_invoice.insert(ignore_permissions=True)

		if not frappe.db.exists(
			"E Invoice Item", "Studded 18KT Gold Chain Making Charges"
		):
			e_invoice = frappe.get_doc(
				{
					"doctype": "E Invoice Item",
					"item_name": "Studded 18KT Gold Chain Making Charges",
					"is_for_finding": 1,
					"metal_type": "Gold",
					"metal_purity": "18KT",
					"uom": "Gram",
					"hsn_code": 711319,
					"finding_category": "Chains",
				}
			)
			e_invoice.append("sales_type", {"sales_type": "Finished Goods"})
			e_invoice.insert(ignore_permissions=True)

		if not frappe.db.exists("E Invoice Item", "Studded 18KT Gold Chain Jewellery"):
			e_invoice = frappe.get_doc(
				{
					"doctype": "E Invoice Item",
					"item_name": "Studded 18KT Gold Chain Jewellery",
					"is_for_finding": 1,
					"metal_type": "Gold",
					"metal_purity": "18KT",
					"uom": "Gram",
					"hsn_code": 711319,
					"finding_category": "Chains",
				}
			)
			e_invoice.append("sales_type", {"sales_type": "Finished Goods"})
			e_invoice.insert(ignore_permissions=True)

		if not frappe.db.exists("E Invoice Item", "Studded 18KT Gold Jewellery"):
			e_invoice = frappe.get_doc(
				{
					"doctype": "E Invoice Item",
					"item_name": "Studded 18KT Gold Jewellery",
					"is_for_metal": 1,
					"metal_type": "Gold",
					"metal_purity": "18KT",
					"uom": "Gram",
					"hsn_code": 711319,
				}
			)
			e_invoice.append("sales_type", {"sales_type": "Finished Goods"})
			e_invoice.insert(ignore_permissions=True)

		if not frappe.db.exists("E Invoice Item", "Studded Natural Diamond Jewellery"):
			e_invoice = frappe.get_doc(
				{
					"doctype": "E Invoice Item",
					"item_name": "Studded Natural Diamond Jewellery",
					"is_for_diamond": 1,
					"diamond_type": "Natural",
					"uom": "Carat",
					"hsn_code": 711319,
				}
			)
			e_invoice.append("sales_type", {"sales_type": "Finished Goods"})
			e_invoice.insert(ignore_permissions=True)

		if not frappe.db.exists("E Invoice Item", "Studded Gemstone Jewellery"):
			e_invoice = frappe.get_doc(
				{
					"doctype": "E Invoice Item",
					"item_name": "Studded Gemstone Jewellery",
					"is_for_gemstone": 1,
					"uom": "Carat",
					"hsn_code": 711319,
				}
			)
			e_invoice.append("sales_type", {"sales_type": "Finished Goods"})
			e_invoice.insert(ignore_permissions=True)

		if not frappe.db.exists("E Invoice Item", "Studded 22KT Gold Jewellery"):
			e_invoice = frappe.get_doc(
				{
					"doctype": "E Invoice Item",
					"item_name": "Studded 22KT Gold Jewellery",
					"is_for_metal": 1,
					"metal_type": "Gold",
					"metal_purity": "22KT",
					"uom": "Gram",
					"hsn_code": 711319,
				}
			)
			e_invoice.append("sales_type", {"sales_type": "Finished Goods"})
			e_invoice.insert(ignore_permissions=True)

		if not frappe.db.exists("E Invoice Item", "22KT Gold Jewellery Making Charges"):
			e_invoice = frappe.get_doc(
				{
					"doctype": "E Invoice Item",
					"item_name": "22KT Gold Jewellery Making Charges",
					"is_for_making": 1,
					"metal_type": "Gold",
					"metal_purity": "22KT",
					"uom": "Gram",
					"hsn_code": 711319,
				}
			)
			e_invoice.append("sales_type", {"sales_type": "Finished Goods"})
			e_invoice.insert(ignore_permissions=True)

		if not frappe.db.exists("E Invoice Item", "22KT Gold Chain Making Charges"):
			e_invoice = frappe.get_doc(
				{
					"doctype": "E Invoice Item",
					"item_name": "22KT Gold Chain Making Charges",
					"is_for_finding_making": 1,
					"metal_type": "Gold",
					"metal_purity": "22KT",
					"uom": "Gram",
					"hsn_code": 711319,
					"finding_category": "Chains",
				}
			)
			e_invoice.append("sales_type", {"sales_type": "Finished Goods"})
			e_invoice.insert(ignore_permissions=True)

		if not frappe.db.exists("E Invoice Item", "Studded 22KT Gold Chain Jewellery"):
			e_invoice = frappe.get_doc(
				{
					"doctype": "E Invoice Item",
					"item_name": "Studded 22KT Gold Chain Jewellery",
					"is_for_finding": 1,
					"metal_type": "Gold",
					"metal_purity": "22KT",
					"uom": "Gram",
					"hsn_code": 711319,
					"finding_category": "Chains",
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

		if not frappe.db.exists("Payment Term", "15"):
			frappe.get_doc(
				{
					"doctype": "Payment Term",
					"payment_term_name": 15,
					"invoice_portion": 100,
					"due_date_based_on": "Day(s) after invoice date",
					"credit_days": 15,
					"discount_type": "Percentage",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Payment Term", "20"):
			frappe.get_doc(
				{
					"doctype": "Payment Term",
					"payment_term_name": 20,
					"invoice_portion": 100,
					"due_date_based_on": "Day(s) after invoice date",
					"credit_days": 20,
					"discount_type": "Percentage",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Payment Term", "30"):
			frappe.get_doc(
				{
					"doctype": "Payment Term",
					"payment_term_name": 30,
					"invoice_portion": 100,
					"due_date_based_on": "Day(s) after invoice date",
					"credit_days": 30,
					"discount_type": "Percentage",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Payment Term", "45"):
			frappe.get_doc(
				{
					"doctype": "Payment Term",
					"payment_term_name": 45,
					"invoice_portion": 100,
					"due_date_based_on": "Day(s) after invoice date",
					"credit_days": 45,
					"discount_type": "Percentage",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Customer Payment Terms", "Test_Customer_External"):
			customer_payment_term = frappe.new_doc("Customer Payment Terms")
			customer_payment_term.customer = "Test_Customer_External"
			payment_term = [
				{
					"item_type": "18KT Gold Jewellery Making Charges",
					"payment_term": "2",
				},
				{
					"item_type": "18KT Gold Chain Making Charges",
					"payment_term": "2",
				},
				{
					"item_type": "Studded 18KT Gold Chain Jewellery",
					"payment_term": "20",
				},
				{
					"item_type": "Studded 18KT Gold Jewellery",
					"payment_term": "15",
				},
				{
					"item_type": "Studded Natural Diamond Jewellery",
					"payment_term": "30",
				},
				{
					"item_type": "Studded Gemstone Jewellery",
					"payment_term": "45",
				},
				{
					"item_type": "Studded 22KT Gold Jewellery",
					"payment_term": "15",
				},
				{
					"item_type": "22KT Gold Jewellery Making Charges",
					"payment_term": "2",
				},
				{
					"item_type": "22KT Gold Chain Making Charges",
					"payment_term": "2",
				},
				{
					"item_type": "Studded 22KT Gold Chain Jewellery",
					"payment_term": "15",
				},
			]
			for row in payment_term:
				customer_payment_term.append("customer_payment_details", row)
			customer_payment_term.save()

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
					"subcontractor": "Test_Supplier",
					"branch": frappe.get_value(
						"Branch", {"branch_name": "Test Branch"}, "name"
					),
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "MUTEST"):
			item = frappe.new_doc("Item")

			item.update(
				{
					"custom_reason_for_design_code_": "New Design",
					"gst_hsn_code": "71131120",
					"item_group": "Casual Mugappu - T",
					"item_code": "MUTEST",
					"stock_uom": "Nos",
					"is_stock_item": 0,
					"has_variants": 1,
					"item_category": "Mugappu",
					"item_subcategory": "Casual Mugappu",
					"item_category_code": "MU",
					"setting_type": "Close",
					"sequence": "01087",
					"productivity": "Studded",
					"designer": frappe.db.exists(
						"Employee", {"employee_name": "Test Designer Employee"}
					),
					"variant_based_on": "Item Attribute",
					"subcategory": "Casual Mugappu",
					"is_purchase_item": 1,
					"is_sales_item": 1,
					"valuation_rate": 0.01,
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"sketch_image": "https://www.chidambaramcovering.in/image/cache/catalog/Mogappu%20Chain/mchn510-gold-plated-jewellery-mugappu-design-without-stone-5-425x500.jpg.webp",
				}
			)

			item.append("uoms", {"uom": "Nos", "conversion_factor": 1})

			attributes = [
				"Diamond Target",
				"Metal Colour",
				"Product Size",
				"Enamal",
				"Rhodium",
				"Two in One",
				"Detachable",
				"Stone Changeable",
				"Feature",
				"Chain Type",
				"Distance Between Kadi To Mugappu",
				"Space between Mugappu",
				"Back Side Size",
				"Number of Ant",
				"Chain Length",
				"Gemstone Type",
				"Cap/Ganthan",
			]

			for attr in attributes:
				item.append("attributes", {"attribute": attr})

			item.insert(ignore_mandatory=True, ignore_permissions=True)

		if not frappe.db.exists("Item", "M"):
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"has_variants": 1,
					"item_code": "M",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "M",
					"gst_hsn_code": "010121",
					"item_group": "Metal - T",
					"stock_uom": "Gram",
					"variant_based_on": "Item Attribute",
				}
			)

			item.append("attributes", {"attribute": "Metal Type"})

			item.append("attributes", {"attribute": "Metal Touch"})

			item.append("attributes", {"attribute": "Metal Purity"})

			item.append("attributes", {"attribute": "Metal Colour"})

			item.insert(ignore_mandatory=True, ignore_permissions=True)

		if not frappe.db.exists(
			"Making Charge Price",
			{
				"customer": "Test_Customer_External",
				"metal_touch": "22KT",
				"setting_type": "Close",
				"metal_type": "Gold",
			},
		):
			making_charge_price = frappe.get_doc(
				{
					"doctype": "Making Charge Price",
					"customer": "Test_Customer_External",
					"setting_type": "Close",
					"currency": "INR",
					"metal_touch": "22KT",
					"metal_type": "Gold",
					"from_gold_rate": 5000,
					"to_gold_rate": 20000,
				}
			)
			making_charge_price.append(
				"subcategory",
				{"subcategory": "Casual Mugappu", "rate_per_gram": 500, "wastage": 5},
			)
			making_charge_price.insert(ignore_permissions=True)

		if not frappe.db.exists("Purchase Type", "FG Purchase"):
			frappe.get_doc({"doctype": "Purchase Type", "type": "FG Purchase"}).insert(
				ignore_permissions=True
			)

		if not frappe.db.exists("Warehouse Type", "Scrap"):
			frappe.get_doc({"doctype": "Warehouse Type", "__newname": "Scrap"}).insert(
				ignore_permissions=True
			)

		if not frappe.db.exists("Warehouse Type", "Manufacturing"):
			frappe.get_doc(
				{"doctype": "Warehouse Type", "__newname": "Manufacturing"}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "D"):
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "D",
					"item_name": "D",
					"item_group": "Diamond - T",
					"stock_uom": "Carat",
					"has_variants": 1,
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"is_sales_item": 1,
					"country_of_origin": "India",
					"description": "D",
					"end_of_life": "2099-12-31",
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"uoms": [
						{
							"doctype": "UOM Conversion Detail",
							"uom": "Carat",
							"conversion_factor": 1,
						}
					],
					"attributes": [
						{
							"doctype": "Item Variant Attribute",
							"attribute": "Diamond Type",
						},
						{
							"doctype": "Item Variant Attribute",
							"attribute": "Stone Shape",
						},
						{
							"doctype": "Item Variant Attribute",
							"attribute": "Diamond Grade",
						},
						{
							"doctype": "Item Variant Attribute",
							"attribute": "Diamond Sieve Size",
						},
						{
							"doctype": "Item Variant Attribute",
							"attribute": "Diamond Sieve Size Range",
						},
						{
							"doctype": "Item Variant Attribute",
							"attribute": "Diamond Certificate No",
						},
					],
					"item_defaults": [
						{
							"doctype": "Item Default",
							"company": "Test_Company",
							"default_warehouse": "Product Allocation FG - T",
						}
					],
				}
			)

			item.insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "F"):
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "F",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "F",
					"gst_hsn_code": "010121",
					"item_group": "Finding - T",
					"stock_uom": "Gram",
					"has_variants": 1,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "F",
					"end_of_life": "2099-12-31",
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"country_of_origin": "India",
					"is_sales_item": 1,
					"uoms": [
						{
							"doctype": "UOM Conversion Detail",
							"uom": "Gram",
							"conversion_factor": 1,
						}
					],
					"attributes": [
						{
							"doctype": "Item Variant Attribute",
							"attribute": "Metal Type",
						},
						{
							"doctype": "Item Variant Attribute",
							"attribute": "Metal Touch",
						},
						{
							"doctype": "Item Variant Attribute",
							"attribute": "Metal Purity",
						},
						{
							"doctype": "Item Variant Attribute",
							"attribute": "Metal Colour",
						},
						{
							"doctype": "Item Variant Attribute",
							"attribute": "Finding Category",
						},
						{
							"doctype": "Item Variant Attribute",
							"attribute": "Finding Sub-Category",
						},
						{
							"doctype": "Item Variant Attribute",
							"attribute": "Finding Size",
						},
					],
					"item_defaults": [
						{
							"doctype": "Item Default",
							"company": "Test_Company",
							"default_warehouse": "Product Allocation FG - T",
						}
					],
				}
			)

			item.insert(ignore_permissions=True)

		if not frappe.db.exists(
			"Department Operation", "Manufacturing Plan & Management"
		):
			frappe.get_doc(
				{
					"doctype": "Department Operation",
					"operation": "Manufacturing Plan & Management",
					"company": "Test_company",
					"department": "Manufacturing Plan & Management - T",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Department Operation", "Wax Pull Out"):
			frappe.get_doc(
				{
					"doctype": "Department Operation",
					"operation": "Wax Pull Out",
					"company": "Test_Company",
					"department": "Waxing - T",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item Tax Template", "GST 3% - T"):
			frappe.get_doc(
				{
					"doctype": "Item Tax Template",
					"title": "GST 3%",
					"company": "Test_Company",
					"gst_treatment": "Taxable",
					"gst_rate": 3,
					"taxes": [
						{"tax_type": "Output Tax CGST - T", "tax_rate": 1.5},
						{"tax_type": "Output Tax SGST - T", "tax_rate": 1.5},
						{"tax_type": "Output Tax IGST - T", "tax_rate": 3},
						{"tax_type": "Input Tax CGST - T", "tax_rate": 1.5},
						{"tax_type": "Input Tax SGST - T", "tax_rate": 1.5},
						{"tax_type": "Input Tax IGST - T", "tax_rate": 3},
						{"tax_type": "Input Tax CGST RCM - T", "tax_rate": 1.5},
						{"tax_type": "Input Tax SGST RCM - T", "tax_rate": 1.5},
						{"tax_type": "Input Tax IGST RCM - T", "tax_rate": 3},
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item Tax Template", "GST 5% - T"):
			frappe.get_doc(
				{
					"doctype": "Item Tax Template",
					"title": "GST 5%",
					"company": "Test_Company",
					"gst_treatment": "Taxable",
					"gst_rate": 5,
					"disabled": 0,
					"taxes": [
						{"tax_type": "Output Tax SGST - T", "tax_rate": 2.5},
						{"tax_type": "Output Tax CGST - T", "tax_rate": 2.5},
						{"tax_type": "Output Tax IGST - T", "tax_rate": 5},
						{"tax_type": "Input Tax SGST - T", "tax_rate": 2.5},
						{"tax_type": "Input Tax CGST - T", "tax_rate": 2.5},
						{"tax_type": "Input Tax IGST - T", "tax_rate": 5},
						{"tax_type": "Output Tax SGST RCM - T", "tax_rate": 2.5},
						{"tax_type": "Output Tax CGST RCM - T", "tax_rate": 2.5},
						{"tax_type": "Output Tax IGST RCM - T", "tax_rate": 5},
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item Tax Template", "GST 1.5% - T"):
			frappe.get_doc(
				{
					"doctype": "Item Tax Template",
					"title": "GST 1.5%",
					"company": "Test_Company",
					"gst_treatment": "Taxable",
					"gst_rate": 1.5,
					"disabled": 0,
					"taxes": [
						{"tax_type": "Output Tax CGST - T", "tax_rate": 0.75},
						{"tax_type": "Output Tax SGST - T", "tax_rate": 0.75},
						{"tax_type": "Output Tax IGST - T", "tax_rate": 1.5},
						{"tax_type": "Input Tax CGST - T", "tax_rate": 0.75},
						{"tax_type": "Input Tax SGST - T", "tax_rate": 0.75},
						{"tax_type": "Input Tax IGST - T", "tax_rate": 1.5},
						{"tax_type": "Output Tax CGST RCM - T", "tax_rate": 0.75},
						{"tax_type": "Output Tax SGST RCM - T", "tax_rate": 0.75},
						{"tax_type": "Output Tax IGST RCM - T", "tax_rate": 1.5},
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Manufacturer", "Shubh"):
			frappe.get_doc(
				{
					"doctype": "Manufacturer",
					"short_name": "Shubh",
					"custom_abbreviation": "SH",
					"country": "India",
					"custom_central_department": "Central - T",
					"company": "Test_Company",
					"custom_repair_warehouse": "MPM WO - T",
					"custom_variant_loss_details": [
						{
							"variant": "M",
							"consider_department_warehouse": 1,
							"warehouse_type": "Scrap",
						},
						{
							"variant": "D",
							"consider_department_warehouse": 1,
							"warehouse_type": "Scrap",
						},
						{
							"variant": "F",
							"consider_department_warehouse": 1,
							"warehouse_type": "Scrap",
						},
					],
					"metal_criteria": [
						{
							"metal_type": "Gold",
							"metal_touch": "22KT",
							"metal_purity": "91.6",
						}
					],
					"custom_reservation_table": [
						{
							"variant": "M",
							"department": "Central - T",
							"target_warehouse": "Waxing RSV - T",
						},
						{
							"variant": "D",
							"department": "Diamond Bagging - T",
							"target_warehouse": "Diamond Setting RSV - T",
						},
						{
							"variant": "F",
							"department": "Central - T",
							"target_warehouse": "Central RSV - T",
						},
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Department Operation", "Serial No Generation"):
			frappe.get_doc(
				{
					"doctype": "Department Operation",
					"operation": "Serial No Generation",
					"company": "Test_Company",
					"department": "Tagging - T",
					"manufacturer": "Shubh",
					"is_last_operation": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Inventory Type", "Customer Goods"):
			frappe.get_doc(
				{"doctype": "Inventory Type", "inventory_type": "Customer Goods"}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Inventory Type", "Regular Stock"):
			frappe.get_doc(
				{"doctype": "Inventory Type", "inventory_type": "Regular Stock"}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "M-G-22KT-91.6-Y"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "M-G-22KT-91.6-Y",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "M-G-22KT-91.6-Y",
					"gst_hsn_code": "010121",
					"item_group": "Metal DNU",
					"stock_uom": "Gram",
					"is_stock_item": 1,
					"has_variants": 0,
					"include_item_in_manufacturing": 1,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "<b><u>M</u></b><br>Metal Type : Gold<br>Metal Touch : 22KT<br>Metal Purity : 91.6<br>Metal Colour : Yellow<br>",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"has_batch_no": 1,
					"create_new_batch": 1,
					"variant_of": "M",
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"country_of_origin": "India",
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [{"uom": "Gram", "conversion_factor": 1}],
					"attributes": [
						{
							"variant_of": "M",
							"attribute": "Metal Type",
							"attribute_value": "Gold",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Touch",
							"attribute_value": "22KT",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Purity",
							"attribute_value": "91.6",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Colour",
							"attribute_value": "Yellow",
						},
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "M-G-22KT-91.6-P"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "M-G-22KT-91.6-P",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "M-G-22KT-91.6-P",
					"gst_hsn_code": "010121",
					"item_group": "Metal DNU",
					"stock_uom": "Gram",
					"is_stock_item": 1,
					"has_variants": 0,
					"include_item_in_manufacturing": 1,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "<b><u>M</u></b><br>Metal Type : Gold<br>Metal Touch : 22KT<br>Metal Purity : 91.6<br>Metal Colour : Pink<br>",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"has_batch_no": 1,
					"create_new_batch": 1,
					"variant_of": "M",
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"country_of_origin": "India",
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [{"uom": "Gram", "conversion_factor": 1}],
					"attributes": [
						{
							"variant_of": "M",
							"attribute": "Metal Type",
							"attribute_value": "Gold",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Touch",
							"attribute_value": "22KT",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Purity",
							"attribute_value": "91.6",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Colour",
							"attribute_value": "Pink",
						},
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "M-G-24KT-99.9-Y"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "M-G-24KT-99.9-Y",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "M-G-24KT-99.9-Y",
					"gst_hsn_code": "010121",
					"item_group": "Metal - V",
					"stock_uom": "Gram",
					"is_stock_item": 1,
					"has_variants": 0,
					"include_item_in_manufacturing": 1,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "<b><u>M</u></b><br>Metal Type : Gold<br>Metal Touch : 24KT<br>Metal Purity : 99.9<br>Metal Colour : Yellow<br>",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"has_batch_no": 1,
					"create_new_batch": 1,
					"variant_of": "M",
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"country_of_origin": "India",
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [{"uom": "Gram", "conversion_factor": 1}],
					"attributes": [
						{
							"variant_of": "M",
							"attribute": "Metal Type",
							"attribute_value": "Gold",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Touch",
							"attribute_value": "24KT",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Purity",
							"attribute_value": "99.9",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Colour",
							"attribute_value": "Yellow",
						},
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "D-NT-RO-6B-+9-9.5"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "D-NT-RO-6B-+9-9.5",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "D-NT-RO-6B-+9-9.5",
					"gst_hsn_code": "71131120",
					"item_group": "Diamond - V",
					"stock_uom": "Carat",
					"is_stock_item": 1,
					"has_variants": 0,
					"include_item_in_manufacturing": 1,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "<b><u>D</u></b><br>Diamond Type : Natural<br>Stone Shape : Round<br>Diamond Grade : 6B<br>Diamond Sieve Size : +9-9.5<br>",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"has_batch_no": 1,
					"create_new_batch": 1,
					"batch_number_series": "GE2D075-DNTROX7I00I05-.##.",
					"variant_of": "D",
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"country_of_origin": "India",
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [
						{
							"uom": "Carat",
							"conversion_factor": 1,
						}
					],
					"item_defaults": [
						{
							"company": "Test_Company",
							"default_warehouse": "RM Procurement - T",
						}
					],
					"attributes": [
						{
							"variant_of": "D",
							"attribute": "Diamond Type",
							"attribute_value": "Natural",
						},
						{
							"variant_of": "D",
							"attribute": "Stone Shape",
							"attribute_value": "Round",
						},
						{
							"variant_of": "D",
							"attribute": "Diamond Grade",
							"attribute_value": "6B",
						},
						{
							"variant_of": "D",
							"attribute": "Diamond Sieve Size",
							"attribute_value": "+9-9.5",
						},
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "Hallmarking Charges"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "Hallmarking Charges",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "Hallmarking Charges",
					"gst_hsn_code": "71131120",
					"item_group": "Services",
					"stock_uom": "Nos",
					"is_stock_item": 0,
					"has_variants": 0,
					"include_item_in_manufacturing": 0,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "Hallmarking Charges",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"has_batch_no": 0,
					"create_new_batch": 0,
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"country_of_origin": "India",
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [
						{
							"uom": "Nos",
							"conversion_factor": 1,
						}
					],
					"taxes": [{"item_tax_template": "GST 18% - T"}],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "Diamond Certification Charges"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "Diamond Certification Charges",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "Diamond Certification Charges",
					"gst_hsn_code": "71131120",
					"item_group": "Services",
					"stock_uom": "Nos",
					"is_stock_item": 0,
					"has_variants": 0,
					"include_item_in_manufacturing": 1,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "Diamond Certification Charges",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"has_batch_no": 0,
					"create_new_batch": 0,
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"country_of_origin": "India",
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [
						{
							"uom": "Nos",
							"conversion_factor": 1,
						}
					],
					"taxes": [{"item_tax_template": "GST 18% - T"}],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "G"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "G",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "G",
					"gst_hsn_code": "010121",
					"item_group": "Gemstone - T",
					"stock_uom": "Carat",
					"has_variants": 1,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "G",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"country_of_origin": "India",
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [
						{
							"uom": "Carat",
							"conversion_factor": 1,
						}
					],
					"attributes": [
						{"attribute": "Gemstone Type"},
						{"attribute": "Stone Shape"},
						{"attribute": "Gemstone Quality"},
						{"attribute": "Gemstone Grade"},
						{"attribute": "Gemstone Size"},
						{"attribute": "Cut or Cab"},
						{"attribute": "Gemstone PR"},
						{"attribute": "Per Pc or Per Carat"},
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "G-PER-DUM-PRE-CC"):
			item = frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "G-PER-DUM-PRE-CC",
					"item_name": "Default Dummy Gemstone",
					"item_group": "Gemstone DNU",
					"stock_uom": "Carat",
					"is_stock_item": 0,
					"disabled": 0,
				}
			)

			item.insert(ignore_permissions=True)

		if not frappe.db.exists("Manufacturing Setting", "Shubh"):
			frappe.get_doc(
				{
					"doctype": "Manufacturing Setting",
					"company": "Test_Company",
					"manufacturer": "Shubh",
					"series_start": "G",
					"default_operation": "Manufacturing Plan & Management",
					"default_diamond_department": "Diamond Setting - T",
					"default_gemstone_department": "Diamond Setting - T",
					"default_finding_department": "Model Making - T",
					"default_other_material_department": "Final Polish - T",
					"default_department": "Manufacturing Plan & Management - T",
					"default_fg_department": "Serial Number - T",
					"default_fg_warehouse": "Tagging FG - T",
					"pure_gold_item": "M-G-24KT-99.9-Y",
					"subcontracting_repack_item": "M-G-24KT-99.9-Y",
					"default_gemstone_item": "G-PER-DUM-PRE-CC",
					"addition_maximum_item__tolerance_percentage": 50000,
					"powder_value": 1000,
					"power_value_individual": 1000,
					"water_value": 390,
					"water_value_individual": 0.36,
					"inventory_type": "Customer Goods",
					"boric_value": 10,
					"special_powder_boric_value": 5,
					"wo_split_limit": 2,
					"refining_warehouse": "Refining WO - T",
					"cad_to_rpt": 13.62,
					"rpt_to_wax": 1.29,
					"wax_to_gold_10": 12.73,
					"wax_to_gold_14": 14.23,
					"wax_to_gold_18": 16,
					"wax_to_gold_22": 18,
					"wax_to_gold_24": 0,
					"wax_to_silver": 10,
					"check_purity": "Both",
					"check_colour": "Both",
					"check_touch": "Both",
					"item_variant_groups": [
						{"item_variant": "M", "item_group": "Metal DNU"},
						{"item_variant": "D", "item_group": "Diamond DNU"},
						{"item_variant": "F", "item_group": "Finding DNU"},
					],
					"product_certification_details": [
						{
							"certification_type": "Hall Marking Service",
							"purchase_item": "Hallmarking Charges",
						},
						{
							"certification_type": "Diamond Certificate service",
							"purchase_item": "Diamond Certification Charges",
						},
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Transfer Type", "Transfer To Branch"):
			frappe.get_doc(
				{
					"doctype": "Transfer Type",
					"transfer_type": "Transfer To Branch",
					"stock_entry_type": "Material Transfer to Branch",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Transfer Type", "Transfer To Department"):
			frappe.get_doc(
				{
					"doctype": "Transfer Type",
					"transfer_type": "Transfer To Department",
					"stock_entry_type": "Material Transfer (DEPARTMENT)",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Transfer Type", "Transfer to Reserve"):
			frappe.get_doc(
				{
					"doctype": "Transfer Type",
					"transfer_type": "Transfer to Reserve",
					"stock_entry_type": "Material transfer to Reserve",
				}
			).insert(ignore_permissions=True)

		stock = frappe.get_single("Stock Settings")
		stock.stock_uom = "Nos"
		stock.enable_serial_and_batch_no_for_item = 1
		stock.do_not_update_serial_batch_on_creation_of_auto_bundle = 0
		stock.over_delivery_receipt_allowance = 5
		stock.mr_qty_allowance = 10
		stock.enable_stock_reservation = 1
		stock.auto_indent = 1
		stock.reorder_email_notify = 1
		# stock.allow_negative_stock = 1
		# stock.allow_negative_stock_for_batch = 1
		stock.save()

		itm_varient_setting = frappe.get_single("Item Variant Settings")
		itm_varient_setting.allow_rename_attribute_value = 1
		itm_varient_setting.save()

		order_criteria = frappe.get_single("Order Criteria")
		if not order_criteria.order:
			order_criteria.append(
				"order",
				{
					"sketch_submission_time": "6:00:00",
					"sketch_submission": 2,
					"cad_submission": 6,
					"skecth_approval_timefrom_ibm_team": 4,
					"cad_appoval_timefrom_ibm_team": "6:00:00",
					"cad_approval_day": 6,
					"cad_submission_time": "10:45:00",
				},
			)
		if not order_criteria.department_shift:
			order_criteria.append(
				"department_shift",
				{
					"branch": frappe.db.exists(
						"Branch", {"branch_name": "Test Branch"}
					),
					"department": frappe.db.exists(
						"Department", {"Department_name": "Test_Department"}
					),
					"shift_start_time": "8:15:00",
					"shift_end_time": "6:15:00",
				},
			)
		order_criteria.save()

		settings = frappe.get_single("Jewellery Settings")
		settings.gold_gst_rate = "3"
		settings.default_item = "ITEM-001"
		settings.save()

		if (
			frappe.db.get_value(
				"Bin",
				{"item_code": "M-G-22KT-91.6-Y", "warehouse": "Central RM - T"},
				"actual_qty",
			)
			or 0
		) < 1:
			stock_entry = frappe.get_doc(
				{
					"doctype": "Stock Entry",
					"company": "Test_Company",
					"stock_entry_type": "Material Receipt",
					"branch": frappe.db.exists(
						"Branch", {"branch_name": "Test Branch"}
					),
					"to_warehouse": "Central RM - T",
					"manufacturer": "Shubh",
				}
			)
			stock_entry.append(
				"items",
				{"item_code": "M-G-22KT-91.6-Y", "qty": 25, "basic_rate": 5626},
			)
			stock_entry.insert(ignore_permissions=True)
			stock_entry.submit()

		if (
			frappe.db.get_value(
				"Bin",
				{
					"item_code": "D-NT-RO-6B-+9-9.5",
					"warehouse": "Diamond Bagging RM - T",
				},
				"actual_qty",
			)
			or 0
		) < 1:
			stock_entry = frappe.get_doc(
				{
					"doctype": "Stock Entry",
					"company": "Test_Company",
					"stock_entry_type": "Material Receipt",
					"branch": frappe.db.exists(
						"Branch", {"branch_name": "Test Branch"}
					),
					"to_warehouse": "Diamond Bagging RM - T",
					"manufacturer": "Shubh",
				}
			)
			stock_entry.append(
				"items",
				{"item_code": "D-NT-RO-6B-+9-9.5", "qty": 25, "basic_rate": 5626.24},
			)
			stock_entry.insert(ignore_permissions=True)
			stock_entry.submit()

		if not frappe.db.exists("Employee", {"employee_name": "Test Waxing Employee"}):
			frappe.get_doc(
				{
					"doctype": "Employee",
					"first_name": "Test",
					"middle_name": "Waxing",
					"last_name": "Employee",
					"company": "Test_Company",
					"gender": "Other",
					"date_of_birth": "2000-01-01",
					"salutation": "Mx",
					"date_of_joining": "2024-04-01",
					"old_employee_code": "GF02868",
					"old_punch_id": "2868",
					"designation": "Software Tester L1",
					"branch": frappe.get_value(
						"Branch", {"branch_name": "Test Branch"}, "name"
					),
					"department": "Waxing - T",
					"final_confirmation_date": "2024-04-01",
					"custom_notice_dayes": "30",
					"cell_number": "9876543211",
					"personal_email": "test1@gmail.com",
					"current_address": "Coimbatore",
					"permanent_address": "Coimbatore",
					"attendance_device_id": "2868",
				}
			).insert(ignore_permissions=True, ignore_mandatory=True)

		if not frappe.db.exists("Warehouse", "Test Supplier WIP WH - T"):
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": "Test Supplier WIP WH",
					"subcontractor": "Test_Supplier",
					"is_group": 0,
					"parent_warehouse": "Sub Contracting WS - T",
					"account": "Stock in Hand - T",
					"company": "Test_Company",
					"warehouse_type": "Manufacturing",
					"default_in_transit_warehouse": "Sub Contracting Transit - T",
					"custom_branch": frappe.get_value(
						"Branch", {"branch_name": "Test Branch"}, "name"
					),
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Supplier Group", "Services"):
			frappe.get_doc(
				{
					"doctype": "Supplier Group",
					"supplier_group_name": "Services",
					"is_group": 1,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Supplier Group", "Subcontracting"):
			frappe.get_doc(
				{
					"doctype": "Supplier Group",
					"supplier_group_name": "Subcontracting",
					"parent_supplier_group": "Services",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "FG Subcontracting"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "FG Subcontracting",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "FG Subcontracting",
					"gst_hsn_code": "71131120",
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 0,
					"has_variants": 0,
					"include_item_in_manufacturing": 0,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "FG Subcontracting",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"has_batch_no": 0,
					"create_new_batch": 0,
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"country_of_origin": "India",
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [
						{
							"uom": "Nos",
							"conversion_factor": 1,
						}
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists(
			"Department Operation",
			"Wax Setting/Filling/Diamond Setting/Final Polish without Rhodium/Plating SC",
		):
			frappe.get_doc(
				{
					"doctype": "Department Operation",
					"operation": "Wax Setting/Filling/Diamond Setting/Final Polish without Rhodium/Plating SC",
					"department": "Sub Contracting - T",
					"company": "Test_Company",
					"is_subcontracted": 1,
					"allow_zero_qty_wo": 1,
					"is_main_slip_required": 1,
					"supplier_group": "Subcontracting",
					"service_item": "FG Subcontracting",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "F-G-22KT-91.9-Y-CHA-KC-2.50 MM"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "F-G-22KT-91.9-Y-CHA-KC-2.50 MM",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "F-G-22KT-91.9-Y-CHA-KC-2.50 MM",
					"gst_hsn_code": "71131120",
					"item_group": "Finding - V",
					"stock_uom": "Gram",
					"is_stock_item": 1,
					"has_variants": 0,
					"include_item_in_manufacturing": 1,
					"custom_is_manufacturing_item": 1,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "<b><u>F</u></b><br>Metal Type : Gold<br>Metal Touch : 22KT<br>Metal Purity : 91.9<br>Metal Colour : Yellow<br>Finding Category : Chains<br>Finding Sub-Category : Kodi Chain<br>Finding Size : 2.50 MM<br>",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"has_batch_no": 1,
					"create_new_batch": 1,
					"batch_number_series": "GE2D081-FGL22919Y0KCB50MM0-.##.",
					"variant_of": "F",
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"country_of_origin": "India",
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [
						{
							"uom": "Gram",
							"conversion_factor": 1,
						}
					],
					"item_defaults": [
						{
							"company": "Test_Company",
							"default_warehouse": "RM Procurement - T",
						},
					],
					"attributes": [
						{
							"variant_of": "F",
							"attribute": "Metal Type",
							"attribute_value": "Gold",
						},
						{
							"variant_of": "F",
							"attribute": "Metal Touch",
							"attribute_value": "22KT",
						},
						{
							"variant_of": "F",
							"attribute": "Metal Purity",
							"attribute_value": "91.9",
						},
						{
							"variant_of": "F",
							"attribute": "Metal Colour",
							"attribute_value": "Yellow",
						},
						{
							"variant_of": "F",
							"attribute": "Finding Category",
							"attribute_value": "Chains",
						},
						{
							"variant_of": "F",
							"attribute": "Finding Sub-Category",
							"attribute_value": "Kodi Chain",
						},
						{
							"variant_of": "F",
							"attribute": "Finding Size",
							"attribute_value": "2.50 MM",
						},
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "F-G-22KT-91.9-P-CHA-KC-2.50 MM"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "F-G-22KT-91.9-P-CHA-KC-2.50 MM",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "F-G-22KT-91.9-P-CHA-KC-2.50 MM",
					"gst_hsn_code": "71131120",
					"item_group": "Finding - V",
					"stock_uom": "Gram",
					"is_stock_item": 1,
					"has_variants": 0,
					"include_item_in_manufacturing": 1,
					"custom_is_manufacturing_item": 1,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "<b><u>F</u></b><br>Metal Type : Gold<br>Metal Touch : 22KT<br>Metal Purity : 91.9<br>Metal Colour : Pink<br>Finding Category : Chains<br>Finding Sub-Category : Kodi Chain<br>Finding Size : 2.50 MM<br>",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"has_batch_no": 1,
					"create_new_batch": 1,
					"batch_number_series": "GE2D081-FGL22919Y0KCB50MM0-.##.",
					"variant_of": "F",
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"country_of_origin": "India",
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [
						{
							"uom": "Gram",
							"conversion_factor": 1,
						}
					],
					"item_defaults": [
						{
							"company": "Test_Company",
							"default_warehouse": "RM Procurement - T",
						},
					],
					"attributes": [
						{
							"variant_of": "F",
							"attribute": "Metal Type",
							"attribute_value": "Gold",
						},
						{
							"variant_of": "F",
							"attribute": "Metal Touch",
							"attribute_value": "22KT",
						},
						{
							"variant_of": "F",
							"attribute": "Metal Purity",
							"attribute_value": "91.9",
						},
						{
							"variant_of": "F",
							"attribute": "Metal Colour",
							"attribute_value": "Pink",
						},
						{
							"variant_of": "F",
							"attribute": "Finding Category",
							"attribute_value": "Chains",
						},
						{
							"variant_of": "F",
							"attribute": "Finding Sub-Category",
							"attribute_value": "Kodi Chain",
						},
						{
							"variant_of": "F",
							"attribute": "Finding Size",
							"attribute_value": "2.50 MM",
						},
					],
				}
			).insert(ignore_permissions=True)

		# The repair "Unpack Raw Material" flow submits SEs of this type; ensure it
		# exists before the reservation seed links to it (not shipped in fixtures).
		if not frappe.db.exists("Stock Entry Type", "Repair Unpack"):
			frappe.get_doc(
				{
					"doctype": "Stock Entry Type",
					"name": "Repair Unpack",
					"purpose": "Repack",
				}
			).insert(ignore_permissions=True)

		mop_settings = frappe.get_single("MOP Settings")
		if not mop_settings.stock_entry_type_to_reservation:
			mop_settings.append(
				"stock_entry_type_to_reservation",
				{
					"stock_entry_type_to_reservation": "Material Transfer (WORK ORDER)",
					"is_increase_weight": 1,
				},
			)
			mop_settings.append(
				"stock_entry_type_to_reservation",
				{
					"stock_entry_type_to_reservation": "Repack",
					"is_increase_weight": 1,
					"is_decrease_weight": 1,
				},
			)
			mop_settings.append(
				"stock_entry_type_to_reservation",
				{
					"stock_entry_type_to_reservation": "Material Receive (WORK ORDER)",
					"is_decrease_weight": 1,
				},
			)
			# Repair "Unpack Raw Material" books BOM raw materials into the work order
			# as an inward move, so it reserves + logs like a WORK ORDER transfer.
			mop_settings.append(
				"stock_entry_type_to_reservation",
				{
					"stock_entry_type_to_reservation": "Repair Unpack",
					"is_increase_weight": 1,
				},
			)
			mop_settings.save()

		frappe.db.set_single_value("System Settings", "float_precision", "3")

		if not frappe.db.exists("Item", "ML"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "ML",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "ML",
					"gst_hsn_code": "010121",
					"item_group": "Metal - T",
					"stock_uom": "Gram",
					"is_stock_item": 0,
					"has_variants": 1,
					"include_item_in_manufacturing": 0,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "M",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"has_batch_no": 0,
					"create_new_batch": 0,
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"country_of_origin": "India",
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [
						{
							"uom": "Gram",
							"conversion_factor": 1,
						}
					],
					"attributes": [
						{
							"attribute": "Metal Type",
						},
						{
							"attribute": "Metal Touch",
						},
						{
							"attribute": "Metal Purity",
						},
						{
							"attribute": "Metal Colour",
						},
					],
					"taxes": [],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "M-G-22KT-91.9-Y"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "M-G-22KT-91.9-Y",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "M-G-22KT-91.9-Y",
					"gst_hsn_code": "010121",
					"item_group": "Metal - V",
					"stock_uom": "Gram",
					"is_stock_item": 1,
					"has_variants": 0,
					"include_item_in_manufacturing": 1,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "<b><u>M</u></b><br>Metal Type : Gold<br>Metal Touch : 22KT<br>Metal Purity : 91.9<br>Metal Colour : Yellow<br>",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"valuation_rate": 0.01,
					"has_batch_no": 1,
					"create_new_batch": 1,
					"batch_number_series": "GE2D081-MGL22919Y0-.##.",
					"variant_of": "M",
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"country_of_origin": "India",
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [
						{
							"uom": "Gram",
							"conversion_factor": 1,
						}
					],
					"attributes": [
						{
							"variant_of": "M",
							"attribute": "Metal Type",
							"attribute_value": "Gold",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Touch",
							"attribute_value": "22KT",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Purity",
							"attribute_value": "91.9",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Colour",
							"attribute_value": "Yellow",
						},
					],
					"taxes": [],
					"item_defaults": [
						{
							"company": "Test_Company",
							"income_account": "Sales - T",
						},
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "M-G-18KT-75.4-P"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "M-G-18KT-75.4-P",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "M-G-18KT-75.4-P",
					"gst_hsn_code": "010121",
					"item_group": "Metal - V",
					"stock_uom": "Gram",
					"is_stock_item": 1,
					"has_variants": 0,
					"include_item_in_manufacturing": 1,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "<b><u>M</u></b><br>Metal Type : Gold<br>Metal Touch : 18KT<br>Metal Purity : 75.4<br>Metal Colour : Pink<br>",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"has_batch_no": 1,
					"create_new_batch": 1,
					"variant_of": "M",
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"country_of_origin": "India",
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [
						{
							"uom": "Gram",
							"conversion_factor": 1,
						}
					],
					"attributes": [
						{
							"variant_of": "M",
							"attribute": "Metal Type",
							"attribute_value": "Gold",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Touch",
							"attribute_value": "18KT",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Purity",
							"attribute_value": "75.4",
						},
						{
							"variant_of": "M",
							"attribute": "Metal Colour",
							"attribute_value": "Pink",
						},
					],
					"taxes": [],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "ML-G-18KT-75.4-P"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "ML-G-18KT-75.4-P",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "ML-G-18KT-75.4-P",
					"gst_hsn_code": "010121",
					"item_group": "Metal - V",
					"stock_uom": "Gram",
					"is_stock_item": 1,
					"has_variants": 0,
					"include_item_in_manufacturing": 1,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "<b><u>ML</u></b><br>Metal Type : Gold<br>Metal Touch : 18KT<br>Metal Purity : 75.4<br>Metal Colour : Pink<br>",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"valuation_rate": 1,
					"has_batch_no": 1,
					"create_new_batch": 1,
					"batch_number_series": "GE2D075-MGL18754P0-.##.",
					"variant_of": "ML",
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [
						{
							"uom": "Gram",
							"conversion_factor": 1,
						}
					],
					"attributes": [
						{
							"variant_of": "ML",
							"attribute": "Metal Type",
							"attribute_value": "Gold",
						},
						{
							"variant_of": "ML",
							"attribute": "Metal Touch",
							"attribute_value": "18KT",
						},
						{
							"variant_of": "ML",
							"attribute": "Metal Purity",
							"attribute_value": "75.4",
						},
						{
							"variant_of": "ML",
							"attribute": "Metal Colour",
							"attribute_value": "Pink",
						},
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "ML-G-22KT-91.9-Y"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "ML-G-22KT-91.9-Y",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "ML-G-22KT-91.9-Y",
					"gst_hsn_code": "010121",
					"item_group": "Metal - V",
					"stock_uom": "Gram",
					"is_stock_item": 1,
					"include_item_in_manufacturing": 1,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "<b><u>ML</u></b><br>Metal Type : Gold<br>Metal Touch : 22KT<br>Metal Purity : 91.9<br>Metal Colour : Yellow<br>",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"valuation_rate": 1,
					"has_batch_no": 1,
					"create_new_batch": 1,
					"batch_number_series": "GE2D075-MGL22919Y0-.##.",
					"variant_of": "ML",
					"variant_based_on": "Item Attribute",
					"is_purchase_item": 1,
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [
						{
							"uom": "Gram",
							"conversion_factor": 1,
						}
					],
					"attributes": [
						{
							"variant_of": "ML",
							"attribute": "Metal Type",
							"attribute_value": "Gold",
						},
						{
							"variant_of": "ML",
							"attribute": "Metal Touch",
							"attribute_value": "22KT",
						},
						{
							"variant_of": "ML",
							"attribute": "Metal Purity",
							"attribute_value": "91.9",
						},
						{
							"variant_of": "ML",
							"attribute": "Metal Colour",
							"attribute_value": "Yellow",
						},
					],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Item", "Cap"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"is_design_code": 0,
					"item_code": "Cap",
					"custom_reason_for_design_code_": "New Design",
					"item_name": "C - Cap",
					"gst_hsn_code": "98010030",
					"item_group": "Tools & Accessories",
					"stock_uom": "Nos",
					"is_stock_item": 1,
					"has_variants": 0,
					"include_item_in_manufacturing": 1,
					"manufacturing_type": "Casted",
					"productivity": "Studded",
					"description": "C - Cap",
					"end_of_life": "2099-12-31",
					"default_material_request_type": "Purchase",
					"has_batch_no": 1,
					"create_new_batch": 1,
					"batch_number_series": "GE2D085-COTOATOA201-.##.",
					"is_purchase_item": 1,
					"last_purchase_rate": 2.5,
					"grant_commission": 1,
					"is_sales_item": 1,
					"uoms": [
						{
							"uom": "Nos",
							"conversion_factor": 1,
						}
					],
					"item_defaults": [
						{
							"company": "Test_Company",
							"default_warehouse": "CSB Procurement 1 - T",
							"expense_account": "Print and Stationery - T",
						},
					],
				}
			).insert(ignore_permissions=True)
		# Patches
		in_migrate = getattr(frappe.flags, "in_migrate", False)
		frappe.flags.in_migrate = True
		try:
			from jewellery_erpnext.patches.add_mr_transfer_se_fields import (
				execute as _ensure_mr_transfer_se_fields,
			)

			_ensure_mr_transfer_se_fields()

			from jewellery_erpnext.patches.add_order_form_detail_pre_order_field import (
				execute as _ensure_order_form_detail_pre_order_field,
			)

			_ensure_order_form_detail_pre_order_field()

			from jewellery_erpnext.patches.add_stock_entry_tree_number_field import (
				execute as _ensure_stock_entry_tree_number_field,
			)

			_ensure_stock_entry_tree_number_field()

			from jewellery_erpnext.patches.add_warehouse_msl_tracking_field import (
				execute as _ensure_warehouse_msl_tracking_field,
			)

			_ensure_warehouse_msl_tracking_field()

			from jewellery_erpnext.fetch_from_guard import ensure_fetch_from_columns

			ensure_fetch_from_columns()

			from jewellery_erpnext.property_setter_guard import (
				ensure_field_precision_property_setters,
			)

			ensure_field_precision_property_setters()

			from jewellery_erpnext.patches.ensure_float_precision_three import (
				ensure_float_precision,
			)

			ensure_float_precision()

			from jewellery_erpnext.patches.add_order_type_repair_option import (
				ensure_order_type_repair_option,
			)

			ensure_order_type_repair_option()

			from jewellery_erpnext.patches.add_customer_refining_flags import (
				execute as customer_refining_flags,
			)

			customer_refining_flags()

			from jewellery_erpnext.patches.add_po_refining_entry_field import (
				execute as po_refining_entry_field,
			)

			po_refining_entry_field()

			# Masters (the dust/scrap Items) MUST be seeded before the price list:
			# seed_refinery_price_list skips any price row whose Item does not exist yet,
			# so running it first would create no price lists at all (get_refinery_rate
			# then returns None and external-refining POs are priced at 0).
			from jewellery_erpnext.patches.seed_refining_masters import (
				execute as refining_masters,
			)

			refining_masters()

			from jewellery_erpnext.patches.seed_refinery_price_list import (
				execute as refining_price_list,
			)

			refining_price_list()
		finally:
			frappe.flags.in_migrate = in_migrate

	setup_data()
	create_attribute_value()
	create_item_attribute()
	create_users_data()
	frappe.db.commit()


def setup_data():
	if not frappe.db.exists("Stock Entry Type", "Material Transfer (MAIN SLIP)"):
		frappe.get_doc(
			{
				"doctype": "Stock Entry Type",
				"name": "Material Transfer (MAIN SLIP)",
				"purpose": "Material Transfer",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Stock Entry Type", "Process Loss"):
		frappe.get_doc(
			{
				"doctype": "Stock Entry Type",
				"name": "Process Loss",
				"purpose": "Repack",
			}
		).insert(ignore_permissions=True)

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
		frappe.get_doc({"doctype": "Warehouse Type", "__newname": "Transit"}).insert(
			ignore_permissions=True
		)

	if not frappe.db.exists("Company", "Test_Company"):
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "Test_Company",
				"country": "India",
				"default_currency": "INR",
				"create_chart_of_accounts_based_on": "Standard Template",
				"chart_of_accounts": "India - Chart of Accounts",
				"enable_perpetual_inventory": 0,
				"gstin": "24AAQCA8719H1ZC",
				"gst_category": "Registered Regular",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Fiscal Year", "2026-2027"):
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": "2026-2027",
				"year_start_date": "2026-04-01",
				"year_end_date": "2027-03-31",
				"companies": [{"company": "Test_Company"}],
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Customer Group", "All Customer Groups"):
		frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": "All Customer Groups",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Customer Group", "Test_Customer_Group"):
		_parent_cg = (
			frappe.db.get_value("Customer Group", {"is_group": 1}, "name")
			or "All Customer Groups"
		)
		frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": "Test_Customer_Group",
				"parent_customer_group": _parent_cg,
				"is_group": 0,
			}
		).insert(ignore_permissions=True)

	for _attr_value in ("Gold", "22KT", "91.6", "EF-VVS", "6B", "4"):
		if not frappe.db.exists("Attribute Value", _attr_value):
			frappe.get_doc(
				{"doctype": "Attribute Value", "attribute_value": _attr_value}
			).insert(ignore_permissions=True)

	for _setting_value in (
		{"attribute_value": "Open", "is_setting_type": 1},
		{"attribute_value": "Close", "is_setting_type": 1},
		{
			"attribute_value": "Close-Open Setting",
			"is_sub_setting_type": 1,
			"parent_attribute_value": "Open",
		},
		{
			"attribute_value": "Close Setting",
			"is_sub_setting_type": 1,
			"parent_attribute_value": "Close",
		},
	):
		if not frappe.db.exists("Attribute Value", _setting_value["attribute_value"]):
			frappe.get_doc({"doctype": "Attribute Value", **_setting_value}).insert(
				ignore_permissions=True
			)

	if not frappe.db.exists("Customer", "Test_Customer_External"):
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "Test_Customer_External",
				"customer_type": "Individual",
				"customer_group": "Test_Customer_Group",
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
		customer.append(
			"metal_criteria",
			{"metal_type": "Gold", "metal_touch": "22KT", "metal_purity": "91.6"},
		)
		customer.insert(ignore_permissions=True)

	if not frappe.db.exists("Customer", "Test_Customer_Internal"):
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "Test_Customer_Internal",
				"customer_type": "Individual",
				"customer_group": "Test_Customer_Group",
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
	if not frappe.db.exists("Employee", {"employee_name": "Test Designer Employee"}):
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

	if not frappe.db.exists("Item Group", "Raw Material"):
		frappe.get_doc(
			{"doctype": "Item Group", "item_group_name": "Raw Material", "is_group": 1}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Raw Material Template"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Raw Material Template",
				"parent_item_group": "Raw Material",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Metal - T"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Metal - T",
				"parent_item_group": "Raw Material Template",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Raw Material Variant"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Raw Material Variant",
				"parent_item_group": "Raw Material",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Metal - V"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Metal - V",
				"parent_item_group": "Raw Material Variant",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Diamond - V"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Diamond - V",
				"parent_item_group": "Raw Material Variant",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Finding - V"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Finding - V",
				"parent_item_group": "Raw Material Variant",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Diamond - T"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Diamond - T",
				"parent_item_group": "Raw Material Template",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Finding - T"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Finding - T",
				"parent_item_group": "Raw Material Template",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Gemstone - T"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Gemstone - T",
				"parent_item_group": "Raw Material Template",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "DNU"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "DNU",
				"parent_item_group": "All Item Groups",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Diamond DNU"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Diamond DNU",
				"parent_item_group": "DNU",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Metal DNU"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Metal DNU",
				"parent_item_group": "DNU",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Finding DNU"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Finding DNU",
				"parent_item_group": "DNU",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Gemstone DNU"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Gemstone DNU",
				"parent_item_group": "DNU",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Services"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Services",
				"parent_item_group": "All Item Groups",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Consumable"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Consumable",
				"parent_item_group": "All Item Groups",
				"is_group": 1,
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Item Group", "Tools & Accessories"):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "Tools & Accessories",
				"parent_item_group": "Consumable",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("UOM", "Nos"):
		frappe.get_doc(
			{"doctype": "UOM", "uom_name": "Nos", "must_be_whole_number": 0}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("UOM", "Gram"):
		frappe.get_doc(
			{
				"doctype": "UOM",
				"uom_name": "Gram",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("UOM", "Carat"):
		frappe.get_doc(
			{
				"doctype": "UOM",
				"uom_name": "Carat",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("UOM", "Litre"):
		frappe.get_doc({"doctype": "UOM", "uom_name": "Litre"}).insert(
			ignore_permissions=True
		)

	create_warehouse_and_department()
	print("Setup for the test data has been completed")


def create_warehouse_and_department():
	path = os.path.join(os.path.dirname(__file__), "jewellery_erpnext/test_data")

	import_file_by_path(
		os.path.join(path, "department.json"), force=True, data_import=True
	)

	import_file_by_path(
		os.path.join(path, "warehouse.json"),
		force=True,
	)
