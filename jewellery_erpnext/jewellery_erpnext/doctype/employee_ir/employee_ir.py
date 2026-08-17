import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder import DocType

# timer code
from frappe.utils import (
	cint,
	date_diff,
	flt,
	get_datetime,
	get_first_day,
	get_last_day,
	getdate,
	now_datetime,
	nowdate,
	time_diff,
	time_diff_in_hours,
	time_diff_in_seconds,
	today,
)

from jewellery_erpnext.jewellery_erpnext.customization.utils.ownership_priority import (
	batch_priority_map,
	describe_customer_spill,
	is_customer_rank,
	loss_rank,
	tiered_allocate,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.employee_ir_utils import (
	get_po_rates,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.finding_loss_gate import (
	get_finding_category_map,
	get_loss_booking_map,
	is_loss_booking_blocked,
	validate_loss_rows_against_gate,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.html_utils import (
	get_summary_data,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry import (
	cancel_loss_stock_entries,
	create_loss_stock_entries,
	get_batch_sre_headroom,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject import (
	cancel_injections_for_eir,
	inject_extra_metal_for_eir_receive,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.mould_utils import (
	create_mould,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.precision import (
	round_employee_ir_weights_to_precision,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.subcontracting_utils import (
	create_so_for_subcontracting,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.tree_casting import (
	create_tree_on_issue,
	lock_trees_for_eir,
	pin_tree_numbers_on_receive,
	unlink_tree_on_issue_cancel,
	update_tree_on_receive,
	validate_casting_group_complete,
	validate_casting_receive,
	validate_casting_tree,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
	validate_duplication_and_gr_wt,
	validate_employee_ir_receive_delay,
	validate_loss_qty,
	validate_loss_tables_required,
	validate_manually_book_loss_details,
)
from jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_operation.manufacturing_operation import (
	update_new_mop_wtg,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	# create_mop_log_for_employee_ir_loss,
	create_mop_log_for_employee_ir_receive,
	creste_mop_log_for_employee_ir,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_settings.mop_eod_sync import (
	_get_t_warehouse_from_logs,
	_resolve_department_warehouse,
)
from jewellery_erpnext.utils import (
	get_item_from_attribute_full,  # noqa: F401 – patched by tests
)


class EmployeeIR(Document):
	@frappe.whitelist()
	def get_operations(self):
		records = frappe.get_list(
			"Manufacturing Operation",
			{
				"department": self.department,
				"employee": ["is", "not set"],
				"operation": ["is", "not set"],
			},
			["name", "gross_wt"],
		)
		self.employee_ir_operations = []
		if records:
			for row in records:
				self.append(
					"employee_ir_operations", {"manufacturing_operation": row.name}
				)

	def before_submit(self):
		if self.type == "Issue":
			self.issue_submitted_on = now_datetime()
			validate_casting_group_complete(self)
		else:
			validate_employee_ir_receive_delay(self)

	def on_submit(self):
		validate_loss_tables_required(self)
		# Re-checked at submit, not just at validate: validate_process_loss and
		# validate_manually_book_loss_details both early-return once docstatus != 0,
		# so a draft saved before the Department Operation flag was flipped would
		# otherwise submit with stale loss rows on a now-blocked category.
		validate_loss_rows_against_gate(self)
		validate_qc(self)
		if self.type == "Issue":
			self.validate_qc("Warn")
			self.on_submit_issue_new()
			if self.subcontracting == "Yes":
				self.create_subcontracting_order()
		else:
			self.on_submit_receive()

	def before_validate(self):
		if self.docstatus != 0:
			return
		warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"department": self.department,
				"warehouse_type": "Manufacturing",
			},
		)
		if not warehouse:
			frappe.throw(_("MFG Warehouse not available for department"))
		if frappe.db.get_value(
			"Stock Reconciliation",
			{
				"set_warehouse": warehouse,
				"workflow_state": ["in", ["In Progress", "Send for Approval"]],
			},
		):
			frappe.throw(_("Stock Reconciliation is under process"))

		validate_duplication_and_gr_wt(self)

	def validate(self):
		# self.validate_gross_wt()
		# self.validate_main_slip()
		# self.update_main_slip()
		round_employee_ir_weights_to_precision(self)
		self.validate_process_loss()
		validate_manually_book_loss_details(self)
		validate_loss_rows_against_gate(self)
		# valid_reparing_or_next_operation(self)
		validate_loss_qty(self)
		validate_casting_tree(self)
		validate_casting_receive(self)
		self.validate_fg_bom_fields()
		self.validate_purchase_invoice()

	def validate_purchase_invoice(self):
		if self.type == "Receive" and self.subcontracting == "Yes":
			if not self.employee_ir_operations:
				return
				
			# Trace back to the Issue IR using the first MWO in the child table
			mwo = self.employee_ir_operations[0].manufacturing_work_order
			if not mwo:
				return
				
			# Get the Employee IR Issue using MWO and Department
			issue_ir = frappe.db.sql("""
				SELECT parent 
				FROM `tabEmployee IR Operation` 
				WHERE manufacturing_work_order = %s 
				AND parent IN (SELECT name FROM `tabEmployee IR` WHERE type = 'Issue' AND department = %s AND docstatus = 1)
				LIMIT 1
			""", (mwo, self.department))
			
			if issue_ir:
				issue_name = issue_ir[0][0]
				po = frappe.db.get_value("Purchase Order", {"employee_ir": issue_name, "docstatus": ["<", 2]}, "name")
				
				if po:
					pi_exists = frappe.db.sql("""
						SELECT parent 
						FROM `tabPurchase Invoice Item` 
						WHERE purchase_order = %s AND docstatus = 1
						LIMIT 1
					""", (po,))
					
					if not pi_exists:
						frappe.throw(
							"Please create a Purchase Invoice for Purchase Order {0} before receiving the Employee IR.".format(
								f"<a href='/app/purchase-order/{po}'>{po}</a>"
							)
						)
				else:
					frappe.throw(
						"No Purchase Order found for Issue IR {0}. A Purchase Invoice is required before receiving.".format(
							f"<a href='/app/employee-ir/{issue_name}'>{issue_name}</a>"
						)
					)
		self.set_repeat_receive_flag()

	def set_repeat_receive_flag(self):
		"""Stamp ``is_repeat_receive`` and clear a Worker Performance that no longer applies.

		Recomputed from the database rather than trusted from the client, the same
		stance validate_fg_bom_fields takes: the flag drives a depends_on, so a
		posted value could otherwise reveal (or hide) the field at will.

		The flag is ANY-of-rows: one Employee IR carries a single answer but many
		work orders, so a Receive containing at least one repeat asks the question.
		"""
		if self.type != "Receive":
			self.is_repeat_receive = 0
			self.worker_performance = None
			return

		ops = [
			{
				"manufacturing_operation": row.manufacturing_operation,
				"manufacturing_work_order": row.manufacturing_work_order,
			}
			for row in (self.employee_ir_operations or [])
		]
		repeats = get_repeat_work_orders(ops, self.operation, self.name)
		self.is_repeat_receive = 1 if repeats else 0
		if not self.is_repeat_receive:
			# A Receive can stop being a repeat (the earlier one gets cancelled)
			# after somebody answered; a hidden field must not keep a stale verdict.
			self.worker_performance = None

	def validate_fg_bom_fields(self):
		"""Enforce subcategory-driven FG BOM fields on Receive.

		Recomputes the required fields from the configuration (not from the
		client-populated grid) so a receive submitted with an empty/tampered
		custom_fg_bom_fields table cannot bypass mandatory entry, then validates
		each entered value against its configured type.
		"""
		if self.type != "Receive":
			return

		ops = [
			{
				"manufacturing_operation": r.manufacturing_operation,
				"manufacturing_work_order": r.manufacturing_work_order,
			}
			for r in (self.employee_ir_operations or [])
		]
		expected = get_fg_bom_fields(ops)
		entered = {
			(r.manufacturing_operation, r.field_name): r
			for r in (self.custom_fg_bom_fields or [])
		}
		for cfg in expected:
			if not cfg.get("is_mandatory"):
				continue
			row = entered.get((cfg["manufacturing_operation"], cfg["field_name"]))
			if not row or not (row.value or "").strip():
				frappe.throw(
					_("FG BOM Field '{0}' is mandatory.").format(
						cfg["field_label"] or cfg["field_name"]
					)
				)

		for row in self.custom_fg_bom_fields or []:
			_validate_fg_bom_field_value(row)

	def on_cancel(self):
		if self.type == "Issue":
			self.on_submit_issue_new(cancel=True)
		else:
			self.on_submit_receive(cancel=True)

	def on_submit_issue_new(self, cancel=False):
		# if self.mop_data:
		# 	mop_data = json.loads(self.mop_data)
		# 	return create_single_se_entry(self, mop_data)
		if cancel:
			affected_mops_issue = [
				r[0]
				for r in frappe.db.sql(
					"""
					SELECT DISTINCT manufacturing_operation FROM `tabMOP Log`
					WHERE voucher_type = %s AND voucher_no = %s AND is_cancelled = 0
					  AND manufacturing_operation IS NOT NULL
					""",
					(self.doctype, self.name),
				)
			]
			frappe.db.set_value(
				"MOP Log",
				{
					"voucher_type": self.doctype,
					"voucher_no": self.name,
					"is_cancelled": 0,
				},
				"is_cancelled",
				1,
			)
			from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
				recalculate_manufacturing_operation_weights,
			)

			for mop_name in affected_mops_issue:
				recalculate_manufacturing_operation_weights(mop_name)
		# Set initial values based on cancel flag
		employee = None if cancel else self.employee
		operation = None if cancel else self.operation
		status = "Not Started" if cancel else "WIP"
		values = {"operation": operation, "status": status}
		if self.subcontracting == "Yes":
			values["for_subcontracting"] = 1
			values["subcontractor"] = None if cancel else self.subcontractor
		else:
			values["employee"] = employee

		# mop_data = {}
		mops_to_update = {}
		time_log_args = []
		# stock_entry_data = []
		start_time = frappe.utils.now() if not cancel else None
		from_warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"department": self.department,
				"warehouse_type": "Manufacturing",
			},
		)
		if self.subcontracting == "Yes":
			to_warehouse = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"company": self.company,
					"subcontractor": self.subcontractor,
					"warehouse_type": "Manufacturing",
				},
			)
		else:
			to_warehouse = frappe.db.get_value(
				"Warehouse",
				{
					"warehouse_type": "Manufacturing",
					"disabled": 0,
					"employee": self.employee,
				},
			)
		if not (to_warehouse and from_warehouse):
			frappe.throw(_("To Warehouse or From Warehouse not available"))
		for row in self.employee_ir_operations:
			values.update(
				{
					"operation": operation,
					"rpt_wt_issue": row.rpt_wt_issue,
					"start_time": start_time,
				}
			)
			mops_to_update[row.manufacturing_operation] = values
			if not cancel:
				# stock_entry_data.append(
				# 	(row.manufacturing_work_order, row.manufacturing_operation)
				# )
				# mop_data[row.manufacturing_work_order] = row.manufacturing_operation
				time_log_args.append((row.manufacturing_operation, values))
				creste_mop_log_for_employee_ir(self, row, from_warehouse, to_warehouse)

		if mops_to_update:
			frappe.db.bulk_update(
				"Manufacturing Operation",
				mops_to_update,
				chunk_size=100,
				update_modified=True,
			)

		# Batch add time logs
		if time_log_args and not cancel:
			batch_add_time_logs(self, time_log_args)

		# Casting tree: create on issue, release+remove on issue cancel.
		if not cancel:
			create_tree_on_issue(self)
		else:
			unlink_tree_on_issue_cancel(self)

		self._refresh_msl_tracking()

	# for receive
	def on_submit_receive(self, cancel=False):
		# Canonical lock order (lock_order.py): the Tree Number is a PARENT CONTROL ROW and must
		# be locked at position 1 — before any tabSeries / tabBin lock taken further down by the
		# Main Slip injection and the Process Loss entries. Locking it only at
		# update_tree_on_receive time (the tail of this method) would let this transaction hold
		# Bins while waiting on a Tree that a concurrent Tree Number button holds while waiting on
		# those same Bins: a textbook 1213 cycle.
		lock_trees_for_eir(self)

		precision = cint(
			frappe.db.get_single_value("System Settings", "float_precision")
		)

		mwo_loss_dict = {}
		for row in self.manually_book_loss_details + self.employee_loss_details:
			if row.variant_of in ["M", "F"]:
				mwo_loss_dict.setdefault(row.manufacturing_work_order, 0)
				mwo_loss_dict[row.manufacturing_work_order] += row.proportionally_loss

		is_mould_operation = frappe.db.get_value(
			"Department Operation", self.operation, "is_mould_manufacturer"
		)

		# Resolve warehouses for MOP Log entries
		department_wh = frappe.db.get_value(
			"Warehouse",
			{
				"disabled": 0,
				"department": self.department,
				"warehouse_type": "Manufacturing",
			},
		)
		if self.subcontracting == "Yes":
			actor_wh = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"company": self.company,
					"subcontractor": self.subcontractor,
					"warehouse_type": "Manufacturing",
				},
			)
		else:
			actor_wh = frappe.db.get_value(
				"Warehouse",
				{
					"disabled": 0,
					"employee": self.employee,
					"warehouse_type": "Manufacturing",
				},
			)

		curr_time = frappe.utils.now()

		if cancel:
			# Cancel Process Loss SEs and restore SREs before MOP Log flip.
			cancel_loss_stock_entries(self)

			# Capture which Manufacturing Operations need bucket recompute BEFORE
			# the bulk is_cancelled flip — afterwards the rows are filtered out
			# by the `is_cancelled = 0` clause the recompute uses.
			affected_mops = [
				r[0]
				for r in frappe.db.sql(
					"""
					SELECT DISTINCT manufacturing_operation FROM `tabMOP Log`
					WHERE voucher_type = %s AND voucher_no = %s AND is_cancelled = 0
					  AND manufacturing_operation IS NOT NULL
					""",
					(self.doctype, self.name),
				)
			]
			frappe.db.set_value(
				"MOP Log",
				{
					"voucher_type": self.doctype,
					"voucher_no": self.name,
					"is_cancelled": 0,
				},
				"is_cancelled",
				1,
			)
			# Cancel any auto-created Main Slip Repack SEs; their on_cancel hook
			# flips the matching MOP Log rows to is_cancelled=1 via the bridge.
			cancel_injections_for_eir(self.name)
			# Bulk db.set_value above bypasses MOPLog.validate, so the prefix
			# buckets stay stale showing pre-cancel balances. Re-run the central
			# aggregator on each affected MOP so gross_wt restores to the
			# remaining-active-rows balance.
			from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
				recalculate_manufacturing_operation_weights,
			)

			for mop_name in affected_mops:
				recalculate_manufacturing_operation_weights(mop_name)

		if not cancel:
			# Canonical lock order for the EIR receive cascade: it mints several Stock
			# Entries (per-operation metal injections + the combined Process Loss SE), each
			# otherwise locking its Bins independently. Pin the Stock Entry series, then
			# pre-lock the manufacturing-warehouse Bins this receive draws on, in sorted
			# order, so concurrent EIR/SNC/PC submits acquire shared Bins in the same
			# sequence. Loss-SE source Bins resolved later are still locked (in sorted order)
			# by each SE's prelock_bins hook; create_loss_stock_entries reduces SREs in
			# stock_lock_key order (RULE A).
			from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.loss_stock_entry import (
				PROCESS_LOSS_SE_TYPE,
			)
			from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject import (
				MATERIAL_TRANSFER_STOCK_ENTRY_TYPE,
				REPACK_STOCK_ENTRY_TYPE,
			)
			from jewellery_erpnext.jewellery_erpnext.lock_order import (
				lock_bins,
				preallocate_series_for_docs,
				series_stubs,
			)

			_eir_pairs = [
				(getattr(r, "item_code", None), wh)
				for r in (self.manually_book_loss_details + self.employee_loss_details)
				for wh in (department_wh, actor_wh)
			]
			# Pin each nested SE type's naming counter (the per-(company x type)
			# Document Naming Rule counter post-reshard, or the tabSeries fallback)
			# BEFORE the Bins -- a blank stub matches no naming rule and would pin
			# the wrong (shared MAT-STE-) row while leaving the real counters unpinned.
			preallocate_series_for_docs(
				*series_stubs(
					self.company,
					MATERIAL_TRANSFER_STOCK_ENTRY_TYPE,
					REPACK_STOCK_ENTRY_TYPE,
					PROCESS_LOSS_SE_TYPE,
				)
			)
			lock_bins(_eir_pairs)

		for row in self.employee_ir_operations:
			if is_mould_operation and not cancel:
				create_mould(self, row)
			net_loss_wt = mwo_loss_dict.get(row.manufacturing_work_order) or 0

			net_wt = frappe.db.get_value(
				"Manufacturing Operation", row.manufacturing_operation, "net_wt"
			)
			is_received_gross_greater_than = (
				True if row.received_gross_wt > row.gross_wt else False
			)
			difference_wt = flt(row.received_gross_wt, precision) - flt(
				row.gross_wt, precision
			)

			res = frappe._dict(
				{
					"received_gross_wt": row.received_gross_wt,
					"loss_wt": difference_wt,
					"received_net_wt": flt(net_wt - net_loss_wt, precision),
					"status": "WIP",
					"is_received_gross_greater_than": is_received_gross_greater_than,
				}
			)

			if row.received_gross_wt == 0 and row.gross_wt != 0:
				frappe.throw(_("Row {0}: Received Gross Wt Missing").format(row.idx))

			time_log_args = []
			if not cancel:
				res["status"] = "Finished"
				res["employee"] = self.employee
				new_operation = create_operation_for_next_op(
					row.manufacturing_operation,
					employee_ir=self.name,
					gross_wt=row.gross_wt,
				)

				frappe.db.set_value(
					"Manufacturing Work Order",
					row.manufacturing_work_order,
					"manufacturing_operation",
					new_operation.name,
				)
				time_log_args.append(
					(row.manufacturing_operation, {**res, "complete_time": curr_time})
				)

				# Main Slip gain injection: when is_raw_material and
				# received_gross_wt > gross_wt, repack the delta from the
				# employee/subcontractor warehouse into the MOP warehouse.
				# The SE bridge then writes the positive MOP Log row that
				# create_mop_log_for_employee_ir_receive will see.
				stock_entry_name = inject_extra_metal_for_eir_receive(self, row)

				# Combined-loss receive: create_mop_log_for_employee_ir_receive
				# now subtracts employee_loss_details + manually_book_loss_details
				# directly from each receive MOP Log row. There is no longer a
				# separate Loss Attribution writer pass — the loss audit
				# metadata (loss_weight, loss_source_row, loss_type) lives on
				# the combined receive row itself, and MOPLog.validate updates
				# Manufacturing Operation buckets exactly once.
				# The receive audit clones land on the SOURCE MOP, which may
				# belong to a different department than the EIR header. Resolve
				# a guaranteed destination so to_warehouse is never blank:
				# header department WH -> row MOP's department WH -> latest
				# non-null to_warehouse already on the MWO's logs. Fallback only;
				# we never block the submit (see _resolve_department_warehouse /
				# _get_t_warehouse_from_logs in mop_eod_sync).
				row_to_wh = department_wh
				if not row_to_wh:
					row_to_wh = _resolve_department_warehouse(
						frappe.get_cached_doc(
							"Manufacturing Operation", row.manufacturing_operation
						)
					) or _get_t_warehouse_from_logs(
						frappe.get_all(
							"MOP Log",
							filters={
								"manufacturing_work_order": row.manufacturing_work_order,
								"is_cancelled": 0,
							},
							fields=["to_warehouse", "flow_index", "creation"],
						)
					)

				create_mop_log_for_employee_ir_receive(
					self, row, actor_wh, row_to_wh, stock_entry_name
				)

				# update_new_mop_wtg now does both jobs in one pass:
				# clones the previous MOP's flow_index=0 baseline rows AND
				# subtracts loss in-place per (item, batch). One MOP Log
				# row per item/batch on the new operation. Source MOP is
				# left unchanged.
				update_new_mop_wtg(
					new_operation,
					employee_ir_doc=self,
					employee_ir_operation_row=row,
					from_warehouse=actor_wh,
					to_warehouse=row_to_wh,
				)
			else:
				for sre in frappe.db.get_all(
					"Stock Reservation Entry",
					{
						"manufacturing_work_order": row.manufacturing_work_order,
						"manufacturing_operation": row.manufacturing_operation,
						"docstatus": 1,
					},
					pluck="name",
				):
					frappe.get_doc("Stock Reservation Entry", sre).cancel()

				next_op_name = frappe.db.get_value(
					"Manufacturing Operation",
					{
						"employee_ir": self.name,
						"previous_mop": row.manufacturing_operation,
					},
				)

				frappe.db.set_value(
					"Manufacturing Work Order",
					row.manufacturing_work_order,
					"manufacturing_operation",
					row.manufacturing_operation,
				)
				if next_op_name:
					frappe.db.set_value(
						"Department IR Operation",
						{
							"docstatus": 2,
							"manufacturing_operation": next_op_name,
						},
						"manufacturing_operation",
						None,
					)
					frappe.delete_doc(
						"Manufacturing Operation",
						next_op_name,
						ignore_permissions=1,
					)

				frappe.db.set_value(
					"Manufacturing Operation",
					row.manufacturing_operation,
					"status",
					"Not Started",
				)

			if row.rpt_wt_receive:
				issue_wt = frappe.db.get_value(
					"Manufacturing Operation",
					row.manufacturing_operation,
					"rpt_wt_issue",
				)
				res["rpt_wt_receive"] = row.rpt_wt_receive
				res["rpt_wt_loss"] = flt(row.rpt_wt_receive - issue_wt, 3)

			frappe.db.set_value(
				"Manufacturing Operation", row.manufacturing_operation, res
			)

			if time_log_args and not cancel:
				batch_add_time_logs(self, time_log_args)

		if not cancel:
			# ONE Repack SE for all loss rows across the entire EIR.
			create_loss_stock_entries(self)

		# Casting tree: this receive draws metal OUT of the tree only to the extent it returned
		# more than the operation carried (received_gross_wt - gross_wt, per row) — exactly what
		# the Main Slip injection minted out of the tree's MSL warehouse. The tree Receive button
		# separately returns the post-cast LEFTOVER to Dept RM, and both are capped by pending, so
		# the two can never overlap. Runs on submit and cancel. Early-returns for non-casting EIRs.
		if not cancel:
			# Pin the tree per row BEFORE applying, so a later re-issue (which repoints
			# MWO.tree_number at a brand-new tree) cannot make this voucher's cancel reverse
			# against the wrong ledger.
			pin_tree_numbers_on_receive(self)
		update_tree_on_receive(self, cancel=cancel)

		self._refresh_msl_tracking()

	def _refresh_msl_tracking(self):
		"""Re-materialize the employee's Raw Material (MSL) warehouse tracking
		table from the ledger after Issue/Receive posts stock.

		``custom_msl_tracking`` is a materialized cache — its only source of
		truth is ``recalculate_msl_tracking`` (a full recompute from the Stock
		Ledger). The Employee IR Issue/Receive posts SLEs against the employee's
		Raw Material (MSL) warehouse but never refreshed this cache, so the
		on-form Receive/Pending qty drifted from the ledger. Mirror the Employee
		Loss Entry / warehouse-button pattern. A refresh failure must never roll
		back the stock posting, so failures are logged, not raised.
		"""
		from jewellery_erpnext.jewellery_erpnext.doc_events.warehouse_tracking import (
			recalculate_msl_tracking,
		)
		from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.main_slip_inject import (
			_resolve_source_warehouse_raw_material,
		)

		try:
			msl_wh = _resolve_source_warehouse_raw_material(self)
			if msl_wh:
				recalculate_msl_tracking(msl_wh)
		except Exception:
			frappe.log_error(
				title="Employee IR: MSL tracking refresh failed",
				message=frappe.get_traceback(),
			)

	def validate_qc(self, action="Warn"):
		if not self.is_qc_reqd or self.type == "Receive":
			return

		qc_list = []
		for row in self.employee_ir_operations:
			operation = frappe.db.get_value(
				"Manufacturing Operation",
				row.manufacturing_operation,
				["status"],
				as_dict=1,
			)
			if operation.get("status") == "Not Started":
				if action == "Warn":
					create_qc_record(row, self.operation, self.name)
				qc_list.append(row.manufacturing_operation)
		if qc_list:
			msg = _("Please complete QC for the following: {0}").format(
				", ".join(qc_list)
			)
			if action == "Warn":
				frappe.msgprint(msg)
			elif action == "Stop":
				frappe.msgprint(msg)

	@frappe.whitelist()
	def create_subcontracting_order(self):
		service_item = frappe.db.get_value(
			"Department Operation", self.operation, "service_item"
		)
		if not service_item:
			frappe.throw(_("Please set service item for {0}").format(self.operation))
		po = frappe.new_doc("Purchase Order")
		po.supplier = self.subcontractor
		company = frappe.db.get_value(
			"Company", {"supplier_code": self.subcontractor}, "name"
		)
		po.company = self.company or company
		po.employee_ir = self.name
		po.purchase_type = "FG Purchase"

		for row in self.employee_ir_operations:
			rate = get_po_rates(
				self.subcontractor, self.operation, po.purchase_type, row
			)
			pmo = frappe.db.get_value(
				"Manufacturing Work Order",
				row.manufacturing_work_order,
				"manufacturing_order",
			)
			po.append(
				"items",
				{
					"item_code": service_item,
					"qty": 1,
					"custom_gross_wt": row.gross_wt,
					"rate": flt(rate[0].get("rate_per_gm") * row.gross_wt, 3)
					if rate
					else 0,
					"schedule_date": today(),
					"manufacturing_operation": row.manufacturing_operation,
					"custom_pmo": pmo,
				},
			)
		if not po.items:
			return
		po.flags.ignore_mandatory = True
		po.taxes_and_charges = None
		po.taxes = []
		po.save()
		po.db_set("schedule_date", None)
		for row in po.items:
			row.db_set("schedule_date", None)

		supplier_group = frappe.db.get_value(
			"Supplier", self.subcontractor, "supplier_group"
		)
		if frappe.db.get_value(
			"Supplier Group", supplier_group, "custom_create_so_for_subcontracting"
		):
			create_so_for_subcontracting(po)

	@frappe.whitelist()
	def validate_process_loss(self):
		if (self.docstatus != 0) or self.type == "Issue":
			return
		allowed_loss_percentage = frappe.get_cached_value(
			"Department Operation",
			{"company": self.company, "department": self.department},
			"allowed_loss_percentage",
		)
		# Per-operation finding-category loss gate, read once for the whole
		# document rather than per book_metal_loss call. Keyed on self.operation
		# (the Department Operation actually being received), not on the
		# {company, department} filter dict used for allowed_loss_percentage above.
		booking_map = get_loss_booking_map(self.operation)

		# Recomputed from scratch on every validate, so the spill collected by the
		# previous run must not leak into this one.
		self.flags.customer_loss_spill = []
		self.flags.loss_overflow = []

		rows_to_append = []
		for child in self.employee_ir_operations:
			if child.received_gross_wt and self.type == "Receive":
				mwo = child.manufacturing_work_order
				gwt = child.gross_wt
				opt = child.manufacturing_operation
				r_gwt = child.received_gross_wt
				rows_to_append += self.book_metal_loss(
					mwo, opt, gwt, r_gwt, allowed_loss_percentage, booking_map
				)

		booked_rows = [
			r for r in rows_to_append if flt(r["proportionally_loss"], 3) > 0
		]
		# Two bulk maps instead of three DB round-trips per booked row
		# (Item.variant_of, plus Batch -> Customer inside batch_owner_no_wastage).
		variant_map = _bulk_variant_of([r["item_code"] for r in booked_rows])
		no_wastage_batches = _bulk_no_wastage_batches(
			[r.get("batch_no") for r in booked_rows]
		)

		self.employee_loss_details = []
		for row in booked_rows:
			proportionally_loss = flt(row["proportionally_loss"], 3)
			# No wastage for customer-supplied material: block loss on a no-wastage
			# customer's batch so it never becomes a customer-owned scrap batch. The
			# operator must receive the full weight (received == issued) for the
			# operation holding this customer's material; the unused metal returns as
			# raw material. Such batches rank LAST in the loss waterfall, so reaching
			# one means nothing else on the operation had any capacity left — the
			# throw is the correct outcome, not an accident of proportional spreading.
			if row.get("batch_no") in no_wastage_batches:
				frappe.throw(
					_(
						"No wastage is allowed for customer material (MWO {0}, operation "
						"{1}, batch {2}). Set the received gross weight equal to the issued "
						"weight so no loss is booked; the unused metal is returned as raw "
						"material."
					).format(
						row.get("manufacturing_work_order"),
						row.get("manufacturing_operation"),
						row.get("batch_no"),
					)
				)
			self.append(
				"employee_loss_details",
				{
					"item_code": row["item_code"],
					"net_weight": row["qty"],
					# "stock_uom": row["stock_uom"],
					"variant_of": variant_map.get(row["item_code"]),
					"batch_no": row["batch_no"],
					"manufacturing_work_order": row["manufacturing_work_order"],
					"manufacturing_operation": row["manufacturing_operation"],
					"proportionally_loss": proportionally_loss,
					"received_gross_weight": row["received_gross_weight"],
					"main_slip_consumption": row.get("main_slip_consumption"),
					# "inventory_type": row["inventory_type"],
					"customer": row.get("customer"),
				},
			)

		# ONE warning for the whole document, after every operation row is booked.
		self._warn_customer_loss_spill()

		# Pre-deduction MOP baseline: total loss available from the operations
		# before any manual deduction. Drives downstream caps and serves as the
		# reference for `remaining_loss = baseline - sum(manually_book_loss)`.
		mop_baseline = 0.0
		for child in self.employee_ir_operations:
			if child.received_gross_wt and flt(child.gross_wt) > flt(
				child.received_gross_wt
			):
				mop_baseline += flt(child.gross_wt) - flt(child.received_gross_wt)
		self.mop_loss_details_total = flt(mop_baseline, 3)

	@frappe.whitelist()
	def book_metal_loss(
		self, mwo, opt, gwt, r_gwt, allowed_loss_percentage=None, booking_map=None
	):
		doc = self
		# booking_map is prefetched once by validate_process_loss; resolve it here
		# for direct/whitelisted callers so the gate can never be bypassed by
		# calling this method on its own.
		if booking_map is None:
			booking_map = get_loss_booking_map(self.operation)
		# mnf_opt = frappe.get_doc("Manufacturing Operation", opt)

		# To Check Tollarance which book a loss down side.
		if allowed_loss_percentage:
			cal = round(flt((100 - allowed_loss_percentage) / 100) * flt(gwt), 2)
			if flt(r_gwt) < cal:
				frappe.throw(
					f"Department Operation Standard Process Loss Percentage set by <b>{allowed_loss_percentage}%. </br> Not allowed to book a loss less than {cal}</b>"
				)
		data = []  # for final data list
		# Fetching Stock Entry based on MNF Work Order
		if gwt != r_gwt:
			# Forensic C4 fix: previously aliased pcs_after_transaction_batch_based
			# to BOTH qty and pcs, making the proportional loss formula run on
			# PCS counts instead of grams. Use the weight column for qty so
			# downstream `stock_loss = (entry["qty"] * loss) / total_qty` produces
			# gram-based outputs matching the spec examples.
			fields = [
				"item_code",
				"batch_no",
				"qty_after_transaction_batch_based as qty",
				"pcs_after_transaction_batch_based as pcs",
			]
			mop_balance_table = (
				frappe.db.get_all(
					"MOP Log",
					{
						"manufacturing_work_order": mwo,
						"manufacturing_operation": opt,
						"is_cancelled": 0,
					},
					fields,
					order_by="creation asc",
				)
				or []
			)
			# Declaration & fetch required value
			sum_qty = {}  # for sum of qty matched item

			# Finding-category loss gate: a finding whose category is flagged
			# "no loss booking" for this Department Operation drops out of the
			# proportional pool below, BEFORE total_qty is summed — so the
			# remaining rows absorb its share and the booked total still equals
			# flt(loss, 3). Categories that are not listed book loss as before.
			# Skipped entirely when the operation configures no categories — which
			# is the common case — so the gate costs zero extra queries by default.
			category_map = (
				get_finding_category_map(
					[child["item_code"] for child in mop_balance_table]
				)
				if booking_map
				else {}
			)

			# Keep only the latest qty snapshot per (item_code, batch_no).
			# qty_after_transaction_batch_based is a running balance so the last
			# row in creation order is the current stock for that batch.
			latest_per_batch = {}
			blocked_categories = set()
			for child in mop_balance_table:
				if child["item_code"][0] not in ["M", "F"]:
					continue
				if is_loss_booking_blocked(
					child["item_code"], booking_map, category_map
				):
					blocked_categories.add(category_map.get(child["item_code"]))
					continue
				latest_per_batch[(child["item_code"], child["batch_no"])] = child

			# Every eligible row was gated out, so the shortfall has nothing to be
			# booked against. Fail here naming the cause rather than letting
			# validate_loss_tables_required raise its generic "no loss details found".
			# Only a shortfall needs attributing; a receive that gained weight books
			# no loss rows either way.
			if (
				blocked_categories
				and not latest_per_batch
				and flt(gwt, 3) > flt(r_gwt, 3)
			):
				frappe.throw(
					_(
						"Manufacturing Work Order {0}: the receive is short by {1} g but every "
						"item in the operation balance belongs to a finding category with Loss "
						"Booking turned off ({2}) on operation <b>{3}</b>. There is nothing left "
						"to book the loss against — either receive the full issued weight, or "
						"tick Loss Booking for one of those categories on the Department Operation."
					).format(
						mwo,
						flt(flt(gwt, 3) - flt(r_gwt, 3), 3),
						", ".join(sorted(c for c in blocked_categories if c)),
						doc.operation,
					)
				)

			total_qty = 0
			for key, child in latest_per_batch.items():
				total_qty += child["qty"]
				sum_qty[key] = {
					"item_code": child["item_code"],
					"qty": child["qty"],
					"batch_no": child["batch_no"],
					"manufacturing_work_order": mwo,
					"manufacturing_operation": opt,
					"pcs": child["pcs"],
					"proportionally_loss": 0.0,
					"received_gross_weight": 0.0,
				}
			data = list(sum_qty.values())

			# Ownership tier + reservation headroom for every batch in the balance,
			# resolved in two round-trips for the whole operation rather than per row.
			ownership = batch_priority_map(
				[e["batch_no"] for e in data], with_no_wastage=True
			)
			headroom = get_batch_sre_headroom(mwo, [e["batch_no"] for e in data])
			for entry in data:
				meta = ownership.get(entry["batch_no"])
				entry["_inventory_type"] = meta.get("inventory_type") if meta else None
				entry["_customer"] = meta.get("customer") if meta else None
				entry["_no_wastage"] = bool(meta.get("no_wastage")) if meta else False
				# Capacity drives the tier cap and the within-tier proportion; `qty`
				# stays the true MOP balance so received_gross_weight is unaffected.
				# They differ only when a reservation is smaller than the balance,
				# i.e. exactly the case that would otherwise trip _validate_sre_qty.
				cap = headroom.get((entry["item_code"], entry["batch_no"]))
				entry["_capacity"] = (
					min(flt(entry["qty"]), flt(cap)) if cap else flt(entry["qty"])
				)

			# -------------------------------------------------------------------------
			# Prepare data and calculation proportionally devide each row based on each qty.
			total_mannual_loss = 0
			if len(doc.manually_book_loss_details) > 0:
				for row in doc.manually_book_loss_details:
					if row.manufacturing_work_order == mwo:
						loss_qty = (
							row.proportionally_loss
							if row.stock_uom != "Carat"
							else (row.proportionally_loss * 0.2)
						)
						total_mannual_loss += loss_qty

			loss = flt(flt(gwt, 3) - flt(r_gwt, 3) - flt(total_mannual_loss, 3), 3)
			ms_consum = 0
			ms_consum_book = 0
			if loss < 0:
				ms_consum = abs(round(loss, 2))

			# Ownership waterfall. Loss lands on Regular Stock first, then Pure
			# Metal, and reaches a customer's gold only when nothing else on the
			# operation has capacity left. Within a tier the split is proportional
			# by balance, so an operation whose batches are all one tier — every
			# site with no customer-owned metal — books exactly what the previous
			# flat split booked, row for row. tiered_allocate also owns the
			# precision-3 reconciliation: it guarantees the booked total equals
			# flt(loss, 3) exactly, which validate_loss_tables_required asserts
			# against the gross_wt - received_gross_wt baseline (within 0.0005).
			if total_qty != 0 and loss > 0:
				allocations, alloc_info = tiered_allocate(
					data,
					loss,
					rank_of=lambda e: loss_rank(e["_inventory_type"], e["_no_wastage"]),
					qty_of=lambda e: flt(e["_capacity"]),
					precision=3,
					key_of=lambda e: (e["item_code"], e["batch_no"]),
				)
				for entry in data:
					booked = flt(
						allocations.get((entry["item_code"], entry["batch_no"])), 3
					)
					entry["proportionally_loss"] = booked
					entry["received_gross_weight"] = (
						flt(entry["qty"] - booked, 3) if booked else 0
					)
					entry["main_slip_consumption"] = 0
					if booked and is_customer_rank(
						loss_rank(entry["_inventory_type"], entry["_no_wastage"])
					):
						doc._collect_customer_loss_spill(entry, booked)
				if alloc_info.overflow:
					# Loss exceeded every batch's capacity on this operation. The
					# excess is anchored on the FIRST funded tier (company metal) by
					# tiered_allocate — never on the customer — so nothing here has
					# to redistribute it, but it is worth surfacing.
					doc._collect_loss_overflow(mwo, opt, alloc_info.overflow)
			elif total_qty != 0 and ms_consum:
				# Gain: nothing is lost, the operation drew extra from the Main Slip.
				for entry in data:
					ms_consum_book = round((ms_consum * entry["qty"]) / total_qty, 4)
					entry["proportionally_loss"] = 0
					entry["received_gross_weight"] = 0
					entry["main_slip_consumption"] = ms_consum_book

			for entry in data:
				for helper_key in (
					"_inventory_type",
					"_customer",
					"_no_wastage",
					"_capacity",
				):
					entry.pop(helper_key, None)
			# -------------------------------------------------------------------------
		return data

	def _collect_customer_loss_spill(self, entry, qty):
		"""Record that customer-owned metal absorbed loss, for ONE warning later.

		Deliberately data, not a ``msgprint``. ``book_metal_loss`` is reached from
		``validate`` on every draft save and runs once per operation row, so warning
		in place would fire repeatedly per save and again from every whitelisted
		caller. ``validate_process_loss`` emits a single deduplicated message after
		the loop instead.
		"""
		spill = self.flags.setdefault("customer_loss_spill", [])
		spill.append(
			{
				"customer": entry.get("_customer"),
				"item_code": entry.get("item_code"),
				"batch_no": entry.get("batch_no"),
				"qty": flt(qty, 3),
			}
		)

	def _collect_loss_overflow(self, mwo, opt, qty):
		"""Record loss that exceeded every batch's capacity on an operation."""
		self.flags.setdefault("loss_overflow", []).append(
			{"mwo": mwo, "operation": opt, "qty": flt(qty, 3)}
		)

	def _warn_customer_loss_spill(self):
		"""Emit ONE orange warning naming the customer metal that absorbed loss.

		Warns whenever a customer tier was funded at all -- not only when the
		waterfall overflowed. The ordinary business case is "regular stock ran out
		and the remainder landed on the customer's gold", which produces no overflow
		and is exactly what the operator needs to see.

		Never throws: spilling is allowed. The one hard stop remains
		``batch_owner_no_wastage`` below, and that batch is ranked last precisely so
		the waterfall reaches it only when nothing else can absorb the loss.
		"""
		spill = self.flags.get("customer_loss_spill") or []
		if not spill:
			return

		merged = {}
		for row in spill:
			key = (row["customer"], row["item_code"], row["batch_no"])
			merged[key] = flt(merged.get(key, 0) + flt(row["qty"]), 3)

		lines = describe_customer_spill(
			[
				{"customer": c, "item_code": i, "batch_no": b, "qty": q}
				for (c, i, b), q in sorted(merged.items(), key=lambda kv: str(kv[0]))
			]
		)
		total = flt(sum(merged.values()), 3)
		frappe.msgprint(
			_(
				"Company stock could not absorb the whole process loss, so {0} g was "
				"booked against customer-owned material:"
			).format(frappe.bold(total))
			+ "<br><br>"
			+ "<br>".join(lines),
			title=_("Customer Material Absorbed Loss"),
			indicator="orange",
		)
		# Durable trace: Employee IR submit runs on queue="long" (CustomSubmissionQueue),
		# where a msgprint lands in the job log rather than the operator's browser.
		self.flags.customer_loss_spill_total = total

	@frappe.whitelist()
	def get_summary_data(self):
		return get_summary_data(self)


