import copy
import json
import frappe
import erpnext
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
	get_auto_batch_nos,
)
from frappe import _
from frappe.query_builder import Case
from frappe.query_builder.functions import Locate
from frappe.utils import flt, nowtime, now

from erpnext.stock.serial_batch_bundle import SerialNoValuation
from jewellery_erpnext.jewellery_erpnext.customization.serial_and_batch_bundle.serial_and_batch_bundle import CustomBatchNoValuation
from erpnext.stock.utils import get_valuation_method, _get_fifo_lifo_rate, get_serial_nos_data

from jewellery_erpnext.jewellery_erpnext.customization.stock_entry.doc_events.subcontracting_utils import (
	create_subcontracting_doc,
)
from jewellery_erpnext.utils import get_item_from_attribute
from frappe.query_builder.functions import CombineDatetime, Sum


def validate_inventory_dimention(self):
	pass
	# pmo_customer_data = frappe._dict()
	# manufacturer_data = frappe._dict()
	# for row in self.items:
	# 	pmo_list = row.custom_parent_manufacturing_order or self.manufacturing_order
	# 	if not pmo_list:
	# 		continue
	# 	for pmo in pmo_list.split(","):
	# 		if not pmo_customer_data.get(pmo):
	# 			pmo_customer_data[pmo] = frappe.db.get_value(
	# 				"Parent Manufacturing Order",
	# 				pmo,
	# 				[
	# 					"is_customer_gold",
	# 					"is_customer_diamond",
	# 					"is_customer_gemstone",
	# 					"is_customer_material",
	# 					"customer",
	# 					"manufacturer",
	# 				],
	# 				as_dict=1,
	# 			)
	# 		pmo_data = pmo_customer_data.get(pmo)
	# 		if not manufacturer_data.get(pmo_data["manufacturer"]):
	# 			manufacturer_data[pmo_data["manufacturer"]] = frappe.db.get_value(
	# 				"Manufacturer",
	# 				pmo_data.get("manufacturer"),
	# 				"custom_allow_regular_goods_instead_of_customer_goods",
	# 			)

	# 		allow_customer_goods = manufacturer_data.get(pmo_data.get("manufacturer"))

	# 		if (
	# 			row.inventory_type in ["Customer Goods", "Customer Stock"]
	# 			and pmo_data.get("customer") != row.customer
	# 		):
	# 			frappe.throw(_("Only {0} allowed in Stock Entry").format(pmo_data.get("customer")))
	# 		else:
	# 			variant_mapping = {
	# 				"M": "is_customer_gold",
	# 				"F": "is_customer_gold",
	# 				"D": "is_customer_diamond",
	# 				"G": "is_customer_gemstone",
	# 				"O": "is_customer_material",
	# 			}

	# 			if row.custom_variant_of in variant_mapping:
	# 				customer_key = variant_mapping[row.custom_variant_of]
	# 				if pmo_data.get(customer_key) and row.inventory_type not in [
	# 					"Customer Goods",
	# 					"Customer Stock",
	# 				]:
	# 					if allow_customer_goods:
	# 						frappe.msgprint(_("Can not use regular stock inventory for Customer provided Item"))
	# 					else:
	# 						frappe.throw(_("Can not use regular stock inventory for Customer provided Item"))
	# 				elif not pmo_data.get(customer_key) and row.inventory_type in [
	# 					"Customer Goods",
	# 					"Customer Stock",
	# 				]:
	# 					if allow_customer_goods:
	# 						frappe.msgprint(_("Can not use Customer Goods inventory for non provided customer Item"))
	# 					else:
	# 						frappe.throw(_("Can not use Customer Goods inventory for non provided customer Item"))

