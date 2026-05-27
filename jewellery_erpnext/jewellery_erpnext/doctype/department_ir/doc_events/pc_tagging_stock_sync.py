# Copyright (c) 2024, Nirali and contributors
# For license information, please see license.txt
"""
Department IR Product Certification Stock Sync Service

Handles the two Product-Certification-centered Department IR scenarios:

  PC_TO_TAGGING_ISSUE    – Issue: current=PC, next=Tagging
                           Source = active SRE warehouse (not MOP Log from_warehouse)
                           Target = PC Transit WH
  TAGGING_TO_PC_RECEIVE  – Receive: current=PC, previous=Tagging
                           Source = PC Transit WH (MOP Log from_warehouse)
                           Target = PC WIP/Manufacturing WH

All other Tagging ↔ PC flows (Tagging→PC Issue, PC→Tagging Receive) are
NOT_APPLICABLE and do not create Stock Entries.

For each applicable scenario the service:
  1. Builds a ``MovementPlan`` from Department IR MOP Logs (qty/pcs intent).
  2. For Issue: resolves source warehouse from active Stock Reservation Entry
     (not from MOP Log from_warehouse, which may not hold the physical stock).
  3. Validates stock availability in the correct source warehouse.
  4. Creates Material Transfer Stock Entries (one per warehouse group).
  5. Creates Repack Stock Entries for Employee IR manual loss and MOP Log loss.
  6. Cancels old Stock Reservation Entries and creates replacements at the new
     warehouse with only the non-loss qty reserved.
  7. Syncs only the exact planned MOP Log names (prevents EOD duplication).
  8. On cancel: releases replacement SREs → cancels SEs → restores SREs.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	get_current_mop_balance_rows,
	recalculate_manufacturing_operation_weights,
)

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

TOLERANCE = 0.001

# Only PC-centered flows create Stock Entries.
# Tagging→PC Issue and PC→Tagging Receive are NOT_APPLICABLE.
SCENARIO_PC_TO_TAGGING_ISSUE = "PC_TO_TAGGING_ISSUE"
SCENARIO_TAGGING_TO_PC_RECEIVE = "TAGGING_TO_PC_RECEIVE"
SCENARIO_NOT_APPLICABLE = "NOT_APPLICABLE"

# Base department names (after stripping company-abbreviation suffix)
PC_DEPT_NAME = "Product Certification"
TAGGING_DEPT_NAME = "Tagging"

# ────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class TransferLineSpec:
	item_code: str
	batch_no: Optional[str]
	qty: float
	pcs: int
	s_warehouse: str
	t_warehouse: str
	uom: str
	manufacturing_operation: str  # mop_name — NOT a MOP Log name
	manufacturing_work_order: str
	source_mop_log_names: List[str]
	source_variant_of: str


@dataclass
class LossRepackSpec:
	item_code: str
	batch_no: Optional[str]
	loss_qty: float
	loss_pcs: int
	source_variant_of: str
	loss_type: str
	loss_variant: str
	loss_item: str
	s_warehouse: str
	t_warehouse: str
	source_type: str  # "MANUAL" | "MOP_LOG"
	employee_ir_name: Optional[str]
	manual_loss_row_name: Optional[str]
	mop_log_loss_name: Optional[str]
	deduct_from_transfer: bool
	create_stock_entry: bool


@dataclass
class SRESpec:
	old_sre_name: str
	old_sre_snapshot: dict
	item_code: str
	new_warehouse: str
	new_qty: float
	new_sb_rows: Optional[List[dict]]


@dataclass
class LossClassification:
	balance_effect: str  # "ALREADY_NET" | "NOT_NET" | "UNKNOWN"
	stock_effect: str  # "PROCESSED" | "NOT_PROCESSED" | "UNKNOWN"
	deduct_from_transfer: bool
	create_stock_entry: bool


@dataclass
class MovementPlan:
	scenario: str
	dept_ir_name: str
	dept_ir_row_name: str
	mop_name: str
	mwo: str
	company: str
	mop_department: str
	manufacturer: Optional[str]
	transfer_groups: Dict  # {(s_wh, t_wh): [TransferLineSpec]}
	loss_repacks: List[LossRepackSpec]
	sre_replacements: List[SRESpec]
	planned_dept_ir_mop_log_names: List[str]
	planned_loss_mop_log_names: List[str]
	has_any_transfer: bool
	has_any_loss: bool


# ────────────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────────────


def process_pc_tagging_stock_sync(dept_ir_doc, cancel=False, dry_run=False):
	"""Called ONCE from Department IR controller after the existing submit/cancel loop.

	Returns a list of ``MovementPlan`` objects on dry_run, None otherwise.
	"""
	scenario = resolve_pc_tagging_scenario(dept_ir_doc)
	if scenario == SCENARIO_NOT_APPLICABLE:
		frappe.logger("pc_tagging_sync").debug(
			"PC Tagging Sync: NOT_APPLICABLE for %s "
			"(type=%s current=%s previous=%s next=%s)",
			dept_ir_doc.name,
			dept_ir_doc.type,
			dept_ir_doc.current_department,
			dept_ir_doc.previous_department,
			dept_ir_doc.next_department,
		)
		return None

	if cancel:
		_handle_cancel(dept_ir_doc)
		return None

	validate_required_custom_fields()

	plans = []
	for row in dept_ir_doc.department_ir_operation:
		if not row.manufacturing_operation:
			continue
		plan = _build_row_plan(dept_ir_doc, row, scenario)
		_validate_plan(plan)
		plans.append(plan)

	if dry_run:
		return plans

	for plan in plans:
		_execute_plan(plan)

	return plans


# ────────────────────────────────────────────────────────────────────────────
# Scenario resolution
# ────────────────────────────────────────────────────────────────────────────


def _normalize_department(dept_name: str, company: str) -> str:
	"""Strip the ERPNext company-abbreviation suffix from a department name.

	ERPNext stores departments as "<Base Name> - <Company Abbr>", e.g.
	"Tagging - GEPL".  This function strips the suffix so that scenario
	resolution can compare base names regardless of which company the
	Department IR belongs to.

	Returns the bare base name, or the original string if no suffix is found.
	"""
	if not dept_name:
		return ""
	company_abbr = (
		frappe.get_cached_value("Company", company, "abbr") if company else ""
	)
	d = dept_name.strip()
	if company_abbr:
		suffix = f" - {company_abbr}"
		if d.endswith(suffix):
			return d[: -len(suffix)].strip()
	return d


def resolve_pc_tagging_scenario(dept_ir_doc) -> str:
	"""Return the scenario key string for this Department IR document.

	Only the two Product-Certification-centred scenarios create Stock Entries:
	  PC_TO_TAGGING_ISSUE    – current=PC, next=Tagging, type=Issue
	  TAGGING_TO_PC_RECEIVE  – current=PC, previous=Tagging, type=Receive

	All other flows return NOT_APPLICABLE.
	Department names are normalised (company-abbr suffix stripped) before
	comparison so "Tagging - GEPL" matches the canonical name "Tagging".
	"""
	company = dept_ir_doc.company or ""
	ir_type = dept_ir_doc.type
	cur = _normalize_department(dept_ir_doc.current_department or "", company)
	nxt = _normalize_department(dept_ir_doc.next_department or "", company)
	prv = _normalize_department(dept_ir_doc.previous_department or "", company)

	if ir_type == "Issue" and cur == PC_DEPT_NAME and nxt == TAGGING_DEPT_NAME:
		return SCENARIO_PC_TO_TAGGING_ISSUE
	if ir_type == "Receive" and prv == TAGGING_DEPT_NAME and cur == PC_DEPT_NAME:
		return SCENARIO_TAGGING_TO_PC_RECEIVE

	return SCENARIO_NOT_APPLICABLE


def _resolve_issue_mop(dept_ir_name: str, row_mop: str) -> str:
	"""Return the child MOP created for the next department during Issue submit.

	When a PC→Tagging Issue is submitted, ``on_submit_issue_new`` calls
	``create_operation_for_next_dept`` which inserts a NEW Manufacturing
	Operation with ``department_issue_id = dept_ir_name`` and
	``previous_mop = row_mop``.  ``create_mop_log_for_department_ir`` stores
	Dept IR MOP Logs under this new operation — NOT under the row's source MOP.

	Returns the child MOP name, or ``row_mop`` if not found (safe fallback).
	"""
	new_mop = frappe.db.get_value(
		"Manufacturing Operation",
		{"department_issue_id": dept_ir_name, "previous_mop": row_mop},
		"name",
	)
	return new_mop or row_mop


def _resolve_dept_transit_wh(department: str) -> Optional[str]:
	"""Return the transit warehouse linked to a department's manufacturing WH."""
	return frappe.db.get_value(
		"Warehouse",
		{"department": department, "warehouse_type": "Manufacturing", "disabled": 0},
		"default_in_transit_warehouse",
	)


# ────────────────────────────────────────────────────────────────────────────
# Build movement plan
# ────────────────────────────────────────────────────────────────────────────


