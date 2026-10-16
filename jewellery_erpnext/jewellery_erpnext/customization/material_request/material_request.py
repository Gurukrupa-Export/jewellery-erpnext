import json

import frappe
from frappe import _
from frappe.model.mapper import get_mapped_doc
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils.prefetch import (
	mri_warehouse_map,
)
from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.transit import (
	has_non_transit_target,
)


@frappe.whitelist()
def make_mop_stock_entry(self, **kwargs):
	try:
		if isinstance(self, str):
			self = json.loads(self)
		# before_update_after_submit fires on every update-after-submit save while the
		# document sits in "Material Transferred to MOP", so without this a plain Update
		# mints a second Work Order Stock Entry and a second set of MOP Log rows.
		if self.get("custom_mop_se"):
			return None
		if not self.get("custom_reserve_se"):
			return

		se_doc = frappe.get_doc("Stock Entry", self.get("custom_reserve_se"))
		mop_data = frappe.db.get_value(
			"Manufacturing Operation",
			kwargs.get("mop"),
			["department", "status", "employee", "department_ir_status"],
			as_dict=1,
		)
		if mop_data.get("department_ir_status") == "In-Transit":
			frappe.throw(
				_(
					"{0} Manufacturing Operation not allowd becuase it is in-transit status."
				).format(kwargs.get("mop"))
			)
		new_se_doc = frappe.copy_doc(se_doc)
		manufacturing_work_order, manufacturing_order = frappe.get_cached_value(
			"Manufacturing Operation",
			kwargs.get("mop"),
			["manufacturing_work_order", "manufacturing_order"],
		)
		new_se_doc.stock_entry_type = "Material Transfer (WORK ORDER)"
		new_se_doc.manufacturing_operation = kwargs.get("mop")
		new_se_doc.manufacturing_order = manufacturing_order
		new_se_doc.manufacturing_work_order = manufacturing_work_order
		new_se_doc.auto_created = 1
		new_se_doc.to_department = mop_data.get("department")
		new_se_doc.add_to_transit = 0
		t_warehouse = frappe.db.get_value(
			"Warehouse",
			{
				"department": mop_data.get("department"),
				"warehouse_type": "Manufacturing",
			},
			"name",
		)
		if mop_data.get("status") == "WIP" and mop_data.get("employee"):
			t_warehouse = frappe.db.get_value(
				"Warehouse",
				{
					"employee": mop_data.get("employee"),
					"warehouse_type": "Manufacturing",
				},
				"name",
			)
		# One query for every referenced Material Request Item instead of one per distinct
		# item on the copied Stock Entry. ``self`` here is the client-supplied document as a
		# plain dict, so its rows are not passed to the helper -- the warehouses are read
		# from the database exactly as they always were.
		warehouse_data = mri_warehouse_map(new_se_doc.items)

		for row in new_se_doc.items:
			row.s_warehouse = warehouse_data.get(row.material_request_item)
			row.t_warehouse = t_warehouse
			row.to_department = mop_data.get("department")
			row.manufacturing_operation = kwargs.get("mop")
			row.serial_and_batch_bundle = None

		new_se_doc.save()
		new_se_doc.submit()
		frappe.msgprint(_("Stock Entry Created"))
		self.db_set("custom_mop_se", new_se_doc.name)
		# frappe.db.set_value("Material Request", self.get("name"), "custom_mop_se", new_se_doc.name)

		return new_se_doc.name

	except Exception as e:
		frappe.log_error("data Error", e)
		frappe.throw(str(e))
		return e


def get_submitted_from_reserve_se(mr_name, stock_entry=None):
	"""Name of a submitted "Material Transfer From Reserve" entry that moved ``mr_name``.

	Keyed on the entry's rows, not on ``Material Request.custom_transfer_se``: a request
	split off another one by ``frappe.copy_doc`` carries the parent's stamp, which names the
	entry that moved the parent's material rather than its own. Pass ``stock_entry`` to ask
	about that one entry.
	"""
	existing = frappe.db.sql(
		"""
		SELECT se.name FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.stock_entry_type = 'Material Transfer From Reserve'
		  AND se.docstatus = 1 AND sed.material_request = %(mr_name)s
		  AND (%(stock_entry)s IS NULL OR se.name = %(stock_entry)s)
		LIMIT 1
		""",
		{"mr_name": mr_name, "stock_entry": stock_entry},
	)
	return existing[0][0] if existing else None