# chnages in this function
def get_fifo_batches(self, row):
	rows_to_append = []
	row.batch_no = None
	total_qty = row.qty
	existing_updated = False

	msl = self.get("main_slip") or self.get("to_main_slip")
	warehouse = row.get("s_warehouse") or self.get("source_warehouse")
	if msl and frappe.db.get_value("Main Slip", msl, "raw_material_warehouse") == row.s_warehouse:
		main_slip = self.main_slip or self.to_main_slip
		batch_data = get_batch_data_from_msl(row.item_code, main_slip, row.s_warehouse)
	else:
		posting_date = self.get("posting_date") or self.get("date")
		batch_data = get_auto_batch_nos(
			frappe._dict(
				{
					"posting_date": posting_date,
					"item_code": row.item_code,
					"warehouse": warehouse,
					"qty": row.qty,
				}
			)
		)

	customer_item_data = frappe._dict({})
	manufacturer_data = frappe._dict({})
	if row.get("custom_parent_manufacturing_order"):
		customer_item_data = frappe.db.get_value(
			"Parent Manufacturing Order",
			row.custom_parent_manufacturing_order,
			[
				"is_customer_gold",
				"is_customer_diamond",
				"is_customer_gemstone",
				"is_customer_material",
				"customer",
				"manufacturer",
			],
			as_dict=1,
		)
	# if not manufacturer_data.get(customer_item_data.get("manufacturer")):
	# 	manufacturer_data[customer_item_data.get("manufacturer")] = frappe.db.get_value(
	# 		"Manufacturer",
	# 		customer_item_data.get("manufacturer"),
	# 		"custom_allow_regular_goods_instead_of_customer_goods",
	# 	)
	
	# new Chnage Start
	if not manufacturer_data.get(customer_item_data.get("customer")):
		manufacturer_data[customer_item_data.get("customer")] = frappe.db.get_value(
			"Customer",
			customer_item_data.get("Customer"),
			"custom_allow_regular_goods_instead_of_customer_goods",
		)
	# new Chnage End

	# new Chnage Start
	# allow_customer_goods = manufacturer_data.get(customer_item_data.get("manufacturer"))
	allow_customer_goods = manufacturer_data.get(customer_item_data.get("customer"))
	# new Chnage End
	variant_to_customer_key = {
		"M": "is_customer_gold",
		"F": "is_customer_gold",
		"D": "is_customer_diamond",
		"G": "is_customer_gemstone",
		"O": "is_customer_material",
	}

	if (
		row.get("custom_variant_of")
		and row.custom_variant_of in variant_to_customer_key
		and customer_item_data.get(variant_to_customer_key[row.custom_variant_of])
	):
		row.inventory_type = "Customer Goods"
		row.customer = customer_item_data.customer

	if not row.inventory_type:
		row.inventory_type = "Regular Stock"
	for batch in batch_data:
		if (
			row.inventory_type in ["Customer Goods", "Customer Stock"]
			and frappe.db.get_value("Batch", batch.batch_no, "custom_inventory_type") == row.inventory_type
			and frappe.db.get_value("Batch", batch.batch_no, "custom_customer") == row.customer
		):
			if total_qty > 0 and batch.qty > 0:
				if not existing_updated:
					row.db_set("qty", min(total_qty, batch.qty))
					if self.get("date"):
						row.db_set("batch", batch.batch_no)
					else:
						row.db_set("transfer_qty", row.qty)
						row.db_set("batch_no", batch.batch_no)
					total_qty -= batch.qty
					existing_updated = True
					rows_to_append.append(row.__dict__)
				else:
					temp_row = copy.deepcopy(row.__dict__)
					temp_row["name"] = None
					temp_row["idx"] = None
					temp_row["batch_no"] = batch.batch_no
					temp_row["transfer_qty"] = 0
					temp_row["qty"] = flt(min(total_qty, batch.qty), 4)
					rows_to_append.append(temp_row)
					total_qty -= batch.qty

		elif (
			row.inventory_type in ["Customer Goods", "Customer Stock"]
			and frappe.db.get_value("Batch", batch.batch_no, "custom_inventory_type") != row.inventory_type
			and allow_customer_goods == 1
		):
			if total_qty > 0 and batch.qty > 0:
				if not existing_updated:
					row.db_set("qty", min(total_qty, batch.qty))
					if self.get("date"):
						row.db_set("batch", batch.batch_no)
					else:
						row.db_set("transfer_qty", row.qty)
						row.db_set("batch_no", batch.batch_no)
					total_qty -= batch.qty
					existing_updated = True
					rows_to_append.append(row.__dict__)
				else:
					temp_row = copy.deepcopy(row.__dict__)
					temp_row["name"] = None
					temp_row["idx"] = None
					temp_row["batch_no"] = batch.batch_no
					temp_row["transfer_qty"] = 0
					temp_row["qty"] = flt(min(total_qty, batch.qty), 4)
					rows_to_append.append(temp_row)
					total_qty -= batch.qty

		elif row.inventory_type not in ["Customer Goods", "Customer Stock"]:
			if self.flags.only_regular_stock_allowed and frappe.db.get_value(
				"Batch", batch.batch_no, "custom_inventory_type"
			) in ["Customer Goods", "Customer Stock"]:
				continue

			if total_qty > 0 and batch.qty > 0:
				if not existing_updated:
					row.db_set("qty", min(total_qty, batch.qty))
					if self.get("date"):
						row.db_set("batch", batch.batch_no)
					else:
						row.db_set("transfer_qty", row.qty)
						row.db_set("batch_no", batch.batch_no)
					total_qty -= batch.qty
					existing_updated = True
					rows_to_append.append(row.__dict__)
				else:
					temp_row = copy.deepcopy(row.__dict__)
					temp_row["name"] = None
					temp_row["idx"] = None
					temp_row["batch_no"] = batch.batch_no
					temp_row["transfer_qty"] = 0
					temp_row["qty"] = flt(min(total_qty, batch.qty), 4)
					rows_to_append.append(temp_row)
					total_qty -= batch.qty

	if total_qty > 0:
		message = _("For <b>{0}</b> {1} is missing in <b>{2}</b>").format(
			row.item_code, flt(total_qty, 2), warehouse
		)
		if row.get("manufacturing_operation"):
			message += _("<br><b>Ref : {0}</b>").format(row.manufacturing_operation)
		if self.flags.throw_batch_error:
			frappe.throw(message)
			self.flags.throw_batch_error = False
		else:
			frappe.msgprint(message)

	# new Chnage Start
	if self.stock_entry_type == "Material Receive (WORK ORDER)":
		batch_rows = []
		key = (row.manufacturing_operation, row.item_code)

		# Fetch all matching batch rows for this operation & item
		mop_batches = frappe.get_all(
			"MOP Balance Table",
			filters={"parent": row.manufacturing_operation, "item_code": row.item_code},
			fields=["batch_no", "qty"],
			order_by="creation asc",
		)

		total_qty = row.qty
		existing_updated = False

		for batch in mop_batches:
			if total_qty <= 0:
				break

			if batch.qty <= 0:
				continue

			batch_qty = flt(min(total_qty, batch.qty), 4)

			if not existing_updated:
				# update current row
				row.batch_no = batch.batch_no
				row.qty = batch_qty
				row.transfer_qty = batch_qty
				row.idx = None
				row.name = None
				rows_to_append.append(row.__dict__)
				existing_updated = True
			else:
				temp_row = copy.deepcopy(row.__dict__)
				temp_row["name"] = None
				temp_row["idx"] = None
				temp_row["batch_no"] = batch.batch_no
				temp_row["qty"] = batch_qty
				temp_row["transfer_qty"] = batch_qty
				rows_to_append.append(temp_row)
			# temp_row = copy.deepcopy(row.__dict__)
			# temp_row["name"] = None
			# temp_row["idx"] = None
			# temp_row["batch_no"] = batch.batch_no
			# temp_row["qty"] = batch_qty
			# temp_row["transfer_qty"] = batch_qty
			

			total_qty -= batch_qty

		if total_qty > 0:
			frappe.msgprint(
				_("Not enough quantity in MOP batches for <b>{0}</b>. Missing: {1}").format(
					row.item_code, flt(total_qty, 2)
				)
			)

	# frappe.throw(f"{(rows_to_append[1:])}")
	if self.stock_entry_type == "Material Receive (WORK ORDER)":
		return rows_to_append[1:]
	else:
		return rows_to_append
	# new Chnage End