def _build_row_plan(dept_ir_doc, row, scenario) -> MovementPlan:
	mop_name = row.manufacturing_operation  # source MOP on the row
	mwo = row.manufacturing_work_order
	company = dept_ir_doc.company

	# For PC_TO_TAGGING_ISSUE, MOP Logs live under the NEW child MOP created by
	# create_operation_for_next_dept — not under the row's source MOP.
	if scenario == SCENARIO_PC_TO_TAGGING_ISSUE:
		mop_log_name = _resolve_issue_mop(dept_ir_doc.name, mop_name)
	else:
		mop_log_name = mop_name

	mop_department = (
		frappe.db.get_value("Manufacturing Operation", mop_log_name, "department") or ""
	)
	manufacturer = _get_manufacturer_for_mop(mop_log_name)

	# 1. Dept IR MOP Logs — qty/pcs intent only (not warehouse source for Issue)
	dept_ir_logs = _get_dept_ir_mop_logs(dept_ir_doc.name, mop_log_name)
	planned_dept_ir_mop_log_names = [l["name"] for l in dept_ir_logs]
	dept_ir_logs = _deduplicate_dept_ir_mop_logs(dept_ir_logs)

	# 2. Loss rows
	manual_loss_rows = _get_employee_ir_manual_loss_rows(mop_log_name, mwo)
	mop_log_loss_rows = _get_mop_log_loss_rows(mop_log_name, mwo)
	all_loss_rows, planned_loss_mop_log_names = _deduplicate_loss_rows(
		manual_loss_rows, mop_log_loss_rows
	)

	# 3. SRE source resolution — scenario-specific
	if scenario == SCENARIO_PC_TO_TAGGING_ISSUE:
		# s_warehouse = SRE.warehouse (current stock location, wherever it is).
		# t_warehouse = next_department's transit WH (Tagging Transit, where stock is going).
		# SRE is cancelled in _execute_plan before SE creation so reserved stock is freed.
		transit_wh = _resolve_dept_transit_wh(dept_ir_doc.next_department)
		if not transit_wh:
			frappe.throw(
				_(
					"Transit warehouse not found for department {0}. "
					"Set 'Default In Transit Warehouse' on the department's manufacturing warehouse."
				).format(dept_ir_doc.next_department)
			)
		item_batch_keys = {(l["item_code"], l["batch_no"]) for l in dept_ir_logs}
		active_sres = _get_active_sres_for_mwo(mwo)
		sre_info_by_key = _build_sre_info_by_key(active_sres, item_batch_keys)
	else:
		# TAGGING_TO_PC_RECEIVE: SRE must be at the PREVIOUS department's transit WH (Tagging Transit).
		# The Issue service created a replacement SRE there. If missing → Issue not yet submitted.
		transit_wh = _resolve_dept_transit_wh(dept_ir_doc.previous_department)
		if not transit_wh:
			frappe.throw(
				_(
					"Transit warehouse not found for department {0}. "
					"Set 'Default In Transit Warehouse' on the department's manufacturing warehouse."
				).format(dept_ir_doc.previous_department)
			)
		item_batch_keys = {(l["item_code"], l["batch_no"]) for l in dept_ir_logs}
		active_sres = _get_active_sres_for_mwo(
			mwo
		)  # no MOP filter — SRE may be on child MOP
		sre_info_by_key = _build_sre_info_by_key(active_sres, item_batch_keys)
		for key in item_batch_keys:
			sre_hit = sre_info_by_key.get(key)
			if not sre_hit:
				frappe.throw(
					_(
						"No active Stock Reservation Entry found for item {0} batch {1} "
						"on work order {2}. Cannot complete this Receive."
					).format(key[0], key[1], mwo)
				)
			sre_wh = sre_hit[1]
			if sre_wh != transit_wh:
				frappe.throw(
					_(
						"Cannot receive {0} batch {1}: the stock reservation is at {2}, "
						"not {3} (transit warehouse for {4}). "
						"Submit the PC-to-Tagging Issue movement first."
					).format(
						key[0],
						key[1],
						sre_wh,
						transit_wh,
						dept_ir_doc.previous_department,
					)
				)

	# 3b. MOP balance — authoritative qty source (not SRE reserved_qty which may be stale).
	#     mop_name = source MOP (PC MOP for Issue; same as mop_log_name for Receive).
	#     get_current_mop_balance_rows accumulates ALL log entries including loss logs.
	_balance_keys = list({(l["item_code"], l["batch_no"]) for l in dept_ir_logs})
	balance_map = _get_balance_for_keys(mop_name, _balance_keys)

	# 4. Loss specs — throw if manufacturer missing and loss rows exist
	if all_loss_rows and not manufacturer:
		frappe.throw(
			_(
				"Manufacturer is required to process loss for Manufacturing Operation {0}"
			).format(mop_log_name)
		)
	loss_specs = []
	for lr in all_loss_rows:
		cl = _classify_loss_effect(lr, mop_log_name)
		if not cl.create_stock_entry:
			continue
		from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
			get_loss_item_from_manufacturer_mapping,
		)

		loss_variant = _get_loss_variant_from_manufacturer(
			lr["item_code"], manufacturer, lr["loss_type"]
		)
		loss_item = get_loss_item_from_manufacturer_mapping(
			lr["item_code"], manufacturer, lr["loss_type"]
		)
		# For both scenarios, SRE warehouse is the authoritative stock location
		sre_hit = sre_info_by_key.get((lr["item_code"], lr.get("batch_no")))
		loss_s_wh = (
			sre_hit[1] if sre_hit else _resolve_loss_source_warehouse(lr, dept_ir_logs)
		)
		loss_t_wh = _resolve_loss_warehouse(
			lr["item_code"], manufacturer, mop_department, company
		)
		_validate_warehouse_company(loss_s_wh, company)
		_validate_warehouse_company(loss_t_wh, company)
		is_dg = requires_pcs(lr["item_code"])
		loss_specs.append(
			LossRepackSpec(
				item_code=lr["item_code"],
				batch_no=lr.get("batch_no"),
				loss_qty=flt(
					lr.get("proportionally_loss") or abs(flt(lr.get("qty_change") or 0))
				),
				loss_pcs=cint(lr.get("pcs") or 0) if is_dg else 0,
				source_variant_of=(
					frappe.get_cached_value("Item", lr["item_code"], "variant_of") or ""
				),
				loss_type=lr["loss_type"],
				loss_variant=loss_variant,
				loss_item=loss_item,
				s_warehouse=loss_s_wh,
				t_warehouse=loss_t_wh,
				source_type=lr["_source_type"],
				employee_ir_name=lr.get("_employee_ir"),
				manual_loss_row_name=(
					lr.get("_row_name") if lr["_source_type"] == "MANUAL" else None
				),
				mop_log_loss_name=(
					lr.get("_row_name") if lr["_source_type"] == "MOP_LOG" else None
				),
				deduct_from_transfer=cl.deduct_from_transfer,
				create_stock_entry=True,
			)
		)

	# 5. Pending loss map — sum, not overwrite
	pending_loss_by_key = {}
	for spec in loss_specs:
		if spec.deduct_from_transfer:
			key = (spec.item_code, spec.batch_no)
			pending_loss_by_key[key] = pending_loss_by_key.get(key, 0.0) + spec.loss_qty

	# 6. Transfer lines — scenario-specific source warehouse
	if scenario == SCENARIO_PC_TO_TAGGING_ISSUE:
		# s_warehouse = SRE.warehouse; t_warehouse = transit_wh (next_dept transit = Tagging Transit).
		# SRE is cancelled in _execute_plan Step 1 before SE creation — no pre-availability check needed.
		transfer_lines = _build_transfer_specs_issue_sre_based(
			dept_ir_logs,
			sre_info_by_key,
			pending_loss_by_key,
			transit_wh,
			mop_log_name,
			mwo,
			balance_map,
		)
	else:
		# TAGGING_TO_PC_RECEIVE: SRE validated at transit_wh (Tagging Transit) above.
		# s_warehouse = SRE.warehouse (Tagging Transit); t_warehouse from MOP Log to_warehouse.
		transfer_lines = _build_transfer_specs_receive_sre_based(
			dept_ir_logs,
			sre_info_by_key,
			pending_loss_by_key,
			mop_log_name,
			mwo,
			balance_map,
		)

	# 8. Group by (s_wh, t_wh)
	transfer_groups = {}
	for line in transfer_lines:
		k = (line.s_warehouse, line.t_warehouse)
		transfer_groups.setdefault(k, []).append(line)

	# 9. SRE specs
	all_lines = [l for ls in transfer_groups.values() for l in ls]
	if all_lines:
		sre_specs = _build_sre_specs(active_sres, all_lines, dept_ir_doc.name)
	else:
		sre_specs = _build_loss_only_sre_specs(active_sres, loss_specs)

	return MovementPlan(
		scenario=scenario,
		dept_ir_name=dept_ir_doc.name,
		dept_ir_row_name=row.name,
		mop_name=mop_log_name,  # child MOP for Issue; row MOP for Receive
		mwo=mwo,
		company=company,
		mop_department=mop_department,
		manufacturer=manufacturer,
		transfer_groups=transfer_groups,
		loss_repacks=loss_specs,
		sre_replacements=sre_specs,
		planned_dept_ir_mop_log_names=planned_dept_ir_mop_log_names,
		planned_loss_mop_log_names=planned_loss_mop_log_names,
		has_any_transfer=bool(all_lines),
		has_any_loss=bool(loss_specs),
	)


