"""Optional dedicated naming counter for the Stock Entries a Refining Entry creates.

WHY
---
For KG GK, no Document Naming Rule matches the Stock Entry types refining creates
(Material Transfer, Material Receipt, Manufacture), so they are named from the naming
series: like every other KG GK transfer, receipt and manufacture, each one increments ONE
``tabSeries`` row, ``MAT-STE-``. ``frappe.model.naming.getseries`` locks
that row ``FOR UPDATE`` when the Stock Entry is inserted and keeps it until COMMIT, and
``doc_events.stock_entry.prelock_bins`` pins it again at submit. A refining submit posts
its whole transfer (thousands of lines on a scrap consignment) inside that hold, so for
minutes no other KG GK stock posting can get a number: on 16 Nov, 39 of 47 lock failures
waited behind two scrap-refining submits.

Naming refining's Stock Entries from their own series moves both locks onto a row only
refining transactions want: ``getseries`` locks the key for ``doc.naming_series``, and
``lock_order.series_prefix_for_doc`` derives the pre-lock key from the same field.

GATE (OFF BY DEFAULT)
---------------------
The visible number format is a business decision, so nothing changes until a site opts in::

    bench --site <site> set-config refining_se_naming_series "RFN-STE-"
    bench --site <site> execute jewellery_erpnext.refining.stock_entry_naming.ensure_refining_naming_series

``RFN-STE-00001`` is 13 characters, inside the 16-character limit on a GST document
number. Unset or empty keeps today's behaviour: the Stock Entry keeps the default series
(the first naming-series option, ``MAT-STE-``).

With the gate on, EVERY Stock Entry ``RefiningEntry`` creates takes the series, so a
transaction that creates two of them (submit: transfer then dust receipt; complete_refining:
repack then loss transfer) re-uses the row it already holds instead of taking a second
counter after its Bin locks.

It does nothing for a Stock Entry an active Document Naming Rule matches: frappe's
``set_new_name`` applies the rule before the naming series, and ``lock_order`` pins the
rule's counter instead. No such rule matches these types for KG GK today; a future naming
shard would take over from this setting.

The option is a convenience, not a requirement: frappe does not check ``naming_series``
against the field's options when a document is saved (``_validate_selects`` skips that
field; checked on frappe 16.52 and on the production frappe 3d1284c5).
:func:`ensure_refining_naming_series` adds it anyway, after the existing options so the
default stays ``MAT-STE-``, which lists the series in Document Naming Settings where its
counter can be read and corrected. It also creates the counter row, so the first refining
Stock Entry locks an existing row instead of inserting a new one.
"""

import frappe

#: site_config key holding the series refining Stock Entries are named from.
CONFIG_KEY = "refining_se_naming_series"
#: Proposed value, awaiting GK sign-off. Documentation only: it is not a default.
PROPOSED_SERIES = "RFN-STE-"

_DOCTYPE = "Stock Entry"
_FIELDNAME = "naming_series"


def refining_se_naming_series():
	"""The series configured for refining Stock Entries, or ``None`` while the gate is off."""
	value = frappe.conf.get(CONFIG_KEY)
	if not isinstance(value, str):
		return None
	return value.strip() or None


def apply_refining_naming_series(se):
	"""Name the new refining Stock Entry ``se`` from the refining series, when one is set.

	Call it before ``se.insert()``: frappe picks the name, and locks its counter, inside
	insert. Returns the series it set, or ``None`` (``se`` untouched) while the gate is off.
	"""
	series = refining_se_naming_series()
	if series:
		se.naming_series = series
	return series


def ensure_refining_naming_series(series=None):
	"""Add ``series`` (default: the configured one) to the Stock Entry naming-series
	options and create its counter row. Does nothing while no series is configured.

	Idempotent. The option is appended, never reordered, and the call refuses to run if
	that would change the default series. The counter row is created at the highest number
	already issued under the prefix, and only ever raised, so no name can repeat.

	Returns the counter prefix it ensured, or ``None`` when it had nothing to do.
	"""
	from frappe.model.naming import NamingSeries

	if series is None:
		series = refining_se_naming_series()
	series = (series or "").strip()
	if not series:
		return None

	naming = NamingSeries(series)
	naming.validate()
	_ensure_option(series)
	prefix = naming.get_prefix()
	_ensure_counter_row(prefix)
	return prefix


