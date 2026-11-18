"""C-01 integration harness (lock_order RULE F) for a THROWAWAY site restored from a backup.

Not an auto-discovered test (leading underscore). Every submit is rolled back unless a step says
it commits; the site must be ``kggk-pi-test.localhost`` with ``allow_tests`` on. Results are JSON
on stdout. Run on the baseline build and on the C-01 branch to get before/after evidence.

Site setup (once, by hand): new site + its own database, restore the post-incident backup,
``allow_tests 1``, scheduler disabled, ``mute_emails 1``, gke_customization installed (otherwise
Manufacturing Operation naming takes a ``MOP-.YY.MM.-`` counter).

    bench --site kggk-pi-test.localhost execute \\
        jewellery_erpnext.jewellery_erpnext.tests._c01_harness.build_fixtures --kwargs "{'commit': 1}"
    bench --site kggk-pi-test.localhost execute \\
        jewellery_erpnext.jewellery_erpnext.tests._c01_harness.run_scenarios

Scenarios (D1 4.2): I0 smoke, I1 no-mint receive, I2 mould no-mint receive, I3 Process-Loss-only,
I4 gain, I5 finding, I6 Product Certification receive, I7 Serial Number Creator without loss,
I8 negative control (I3 with the predicate forced to nothing, guard off then on). Fixtures that the
backup cannot supply are reported as skipped with the reason.

Output equivalence (T-11, D1 4.4): ``capture`` / ``diff`` below. Commit the fixture drafts, take
snapshot S, then on each build: submit + commit the fixtures serially, ``capture``, restore S.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from time import monotonic
from unittest.mock import patch

import frappe
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext import lock_order
from jewellery_erpnext.jewellery_erpnext.tests.naming_lock_probe import (
	NamingLockProbe,
	assert_naming_order,
	stock_entry_counter_keys,
)

THROWAWAY_SITE = "kggk-pi-test.localhost"
FIXTURE_TAG = "C01-HARNESS"  # written into the _user_tags of the drafts we build
EIR_SCENARIOS = ("I1", "I2", "I3", "I4", "I5")


def require_throwaway_site():
	site = frappe.local.site
	if site != THROWAWAY_SITE:
		raise RuntimeError(f"C-01 harness runs only on {THROWAWAY_SITE}, not {site!r}")
	if not frappe.conf.get("allow_tests"):
		raise RuntimeError(f"{site}: set allow_tests before running the C-01 harness")


def environment():
	"""Facts the report must state next to every result."""
	# MariaDB >= 11.1 (production 11.8) has transaction_isolation; 10.x only tx_isolation.
	try:
		isolation = frappe.db.sql("SELECT @@transaction_isolation")[0][0]
	except Exception:
		isolation = frappe.db.sql("SELECT @@tx_isolation")[0][0]
	return {
		"site": frappe.local.site,
		"apps": frappe.get_installed_apps(),
		"gke_installed": "gke_customization" in frappe.get_installed_apps(),
		"isolation": isolation,
		"use_read_committed": bool(frappe.conf.get("use_read_committed")),
		"mariadb": frappe.db.sql("SELECT VERSION()")[0][0],
	}


# ---------------------------------------------------------------------------
# Counter resolution
# ---------------------------------------------------------------------------
def expected_counters(company, stock_entry_types):
	"""The ``[(kind, key)]`` preallocate_series_for_docs takes for these types, in its order:
	rule rows sorted by name, then tabSeries rows sorted by prefix."""
	rules, series = set(), set()
	for stub in lock_order.series_stubs(company, *sorted(set(stock_entry_types))):
		rule = lock_order.document_naming_rule_for_doc(stub)
		if rule:
			rules.add(rule)
		else:
			prefix = lock_order.series_prefix_for_doc(stub)
			if prefix:
				series.add(prefix)
	return [("rule", r) for r in sorted(rules)] + [
		("series", s) for s in sorted(series)
	]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _open_issues(company, limit=200):
	"""Submitted Issues whose every operation is still WIP with the issued employee and is its
	work order's current operation -- i.e. a Receive can still be built from them."""
	return frappe.db.sql(
		"""
		SELECT e.name, e.company, e.department, e.operation, e.employee, e.subcontractor,
		       e.subcontracting,
		       IFNULL(dop.is_raw_material, 0) AS is_raw_material,
		       IFNULL(dop.is_finding_repack_requirement, 0) AS is_finding_repack,
		       IFNULL(dop.is_mould_manufacturer, 0) AS is_mould
		FROM `tabEmployee IR` e
		JOIN `tabDepartment Operation` dop ON dop.name = e.operation
		WHERE e.type = 'Issue' AND e.docstatus = 1 AND e.company = %(company)s
		  AND NOT EXISTS (
		      SELECT 1 FROM `tabEmployee IR Operation` o
		      JOIN `tabManufacturing Operation` m ON m.name = o.manufacturing_operation
		      JOIN `tabManufacturing Work Order` w ON w.name = o.manufacturing_work_order
		      WHERE o.parent = e.name AND o.parenttype = 'Employee IR'
		        AND (m.status != 'WIP' OR w.manufacturing_operation != m.name
		             OR IFNULL(m.operation, '') != e.operation)
		  )
		ORDER BY e.creation DESC
		LIMIT %(limit)s
		""",
		{"company": company, "limit": limit},
		as_dict=True,
	)


