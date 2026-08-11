import math

import frappe
from frappe import _
from frappe.utils import (
	add_to_date,
	cint,
	flt,
	get_datetime,
	now_datetime,
	time_diff_in_seconds,
)

from jewellery_erpnext.jewellery_erpnext.customization.utils.sample_goods import (
	assert_no_sample_in_operations,
)


def validate_no_sample_issue(doc, method=None):
	"""Block an Employee IR "Issue" that would push Customer Sample Goods to the floor.

	Fail-fast guard wired on Employee IR ``before_submit`` (runs before
	``on_submit -> on_submit_issue_new`` writes any MOP Log), so the operator is blocked at
	the Issue click with a clear message rather than the block surfacing later in the
	EOD-minted "Material Transfer to Department" Stock Entry. The Stock Entry backstop
	(``validate_sample_goods_not_consumed``) remains the last-resort net.
	"""
	if getattr(doc, "type", None) != "Issue":
		return
	assert_no_sample_in_operations(doc.employee_ir_operations, doc)


def get_loss_qty_in_grams(item_code: str | None, qty) -> float:
	# Normalize a loss qty to grams. D/G item codes carry carat-denominated
	# quantities; the rest of the loss pipeline (MOP Log, MOP weight buckets)
	# is gram-based, so callers summing across mixed prefixes need conversion
	# applied uniformly.
	#
	# 1 carat = 0.2 g. Item-code prefix is the load-bearing signal here
	# because variant_of, stock_uom, and prefix all agree in this codebase
	# today, and prefix is the only one available without a DB lookup.
	q = flt(qty, 3)
	if not item_code:
		return q
	if item_code[0] in ("D", "G"):
		return flt(q * 0.2, 3)
	return q


def validate_duplication_and_gr_wt(self):
	# if self.main_slip and frappe.db.get_value("Main Slip", self.main_slip, "workflow_state") != "In Use":
	# 	self.main_slip = None

	precision = cint(frappe.db.get_single_value("System Settings", "float_precision"))
	loss_details = {}
	existing_mop = set()
	is_finding = frappe.db.get_value(
		"Department Operation", self.operation, "allow_finding_mwo"
	)

	# Batch database check
	mop_list = [row.manufacturing_operation for row in self.employee_ir_operations]
	EIR = frappe.qb.DocType("Employee IR")
	EOP = frappe.qb.DocType("Employee IR Operation")
	duplicates = (
		frappe.qb.from_(EIR)
		.left_join(EOP)
		.on(EOP.parent == EIR.name)
		.select(EOP.manufacturing_operation)
		.where(
			(EIR.name != self.name)
			& (EIR.type == self.type)
			& (EOP.manufacturing_operation.isin(mop_list))
			& (EIR.docstatus != 2)
		)
	).run(pluck="manufacturing_operation")

	if duplicates:
		frappe.throw(
			title=_("Employee IR exists for MOP"),
			msg="{0}".format(", ".join(duplicates)),
		)

	# Process child table
	for row in self.employee_ir_operations:
		validate_mwo(self, row, is_finding)
		loss_details = get_loss_details(row)

		if row.manufacturing_operation in existing_mop:
			frappe.throw(
				_("{0} appeared multiple times in Employee IR").format(
					row.manufacturing_operation
				)
			)

		existing_mop.add(row.manufacturing_operation)
		mop_doc = frappe.db.get_value(
			"Manufacturing Operation",
			row.manufacturing_operation,
			[
				"gross_wt",
				"net_wt",
				"finding_wt",
				"diamond_wt",
				"gemstone_wt",
				"diamond_pcs",
				"gemstone_pcs",
			],
			as_dict=True,
		)
		row.update(
			{
				"gross_wt": mop_doc["gross_wt"],
				"net_wt": mop_doc["net_wt"],
				"finding_wt": mop_doc["finding_wt"],
				"diamond_wt": mop_doc["diamond_wt"],
				"gemstone_wt": mop_doc["gemstone_wt"],
				"diamond_pcs": mop_doc["diamond_pcs"],
				"gemstone_pcs": mop_doc["gemstone_pcs"],
			}
		)
		if self.type == "Receive":
			validate_gross_wt(
				row,
				precision,
				main_slip=getattr(self, "main_slip", None),
				is_raw_material=self.is_raw_material,
			)

	if loss_details:
		return loss_details


