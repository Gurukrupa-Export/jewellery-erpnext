import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import flt
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
	update_mop_balance
)


def valid_reparing_or_next_operation(self, mwo_list):
	if self.type == "Issue":
		if not mwo_list:
			mwo_list = [row.manufacturing_work_order for row in self.department_ir_operation]

		DepartmentIR = DocType("Department IR")
		DepartmentIROperation = DocType("Department IR Operation")

		query = (
			frappe.qb.from_(DepartmentIR)
			.join(DepartmentIROperation)
			.on(DepartmentIROperation.parent == DepartmentIR.name)
			.select(DepartmentIR.name)
			.where(
				(DepartmentIR.name != self.name)
				& (DepartmentIROperation.manufacturing_work_order.isin(mwo_list))
				& (DepartmentIR.next_department == self.next_department)
			)
		)

		if query.run(as_dict=True):
			self.transfer_type = "Repairing"

	if (
		self.current_department or self.next_department
	) and self.current_department == self.next_department:
		frappe.throw(_("Current and Next department cannot be same"))
	if self.type == "Receive" and self.receive_against:
		if existing := frappe.db.exists(
			"Department IR",
			{"receive_against": self.receive_against, "name": ["!=", self.name], "docstatus": ["!=", 2]},
		):
			frappe.throw(
				_("Department IR: {0} already exists for Issue: {1}").format(existing, self.receive_against)
			)


def validate_mwo(self):
	if self.type != "Issue":
		return

	for i in self.department_ir_operation:
		is_finding_mwo = frappe.db.get_value(
			"Manufacturing Work Order", i.manufacturing_work_order, "is_finding_mwo"
		)
		if is_finding_mwo:
			if not self.is_finding:
				frappe.throw(
					_("Finding MWO {0} not allowd to transfer in {1} Department.").format(
						i.manufacturing_work_order, self.next_department
					)
				)


def get_summary_data(self):
	data = [
		{
			"gross_wt": 0,
			"net_wt": 0,
			"finding_wt": 0,
			"diamond_wt": 0,
			"gemstone_wt": 0,
			"other_wt": 0,
			"diamond_pcs": 0,
			"gemstone_pcs": 0,
		}
	]
	for row in self.department_ir_operation:
		for i in data[0]:
			if row.get(i):
				value = row.get(i)
				if i in ["diamond_pcs", "gemstone_pcs"] and row.get(i):
					value = int(row.get(i))
				data[0][i] += flt(value, 3)
			data[0][i] = flt(data[0][i], 3)

	return data


def validate_and_update_gross_wt_from_mop(self):
	if not self.department_ir_operation:
		return

	validate_duplicate(self)
	for row in self.department_ir_operation:
		mwo_list = []
		validate_allowed_operation(row.manufacturing_work_order, self.next_department)
		doc = update_mop_balance(row.manufacturing_operation)
		update_previous_mop_data(doc)

		mop_data = frappe.db.get_value(
			"Manufacturing Operation",
			row.manufacturing_operation,
			[
				"gross_wt",
				"diamond_wt",
				"net_wt",
				"finding_wt",
				"diamond_pcs",
				"gemstone_pcs",
				"gemstone_wt",
				"other_wt",
			],
			as_dict=1,
		)
		previous_mop = frappe.db.get_value(
			"Manufacturing Operation", row.manufacturing_operation, "previous_mop"
		)

		previous_mop_data = frappe._dict()

		if previous_mop:
			previous_mop_data = frappe.db.get_value(
				"Manufacturing Operation",
				previous_mop,
				[
					"received_gross_wt",
					"gross_wt",
					"diamond_wt",
					"net_wt",
					"finding_wt",
					"diamond_pcs",
					"gemstone_pcs",
					"gemstone_wt",
					"other_wt",
				],
				as_dict=1,
			)

		row.gross_wt = (
			mop_data.get("gross_wt")
			or previous_mop_data.get("received_gross_wt")
			or previous_mop_data.get("gross_wt")
		)
		row.net_wt = mop_data.get("net_wt") or previous_mop_data.get("net_wt")
		row.diamond_wt = mop_data.get("diamond_wt") or previous_mop_data.get("diamond_wt")
		row.finding_wt = mop_data.get("finding_wt") or previous_mop_data.get("finding_wt")
		row.diamond_pcs = mop_data.get("diamond_pcs") or previous_mop_data.get("diamond_pcs")
		row.gemstone_pcs = mop_data.get("gemstone_pcs") or previous_mop_data.get("gemstone_pcs")
		row.gemstone_wt = mop_data.get("gemstone_wt") or previous_mop_data.get("gemstone_wt")
		row.other_wt = mop_data.get("other_wt") or previous_mop_data.get("other_wt")
		mwo_list.append(row.manufacturing_work_order)

	return mwo_list