def tag_draft(name, tag):
	"""Tag an Employee IR in the standard ``_user_tags`` column (Employee IR has no remarks
	field on KG GK, so a ``remarks`` value would be silently dropped)."""
	frappe.db.sql(
		"UPDATE `tabEmployee IR` SET `_user_tags` = %s WHERE name = %s",
		(f",{tag}", name),
	)


def tagged_drafts(tag_prefix, docstatus=0):
	"""``[(name, tag)]`` of Receive Employee IRs whose tag starts with ``tag_prefix``, in
	creation order. ``docstatus=None`` returns every status."""
	condition = "" if docstatus is None else "AND docstatus = %(docstatus)s"
	rows = frappe.db.sql(
		f"""SELECT name, `_user_tags` FROM `tabEmployee IR`
		WHERE type = 'Receive' AND `_user_tags` LIKE %(like)s {condition}
		ORDER BY creation, name""",
		{"like": f",{tag_prefix}%", "docstatus": docstatus},
	)
	return [(name, (tags or "").lstrip(",").split(",")[0]) for name, tags in rows]


def build_receive_draft(issue, received_delta=0.0, finding=None):
	"""A Receive draft for every row of ``issue``: received = gross + ``received_delta``.

	``received_delta`` < 0 books a loss (Process Loss), > 0 a gain (when the operation is a raw
	material one). ``finding`` = ``(item_code, weight)`` fills slot 1 of the first row.
	"""
	rows = frappe.get_all(
		"Employee IR Operation",
		{"parent": issue.name, "parenttype": "Employee IR"},
		["manufacturing_operation", "manufacturing_work_order"],
		order_by="idx",
	)
	doc = frappe.new_doc("Employee IR")
	doc.update(
		{
			"type": "Receive",
			"company": issue.company,
			"department": issue.department,
			"operation": issue.operation,
			"subcontracting": issue.subcontracting,
			"employee": issue.employee,
			"subcontractor": issue.subcontractor,
		}
	)
	for i, r in enumerate(rows):
		gross = flt(
			frappe.db.get_value(
				"Manufacturing Operation", r.manufacturing_operation, "gross_wt"
			),
			3,
		)
		row = {
			"manufacturing_operation": r.manufacturing_operation,
			"manufacturing_work_order": r.manufacturing_work_order,
			"gross_wt": gross,
			"received_gross_wt": flt(gross + received_delta, 3),
		}
		if finding and i == 0:
			row.update({"finding_item1": finding[0], "finding_wt1": finding[1]})
		doc.append("employee_ir_operations", row)
	doc.insert(ignore_permissions=True)
	tag_draft(doc.name, FIXTURE_TAG)
	return doc.name


