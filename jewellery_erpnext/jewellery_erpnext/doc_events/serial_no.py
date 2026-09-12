import frappe
from frappe.utils import cint, now_datetime

STAMPING_NO_FIELD = "custom_stamping_no"

# Year code: A=2021, B=2022, ... F=2026, G=2027.
_YEAR_CODE_EPOCH = 2021

# Namespace for this counter's rows in `tabSeries`. That table's `name` is a SITE-WIDE
# namespace shared with every naming series, and the bare stamping prefix "2F" is exactly
# the key a naming_series of "2F.####" would claim. A key STARTING WITH "#" is unreachable
# from any naming series: parse_naming_series consumes a "#"-leading part as the counter
# itself (frappe/model/naming.py:355-358), so an accumulated prefix can never begin with one.
_STAMPING_SERIES_NS = "#JWL-STAMP-"


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

	prefix = stamping_prefix()
	self.set(STAMPING_NO_FIELD, f"{prefix}{reserve_stamping_sequence(prefix):04d}")


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


def stamping_prefix(when=None):
	"""The 2-char prefix ("2" + year code) a number issued at ``when`` carries.

	The live path passes nothing and stamps by the CURRENT year -- the year the number is
	issued. The backfill passes a row's ``creation`` instead, to label a legacy piece by the
	year it was made. That difference is deliberate, not a bug: each prefix has its own
	counter row, so the two can never collide, and retro-labelling old pieces with this
	year would destroy the year signal the format exists to carry.
	"""
	return f"2{stamping_year_code((when or now_datetime()).year)}"


def stamping_series_key(prefix):
	"""The ``tabSeries`` row holding the counter for ``prefix`` -- e.g. "#JWL-STAMP-2F"."""
	return f"{_STAMPING_SERIES_NS}{prefix}"


def reserve_stamping_sequence(prefix):
	"""Atomically claim the next sequence under ``prefix``.

	This is the collision fix. The old ``MAX(...) + 1`` read (kept below as
	:func:`next_stamping_sequence`, now only used for seeding) was an unlocked
	read-modify-write, and TWO things made its window far wider than "the same millisecond":

	* The number is minted at the very END of the Serial Number Creator cascade
	  (``update_new_serial_no``), and nothing in that cascade commits, so one submit's number
	  stayed invisible to the next for as long as the submit took -- seconds.
	* Under REPEATABLE READ a plain ``MAX()`` reads the transaction's SNAPSHOT, so a second
	  submit could miss the first's number even AFTER it committed.

	``INSERT .. ON DUPLICATE KEY UPDATE`` is one atomic statement: it takes an exclusive row
	lock held to COMMIT, so concurrent claimants serialise, and the follow-up read sees our
	own uncommitted value. Preferred over ``frappe.model.naming.getseries`` because that one
	falls through to a BARE INSERT when the row is absent -- on the first use of a key, two
	concurrent transactions both find nothing and one dies on a duplicate-key error.

	GAPLESS: the counter is an ordinary InnoDB row updated inside the caller's own
	transaction, so a failed submit rolls the number back with everything else. That holds
	only while nothing commits mid-cascade; ``test_stamping_no`` pins it.
	"""
	key = stamping_series_key(prefix)
	_ensure_stamping_series_row(key, prefix)
	frappe.db.sql(
		"""
		INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, 1)
		ON DUPLICATE KEY UPDATE `current` = `current` + 1
		""",
		(key,),
	)
	return cint(
		frappe.db.sql("SELECT `current` FROM `tabSeries` WHERE `name` = %s", (key,))[0][
			0
		]
	)


def _ensure_stamping_series_row(key, prefix):
	"""Create ``key``'s counter row seeded from the numbers ALREADY issued -- once per site.

	Never let the counter start from zero on a site that already has stamped pieces: that
	re-issues ``2F0001``, a number physically on a piece. Seeding is a single INSERT..SELECT,
	so there is no read-modify-write window. A loser of the race lands in the ``ON DUPLICATE``
	branch, which is monotonic -- ``GREATEST`` can only raise a counter, never hand back a
	number that is already on a piece.

	The cheap primary-key probe in front is what keeps the ``MAX()`` scan OFF the hot path --
	it runs once per prefix per site, not on every Serial No save.

	This is also the only seeding that works on a site where ``bench install-app`` marked
	every patch as applied without running it (the same failure mode ``_has_stamping_no_field``
	guards against), which is why ``seed_serial_no_stamping_series`` is a deploy-time
	convenience rather than the correctness guarantee.
	"""
	if frappe.db.sql("SELECT 1 FROM `tabSeries` WHERE `name` = %s", (key,)):
		return

	frappe.db.sql(
		"""
		INSERT INTO `tabSeries` (`name`, `current`)
		SELECT %(key)s,
		       COALESCE(MAX(CAST(SUBSTRING(custom_stamping_no, %(offset)s) AS UNSIGNED)), 0)
		FROM `tabSerial No`
		WHERE custom_stamping_no LIKE %(like)s
		ON DUPLICATE KEY UPDATE
			`current` = GREATEST(`tabSeries`.`current`, VALUES(`current`))
		""",
		{"key": key, "offset": len(prefix) + 1, "like": f"{prefix}%"},
	)


def next_stamping_sequence(prefix):
	"""One past the highest sequence already issued under ``prefix``.

	SEEDING ONLY -- no longer on the live path, which uses the atomic
	:func:`reserve_stamping_sequence`. This read is not safe against concurrent writers and
	must never be used to hand a number to a piece. Kept because the backfill in
	``patches/add_serial_no_stamping_no_field`` imports it.

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
