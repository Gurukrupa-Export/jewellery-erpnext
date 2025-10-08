# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt


import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, today

from jewellery_erpnext.utils import get_item_from_attribute

template = """
	<table class="table table-bordered table-hover" width="100%" style="border: 1px solid #d1d8dd;">
		<thead>
			<tr style = "text-align:center">
				<th style="border: 1px solid #d1d8dd; font-size: 11px;">Item Code</th>
				<th style="border: 1px solid #d1d8dd; font-size: 11px;">Qty</th>
				<th style="border: 1px solid #d1d8dd; font-size: 11px;">Pcs</th>
				<th style="border: 1px solid #d1d8dd; font-size: 11px;">Inventory Type</th>
				<th style="border: 1px solid #d1d8dd; font-size: 11px;">Customer</th>
			</tr>
		</thead>
		<tbody>
		{% for item in data %}
			<tr style = "text-align:center">
				<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.item_code }}</td>
				<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.qty }}</td>
				<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.pcs }}</td>
				<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.inventory_type }}</td>
				<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.customer }}</td>
			</tr>
		{% endfor %}
		</tbody>
	</table>"""
class Refining(Document):
	def validate(self):
		# self.check_overlap()

		self.refining_loss = self.gross_pure_weight - self.refined_fine_weight

		if not self.refining_department:
			frappe.throw(_("Please Select Refining Department"))

		if self.refining_type == "Recovery Material":

			if self.multiple_department and self.multiple_operation:
				frappe.throw(_("Chose any one For Multiple Operations or For Multiple Department"))

			if self.multiple_department:
				check_allocation(self, self.refining_department_detail)
			elif self.multiple_operation:
				check_allocation(self, self.refining_operation_detail)
		self.mop_balance_data =""
		self.metal_wt = 0
		self.gemstone_wt = 0
		self.diamond_wt = 0
		self.mop_balance_table_items = {}
		if self.refining_type == "Parent Manufacturing Order":
			if self.manufacturing_work_order:
				manufacturing_operations =[]
				throw_message = ""
				for row in self.manufacturing_work_order:
					row.manufacturing_operation =  frappe.db.get_value("Manufacturing Operation",{"manufacturing_work_order":row.manufacturing_work_order,"status":"Not Started"})
					if row.manufacturing_operation:
						manufacturing_operations.append(row.manufacturing_operation)
					if row.company != self.company:
						throw_message +=f"<br> Row {row.idx}  Manufacturing work order : <b>{row.manufacturing_work_order}</b> does not belong to Company: <b>{self.compay}</b> <br>"
					if self.refining_department != row.department:
						throw_message +=f"<br> Row {row.idx}  Manufacturing work order : <b>{row.manufacturing_work_order}</b> does not belong to Department: <b>{self.refining_department}</b> <br>"
				if throw_message:
					frappe.throw(throw_message)

				self.get_balance_table(manufacturing_operation=manufacturing_operations)
			else:
				if self.refined_gold:
					self.refined_gold =[]
			self.validate_weight_for_parent_manufacturing_order()
		self.set_fine_weight()
		self.set_gross_pure_weight()

	def on_submit(self):
		create_refining_entry(self)

	def check_overlap(self):

		if not self.multiple_operation:
			Refining = frappe.qb.DocType("Refining")

			# Build the conditions
			conditions = (
				(Refining.docstatus != 2)
				& (Refining.dustname == self.dustname)
				& (Refining.multiple_operation == self.multiple_operation)
				& (Refining.operation == self.operation)
				& (Refining.employee == self.employee)
				& (
					((self.date_from > Refining.date_from) & (self.date_from < Refining.date_to))
					| ((self.date_to > Refining.date_from) & (self.date_to < Refining.date_to))
					| ((self.date_from <= Refining.date_from) & (self.date_to >= Refining.date_to))
				)
			)
			# query
			query = (
				frappe.qb.from_(Refining)
				.select(Refining.name)
				.where((Refining.name != self.name) & conditions)
			)
			name = query.run()

			if name:
				frappe.throw(
					f"Document is overlapping with <b><a href='/app/refining/{name[0][0]}'>{name[0][0]}</a></b>"
				)
		else:
			if not self.refining_operation_detail:
				return

			Refining = frappe.qb.DocType("Refining")
			RefiningOperationDetail = frappe.qb.DocType("Refining Operation Detail")
			operation_list = [frappe.db.escape(row.operation) for row in self.refining_operation_detail]

			# Build the conditions
			conditions = (
				(Refining.docstatus != 2)
				& (Refining.dustname == self.dustname)
				& (Refining.multiple_operation == self.multiple_operation)
				& (RefiningOperationDetail.operation.isin(operation_list))
				& (
					((self.date_from >= Refining.date_from) & (self.date_from <= Refining.date_to))
					| ((self.date_to >= Refining.date_from) & (self.date_to <= Refining.date_to))
					| ((self.date_from <= Refining.date_from) & (self.date_to >= Refining.date_to))
				)
			)
			# query
			query = (
				frappe.qb.from_(Refining)
				.join(RefiningOperationDetail)
				.on(Refining.name == RefiningOperationDetail.parent)
				.select(Refining.name)
				.where((Refining.name != self.name) & conditions)
			)
			name = query.run()

			if name:
				frappe.throw(
					f"Document is overlapping with <b><a href='/app/refining/{name[0][0]}'>{name[0][0]}</b>"
				)

	def set_gross_pure_weight(self):
		gross_pure_weight = 0
		if self.refining_type == "Parent Manufacturing Order" and len(self.manufacturing_work_order) > 0:
			for row in self.manufacturing_work_order:
				if row.metal_weight:
					gross_pure_weight += row.metal_weight
		elif self.refining_type == "Serial Number" and len(self.refining_serial_no_detail) > 0:
			for row in self.refining_serial_no_detail:
				if row.pure_weight:
					gross_pure_weight += row.pure_weight
		self.gross_pure_weight = gross_pure_weight

	def set_fine_weight(self):
		total_pure_weight = 0
		if len(self.refined_gold) > 0:
			for row in self.refined_gold:
				if row.pure_weight:
					total_pure_weight += row.pure_weight
		self.refined_fine_weight = total_pure_weight

	@frappe.whitelist()
	def create_dust_receive_entry(self):
		if not self.multiple_operation and not self.multiple_department:
			if self.department:
				mr_se = dust_receipt_entry(self, "single")
				test_create_transfer_entry(self, "refining_department_detail", mr_se)
				# create_transfer_entry(self,"single")
			elif self.employee:
				mr_se = dust_receipt_entry(self, "single")
				test_create_transfer_entry(self, "refining_department_detail", mr_se)
				# create_transfer_entry(self,"single")
			else:
				frappe.throw(_("Please Select Department or Employee"))

		elif not self.multiple_operation and self.multiple_department:
			if len(self.refining_department_detail) > 0:
				mr_se = dust_receipt_entry(self, "refining_department_detail")
				test_create_transfer_entry(self, "refining_department_detail", mr_se)
			else:
				frappe.throw(_("Table Refining Department Detail Is Empty"))

		elif self.multiple_operation and not self.multiple_department:
			mr_se = dust_receipt_entry(self, "refining_operation_detail")
			test_create_transfer_entry(self, "refining_operation_detail", mr_se)

		return 1

	@frappe.whitelist()
	def get_linked_stock_entries(self):
		target_wh = self.refining_warehouse
		all_stock_entry = []
		for row in self.manufacturing_work_order:
			mwo = row.manufacturing_work_order
			mop = row.manufacturing_operation
			StockEntry = frappe.qb.DocType("Stock Entry")
			StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")

			query = (
				frappe.qb.from_(StockEntryDetail)
				.join(StockEntry)
				.on(StockEntryDetail.parent == StockEntry.name)
				.select(
					StockEntry.manufacturing_work_order,
					StockEntry.manufacturing_operation,
					StockEntryDetail.parent,
					StockEntryDetail.item_code,
					StockEntryDetail.item_name,
					StockEntryDetail.qty,
					StockEntryDetail.uom,
				)
				.where(
					(StockEntry.docstatus == 1)
					& (StockEntryDetail.t_warehouse == target_wh)
					& (StockEntry.manufacturing_operation == mop)
					& (StockEntry.manufacturing_work_order == mwo)
				)
			)
			data = query.run(as_dict=True)

			all_stock_entry += data
		if all_stock_entry:
			return frappe.render_template(
				"jewellery_erpnext/jewellery_erpnext/doctype/refining/refining.html", {"data": all_stock_entry}
			)

	def get_balance_table(self,manufacturing_operation):
		mop_balance_table = []
		if manufacturing_operation:
			mop_balance_table = frappe.get_all("MOP Balance Table",{"parent":["in",manufacturing_operation]},["item_code","sum(qty) as qty","sum(pcs) as pcs","inventory_type","customer"],group_by="item_code,inventory_type,customer",order_by ="item_code,inventory_type,customer")
			mop_balance_table_based_on_qty = frappe.get_all("MOP Balance Table",{"parent":["in",manufacturing_operation]},["item_code","sum(qty) as qty","sum(pcs) as pcs"],group_by="item_code",order_by ="item_code")
			mop_balance_table_based_on_qty = {value["item_code"]:value["qty"] for value in mop_balance_table_based_on_qty}
			mop_balance_table_items = frappe.get_all("MOP Balance Table",{"parent":["in",manufacturing_operation]},["distinct item_code"],pluck ="item_code")
			filter_wt = ""
			if len(manufacturing_operation) == 1:
				filter_wt += f"mop.parent = '{manufacturing_operation[0]}' "
			else:
				filter_wt += f" mop.parent in {tuple(manufacturing_operation)}"
			wt = frappe.db.sql(f"""
				SELECT sum( if(mop.item_code LIKE 'D%',mop.qty,0)) as diamond_wt,
				sum( if(mop.item_code LIKE 'G%',mop.qty,0) ) as gemstone_wt,
				sum( if( (mop.item_code LIKE 'M%')  or (mop.item_code LIKE 'F%') ,mop.qty,0) ) as metal_wt
				FROM `tabMOP Balance Table` as mop
				WHERE {filter_wt}
			""", as_dict=True )
			
			self.metal_wt = wt[0].get("metal_wt")
			self.gemstone_wt = wt[0].get("gemstone_wt")
			self.diamond_wt = wt[0].get("diamond_wt")
			html_content = frappe.render_template(
				template, {"data": mop_balance_table,}
			)
			self.mop_balance_data = html_content
			self.mop_balance_table_items = {"item_code":mop_balance_table_items,"qty":mop_balance_table_based_on_qty}
			mop_balance_table_items_based_on_item_code = frappe.db.sql(f"""
					SELECT
						COALESCE(
							CASE 
								WHEN mq.item_code LIKE 'M%' THEN mq.item_code
								WHEN mq.item_code LIKE 'F%' THEN (
									SELECT i.name 
									FROM `tabItem` i
									JOIN (
										SELECT parent,
											MAX(CASE WHEN attribute = 'Metal Colour' THEN attribute_value END) AS `Metal Colour`,
											MAX(CASE WHEN attribute = 'Metal Purity' THEN attribute_value END) AS `Metal Purity`,
											MAX(CASE WHEN attribute = 'Metal Touch'  THEN attribute_value END) AS `Metal Touch`,
											MAX(CASE WHEN attribute = 'Metal Type'   THEN attribute_value END) AS `Metal Type`
										FROM `tabItem Variant Attribute`
										WHERE attribute IN ('Metal Colour','Metal Purity','Metal Touch','Metal Type')
										and parent like 'M%'
										GROUP BY parent
									) ia0 ON ia0.parent = i.name
									WHERE i.name LIKE 'M%'
									AND ia.`Metal Colour` = ia0.`Metal Colour`
									AND ia.`Metal Purity` = ia0.`Metal Purity`
									AND ia.`Metal Touch`  = ia0.`Metal Touch`
									AND ia.`Metal Type`   = ia0.`Metal Type`
									LIMIT 1
								)
							END
						) AS item_code,
						SUM(mq.qty) AS qty
					FROM (
						SELECT mop.item_code, SUM(mop.qty) AS qty
						FROM `tabMOP Balance Table` AS mop
						WHERE {filter_wt}
						AND (mop.item_code LIKE 'M%' OR mop.item_code LIKE 'F%')
						GROUP BY mop.item_code
					) mq
					 JOIN (
						SELECT parent,
							MAX(CASE WHEN attribute = 'Metal Colour' THEN attribute_value END)   AS `Metal Colour`,
							MAX(CASE WHEN attribute = 'Metal Purity' THEN attribute_value END)   AS `Metal Purity`,
							MAX(CASE WHEN attribute = 'Metal Touch'  THEN attribute_value END)   AS `Metal Touch`,
							MAX(CASE WHEN attribute = 'Metal Type'   THEN attribute_value END)   AS `Metal Type`
						FROM `tabItem Variant Attribute`
						WHERE attribute IN ('Metal Colour','Metal Purity','Metal Touch','Metal Type')
						GROUP BY parent
					) ia ON ia.parent = mq.item_code
					GROUP BY ia.`Metal Colour`, ia.`Metal Purity`, ia.`Metal Touch`, ia.`Metal Type`
				""", as_dict=True)

			print("mop_balance_table_items_based_on_item_code",mop_balance_table_items_based_on_item_code)
			mop_balance_table_items_based_on_item_code_dic ={}
			for itmes in mop_balance_table_items_based_on_item_code:
				# if itmes["item_code"][0] == "F":
				# 	change_item_code_raw = "M-"+itmes['Metal Type'][0]+"-"+itmes['Metal Touch']+"-"+itmes["Metal Purity"]+"-"+itmes["Metal Colour"][0]
				# 	change_item_code = frappe.db.get_value("Item",change_item_code_raw)
				# 	if change_item_code:
				# 		itmes["item_code"] = change_item_code
				# 	else:
				# 		frappe.throw(f"Need item {change_item_code_raw}")

				mop_balance_table_items_based_on_item_code_dic[itmes["item_code"]] = itmes["qty"] 
			append_refined_gold = {row.item_code: row for row in self.refined_gold} 
			for row_item in mop_balance_table_items_based_on_item_code_dic:
				if row_item not in append_refined_gold:
					self.append("refined_gold",{
						"item_code":row_item,
						"refining_gold_weight":mop_balance_table_items_based_on_item_code_dic[row_item],
						"metal_purity" :99.9,
						"pure_weight":mop_balance_table_items_based_on_item_code_dic[row_item] * ( 99.9/ 100)
					})
				elif append_refined_gold[row_item].refining_gold_weight != mop_balance_table_items_based_on_item_code_dic[row_item]:
					append_refined_gold[row_item].refining_gold_weight = mop_balance_table_items_based_on_item_code_dic[row_item]
					append_refined_gold[row_item].pure_weight = mop_balance_table_items_based_on_item_code_dic[row_item] * ( 99.9/ 100)
			remove_items =[]
			for row_item in append_refined_gold:
				if row_item not in mop_balance_table_items_based_on_item_code_dic:
					self.remove(append_refined_gold[row_item])
					remove_items.append(row_item)
				if row_item not in remove_items:
					if append_refined_gold[row_item].metal_purity != 99.9:
						append_refined_gold[row_item].metal_purity = 99.9
					if append_refined_gold[row_item].refining_gold_weight != mop_balance_table_items_based_on_item_code_dic[row_item]:
						append_refined_gold[row_item].refining_gold_weight = mop_balance_table_items_based_on_item_code_dic[row_item]
						append_refined_gold[row_item].pure_weight = mop_balance_table_items_based_on_item_code_dic[row_item] * ( 99.9/ 100)

						

	def validate_weight_for_parent_manufacturing_order(self):
		err_msg =""
		recovered_diamond =0.0
		for row in self.recovered_diamond:
			recovered_diamond = recovered_diamond+ (row.get("weight") or 0)
			if  self.mop_balance_table_items.get("qty") and (row.get("weight") or 0) > self.mop_balance_table_items.get("qty").get(row.get("item")):
				err_msg += f"<br> Items {row.get("item")} weight {row.get("weight")} Can not be greater MOP's item weight { self.mop_balance_table_items.get("qty")[row.get("item")]} <br>"

		if recovered_diamond > (self.get("diamond_wt") or 0):
			err_msg += f"<br> Recovered Diamond weight : {recovered_diamond} can not be greater than  MOP's diamond weight : {self.diamond_wt} <br>"
		recovered_diamond = 0.0
		for row in self.recovered_gemstone:
			recovered_diamond = recovered_diamond +(row.get("weight") or 0)
			if  self.mop_balance_table_items.get("qty") and (row.get("weight") or 0) > self.mop_balance_table_items.get("qty").get(row.get("item")):
				err_msg += f"<br> Items {row.get("item")} weight {row.get("weight")} Can not be greater MOP' item weight { self.mop_balance_table_items.get("qty")[row.get("item")]} <br>"
		if recovered_diamond > (self.get("gemstone_wt") or 0):
			err_msg += f"<br> Recovered Gemstone weight : {recovered_diamond} can not be greater than  MOP's gemstone weight : {self.gemstone_wt} <br>"
		recovered_diamond = 0.0
		for row in self.refined_gold:
			print(row.as_dict())
			recovered_diamond = recovered_diamond + (row.get("after_burn_weight") or 0)
		if recovered_diamond > (self.get("metal_wt") or 0):
			err_msg += f"<br> Recovered Metal weight : {recovered_diamond} can not be greater than  MOP's Metal weight : {self.metal_wt}<br>"
		if err_msg:
			frappe.throw(err_msg)

