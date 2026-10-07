from datetime import datetime

import frappe
from frappe import _
from frappe.utils import flt

from jewellery_erpnext.customer_subcontracting.customer_goods_eligibility import (
	get_customer_goods_eligible_items,
)
from jewellery_erpnext.customer_subcontracting.doctype.subcontracting_settings.subcontracting_settings import (
	get_customer_gold_receipt_type,
)
from jewellery_erpnext.customer_subcontracting.hybrid_findings import (
	get_batch_ref_customer_map,
)
from jewellery_erpnext.customer_subcontracting.report.subcontracting_report.subcontracting_report import (
	execute as get_report_data,
)
from jewellery_erpnext.customer_subcontracting.report.subcontracting_report.subcontracting_report import (
	get_linked_batches,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.loss_valuation import (
	iter_loss_runs,
)
from jewellery_erpnext.jewellery_erpnext.customization.utils.row_ownership import (
	CUSTOMER_INVENTORY_TYPES,
	PROCESS_LOSS_SE_TYPE,
)

#: Stock Entry Types that have always minted customer parent batches. Kept as a literal
#: list because they are not interchangeable with the configured receipt type:
#: "Subcontracting Repack" has purpose `Repack`, while the settings validator requires the
#: configured type to have purpose `Material Receipt`
#: (``subcontracting_settings.RECEIPT_PURPOSE``). It can therefore never BE the configured
#: type, and replacing this list with the configured one alone would silently kill the
#: Subcontracting Repack leg.
_LEGACY_PARENT_BATCH_TYPES = (
	"Customer Goods Received",
	"Subcontracting Repack",
)


def _is_eligible_item(item_code, flagged_items):
	"""Whether this row's item should be minted a customer parent batch.

	Two independent tests, either of which qualifies:

	* the Item master allows Customer Goods -- ``flagged_items``, the rows' items whose
	  ``custom_inventory_type_can_be_customer_goods`` flag is on. This is the eligibility rule,
	  the same one the Customer Gold receipt enforces; it replaced the Subcontracting Settings
	  item list, and like that list it is consulted only while the Customer Gold flow is on; and
	* it carries the ``24KT`` token -- the historical minting rule, retained so every legacy
	  path (and every site with the flow off) mints exactly as before.

	An item whose code lacks the token -- a 22KT purity, a finding, a diamond -- therefore gets
	a custody identity only when its own flag says it may be customer goods.
	"""
	if item_code in flagged_items:
		return True
	return "24KT" in item_code


def create_parent_batches(doc, method=None):
	# ``None`` when the Customer Gold flow is off, which leaves the legacy types and the
	# ``24KT`` token exactly as they were.
	configured_type = get_customer_gold_receipt_type()

	if doc.doctype == "Stock Entry":
		accepted = _LEGACY_PARENT_BATCH_TYPES + (
			(configured_type,) if configured_type else ()
		)
		if getattr(doc, "stock_entry_type", None) not in accepted:
			return

	elif doc.doctype == "Purchase Receipt":
		if getattr(doc, "purchase_type", None) != "Subcontracting":
			return

	else:
		return

	# One query for the document, and only while the flow is on -- the scope the Settings list
	# had. With the flow off the token alone decides, as it always did. Batch controlled only:
	# the old list was, because Settings validation demanded it, and minting a batch for a
	# flagged Nos item would fail the submit rather than skip the row.
	flagged_items = (
		get_customer_goods_eligible_items(
			(row.item_code for row in doc.items), batch_controlled=True
		)
		if configured_type
		else set()
	)

	for row in doc.items:
		if not row.item_code:
			continue

		if not _is_eligible_item(row.item_code, flagged_items):
			continue

		if row.batch_no:
			continue

		customer = getattr(row, "customer", None) or getattr(doc, "_customer", None)

		if not customer:
			continue

		year_code = get_year_code()
		month = datetime.today().strftime("%m")

		item_code = row.item_code

		serial = get_next_serial(customer, year_code, month)

		batch_name = f"{customer}-{year_code}{month}-{item_code}-{serial}"

		while frappe.db.exists("Batch", batch_name):
			serial = str(int(serial) + 1).zfill(2)
			batch_name = f"{customer}-{year_code}{month}-{item_code}-{serial}"

		previous_autoname_flag = frappe.flags.is_batch_autoname
		frappe.flags.is_batch_autoname = True

		try:
			batch = frappe.new_doc("Batch")
			batch.batch_id = batch_name
			batch.item = item_code
			batch.reference_doctype = doc.doctype
			batch.reference_name = doc.name
			batch.custom_voucher_detail_no = row.name
			# doc._customer is set by the customer-subcontracting orchestration on the
			# Stock Entry leg only; on a Purchase Receipt the owning customer arrives on
			# the row, resolved from the supplier's Party Link by
			# purchase_receipt/doc_events/utils.py::update_inventory_type.
			# getattr, not bare attribute access: ``_customer`` is a Custom Field on
			# Stock Entry only. Purchase Receipt has no such field, so the bare form
			# raised AttributeError on the Purchase Receipt leg (frappe's Document
			# defines no __getattr__ fallback). That crash is pre-existing for any
			# ``24KT`` item on a Subcontracting receipt; configuring a customer item
			# whose code lacks the token newly routes THOSE rows here too, turning a
			# row that used to be skipped into a hard submit failure.
			# Precedence is deliberately unchanged -- header first, then the row value
			# already resolved above -- because which customer owns a batch on a mixed
			# voucher is an ownership decision, not a crash fix. See HANDOFF.md C7.
			batch.custom_customer = getattr(doc, "_customer", None) or customer
			batch.custom_inventory_type = "Customer Goods"
			batch.custom_customer_voucher_type = "Customer Subcontracting"
			# The end customer the goods were received for (hybrid_findings). Header only, and
			# getattr for the same reason as ``_customer``: Purchase Receipt has no such field.
			batch.custom_ref_customer = getattr(doc, "ref_customer", None)
			batch.custom_metal_rate = _source_row_rate(doc, row)
			batch.insert(ignore_permissions=True)
		finally:
			frappe.flags.is_batch_autoname = previous_autoname_flag

		row.batch_no = batch_name


def _source_row_rate(doc, row):
	"""Batch Rate for a batch this module builds by hand.

	These batches are inserted under ``frappe.flags.is_batch_autoname``, which makes
	``Batch.validate`` return before ``update_inventory_dimentions`` -- so the shared
	rate stamping in ``customization/batch/doc_events/utils.py`` never runs for them
	and they were created with no Batch Rate at all. Read it straight off the row
	that is minting the batch instead: the Stock Entry Detail's maintained rate
	falling back to ``valuation_rate`` and then ``basic_rate``, or the Purchase
	Receipt Item's ``rate``. ``valuation_rate`` is the ledger's incoming rate for the
	row (``basic_rate`` plus its share of additional costs), and that minting stamp
	stays the batch's rate: nothing restates it later (F26).

	A Manufacture's finished piece gets none (F8). ``create_child_batches`` also mints the piece's
	batch on a customer order, and that row's rate is the whole piece -- customer gold, company
	alloy and diamond, production cost. Stamped as a Batch Rate it read Rs.8,01,654.99 on
	KLHGX62F1119's batch, and anything that trusts Batch Rate over ``basic_rate`` took the company
	diamond as metal. Every other row keeps its rate, stones included -- the app deliberately
	gives diamond and gemstone batches a Batch Rate (``_rate_field_for_item``).
	"""
	if doc.doctype == "Stock Entry":
		if getattr(doc, "purpose", None) == "Manufacture" and row.get(
			"is_finished_item"
		):
			return 0.0
		return (
			flt(row.get("custom_metal_rate"))
			or flt(row.get("valuation_rate"))
			or flt(row.get("basic_rate"))
		)

	return flt(row.get("rate"))


def get_year_code():
	year_dict = {
		"1": "A",
		"2": "B",
		"3": "C",
		"4": "D",
		"5": "E",
		"6": "F",
		"7": "G",
		"8": "H",
		"9": "I",
		"0": "J",
	}

	year = datetime.today().year
	last_two = str(year)[-2:]

	return last_two[0] + year_dict[last_two[1]]


def get_next_serial(customer, year_code, month):
	"""Next receipt serial for ``customer`` in this year-month, across every item.

	Serials are per customer and month. The old query took the lexically greatest name,
	which became a child batch (``...-Y-A-Z``) as soon as children existed, so each item
	restarted at ``01`` and receipts of different items shared a serial
	(``...-M-G-22KT-91.75-Y-04`` and ``...-M-G-24KT-99.9-Y-04``) -- and with it a child
	name pool (see ``child_batch_base``).
	"""
	prefix = f"{customer}-{year_code}{month}-"
	names = frappe.db.sql(
		"SELECT name FROM `tabBatch` WHERE name LIKE %s",
		(_escape_like(prefix) + "%",),
		pluck=True,
	)
	return str(highest_receipt_serial(names) + 1).zfill(2)


def highest_receipt_serial(names):
	"""Highest numeric serial among receipt batch names; 0 when there is none.

	A receipt batch ends in its serial (``...-Y-12``) and a child ends in letters
	(``...-Y-12-A``), so only an all-digit last segment counts.
	"""
	highest = 0
	for name in names or ():
		if not isinstance(name, str):
			continue
		last = name.rsplit("-", 1)[-1]
		if last.isascii() and last.isdigit():
			highest = max(highest, int(last))
	return highest


#: ``tabBatch.name`` and ``batch_id`` are varchar(140), as is every column storing a batch.
_BATCH_NAME_LENGTH = 140
#: Each clash is one child that another submit committed on the same pool after this
#: transaction's snapshot, so the budget only has to exceed realistic concurrency on one
#: parent. A clash costs one savepoint and one rejected insert; the cap stops a loop.
_CHILD_BATCH_ATTEMPTS = 50


def encode_child_suffix(ordinal):
	"""Child suffix for ``ordinal``: 1 -> A, 26 -> Z, 27 -> AA, 702 -> ZZ, 703 -> AAA.

	Bijective base 26: the letters never run out, and every existing A-Z name keeps
	its ordinal.
	"""
	if ordinal < 1:
		raise ValueError(f"child suffix ordinal must be positive, got {ordinal}")
	letters = []
	while ordinal:
		ordinal, remainder = divmod(ordinal - 1, 26)
		letters.append(chr(ord("A") + remainder))
	return "".join(reversed(letters))


def decode_child_suffix(suffix):
	"""Ordinal of a child suffix, or ``None`` when ``suffix`` is not ASCII letters.

	Case-insensitive like ``tabBatch.name`` (utf8mb4_unicode_ci): ``...-a`` occupies the
	``...-A`` slot.
	"""
	if not suffix or not suffix.isascii() or not suffix.isalpha():
		return None
	ordinal = 0
	for letter in suffix.upper():
		ordinal = ordinal * 26 + (ord(letter) - ord("A") + 1)
	return ordinal


def child_batch_base(parent_batch, parent_item, customer, child_item):
	"""The name pool a child of ``parent_batch`` is minted in, or ``None``.

	``{customer}-{year-month}-{child item}-{parent serial path}``, where the serial path
	is everything after the parent's own item code: ``12`` for the receipt
	``GJCU0009-2F09-M-G-24KT-99.9-Y-12`` and ``12-A`` for its conversion child
	``GJCU0009-2F09-M-G-22KT-91.75-Y-12-A``. The old base kept only the parent's last
	segment, so every ``...-NN-A`` parent shared one pool that filled up at Z
	(EMP-IR-Labh-2026-14111).

	Item and customer codes contain hyphens, so the path is found by stripping the known
	owner and ``parent_item`` rather than by splitting. A name that does not parse keeps
	the old ``parts[1]`` / ``parts[-1]`` base. Children of a receipt batch therefore keep
	the names they always had -- except where the customer code itself contains a hyphen,
	whose year-month segment the old split misread and which is now the real one.

	``None`` for a parent with fewer than four segments (a Serial-and-Batch autoname
	such as ``KG2F093-MGL229175Y0-604EO``): this module did not name it, so there is no
	serial to extend.
	"""
	parts = (parent_batch or "").split("-")
	if len(parts) < 4:
		return None

	year_month, serial_path = parts[1], parts[-1]
	owner = (
		customer if customer and parent_batch.startswith(f"{customer}-") else parts[0]
	)
	head, separator, tail = parent_batch[len(owner) + 1 :].partition("-")
	item_prefix = f"{parent_item}-" if parent_item else None
	if (
		head
		and separator
		and item_prefix
		and tail.startswith(item_prefix)
		and len(tail) > len(item_prefix)
	):
		year_month, serial_path = head, tail[len(item_prefix) :]

	return f"{customer or parts[0]}-{year_month}-{child_item}-{serial_path}"


def highest_child_ordinal(base_name, names):
	"""Highest ordinal among ``names`` that are exactly ``base_name-<letters>``; 0 if none.

	Compared as numbers, never as text (``Z`` sorts after ``AA``). Deeper descendants
	(``base-A-B``), non-letter suffixes and prefix siblings do not count. Gaps are left
	alone, so no name is ever handed out a second time.
	"""
	prefix = f"{base_name}-"
	folded_prefix = prefix.casefold()
	highest = 0
	for name in names or ():
		if not isinstance(name, str) or name[: len(prefix)].casefold() != folded_prefix:
			continue
		ordinal = decode_child_suffix(name[len(prefix) :])
		if ordinal and ordinal > highest:
			highest = ordinal
	return highest


def _escape_like(value):
	return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_lane_key(row):
	"""Ownership key of a Stock Entry row: ``(inventory_type, customer)``.

	An empty ``inventory_type`` means company stock, so it normalises to
	"Regular Stock" rather than becoming a third kind of ownership.
	"""
	return (
		getattr(row, "inventory_type", None) or "Regular Stock",
		getattr(row, "customer", None) or None,
	)


def _row_group_key(row):
	"""The group a row is minted in: its conversion lane when the builder tagged one.

	Metal Conversions tags every row with its lane (``metal_conversions.lane_tag``), and a
	customer batch is a lane of its own -- so two batches of ONE customer are two groups,
	each minted from its own source batch. Settle and SNC tag a customer's conversion with
	the owner's lane alone, one group as before. An untagged voucher (Customer Goods
	Received, Subcontracting Repack, SNC's company-metal leg) groups by ownership, as it
	always has.
	"""
	return getattr(row, "custom_conversion_lane", None) or _row_lane_key(row)


def _lane_source_rows(doc):
	"""``(group key, source row)`` for every source row that can parent a child, in row order.

	In a tagged group only a customer-owned source row can be the parent: the lane's
	alloy is consumed under the same tag as Regular Stock, and a company alloy batch
	must never lend its serial to the customer's converted batch.
	"""
	for row in doc.items:
		if not (row.s_warehouse and row.batch_no):
			continue
		if (
			getattr(row, "custom_conversion_lane", None)
			and _row_lane_key(row)[0] not in CUSTOMER_INVENTORY_TYPES
		):
			continue
		yield _row_group_key(row), row


def _lane_parent_batches(doc):
	"""``{group key: first source batch of that group}``, in row order."""
	parents = {}
	for key, row in _lane_source_rows(doc):
		parents.setdefault(key, row.batch_no)
	return parents


def _lane_source_batches(doc):
	"""``{group key: every source batch of that group}`` -- what a child's Ref Customer is read from."""
	sources = {}
	for key, row in _lane_source_rows(doc):
		sources.setdefault(key, set()).add(row.batch_no)
	return sources


def _common_ref_customer(lineage, ref_customers):
	"""The Ref Customer all of ``lineage`` agrees on, else ``None``.

	A conversion that mixes two end customers' material -- or tagged with untagged -- must not
	pose as one customer's: ``hybrid_findings`` would then issue it to that customer's order.
	"""
	values = {ref_customers.get(batch) for batch in lineage or ()}
	return values.pop() if len(values) == 1 else None


def _run_sources(doc):
	"""``{id(produce row): the consume row it was lost from}`` for a Process Loss entry.

	Every Process Loss builder emits runs of consume rows followed by the produce rows
	they feed (``loss_valuation.iter_loss_runs``): the Employee IR and Tree Number
	builders one pair per loss, the warehouse receive ``[consume..., produce...]`` split
	by owner. A produce row's parent is the first consume row, in its own lane, of its
	OWN run. Naming it from the lane's first source instead mislabelled every loss after
	the first (MAT-STE-17857: the loss drawn from ``...-Y-04`` was named as a child of
	``...-Y-03-A``). Other entry types keep the lane-level parent.
	"""
	if getattr(doc, "stock_entry_type", None) != PROCESS_LOSS_SE_TYPE:
		return {}

	sources = {}
	for consumed, produced in iter_loss_runs(doc.items):
		for row in produced:
			lane_key = _row_lane_key(row)
			source = next(
				(
					candidate
					for candidate in consumed
					if getattr(candidate, "batch_no", None)
					and _row_lane_key(candidate) == lane_key
				),
				None,
			)
			if source is not None:
				sources[id(row)] = source
	return sources


def _mint_child_batch(
	doc, row, row_number, parent_batch, customer, base_name, ref_customer=None
):
	"""Insert the next free child of ``base_name`` for ``row`` and return its name.

	The pool is read with a plain snapshot read, so under REPEATABLE READ it can miss a
	child another submit committed after this transaction began. The primary key still
	rejects that name, so a clash (ER_DUP_ENTRY) rolls back to a savepoint and takes the
	next suffix: the ordinal only moves forward, even past a name the parser cannot read.

	No row is locked for this. The primary key already serialises two inserts of one name
	-- the second waits for the first transaction and then clashes -- and an early lock on
	the parent would not refresh the snapshot; it would only hold a Batch row across the
	Serial-and-Batch and ledger posting, against transfers that lock batches in another
	order. With ``innodb_snapshot_isolation`` on (the MariaDB default from 11.6.2) the
	clash surfaces as a deadlock instead; that is not caught here, so the submit fails
	and is simply retried.
	"""
	existing = frappe.db.sql(
		"SELECT name FROM `tabBatch` WHERE name LIKE %s",
		(_escape_like(base_name) + "-%",),
		pluck=True,
	)
	ordinal = highest_child_ordinal(base_name, existing)

	for _attempt in range(_CHILD_BATCH_ATTEMPTS):
		ordinal += 1
		batch_name = f"{base_name}-{encode_child_suffix(ordinal)}"
		if len(batch_name) > _BATCH_NAME_LENGTH:
			_throw_child_batch_error(
				doc,
				row,
				row_number,
				parent_batch,
				customer,
				_(
					"the next name, {0}, is longer than the {1} characters a batch name can hold."
				).format(batch_name, _BATCH_NAME_LENGTH),
			)

		savepoint = f"child_batch_{frappe.generate_hash(length=10)}"
		message_count = len(frappe.local.message_log)
		frappe.db.savepoint(savepoint)
		try:
			_insert_child_batch(doc, row, customer, batch_name, ref_customer)
		except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
			frappe.db.rollback(save_point=savepoint)
			# db_insert announced "Batch ... already exists" before raising; that clash is
			# handled here, so it must not reach the operator.
			del frappe.local.message_log[message_count:]
			continue
		frappe.db.release_savepoint(savepoint)
		return batch_name

	_throw_child_batch_error(
		doc,
		row,
		row_number,
		parent_batch,
		customer,
		_(
			"{0} names in a row under {1} were taken by submissions running at the same time. Submit again."
		).format(_CHILD_BATCH_ATTEMPTS, base_name),
	)


def _insert_child_batch(doc, row, customer, batch_name, ref_customer=None):
	previous_autoname_flag = frappe.flags.is_batch_autoname
	frappe.flags.is_batch_autoname = True

	try:
		batch = frappe.new_doc("Batch")
		batch.batch_id = batch_name
		batch.item = row.item_code
		batch.reference_doctype = doc.doctype
		batch.reference_name = doc.name
		batch.custom_voucher_detail_no = row.name
		# The owning customer comes from the ROW on a mixed voucher: the header
		# describes at most one lane, and stamping it everywhere is what would
		# mislabel another lane's target batch.
		batch.custom_customer = customer
		batch.custom_inventory_type = "Customer Goods"
		batch.custom_customer_voucher_type = "Customer Subcontracting"
		batch.custom_ref_customer = ref_customer
		batch.custom_metal_rate = _source_row_rate(doc, row)
		batch.insert(ignore_permissions=True)
	finally:
		frappe.flags.is_batch_autoname = previous_autoname_flag


def _throw_child_batch_error(doc, row, row_number, parent_batch, customer, reason):
	source = _("Stock Entry {0}").format(doc.name)
	if getattr(doc, "employee_ir", None):
		source = _("{0} (Employee IR {1})").format(source, doc.employee_ir)
	frappe.throw(
		_(
			"Cannot create the customer child batch for row {0} ({1}) of {2}, drawn from "
			"parent batch {3} of customer {4}: {5}"
		).format(row_number, row.item_code, source, parent_batch, customer, reason),
		title=_("Child Batch Not Created"),
	)


def create_child_batches(doc, method=None):
	if doc.doctype != "Stock Entry":
		return

	# The gate. A voucher mints customer child batches here when its header names the
	# owning customer (the customer-subcontracting orchestration sets ``doc._customer``)
	# or any of its rows carries one. The row test admits Metal Conversion lanes AND the
	# auto-created Process Loss entries of the Employee IR, Tree Number and warehouse
	# receive builders, whose customer-owned scrap is named here as a child of the batch
	# it was lost from: a customer-coded name that shows its lineage. (Custody does not
	# depend on it -- record_stock_movement reads the row's bundle or batch either way.)
	# Rows with no customer anywhere -- Regular Stock loss, melting loss -- still mint
	# through the Serial-and-Batch path on submit.
	header_customer = getattr(doc, "_customer", None)
	if not header_customer and not any(
		getattr(row, "customer", None) for row in doc.items
	):
		return

	parents = _lane_parent_batches(doc)
	lane_sources = _lane_source_batches(doc)

	# An untagged voucher whose rows are all one ownership is handled exactly as before:
	# one parent batch for the whole entry, every batch-less produce row minted from it.
	# Customer Goods Received and Subcontracting Repack build such a voucher.
	#
	# A lane-tagged voucher (Metal Conversions) always takes the per-row path, even with
	# one lane: its rows say which lane -- and so which parent -- each output belongs to,
	# and the single-lane path would mint its Regular Stock rows (the C09 company alloy
	# carve-out) as the customer's. SNC's create_repack_metal_conversion tags a customer's
	# conversion too: its one source row and one output share the owner's lane, so this
	# path finds the same parent and mints the same name -- which matters because SNC
	# reads its target batch back off Stock Entry Detail and throws if nothing was minted.
	tagged = any(getattr(row, "custom_conversion_lane", None) for row in doc.items)
	single_lane = not tagged and len(parents) <= 1

	if not parents:
		return

	source_items = {
		row.batch_no: row.item_code
		for row in doc.items
		if row.s_warehouse and row.batch_no
	}
	run_sources = _run_sources(doc)

	plan = []
	for row_number, row in enumerate(doc.items, start=1):
		if row.s_warehouse or row.batch_no:
			continue

		if not row.t_warehouse:
			continue

		lane_key = _row_lane_key(row)

		if single_lane:
			parent_batch = next(iter(parents.values()))
			lineage = next(iter(lane_sources.values()))
			customer = getattr(row, "customer", None) or header_customer
		else:
			# Mixed ownership: only customer-owned produce rows get a customer child
			# batch. A Regular Stock row is left with an empty batch_no on purpose so
			# the Serial-and-Batch path mints it -- that is the only path that stamps
			# ownership from the row itself and runs the Customer-Goods item guard.
			if lane_key[0] not in CUSTOMER_INVENTORY_TYPES:
				continue

			parent_batch = parents.get(_row_group_key(row))
			if not parent_batch:
				continue
			lineage = lane_sources.get(_row_group_key(row))

			customer = lane_key[1] or header_customer

		source = run_sources.get(id(row))
		if source is not None:
			parent_batch = source.batch_no
			parent_item = source.item_code
			lineage = {source.batch_no}
		else:
			parent_item = source_items.get(parent_batch)

		base_name = child_batch_base(parent_batch, parent_item, customer, row.item_code)
		if not base_name:
			# The parent was not named by this module (e.g. a Customer Goods batch
			# created by a customer Purchase Receipt has only three segments), so there
			# is no serial to extend. Skip this row and let the Serial-and-Batch path
			# mint it -- historically this aborted the whole voucher.
			continue

		plan.append(
			(
				row,
				getattr(row, "idx", None) or row_number,
				parent_batch,
				customer or header_customer,
				base_name,
				lineage,
			)
		)

	if not plan:
		return

	# A child keeps the Ref Customer of the batches it was made from (hybrid_findings), read in
	# one query for the whole voucher.
	ref_customers = get_batch_ref_customer_map(
		[batch for *_head, lineage in plan for batch in lineage or ()]
	)
	for row, row_number, parent_batch, customer, base_name, lineage in plan:
		row.batch_no = _mint_child_batch(
			doc,
			row,
			row_number,
			parent_batch,
			customer,
			base_name,
			ref_customer=_common_ref_customer(lineage, ref_customers),
		)


def get_purity(item_code):
	item = frappe.get_doc("Item", item_code)
	purity = 100
	for attr in item.attributes:
		if attr.attribute == "Metal Purity":
			purity = flt(attr.attribute_value)
	return purity


def create_repack_for_used_other(doc, method=None):
	if doc.doctype != "Stock Entry":
		return

	if doc.stock_entry_type not in [
		"Customer Goods Received",
		"Customer Goods Transfer",
	]:
		return

	for item in doc.items:
		if not getattr(item, "t_warehouse", "").startswith("Central RM"):
			return

	source_customer = (
		getattr(doc, "_customer", None)
		or getattr(doc, "customer", None)
		or next(
			(row.customer for row in doc.items if getattr(row, "customer", None)), None
		)
	)

	if not source_customer:
		return []

	columns, report_data = get_report_data(filters={"other_customer": source_customer})
	if not report_data:
		return []

	matched_rows = []

	for row in report_data:
		try:
			batch_no = row[0]
			owner = row[1]
			item = row[2]
			used_other = flt(row[5])
			other_customer = row[6]
		except Exception:
			continue

		if not other_customer or used_other <= 0 or not owner:
			continue

		other_customers = [
			c.strip() for c in (other_customer or "").split(",") if c.strip()
		]
		if source_customer in other_customers:
			matched_rows.append(
				{
					"batch_no": batch_no,
					"owner": owner,
					"item": item,
					"used_other": used_other,
				}
			)

	if not matched_rows:
		return []

	matched_rows.sort(key=lambda x: x["batch_no"])

	source_batch = next((d.batch_no for d in doc.items if d.batch_no), None)
	source_warehouse = next((d.s_warehouse for d in doc.items if d.batch_no), None)

	if not source_batch:
		return

	total_source_qty = sum(flt(d.qty) for d in doc.items if d.batch_no)
	source_item = next((d.item_code for d in doc.items if d.batch_no), None)

	source_purity = get_purity(source_item)
	total_source_24kt = total_source_qty * (source_purity / 100)

	remaining_qty = total_source_24kt

	for row in matched_rows:
		if remaining_qty <= 0:
			break

		child_batch = row["batch_no"]
		item_code = row["item"]
		used_other = row["used_other"]
		owner = row["owner"]

		if owner == source_customer:
			continue

		linked_batches = get_linked_batches(child_batch)

		parent_batch = None
		for b in linked_batches:
			item = frappe.get_value("Batch", b, "item")
			if item and "24KT" in item:
				parent_batch = b
				break

		if not parent_batch:
			continue

		parent_item = frappe.get_value("Batch", parent_batch, "item")

		already_repacked = (
			frappe.db.sql(
				"""
			SELECT IFNULL(SUM(sed_source.qty), 0)
			FROM `tabStock Entry` se
			JOIN `tabStock Entry Detail` sed_source
				ON sed_source.parent = se.name AND sed_source.is_finished_item = 0
			JOIN `tabStock Entry Detail` sed_target
				ON sed_target.parent = se.name AND sed_target.is_finished_item = 1
			WHERE se.stock_entry_type = 'Subcontracting Repack'
			AND sed_target.batch_no = %s
			AND sed_source.batch_no = %s
			AND se.docstatus = 1
		""",
				(parent_batch, source_batch),
			)[0][0]
			or 0
		)

		purity = get_purity(item_code)
		used_other_24kt = used_other * (purity / 100)

		pending_qty = used_other_24kt - already_repacked

		if pending_qty <= 0:
			continue

		process_qty = min(pending_qty, remaining_qty)

		purity = get_purity(item_code)
		converted_qty = process_qty * (purity / 100)

		try:
			se = frappe.new_doc("Stock Entry")
			se.stock_entry_type = "Subcontracting Repack"
			se.purpose = "Repack"
			se.company = doc.company

			se.append(
				"items",
				{
					"item_code": parent_item,
					"batch_no": source_batch,
					"qty": converted_qty,
					"s_warehouse": source_warehouse,
					"customer": source_customer,
					"inventory_type": "Regular Stock",
					"is_finished_item": 0,
					"use_serial_batch_fields": 1,
				},
			)

			se.append(
				"items",
				{
					"item_code": parent_item,
					"batch_no": parent_batch,
					"qty": converted_qty,
					"t_warehouse": source_warehouse,
					"customer": owner,
					"inventory_type": "Regular Stock",
					"is_finished_item": 1,
					"use_serial_batch_fields": 1,
				},
			)

			se.insert(ignore_permissions=True)
			se.submit()

		except Exception as e:
			frappe.log_error("Repack Error", str(e))
			continue

		remaining_qty -= process_qty