def _validate_plan(plan: MovementPlan):
	if not plan.has_any_transfer and not plan.has_any_loss:
		frappe.log_error(
			f"No transfer items or loss rows for Dept IR {plan.dept_ir_name} "
			f"MOP {plan.mop_name}. Skipping.",
			"PC Tagging Sync",
		)
	if plan.mop_name and not plan.mwo:
		frappe.throw(
			_(
				"Manufacturing Work Order is required for Manufacturing Operation {0}"
			).format(plan.mop_name)
		)


# ────────────────────────────────────────────────────────────────────────────
# Execute plan
# ────────────────────────────────────────────────────────────────────────────


def _execute_plan(plan: MovementPlan):
	# Concurrency lock — serialize parallel submissions for the same MOP
	frappe.db.get_value(
		"Manufacturing Operation", plan.mop_name, "name", for_update=True
	)

	# Step 1: Cancel old SREs first — unreserves stock so the SE transfer can proceed
	for sre_spec in plan.sre_replacements:
		old = frappe.get_doc("Stock Reservation Entry", sre_spec.old_sre_name)
		if cint(old.docstatus) == 1:
			old.cancel()

	# Step 2: Main transfer SEs — stock now unreserved, SE submit can validate freely
	if plan.has_any_transfer:
		for (s_wh, t_wh), lines in plan.transfer_groups.items():
			main_hash = _make_main_hash(plan, lines, s_wh, t_wh)
			if not frappe.db.get_value(
				"Stock Entry",
				{"custom_pc_tagging_sync_hash": main_hash, "docstatus": 1},
				"name",
			):
				_create_material_transfer_se(plan, lines, s_wh, t_wh, main_hash)

	# Step 3: Loss Repack SEs — independent per loss row
	for spec in plan.loss_repacks:
		if not spec.create_stock_entry:
			continue
		loss_hash = _make_global_loss_hash(spec, plan.mop_name, plan.mwo)
		if not frappe.db.get_value(
			"Stock Entry",
			{"custom_pc_tagging_sync_hash": loss_hash, "docstatus": 1},
			"name",
		):
			_create_loss_repack_se(plan, spec, loss_hash)

	# Step 4: Create replacement SREs at new warehouse (SE has moved stock there)
	for sre_spec in plan.sre_replacements:
		sre_hash = _make_sre_hash(plan, sre_spec)
		if frappe.db.get_value(
			"Stock Reservation Entry",
			{"custom_pc_tagging_sync_hash": sre_hash, "docstatus": 1},
			"name",
		):
			continue
		if sre_spec.new_qty > TOLERANCE:
			_create_replacement_sre(sre_spec, plan, sre_hash)

	# Step 5: Sync exact MOP Logs — prevents EOD duplication
	all_names = plan.planned_dept_ir_mop_log_names + plan.planned_loss_mop_log_names
	_sync_exact_mop_logs_strict(
		all_names,
		plan.mop_name,
		plan.dept_ir_name,
		plan.planned_dept_ir_mop_log_names,
		plan.planned_loss_mop_log_names,
	)


# ────────────────────────────────────────────────────────────────────────────
# Cancel handler
# ────────────────────────────────────────────────────────────────────────────


def _handle_cancel(dept_ir_doc):
	"""Cancel in safe order: SREs → SEs → restore SREs → recalculate weights.

	For Issue cancel: existing code already cancels SEs via {department_ir: name}.
	Those SEs will have docstatus=2 by the time this runs — service skips them.
	For Receive cancel: existing code does NOT cancel SEs — service does.
	Both paths: service cancels replacement SREs and restores originals.
	"""
	# Step 1: Cancel replacement SREs first (unreserve target stock before SE reversal)
	repl_sres = frappe.db.get_all(
		"Stock Reservation Entry",
		{
			"custom_department_ir": dept_ir_doc.name,
			"custom_pc_tagging_movement_type": "SRE_REPLACE",
		},
		["name", "docstatus", "custom_replaced_sre_snapshot"],
	)
	sre_snapshots = []
	for row in repl_sres:
		if cint(row.docstatus) == 1:
			frappe.get_doc("Stock Reservation Entry", row.name).cancel()
		if row.custom_replaced_sre_snapshot:
			sre_snapshots.append(row.custom_replaced_sre_snapshot)

	# Step 2: Cancel service-created Stock Entries (skip already-cancelled)
	ses = frappe.db.get_all(
		"Stock Entry",
		{"department_ir": dept_ir_doc.name, "auto_created": 1},
		["name", "docstatus", "custom_pc_tagging_movement_type"],
		order_by="creation desc",
	)
	loss_ses = [s for s in ses if s.custom_pc_tagging_movement_type == "LOSS_REPACK"]
	main_ses = [s for s in ses if s.custom_pc_tagging_movement_type == "MAIN_TRANSFER"]
	for se in loss_ses + main_ses:
		if cint(se.docstatus) == 1:
			frappe.get_doc("Stock Entry", se.name).cancel()

	# Step 3: Restore original SREs from persisted JSON snapshots
	for snapshot_json in sre_snapshots:
		_restore_sre_from_snapshot(snapshot_json, dept_ir_doc.name)

	# Step 4: Recalculate weights for all affected MOPs
	for row in dept_ir_doc.department_ir_operation:
		if row.manufacturing_operation:
			recalculate_manufacturing_operation_weights(row.manufacturing_operation)


# ────────────────────────────────────────────────────────────────────────────
# Stock Entry creation helpers
# ────────────────────────────────────────────────────────────────────────────


def _create_material_transfer_se(plan, lines, s_wh, t_wh, sync_hash) -> str:
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Material Transfer to Department"
	se.company = plan.company
	se.department_ir = plan.dept_ir_name
	se.auto_created = 1
	se.custom_pc_tagging_sync_hash = sync_hash
	se.custom_pc_tagging_scenario = plan.scenario
	se.custom_pc_tagging_movement_type = "MAIN_TRANSFER"

	se_meta = frappe.get_meta("Stock Entry")
	sed_meta = frappe.get_meta("Stock Entry Detail")

	if se_meta.has_field("manufacturing_work_order"):
		se.manufacturing_work_order = plan.mwo
	if se_meta.has_field("manufacturing_operation"):
		se.manufacturing_operation = plan.mop_name

	has_pcs = sed_meta.has_field("pcs")
	has_mop = sed_meta.has_field("manufacturing_operation")
	has_mwo = sed_meta.has_field("custom_manufacturing_work_order")
	has_src_mop_log = sed_meta.has_field("custom_source_mop_log")
	has_line_hash = sed_meta.has_field("custom_pc_tagging_line_hash")

	for line in lines:
		line_hash = _make_line_hash(
			plan.mop_name,
			line.item_code,
			line.batch_no,
			line.qty,
			line.s_warehouse,
			line.t_warehouse,
		)
		item_row = {
			"item_code": line.item_code,
			"qty": line.qty,
			"s_warehouse": line.s_warehouse,
			"t_warehouse": line.t_warehouse,
			"batch_no": line.batch_no,
			"uom": line.uom,
			"use_serial_batch_fields": True,
		}
		if has_pcs:
			item_row["pcs"] = str(line.pcs) if line.pcs else "0"
		if has_mop:
			item_row["manufacturing_operation"] = line.manufacturing_operation
		if has_mwo:
			item_row["custom_manufacturing_work_order"] = line.manufacturing_work_order
		if has_src_mop_log:
			item_row["custom_source_mop_log"] = ",".join(line.source_mop_log_names[:5])
		if has_line_hash:
			item_row["custom_pc_tagging_line_hash"] = line_hash
		se.append("items", item_row)

	se.flags.ignore_permissions = True
	se.save()
	se.submit()
	return se.name