@frappe.whitelist()
def get_manufacturing_operations(source_name, target_doc=None):
	if not target_doc:
		target_doc = frappe.new_doc("Refining")
	elif isinstance(target_doc, str):
		target_doc = frappe.get_doc(json.loads(target_doc))

	operation = frappe.db.get_value(
		"Manufacturing Operation",
		source_name,
		["metal_type", "manufacturing_order", "gross_wt", "manufacturing_work_order"],
		as_dict=1,
	)

	target_doc.append(
		"manufacturing_work_order",
		{
			"manufacturing_operation": source_name,
			"manufacturing_work_order": operation["manufacturing_work_order"],
			"metal_type": operation["metal_type"],
			# "metal_weight":operation["metal_weight"],
			"parent_manufacturing_work_order": operation["manufacturing_order"],
		},
	)
	return target_doc


def get_stock_entries_against_mfg_operation(doc):
	if isinstance(doc, str):
		doc = frappe.get_doc("Manufacturing Operation", doc)
	wh = frappe.db.get_value("Warehouse", {"disabled": 0, "department": doc.department}, "name")
	if doc.employee:
		wh = frappe.db.get_value("Warehouse", {"disabled": 0, "employee": doc.employee}, "name")

	stock_entry_details = frappe.db.get_all(
		"Stock Entry Detail",
		filters={"t_warehouse": wh, "manufacturing_operation": doc.name, "docstatus": 1},
		fields=["item_code", "qty", "uom", "batch_no", "serial_no"],
	)
	return stock_entry_details


