import json

import frappe
from erpnext.manufacturing.doctype.job_card.job_card import JobCard
from frappe import _, bold
from frappe.model.document import Document
from frappe.query_builder import Case, CustomFunction
from frappe.query_builder.custom import ConstantColumn
from frappe.query_builder.functions import IfNull, Sum
from frappe.utils import add_to_date, get_datetime, time_diff, time_diff_in_hours

from jewellery_erpnext.jewellery_erpnext.doc_events.job_card import make_stock_entry


class OverlapError(frappe.ValidationError):
	pass


class CombineJobCard(Document):
	def onload(self):
		self.render_job_card_data()

	def validate(self):
		self.render_job_card_data()
		self.validate_time_logs()
		# Set Fields in Individual Job Cards
		self.set_individual_in_and_out_weights()
		if self.operation == "Tree Making":
			self.set_wax_weight()

	def set_wax_weight(self):
		wo_list = [jc.work_order for jc in self.details]

		# JobCard = frappe.qb.DocType("Job Card")
		# IF = CustomFunction("IF", ["condition", "true_expr", "false_expr"])
		# query = (
		# 	frappe.qb.from_(JobCard)
		# 	.select(
		# 		Sum(IF(JobCard.operation == "Waxing 1", JobCard.wax_weight, 0)).as_("wax1"),
		# 		Sum(
		# 			IF(
		# 				JobCard.operation == "Waxing 2",
		# 				(JobCard.wax_weight - JobCard.in_diamond_weight - JobCard.in_gemstone_weight),
		# 				0,
		# 			)
		# 		).as_("wax2"),
		# 	)
		# 	.where(
		# 		(JobCard.operation.isin(["Waxing 1", "Waxing 2"]))
		# 		& (JobCard.work_order.isin(wo_list))
		# 		& (JobCard.docstatus != 2)
		# 	)
		# )
		# ww = query.run(as_dict=True)[0]
		# self.tree_wax_weight = ww.get("wax1", 0) + ww.get("wax2", 0)

	def before_save(self):
		# Link Docname to Individual Job Card
		for job_card in self.details:
			frappe.db.set_value("Job Card", job_card.job_card, "in_combined_job_card", True)
			frappe.db.set_value("Job Card", job_card.job_card, "workstation_type", self.workstation_type)
			# frappe.db.set_value('Job Card', job_card.job_card, 'workstation', self.workstation)

	def on_submit(self):
		balance = self.total_in_gross_weight - self.total_out_gross_weight
		balance = round(balance, 3)
		if balance != 0:
			return frappe.throw(_("Difference Between In Weight and Out Weight Should Be 0"))
		self.submit_job_card()

	def render_job_card_data(self):
		self.get_internal_in_weight()
		self.get_external_in_weight()
		self.get_out_weights()
		self.get_wastages()
		self.get_totals()

	def get_internal_in_weight(self):
		job_card_list = tuple(row.job_card for row in self.details)

		JobCardInternalTransferItem = frappe.qb.DocType("Job Card Internal Transfer Item")
		# JobCard = frappe.qb.DocType("Job Card")

		# query = (
		# 	frappe.qb.from_(JobCardInternalTransferItem)
		# 	.join(JobCard)
		# 	.on(JobCard.name == JobCardInternalTransferItem.from_job_card)
		# 	.select(
		# 		JobCardInternalTransferItem.item_code,
		# 		JobCardInternalTransferItem.from_job_card,
		# 		JobCardInternalTransferItem.to_job_card,
		# 		JobCardInternalTransferItem.from_combine_job_card,
		# 		JobCardInternalTransferItem.to_combine_job_card,
		# 		JobCardInternalTransferItem.gross_wt,
		# 		JobCardInternalTransferItem.purity,
		# 		(JobCard.out_gold_weight + JobCard.out_finding_weight).as_("net_wt"),
		# 		JobCardInternalTransferItem.uom,
		# 	)
		# 	.where(
		# 		(JobCardInternalTransferItem.docstatus == 1)
		# 		& (
		# 			(JobCardInternalTransferItem.to_job_card.isin(job_card_list))
		# 			| (JobCardInternalTransferItem.to_combine_job_card == self.name)
		# 		)
		# 		& (
		# 			(JobCardInternalTransferItem.from_job_card.isnotnull())
		# 			| (JobCardInternalTransferItem.from_combine_job_card.isnotnull())
		# 		)
		# 	)
		# )
		# internal_in_weight_data = query.run(as_dict=True)

		# if internal_in_weight_data:
		# 	if internal_in_weight_data[0].get("from_combine_job_card") and internal_in_weight_data[0].get(
		# 		"to_combine_job_card"
		# 	):
		# 		return self.get_internal_in_weight_from_prev_combined_job_card(internal_in_weight_data)
		# 	self.set_in_weight_fields(internal_in_weight_data, "total_in_gross_weight")
		# 	html_content = frappe.render_template(
		# 		table_html, {"data": internal_in_weight_data, "table_type": "Internal In Weight"}
		# 	)
		# 	self.internal_in_weight_html = html_content

	def get_internal_in_weight_from_prev_combined_job_card(self, internal_in_weight_data):
		data = []
		job_cards = [
			jc.get("job_card")
			for i in internal_in_weight_data
			for jc in frappe.get_doc("Combine Job Card", i.get("from_combine_job_card")).details
		]
		job_cards_tuple = tuple(job_cards) if job_cards else ("",)

		# JobCard = frappe.qb.DocType("Job Card")
		# query = (
		# 	frappe.qb.from_(JobCard)
		# 	.select(
		# 		JobCard.name, JobCard.out_gross_weight, JobCard.out_gold_weight, JobCard.out_finding_weight
		# 	)
		# 	.where(JobCard.name.isin(job_cards_tuple))
		# )
		# job_card_data = query.run(as_dict=True)

		# job_card_data = {jc["name"]: jc for jc in job_card_data}

		# for i in internal_in_weight_data:
		# 	cj_doc = frappe.get_doc("Combine Job Card", i.get("from_combine_job_card"))
		# 	for jc in cj_doc.details:
		# 		net_wt = (
		# 			job_card_data[jc.get("job_card")]["out_gold_weight"]
		# 			+ job_card_data[jc.get("job_card")]["out_finding_weight"]
		# 		)
		# 		data_dict = {
		# 			"item_code": jc.get("production_item"),
		# 			"from_job_card": jc.get("job_card"),
		# 			"gross_wt": round(job_card_data[jc.get("job_card")]["out_gross_weight"], 3),
		# 			"purity": i.get("purity"),
		# 			"net_wt": round(net_wt, 3),
		# 		}
		# 		data.append(data_dict)
		# if data:
		# 	self.set_in_weight_fields(data, "total_in_gross_weight")
		# 	html_content = frappe.render_template(
		# 		table_html, {"data": data, "table_type": "Internal In Weight"}
		# 	)
		# 	self.internal_in_weight_html = html_content

	def get_external_in_weight(self):
		job_card_list = tuple(row.job_card for row in self.details)

		StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")
		query = (
			frappe.qb.from_(StockEntryDetail)
			.select(
				StockEntryDetail.item_code,
				StockEntryDetail.from_job_card,
				StockEntryDetail.to_job_card,
				StockEntryDetail.s_warehouse.as_("from_warehouse"),
				StockEntryDetail.t_warehouse.as_("to_warehouse"),
				StockEntryDetail.qty.as_("gross_wt"),
				ConstantColumn(0).as_("purity"),
				ConstantColumn(0).as_("net_wt"),
				StockEntryDetail.uom,
			)
			.where(
				(StockEntryDetail.docstatus == 1)
				& (StockEntryDetail.to_job_card.isin(job_card_list))
				& (StockEntryDetail.from_job_card == "")
			)
		)
		external_in_weight_data = query.run(as_dict=True)

		if external_in_weight_data:
			external_in_wt = sum(
				i["gross_wt"] / 5
				if (i.get("item_code").startswith("D") or i.get("item_code").startswith("G"))
				else i["gross_wt"]
				for i in external_in_weight_data
			)
			self.total_in_gross_weight += self.total_in_gross_weight + round(external_in_wt, 3)
			self.total_in_gross_weight = round(self.total_in_gross_weight, 3)
			html_content = frappe.render_template(
				table_html, {"data": external_in_weight_data, "table_type": "External In Weight"}
			)
			self.external_in_weight_html = html_content

	def get_out_weights(self):
		self.total_out_gross_weight = 0
		job_card_list = tuple(row.job_card for row in self.details)

		JobCardInternalTransferItem = frappe.qb.DocType("Job Card Internal Transfer Item")
		query = (
			frappe.qb.from_(JobCardInternalTransferItem)
			.select(
				JobCardInternalTransferItem.item_code,
				JobCardInternalTransferItem.from_job_card,
				JobCardInternalTransferItem.to_job_card,
				JobCardInternalTransferItem.gross_wt,
				JobCardInternalTransferItem.purity,
				JobCardInternalTransferItem.net_wt,
				JobCardInternalTransferItem.uom,
			)
			.where(
				(JobCardInternalTransferItem.docstatus == 1)
				& (
					(JobCardInternalTransferItem.from_job_card.isin(job_card_list))
					| (JobCardInternalTransferItem.from_combine_job_card == self.name)
				)
				& (
					(JobCardInternalTransferItem.to_job_card.isnotnull())
					| (JobCardInternalTransferItem.to_combine_job_card.isnotnull())
				)
			)
		)
		out_weights_data = query.run(as_dict=True)

		if out_weights_data:
			self.set_in_weight_fields(out_weights_data, "total_out_gross_weight")
			html_content = frappe.render_template(
				table_html, {"data": out_weights_data, "table_type": "Out Weight"}
			)

			self.out_weights_html = html_content

	def set_in_weight_fields(self, data, field_name):
		total = sum(i.get("gross_wt") for i in data)
		if field_name == "total_in_gross_weight":
			self.total_in_gross_weight = 0
			self.total_in_gross_weight = total
		elif field_name == "total_out_gross_weight":
			self.total_out_gross_weight = 0
			self.total_out_gross_weight = total

	def get_wastages(self):
		# TODO: Wastages To be Fetched From Scrap Items
		loss_gold_weight = sum(item.get("stock_qty") for item in self.scrap_items)
		self.total_metal_loss = loss_gold_weight
		self.total_out_gross_weight += self.total_metal_loss

	def validate_time_logs(self):
		if not self.get("time_logs"):
			return
		for idx, d in enumerate(self.get("time_logs")):
			if d.to_time and get_datetime(d.from_time) > get_datetime(d.to_time):
				frappe.throw(_("Row {0}: From time must be less than to time").format(d.idx))

			if d.from_time and d.to_time:
				d.time_in_mins = time_diff_in_hours(d.to_time, d.from_time) * 60
				# self.total_time_in_mins += d.time_in_mins

			if idx != 0:
				last_row = self.get("time_logs")[idx - 1]
				last_row_mins = last_row.get("time_in_mins", 0) * len(self.details)
				last_row_to_time = last_row.get("to_time")
				final_time = add_to_date(last_row_to_time, minutes=last_row_mins)
				if d.from_time <= final_time:
					frappe.throw(f"Row: {idx} From time should be after {str(final_time)}")

	def submit_job_card(self):
		for idx, jc in enumerate(self.details):
			doc = frappe.get_doc("Job Card", jc.job_card)
			if self.time_logs:
				for d in self.time_logs:
					if idx != 0:
						time_in_mins = d.time_in_mins * (len(self.details) - 1)
						doc.append(
							"time_logs",
							{
								"employee": d.employee,
								"from_time": add_to_date(d.from_time, minutes=time_in_mins),
								"to_time": add_to_date(d.to_time, minutes=time_in_mins),
								"time_in_mins": d.time_in_mins,
								"completed_qty": jc.for_quantity,
								"operation": d.operation,
							},
						)
					else:
						doc.append(
							"time_logs",
							{
								"employee": d.employee,
								"from_time": d.from_time,
								"to_time": d.to_time,
								"time_in_mins": d.time_in_mins,
								"completed_qty": jc.for_quantity,
								"operation": d.operation,
							},
						)
			if self.time_logs:
				for d in self.scrap_items:
					doc.append(
						"scrap_items",
						{
							"item_code": d.item_code,
							"item_name": d.item_name,
							"description": d.description,
							"stock_qty": d.stock_qty / len(self.details),
							"stock_uom": d.stock_uom,
						},
					)
			doc.save(ignore_permissions=True)
			doc.submit()

	@frappe.whitelist()
	def get_job_cards(self):
		if not self.production_plan:
			frappe.throw(_("Please select production plan to get job cards."))
		if not self.operation:
			frappe.throw(_("Please select operation to get job cards."))
		wo = frappe.get_list("Work Order", {"production_plan": self.production_plan, "docstatus": 1})
		wo_list = tuple(row.name for row in wo)

		# JobCard = frappe.qb.DocType("Job Card")
		# query = (
		# 	frappe.qb.from_(JobCard)
		# 	.select(JobCard.name, JobCard.production_item, JobCard.for_quantity, JobCard.work_order)
		# 	.where(
		# 		(JobCard.operation == self.operation)
		# 		& (JobCard.docstatus == 0)
		# 		& (JobCard.work_order.isin(wo_list))
		# 	)
		# )
		# job_cards = query.run(as_dict=True)

		# self.details = []
		# for row in job_cards:
		# 	self.append(
		# 		"details",
		# 		{
		# 			"job_card": row.name,
		# 			"production_item": row.production_item,
		# 			"for_quantity": row.for_quantity,
		# 			"work_order": row.work_order,
		# 		},
		# 	)

	def get_totals(self):
		job_card_list = tuple(row.job_card for row in self.details)

		JobCardInternalTransferItem = frappe.qb.DocType("Job Card Internal Transfer Item")
		StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")

		internal_in_weights_query = (
			frappe.qb.from_(JobCardInternalTransferItem)
			.select(
				IfNull(Sum(JobCardInternalTransferItem.gross_wt), 0).as_("internal_in_gross_wt"),
				IfNull(Sum(JobCardInternalTransferItem.net_wt), 0).as_("internal_in_net_wt"),
			)
			.where(
				(JobCardInternalTransferItem.docstatus == 1)
				& (
					(JobCardInternalTransferItem.to_job_card.isin(job_card_list))
					| (JobCardInternalTransferItem.to_combine_job_card == self.name)
				)
				& (
					(JobCardInternalTransferItem.from_job_card.isnotnull())
					| (JobCardInternalTransferItem.from_combine_job_card.isnotnull())
				)
			)
		)
		internal_in_weights = internal_in_weights_query.run(as_dict=True)

		external_in_weights_query = (
			frappe.qb.from_(StockEntryDetail)
			.select(
				IfNull(Sum(StockEntryDetail.qty), 0).as_("external_in_gross_wt"),
				ConstantColumn(0).as_("external_in_net_wt"),
				StockEntryDetail.to_job_card,
			)
			.where(
				(StockEntryDetail.docstatus == 1)
				& (StockEntryDetail.to_job_card.isin(job_card_list))
				& ((StockEntryDetail.from_job_card == "") | (StockEntryDetail.from_job_card.isnull()))
			)
		)
		external_in_weights = external_in_weights_query.run(as_dict=True)

		out_weights_query = (
			frappe.qb.from_(JobCardInternalTransferItem)
			.select(
				IfNull(Sum(JobCardInternalTransferItem.gross_wt), 0).as_("out_weights_gross_wt"),
				IfNull(Sum(JobCardInternalTransferItem.net_wt), 0).as_("out_weights_net_wt"),
			)
			.where(
				(JobCardInternalTransferItem.docstatus == 1)
				& (
					(JobCardInternalTransferItem.from_job_card.isin(job_card_list))
					| (JobCardInternalTransferItem.from_combine_job_card == self.name)
				)
				& (
					(JobCardInternalTransferItem.to_job_card.isnotnull())
					| (JobCardInternalTransferItem.to_combine_job_card.isnotnull())
				)
			)
		)
		out_weights = out_weights_query.run(as_dict=True)

		wastages_query = (
			frappe.qb.from_(JobCardInternalTransferItem)
			.select(
				IfNull(Sum(JobCardInternalTransferItem.gross_wt), 0).as_("wastages_gross_wt"),
				IfNull(Sum(JobCardInternalTransferItem.net_wt), 0).as_("wastages_net_wt"),
			)
			.where(
				(JobCardInternalTransferItem.docstatus == 1)
				& (
					(JobCardInternalTransferItem.from_job_card.isin(job_card_list))
					| (JobCardInternalTransferItem.from_combine_job_card == self.name)
				)
				& (
					(JobCardInternalTransferItem.to_job_card == "")
					| (JobCardInternalTransferItem.to_job_card.isnull())
				)
				& (
					(JobCardInternalTransferItem.to_combine_job_card == "")
					| (JobCardInternalTransferItem.to_combine_job_card.isnull())
				)
			)
		)
		wastages = wastages_query.run(as_dict=True)
		# self.total_in_gross_weight = internal_in_weights[0]['internal_in_gross_wt']
		# self.total_out_gross_weight  = out_weights[0]['out_weights_gross_wt']

		out_weight = self.total_out_gross_weight - self.total_metal_loss
		out_weight = round(out_weight, 3)
		balance = self.total_in_gross_weight - self.total_out_gross_weight

		balance = round(balance, 3)

		html_content = frappe.render_template(
			total_html,
			{
				"internal_in_gross_wt": self.total_in_gross_weight,
				"external_in_gross_wt": 0,
				"out_weights_gross_wt": out_weight,
				"wastages_gross_wt": self.total_metal_loss,
				"balance": balance,
				"internal_in_net_wt": 0,
				"external_in_net_wt": 0,
				"out_weights_net_wt": 0,
				"wastages_net_wt": 0,
			},
		)
		self.total_html = html_content

	def set_individual_in_and_out_weights(self):
		for jc in self.details:
			jc_doc = frappe.get_doc("Job Card", jc.get("job_card"))
			for i in jc_doc.time_logs:
				i.completed_qty = 1
			jc_doc.save()


