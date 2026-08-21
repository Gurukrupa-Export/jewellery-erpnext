import json

import frappe
from frappe import _
from frappe.model.mapper import get_mapped_doc
from frappe.utils import flt, nowdate

from jewellery_erpnext.jewellery_erpnext.customization.material_request.material_request import (
	make_department_mop_stock_entry,
	make_mop_stock_entry,
)
from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils.before_validate import (
	update_pure_qty,
	validate_warehouse,
)
from jewellery_erpnext.jewellery_erpnext.customization.material_request.utils.prefetch import (
	mri_warehouse_map,
)


def _get_default_gemstone_item(manufacturer):
	"""Get the default dummy gemstone item for a manufacturer."""
	if not manufacturer:
		return None
	return frappe.db.get_value(
		"Manufacturing Setting",
		{"manufacturer": manufacturer},
		"default_gemstone_item",
	)


# def _is_dummy_gemstone_item(item_code, manufacturer):
# 	"""Check if item is the default dummy gemstone item."""
# 	default_gemstone_item = _get_default_gemstone_item(manufacturer)
# 	return item_code == default_gemstone_item if default_gemstone_item else False


def validate_gemstone_alternative_items(self, method=None):
	# Run only when clicking "Send for Reservation"
	if self.workflow_state != "Material Reserved":
		return

	if self.material_request_type != "Manufacture":
		return

	manufacturer = self.custom_manufacturer or frappe.defaults.get_user_default(
		"manufacturer"
	)

	if not manufacturer:
		return

	default_gemstone_item = _get_default_gemstone_item(manufacturer)

	if not default_gemstone_item:
		return

	errors = []

	for idx, row in enumerate(self.items, 1):
		# Check only dummy gemstone items
		if row.item_code == default_gemstone_item:
			# Alternative item mandatory
			if not row.custom_alternative_item:
				errors.append(
					_(
						"Row {0}: Please select Alternative Item for dummy gemstone item."
					).format(idx)
				)

			# Prevent same dummy item again
			elif row.custom_alternative_item == default_gemstone_item:
				errors.append(
					_(
						"Row {0}: Alternative Item cannot be dummy gemstone item."
					).format(idx)
				)

	if errors:
		frappe.throw("<br>".join(errors))


def before_validate(self, method):
	# Auto-derive the transfer type ONLY when it has not been set yet. Once a
	# value exists (chosen manually, or defaulted on a prior save) it is
	# respected and never overwritten on subsequent saves. ``or None`` normalises
	# a blank ("") vs NULL custom_branch so two no-branch warehouses are treated
	# as the same branch (Transfer To Department) instead of "" != None.
	if not self.custom_transfer_type:
		if self.set_warehouse and self.set_from_warehouse:
			source_branch = (
				frappe.db.get_value(
					"Warehouse", self.set_from_warehouse, "custom_branch"
				)
				or None
			)
			target_branch = (
				frappe.db.get_value("Warehouse", self.set_warehouse, "custom_branch")
				or None
			)

			if source_branch == target_branch:
				self.custom_transfer_type = "Transfer To Department"
			else:
				self.custom_transfer_type = "Transfer To Branch"

		elif self.material_request_type == "Manufacture":
			self.custom_transfer_type = "Transfer to Reserve"

	update_pure_qty(self)
	validate_target_item(self)
	validate_warehouse(self)

	if self.custom_manufacturing_operation:
		linked_mo = frappe.db.get_value(
			"Manufacturing Operation",
			self.custom_manufacturing_operation,
			"manufacturing_order",
		)
		if self.manufacturing_order != linked_mo:
			frappe.throw(
				_("Manufacturing Order and Manufacturing Operation are not linked.")
			)