def create_refining_entry(self):
	target_wh = frappe.db.get_value(
		"Warehouse", {"disabled": 0, "department": self.refining_department}
	)

	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Repack",
			"custom_refining": self.name,
			# "manufacturing_work_order": row.manufacturing_work_order,
			"inventory_type": "Regular Stock",
			"auto_created": 1,
		}
	)

	if self.refining_type == "Parent Manufacturing Order":
		all_items = []
		for row in self.manufacturing_work_order:
			# get items from operations
			data = get_stock_entries_against_mfg_operation(row.manufacturing_operation)
			if not data:
				frappe.throw(f"No stock entry against : {row.manufacturing_operation}")
			if data:
				all_items += data

		if all_items:
			for entry in all_items:
				se.append(
					"items",
					{
						"item_code": entry.item_code,
						"qty": entry.qty,
						"uom": entry.uom,
						"batch_no": entry.batch_no,
						"serial_no": entry.serial_no,
						"inventory_type": "Regular Stock",
						"manufacturing_operation": row.manufacturing_operation,
						"department": self.department,
						# "to_department": doc.department,
						"s_warehouse": target_wh,
						"use_serial_batch_fields": True,
						"serial_and_batch_bundle": None,
					},
				)

	if self.refining_type == "Serial Number":
		for row in self.refining_serial_no_detail:
			# get items from serial no
			se.append(
				"items",
				{
					"item_code": row.item_code,
					"qty": 1,
					"gross_weight": row.gross_weight,
					# "uom": row.uom,
					# "batch_no":row.batch_no,
					"inventory_type": "Regular Stock",
					"serial_no": row.serial_number,
					# "manufacturing_operation": row.manufacturing_operation,
					"department": self.department,
					# "to_department": doc.department,
					"s_warehouse": target_wh,
					"use_serial_batch_fields": True,
					"serial_and_batch_bundle": None,
				},
			)

	if self.refining_type == "Recovery Material":
		frappe.throw(_("Dust Not Received in Refining Department")) if not self.dust_received else 0
		if not self.multiple_operation and not self.multiple_department:
			copy_enter_stock_row(self, se, self.stock_entry)
			# enter_stock_row(self,se)
		elif not self.multiple_operation and self.multiple_department:
			copy_enter_stock_row(self, se, self.stock_entry)
			# enter_stock_row(self,se)
		elif self.multiple_operation and not self.multiple_department:
			copy_enter_stock_row(self, se, self.stock_entry)
			# enter_stock_row(self,se)

	elif self.refining_type == "Re-Refining Material":
		enter_stock_row(self, se)

	elif self.refining_type in ["Parent Manufacturing Order", "Serial Number"]:
		if (
			len(self.recovered_diamond) < 1
			and len(self.recovered_gemstone) < 1
			and len(self.refined_gold) < 1
		):
			frappe.throw(
				_(
					"Please Select at Least 1 item in <strong> Recovered Diamond </strong> or <strong> Recovered Metal</strong> or <strong> Recovered Gemstone</strong>"
				)
			)
		append_recovered_items(self, se, target_wh)

	se.save()
	se.submit()
	self.stock_entry = se.name
	frappe.msgprint(_("Refining Entry Passed successfully"))