@frappe.whitelist()
def create_stock_entry(job_cards):
	job_cards = json.loads(job_cards)
	se_created = []
	for jc in job_cards:
		doc = frappe.get_doc("Job Card", jc[0])
		if not doc.get("items", []):
			continue
		try:
			doc = make_stock_entry(jc[0])
			doc.save()
			se_created.append(doc.name)
			frappe.db.set_value("Combine Job Card Detail", jc[1], "stock_entry", doc.name)
		except Exception as e:
			frappe.log_error("Error in create_stock_entry(Cjobcard)", e)
	return se_created


@frappe.whitelist()
def submit_stock_entry(doclist):
	doclist = json.loads(doclist)
	failed = []
	for name in doclist:
		try:
			doc = frappe.get_doc("Stock Entry", name[0])
			doc.submit()
			frappe.db.set_value("Combine Job Card Detail", name[1], "submitted", 1)
		except Exception as e:
			failed.append(name[0])
			frappe.log_error("Error in submit_stock_entry(Cjobcard)", e)

	return failed


@frappe.whitelist()
def stock_entry_detail(item):
	job_card = frappe.get_doc("Job Card", item)

	# JobCard = frappe.qb.DocType("Job Card")
	# JobCardScrapItem = frappe.qb.DocType("Job Card Scrap Item")
	# StockEntry = frappe.qb.DocType("Stock Entry")
	# StockEntryDetail = frappe.qb.DocType("Stock Entry Detail")

	# # Define the case condition for gross_weight
	# gross_weight_case = Case()
	# gross_weight_case = gross_weight_case.when(
	# 	StockEntryDetail.item_code == JobCardScrapItem.item_code,
	# 	StockEntryDetail.qty - JobCardScrapItem.stock_qty,
	# ).else_(StockEntryDetail.qty)

	# query = (
	# 	frappe.qb.from_(JobCard)
	# 	.left_join(JobCardScrapItem)
	# 	.on(JobCardScrapItem.parent == JobCard.name)
	# 	.left_join(StockEntry)
	# 	.on(StockEntry.work_order == JobCard.work_order)
	# 	.left_join(StockEntryDetail)
	# 	.on(StockEntryDetail.parent == StockEntry.name)
	# 	.select(
	# 		JobCard.name,
	# 		StockEntryDetail.item_code,
	# 		StockEntryDetail.item_name,
	# 		JobCard.work_order,
	# 		gross_weight_case.as_("gross_weight"),
	# 		StockEntryDetail.metal_purity.as_("purity"),
	# 		StockEntryDetail.uom,
	# 	)
	# 	.where(
	# 		(JobCard.name == item)
	# 		& (StockEntry.stock_entry_type != "Manufacture")
	# 		& (StockEntry.docstatus == 1)
	# 	)
	# )
	# stock_entry_detail = query.run(as_dict=True)

	# html_content_stock_entry = frappe.render_template(
	# 	table_stock_entry, {"data": stock_entry_detail, "table_type": "Internal In Weight"}
	# )
	# frappe.msgprint(str(html_content_stock_entry))
	# if job_card.scrap_items:

	# 	scrap_query = (
	# 		frappe.qb.from_(JobCard)
	# 		.left_join(JobCardScrapItem)
	# 		.on(JobCardScrapItem.parent == JobCard.name)
	# 		.select(
	# 			JobCard.name,
	# 			JobCardScrapItem.item_code,
	# 			JobCardScrapItem.stock_qty,
	# 			JobCardScrapItem.stock_uom,
	# 		)
	# 		.where(JobCard.name == item)
	# 	)
	# 	scrap_item = scrap_query.run(as_dict=True)

	# 	html_content_scrap_item = frappe.render_template(
	# 		table_scrap_item, {"data": scrap_item, "table_type": "Scarp Item"}
	# 	)
	# 	frappe.msgprint(str(html_content_scrap_item))