def _create_loss_repack_se(plan, spec: LossRepackSpec, sync_hash) -> str:
	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Repack"
	se.company = plan.company
	se.department_ir = plan.dept_ir_name
	se.auto_created = 1
	se.custom_pc_tagging_sync_hash = sync_hash
	se.custom_pc_tagging_scenario = plan.scenario
	se.custom_pc_tagging_movement_type = "LOSS_REPACK"

	se_meta = frappe.get_meta("Stock Entry")
	sed_meta = frappe.get_meta("Stock Entry Detail")

	if se_meta.has_field("manufacturing_work_order"):
		se.manufacturing_work_order = plan.mwo
	if se_meta.has_field("manufacturing_operation"):
		se.manufacturing_operation = plan.mop_name

	has_pcs = sed_meta.has_field("pcs")
	has_mop = sed_meta.has_field("manufacturing_operation")
	has_mwo = sed_meta.has_field("custom_manufacturing_work_order")
	has_emp_ir_row = sed_meta.has_field("custom_employee_ir_manual_loss_row")
	has_loss_type = sed_meta.has_field("custom_loss_type")
	has_loss_item = sed_meta.has_field("custom_loss_item")
	has_line_hash = sed_meta.has_field("custom_pc_tagging_line_hash")

	src_uom = frappe.get_cached_value("Item", spec.item_code, "stock_uom")
	loss_uom = frappe.get_cached_value("Item", spec.loss_item, "stock_uom")

	loss_hash = _make_line_hash(
		plan.mop_name,
		spec.item_code,
		spec.batch_no,
		spec.loss_qty,
		spec.s_warehouse,
		"",
	)

	# Outgoing row (original item consumed, source batch)
	outgoing = {
		"item_code": spec.item_code,
		"qty": flt(spec.loss_qty, 3),
		"s_warehouse": spec.s_warehouse,
		"t_warehouse": None,
		"batch_no": spec.batch_no,
		"uom": src_uom,
		"use_serial_batch_fields": True,
	}
	if has_pcs:
		outgoing["pcs"] = str(spec.loss_pcs) if requires_pcs(spec.item_code) else "0"
	if has_mop:
		outgoing["manufacturing_operation"] = plan.mop_name
	if has_mwo:
		outgoing["custom_manufacturing_work_order"] = plan.mwo
	if has_emp_ir_row and spec.manual_loss_row_name:
		outgoing["custom_employee_ir_manual_loss_row"] = spec.manual_loss_row_name
	if has_loss_type:
		outgoing["custom_loss_type"] = spec.loss_type
	if has_loss_item:
		outgoing["custom_loss_item"] = spec.loss_item
	if has_line_hash:
		outgoing["custom_pc_tagging_line_hash"] = loss_hash

	se.append("items", outgoing)

	# Incoming row (loss item produced — no batch_no; ERPNext auto-creates via create_new_batch)
	incoming = {
		"item_code": spec.loss_item,
		"qty": flt(spec.loss_qty, 3),
		"s_warehouse": None,
		"t_warehouse": spec.t_warehouse,
		"uom": loss_uom,
	}
	if has_pcs:
		incoming["pcs"] = str(spec.loss_pcs) if requires_pcs(spec.loss_item) else "0"
	if has_mop:
		incoming["manufacturing_operation"] = plan.mop_name
	if has_mwo:
		incoming["custom_manufacturing_work_order"] = plan.mwo
	if has_loss_type:
		incoming["custom_loss_type"] = spec.loss_type

	se.append("items", incoming)

	se.flags.ignore_permissions = True
	se.save()
	se.submit()
	return se.name


# ────────────────────────────────────────────────────────────────────────────
# SRE helpers
# ────────────────────────────────────────────────────────────────────────────


def _build_sre_specs(active_sres, all_transfer_lines, dept_ir_name) -> List[SRESpec]:
	"""Build SRE replacement specs with qty allocation tracking (no over-allocation)."""
	# remaining_by_key: allocated transfer qty per (item_code, batch_no)
	remaining_by_key = {}
	for line in all_transfer_lines:
		key = (line.item_code, line.batch_no)
		remaining_by_key[key] = remaining_by_key.get(key, 0.0) + line.qty

	t_wh_by_item = {}
	for line in all_transfer_lines:
		t_wh_by_item.setdefault(line.item_code, line.t_warehouse)

	specs = []
	for sre in active_sres:
		snapshot = _capture_sre_snapshot(sre)
		t_wh = t_wh_by_item.get(sre.item_code)
		if not t_wh:
			continue

		if sre.reservation_based_on != "Qty":
			sb_rows = frappe.db.get_all(
				"Serial and Batch Entry",
				{"parent": sre.name},
				["batch_no", "qty", "delivered_qty", "serial_no"],
			)
			new_sb = []
			for sb in sb_rows:
				sb_remaining = flt(sb.qty) - flt(sb.delivered_qty)
				if sb_remaining <= TOLERANCE:
					continue
				key = (sre.item_code, sb.batch_no)
				avail = remaining_by_key.get(key, 0.0)
				alloc = min(sb_remaining, avail)
				if alloc > TOLERANCE:
					new_sb.append(
						{
							"batch_no": sb.batch_no,
							"qty": alloc,
							"serial_no": sb.serial_no,
						}
					)
					remaining_by_key[key] = remaining_by_key.get(key, 0.0) - alloc
			specs.append(
				SRESpec(
					old_sre_name=sre.name,
					old_sre_snapshot=snapshot,
					item_code=sre.item_code,
					new_warehouse=t_wh,
					new_qty=sum(r["qty"] for r in new_sb),
					new_sb_rows=new_sb or None,
				)
			)
		else:
			sre_remaining = flt(sre.reserved_qty) - flt(sre.delivered_qty)
			# Aggregate across all batch keys for qty-based SRE
			item_avail = sum(
				v for k, v in remaining_by_key.items() if k[0] == sre.item_code
			)
			alloc = min(sre_remaining, item_avail)
			# Decrement remaining_by_key in key order
			to_consume = alloc
			for k in sorted(k for k in remaining_by_key if k[0] == sre.item_code):
				consume = min(to_consume, remaining_by_key.get(k, 0.0))
				remaining_by_key[k] -= consume
				to_consume -= consume
				if to_consume <= TOLERANCE:
					break
			specs.append(
				SRESpec(
					old_sre_name=sre.name,
					old_sre_snapshot=snapshot,
					item_code=sre.item_code,
					new_warehouse=t_wh,
					new_qty=alloc,
					new_sb_rows=None,
				)
			)
	return specs


def _build_loss_only_sre_specs(active_sres, loss_specs) -> List[SRESpec]:
	"""When there is no material transfer (loss-only), reduce SRE by loss qty."""
	total_loss_by_key = {}
	for spec in loss_specs:
		key = (spec.item_code, spec.batch_no)
		total_loss_by_key[key] = total_loss_by_key.get(key, 0.0) + spec.loss_qty

	specs = []
	for sre in active_sres:
		snapshot = _capture_sre_snapshot(sre)
		if sre.reservation_based_on != "Qty":
			sb_rows = frappe.db.get_all(
				"Serial and Batch Entry",
				{"parent": sre.name},
				["batch_no", "qty", "delivered_qty"],
			)
			new_sb = []
			for sb in sb_rows:
				sb_remaining = flt(sb.qty) - flt(sb.delivered_qty)
				key = (sre.item_code, sb.batch_no)
				loss = flt(total_loss_by_key.get(key, 0.0))
				new_qty = max(0.0, sb_remaining - loss)
				if new_qty > TOLERANCE:
					new_sb.append({"batch_no": sb.batch_no, "qty": new_qty})
			specs.append(
				SRESpec(
					old_sre_name=sre.name,
					old_sre_snapshot=snapshot,
					item_code=sre.item_code,
					new_warehouse=sre.warehouse,
					new_qty=sum(r["qty"] for r in new_sb),
					new_sb_rows=new_sb or None,
				)
			)
		else:
			sre_remaining = flt(sre.reserved_qty) - flt(sre.delivered_qty)
			key = (sre.item_code, None)
			loss = flt(total_loss_by_key.get(key, 0.0))
			new_qty = max(0.0, sre_remaining - loss)
			specs.append(
				SRESpec(
					old_sre_name=sre.name,
					old_sre_snapshot=snapshot,
					item_code=sre.item_code,
					new_warehouse=sre.warehouse,
					new_qty=new_qty,
					new_sb_rows=None,
				)
			)
	return specs


