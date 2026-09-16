# Copyright (c) 2026, Nirali and contributors
# For license information, please see license.txt

"""Install-time schema provisioning -- the C04 fix.

THE PROBLEM, MEASURED
---------------------
A fresh ``bench new-site`` + ``install-app jewellery_erpnext`` + ``migrate`` produces a site
on which this app's own schema does not exist. Verified on a clean site:

=========================================  ==========================
Object                                     Present after install+migrate
=========================================  ==========================
``Warehouse.department``                   no
``Stock Entry._customer``                  no
``Batch.custom_customer``                  no
``Batch.custom_inventory_type``            no
``Stock Entry Detail.inventory_type``      no
``Stock Entry Detail.custom_pure_qty``     no
``Stock Entry.custom_gold_rate_per_gram``  no
=========================================  ==========================

698 Custom Fields existed (erpnext / india_compliance / gke); none of this app's 42
``custom_fields/*.json`` files had been applied. The app is not merely incomplete on a fresh
site -- it is non-functional: creating a Warehouse raises
``AttributeError: 'Warehouse' object has no attribute 'department'``, and
``create_test_data.setup_data`` -- the documented bootstrap -- fails on that same line.

THREE INDEPENDENT CAUSES
------------------------
1. ``custom_fields/*.json`` is inert. It would be loaded by ``after_migrate``, which is
   commented out at ``hooks.py:12``.
2. Patches are MARKED, not RUN, on a fresh install. ``frappe/installer.py`` calls
   ``set_all_patches_as_completed(app)``, which inserts a ``Patch Log`` row per entry in
   ``patches.txt`` without importing the module. Confirmed: the ``Patch Log`` row for
   ``add_customer_gold_rate_snapshot_fields`` exists on the fresh site while the fields it
   creates do not.
3. The remaining route, ``create_test_data.setup_data``, is a test bootstrap -- not
   something a real installation runs -- and is itself broken by cause 1.

THE FIX
-------
One entry point, wired to ``after_install``. It calls ``create_custom_fields`` with
``update=False`` -- create-if-missing, never modify what is already there.

PRESERVATION, NOT CONVERGENCE -- and why an earlier version of this docstring was dangerous
-------------------------------------------------------------------------------------------
This docstring used to say "running it on an already-provisioned site is a no-op" and offer::

    bench --site <site> execute jewellery_erpnext.install.provision_schema

as a repair path. **Both claims were wrong, and together they invited damage on a live site.**

``create_custom_fields`` defaults to ``update=True``
(``frappe/custom/doctype/custom_field/custom_field.py:322``). On an existing field that branch
builds the doc from the DB row, calls ``custom_field.update(df)`` -- a MERGE over the keys named
in the JSON -- and saves when anything differs. So every property this app's JSON names is
overwritten with the repo's value, discarding whatever a site had legitimately customised.
Measured on ``gk``: 686 of 1693 fields would have been rewritten.

Note the second edge, which cuts the other way: because it MERGES rather than replaces, it also
cannot heal drift in properties the JSON omits. So the old text was wrong in both directions --
it overwrote what it should have preserved, and could not repair what it claimed to repair.

The rule now: **provisioning creates; it never modifies.** Incompatible drift is reported by
``report_field_drift`` and is a human decision, not a silent rewrite. Approved field migrations
must be narrow, explicit and separately tested.

Deliberately NOT wired to ``after_migrate``: that hook is disabled for reasons recorded at
``hooks.py:12`` and re-enabling it would load far more than this.
"""

import json
import os

import frappe
from frappe.custom.doctype.custom_field.custom_field import (
	create_custom_fields,
	get_existing_custom_fields,
)

CUSTOM_FIELDS_DIR = os.path.join(
	os.path.dirname(os.path.abspath(__file__)), "jewellery_erpnext", "custom_fields"
)