table_html = """
<table class="table table-bordered table-hover" width='100%' style="border: 1px solid #d1d8dd;border-collapse: collapse;">
<thead>
	<tr>
		<th style="border: 1px solid #d1d8dd; font-size: 11px;">Item Code</th>
		{% if table_type == 'Internal In Weight' %}
			<th style="border: 1px solid #d1d8dd; font-size: 11px;">Job Card</th>
		{% elif table_type == 'External In Weight' %}
			<th style="border: 1px solid #d1d8dd; font-size: 11px;">Job Card</th>
		{% elif table_type == 'Out Weight' %}
			<th style="border: 1px solid #d1d8dd; font-size: 11px;">Job Card</th>
		{% elif table_type == 'Wastage Weight' %}
			<th style="border: 1px solid #d1d8dd; font-size: 11px;">To Warehouse</th>
		{% endif %}
		<th style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;">Gross Wt</th>
		<th style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;">Purity</th>
		{% if table_type != 'Out Weight' %}
		<th style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;">Net Wt</th>
		{% endif %}
	</tr>
</thead>
<tbody>
{% for item in data %}
	<tr>
	<script type = 'text/Javascript'>
			function stock_entry(job_card){
				frappe.call({
					method: "jewellery_erpnext.jewellery_erpnext.doctype.combine_job_card.combine_job_card.stock_entry_detail",
					args:{
						'item':job_card
					},
					callback:function(r){
						console.log(job_card)
					}
				})
			}
		</script>
		<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.item_code }}</td>
		{% if table_type == 'Internal In Weight' %}
			<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.from_job_card or '' }}</td>
		{% elif table_type == 'External In Weight' %}
			<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.to_job_card or '' }}</td>

		{% elif table_type == 'Out Weight' %}
			<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.to_job_card or '' }}</td>
		{% elif table_type == 'Wastage Weight' %}
			<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.to_warehouse or '' }}</td>
		{% endif %}
		<td style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem" onclick=stock_entry('{{item.from_job_card}}')>{{ item.gross_wt }} {{ item.uom or '' }}</td>
		<td style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.purity }}</td>
		{% if table_type != 'Out Weight' %}
		<td style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.net_wt }} {{ item.uom or '' }}</td>
		{% endif %}
	</tr>
{% endfor %}
</tbody>
</table>
"""

