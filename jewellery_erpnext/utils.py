import json
from collections import defaultdict
from datetime import datetime

import frappe
from erpnext.controllers.item_variant import create_variant, get_variant
from frappe import _
from frappe.desk.reportview import get_match_cond
from frappe.query_builder import CustomFunction
from frappe.query_builder.functions import Locate
from frappe.utils import flt

# One carat is 0.2 g. A ``*_wt_in_gram`` field is a DERIVED view of its carat
# counterpart -- convert the total once, at the end, never per ledger row.
CARAT_TO_GRAM = 0.2


def carat_to_gram(carats, precision=3):
	"""Convert a carat total to grams, rounding exactly once.

	Rounding each row before summing makes ``{diamond,gemstone}_wt_in_gram``
	drift from ``{diamond,gemstone}_wt`` by up to half a milligram per row, which
	surfaces downstream as a phantom ``gross_wt`` loss or gain against
	``prev_gross_wt`` -- see ``recalculate_manufacturing_operation_weights``. The
	carat total is rounded to the field's own precision first, so the invariant
	holds against the value actually stored.
	"""
	return flt(flt(carats, precision) * CARAT_TO_GRAM, precision)


def clamp_negative_balance(qty, pcs=0):
	"""What a MOP Log ``(item, batch)`` balance contributes to a HEADER tally.

	A negative batch balance says the ledger consumed more of a batch than it ever
	held -- physically impossible, so it is corruption, not stock. Every other reader
	of that tier already refuses to count it: SerialNumberCreator's source-row builder
	(``if qty <= 0: continue``), the EOD Stock Entry builder, the PC-to-Tagging
	transfer builder, and ``get_available_qty_pcs_for_mop_item``'s ``max(0.0, ...)``.
	``recalculate_manufacturing_operation_weights`` was the only one that summed it,
	which is why MOP-3DP57 read gross_wt 16.440 against a Serial Number Creator
	total_weight of 16.720 -- one gold batch ``KG2F081-MGL229175Y0-P29A8`` sitting at
	-0.28, returned by a Material Receive stamped to an operation that never held it.

	Clamps WITHOUT a tolerance. ``max`` leaves every non-negative row byte-identical,
	so the blast radius is exactly the corrupt rows; a tolerance would also discard
	sub-milligram POSITIVE balances and change the answer for healthy ledgers.

	qty and pcs are clamped INDEPENDENTLY, mirroring
	``get_available_qty_pcs_for_mop_item``. The two tiers disagree in sign on real
	data -- the FG-MWO seed's ``HAVING SUM(qty_change) > 0 OR SUM(pcs_change) > 0``
	admits a qty-negative row whose pcs sum is positive -- so dropping a whole row on
	a qty signal would silently delete a stone COUNT that Product Certification and
	the Employee IR PCS cap still read.

	HEADER-ONLY, and that is the whole contract. The MOP Log row keeps its negative
	value: the audit trail must stay honest, ``audit_negative_batch_balances`` must
	keep finding it, and ``update_new_mop_wtg``'s delta floor deliberately clones an
	inherited negative forward rather than inventing metal. Clamping the LEDGER would
	add metal no Stock Ledger Entry ever created; clamping the HEADER only stops a
	phantom from being reported as a holding. See TestNewMopBaselineNegativeInheritance,
	which asserts the opposite of this function about the same -0.28 -- both are correct.
	"""
	return max(0.0, flt(qty)), max(0.0, flt(pcs))