def _bulk_variant_of(item_codes):
	"""``{item_code: variant_of}`` in one round-trip.

	Replaces a per-row ``frappe.db.get_value("Item", ..., "variant_of")`` inside the
	loss append loop.
	"""
	item_codes = sorted({i for i in (item_codes or []) if i})
	if not item_codes:
		return {}
	return {
		r["name"]: r["variant_of"]
		for r in frappe.db.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "variant_of"],
		)
	}


def _bulk_no_wastage_batches(batch_nos):
	"""Batches whose owning Customer is flagged ``custom_no_wastage``.

	Same answer as calling ``loss_stock_entry.batch_owner_no_wastage`` per row, in
	two round-trips instead of two per row.
	"""
	batch_nos = sorted({b for b in (batch_nos or []) if b})
	if not batch_nos:
		return set()
	batches = frappe.db.get_all(
		"Batch",
		filters={"name": ["in", batch_nos], "custom_customer": ["is", "set"]},
		fields=["name", "custom_customer"],
	)
	customers = sorted({b["custom_customer"] for b in batches if b["custom_customer"]})
	if not customers:
		return set()
	flagged = {
		c["name"]
		for c in frappe.db.get_all(
			"Customer",
			filters={"name": ["in", customers], "custom_no_wastage": 1},
			fields=["name"],
		)
	}
	return {b["name"] for b in batches if b["custom_customer"] in flagged}