table_stock_entry = """
<table class="table table-bordered table-hover" width='100%' style="border: 1px solid #d1d8dd;border-collapse: collapse;">
<thead>
	<tr>
		<th style="border: 1px solid #d1d8dd; font-size: 11px;">Item Code</th>
		<th style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;">Item Name</th>
		<th style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;">Gross Wgt</th>
		<th style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;">Purity</th>
		<th style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;">UOM</th>
	</tr>
</thead>
<tbody>
{% for item in data %}
	<tr>
		<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.item_code }}</td>
		<td style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.item_name}} </td>
		<td style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.gross_weight}} </td>
		<td style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.purity}} </td>
		<td style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.uom}} </td>
	</tr>
{% endfor %}
</tbody>
</table>
"""
table_scrap_item = """
<table class="table table-bordered table-hover" width='100%' style="border: 1px solid #d1d8dd;border-collapse: collapse;">
<thead>
	<tr>
		<th style="border: 1px solid #d1d8dd; font-size: 11px;">Item Code</th>
		<th style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;">Stock Qty</th>
		<th style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;">Stock UOM</th>
	</tr>
</thead>
<tbody>
{% for item in data %}
	<tr>
		<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.item_code }}</td>
		<td style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.stock_qty}} </td>
		<td style="text-align:end;border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">{{ item.stock_uom}} </td>
	</tr>
{% endfor %}
</tbody>
</table>
"""