def _first_option(options):
	return next((o for o in options if o.strip()), None)


def _ensure_option(series):
	"""Append ``series`` to the naming-series options. True when it was added."""
	ps_name = frappe.db.get_value(
		"Property Setter",
		{
			"doc_type": _DOCTYPE,
			"field_name": _FIELDNAME,
			"property": "options",
			"doctype_or_field": "DocField",
		},
		"name",
	)
	if ps_name:
		current = frappe.db.get_value("Property Setter", ps_name, "value") or ""
	else:
		field = frappe.get_meta(_DOCTYPE, cached=False).get_field(_FIELDNAME)
		current = (field.options if field else "") or ""

	options = current.split("\n")
	if series in (o.strip() for o in options):
		return False

	# get_default_naming_series takes the first non-empty option, and new_doc fills it in.
	# The refining series must never become that default for everyone else.
	default = _first_option(options)
	if not default:
		frappe.throw(
			f"{_DOCTYPE}.{_FIELDNAME} has no option, so {series} would become the "
			"default series for every Stock Entry. Not adding it."
		)
	value = "\n".join([*options, series])

	if ps_name:
		ps = frappe.get_doc("Property Setter", ps_name)
		ps.value = value
		ps.flags.validate_fields_for_doctype = False
		ps.save(ignore_permissions=True)
	else:
		frappe.make_property_setter(
			{
				"doctype": _DOCTYPE,
				"fieldname": _FIELDNAME,
				"property": "options",
				"value": value,
				"property_type": "Small Text",
			},
			validate_fields_for_doctype=False,
			is_system_generated=False,
		)
	frappe.clear_cache(doctype=_DOCTYPE)
	return True


#: Highest number already issued under a prefix. Amended names (``RFN-STE-00007-1``) are
#: skipped by the REGEXP; their base number is counted from the original.
_MAX_ISSUED_SQL = """
	SELECT COALESCE(MAX(CAST(SUBSTRING(`name`, %(offset)s) AS UNSIGNED)), 0) AS issued
	FROM `tabStock Entry`
	WHERE `name` LIKE %(like)s
	  AND SUBSTRING(`name`, %(offset)s) REGEXP '^[0-9]+$'
"""


def _counter_params(prefix):
	like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
	return {"prefix": prefix, "offset": len(prefix) + 1, "like": like}


def _ensure_counter_row(prefix):
	"""Create ``tabSeries[prefix]`` at the highest number already issued, or raise it to
	that number. Never lowers it.

	Without the row, the first refining Stock Entry INSERTs it inside getseries, and the
	``FOR UPDATE`` before that insert locks the gap where the row will go rather than a row.
	"""
	frappe.db.sql(
		f"""
		INSERT INTO `tabSeries` (`name`, `current`)
		SELECT %(prefix)s, max_issued.issued FROM ({_MAX_ISSUED_SQL}) AS max_issued
		ON DUPLICATE KEY UPDATE
			`current` = GREATEST(`tabSeries`.`current`, VALUES(`current`))
		""",
		_counter_params(prefix),
	)


def after_migrate():
	"""Re-apply the option and counter row on every migrate (wired from ``hooks.py``).

	A ``patches.txt`` entry runs once. With the gate off by default it would run as a no-op,
	be marked done, and never add the option when the site later opts in. Run on every
	migrate instead, this also restores the option if a Customize Form reset drops it.

	Non-fatal: a naming-series option must never fail a migrate. Its own changes are rolled
	back on failure, and the failure is printed as well as logged.
	"""
	savepoint = "refining_se_naming_series"
	try:
		frappe.db.savepoint(savepoint)
		ensure_refining_naming_series()
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(
			title="after_migrate: refining Stock Entry naming series",
			message=frappe.get_traceback(),
		)
		print(
			"WARNING: the refining Stock Entry naming series could not be set up. "
			"The migrate itself succeeded; see Error Log "
			'"after_migrate: refining Stock Entry naming series". Re-run with: '
			"bench --site <site> execute "
			"jewellery_erpnext.refining.stock_entry_naming.ensure_refining_naming_series"
		)