def validate_mwo(self, row, is_finding):
	if self.type != "Issue":
		return

	is_finding_mwo = row.is_finding_mwo
	if is_finding_mwo:
		if not is_finding:
			frappe.throw(
				_(
					"Finding MWO <b>{0}</b> not allowed to transfer in <b>{1}</b> Department Operation."
				).format(row.manufacturing_work_order, self.operation)
			)


def validate_gross_wt(row, precision, main_slip=None, is_raw_material=False):
	if main_slip or is_raw_material:
		return
	if flt(row.gross_wt, precision) < flt(row.received_gross_wt, precision):
		frappe.throw(
			_(
				"Row #{0}: Received gross wt {1} cannot be greater than gross wt {2}"
			).format(row.idx, row.received_gross_wt, row.gross_wt)
		)


def update_mop_balance(mop_name):
	doc = frappe.get_doc("Manufacturing Operation", mop_name)
	return doc


def validate_manually_book_loss_details(self):
	if self.docstatus != 0:
		return
	# Per-key (mop, mwo, item, batch) combined-loss cap: the sum across both
	# loss tables for a given key must not exceed the latest MOP Log balance
	# for that key. Pre-submit, the latest balance already reflects any prior
	# submitted losses (loss MOP Log rows post a real negative qty_change),
	# so no log_category filter is needed here. Comparison is in the item's
	# stock UOM (carats for D/G, grams for M/F/O) — both sides match.
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_available_qty_pcs_for_mop_item,
	)

	combined_loss_by_key = {}
	for src_label, table in (
		("employee", self.employee_loss_details or []),
		("manual", self.manually_book_loss_details or []),
	):
		for row in table:
			if not row.manufacturing_operation:
				continue
			key = (
				row.manufacturing_operation,
				row.manufacturing_work_order,
				row.item_code,
				row.batch_no,
			)
			bucket = combined_loss_by_key.setdefault(
				key, {"employee": 0.0, "manual": 0.0}
			)
			bucket[src_label] += flt(row.proportionally_loss, 3)

	for row in self.manually_book_loss_details:
		if not row.manufacturing_operation:
			continue

		# D/G PCS reconciliation: manual loss PCS must not drive the MOP
		# diamond_pcs/gemstone_pcs negative. Only D/G items carry meaningful
		# PCS in MOP Log; for M/F/O the loss MOP Log emits pcs_change=0 so we
		# skip this branch entirely. A pcs=0 (or missing/non-numeric) row
		# never reduces balance, so we also skip the helper query.
		if row.item_code and row.item_code[0] in ("D", "G"):
			raw_pcs = getattr(row, "pcs", 0)
			try:
				pcs_to_book = int(float(raw_pcs)) if raw_pcs not in (None, "") else 0
			except (TypeError, ValueError):
				pcs_to_book = 0
			if pcs_to_book < 0:
				frappe.throw(_("Row #{0}: PCS must be >= 0").format(row.idx))
			if pcs_to_book > 0:
				ctx = get_available_qty_pcs_for_mop_item(
					manufacturing_operation=row.manufacturing_operation,
					item_code=row.item_code,
					batch_no=row.batch_no,
				)
				if ctx["available_pcs"] and pcs_to_book > ctx["available_pcs"]:
					frappe.throw(
						_(
							"Row #{0} <b>{1}</b> batch {2}: PCS to book ({3}) "
							"exceeds available PCS in MOP balance ({4})"
						).format(
							row.idx,
							row.item_code,
							row.batch_no,
							pcs_to_book,
							ctx["available_pcs"],
						)
					)

	# Additional cap: total combined loss (employee + manual, normalised to
	# grams via get_loss_qty_in_grams) for a MWO cannot exceed the true
	# available baseline (gross_wt - received_gross_wt) for that MWO.
	precision = cint(frappe.db.get_single_value("System Settings", "float_precision"))

	baseline_by_mwo = {}
	for op in self.employee_ir_operations:
		if not op.received_gross_wt:
			continue
		baseline = max(
			0.0, flt(op.gross_wt, precision) - flt(op.received_gross_wt, precision)
		)
		baseline_by_mwo.setdefault(op.manufacturing_work_order, 0.0)
		baseline_by_mwo[op.manufacturing_work_order] += baseline

	loss_by_mwo = {}
	for row in (self.manually_book_loss_details or []) + (
		self.employee_loss_details or []
	):
		qty = get_loss_qty_in_grams(row.item_code, row.proportionally_loss)
		loss_by_mwo.setdefault(row.manufacturing_work_order, 0.0)
		loss_by_mwo[row.manufacturing_work_order] += qty

	for mwo, total in loss_by_mwo.items():
		available = baseline_by_mwo.get(mwo, 0.0)
		if flt(total, 3) > flt(available, 3):
			frappe.throw(
				_(
					"Total Loss for Manufacturing Work Order {0} "
					"({1} g, employee + manual normalised to grams) cannot "
					"exceed available loss baseline ({2} g = Gross Wt - "
					"Received Gross Wt)."
				).format(mwo, flt(total, 3), flt(available, 3))
			)


