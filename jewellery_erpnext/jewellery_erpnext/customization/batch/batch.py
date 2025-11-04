from jewellery_erpnext.jewellery_erpnext.customization.batch.doc_events.utils import (
	update_inventory_dimentions,
	update_pure_qty,
)

import frappe
import datetime
import random
import string

def validate(self, method):
	update_pure_qty(self)
	update_inventory_dimentions(self)
	
	if frappe.flags.get('autoname_done'):
		return

	item_group = frappe.db.get_value("Item", self.item, "item_group")
	year_code = get_year_code()
	month_code = get_month_code()
	week_code = get_week_code()

	company = {
		"Gurukrupa Export Private Limited": "GE",
		"KG GK Jewellers Private Limited": "KG",
		"Sadguru Diamond": "SD",
		"Sadguru Hallmarking Centre": "SHC"
	}
	company_abbr = company.get(self.custom_company)

	# --- Initialize batch_number safely ---
	batch_number = None

	if item_group:
		if item_group.startswith("D"):
			batch_number = f"{company_abbr}{year_code}{month_code}{week_code}-D"
		elif item_group.startswith("M"):
			batch_number = f"{company_abbr}{month_code}{week_code}-M"
		elif item_group.startswith("G"):
			batch_number = f"{company_abbr}{month_code}{week_code}-G"
		elif item_group.startswith("F"):
			batch_number = f"{company_abbr}{month_code}{week_code}-F"
		elif item_group.startswith("O"):
			batch_number = f"{company_abbr}{month_code}{week_code}-O"
		elif item_group.startswith("R"):
			batch_number = f"{company_abbr}{month_code}{week_code}-R"

	# --- Safety fallback ---
	# if not batch_number:
	# 	# Either throw an error OR assign a default
	# 	frappe.throw(f"Cannot generate batch number for Item Group: {item_group}")

	# --- Collect abbreviations ---
	batch_abbr_code_list = []
	for i in frappe.get_doc("Item", self.item).attributes:
		if i.attribute == "Finding Category":
			continue

		batch_abbreviation = frappe.db.get_value(
			"Attribute Value", i.attribute_value, "custom_batch_abbreviation"
		)

		if i.attribute_value:
			if batch_abbreviation:
				batch_abbr_code_list.append(batch_abbreviation)
			else:
				frappe.throw(f"Abbreviation is missing for {i.attribute_value}")

	# --- Final Batch Code ---
	if batch_number:
		batch_code = batch_number + "".join(batch_abbr_code_list)
		sequence = generate_unique_alphanumeric()
		self.name = batch_code + '-' + sequence

def autoname(self,method=None):
	# year_code = get_year_code()
	# month_code = get_month_code()
	# week_code = get_week_code()

	if frappe.flags.get('autoname_done'):
		return
	
	item_group = frappe.db.get_value("Item",{self.item},"item_group")

	if item_group in ["Metal - V", "Diamond - V", "Gemstone - V", "Finding - V", "Other - V"]:
		year_code = get_year_code()
		month_code = get_month_code()
		week_code = get_week_code()
		# start_of_week, end_of_week = get_current_week_date_range()
		company ={"Gurukrupa Export Private Limited":"GE",
			"KG GK Jewellers Private Limited":"KG",
			"Sadguru Diamond":"SD",
			"Sadguru Hallmarking Centre":"SHC"}
		company_abbr = company.get(self.custom_company)

		if item_group == "Diamond - V":
			batch_number = f"{company_abbr}{year_code}{month_code}{week_code}-D".format(
				year_code=year_code, month_code=month_code, week_code=week_code
			)
		elif item_group == "Metal - V":
			batch_number = f"{company_abbr}{month_code}{week_code}-M".format(
				year_code=year_code, month_code=month_code, week_code=week_code
			)
		elif item_group == "Gemstone - V":
			batch_number = f"{company_abbr}{month_code}{week_code}-G".format(
				year_code=year_code, month_code=month_code, week_code=week_code
			)
		elif item_group == "Finding - V":
			batch_number = f"{company_abbr}{month_code}{week_code}-F".format(
				year_code=year_code, month_code=month_code, week_code=week_code
			)
		elif item_group == "Other - V":
			batch_number = f"{company_abbr}{month_code}{week_code}-O".format(
				year_code=year_code, month_code=month_code, week_code=week_code
			)
		batch_abbr_code_list = []

		for i in frappe.get_doc("Item",self.item).attributes:
			if i.attribute == "Finding Category":
				continue
			batch_abbreviation = frappe.db.get_value(
				"Attribute Value", i.attribute_value, "custom_batch_abbreviation"
			)
			if i.attribute_value:
				if batch_abbreviation:
					batch_abbr_code_list.append(batch_abbreviation)
				else:
					frappe.throw(("Abbrivation is missing for {0}").format(i.attribute_value))
		batch_code = batch_number + "".join(batch_abbr_code_list)
		# batch_list = frappe.db.sql(f"""SELECT
		# 								name
		# 							FROM
		# 								`tabBatch`
		# 							WHERE
		# 								manufacturing_date > '{start_of_week}'
		# 								AND manufacturing_date < '{end_of_week}'
		# 								AND item = '{self.item}'
		# 							ORDER BY
		# 								CAST(SUBSTRING_INDEX(name, '-', -1) AS UNSIGNED) DESC;
		# 							""",as_dict=1)
		# if batch_list:
		# 	batch = batch_list[0]["name"].split('-')[-1]
		# 	sequence = int(batch) + 1
		# 	sequence = f"{sequence:04}"
		# else:
		# 	sequence = '0001'
		sequence = generate_unique_alphanumeric()
		self.name = batch_code + '-' + sequence


