"""Customer-batch-only findings for Hybrid sales orders.

KG GK builds its orders for GK Export, so every KG GK Sales Order -- and every PMO, MWO and
Customer Goods batch below it -- carries the same customer (GJCU0009). The end customer the
goods really belong to is the order's ``ref_customer``. A "Customer Goods Received" entry
records it on its header (``Stock Entry.ref_customer``) and every batch the entry mints carries
it as ``Batch.custom_ref_customer`` (stamped by ``batch_rename`` and, for batches ERPNext mints,
by ``customization/batch/doc_events/utils.update_inventory_dimentions``).

The rule: in a **Hybrid** order, a finding whose Finding Category -- narrowed to a Finding
Sub-Category when the settings row names one -- is listed in
``Subcontracting Settings.hybrid_finding_categories`` may be issued only from a batch that is

* ``Customer Goods``,
* owned by the order's customer (``custom_customer == PMO.customer``, the existing lane), and
* received for the order's end customer (``custom_ref_customer == PMO.ref_customer``).

The manufacturer's "allow regular goods instead of customer goods" fallback does not apply to
such a row. Every path reads ``hybrid_requirement``:

* ``se_utils.get_fifo_batches`` -- the FIFO fill of the Department / Reserve entries a Material
  Request builds and of a Material Transfer (WORK ORDER) made on the desk. The From-Reserve and
  Transfer-to-MOP copies inherit the Reserve entry's batch and never re-pick;
* ``main_slip_inject._expand_source_rows_for_fifo`` -- the Employee IR receive leg;
* ``get_batch_no`` -- the batch pickers on the Stock Entry form;
* ``validate_hybrid_finding_batches`` -- the Stock Entry ``validate`` hook, which rejects a batch
  typed by hand or carried over from a Material Request item.

``snc._row_needs_settlement`` asks ``is_own_hybrid_finding`` so the customer's own findings held
by a Hybrid operation are not settled as borrowed gold.

**Fail-open contract.** An empty settings table, a non-Hybrid order, a Stock Entry Type outside
``HYBRID_FINDING_SE_TYPES`` or a site that has not migrated leaves every path exactly as it was.
Nothing here takes a lock.
"""

import frappe
from erpnext.controllers.queries import get_batch_no as erpnext_get_batch_no
from frappe import _

from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	get_customer_gold_settings,
	has_settings_capability,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events.finding_loss_gate import (
	FINDING_CATEGORY_ATTRIBUTE,
	FINDING_PREFIX,
)

HYBRID_SALES_TYPE = "Hybrid"
CUSTOMER_GOODS = "Customer Goods"
FINDING_SUB_CATEGORY_ATTRIBUTE = "Finding Sub-Category"
RULES_FIELD = "hybrid_finding_categories"
BATCH_REF_CUSTOMER_FIELD = "custom_ref_customer"
STOCK_ENTRY_REF_CUSTOMER_FIELD = "ref_customer"

#: The PMO issue chain -- the Stock Entry Types on which a batch is CHOSEN for an order's raw
#: material. "Material Transfer From Reserve" is a background copy of the Reserve entry and
#: never re-picks, so it is left out.
HYBRID_FINDING_SE_TYPES = (
	"Material Transfer (DEPARTMENT)",
	"Material transfer to Reserve",
	"Material Transfer (WORK ORDER)",
)

#: ``snc.create_material_transfer_work_order`` tags its own settlement transfers this way.
SNC_REQUEST_PREFIX = "SNC-"

#: Filter keys the Stock Entry form adds for ``get_batch_no``; never passed on to ERPNext.
PICKER_CONTEXT_KEYS = (
	"stock_entry_type",
	"parent_manufacturing_order",
	"manufacturing_order",
	"manufacturing_work_order",
)

_CACHE_FLAG = "hybrid_finding_cache"


def _get(obj, key):
	"""Read ``key`` off a Document, a dict or a plain namespace alike."""
	if obj is None:
		return None
	if isinstance(obj, dict) or hasattr(obj, "get"):
		return obj.get(key)
	return getattr(obj, key, None)


def _cache(doc):
	"""Per-document memo, held on ``doc.flags`` for the duration of the save."""
	flags = getattr(doc, "flags", None)
	if flags is None:
		return {}
	if isinstance(flags, dict):
		cache = flags.get(_CACHE_FLAG)
		if cache is None:
			cache = flags[_CACHE_FLAG] = {}
		return cache
	cache = getattr(flags, _CACHE_FLAG, None)
	if cache is None:
		cache = {}
		setattr(flags, _CACHE_FLAG, cache)
	return cache