def get_batch_data_from_msl(item_code, main_slip, warehouse):
	batch_data = []
	msl_doc = frappe.get_doc("Main Slip", main_slip)

	if warehouse != msl_doc.raw_material_warehouse:
		frappe.msgprint(_("Please select batch manually for receving goods in Main Slip"))
		return batch_data

	for row in msl_doc.batch_details:
		if row.qty != row.consume_qty and row.item_code == item_code:
			batch_row = frappe._dict()
			batch_row.update({"batch_no": row.batch_no, "qty": row.qty - row.consume_qty})
			batch_data.append(batch_row)

	return batch_data


def create_repack_for_subcontracting(self, subcontractor, main_slip=None):
	if not subcontractor and main_slip:
		subcontractor = frappe.db.get_value("Main Slip", main_slip, "subcontractor")

	raw_warehouse = frappe.db.get_value(
		"Warehouse",
		{
			"disabled": 0,
			"company": self.company,
			"subcontractor": subcontractor,
			"warehouse_type": "Raw Material",
		},
	)
	mfg_warehouse = frappe.db.get_value(
		"Warehouse",
		{
			"disabled": 0,
			"company": self.company,
			"subcontractor": subcontractor,
			"warehouse_type": "Manufacturing",
		},
	)
	repack_raws = []
	receive = False
	for row in self.items:
		temp_raw = copy.deepcopy(row.__dict__)
		if row.t_warehouse == raw_warehouse:
			receive = True
			temp_raw["name"] = None
			temp_raw["idx"] = None
			repack_raws.append(temp_raw)
		elif row.s_warehouse == raw_warehouse and row.t_warehouse == mfg_warehouse:
			temp_raw["name"] = None
			temp_raw["idx"] = None
			repack_raws.append(temp_raw)

	if repack_raws:
		create_subcontracting_doc(self, subcontractor, self.department, repack_raws, main_slip, receive)


