import frappe
from frappe import _
from frappe.utils import cint, flt


def validate_duplication_and_gr_wt(self):
	if (
		self.main_slip and frappe.db.get_value("Main Slip", self.main_slip, "workflow_state") != "In Use"
	):
		self.main_slip = None
	precision = cint(frappe.db.get_single_value("System Settings", "float_precision"))
	existing_mop = []
	for row in self.employee_ir_operations:
		if row.manufacturing_operation in existing_mop:
			frappe.throw(
				_("{0} appeared multiple times in Employee IR").format(row.manufacturing_operation)
			)
		existing_mop.append(row.manufacturing_operation)
		EIR = frappe.qb.DocType("Employee IR")
		EOP = frappe.qb.DocType("Employee IR Operation")
		exists = (
			frappe.qb.from_(EIR)
			.left_join(EOP)
			.on(EOP.parent == EIR.name)
			.select(EIR.name)
			.where(
				(EIR.name != self.name)
				& (EIR.type == self.type)
				& (EOP.manufacturing_operation == row.manufacturing_operation)
				& (EIR.docstatus != 2)
			)
		).run(as_dict=1)
		if exists:
			frappe.throw(_("Employee IR exists for MOP {0}").format(row.manufacturing_operation))

		save_mop(row.manufacturing_operation)
		validate_gross_wt(row, precision, self.main_slip)


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


def save_mop(mop_name):
	doc = frappe.get_doc("Manufacturing Operation", mop_name)
	doc.save()