def _create_replacement_sre(
	sre_spec: SRESpec, plan: MovementPlan, sre_hash: str
) -> str:
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_available_qty_to_reserve,
	)

	snap = sre_spec.old_sre_snapshot
	new_sre = frappe.new_doc("Stock Reservation Entry")
	new_sre.voucher_type = snap["voucher_type"]
	new_sre.voucher_no = snap["voucher_no"]
	new_sre.voucher_detail_no = snap["voucher_detail_no"]
	new_sre.item_code = snap["item_code"]
	new_sre.warehouse = sre_spec.new_warehouse
	new_sre.voucher_qty = snap["reserved_qty"]
	new_sre.reserved_qty = sre_spec.new_qty
	new_sre.company = snap["company"]
	new_sre.stock_uom = snap["stock_uom"]
	new_sre.reservation_based_on = snap["reservation_based_on"]
	new_sre.manufacturing_work_order = snap.get("manufacturing_work_order")
	new_sre.manufacturing_operation = snap.get("manufacturing_operation")
	new_sre.has_batch_no = cint(snap.get("has_batch_no", 0))
	new_sre.has_serial_no = cint(snap.get("has_serial_no", 0))
	new_sre.custom_pc_tagging_sync_hash = sre_hash
	new_sre.custom_department_ir = plan.dept_ir_name
	new_sre.custom_replaced_sre = snap["name"]
	new_sre.custom_replaced_sre_snapshot = json.dumps(snap)
	new_sre.custom_pc_tagging_scenario = plan.scenario
	new_sre.custom_pc_tagging_movement_type = "SRE_REPLACE"

	positive_sb = []
	if sre_spec.new_sb_rows:
		for sb in sre_spec.new_sb_rows:
			if sb["qty"] > TOLERANCE:
				new_sre.append(
					"sb_entries", {"batch_no": sb["batch_no"], "qty": sb["qty"]}
				)
				positive_sb.append(sb)

	if (
		cint(snap.get("has_batch_no"))
		and snap.get("reservation_based_on") != "Qty"
		and not positive_sb
	):
		return None  # all sb rows consumed by loss — no replacement needed

	if positive_sb:
		avail = get_available_qty_to_reserve(
			snap["item_code"],
			sre_spec.new_warehouse,
			batch_no=positive_sb[0]["batch_no"],
		)
	else:
		avail = get_available_qty_to_reserve(snap["item_code"], sre_spec.new_warehouse)

	new_sre.available_qty = max(flt(avail), sre_spec.new_qty)
	new_sre.flags.ignore_permissions = True
	new_sre.insert(ignore_links=1)
	new_sre.submit()
	return new_sre.name


def _capture_sre_snapshot(sre) -> dict:
	"""Capture full SRE data before cancellation for restoration on cancel."""
	sb_entries_data = []
	if cint(sre.has_batch_no) and sre.reservation_based_on != "Qty":
		sb_rows = frappe.db.get_all(
			"Serial and Batch Entry",
			{"parent": sre.name},
			["batch_no", "qty", "delivered_qty", "serial_no"],
		)
		sb_entries_data = [
			{
				"batch_no": s.batch_no,
				"qty": flt(s.qty),
				"delivered_qty": flt(s.delivered_qty),
				"serial_no": s.serial_no,
			}
			for s in sb_rows
		]
	return {
		"name": sre.name,
		"item_code": sre.item_code,
		"warehouse": sre.warehouse,
		"reserved_qty": flt(sre.reserved_qty),
		"delivered_qty": flt(sre.delivered_qty),
		"voucher_type": sre.voucher_type,
		"voucher_no": sre.voucher_no,
		"voucher_detail_no": sre.voucher_detail_no,
		"reservation_based_on": sre.reservation_based_on,
		"manufacturing_work_order": sre.manufacturing_work_order,
		"manufacturing_operation": sre.manufacturing_operation,
		"company": sre.company,
		"stock_uom": sre.stock_uom,
		"has_batch_no": cint(sre.has_batch_no),
		"has_serial_no": cint(sre.has_serial_no),
		"sb_entries": sb_entries_data,
	}


def _restore_sre_from_snapshot(snapshot_json, dept_ir_name):
	"""Restore original SRE from stored JSON snapshot. Idempotent."""
	if not snapshot_json:
		return
	snap = (
		json.loads(snapshot_json) if isinstance(snapshot_json, str) else snapshot_json
	)
	original_name = snap.get("name")
	if not original_name:
		return

	remaining = flt(snap["reserved_qty"]) - flt(snap["delivered_qty"])
	if remaining <= TOLERANCE:
		return

	restore_hash = hashlib.sha256(
		json.dumps(
			{
				"movement_type": "SRE_RESTORE",
				"department_ir": dept_ir_name,
				"original_sre": original_name,
				"item_code": snap["item_code"],
				"warehouse": snap["warehouse"],
				"remaining_qty": str(round(remaining, 3)),
			},
			sort_keys=True,
		).encode()
	).hexdigest()[:40]

	if frappe.db.get_value(
		"Stock Reservation Entry",
		{"custom_pc_tagging_sync_hash": restore_hash, "docstatus": 1},
		"name",
	):
		return

	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
		get_available_qty_to_reserve,
	)

	new_sre = frappe.new_doc("Stock Reservation Entry")
	new_sre.voucher_type = snap["voucher_type"]
	new_sre.voucher_no = snap["voucher_no"]
	new_sre.voucher_detail_no = snap["voucher_detail_no"]
	new_sre.item_code = snap["item_code"]
	new_sre.warehouse = snap["warehouse"]
	new_sre.voucher_qty = snap["reserved_qty"]
	new_sre.reserved_qty = remaining
	new_sre.company = snap["company"]
	new_sre.stock_uom = snap["stock_uom"]
	new_sre.reservation_based_on = snap["reservation_based_on"]
	new_sre.manufacturing_work_order = snap.get("manufacturing_work_order")
	new_sre.manufacturing_operation = snap.get("manufacturing_operation")
	new_sre.has_batch_no = cint(snap.get("has_batch_no", 0))
	new_sre.has_serial_no = cint(snap.get("has_serial_no", 0))
	new_sre.custom_replaced_sre = original_name
	new_sre.custom_department_ir = dept_ir_name
	new_sre.custom_pc_tagging_sync_hash = restore_hash
	new_sre.custom_pc_tagging_movement_type = "SRE_RESTORE"

	positive_sb = []
	for sb in snap.get("sb_entries", []):
		sb_remaining = flt(sb["qty"]) - flt(sb["delivered_qty"])
		if sb_remaining > TOLERANCE:
			new_sre.append(
				"sb_entries", {"batch_no": sb["batch_no"], "qty": sb_remaining}
			)
			positive_sb.append(sb)

	if (
		cint(snap.get("has_batch_no"))
		and snap.get("reservation_based_on") != "Qty"
		and not positive_sb
	):
		return

	if positive_sb:
		avail = get_available_qty_to_reserve(
			snap["item_code"], snap["warehouse"], batch_no=positive_sb[0]["batch_no"]
		)
	else:
		avail = get_available_qty_to_reserve(snap["item_code"], snap["warehouse"])

	new_sre.available_qty = max(flt(avail), remaining)
	new_sre.flags.ignore_permissions = True
	new_sre.insert(ignore_links=1)
	new_sre.submit()


# ────────────────────────────────────────────────────────────────────────────
# MOP Log sync
# ────────────────────────────────────────────────────────────────────────────


def _sync_exact_mop_logs_strict(
	all_names, mop_name, dept_ir_name, dept_ir_log_names_set, loss_log_names_set
):
	"""Sync ONLY the exact planned MOP Log names with full ownership verification."""
	dept_ir_set = set(dept_ir_log_names_set)
	loss_set = set(loss_log_names_set)

	for name in all_names:
		row = frappe.db.get_value(
			"MOP Log",
			name,
			[
				"manufacturing_operation",
				"voucher_type",
				"voucher_no",
				"is_cancelled",
				"is_synced",
				"log_category",
				"loss_type",
			],
			as_dict=True,
		)
		if not row or row.is_cancelled or row.is_synced:
			continue

		if name in dept_ir_set:
			if not (
				row.voucher_type == "Department IR"
				and row.voucher_no == dept_ir_name
				and row.manufacturing_operation == mop_name
			):
				frappe.log_error(
					f"Dept IR MOP Log {name} ownership check failed — skipping sync",
					"PC Tagging Sync",
				)
				continue
		elif name in loss_set:
			if not (
				row.manufacturing_operation == mop_name
				and row.log_category == "Loss Attribution"
				and row.loss_type
			):
				frappe.log_error(
					f"Loss MOP Log {name} ownership check failed — skipping sync",
					"PC Tagging Sync",
				)
				continue
		else:
			continue

		frappe.db.set_value("MOP Log", name, "is_synced", 1)


# ────────────────────────────────────────────────────────────────────────────
# Warehouse helpers
# ────────────────────────────────────────────────────────────────────────────


def _validate_warehouse_company(warehouse, company):
	wh_company = frappe.db.get_value("Warehouse", warehouse, "company")
	if wh_company and wh_company != company:
		frappe.throw(
			_("Warehouse {0} belongs to company {1}, expected {2}").format(
				warehouse, wh_company, company
			)
		)


def _resolve_loss_source_warehouse(loss_row, dept_ir_logs) -> str:
	"""Resolve source warehouse for a loss row with priority:
	1. MOP Log from_warehouse on the loss row
	2. Matching Dept IR transfer s_warehouse for same item/batch
	3. Fallback to first Dept IR log from_warehouse
	"""
	if loss_row.get("from_warehouse"):
		return loss_row["from_warehouse"]
	for log in dept_ir_logs:
		if log["item_code"] == loss_row["item_code"] and log[
			"batch_no"
		] == loss_row.get("batch_no"):
			return log["from_warehouse"]
	if dept_ir_logs:
		return dept_ir_logs[0]["from_warehouse"]
	frappe.throw(
		_("Cannot resolve source warehouse for loss item {0}").format(
			loss_row["item_code"]
		)
	)


