import json

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.sample_goods import (
	assert_no_sample_in_operations,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.validation_utils import (
	update_mop_balance,
)

# The weight buckets a Department IR Operation row mirrors from its Manufacturing
# Operation. Same names on both doctypes, so one list drives the read and the write and
# the two cannot drift apart. Order matches the child table's field order.
WEIGHT_FIELDS = (
	"gross_wt",
	"net_wt",
	"finding_wt",
	"diamond_wt",
	"diamond_pcs",
	"gemstone_wt",
	"gemstone_pcs",
	"other_wt",
)


def validate_no_sample_issue(doc, method=None):
	"""Block a Department IR "Issue" that would move Customer Sample Goods into production.

	Symmetric to the Employee IR guard; wired on Department IR ``before_submit`` so the
	block lands at the Issue click, before ``on_submit -> on_submit_issue_new`` writes any
	MOP Log. The Department IR issue also clones the operation's current MOP Log balance, so
	the same balance-based detection applies.
	"""
	if getattr(doc, "type", None) != "Issue":
		return
	assert_no_sample_in_operations(doc.department_ir_operation, doc)


def valid_reparing_or_next_operation(self, mwo_list):
	if self.type == "Issue":
		if not mwo_list:
			mwo_list = [
				row.manufacturing_work_order for row in self.department_ir_operation
			]

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
			{
				"receive_against": self.receive_against,
				"name": ["!=", self.name],
				"docstatus": ["!=", 2],
			},
		):
			frappe.throw(
				_("Department IR: {0} already exists for Issue: {1}").format(
					existing, self.receive_against
				)
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
					_(
						"Finding MWO {0} not allowd to transfer in {1} Department."
					).format(i.manufacturing_work_order, self.next_department)
				)


@frappe.whitelist()
def get_summary_data(doc):
	if isinstance(doc, str):
		doc = json.loads(doc)

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

	for row in doc.get("department_ir_operation"):
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
	# Built once, not reset per row: valid_reparing_or_next_operation matches the whole
	# document's work orders against earlier transfers to the same next_department, and a
	# list holding only the last row's MWO made that answer depend on row order.
	mwo_list = []
	for row in self.department_ir_operation:
		validate_allowed_operation(row.manufacturing_work_order, self.next_department)
		doc = update_mop_balance(row.manufacturing_operation)
		update_previous_mop_data(doc)

		mop_data = frappe.db.get_value(
			"Manufacturing Operation",
			row.manufacturing_operation,
			WEIGHT_FIELDS,
			as_dict=1,
		)

		# The row shows what the OPERATION holds, and nothing else. A zero bucket is a
		# real zero, not a missing reading: Manufacturing Operation weights are written
		# only by mop_log.update_wt_detail replaying the MOP Log ledger, so an operation
		# reading 0 is one whose ledger holds nothing.
		#
		# This used to fall through an `or` chain to the previous operation's
		# received_gross_wt / gross_wt, which treats a legitimate 0.0 as "unknown" and
		# resurrects weight the operation does not hold: Department-IR-Labh-2026-02662
		# asked an operator to move 7.99 g out of MOP-G6L23, every bucket of which was 0,
		# because its previous operation MOP-2FR81 still read 7.99. The is_finding and
		# is_mwo_refined carve-outs that guarded this were the two zeroes already known to
		# be honest; every other honest zero was still overwritten. One rule now covers
		# all of them, so there is no branch left to keep in step.
		#
		# A freshly minted in-transit operation is not a counter-example:
		# create_operation_for_next_dept copies the weight buckets forward and
		# create_mop_log_for_department_ir clones the ledger, so it already carries its
		# own figure before this runs.
		#
		# These row weights are display and product-tolerance input only. The stock moves
		# from the MOP Log clone in create_mop_log_for_department_ir, which never reads a
		# row weight, so showing the real 0 cannot mis-move metal.
		for field in WEIGHT_FIELDS:
			setattr(row, field, mop_data.get(field) or 0)
		mwo_list.append(row.manufacturing_work_order)

	return mwo_list


def update_previous_mop_data(doc):
	previous_data = frappe.db.get_value(
		"Manufacturing Operation",
		doc.previous_mop,
		["received_gross_wt", "received_net_wt"],
		as_dict=1,
	)

	if previous_data:
		if not previous_data.get("received_net_wt"):
			frappe.db.set_value(
				"Manufacturing Operation",
				doc.previous_mop,
				"received_net_wt",
				doc.net_wt,
			)

		if not previous_data.get("received_gross_wt"):
			frappe.db.set_value(
				"Manufacturing Operation",
				doc.previous_mop,
				"received_gross_wt",
				doc.gross_wt,
			)


def validate_allowed_operation(manufacturing_work_order, next_department):
	customer = frappe.db.get_value(
		"Manufacturing Work Order", manufacturing_work_order, "customer"
	)

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
		frappe.throw(
			title=_("Department IR exists for MOP"),
			msg="{0}".format(", ".join(duplicates)),
		)


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
		"Customer Product Tolerance Master",
		{"customer_name": customer, "product_tolerance": "Yes"},
	) or frappe.db.get_value(
		"Customer Product Tolerance Master",
		{"is_standard": 1, "product_tolerance": "Yes"},
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
		temp_data += frappe.db.get_all(
			row, {"parent": mop_details.design_id_bom}, metal_fields
		)

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
		temp_data += frappe.db.get_all(
			row, {"parent": mop_details.design_id_bom}, diamond_fields
		)

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
		temp_data += frappe.db.get_all(
			row, {"parent": mop_details.design_id_bom}, gemstone_fields
		)

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