def create_operation_for_next_op(docname, employee_ir=None, gross_wt=0):
	new_mop_doc = frappe.copy_doc(
		frappe.get_doc("Manufacturing Operation", docname), ignore_no_copy=False
	)
	new_mop_doc.name = None
	new_mop_doc.department_issue_id = None
	new_mop_doc.status = "Not Started"
	new_mop_doc.department_ir_status = None
	new_mop_doc.department_receive_id = None
	new_mop_doc.prev_gross_wt = gross_wt
	new_mop_doc.employee_ir = employee_ir
	new_mop_doc.employee = None
	new_mop_doc.previous_mop = docname
	new_mop_doc.operation = None
	new_mop_doc.previous_se_data_updated = 0
	new_mop_doc.save()
	return new_mop_doc


@frappe.whitelist()
def get_manufacturing_operations(source_name, target_doc=None):
	if not target_doc:
		target_doc = frappe.new_doc("Employee IR")
	elif isinstance(target_doc, str):
		target_doc = frappe.get_doc(json.loads(target_doc))
	if not target_doc.get(
		"employee_ir_operations", {"manufacturing_operation": source_name}
	):
		operation = frappe.db.get_value(
			"Manufacturing Operation",
			source_name,
			[
				"gross_wt",
				"manufacturing_work_order",
				"diamond_wt",
				"diamond_pcs",
				"gemstone_wt",
				"gemstone_pcs",
			],
			as_dict=1,
		)
		target_doc.append(
			"employee_ir_operations",
			{
				"manufacturing_operation": source_name,
				"gross_wt": operation["gross_wt"],
				"manufacturing_work_order": operation["manufacturing_work_order"],
				"diamond_wt": operation.get("diamond_wt"),
				"diamond_pcs": operation.get("diamond_pcs"),
				"gemstone_wt": operation.get("gemstone_wt"),
				"gemstone_pcs": operation.get("gemstone_pcs"),
			},
		)
	return target_doc


