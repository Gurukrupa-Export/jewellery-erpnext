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
	if not company_abbr and self.reference_doctype == "Stock Entry" and self.reference_name:
		se_company = frappe.db.get_value("Stock Entry", self.reference_name, "company")
		company_abbr = company.get(se_company)
		if se_company:
			self.custom_company = se_company

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

		# --- Append RP if batch is created from a Repack Stock Entry ---
		if (
			self.reference_doctype == "Stock Entry"
			and self.reference_name
		):
			se_type = frappe.db.get_value(
				"Stock Entry", self.reference_name, "stock_entry_type"
			)
			if se_type == "Repack":
				batch_abbr_code_list.append("RP")

		# --- Final Batch Code ---
		if batch_number:
			batch_code = batch_number + "".join(batch_abbr_code_list)
			sequence = generate_unique_alphanumeric()
			self.name = batch_code + '-' + sequence

def autoname(self,method=None):
	# year_code = get_year_code()
	# month_code = get_month_code()
	# week_code = get_week_code()
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

import frappe
from frappe import _
from frappe.utils import today, now_datetime


@frappe.whitelist()
def create_repack_stock_entry(batch_no, item_code, qty, target_rate, company, warehouse):
    qty = float(qty)
    target_rate = float(target_rate)

    # --- Validate batch belongs to company ---
    batch_doc = frappe.get_doc("Batch", batch_no)
    if batch_doc.custom_company != "Sadguru Diamond":
        frappe.throw(_("Repack is only allowed for Sadguru Diamond batches."))

    # --- Fetch item details ---
    item = frappe.get_doc("Item", item_code)
    gst_hsn_code = item.gst_hsn_code or ""
    uom = item.stock_uom or "Nos"
    item_name = item.item_name or item_code

    # --- Fetch expense account and cost center ---
    expense_account = frappe.db.get_value(
        "Company", company, "stock_adjustment_account"
    ) or "Stock Adjustment - SD"

    cost_center = frappe.db.get_value(
        "Company", company, "cost_center"
    ) or "Common - SD"

    # --- Build Stock Entry ---
    se = frappe.new_doc("Stock Entry")
    se.stock_entry_type = "Repack"
    se.purpose = "Repack"
    se.company = company
    se.posting_date = today()
    se.posting_time = now_datetime().strftime("%H:%M:%S")
    se.set_posting_time = 0

    # --- Source Row (outgoing) — let ERPNext auto-fetch rate from SLE ---
    se.append("items", {
        "item_code": item_code,
        "item_name": item_name,
        "s_warehouse": warehouse,
        "qty": qty,
        "transfer_qty": qty,
        "uom": uom,
        "stock_uom": uom,
        "conversion_factor": 1,
        "batch_no": batch_no,
        "use_serial_batch_fields": 1,
        "is_finished_item": 0,
        "gst_hsn_code": gst_hsn_code,
        "expense_account": expense_account,
        "cost_center": cost_center,
        "pcs": "1"
    })

    # --- Target Row (incoming) ---
    se.append("items", {
        "item_code": item_code,
        "item_name": item_name,
        "t_warehouse": warehouse,
        "qty": qty,
        "transfer_qty": qty,
        "uom": uom,
        "stock_uom": uom,
        "conversion_factor": 1,
        "basic_rate": target_rate,
        "valuation_rate": target_rate,
        "basic_amount": target_rate * qty,
        "amount": target_rate * qty,
        "set_basic_rate_manually": 1,
        "is_finished_item": 1,
        "gst_hsn_code": gst_hsn_code,
        "expense_account": expense_account,
        "cost_center": cost_center,
        "use_serial_batch_fields": 1,
        "pcs": "1"
    })

    se.insert(ignore_permissions=True)
    se.submit()

    gl_totals = frappe.db.get_all(
        "GL Entry",
        filters={
            "voucher_type": "Stock Entry",
            "voucher_no": se.name,
            "account": expense_account,
            "is_cancelled": 0,
        },
        fields=["sum(debit) as total_debit", "sum(credit) as total_credit"],
    )[0]

    total_debit = gl_totals.total_debit or 0
    total_credit = gl_totals.total_credit or 0
    net_stock_adjustment = total_debit - total_credit  # net amount currently sitting in Stock Adjustment for this SE


    # --- Create Journal Entry only if there is a difference ---
    if net_stock_adjustment != 0:
        je = _create_repack_journal_entry(
            se_name=se.name,
            company=company,
            cost_center=cost_center,
            expense_account=expense_account,
            value_difference=net_stock_adjustment,
            posting_date=se.posting_date
        )
        return {"stock_entry": se.name, "journal_entry": je}

    return {"stock_entry": se.name, "journal_entry": None}


def _create_repack_journal_entry(
    se_name, company, cost_center, expense_account, value_difference, posting_date
):
    abs_amount = abs(value_difference)

    # Fetch Stock Received But Not Billed account from Company
    stock_rbnb_account = frappe.db.get_value(
        "Company", company, "stock_received_but_not_billed"
    ) or f"Stock Received But Not Billed - SD"

    # --- Determine debit/credit based on sign of value_difference ---
    # Positive difference (incoming > outgoing):
    #   Stock Adjustment  → Credit
    #   Stock RBNB        → Debit
    # Negative difference (outgoing > incoming):
    #   Stock Adjustment  → Debit
    #   Stock RBNB        → Credit

    if value_difference > 0:
        stock_adj_debit  = 0
        stock_adj_credit = abs_amount
        rbnb_debit       = abs_amount
        rbnb_credit      = 0
    else:
        stock_adj_debit  = abs_amount
        stock_adj_credit = 0
        rbnb_debit       = 0
        rbnb_credit      = abs_amount

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = company
    je.posting_date = posting_date
    je.title = expense_account
    je.user_remark = f"Stock Adjustment Entry | Repack: {se_name}"
    je.remark = f"Note: Stock Adjustment Entry | Ref: {se_name}"

    # --- Stock Adjustment Account row ---
    je.append("accounts", {
        "account": expense_account,
        "cost_center": cost_center,
        "debit_in_account_currency": stock_adj_debit,
        "debit": stock_adj_debit,
        "credit_in_account_currency": stock_adj_credit,
        "credit": stock_adj_credit,
        "exchange_rate": 1,
        "against_account": stock_rbnb_account
    })

    # --- Stock Received But Not Billed row ---
    je.append("accounts", {
        "account": stock_rbnb_account,
        "cost_center": cost_center,
        "debit_in_account_currency": rbnb_debit,
        "debit": rbnb_debit,
        "credit_in_account_currency": rbnb_credit,
        "credit": rbnb_credit,
        "exchange_rate": 1,
        "against_account": expense_account
    })

    je.insert(ignore_permissions=True)
    je.submit()

    return je.name