#: Fieldtypes whose ``options`` names another DocType that must exist first.
#:
#: The spelling matters: Frappe's fieldtype is ``"Table MultiSelect"`` WITH a space
#: (``frappe/model/__init__.py:60,101``). An earlier revision of this tuple wrote
#: ``"TableMultiSelect"``, which matches no fieldtype at all, so the 25 Table MultiSelect
#: fields across ``customer.json``, ``item.json``, ``material_request.json``,
#: ``quotation.json``, ``sales_order.json`` and ``stock_entry_type.json`` were never
#: checked. They happen to target fixture-only doctypes this app owns, so nothing broke --
#: but the guard was not doing its job for that fieldtype.
_DOCTYPE_LINK_TYPES = ("Link", "Table", "Table MultiSelect")


def after_install():
	"""Entry point for ``hooks.py``'s ``after_install``."""
	provision_schema()


def _fieldnames_owned_by_another_apps_fixtures():
	"""``{(dt, fieldname)}`` that some OTHER app on this bench ships as a ``fixtures`` record.

	THE BUG THIS EXISTS TO PREVENT, MEASURED IN CI
	-----------------------------------------------
	Two apps can declare the same field on the same DocType through different mechanisms, and
	they then collide at the Custom Field DOCUMENT level even though the schema they want is
	the same::

	    jewellery_erpnext/custom_fields/warehouse.json   -> Custom Field "Warehouse-department"
	    gke_customization/fixtures/custom_field.json     -> Custom Field "Warehouse-custom_department"

	Both carry ``fieldname = "department"`` on ``Warehouse``. ``custom_field.py``'s ``validate``
	rejects the second on ``(dt, fieldname)`` regardless of document name::

	    frappe.exceptions.ValidationError:
	        A field with the name department already exists in Warehouse

	Before ``provision_schema`` existed, this app's ``custom_fields/*.json`` were never applied
	(``after_migrate`` is commented out at ``hooks.py:12``), so the sibling's fixture always won
	and nothing ever collided. Provisioning them made this app create the field FIRST, and then
	``sync_fixtures`` -- which re-imports every app's fixtures on EVERY ``bench migrate`` --
	died importing the sibling's record. A fresh install could no longer migrate.

	WHY DEFER TO THE FIXTURE RATHER THAN RACE IT
	---------------------------------------------
	Ordering cannot be fixed: this app installs before ``gke_customization`` (``sites/apps.txt``),
	so its provisioning always runs before that app's fixtures exist, whether it is hooked on
	``after_install`` or ``after_sync``. And a fixture is re-imported on every migrate, so the
	sibling's record is the one that keeps being reasserted. The field still ends up on the
	DocType -- by the sibling's hand -- which is the outcome provisioning wanted anyway.

	Scans the bench's ``apps/`` DIRECTORY rather than any list of installed apps, because at
	``after_install`` time the sibling is typically not installed yet while its fixture is
	already on disk and certain to be imported later. See the comment on the scan itself.
	"""
	owned = set()

	# THE APPS DIRECTORY ON DISK, NOT ``apps.txt`` AND NOT THE INSTALLED LIST.
	#
	# This distinction is the whole fix, and CI proved it. Both ``frappe.get_all_apps()`` and
	# ``frappe.get_installed_apps()`` answer "what is installed SO FAR", and apps install one
	# at a time -- the CI log shows jewellery_erpnext installing BEFORE gke_customization. So
	# at this app's ``after_install`` the sibling is in neither list, its fixture is never
	# scanned, and the very field that collides gets provisioned anyway.
	#
	# Measured: reading apps.txt deferred 1280 fields on a fully-installed bench but only 185
	# in CI, and the migrate died on exactly the field that fell through that gap. The
	# directory is on disk from the start, so it answers the same on the first install and
	# the thousandth.
	try:
		apps_dir = os.path.dirname(os.path.dirname(frappe.get_app_path("frappe")))
		candidates = sorted(os.listdir(apps_dir))
	except Exception:  # noqa: BLE001 - unreadable bench layout; provisioning must still run
		return owned

	for app in candidates:
		if app == "jewellery_erpnext":
			continue
		path = os.path.join(apps_dir, app, app, "fixtures", "custom_field.json")
		if not os.path.isfile(path):
			continue
		try:
			with open(path) as handle:
				records = json.load(handle)
		except Exception:  # noqa: BLE001 - a malformed sibling fixture is not ours to fix
			continue
		if not isinstance(records, list):
			continue
		for record in records:
			if (
				isinstance(record, dict)
				and record.get("dt")
				and record.get("fieldname")
			):
				owned.add((record["dt"], record["fieldname"]))

	return owned