@frappe.whitelist()
def set_items_from_attribute(item_template, item_template_attribute):
	if isinstance(item_template_attribute, str):
		item_template_attribute = json.loads(item_template_attribute)
	args = {}
	for row in item_template_attribute:
		if not row.get("attribute_value"):
			frappe.throw(
				f"Row: {row.get('idx')} Please select attribute value for {row.get('item_attribute')}."
			)
		args.update({row.get("item_attribute"): row.get("attribute_value")})
	variant = get_variant(item_template, args)
	if variant:
		return frappe.get_doc("Item", variant)
	else:
		variant = create_variant(item_template, args)
		# create_variant copies all template attributes; drop any that have no
		# value (template attribute not present on the source item) to avoid
		# ERPNext's "Attribute Value None is not valid" validation error.
		variant.attributes = [a for a in variant.attributes if a.attribute_value]
		# The loss/target template often defines fewer attributes than the source
		# item, so get_variant()'s exact attribute-count match can miss an item that
		# was already created (possibly within this same transaction). create_variant
		# assigns a deterministic item_code, so look it up by name to stay idempotent
		# and avoid a DuplicateEntryError.
		if frappe.db.exists("Item", variant.item_code):
			return frappe.get_doc("Item", variant.item_code)
		try:
			variant.save()
		except frappe.DuplicateEntryError:
			return frappe.get_doc("Item", variant.item_code)
		return variant


def bulk_map(doctype, names, fields):
	"""{name: _dict(field->value)} for the given names.

	Behaviour-identical replacement for per-row frappe.db.get_value: a missing
	name OR a NULL column both resolve to None via
	``(bulk_map(...).get(name) or {}).get(field)``.

	``order_by=None`` because frappe.get_all otherwise appends the doctype's own
	default sort (``creation desc`` for Item/Batch, ``creation asc`` for
	Department), costing a filesort on a result that is keyed by the primary key
	and consumed only through ``.get(name)`` — never iterated, so row order is
	unobservable.

	Lives here (a leaf module) rather than in either stock_entry file so both the
	CustomStockEntry controller and the doc_events module can import it without a
	circular import (customization.stock_entry <-> doc_events.stock_entry).
	"""
	names = list({n for n in names if n})
	out = {}
	if names:
		for r in frappe.get_all(
			doctype,
			filters={"name": ["in", names]},
			fields=["name"] + list(fields),
			order_by=None,
		):
			out[r.name] = r
	return out


@frappe.whitelist()
def get_item_from_attribute(metal_type, metal_touch, metal_purity, metal_colour=None):
	# items are created without metal_touch as attribute so not considering it in condition for now
	ItemVariantAttribute = frappe.qb.DocType("Item Variant Attribute")
	Item = frappe.qb.DocType("Item")

	# Subqueries for each attribute
	mtp = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(
			ItemVariantAttribute.parent,
			ItemVariantAttribute.attribute_value.as_("metal_type"),
		)
		.where(ItemVariantAttribute.attribute == "Metal Type")
	).as_("mtp")

	mt = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(
			ItemVariantAttribute.parent,
			ItemVariantAttribute.attribute_value.as_("metal_touch"),
		)
		.where(ItemVariantAttribute.attribute == "Metal Touch")
	).as_("mt")

	mp = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(
			ItemVariantAttribute.parent,
			ItemVariantAttribute.attribute_value.as_("metal_purity"),
		)
		.where(ItemVariantAttribute.attribute == "Metal Purity")
	).as_("mp")

	mc = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(
			ItemVariantAttribute.parent,
			ItemVariantAttribute.attribute_value.as_("metal_colour"),
		)
		.where(ItemVariantAttribute.attribute == "Metal Colour")
	).as_("mc")

	# Main query with joins and conditions
	query = (
		frappe.qb.from_(mtp)
		.join(mt)
		.on(mt.parent == mtp.parent)
		.join(mp)
		.on(mp.parent == mtp.parent)
		.join(mc)
		.on(mc.parent == mtp.parent)
		.join(Item)
		.on(Item.name == mtp.parent)
		.select(mtp.parent.as_("item_code"))
		.where(
			(Item.variant_of == "M")
			& (mtp.metal_type == metal_type)
			& (mt.metal_touch == metal_touch)
			& (mp.metal_purity == metal_purity)
		)
	)

	if metal_colour:
		query = query.where(mc.metal_colour == metal_colour)

	data = query.run()
	if data:
		return data[0][0]
	return None