@frappe.whitelist()
def get_casting_group_operations(department, subcontracting, present_operations):
	"""Return the issue-eligible sibling MOP rows of the casting tree(s) already in this EIR.

	Powers the "Load Full Casting Tree" button: given the operations already present, resolve
	their MWOs' casting groups and return every still-at-casting sibling MOP not yet present, so
	one click completes the tree. Shares ``eligible_casting_group_mops`` with the submit-time
	completeness validator, so the button can never disagree with that check when it is enabled.

	Deliberately NOT gated by ``MOP Settings.enforce_full_casting_tree_reissue``: assembling a
	whole tree in one click is useful whether or not the rule is enforced, and gating it would
	only make the helper disappear exactly when someone turns the rule on to use it.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.tree_casting import (
		eligible_casting_group_mops,
	)

	present = (
		json.loads(present_operations)
		if isinstance(present_operations, str)
		else (present_operations or [])
	)
	present = {p for p in present if p}
	if not present:
		return []

	mwo_names = frappe.get_all(
		"Manufacturing Operation",
		{"name": ["in", list(present)]},
		pluck="manufacturing_work_order",
	)
	groups = set(
		frappe.get_all(
			"Manufacturing Work Order",
			{"name": ["in", [m for m in mwo_names if m]]},
			pluck="casting_group",
		)
	)
	eligible = eligible_casting_group_mops(department, subcontracting, groups)
	return [m for m in eligible if m["manufacturing_operation"] not in present]


def create_qc_record(row, operation, employee_ir):
	item = frappe.db.get_value(
		"Manufacturing Operation", row.manufacturing_operation, "item_code"
	)
	category = frappe.db.get_value("Item", item, "item_category")
	template_based_on_cat = frappe.db.get_all(
		"Category MultiSelect", {"category": category}, pluck="parent"
	)
	templates = frappe.db.get_all(
		"Operation MultiSelect",
		{
			"operation": operation,
			"parent": ["in", template_based_on_cat],
			"parenttype": "Quality Inspection Template",
		},
		pluck="parent",
	)
	if not templates:
		frappe.msgprint(
			f"No Templates found for given category and operation i.e. {category} and {operation}"
		)
	for template in templates:
		# if frappe.db.sql(
		# 	f"""select name from `tabQC` where manufacturing_operation = '{row.manufacturing_operation}' and
		# 			quality_inspection_template = '{template}' and ((docstatus = 1 and status in ('Accepted', 'Force Approved')) or docstatus = 0)"""
		# ):
		QC = DocType("QC")
		query = (
			frappe.qb.from_(QC)
			.select(QC.name)
			.where(
				(QC.manufacturing_operation == row.manufacturing_operation)
				& (QC.quality_inspection_template == template)
				& (
					(
						(QC.docstatus == 1)
						& (QC.status.isin(["Accepted", "Force Approved"]))
					)
					| (QC.docstatus == 0)
				)
			)
		)
		qc_output = query.run(as_dict=True)
		if qc_output:
			continue
		doc = frappe.new_doc("QC")
		doc.manufacturing_work_order = row.manufacturing_work_order
		doc.manufacturing_operation = row.manufacturing_operation
		doc.received_gross_wt = row.received_gross_wt
		doc.employee_ir = employee_ir
		doc.quality_inspection_template = template
		doc.posting_date = frappe.utils.getdate()
		doc.save(ignore_permissions=True)


