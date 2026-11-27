"""
Point attachment paths at the site that holds the files, so Frappe stops logging
"Error Attaching File" on every save.

The GK -> KGGK data migration copied GK's attachment *paths* (``/files/x.jpg``) into KGGK
Items and BOMs but never the *files*. Frappe core's ``attach_files_to_document``
(``frappe/core/doctype/file/utils.py``) runs on every ``on_update`` and
``on_update_after_submit`` of every doctype. For a relative path with no File row it inserts
one; ``File.before_insert`` reads the file from disk, gets ``FileNotFoundError``, and the hook
logs an Error Log row. That happens for every broken field on every save, because nothing
remembers the failure. An SNC submit copies the design BOM, inserts it and submits it, so it
logs two rows per broken field.

An absolute URL never raises it: core keeps it as a remote File and downloads nothing. So
when ``site_config.json`` sets ``foreign_attachment_origin`` (on KGGK,
``https://gkexport.frappe.cloud``):

* ``normalize_foreign_attachments`` is a ``before_validate`` hook (wired in hooks.py). It
  rewrites ``/files/x`` or ``/private/files/x`` to ``<origin>/files/x`` when ``x`` is not on
  this site's disk. ``before_validate`` runs on insert, save and submit, even with
  ``flags.ignore_validate``. It runs after ``fetch_from`` has copied ``item.image`` into
  ``BOM.image`` and before core's attach hook, so copies, REST writes, Server Scripts and desk
  saves are all covered.
* ``repair`` fixes the rows already stored, with ``frappe.db.set_value(update_modified=False)``.
  No hook runs, so it writes no Error Log, no Version and no ``modified``. It is the only fix
  for submitted documents: ``before_validate`` does not run on update after submit, and
  rewriting a field that is not allow_on_submit there would raise UpdateAfterSubmitError.
  Start it from System Console (Python)::

      frappe.call("jewellery_erpnext.foreign_attachments.enqueue_repair", dry_run=1)

Files that exist locally and values that are already absolute are never changed. A site
without the key is not touched at all.
"""

import os
import random
import time
from urllib.parse import quote, unquote

import frappe
from frappe import _
from frappe.utils import cint, get_files_path, get_url, now

ORIGIN_KEY = "foreign_attachment_origin"
ATTACH_FIELDTYPES = ("Attach", "Attach Image")

# Path prefix -> is_private, resolved the way File.get_full_path resolves them.
LOCAL_PREFIXES = (("/files/", False), ("/private/files/", True))

# hooks.py wires the guard to these doctypes; repair sweeps them unless told otherwise.
DOCTYPES = ("BOM", "Item", "Product Return Order", "Customer Design Information Sheet")

JOB_ID = "foreign-attachment-repair"
REPAIR_TITLE = "Foreign attachment repair"
PAGE_SIZE = 2000
SAMPLES_PER_DOCTYPE = 5
ORIGIN_CHECK_LIMIT = 300
ORIGIN_CHECK_SECONDS = 120


def get_origin():
	"""Return the configured origin without a trailing slash, or None when the guard is off."""
	origin = (frappe.conf.get(ORIGIN_KEY) or "").strip().rstrip("/")
	if not origin.startswith(("http://", "https://")):
		return None

	# Pointing a site at itself only turns its own missing files into dead absolute links.
	if origin == get_url().rstrip("/"):
		return None

	return origin


def is_missing_local_file(value):
	"""True when ``value`` is a ``/files/`` or ``/private/files/`` path whose file is not on disk.

	The path is also tried unquoted (File.before_insert stores it that way) and without a query
	string, so a link that works is never rewritten. Values that would point outside the files
	folder are left alone.
	"""
	if not isinstance(value, str):
		return False

	for prefix, is_private in LOCAL_PREFIXES:
		if value.startswith(prefix):
			break
	else:
		return False

	relative = value[len(prefix) :]
	candidates = {relative, unquote(relative)}
	candidates |= {candidate.split("?", 1)[0] for candidate in candidates}
	candidates = {
		candidate
		for candidate in candidates
		if candidate.strip("/") and ".." not in candidate.split("/")
	}
	if not candidates:
		return False

	return not any(
		os.path.isfile(get_files_path(*candidate.split("/"), is_private=is_private))
		for candidate in candidates
	)


def normalize_foreign_attachments(doc, method=None):
	"""``before_validate``: point attachment paths whose file is missing here at the origin."""
	origin = get_origin()
	if not origin:
		return

	for df in doc.meta.get("fields", {"fieldtype": ["in", ATTACH_FIELDTYPES]}):
		value = doc.get(df.fieldname)
		if is_missing_local_file(value):
			doc.set(df.fieldname, origin + value)


# POST only: a GET can be triggered by a link or an image in a logged-in System Manager's
# browser. System Console's frappe.call is not affected (in_safe_exec skips the method check).
@frappe.whitelist(methods=["POST"])
def enqueue_repair(dry_run=1, check_origin=0, doctypes=None):
	"""Queue ``repair`` on the long queue. System Manager only, one run at a time."""
	frappe.only_for("System Manager")

	job = frappe.enqueue(
		"jewellery_erpnext.foreign_attachments.repair",
		queue="long",
		timeout=4 * 60 * 60,
		job_id=JOB_ID,
		deduplicate=True,
		dry_run=cint(dry_run),
		check_origin=cint(check_origin),
		doctypes=doctypes,
	)
	if not job:
		return _("A foreign attachment repair is already queued or running.")

	return _("Queued. The summary will appear in Error Log as '{0}'.").format(
		REPAIR_TITLE
	)