def get_hybrid_finding_rules():
	"""``{finding_category: None | {sub_category, ...}}`` from Subcontracting Settings.

	``None`` means every sub-category of the category. A category listed both bare and with a
	sub-category resolves to ``None``: the bare row already covers the narrower one. Empty when
	the table is empty or the site has not migrated -- the fail-open case.
	"""
	if not has_settings_capability(RULES_FIELD):
		return {}

	rules = {}
	for row in get_customer_gold_settings().get(RULES_FIELD) or []:
		category = row.get("finding_category")
		if not category:
			continue
		sub_category = row.get("finding_sub_category")
		if not sub_category:
			rules[category] = None
		elif category not in rules:
			rules[category] = {sub_category}
		elif rules[category] is not None:
			rules[category].add(sub_category)
	return rules


def matches_rule(rules, category, sub_category):
	if not rules or not category or category not in rules:
		return False
	sub_categories = rules[category]
	return sub_categories is None or sub_category in sub_categories


def get_finding_attribute_map(item_codes):
	"""Bulk ``{item_code: (finding_category, finding_sub_category)}`` for finding items.

	One ``Item Variant Attribute`` query for both attributes, following
	``finding_loss_gate.get_finding_category_map``: non-finding codes are dropped up front.
	"""
	codes = sorted({c for c in (item_codes or []) if c and c[0] == FINDING_PREFIX})
	if not codes:
		return {}

	attributes = {}
	for row in frappe.get_all(
		"Item Variant Attribute",
		filters={
			"parent": ["in", codes],
			"attribute": [
				"in",
				[FINDING_CATEGORY_ATTRIBUTE, FINDING_SUB_CATEGORY_ATTRIBUTE],
			],
		},
		fields=["parent", "attribute", "attribute_value"],
		order_by=None,
	):
		category, sub_category = attributes.get(row.parent, (None, None))
		if row.attribute == FINDING_CATEGORY_ATTRIBUTE:
			category = row.attribute_value
		else:
			sub_category = row.attribute_value
		attributes[row.parent] = (category, sub_category)
	return attributes


def batch_has_ref_customer_field():
	"""Whether ``tabBatch`` carries the Ref Customer column yet (``add_ref_customer_fields`` ran).

	Asked of the column cache, not the meta: ``batch_rename`` runs under tests that stub
	``frappe.db.sql``, where a cold meta load would cache a broken Batch meta. A cold column
	cache under such a stub reads no columns and raises ``TableMissingError`` -- "not yet".
	"""
	try:
		return frappe.db.has_column("Batch", BATCH_REF_CUSTOMER_FIELD)
	except frappe.db.TableMissingError:
		return False


def get_batch_ownership_map(batch_nos):
	"""``{batch_no: _dict(custom_inventory_type, custom_customer, custom_ref_customer)}``."""
	names = sorted({b for b in (batch_nos or []) if b})
	if not names:
		return {}

	fields = ["name", "custom_inventory_type", "custom_customer"]
	if batch_has_ref_customer_field():
		fields.append(BATCH_REF_CUSTOMER_FIELD)
	return {
		row.name: row
		for row in frappe.get_all(
			"Batch", filters={"name": ["in", names]}, fields=fields, order_by=None
		)
	}


def get_batch_ref_customer_map(batch_nos):
	"""``{batch_no: custom_ref_customer}`` read through ``frappe.db.sql``.

	For ``batch_rename``, whose callers run inside the Stock Entry submit. Empty when the column
	has not been provisioned yet.
	"""
	names = sorted({b for b in (batch_nos or []) if b})
	if not names or not batch_has_ref_customer_field():
		return {}

	rows = frappe.db.sql(
		"SELECT name, custom_ref_customer FROM `tabBatch` WHERE name IN %(names)s",
		{"names": names},
	)
	return {row[0]: row[1] for row in rows or ()}


def _rules(doc):
	cache = _cache(doc)
	if "rules" not in cache:
		cache["rules"] = get_hybrid_finding_rules()
	return cache["rules"]


def _attributes(doc, item_code):
	cache = _cache(doc).setdefault("attributes", {})
	if item_code not in cache:
		cache[item_code] = get_finding_attribute_map([item_code]).get(
			item_code, (None, None)
		)
	return cache[item_code]