@frappe.whitelist()
def get_item_from_attribute_full(
	metal_type, metal_touch, metal_purity, metal_colour=None
):
	# items are created without metal_touch as attribute so not considering it in condition for now
	ItemVariantAttribute = frappe.qb.DocType("Item Variant Attribute")
	Item = frappe.qb.DocType("Item")

	# Subqueries for each attribute
	mtp = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(
			ItemVariantAttribute.parent,
			ItemVariantAttribute.attribute_value.as_("metal_type"),
		)
		.where(ItemVariantAttribute.attribute == "Metal Type")
	).as_("mtp")

	mt = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(
			ItemVariantAttribute.parent,
			ItemVariantAttribute.attribute_value.as_("metal_touch"),
		)
		.where(ItemVariantAttribute.attribute == "Metal Touch")
	).as_("mt")

	mp = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(
			ItemVariantAttribute.parent,
			ItemVariantAttribute.attribute_value.as_("metal_purity"),
		)
		.where(ItemVariantAttribute.attribute == "Metal Purity")
	).as_("mp")

	mc = (
		frappe.qb.from_(ItemVariantAttribute)
		.select(
			ItemVariantAttribute.parent,
			ItemVariantAttribute.attribute_value.as_("metal_colour"),
		)
		.where(ItemVariantAttribute.attribute == "Metal Colour")
	).as_("mc")

	# Main query with left joins and conditions
	query = (
		frappe.qb.from_(mtp)
		.left_join(mt)
		.on(mt.parent == mtp.parent)
		.left_join(mp)
		.on(mp.parent == mtp.parent)
		.left_join(mc)
		.on(mc.parent == mtp.parent)
		.right_join(Item)
		.on(Item.name == mtp.parent)
		.select(mtp.parent.as_("item_code"))
		.where(
			(Item.variant_of == "M")
			& (mtp.metal_type == metal_type)
			& (mt.metal_touch == metal_touch)
			& (mp.metal_purity == metal_purity)
		)
	)
	if metal_colour:
		query = query.where(mc.metal_colour == metal_colour)

	data = query.run()

	if data:
		return data
	return None


def get_variant_of_item(item_code):
	return frappe.db.get_value("Item", item_code, "variant_of")


def resolve_manufacturing_setting(company=None, manufacturer=None, throw=False):
	"""NAME of the Manufacturing Setting that applies, or ``None``.

	``Manufacturing Setting`` autonames on ``manufacturer``, but the live records are
	per-COMPANY (one each, named after the company) with a ``manufacturer`` that is not a
	real Manufacturer. So a manufacturer-keyed lookup matches nothing for any actual
	manufacturer, and callers that pass one — or that read a session default — get nothing.

	Resolution, extending the fallback in ``doc_events/stock_entry.before_validate``:

	  1. the record matching BOTH ``manufacturer`` and ``company``;
	  2. else the record matching ``manufacturer`` alone;
	  3. else, among the records for ``company``: the only one, else the only one with a
	     blank ``manufacturer``;
	  4. else ``None``.

	Step 1 exists because a manufacturer's setting is company-scoped: on gk.site
	``Labh``'s setting belongs to KGJPL, yet GEPL has entries stamped ``manufacturer =
	Labh``, and a manufacturer-only lookup would hand them the other company's config. The
	rest of the app does the manufacturer-only lookup, so step 2 keeps behaviour identical
	wherever the exact pair does not exist.

	Step 3 never GUESSES between siblings: a company that genuinely keeps one setting per
	manufacturer resolves to ``None`` rather than silently picking someone else's config.

	Callers get the NAME so they can read whichever field — or child table, keyed on
	``parent`` — they need. ``throw=False`` (the default) returns ``None`` quietly, which is
	what any fail-open consumer wants.
	"""
	if manufacturer:
		if company:
			name = frappe.db.get_value(
				"Manufacturing Setting",
				{"manufacturer": manufacturer, "company": company},
				"name",
			)
			if name:
				return name
		name = frappe.db.get_value(
			"Manufacturing Setting", {"manufacturer": manufacturer}, "name"
		)
		if name:
			return name

	if company:
		settings = frappe.get_all(
			"Manufacturing Setting",
			filters={"company": company},
			fields=["name", "manufacturer"],
			order_by="name",
		)
		if len(settings) == 1:
			return settings[0].name
		company_wide = [s for s in settings if not s.manufacturer]
		if len(company_wide) == 1:
			return company_wide[0].name

	if throw:
		frappe.throw(
			_(
				"No Manufacturing Setting found for manufacturer {0} or company {1}"
			).format(
				frappe.bold(manufacturer or _("(not set)")),
				frappe.bold(company or _("(not set)")),
			)
		)
	return None