def build_fixtures(
	company="KG GK Jewellers Private Limited", commit=0, finding_item=None
):
	"""Build one Receive draft per EIR scenario from the open Issues; commit only if asked.

	I1 plain flat receive; I2 flat receive on a mould operation; I3 receive short by 0.01 g per
	row (Process Loss); I4 receive 0.05 g over on a raw-material operation (gain); I5 a finding on
	a finding-repack operation (``finding_item`` must be an F item the tree's metal can pour).
	"""
	require_throwaway_site()
	issues = _open_issues(company)
	wanted = {
		"I1": lambda i: not i.is_mould
		and not i.is_raw_material
		and not i.is_finding_repack,
		"I2": lambda i: i.is_mould,
		"I3": lambda i: not i.is_mould,
		"I4": lambda i: i.is_raw_material,
		"I5": lambda i: i.is_finding_repack,
	}
	deltas = {"I1": 0.0, "I2": 0.0, "I3": -0.01, "I4": 0.05, "I5": 0.0}
	used, built, skipped = set(), {}, {}
	for scenario, fits in wanted.items():
		issue = next((i for i in issues if i.name not in used and fits(i)), None)
		if not issue:
			skipped[scenario] = "no open Issue on a fitting operation in the backup"
			continue
		if scenario == "I5" and not finding_item:
			skipped[scenario] = "pass finding_item (an F item matching the tree metal)"
			continue
		try:
			built[scenario] = build_receive_draft(
				issue,
				deltas[scenario],
				finding=(finding_item, 0.05) if scenario == "I5" else None,
			)
			used.add(issue.name)
		except Exception as exc:
			frappe.db.rollback()
			skipped[scenario] = f"draft failed: {exc!r}"
	if commit:
		frappe.db.commit()
	else:
		frappe.db.rollback()
	out = {"built": built, "skipped": skipped, "committed": bool(commit)}
	print(json.dumps(out, indent=1, default=str))
	return out


def fixture_drafts():
	"""``{scenario: draft name}`` from the committed drafts, by build order."""
	names = [name for name, tag in tagged_drafts(FIXTURE_TAG) if tag == FIXTURE_TAG]
	return dict(zip(EIR_SCENARIOS, names))


# ---------------------------------------------------------------------------
# One probed submit
# ---------------------------------------------------------------------------
def probed_submit(doctype, name, *, mint_types=None, guard=True, snc_spy=False):
	"""Submit ``doctype/name`` inside the probe and roll it back. Returns the raw record."""
	record = {"doctype": doctype, "name": name}
	doc = frappe.get_doc(doctype, name)
	spy_result = {}
	with ExitStack() as stack:
		if mint_types is not None:
			from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
				receive_naming,
			)

			stack.enter_context(
				patch.object(
					receive_naming, "receive_mint_types", return_value=tuple(mint_types)
				)
			)
		if not guard:
			stack.enter_context(
				patch.object(
					lock_order,
					"guard_unplanned_stock_entry_counter",
					lambda *a, **k: None,
				)
			)
		if snc_spy:
			from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator import (
				serial_number_creator as snc_mod,
			)

			real = snc_mod._snc_may_book_loss

			def _spy(*args, **kwargs):
				spy_result["may_book_loss"] = real(*args, **kwargs)
				return spy_result["may_book_loss"]

			stack.enter_context(patch.object(snc_mod, "_snc_may_book_loss", _spy))
		probe = stack.enter_context(NamingLockProbe())
		started = monotonic()
		try:
			doc.submit()
			record["outcome"] = "submitted"
		except Exception as exc:
			record["outcome"] = f"{type(exc).__name__}: {exc}"
		record["seconds"] = round(monotonic() - started, 3)
	link = {
		"Employee IR": "employee_ir",
		"Serial Number Creator": "custom_serial_number_creator",
		"Product Certification": "product_certification",
	}[doctype]
	record["minted"] = sorted(
		frappe.get_all(
			"Stock Entry", {link: name, "docstatus": 1}, pluck="stock_entry_type"
		)
	)
	record["plan"] = sorted(
		t
		for _company, t in (
			frappe.flags.get(lock_order.PLANNED_COUNTERS_FLAG) or {}
		).get((doctype, name), set())
	)
	record["company"] = doc.company
	record.update(spy_result)
	record["probe"] = probe
	frappe.db.rollback()
	return record


def _judge(record, scenario_kind, expected, rules, series):
	probe = record.pop("probe")
	try:
		summary = assert_naming_order(probe, scenario_kind, expected, rules, series)
		record.update(passed=True, summary=summary)
	except AssertionError as exc:
		record.update(passed=False, why=str(exc), events=probe.report())
	return record