def update_previous_mop_data(doc):
	previous_data = frappe.db.get_value(
		"Manufacturing Operation", doc.previous_mop, ["received_gross_wt", "received_net_wt"], as_dict=1
	)

	if previous_data:
		if not previous_data.get("received_net_wt"):
			frappe.db.set_value("Manufacturing Operation", doc.previous_mop, "received_net_wt", doc.net_wt)

		if not previous_data.get("received_gross_wt"):
			frappe.db.set_value(
				"Manufacturing Operation", doc.previous_mop, "received_gross_wt", doc.gross_wt
			)


def validate_allowed_operation(manufacturing_work_order, next_department):
	customer = frappe.db.get_value("Manufacturing Work Order", manufacturing_work_order, "customer")

	ignored_department = []
	if customer:
		ignored_department = frappe.db.get_all(
			"Ignore Department For MOP", {"parent": customer}, ["department"]
		)

	ignored_department = [row.department for row in ignored_department]
	if next_department in ignored_department:
		frappe.throw(_("Customer does not required this operation"))


def validate_duplicate(self):
	mop_list = [row.manufacturing_operation for row in self.department_ir_operation]
	DIP = frappe.qb.DocType("Department IR Operation")
	DI = frappe.qb.DocType("Department IR")

	duplicates = (
		frappe.qb.from_(DIP)
		.left_join(DI)
		.on(DIP.parent == DI.name)
		.select(DIP.manufacturing_operation)
		.where(
			(DI.docstatus != 2)
			& (DI.name != self.name)
			& (DI.type == self.type)
			& (DIP.manufacturing_operation.isin(mop_list))
		)
	).run(pluck="manufacturing_operation")

	if duplicates:
		frappe.throw(title=_("Department IR exists for MOP"), msg="{0}".format(", ".join(duplicates)))


