"""
Idempotent safeguard that guarantees every ``custom_*`` column referenced by a
jewellery_erpnext ``fetch_from`` actually EXISTS on its target doctype.

Frappe resolves every Link field's ``fetch_from`` on each save/submit inside
``get_invalid_links`` -> ``frappe.db.get_value(target_doctype, name, [..., target_column])``
(see ``frappe/model/base_document.py``). If the target column is missing, MariaDB raises
``(1054, "Unknown column ...")`` and the whole save aborts -- even when the fetched field is
never touched. The crash is about *schema*, not values, so merely guaranteeing the column
exists fully prevents it without removing any ``fetch_from``.

Why the gap exists: this app's ``custom_fields/*.json`` are only applied by
``migrate.after_migrate()``, and that hook is intentionally disabled (hooks.py). So fields
declared only there never reach real sites; they exist only if a dedicated patch creates them
-- the recurring patch-only custom-field gap. This guard closes it generically for the whole
class of cross-app ``fetch_from`` dependencies.

Wired in two places (both idempotent): a ``post_model_sync`` patch (existing-site migrate) and
``create_test_data.setup_data`` (fresh / CI sites). Can also be run ad-hoc:

    bench --site <site> execute jewellery_erpnext.fetch_from_guard.ensure_fetch_from_columns
"""

import json
import os

import frappe

APP_MODULE = "Jewellery Erpnext"


def ensure_fetch_from_columns():
	"""Create any missing ``custom_*`` column targeted by an app ``fetch_from``.

	Returns the list of ``"<doctype>.<column>"`` strings it created (empty when everything
	already exists -- the steady state).
	"""
	declared = _load_declared_custom_fields()
	created = []

	for _source_dt, _link_field, target_dt, target_column in get_custom_fetch_targets():
		if frappe.db.has_column(target_dt, target_column):
			continue

		_ensure_column(target_dt, target_column, declared)

		if frappe.db.has_column(target_dt, target_column):
			created.append(f"{target_dt}.{target_column}")

	if created:
		frappe.db.commit()
		frappe.logger().info(
			"ensure_fetch_from_columns: created missing columns -> "
			+ ", ".join(sorted(set(created)))
		)

	return created


def get_custom_fetch_targets():
	"""Return resolvable (source_dt, link_field, target_dt, target_column) tuples for every
	app ``fetch_from`` whose target is a ``custom_*`` column.

	Shared by the guard and its regression test. Only ``custom_*`` columns are included --
	standard columns are owned by their app's schema sync, and acting on them would mask real
	problems. Unresolvable links (non-Link field, missing field, Dynamic Link) are dropped.
	"""
	declared = _load_declared_custom_fields()
	targets = []
	seen = set()

	for source_dt, link_field, target_column in _collect_candidates(declared):
		if not target_column.startswith("custom_"):
			continue
		target_dt = _resolve_target_doctype(source_dt, link_field)
		if not target_dt:
			continue
		key = (target_dt, target_column)
		if key in seen:
			continue
		seen.add(key)
		targets.append((source_dt, link_field, target_dt, target_column))

	return targets


def _ensure_column(target_dt, target_column, declared):
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	df_def = _resolve_field_def(target_dt, target_column, declared)

	# update=False: never overwrite a Custom Field doc another app (e.g. gke_customization)
	# owns -- we only fill gaps. When the doc is absent this creates it (and its column).
	create_custom_fields({target_dt: [df_def]}, ignore_validate=True, update=False)

	# Safety net for the doc-present / column-absent case: create_custom_fields is a no-op
	# when the Custom Field doc already exists, so the physical column can still be missing.
	# Force a schema sync from the (now-present) meta to add it.
	if not frappe.db.has_column(target_dt, target_column):
		frappe.clear_cache(doctype=target_dt)
		frappe.db.updatedb(target_dt)


def _resolve_field_def(target_dt, target_column, declared):
	# 1) an existing Custom Field doc (e.g. installed by the gke_customization fixture) is the
	#    source of truth for fieldtype/label even when its physical column got lost.
	existing = frappe.db.get_value(
		"Custom Field",
		{"dt": target_dt, "fieldname": target_column},
		["fieldtype", "label", "options", "insert_after"],
		as_dict=True,
	)
	if existing:
		return {
			"fieldname": target_column,
			"fieldtype": existing.fieldtype or "Data",
			"label": existing.label or _prettify(target_column),
			"options": existing.options,
			"insert_after": existing.insert_after,
		}

	# 2) this app's own declaration in custom_fields/*.json
	df = declared.get((target_dt, target_column))
	if df:
		return dict(df)

	# 3) safe default -- a Data column is enough to make the fetch SELECT valid
	return {
		"fieldname": target_column,
		"fieldtype": "Data",
		"label": _prettify(target_column),
	}


def _collect_candidates(declared):
	"""Return a de-duplicated list of (source_doctype, link_field, target_column).

	Sources: (a) live meta of every doctype in this module (incl. child / istable -- child
	rows run _validate_links too), and (b) fetch_from declared on standard doctypes via this
	app's custom_fields/*.json (those columns share the same not-synced fragility).
	"""
	seen = set()
	out = []

	def add(source_dt, fetch_from):
		if not fetch_from or "." not in fetch_from:
			return
		link_field, target_column = fetch_from.split(".", 1)
		key = (source_dt, link_field, target_column)
		if key not in seen:
			seen.add(key)
			out.append(key)

	for dt in frappe.get_all("DocType", filters={"module": APP_MODULE}, pluck="name"):
		try:
			meta = frappe.get_meta(dt)
		except Exception:
			continue
		for df in meta.fields:
			if df.get("fetch_from"):
				add(dt, df.fetch_from)

	for (dt, _fieldname), df in declared.items():
		if df.get("fetch_from"):
			add(dt, df["fetch_from"])

	return out


def _resolve_target_doctype(source_dt, link_field):
	try:
		meta = frappe.get_meta(source_dt)
	except Exception:
		return None

	df = meta.get_field(link_field)
	if not df or df.fieldtype != "Link":
		# Dynamic Link targets vary at runtime; a non-Link "link_field" is dead config.
		return None

	return df.options


def _load_declared_custom_fields():
	"""Map (dt, fieldname) -> field def, read from this app's custom_fields/*.json."""
	declared = {}
	path = os.path.join(os.path.dirname(__file__), "jewellery_erpnext", "custom_fields")
	for file in os.listdir(path):
		if not file.endswith(".json"):
			continue
		with open(os.path.join(path, file)) as f:
			data = json.load(f)
		for dt, fields in data.items():
			for df in fields:
				if df.get("fieldname"):
					declared[(dt, df["fieldname"])] = df
	return declared


def _prettify(fieldname):
	name = fieldname
	if name.startswith("custom_"):
		name = name[len("custom_") :]
	return name.replace("_", " ").strip().title()