# ---------------------------------------------------------------------------
# Scenarios I0-I8
# ---------------------------------------------------------------------------
def run_scenarios(company="KG GK Jewellers Private Limited"):
	require_throwaway_site()
	env = environment()
	rules, series = stock_entry_counter_keys()
	drafts = fixture_drafts()
	results = {"environment": env, "scenarios": {}}

	# What each fixture is built to plan; a loss row may add Process Loss to a gain fixture.
	scenario_plan = {"I1": set(), "I2": set(), "I3": {"Process Loss"}}
	scenario_plan["I4"] = scenario_plan["I5"] = {
		"Material Transfer (WORK ORDER)",
		"Repack",
	}
	for scenario in EIR_SCENARIOS:
		name = drafts.get(scenario)
		if not name:
			results["scenarios"][scenario] = {"skipped": "no committed draft"}
			continue
		record = probed_submit("Employee IR", name)
		plan = set(record["plan"])
		record["I0_smoke"] = record["outcome"] == "submitted"
		record["plan_fits_scenario"] = scenario_plan[scenario] <= plan and (
			plan - scenario_plan[scenario] <= {"Process Loss"}
		)
		record["minted_within_plan"] = set(record["minted"]) <= plan
		results["scenarios"][scenario] = _judge(
			record, "eir", expected_counters(company, plan), rules, series
		)

	# I6: Product Certification Fire Assy / XRF receive drafts -- exact plan from what it mints.
	pcs = frappe.get_all(
		"Product Certification",
		{
			"docstatus": 0,
			"type": "Receive",
			"service_type": ["in", ["Fire Assy Service", "XRF Services"]],
		},
		pluck="name",
		limit=3,
	)
	for name in pcs or []:
		record = probed_submit("Product Certification", name)
		record["minted_equals_plan"] = set(record["minted"]) == set(record["plan"])
		results["scenarios"][f"I6:{name}"] = _judge(
			record,
			"pc",
			expected_counters(record["company"], record["plan"]),
			rules,
			series,
		)
	if not pcs:
		results["scenarios"]["I6"] = {"skipped": "no Fire Assy / XRF receive draft"}

	# I7: Serial Number Creator drafts -- Manufacture always, Repack only when the bound said so.
	sncs = frappe.get_all(
		"Serial Number Creator", {"docstatus": 0}, pluck="name", limit=3
	)
	for name in sncs or []:
		record = probed_submit("Serial Number Creator", name, snc_spy=True)
		types = ["Manufacture"] + (["Repack"] if record.get("may_book_loss") else [])
		record["plan_from_bound"] = set(record["plan"]) == set(types)
		record["minted_within_plan"] = set(record["minted"]) <= set(record["plan"])
		results["scenarios"][f"I7:{name}"] = _judge(
			record,
			"snc",
			expected_counters(record["company"], record["plan"]),
			rules,
			series,
		)
	if not sncs:
		results["scenarios"]["I7"] = {"skipped": "no Serial Number Creator draft"}

	# I8: negative control on the Process-Loss-only fixture.
	name = drafts.get("I3")
	if name:
		off = probed_submit("Employee IR", name, mint_types=(), guard=False)
		off = _judge(off, "eir", [], rules, series)
		on = probed_submit("Employee IR", name, mint_types=(), guard=True)
		on = _judge(on, "eir", [], rules, series)
		results["scenarios"]["I8"] = {
			# Guard off: the probe MUST flag MAT-STE- first taken after the Bins.
			"probe_is_sensitive": off.get("passed") is False
			and "after Bins" in (off.get("why") or ""),
			"guard_off": off,
			# Guard on: the late acquisition is NOWAIT, so the order check passes.
			"guard_on_passes": on.get("passed") is True,
			"guard_on": on,
		}
	print(json.dumps(results, indent=1, default=str))
	return results