def update_existing(doctype, name, field, value=None, debug=False):
	modified = frappe.utils.now()
	modified_by = frappe.session.user
	Doc = frappe.qb.DocType(doctype)

	query = (
		frappe.qb.update(Doc)
		.set(Doc.modified, modified)
		.set(Doc.modified_by, modified_by)
		.where(Doc.name == name)
	)

	if isinstance(field, dict):
		# If field is a dictionary, prepare multiple field updates
		for key, _value in field.items():
			if isinstance(_value, str) and ("+" in _value or "-" in _value):
				operation = _value.split()
				if (
					len(operation) == 3
					and operation[0] == key
					and operation[2].lstrip("-").replace(".", "", 1).isdigit()
				):
					query = query.set(
						getattr(Doc, key), getattr(Doc, key) + float(operation[2])
					)
				else:
					query = query.set(getattr(Doc, key), _value)
			else:
				query = query.set(getattr(Doc, key), _value)
	else:
		# Single field update
		if isinstance(value, str) and ("+" in value or "-" in value):
			operation = value.split()
			if (
				len(operation) == 3
				and operation[0] == field
				and operation[2].lstrip("-").replace(".", "", 1).isdigit()
			):
				query = query.set(
					getattr(Doc, field), getattr(Doc, field) + float(operation[2])
				)
			else:
				query = query.set(getattr(Doc, field), value)
		else:
			query = query.set(getattr(Doc, field), value)

	query.run(debug=debug)


def is_mwo_refined(manufacturing_work_order):
	"""True when a submitted Work Order Refining Entry has consumed this MWO's metal.

	Once refined, the MWO is dead per SOP (qty and all operation weights are zeroed
	by the Refining Entry); operations created afterwards must start at 0 weight and
	must not inherit/clone pre-refining balances. Checked via the Refining Entry's
	mwo_details child table so a cancelled Refining Entry (docstatus 2) automatically
	un-marks the MWO — no flag field to keep in sync."""
	if not manufacturing_work_order:
		return False
	return bool(
		frappe.db.sql(
			"""
			SELECT 1
			FROM `tabManufacturing Work Order Refining Details` d
			INNER JOIN `tabRefining Entry` re ON re.name = d.parent
			WHERE d.manufacturing_work_order = %s
			  AND d.parenttype = 'Refining Entry'
			  AND re.docstatus = 1
			  AND re.refining_type = 'Work Order Refining'
			LIMIT 1
			""",
			(manufacturing_work_order,),
		)
	)