# timer code
def add_time_log(doc, args):
	doc = frappe.get_doc("Manufacturing Operation", doc)

	doc.status = args.get("status")
	last_row = []
	employees = args.get("employee")

	# if isinstance(employees, str):
	# 	employees = json.loads(employees)
	if doc.time_logs and len(doc.time_logs) > 0:
		last_row = doc.time_logs[-1]

	doc.reset_timer_value(args)
	if last_row and args.get("complete_time"):
		for row in doc.time_logs:
			if not row.to_time:
				row.update(
					{
						"to_time": get_datetime(args.get("complete_time")),
					}
				)
	elif args.get("start_time"):
		new_args = frappe._dict(
			{
				"from_time": get_datetime(args.get("start_time")),
			}
		)

		if employees:
			new_args.employee = employees
			doc.add_start_time_log(new_args)
		else:
			doc.add_start_time_log(new_args)

	if doc.status in ["QC Pending", "On Hold"]:
		# and self.status == "On Hold":
		doc.current_time = time_diff_in_seconds(last_row.to_time, last_row.from_time)

	doc.flags.ignore_validation = True
	doc.flags.ignore_permissions = True
	doc.save()


def batch_add_time_logs(self, mop_args_list):
	"""
	Batch update time logs and Manufacturing Operation fields via doc objects.
	mop_args_list: List of (mop_name, args) tuples.
	"""
	# Batch fetch minimal data for status check
	mop_names = [mop[0] for mop in mop_args_list]
	mop_docs = frappe.get_all(
		"Manufacturing Operation",
		filters={"name": ["in", mop_names]},
		fields=["name", "status"],
	)
	mop_dict = {d.name: d for d in mop_docs}
	full_docs = {}

	for mop_name, args in mop_args_list:
		doc_data = mop_dict.get(mop_name)
		if not doc_data:
			continue

		doc = full_docs.get(mop_name) or frappe.get_doc(
			"Manufacturing Operation", mop_name
		)
		full_docs[mop_name] = doc

		new_status = args.get("status")
		if new_status and doc.status != new_status:
			doc.status = new_status

		last_row = doc.time_logs[-1] if doc.time_logs else None
		doc.reset_timer_value(args)

		if args.get("complete_time") and last_row:
			for row in doc.time_logs:
				if not row.to_time:
					row.to_time = get_datetime(args.get("complete_time"))
					calculation_time_log(doc, row, self)
					break

		elif args.get("start_time"):
			employee = args.get("employee")

			new_time_log = frappe._dict(
				{
					"from_time": get_datetime(args.get("start_time")),
					"employee": employee,
				}
			)
			doc.add_start_time_log(new_time_log)

		if (
			doc.status in ["QC Pending", "On Hold"]
			and last_row
			and last_row.to_time
			and last_row.from_time
		):
			doc.current_time = time_diff_in_seconds(
				last_row.to_time, last_row.from_time
			)

	for doc in full_docs.values():
		doc.flags.ignore_validation = True
		doc.flags.ignore_permissions = True
		doc.save()


