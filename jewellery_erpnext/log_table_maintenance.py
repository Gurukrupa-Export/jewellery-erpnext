"""
Shrink the log tables the GK -> KGGK migration left behind, from the desk.

* ``shrink_error_log``: ``tabError Log`` is MyISAM (Frappe keeps errors outside transactions).
  Frappe's daily ``run_log_clean_up`` removes old rows with one DELETE that locks the whole
  table while it runs, and MyISAM never shrinks its file afterwards: the freed rows only show
  up as ``DATA_FREE``. This calls Frappe's own ``clear_log_table`` (the code behind
  ``bench clear-log-table``). It copies the last ``days`` days into a new table, swaps it in
  and drops the old one, so the disk comes back at once. Errors logged while the copy runs
  land in the old table and are dropped with it.
* ``purge_deleted_documents``: deleting rows from a list view copies every row into Deleted
  Document, so bulk deletes of Version and Error Log rows grew that table. This deletes those
  copies by primary key in chunks; a plain DELETE does not create new Deleted Document rows.
  ``optimize=1`` then rebuilds the table to give the disk back.
* ``purge_malformed_versions`` (optional): the KGGK receiver Server Scripts inserted Versions
  by hand whose ``data`` is a Python repr (``{'version': ...``), which Frappe cannot render.
  The standard Version written by the same save already records the change.

Every task defaults to a dry run that only counts, and records its summary as one Error Log.
Start one from System Console (Python)::

    frappe.call("jewellery_erpnext.log_table_maintenance.enqueue_task", task="error_log", dry_run=1)
"""

from collections import Counter

import frappe
from frappe import _
from frappe.utils import cint, now

TITLE = "Log table maintenance"
CHUNK_SIZE = 5000
DEFAULT_DELETED_DOCTYPES = ("Version", "Error Log")
DEFAULT_VERSION_DOCTYPES = ("BOM", "Item")
MALFORMED_VERSION_PREFIX = "{'version': "

TASKS = {
	"error_log": "shrink_error_log",
	"deleted_document": "purge_deleted_documents",
	"malformed_versions": "purge_malformed_versions",
}


# POST only: a GET can be triggered by a link or an image in a logged-in System Manager's
# browser. System Console's frappe.call is not affected (in_safe_exec skips the method check).
@frappe.whitelist(methods=["POST"])
def enqueue_task(task, dry_run=1, days=7, optimize=0, doctypes=None):
	"""Queue one maintenance task on the long queue. System Manager only.

	``days`` applies to ``error_log``, ``optimize`` to ``deleted_document``. ``doctypes``
	narrows ``deleted_document`` (by ``deleted_doctype``) or ``malformed_versions`` (by
	``ref_doctype``).
	"""
	frappe.only_for("System Manager")

	method = TASKS.get(task)
	if not method:
		frappe.throw(
			_("Unknown task {0}. Use one of: {1}").format(task, ", ".join(TASKS))
		)

	kwargs = {"dry_run": cint(dry_run)}
	if task == "error_log":
		kwargs["days"] = cint(days)
	elif task == "deleted_document":
		kwargs["optimize"] = cint(optimize)
		kwargs["deleted_doctypes"] = doctypes
	else:
		kwargs["ref_doctypes"] = doctypes

	job = frappe.enqueue(
		f"jewellery_erpnext.log_table_maintenance.{method}",
		queue="long",
		timeout=4 * 60 * 60,
		job_id=f"log-table-maintenance::{task}",
		deduplicate=True,
		**kwargs,
	)
	if not job:
		return _("This task is already queued or running.")

	return _("Queued. The summary will appear in Error Log as '{0}: {1}'.").format(
		TITLE, task
	)