def before_update_after_submit(self, method):
	if self.workflow_state != "Material Transferred to MOP":
		return

	if not self.custom_manufacturing_operation:
		frappe.throw(_("Please select a Manufacturing Operation."))

	mop_fields = frappe.db.get_value(
		"Manufacturing Operation",
		self.custom_manufacturing_operation,
		["status", "department", "previous_mop"],
		as_dict=True,
	)

	if not mop_fields:
		frappe.throw(
			_("Manufacturing Operation {0} not found.").format(
				self.custom_manufacturing_operation
			)
		)

	if mop_fields.status == "Finished":
		frappe.throw(_("Cannot select an operation that is already Finished."))

	# The material physically sits in the Request Items' warehouse -- the
	# "Material Transfer From Reserve" Stock Entry put it there on submit. Gate
	# BOTH branches on that department matching the operation's: ``custom_department``
	# is a write-once stamp of the *source* (bagging) department and is never equal
	# to the operation's department, so keying the guard on it -- or leaving it, as
	# before, only on the no-``custom_department`` branch -- lets every wrong-department
	# operation through.
	#
	# Only enforce once the operation has been walked into a department by a
	# Department IR. The MWO's first operation is minted in Manufacturing Setting's
	# ``default_department`` (manufacturing_work_order.create_manufacturing_operation)
	# and acts as a gathering point for material staged across several departments,
	# so it can never match and must not be blocked.
	# department_ir.create_operation_for_next_dept_new stamps ``previous_mop`` on
	# every Department-IR-created operation, so its absence marks a never-moved one.
	if mop_fields.previous_mop:
		if not self.items or not self.items[0].warehouse:
			frappe.throw(_("Warehouse is missing from Request Items."))

		row_department = frappe.db.get_value(
			"Warehouse", self.items[0].warehouse, "department"
		)

		if mop_fields.department != row_department:
			frappe.throw(
				_(
					"Material is in department {0}, but Manufacturing Operation {1} "
					"belongs to department {2}. Transfer the material to {2} before "
					"using Transfer to MOP."
				).format(
					row_department or _("(not set)"),
					self.custom_manufacturing_operation,
					mop_fields.department or _("(not set)"),
				)
			)

	if self.custom_department:
		make_department_mop_stock_entry(self, mop=self.custom_manufacturing_operation)
	else:
		make_mop_stock_entry(self, mop=self.custom_manufacturing_operation)


def _dimension_key(attribute_value):
	"""Normalise an attribute value the way MariaDB's collation compares it.

	``utf8mb4_unicode_ci`` is case-insensitive and PAD SPACE, so ``"+11-12 "`` and
	``"+11-12"`` are the same string to the database.
	"""
	return (attribute_value or "").strip().casefold()


def _get_dimension(dimension_map, attribute_value):
	"""Look up an Attribute Value row, exact match first, collation-equivalent second."""
	return dimension_map.get(attribute_value) or dimension_map.get(
		_dimension_key(attribute_value)
	)


def validate_target_item(self):
	rows = [row for row in self.items if getattr(row, "custom_alternative_item", None)]
	if not rows:
		return

	# Sieve size + dimensions are fetched for the whole table up front. Per row this cost up
	# to four queries -- two Item Variant Attribute reads and two Attribute Value reads, and
	# only for rows carrying an alternative item -- which is now two queries for the whole
	# document. One "Diamond Sieve Size" row per item is expected; should a malformed
	# duplicate appear, first-win via setdefault keeps the newest row, which is the same one
	# the replaced frappe.db.get_value returned (both resolve to ORDER BY creation DESC).
	item_codes = {row.item_code for row in rows if row.item_code}
	item_codes.update(row.custom_alternative_item for row in rows)
	sieve_size_map = {}
	for d in frappe.get_all(
		"Item Variant Attribute",
		filters={
			"attribute": "Diamond Sieve Size",
			"parent": ("in", sorted(item_codes)),
		},
		fields=["parent", "attribute_value"],
	):
		sieve_size_map.setdefault(d.parent, d.attribute_value)

	attribute_values = {value for value in sieve_size_map.values() if value}
	dimension_map = {}
	if attribute_values:
		for d in frappe.get_all(
			"Attribute Value",
			filters={"name": ("in", sorted(attribute_values))},
			fields=["name", "height", "weight"],
		):
			dimension_map[d.name] = d
			# Item Variant Attribute.attribute_value is free-text Data, so it carries no
			# guarantee of matching an Attribute Value name byte for byte. The replaced
			# frappe.db.get_value compared in SQL, where the collation forgives case and
			# trailing spaces; a bare dict lookup does not, and a miss here would skip the
			# size check below entirely -- failing open on a validation. Keep a normalised
			# key alongside the exact one so that tolerance survives.
			dimension_map.setdefault(_dimension_key(d.name), d)

	for row in rows:
		attr_value = sieve_size_map.get(row.item_code)
		if not attr_value:
			continue

		alternative_item_attr_value = sieve_size_map.get(row.custom_alternative_item)

		if not alternative_item_attr_value:
			continue

		height_weight = _get_dimension(dimension_map, attr_value)
		alt_height_weight = _get_dimension(dimension_map, alternative_item_attr_value)

		if not height_weight or not alt_height_weight:
			continue

		height, weight = height_weight.height, height_weight.weight
		alt_height, alt_weight = alt_height_weight.height, alt_height_weight.weight

		if height is None or weight is None or alt_height is None or alt_weight is None:
			continue

		if abs(alt_height - height) > 0.5 or abs(weight - alt_weight) > 0.5:
			frappe.throw(
				_(
					"The Diamond Sieve Size in <b>{0}</b> is not within the size range of <b>{1}</b>."
				).format(row.item_code, row.custom_alternative_item)
			)