def get_mwo_refining_cutoff(manufacturing_work_order):
	"""Submit time of the Work Order Refining Entry that consumed this MWO's metal, else None.

	Companion to :func:`is_mwo_refined`. Refining zeroes the MWO's balances, so every MOP
	Log row created at or before this moment describes metal that no longer exists. Readers
	that rebuild an opening balance from the ledger must ignore those rows: on data written
	before the refining zero-out was made reliable, a pre-refining row can still be the
	latest one for its (item, batch) and would resurrect a dead balance.

	Recasting a refined MWO is an expected manual flow (``complete_refining`` only
	*advises* the Work Order action), so this is a cutoff, never a reason to drop a real
	movement -- post-refining stock movements must still be ledgered, just against a zero
	opening balance.
	"""
	if not manufacturing_work_order:
		return None
	rows = frappe.db.sql(
		"""
		SELECT MAX(re.modified) AS refined_on
		FROM `tabManufacturing Work Order Refining Details` d
		INNER JOIN `tabRefining Entry` re ON re.name = d.parent
		WHERE d.manufacturing_work_order = %s
		  AND d.parenttype = 'Refining Entry'
		  AND re.docstatus = 1
		  AND re.refining_type = 'Work Order Refining'
		""",
		(manufacturing_work_order,),
		as_dict=True,
	)
	return rows[0].get("refined_on") if rows else None


def set_values_in_bulk(doctype, doclist, values):
	Doc = frappe.qb.DocType(doctype)
	query = frappe.qb.update(Doc)

	for key, val in values.items():
		query = query.set(key, val)

	query = query.where(Doc.name.isin(doclist))
	query = query.run()


def get_value(doctype, filters, fields, default=None, debug=0):
	Doc = frappe.qb.DocType(doctype)

	fields = fields if isinstance(fields, list) else [fields]

	query = frappe.qb.from_(Doc).select(*fields)

	conditions = []
	for key, value in filters.items():
		if isinstance(value, str):
			value = frappe.db.escape(value)
		conditions.append(Doc[key] == value)

	for condition in conditions:
		query = query.where(condition)

	res = query.run(debug=debug)

	if res:
		return res[0][0] or default

	return default


@frappe.whitelist()
def db_get_value(doctype, docname, fields):
	# this is created to bypass permission issue during db call from client script
	import json

	fields = json.loads(fields)
	return frappe.db.get_value(doctype, docname, fields, as_dict=1)


# searches for customers with Sales Type
@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def customer_query(doctype, txt, searchfield, start, page_len, filters):
	"""query to filter customers with sales type"""

	Customer = frappe.qb.DocType("Customer")
	SalesType = frappe.qb.DocType("Sales Type")

	txt = f"%{txt}%"
	_txt = txt.replace("%", "")

	IF = CustomFunction("IF", ["condition", "true_expr", "false_expr"])

	sales_type_subquery = (
		frappe.qb.from_(SalesType)
		.select(SalesType.parent)
		.where(SalesType.sales_type == filters["sales_type"])
	)

	query = (
		frappe.qb.from_(Customer)
		.select(
			Customer.name,
			Customer.customer_name,
			Customer.customer_group,
			Customer.territory,
		)
		.where(
			(Customer.docstatus < 2)
			& (Customer.name.isin(sales_type_subquery))
			& (frappe.qb.Field(searchfield).like(txt))
			| (Customer.customer_name.like(txt))
			| (Customer.territory.like(txt))
			| (Customer.customer_group.like(txt))
		)
		.limit(page_len)
		.offset(start)
	)
	# Add match conditions
	match_cond = get_match_cond(doctype)
	if match_cond:
		query = query.where(match_cond)

	# Add ordering conditions
	order_by_conditions = [
		IF(Locate(_txt, Customer.name), Locate(_txt, Customer.name), 99999),
		IF(
			Locate(_txt, Customer.customer_name),
			Locate(_txt, Customer.customer_name),
			99999,
		),
		IF(
			Locate(_txt, Customer.customer_group),
			Locate(_txt, Customer.customer_group),
			99999,
		),
		IF(Locate(_txt, Customer.territory), Locate(_txt, Customer.territory), 99999),
		Customer.customer_name,
		Customer.name,
	]
	query = query.orderby(*order_by_conditions)

	customers = query.run()

	return customers


