import frappe
from frappe.utils import now_datetime

STAMPING_NO_FIELD = "custom_stamping_no"

# Year code: A=2021, B=2022, ... F=2026, G=2027.
_YEAR_CODE_EPOCH = 2021


def set_stamping_no(self, method=None):
	"""Stamp a Serial No with a unique, year-scoped sequential number -- once.

	Format is "2" + year code + a 4-digit sequence that restarts each year, so the
	third piece serialised in 2026 is ``2F0003``.
	"""
	if self.get(STAMPING_NO_FIELD):
		# before_save fires on every later update too -- never re-stamp a piece.
		return

	if not _has_stamping_no_field():
		return

	prefix = f"2{stamping_year_code(now_datetime().year)}"
	self.set(STAMPING_NO_FIELD, f"{prefix}{next_stamping_sequence(prefix):04d}")


def _has_stamping_no_field():
	"""``True`` when ``Serial No.custom_stamping_no`` exists on this site.

	The column is provisioned only by ``add_serial_no_stamping_no_field``, and
	``bench install-app`` marks every patch as already applied on a fresh site -- so a
	freshly installed site never runs it and has no column. Without this guard the
	attribute lookup takes down EVERY Serial No save with ``AttributeError``.
	``frappe.get_meta`` is request-cached, so the check is effectively free.
	"""
	return frappe.get_meta("Serial No").has_field(STAMPING_NO_FIELD)


def stamping_year_code(year):
	return chr(ord("A") + (year - _YEAR_CODE_EPOCH))


def next_stamping_sequence(prefix):
	"""One past the highest sequence already issued under ``prefix``.

	Counting rows is what broke this before: the count was of Serial Nos with NO
	stamping number, which is 0 on a backfilled site, so every new piece was handed
	``0001``. Read the high-water mark off the numbers actually issued instead, and
	compare the sequences numerically -- a plain ``MAX()`` over the strings ranks
	``2F9999`` above ``2F10000`` once a year passes 9,999 pieces.
	"""
	highest = frappe.db.sql(
		"""
		select max(cast(substring(custom_stamping_no, %(offset)s) as unsigned))
		from `tabSerial No`
		where custom_stamping_no like %(prefix)s
		""",
		{"offset": len(prefix) + 1, "prefix": f"{prefix}%"},
	)[0][0]

	return (highest or 0) + 1


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
