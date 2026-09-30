import datetime
import random
import re
import string

import frappe
from frappe import _
from frappe.utils import cstr, flt

from jewellery_erpnext.jewellery_erpnext.customization.batch.doc_events.utils import (
	update_inventory_dimentions,
	update_pure_qty,
)

# Hand-picked batch prefixes for the Gurukrupa group companies. These are NOT derivable
# from Company.abbr — they are baked into every existing batch name and into the '-'
# delimited parsing in customer_subcontracting/batch_rename.py, so they stay fixed.
BATCH_COMPANY_ABBR = {
	"Gurukrupa Export Private Limited": "GE",
	"KG GK Jewellers Private Limited": "KG",
	"Sadguru Diamond": "SD",
	"Sadguru Hallmarking Centre": "SHC",
}


def _sanitize_abbr(value):
	"""Batch names are '-' delimited (batch_rename splits on it and reads the trailing
	serial), so a prefix must stay alphanumeric."""
	return re.sub(r"[^A-Za-z0-9]", "", cstr(value)).upper()


def get_batch_company_abbr(doc):
	"""Company prefix for a variant batch name.

	Resolution order — company: the Batch's own ``custom_company`` (set explicitly by
	callers that know which company owns the material, e.g. Refining Entry's
	``_auto_create_batch``), else the session user's default, else the global default,
	else the only Company on the site when there is exactly one (CI, a fresh bench:
	nothing to disambiguate).

	Abbreviation: the group map above, else the Company's own ``abbr``. The fallback
	matters because batch creation is implicit — a Stock Entry on a company nobody added
	to the map must still be able to mint a batch instead of aborting the transaction.

	Only a company that cannot be determined AT ALL on a multi-company site is refused:
	guessing a prefix there stamps one company's material with another's code, which is
	unrecoverable once the batch is transacted (and the pre-existing behaviour — a batch
	literally named ``None2607W1-M...`` — is not a usable alternative)."""
	company = (
		doc.get("custom_company")
		or frappe.defaults.get_user_default("company")
		or frappe.defaults.get_global_default("company")
	)
	if not company:
		companies = frappe.get_all("Company", pluck="name", limit=2)
		if len(companies) == 1:
			company = companies[0]

	if company:
		abbr = (
			BATCH_COMPANY_ABBR.get(company)
			or _sanitize_abbr(frappe.db.get_value("Company", company, "abbr"))
			or _sanitize_abbr(company)[:3]
		)
		if abbr:
			return abbr

	frappe.throw(
		_(
			"Cannot generate a batch name for item {0}: the company it belongs to could "
			"not be determined. Set the Batch's Company, or set a default company for "
			"your user / in Global Defaults."
		).format(frappe.bold(doc.item))
	)


def validate(self, method):
	if frappe.flags.is_batch_autoname:
		return
	update_pure_qty(self)
	update_inventory_dimentions(self)


def autoname(self, method=None):
	# year_code = get_year_code()
	# month_code = get_month_code()
	# week_code = get_week_code()
	if frappe.flags.is_batch_autoname:
		return

	item_group = frappe.db.get_value("Item", self.item, "item_group")
	variant = frappe.db.get_value("Item", self.item, "variant_of")

	if item_group in [
		"Metal - V",
		"Diamond - V",
		"Gemstone - V",
		"Finding - V",
		"Other - V",
	]:
		year_code = get_year_code()
		month_code = get_month_code()
		week_code = get_week_code()
		# start_of_week, end_of_week = get_current_week_date_range()
		company_abbr = get_batch_company_abbr(self)

		if item_group == "Diamond - V":
			batch_number = f"{company_abbr}{year_code}{month_code}{week_code}-D".format(
				year_code=year_code, month_code=month_code, week_code=week_code
			)
		elif item_group == "Metal - V":
			batch_number = f"{company_abbr}{year_code}{month_code}{week_code}-M".format(
				year_code=year_code, month_code=month_code, week_code=week_code
			)
		elif item_group == "Gemstone - V":
			batch_number = (
				f"{company_abbr}{year_code}{month_code}{week_code}-{variant}".format(
					year_code=year_code, month_code=month_code, week_code=week_code
				)
			)
		elif item_group == "Finding - V":
			batch_number = f"{company_abbr}{year_code}{month_code}{week_code}-F".format(
				year_code=year_code, month_code=month_code, week_code=week_code
			)
		elif item_group == "Other - V":
			batch_number = f"{company_abbr}{year_code}{month_code}{week_code}-O".format(
				year_code=year_code, month_code=month_code, week_code=week_code
			)
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
					frappe.throw(
						("Abbrivation is missing for {0}").format(i.attribute_value)
					)
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
		self.name = batch_code + "-" + sequence


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
		random_code = "".join(random.sample(letters + digits, 5))  # Shuffle & combine

		# Check if it already exists
		existing_doc = frappe.get_value(
			"Manufacturing Operation", {"name": f"MOP-{random_code}"}, "name"
		)

		if not existing_doc:  # If unique, return it
			return random_code