def check_allocation(self, allocation_table):
	allocation = 0
	for row in allocation_table:
		allocation += row.ratio or 0

	if allocation != 100:
		frappe.throw(_("Ratio Should be 100%"))


def dust_receipt_entry(self, type):
	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"custom_refining": self.name,
			# "manufacturing_work_order": row.manufacturing_work_order,
			"inventory_type": "Regular Stock",
			"auto_created": 1,
		}
	)
	if type == "single":
		for row in self.refined_gold:
			se.append(
				"items",
				{
					"item_code": row.dust_item,
					"qty": row.dust_weight,
					# "s_warehouse": self.source_warehouse,
					"inventory_type": "Regular Stock",
					"t_warehouse": self.source_warehouse,
					# "to_department": self.refining_department,
				},
			)
	elif type == "refining_department_detail":
		for row in self.refining_department_detail:
			allocate_dust_department_wise(self, row, se, "mr")
	elif type == "refining_operation_detail":
		for row in self.refining_operation_detail:
			allocate_dust_employee_wise_operations(self, row, se, "mr")

	se.save()
	se.submit()
	return se


def test_create_transfer_entry(self, type, mr_se=None):
	mr_se = frappe.copy_doc(mr_se)
	mr_se.stock_entry_type = "Material Transfer to Department"
	mr_se.inventory_type = "Regular Stock"
	mr_se.custom_refining = self.name

	if type == "single":
		for item in mr_se.items:
			item.inventory_type = "Regular Stock"
			item.s_warehouse = item.t_warehouse
			item.t_warehouse = self.refining_warehouse
			item.to_department = self.refining_department

	elif type == "refining_department_detail":
		for item in mr_se.items:
			item.inventory_type = "Regular Stock"
			item.s_warehouse = item.t_warehouse
			item.t_warehouse = self.refining_warehouse
			item.to_department = self.refining_department

	elif type == "refining_operation_detail":
		for item in mr_se.items:
			item.inventory_type = "Regular Stock"
			item.s_warehouse = item.t_warehouse
			item.t_warehouse = self.refining_warehouse
			item.to_department = self.refining_department

	mr_se.save()
	mr_se.submit()
	self.stock_entry = mr_se.name


