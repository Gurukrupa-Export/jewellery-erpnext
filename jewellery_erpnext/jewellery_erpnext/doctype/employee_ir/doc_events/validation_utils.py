import frappe
from frappe import _
from frappe.utils import cint, flt


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
				is_main_slip_required=self.is_main_slip_required,
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


def validate_gross_wt(row, precision, main_slip=None, is_main_slip_required=False):
	if main_slip or is_main_slip_required:
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
	# Stricter floor: per-row balance must cover the manual loss qty. Pre-submit
	# the latest balance already reflects any prior submitted losses (loss MOP
	# Log rows post a real negative qty_change), so no log_category filter is
	# needed here.
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_available_qty_pcs_for_mop_item,
	)

	for row in self.manually_book_loss_details:
		if not row.manufacturing_operation:
			continue
		balance_qty = (
			frappe.db.get_value(
				"MOP Log",
				{
					"manufacturing_operation": row.manufacturing_operation,
					"item_code": row.item_code,
					"batch_no": row.batch_no,
					"is_cancelled": 0,
				},
				"qty_after_transaction_batch_based",
				order_by="creation desc",
			)
			or 0
		)
		if row.proportionally_loss > balance_qty:
			frappe.throw(
				_(
					"Row #{0}: <b>{1}</b> Proportionally Loss {2} cannot be greater than Balance Qty {3}"
				).format(row.idx, row.item_code, row.proportionally_loss, balance_qty)
			)

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

	# Additional cap: total manual loss for a MWO cannot exceed the true
	# available baseline (gross_wt - received_gross_wt) for that MWO.
	# Carat manual loss converted to grams for comparison.
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

	manual_by_mwo = {}
	for row in self.manually_book_loss_details:
		qty = flt(row.proportionally_loss)
		stock_uom = frappe.get_cached_value("Item", row.item_code, "stock_uom")
		if stock_uom == "Carat":
			qty = qty * 0.2
		manual_by_mwo.setdefault(row.manufacturing_work_order, 0.0)
		manual_by_mwo[row.manufacturing_work_order] += qty

	for mwo, manual_total in manual_by_mwo.items():
		available = baseline_by_mwo.get(mwo, 0.0)
		if manual_total > available:
			frappe.throw(
				_(
					"Total Manually Booked Loss for Manufacturing Work Order {0} "
					"({1} g) cannot exceed available loss baseline ({2} g = "
					"Gross Wt - Received Gross Wt)."
				).format(mwo, flt(manual_total, 3), flt(available, 3))
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