def on_submit(self, method=None):
	if not self.custom_reserve_se:
		return

	# Defer the secondary "Material Transfer From Reserve" SE to a background job.
	# Previously this copied + submitted a second full Stock Entry INSIDE the Material
	# Request submit transaction, holding Series/Bin/SLE/SRE row locks for its whole
	# duration (a top deadlock/lock-wait contributor). That SE has no synchronous
	# same-request consumer — it is read later by PMO / mapped-SE creation — so it is
	# safe to materialise it just after the MR commits (eventual consistency, seconds).
	# Idempotent + reconcilable via custom_transfer_se / custom_transfer_se_state.
	if getattr(self, "custom_transfer_se", None):
		return

	self.db_set("custom_transfer_se_state", "Pending", update_modified=False)
	frappe.enqueue(
		materialize_transfer_se,
		queue="long",
		enqueue_after_commit=True,
		job_id=f"mr_transfer_se::{self.name}",
		deduplicate=True,
		mr_name=self.name,
	)


def materialize_transfer_se(mr_name):
	"""Idempotently create + submit the 'Material Transfer From Reserve' SE for an MR.

	Deferred from ``MR.on_submit`` so the submit transaction stays short. Safe to
	re-run: guards on ``custom_transfer_se`` and on an already-existing transfer SE,
	serialises per-MR, and retries only transient 1205/1213. A genuine failure is
	recorded on the MR (state=Failed + error) and logged to Error Log for manual
	re-trigger. If this runs during the EOD sync window, the Stock Entry's EOD-lock
	validator blocks the submit and it is recorded as Failed (re-submit after EOD).
	"""
	from jewellery_erpnext.jewellery_erpnext.bounded_retry import run_with_retry
	from jewellery_erpnext.jewellery_erpnext.serialize import (
		LockTimeoutError,
		conflict_lock,
	)

	try:
		with conflict_lock("mr_transfer_se", mr_name, timeout=30):
			run_with_retry(_create_transfer_se, mr_name)
	except LockTimeoutError:
		# deduplicate normally prevents a second concurrent job; if one slips through,
		# the other holder is already doing the work — just yield.
		return
	except Exception as e:
		frappe.db.rollback()
		frappe.db.set_value(
			"Material Request",
			mr_name,
			{
				"custom_transfer_se_state": "Failed",
				"custom_transfer_se_error": str(e)[:140],
			},
			update_modified=False,
		)
		frappe.db.commit()
		frappe.log_error(
			title=f"MR transfer SE failed: {mr_name}", message=frappe.get_traceback()
		)
		raise