def allocate_dust_department_wise(self, row, se, se_type):
	for dust_row in self.refined_gold:
		source_warehouse = frappe.db.get_value(
			"Warehouse", {"disabled": 0, "department": row.department}, "name"
		)
		item_row = {
			"item_code": dust_row.dust_item,
			"qty": flt((dust_row.dust_weight * row.ratio) / 100),
			"inventory_type": "Regular Stock",
			"t_warehouse": self.refining_warehouse,
		}
		if se_type == "mt":
			item_row["s_warehouse"] = (source_warehouse,)
			item_row["to_department"] = (self.refining_department,)
		else:
			item_row["t_warehouse"] = source_warehouse

		se.append("items", item_row)


def allocate_dust_employee_wise_operations(self, row, se, se_type):
	list_of_operation = frappe.db.get_list(
		"Manufacturing Operation",
		filters=[
			["start_time", ">=", self.date_from],
			["finish_time", "<=", self.date_to],
			["operation", "=", row.operation],
			["net_wt", "!=", 0],
		],
		fields=["name", "operation", "employee", "SUM(net_wt) as net_wt"],
		group_by="employee",
	)
	if not list_of_operation:
		frappe.throw(
			f"No operations found Between the Selected Period {self.date_from} and {self.date_to}"
		)
	for dust_row in self.refined_gold:
		dust_weight = flt((dust_row.dust_weight * row.ratio) / 100)
		total_net_wt = sum([operation.get("net_wt") for operation in list_of_operation])
		for operation in list_of_operation:
			source_warehouse = frappe.db.get_value(
				"Warehouse", {"disabled": 0, "company": self.company, "employee": operation.employee}, "name"
			)
			if not source_warehouse:
				frappe.throw(f"Employee Not assigned any WareHouse : {operation.employee}")

			item_row = {
				"item_code": dust_row.dust_item,
				"qty": dust_weight * (operation.net_wt) / (total_net_wt),
				"t_warehouse": self.refining_warehouse,
				"inventory_type": "Regular Stock",
			}
			if se_type == "mt":
				item_row["s_warehouse"] = (source_warehouse,)
				item_row["to_department"] = (self.refining_department,)
			else:
				item_row["t_warehouse"] = source_warehouse

			se.append("items", item_row)


