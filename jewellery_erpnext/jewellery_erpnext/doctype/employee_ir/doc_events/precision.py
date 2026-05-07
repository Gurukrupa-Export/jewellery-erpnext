from frappe.utils import flt

# Weight fields on Employee IR Operation that must persist at precision 3.
# PCS / Int / UOM / text fields are intentionally excluded.
EIR_OPERATION_WEIGHT_FIELDS = (
	"gross_wt",
	"received_gross_wt",
	"gold_loss",
	"net_wt",
	"finding_wt",
	"other_wt",
	"diamond_wt",
	"gemstone_wt",
	"rpt_wt_issue",
	"rpt_wt_receive",
	"rpt_wt_loss",
	"mould_wtin_gram",
)

LOSS_DETAIL_WEIGHT_FIELDS = (
	"net_weight",
	"proportionally_loss",
	"received_gross_weight",
	"main_slip_consumption",
)


def round_employee_ir_weights_to_precision(doc, precision=3):
	for child in getattr(doc, "employee_ir_operations", []) or []:
		for f in EIR_OPERATION_WEIGHT_FIELDS:
			val = getattr(child, f, None)
			if val is not None:
				child.set(f, flt(val, precision))

	if getattr(doc, "mop_loss_details_total", None) is not None:
		doc.mop_loss_details_total = flt(doc.mop_loss_details_total, precision)

	for tbl_name in ("employee_loss_details", "manually_book_loss_details"):
		for row in getattr(doc, tbl_name, []) or []:
			for f in LOSS_DETAIL_WEIGHT_FIELDS:
				val = getattr(row, f, None)
				if val is not None:
					row.set(f, flt(val, precision))