def _row_pmo_names(doc, row):
	"""The row's PMOs: its own (Small Text, may be comma-joined), else the header's, else the MWO's.

	The MWO fallback covers a Material Transfer (WORK ORDER) made on the desk, whose header PMO is
	filled by ``set_manufacturing_refs`` only after FIFO has run.
	"""
	value = _get(row, "custom_parent_manufacturing_order") or _get(
		doc, "manufacturing_order"
	)
	if not value:
		mwo = _get(row, "custom_manufacturing_work_order") or _get(
			doc, "manufacturing_work_order"
		)
		if mwo:
			mwo_pmo = _cache(doc).setdefault("mwo_pmo", {})
			if mwo not in mwo_pmo:
				mwo_pmo[mwo] = frappe.db.get_value(
					"Manufacturing Work Order", mwo, "manufacturing_order"
				)
			value = mwo_pmo[mwo]
	return [name.strip() for name in (value or "").split(",") if name.strip()]


def _pmo_rows(doc, pmo_names):
	cache = _cache(doc).setdefault("pmo", {})
	missing = [name for name in pmo_names if name not in cache]
	if missing:
		for row in frappe.get_all(
			"Parent Manufacturing Order",
			filters={"name": ["in", missing]},
			fields=["name", "sales_type", "customer", "ref_customer"],
			order_by=None,
		):
			cache[row.name] = row
		for name in missing:
			cache.setdefault(name, None)
	return [cache[name] for name in pmo_names if cache.get(name)]


def hybrid_requirement(doc, row):
	"""What a Hybrid order requires of ``row``'s batch, or ``None`` when the rule does not apply.

	Returns ``_dict(pmo, customer, ref_customer, problem, item_code, finding_category,
	finding_sub_category)``. ``ref_customer`` is ``None`` -- and ``problem`` says why -- when the
	order has no Ref Customer or the row mixes Hybrid orders of different Ref Customers; no batch
	is then eligible and the validator says so.

	Gates run cheapest first, so a non-finding row, an out-of-scope Stock Entry Type or an empty
	settings table costs no query.
	"""
	if _get(doc, "stock_entry_type") not in HYBRID_FINDING_SE_TYPES:
		return None

	item_code = _get(row, "item_code")
	if not item_code or item_code[0] != FINDING_PREFIX:
		return None

	rules = _rules(doc)
	if not rules:
		return None

	pmos = [
		pmo
		for pmo in _pmo_rows(doc, _row_pmo_names(doc, row))
		if pmo.sales_type == HYBRID_SALES_TYPE
	]
	if not pmos:
		return None

	category, sub_category = _attributes(doc, item_code)
	if not matches_rule(rules, category, sub_category):
		return None

	ref_customers = {pmo.ref_customer or None for pmo in pmos}
	customers = {pmo.customer or None for pmo in pmos}
	problem = None
	if len(ref_customers) > 1 or len(customers) > 1:
		problem = "conflict"
	elif None in ref_customers:
		problem = "no_ref_customer"

	return frappe._dict(
		pmo=", ".join(pmo.name for pmo in pmos),
		customer=pmos[0].customer if len(customers) == 1 else None,
		ref_customer=None if problem else pmos[0].ref_customer,
		problem=problem,
		item_code=item_code,
		finding_category=category,
		finding_sub_category=sub_category,
	)


def batch_is_eligible(batch_info, requirement):
	"""True when the batch is the order customer's Customer Goods, received for its Ref Customer."""
	info = batch_info or {}
	return bool(
		requirement
		and requirement.ref_customer
		and info.get("custom_inventory_type") == CUSTOMER_GOODS
		and info.get("custom_customer") == requirement.customer
		and info.get(BATCH_REF_CUSTOMER_FIELD) == requirement.ref_customer
	)


def _finding_label(requirement):
	if requirement.finding_sub_category:
		return f"{requirement.finding_category} / {requirement.finding_sub_category}"
	return requirement.finding_category


def shortfall_hint(requirement):
	"""One line appended to a FIFO "missing" message for a Hybrid row."""
	if requirement.problem == "conflict":
		return _(
			"This row serves Hybrid orders {0} that have different Ref Customers; split it per order."
		).format(frappe.bold(requirement.pmo))
	if requirement.problem == "no_ref_customer":
		return _(
			"Hybrid order {0} has no Ref Customer, so its {1} findings cannot be matched to a customer batch."
		).format(frappe.bold(requirement.pmo), frappe.bold(_finding_label(requirement)))
	return _(
		"Hybrid order {0}: only Customer Goods batches received for Ref Customer {1} can be used for "
		"{2} findings."
	).format(
		frappe.bold(requirement.pmo),
		frappe.bold(requirement.ref_customer),
		frappe.bold(_finding_label(requirement)),
	)