def get_loss_details(row):
	loss_details = {}
	if row.received_gross_wt > row.gross_wt:
		return
	key = row.manufacturing_work_order
	if not loss_details.get(key):
		loss_details[key] = flt((row.received_gross_wt - row.gross_wt), 3)
	else:
		loss_details[key] = loss_details.get(key) + flt(
			(row.received_gross_wt - row.gross_wt), 3
		)

	return loss_details


def validate_loss_tables_required(self):
	"""On-submit gate for Employee IR Receive loss tables.

	Two contracts, both gated on baseline > 0 (rounded to 3):

	  1. At least one of ``employee_loss_details`` /
	     ``manually_book_loss_details`` must be populated.
	  2. The sum across both tables, normalised to grams via
	     ``get_loss_qty_in_grams`` (D/G items × 0.2), must equal the
	     baseline within precision-3 tolerance.

	Baseline = ``sum(gross_wt − received_gross_wt)`` over rows where the
	receive shrunk the weight, rounded to 3. Caller is ``EmployeeIR.on_submit``;
	at that point ``docstatus`` is still 0, so we don't gate on it — running
	this validator post-submit would be a logic bug, not a no-op.
	"""
	if getattr(self, "type", None) != "Receive":
		return

	baseline = 0.0
	for op in self.employee_ir_operations:
		if op.received_gross_wt and flt(op.gross_wt, 3) > flt(op.received_gross_wt, 3):
			baseline += flt(op.gross_wt, 3) - flt(op.received_gross_wt, 3)
	baseline = flt(baseline, 3)

	if baseline <= 0:
		return

	has_employee = bool(getattr(self, "employee_loss_details", None))
	has_manual = bool(getattr(self, "manually_book_loss_details", None))
	if not (has_employee or has_manual):
		frappe.throw(
			_(
				"Loss exists ({0} g) but no Employee Loss Details or Manually "
				"Book Loss Details found. Please book the manufacturing loss "
				"before submit."
			).format(baseline)
		)

	# Sum-match: every gram of loss must be allocated across the two
	# tables. ``get_loss_qty_in_grams`` handles carat→gram for D/G items.
	total = 0.0
	for row in (self.employee_loss_details or []) + (
		self.manually_book_loss_details or []
	):
		total += get_loss_qty_in_grams(row.item_code, row.proportionally_loss)
	total = flt(total, 3)

	# Tolerance = 0.0005 — half the precision-3 floor. After ``flt(_, 3)``
	# on each side, two values that round to the same display equal each
	# other within this margin; values that differ by ≥ 0.001 don't.
	if abs(total - baseline) > 0.0005:
		diff = flt(total - baseline, 3)
		frappe.throw(
			_(
				"Total Loss Details ({0} g, employee + manual normalised to "
				"grams) must match available loss baseline ({1} g = sum of "
				"Gross Wt − Received Gross Wt). Difference: {2} g."
			).format(total, baseline, diff)
		)