# ---------------------------------------------------------------------------
# T-11: outputs unchanged (D1 4.4)
# ---------------------------------------------------------------------------
def capture(eir_names=None):
	"""Normalised outputs of the given (default: all fixture) receives, for :func:`diff`.

	Batches minted by the run are mapped to ``NEW#<item>#<n>`` and Stock Entry names are kept
	(serial execution must give identical numbers on both builds).
	"""
	require_throwaway_site()
	eir_names = eir_names or [
		name
		for name, tag in tagged_drafts(FIXTURE_TAG, docstatus=None)
		if tag == FIXTURE_TAG
	]
	new_batches, per_item = {}, {}
	run_entries = set(
		frappe.get_all("Stock Entry", {"employee_ir": ["in", eir_names]}, pluck="name")
	)

	def _batch(batch_no, item_code):
		"""A batch minted by this run's entries becomes NEW#<item>#<n>, numbered by first sight."""
		if not batch_no or batch_no in new_batches:
			return new_batches.get(batch_no, batch_no)
		if frappe.db.get_value("Batch", batch_no, "reference_name") in run_entries:
			per_item[item_code] = per_item.get(item_code, 0) + 1
			new_batches[batch_no] = f"NEW#{item_code}#{per_item[item_code]}"
			return new_batches[batch_no]
		return batch_no

	out = {}
	for eir in sorted(eir_names):
		ses = frappe.get_all(
			"Stock Entry",
			{"employee_ir": eir, "docstatus": 1},
			["name", "stock_entry_type", "purpose"],
			order_by="name",
		)
		entries, touched = [], set()
		for se in ses:
			items = frappe.get_all(
				"Stock Entry Detail",
				{"parent": se.name},
				[
					"item_code",
					"s_warehouse",
					"t_warehouse",
					"qty",
					"transfer_qty",
					"inventory_type",
					"customer",
					"batch_no",
				],
				order_by="idx",
			)
			for it in items:
				it["batch_no"] = _batch(it.pop("batch_no"), it.item_code)
				touched.update(
					{(it.item_code, it.s_warehouse), (it.item_code, it.t_warehouse)}
				)
			sle = frappe.db.sql(
				"""SELECT item_code, warehouse, SUM(actual_qty) q, SUM(stock_value_difference) v
				FROM `tabStock Ledger Entry` WHERE voucher_no = %s AND is_cancelled = 0
				GROUP BY item_code, warehouse ORDER BY item_code, warehouse""",
				se.name,
				as_dict=True,
			)
			gl = frappe.db.sql(
				"""SELECT account, SUM(debit) d, SUM(credit) c FROM `tabGL Entry`
				WHERE voucher_no = %s AND is_cancelled = 0 GROUP BY account ORDER BY account""",
				se.name,
				as_dict=True,
			)
			entries.append({**se, "items": items, "sle": sle, "gl": gl})
		rows = frappe.get_all(
			"Employee IR Operation",
			{"parent": eir},
			["manufacturing_operation", "manufacturing_work_order"],
			order_by="idx",
		)
		mops = []
		for r in rows:
			next_mop = frappe.db.get_value(
				"Manufacturing Operation",
				{"previous_mop": r.manufacturing_operation, "employee_ir": eir},
			)
			fields = [
				"gross_wt",
				"net_wt",
				"finding_wt",
				"diamond_wt",
				"gemstone_wt",
				"other_wt",
				"status",
			]
			mops.append(
				{
					"source": frappe.db.get_value(
						"Manufacturing Operation",
						r.manufacturing_operation,
						fields,
						as_dict=True,
					),
					"next": frappe.db.get_value(
						"Manufacturing Operation", next_mop, fields, as_dict=True
					)
					if next_mop
					else None,
					"mwo_points_to_next": frappe.db.get_value(
						"Manufacturing Work Order",
						r.manufacturing_work_order,
						"manufacturing_operation",
					)
					== next_mop,
					"sre": frappe.get_all(
						"Stock Reservation Entry",
						{
							"manufacturing_work_order": r.manufacturing_work_order,
							"docstatus": 1,
						},
						[
							"item_code",
							"warehouse",
							"reserved_qty",
							"delivered_qty",
							"status",
						],
						order_by="item_code, warehouse, reserved_qty",
					),
				}
			)
		bins = {
			f"{i}|{w}": frappe.db.get_value(
				"Bin",
				{"item_code": i, "warehouse": w},
				["actual_qty", "reserved_qty", "reserved_stock"],
				as_dict=True,
			)
			for i, w in sorted(t for t in touched if t[0] and t[1])
		}
		out[eir] = {
			"docstatus": frappe.db.get_value("Employee IR", eir, "docstatus"),
			"stock_entries": entries,
			"operations": mops,
			"bins": bins,
		}
	print(json.dumps(out, indent=1, default=str, sort_keys=True))
	return out


def diff(a, b, path=""):
	"""Differences between two :func:`capture` results (JSON-loaded or in memory)."""
	if isinstance(a, dict) and isinstance(b, dict):
		out = []
		for key in sorted(set(a) | set(b), key=str):
			out += diff(a.get(key), b.get(key), f"{path}/{key}")
		return out
	if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
		out = []
		for i, (x, y) in enumerate(zip(a, b)):
			out += diff(x, y, f"{path}[{i}]")
		return out
	if isinstance(a, float) or isinstance(b, float):
		return [] if abs(flt(a) - flt(b)) < 1e-9 else [(path, a, b)]
	return [] if a == b else [(path, a, b)]
