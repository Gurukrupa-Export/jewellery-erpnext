import frappe
from frappe import _


def crate_autoname(self):
	company_abbr = frappe.db.get_value("Company", self.company, "abbr")
	self.naming_series = "M-" + company_abbr + "-.{category_code}.-.#####"


def get_current_mould_id(item_code):
	"""Single source of truth: return the Mould List ID (the Mould docname) for
	item_code, or None. This is the value propagated to Manufacturing Plan / PMO /
	MWO -- the Mould's own ``mould_no`` (a warehouse/rake/tray/box location string)
	is a separate concern and is often blank for auto-created Moulds."""
	if not item_code:
		return None
	return frappe.db.get_value(
		"Mould", {"item_code": item_code}, "name", order_by="creation desc"
	)


def get_mould_id_map(item_codes):
	"""Batched version of get_current_mould_id for many item_codes at once."""
	item_codes = {code for code in item_codes if code}
	if not item_codes:
		return {}
	rows = frappe.get_all(
		"Mould",
		filters={"item_code": ["in", list(item_codes)]},
		fields=["item_code", "name"],
		order_by="creation desc",
	)
	mould_map = {}
	for row in rows:
		mould_map.setdefault(row.item_code, row.name)
	return mould_map


def mould_exists_for_item(item_code, exclude=None):
	if not item_code:
		return None
	filters = {"item_code": item_code}
	if exclude:
		filters["name"] = ["!=", exclude]
	return frappe.db.exists("Mould", filters)


def validate_unique_item_code(self):
	existing = mould_exists_for_item(self.item_code, exclude=self.name)
	if existing:
		frappe.throw(
			_(
				"A Mould record already exists for Item {0} ({1}). Only one Mould is allowed per Item."
			).format(self.item_code, existing)
		)


def clear_item_mould_cache(self, method=None):
	frappe.db.set_value("Item", self.item_code, "mould", None)


def update_details(self):
	# Compute the location string (mould_no) only once every input is present.
	# Auto-created Moulds (Employee IR casting flow) are inserted with blank
	# warehouse/rake/tray/box; leave mould_no blank (no throw, no rake[0] crash)
	# until the user fills them in on the Mould screen -- at which point a normal
	# save recomputes mould_no and refreshes the Item.mould cache.
	if not (self.warehouse and self.rake and self.tray_no and self.box_no):
		return
	rake = self.rake
	rake = rake[0].capitalize()
	if rake.isnumeric():
		frappe.throw(_("Rake is Alphabet"))
	self.rake = rake

	tray_no = self.tray_no
	if tray_no.isnumeric():
		tray_no = int(self.tray_no)
		tray_no = "{:02}".format(tray_no)

		self.tray_no = tray_no
	else:
		frappe.throw(_("Try No must be Numeric"))

	box_no = self.box_no
	if box_no.isnumeric():
		box_no = int(self.box_no)
		box_no = "{:02}".format(box_no)
		self.box_no = box_no
	else:
		frappe.throw(_("Box No must be Numeric"))

	warehouse_abbr = frappe.db.get_value("Warehouse", self.warehouse, "custom_abbr")
	if not warehouse_abbr:
		frappe.throw(
			_("Add abbreviation for Warehouse <b>{0}</b>").format(self.warehouse)
		)

	mould_no = warehouse_abbr + "/" + rake + "/" + tray_no + "/" + box_no
	self.mould_no = mould_no
	frappe.db.set_value("Item", self.item_code, "mould", self.mould_no)