def validate_loss_qty(self):
	if self.docstatus != 0:
		return
	loss_details = {}
	er_loss_details = validate_duplication_and_gr_wt(self)

	for row in self.employee_loss_details:
		key = row.manufacturing_work_order

		if not loss_details.get(key):
			loss_details[key] = flt(row.proportionally_loss, 3)
		else:
			loss_details[key] = loss_details.get(key) + flt(row.proportionally_loss, 3)

	for row in self.manually_book_loss_details:
		key = row.manufacturing_work_order
		loss_multiplier = 0.2 if row.variant_of in ["D", "G"] else 1.0
		if not loss_details.get(key):
			loss_details[key] = flt((row.proportionally_loss * loss_multiplier), 3)
		else:
			loss_details[key] = loss_details.get(key) + flt(
				(row.proportionally_loss * loss_multiplier), 3
			)

	if not er_loss_details:
		er_loss_details = []

	for i in er_loss_details:
		if (
			er_loss_details.get(i)
			and loss_details.get(i)
			and er_loss_details.get(i) > 0
			and er_loss_details.get(i) != loss_details.get(i)
		):
			frappe.throw(
				_(
					"<b>{0}</b> Proportionally Loss {1} not match with recive weight {2}"
				).format(i, loss_details.get(i), er_loss_details.get(i))
			)


def validate_employee_ir_receive_delay(doc):
	"""Block Employee IR Receive submission until each row's Issue-configured
	delay (Department Operation.employee_ir_receive_delay) has elapsed.

	Correspondence between a Receive row and its Issue reuses the same
	resolver ``on_submit_receive`` already relies on for MOP Log lineage
	(``resolve_employee_ir_issue_voucher_for_receive``), so "no Issue found"
	means the same thing here as it does everywhere else in the app. Rows
	whose corresponding Issue cannot be resolved, or whose Issue has no
	usable submission timestamp, are skipped rather than blocked.
	"""
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		resolve_employee_ir_issue_voucher_for_receive,
	)

	now = now_datetime()
	issue_cache = {}
	delay_cache = {}
	worst_wait = 0
	worst_row = None
	worst_issue = None

	for row in doc.employee_ir_operations or []:
		issue_name = resolve_employee_ir_issue_voucher_for_receive(doc, row)
		if not issue_name:
			continue

		if issue_name not in issue_cache:
			issue_cache[issue_name] = frappe.db.get_value(
				"Employee IR",
				issue_name,
				["operation", "issue_submitted_on", "date_time"],
				as_dict=True,
			)
		issue = issue_cache[issue_name]
		if not issue:
			continue

		submitted_on = issue.issue_submitted_on or issue.date_time
		if not submitted_on:
			continue

		if issue.operation not in delay_cache:
			delay_cache[issue.operation] = cint(
				frappe.db.get_value(
					"Department Operation", issue.operation, "employee_ir_receive_delay"
				)
			)
		delay_minutes = delay_cache[issue.operation]
		if delay_minutes <= 0:
			continue

		allowed_from = add_to_date(get_datetime(submitted_on), minutes=delay_minutes)
		if now < allowed_from:
			remaining_minutes = math.ceil(time_diff_in_seconds(allowed_from, now) / 60)
			if remaining_minutes > worst_wait:
				worst_wait, worst_row, worst_issue = remaining_minutes, row, issue_name

	if worst_wait > 0:
		frappe.throw(
			_(
				"This Employee IR Receive entry cannot be submitted at this stage. Please try again later."
			)
		)