def _drop_fields_owned_by_another_apps_fixtures(spec, owned):
	"""Drop fields a sibling app's fixtures will create, so the two do not collide.

	See :func:`_fieldnames_owned_by_another_apps_fixtures` for the failure this avoids.
	"""
	usable = {}
	dropped = []

	for doctype, fields in spec.items():
		keep = []
		for field in fields:
			if (doctype, field.get("fieldname")) in owned:
				dropped.append(f"{doctype}.{field.get('fieldname')}")
				continue
			keep.append(field)

		if keep:
			usable[doctype] = keep

	return usable, dropped


def provision_schema(verbose=True):
	"""Apply every ``custom_fields/*.json`` file, then the schema patches.

	CREATE-ONLY. ``update=False`` means an existing Custom Field is left byte-for-byte alone,
	whatever the repo JSON says about it. Anything already present that differs is reported as
	drift and left for a human -- see the module docstring for why the previous
	overwrite-on-every-run behaviour was unsafe.

	Returns ``(applied, skipped, drift)`` so a caller can act on the drift rather than have it
	buried in stdout.
	"""
	applied = skipped = 0
	drift = []
	deferred = []
	owned_elsewhere = _fieldnames_owned_by_another_apps_fixtures()

	for filename in sorted(os.listdir(CUSTOM_FIELDS_DIR)):
		if not filename.endswith(".json"):
			continue

		with open(os.path.join(CUSTOM_FIELDS_DIR, filename)) as handle:
			spec = json.load(handle)

		usable, dropped = _drop_fields_with_missing_targets(spec)
		skipped += dropped

		usable, yielded = _drop_fields_owned_by_another_apps_fixtures(
			usable, owned_elsewhere
		)
		deferred.extend(yielded)

		if usable:
			drift.extend(report_field_drift(usable))
			create_custom_fields(usable, ignore_validate=True, update=False)
			applied += 1

	_run_schema_patches()

	if verbose:
		print(
			f"jewellery_erpnext: provisioned custom fields from {applied} file(s); "
			f"skipped {skipped} field(s) targeting doctypes that are not installed"
		)
		if deferred:
			print(
				f"jewellery_erpnext: deferred {len(deferred)} field(s) to the app whose "
				f"fixtures already own them -- creating them here would make that app's "
				f"fixture import fail on every migrate: " + ", ".join(sorted(deferred))
			)
		if drift:
			print(
				f"jewellery_erpnext: {len(drift)} existing field(s) DIFFER from this app's "
				f"definition and were left untouched. Review before relying on them:"
			)
			for item in drift[:20]:
				print(
					f"  {item['dt']}.{item['fieldname']}: "
					+ ", ".join(
						f"{k} site={v['site']!r} repo={v['repo']!r}"
						for k, v in item["differences"].items()
					)
				)
			if len(drift) > 20:
				print(f"  ... and {len(drift) - 20} more")

	return applied, skipped, drift, deferred