def validate_qc(self):
	pending_qc = []
	for row in self.employee_ir_operations:
		if not row.get("qc"):
			continue

		if frappe.db.get_value("QC", row.qc, "status") not in [
			"Accepted",
			"Force Approved",
		]:
			pending_qc.append(row.qc)

	if pending_qc:
		frappe.throw(
			_("Following QC are not approved </n> {0}").format(
				", ".join(row for row in pending_qc)
			)
		)


def get_hourly_rate(employee):
	hourly_rate = 0
	now_date = nowdate()
	start_date, end_date = get_first_day(now_date), get_last_day(now_date)
	shift = get_shift(employee, start_date, end_date)
	shift_hours = (
		frappe.utils.flt(frappe.db.get_value("Shift Type", shift, "shift_hours")) or 10
	)

	base = frappe.db.get_value("Employee", employee, "ctc")

	holidays = get_holidays_for_employee(employee, start_date, end_date)
	working_days = date_diff(end_date, start_date) + 1

	working_days -= len(holidays)

	total_working_days = working_days
	target_working_hours = frappe.utils.flt(shift_hours * total_working_days)

	if target_working_hours:
		hourly_rate = frappe.utils.flt(base / target_working_hours)

	return hourly_rate


def get_shift(employee, start_date, end_date):
	Attendance = frappe.qb.DocType("Attendance")

	shift = (
		frappe.qb.from_(Attendance)
		.select(Attendance.shift)
		.distinct()
		.where(
			(Attendance.employee == employee)
			& (Attendance.attendance_date.between(start_date, end_date))
			& (Attendance.shift.notnull())
		)
		.limit(1)
	).run(pluck=True)

	if shift:
		return shift[0]

	return ""