def get_year_code():
	year_dict = {
		"1": "A",
		"2": "B",
		"3": "C",
		"4": "D",
		"5": "E",
		"6": "F",
		"7": "G",
		"8": "H",
		"9": "I",
		"0": "J",
	}
	current_year = datetime.datetime.now().year
	last_two_digits = current_year % 100
	return str(last_two_digits)[0] + year_dict[str(last_two_digits)[1]]

def get_week_code():
	current_date = datetime.date.today()
	week_number = (current_date.day - 1) // 7 + 1
	return str(week_number)

def get_month_code():
	current_date = datetime.datetime.now()
	month_two_digit = current_date.strftime("%m")
	return str(month_two_digit)

# def get_current_week_date_range():
# 	current_date = datetime.date.today()
# 	first_day_of_month = current_date.replace(day=1)

# 	# Calculate start of the week
# 	day_of_week = current_date.weekday()  # Monday is 0, Sunday is 6
# 	start_of_week = current_date - datetime.timedelta(days=day_of_week)

# 	# Make sure the week doesn't start before the first of the month
# 	start_of_week = max(start_of_week, first_day_of_month)

# 	# Calculate end of the week
# 	end_of_week = start_of_week + datetime.timedelta(days=6)

# 	# Make sure the week doesn't extend beyond the month
# 	last_day_of_month = (
# 		current_date.replace(day=28) + datetime.timedelta(days=4)
# 	).replace(day=1) - datetime.timedelta(days=1)
# 	end_of_week = min(end_of_week, last_day_of_month)

# 	start_formatted = start_of_week.strftime("%Y-%-m-%-d")
# 	end_formatted = end_of_week.strftime("%Y-%-m-%-d")

# 	return start_formatted, end_formatted

def generate_unique_alphanumeric():
    while True:
        # Ensure at least one letter and one number
        letters = random.choices(string.ascii_uppercase, k=2)  # At least 2 letters
        digits = random.choices(string.digits, k=3)  # At least 3 numbers
        random_code = ''.join(random.sample(letters + digits, 5))  # Shuffle & combine

        # Check if it already exists
        existing_doc = frappe.get_value("Manufacturing Operation", {"name": f"MOP-{random_code}"}, "name")

        if not existing_doc:  # If unique, return it
            return random_code


GOLD_ITEMS = {"M-G-24KT-99.9-Y", "M-G-24KT-99.5-Y"}

def on_update(doc, method):
	if not doc.flags.is_update_origin_entries:
		return

	if not doc.custom_origin_entries:
		return

	if doc.reference_doctype != "Stock Entry" or not doc.custom_voucher_detail_no:
		return

	se_type = frappe.db.get_value(doc.reference_doctype, doc.reference_name, "stock_entry_type")
	if se_type != "Repack-Metal Conversion":
		return

	metal_purity = frappe.db.get_value("Item Variant Attribute", {
		"parent": doc.item,
		"attribute": "Metal Purity"
	}, "attribute_value")

	if not metal_purity:
		metal_purity = doc.item.split('-')[-2]

	alloy_rate = float()
	metal_rate = float()

	def _is_alloy(item_code):
		item = frappe.get_doc("Item", item_code)
		res = False

		if item.item_group == "Alloy":
			res = True
		elif len(item.attributes) == 1:
			res = True

		return res

	for row in doc.custom_origin_entries:
		if _is_alloy(row.item_code):
			alloy_rate = row.rate
		elif row.item_code in GOLD_ITEMS:
			metal_rate = ((row.rate * float(metal_purity)) / 100)

	doc.db_set("custom_alloy_rate", alloy_rate)
	doc.db_set("custom_metal_rate", metal_rate)