# Purities within this tolerance (percentage points) count as equal, so a
# same-purity conversion (e.g. 18KT metal -> 18KT finding) inherits the source
# Batch Rate unchanged instead of being re-scaled. Used by the retired blend's
# one-off backfill (patches/backfill_blended_batch_metal_rate) -- see on_update.
PURITY_TOLERANCE = 0.01


def _resolve_metal_purity(item_code):
	"""Return the numeric metal purity % for an item, robust to unset data.

	Prefers ``Attribute Value.purity_percentage``; falls back to the numeric
	Attribute Value *name* (production data stores e.g. "75.4" as the name with
	``purity_percentage`` left at 0), then to the purity token in the item code.
	Returns 0.0 when nothing resolves so callers never crash.
	"""
	if not item_code:
		return 0.0

	attribute_value = frappe.db.get_value(
		"Item Variant Attribute",
		{"parent": item_code, "attribute": "Metal Purity"},
		"attribute_value",
	)
	if attribute_value:
		pct = frappe.db.get_value(
			"Attribute Value", attribute_value, "purity_percentage"
		)
		if pct:
			return flt(pct)
		try:
			return flt(float(attribute_value))
		except (TypeError, ValueError):
			pass

	try:
		return flt(float(item_code.split("-")[-2]))
	except (TypeError, ValueError, IndexError):
		return 0.0


# Which Batch fields may hold a source row's own rate, in preference order. Only
# patches/backfill_blended_batch_metal_rate reads these now: the blend they served is retired
# (see on_update).
ALLOY_SOURCE_RATE_FIELDS = ("custom_alloy_rate", "custom_metal_rate")
METAL_SOURCE_RATE_FIELDS = ("custom_metal_rate",)


def on_update(doc, method=None):
	"""Retired (F26). No longer registered in hooks.py; kept so a stale hooks cache cannot
	break a Batch save between deploy and cache clear.

	This used to restate a Repack-Metal Conversion target's ``custom_metal_rate`` and
	``custom_alloy_rate`` from its origin entries on every provenance save: a qty-weighted,
	purity-scaled mix of the source batches' rates. That overwrote the rate the batch was
	minted with -- the minting row's own rate, which is the ledger's incoming rate and so the
	figure batch-wise valuation charges on every issue (or, on a zero-valued customer row, the
	rate the user entered). The mix also left the company alloy out of the metal rate,
	divided by 100 instead of the source purity, and depended on an alloy classifier that
	differs between sites; on the 22KT batch of the KLHGX62F1119 audit it read 144,648.4625
	against a ledger rate of 144,642.733945.

	Batch Rate is now the minting stamp alone: ``batch_rename._source_row_rate`` for batches
	minted by hand, ``doc_events.utils._source_row_rate`` for the rest. Origin entries are
	still recorded as provenance, and Batch Component is unchanged. Batches already restated
	by the blend are re-stamped by ``patches/restamp_batch_rate_from_ledger``.
	"""
	return