def throw_no_customer_batch(requirement, item_code, warehouse, need, available=0):
	frappe.throw(
		_(
			"Not enough {0} in {1}: need {2}, the order's customer batches hold {3}."
		).format(
			frappe.bold(item_code),
			frappe.bold(warehouse),
			frappe.format(need, {"fieldtype": "Float"}),
			frappe.format(available or 0, {"fieldtype": "Float"}),
		)
		+ "<br>"
		+ shortfall_hint(requirement),
		title=_("Customer Batch Required"),
	)


def _describe_batch(info):
	info = info or {}
	inventory_type = info.get("custom_inventory_type") or _("Regular Stock")
	if inventory_type != CUSTOMER_GOODS:
		return _("a {0} batch").format(inventory_type)
	ref_customer = info.get(BATCH_REF_CUSTOMER_FIELD)
	if not ref_customer:
		return _("a Customer Goods batch of {0} with no Ref Customer").format(
			info.get("custom_customer") or "-"
		)
	return _("a Customer Goods batch of {0} for Ref Customer {1}").format(
		info.get("custom_customer") or "-", ref_customer
	)


def _row_batches(row):
	"""The batches a source row draws: ``batch_no``, else the entries of its bundle."""
	if _get(row, "batch_no"):
		return [row.batch_no]
	bundle = _get(row, "serial_and_batch_bundle")
	if not bundle:
		return []
	return [
		batch
		for batch in frappe.get_all(
			"Serial and Batch Entry",
			filters={"parent": bundle},
			pluck="batch_no",
			order_by=None,
		)
		if batch
	]


def validate_hybrid_finding_batches(doc, method=None):
	"""Stock Entry ``validate``: a listed finding of a Hybrid order must draw its customer's batch.

	Runs after ``before_validate``, where ``update_batches`` has already FIFO-filled empty
	batches. Catches what FIFO never sees: a batch typed or picked by hand, one carried over from
	a Material Request item, and the auto-created copies. SNC's own settlement transfers are left
	to SNC. Read-only.
	"""
	if _get(doc, "stock_entry_type") not in HYBRID_FINDING_SE_TYPES:
		return
	if (_get(doc, "custom_request_id") or "").startswith(SNC_REQUEST_PREFIX):
		return
	if not _rules(doc):
		return

	checks = []
	for row in doc.get("items") or []:
		if not _get(row, "s_warehouse"):
			continue
		requirement = hybrid_requirement(doc, row)
		if requirement:
			checks.append((row, requirement, _row_batches(row)))
	if not checks:
		return

	batch_info = get_batch_ownership_map(
		[batch for _row, _requirement, batches in checks for batch in batches]
	)

	errors = []
	for row, requirement, batches in checks:
		label = _("Row #{0}: {1}").format(row.idx, frappe.bold(row.item_code))
		if requirement.problem:
			errors.append(f"{label} -- {shortfall_hint(requirement)}")
			continue
		if not batches:
			if frappe.get_cached_value("Item", row.item_code, "has_batch_no"):
				errors.append(
					_("{0} has no batch. {1}").format(
						label, shortfall_hint(requirement)
					)
				)
			continue
		for batch in batches:
			info = batch_info.get(batch)
			if not batch_is_eligible(info, requirement):
				errors.append(
					_("{0} draws batch {1}, which is {2}. {3}").format(
						label,
						frappe.bold(batch),
						_describe_batch(info),
						shortfall_hint(requirement),
					)
				)

	if errors:
		frappe.throw(
			"<br>".join(errors)
			+ "<br><br>"
			+ _(
				"Receive the customer's findings with a <b>Customer Goods Received</b> entry that names "
				"the Ref Customer, then pick that batch."
			),
			title=_("Customer Batch Required"),
		)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_batch_no(doctype, txt, searchfield, start, page_len, filters):
	"""ERPNext's batch query, narrowed to the customer's batches for a listed Hybrid finding.

	The Stock Entry form adds ``PICKER_CONTEXT_KEYS`` to the filters; anything else (another
	doctype, a non-Hybrid order, an unlisted finding) gets ERPNext's result unchanged.
	"""
	filters = frappe._dict(filters or {})
	context = {key: filters.pop(key, None) for key in PICKER_CONTEXT_KEYS}
	batches = erpnext_get_batch_no(doctype, txt, searchfield, start, page_len, filters)

	requirement = hybrid_requirement(
		frappe._dict(
			stock_entry_type=context["stock_entry_type"],
			manufacturing_order=context["manufacturing_order"],
			manufacturing_work_order=context["manufacturing_work_order"],
			flags=frappe._dict(),
		),
		frappe._dict(
			item_code=filters.get("item_code"),
			custom_parent_manufacturing_order=context["parent_manufacturing_order"],
		),
	)
	if not requirement:
		return batches

	batch_info = get_batch_ownership_map([batch[0] for batch in batches])
	return [
		batch
		for batch in batches
		if batch_is_eligible(batch_info.get(batch[0]), requirement)
	]


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_finding_sub_categories(doctype, txt, searchfield, start, page_len, filters):
	"""Link query for ``Hybrid Finding Category.finding_sub_category``.

	The sibling of ``department_operation.get_finding_categories``: sourced from the Item
	Attribute, which holds the strings finding items actually carry.
	"""
	IAV = frappe.qb.DocType("Item Attribute Value")
	return (
		frappe.qb.from_(IAV)
		.select(IAV.attribute_value)
		.distinct()
		.where(
			(IAV.parent == FINDING_SUB_CATEGORY_ATTRIBUTE)
			& (IAV.attribute_value.like(f"%{txt}%"))
		)
		.orderby(IAV.attribute_value)
		.limit(page_len)
		.offset(start)
	).run()