@frappe.whitelist()
def get_sales_invoice_items(sales_invoices):
	if isinstance(sales_invoices, str):
		sales_invoices = json.loads(sales_invoices)

	items = frappe.get_all(
		"Sales Invoice Item",
		{"parent": ["in", sales_invoices]},
		[
			"item_code",
			"item_name",
			"uom",
			"qty",
			"rate",
			"serial_no",
			"bom",
			"parent",
			"warehouse",
		],
	)
	sales_invoice_gold_rates = frappe.get_all(
		"Sales Invoice",
		{"name": ["in", sales_invoices]},
		["name", "gold_rate_with_gst"],
	)

	gold_rate_map = {s.name: s.gold_rate_with_gst for s in sales_invoice_gold_rates}

	return {"items": items, "gold_rates": gold_rate_map}


@frappe.whitelist()
def get_sales_order_items(customer_approval_name):
	doc = frappe.get_doc("Customer Approval", customer_approval_name)

	if doc.docstatus != 1:
		frappe.throw(_("This Customer Approval is not submitted."))

	items = frappe.get_all(
		"Sales Order Item Child",
		filters={"parent": customer_approval_name},
		fields=[
			"item_code",
			"rate",
			"item_name",
			"quantity",
			"amount",
			"uom",
			"serial_no",
			"bom_number",
			"delivery_date",
		],
	)
	return items


# searches for suppliers with purchase Type
@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def supplier_query(doctype, txt, searchfield, start, page_len, filters):
	"""query to filter suppliers with purchase type"""
	Supplier = frappe.qb.DocType("Supplier")
	PurchaseType = frappe.qb.DocType("Purchase Type")

	txt = f"%{txt}%"
	_txt = txt.replace("%", "")

	IF = CustomFunction("IF", ["condition", "true_expr", "false_expr"])
	Locate = CustomFunction("LOCATE", ["substr", "str"])

	query_filters = None
	if filters and filters.get("purchase_type"):
		# Subquery to filter suppliers with the specified purchase type
		purchase_type_subquery = (
			frappe.qb.from_(PurchaseType)
			.select(PurchaseType.parent)
			.where(PurchaseType.purchase_type == filters["purchase_type"])
		)
		query_filters = Supplier.name.isin(purchase_type_subquery)

	query = (
		frappe.qb.from_(Supplier)
		.select(Supplier.name, Supplier.supplier_name, Supplier.supplier_group)
		.where(
			(Supplier.docstatus < 2)
			& (
				frappe.qb.Field(searchfield).like(txt)
				| Supplier.supplier_name.like(txt)
				| Supplier.supplier_group.like(txt)
			)
		)
	)

	if query_filters:
		query = query.where(query_filters)

	match_cond = get_match_cond(doctype)
	if match_cond:
		query = query.where(match_cond)

	# Add ordering conditions
	order_by_conditions = [
		IF(Locate(_txt, Supplier.name), Locate(_txt, Supplier.name), 99999),
		IF(
			Locate(_txt, Supplier.supplier_name),
			Locate(_txt, Supplier.supplier_name),
			99999,
		),
		IF(
			Locate(_txt, Supplier.supplier_group),
			Locate(_txt, Supplier.supplier_group),
			99999,
		),
		Supplier.supplier_name,
		Supplier.name,
	]
	query = query.orderby(*order_by_conditions).limit(page_len).offset(start)

	suppliers = query.run()
	return suppliers


@frappe.whitelist()
def get_type_of_party(doc, parent, field):
	return frappe.db.get_value(doc, {"parent": parent}, field)


def is_item_consistent(
	grouped, key, item, group_keys, sum_keys, concat_keys, exclude_keys
):
	"""
	Check if an item's non-group keys are consistent within an existing group.

	Args:
	        grouped (dict): The current grouped items.
	        key (tuple): Group key generated from the item.
	        item (dict): The item to check consistency.
	        group_keys (list[str]): Keys used for grouping.
	        sum_keys (list[str]): Keys whose values are summed.
	        concat_keys (list[str]): Keys whose values are concatenated.

	Returns:
	        bool: True if the item is consistent within the group, False otherwise.
	"""
	for gk in item.keys():
		if gk not in group_keys + sum_keys + concat_keys + exclude_keys:
			if key in grouped and gk in grouped[key]:
				if grouped[key][gk] != item.get(gk):
					return False
	return True


