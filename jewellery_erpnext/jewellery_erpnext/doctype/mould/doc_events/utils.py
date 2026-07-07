import frappe
from frappe import _


def crate_autoname(self):
	company_abbr = frappe.db.get_value("Company", self.company, "abbr")
	self.naming_series = "M-" + company_abbr + "-.{category_code}.-.#####"


def get_current_mould_no(item_code):
	"""Single source of truth: return the Mould.mould_no for item_code, or None."""
	if not item_code:
		return None
	return frappe.db.get_value("Mould", {"item_code": item_code}, "mould_no")


def validate_unique_item_code(self):
	existing = frappe.db.exists(
		"Mould", {"item_code": self.item_code, "name": ["!=", self.name]}
	)
	if existing:
		frappe.throw(
			_(
				"A Mould record already exists for Item {0} ({1}). Only one Mould is allowed per Item."
			).format(self.item_code, existing)
		)


def clear_item_mould_cache(self, method=None):
	frappe.db.set_value("Item", self.item_code, "mould", None)


def update_details(self):
	if self.is_new():
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
