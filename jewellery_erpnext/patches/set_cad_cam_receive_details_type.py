"""Flag the CAD / CAM operations and backfill the flag on existing Manufacturing Operations.

Employee IR Receive asks for CAD / CAM details on the Department Operations flagged
``receive_details_type`` = CAD / CAM. This patch flags the two operations that carried those
details before the flag existed, then copies the flag onto the Manufacturing Operations that
already exist: ``fetch_from`` only fills on save, and their CAD / CAM fields now show off the
flag. One UPDATE ... JOIN, not a save per record, because Manufacturing Operation is large.

Operations are matched by name. A company that names them differently has to flag its own
Department Operations by hand; the unmatched names are printed so that is not silent.
"""

import frappe

OPERATION_TYPES = {
	"RPT Digital File Generation": "CAD",
	"RPT Support Generation": "CAM",
}


def execute():
	if not frappe.db.has_column("Department Operation", "receive_details_type"):
		return

	for operation, details_type in OPERATION_TYPES.items():
		if frappe.db.exists("Department Operation", operation):
			# Only fill a blank: never overwrite a choice someone already made.
			if not frappe.db.get_value(
				"Department Operation", operation, "receive_details_type"
			):
				frappe.db.set_value(
					"Department Operation",
					operation,
					"receive_details_type",
					details_type,
					update_modified=False,
				)
		else:
			print(
				f"set_cad_cam_receive_details_type: Department Operation '{operation}' not found - flag it by hand"
			)

	frappe.db.sql(
		"""
		UPDATE `tabManufacturing Operation` mop
		JOIN `tabDepartment Operation` dop ON dop.name = mop.operation
		SET mop.receive_details_type = dop.receive_details_type
		WHERE IFNULL(dop.receive_details_type, '') != ''
		  AND IFNULL(mop.receive_details_type, '') != dop.receive_details_type
		"""
	)