def shrink_error_log(days=7, dry_run=1):
	"""Keep only the last ``days`` days of Error Log by swapping in a table that holds just them."""
	from frappe.core.doctype.log_settings.log_settings import clear_log_table

	days = cint(days)
	if days < 1:
		frappe.throw(_("Keep at least one day of Error Log."))

	summary = _start("error_log", dry_run, days=days, before=_table_size("Error Log"))
	# The same condition clear_log_table copies with, so the count matches what survives.
	summary["rows_kept"] = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabError Log` WHERE `creation` > NOW() - INTERVAL %s DAY",
		(days,),
	)[0][0]

	if not summary["dry_run"]:
		clear_log_table("Error Log", days=days)
		summary["after"] = _table_size("Error Log")

	return _finish(summary)


def purge_deleted_documents(dry_run=1, optimize=0, deleted_doctypes=None):
	"""Delete the Deleted Document copies of the given doctypes (default: Version, Error Log)."""
	deleted_doctypes = _parse_list(deleted_doctypes) or list(DEFAULT_DELETED_DOCTYPES)
	summary = _start(
		"deleted_document",
		dry_run,
		deleted_doctypes=deleted_doctypes,
		before=_table_size("Deleted Document"),
	)

	rows = _deleted_document_rows(deleted_doctypes)
	summary["rows"] = dict(Counter(deleted_doctype for _name, deleted_doctype in rows))

	if not summary["dry_run"]:
		_delete_in_chunks("Deleted Document", [name for name, _deleted_doctype in rows])
		if cint(optimize):
			frappe.db.sql("OPTIMIZE TABLE `tabDeleted Document`")
		summary["after"] = _table_size("Deleted Document")

	return _finish(summary)


def purge_malformed_versions(dry_run=1, ref_doctypes=None):
	"""Delete Versions whose ``data`` is a Python repr instead of JSON (default: BOM, Item)."""
	ref_doctypes = _parse_list(ref_doctypes) or list(DEFAULT_VERSION_DOCTYPES)
	summary = _start(
		"malformed_versions",
		dry_run,
		ref_doctypes=ref_doctypes,
		before=_table_size("Version"),
	)

	rows = _malformed_version_rows(ref_doctypes)
	summary["rows"] = dict(Counter(ref_doctype for _name, ref_doctype in rows))

	if not summary["dry_run"]:
		_delete_in_chunks("Version", [name for name, _ref_doctype in rows])
		summary["after"] = _table_size("Version")

	return _finish(summary)


def _deleted_document_rows(deleted_doctypes):
	"""(name, deleted_doctype) of the Deleted Document rows holding copies of these doctypes."""
	table = frappe.qb.DocType("Deleted Document")
	return (
		frappe.qb.from_(table)
		.select(table.name, table.deleted_doctype)
		.where(table.deleted_doctype.isin(deleted_doctypes))
		.run()
	)


def _malformed_version_rows(ref_doctypes):
	"""(name, ref_doctype) of the Versions whose ``data`` is a Python repr, not JSON."""
	table = frappe.qb.DocType("Version")
	return (
		frappe.qb.from_(table)
		.select(table.name, table.ref_doctype)
		.where(table.ref_doctype.isin(ref_doctypes))
		.where(table.data.like(MALFORMED_VERSION_PREFIX + "%"))
		.run()
	)


def _delete_in_chunks(doctype, names):
	for start in range(0, len(names), CHUNK_SIZE):
		frappe.db.delete(doctype, {"name": ("in", names[start : start + CHUNK_SIZE])})
		frappe.db.commit()


def _start(task, dry_run, **details):
	return {"task": task, "dry_run": cint(dry_run), "started": now(), **details}


def _finish(summary):
	summary["finished"] = now()
	title = f"{TITLE}: {summary['task']}" + (" (dry run)" if summary["dry_run"] else "")
	frappe.log_error(title=title, message=frappe.as_json(summary, indent=1))
	frappe.db.commit()
	return summary


def _table_size(doctype):
	"""Estimated rows and on-disk GB of the doctype's table, from information_schema."""
	if frappe.db.db_type != "mariadb":
		return None

	rows = frappe.db.sql(
		"""SELECT TABLE_ROWS AS estimated_rows, DATA_LENGTH AS data_bytes,
			INDEX_LENGTH AS index_bytes, DATA_FREE AS free_bytes
		FROM information_schema.TABLES
		WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s""",
		(f"tab{doctype}",),
		as_dict=True,
	)
	if not rows:
		return None

	size = rows[0]
	return {
		"estimated_rows": size.estimated_rows,
		"data_gb": _gb(size.data_bytes),
		"index_gb": _gb(size.index_bytes),
		"free_gb": _gb(size.free_bytes),
	}


def _gb(value):
	return round((value or 0) / 1024**3, 2)


def _parse_list(value):
	if not value:
		return []

	if isinstance(value, str):
		value = (
			frappe.parse_json(value)
			if value.lstrip().startswith("[")
			else value.split(",")
		)

	return [item.strip() for item in value if item and item.strip()]
