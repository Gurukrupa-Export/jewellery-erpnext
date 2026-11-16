"""Flag the CAD / CAM operations and backfill the flag on existing Manufacturing Operations.

Employee IR Receive asks for CAD / CAM details on the Department Operations flagged
``receive_details_type`` = CAD / CAM. This patch flags the two operations that carried those
details before the flag existed, then copies the flag onto the Manufacturing Operations that
already exist: ``fetch_from`` only fills on save, and their CAD / CAM fields now show off the
flag. One UPDATE ... JOIN, not a save per record, because Manufacturing Operation is large.

Operations are matched by name: the base name and the per-company copies ("<base> - <ABBR>").
A company that names them differently has to flag its own Department Operations by hand; a
base name with no match is printed so that is not silent.
"""

import frappe

OPERATION_TYPES = {
	"RPT Digital File Generation": "CAD",
	"RPT Support Generation": "CAM",
}


def execute():
	if not frappe.db.has_column("Department Operation", "receive_details_type"):
		return

	for base, details_type in OPERATION_TYPES.items():
		# "RPT Digital File Generation" and the per-company copies named "<base> - <ABBR>".
		names = frappe.get_all(
			"Department Operation",
			or_filters=[["name", "=", base], ["name", "like", f"{base} - %"]],
			pluck="name",
		)
		if not names:
			print(
				f"set_cad_cam_receive_details_type: no Department Operation '{base}' - flag it by hand"
			)
		for name in names:
			# Only fill a blank: never overwrite a choice someone already made.
			if not frappe.db.get_value(
				"Department Operation", name, "receive_details_type"
			):
				frappe.db.set_value(
					"Department Operation",
					name,
					"receive_details_type",
					details_type,
					update_modified=False,
				)
				print(f"set_cad_cam_receive_details_type: {name} = {details_type}")

	frappe.db.sql(
		"""
		UPDATE `tabManufacturing Operation` mop
		JOIN `tabDepartment Operation` dop ON dop.name = mop.operation
		SET mop.receive_details_type = dop.receive_details_type
		WHERE IFNULL(dop.receive_details_type, '') != ''
		  AND IFNULL(mop.receive_details_type, '') != dop.receive_details_type
		"""
	)