def get_holidays_for_employee(employee, start_date, end_date):
	from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee
	from hrms.utils.holiday_list import get_holiday_dates_between

	HOLIDAYS_BETWEEN_DATES = "holidays_between_dates"

	holiday_list = get_holiday_list_for_employee(employee)
	key = f"{holiday_list}:{start_date}:{end_date}"
	holiday_dates = frappe.cache().hget(HOLIDAYS_BETWEEN_DATES, key)

	if not holiday_dates:
		holiday_dates = get_holiday_dates_between(holiday_list, start_date, end_date)
		frappe.cache().hset(HOLIDAYS_BETWEEN_DATES, key, holiday_dates)

	return holiday_dates


@frappe.whitelist()
def calculation_time_log(doc, row, self):
	# calculation of from and to time
	if row.from_time and row.to_time:
		if get_datetime(row.from_time) > get_datetime(row.to_time):
			frappe.throw(
				_("Row {0}: From time must be less than to time").format(row.idx)
			)

		row_date = getdate(row.from_time)
		doc_date = getdate(self.date_time)

		checkin_doc = frappe.db.sql(
			"""
				SELECT name, log_type ,time
				FROM `tabEmployee Checkin`
				WHERE employee = %s
				AND DATE(time) BETWEEN %s AND %s
			""",
			(row.employee, row_date, doc_date),
			as_dict=1,
		)

		# frappe.throw(f"{checkin_doc}")
		out_time = ""
		in_time = ""
		default_shift = frappe.db.get_value("Employee", row.employee, "default_shift")
		# frappe.throw(f"{default_shift}")
		for emp in checkin_doc:
			if emp.log_type == "OUT" and get_datetime(emp.time) >= row.from_time:
				out_time = get_datetime(emp.time)
			if emp.log_type == "IN" and get_datetime(emp.time) <= row.to_time:
				in_time = get_datetime(emp.time)

		if out_time and in_time:
			out_time_min = (
				time_diff_in_hours(out_time, row.from_time) * 60 if out_time else 0
			)
			in_time_min = (
				time_diff_in_hours(row.to_time, in_time) * 60 if in_time else 0
			)

			# Time in minutes
			row.time_in_mins = out_time_min + in_time_min

			# Time in HH:MM format
			out_hours = time_diff(out_time, row.from_time)
			in_hours = time_diff(row.to_time, in_time)
			total_duration = out_hours + in_hours
			row.time_in_hour = str(total_duration)[:-3]

			# Time in days based on shift
			if default_shift:
				shift_hours = frappe.db.get_value(
					"Shift Type", default_shift, ["start_time", "end_time"]
				)
				total_shift_hours = time_diff(shift_hours[1], shift_hours[0])

				if total_duration >= total_shift_hours:
					row.time_in_days = total_duration / total_shift_hours

		else:
			# Time in minutes
			row.time_in_mins = time_diff_in_hours(row.to_time, row.from_time) * 60

			# Time in HH:MM format
			full_hours = time_diff(row.to_time, row.from_time)
			row.time_in_hour = str(full_hours)[:-2]

			# Time in days based on shift
			if default_shift:
				shift_hours = frappe.db.get_value(
					"Shift Type", default_shift, ["start_time", "end_time"]
				)

				total_shift_hours = time_diff(shift_hours[1], shift_hours[0])

				if full_hours >= total_shift_hours:
					row.time_in_days = full_hours / total_shift_hours


