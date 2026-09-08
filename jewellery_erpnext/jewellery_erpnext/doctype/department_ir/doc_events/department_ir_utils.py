import json

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.sample_goods import (
	assert_no_sample_in_operations,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
	get_ledgered_operations,
)
from jewellery_erpnext.utils import get_refined_mwos, is_mwo_refined


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


#: The eight weight columns a Department IR Operation row carries, in the order the grid
#: shows them. Shared by the resolver, the single-row fetcher and the batched reads so a
#: field cannot be added to one and forgotten in the others.
MOP_WT_FIELDS = (
	"gross_wt",
	"diamond_wt",
	"net_wt",
	"finding_wt",
	"diamond_pcs",
	"gemstone_pcs",
	"gemstone_wt",
	"other_wt",
)

#: Every weight field except gross_wt, which has its own two-step previous-MOP fallback.
_FALLBACK_FIELDS = tuple(f for f in MOP_WT_FIELDS if f != "gross_wt")


def resolve_department_ir_row_weights(
	mop_data, previous_mop_data, is_ledgered, is_refined
):
	"""Resolve the eight weights a Department IR Operation row should carry.

	THE single definition of that rule. ``validate_and_update_gross_wt_from_mop`` calls it
	per row on every draft save (batching its reads across the child table), and
	``resolve_weights_for_operation`` feeds the single-row callers --
	``DepartmentIR.scan_manufacturing_operation`` and the "Get Manufacturing Operations"
	mapper. So the grid preview and the saved document cannot disagree: they used to be two
	hand-written implementations and had already drifted (the client never got the refining
	guard, and it compared ``x > 0`` where the server used ``x or y``).

	Mirror the operation exactly -- no previous-MOP fallback -- in three cases:

	* ``is_finding``: a finding's "receive from work order" legitimately empties the
	  operation balance.
	* ``is_refined``: the Refining Entry zeroed the weights because the metal physically
	  left for the refinery, so a 0 here is real (the "gross wt reappears after refining"
	  bug).
	* ``is_ledgered``: MOP Log has been written for this operation, so
	  ``recalculate_manufacturing_operation_weights`` owns every bucket below and a 0 is a
	  MEASUREMENT. This is the case a full "Make Receive Entry" produces -- a
	  ``Material Receive (WORK ORDER)`` debits the ledger to nothing, and the ``or``-chain
	  then resurrected the PREVIOUS operation's figures, mixing that op's post-loss
	  ``received_gross_wt`` with its pre-loss ``net_wt`` and printing gross < net, which
	  ``gross = net + finding + diamond_g + gemstone_g + other`` forbids. It also covers a
	  stone-only operation, whose ``net_wt`` is a real 0 while ``gross_wt`` is positive.

	The three are complementary, not redundant: a refined MWO that is recast has a positive
	post-refining ledger balance, and an operation can be a finding before anything is
	ledgered.

	The fallback is kept for an operation with NO ledger rows: nothing has arrived yet,
	``update_new_mop_wtg`` has not seeded it, and the previous MOP is the only estimate
	available. That is the normal state of a freshly minted operation and what the grid
	shows the operator as the weight expected to arrive.

	Values are written as ``mop_data.get(field) or 0``, never a literal 0: diamond and
	gemstone buckets can be authored outside MOP Log (see the ``prefixes`` narrowing in
	``recalculate_manufacturing_operation_weights``, which exists so the MWO->MOP seed
	survives), and forcing zeros would wipe a legitimately seeded stone weight.
	"""
	mop_data = mop_data or frappe._dict()
	previous_mop_data = previous_mop_data or frappe._dict()
	resolved = frappe._dict()

	if mop_data.get("is_finding") or is_refined or is_ledgered:
		for field in MOP_WT_FIELDS:
			resolved[field] = mop_data.get(field) or 0
		return resolved

	resolved.gross_wt = (
		mop_data.get("gross_wt")
		or previous_mop_data.get("received_gross_wt")
		or previous_mop_data.get("gross_wt")
	)
	for field in _FALLBACK_FIELDS:
		resolved[field] = mop_data.get(field) or previous_mop_data.get(field)

	return resolved