def after_sync():
	"""Entry point for ``hooks.py``'s ``after_sync``.

	``after_sync`` and not ``after_install``, deliberately. ``frappe/installer.py`` runs
	``after_install`` at ``:360`` but ``sync_fixtures`` only at ``:366`` -- so anything done from
	``after_install`` happens BEFORE any app's fixtures exist. That ordering is the direct cause of
	one of the fresh-install failures measured on this bench: provisioning created Table MultiSelect
	fields whose option ``Design Attribute - Multiselect`` had not been created yet, and the later
	``validate_fields_for_doctype`` raised during fixture sync. ``after_sync`` (``:371``) runs after
	fixtures, customizations and dashboards, which is the only point at which the site is whole.
	"""
	reconcile_cross_app_fixtures()


def reconcile_cross_app_fixtures(verbose=True):
	"""Create Custom Fields that a sibling app's fixture import silently skipped.

	WHY THIS APP REPAIRS ANOTHER APP'S FIXTURES -- and why that is not overreach
	---------------------------------------------------------------------------
	``frappe.utils.fixtures.import_fixtures`` wraps an ENTIRE FILE in one ``try/except``
	(``fixtures.py:44-48``), outside the per-record loop. The first record that raises
	``ImportError`` or ``DoesNotExistError`` therefore discards every record after it in that file
	-- reported by a bare ``print``, with no Error Log, and **exit code 0**.

	``gke_customization``'s ``custom_field.json`` hits that twice on a fresh site, measured:

	* ``BOM-custom_creation_docname`` (record 115) is a Dynamic Link whose target
	  ``custom_creation_doctype`` is record 629. Fixture records import in FILE ORDER --
	  ``import_doc(path, sort=True)`` sorts files in a directory, never records within a file
	  (``data_import.py:350-356``) -- so the Dynamic Link is validated before its target exists and
	  ``check_dynamic_link_options`` (``doctype.py:1412-1422``) throws.
	* 27 further records name DocTypes that no installed app ships, the earliest at file line 8645.

	Either abort strands ~1,900 declared Custom Fields. This app cannot reorder another app's file,
	and editing it is not this app's call -- but it CAN reconcile the SITE afterwards, which is the
	same thing ``provision_schema`` already does for this app's own inert ``custom_fields/*.json``.
	The boundary is deliberate: fields are reconciled, DocTypes are not (see ``verify_site_schema``).

	Per-record, never per-file. That is the entire point.
	"""
	created = skipped_present = skipped_target = failed = 0
	skipped_doctypes = set()
	failures = []
	skipped_reasons = {}

	apps_dir = os.path.dirname(os.path.dirname(frappe.get_app_path("frappe")))
	installed = set(frappe.get_installed_apps())
	module_app = {
		row.name: row.app_name
		for row in frappe.get_all("Module Def", fields=["name", "app_name"])
	}

	for app in frappe.get_installed_apps():
		fixture = os.path.join(apps_dir, app, app, "fixtures", "custom_field.json")
		if not os.path.exists(fixture):
			continue

		with open(fixture) as handle:
			try:
				records = json.load(handle)
			except ValueError:
				continue

		for record in records:
			name = record.get("name")
			if not name or frappe.db.exists("Custom Field", name):
				skipped_present += 1
				continue

			target = _unresolvable_target(record)
			if target:
				skipped_target += 1
				skipped_doctypes.add(target)
				skipped_reasons[name] = f"target DocType absent: {target}"
				continue

			# A record whose module belongs to an app this site does not have is not ours to
			# create: Custom Field.module is a Link to Module Def, so the insert fails on that
			# link anyway ("Could not find Module (for export)"). Classifying it here turns 74
			# identical stack traces into one counted, named category.
			module = record.get("module")
			if module and module_app.get(module) not in installed:
				skipped_target += 1
				skipped_reasons[
					name
				] = f"module belongs to an app that is not installed: {module}"
				continue

			# The fixture's fieldname already exists as a column on the parent -- typically a
			# stale record whose fieldname core has since taken for itself (a record named
			# `Warehouse-custom_department` whose fieldname is plain `department`). Creating it
			# raises "A field with the name X already exists", which is correct and not fixable
			# from here.
			try:
				collides = frappe.db.has_column(record["dt"], record.get("fieldname"))
			except Exception:  # noqa: BLE001 -- a missing table is already answered above
				collides = False
			if collides:
				skipped_target += 1
				skipped_reasons[name] = "fieldname already exists on the parent DocType"
				continue

			try:
				# The same call the fixture importer makes, one record at a time, so one bad
				# record costs one record instead of the rest of the file.
				frappe.get_doc(record).insert(ignore_permissions=True)
				created += 1
			except Exception as exc:  # noqa: BLE001 -- record every failure, never abort the run
				failed += 1
				failures.append((name, type(exc).__name__, str(exc)[:160]))

	if verbose:
		print(
			f"jewellery_erpnext: cross-app fixture reconciliation -- created {created}, "
			f"already present {skipped_present}, skipped {skipped_target} (classified), "
			f"failed {failed}"
		)
		buckets = {}
		for reason in skipped_reasons.values():
			head = reason.split(":")[0]
			buckets[head] = buckets.get(head, 0) + 1
		for reason, count in sorted(buckets.items(), key=lambda kv: -kv[1]):
			print(f"  skipped -- {reason}: {count}")
		if skipped_doctypes:
			print(
				f"  absent DocTypes ({len(skipped_doctypes)}): {', '.join(sorted(skipped_doctypes))}"
			)
		for name, kind, message in failures[:15]:
			print(f"  FAILED {name}: {kind}: {message}")
		if len(failures) > 15:
			print(f"  ... and {len(failures) - 15} more failures")

	return created, skipped_reasons, failures