def initialize_group(grouped, key, item, group_keys, sum_keys, concat_keys):
	"""
	Initialize a new group in the grouped dictionary.

	Args:
	        grouped (dict): The current grouped items.
	        key (tuple): Group key generated from the item.
	        item (dict): The item to initialize the group with.
	        group_keys (list[str]): Keys used for grouping.
	        sum_keys (list[str]): Keys whose values are summed.
	        concat_keys (list[str]): Keys whose values are concatenated.
	"""
	grouped[key].update({sk: 0 for sk in sum_keys})
	for ck in concat_keys:
		grouped[key][ck] = []
	for k in group_keys:
		grouped[key][k] = item.get(k)
	for gk in item.keys():
		if gk not in group_keys + sum_keys + concat_keys:
			grouped[key][gk] = item.get(gk)


def aggregate_item(grouped, key, item, sum_keys, concat_keys):
	"""
	Aggregate item values into an existing group.

	Args:
	        grouped (dict): The current grouped items.
	        key (tuple): Group key generated from the item.
	        item (dict): The item to aggregate.
	        sum_keys (list[str]): Keys whose values are summed.
	        concat_keys (list[str]): Keys whose values are concatenated.
	"""
	for sk in sum_keys:
		grouped[key][sk] += float(item.get(sk, 0) or 0)
	for ck in concat_keys:
		val = item.get(ck)
		if val:
			grouped[key][ck].append(str(val))


def finalize_grouped(grouped, concat_keys):
	"""
	Finalize the grouped items by concatenating list fields.

	Args:
	        grouped (dict): The current grouped items.
	        concat_keys (list[str]): Keys whose values are concatenated.

	Returns:
	        list[dict]: Final list of grouped items with concatenated values.
	"""
	final_grouped = []
	for g in grouped.values():
		for ck in concat_keys:
			g[ck] = ",".join(g[ck])
		final_grouped.append(g)
	return final_grouped


def group_aggregate_with_concat(
	items, group_keys, sum_keys, concat_keys, exclude_keys=[]
):
	"""
	Group items based on specified keys, sum numerical fields, and concatenate values.
	If an item is inconsistent (i.e., non-group keys do not match), it is kept separately.

	Args:
	        items (list[dict]): List of items to group and aggregate.
	        group_keys (list[str]): Keys used for grouping items.
	        sum_keys (list[str]): Keys whose values are summed within groups.
	        concat_keys (list[str]): Keys whose values are concatenated within groups.

	Returns:
	        list[dict]: Aggregated and grouped items, with inconsistent items separated.
	"""
	grouped = defaultdict(lambda: {sk: 0 for sk in sum_keys})
	non_grouped = []

	for item in items:
		key = tuple(item.get(k) for k in group_keys)
		if not is_item_consistent(
			grouped, key, item, group_keys, sum_keys, concat_keys, exclude_keys
		):
			non_grouped.append(item)
			continue

		if key not in grouped:
			initialize_group(grouped, key, item, group_keys, sum_keys, concat_keys)

		aggregate_item(grouped, key, item, sum_keys, concat_keys)

	final_grouped = finalize_grouped(grouped, concat_keys)

	return final_grouped + non_grouped


def serialize_for_json(obj):
	if isinstance(obj, datetime):
		return frappe.utils.get_datetime_str(obj)
	if isinstance(obj, frappe.Document):
		return obj.as_dict()

	raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def get_warehouse_from_user(user_id, warehouse_type):
	department = frappe.db.get_value("Employee", {"user_id": user_id}, "department")

	if not department:
		frappe.throw("Department not specified in Employee record")

	warehouse_name = frappe.db.get_value(
		"Warehouse",
		{"warehouse_type": warehouse_type, "department": department},
		"name",
	)

	return warehouse_name