def _create_transfer_se(mr_name):
	"""Core copy + submit, factored out so it can be retried as one idempotent unit."""
	mr = frappe.get_doc("Material Request", mr_name)
	if not mr.custom_reserve_se or mr.get("custom_transfer_se"):
		return

	# Belt-and-suspenders idempotency: a submitted transfer SE already linked to this MR.
	existing = frappe.db.sql(
		"""
		SELECT se.name FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.stock_entry_type = 'Material Transfer From Reserve'
		  AND se.docstatus = 1 AND sed.material_request = %s
		LIMIT 1
		""",
		(mr_name,),
	)
	if existing:
		mr.db_set("custom_transfer_se", existing[0][0], update_modified=False)
		mr.db_set("custom_transfer_se_state", "Done", update_modified=False)
		return

	se_doc = frappe.get_doc("Stock Entry", mr.custom_reserve_se)
	new_se_doc = frappe.copy_doc(se_doc)

	new_se_doc.stock_entry_type = "Material Transfer From Reserve"

	mr_item_to_alternative = {}
	for item_row in mr.items:
		if item_row.custom_alternative_item:
			mr_item_to_alternative[item_row.name] = item_row.custom_alternative_item

	# ``mr`` is already loaded with its child rows, and the copied SE's rows were stamped from
	# them, so the warehouses are resolved in memory instead of read back. Being in-memory
	# also keeps this correct across a bounded_retry rollback, which re-runs the whole
	# function -- an up-front read hoisted out of the loop would not be re-taken.
	mri_warehouse_by_name = mri_warehouse_map(new_se_doc.items, mr)

	for row in new_se_doc.items:
		alternative_item = mr_item_to_alternative.get(row.material_request_item)
		if alternative_item:
			row.item_code = alternative_item

		original_t_warehouse = mri_warehouse_by_name.get(row.material_request_item)
		row.s_warehouse = row.t_warehouse
		row.t_warehouse = original_t_warehouse
		row.serial_and_batch_bundle = None

	new_se_doc.auto_created = 1
	new_se_doc.save()
	new_se_doc.submit()

	mr.db_set("custom_transfer_se", new_se_doc.name, update_modified=False)
	mr.db_set("custom_transfer_se_state", "Done", update_modified=False)


@frappe.whitelist()
def make_stock_in_entry(source_name, target_doc=None):
	def set_missing_values(source, target):
		target.material_request_type = "Material Transfer"
		target.customer = source._customer
		target.set_missing_values()
		target.custom_reserve_se = None

	def update_item(source_doc, target_doc, source_parent):
		target_doc.material_request = source_doc.parent
		target_doc.material_request_item = source_doc.name
		target_doc.warehouse = ""
		target_doc.from_warehouse = source_doc.t_warehouse
		target_doc.qty = source_doc.qty

	return get_mapped_doc(
		"Stock Entry",
		source_name,
		{
			"Stock Entry": {
				"doctype": "Material Request",
				"validation": {"docstatus": ["=", 1]},
			},
			"Stock Entry Detail": {
				"doctype": "Material Request Item",
				"field_map": {
					"name": "ste_detail",
					"parent": "against_stock_entry",
					"serial_no": "serial_no",
					"batch_no": "batch_no",
				},
				"postprocess": update_item,
			},
		},
		target_doc,
		set_missing_values,
	)


@frappe.whitelist()
def make_stock_entry(source_name, target_doc=None):
	def update_item(obj, target, source_parent):
		qty = (
			flt(flt(obj.stock_qty) - flt(obj.ordered_qty)) / target.conversion_factor
			if flt(obj.stock_qty) > flt(obj.ordered_qty)
			else 0
		)
		target.qty = qty
		target.transfer_qty = qty * obj.conversion_factor
		target.conversion_factor = obj.conversion_factor

		if source_parent.material_request_type in [
			"Material Transfer",
			"Customer Provided",
		]:
			target.t_warehouse = obj.warehouse
		else:
			target.s_warehouse = obj.warehouse

		if source_parent.material_request_type == "Customer Provided":
			target.allow_zero_valuation_rate = 1

		if source_parent.material_request_type == "Material Transfer":
			target.s_warehouse = obj.from_warehouse

	def set_missing_values(source, target):
		target.purpose = source.material_request_type
		target.custom_material_request_reference = source.name

		if source.job_card:
			target.purpose = "Material Transfer for Manufacture"
		elif source.material_request_type == "Customer Provided":
			target.purpose = "Material Receipt"

		target.set_transfer_qty()
		target.set_actual_qty()
		target.calculate_rate_and_amount(raise_error_if_no_rate=False)

		if (
			source.material_request_type == "Material Transfer"
			and source.inventory_type == "Customer Goods"
		):
			target.stock_entry_type = "Customer Goods Transfer"
		else:
			target.stock_entry_type = target.purpose

		target.set_job_card_data()

		# Map item batches using O(N) lookup instead of O(N^2)
		batch_map = {
			(i.item_code, i.idx): {"batch": i.batch_no, "serial": i.serial_no}
			for i in source.items
		}
		for itm in target.items:
			mapped_data = batch_map.get((itm.item_code, itm.idx))
			if mapped_data:
				itm.batch_no = mapped_data["batch"]
				itm.serial_no = mapped_data["serial"]

		if source.job_card:
			job_card_details = frappe.get_value(
				"Job Card", source.job_card, ["bom_no", "for_quantity"], as_dict=True
			)
			if job_card_details:
				target.bom_no = job_card_details.bom_no
				target.fg_completed_qty = job_card_details.for_quantity
				target.from_bom = 1

	return get_mapped_doc(
		"Material Request",
		source_name,
		{
			"Material Request": {
				"doctype": "Stock Entry",
				"field_no_map": ["manufacturing_order"],
				"validation": {
					"docstatus": ["=", 1],
					"material_request_type": [
						"in",
						["Material Transfer", "Material Issue", "Customer Provided"],
					],
				},
			},
			"Material Request Item": {
				"doctype": "Stock Entry Detail",
				"field_map": {
					"name": "material_request_item",
					"parent": "material_request",
					"uom": "stock_uom",
					"job_card_item": "job_card_item",
				},
				"postprocess": update_item,
				"condition": lambda doc: flt(
					doc.ordered_qty, doc.precision("ordered_qty")
				)
				< flt(doc.stock_qty, doc.precision("ordered_qty")),
			},
		},
		target_doc,
		set_missing_values,
	)