def _resolve_loss_warehouse(item_code, manufacturer, department, company) -> str:
	"""Resolve loss target warehouse from Manufacturer Variant Loss Warehouse."""
	source_variant_of = frappe.get_cached_value("Item", item_code, "variant_of") or ""
	details = frappe.db.get_value(
		"Variant Loss Warehouse",
		{"parent": manufacturer, "variant": source_variant_of},
		["loss_warehouse", "consider_department_warehouse", "warehouse_type"],
		as_dict=1,
	)
	if details:
		if details.loss_warehouse:
			return details.loss_warehouse
		if details.consider_department_warehouse and details.warehouse_type:
			wh = frappe.db.get_value(
				"Warehouse",
				{
					"department": department,
					"warehouse_type": details.warehouse_type,
					"disabled": 0,
				},
				"name",
			)
			if wh:
				return wh

	default_loss_wh = frappe.db.get_value(
		"Manufacturer", manufacturer, "default_loss_warehouse"
	)
	if default_loss_wh:
		return default_loss_wh

	frappe.throw(
		_(
			"Default loss warehouse is not set in Manufacturer loss table for "
			"Variant {0}, Manufacturer {1}"
		).format(source_variant_of, manufacturer)
	)


# ────────────────────────────────────────────────────────────────────────────
# MOP Log query helpers
# ────────────────────────────────────────────────────────────────────────────


def _get_dept_ir_mop_logs(dept_ir_name, mop_name) -> list:
	return frappe.db.get_all(
		"MOP Log",
		filters={
			"voucher_type": "Department IR",
			"voucher_no": dept_ir_name,
			"manufacturing_operation": mop_name,
			"is_cancelled": 0,
		},
		fields=[
			"name",
			"item_code",
			"batch_no",
			"from_warehouse",
			"to_warehouse",
			"qty_after_transaction_batch_based",
			"pcs_after_transaction_batch_based",
			"row_name",
			"flow_index",
		],
		order_by="creation asc",
	)


def _deduplicate_dept_ir_mop_logs(dept_ir_logs) -> list:
	"""Remove exact snapshot duplicates keyed by (item, batch, wh, row_name, flow_index)."""
	seen = set()
	deduped = []
	for log in dept_ir_logs:
		key = (
			log["item_code"],
			log["batch_no"],
			log["from_warehouse"],
			log["to_warehouse"],
			log.get("row_name"),
			log.get("flow_index"),
		)
		if key not in seen:
			seen.add(key)
			deduped.append(log)
	return deduped


def _get_balance_for_keys(mop_name, keys) -> dict:
	"""Return balance_map {(item_code, batch_no): row_dict} for specific keys only."""
	if not keys:
		return {}
	rows = get_current_mop_balance_rows(mop_name, keys=keys)
	return {(r.get("item_code"), r.get("batch_no")): r for r in rows}


def _get_mop_log_loss_rows(mop_name, mwo) -> list:
	"""Query MOP Log loss attribution rows SEPARATELY from balance rows."""
	rows = frappe.db.get_all(
		"MOP Log",
		filters={
			"manufacturing_operation": mop_name,
			"is_cancelled": 0,
			"log_category": "Loss Attribution",
		},
		fields=[
			"name",
			"item_code",
			"batch_no",
			"qty_change",
			"pcs_change",
			"loss_type",
			"loss_weight",
			"loss_source_row",
			"from_warehouse",
		],
		order_by="creation asc",
	)
	enriched = []
	for r in rows:
		d = dict(r)
		d["_source_type"] = "MOP_LOG"
		d["_row_name"] = r["name"]
		d["_employee_ir"] = None
		enriched.append(d)
	return enriched


def _get_employee_ir_manual_loss_rows(mop_name, mwo) -> list:
	"""Read manually_book_loss_details from the Employee IR linked to this MOP."""
	employee_ir_name = frappe.db.get_value(
		"Manufacturing Operation", mop_name, "employee_ir"
	)
	if not employee_ir_name:
		return []

	rows = frappe.db.get_all(
		"Manually Book Loss Details",
		filters={
			"parent": employee_ir_name,
			"parenttype": "Employee IR",
			"manufacturing_operation": mop_name,
		},
		fields=[
			"name",
			"item_code",
			"batch_no",
			"variant_of",
			"loss_type",
			"proportionally_loss",
			"pcs",
			"stock_uom",
			"manufacturing_operation",
			"manufacturing_work_order",
		],
	)
	enriched = []
	for r in rows:
		d = dict(r)
		d["_source_type"] = "MANUAL"
		d["_row_name"] = r["name"]
		d["_employee_ir"] = employee_ir_name
		enriched.append(d)
	return enriched


def _deduplicate_loss_rows(manual_loss_rows, mop_log_loss_rows):
	"""Manual loss rows are canonical. Drop MOP Log attribution rows that already
	represent a manual loss row (loss_source_row links back to manual row name)."""
	manual_row_names = {r["_row_name"] for r in manual_loss_rows}
	filtered_mop_logs = []
	planned_loss_mop_log_names = []
	for row in mop_log_loss_rows:
		if row.get("loss_source_row") and row["loss_source_row"] in manual_row_names:
			planned_loss_mop_log_names.append(row["_row_name"])  # still sync it
		else:
			filtered_mop_logs.append(row)
			planned_loss_mop_log_names.append(row["_row_name"])

	combined = list(manual_loss_rows) + filtered_mop_logs
	return combined, planned_loss_mop_log_names


def _classify_loss_effect(loss_row, mop_name) -> LossClassification:
	"""Determine if MOP balance already nets this loss and if a Repack SE is needed."""
	source_type = loss_row.get("_source_type")

	# MOP Log attribution rows already post a negative qty_change → balance is already net
	if source_type == "MOP_LOG":
		balance_effect = "ALREADY_NET"
	else:
		# Manual loss row: check if a loss attribution MOP Log already exists for it
		existing_loss_log = frappe.db.get_value(
			"MOP Log",
			{
				"manufacturing_operation": mop_name,
				"loss_source_row": loss_row.get("_row_name"),
				"is_cancelled": 0,
				"log_category": "Loss Attribution",
			},
			"name",
		)
		balance_effect = "ALREADY_NET" if existing_loss_log else "NOT_NET"

	# Stock effect: Repack SE already processed?
	global_hash = _make_global_loss_hash_raw(loss_row, mop_name, "")
	stock_processed = bool(
		frappe.db.get_value(
			"Stock Entry",
			{"custom_pc_tagging_sync_hash": global_hash, "docstatus": 1},
			"name",
		)
	)
	stock_effect = "PROCESSED" if stock_processed else "NOT_PROCESSED"

	deduct_from_transfer = balance_effect == "NOT_NET"
	create_se = stock_effect == "NOT_PROCESSED"

	return LossClassification(
		balance_effect=balance_effect,
		stock_effect=stock_effect,
		deduct_from_transfer=deduct_from_transfer,
		create_stock_entry=create_se,
	)


def _get_loss_variant_from_manufacturer(item_code, manufacturer, loss_type) -> str:
	"""Return the loss_variant template code from the Manufacturer mapping."""
	source_variant_of = frappe.get_cached_value("Item", item_code, "variant_of") or ""
	loss_variant = frappe.db.get_value(
		"Variant Loss Table",
		{
			"parenttype": "Manufacturer",
			"parent": manufacturer,
			"parentfield": "custom_variant_loss_table",
			"variant": source_variant_of,
			"loss_type": loss_type,
		},
		"loss_variant",
	)
	if not loss_variant:
		frappe.throw(
			_(
				"Missing Manufacturer loss variant mapping for Variant {0}, "
				"Loss Type {1}, Manufacturer {2}."
			).format(source_variant_of, loss_type, manufacturer)
		)
	return loss_variant


def _get_active_sres(mwo, mop_name) -> list:
	return frappe.db.get_all(
		"Stock Reservation Entry",
		filters={
			"manufacturing_work_order": mwo,
			"manufacturing_operation": mop_name,
			"docstatus": 1,
			"status": ["not in", ("Cancelled", "Delivered")],
		},
		fields=[
			"name",
			"item_code",
			"warehouse",
			"reserved_qty",
			"delivered_qty",
			"reservation_based_on",
			"has_batch_no",
			"has_serial_no",
			"manufacturing_work_order",
			"manufacturing_operation",
			"voucher_type",
			"voucher_no",
			"voucher_detail_no",
			"company",
			"stock_uom",
		],
	)


def _get_active_sres_for_mwo(mwo) -> list:
	"""Get all active SREs for a Manufacturing Work Order (no MOP filter).

	Used for PC_TO_TAGGING_ISSUE where the SRE may be linked to an earlier
	operation (e.g. Waxing) rather than the current Tagging/PC MOP.
	"""
	return frappe.db.get_all(
		"Stock Reservation Entry",
		filters={
			"manufacturing_work_order": mwo,
			"docstatus": 1,
			"status": ["not in", ("Cancelled", "Delivered")],
		},
		fields=[
			"name",
			"item_code",
			"warehouse",
			"reserved_qty",
			"delivered_qty",
			"reservation_based_on",
			"has_batch_no",
			"has_serial_no",
			"manufacturing_work_order",
			"manufacturing_operation",
			"voucher_type",
			"voucher_no",
			"voucher_detail_no",
			"company",
			"stock_uom",
		],
	)