def _unresolvable_target(record):
	"""The DocType name this record needs and cannot have, or ``None``.

	Two ways a Custom Field record can be unusable, and BOTH matter:

	* its ``dt`` names a DocType that is not installed -- this is what aborts the fixture file;
	* for a Link/Table/Table MultiSelect, its ``options`` names one. That is latent rather than
	  fatal at import time, because ``check_link_table_options`` returns early under
	  ``frappe.flags.in_fixtures`` (``doctype.py:1352-1354``) -- but it poisons the PARENT
	  doctype's meta afterwards, and a single such field makes every ``frappe.get_doc`` on that
	  parent raise ``ImportError``. Measured on this bench with ``Master Serial No Table``.
	"""
	dt = record.get("dt")
	if not dt or not frappe.db.exists("DocType", dt):
		return dt or "(unnamed)"

	options = record.get("options")
	if record.get("fieldtype") in _DOCTYPE_LINK_TYPES and options:
		if not frappe.db.exists("DocType", options):
			return options

	return None


def verify_site_schema(verbose=True):
	"""Assert this app's schema is actually present. THE INSTALL GATE.

	``bench install-app`` returning zero is not evidence. Frappe's fixture importer wraps a whole
	file in one ``try/except`` (``frappe/utils/fixtures.py:44-48``) and reports a skipped file with
	a bare ``print`` -- so an install can drop hundreds of Custom Fields, exit 0, and look fine.
	``run-tests`` has the same shape: it exits 0 on a site without ``allow_tests``.

	So the recipe installs, migrates, and then calls this. Raises on the first missing object, with
	the object named. Safe to run repeatedly; it writes nothing.
	"""
	missing = []

	def require(kind, name, present):
		if not present:
			missing.append(f"{kind}: {name}")

	# --- this app's own DocTypes, including the two the Customer Gold work added -----------
	for doctype in (
		"Subcontracting Settings",
		"Subcontracting Log",
		"Sketch Order",
		"Sketch Order Form",
	):
		require("DocType", doctype, frappe.db.exists("DocType", doctype))

	# --- the cross-app DocType whose absence silently aborts fixture sync -------------------
	require(
		"DocType (gke_customization)",
		"Design Attribute - Multiselect",
		frappe.db.exists("DocType", "Design Attribute - Multiselect"),
	)

	# --- fields this app cannot function without ------------------------------------------
	for doctype, fieldname in (
		("Warehouse", "department"),
		("Stock Entry", "_customer"),
		("Stock Entry Detail", "inventory_type"),
		("Stock Entry Detail", "custom_pure_qty"),
		("Stock Entry", "custom_gold_rate_per_gram"),
		("Batch", "custom_customer"),
		("Batch", "custom_inventory_type"),
		("Subcontracting Settings", "enable_customer_gold_flow"),
		("Sketch Order Form", "workflow_state"),
	):
		present = frappe.db.exists("DocType", doctype) and frappe.get_meta(
			doctype
		).has_field(fieldname)
		require("Field", f"{doctype}.{fieldname}", present)

	# --- a gke-owned field that sits AFTER the abort point --------------------------------
	# gke_customization's custom_field.json aborts at record 115 on a fresh site (a Dynamic Link
	# ordered before its target). Everything after that reaches the site only through
	# reconcile_cross_app_fixtures. This field is well past the abort, so its presence is a live
	# assertion that the reconciliation ran and worked -- not merely that gke installed.
	require(
		"Field (reconciled from gke_customization)",
		"BOM.custom_creation_docname",
		frappe.db.exists("Custom Field", "BOM-custom_creation_docname"),
	)

	# --- known gap, owned elsewhere, reported rather than repaired -------------------------
	# gke_customization's fixtures/doctype.json ships `Demo Work` and `Gold MRP Preset` with
	# `amended_from` listed TWICE, which frappe rejects with UniqueFieldnameError. Both are
	# is_submittable, so frappe would add that field itself (doctype.py:926-937) -- the explicit
	# duplicates are redundant as well as invalid. Creating another app's DocTypes from here
	# would be overreach, so this is surfaced, not fixed. Neither DocType is used by this app.
	unreachable = [
		name
		for name in ("Demo Work", "Gold MRP Preset")
		if not frappe.db.exists("DocType", name)
	]
	if unreachable and verbose:
		print(
			"jewellery_erpnext: NOTE -- {0} could not be installed by gke_customization "
			"(duplicate `amended_from` in its fixtures/doctype.json). Not used by this app; "
			"reported for the owning team.".format(", ".join(unreachable))
		)

	# --- every fixture record this app ships must have landed -------------------------------
	fixture = os.path.join(
		os.path.dirname(os.path.abspath(__file__)),
		"jewellery_erpnext",
		"fixtures",
		"custom_field.json",
	)
	if os.path.exists(fixture):
		with open(fixture) as handle:
			for record in json.load(handle):
				require(
					"Fixture Custom Field",
					record["name"],
					frappe.db.exists("Custom Field", record["name"]),
				)

	if missing:
		frappe.throw(
			"Site schema verification FAILED -- {0} object(s) missing:\n  {1}".format(
				len(missing), "\n  ".join(missing)
			)
		)

	if verbose:
		print(
			f"jewellery_erpnext: site schema verified -- {frappe.db.count('Custom Field')} Custom Fields present"
		)

	return True