def get_item_based_on_purity(self):
	if not self.metal_purity:
		frappe.throw(_("Please Select Metal Purity"))
	elif self.fine_weight <= 0:
		frappe.throw(_("Fine Weight Should be Greater Than 0"))

	metal_touch = frappe.db.get_value("Attribute Value", {"name": self.metal_purity}, "metal_touch")

	recovered_item = get_item_from_attribute("Gold", metal_touch, self.metal_purity, "Yellow")

	recovered_item = recovered_item if recovered_item else "Refined Gold - Test"

	frappe.db.set_value("Refining", self.name, "recovered_item", recovered_item)
	return recovered_item


def copy_enter_stock_row(self, se, trf_se):
	for row in self.refined_gold:
		if row.item_code and row.dust_item:
			transfer_entry = frappe.get_doc("Stock Entry", trf_se)
			for item in transfer_entry.items:
				item.t_warehouse = ""
				item.inventory_type = "Regular Stock"
				item.s_warehouse = self.refining_warehouse

				se.append("items", item)

			se.append(
				"items",
				{
					"item_code": row.item_code,
					"qty": row.refining_gold_weight,
					"t_warehouse": self.refining_warehouse,
					"inventory_type": "Regular Stock",
					"to_department": self.refining_department,
					"use_serial_batch_fields": True,
					"serial_and_batch_bundle": None,
				},
			)
		else:
			frappe.throw(_("Please add item code in Recovered Metal Table"))