@frappe.whitelist()
def make_in_transit_stock_entry(
	source_name, to_warehouse, transfer_type, pmo=None, mnfr=None
):
	# All three fields come off the same Warehouse row, so read it once.
	to_department, warehouse_type, in_transit_warehouse = frappe.db.get_value(
		"Warehouse",
		to_warehouse,
		["department", "warehouse_type", "default_in_transit_warehouse"],
	)
	from_department, set_warehouse = frappe.db.get_value(
		"Material Request", source_name, ["set_from_warehouse", "set_warehouse"]
	)

	check_frm_warehus_type = None
	if from_department:
		check_frm_warehus_type = frappe.db.get_value(
			"Warehouse", from_department, "warehouse_type"
		)

	if not in_transit_warehouse:
		frappe.throw(_("Transit warehouse is not mentioned in Target Warehouse"))

	ste_doc = make_stock_entry(source_name)
	if not getattr(ste_doc, "employee", None):
		ste_doc.add_to_transit = 1

	stock_entry_type = frappe.db.get_value(
		"Transfer Type", transfer_type, "stock_entry_type"
	)
	if not stock_entry_type:
		frappe.throw(
			_("Please specify a Stock Entry Type for the selected Transfer Type.")
		)

	if ste_doc.items and ste_doc.items[0].customer:
		ste_doc.stock_entry_type = "Customer Goods Transfer"
	else:
		if (
			check_frm_warehus_type
			and to_department
			and check_frm_warehus_type == "Consumables"
			and warehouse_type == "Consumables"
		):
			ste_doc.stock_entry_type = "Consumables Issue to  Department"
			ste_doc.to_warehouse = set_warehouse
		else:
			ste_doc.stock_entry_type = stock_entry_type
			ste_doc.to_warehouse = in_transit_warehouse
			ste_doc.to_department = to_department

	if mnfr and pmo:
		pmo_doc = frappe.get_value(
			"Parent Manufacturing Order",
			pmo,
			[
				"customer_sample",
				"customer_voucher_no",
				"customer_gold",
				"customer_diamond",
				"customer_stone",
				"customer_good",
				"customer",
			],
			as_dict=True,
		)

		if pmo_doc and all(
			[
				pmo_doc.customer_sample,
				pmo_doc.customer_voucher_no,
				pmo_doc.customer_gold,
				pmo_doc.customer_diamond,
				pmo_doc.customer_stone,
				pmo_doc.customer_good,
			]
		):
			ste_doc.inventory_type = "Customer Goods"
			ste_doc.customer = pmo_doc.customer
			for row in ste_doc.items:
				row.inventory_type = "Customer Goods"
				row.customer = pmo_doc.customer

	for row in ste_doc.items:
		if ste_doc.stock_entry_type == "Consumables Issue to  Department":
			row.t_warehouse = set_warehouse
		else:
			row.t_warehouse = in_transit_warehouse

	return ste_doc