def validate_from_reserve_done(self):
	"""Refuse to move a request's material before it has reached ``set_warehouse``.

	The reserve Stock Entry parks the material in the department's Reserve warehouse; the
	"Material Transfer From Reserve" entry that ``MR.on_submit`` defers to a background job
	(``doc_events.material_request.materialize_transfer_se``) is what brings it into
	``set_warehouse``. Nothing in the workflow waits for that job, so when it is still
	running or has failed, ``set_warehouse`` does not hold the material and the transfer
	would only fail later on stock -- with a message that points nowhere near the cause.
	"""
	state = self.get("custom_transfer_se_state")
	if state == "Pending":
		frappe.throw(
			_(
				"The Material Transfer From Reserve entry for this request is still being "
				"created. Please try again in a few seconds; if this persists, ask your "
				"system manager to check the background job."
			)
		)

	if state == "Failed":
		frappe.throw(
			_(
				"The Material Transfer From Reserve entry for this request failed, so the "
				"material has not reached {0}. Ask your system manager to re-create it. "
				"Error: {1}"
			).format(
				self.get("set_warehouse"),
				self.get("custom_transfer_se_error") or _("(not recorded)"),
			)
		)

	transfer_se = self.get("custom_transfer_se")
	if not transfer_se or not get_submitted_from_reserve_se(self.name, transfer_se):
		frappe.throw(
			_(
				"No submitted Material Transfer From Reserve entry moved this request's "
				"material into {0}, so there is nothing there to transfer."
			).format(self.get("set_warehouse"))
		)


def validate_department_transfer_received(self):
	"""Refuse to hand the material on while its Transfer to Department is still in transit.

	``make_department_transfer_stock_entry`` sends the material to the destination's
	transit warehouse; it reaches ``custom_destination_warehouse`` -- the source the MOP
	transfer reads -- only once the receiving department ends the transit. Entries made
	before it became a transit leg carry add_to_transit too but moved the material straight
	into the destination, so only an entry whose rows went into Transit is waited on.
	"""
	transfer_se = self.get("custom_department_transfer_se")
	if not transfer_se:
		return

	transfer = frappe.db.get_value(
		"Stock Entry", transfer_se, ["add_to_transit", "per_transferred"], as_dict=True
	)
	if (
		transfer
		and transfer.add_to_transit
		and flt(transfer.per_transferred) < 100
		and not has_non_transit_target(transfer_se)
	):
		frappe.throw(
			_(
				"The material is still in transit to {0}: Stock Entry {1} has not been "
				"received yet. The receiving department must End Transit on {1} before it "
				"can be transferred to a Manufacturing Operation."
			).format(self.get("custom_destination_warehouse"), transfer_se)
		)