def validate_hybrid_finding_categories(settings):
	"""Subcontracting Settings: keep the Hybrid table resolvable.

	Same two failure modes ``department_operation.validate_finding_loss_booking`` guards: a
	duplicate row, and a value no finding item can ever carry (anything outside the Finding
	Category / Finding Sub-Category Item Attributes), which would make the row a silent no-op.
	"""
	rows = settings.get(RULES_FIELD) or []
	if not rows:
		return

	seen = {}
	for row in rows:
		key = (row.finding_category, row.finding_sub_category or None)
		if key in seen:
			frappe.throw(
				_("Row #{0}: {1} is already listed in row #{2} of {3}.").format(
					row.idx,
					frappe.bold(" / ".join(filter(None, key))),
					seen[key],
					_("Customer Batch Only Finding Categories"),
				)
			)
		seen[key] = row.idx

	for attribute, fieldname in (
		(FINDING_CATEGORY_ATTRIBUTE, "finding_category"),
		(FINDING_SUB_CATEGORY_ATTRIBUTE, "finding_sub_category"),
	):
		values = {row.get(fieldname): row.idx for row in rows if row.get(fieldname)}
		if not values:
			continue
		valid = set(
			frappe.get_all(
				"Item Attribute Value",
				filters={"parent": attribute, "attribute_value": ["in", list(values)]},
				pluck="attribute_value",
			)
		)
		unknown = [(value, idx) for value, idx in values.items() if value not in valid]
		if unknown:
			frappe.throw(
				_(
					"Row #{0}: <b>{1}</b> is not a value of the <b>{2}</b> Item Attribute, so no "
					"finding item can ever match it."
				).format(unknown[0][1], unknown[0][0], attribute)
			)


def hybrid_settlement_context(pmo_name, item_codes):
	"""What ``is_own_hybrid_finding`` needs for one MWO, or ``None`` when no row can qualify.

	No query at all unless a finding is among ``item_codes``.
	"""
	codes = [code for code in item_codes or () if code and code[0] == FINDING_PREFIX]
	if not pmo_name or not codes:
		return None
	rules = get_hybrid_finding_rules()
	if not rules:
		return None

	pmo = frappe.db.get_value(
		"Parent Manufacturing Order",
		pmo_name,
		["name", "sales_type", "customer", "ref_customer"],
		as_dict=True,
	)
	if not pmo or pmo.sales_type != HYBRID_SALES_TYPE or not pmo.ref_customer:
		return None

	listed = {
		item_code
		for item_code, (category, sub_category) in get_finding_attribute_map(
			codes
		).items()
		if matches_rule(rules, category, sub_category)
	}
	if not listed:
		return None

	# Not ``items``: on a _dict that name is the dict method, so ``context.items`` never
	# reaches the key.
	return frappe._dict(
		pmo=pmo.name,
		customer=pmo.customer,
		ref_customer=pmo.ref_customer,
		item_codes=listed,
	)


def is_own_hybrid_finding(context, row):
	"""True when an operation's held row is the Hybrid order's own customer finding.

	``row`` is a ``snc._get_receivable_gold_rows`` row: ownership read off the batch master.
	"""
	if not context or _get(row, "item_code") not in context.item_codes:
		return False
	return batch_is_eligible(
		{
			"custom_inventory_type": _get(row, "inventory_type"),
			"custom_customer": _get(row, "batch_customer"),
			BATCH_REF_CUSTOMER_FIELD: _get(row, "batch_ref_customer"),
		},
		context,
	)
