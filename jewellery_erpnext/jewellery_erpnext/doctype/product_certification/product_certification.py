# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

from collections import defaultdict

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.utils import cint, flt

from jewellery_erpnext.jewellery_erpnext.customization.utils.metal_utils import (
	get_purity_percentage,
)
from jewellery_erpnext.jewellery_erpnext.doctype.main_slip.main_slip import (
	get_item_loss_item,
)
from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.doc_events.receive_status import (
	FULLY_RECEIVED,
	pending_eps,
	se_precision,
	update_receive_status,
	validate_over_receipt,
)
from jewellery_erpnext.jewellery_erpnext.doctype.product_certification.doc_events.utils import (
	create_material_receipt_for_certification,
	create_po,
	process_fire_assy_xrf_submit,
	update_bom_details,
	validate_po_configuration,
)
from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator import (
	resolve_and_validate,
)


def _slip_key(row):
	"""Fire Assy / XRF grouping key, shared by every routine that pairs a Product Details
	row with its generated Exploded Product Details rows.

	It is the same ``[main_slip, tree_no]`` pair ``get_exploded_table`` dedupes on, with
	blanks normalised so ``None`` and ``""`` land in one group instead of two.
	"""
	return (row.get("main_slip") or "", row.get("tree_no") or "")


def _department_wo_warehouse(department, throw=True):
	"""The department's WO warehouse -- ``warehouse_type = "Manufacturing"``, e.g.
	``Product Certification WO - KGJPL``.

	Every Product Certification stock line belongs here. Resolving it once, from the document's
	department, is what keeps a certification inside its own department ledger: the previous code
	fell back to *any* warehouse of the department and then let the serial's live warehouse
	override the answer, which leaked issues into Tagging FG, department Transit and even the
	other company's Product Certification warehouse.

	Same filter as the canonical resolvers elsewhere in the app -- see
	``department_ir/doc_events/pc_tagging_stock_sync._resolve_dept_manufacturing_wh``.
	"""
	if not department:
		return None

	warehouse = frappe.db.get_value(
		"Warehouse",
		{
			"disabled": 0,
			"is_group": 0,
			"department": department,
			"warehouse_type": "Manufacturing",
		},
		"name",
	)
	if not warehouse and throw:
		frappe.throw(
			_(
				"No WO warehouse found for Department {0}. "
				"Create an enabled warehouse of type Manufacturing for it."
			).format(frappe.bold(department)),
			title=_("Department Warehouse Missing"),
		)
	return warehouse