def report_field_drift(spec):
	"""Which already-present fields differ from this app's definition. READ-ONLY.

	Built on ``get_existing_custom_fields`` (``custom_field.py:391-401``), which is a plain
	``frappe.get_all`` over the same ``{doctype: [df, ...]}`` shape ``create_custom_fields``
	takes. Comparing only the keys the repo JSON actually names reproduces
	``create_custom_fields``' own merge semantics -- so this reports exactly what an
	``update=True`` run WOULD have overwritten, without writing anything.

	A field that is absent is not drift; it is simply about to be created.
	"""
	existing = get_existing_custom_fields(spec)
	drift = []

	for doctype, fields in spec.items():
		for field in fields:
			current = existing.get((doctype, field.get("fieldname")))
			if not current:
				continue

			differences = {
				key: {"site": current.get(key), "repo": value}
				for key, value in field.items()
				if key not in ("dt", "fieldname") and current.get(key) != value
			}
			if differences:
				drift.append(
					{
						"dt": doctype,
						"fieldname": field.get("fieldname"),
						"differences": differences,
					}
				)

	return drift


def _drop_fields_with_missing_targets(spec):
	"""Drop fields whose Link/Table target DocType is not installed.

	``custom_fields/company.json`` carries ``loan_classification_ranges``, a Table field
	pointing at ``Loan Classification Range`` -- a DocType owned by the ``lending`` app. On
	a site without ``lending`` that field is created anyway and then breaks anything that
	loads the Company meta, including the whole test runner::

	    ImportError: Module import failed for Loan Classification Range

	This app declares no ``required_apps``, so an installation without ``lending`` is
	legitimate. Such fields are therefore skipped rather than allowed to poison the schema.
	"""
	usable = {}
	dropped = 0

	for doctype, fields in spec.items():
		keep = []
		for field in fields:
			target = field.get("options")
			if (
				field.get("fieldtype") in _DOCTYPE_LINK_TYPES
				and target
				and _target_belongs_to_an_uninstalled_app(target)
			):
				dropped += 1
				continue
			keep.append(field)

		if keep:
			usable[doctype] = keep

	return usable, dropped