def validate_gross_weight_for_unpack(self):
	if self.stock_entry_type == "Repair Unpack":
		source_gr_wt = 0
		receive_gr_wt = 0
		for row in self.items:
			if row.s_warehouse:
				source_gr_wt += row.get("gross_weight") or 0
			elif row.t_warehouse:
				receive_gr_wt += row.get("gross_weight") or 0

		if flt(receive_gr_wt, 3) != flt(source_gr_wt, 3):
			frappe.throw(_("Gross weight does not match for source and target items"))


def validation_for_stock_entry_submission(self):
	for item in self.items:
		stock_reco = frappe.get_doc("Stock Reconciliation", {"set_warehouse": item.s_warehouse})
		if stock_reco.docstatus != 1:
			frappe.throw(
				_(
					"Please complete the Stock Reconciliation {0}  to Submit the Stock Entry".format_(
						stock_reco.name
					)
				)
			)


def set_employee(self):
	if self.stock_entry_type != "Material Transfer (WORK ORDER)":
		return

	if mop_details := frappe.db.get_value(
		"Manufacturing Operation", self.manufacturing_operation, ["status", "employee"], as_dict=1
	):
		if mop_details.status == "WIP":
			self.to_employee = mop_details.employee


def set_gross_wt(self):
	for row in self.items:
		if row.serial_no:
			gross_weight = frappe.db.get_value("Serial No", row.serial_no, "custom_gross_wt")
			row.gross_weight = gross_weight


def validate_warehouse(self):
	if self.stock_entry_type != "Material Transfer (WORK ORDER)":
		return
	if self.from_warehouse and self.to_warehouse:
		if self.from_warehouse == self.to_warehouse:
			frappe.throw(_("The source warehouse and the target warehouse cannot be the same."))

	for row in self.items:
		if row.s_warehouse == row.t_warehouse:
			frappe.throw(_("The source warehouse and the target warehouse cannot be the same."))