def _build_sre_info_by_key(sre_rows, item_batch_keys: set) -> dict:
	"""Build {(item_code, batch_no): (sre_name, warehouse, available_qty)} from active SREs.

	For batch-based SREs (reservation_based_on != "Qty") expands sb_entries to find
	the exact batch_no.  Only keys present in ``item_batch_keys`` are returned.
	When multiple SREs match the same key the one with the highest available qty wins.
	"""
	result = {}
	for sre in sre_rows:
		if sre.reservation_based_on != "Qty":
			sb_entries = frappe.db.get_all(
				"Serial and Batch Entry",
				{"parent": sre.name},
				["batch_no", "qty", "delivered_qty"],
			)
			for sb in sb_entries:
				sb_avail = flt(sb.qty) - flt(sb.delivered_qty)
				if sb_avail <= TOLERANCE:
					continue
				key = (sre.item_code, sb.batch_no)
				if key not in item_batch_keys:
					continue
				existing = result.get(key)
				if not existing or sb_avail > existing[2]:
					result[key] = (sre.name, sre.warehouse, sb_avail)
		else:
			sre_avail = flt(sre.reserved_qty) - flt(sre.delivered_qty)
			if sre_avail <= TOLERANCE:
				continue
			for key in item_batch_keys:
				if key[0] == sre.item_code:
					existing = result.get(key)
					if not existing or sre_avail > existing[2]:
						result[key] = (sre.name, sre.warehouse, sre_avail)
	return result


def _get_manufacturer_for_mop(mop_name) -> Optional[str]:
	"""Resolve manufacturer from Manufacturing Operation → MWO → PMO."""
	manufacturer = frappe.db.get_value(
		"Manufacturing Operation", mop_name, "manufacturer"
	)
	if manufacturer:
		return manufacturer
	mwo = frappe.db.get_value(
		"Manufacturing Operation", mop_name, "manufacturing_work_order"
	)
	if mwo:
		manufacturer = frappe.db.get_value(
			"Manufacturing Work Order", mwo, "manufacturer"
		)
	if manufacturer:
		return manufacturer
	return None


# ────────────────────────────────────────────────────────────────────────────
# Transfer spec builder
# ────────────────────────────────────────────────────────────────────────────


def _build_transfer_specs_from_dept_ir_logs(
	dept_ir_logs, balance_map, pending_loss_by_key, mop_name, mwo
) -> list:
	"""Build TransferLineSpec list from deduped Dept IR MOP Logs."""
	grouped = {}
	for log in dept_ir_logs:
		key = (
			log["item_code"],
			log["batch_no"],
			log["from_warehouse"],
			log["to_warehouse"],
		)
		if key not in grouped:
			grouped[key] = {
				"item_code": log["item_code"],
				"batch_no": log["batch_no"],
				"s_warehouse": log["from_warehouse"],
				"t_warehouse": log["to_warehouse"],
				"intended_qty": 0.0,
				"intended_pcs": 0,
				"source_mop_log_names": [],
			}
		grouped[key]["intended_qty"] += flt(
			log.get("qty_after_transaction_batch_based") or 0
		)
		if requires_pcs(log["item_code"]):
			grouped[key]["intended_pcs"] += cint(
				log.get("pcs_after_transaction_batch_based") or 0
			)
		grouped[key]["source_mop_log_names"].append(log["name"])

	transfer_lines = []
	for (item_code, batch_no, s_wh, t_wh), group in grouped.items():
		if s_wh == t_wh:
			frappe.throw(
				_("Source and target warehouse are the same ({0}) for item {1}").format(
					s_wh, item_code
				)
			)
		balance_row = balance_map.get((item_code, batch_no), {})
		balance_qty = flt(balance_row.get("qty_after_transaction_batch_based") or 0)
		pending = flt(pending_loss_by_key.get((item_code, batch_no), 0.0))
		available = max(0.0, balance_qty - pending)
		net_qty = round(min(float(group["intended_qty"]), float(available)), 3)
		if net_qty <= TOLERANCE:
			continue
		transfer_lines.append(
			TransferLineSpec(
				item_code=item_code,
				batch_no=batch_no,
				qty=net_qty,
				pcs=group["intended_pcs"] if requires_pcs(item_code) else 0,
				s_warehouse=s_wh,
				t_warehouse=t_wh,
				uom=frappe.get_cached_value("Item", item_code, "stock_uom"),
				manufacturing_operation=mop_name,
				manufacturing_work_order=mwo,
				source_mop_log_names=group["source_mop_log_names"],
				source_variant_of=(
					frappe.get_cached_value("Item", item_code, "variant_of") or ""
				),
			)
		)
	return transfer_lines


def _build_transfer_specs_issue_sre_based(
	dept_ir_logs, sre_info_by_key, pending_loss_by_key, t_wh, mop_name, mwo, balance_map
) -> list:
	"""Build TransferLineSpec for PC_TO_TAGGING_ISSUE using SRE as the stock source.

	Groups MOP Logs by (item_code, batch_no) to get intended qty/pcs, then caps
	qty at ``min(intended, mop_balance - pending_loss)``.  Source warehouse comes
	from the SRE; qty comes from the MOP balance (accounts for all intermediate losses).
	Target warehouse is ``t_wh`` (Tagging Transit).
	"""
	grouped = {}
	for log in dept_ir_logs:
		key = (log["item_code"], log["batch_no"])
		if key not in grouped:
			grouped[key] = {
				"intended_qty": 0.0,
				"intended_pcs": 0,
				"source_mop_log_names": [],
			}
		grouped[key]["intended_qty"] += flt(
			log.get("qty_after_transaction_batch_based") or 0
		)
		if requires_pcs(log["item_code"]):
			grouped[key]["intended_pcs"] += cint(
				log.get("pcs_after_transaction_batch_based") or 0
			)
		grouped[key]["source_mop_log_names"].append(log["name"])

	transfer_lines = []
	for (item_code, batch_no), group in grouped.items():
		sre_hit = sre_info_by_key.get((item_code, batch_no))
		if not sre_hit:
			frappe.log_error(
				f"PC Tagging Sync: no active SRE found for item {item_code} "
				f"batch {batch_no} MWO {mwo} — skipping transfer line.",
				"PC Tagging Sync",
			)
			continue
		(
			sre_name,
			sre_wh,
			_sre_avail,
		) = sre_hit  # warehouse only; sre_avail not used for qty
		if sre_wh == t_wh:
			frappe.throw(
				_(
					"SRE source warehouse ({0}) equals PC Transit target ({1}) for item {2}"
				).format(sre_wh, t_wh, item_code)
			)
		balance_row = balance_map.get((item_code, batch_no), {})
		balance_qty = flt(balance_row.get("qty_after_transaction_batch_based") or 0)
		pending = flt(pending_loss_by_key.get((item_code, batch_no), 0.0))
		available = max(0.0, balance_qty - pending)
		net_qty = round(min(float(group["intended_qty"]), float(available)), 3)
		if net_qty <= TOLERANCE:
			continue
		transfer_lines.append(
			TransferLineSpec(
				item_code=item_code,
				batch_no=batch_no,
				qty=net_qty,
				pcs=group["intended_pcs"] if requires_pcs(item_code) else 0,
				s_warehouse=sre_wh,
				t_warehouse=t_wh,
				uom=frappe.get_cached_value("Item", item_code, "stock_uom"),
				manufacturing_operation=mop_name,
				manufacturing_work_order=mwo,
				source_mop_log_names=group["source_mop_log_names"],
				source_variant_of=(
					frappe.get_cached_value("Item", item_code, "variant_of") or ""
				),
			)
		)
	return transfer_lines


