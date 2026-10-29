# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import frappe
from erpnext.controllers.item_variant import create_variant, get_variant
from frappe import _
from frappe.model import no_value_fields, table_fields
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.query_builder.functions import Max
from frappe.utils import cint, flt, get_link_to_form, strip_html

from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	CUSTOMER_GOODS_INVENTORY_TYPE,
	pmo_expects_customer_goods,
)
from jewellery_erpnext.jewellery_erpnext.doc_events.bom import set_item_variant
from jewellery_erpnext.jewellery_erpnext.doctype.customer_product_tolerance_master.tolerance_utils import (
	diamond_group_key,
	gemstone_group_key,
	group_tolerance_rows,
	metal_group_key,
	pick_tolerance_row,
)
from jewellery_erpnext.jewellery_erpnext.doctype.mould.doc_events.utils import (
	get_current_mould_id,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.close_open import (
	CLOSED_DRAFT_STATE,
	CLOSED_STATE,
	HOLD_DRAFT_STATE,
	HOLD_STATE,
	on_workflow_state_change,
	pmo_blocking_state,
	refuse_while_blocked,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.filters_query import (
	custom_override_grades,
	customer_grades,
	is_customer_diamond_flag,
	resolve_diamond_grade,
	sales_type_expects,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.finding_mwo import (
	create_finding_mwo,
	create_finding_without_so_mwo,
	create_stock_entry,
	delete_without_so_drafts,
	validate_without_so_source_submitted,
)
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.doc_events.utils import (
	update_parent_details,
)
from jewellery_erpnext.utils import get_serial_bom, update_existing

_VARIANT_TO_ITEM_TYPE = {
	"M": "metal_item",
	"D": "diamond_item",
	"G": "gemstone_item",
	"F": "finding_item",
}

_ITEM_TYPE_PREFIX = {
	"metal_item": "M",
	"diamond_item": "D",
	"gemstone_item": "G",
	"finding_item": "F",
	"other_item": "O",
}

# Fields to fetch per BOM table
_BOM_TABLE_FIELDS = {
	"BOM Metal Detail": [
		"item_variant",
		"item",
		"quantity",
		"is_customer_item",
		"metal_type",
		"metal_touch",
		"metal_purity",
		"metal_colour",
	],
	"BOM Finding Detail": [
		"item_variant",
		"item",
		"finding_category",
		"quantity",
		"qty",
		"is_customer_item",
		"metal_type",
		"metal_touch",
		"metal_purity",
		"metal_colour",
		"finding_type",
		"finding_size",
	],
	"BOM Diamond Detail": [
		"item_variant",
		"item",
		"quantity",
		"pcs",
		"is_customer_item",
		"sub_setting_type",
		"diamond_grade",
		"diamond_sieve_size",
		"sieve_size_range",
		"size_in_mm",
		"diamond_type",
		"stone_shape",
		"diamond_cut",
		"diamond_size_in_mm",
		"quality",
	],
	"BOM Gemstone Detail": [
		"item_variant",
		"item",
		"quantity",
		"pcs",
		"is_customer_item",
	],
	"BOM Other Detail": ["item_code", "quantity", "qty"],
}

_ATTRIBUTES_CACHE = {}


def _get_raw_material_warehouse(department):
	"""Return the Raw Material warehouse for a given department."""
	return frappe.db.get_value(
		"Warehouse",
		{"disabled": 0, "department": department, "warehouse_type": "Raw Material"},
		"name",
	)


def _get_variant_attribute_value(row, attribute):
	normalized_attr = frappe.scrub(attribute)
	if attribute == "Finding Sub-Category":
		normalized_attr = "finding_type"
	return row.get(normalized_attr)


def _resolve_existing_variant_item_code(row, bom_table, pmo_diamond_grade=None):
	item_code = row.get("item_variant") or row.get("item_code")
	item_template = row.get("item") or (
		frappe.db.get_value("Item", item_code, "variant_of") if item_code else None
	)

	if item_code:
		item_has_variants = frappe.db.get_value("Item", item_code, "has_variants")
		if not item_has_variants and not item_template:
			return item_code

	if not item_template:
		return item_code

	template_has_variants = frappe.db.get_value("Item", item_template, "has_variants")
	if not template_has_variants:
		return item_template

	if item_template not in _ATTRIBUTES_CACHE:
		_ATTRIBUTES_CACHE[item_template] = frappe.db.get_all(
			"Item Variant Attribute", {"parent": item_template}, pluck="attribute"
		)
	item_attributes = set(_ATTRIBUTES_CACHE[item_template])
	args = {}
	missing_attributes = set()
	for attribute in item_attributes:
		value = _get_variant_attribute_value(row, attribute)
		if value:
			args[attribute] = value
		else:
			missing_attributes.add(attribute)

	if "Diamond Grade" in item_attributes:
		diamond_grade = pmo_diamond_grade or row.get("diamond_grade")
		if diamond_grade:
			args["Diamond Grade"] = diamond_grade
			missing_attributes.discard("Diamond Grade")

	if not args:
		frappe.throw(
			_(
				"Variant attributes are missing in {0} (Row {1}) for template item {2}."
			).format(bom_table, row.get("idx") or row.get("name") or "?", item_template)
		)

	if variant := get_variant(item_template, args):
		return variant

	used_attributes = ", ".join([f"{k}: {v}" for k, v in sorted(args.items())]) or _(
		"None"
	)
	missing_text = ", ".join(sorted(missing_attributes)) or _("None")
	frappe.throw(
		_(
			"Variant not found for template item {0} in {1} (Row {2}). Missing attributes: {3}. Used attributes: {4}."
		).format(
			item_template,
			bom_table,
			row.get("idx") or row.get("name") or "?",
			missing_text,
			used_attributes,
		)
	)


def _validate_non_template_item(item_code, bom_table):
	has_variants = frappe.db.get_value("Item", item_code, "has_variants")
	if has_variants:
		frappe.throw(
			_(
				"Template Item {0} found in {1}. Please select the correct variant item."
			).format(item_code, bom_table),
			title=_("Template Item Selected"),
		)


def _validate_item_exists(item_code, bom_table, row):
	"""Raise a clear error when a resolved item code does not exist in Item master."""
	if not frappe.db.exists("Item", item_code):
		frappe.throw(
			_(
				"Item {0} does not exist in the system. Found in {1} (Row {2}). "
				"Please verify the BOM configuration."
			).format(
				frappe.bold(item_code),
				bom_table,
				row.get("idx") or row.get("name") or "?",
			),
			title=_("Item Not Found"),
		)


def _validate_metal_item_attributes(item_code, row, bom_table):
	"""Verify that a directly-specified metal item's touch and purity match the BOM row.

	Only called when item was given directly (no template → variant resolution),
	i.e. row["item"] is absent. Template-resolved items already match by construction.
	"""
	item_attrs = {
		frappe.scrub(r["attribute"]): r["attribute_value"]
		for r in frappe.db.get_all(
			"Item Variant Attribute",
			{"parent": item_code},
			["attribute", "attribute_value"],
		)
	}
	row_idx = row.get("idx") or row.get("name") or "?"

	row_touch = row.get("metal_touch")
	item_touch = item_attrs.get("metal_touch")
	if row_touch and item_touch and row_touch != item_touch:
		frappe.throw(
			_(
				"Metal Touch mismatch in {0} Row {1}: BOM specifies {2} but item {3} has {4}."
			).format(
				bom_table,
				row_idx,
				frappe.bold(row_touch),
				frappe.bold(item_code),
				frappe.bold(item_touch),
			),
			title=_("Metal Touch Mismatch"),
		)

	row_purity = row.get("metal_purity")
	item_purity = item_attrs.get("metal_purity")
	if row_purity and item_purity and row_purity != item_purity:
		frappe.throw(
			_(
				"Metal Purity mismatch in {0} Row {1}: BOM specifies {2} but item {3} has {4}."
			).format(
				bom_table,
				row_idx,
				frappe.bold(row_purity),
				frappe.bold(item_code),
				frappe.bold(item_purity),
			),
			title=_("Metal Purity Mismatch"),
		)


# What a Hold / Open / Close of the PMO writes besides its state: the reason row and the
# snapshot, both by the action itself (doc_events/close_open.py).
STATE_CHANGE_FIELDS = {"workflow_state", "status_reasons", "hold_snapshot"}


class ParentManufacturingOrder(Document):
	def before_save(self):
		is_new = self.is_new()
		if is_new or self.flags.ignore_validations:
			if is_new:
				# PMOs are inserted from Manufacturing Plan with little more than
				# sales_order_item set, so the parent chain has to be walked here. Doing it on
				# insert stores ref_customer (and the quotation it comes from) on the created
				# row instead of leaving it blank until the first manual save.
				update_parent_details(self)
			# Must stay AFTER update_parent_details: that resolves ref_customer, which the grade
			# is resolved against.
			self._set_diamond_grade()
			return
		update_parent_details(self)
		# Must stay AFTER update_parent_details: that refreshes ref_customer, which the grade is
		# resolved against.
		self._set_diamond_grade()
		if not self.diamond_grade and not frappe.db.get_value(
			"Item", self.item_code, "has_batch_no"
		):
			frappe.throw(_("Diamond Grade is not mentioned in customer"))

	def before_submit(self):
		# A held or closed draft is submitted only through its workflow action, which has
		# already moved the state to Submitted by now; a direct submit still carries the old one.
		if self.get("workflow_state") == HOLD_DRAFT_STATE:
			frappe.throw(
				_("{0} is on Hold: submit it with the Submit action.").format(self.name)
			)
		if self.get("workflow_state") == CLOSED_DRAFT_STATE:
			frappe.throw(_("{0} is Closed and cannot be submitted.").format(self.name))
		# frappe runs validate + before_submit on submit and skips before_save, so the grade has
		# to be resolved here too: without it a PMO that reached submit without a stored grade
		# silently skips the tracking BOM update below.
		if self.is_without_so:
			validate_without_so_source_submitted(
				"Manufacturing Plan", self.manufacturing_plan, _("Manufacturing Plan")
			)
		self._set_diamond_grade()
		if self.diamond_grade and self.custom_tracking_bom:
			frappe.db.sql(
				"""
				UPDATE `tabBOM Diamond Detail`
				SET diamond_grade = %s
				WHERE parent = %s
			""",
				(self.diamond_grade, self.custom_tracking_bom),
			)

	# What the stored grade depends on. A save that touches none of these leaves a PMO created
	# before the policy existed editable for unrelated reasons, rather than dead until someone
	# reconciles its customer master.
	GRADE_POLICY_FIELDS = (
		"diamond_grade",
		"use_custom_diamond_grade",
		"sales_type",
		"is_customer_diamond",
		"diamond_quality",
		"customer",
		"ref_customer",
	)

	def _grade_policy_inputs_changed(self):
		before = self.get_doc_before_save()
		if not before:
			# Insert: nothing to compare against, and a new PMO must be valid from the start.
			return True

		return any(
			self.get(field) != before.get(field) for field in self.GRADE_POLICY_FIELDS
		)

	def _validate_diamond_grade_policy(self):
		"""Enforce the grade rules on the document, not just in the dropdown.

		The link query filters what the picker offers, which is UX: a REST write, an import, a
		server script, or simply a value already sitting in the field when the user ticks
		Use Custom Diamond Grade all reach save without passing through it. Since before_submit
		copies this grade onto the tracking BOM and it also selects the diamond item variant, a
		wrong value here is silent and durable -- so the rule has to live on the save path.
		"""
		if self.flags.ignore_validations or not self._grade_policy_inputs_changed():
			return

		expected = sales_type_expects(self.sales_type)
		if expected is not None and expected != cint(self.is_customer_diamond):
			# Sales Type is what sets customer_diamond on the quotation in the first place, so
			# the two disagreeing is a contradiction rather than a preference -- and leaving it
			# to the dropdown alone let automatic mode save a grade the override forbids.
			frappe.throw(
				_("A {0} order requires Is Customer Diamond to be {1}.").format(
					frappe.bold(self.sales_type),
					_("checked") if expected else _("unchecked"),
				)
			)

		if not (self.use_custom_diamond_grade and self.diamond_grade):
			return

		customer = self.ref_customer or self.customer
		allowed = custom_override_grades(
			customer_grades(customer, self.diamond_quality),
			self.sales_type,
			self.is_customer_diamond,
		)
		if self.diamond_grade in allowed:
			return

		if not allowed:
			frappe.throw(
				_(
					"{0} has no Diamond Grade for quality {1} that this order can use."
				).format(frappe.bold(customer), frappe.bold(self.diamond_quality))
			)

		frappe.throw(
			_(
				"Diamond Grade {0} is not allowed for this order. Choose one of: {1}."
			).format(frappe.bold(self.diamond_grade), ", ".join(allowed))
		)

	def _set_diamond_grade(self):
		if self.use_custom_diamond_grade:
			return

		grade = resolve_diamond_grade(
			self.ref_customer or self.customer,
			self.diamond_quality,
			self.is_customer_diamond,
		)
		if grade:
			self.diamond_grade = grade

	def metal_details(self):
		if not (self.manufacturer and self.metal_type and self.metal_touch):
			return

		metal_purity = frappe.db.get_value(
			"Metal Criteria",
			{
				"parent": self.manufacturer,
				"parenttype": "Manufacturer",
				"parentfield": "metal_criteria",
				"metal_type": self.metal_type,
				"metal_touch": self.metal_touch,
			},
			"metal_purity",
		)
		if metal_purity:
			self.metal_purity = metal_purity

	def after_insert(self):
		if self.custom_tracking_bom:
			if frappe.flags.get("creating_from_manufacturing_plan"):
				frappe.flags.setdefault("_mp_tracking_bom_queue", []).append(
					(self.custom_tracking_bom, self.name)
				)
			else:
				frappe.db.set_value(
					"Tracking Bom",
					self.custom_tracking_bom,
					{"reference_doctype": self.doctype, "reference_docname": self.name},
				)
		# The serial's BOM is the BOM of THAT serial -- the one tagged to it (as kggk_prod
		# always did) -- not whatever the quotation / plan row carried, which for a repair
		# is usually the item's design BOM. The row's value is kept only when the serial
		# has no BOM of its own.
		if serial_bom := get_serial_bom(self.serial_no):
			if serial_bom != self.serial_id_bom:
				self.db_set("serial_id_bom", serial_bom)

	def validate(self):
		# Kept ABOVE the is_new/ignore_validations early-return so the Mould List ID
		# is populated on insert (independent re-derivation from this PMO's own item_code).
		self.mould_id = get_current_mould_id(self.item_code)
		# Also above it: the grade a PMO carries has to be legitimate from the moment it is
		# created, and validate is the only hook frappe runs on both save and submit.
		self._validate_diamond_grade_policy()
		# Resolve derived purity on insert and automated saves as well as submission.
		self.metal_details()
		if self.is_new() or self.flags.ignore_validations:
			return
		validate_mfg_date(self)
		if not self.manufacturer:
			return
		warehouse_details = frappe.db.get_value(
			"Manufacturing Setting",
			{"manufacturer": self.manufacturer},
			[
				"default_department",
				"default_diamond_department",
				"default_gemstone_department",
				"default_finding_department",
				"default_other_material_department",
			],
			as_dict=1,
		)
		self.db_set(
			{
				"metal_department": warehouse_details.get("default_department"),
				"diamond_department": warehouse_details.get(
					"default_diamond_department"
				)
				or None,
				"gemstone_department": warehouse_details.get(
					"default_gemstone_department"
				)
				or None,
				"finding_department": warehouse_details.get(
					"default_finding_department"
				)
				or None,
				"other_material_department": warehouse_details.get(
					"default_other_material_department"
				)
				or None,
			}
		)

	def _state_change_only(self):
		"""True for a save that only moves the workflow state.

		apply_workflow reloads the PMO and changes nothing but its state, so a Hold, Open or
		Close carries no edit that the PMO's own validation has to see -- and must not be
		refused by it: a draft whose Sales Order or Manufacturing Plan has been cancelled since
		is exactly the one to close. Only saves and updates after submit qualify; a submit
		always runs the full validation.
		"""
		if getattr(self, "_action", None) not in ("save", "update_after_submit"):
			return False
		before = self.get_doc_before_save()
		if not before or before.get("workflow_state") == self.get("workflow_state"):
			return False
		for df in self.meta.fields:
			if df.fieldname in STATE_CHANGE_FIELDS:
				continue
			if df.fieldtype in table_fields:
				rows = [row.name for row in self.get(df.fieldname) or []]
				if rows != [row.name for row in before.get(df.fieldname) or []]:
					return False
			elif df.fieldtype not in no_value_fields:
				if self.get(df.fieldname) != before.get(df.fieldname):
					return False
		return True

	def _validate_links(self):
		if self._state_change_only():
			return
		return super()._validate_links()

	def before_validate(self):
		if self._state_change_only():
			self.flags.ignore_validate = True
			self.flags.ignore_mandatory = True

	def before_update_after_submit(self):
		if self._state_change_only():
			self.flags.ignore_mandatory = True

	def on_update(self):
		on_workflow_state_change(self)

	def onload(self):
		# Jewellery Settings is readable by System Manager only, so the form cannot work
		# out the Create Material Request role itself. A PMO on Hold or Closed, or one whose
		# Tracking BOM is submitted, is refused by the server anyway, so the button is not
		# offered for it.
		self.set_onload(
			"can_create_material_request",
			self.docstatus == 1
			and not pmo_blocking_state(self.name)
			and tracking_bom_is_draft(self)
			and can_create_pmo_material_request(),
		)

	def on_update_after_submit(self):
		if self._state_change_only():
			on_workflow_state_change(self)
			return
		update_due_days(self)
		validate_mfg_date(self)
		on_workflow_state_change(self)

	def before_cancel(self):
		# Hold has to be undone (Open) before a cancel, and a Close is final. A cancel through
		# the API (or Hybrid Cancel) skips the workflow, so it is refused here.
		if self.get("workflow_state") == HOLD_STATE:
			frappe.throw(
				_("{0} is on Hold: Open it before cancelling.").format(self.name)
			)
		if self.get("workflow_state") == CLOSED_STATE:
			frappe.throw(_("{0} is Closed and cannot be cancelled.").format(self.name))

	def on_submit(self):
		if self.is_without_so:
			# No Sales Order, customer or Tracking BOM stands behind this PMO, so the tolerance
			# tables, the Tracking BOM submit and the Material Requests below -- all read off
			# those -- have nothing to work on. Its single MWO makes the finding item itself.
			create_finding_without_so_mwo(self)
			return
		# First, before the tolerance save, BOM submit and Material Requests.
		_validate_chain_findings_are_manufacturing_items(self)
		if not self.order_form_type or self.order_form_type == "Order":
			# One update_after_submit cycle for all three tolerance tables instead of one
			# per populator, and none at all for a customer with no tolerance master --
			# the populators return False in that case, matching the old behaviour where
			# they returned before ever reaching their own save(). Must stay ahead of
			# submit_bom / create_material_requests / create_manufacturing_work_order:
			# those db.set_value the PMO row, so a save() after them would raise
			# TimestampMismatchError.
			touched = set_metal_tolerance_table(self)
			touched = set_diamond_tolerance_table(self) or touched
			# touched = set_gemstone_tolerance_table(self) or touched
			if touched:
				self.save()
			self.submit_bom()
			if self.type != "Finding Manufacturing":
				self.create_material_requests()
		create_manufacturing_work_order(self)
		# gemstone_details_set_mandatory_field(self)

	def on_cancel(self):
		if self.is_without_so:
			delete_without_so_drafts(
				"Manufacturing Work Order", {"manufacturing_order": self.name}
			)
			return
		update_existing(
			"Manufacturing Plan Table",
			self.rowname,
			"manufacturing_order_qty",
			f"manufacturing_order_qty - {self.qty}",
		)
		update_existing(
			"Sales Order Item",
			self.sales_order_item,
			"manufacturing_order_qty",
			f"manufacturing_order_qty - {self.qty}",
		)

	def submit_bom(self):
		if (
			frappe.db.get_value("Tracking Bom", self.custom_tracking_bom, "docstatus")
			== 1
		):
			return
		bom = frappe.get_doc("Tracking Bom", self.custom_tracking_bom)
		mfg_data = frappe.db.get_all(
			"Metal Criteria",
			{"parent": self.manufacturer},
			["metal_type", "metal_touch", "metal_purity"],
		)
		metal_data = {}
		for metal in mfg_data:
			metal_data.setdefault(metal.metal_type, frappe._dict())
			metal_data[metal.metal_type].setdefault(
				metal.metal_touch, metal.metal_purity
			)

		if metal_data:
			for metal in bom.metal_detail + bom.finding_detail:
				purity = (metal_data.get(metal.metal_type) or {}).get(metal.metal_touch)
				if purity:
					metal.metal_purity = purity
		bom.flags.ignore_validations = True
		bom.save()

	def update_estimated_delivery_date_in_prev_docs(self):
		frappe.db.set_value(
			"Manufacturing Plan",
			self.manufacturing_plan,
			"estimated_delivery_date",
			self.estimated_delivery_date,
		)

	def create_material_requests(self):
		bom = self.custom_tracking_bom
		if not bom:
			frappe.throw(_("BOM is missing"))

		bom_doc = frappe.get_doc("Tracking Bom", bom)
		if bom_doc.get("bom_type") == "Template":
			set_item_variant(bom_doc)
			bom_doc.save(ignore_permissions=True)

		items = self.get_bom_raw_materials()
		self.make_material_requests(items, self.qty)
		frappe.msgprint(_("Material Request Created !!"))

	def get_bom_raw_materials(self):
		"""Raw materials of the PMO's Tracking BOM, bucketed by item type.

		Each row carries the resolved item_code, the per-piece BOM ``qty`` and the
		source/target warehouses. Shared by the submit-time Material Requests and the
		Create Material Request button, so both read the BOM the same way.
		"""
		bom = self.custom_tracking_bom
		if not bom:
			frappe.throw(_("BOM is missing"))

		warehouse_dict = {
			row.variant: row
			for row in frappe.db.get_all(
				"Variant based Warehouse",
				{"parent": self.manufacturer},
				["variant", "department", "target_warehouse"],
			)
		}

		department_warehouse_map = {}
		for dept_info in warehouse_dict.values():
			if dept_info.department not in department_warehouse_map:
				department_warehouse_map[
					dept_info.department
				] = _get_raw_material_warehouse(dept_info.department)

		metal_items = []
		diamond_items = []
		finding_items = []
		gemstone_items = []
		other_items = []

		for bom_table, fields in _BOM_TABLE_FIELDS.items():
			safe_fields = [f for f in fields if frappe.db.has_column(bom_table, f)]
			data = frappe.get_all(bom_table, {"parent": bom}, safe_fields)
			if not data:
				continue
			for row in data:
				if bom_table == "BOM Gemstone Detail":
					item_code = (
						row.get("item_variant")
						or row.get("item")
						or row.get("item_code")
					)
				else:
					item_code = _resolve_existing_variant_item_code(
						row, bom_table, self.diamond_grade
					)
				if not item_code:
					field_name = (
						"item_variant"
						if bom_table != "BOM Other Detail"
						else "item_code"
					)
					frappe.throw(
						_("{0} is missing in {1}").format(field_name, bom_table)
					)
				if bom_table != "BOM Gemstone Detail":
					_validate_non_template_item(item_code, bom_table)
				_validate_item_exists(item_code, bom_table, row)
				if bom_table in (
					"BOM Metal Detail",
					"BOM Finding Detail",
				) and not row.get("item"):
					_validate_metal_item_attributes(item_code, row, bom_table)
				item_type = get_item_type(item_code)
				variant_key = _ITEM_TYPE_PREFIX[item_type]
				if variant_key not in warehouse_dict:
					frappe.throw(_("Please mention warehouse details in Manufacturer"))
				dept_info = warehouse_dict[variant_key]
				from_wh = department_warehouse_map[dept_info.department]
				if item_type == "metal_item":
					metal_items.append(
						{
							"item_code": item_code,
							"qty": row.quantity,
							"from_warehouse": from_wh,
							"warehouse": dept_info.target_warehouse,
							"is_customer_item": row.is_customer_item,
							"sub_setting_type": None,
							"pcs": None,
						}
					)
				elif item_type == "finding_item":
					finding_items.append(
						{
							"item_code": item_code,
							"qty": row.quantity,
							"from_warehouse": from_wh,
							"warehouse": dept_info.target_warehouse,
							"is_customer_item": row.is_customer_item,
							"sub_setting_type": None,
							"pcs": row.get("qty"),
						}
					)
				elif item_type == "diamond_item":
					diamond_items.append(
						{
							"item_code": item_code,
							"qty": row.quantity,
							"from_warehouse": from_wh,
							"warehouse": dept_info.target_warehouse,
							"is_customer_item": row.is_customer_item,
							"sub_setting_type": row.get("sub_setting_type"),
							"pcs": row.get("pcs"),
						}
					)
				elif item_type == "gemstone_item":
					gemstone_items.append(
						{
							"item_code": item_code,
							"qty": row.quantity,
							"from_warehouse": from_wh,
							"warehouse": dept_info.target_warehouse,
							"is_customer_item": row.is_customer_item,
							"sub_setting_type": None,
							"pcs": row.get("pcs"),
						}
					)
				elif item_type == "other_item":
					other_items.append(
						{
							"item_code": item_code,
							"qty": row.quantity,
							"from_warehouse": from_wh,
							"warehouse": dept_info.target_warehouse,
							"is_customer_item": "0",
							"sub_setting_type": None,
							"pcs": row.get("qty"),
						}
					)

		items = {
			"metal_item": metal_items,
			"diamond_item": diamond_items,
			"finding_item": finding_items,
			"gemstone_item": gemstone_items,
			"other_item": other_items,
		}
		return items

	def make_material_requests(self, items, qty_multiplier):
		"""One draft Material Request per item type in ``items`` (bucketed as
		``get_bom_raw_materials`` returns them), each row asking for ``qty x qty_multiplier``.
		Returns the names of the requests created."""
		mnf_abb = frappe.get_value(
			"Manufacturer", self.manufacturer, "custom_abbreviation"
		)
		material_requests = []

		counter = 1
		for item_type, val in items.items():
			if not val:
				continue

			default_gemstone_item = None
			if item_type == "gemstone_item":
				default_gemstone_item = frappe.db.get_value(
					"Manufacturing Setting",
					{"manufacturer": self.manufacturer},
					"default_gemstone_item",
				)
				if not default_gemstone_item:
					frappe.throw(
						_(
							"Default Gemstone Item is not set in Manufacturing Setting. Please set this item G-PER-DUM-PRE-CC"
						)
					)

			prefix = _ITEM_TYPE_PREFIX[item_type]
			mr_doc = frappe.new_doc("Material Request")
			mr_doc.title = f"MR{prefix}-{mnf_abb}-({self.item_code})-{counter}"
			mr_doc.company = self.company
			mr_doc.material_request_type = "Manufacture"
			mr_doc.schedule_date = frappe.utils.nowdate()
			mr_doc.manufacturing_order = self.name
			mr_doc.custom_manufacturer = self.manufacturer

			# OWNERSHIP IS DECIDED PER MATERIAL TYPE, NOT ALL-OR-NOTHING.
			#
			# This used to require customer_gold AND customer_diamond AND customer_stone AND
			# customer_good to all read "Yes", so an order where the customer supplied only the
			# diamonds -- the ordinary case for a customer-diamond order -- got no ownership stamp
			# at all and its stones were requested as company stock. Each request carries exactly
			# one material type, so the flag for THAT type is the one that decides it:
			# ``_ITEM_TYPE_PREFIX`` already maps the bucket to the same M/F/D/G/O letter
			# ``VARIANT_CUSTOMER_FLAG`` is keyed on.
			#
			# Read off the ``is_customer_*`` checkboxes rather than the Yes/No Data strings beside
			# them: the checkboxes are the normalised form (``is_customer_diamond_flag`` handles
			# "yes" as well as "Yes") and they are what every other reader in the app uses --
			# ``se_utils.get_fifo_batches`` and ``validate_customer_diamond_grade`` among them.
			customer_owned = pmo_expects_customer_goods(self, prefix)

			# A BOM row ticked ``is_customer_item`` is the customer's even when the order-level
			# flag is clear, which is how ownership was expressed before the PMO flags existed.
			# ``cint`` rather than ``== 1``: the BOM stores a Check, but the bucket dicts these
			# rows are built from also carry the string "0" for other_item.
			def _row_is_customer_owned(bom_row):
				return customer_owned or bool(cint(bom_row.get("is_customer_item")))

			# Customer Goods with no customer is worse than no stamp: ``normalize_ownership``
			# rule 3 silently downgrades it back to Regular Stock, which is how a customer's
			# stones end up booked as the company's. Refuse to write the half-stamp instead.
			if not self.customer and any(_row_is_customer_owned(i) for i in val):
				frappe.throw(
					_(
						"{0} requests customer-supplied {1}, but the order has no Customer. "
						"Material booked as Customer Goods without an owner is booked as the "
						"company's, so the request cannot be raised."
					).format(frappe.bold(self.name), item_type.replace("_", " "))
				)

			if customer_owned:
				# ``customer``, NOT ``_customer``. Material Request HAS a real ``customer``
				# field; the leading underscore made this a throwaway Python attribute that
				# ``get_valid_dict`` dropped, so the header has never once been stamped. Same
				# bug class as the ``custom_inventory_type`` -> ``inventory_type`` fix on the
				# rows below, which sat one line away and was found first.
				mr_doc.customer = self.customer
				mr_doc.inventory_type = CUSTOMER_GOODS_INVENTORY_TYPE
			mr_doc.custom_department = frappe.db.get_value(
				"Warehouse", val[0]["from_warehouse"], "department"
			)
			for i in val:
				if i["qty"] > 0:
					item_code_to_use = (
						default_gemstone_item
						if item_type == "gemstone_item"
						else i["item_code"]
					)
					mr_doc.append(
						"items",
						{
							"item_code": item_code_to_use,
							"qty": i["qty"] * qty_multiplier,
							"warehouse": i["warehouse"],
							"from_warehouse": i["from_warehouse"],
							"custom_is_customer_item": i.get("is_customer_item", 0),
							"custom_sub_setting_type": i.get("sub_setting_type"),
							"pcs": i.get("pcs"),
							# ``inventory_type``, NOT ``custom_inventory_type``. Material
							# Request Item has no field by the latter name -- verified in
							# tabCustom Field on kg-gk as well as the test site -- and
							# ``get_valid_dict`` drops unknown child keys without a word, so
							# this ownership stamp has never once reached the database.
							# ``custom_is_customer_item`` above IS a real field and did land,
							# which is why the row looked half-tagged rather than untagged.
							# "Customer Goods", NOT "Customer Stock". ``inventory_type`` is a
							# LINK to Inventory Type, so the value has to exist as a record or
							# the insert hard-throws LinkValidationError. Only kg-gk, alfarsi
							# and gk were checked, and all three hold exactly two records:
							# "Customer Goods" and "Regular Stock". "Customer Stock" exists
							# only on a disposable test site whose fixtures create it.
							#
							# This never mattered while the key was ``custom_inventory_type``:
							# get_valid_dict dropped it before any link check ran. Landing the
							# stamp makes the value real, so it has to be a real one -- and
							# "Customer Goods" is what the header stamp above writes on the
							# parent Material Request, so the header and its rows agree.
							"inventory_type": CUSTOMER_GOODS_INVENTORY_TYPE
							if _row_is_customer_owned(i)
							else None,
							# The owner has to travel WITH the type. ``inventory_type`` alone is a
							# half-stamp: the reserve Stock Entry copies both fields off this row
							# (``doc_events/material_request.create_stock_entry``), and
							# ``normalize_ownership`` rule 3 turns "Customer Goods, owner unknown"
							# straight back into Regular Stock -- the customer's stones booked as
							# the company's. The blank-customer guard above is what lets this be
							# unconditional.
							"customer": self.customer
							if _row_is_customer_owned(i)
							else None,
							"description": i["item_code"]
							if item_type == "gemstone_item"
							else None,
						},
					)
				else:
					frappe.throw(
						f"Please Check BOM Table:<b>{prefix}</b>, <b>{i['item_code']}</b> is {i['qty']} Not Allowed."
					)
			counter += 1
			mr_doc.save()
			material_requests.append(mr_doc.name)
		return material_requests

	@frappe.whitelist()
	def send_to_customer_for_approval(self):
		refuse_while_blocked(self.name, _("It cannot be sent for customer approval."))
		mwo_list = frappe.db.get_all(
			"Manufacturing Work Order",
			{"manufacturing_order": self.name, "docstatus": 1},
			["name", "department", "manufacturing_operation"],
		)
		department_data = {}
		for row in mwo_list:
			department_data.setdefault(row.department, []).append(
				row.manufacturing_operation
			)

		if len(department_data) > 1:
			frappe.throw(
				_("All Manufacturing Work Orders should be in same Department")
			)
			return

		create_stock_entry(self, department_data)


def get_item_type(item_code):
	if item_code in _VARIANT_TO_ITEM_TYPE:
		return _VARIANT_TO_ITEM_TYPE[item_code]
	variant_of = frappe.db.get_value("Item", item_code, "variant_of")
	return _VARIANT_TO_ITEM_TYPE.get(variant_of, "other_item")


@frappe.whitelist()
def get_item_code(sales_order_item):
	return frappe.db.get_value("Sales Order Item", sales_order_item, "item_code")


@frappe.whitelist()
def make_manufacturing_order(
	source_doc,
	row,
	master_bom=None,
	so_det=None,
	service_type=None,
	mp_context=None,
):
	so_det = so_det or {}
	service_type = service_type or []
	mp_context = mp_context or {}

	if row.sales_order:
		doc = frappe.new_doc("Parent Manufacturing Order")
		doc.company = source_doc.company
		doc.sales_order = row.sales_order
		doc.custom_tracking_bom = row.custom_tracking_bom
		doc.sales_order_item = row.docname
		doc.item_code = row.item_code
		doc.metal_type = so_det.get("metal_type")
		doc.metal_touch = so_det.get("metal_touch")
		doc.metal_colour = so_det.get("metal_colour")
		doc.customer_sample = row.customer_sample
		doc.customer_voucher_no = row.customer_voucher_no
		# All four read the Yes/No text through the SAME case-insensitive helper. Only
		# is_customer_diamond did, so a plan row saved as "yes" set the diamond flag and
		# silently cleared the other three -- and a cleared is_customer_gemstone is an order
		# whose customer's stones get requested as company stock.
		doc.is_customer_gold = is_customer_diamond_flag(row.customer_gold)
		# Shared with Manufacturing Plan's own reading of the same string, so the grade the plan
		# computes for a row and the one this PMO resolves on save agree on the flag.
		doc.is_customer_diamond = is_customer_diamond_flag(row.customer_diamond)
		doc.is_customer_gemstone = is_customer_diamond_flag(row.customer_stone)
		doc.is_customer_material = is_customer_diamond_flag(row.customer_good)
		doc.customer_weight = row.customer_weight
		doc.repair_type = row.repair_type
		doc.product_type = row.product_type
		doc.type = source_doc.select_manufacture_order
		doc.service_type = service_type
		doc.manufacturing_plan = source_doc.name
		doc.qty = row.qty_per_manufacturing_order
		doc.rowname = row.name
		doc.master_bom = master_bom
		doc.diamond_grade = so_det.get("diamond_grade")
		# Repair lineage, carried explicitly off the plan row.
		#
		# Both fields used to arrive only by side-channel: serial_no via the
		# ``sales_order_item.serial_no`` fetch_from, and serial_id_bom via after_insert
		# re-deriving it from BOM.tag_no. The plan row already holds both (queried in
		# get_items_for_production and stored on Manufacturing Plan Table), so the values
		# were being thrown away and then guessed at again -- and the guess fails whenever
		# no BOM is tagged to the serial, leaving a Repair PMO (and every MWO under it)
		# with no serial and no serial BOM, which is what hides Unpack Serial No.
		# Assigning them here makes the row the single source of truth.
		doc.serial_no = row.serial_no
		doc.serial_id_bom = row.serial_id_bom
		doc.insert(ignore_mandatory=True)

	elif row.mwo:
		doc = frappe.new_doc("Parent Manufacturing Order")
		doc.company = source_doc.company
		manufacturer = mp_context.get(
			"manufacturer"
		) or frappe.defaults.get_user_default("manufacturer")
		doc.department = mp_context.get("finding_default_department")
		if doc.department is None and manufacturer:
			doc.department = frappe.db.get_value(
				"Manufacturing Setting",
				{"manufacturer": manufacturer},
				"default_department",
			)
		doc.type = "Finding Manufacturing"
		doc.is_finding_mwo = True
		doc.item_code = row.item_code
		doc.master_bom = so_det.get("master_bom")
		doc.custom_tracking_bom = so_det.get("custom_tracking_bom")
		doc.metal_type = so_det.get("metal_type")
		doc.metal_touch = so_det.get("metal_touch")
		doc.metal_colour = so_det.get("metal_colour")
		doc.manufacturing_plan = source_doc.name
		doc.qty = row.qty_per_manufacturing_order
		doc.rowname = row.name
		doc.insert(ignore_mandatory=True)
		row.manufacturing_bom = so_det.get("master_bom")


def make_finding_without_so_order(source_doc, row, mp_context=None):
	"""Create one Draft Parent Manufacturing Order from a Without-SO plan's Finding Details row.

	There is no Sales Order line or Manufacturing Work Order behind the row, so everything the
	other two paths read off those comes from the row itself. ``row.item_code`` is the finding
	variant the plan resolved from the row's attributes on validate. Setting Type goes to
	``finding_setting_type`` because ``setting_type`` is fetched from the item, and finding
	items carry none. Department and Type are the plan's: its creator's Employee department,
	and the plan's own mode.
	"""
	mp_context = mp_context or {}
	manufacturer = mp_context.get("manufacturer") or frappe.defaults.get_user_default(
		"manufacturer"
	)

	doc = frappe.new_doc("Parent Manufacturing Order")
	doc.company = source_doc.company
	doc.branch = source_doc.branch
	if manufacturer:
		doc.manufacturer = manufacturer
	doc.department = source_doc.department
	doc.type = source_doc.select_manufacture_order
	doc.is_without_so = 1
	doc.item_code = row.item_code
	doc.metal_type = row.metal_type
	doc.metal_touch = row.metal_touch
	doc.metal_purity = row.metal_purity
	doc.metal_colour = row.metal_colour
	doc.finding_category = row.finding_category
	doc.finding_type = row.finding_type
	doc.finding_setting_type = row.setting_type
	doc.finding_size = row.finding_size
	doc.manufacturing_plan = source_doc.name
	doc.qty = 1
	doc.rowname = row.name
	doc.insert()
	return doc


def _get_chain_finding_rows(self):
	"""The Tracking BOM finding rows a Finding Manufacturing Work Order is raised for,
	each with its resolved ``item_variant``."""
	BOMFindingDetail = frappe.qb.DocType("BOM Finding Detail")
	finding_details = (
		frappe.qb.from_(BOMFindingDetail)
		.select("*")
		.where(BOMFindingDetail.parent == self.custom_tracking_bom)
	).run(as_dict=True)

	finding_data = []
	for row in finding_details:
		item_code = row.get("item_variant") or _resolve_existing_variant_item_code(
			row, "BOM Finding Detail", self.diamond_grade
		)

		template_item = row.get("item") or row.get("item_code")
		if not template_item:
			template_item = (
				frappe.db.get_value("Item", item_code, "variant_of") or item_code
			)

		ignore_wo = frappe.db.get_value(
			"Item", template_item, "custom_ignore_work_order"
		)
		if ignore_wo:
			continue

		row["item_variant"] = item_code

		# Only findings with finding_category == "Chains" should create their
		# own Finding Manufacturing Work Order. The resolved/created item is
		# the same one used when raising the Material Request, so
		# create_finding_mwo() makes the final call from the item's
		# custom_is_manufacturing_item flag.
		if (
			row.get("finding_category") == "Chains"
			and row.get("parentfield") == "finding_detail"
		):
			finding_data.append(row)

	return finding_data


def _validate_chain_findings_are_manufacturing_items(self):
	"""Refuse the submit when a chain would get no Finding Manufacturing Work Order.

	create_finding_mwo() skips a chain whose variant is not ticked Is Manufacturing
	Item, and the PMO used to submit without one -- unnoticed until the floor asked
	for the chain. Ticking the item afterwards never raised the missing order.
	"""
	if not self.custom_tracking_bom:
		return
	# Same gate as create_manufacturing_work_order(): no Finding MWO is made here.
	variant_of = frappe.get_cached_value("Item", self.item_code, "variant_of")
	if self.type == "Finding Manufacturing" and variant_of != "F":
		return

	not_ticked = sorted(
		{
			row.item_variant
			for row in _get_chain_finding_rows(self)
			if not frappe.db.get_value(
				"Item", row.item_variant, "custom_is_manufacturing_item"
			)
		}
	)
	if not_ticked:
		frappe.throw(
			_(
				"Chain item(s) {0} are not marked Is Manufacturing Item, so no Finding "
				"Work Order would be created for this PMO. Tick Is Manufacturing Item "
				"on the item(s) and submit again."
			).format(", ".join(frappe.bold(item) for item in not_ticked)),
			title=_("Chain Not Marked for Manufacturing"),
		)


def create_manufacturing_work_order(self):
	if not self.custom_tracking_bom:
		return

	BOMMetalDetail = frappe.qb.DocType("BOM Metal Detail")
	# Item = frappe.qb.DocType("Item")

	metal_detail_query = (
		frappe.qb.from_(BOMMetalDetail)
		.select(
			BOMMetalDetail.metal_touch,
			BOMMetalDetail.metal_type,
			BOMMetalDetail.metal_purity,
			BOMMetalDetail.metal_colour,
			BOMMetalDetail.parent,
		)
		.where(BOMMetalDetail.parent == self.custom_tracking_bom)
	)

	metal_details = metal_detail_query.run(as_dict=True)

	finding_data = _get_chain_finding_rows(self)

	# Build multicolour grouping from BOM Metal Detail rows only.
	grouped_data = {}
	variant_of = frappe.get_cached_value("Item", self.item_code, "variant_of")
	for item in metal_details:
		metal_purity = self.metal_purity or item["metal_purity"]
		metal_colour = item["metal_colour"] or self.metal_colour
		if metal_purity not in grouped_data:
			grouped_data[metal_purity] = {metal_colour}
		else:
			grouped_data[metal_purity].add(metal_colour)

	updated_data = [
		{
			"metal_purity": purity,
			"metal_colours": "".join(sorted([c[0].upper() for c in colours])),
		}
		for purity, colours in grouped_data.items()
	]

	mfg_settings = frappe.db.get_value(
		"Manufacturing Setting",
		{"manufacturer": self.manufacturer},
		["default_department", "default_fg_department"],
		as_dict=1,
	)

	if variant_of != "F":
		map_fields = {
			"Parent Manufacturing Order": {
				"doctype": "Manufacturing Work Order",
				"field_map": {"name": "manufacturing_order"},
			}
		}
		template_mwo = get_mapped_doc(
			"Parent Manufacturing Order",
			self.name,
			map_fields,
		)
		last_metal_row = metal_details[-1] if metal_details else None
		for row in metal_details:
			doc = frappe.copy_doc(template_mwo, ignore_no_copy=True)
			for color in updated_data:
				if (
					row.get("metal_purity") == color["metal_purity"]
					and len(color["metal_colours"]) > 1
				):
					doc.multicolour = 1
					doc.allowed_colours = color["metal_colours"]
			doc.seq = int(self.name.split("-")[-1])
			doc.department = mfg_settings.get("default_department")
			doc.metal_touch = self.metal_touch or row.get("metal_touch")
			doc.metal_type = self.metal_type or row.get("metal_type")
			doc.metal_purity = self.metal_purity or row.get("metal_purity")
			doc.metal_colour = row.get("metal_colour") or self.metal_colour
			doc.auto_created = 1
			doc.save()

		# FG item work order (metal attrs follow last BOM metal row, matching prior behaviour)
		fg_doc = frappe.copy_doc(template_mwo, ignore_no_copy=True)
		row = last_metal_row or {}
		fg_doc.seq = int(self.name.split("-")[-1])
		fg_doc.department = mfg_settings.get("default_fg_department")
		fg_doc.metal_touch = self.metal_touch or row.get("metal_touch")
		fg_doc.metal_type = self.metal_type or row.get("metal_type")
		fg_doc.metal_purity = self.metal_purity or row.get("metal_purity")
		fg_doc.metal_colour = self.metal_colour or row.get("metal_colour")
		fg_doc.for_fg = 1
		fg_doc.auto_created = 1
		fg_doc.save()

	if self.type != "Finding Manufacturing" or variant_of == "F":
		create_finding_mwo(self, finding_data)


def get_diamond_item_code_by_variant(self, bom, target_warehouse):
	diamond_list = []
	diamond_bom = frappe.get_doc("Tracking Bom", bom)
	if diamond_bom.diamond_detail:
		for row in diamond_bom.diamond_detail:
			item_template = row.item
			if item_template not in _ATTRIBUTES_CACHE:
				_ATTRIBUTES_CACHE[item_template] = frappe.db.get_all(
					"Item Variant Attribute",
					{"parent": item_template},
					pluck="attribute",
				)
			item_attributes = _ATTRIBUTES_CACHE[item_template]

			args = {
				attr: row.get(frappe.scrub(attr))
				for attr in item_attributes
				if row.get(frappe.scrub(attr))
			}
			args["Diamond Grade"] = self.diamond_grade

			variant = get_variant(item_template, args)
			if variant:
				diamond_list.append(
					{
						"item_code": variant,
						"qty": self.qty,
						"warehouse": target_warehouse,
					}
				)
			else:
				variant_doc = create_variant(item_template, args)
				variant_doc.flags.ignore_permissions = True
				try:
					variant_name = variant_doc.insert().name
				except frappe.DuplicateEntryError:
					frappe.db.rollback()
					variant_name = get_variant(item_template, args)
				diamond_list.append(
					{
						"item_code": variant_name,
						"qty": self.qty,
						"warehouse": target_warehouse,
					}
				)
	return diamond_list


# def get_gemstone_item_code_by_variant(self, bom, target_warehouse):
# 	gemstone_list = []
# 	if self.gemstone_table:
# 		item_template = "G"
# 		if item_template not in _ATTRIBUTES_CACHE:
# 			_ATTRIBUTES_CACHE[item_template] = frappe.db.get_all(
# 				"Item Variant Attribute", {"parent": item_template}, pluck="attribute"
# 			)
# 		item_attributes = _ATTRIBUTES_CACHE[item_template]

# 		for row in self.gemstone_table:
# 			args = {
# 				attr: row.get(frappe.scrub(attr))
# 				for attr in item_attributes
# 				if row.get(frappe.scrub(attr))
# 			}
# 			variant = get_variant(item_template, args)
# 			if variant:
# 				gemstone_list.append(
# 					{
# 						"item_code": variant,
# 						"qty": row.quantity,
# 						"warehouse": target_warehouse,
# 					}
# 				)
# 			else:
# 				variant_doc = create_variant(item_template, args)
# 				variant_doc.flags.ignore_permissions = True
# 				try:
# 					variant_name = variant_doc.insert().name
# 				except frappe.DuplicateEntryError:
# 					frappe.db.rollback()
# 					variant_name = get_variant(item_template, args)

# 				gemstone_list.append(
# 					{
# 						"item_code": variant_name,
# 						"qty": row.quantity,
# 						"warehouse": target_warehouse,
# 					}
# 				)
# 	return gemstone_list


# def get_gemstone_details(self):
# 	bom = self.custom_tracking_bom
# 	if not bom:
# 		frappe.throw(_("Sales Order BOM is Missing on Manufacturing Plan Table"))
# 	bom_doc = frappe.get_doc("Tracking Bom", bom)
# 	if bom_doc.gemstone_detail:
# 		for gem_row in bom_doc.gemstone_detail:
# 			self.append(
# 				"gemstone_table",
# 				{
# 					"price_list_type": gem_row.price_list_type,
# 					"gemstone_type": gem_row.gemstone_type,
# 					"cut_or_cab": gem_row.cut_or_cab,
# 					"stone_shape": gem_row.stone_shape,
# 					"gemstone_quality": gem_row.gemstone_quality,
# 					"gemstone_grade": gem_row.gemstone_grade,
# 					"is_customer_item": gem_row.is_customer_item,
# 					"total_gemstone_rate": gem_row.total_gemstone_rate,
# 					"gemstone_size": gem_row.gemstone_size,
# 					"gemstone_code": gem_row.gemstone_code,
# 					"sub_setting_type": gem_row.sub_setting_type,
# 					"pcs": gem_row.pcs,
# 					"quantity": gem_row.quantity,
# 					"weight_in_gms": gem_row.weight_in_gms,
# 					"stock_uom": gem_row.stock_uom,
# 					"item_variant": gem_row.item_variant,
# 					"gemstone_rate_for_specified_quantity": gem_row.gemstone_rate_for_specified_quantity,
# 					"navratna": gem_row.get("navratna"),
# 					"gemstone_pr": gem_row.get("gemstone_pr"),
# 					"per_pc_or_per_carat": gem_row.get("per_pc_or_per_carat"),
# 				},
# 			)
# 		self.save()


def gemstone_details_set_mandatory_field(self):
	errors = []
	if self.gemstone_table:
		for row in self.gemstone_table:
			if not row.gemstone_quality:
				errors.append("Gemstone Details Table <b>Quality</b> is required.")
			if not row.gemstone_type:
				errors.append("Gemstone Details Table <b>Type</b> is required.")
	if errors:
		frappe.throw("<br>".join(errors))


def _get_tolerance_master(customer):
	"""Name of the Customer Product Tolerance Master for ``customer``, or None."""
	return frappe.db.get_value(
		"Customer Product Tolerance Master", {"customer_name": customer}, "name"
	)


def _throw_no_band(cptm_name, category, group_label, weight):
	"""No master row's From/To band covers ``weight``.

	This is master-data breakage, not "no tolerance applies": the operator has entered
	bands that leave this weight in a gap, so silently skipping would ship an order with
	no tolerance at all. Name the master, the group and the weight so the gap can be
	closed. Customer Product Tolerance Master.validate refuses new overlapping bands, so
	in practice this fires on gaps left by historic data.
	"""
	frappe.throw(
		_(
			"{0}: no tolerance band covers a weight of <b>{1}</b> for {2} in Customer "
			"Product Tolerance Master {3}. Add or correct the From/To band so this "
			"weight falls inside one."
		).format(
			category,
			weight,
			group_label,
			get_link_to_form("Customer Product Tolerance Master", cptm_name),
		),
		title=_("Product Tolerance Band Missing"),
	)


def _group_label(*parts):
	"""Human-readable name for a tolerance group, e.g. "Net Weight / Gold"."""
	return " / ".join([str(part) for part in parts if part]) or _("all rows")


def _percent_bounds(base, minus_percent, plus_percent):
	return (
		round(base * ((100 - flt(minus_percent)) / 100), 4),
		round(base * ((100 + flt(plus_percent)) / 100), 4),
	)


def set_metal_tolerance_table(self):
	cpt = _get_tolerance_master(self.customer)
	if not cpt:
		return False
	cptm = frappe.get_doc("Customer Product Tolerance Master", cpt)
	bom = self.custom_tracking_bom
	if not bom:
		frappe.throw(_("BOM is missing"))
	bom_doc = frappe.get_doc("Tracking Bom", bom)

	# Rebuilt from scratch on every run. The table carries no no_copy, so an amended PMO
	# arrives holding the previous document's rows and would otherwise accumulate a
	# second full set on submit.
	self.set("metal_product_tolerance", [])

	# One winner per (weight_type, metal_type): a master legitimately defines Gross AND
	# Net bands, or Gold AND Silver bands, and each dimension picks its own row. Within a
	# group the rows are weight bands and exactly one covers the BOM weight.
	groups = group_tolerance_rows(cptm.metal_tolerance_table or [], metal_group_key)
	for (weight_type, metal_type), rows in groups.items():
		bom_gross_wt = flt(
			bom_doc.gross_weight
			if weight_type == "Gross Weight"
			else bom_doc.metal_and_finding_weight
		)
		mtt_tbl = pick_tolerance_row(rows, bom_gross_wt)
		if not mtt_tbl:
			_throw_no_band(
				cpt, _("Metal"), _group_label(weight_type, metal_type), bom_gross_wt
			)

		if mtt_tbl.range_type == "Weight Range":
			from_tolerance_wt = round(bom_gross_wt - flt(mtt_tbl.tolerance_range), 4)
			to_tolerance_wt = round(bom_gross_wt + flt(mtt_tbl.tolerance_range), 4)
		else:
			from_tolerance_wt, to_tolerance_wt = _percent_bounds(
				bom_gross_wt, mtt_tbl.minus_percent, mtt_tbl.plus_percent
			)

		self.append(
			"metal_product_tolerance",
			{
				"weight_type": weight_type or None,
				"metal_type": mtt_tbl.metal_type,
				"from_weight": flt(mtt_tbl.from_weight),
				"to_weight": flt(mtt_tbl.to_weight),
				"from_tolerance_wt": from_tolerance_wt,
				"to_tolerance_wt": to_tolerance_wt,
				"standard_tolerance_wt": round(bom_gross_wt, 4),
				"product_wt": flt(
					self.gross_weight
					if weight_type == "Gross Weight"
					else self.net_weight
				),
			},
		)
	return True


def set_diamond_tolerance_table(self):
	cpt = _get_tolerance_master(self.customer)
	if not cpt:
		return False
	cptm = frappe.get_doc("Customer Product Tolerance Master", cpt)
	bom = self.custom_tracking_bom
	if not bom:
		frappe.throw(_("BOM is missing"))
	bom_doc = frappe.get_doc("Tracking Bom", bom)

	self.set("diamond_product_tolerance", [])

	groups = group_tolerance_rows(cptm.diamond_tolerance_table or [], diamond_group_key)
	for (weight_type, diamond_type, scope), rows in groups.items():
		matched_rows = list(bom_doc.diamond_detail)

		# diamond_type scopes the aggregate independently of the sieve scope, so it is
		# applied first and each branch narrows further. Without it a band scoped to one
		# type was computed from every stone of that sieve, other types included --
		# diamond_group_key has always carried the type, the filter just never did.
		# "Universal" keys on "" (the master hides the field for it) and stays unfiltered.
		if diamond_type:
			matched_rows = [d for d in matched_rows if d.diamond_type == diamond_type]

		if weight_type == "MM Size wise":
			matched_rows = [d for d in matched_rows if d.diamond_sieve_size == scope]
		elif weight_type == "Group Size wise":
			matched_rows = [d for d in matched_rows if d.sieve_size_range == scope]
		# "Weight wise" bands the whole product's diamond weight and "Universal" is the
		# band-less catch-all over every diamond, so both take the type-filtered set
		# as-is. The old code filtered Universal on `sieve_size_range is None`, which
		# never matches a blank Link stored as "".

		if not matched_rows:
			# Includes a band whose diamond type does not appear in this BOM at all --
			# it must produce no row rather than one over somebody else's weight.
			continue

		weight_in_cts = sum(flt(d.quantity) for d in matched_rows)
		# A sieve's stone diameter, so only meaningful for MM Size wise where the group
		# is exactly one sieve. Group Size wise spans a range of sieves and Weight wise /
		# Universal span every sieve, so any single row's diameter there is an arbitrary
		# pick and summing them (what this did originally) is meaningless.
		size_in_mm = (
			flt(matched_rows[0].size_in_mm) if weight_type == "MM Size wise" else 0
		)

		dtt_tbl = pick_tolerance_row(rows, weight_in_cts, "from_diamond", "to_diamond")
		if not dtt_tbl:
			_throw_no_band(
				cpt,
				_("Diamond"),
				_group_label(weight_type, diamond_type, scope),
				weight_in_cts,
			)

		from_tolerance_wt, to_tolerance_wt = _percent_bounds(
			weight_in_cts, dtt_tbl.minus_percent, dtt_tbl.plus_percent
		)

		self.append(
			"diamond_product_tolerance",
			{
				"weight_type": dtt_tbl.weight_type,
				"diamond_type": dtt_tbl.diamond_type or None,
				"sieve_size": dtt_tbl.sieve_size
				if weight_type == "MM Size wise"
				else None,
				"sieve_size_range": dtt_tbl.sieve_size_range
				if weight_type == "Group Size wise"
				else None,
				"size_in_mm": round(size_in_mm, 4),
				"from_tolerance_wt": from_tolerance_wt,
				"to_tolerance_wt": to_tolerance_wt,
				"standard_tolerance_wt": round(weight_in_cts, 4),
				"product_wt": flt(self.diamond_weight),
			},
		)
	return True


def set_gemstone_tolerance_table(self):
	cpt = _get_tolerance_master(self.customer)
	if not cpt:
		return False
	cptm = frappe.get_doc("Customer Product Tolerance Master", cpt)
	bom = self.custom_tracking_bom
	if not bom:
		frappe.throw(_("BOM is missing"))
	bom_doc = frappe.get_doc("Tracking Bom", bom)

	self.set("gemstone_product_tolerance", [])

	groups = group_tolerance_rows(
		cptm.gemstone_tolerance_table or [], gemstone_group_key
	)
	for (weight_type, scope), rows in groups.items():
		if weight_type == "Weight Range":
			matched_rows = [
				g for g in bom_doc.gemstone_detail if g.stone_shape == scope
			]
		elif weight_type == "Gemstone Type Range":
			matched_rows = [
				g for g in bom_doc.gemstone_detail if g.gemstone_type == scope
			]
		else:
			matched_rows = list(bom_doc.gemstone_detail)

		if not matched_rows:
			continue

		quantity_sum = sum(flt(g.quantity) for g in matched_rows)

		gtt_tbl = pick_tolerance_row(rows, quantity_sum, "from_diamond", "to_diamond")
		if not gtt_tbl:
			_throw_no_band(
				cpt, _("Gemstone"), _group_label(weight_type, scope), quantity_sum
			)

		from_tolerance_wt, to_tolerance_wt = _percent_bounds(
			quantity_sum, gtt_tbl.minus_percent, gtt_tbl.plus_percent
		)

		self.append(
			"gemstone_product_tolerance",
			{
				"weight_type": gtt_tbl.weight_type,
				"gemstone_shape": gtt_tbl.gemstone_shape
				if weight_type == "Weight Range"
				else None,
				"gemstone_type": gtt_tbl.gemstone_type
				if weight_type == "Gemstone Type Range"
				else None,
				"standard_tolerance_wt": round(quantity_sum, 4),
				"from_tolerance_wt": from_tolerance_wt,
				"to_tolerance_wt": to_tolerance_wt,
				"product_wt": flt(self.gemstone_weight),
			},
		)
	return True


def create_repair_un_pack_stock_entry(self):
	wh = frappe.get_value("Manufacturer", self.manufacturer, "custom_repair_warehouse")
	bom_item = frappe.get_doc("Tracking Bom", self.master_bom)
	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Repack-Repair Unpack",
			"purpose": "Repack",
			"company": self.company,
			"inventory_type": "Regular Stock",
			"auto_created": 1,
			"branch": self.branch,
			"manufacturing_order": self.name,
		}
	)
	source_item = [
		{
			"item_code": self.item_code,
			"qty": self.qty,
			"inventory_type": "Regular Stock",
			"serial_no": self.serial_no,
			"department": self.department,
			"manufacturer": self.manufacturer,
			"use_serial_batch_fields": 1,
			"s_warehouse": wh,
		}
	]
	target_item = []
	for row in bom_item.items:
		row_data = row.__dict__.copy()
		row_data["name"] = None
		row_data["idx"] = None
		row_data["t_warehouse"] = wh
		row_data["inventory_type"] = "Regular Stock"
		row_data["department"] = self.department
		target_item.append(row_data)

	for row in source_item:
		se.append("items", row)
	for row in target_item:
		se.append(
			"items",
			{
				"item_code": row["item_code"],
				"qty": row["qty"],
				"inventory_type": row["inventory_type"],
				"t_warehouse": row["t_warehouse"],
				"use_serial_batch_fields": 1,
			},
		)
	se.save()


def update_due_days(self):
	self.due_days = frappe.utils.date_diff(self.delivery_date, self.posting_date)
	if self.custom_updated_delivery_date:
		self.est_delivery_days = frappe.utils.date_diff(
			self.custom_updated_delivery_date, self.posting_date
		)
	self.manufacturing_end_due_days = frappe.utils.date_diff(
		self.manufacturing_end_date, self.posting_date
	)


def validate_mfg_date(self):
	date = self.custom_updated_delivery_date or self.delivery_date
	if self.manufacturing_end_date and self.manufacturing_end_date >= date:
		frappe.throw(_("Manufacturing date is not allowed over delivery date"))


@frappe.whitelist()
def get_linked_stock_entries(pmo_name):
	StockEntry = frappe.qb.DocType("Stock Entry")
	StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")

	query = (
		frappe.qb.from_(StockEntryDetail)
		.left_join(StockEntry)
		.on(StockEntryDetail.parent == StockEntry.name)
		.select(
			StockEntry.manufacturing_work_order,
			StockEntry.manufacturing_operation,
			StockEntry.department,
			StockEntry.to_department,
			StockEntry.employee,
			StockEntry.stock_entry_type,
			StockEntryDetail.parent,
			StockEntryDetail.item_code,
			StockEntryDetail.item_name,
			StockEntryDetail.qty,
			StockEntryDetail.uom,
		)
		.where(
			(StockEntry.docstatus == 1) & (StockEntry.manufacturing_order == pmo_name)
		)
		.orderby(StockEntry.modified, order=frappe.qb.asc)
	)

	data = query.run(as_dict=True)
	total_qty = len([item["qty"] for item in data])
	return frappe.render_template(
		"jewellery_erpnext/jewellery_erpnext/doctype/parent_manufacturing_order/stock_entry_details.html",
		{"data": data, "total_qty": total_qty},
	)


@frappe.whitelist()
def get_stock_summary(pmo_name):
	mwo = frappe.get_all(
		"Manufacturing Work Order", {"manufacturing_order": pmo_name}, pluck="name"
	)

	mwo_last_operation = []
	if mwo:
		operations = frappe.get_all(
			"Manufacturing Work Order",
			filters={"name": ["in", mwo]},
			pluck="manufacturing_operation",
		)
		mwo_last_operation = [op for op in operations if op is not None]

	StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")
	StockEntry = frappe.qb.DocType("Stock Entry")

	max_se_subquery = (
		frappe.qb.from_(StockEntry)
		.select(
			StockEntry.manufacturing_operation,
			Max(StockEntry.modified).as_("max_modified"),
		)
		.where(StockEntry.docstatus == 1)
		.groupby(StockEntry.manufacturing_operation)
	).as_("max_se")

	query = (
		frappe.qb.from_(StockEntryDetail)
		.left_join(max_se_subquery)
		.on(
			StockEntryDetail.manufacturing_operation
			== max_se_subquery.manufacturing_operation
		)
		.left_join(StockEntry)
		.on(
			(StockEntryDetail.parent == StockEntry.name)
			& (StockEntry.modified == max_se_subquery.max_modified)
		)
		.select(
			StockEntry.manufacturing_work_order,
			StockEntry.manufacturing_operation,
			StockEntryDetail.parent,
			StockEntryDetail.item_code,
			StockEntryDetail.item_name,
			StockEntryDetail.inventory_type,
			StockEntryDetail.pcs,
			StockEntryDetail.batch_no,
			StockEntryDetail.qty,
			StockEntryDetail.uom,
		)
		.where(StockEntry.docstatus == 1)
	)

	if mwo_last_operation:
		query = query.where(
			StockEntryDetail.manufacturing_operation.isin(mwo_last_operation)
		)

	data = query.run(as_dict=True)

	total_qty = 0
	for row in data:
		if row.uom == "Carat":
			total_qty += row.get("qty", 0) * 0.2
		else:
			total_qty += row.get("qty", 0)
	total_qty = round(total_qty, 4)

	return frappe.render_template(
		"jewellery_erpnext/jewellery_erpnext/doctype/parent_manufacturing_order/stock_summery.html",
		{"data": data, "total_qty": total_qty},
	)


@frappe.whitelist()
def add_hold_comment(doctype, docname, reason):
	frappe.logger().info(
		f"add_hold_comment called with: {doctype}, {docname}, {reason}"
	)
	if not reason:
		return
	doc = frappe.get_doc(doctype, docname)
	doc.add_comment("Comment", f"Put on hold due to: {reason}")


def hold_mop(self):
	operations = frappe.db.get_list(
		"Manufacturing Operation",
		filters={"manufacturing_order": self.name},
		fields="name",
	)
	for i in operations:
		if (
			frappe.db.get_value("Manufacturing Operation", i["name"], "status")
			!= "Finished"
		):
			frappe.db.set_value(
				"Manufacturing Operation", i["name"], "status", "Finished"
			)


@frappe.whitelist()
def create_mwo(pmo, doc, reason=None):
	refuse_while_blocked(pmo, _("No new work order can be created for it."))
	cad_mwo = frappe.db.get_value(
		"Manufacturing Work Order", {"manufacturing_order": pmo, "for_cad_cam": 1}
	)
	if cad_mwo:
		return frappe.msgprint(
			f"Manufacturing Work Order for CAD/CAM Department is <b>already</b> created. <b>{get_link_to_form('Manufacturing Work Order', cad_mwo)}</b>"
		)

	doc = frappe.get_doc("Parent Manufacturing Order", pmo)
	fg_doc = get_mapped_doc(
		"Parent Manufacturing Order",
		pmo,
		{
			"Parent Manufacturing Order": {
				"doctype": "Manufacturing Work Order",
				"field_map": {"name": "manufacturing_order"},
			}
		},
	)
	fg_doc.seq = int(pmo.split("-")[-1])
	fg_doc.department = frappe.db.get_value(
		"Manufacturing Setting",
		{"manufacturer": doc.manufacturer},
		"default_cad_department",
	)
	fg_doc.metal_touch = doc.metal_touch
	fg_doc.metal_type = doc.metal_type
	fg_doc.metal_purity = doc.metal_purity
	fg_doc.metal_colour = doc.metal_colour
	fg_doc.for_cad_cam = 1
	fg_doc.auto_created = 1
	if reason:
		fg_doc.reason = reason
	fg_doc.save()
	frappe.msgprint("Manufacturing Work Order for CAD/CAM Department is created.")


def can_create_pmo_material_request(user=None):
	"""True when ``user`` may use Create Material Request on a PMO: Administrator, System
	Manager, or a holder of a role listed in Jewellery Settings. No role listed means
	nobody else may."""
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	roles = set(frappe.get_roles(user))
	if "System Manager" in roles:
		return True
	# Rows of core's Has Role parented to Jewellery Settings never reach a user's own
	# roles: frappe.get_roles reads only rows parented to a User.
	allowed = frappe.get_all(
		"Has Role",
		filters={
			"parenttype": "Jewellery Settings",
			"parentfield": "pmo_material_request_roles",
		},
		pluck="role",
	)
	return bool(roles.intersection(role for role in allowed if role))


def check_pmo_material_request_role():
	if not can_create_pmo_material_request():
		frappe.throw(
			_("You are not permitted to create Material Requests from a PMO."),
			frappe.PermissionError,
		)


def tracking_bom_is_draft(doc):
	"""A submitted Tracking BOM means the finished item has been manufactured, so there is
	nothing left to request; only a draft one can still change."""
	return (
		frappe.db.get_value("Tracking Bom", doc.custom_tracking_bom, "docstatus") == 0
		if doc.custom_tracking_bom
		else False
	)


def check_tracking_bom_is_draft(doc):
	if not tracking_bom_is_draft(doc):
		frappe.throw(
			_(
				"Material Request can be created only while the PMO's Tracking BOM is in draft. "
				"A submitted Tracking BOM means the item is already manufactured."
			)
		)


@frappe.whitelist()
def create_pmo_material_request(pmo_name, items=None):
	"""Create draft Material Requests for the PMO's pending raw materials; returns the names
	of the requests created. ``items`` is a list of ``{"item_code", "qty"}``; without it,
	as from the Create Material Request button, every pending raw material is requested at
	its full pending quantity.

	Locking: the PMO row first, then the PMO's own Material Requests (a narrow, indexed
	range). The Hold/Open/Close flow takes the same two in the same order, and Material
	Request flows only read the PMO, so the button queues behind them instead of deadlocking.
	A second click or user waits here until the first commits."""
	check_pmo_material_request_role()
	try:
		# The locking read of the requests below sees the ones just committed even though
		# this transaction's snapshot is older under REPEATABLE READ.
		frappe.db.get_value(
			"Parent Manufacturing Order", pmo_name, "name", for_update=True
		)
		doc = frappe.get_doc("Parent Manufacturing Order", pmo_name)
		doc.check_permission("read")
		if items is None:
			items = [
				{"item_code": row["item_code"], "qty": row["pending_qty"]}
				for row in get_pending_raw_materials(doc, for_update=True)
				if row["pending_qty"] > 0
			]
		else:
			items = frappe.parse_json(items) or []
		selected = validate_material_request_items(doc, items)
		# The selected quantities are already for the whole order, so no PMO qty multiplier.
		material_requests = doc.make_material_requests(selected, 1)
		add_material_request_comment(doc, material_requests)
		return material_requests
	except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
		# Another session held the PMO or one of its requests past the lock wait. Nothing
		# was created; say so instead of showing the raw database error.
		frappe.throw(
			_(
				"Another change to this PMO's Material Requests is in progress. "
				"Please try again in a moment."
			)
		)


def get_pending_raw_materials(doc, for_update=False):
	"""Every raw material of the PMO's Tracking BOM with its ``required_qty`` for the whole
	order (per-piece BOM qty x PMO qty), the ``requested_qty`` already covered by the PMO's
	non-cancelled Material Requests, and the ``pending_qty`` still left to request.
	``for_update`` reads those requests with a locking read, for the creation path."""
	precision = frappe.get_precision("Material Request Item", "qty")
	raw_materials = []
	for item_type, rows in doc.get_bom_raw_materials().items():
		for row in rows:
			required_qty = flt(flt(row["qty"]) * flt(doc.qty), precision)
			if required_qty <= 0:
				continue
			raw_materials.append(
				{
					"item_type": item_type,
					"item_code": row["item_code"],
					"required_qty": required_qty,
					"requested_qty": 0,
					"pcs": row.get("pcs"),
					"from_warehouse": row["from_warehouse"],
					"warehouse": row["warehouse"],
					"is_customer_item": row.get("is_customer_item"),
					"sub_setting_type": row.get("sub_setting_type"),
				}
			)

	requested = _get_requested_qty_by_item(doc, for_update=for_update)

	# Pass 1: a requested line counts against the BOM row it was raised for -- same item,
	# ownership and sub setting type -- so when one item sits on several rows (a gemstone
	# in two settings, a customer's and a company's stone) a request never covers the
	# wrong row and the next request never carries the wrong ownership.
	for row in raw_materials:
		row["requested_qty"] = _take_qty(
			requested,
			_requirement_key(
				row["item_code"], row["is_customer_item"], row["sub_setting_type"]
			),
			row["required_qty"],
		)

	# Pass 2: what is left counts by item code alone, for a line whose ownership or sub
	# setting type was edited on the request.
	requested_by_item = {}
	for (item_code, _owned, _setting), qty in requested.items():
		if qty > 0:
			requested_by_item[item_code] = requested_by_item.get(item_code, 0) + qty
	for row in raw_materials:
		remaining = row["required_qty"] - row["requested_qty"]
		if remaining > 0:
			row["requested_qty"] += _take_qty(
				requested_by_item, row["item_code"], remaining
			)

	# Pass 3: diamond quantity still unmatched counts against a still-pending diamond row
	# that differs only in Diamond Grade. Users regrade diamond lines on the Material
	# Request (mostly to 6B) before submitting, and those stones are already requested.
	grade_free_requested = {}
	for item_code, qty in requested_by_item.items():
		key = _get_grade_free_key(item_code) if qty > 0 else None
		if key:
			grade_free_requested[key] = grade_free_requested.get(key, 0) + qty
	for row in raw_materials:
		remaining = row["required_qty"] - row["requested_qty"]
		if row["item_type"] != "diamond_item" or remaining <= 0:
			continue
		key = _get_grade_free_key(row["item_code"])
		if key:
			row["requested_qty"] += _take_qty(grade_free_requested, key, remaining)

	for row in raw_materials:
		row["requested_qty"] = flt(row["requested_qty"], precision)
		row["pending_qty"] = flt(row["required_qty"] - row["requested_qty"], precision)
	return raw_materials


def _requirement_key(item_code, is_customer_item, sub_setting_type):
	"""What tells two BOM rows of the same item apart, as stamped on the request rows."""
	return (item_code, cint(is_customer_item), sub_setting_type or "")


def _get_requested_qty_by_item(doc, for_update=False):
	"""Stock-UOM quantity on the PMO's draft and submitted Material Requests, per
	``_requirement_key`` of the BOM row each line was raised for.

	Only requests linked to the PMO through ``manufacturing_order`` count: a request raised
	by hand without that link cannot be told apart from another PMO's demand for the same
	item, so it has to be linked to the PMO to be counted."""
	MaterialRequest = frappe.qb.DocType("Material Request")
	MaterialRequestItem = frappe.qb.DocType("Material Request Item")
	query = (
		frappe.qb.from_(MaterialRequestItem)
		.join(MaterialRequest)
		.on(MaterialRequestItem.parent == MaterialRequest.name)
		.select(
			MaterialRequestItem.item_code,
			MaterialRequestItem.description,
			MaterialRequestItem.stock_qty,
			MaterialRequestItem.custom_is_customer_item,
			MaterialRequestItem.custom_sub_setting_type,
		)
		.where(MaterialRequest.manufacturing_order == doc.name)
		.where(MaterialRequest.docstatus < 2)
	)
	if for_update:
		query = query.for_update()
	lines = query.run(as_dict=True)

	# Gemstones are requested as the manufacturer's default gemstone item with the
	# actual BOM item code in the description (see make_material_requests).
	default_gemstone_item = frappe.db.get_value(
		"Manufacturing Setting",
		{"manufacturer": doc.manufacturer},
		"default_gemstone_item",
	)
	requested = {}
	for line in lines:
		item_code = line.item_code
		if default_gemstone_item and item_code == default_gemstone_item:
			item_code = strip_html(line.description or "").strip()
		key = _requirement_key(
			item_code, line.custom_is_customer_item, line.custom_sub_setting_type
		)
		requested[key] = requested.get(key, 0) + flt(line.stock_qty)
	return requested


def _take_qty(pool, key, qty):
	"""Take up to ``qty`` of ``key`` out of ``pool`` and return what was taken."""
	taken = min(pool.get(key, 0), qty)
	if taken <= 0:
		return 0
	pool[key] -= taken
	return taken


def _get_grade_free_key(item_code):
	"""(template, attributes other than Diamond Grade) for a graded diamond variant, else
	None. Two diamonds with the same key differ only in grade."""
	variant_of = frappe.db.get_value("Item", item_code, "variant_of")
	if not variant_of:
		return None
	attributes = frappe.get_all(
		"Item Variant Attribute",
		{"parent": item_code},
		["attribute", "attribute_value"],
	)
	if not any(a.attribute == "Diamond Grade" for a in attributes):
		return None
	return (
		variant_of,
		tuple(
			sorted(
				(a.attribute, a.attribute_value)
				for a in attributes
				if a.attribute != "Diamond Grade"
			)
		),
	)


def validate_material_request_items(doc, items):
	"""Check the dialog's selection against what the PMO still has pending, worked out
	afresh so a request raised since the dialog opened is not requested again. Returns the
	selection bucketed by item type with each BOM row's details, ready for
	``make_material_requests``."""
	if doc.docstatus != 1:
		frappe.throw(_("Material Request can be created only for a submitted PMO."))
	refuse_while_blocked(doc.name, _("No Material Request can be created for it."))
	check_tracking_bom_is_draft(doc)

	pending_by_item = {}
	# Locking read: a request committed by a concurrent creation is seen and not repeated.
	for row in get_pending_raw_materials(doc, for_update=True):
		if row["pending_qty"] > 0:
			pending_by_item.setdefault(row["item_code"], []).append(row)
	if not pending_by_item:
		frappe.throw(
			_("No pending raw materials are available for Material Request creation.")
		)
	if not items:
		frappe.throw(_("Please select at least one raw material."))

	precision = frappe.get_precision("Material Request Item", "qty")
	errors = []
	requested_by_item = {}
	for item in items:
		item_code = item.get("item_code")
		qty = flt(item.get("qty"), precision)
		if item_code not in pending_by_item:
			errors.append(
				_("Item {0} is not pending in the BOM of this PMO.").format(
					frappe.bold(item_code)
				)
			)
		elif qty <= 0:
			errors.append(
				_("Quantity for {0} must be greater than 0.").format(
					frappe.bold(item_code)
				)
			)
		else:
			requested_by_item[item_code] = flt(
				requested_by_item.get(item_code, 0) + qty, precision
			)

	for item_code, qty in requested_by_item.items():
		pending_qty = flt(
			sum(row["pending_qty"] for row in pending_by_item[item_code]), precision
		)
		if qty > pending_qty:
			errors.append(
				_("Quantity for {0} ({1}) exceeds the pending quantity ({2}).").format(
					frappe.bold(item_code), qty, pending_qty
				)
			)
	if errors:
		frappe.throw("<br>".join(errors), title=_("Invalid Raw Material Selection"))

	# Spread each item's quantity over its pending BOM rows in order, so every Material
	# Request row keeps the warehouses and sub setting type of the BOM row it covers.
	selected = {}
	for item_code, qty in requested_by_item.items():
		for row in pending_by_item[item_code]:
			take = min(qty, row["pending_qty"])
			if take <= 0:
				break
			qty = flt(qty - take, precision)
			selected.setdefault(row["item_type"], []).append(
				{
					"item_code": item_code,
					"qty": take,
					"from_warehouse": row["from_warehouse"],
					"warehouse": row["warehouse"],
					"is_customer_item": row["is_customer_item"],
					"sub_setting_type": row["sub_setting_type"],
					# The BOM pcs describe the whole row; a part of it has no known count,
					# so the user fills pcs in on the draft request.
					"pcs": row["pcs"] if take == row["required_qty"] else None,
				}
			)
	return selected


def add_material_request_comment(doc, material_requests):
	"""Note the new requests on the PMO's timeline. The requests themselves are linked
	through ``manufacturing_order`` and listed under the PMO's Connections."""
	if not material_requests:
		return
	doc.add_comment(
		"Info",
		_("Material Request {0} created for pending raw materials.").format(
			", ".join(
				get_link_to_form("Material Request", name) for name in material_requests
			)
		),
	)
