"""CAD / CAM details captured on an Employee IR Receive.

Which operations ask for these details is a flag on the Department Operation
(``receive_details_type``: CAD / CAM / blank), not a hardcoded name. The flag is copied onto
the Employee IR header and the Manufacturing Operation for the form's ``depends_on``, but
every server decision here re-reads it from the Department Operation, so a posted value
cannot switch the rules on or off.

The details are entered per Work Order row on the Employee IR Operation table and written to
the linked Manufacturing Operation by ``on_submit_receive`` (cleared again on cancel).
"""

import frappe
from frappe import _
from frappe.utils import flt

# fieldname -> label, per type. The same fieldnames exist on Employee IR Operation and
# Manufacturing Operation, so a row value maps straight onto the operation.
CAD_FIELDS = {
	"cad_file": "CAD File",
	"cad_numbering_file": "CAD Numbering File",
	"estimated_rpt_wt": "Estimated RPT WT",
	"ref_name": "Ref Name",
	"estimated_casting_wt": "Estimated Casting WT",
}
CAM_FIELDS = {
	"support_cam_file": "Support CAM File",
	"platform_wt": "Platform WT",
}
FIELDS_BY_TYPE = {"CAD": CAD_FIELDS, "CAM": CAM_FIELDS}
ALL_FIELDS = {**CAD_FIELDS, **CAM_FIELDS}
FLOAT_FIELDS = ("estimated_rpt_wt", "estimated_casting_wt", "platform_wt")


def get_receive_details_type(operation):
	"""Return "CAD", "CAM" or None for a Department Operation."""
	if not operation:
		return None
	value = frappe.db.get_value(
		"Department Operation", operation, "receive_details_type"
	)
	return value if value in FIELDS_BY_TYPE else None


def _is_empty(fieldname, value):
	# A weight of 0 is as good as no entry, so floats must be positive.
	if fieldname in FLOAT_FIELDS:
		return flt(value) <= 0
	return not value


def validate_cad_cam_receive_details(doc):
	"""Server-side rules behind the form's depends_on / mandatory_depends_on.

	* Receive on a CAD / CAM operation: every field of that type is required on every row, and
	  no field of the other type may be filled.
	* Anything else (Issue, or an operation with no flag): no CAD / CAM value may be present.
	"""
	details_type = (
		get_receive_details_type(doc.operation) if doc.type == "Receive" else None
	)
	# The header copy drives the form only; keep it in step with the operation.
	doc.receive_details_type = details_type

	required = FIELDS_BY_TYPE.get(details_type, {})
	for row in doc.employee_ir_operations:
		missing = [
			label for fn, label in required.items() if _is_empty(fn, row.get(fn))
		]
		if missing:
			frappe.throw(
				_("Row {0}: {1} required for {2} receive of {3}").format(
					row.idx, ", ".join(missing), details_type, doc.operation
				)
			)
		# Whatever is not this operation's own type must stay empty: every CAD / CAM field on
		# a row with no flag, or the CAM fields on a CAD receive (and the reverse).
		extra = [
			label
			for fn, label in ALL_FIELDS.items()
			if fn not in required and row.get(fn)
		]
		if extra:
			frappe.throw(
				_("Row {0}: {1} not applicable for {2} {3}").format(
					row.idx, ", ".join(extra), doc.type, doc.operation
				)
			)


def cad_cam_values_for_operation(doc, row, cancel=False):
	"""Values to merge into the Manufacturing Operation update for one row.

	Submit writes the row's details (overwriting any earlier receive); cancel blanks them
	(0 for a weight, None for a file or text).
	``receive_details_type`` is set too: frappe.db.set_value skips the fetch_from that
	would otherwise fill it on a save.
	"""
	details_type = (
		get_receive_details_type(doc.operation) if doc.type == "Receive" else None
	)
	if not details_type:
		return {}
	values = {"receive_details_type": details_type}
	for fn in FIELDS_BY_TYPE[details_type]:
		if cancel:
			# Float columns are NOT NULL; an Attach / Data column is blank as None.
			values[fn] = 0 if fn in FLOAT_FIELDS else None
		else:
			values[fn] = row.get(fn)
	return values


def sync_item_master_for_receive(manufacturing_operation):
	"""Copy a received operation's CAD / CAM details into the Item's Cam Weight Detail.

	``frappe.db.set_value`` in on_submit_receive skips the Manufacturing Operation's
	on_update, which is where this sync normally runs, so it is called explicitly. The row is
	read fresh, after the update. Not called on cancel: the Item keeps what was received.
	"""
	mop = frappe.get_doc("Manufacturing Operation", manufacturing_operation)
	mop.attach_cad_cam_file_into_item_master()


@frappe.whitelist()
def get_estimated_rpt_wt(manufacturing_operation):
	"""Estimated RPT WT of the operation's Item, to pre-fill the CAD receive row.

	The Item keeps it per row of "Cad To Finish Weight Estimated Details", worked out as CAD
	Weight / the CAD-to-RPT ratio in Jewellery Settings. Use the first stored value; failing
	that, work it out from the first CAD Weight. 0 when the Item has neither.
	"""
	item_code = frappe.db.get_value(
		"Manufacturing Operation", manufacturing_operation, "item_code"
	)
	if not item_code:
		return 0
	frappe.has_permission("Item", "read", item_code, throw=True)

	rows = frappe.get_all(
		"Cad To Finish Weight Estimated Details",
		filters={"parent": item_code, "parenttype": "Item"},
		fields=["cad_weight", "estimated_rpt_wt"],
		order_by="idx",
	)
	for r in rows:
		if flt(r.estimated_rpt_wt) > 0:
			return flt(r.estimated_rpt_wt, 3)

	ratio = flt(frappe.db.get_single_value("Jewellery Settings", "cad_to_rpt"))
	if ratio:
		for r in rows:
			if flt(r.cad_weight) > 0:
				return flt(flt(r.cad_weight) / ratio, 3)
	return 0