def scan(doctypes=None, check_origin=0):
	"""Report what ``repair`` would change, without changing it."""
	return repair(dry_run=1, doctypes=doctypes, check_origin=check_origin)


def repair(dry_run=1, doctypes=None, check_origin=0):
	"""Rewrite stored attachment paths whose file is missing on this site, without any hook.

	Pages through each doctype by name, keeps the values ``is_missing_local_file`` flags and,
	unless ``dry_run``, rewrites them with ``frappe.db.set_value(update_modified=False)``,
	committing each page. ``check_origin`` also sends HEAD requests for a random sample of the
	missing paths to the origin and counts the replies. Returns the summary and records it as
	one Error Log; a dry run writes nothing else.
	"""
	dry_run = cint(dry_run)
	origin = get_origin()
	if not origin and not dry_run:
		frappe.throw(
			_("Set {0} in site_config.json before repairing.").format(ORIGIN_KEY)
		)

	summary = {"origin": origin, "dry_run": dry_run, "started": now(), "doctypes": {}}
	missing_paths = set()

	for doctype in _parse_doctypes(doctypes):
		summary["doctypes"][doctype] = _sweep_doctype(
			doctype, origin, dry_run, missing_paths
		)

	summary["totals"] = {
		key: sum(stats.get(key, 0) for stats in summary["doctypes"].values())
		for key in ("rows", "values", "private")
	}
	summary["totals"]["distinct_paths"] = len(missing_paths)

	if cint(check_origin) and origin:
		summary["origin_check"] = check_paths_on_origin(origin, missing_paths)

	summary["finished"] = now()

	title = REPAIR_TITLE + (" (dry run)" if dry_run else "")
	frappe.log_error(title=title, message=frappe.as_json(summary, indent=1))
	frappe.db.commit()

	return summary


def check_paths_on_origin(
	origin, paths, limit=ORIGIN_CHECK_LIMIT, budget_seconds=ORIGIN_CHECK_SECONDS
):
	"""HEAD a random sample of ``paths`` on ``origin`` and count the replies by status."""
	import requests

	sample = random.sample(sorted(paths), min(limit, len(paths)))
	deadline = time.monotonic() + budget_seconds
	counts = {}
	examples_404 = []
	checked = 0

	for path in sample:
		if time.monotonic() > deadline:
			break

		try:
			status = requests.head(
				origin + quote(path, safe="/"), timeout=10, allow_redirects=True
			).status_code
		except requests.RequestException as e:
			status = type(e).__name__

		counts[str(status)] = counts.get(str(status), 0) + 1
		if status == 404 and len(examples_404) < 10:
			examples_404.append(path)
		checked += 1

	return {
		"sample_size": len(sample),
		"checked": checked,
		"status_counts": counts,
		"examples_404": examples_404,
	}


def _sweep_doctype(doctype, origin, dry_run, missing_paths):
	stats = {"rows": 0, "values": 0, "private": 0, "fields": {}, "samples": []}

	fields = _attach_columns(doctype)
	if fields is None:
		stats["skipped"] = "not a regular doctype on this site"
		return stats
	if not fields:
		return stats

	select = ", ".join(f"`{fieldname}`" for fieldname in fields)
	where = " OR ".join(
		f"`{fieldname}` LIKE %(public)s OR `{fieldname}` LIKE %(private)s"
		for fieldname in fields
	)
	query = (
		f"SELECT `name`, {select} FROM `tab{doctype}`"
		f" WHERE `name` > %(after)s AND ({where}) ORDER BY `name` LIMIT %(limit)s"
	)
	values = {
		"public": "/files/%",
		"private": "/private/files/%",
		"limit": PAGE_SIZE,
		"after": "",
	}

	while True:
		rows = frappe.db.sql(query, values, as_dict=True)
		if not rows:
			break
		values["after"] = rows[-1].name

		for row in rows:
			updates = {}
			for fieldname in fields:
				value = row.get(fieldname)
				if not is_missing_local_file(value):
					continue

				updates[fieldname] = (origin or "") + value
				missing_paths.add(value)
				stats["fields"][fieldname] = stats["fields"].get(fieldname, 0) + 1
				if value.startswith("/private/"):
					stats["private"] += 1

			if not updates:
				continue

			stats["rows"] += 1
			stats["values"] += len(updates)
			if len(stats["samples"]) < SAMPLES_PER_DOCTYPE:
				stats["samples"].append({"name": row.name, "fields": sorted(updates)})

			if not dry_run:
				frappe.db.set_value(doctype, row.name, updates, update_modified=False)

		if not dry_run:
			frappe.db.commit()

	return stats


def _attach_columns(doctype):
	"""Attach fields of ``doctype`` that have a column, or None when it is not a regular doctype."""
	if not frappe.db.exists("DocType", doctype):
		return None

	meta = frappe.get_meta(doctype)
	if meta.issingle or meta.istable or meta.get("is_virtual"):
		return None

	columns = set(frappe.db.get_table_columns(doctype))
	return [
		df.fieldname
		for df in meta.get("fields", {"fieldtype": ["in", ATTACH_FIELDTYPES]})
		if df.fieldname in columns
	]


def _parse_doctypes(doctypes):
	if not doctypes:
		return list(DOCTYPES)

	if isinstance(doctypes, str):
		doctypes = (
			frappe.parse_json(doctypes)
			if doctypes.lstrip().startswith("[")
			else [doctypes]
		)

	return [doctype for doctype in doctypes if doctype]