def get_incoming_rate(args, raise_error_if_no_rate=True):
	"""Get Incoming Rate based on valuation method"""
	from erpnext.stock.stock_ledger import get_previous_sle, get_valuation_rate

	if isinstance(args, str):
		args = json.loads(args)

	in_rate = None

	item_details = frappe.get_cached_value(
		"Item", args.get("item_code"), ["has_serial_no", "has_batch_no"], as_dict=1
	)

	use_moving_avg_for_batch = frappe.db.get_single_value("Stock Settings", "do_not_use_batchwise_valuation")

	if isinstance(args, dict):
		args = frappe._dict(args)

	if item_details and item_details.has_serial_no and args.get("serial_and_batch_bundle"):
		args.actual_qty = args.qty
		sn_obj = SerialNoValuation(
			sle=args,
			warehouse=args.get("warehouse"),
			item_code=args.get("item_code"),
		)

		return sn_obj.get_incoming_rate()

	elif (
		item_details
		and item_details.has_batch_no
		and args.get("serial_and_batch_bundle")
		and not use_moving_avg_for_batch
	):
		args.actual_qty = args.qty
		batch_obj = CustomBatchNoValuation(
			sle=args,
			warehouse=args.get("warehouse"),
			item_code=args.get("item_code"),
		)

		return batch_obj.get_incoming_rate()

	elif (args.get("serial_no") or "").strip() and not args.get("serial_and_batch_bundle"):
		args.actual_qty = args.qty
		args.serial_nos = get_serial_nos_data(args.get("serial_no"))

		sn_obj = SerialNoValuation(sle=args, warehouse=args.get("warehouse"), item_code=args.get("item_code"))

		return sn_obj.get_incoming_rate()
	elif args.get("batch_no") and not args.get("serial_and_batch_bundle") and not use_moving_avg_for_batch:
		args.actual_qty = args.qty
		args.batch_nos = frappe._dict({args.batch_no: args})

		batch_obj = CustomBatchNoValuation(
			sle=args,
			warehouse=args.get("warehouse"),
			item_code=args.get("item_code"),
		)

		return batch_obj.get_incoming_rate()
	else:
		valuation_method = get_valuation_method(args.get("item_code"))
		previous_sle = get_previous_sle(args)
		if valuation_method in ("FIFO", "LIFO"):
			if previous_sle:
				previous_stock_queue = json.loads(previous_sle.get("stock_queue", "[]") or "[]")
				in_rate = (
					_get_fifo_lifo_rate(previous_stock_queue, args.get("qty") or 0, valuation_method)
					if previous_stock_queue
					else None
				)
		elif valuation_method == "Moving Average":
			in_rate = previous_sle.get("valuation_rate")

	if in_rate is None:
		voucher_no = args.get("voucher_no") or args.get("name")
		in_rate = get_valuation_rate(
			args.get("item_code"),
			args.get("warehouse"),
			args.get("voucher_type"),
			voucher_no,
			args.get("allow_zero_valuation"),
			currency=erpnext.get_company_currency(args.get("company")),
			company=args.get("company"),
			raise_error_if_no_rate=raise_error_if_no_rate,
		)

	return flt(in_rate)


def rename_stock_entry_docs():
	"""Rename temporarily named docs using autoname options"""
	from frappe.model.naming import set_name_from_naming_options

	doctype = "Stock Entry"
	docs_to_rename = frappe.get_all(doctype, {"to_rename": "1"}, order_by="creation", limit=50000)

	for doc in docs_to_rename:
		doc.doctype = doctype
		oldname = doc.name
		set_name_from_naming_options(frappe.get_meta(doctype).autoname, doc)
		new_name = frappe.rename_doc(
			doctype,
			oldname,
			doc.name,
			force=True,
		)