total_html = """
<table class="table table-hover" width='100%'  style="border: 1px solid #d1d8dd;border-collapse: collapse;">
<tbody>


	<tr>
                <th style="border: 1px solid #d1d8dd;font-size: 11px;"></th>
                <th style="border: 1px solid #d1d8dd;font-size: 11px;">Gross</th>
                <th  style="border: 1px solid #d1d8dd;font-size: 11px;">Fine</th>
        </tr>


	 <tr>
                <td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">In</th>
                <td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem;text-align:end">{{ internal_in_gross_wt}}</td>
                <td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem;text-align:end">{{ 0 }}</td>
         </tr>

	<tr>
                <td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">Out</th>
                <td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem;text-align:end">{{ out_weights_gross_wt }}</td>
                <td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem;text-align:end">{{ out_weights_net_wt + wastages_net_wt }}</td>
        </tr>

	<tr>
                <td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem">Wastage</th>
                <td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem;text-align:end">{{ wastages_gross_wt }}</td>
                <td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem;text-align:end">{{ 0 }}</td>
            </tr>

	<tr>
		<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem"><b>Balance</b></td>
		<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem;text-align:end"><b>{{ balance }}<b></td>
		<td style="border: 1px solid #d1d8dd; font-size: 11px;padding:0.25rem;text-align:end"><b>{{ 0 }}<b></td>
	</tr>
</tbody>
</table>
"""