@frappe.whitelist()
def create_stock_entry(self, method):
	validate_gemstone_alternative_items(self)

	if (
		self.workflow_state != "Material Reserved"
		or self.custom_reserve_se
		or not self.manufacturing_order
	):
		return

	se_doc = frappe.new_doc("Stock Entry")
	se_doc.company = self.company

	stock_entry_type = frappe.db.get_value(
		"Transfer Type", self.custom_transfer_type, "stock_entry_type"
	)
	if not stock_entry_type:
		frappe.throw(
			_("Please specify a Stock Entry type for the selected Transfer type.")
		)

	se_doc.stock_entry_type = stock_entry_type
	se_doc.purpose = "Material Transfer"
	se_doc.add_to_transit = True

	# Rows nearly always share a handful of source warehouses, so each distinct one is
	# resolved once rather than costing two queries on every row. The two lookups are
	# memoised separately because the reserve warehouse depends only on the department:
	# several source warehouses in the same department then share one resolution. That
	# second query filters on disabled/department/warehouse_type, none of which is indexed
	# on tabWarehouse, so it is the expensive half and worth collapsing hardest.
	department_map = {}
	reserve_warehouse_map = {}

	for row in self.items:
		if row.from_warehouse not in department_map:
			department_map[row.from_warehouse] = frappe.db.get_value(
				"Warehouse", row.from_warehouse, "department"
			)

		department = department_map[row.from_warehouse]

		if department not in reserve_warehouse_map:
			t_warehouse = frappe.db.get_value(
				"Warehouse",
				{"disabled": 0, "department": department, "warehouse_type": "Reserve"},
				"name",
			)

			if not t_warehouse:
				frappe.throw(
					_("Transit warehouse not found for {0}").format(department)
				)

			reserve_warehouse_map[department] = t_warehouse

		t_warehouse = reserve_warehouse_map[department]

		se_doc.append(
			"items",
			{
				"material_request": self.name,
				"material_request_item": row.name,
				"s_warehouse": row.from_warehouse,
				"t_warehouse": t_warehouse,
				"item_code": row.custom_alternative_item or row.item_code,
				"qty": row.qty,
				"inventory_type": row.inventory_type,
				"customer": row.customer,
				"batch_no": row.batch_no,
				"pcs": row.pcs,
				"cost_center": row.cost_center,
				"sub_setting_type": row.custom_sub_setting_type,
				"use_serial_batch_fields": True,
				"custom_parent_manufacturing_order": self.manufacturing_order,
			},
		)

	se_doc.flags.throw_batch_error = True
	se_doc.save()
	self.custom_reserve_se = se_doc.name
	se_doc.submit()

	frappe.msgprint(_("Reserved Stock Entry {0} has been created").format(se_doc.name))


@frappe.whitelist()
def get_item_details(args, for_update=False):
	if isinstance(args, str):
		args = json.loads(args)

	Item = frappe.qb.DocType("Item")
	ItemDefault = frappe.qb.DocType("Item Default")

	item_data = (
		frappe.qb.from_(Item)
		.left_join(ItemDefault)
		.on(
			(Item.name == ItemDefault.parent)
			& (ItemDefault.company == args.get("company"))
		)
		.select(
			Item.name,
			Item.stock_uom,
			Item.description,
			Item.image,
			Item.item_name,
			Item.item_group,
			Item.has_batch_no,
			Item.sample_quantity,
			Item.has_serial_no,
			Item.allow_alternative_item,
			ItemDefault.expense_account,
			ItemDefault.buying_cost_center,
		)
		.where(
			(Item.name == args.get("item_code"))
			& (Item.disabled == 0)
			& (
				(Item.end_of_life.isnull())
				| (Item.end_of_life < "1900-01-01")
				| (Item.end_of_life > nowdate())
			)
		)
	).run(as_dict=True)

	if not item_data:
		frappe.throw(
			_("Item {0} is inactive or its end-of-life has been reached.").format(
				args.get("item_code")
			)
		)

	item = item_data[0]

	return frappe._dict(
		{
			"uom": item.stock_uom,
			"stock_uom": item.stock_uom,
			"description": item.description,
			"image": item.image,
			"item_name": item.item_name,
			"qty": args.get("qty"),
			"transfer_qty": args.get("qty"),
			"conversion_factor": 1,
			"actual_qty": 0,
			"basic_rate": 0,
			"has_serial_no": item.has_serial_no,
			"has_batch_no": item.has_batch_no,
			"sample_quantity": item.sample_quantity,
			"expense_account": item.expense_account,
		}
	)