def make_department_transfer_stock_entry(self):
	"""Send a submitted request's material out of ``set_warehouse`` to another department.

	This is the "Transfer to Department" half of the workflow's final step -- the sibling
	of ``make_mop_stock_entry``, reached when ``custom_operation_type`` selects the
	department route instead of the MOP one. The deferred "Material Transfer From Reserve"
	Stock Entry must already have put the material in ``set_warehouse`` -- see
	``validate_from_reserve_done`` -- so that is the source.

	The entry is a transit leg, like the MR's "Material Transfer (In Transit)": it moves the
	material into the operator's ``custom_destination_warehouse``'s Default In-Transit
	Warehouse with Add to Transit on, and the receiving department ends the transit (End
	Transit, which ``doc_events.stock_entry.make_stock_in_entry`` routes into
	``custom_destination_warehouse``). ERPNext v16.36.0 only accepts Add to Transit into a
	Transit warehouse, so a destination that is itself a Transit warehouse is refused, and
	Transfer to MOP waits for the receipt (``validate_department_transfer_received``). The
	request's ``transfer_status`` goes In Transit on this entry and Completed on its receipt.

	Not whitelisted, unlike the older makers beside it: the only caller is
	``before_update_after_submit``, and exposing it would let an API client mint a
	submitted Stock Entry while skipping the workflow transition that gates it.

	The reserve Stock Entry is the template, exactly as it is for the MOP makers. It
	already carries the rows' ``material_request`` / ``material_request_item`` links --
	the Material Request reference this entry keeps -- along with ``batch_no``, ``pcs``,
	``inventory_type``, ``customer`` and the alternative item already substituted into
	``item_code`` (which ``CustomStockEntry.validate_with_material_request`` accepts,
	matching against both ``item_code`` and ``custom_alternative_item``).

	Deliberately does NOT stamp ``custom_material_request_reference`` on the header. That
	field is the gate for ``doc_events.stock_entry.validate_material_request_warehouses``,
	which asserts every row's ``t_warehouse`` equals ``Material Request Item.warehouse`` --
	the precise routing this transfer departs from. Leaving it blank matches the three
	Stock Entries this chain already creates.

	``Material Transfer (DEPARTMENT)`` is absent from
	``MOP Settings.stock_entry_type_to_reservation``, so submitting it writes no Stock
	Reservation Entries and no MOP Log rows -- correct for a move that hands the material
	to a department rather than to an operation.
	"""
	# before_update_after_submit fires on every update-after-submit save while the
	# document sits in this state, so the stamp is the guard against a second entry.
	if self.get("custom_department_transfer_se"):
		return None

	department = self.get("custom_destination_department")
	warehouse = self.get("custom_destination_warehouse")

	if not department:
		frappe.throw(_("Please select a Destination Department."))

	if not warehouse:
		frappe.throw(_("Please select a Destination Warehouse."))

	if not self.get("set_warehouse"):
		frappe.throw(
			_(
				"Target Warehouse is not set on this Material Request, so there is no "
				"source to transfer the material from."
			)
		)

	if self.get("set_warehouse") == warehouse:
		frappe.throw(
			_("The source warehouse and the target warehouse cannot be the same.")
		)

	target = frappe.db.get_value(
		"Warehouse",
		warehouse,
		[
			"department",
			"company",
			"is_group",
			"warehouse_type",
			"default_in_transit_warehouse",
		],
		as_dict=True,
	)

	if not target:
		frappe.throw(_("Destination Warehouse {0} not found.").format(warehouse))

	if target.is_group:
		frappe.throw(
			_("Destination Warehouse {0} is a group warehouse.").format(warehouse)
		)

	if target.warehouse_type == "Transit":
		frappe.throw(
			_(
				"Destination Warehouse {0} is a Transit warehouse. Choose the department's "
				"own warehouse; the material goes through its transit warehouse on the way."
			).format(warehouse)
		)

	transit_warehouse = target.default_in_transit_warehouse
	if not transit_warehouse:
		frappe.throw(
			_(
				"Transit warehouse is not mentioned in Destination Warehouse {0}. Set its "
				"Default In-Transit Warehouse."
			).format(warehouse)
		)

	if target.department != department:
		frappe.throw(
			_("Destination Warehouse {0} belongs to department {1}, not {2}.").format(
				warehouse, target.department or _("(not set)"), department
			)
		)

	if target.company != self.get("company"):
		frappe.throw(
			_("Destination Warehouse {0} belongs to company {1}, not {2}.").format(
				warehouse, target.company or _("(not set)"), self.get("company")
			)
		)

	if not self.get("custom_reserve_se"):
		frappe.throw(
			_(
				"This Material Request has no Reserve Stock Entry, so there is nothing "
				"to transfer."
			)
		)

	validate_from_reserve_done(self)

	se_doc = frappe.get_doc("Stock Entry", self.get("custom_reserve_se"))
	new_se_doc = frappe.copy_doc(se_doc)

	new_se_doc.stock_entry_type = "Material Transfer (DEPARTMENT)"
	new_se_doc.purpose = "Material Transfer"
	new_se_doc.auto_created = 1
	# A transit leg. Set explicitly rather than left to the type's fetch: a 1 is never
	# fetched away, so this holds whatever the Stock Entry Type is configured with.
	new_se_doc.add_to_transit = 1
	new_se_doc.to_department = department
	new_se_doc.from_warehouse = self.get("set_warehouse")
	new_se_doc.to_warehouse = transit_warehouse
	# The reserve SE predates any operation; make sure a copied value can never leak in
	# and pull this entry into the MOP ledger.
	new_se_doc.manufacturing_operation = None

	for row in new_se_doc.items:
		row.s_warehouse = self.get("set_warehouse")
		row.t_warehouse = transit_warehouse
		row.to_department = department
		row.manufacturing_operation = None
		row.serial_and_batch_bundle = None

	new_se_doc.save()
	new_se_doc.submit()
	frappe.msgprint(_("Stock Entry {0} created").format(new_se_doc.name))
	self.db_set("custom_department_transfer_se", new_se_doc.name)

	return new_se_doc.name


