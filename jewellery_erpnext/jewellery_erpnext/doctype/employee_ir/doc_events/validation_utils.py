import frappe
from frappe import _
from frappe.utils import cint, flt


def validate_duplication_and_gr_wt(self):
	if self.main_slip and frappe.db.get_value("Main Slip", self.main_slip, "workflow_state") != "In Use":
		self.main_slip = None

	precision = cint(frappe.db.get_single_value("System Settings", "float_precision"))
	loss_details = {}
	existing_mop = set()
	is_finding = frappe.db.get_value("Department Operation", self.operation, "allow_finding_mwo")

	# Batch database check
	mop_list = [row.manufacturing_operation for row in self.employee_ir_operations]
	EIR = frappe.qb.DocType("Employee IR")
	EOP = frappe.qb.DocType("Employee IR Operation")
	duplicates = (
		frappe.qb.from_(EIR)
		.left_join(EOP)
		.on(EOP.parent == EIR.name)
		.select(EIR.name,EOP.manufacturing_operation)
		.where(
			(EIR.name != self.name)
			& (EIR.type == self.type)
			& (EOP.manufacturing_operation.isin(mop_list))
			& (EIR.docstatus != 2)
		)
	).run(as_dict=True)

	if duplicates:
		for row in duplicates:
			msg = f"Following Manufacturing Operation already exist in Employee IR in <b>{row.name}</b>"
			frappe.throw(msg)

	# Process child table
	for row in self.employee_ir_operations:
		validate_mwo(self, row, is_finding)
		loss_details = get_loss_details(row)

		if row.manufacturing_operation in existing_mop:
			frappe.throw(_("{0} appeared multiple times in Employee IR").format(row.manufacturing_operation))

		existing_mop.add(row.manufacturing_operation)
		mop_doc = update_mop_balance(row.manufacturing_operation)
		row.update({
			"gross_wt": mop_doc.gross_wt,
			"net_wt": mop_doc.net_wt,
			"finding_wt": mop_doc.finding_wt,
			"diamond_wt": mop_doc.diamond_wt,
			"gemstone_wt": mop_doc.gemstone_wt,
			"diamond_pcs": mop_doc.diamond_pcs,
			"gemstone_pcs": mop_doc.gemstone_pcs
		})
		validate_gross_wt(row, precision, self.main_slip)

	if loss_details:
		return loss_details

def validate_mwo(self, row, is_finding):
	if self.type != "Issue":
		return

	is_finding_mwo = row.is_finding_mwo
	if is_finding_mwo:
		if not is_finding:
			frappe.throw(
				_("Finding MWO <b>{0}</b> not allowed to transfer in <b>{1}</b> Department Operation.").format(
					row.manufacturing_work_order, self.operation
				)
			)

def validate_gross_wt(row, precision, main_slip=None):
	row.gross_wt = frappe.db.get_value(
		"Manufacturing Operation", row.manufacturing_operation, "gross_wt"
	)
	if not main_slip:
		if flt(row.gross_wt, precision) < flt(row.received_gross_wt, precision):
			frappe.throw(
				_("Row #{0}: Received gross wt {1} cannot be greater than gross wt {2}").format(
					row.idx, row.received_gross_wt, row.gross_wt
				)
			)


def update_mop_balance(mop_name):
	doc = frappe.get_doc("Manufacturing Operation", mop_name)
	doc.validate()
	doc.on_update()
	doc.update_children()
	doc.db_update_all()

	return doc


def validate_manually_book_loss_details(self):
	if self.docstatus != 0:
		return
	for row in self.manually_book_loss_details:
		if not row.manufacturing_operation:
			continue
		filter = {
			"parent": row.manufacturing_operation,
			"item_code": row.item_code,
			"batch_no": row.batch_no,
		}
		balance_qty = frappe.db.get_value("MOP Balance Table", filter, "qty")
		if row.proportionally_loss > balance_qty:
			frappe.throw(
				_(
					"Row #{0}: <b>{1}</b> Proportionally Loss {2} cannot be greater than Balance Qty {3}"
				).format(row.idx, row.item_code, row.proportionally_loss, balance_qty)
			)


def get_loss_details(row):
	loss_details = {}
	if row.received_gross_wt > row.gross_wt:
		return
	key = row.manufacturing_work_order
	if not loss_details.get(key):
		loss_details[key] = flt((row.received_gross_wt - row.gross_wt), 3)
	else:
		loss_details.get[key] = loss_details.get(key) + flt((row.received_gross_wt - row.gross_wt), 3)

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
			loss_details[key] = loss_details.get(key) + flt((row.proportionally_loss * loss_multiplier), 3)

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
				_("<b>{0}</b> Proportionally Loss {1} not match with recive weight {2}").format(
					i, loss_details.get(i), er_loss_details.get(i)
				)
			)