def validate_and_update_gross_wt_from_mop(self):
	if not self.department_ir_operation:
		return

	validate_duplicate(self)

	# A cancelled Issue leg nulls manufacturing_operation on its rows
	# (on_submit_issue_new), and frappe.db.get_value with a blank name has no WHERE clause
	# and would hand back an arbitrary operation's weights.
	live_rows = [
		row for row in self.department_ir_operation if row.manufacturing_operation
	]
	mop_names = {row.manufacturing_operation for row in live_rows}

	# Batched. This loop used to run one frappe.get_doc (a full document load), four
	# get_values and one joined refining query PER ROW, and a Department IR carries up to
	# 300 rows -- roughly 2,400 round trips plus 300 document loads for one draft save.
	mop_map = {}
	if mop_names:
		mop_map = {
			d.name: d
			for d in frappe.get_all(
				"Manufacturing Operation",
				filters={"name": ["in", sorted(mop_names)]},
				fields=["name", "previous_mop", "is_finding", *MOP_WT_FIELDS],
				limit_page_length=0,
			)
		}

	previous_names = {d.previous_mop for d in mop_map.values() if d.previous_mop}
	previous_map = {}
	if previous_names:
		previous_map = {
			d.name: d
			for d in frappe.get_all(
				"Manufacturing Operation",
				filters={"name": ["in", sorted(previous_names)]},
				fields=["name", "received_gross_wt", "received_net_wt", *MOP_WT_FIELDS],
				limit_page_length=0,
			)
		}

	ledgered = get_ledgered_operations(mop_names)
	refined = get_refined_mwos({row.manufacturing_work_order for row in live_rows})

	# Was reset INSIDE the loop, so only the last row's MWO ever escaped -- the "Repairing"
	# detection in valid_reparing_or_next_operation has been judging a whole document by
	# one work order.
	mwo_list = []
	for row in self.department_ir_operation:
		validate_allowed_operation(row.manufacturing_work_order, self.next_department)

		if not row.manufacturing_operation:
			continue

		mop_data = mop_map.get(row.manufacturing_operation) or frappe._dict()
		previous_mop_data = (
			previous_map.get(mop_data.get("previous_mop")) or frappe._dict()
		)
		update_previous_mop_data(mop_data, previous_mop_data)

		resolved = resolve_department_ir_row_weights(
			mop_data,
			previous_mop_data,
			is_ledgered=row.manufacturing_operation in ledgered,
			is_refined=row.manufacturing_work_order in refined,
		)
		apply_department_ir_weights(row, resolved)

		mwo_list.append(row.manufacturing_work_order)

	return mwo_list


def resolve_weights_for_operation(
	manufacturing_operation, manufacturing_work_order=None
):
	"""Fetch-and-resolve for callers holding ONE operation -- the scan and the mapper.

	``validate_and_update_gross_wt_from_mop`` does not use this: it batches its reads
	across the whole child table and calls :func:`resolve_department_ir_row_weights`
	directly. Both end at the same decision function, so a row built here is the row the
	next save would compute.

	``manufacturing_work_order`` is an override for callers that already hold the child
	row's value; it feeds the refining test only. The child's ``fetch_from`` carries
	``fetch_if_empty``, so a caller-supplied MWO is not refreshed from the link and the two
	can legitimately differ.

	Returns the eight weights plus the context needed to build a row.
	"""
	if not manufacturing_operation:
		frappe.throw(_("Manufacturing Operation is required to resolve weights"))

	mop_data = frappe.db.get_value(
		"Manufacturing Operation",
		manufacturing_operation,
		[
			"name",
			"manufacturing_work_order",
			"status",
			"previous_mop",
			"is_finding",
			*MOP_WT_FIELDS,
		],
		as_dict=1,
	)
	if not mop_data:
		frappe.throw(_("No Manufacturing Operation Found"))

	mwo = manufacturing_work_order or mop_data.manufacturing_work_order

	# Guarded on purpose. frappe.db.get_value(dt, None, ...) degrades into an unfiltered
	# read of the first row by the doctype's default sort, which for Manufacturing
	# Operation is `modified DESC` -- the most recently touched operation in the system,
	# whose weights would then feed the fallback.
	previous_mop_data = frappe._dict()
	if mop_data.previous_mop:
		previous_mop_data = (
			frappe.db.get_value(
				"Manufacturing Operation",
				mop_data.previous_mop,
				["received_gross_wt", "received_net_wt", *MOP_WT_FIELDS],
				as_dict=1,
			)
			or frappe._dict()
		)

	resolved = resolve_department_ir_row_weights(
		mop_data,
		previous_mop_data,
		is_ledgered=bool(get_ledgered_operations([mop_data.name])),
		is_refined=is_mwo_refined(mwo),
	)
	resolved.update(
		{
			"manufacturing_operation": mop_data.name,
			"manufacturing_work_order": mwo,
			"status": mop_data.status,
		}
	)
	return resolved