def enter_stock_row(self, se):
	for row in self.refined_gold:
		if row.item_code and row.dust_item:
			se.append(
				"items",
				{
					"item_code": row.dust_item,
					"qty": row.dust_weight,
					"s_warehouse": self.refining_warehouse,
					"to_department": self.refining_department,
					"use_serial_batch_fields": True,
					"serial_and_batch_bundle": None,
				},
			)
			se.append(
				"items",
				{
					"item_code": row.item_code,
					"qty": row.refining_gold_weight,
					"t_warehouse": self.refining_warehouse,
					"to_department": self.refining_department,
					"use_serial_batch_fields": True,
					"serial_and_batch_bundle": None,
				},
			)
		else:
			frappe.throw(_("Please add item code in Recovered Metal Table"))


def append_recovered_items(self, se, target_wh):
	if len(self.recovered_diamond) > 0:

		for diamond_row in self.recovered_diamond:
			se.append(
				"items",
				{
					"item_code": diamond_row.item,
					"qty": diamond_row.weight,
					"pcs": diamond_row.pcs,
					# "batch_no":entry.batch_no,
					# "serial_no" :entry.serial_no,
					"t_warehouse": target_wh,
					"inventory_type": "Regular Stock",
					# "department": doc.department,
					# "to_department": doc.department,
					# "manufacturing_operation": doc.name,
					# "is_finished_item":1
				},
			)

	if len(self.recovered_gemstone) > 0:
		for gen_row in self.recovered_gemstone:
			se.append(
				"items",
				{
					"item_code": gen_row.item,
					"qty": gen_row.weight,
					"pcs": gen_row.pcs,
					# "batch_no":entry.batch_no,
					# "serial_no" :entry.serial_no,
					"t_warehouse": target_wh,
					"inventory_type": "Regular Stock",
					# "department": doc.department,
					# "to_department": doc.department,
					# "manufacturing_operation": doc.name,
					# "is_finished_item":1
				},
			)
	if len(self.refined_gold) > 0:
		for metal_row in self.refined_gold:
			se.append(
				"items",
				{
					"item_code": metal_row.item_code,
					"qty": metal_row.refining_gold_weight,
					# "pcs":metal_row.pcs,s
					# "batch_no":entry.batch_no,
					# "serial_no" :entry.serial_no,
					"t_warehouse": target_wh,
					"inventory_type": "Regular Stock",
					# "department": doc.department,
					# "to_department": doc.department,
					# "manufacturing_operation": doc.name,
					# "is_finished_item":1
				},
			)