def _build_transfer_specs_receive_sre_based(
	dept_ir_logs, sre_info_by_key, pending_loss_by_key, mop_name, mwo, balance_map
) -> list:
	"""Build TransferLineSpec for TAGGING_TO_PC_RECEIVE using SRE as the stock source.

	SRE validation in _build_row_plan has already confirmed sre_wh == transit_wh for
	every key.  Groups MOP Logs by (item_code, batch_no, to_warehouse); caps qty at
	min(mop_log_intent, mop_balance - pending_loss).
	Source = SRE warehouse (Tagging Transit). Target = MOP Log to_warehouse (PC WO).
	"""
	grouped = {}
	for log in dept_ir_logs:
		key = (log["item_code"], log["batch_no"], log["to_warehouse"])
		if key not in grouped:
			grouped[key] = {
				"intended_qty": 0.0,
				"intended_pcs": 0,
				"source_mop_log_names": [],
			}
		grouped[key]["intended_qty"] += flt(
			log.get("qty_after_transaction_batch_based") or 0
		)
		if requires_pcs(log["item_code"]):
			grouped[key]["intended_pcs"] += cint(
				log.get("pcs_after_transaction_batch_based") or 0
			)
		grouped[key]["source_mop_log_names"].append(log["name"])

	transfer_lines = []
	for (item_code, batch_no, t_wh), group in grouped.items():
		sre_hit = sre_info_by_key.get((item_code, batch_no))
		if not sre_hit:
			frappe.log_error(
				f"PC Tagging Sync (Receive): no active SRE for {item_code} batch {batch_no} "
				f"MWO {mwo} — skipping line.",
				"PC Tagging Sync",
			)
			continue
		(
			sre_name,
			sre_wh,
			_sre_avail,
		) = sre_hit  # warehouse only; sre_avail not used for qty
		if sre_wh == t_wh:
			frappe.throw(
				_("SRE source warehouse ({0}) equals target ({1}) for item {2}").format(
					sre_wh, t_wh, item_code
				)
			)
		balance_row = balance_map.get((item_code, batch_no), {})
		balance_qty = flt(balance_row.get("qty_after_transaction_batch_based") or 0)
		pending = flt(pending_loss_by_key.get((item_code, batch_no), 0.0))
		available = max(0.0, balance_qty - pending)
		net_qty = round(min(float(group["intended_qty"]), float(available)), 3)
		if net_qty <= TOLERANCE:
			continue
		transfer_lines.append(
			TransferLineSpec(
				item_code=item_code,
				batch_no=batch_no,
				qty=net_qty,
				pcs=group["intended_pcs"] if requires_pcs(item_code) else 0,
				s_warehouse=sre_wh,
				t_warehouse=t_wh,
				uom=frappe.get_cached_value("Item", item_code, "stock_uom"),
				manufacturing_operation=mop_name,
				manufacturing_work_order=mwo,
				source_mop_log_names=group["source_mop_log_names"],
				source_variant_of=(
					frappe.get_cached_value("Item", item_code, "variant_of") or ""
				),
			)
		)
	return transfer_lines


# ────────────────────────────────────────────────────────────────────────────
# Item profile helper
# ────────────────────────────────────────────────────────────────────────────


def requires_pcs(item_code: str) -> bool:
	"""True for Diamond and Gemstone items (Item.variant_of in ("D", "G"))."""
	variant_of = frappe.get_cached_value("Item", item_code, "variant_of") or ""
	return variant_of in ("D", "G")


# ────────────────────────────────────────────────────────────────────────────
# Stock availability validation
# ────────────────────────────────────────────────────────────────────────────


def _validate_source_availability(item_code, batch_no, warehouse, qty):
	has_batch_no, has_serial_no = frappe.get_cached_value(
		"Item", item_code, ["has_batch_no", "has_serial_no"]
	)
	if cint(has_batch_no):
		if not batch_no:
			frappe.throw(
				_("Batch number required for item {0} in {1}").format(
					item_code, warehouse
				)
			)
		from erpnext.stock.doctype.batch.batch import get_batch_qty

		avail = flt(get_batch_qty(batch_no, warehouse, item_code))
		if avail < qty - TOLERANCE:
			frappe.throw(
				_(
					"Insufficient batch qty for {0} batch {1} in {2}: need {3}, available {4}"
				).format(item_code, batch_no, warehouse, qty, avail)
			)
	elif cint(has_serial_no):
		count = frappe.db.count(
			"Serial No",
			{"item_code": item_code, "warehouse": warehouse, "status": "Active"},
		)
		if count < cint(qty) - 1:
			frappe.throw(
				_("Insufficient serial nos for {0} in {1}: need {2}, found {3}").format(
					item_code, warehouse, cint(qty), count
				)
			)
	else:
		try:
			from erpnext.stock.stock_ledger import get_stock_balance

			avail = flt(get_stock_balance(item_code, warehouse))
		except (ImportError, Exception):
			avail = flt(
				frappe.db.sql(
					"SELECT COALESCE(SUM(actual_qty),0) FROM `tabStock Ledger Entry` "
					"WHERE item_code=%s AND warehouse=%s AND is_cancelled=0",
					(item_code, warehouse),
				)[0][0]
			)
		if avail < qty - TOLERANCE:
			frappe.throw(
				_("Insufficient stock for {0} in {1}: need {2}, available {3}").format(
					item_code, warehouse, qty, avail
				)
			)


# ────────────────────────────────────────────────────────────────────────────
# Custom field validation
# ────────────────────────────────────────────────────────────────────────────


def validate_required_custom_fields():
	"""Fail fast if the patch has not been run."""
	required = {
		"Stock Entry": [
			"custom_pc_tagging_sync_hash",
			"custom_pc_tagging_scenario",
			"custom_pc_tagging_movement_type",
		],
		"Stock Entry Detail": [
			"custom_source_mop_log",
			"custom_employee_ir_manual_loss_row",
			"custom_loss_type",
			"custom_loss_item",
			"custom_pc_tagging_line_hash",
		],
		"Stock Reservation Entry": [
			"custom_pc_tagging_sync_hash",
			"custom_department_ir",
			"custom_replaced_sre",
			"custom_replaced_sre_snapshot",
			"custom_pc_tagging_scenario",
			"custom_pc_tagging_movement_type",
		],
	}
	for doctype, fields in required.items():
		meta = frappe.get_meta(doctype)
		for f in fields:
			if not meta.has_field(f):
				frappe.throw(
					_(
						"Required custom field '{0}' missing from '{1}'. "
						"Run: bench --site <site> migrate"
					).format(f, doctype)
				)


# ────────────────────────────────────────────────────────────────────────────
# Idempotency hash helpers
# ────────────────────────────────────────────────────────────────────────────


def _make_main_hash(plan, lines, s_wh, t_wh) -> str:
	lines_repr = sorted(
		[f"{l.item_code}|{l.batch_no or ''}|{round(l.qty, 3)}|{l.pcs}" for l in lines]
	)
	payload = {
		"movement_type": "MAIN_TRANSFER",
		"scenario": plan.scenario,
		"dept_ir": plan.dept_ir_name,
		"dept_ir_row": plan.dept_ir_row_name,
		"mop": plan.mop_name,
		"mwo": plan.mwo,
		"s_wh": s_wh,
		"t_wh": t_wh,
		"lines": lines_repr,
	}
	return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:40]


def _make_global_loss_hash(spec: LossRepackSpec, mop_name, mwo) -> str:
	payload = {
		"movement_type": "LOSS_REPACK",
		"source_type": spec.source_type,
		"employee_ir": spec.employee_ir_name or "",
		"manual_loss_row": spec.manual_loss_row_name or "",
		"mop_log_loss": spec.mop_log_loss_name or "",
		"manufacturing_operation": mop_name,
		"manufacturing_work_order": mwo,
		"item_code": spec.item_code,
		"batch_no": spec.batch_no or "",
		"source_variant_of": spec.source_variant_of,
		"loss_type": spec.loss_type,
		"loss_item": spec.loss_item,
		"loss_qty": str(round(spec.loss_qty, 3)),
		"loss_pcs": str(spec.loss_pcs),
		"s_warehouse": spec.s_warehouse,
		"t_warehouse": spec.t_warehouse,
	}
	return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:40]


def _make_global_loss_hash_raw(loss_row, mop_name, mwo) -> str:
	"""Used in _classify_loss_effect before spec is fully built."""
	payload = {
		"movement_type": "LOSS_REPACK",
		"source_type": loss_row.get("_source_type", ""),
		"employee_ir": loss_row.get("_employee_ir") or "",
		"manual_loss_row": (
			loss_row.get("_row_name")
			if loss_row.get("_source_type") == "MANUAL"
			else ""
		),
		"mop_log_loss": (
			loss_row.get("_row_name")
			if loss_row.get("_source_type") == "MOP_LOG"
			else ""
		),
		"manufacturing_operation": mop_name,
		"manufacturing_work_order": mwo,
		"item_code": loss_row.get("item_code", ""),
		"batch_no": loss_row.get("batch_no") or "",
		"loss_type": loss_row.get("loss_type", ""),
		"loss_qty": str(
			round(
				flt(
					loss_row.get("proportionally_loss")
					or abs(flt(loss_row.get("qty_change") or 0))
				),
				3,
			)
		),
	}
	return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:40]


def _make_sre_hash(plan, sre_spec: SRESpec) -> str:
	payload = {
		"movement_type": "SRE_REPLACE",
		"scenario": plan.scenario,
		"dept_ir": plan.dept_ir_name,
		"dept_ir_row": plan.dept_ir_row_name,
		"mop": plan.mop_name,
		"mwo": plan.mwo,
		"old_sre": sre_spec.old_sre_name,
		"item_code": sre_spec.item_code,
		"new_warehouse": sre_spec.new_warehouse,
		"new_qty": str(round(sre_spec.new_qty, 3)),
	}
	return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:40]


def _make_line_hash(mop_name, item_code, batch_no, qty, s_wh, t_wh) -> str:
	payload = {
		"mop": mop_name,
		"item": item_code,
		"batch": batch_no or "",
		"qty": str(round(qty, 3)),
		"s_wh": s_wh,
		"t_wh": t_wh,
	}
	return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