@frappe.whitelist()
def make_department_mop_stock_entry(self, **kwargs):
	if isinstance(self, str):
		self = json.loads(self)
	# Same idempotency guard as make_mop_stock_entry: this state re-saves, and each save
	# would otherwise create another Work Order Stock Entry.
	if self.get("custom_mop_se"):
		return None
	if not self.get("custom_reserve_se"):
		return

	se_doc = frappe.get_doc("Stock Entry", self.get("custom_reserve_se"))
	mop_data = frappe.db.get_value(
		"Manufacturing Operation",
		kwargs.get("mop"),
		["department", "status", "employee", "department_ir_status"],
		as_dict=1,
	)
	if mop_data.get("department_ir_status") == "In-Transit":
		frappe.throw(
			_(
				"{0} Manufacturing Operation not allowd becuase it is in-transit status."
			).format(kwargs.get("mop"))
		)

	# A Transfer to Department sends the material to its destination through transit; it
	# is only there once received.
	validate_department_transfer_received(self)

	# A Transfer to Department already moved the material, and recorded where to. Read that
	# rather than inferring it from "the newest Stock Entry Detail row against this request":
	# the query below filters on no docstatus and would follow any other entry booked against
	# the request afterwards.
	s_warehouse = ""
	if self.get("custom_department_transfer_se") and self.get(
		"custom_destination_warehouse"
	):
		s_warehouse = self.get("custom_destination_warehouse")
	else:
		s_warehouse = frappe.db.sql(
			f"""WITH last_se AS (
				SELECT sei.parent AS stock_entry_name
				FROM `tabStock Entry Detail` sei
				WHERE sei.material_request = '{self.name}'
				ORDER BY sei.creation DESC
				LIMIT 1
				)
				SELECT sei.t_warehouse
				FROM `tabStock Entry Detail` sei
				JOIN last_se ON sei.parent = last_se.stock_entry_name
				GROUP BY sei.t_warehouse
				HAVING COUNT(DISTINCT sei.t_warehouse) = 1
				""",
			as_dict=1,
		)
		if s_warehouse:
			s_warehouse = s_warehouse[0]["t_warehouse"]
		else:
			s_warehouse = self.items[0].warehouse
	manufacturing_work_order, manufacturing_order = frappe.get_cached_value(
		"Manufacturing Operation",
		kwargs.get("mop"),
		["manufacturing_work_order", "manufacturing_order"],
	)
	new_se_doc = frappe.copy_doc(se_doc)

	new_se_doc.stock_entry_type = "Material Transfer (WORK ORDER)"
	new_se_doc.manufacturing_operation = kwargs.get("mop")
	new_se_doc.auto_created = 1
	new_se_doc.manufacturing_order = manufacturing_order
	new_se_doc.manufacturing_work_order = manufacturing_work_order
	new_se_doc.to_department = self.get("custom_department")
	new_se_doc.add_to_transit = 0
	t_warehouse = frappe.db.get_value(
		"Warehouse",
		{"department": mop_data.get("department"), "warehouse_type": "Manufacturing"},
		"name",
	)

	if mop_data.get("status") == "WIP" and mop_data.get("employee"):
		t_warehouse = frappe.db.get_value(
			"Warehouse",
			{"employee": mop_data.get("employee"), "warehouse_type": "Manufacturing"},
			"name",
		)
	for row in new_se_doc.items:
		row.s_warehouse = s_warehouse
		row.t_warehouse = t_warehouse
		row.manufacturing_operation = kwargs.get("mop")
		row.serial_and_batch_bundle = None

	new_se_doc.save()
	new_se_doc.submit()
	frappe.msgprint(_("Stock Entry Created"))
	self.db_set("custom_mop_se", new_se_doc.name)

	return new_se_doc.name


@frappe.whitelist()
def get_pmo_data(source_name, target_doc=None):
	def set_missing_values(source, target):
		MR = frappe.qb.DocType("Stock Entry")
		MRI = frappe.qb.DocType("Stock Entry Detail")

		materail_data = (
			frappe.qb.from_(MR)
			.join(MRI)
			.on(MR.name == MRI.parent)
			.select(
				MRI.item_code,
				MRI.qty,
				MRI.uom,
				MRI.basic_rate,
				MRI.inventory_type,
				MRI.customer,
				MRI.conversion_factor,
				MRI.t_warehouse,
				MRI.s_warehouse,
				MRI.batch_no,
			)
			.where(MRI.custom_parent_manufacturing_order == source_name)
			.where(MR.docstatus == 1)
			.where(MR.stock_entry_type == "Material Transfer From Reserve")
		)

		if target.custom_item_type:
			variant_of_dict = {"Gemstone": "G", "Diamond": "D"}
			if variant_of_dict.get(target.custom_item_type):
				materail_data = materail_data.where(
					MRI.custom_variant_of
					== variant_of_dict.get(target.custom_item_type)
				)

		materail_data = materail_data.run(as_dict=True)

		for row in materail_data:
			target.append(
				"items",
				{
					"warehouse": row.t_warehouse,
					"from_warehouse": row.s_warehouse,
					"item_code": row.item_code,
					"qty": row.qty,
					"uom": row.uom,
					"conversion_factor": row.conversion_factor,
					"rate": row.rate,
					"inventory_type": row.inventory_type,
					"customer": row.get("customer"),
					"batch_no": row.get("batch_no"),
				},
			)

		target.manufacturing_order = source_name

		target.set_missing_values()

	doclist = get_mapped_doc(
		"Parent Manufacturing Order",
		source_name,
		{
			"Parent Manufacturing Order": {
				"validation": {"docstatus": ["=", 1]},
			},
		},
		target_doc,
		set_missing_values,
	)

	return doclist