def apply_department_ir_weights(row, values):
	"""Copy the eight resolved weights onto a Department IR Operation row.

	Only the weight columns are touched -- the caller owns manufacturing_operation /
	manufacturing_work_order / status.
	"""
	for field in MOP_WT_FIELDS:
		row.set(field, values.get(field))


def warn_empty_operation_balance(doc, method=None):
	"""Flag an Issue whose operations hold nothing, without blocking it.

	After a full "Make Receive Entry" the operation's ledger is empty, so the transfer moves
	no material -- ``create_mop_log_for_department_ir`` clones
	``get_current_mop_balance_rows``, and an Issue to an ordinary department creates no
	Stock Entry at all. The submit is therefore harmless but almost certainly not what the
	operator intended, so warn rather than throw: this bench has precedent against blocking
	movements (a refined MWO still gets recast).

	Wired on before_submit because before_validate skips the weight refresh once
	docstatus == 1, so the child rows cannot be trusted to still reflect the ledger here --
	this re-reads it.
	"""
	if getattr(doc, "type", None) != "Issue":
		return

	rows = [
		r
		for r in (doc.get("department_ir_operation") or [])
		if r.manufacturing_operation
	]
	if not rows:
		return

	mop_names = {r.manufacturing_operation for r in rows}
	ledgered = get_ledgered_operations(mop_names)
	if not ledgered:
		return

	balances = frappe.get_all(
		"Manufacturing Operation",
		filters={"name": ["in", sorted(ledgered)]},
		fields=["name", "gross_wt"],
		limit_page_length=0,
	)
	empty = sorted(d.name for d in balances if not flt(d.gross_wt))
	if not empty:
		return

	frappe.msgprint(
		_("These operations hold no material and will transfer nothing: {0}").format(
			", ".join(f"<b>{name}</b>" for name in empty)
		),
		title=_("Empty Operation Balance"),
		indicator="orange",
	)


def update_previous_mop_data(mop_data, previous_mop_data):
	"""Stamp the previous operation's received_* the first time this one has weights.

	Takes the already-fetched dicts because the caller batches those reads, and MIRRORS each
	write back into ``previous_mop_data``. The mirroring is load-bearing: the previous-MOP
	fallback reads ``received_gross_wt`` in the same iteration and, before batching, saw the
	value this function had just written.
	"""
	previous_mop = mop_data.get("previous_mop")
	if not (previous_mop and previous_mop_data):
		return

	if not previous_mop_data.get("received_net_wt"):
		frappe.db.set_value(
			"Manufacturing Operation",
			previous_mop,
			"received_net_wt",
			mop_data.get("net_wt"),
		)
		previous_mop_data.received_net_wt = mop_data.get("net_wt")

	if not previous_mop_data.get("received_gross_wt"):
		frappe.db.set_value(
			"Manufacturing Operation",
			previous_mop,
			"received_gross_wt",
			mop_data.get("gross_wt"),
		)
		previous_mop_data.received_gross_wt = mop_data.get("gross_wt")


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