class ProductCertification(Document):
	def validate(self):
		if self.department and not frappe.db.exists(
			"Warehouse", {"disabled": 0, "department": self.department}
		):
			frappe.throw(_("Please set warehouse for selected Department"))

		if self.department and self.company:
			dept_company = frappe.db.get_value("Department", self.department, "company")
			if dept_company and dept_company != self.company:
				frappe.throw(
					_(
						"Department {0} belongs to Company {1}, not {2}. "
						"Please select a department that belongs to {2}."
					).format(
						frappe.bold(self.department),
						frappe.bold(dept_company),
						frappe.bold(self.company),
					),
					title=_("Company and Department Mismatch"),
				)

		if self.supplier and not frappe.db.exists(
			"Warehouse",
			{"disabled": 0, "company": self.company, "subcontractor": self.supplier},
		):
			frappe.throw(_("Please set warehouse for selected supplier"))

		self.validate_duplicate_product_rows()
		self.validate_serial_warehouse_department()
		self.validate_items()
		validate_over_receipt(self)
		self.update_bom()
		self.get_exploded_table()
		self.calculate_fire_assy_loss_weight()
		self.set_fire_assy_issue_weight()
		self.distribute_amount()

	def before_submit(self):
		self.validate_exploded_qty()
		# Refuse the submit unless the service PO can actually be created. Checked here,
		# where the operator can fix the master data, rather than in the deferred job —
		# which runs after commit and would leave a submitted certification with no PO.
		validate_po_configuration(self)

	def validate_duplicate_product_rows(self):
		"""One Product Details row per serial / per work order.

		The scan handlers append without looking at what is already in the grid, so a repeated
		scan used to add a second row and double the issued weight. This is the server-side half
		of the guard -- ``product_details`` is ``allow_bulk_edit``, so the grid and the paste
		dialog can produce the same duplicates the barcode gun does.

		Tree rows are exempt: several scan lines for one tree are intentional and are summed onto
		a single exploded main row (see ``calculate_fire_assy_loss_weight``).
		"""
		seen = {}
		for row in self.product_details:
			if row.get("tree_no"):
				continue

			# serial_no and manufacturing_work_order are never both set on a row, so whichever
			# one the row carries is its identity.
			if row.get("serial_no"):
				key = ("Serial No", row.serial_no)
			elif row.get("manufacturing_work_order"):
				key = ("Manufacturing Work Order", row.manufacturing_work_order)
			else:
				continue

			first_idx = seen.get(key)
			if first_idx:
				frappe.throw(
					_("Row #{0}: {1} {2} is already entered in Row #{3}.").format(
						row.idx, key[0], frappe.bold(key[1]), first_idx
					),
					title=_("Duplicate Row"),
				)
			seen[key] = row.idx

	def validate_serial_warehouse_department(self):
		"""On an Issue, every Product Details serial must sit in this Department's WO warehouse.

		The check is against that one warehouse by name, not merely "some warehouse of this
		department". ``create_stock_entry`` sources every serial line from the WO warehouse, so
		anything parked elsewhere -- another department's FG warehouse, this department's
		Transit warehouse -- would book an issue out of a warehouse that does not hold the piece.
		A serial in Transit has not finished arriving; move it in first.

		Issue only. A Receive carries serials that are still in the supplier's WIP
		warehouse -- moving them back is what the receipt is for.
		"""
		if self.type != "Issue" or not self.department:
			return

		serials = {row.serial_no for row in self.product_details if row.serial_no}
		if not serials:
			return

		expected_wh = _department_wo_warehouse(self.department)

		serial_wh = dict(
			frappe.get_all(
				"Serial No",
				filters={"name": ("in", list(serials))},
				fields=["name", "warehouse"],
				as_list=True,
			)
		)

		for row in self.product_details:
			if not row.serial_no:
				continue

			warehouse = serial_wh.get(row.serial_no)
			if not warehouse:
				# ERPNext clears Serial No.warehouse on every outward movement, so a
				# blank warehouse means "not in stock", not "unknown".
				frappe.throw(
					_("Row #{0}: Serial No {1} is not in stock.").format(
						row.idx, frappe.bold(row.serial_no)
					),
					title=_("Serial No Not In Stock"),
				)

			if warehouse != expected_wh:
				frappe.throw(
					_(
						"Row #{0}: Serial No {1} is in {2}, not {3}. "
						"Move it into {3} before issuing."
					).format(
						row.idx,
						frappe.bold(row.serial_no),
						frappe.bold(warehouse),
						frappe.bold(expected_wh),
					),
					title=_("Serial No Not In Department WO Warehouse"),
				)

	def validate_items(self):
		if self.type == "Receive":
			for row in self.exploded_product_details:
				if self.service_type == "Hall Marking Service" and not row.huid:
					frappe.throw(
						_(
							"Row #{0}: HUID is mandatory for Hall Marking Service"
						).format(row.idx)
					)
				if (
					self.service_type == "Diamond Certificate service"
					and not row.certification
				):
					frappe.throw(
						_(
							"Row #{0}: Certification No is mandatory for Diamond Certificate service"
						).format(row.idx)
					)

		if self.type == "Issue":
			return
		for row in self.product_details:
			if not frappe.db.get_value(
				"Product Details",
				{
					"parent": self.receive_against,
					"serial_no": row.get("serial_no") if row.get("serial_no") else None,
					"item_code": row.item_code,
					"manufacturing_work_order": row.get("manufacturing_work_order")
					if row.get("manufacturing_work_order")
					else None,
					"parent_manufacturing_order": row.get("parent_manufacturing_order")
					if row.get("parent_manufacturing_order")
					else None,
					"tree_no": row.get("tree_no") if row.get("tree_no") else None,
				},
			):
				# frappe.throw(_(f"Row #{row.idx}: item not found in {self.receive_against}"))
				frappe.throw(
					_("Row #{0}: item not found in {1}").format(
						row.idx, self.receive_against
					)
				)

	def validate_exploded_qty(self):
		if self.type != "Receive":
			return
		if self.service_type not in ["Fire Assy Service", "XRF Services"]:
			return
		if not self.exploded_product_details or not self.product_details:
			return

		# Grouped on the same (main_slip, tree_no) key the loss calculation and
		# get_exploded_table use. A document that carries no main slip / tree at all
		# collapses to a single ("", "") group — i.e. the grand-total comparison.
		issued = defaultdict(float)
		first_idx = {}
		for row in self.product_details:
			key = _slip_key(row)
			issued[key] += flt(row.total_weight)
			first_idx.setdefault(key, row.idx)

		booked = defaultdict(float)
		for row in self.exploded_product_details:
			booked[_slip_key(row)] += flt(row.conversion_quantity or row.gross_weight)

		for key, total_weight in issued.items():
			exploded_weight = booked.get(key, 0.0)
			if abs(total_weight - exploded_weight) <= 0.001:
				continue

			main_slip, tree_no = key
			if main_slip or tree_no:
				frappe.throw(
					_(
						"Row #{0}: Total Gross Weight in Exploded Product Details ({1}) does not match Total Weight in Product Details ({2}) for Main Slip {3}"
					).format(
						first_idx.get(key),
						exploded_weight,
						total_weight,
						main_slip or tree_no,
					)
				)
			frappe.throw(
				_(
					"Total Gross Weight in Exploded Product Details ({0}) does not match Total Weight in Product Details ({1})"
				).format(exploded_weight, total_weight)
			)

	def calculate_fire_assy_loss_weight(self):
		"""Auto-calculate the scrap/loss weight on a Fire Assy / XRF Receive.

		Per (main_slip, tree_no) group in exploded_product_details:
		  Row 1 = main item   (e.g. 22KT-91.9)  – user enters gross_weight (receive wt)
		  Row 2 = pure item   (e.g. 24KT-99.9)  – user enters gross_weight (Fire Assy only)
		  Row 3 = loss/scrap  (e.g. ML-22KT)    – auto-calculated

		Loss formula:
		  converted_pure_wt = pure_wt × (pure_purity / main_purity)
		  loss_wt = issue_wt − receive_wt − converted_pure_wt

		Grouping on (main_slip, tree_no) rather than main_slip alone is the fix for the
		reported "loss is not calculated" bug: most documents are entered WITHOUT a main
		slip, and keying on it alone made the whole routine return early. A document with
		no slip/tree collapses to one ("", "") group and is handled identically.

		XRF Services has no pure row at all, so ``converted_pure_wt`` is simply 0 there —
		it must not short-circuit the loss.
		"""
		if self.type != "Receive" or self.service_type not in [
			"Fire Assy Service",
			"XRF Services",
		]:
			return
		if not self.exploded_product_details or not self.product_details:
			return

		# (main_slip, tree_no) → {main_item, issue_weight, pure_item, loss_item}
		slip_data = {}
		for pd in self.product_details:
			data = slip_data.setdefault(
				_slip_key(pd),
				{
					"main_item": None,
					"issue_weight": 0.0,
					"pure_item": None,
					"loss_item": None,
				},
			)
			# Summed, not overwritten: two rows on the same slip issue their combined weight.
			data["issue_weight"] += flt(pd.total_weight)
			data["main_item"] = data["main_item"] or pd.item_code
			data["pure_item"] = data["pure_item"] or pd.get("pure_item")
			data["loss_item"] = data["loss_item"] or pd.get("loss_item")

		if not slip_data:
			return

		# Cache purity percentages
		purity_cache = {}

		def _get_purity(item_code):
			if item_code not in purity_cache:
				purity_cache[item_code] = flt(get_purity_percentage(item_code))
			return purity_cache[item_code]

		def _require_purity(item_code):
			purity = _get_purity(item_code)
			if not purity:
				frappe.throw(
					_(
						"Item {0} has no Metal Purity percentage configured, so the loss "
						"weight cannot be calculated. Set the purity percentage on its "
						"Metal Purity attribute value."
					).format(frappe.bold(item_code)),
					title=_("Metal Purity Missing"),
				)
			return purity

		slip_rows = defaultdict(list)
		for row in self.exploded_product_details:
			key = _slip_key(row)
			if key in slip_data:
				slip_rows[key].append(row)

		precision = se_precision()

		for key, rows in slip_rows.items():
			sd = slip_data[key]
			main_item = sd["main_item"]
			pure_item = sd["pure_item"]
			loss_item = sd["loss_item"]
			issue_weight = sd["issue_weight"]

			if not (main_item and loss_item):
				continue

			# Find the specific rows
			main_row = None
			pure_row = None
			loss_row = None

			for r in rows:
				if r.item_code == main_item and not main_row:
					main_row = r
				elif pure_item and r.item_code == pure_item and not pure_row:
					pure_row = r
				elif r.item_code == loss_item and not loss_row:
					loss_row = r

			if not (main_row and loss_row):
				continue

			receive_weight = flt(main_row.gross_weight)
			pure_weight = flt(pure_row.gross_weight) if pure_row else 0.0

			# Nothing entered yet (the exploded rows are created on the first save, before
			# the operator types any weight). Booking the entire issue as loss here would
			# balance validate_exploded_qty and let an all-loss document be submitted.
			if not receive_weight and not pure_weight:
				continue

			converted_pure_wt = 0.0
			if pure_weight:
				# Convert 24KT weight into equivalent weight at the main item's purity
				main_purity = _require_purity(main_item)
				pure_purity = _require_purity(pure_item)
				converted_pure_wt = flt(
					pure_weight * (pure_purity / main_purity), precision
				)

			if pure_row:
				pure_row.conversion_quantity = converted_pure_wt

			loss_row.gross_weight = max(
				0.0,
				flt(issue_weight - receive_weight - converted_pure_wt, precision),
			)

	def set_fire_assy_issue_weight(self):
		"""Populate the main exploded row's gross_weight on a Fire Assy / XRF Issue.

		The Issue-side sibling of ``calculate_fire_assy_loss_weight``. On an Issue there is
		no loss/pure — the operator types the sample weight into ``product_details.total_weight``
		(the exploded rows are machine-generated blank), and the whole sample is what leaves for
		assay. So the main exploded row of each ``_slip_key`` group takes that group's issued
		weight; ``create_stock_entry`` then issues exactly that row (pure/loss stay 0 and are
		skipped).

		Grouped identically to the Receive-side loss calc so the two never disagree about a
		group's issued weight. Un-guarded overwrite keeps the main row in sync when the operator
		corrects ``total_weight``; the ``type == "Issue"`` guard keeps it from ever touching a
		Receive's operator-entered main weight. This restores the back-fill the generic
		``distribute_amount`` path used to provide before its Fire Assy / XRF early-return.
		"""
		if self.type != "Issue" or self.service_type not in [
			"Fire Assy Service",
			"XRF Services",
		]:
			return
		if not self.exploded_product_details or not self.product_details:
			return

		# (main_slip, tree_no) → {main_item, issue_weight}
		slip_data = {}
		for pd in self.product_details:
			data = slip_data.setdefault(
				_slip_key(pd), {"main_item": None, "issue_weight": 0.0}
			)
			# Summed, not overwritten: two rows on the same slip issue their combined weight.
			data["issue_weight"] += flt(pd.total_weight)
			data["main_item"] = data["main_item"] or pd.item_code

		slip_rows = defaultdict(list)
		for row in self.exploded_product_details:
			key = _slip_key(row)
			if key in slip_data:
				slip_rows[key].append(row)

		for key, sd in slip_data.items():
			if not sd["main_item"]:
				continue
			# First match only: if get_exploded_table ever emits a duplicate main row, only
			# the first carries the weight — the duplicate stays 0 (skipped in the Stock
			# Entry), so the issued qty is never doubled.
			main_row = next(
				(r for r in slip_rows.get(key, []) if r.item_code == sd["main_item"]),
				None,
			)
			if main_row is not None:
				main_row.gross_weight = sd["issue_weight"]

	def update_bom(self):
		if self.service_type in ["Hall Marking Service", "Diamond Certificate service"]:
			for row in self.product_details:
				if not (
					row.serial_no
					or row.manufacturing_work_order
					or row.parent_manufacturing_order
				):
					# frappe.throw(_(f"Row #{row.idx}: Either select serial no or manufacturing work order"))
					frappe.throw(
						_(
							"Row #{0}: Either select serial no or manufacturing work order or Parent Manufacturing Order"
						).format(row.idx)
					)
				if row.bom:
					continue
				if row.serial_no:
					row.bom = frappe.db.get_value(
						"BOM", {"tag_no": row.serial_no}, "name"
					)
				if not row.bom:
					row.bom = frappe.db.get_value("Item", row.item_code, "master_bom")
				if not row.bom:
					# frappe.throw(_(f"Row #{row.idx}: BOM not found for item or serial no"))
					frappe.throw(
						_("Row #{0}: BOM not found for item or serial no").format(
							row.idx
						)
					)

	def distribute_amount(self):
		if not self.exploded_product_details:
			return
		length = len(self.exploded_product_details)
		if self.type == "Issue":
			self.total_amount = 0
		amt = flt(self.total_amount) / length

		# Fire Assy / XRF weights are owned by calculate_fire_assy_loss_weight — the
		# remainder back-fill below is un-purity-converted and would overwrite the
		# computed loss row. Only the amount split applies there.
		if self.service_type in ["Fire Assy Service", "XRF Services"]:
			for row in self.exploded_product_details:
				row.amount = amt
			return

		qty_data = {}
		for row in self.product_details:
			key = (
				row.parent_manufacturing_order or row.manufacturing_work_order,
				row.serial_no,
			)
			qty_data[key] = flt(qty_data.get(key)) + flt(row.total_weight)

		for row in self.exploded_product_details:
			# Keyed on THIS row's own order — it used to reuse the `common_order` left
			# over from the loop above (the last Product Details row's order), which only
			# happened to be right when every row shared one order.
			key = (
				row.parent_manufacturing_order or row.manufacturing_work_order,
				row.serial_no,
			)
			if qty_data.get(key):
				if not row.gross_weight:
					row.gross_weight = qty_data[key]
					qty_data[key] = 0
				else:
					qty_data[key] -= row.gross_weight
			row.amount = amt

	def on_submit(self):
		if self.service_type in ["Fire Assy Service", "XRF Services"]:
			process_fire_assy_xrf_submit(self, create_stock_entry)
		else:
			create_stock_entry(self)
			# Synchronous: a failure here rolls the whole submit back, so a submitted
			# certification always has its PO. Only the BOM amount roll-up stays deferred —
			# that is the part that loops over exploded rows.
			create_po(self)
			frappe.enqueue(
				"jewellery_erpnext.jewellery_erpnext.doctype.product_certification.product_certification.deferred_po_bom",
				pc_name=self.name,
				enqueue_after_commit=True,
				job_id=f"pc_po_bom::{self.name}",
				deduplicate=True,
			)
		self.update_huid()
		self.update_receive_status()

	def on_cancel(self):
		self.update_receive_status()

	def update_receive_status(self):
		"""Roll the receipt ledger on the Issue this document belongs to.

		Recomputed from every submitted Receive rather than applied as a delta, so
		submit and cancel share one code path and cannot drift apart.

		An Issue rolls its OWN ledger on submit: without that, ``pending_weight`` sits at
		0 on a document where nothing has come back yet, which reads as "fully received"
		to anyone looking at the grid.
		"""
		if self.type == "Receive" and self.receive_against:
			update_receive_status(self.receive_against)
		elif self.type == "Issue":
			self.receive_status = update_receive_status(self.name)

	def update_huid(self):
		for row in self.exploded_product_details:
			if row.serial_no:
				add_to_serial_no(row.serial_no, self, row)
			elif row.manufacturing_work_order or row.parent_manufacturing_order:
				if row.huid or row.certification:
					if row.parent_manufacturing_order:
						pmo = row.parent_manufacturing_order
					else:
						pmo = frappe.db.get_value(
							"Manufacturing Work Order",
							row.manufacturing_work_order,
							"manufacturing_order",
						)

					pmo_doc = frappe.get_doc("Parent Manufacturing Order", pmo)
					pmo_doc.append(
						"product_certification_details",
						{
							"huid": row.huid,
							"certification_no": row.certification,
							"date": self.date if row.huid else None,
							"certification_date": self.certification_date
							if row.certification
							else None,
						},
					)
					pmo_doc.save()

	def get_exploded_table(self):
		exploded_product_details = []
		if self.service_type in ["Hall Marking Service", "Diamond Certificate service"]:
			# cat_det = frappe.get_all(
			# 	"Certification Settings",
			# 	{"parent": "Jewellery Settings"},
			# 	["category", "count"],
			# )
			# custom_cat = {row.category: row.count for row in cat_det}
			metal_det = None
			for row in self.product_details:
				metal_touch = ""
				metal_colour = frappe.db.get_value("BOM", row.bom, "metal_colour")
				count = 1
				if row.manufacturing_work_order:
					mwo = frappe.db.get_value(
						"Manufacturing Work Order",
						row.manufacturing_work_order,
						["department", "qty", "metal_touch", "metal_colour"],
						as_dict=1,
					)
					if self.department != mwo.department:
						# frappe.throw(_(f"Manufacturing Work Order should be in '{self.department}' department"))
						frappe.throw(
							_(
								"Row {0}: Manufacturing Work Order should be in {1} department"
							).format(row.idx, self.department)
						)
					count *= cint(mwo.get("qty"))
					metal_touch = mwo.get("metal_touch")
					metal_colour = mwo.get("metal_colour")
				elif row.parent_manufacturing_order:
					departments = frappe.db.get_all(
						"Manufacturing Work Order",
						{
							"docstatus": 1,
							"manufacturing_order": row.parent_manufacturing_order,
							"is_finding_mwo": 0,
						},
						pluck="department",
					)
					department = list(set(departments))

					if len(department) != 1:
						frappe.throw(
							_(
								"All Manufacturing Work Order should be in same Depratment"
							)
						)

					if departments and departments[0] != self.department:
						frappe.throw(
							_(
								"Row {0}: Manufacturing Work Order should be in {1} department"
							).format(row.idx, self.department)
						)
					pmo_data = frappe.db.get_value(
						"Parent Manufacturing Order",
						row.parent_manufacturing_order,
						["qty", "metal_touch", "metal_colour"],
						as_dict=1,
					)
					count *= cint(pmo_data.get("qty"))
					metal_touch = pmo_data.get("metal_touch")
					metal_colour = pmo_data.get("metal_colour")
				else:
					metal_det = frappe.get_all(
						"BOM Metal Detail",
						filters={"parent": row.bom},
						fields=["metal_touch"],
						distinct=True,
					)
					count *= cint(len(metal_det))

				# Override count based on category: 2 for earrings, 1 for everything else
				if row.category and "earring" in str(row.category).lower():
					count = 2
				else:
					count = 1

				existing = []
				for i in self.exploded_product_details:
					common_order = (
						row.parent_manufacturing_order or row.manufacturing_work_order
					)
					if (
						(
							row.item_code == i.item_code
							or row.item_code == ""
							or not row.item_code
						)
						and (
							row.serial_no == i.serial_no
							or row.serial_no == ""
							or not row.serial_no
						)
						and (
							common_order
							== (
								i.parent_manufacturing_order
								or i.manufacturing_work_order
							)
							or common_order == ""
							or not common_order
						)
					):
						existing.append(i)
				# existing = self.get(
				# 	"exploded_product_details",
				# 	{
				# 		"item_code": row.item_code,
				# 		"serial_no": row.serial_no,
				# 		"manufacturing_work_order": row.manufacturing_work_order,
				# 	},
				# )
				if existing and len(existing) == count:
					continue

				pmo_weights = frappe._dict()

				if row.manufacturing_work_order:
					mwo_data = frappe.db.get_value(
						"Manufacturing Work Order",
						row.manufacturing_work_order,
						[
							"manufacturing_operation",
							"gross_wt",
							"net_wt",
							"finding_wt",
							"diamond_wt_in_gram",
							"gemstone_wt",
							"other_wt",
						],
						as_dict=1,
					)
					if mwo_data and mwo_data.manufacturing_operation:
						mop_weights = frappe.db.get_value(
							"Manufacturing Operation",
							mwo_data.manufacturing_operation,
							[
								"received_gross_wt",
								"gross_wt",
								"received_net_wt",
								"net_wt",
								"diamond_wt_in_gram",
								"gemstone_wt_in_gram",
								"finding_wt",
								"other_wt",
							],
							as_dict=1,
						)
						if mop_weights:
							pmo_weights = frappe._dict(
								{
									"gross_weight": mop_weights.received_gross_wt
									or mop_weights.gross_wt
									or mwo_data.gross_wt,
									"net_weight": mop_weights.received_net_wt
									or mop_weights.net_wt
									or mwo_data.net_wt,
									"diamond_weight": mop_weights.diamond_wt_in_gram
									or mwo_data.diamond_wt_in_gram,
									"gemstone_weight": mop_weights.gemstone_wt_in_gram
									or mwo_data.gemstone_wt,
									"finding_weight": mop_weights.finding_wt
									or mwo_data.finding_wt,
									"other_weight": mop_weights.other_wt
									or mwo_data.other_wt,
								}
							)
					elif mwo_data:
						pmo_weights = frappe._dict(
							{
								"gross_weight": mwo_data.gross_wt,
								"net_weight": mwo_data.net_wt,
								"diamond_weight": mwo_data.diamond_wt_in_gram,
								"gemstone_weight": mwo_data.gemstone_wt,
								"finding_weight": mwo_data.finding_wt,
								"other_weight": mwo_data.other_wt,
							}
						)

				if not pmo_weights and (
					row.parent_manufacturing_order or row.manufacturing_work_order
				):
					pmo_weights = frappe.db.get_value(
						"Parent Manufacturing Order",
						row.parent_manufacturing_order or row.manufacturing_work_order,
						[
							"gross_weight",
							"net_weight",
							"diamond_weight",
							"gemstone_weight",
							"finding_weight",
							"other_weight",
						],
						as_dict=1,
					)
				bom_weights = frappe.db.get_value(
					"BOM",
					row.bom,
					[
						"gross_weight",
						"metal_and_finding_weight",
						"diamond_weight",
						"gemstone_weight",
						"finding_weight_",
						"other_weight",
						"total_diamond_pcs",
						"total_gemstone_pcs",
					],
					as_dict=1,
				)

				# Diamond / stone pcs come from the latest Manufacturing Operation when a
				# Manufacturing Work Order is linked; otherwise (serial-no or PMO rows) fall
				# back to the BOM totals (total_diamond_pcs / total_gemstone_pcs).
				diamond_pcs = 0
				stone_pcs = 0
				if row.manufacturing_work_order:
					latest_operation = frappe.get_all(
						"Manufacturing Operation",
						filters={
							"manufacturing_work_order": row.manufacturing_work_order
						},
						fields=["diamond_pcs", "gemstone_pcs"],
						order_by="creation desc",
						limit=1,
					)

					if latest_operation:
						diamond_pcs = latest_operation[0].diamond_pcs
						stone_pcs = latest_operation[0].gemstone_pcs

				if (
					not diamond_pcs
					and bom_weights
					and bom_weights.get("total_diamond_pcs")
				):
					diamond_pcs = bom_weights.get("total_diamond_pcs")
				if (
					not stone_pcs
					and bom_weights
					and bom_weights.get("total_gemstone_pcs")
				):
					stone_pcs = bom_weights.get("total_gemstone_pcs")

				for i in range(0, count):
					if metal_det:
						if count == 2 and len(metal_det) < count:
							metal_touch = metal_det[0].get("metal_touch")
						else:
							metal_touch = metal_det[i].get("metal_touch")

					matching_existing = None
					if existing:
						for a in existing:
							if a.get("metal_touch") == metal_touch:
								matching_existing = a
								break

					if matching_existing:
						matching_existing.gross_weight = (
							pmo_weights.get("gross_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["gross_weight"] / count
							if bom_weights
							else 0
						)
						matching_existing.gold_weight = (
							pmo_weights.get("net_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["metal_and_finding_weight"] / count
							if bom_weights
							else 0
						)
						matching_existing.chain_weight = (
							pmo_weights.get("finding_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["finding_weight_"] / count
							if bom_weights
							else 0
						)
						matching_existing.other_weight = (
							pmo_weights.get("other_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["other_weight"] / count
							if bom_weights
							else 0
						)
						matching_existing.stone_weight = (
							pmo_weights.get("gemstone_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["gemstone_weight"] / count
							if bom_weights
							else 0
						)
						matching_existing.diamond_weight = (
							pmo_weights.get("diamond_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["diamond_weight"] / count
							if bom_weights
							else 0
						)
						matching_existing.diamond_pcs = (
							cint(diamond_pcs) / count
							if count > 1
							else cint(diamond_pcs)
						)
						matching_existing.stone_pcs = (
							cint(stone_pcs) / count if count > 1 else cint(stone_pcs)
						)
						matching_existing.bom = row.bom
						matching_existing.category = row.category
						matching_existing.sub_category = row.sub_category
						matching_existing.metal_touch = metal_touch
						matching_existing.metal_colour = metal_colour
						continue

					exploded_product_details.append(
						{
							"item_code": row.item_code,
							"serial_no": row.serial_no,
							"bom": row.bom,
							"gross_weight": pmo_weights.get("gross_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["gross_weight"] / count
							if bom_weights
							else 0,
							"gold_weight": pmo_weights.get("net_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["metal_and_finding_weight"] / count
							if bom_weights
							else 0,
							"chain_weight": pmo_weights.get("finding_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["finding_weight_"] / count
							if bom_weights
							else 0,
							"other_weight": pmo_weights.get("other_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["other_weight"] / count
							if bom_weights
							else 0,
							"stone_weight": pmo_weights.get("gemstone_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["gemstone_weight"] / count
							if bom_weights
							else 0,
							"diamond_weight": pmo_weights.get("diamond_weight") / count
							if row.parent_manufacturing_order
							else bom_weights["diamond_weight"] / count
							if bom_weights
							else 0,
							"diamond_pcs": cint(diamond_pcs) / count
							if count > 1
							else cint(diamond_pcs),
							"stone_pcs": cint(stone_pcs) / count
							if count > 1
							else cint(stone_pcs),
							"parent_manufacturing_order": row.parent_manufacturing_order,
							"manufacturing_work_order": row.manufacturing_work_order,
							"supply_raw_material": bool(
								row.parent_manufacturing_order
								or row.manufacturing_work_order
							),
							"metal_touch": metal_touch,
							"metal_colour": metal_colour,
							"category": row.category,
							"sub_category": row.sub_category,
						}
					)

		elif self.service_type in ["Fire Assy Service", "XRF Services"]:
			if self.manufacturer:
				manufacturer = self.manufacturer
			else:
				manufacturer = frappe.defaults.get_user_default("manufacturer")
			if not manufacturer:
				frappe.throw("Set manufacturer in session defaults")
			# pure_item = frappe.db.get_value("Manufacturing Setting", self.company, "pure_gold_item")
			pure_item = frappe.db.get_value(
				"Manufacturing Setting",
				{"manufacturer": self.manufacturer},
				"pure_gold_item",
			)
			if not pure_item:
				# frappe.throw(_("Please mention Pure Item in Manufacturing Setting"))
				frappe.throw(_("Select Manufacturer in session defaults or in Filed"))

			# Normalised through _slip_key: current trees carry no Main Slip at all, so a raw
			# [main_slip, tree_no] comparison mixes None and "" and appends the same group twice.
			existing_data = {_slip_key(row) for row in self.exploded_product_details}

			for row in self.product_details:
				# Resolved unconditionally: loss_item used to be set only on the save that
				# created the exploded rows, so re-saving an existing document left it blank
				# and the loss calculation had nothing to key on.
				loss_item = get_item_loss_item(self.company, row.item_code, "M")
				if _slip_key(row) not in existing_data:
					existing_data.add(_slip_key(row))
					exploded_product_details.append(
						{
							"item_code": row.item_code,
							"main_slip": row.main_slip,
							"tree_no": row.tree_no,
						}
					)
					if self.service_type == "Fire Assy Service":
						exploded_product_details.append(
							{
								"item_code": pure_item,
								"main_slip": row.main_slip,
								"tree_no": row.tree_no,
							}
						)
					exploded_product_details.append(
						{
							"item_code": loss_item,
							"main_slip": row.main_slip,
							"tree_no": row.tree_no,
						}
					)
				row.loss_item = loss_item
				row.pure_item = pure_item

		for row in exploded_product_details:
			self.append("exploded_product_details", row)

	@frappe.whitelist()
	def get_item_from_tree_no(self, tree_no):
		"""Resolve a scanned Tree Number to its metal item (and legacy Main Slip, if any).

		Trees come from two eras and only one of them involves a Main Slip:

		* **Current** — ``employee_ir/doc_events/tree_casting.create_tree_on_issue`` mints the
		  Tree Number straight off the casting Employee IR and copies the metal attributes onto
		  it. There is no Main Slip anywhere in that flow, so resolving through one can only ever
		  fail. These are the trees operators actually scan.
		* **Legacy** — ``Main Slip.before_insert`` used to insert a bare Tree Number carrying
		  nothing but ``company``. Those need the slip to supply the metal.

		Resolution therefore reads the tree first and only falls back to Main Slip when the tree
		itself carries no metal. The fallback is *not* restricted to submitted slips: every other
		consumer of Main Slip in the app works against draft ("In Use") slips, so requiring
		``docstatus == 1`` rejected the live ones.
		"""
		from jewellery_erpnext.utils import get_item_from_attribute

		tree = frappe.db.get_value(
			"Tree Number",
			tree_no,
			["name", "metal_type", "metal_touch", "metal_purity", "metal_colour"],
			as_dict=1,
		)
		if not tree:
			frappe.throw(
				_("Tree No {0} does not exist.").format(frappe.bold(tree_no)),
				title=_("Invalid Tree No"),
			)

		main_slip = None
		metal = tree if tree.metal_touch else None

		if not metal:
			# `tree_number` is not unique on Main Slip; order so the same scan always resolves
			# to the same slip.
			legacy = frappe.get_all(
				"Main Slip",
				filters={"tree_number": tree_no},
				fields=[
					"name",
					"metal_type",
					"metal_touch",
					"metal_purity",
					"metal_colour",
				],
				order_by="modified desc",
				limit=1,
			)
			if legacy:
				main_slip = legacy[0].name
				metal = legacy[0]

		if not metal or not metal.metal_touch:
			frappe.throw(
				_(
					"Tree No {0} carries no metal details, and no Main Slip was found for it. "
					"Set the metal type / touch / purity / colour on the Tree Number before scanning it."
				).format(frappe.bold(tree_no)),
				title=_("Metal Details Missing"),
			)

		item_code = get_item_from_attribute(
			metal.metal_type,
			metal.metal_touch,
			metal.metal_purity,
			metal.metal_colour,
		)
		if not item_code:
			# The tree's own ledger is the metal that was physically issued onto it — a better
			# answer than a blank row when no matching Item variant exists.
			item_code = frappe.db.get_value(
				"Tree Material Detail",
				{
					"parent": tree_no,
					"parenttype": "Tree Number",
					"item_code": ("like", "M-%"),
				},
				"item_code",
				order_by="idx asc",
			)
		if not item_code:
			frappe.throw(
				_("No metal Item found for Tree No {0} ({1} {2} {3} {4}).").format(
					frappe.bold(tree_no),
					metal.metal_type,
					metal.metal_touch,
					metal.metal_purity,
					metal.metal_colour,
				),
				title=_("Metal Item Not Found"),
			)

		return {
			"main_slip": main_slip or "",
			"item_code": item_code,
		}


def create_stock_entry(doc):
	if doc.type == "Issue" or doc.service_type in [
		"Hall Marking Service",
		"Diamond Certificate service",
	]:
		se_doc = frappe.new_doc("Stock Entry")
		se_doc.stock_entry_type = get_stock_entry_type(doc.service_type, doc.type)
		se_doc.company = doc.company
		se_doc.product_certification = doc.name
		se_doc.auto_created = 1
		warehouse_type = "Manufacturing"
		# Fire Assy / XRF issue loose metal for assay, so they still source the department's Raw
		# Material warehouse. Everything else (Hall Marking, Diamond Certificate) moves finished
		# pieces and belongs in the department's WO warehouse.
		s_warehouse = None
		if doc.service_type in ["Fire Assy Service", "XRF Services"]:
			s_warehouse = frappe.db.exists(
				"Warehouse",
				{
					"department": doc.department,
					"warehouse_type": "Raw Material",
					"is_group": 0,
					"disabled": 0,
				},
			)
		# The fallback is the WO warehouse, not "any warehouse of the department" -- the
		# Product Certification departments carry no Raw Material warehouse at all, so the old
		# arbitrary fallback is what actually fired there.
		if not s_warehouse:
			s_warehouse = _department_wo_warehouse(doc.department)

		company_abbr = frappe.get_cached_value("Company", doc.company, "abbr") or ""
		t_warehouse_mwo = frappe.db.get_value(
			"Warehouse",
			{
				"company": doc.company,
				"subcontractor": doc.supplier,
				"name": ["like", f"%WIP WH - {company_abbr}%"],
				"is_group": 0,
				"disabled": 0,
			},
			"name",
		)
		if not t_warehouse_mwo:
			t_warehouse_mwo = frappe.db.exists(
				"Warehouse",
				{
					"company": doc.company,
					"subcontractor": doc.supplier,
					"name": ["like", "%WIP%"],
					"is_group": 0,
					"disabled": 0,
				},
			)
		if not t_warehouse_mwo:
			t_warehouse_mwo = frappe.db.exists(
				"Warehouse",
				{
					"company": doc.company,
					"subcontractor": doc.supplier,
					"is_group": 0,
					"disabled": 0,
				},
			)

		t_warehouse_serial = frappe.db.exists(
			"Warehouse",
			{
				"company": doc.company,
				"subcontractor": doc.supplier,
				"warehouse_type": warehouse_type,
				"is_group": 0,
				"disabled": 0,
			},
		)
		if not t_warehouse_serial:
			t_warehouse_serial = frappe.db.exists(
				"Warehouse",
				{
					"company": doc.company,
					"subcontractor": doc.supplier,
					"is_group": 0,
					"disabled": 0,
				},
			)

		added_mwo = []
		added_serial = []
		is_fire_assy_xrf = doc.service_type in ["Fire Assy Service", "XRF Services"]
		for row in doc.exploded_product_details:
			common_order = (
				row.parent_manufacturing_order or row.manufacturing_work_order
			)
			if row.supply_raw_material and common_order not in added_mwo:
				get_stock_item_against_mwo(
					se_doc, doc, row, s_warehouse, t_warehouse_mwo
				)
				added_mwo.append(common_order)
			else:
				# For Fire Assy / XRF services, exploded rows may lack
				# serial_no and tree_no but must still be processed when
				# they carry a positive gross_weight.
				if not is_fire_assy_xrf:
					if (
						not row.serial_no or row.serial_no in added_serial
					) and not row.tree_no:
						continue
				else:
					# Skip loss rows (gross_weight == 0) for Fire Assy/XRF
					if row.gross_weight <= 0:
						continue

				if row.serial_no:
					added_serial.append(row.serial_no)
				if row.gross_weight > 0:
					# No Serial No.warehouse override: the source is the department warehouse
					# resolved above, and validate_serial_warehouse_department has already
					# proved every serial is sitting there. Sourcing from wherever the serial
					# happened to be is what leaked issues out of the department -- and, across
					# companies, out of the company ledger too.
					source_wh = (
						s_warehouse if doc.type == "Issue" else t_warehouse_serial
					)

					se_doc.append(
						"items",
						{
							"item_code": row.item_code,
							"serial_no": row.serial_no,
							"qty": 1 if row.serial_no else row.gross_weight,
							"s_warehouse": source_wh,
							"t_warehouse": t_warehouse_serial
							if doc.type == "Issue"
							else s_warehouse,
							"Inventory_type": "Regular Stock",
							"reference_doctype": "Serial No",
							"reference_docname": row.serial_no,
							"serial_and_batch_bundle": None,
							"use_serial_batch_fields": True,
							"gross_weight": row.gross_weight,
						},
					)
		if not se_doc.items:
			frappe.throw(_("No item found for Repack"))
		se_doc.flags.throw_batch_error = True
		se_doc.inventory_type = "Regular Stock"
		se_doc.save()
		se_doc.submit()
		frappe.msgprint(_("Stock Entry created"))
	elif doc.type == "Receive" and doc.service_type in [
		"Fire Assy Service",
		"XRF Services",
	]:
		create_material_receipt_for_certification(doc)


def get_stock_entry_type(txn_type, purpose):
	if purpose == "Issue":
		if txn_type == "Hall Marking Service":
			return "Material Issue for Hallmarking"
		else:
			return "Material Issue for Certification"
	else:
		if txn_type == "Hall Marking Service":
			return "Material Receipt for Hallmarking"
		else:
			return "Material Receipt for Certification"


# def get_stock_item_against_mwo(se_doc, doc, row, s_warehouse, t_warehouse):
# 	if doc.type == "Issue":
# 		target_wh = frappe.get_value(
# 			"Warehouse",
# 			{"disabled": 0, "department": doc.department, "warehouse_type": "Manufacturing"},
# 			"name",
# 		)
# 		filters = [
# 			["Stock Entry MOP Item", "manufacturing_operation", "is", "set"],
# 			["Stock Entry MOP Item", "t_warehouse", "=", target_wh],
# 			["Stock Entry MOP Item", "employee", "is", "not set"],
# 		]
# 		if row.manufacturing_work_order:
# 			filters += (
# 				["Stock Entry MOP Item", "custom_manufacturing_work_order", "=", row.manufacturing_work_order],
# 			)
# 			latest_mop = frappe.db.get_value(
# 				"Manufacturing Work Order", row.manufacturing_work_order, "manufacturing_operation"
# 			)
# 			if latest_mop:
# 				filters += [
# 					["Stock Entry MOP Item", "manufacturing_operation", "=", latest_mop],
# 				]
# 		elif row.parent_manufacturing_order:
# 			filters += (
# 				[
# 					"Stock Entry MOP Item",
# 					"custom_parent_manufacturing_order",
# 					"=",
# 					row.parent_manufacturing_order,
# 				],
# 			)
# 			mwo = frappe.db.get_value(
# 				"Manufacturing Work Order",
# 				{"manufacturing_order": row.parent_manufacturing_order, "is_finding_mwo": 0, "docstatus": 1},
# 			)
# 			if mwo:
# 				latest_mop = frappe.db.get_value("Manufacturing Work Order", mwo, "manufacturing_operation")
# 				if latest_mop:
# 					filters += [
# 						["Stock Entry MOP Item", "manufacturing_operation", "=", latest_mop],
# 					]
# 	else:
# 		filters = [["Stock Entry", "product_certification", "=", doc.receive_against]]
# 		if row.manufacturing_work_order:
# 			filters += [
# 				["Stock Entry MOP Item", "reference_docname", "=", row.manufacturing_work_order],
# 				["Stock Entry MOP Item", "reference_doctype", "=", "Manufacturing Work Order"],
# 			]
# 		elif row.parent_manufacturing_order:
# 			filters += [
# 				["Stock Entry MOP Item", "reference_docname", "=", row.parent_manufacturing_order],
# 				["Stock Entry MOP Item", "reference_doctype", "=", "Parent Manufacturing Order"],
# 			]
# 	stock_entries = frappe.get_all(
# 		"Stock Entry",
# 		filters=filters,
# 		fields=[
# 			"`tabStock Entry MOP Item`.item_code",
# 			"`tabStock Entry MOP Item`.qty",
# 			"`tabStock Entry MOP Item`.batch_no",
# 		],
# 		join="right join",
# 	)
# 	if len(stock_entries) < 1:
# 		frappe.msgprint(_("Row {0} : No Stock entry Found against the Order").format(row.idx))

# 	for item in stock_entries:
# 		se_doc.append(
# 			"items",
# 			{
# 				"item_code": item.item_code,
# 				"qty": item.qty,
# 				"s_warehouse": s_warehouse if doc.type == "Issue" else t_warehouse,
# 				"t_warehouse": t_warehouse if doc.type == "Issue" else s_warehouse,
# 				"Inventory_type": "Regular Stock",
# 				"reference_doctype": "Manufacturing Work Order"
# 				if row.manufacturing_work_order
# 				else "Parent Manufacturing Order",
# 				"reference_docname": row.manufacturing_work_order
# 				if row.manufacturing_work_order
# 				else row.parent_manufacturing_order,
# 				"use_serial_batch_fields": True,
# 				"batch_no": item.get("batch_no"),
# 			},
# 		)


def _pd_reference(row):
	"""The order a Product Details row's Stock Entry lines are stamped with.

	``create_stock_entry`` writes ``reference_docname`` as "MWO when set, else PMO", so
	the Issue SE lines can be matched back to the rows that produced them.
	"""
	return row.get("manufacturing_work_order") or row.get("parent_manufacturing_order")


def _pd_common_order(row):
	"""The PMO-else-MWO key ``create_stock_entry`` groups its stock-entry calls on."""
	return row.get("parent_manufacturing_order") or row.get("manufacturing_work_order")


def _receive_fraction_by_reference(doc, common_order):
	"""``{reference_docname: fraction}`` — how much of each order this Receive books.

	Scoped to one ``common_order`` (the PMO-else-MWO key ``create_stock_entry`` dedupes
	its calls on) so that an Issue spanning two orders does not append every Issue SE
	line once per call.

	1.0 means "the whole outstanding issue for that reference"; anything less prorates
	the Issue SE lines. References absent from the map are not on this receipt.
	"""
	receiving = defaultdict(float)
	for pd in doc.product_details:
		reference = _pd_reference(pd)
		if reference and _pd_common_order(pd) == common_order:
			receiving[reference] += flt(pd.total_weight)

	if not receiving:
		return {}

	issued = defaultdict(float)
	for pd in frappe.get_all(
		"Product Details",
		filters={"parent": doc.receive_against, "parenttype": "Product Certification"},
		fields=[
			"manufacturing_work_order",
			"parent_manufacturing_order",
			"total_weight",
		],
	):
		reference = _pd_reference(pd)
		if reference:
			issued[reference] += flt(pd.total_weight)

	fractions = {}
	for reference, weight in receiving.items():
		issued_weight = issued.get(reference)
		# No issued weight to divide by (weights are optional on HM/DCS rows) — the row
		# is on this receipt, so treat it as a full receipt of its reference.
		fractions[reference] = (weight / issued_weight) if issued_weight else 1.0
	return fractions


def _outstanding_issue_qty(doc, issue_se):
	"""``{(reference, item_code, batch_no): qty}`` still to come back from the supplier.

	Issued qty minus everything already drawn by SUBMITTED Receive documents booked
	against the same Issue — the cap that stops repeated partial receipts from
	over-returning stock.
	"""
	outstanding = defaultdict(float)
	for item in frappe.get_all(
		"Stock Entry Detail",
		filters={"parent": issue_se},
		fields=["item_code", "qty", "batch_no", "reference_docname"],
	):
		outstanding[
			(item.get("reference_docname"), item.item_code, item.get("batch_no"))
		] += flt(item.qty)

	prior_receives = frappe.get_all(
		"Product Certification",
		filters={
			"receive_against": doc.receive_against,
			"type": "Receive",
			"docstatus": 1,
			"name": ["!=", doc.name],
		},
		pluck="name",
	)
	if prior_receives:
		for item in frappe.get_all(
			"Stock Entry Detail",
			filters={
				"parent": [
					"in",
					frappe.get_all(
						"Stock Entry",
						filters={
							"product_certification": ["in", prior_receives],
							"docstatus": 1,
						},
						pluck="name",
					)
					or [""],
				]
			},
			fields=["item_code", "qty", "batch_no", "reference_docname"],
		):
			outstanding[
				(item.get("reference_docname"), item.item_code, item.get("batch_no"))
			] -= flt(item.qty)

	return outstanding


def _assert_warehouse_in_department(doc, warehouse, item_code, idx):
	"""A raw-material line must source from the certification's own department.

	``resolve_and_validate`` answers "where is this stock reserved", which is the right question
	for batch/SRE correctness but says nothing about department. Left unchecked it issued metal
	and diamonds straight out of Diamond Setting, Casting and Model Making WIP warehouses on
	Product Certification documents.

	This throws rather than silently substituting the department's WO warehouse: if the stock is
	genuinely still in another department, the missing transfer is the bug, and forcing the
	warehouse here would only book it negative.
	"""
	if not warehouse or not doc.department:
		return

	wh = frappe.db.get_value(
		"Warehouse", warehouse, ["department", "company"], as_dict=1
	)
	if not wh:
		return
	if (wh.department or None) == (doc.department or None):
		return

	frappe.throw(
		_(
			"Row {0}: Item {1} is reserved in {2}, which belongs to department {3}, "
			"not {4}. Transfer it into {5} before issuing."
		).format(
			idx,
			frappe.bold(item_code),
			frappe.bold(warehouse),
			frappe.bold(wh.department or "-"),
			frappe.bold(doc.department),
			frappe.bold(_department_wo_warehouse(doc.department)),
		),
		title=_("Raw Material Not In Department Warehouse"),
	)


def get_stock_item_against_mwo(se_doc, doc, row, s_warehouse, t_warehouse):
	from jewellery_erpnext.jewellery_erpnext.doctype.mop_log.mop_log import (
		get_current_mop_balance_rows,
	)

	if doc.type == "Issue":
		# --- Resolve the MWO and its latest MOP ---
		mwo_name = row.manufacturing_work_order
		if not mwo_name and row.parent_manufacturing_order:
			mwo_name = frappe.db.get_value(
				"Manufacturing Work Order",
				{
					"manufacturing_order": row.parent_manufacturing_order,
					"is_finding_mwo": 0,
					"docstatus": 1,
				},
			)

		latest_mop = None
		if mwo_name:
			latest_mop = frappe.db.get_value(
				"Manufacturing Work Order", mwo_name, "manufacturing_operation"
			)

		# --- Get items from MOP Log balance (weights/qty from the PC dept MOP) ---
		mop_balance_rows = []
		if latest_mop:
			mop_balance_rows = get_current_mop_balance_rows(latest_mop)

		if not mop_balance_rows:
			frappe.msgprint(
				_(
					"Row {0}: No MOP balance found for the Manufacturing Work Order"
				).format(row.idx)
			)
			return

		# --- Find all MWOs linked to this PMO ---
		pmo_name = row.parent_manufacturing_order
		if not pmo_name and mwo_name:
			pmo_name = frappe.db.get_value(
				"Manufacturing Work Order", mwo_name, "manufacturing_order"
			)

		all_pmo_mwos = []
		if pmo_name:
			all_pmo_mwos = frappe.get_all(
				"Manufacturing Work Order",
				{"manufacturing_order": pmo_name, "docstatus": 1},
				pluck="name",
			)
		if not all_pmo_mwos and mwo_name:
			all_pmo_mwos = [mwo_name]

		# --- Find and cancel SREs, use SRE warehouse as source ---
		sre_cols = frappe.db.get_table_columns("Stock Reservation Entry")

		sre_list_1 = []
		if all_pmo_mwos:
			sre_filters = {"docstatus": 1}
			if "manufacturing_work_order" in sre_cols:
				sre_filters["manufacturing_work_order"] = ["in", all_pmo_mwos]
			sre_list_1 = frappe.db.get_all(
				"Stock Reservation Entry",
				filters=sre_filters,
				fields=[
					"name",
					"item_code",
					"warehouse",
					"reserved_qty",
					"delivered_qty",
				],
			)

		sre_list_2 = []
		item_codes = list(
			set(r.get("item_code") for r in mop_balance_rows if r.get("item_code"))
		)
		sales_order = None
		if pmo_name:
			sales_order = frappe.db.get_value(
				"Parent Manufacturing Order", pmo_name, "sales_order"
			)
		if sales_order and item_codes:
			sre_filters_2 = {
				"docstatus": 1,
				"voucher_type": "Sales Order",
				"voucher_no": sales_order,
				"item_code": ["in", item_codes],
			}
			# Only UNTAGGED (SO-level) reservations belong here. Every MWO on a Sales Order
			# shares voucher_no, and the metal/finding item codes are common across the whole
			# order -- without this scope a Product Certification of one PMO consumes the WIP
			# reservations of every other MWO on the SO (measured: 216 SREs across 115 MWOs
			# from a PC covering 2). MWO-tagged SREs for THIS PMO are already in sre_list_1.
			if "manufacturing_work_order" in sre_cols:
				sre_filters_2["manufacturing_work_order"] = ["in", ["", None]]
			sre_list_2 = frappe.db.get_all(
				"Stock Reservation Entry",
				filters=sre_filters_2,
				fields=[
					"name",
					"item_code",
					"warehouse",
					"reserved_qty",
					"delivered_qty",
				],
			)

		# Deduplicate SREs by name
		seen_sres = set()
		sre_list = []
		for sre in sre_list_1 + sre_list_2:
			if sre.name not in seen_sres:
				sre_list.append(sre)
				seen_sres.add(sre.name)

		# --- Create stock entry items from MOP balance rows ---
		for balance_row in mop_balance_rows:
			item_code = balance_row.get("item_code")
			qty = (
				balance_row.get("qty_after_transaction_batch_based")
				or balance_row.get("qty_after_transaction")
				or 0
			)
			batch_no = balance_row.get("batch_no")

			if not item_code or qty <= 0:
				continue

			item_s_warehouse = (
				resolve_and_validate(
					item_code=item_code,
					qty=qty,
					batch_no=batch_no,
					mwo=mwo_name,
					mop=latest_mop,
				)
				or s_warehouse
			)
			_assert_warehouse_in_department(doc, item_s_warehouse, item_code, row.idx)

			# pcs is a stone count: carry the batch-based balance for
			# diamond/gemstone items only (prefix D/G, per FIELD_MAP in mop_log)
			# so metal/finding rows keep the meaningful default of 1.
			pcs = (
				cint(balance_row.get("pcs_after_transaction_batch_based") or 0)
				if item_code and item_code[0] in ("D", "G")
				else 0
			)

			item_row = {
				"item_code": item_code,
				"qty": qty,
				"s_warehouse": item_s_warehouse,
				"t_warehouse": t_warehouse,
				"Inventory_type": "Regular Stock",
				"reference_doctype": "Manufacturing Work Order"
				if row.manufacturing_work_order
				else "Parent Manufacturing Order",
				"reference_docname": row.manufacturing_work_order
				if row.manufacturing_work_order
				else row.parent_manufacturing_order,
				"use_serial_batch_fields": True,
				"batch_no": batch_no,
			}
			if pcs:
				item_row["pcs"] = pcs
			se_doc.append("items", item_row)

		# --- Consume the SREs (mark as Delivered) ---
		from jewellery_erpnext.jewellery_erpnext.doc_events.stock_entry import (
			consume_stock_reservation_entry,
		)

		bins_to_update = set()
		for sre in sre_list:
			try:
				frappe.clear_document_cache("Bin")
				sre_doc = frappe.get_doc("Stock Reservation Entry", sre.name)
				consume_stock_reservation_entry(sre_doc, update_bin=False)
				if sre_doc.item_code and sre_doc.warehouse:
					bins_to_update.add((sre_doc.item_code, sre_doc.warehouse))
				frappe.clear_document_cache("Bin")
			except Exception:
				frappe.log_error(
					title=f"Failed to consume SRE {sre.name} during Product Certification",
					message=frappe.get_traceback(),
				)

		if bins_to_update:
			from erpnext.stock.utils import get_or_make_bin

			bin_names = sorted(
				list(set(get_or_make_bin(item, wh) for item, wh in bins_to_update))
			)
			for bin_name in bin_names:
				bin_doc = frappe.get_cached_doc("Bin", bin_name)
				bin_doc.update_reserved_stock()

		if sre_list:
			frappe.clear_cache()

	else:
		# --- Receive type: get items from the Issue stock entry ---
		issue_se = frappe.db.get_value(
			"Stock Entry",
			{"product_certification": doc.receive_against, "docstatus": 1},
			"name",
		)

		if not issue_se:
			frappe.msgprint(
				_("Row {0}: No Issue Stock Entry found for {1}").format(
					row.idx, doc.receive_against
				)
			)
			return

		# Get items from the issue stock entry
		issue_items = frappe.db.get_all(
			"Stock Entry Detail",
			filters={"parent": issue_se},
			fields=[
				"item_code",
				"qty",
				"pcs",
				"batch_no",
				"s_warehouse",
				"t_warehouse",
				"reference_doctype",
				"reference_docname",
			],
		)

		# A partial receipt must draw only its own share. Restrict the Issue SE lines to
		# the orders THIS receipt covers, prorate by how much of each order's issued
		# weight is being booked now, and cap on what earlier receipts left outstanding.
		common_order = _pd_common_order(row)
		fractions = _receive_fraction_by_reference(doc, common_order)
		outstanding = _outstanding_issue_qty(doc, issue_se)
		eps = pending_eps()
		precision = se_precision()
		has_reference = any(item.get("reference_docname") for item in issue_items)

		for item in issue_items:
			reference = item.get("reference_docname")
			if has_reference and reference not in fractions:
				continue

			key = (reference, item.item_code, item.get("batch_no"))
			available = outstanding.get(key, flt(item.qty))
			if available <= eps:
				continue

			fraction = fractions.get(reference, 1.0)
			if fraction >= 1 - eps:
				# Receiving this reference in full — take everything still outstanding, so
				# repeated partial receipts reconcile exactly instead of stranding
				# rounding dust in the supplier warehouse.
				qty = available
			else:
				qty = min(flt(flt(item.qty) * fraction, precision), available)

			if qty <= eps:
				continue

			item_row = {
				"item_code": item.item_code,
				"qty": qty,
				"s_warehouse": item.t_warehouse,  # Issue's target becomes Receive's source
				"t_warehouse": s_warehouse,  # Department warehouse as target for receive
				"Inventory_type": "Regular Stock",
				# Mirror the Issue line's own reference so the next partial receipt can
				# match its outstanding on the same key. Falls back to the exploded row
				# for legacy Issue entries that carry no reference.
				"reference_doctype": item.get("reference_doctype")
				or (
					"Manufacturing Work Order"
					if row.manufacturing_work_order
					else "Parent Manufacturing Order"
				),
				"reference_docname": reference
				or (
					row.manufacturing_work_order
					if row.manufacturing_work_order
					else row.parent_manufacturing_order
				),
				"use_serial_batch_fields": True,
				"batch_no": item.get("batch_no"),
			}
			# Carry the corrected pcs from the Issue SE (diamond/gemstone rows),
			# scaled to the share being received.
			if item.get("pcs"):
				pcs = (
					cint(item.get("pcs"))
					if fraction >= 1 - eps or not flt(item.qty)
					else cint(flt(item.get("pcs")) * (qty / flt(item.qty)))
				)
				if pcs:
					item_row["pcs"] = pcs
			se_doc.append("items", item_row)


@frappe.whitelist()
def create_product_certification_receive(source_name, target_doc=None):
	if (
		frappe.db.get_value("Product Certification", source_name, "receive_status")
		== FULLY_RECEIVED
	):
		frappe.throw(
			_("{0} is already fully received.").format(frappe.bold(source_name))
		)

	eps = pending_eps()

	def set_missing_values(source, target):
		target.type = "Receive"

	def set_pending_weight(source, target, source_parent):
		# The Receive row carries what is still OUTSTANDING, not what was issued —
		# the operator edits it down further for a partial receipt.
		target.total_weight = flt(source.total_weight) - flt(source.received_weight)

	doc = get_mapped_doc(
		"Product Certification",
		source_name,
		{
			"Product Certification": {
				"doctype": "Product Certification",
				"field_map": {"name": "receive_against"},
				"field_no_map": ["date", "receive_status"],
			},
			# get_mapped_doc auto-copies any same-named child table that has no explicit
			# map, so this has to say "ignore" out loud: a partial receipt must not
			# inherit the exploded rows (and full-issue weights) of items it is not
			# receiving. get_exploded_table rebuilds the table from the rows that
			# actually land on this document.
			"Exploded Product Details": {
				"doctype": "Exploded Product Details",
				"ignore": True,
			},
			"Product Details": {
				"doctype": "Product Details",
				"field_map": {"name": "issue_row"},
				"field_no_map": ["received_weight", "pending_weight"],
				"condition": lambda d: (
					flt(d.total_weight) - flt(d.received_weight) > eps
				),
				"postprocess": set_pending_weight,
			},
		},
		target_doc,
		set_missing_values,
		ignore_permissions=True,
	)

	return doc


def add_to_serial_no(serial_no, doc, row):
	serial_doc = frappe.get_doc("Serial No", serial_no)
	existing_data = [huild.huid for huild in serial_doc.huid]
	if row.huid and row.huid not in existing_data:
		serial_doc.append("huid", {"huid": row.huid, "date": doc.date})
	serial_doc.save()


def deferred_po_bom(pc_name):
	# create_po / update_bom_details come from the module-level import — the local
	# re-import this used to carry shadowed them and left both flagged as unused.
	pc = frappe.get_doc("Product Certification", pc_name)
	create_po(pc)
	update_bom_details(pc)