def _target_belongs_to_an_uninstalled_app(doctype):
	"""True only when ``doctype`` is shipped by an app that is NOT installed.

	The rule is deliberately this narrow, and it took two wrong attempts to get right:

	* Skipping whenever ``frappe.db.exists("DocType", ...)`` is false drops valid fields,
	  because ``after_install`` runs before this app's own doctypes are all visible.
	* Skipping whenever no app FOLDER ships the doctype is worse -- several of this app's
	  doctypes are fixture-only and have no folder at all. ``Inventory Type`` is one, and
	  dropping its Links cost 900 custom fields on a measured fresh install.

	So the question is not "does this exist yet" but "is this owned by something we do not
	have". Only a folder found under a NON-installed app answers yes. A doctype that exists
	nowhere on disk is assumed to be fixture-only and kept.
	"""
	if frappe.db.exists("DocType", doctype):
		return False

	scrubbed = frappe.scrub(doctype)
	installed = set(frappe.get_installed_apps())
	apps_dir = os.path.dirname(os.path.dirname(frappe.get_app_path("frappe")))

	try:
		candidates = os.listdir(apps_dir)
	except OSError:
		return False

	for app in candidates:
		if app in installed:
			continue
		app_root = os.path.join(apps_dir, app, app)
		if not os.path.isdir(app_root):
			continue
		for module in os.listdir(app_root):
			doctype_dir = os.path.join(app_root, module, "doctype", scrubbed)
			# A DIRECTORY IS NOT PROOF THE DOCTYPE SHIPS. The schema lives in
			# ``<scrubbed>/<scrubbed>.json``; without it there is nothing to install, whatever
			# else the folder holds. Three real cases on this bench have a folder and no JSON:
			# ``bom_scrap_item`` (ERPNext v16 dropped it, leaving a stale ``__pycache__``),
			# ``product_return_form`` (``__pycache__`` only) and ``interview_round`` (ships .js
			# and .py but no .json). Treating those as "shipped by an uninstalled app" would
			# drop valid fields on a site that legitimately lacks the app -- the same class of
			# error as attempt 2 above, which cost 900 fields.
			if os.path.isfile(os.path.join(doctype_dir, f"{scrubbed}.json")):
				return True

	return False


def _run_schema_patches():
	"""Run the schema patches a fresh install records but never executes.

	Listed explicitly rather than discovered: only patches that CREATE SCHEMA belong here.
	A data-migration patch must not run on an empty site.
	"""
	from jewellery_erpnext.patches.add_batch_component_field import (
		execute as add_batch_component_field,
	)
	from jewellery_erpnext.patches.add_customer_gold_rate_snapshot_fields import (
		execute as add_rate_snapshot_fields,
	)

	add_rate_snapshot_fields()
	add_batch_component_field()