def _validate_fg_bom_field_value(row):
	"""Reject an entered FG BOM field value that doesn't match its configured type.

	Config field_type is otherwise only advisory (the grid value is free-text), so
	without this a non-numeric Int would silently coerce to 0 and an off-list Select
	would land verbatim on the BOM.
	"""
	value = (row.value or "").strip()
	if not value:
		return
	label = row.field_label or row.field_name
	ftype = row.field_type
	if ftype == "Int":
		if not value.lstrip("-").isdigit():
			frappe.throw(_("FG BOM Field '{0}' must be a whole number.").format(label))
	elif ftype in ("Float", "Currency"):
		try:
			float(value)
		except ValueError:
			frappe.throw(_("FG BOM Field '{0}' must be a number.").format(label))
	elif ftype == "Check":
		if value not in ("0", "1"):
			frappe.throw(_("FG BOM Field '{0}' must be 0 or 1.").format(label))
	elif ftype == "Date":
		try:
			getdate(value)
		except Exception:
			frappe.throw(
				_("FG BOM Field '{0}' must be a valid date (YYYY-MM-DD).").format(label)
			)
	elif ftype == "Select":
		opts = [o.strip() for o in (row.options or "").splitlines() if o.strip()]
		if opts and value not in opts:
			frappe.throw(
				_("FG BOM Field '{0}' must be one of: {1}.").format(
					label, ", ".join(opts)
				)
			)


def _get_mwo_subcategory(mwo):
	"""Subcategory of the FG item the MWO produces.

	Must mirror what create_finished_goods_bom stamps as the FG BOM's
	item_subcategory (the copy step matches on it): the FG item is the PMO's
	new_item when present (repair replacement), else the MWO item. Resolving it
	the same way here keeps capture and apply on the same subcategory.
	"""
	if not mwo:
		return None
	mwo_row = frappe.db.get_value(
		"Manufacturing Work Order",
		mwo,
		["item_code", "manufacturing_order"],
		as_dict=True,
	)
	if not mwo_row:
		return None
	fg_item = None
	if mwo_row.manufacturing_order:
		fg_item = frappe.db.get_value(
			"Parent Manufacturing Order", mwo_row.manufacturing_order, "new_item"
		)
	fg_item = fg_item or mwo_row.item_code
	if not fg_item:
		return None
	return frappe.db.get_value("Item", fg_item, "item_subcategory")


@frappe.whitelist()
def get_fg_bom_fields(operations):
	"""Resolve the configured FG BOM fields for an Employee IR Receive.

	`operations` is the employee_ir_operations rows (JSON). For each row we derive
	the FG item's subcategory from its MWO and pull the active FG BOM Field
	Configuration rows for that subcategory, one output row per configured field.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.fg_bom_field_configuration.fg_bom_field_configuration import (
		get_active_fields_for_subcategory,
	)

	if isinstance(operations, str):
		operations = json.loads(operations or "[]")

	subcat_cache = {}
	seen = set()
	result = []
	for op in operations or []:
		mwo = op.get("manufacturing_work_order")
		mop = op.get("manufacturing_operation")
		if not mwo:
			continue
		if mwo not in subcat_cache:
			subcat_cache[mwo] = _get_mwo_subcategory(mwo)
		subcategory = subcat_cache[mwo]
		if not subcategory:
			continue
		for cfg in get_active_fields_for_subcategory(subcategory):
			# One FG per subcategory -> show each configured field once, even when a
			# receive has several operations of the same subcategory. First operation
			# owns the row (the copy step matches on manufacturing_operation).
			key = (subcategory, cfg["field_name"])
			if key in seen:
				continue
			seen.add(key)
			result.append(
				{
					"manufacturing_operation": mop,
					"subcategory": subcategory,
					"field_label": cfg["field_label"],
					"field_name": cfg["field_name"],
					"field_type": cfg["field_type"],
					"options": cfg["options"],
					"is_mandatory": cfg["is_mandatory"],
					"fg_bom_field": cfg["fg_bom_field"],
				}
			)
	return result


def _resolve_work_orders(operations):
	"""Work order names for ``employee_ir_operations`` rows, in one round trip.

	``manufacturing_work_order`` is ``fetch_from`` + ``fetch_if_empty`` on the child
	row, so a freshly scanned or mapped row can reach us carrying only its
	Manufacturing Operation. Rows like that are resolved through their MOP rather
	than skipped -- dropping them would silently hide the field on exactly the
	entries a shop-floor user creates by scanning.
	"""
	mwos = set()
	missing_mops = set()
	for row in operations or []:
		mwo = row.get("manufacturing_work_order")
		if mwo:
			mwos.add(mwo)
		elif row.get("manufacturing_operation"):
			missing_mops.add(row["manufacturing_operation"])

	if missing_mops:
		for mop in frappe.get_all(
			"Manufacturing Operation",
			filters={"name": ["in", list(missing_mops)]},
			fields=["name", "manufacturing_work_order"],
		):
			if mop.manufacturing_work_order:
				mwos.add(mop.manufacturing_work_order)

	return mwos


@frappe.whitelist()
def get_repeat_work_orders(operations, operation, employee_ir=None):
	"""Work orders on this Receive that already completed a cycle at ``operation``.

	A cycle is counted as a *submitted* Employee IR Receive for the same work order
	at the same Department Operation. Deliberately keyed on
	(manufacturing_work_order, operation) and never on the Manufacturing Operation:
	``create_operation_for_next_op`` copies the MOP and saves a new one per cycle,
	so a MOP name carries no history.

	Returns the list rather than a bare flag so the form can name the offending
	work orders -- with one answer per document, knowing *which* one is rework is
	what makes the question answerable.
	"""
	if isinstance(operations, str):
		operations = json.loads(operations or "[]")

	if not operation:
		return []

	mwos = _resolve_work_orders(operations)
	if not mwos:
		return []

	return _repeat_query(mwos, operation, employee_ir).run(
		pluck="manufacturing_work_order"
	)


def _repeat_query(mwos, operation, employee_ir=None):
	"""The prior-cycle lookup, split out so its predicates are testable without a DB."""
	EIR = DocType("Employee IR")
	EOP = DocType("Employee IR Operation")
	query = (
		frappe.qb.from_(EIR)
		.inner_join(EOP)
		.on(EOP.parent == EIR.name)
		.select(EOP.manufacturing_work_order)
		.distinct()
		.where(
			# docstatus == 1, not != 2: we are counting COMPLETED history, so a
			# draft or cancelled Receive must not make the next one look like rework.
			(EIR.docstatus == 1)
			& (EIR.type == "Receive")
			& (EIR.operation == operation)
			& (EOP.manufacturing_work_order.isin(sorted(mwos)))
		)
	)
	if employee_ir:
		# Save-after-submit would otherwise match the document against itself.
		query = query.where(EIR.name != employee_ir)
	return query