def validate_tolerance(doc, mop_data):
	mop_details = frappe.db.get_value(
		"Manufacturing Operation",
		mop_data["cur_mop"],
		["manufacturing_order", "design_id_bom"],
		as_dict=1,
	)
	customer = frappe.db.get_value(
		"Parent Manufacturing Order", mop_details.manufacturing_order, "customer"
	)
	tolerance_name = None

	tolerance_data = {}
	metal_fields = ["item", "quantity"]
	diamond_fields = [
		"item",
		"quantity",
		"sieve_size_range",
		"size_in_mm as sieve_size",
		"diamond_type",
	]
	gemstone_fields = ["item", "quantity", "gemstone_type", "stone_shape"]
	tolerance_name = frappe.db.get_value(
		"Customer Product Tolerance Master", {"customer_name": customer, "product_tolerance": "Yes"}
	) or frappe.db.get_value(
		"Customer Product Tolerance Master", {"is_standard": 1, "product_tolerance": "Yes"}
	)
	if tolerance_name:

		for row in frappe.db.get_all(
			"Metal Tolerance Table",
			{"parent": tolerance_name},
			[
				"metal_type",
				"range_type",
				"tolerance_range",
				"from_weight",
				"to_weight",
				"plus_percent",
				"minus_percent",
			],
		):
			if row.get("metal_type"):
				tolerance_data.setdefault(row.metal_type, []).append(row)
				# tolerance_data[row.metal_type].append(row)
				if "metal_type" not in metal_fields:
					metal_fields += ["metal_type"]
			else:
				tolerance_data.setdefault("Metal", []).append(row)
				# tolerance_data["Metal"].append(row)
			tolerance_data["metal_included"] = 1

		for row in frappe.db.get_all(
			"Diamond Tolerance Table",
			{"parent": tolerance_name},
			[
				"diamond_type",
				"weight_type",
				"sieve_size",
				"sieve_size_range",
				"from_diamond",
				"to_diamond",
				"plus_percent",
				"minus_percent",
			],
		):
			if row.get("weight_type") != "Universal":
				key = row.sieve_size or row.sieve_size_range or row.diamond_type
				tolerance_data.setdefault(key, []).append(row)
				# tolerance_data[key].append(row)
			else:
				tolerance_data.setdefault("Diamond", []).append(row)
				# tolerance_data["Diamond"].append(row)
			tolerance_data["diamond_included"] = 1

		for row in frappe.db.get_all(
			"Gemstone Tolerance Table",
			{"parent": tolerance_name},
			[
				"weight_type",
				"gemstone_type",
				"gemstone_shape",
				"from_diamond",
				"to_diamond",
				"plus_percent",
				"minus_percent",
			],
		):
			if row.get("weight_type") != "Weight wise":
				key = row.gemstone_shape or row.gemstone_type
				tolerance_data.setdefault(key, []).append(row)
				# tolerance_data[key].append(row)
			else:
				tolerance_data.setdefault("Gemstone", []).append(row)
				# tolerance_data["Gemstone"].append(row)
			tolerance_data["gemstone_included"] = 1

	if not tolerance_name:
		return {}

	temp_data = []
	for row in ["BOM Metal Detail", "BOM Finding Detail"]:
		temp_data += frappe.db.get_all(row, {"parent": mop_details.design_id_bom}, metal_fields)

	for row in temp_data:
		if row.metal_type and tolerance_data.get(row.metal_type):
			for m_data in tolerance_data.get(row.metal_type):
				m_data.setdefault("bom_qty", 0)
				m_data["bom_qty"] += row.quantity or 0
		if not row.metal_type:
			for m_data in tolerance_data["Metal"]:
				m_data.setdefault("bom_qty", 0)
				m_data["bom_qty"] += row.quantity or 0

	temp_data = []
	for row in ["BOM Diamond Detail"]:
		temp_data += frappe.db.get_all(row, {"parent": mop_details.design_id_bom}, diamond_fields)

	for row in temp_data:
		if (
			tolerance_data.get(row.sieve_size)
			or tolerance_data.get(row.sieve_size_range)
			or tolerance_data.get(row.diamond_type)
		):
			if tolerance_data.get(row.sieve_size):
				key = row.sieve_size
			elif tolerance_data.get(row.sieve_size_range):
				key = row.sieve_size_range
			else:
				key = row.diamond_type
			for d_data in tolerance_data[key]:
				d_data.setdefault("bom_qty", 0)
				d_data["bom_qty"] += row.quantity
		elif tolerance_data.get("Diamond"):
			for d_data in tolerance_data["Diamond"]:
				d_data.setdefault("bom_qty", 0)
				d_data["bom_qty"] += row.quantity

	temp_data = []
	for row in ["BOM Gemstone Detail"]:
		temp_data += frappe.db.get_all(row, {"parent": mop_details.design_id_bom}, gemstone_fields)

	for row in temp_data:
		if tolerance_data.get(row.gemstone_type):
			for g_data in tolerance_data[row.gemstone_type]:
				g_data.setdefault("bom_qty", 0)
				g_data["bom_qty"] += row.quantity
		elif tolerance_data.get("Gemstone"):
			for g_data in tolerance_data["Gemstone"]:
				g_data.setdefault("bom_qty", 0)
				g_data["bom_qty"] += row.quantity

	return tolerance_data
