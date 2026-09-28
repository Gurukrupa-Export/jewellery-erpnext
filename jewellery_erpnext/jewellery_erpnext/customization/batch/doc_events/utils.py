import frappe
from frappe import _
from frappe.utils import cstr, flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.party_link import (
	get_linked_customer,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.sample_goods import (
	SAMPLE_VOUCHER_TYPE,
)

# The complete option space of ``Batch.custom_customer_voucher_type`` -- kept in step with
# ``custom_fields/batch.json`` and with the identically-optioned
# ``Stock Entry.customer_voucher_type``.
CUSTOMER_VOUCHER_TYPES = (
	SAMPLE_VOUCHER_TYPE,
	"Customer Subcontracting",
	"Customer Repair",
)


def _valid_voucher_type(value):
	"""``value`` if it is a legal Customer Voucher Type, else None.

	The voucher type is copied onto the Batch from data this module does not own: the
	Stock Entry header, an upstream Batch, a Purchase Receipt's supplier. Those sources
	are not guaranteed to hold one of the three options -- the SE field is skipped by
	frappe's ``_validate_selects`` on any site where its ``options`` were cleared (the
	mechanism documented in ``patches/clear_metal_conversion_remarks_options``), a bad
	Customize Form ``default`` seeds every new document, and a raw ``db.set_value`` or SQL
	backfill bypasses validation entirely. A real site was carrying the literal two-character
	value ``''`` this way.

	The Batch field IS option-checked, so copying junk across does not fail where it is
	introduced -- it fails later, on the next save of the batch, which
	``serial_and_batch_bundle.update_parent_batch_id`` performs on every Manufacture /
	Repack submit. That is why one bad row surfaced as
	``Customer Voucher Type cannot be "''"`` aborting Serial Number Creator and Metal
	Conversion submits that had nothing to do with it.

	Dropping to None rather than throwing is deliberate: an unrecognised voucher type means
	"this batch is not marked", which is the same state as a batch that was never stamped,
	and every consumer (``is_customer_sample_batch``, ``is_repair_unpack``, the Customer
	Goods guards) already treats an unset marker as "no special handling". Throwing instead
	would turn stale data into the very submit failure this prevents.
	"""
	value = cstr(value).strip()
	return value if value in CUSTOMER_VOUCHER_TYPES else None


def update_inventory_dimentions(self):
	item_groups = frappe.db.get_all(
		"Item Group", {"custom_is_alloy_group": 1}, pluck="name"
	)
	alloy_item_list = frappe.db.get_all(
		"Item",
		{"item_group": ["in", item_groups], "variant_of": ["in", ["M", "F"]]},
		pluck="name",
	)
	for row in frappe.db.get_all(
		"DocField",
		{"parent": self.reference_doctype, "fieldtype": "Table"},
		["options"],
	):
		if frappe.db.exists(row.options, self.custom_voucher_detail_no):
			self.custom_inventory_type = _row_value(
				row.options, self.custom_voucher_detail_no, "inventory_type"
			)
			self.custom_customer = _row_value(
				row.options, self.custom_voucher_detail_no, "customer"
			)
			# Stamp the responsible employee so scrap/dust can be fetched employee-wise
			# in refining (Batch.custom_employee). Prefer the source row's own employee
			# (Employee Loss Entry sets it per produce row); fall back to the Stock Entry
			# header employee (Employee IR Process Loss sets it only on the header).
			# Copied the same way custom_customer/custom_inventory_type are.
			emp = _row_value(row.options, self.custom_voucher_detail_no, "employee")
			if not emp and self.reference_doctype == "Stock Entry":
				emp = frappe.db.get_value(
					"Stock Entry", self.reference_name, "employee"
				)
			if emp:
				self.custom_employee = emp
			# Batch Rate is stamped from the row that created the batch: Purchase
			# Receipt Item.rate, or the Stock Entry Detail's own maintained rate
			# falling back to valuation_rate, then basic_rate. Stamped only while
			# the field is still empty (see _can_stamp_rate): this minting stamp is
			# the Batch Rate for the life of the batch (F26).
			#
			# Every item gets a rate, not just metal. The split is only alloy vs
			# everything else: an alloy batch's value belongs on custom_alloy_rate.
			# Diamond, gemstone, finding and consumable batches were previously
			# left at 0 -- see _rate_field_for_item.
			rate_field = _rate_field_for_item(self.item, alloy_item_list)
			if _can_stamp_rate(self, rate_field):
				setattr(
					self, rate_field, _source_row_rate(self, row.options, rate_field)
				)
			break

	item_allows_customer_goods = frappe.db.get_value(
		"Item", self.item, "custom_inventory_type_can_be_customer_goods"
	)
	is_customer_inventory = self.custom_inventory_type in [
		"Customer Goods",
		"Customer Stock",
	]

	if (
		not item_allows_customer_goods
		and is_customer_inventory
		and not is_subcontracting_gold_repack(self)
		and not is_process_loss_repack(self)
		and not is_repair_unpack(self)
	):
		frappe.throw(
			_(
				"Item {0} is not allowed as {1} (customer: {2}, batch: {3}). Tick "
				"'Inventory Type Can be Customer Goods' on the Item, or book it as "
				"Regular Stock."
			).format(
				frappe.bold(self.item),
				self.custom_inventory_type,
				self.custom_customer or "-",
				frappe.bold(self.name or self.get("batch_id") or _("new")),
			)
		)

	# Drop a stored value that is not one of the three options before either leg
	# runs. The field is a Select, so an illegal value already on the row makes
	# frappe's own ``_validate_selects`` throw on the NEXT save of this batch -- and
	# that save is not the user's: ``serial_and_batch_bundle.update_parent_batch_id``
	# re-saves the produced batch on every Manufacture/Repack submit just to append
	# provenance rows, so one poisoned row aborts an unrelated Serial Number Creator
	# or Metal Conversion submit with "Customer Voucher Type cannot be ...".
	#
	# ``getattr`` and the emptiness test keep this a no-op on a site where the custom
	# field was never patched on (the gap documented in ``_row_value``) and on a batch
	# that simply has no voucher type.
	stored_voucher_type = getattr(self, "custom_customer_voucher_type", None)
	if stored_voucher_type and not _valid_voucher_type(stored_voucher_type):
		self.custom_customer_voucher_type = None

	if self.reference_doctype == "Stock Entry" and self.custom_customer:
		self.custom_customer_voucher_type = _valid_voucher_type(
			frappe.db.get_value(
				"Stock Entry", self.reference_name, "customer_voucher_type"
			)
		) or _source_batch_voucher_type(self)
	elif self.reference_doctype == "Purchase Receipt" and self.custom_customer:
		self.custom_customer_voucher_type = (
			_purchase_receipt_voucher_type(self) or self.custom_customer_voucher_type
		)


def _row_value(child_doctype, row_name, fieldname):
	"""Read one field off the voucher row, tolerating a column that is not there.

	The child tables that mint batches do not all carry the same custom fields --
	``employee`` exists on Stock Entry Detail but NOT on Purchase Receipt Item --
	and this app's ``custom_fields/*.json`` are not applied by migrate (the
	patch-only custom-field gap documented in ``fetch_from_guard``), so a column
	can be absent on a real site even where the JSON declares it. An unguarded
	read raises MariaDB 1054 "Unknown column" from inside ``Batch.validate``,
	which aborts the entire submit of the voucher creating the batch -- that is
	what made every batch-tracked Purchase Receipt fail on submit.

	``frappe.db.has_column`` is the right probe rather than ``meta.has_field``:
	the failure is a *schema* one, and a field declared in JSON but never patched
	onto the site is exactly the case that must be treated as missing.
	"""
	if not frappe.db.has_column(child_doctype, fieldname):
		return None

	return frappe.db.get_value(child_doctype, row_name, fieldname)


RATE_FIELDS = ("custom_metal_rate", "custom_alloy_rate")


def carry_rates_from_source_batches(batch, sources):
	"""Copy the Batch Rate / Alloy Rate pools onto a HAND-BUILT batch from its source batches.

	``sources`` is ``[(batch_no, qty)]`` -- the batches consumed to make this one, with the
	quantity taken from each. Both pools are carried, qty-weighted across the sources.

	**Why this exists at all.** ``update_inventory_dimentions`` stamps a new batch's rate from
	the voucher row that minted it, but it can only do so inside
	``if frappe.db.exists(row.options, self.custom_voucher_detail_no)``. A batch built by hand
	BEFORE its Stock Entry exists -- which is what ``finding_repack._create_finding_batch`` and
	``manufacturing_operation._create_scrap_batch`` both do, so the batch id is known in time to
	put on the produce row -- has no ``custom_voucher_detail_no`` to resolve, so that stamper can
	never fire for it. Every batch a plain ``Repack`` has ever minted sits at rate 0 for this
	reason. ``customer_subcontracting/batch_rename`` hit the same wall from the other direction
	(it bypasses the stamper via ``frappe.flags.is_batch_autoname``) and solved it the same way:
	assign the rate directly.

	Carrying BOTH pools unchanged is correct because neither of these repacks changes purity --
	the same reasoning ``_convert_received_scrap_to_scrap_batch`` already states for its own
	rate. Note that ``_rate_field_for_item`` choosing a single field is about which pool a
	*minting row's* rate lands in, not a claim that a batch holds only one: a metal batch
	minted by a Repack-Metal Conversion legitimately carries both.

	Sources with no rate contribute 0 rather than being dropped, so a partly-unvalued input
	dilutes the result honestly instead of inventing value -- the same "no fallback to the Bin
	or Item rate" policy ``loss_valuation`` states.
	"""
	usable = [(b, flt(q)) for b, q in (sources or []) if b]
	if not usable:
		return batch

	rates = {}
	for batch_no, _qty in usable:
		rates[batch_no] = (
			frappe.db.get_value("Batch", batch_no, RATE_FIELDS, as_dict=True) or {}
		)

	total_qty = sum(qty for _b, qty in usable)
	for fieldname in RATE_FIELDS:
		if not _can_stamp_rate(batch, fieldname):
			continue
		if total_qty:
			value = (
				sum(
					flt(rates[batch_no].get(fieldname)) * qty
					for batch_no, qty in usable
				)
				/ total_qty
			)
		else:
			# Every source came through at qty 0 (nothing was really consumed); fall back to a
			# plain mean so a rate is still carried rather than divided by zero.
			value = sum(flt(r.get(fieldname)) for r in rates.values()) / len(rates)
		setattr(batch, fieldname, value)

	return batch


def _can_stamp_rate(batch, fieldname):
	"""Whether the Batch Rate / Alloy Rate may still be written.

	The requirement is that *newly created* batches carry the rate of the voucher
	row that made them, and keep it: that minting stamp is the Batch Rate (F26).
	``serial_and_batch_bundle.update_parent_batch_id`` re-saves a produced batch on
	every Manufacture / Repack submit to record provenance, so re-stamping on every
	save would restate the rate from whatever the row holds by then.

	An empty field is always fillable (a batch that never got a rate should still
	get one); a rate that is already set is only rewritten while the batch is new.
	``getattr`` guards the ``is_new`` lookup because the doc-event tests drive this
	function with ``SimpleNamespace`` stand-ins rather than real Documents.
	"""
	if not flt(getattr(batch, fieldname, 0)):
		return True

	is_new = getattr(batch, "is_new", None)
	return bool(is_new()) if callable(is_new) else True


def _source_row_rate(batch, child_doctype, se_fieldname):
	"""The rate carried by the voucher row that created this batch.

	A Purchase Receipt Item (and any other non-Stock-Entry voucher row) carries a
	single ``rate``. A Stock Entry Detail carries its own maintained Batch/Alloy
	Rate -- fetched from the *consumed* batch, so it is empty on the produce row
	that mints a new batch unless a rate typed on a zero-valued customer row was
	parked there (``entered_metal_rate``) -- and falls back to ``valuation_rate``,
	then ``basic_rate``.

	``valuation_rate`` first because it is what the ledger books: the Stock Entry's
	SLE ``incoming_rate`` for a target row is ``valuation_rate``, which is
	``basic_rate`` plus the row's share of additional costs. With batch-wise
	valuation that is the rate every later issue of the batch is charged, so the
	Batch Rate matches it (F26). ``basic_rate`` stays as the fallback for a row
	whose valuation_rate is not set.
	"""
	if batch.reference_doctype != "Stock Entry":
		return _row_value(child_doctype, batch.custom_voucher_detail_no, "rate")

	for fieldname in (se_fieldname, "valuation_rate", "basic_rate"):
		rate = _row_value(child_doctype, batch.custom_voucher_detail_no, fieldname)
		if flt(rate):
			return rate
	return rate


def _rate_field_for_item(item_code, alloy_item_list):
	"""Which Batch field the minting row's rate belongs on.

	Alloy is the only special case: an alloy batch's value lands on
	``custom_alloy_rate``, the pool ``carry_rates_from_source_batches`` carries
	separately, not on ``custom_metal_rate``.

	Everything else -- metal, diamond, gemstone, finding, consumables -- takes
	``custom_metal_rate``. This used to be narrowed to items carrying a
	``Metal Type`` attribute whose Attribute Value has ``is_metal_type``, behind
	a site_config opt-in for the rest, which left every diamond and gemstone
	batch at 0 (a Material Receipt of ``D-NT-RO-6B-+00-0`` at basic_rate 346
	minted a batch with Batch Rate 0).

	Widening it is not neutral and is intended: ``Stock Entry Detail
	.custom_metal_rate`` is ``fetch_from batch_no.custom_metal_rate``, and the
	SNC / FG-BOM rate maps (``manufacturing_operation._snc_se_detail_maps``) read
	``COALESCE(NULLIF(custom_metal_rate, 0), basic_rate)``. Those previously fell
	through to the live ``basic_rate`` for diamond and gemstone and now use the
	batch's own rate instead -- which is the consistent answer for material that
	is on ``use_batchwise_valuation``.
	"""
	return "custom_alloy_rate" if item_code in alloy_item_list else "custom_metal_rate"


def _purchase_receipt_voucher_type(batch):
	"""``Customer Subcontracting`` for goods received against a customer's supplier.

	A Purchase Receipt whose supplier is ticked
	``custom_consider_purchase_receipt_as_customergoods`` books its rows as Customer
	Goods owned by the Customer linked to that Supplier -- ``primary_party`` of the
	``Party Link`` whose ``secondary_party`` is the supplier (see
	``purchase_receipt/doc_events/utils.py::update_inventory_type``, which stamps
	``row.customer`` the same way). The batch minted from such a receipt is that
	customer's subcontracting stock, so it must carry the voucher type -- the
	Stock Entry leg above never fires for a Purchase Receipt reference, which is
	why these batches previously had a customer but no voucher type.

	The Party Link customer is re-resolved and compared against the customer already
	on the batch: a batch whose ownership came from somewhere else must not be
	relabelled as this customer's subcontracting stock.
	"""
	supplier = frappe.db.get_value("Purchase Receipt", batch.reference_name, "supplier")
	if not supplier:
		return None

	if not frappe.db.get_value(
		"Supplier", supplier, "custom_consider_purchase_receipt_as_customergoods"
	):
		return None

	primary_customer = get_linked_customer(supplier)
	if not primary_customer or primary_customer != batch.custom_customer:
		return None

	return "Customer Subcontracting"


def _source_batch_voucher_type(batch):
	"""The voucher type of the customer's batch this one was made from.

	``customer_voucher_type`` only lives on the SE that first *received* the goods; a derived entry
	(a Process Loss repack, a conversion) leaves the header blank, so reading it alone stamps the
	new batch with an empty voucher type and the material stops looking like the Customer
	Subcontracting / Sample / Repair stock it still is. Fall back to the batch this one was
	physically made from -- the consumed row of the same Stock Entry, matched on the customer we
	already resolved.

	The SE header is deliberately not written instead (as the Employee IR loss engine does at
	loss_stock_entry.py): setting it trips ``validate_customer_voucher``, which throws for batch
	items under "Customer Repair" -- the same trap documented at manufacturing_work_order.py's
	unpack flow.
	"""
	source_batches = frappe.db.get_all(
		"Stock Entry Detail",
		filters={
			"parent": batch.reference_name,
			"customer": batch.custom_customer,
			"s_warehouse": ["is", "set"],
			"batch_no": ["is", "set"],
		},
		pluck="batch_no",
	)
	for batch_no in source_batches:
		if not batch_no or batch_no == batch.name:
			continue
		# Validated, not just truthy: a source batch carrying junk must be SKIPPED so a
		# later legitimate source can still answer, rather than passing the junk on and
		# poisoning every batch downstream of it.
		voucher_type = _valid_voucher_type(
			frappe.db.get_value("Batch", batch_no, "custom_customer_voucher_type")
		)
		if voucher_type:
			return voucher_type
	return None


def is_subcontracting_gold_repack(batch):
	if getattr(batch, "reference_doctype", None) != "Stock Entry":
		return False

	if not getattr(batch, "custom_customer", None):
		return False

	item_code = getattr(batch, "item", None)
	if not isinstance(item_code, str) or not item_code.startswith("M-G-"):
		return False

	return (
		frappe.db.get_value(
			"Stock Entry", getattr(batch, "reference_name", None), "stock_entry_type"
		)
		== "Subcontracting Repack"
	)


def is_process_loss_repack(batch):
	"""Exempt Employee IR Process Loss scrap/loss batches from the Customer Goods
	guard.

	The Process Loss Stock Entry (Employee IR receive) moves a customer's metal
	that has been booked as loss into a scrap/loss *variant* item, and that
	material stays the customer's -- so the produce row is stamped
	inventory_type = "Customer Goods" (see
	employee_ir/doc_events/loss_stock_entry.py::_resolve_batch_inventory). The
	loss variant item is not necessarily flagged
	``custom_inventory_type_can_be_customer_goods``, so without this exemption the
	batch-creation guard above would throw "This item is not allowed as Customer
	Goods" and block the whole EIR receive submit. Mirror
	``is_subcontracting_gold_repack``: only exempt batches minted by a
	Process Loss Stock Entry that actually carries a customer.
	"""
	if getattr(batch, "reference_doctype", None) != "Stock Entry":
		return False

	if not getattr(batch, "custom_customer", None):
		return False

	return (
		frappe.db.get_value(
			"Stock Entry", getattr(batch, "reference_name", None), "stock_entry_type"
		)
		== "Process Loss"
	)


def is_repair_unpack(batch):
	"""Exempt Repair-Unpack Customer Goods batches from the item-flag guard.

	``create_unpack_serial_no_stock_entry`` (manufacturing_work_order.py) disassembles a
	customer's repair article into its FULL design-BOM composition and books every component
	as the customer's stock. The article was physically brought in by the customer, so each
	component IS theirs -- ownership comes from the repair, not from a
	``custom_inventory_type_can_be_customer_goods`` flag on each component's Item master. That
	flag is sparsely maintained (no finding variant carries it), so without this exemption an
	unpack throws "Item ... is not allowed as Customer Goods" on the first unflagged diamond or
	finding and the whole submit fails. Same rationale as the loss variant in
	``is_process_loss_repack``.

	Unlike its two sibling helpers, this one CANNOT key on ``reference_doctype`` alone: the
	unpack mints each component's Batch *standalone, before the Stock Entry exists*
	(``batch_doc.save()`` runs before the SE is built), so at the only moment the guard fires
	``reference_doctype`` is still None. It is instead recognised by the ``Customer Repair``
	voucher type stamped on the batch just before that save -- a marker no other flow writes.
	The reference-based leg (mirroring the two siblings) is kept as well, so the exemption also
	holds if the batch is re-validated after the SE links it. Both legs require a customer:
	a Customer Goods batch with no customer is malformed and must not be silently exempted (see
	``normalize_ownership`` rule 3 in customization/utils/row_ownership.py).
	"""
	if not getattr(batch, "custom_customer", None):
		return False

	if getattr(batch, "custom_customer_voucher_type", None) == "Customer Repair":
		return True

	if getattr(batch, "reference_doctype", None) != "Stock Entry":
		return False

	return (
		frappe.db.get_value(
			"Stock Entry", getattr(batch, "reference_name", None), "stock_entry_type"
		)
		== "Repair Unpack"
	)


def update_pure_qty(self):
	if not self.batch_qty:
		return

	variant_of = frappe.db.get_value("Item", self.item, "variant_of")

	if variant_of not in ["M", "F"]:
		return

	if not self.reference_doctype:
		return

	# company = frappe.db.get_value(self.reference_doctype, self.reference_name, "company")

	# pure_item = frappe.db.get_value("Manufacturing Setting", company, "pure_gold_item")

	# Only the manufacturing documents (Manufacturing Work Order / Operation, Refining
	# Entry, ...) carry a `manufacturer`; a Batch may just as well reference a Stock Entry,
	# Purchase Receipt or Sales Invoice, none of which have the column -- reading it blindly
	# raises OperationalError "Unknown column 'manufacturer'" and aborts the whole save.
	# That stayed hidden while every such batch was saved only at creation time (batch_qty
	# is 0 then, so this returns above); it fires as soon as a batch that already holds
	# stock is re-saved, e.g. the origin-entries update in
	# customization/serial_and_batch_bundle/doc_events/utils.py::update_parent_batch_id when
	# an inward bundle lands in an EXISTING batch.
	if not frappe.get_meta(self.reference_doctype).has_field("manufacturer"):
		return

	manufacturer = frappe.db.get_value(
		self.reference_doctype, self.reference_name, "manufacturer"
	)

	pure_item = frappe.db.get_value(
		"Manufacturing Setting", {"manufacturer": manufacturer}, "pure_gold_item"
	)

	if not pure_item:
		return

	batch_item_purity = get_purity_percentage(self.item)
	pure_item_purity = get_purity_percentage(pure_item)

	if not batch_item_purity:
		return

	self.custom_pure_metal_qty = flt(
		(batch_item_purity * self.batch_qty) / pure_item_purity, 3
	)
