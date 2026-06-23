import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class RefiningEntry(Document):
	def validate(self):
		self.set_naming_series()
		self.validate_configuration()
		self.validate_warehouse()
		self.validate_quantities()
		self.calculate_totals()

		if self.status == "Recovery Entered":
			self.validate_recovery_distribution()

	def before_submit(self):
		if self.refining_type == "Dust Refining":
			if not self.material_items:
				self.build_material_table()
			self.ensure_dust_opening_material_row()

	def on_submit(self):
		self.log_audit_action("Submitted", "Draft", "Submitted")
		self.create_material_transfer_se()

		if self.refining_type == "Dust Refining":
			self.create_dust_opening_receipt_se()

	def on_cancel(self):
		self.log_audit_action("Cancelled", self.status, "Cancelled")
		self.cancel_linked_stock_entries()

	# --- Validations ---

	def set_naming_series(self):
		series_map = {
			"Dust Refining": "RFN-DST-.YY.-.#####",
			"Work Order Refining": "RFN-MWO-.YY.-.#####",
			"Serial Number Refining": "RFN-SRN-.YY.-.#####",
			"Scrap Refining": "RFN-SCP-.YY.-.#####",
		}
		if self.refining_type in series_map:
			self.naming_series = series_map[self.refining_type]

	def validate_configuration(self):
		if not frappe.db.get_single_value(
			"Refining Configuration", "default_refining_warehouse"
		):
			frappe.msgprint(
				_("Please set Default Refining Warehouse in Refining Configuration.")
			)

		if (
			self.refining_type == "Dust Refining"
			and self.multiple_department
			and self.multiple_operation
		):
			frappe.throw(
				_("Choose either Multiple Operations OR Multiple Department, not both.")
			)

	def validate_warehouse(self):
		if not self.refining_warehouse:
			if self.refining_department:
				self.refining_warehouse = frappe.db.get_value(
					"Warehouse",
					{
						"department": self.refining_department,
						"warehouse_type": "Manufacturing",
					},
					"name",
				)
			if not self.refining_warehouse:
				self.refining_warehouse = frappe.db.get_single_value(
					"Refining Configuration", "default_refining_warehouse"
				)

		if not self.warehouse and self.department:
			self.warehouse = frappe.db.get_value(
				"Warehouse",
				{"department": self.department, "warehouse_type": "Manufacturing"},
				"name",
			)

	def validate_quantities(self):
		if self.refining_type == "Dust Refining":
			sys_qty = flt(self.system_quantity)
			phys_qty = flt(self.physical_quantity)
			self.difference_quantity = phys_qty - sys_qty
			if phys_qty <= 0:
				frappe.throw(_("Physical Quantity must be greater than zero."))

	def calculate_totals(self):
		self.gross_pure_weight = 0.0
		self.expected_recovery = 0.0
		for item in self.material_items:
			if item.purity:
				purity_pct = frappe.db.get_value(
					"Attribute Value", item.purity, "custom_purity_percentage"
				)
				if purity_pct:
					pure_weight = flt(item.qty) * (flt(purity_pct) / 100.0)
					self.gross_pure_weight += pure_weight
					self.expected_recovery += pure_weight

		self.refined_fine_weight = 0.0
		self.actual_recovery = 0.0
		for gold in self.refined_gold:
			pure_weight = flt(gold.pure_weight) or flt(gold.refining_gold_weight)
			self.refined_fine_weight += pure_weight
			self.actual_recovery += pure_weight

		self.refining_loss = self.gross_pure_weight - self.refined_fine_weight

		if self.expected_recovery > 0:
			self.recovery_percentage = (
				self.actual_recovery / self.expected_recovery
			) * 100.0
		else:
			self.recovery_percentage = 0.0

	def validate_recovery_distribution(self):
		total_recovered = 0.0
		for gold in self.refined_gold:
			total_recovered += flt(gold.refining_gold_weight)

		for dia in self.recovered_diamond:
			total_recovered += flt(dia.weight)

		for gem in self.recovered_gemstone:
			total_recovered += flt(gem.weight)

		total_input = 0.0
		if self.refining_type == "Serial Number Refining":
			total_input = sum(flt(row.gross_weight) for row in self.serial_no_details)
		else:
			total_input = sum(flt(item.qty) for item in self.material_items)

		# Allow a 0.1 margin for precision/rounding differences
		if total_recovered > total_input + 0.1:
			frappe.throw(
				_(
					"Total recovered weight ({0}) cannot exceed total input weight ({1})."
				).format(total_recovered, total_input)
			)

	# --- Action Handlers (Whitelisted for Client Scripts) ---

	@frappe.whitelist()
	def fetch_dust_balance(self):
		if not self.loss_item:
			frappe.throw(_("Loss Item is required."))

		if self.multiple_department:
			total_qty = 0.0
			for d in self.refining_department_detail:
				dept_wh = frappe.db.get_value(
					"Warehouse",
					{"department": d.department, "warehouse_type": "Manufacturing"},
					"name",
				)
				if dept_wh:
					total_qty += (
						frappe.db.get_value(
							"Bin",
							{"item_code": self.loss_item, "warehouse": dept_wh},
							"actual_qty",
						)
						or 0.0
					)
			self.system_quantity = total_qty
		else:
			if not self.warehouse:
				frappe.throw(_("Source Warehouse is required."))
			balance = (
				frappe.db.get_value(
					"Bin",
					{"item_code": self.loss_item, "warehouse": self.warehouse},
					"actual_qty",
				)
				or 0.0
			)
			self.system_quantity = balance

		self.save(ignore_permissions=True)
		return self.system_quantity

	@frappe.whitelist()
	def scan_mwo_action(self, barcode):
		mwo = frappe.db.get_value(
			"Manufacturing Work Order",
			{"name": barcode},
			["name", "manufacturing_order", "item_code", "qty", "metal_weight"],
			as_dict=True,
		)
		if not mwo:
			frappe.throw(_("Manufacturing Work Order {0} not found.").format(barcode))

		# Check if MWO already added
		for row in self.mwo_details:
			if row.manufacturing_work_order == mwo.name:
				frappe.throw(_("MWO {0} is already added.").format(mwo.name))

		self.append(
			"mwo_details",
			{
				"manufacturing_work_order": mwo.name,
				"parent_manufacturing_work_order": mwo.manufacturing_order,
				"item_code": mwo.item_code,
				"metal_weight": mwo.metal_weight,
				"pcs": mwo.qty,
			},
		)
		self.scan_mwo = ""
		self.build_material_table()
		# self.save(ignore_permissions=True)
		return True

	@frappe.whitelist()
	def scan_serial_no_action(self, barcode):
		serial_no = frappe.db.get_value(
			"Serial No",
			{"name": barcode, "status": "Active"},
			["name", "item_code", "warehouse"],
			as_dict=True,
		)
		if not serial_no:
			frappe.throw(_("Active Serial Number {0} not found.").format(barcode))

		if serial_no.warehouse != self.warehouse:
			frappe.throw(
				_("Serial Number {0} is not in Source Warehouse {1}.").format(
					barcode, self.warehouse
				)
			)

		for row in self.serial_no_details:
			if row.serial_number == serial_no.name:
				frappe.throw(
					_("Serial Number {0} is already added.").format(serial_no.name)
				)

		# fetch BOM details for pure weight, gross weight etc.
		bom_details = frappe.db.get_value(
			"BOM",
			{"item": serial_no.item_code, "is_active": 1},
			[
				"name",
				"metal_and_finding_weight",
				"gross_weight",
				"custom_net_pure_weight",
			],
			as_dict=True,
		)

		pure_weight = bom_details.custom_net_pure_weight if bom_details else 0.0
		gross_weight = bom_details.gross_weight if bom_details else 0.0
		net_weight = bom_details.metal_and_finding_weight if bom_details else 0.0

		self.append(
			"serial_no_details",
			{
				"serial_number": serial_no.name,
				"item_code": serial_no.item_code,
				"pure_weight": pure_weight,
				"gross_weight": gross_weight,
				"net_weight": net_weight,
				"pcs": 1,
			},
		)
		self.scan_serial_no = ""
		self.build_material_table()
		self.save(ignore_permissions=True)
		return True

	@frappe.whitelist()
	def scan_scrap_qr_action(self, barcode):
		# Assuming Scrap item is managed via Batch
		batch = frappe.db.get_value(
			"Batch", {"name": barcode}, ["name", "item"], as_dict=True
		)
		if not batch:
			frappe.throw(_("Batch {0} not found.").format(barcode))

		if self.scrap_item and batch.item != self.scrap_item:
			frappe.throw(
				_("Scanned item {0} does not match selected Scrap Item {1}.").format(
					batch.item, self.scrap_item
				)
			)

		qty = (
			frappe.db.get_value(
				"Bin",
				{
					"item_code": batch.item,
					"batch_no": batch.name,
					"warehouse": self.warehouse,
				},
				"actual_qty",
			)
			or 0.0
		)

		if qty <= 0:
			frappe.throw(
				_("No stock available for Batch {0} in {1}.").format(
					batch.name, self.warehouse
				)
			)

		self.append(
			"material_items",
			{
				"item_code": batch.item,
				"batch_no": batch.name,
				"warehouse": self.warehouse,
				"qty": qty,
				"source_type": "Scrap",
			},
		)
		self.scan_scrap_qr = ""
		self.save(ignore_permissions=True)
		return True

	@frappe.whitelist()
	def build_material_table(self):
		"""Consolidate materials from source documents (MWO, SN, etc.) into material_items."""
		self.set("material_items", [])

		if self.refining_type == "Work Order Refining":
			for mwo_row in self.mwo_details:
				# Fetch materials consumed by this MWO from MOP Log
				mop_logs = frappe.get_all(
					"MOP Log",
					filters={
						"manufacturing_work_order": mwo_row.manufacturing_work_order,
						"is_cancelled": 0,
					},
					fields=[
						"item_code",
						"batch_no",
						"to_warehouse as warehouse",
						"qty_change as qty",
						"manufacturing_operation",
					],
				)
				for log in mop_logs:
					if flt(log.qty) > 0:
						uom = frappe.db.get_value("Item", log.item_code, "stock_uom")
						self.append(
							"material_items",
							{
								"item_code": log.item_code,
								"batch_no": log.batch_no,
								"warehouse": log.warehouse,
								"qty": log.qty,
								"uom": uom,
								"source_type": "MWO",
								"purity": self.get_item_purity(log.item_code),
								"manufacturing_work_order": mwo_row.manufacturing_work_order,
								"manufacturing_operation": log.manufacturing_operation,
							},
						)

		elif self.refining_type == "Serial Number Refining":
			for sn_row in self.serial_no_details:
				# Add FG item directly, repack will handle decomposition
				self.append(
					"material_items",
					{
						"item_code": sn_row.item_code,
						"warehouse": self.warehouse,
						"qty": sn_row.pcs,
						"uom": "Nos",
						"serial_no": sn_row.serial_number,
						"source_type": "Serial Number",
						"purity": self.get_item_purity(sn_row.item_code),
					},
				)

		elif self.refining_type == "Dust Refining":
			if self.multiple_department:
				for d_row in self.refining_department_detail:
					dept_wh = frappe.db.get_value(
						"Warehouse",
						{
							"department": d_row.department,
							"warehouse_type": "Manufacturing",
						},
						"name",
					)
					if dept_wh:
						qty = (
							frappe.db.get_value(
								"Bin",
								{"item_code": self.loss_item, "warehouse": dept_wh},
								"actual_qty",
							)
							or 0.0
						)
						self.append(
							"material_items",
							{
								"item_code": self.loss_item,
								"warehouse": dept_wh,
								"qty": qty,
								"source_type": "Dust",
								"purity": self.get_item_purity(self.loss_item),
							},
						)
			else:
				# Single department dust
				qty = self.system_quantity
				self.append(
					"material_items",
					{
						"item_code": self.loss_item,
						"warehouse": self.warehouse,
						"qty": qty,
						"source_type": "Dust",
						"purity": self.get_item_purity(self.loss_item),
					},
				)

		# Group items item-wise
		grouped_items = {}
		for item in self.material_items:
			key = (item.item_code, item.batch_no, item.serial_no, item.warehouse)
			if key not in grouped_items:
				grouped_items[key] = {
					"item_code": item.item_code,
					"batch_no": item.batch_no,
					"serial_no": item.serial_no,
					"warehouse": item.warehouse,
					"qty": item.qty,
					"uom": item.uom,
					"source_type": item.source_type,
					"purity": item.purity,
					"manufacturing_work_order": item.manufacturing_work_order,
					"manufacturing_operation": item.manufacturing_operation,
				}
			else:
				grouped_items[key]["qty"] += item.qty

		self.set("material_items", [])
		for key, item_dict in grouped_items.items():
			self.append("material_items", item_dict)

	@frappe.whitelist()
	def receive_materials(self):
		self.require_refining_role(
			["Refining User", "Refining Manager", "System Manager"], "receive materials"
		)
		if self.status != "Submitted":
			frappe.throw(_("Can only receive materials if status is Submitted."))

		self.db_set("status", "Received")
		self.log_audit_action("Received", "Submitted", "Received")

		# Create duplicate entry per SOP
		duplicate = frappe.copy_doc(self)
		duplicate.status = "Draft"
		duplicate.material_transfer_se = None
		duplicate.repack_se = None
		duplicate.receiving_se = None
		duplicate.transfer_se = None
		duplicate.insert(ignore_permissions=True)
		frappe.msgprint(
			_("Duplicate Refining Entry {0} created in Draft status.").format(
				duplicate.name
			)
		)

	@frappe.whitelist()
	def generate_recovery_table(self, total_recovered_weight=None):
		"""Distribute gold recovery by input purity proportion."""
		self.require_refining_role(
			["Refining User", "Refining Manager", "System Manager"],
			"classify materials",
		)
		self.set("gold_recovery_details", [])
		self.auto_classify_recoverable_non_metal()

		input_purity_map = {}
		for item in self.material_items:
			if self.is_gold_item(item.item_code) and item.purity:
				pct = frappe.db.get_value(
					"Attribute Value", item.purity, "custom_purity_percentage"
				)
				if pct:
					pct_flt = flt(pct)
					input_purity_map.setdefault(pct_flt, 0.0)
					input_purity_map[pct_flt] += flt(item.qty)

		total_input_weight = sum(input_purity_map.values())
		total_recovered_weight = self.get_recovered_gold_total(total_recovered_weight)
		purity_maps = self.get_purity_distribution_maps(input_purity_map)

		for pmap in purity_maps:
			pmap_pct = flt(pmap.purity_percentage)
			input_weight = input_purity_map.get(pmap_pct, 0)
			if input_weight <= 0:
				continue

			recovered_weight = self.get_proportional_recovery_weight(
				input_weight, total_input_weight, total_recovered_weight
			)
			pure_gold_weight = input_weight * (pmap_pct / 100.0)
			self.append(
				"gold_recovery_details",
				{
					"karat": pmap.karat,
					"purity_percentage": pmap.purity_percentage,
					"item_code": pmap.item_template,
					"input_weight": input_weight,
					"pure_gold_weight": pure_gold_weight,
					"recovered_weight": recovered_weight,
					"loss_weight": max(input_weight - recovered_weight, 0),
					"recovery_pct": (
						(recovered_weight / input_weight) * 100.0
						if input_weight
						else 0.0
					),
				},
			)

		if total_recovered_weight:
			self.populate_refined_gold_from_distribution()
			self.calculate_totals()

		self.status = "Classified"
		self.log_audit_action("Classified", "Received", "Classified")
		self.save(ignore_permissions=True)

	@frappe.whitelist()
	def start_refining(self):
		self.require_refining_role(
			["Refining User", "Refining Manager", "System Manager"], "start refining"
		)
		if self.status != "Classified":
			frappe.throw(_("Materials must be classified before starting refining."))
		self.db_set("status", "Refining In Progress")
		self.log_audit_action("Refining Started", "Classified", "Refining In Progress")

	@frappe.whitelist()
	def distribute_recovered_gold(self, total_recovered_weight=None):
		"""Apply SOP proportional split after actual recovered gold is known."""
		self.require_refining_role(
			["Refining User", "Refining Manager", "System Manager"], "enter recovery"
		)
		if not self.gold_recovery_details:
			self.generate_recovery_table(total_recovered_weight=total_recovered_weight)

		total_input_weight = sum(
			flt(row.input_weight) for row in self.gold_recovery_details
		)
		total_recovered_weight = self.get_recovered_gold_total(total_recovered_weight)
		if total_input_weight <= 0:
			frappe.throw(_("Gold input weight is required for proportional recovery."))
		if total_recovered_weight <= 0:
			frappe.throw(
				_("Recovered gold weight is required for proportional recovery.")
			)
		if total_recovered_weight > total_input_weight + 0.1:
			frappe.throw(
				_(
					"Recovered gold weight ({0}) cannot exceed input gold weight ({1})."
				).format(total_recovered_weight, total_input_weight)
			)

		for row in self.gold_recovery_details:
			row.recovered_weight = self.get_proportional_recovery_weight(
				row.input_weight, total_input_weight, total_recovered_weight
			)
			row.loss_weight = max(flt(row.input_weight) - flt(row.recovered_weight), 0)
			row.recovery_pct = (
				(flt(row.recovered_weight) / flt(row.input_weight)) * 100.0
				if flt(row.input_weight)
				else 0.0
			)

		self.populate_refined_gold_from_distribution()
		self.calculate_totals()
		self.status = "Recovery Entered"
		self.log_audit_action(
			"Recovery Entered", "Refining In Progress", "Recovery Entered"
		)
		self.save(ignore_permissions=True)

	@frappe.whitelist()
	def verify_recovery(self):
		self.require_refining_role(
			["Refining Manager", "System Manager"], "verify recovery"
		)
		self.validate_recovery_distribution()
		self.db_set("status", "Recovery Verified")
		self.log_audit_action(
			"Recovery Verified", "Recovery Entered", "Recovery Verified"
		)

	@frappe.whitelist()
	def complete_refining(self):
		self.require_refining_role(
			["Refining Manager", "System Manager"], "complete refining"
		)
		if self.status != "Recovery Verified":
			frappe.throw(_("Recovery must be verified before completing."))

		self.create_repack_se()

		if self.refining_type == "Work Order Refining":
			# Zero out operation qty
			for mwo in self.mwo_details:
				frappe.db.set_value(
					"Manufacturing Work Order",
					mwo.manufacturing_work_order,
					"status",
					"Completed",
				)
				frappe.db.set_value(
					"Manufacturing Work Order",
					mwo.manufacturing_work_order,
					"current_operation_qty",
					0,
				)

		elif self.refining_type == "Serial Number Refining":
			for sn in self.serial_no_details:
				frappe.db.set_value("Serial No", sn.serial_number, "status", "Inactive")
				bom = frappe.db.get_value(
					"BOM", {"item": sn.item_code, "is_active": 1}, "name"
				)
				if bom:
					frappe.db.set_value("BOM", bom, "is_active", 0)

		# If there is refining loss, convert to dust and move to scrap warehouse
		if self.refining_loss > 0 and self.refining_type != "Scrap Refining":
			self.create_scrap_transfer_se()

		self.db_set("status", "Completed")
		self.log_audit_action("Completed", "Recovery Verified", "Completed")

	@frappe.whitelist()
	def transfer_recovered_materials(self):
		self.require_refining_role(
			["Refining Manager", "System Manager"], "transfer recovered materials"
		)
		if self.status != "Completed":
			frappe.throw(_("Can only transfer if Completed."))

		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Transfer"
		se.purpose = "Material Transfer"
		se.company = self.company
		se.custom_refining_entry = self.name

		# Transfer all recovered items to main department
		for gold in self.refined_gold:
			batch_no = None
			for b in self.batch_tracking:
				if b.output_item == gold.item_code:
					batch_no = b.output_batch
					break

			se.append(
				"items",
				{
					"item_code": gold.item_code,
					"qty": gold.refining_gold_weight,
					"uom": "Gram",
					"s_warehouse": self.refining_warehouse,
					"t_warehouse": self.warehouse,  # Original warehouse
					"batch_no": batch_no,
					"use_serial_batch_fields": 1,
				},
			)

		for dia in self.recovered_diamond:
			conv_item = self.convert_diamond_item_code(dia.item)
			batch_no = None
			for b in self.batch_tracking:
				if b.output_item == conv_item:
					batch_no = b.output_batch
					break

			se.append(
				"items",
				{
					"item_code": conv_item,
					"qty": dia.weight,
					"uom": "Carat",
					"s_warehouse": self.refining_warehouse,
					"t_warehouse": self.warehouse,
					"batch_no": batch_no,
					"use_serial_batch_fields": 1,
				},
			)

		for gem in self.recovered_gemstone:
			batch_no = None
			for b in self.batch_tracking:
				if b.output_item == gem.item:
					batch_no = b.output_batch
					break

			se.append(
				"items",
				{
					"item_code": gem.item,
					"qty": gem.weight,
					"uom": "Carat",
					"s_warehouse": self.refining_warehouse,
					"t_warehouse": self.warehouse,
					"batch_no": batch_no,
					"use_serial_batch_fields": 1,
				},
			)

		se.insert()
		se.submit()
		self.db_set("transfer_se", se.name)
		self.db_set("status", "Transferred")
		self.log_audit_action(
			"Transferred", "Completed", "Transferred", stock_entry_ref=se.name
		)

	# --- Stock Entry Automation ---

	def create_material_transfer_se(self):
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Transfer"
		se.purpose = "Material Transfer"
		se.company = self.company
		se.custom_refining_entry = self.name

		for item in self.material_items:
			if self.is_dust_opening_item(item):
				continue

			s_wh = item.warehouse or self.warehouse
			has_batch = frappe.db.get_value("Item", item.item_code, "has_batch_no")

			if has_batch and not item.batch_no:
				allocations = self.allocate_fifo_batches(item.item_code, s_wh, item.qty)
				for alloc in allocations:
					se.append(
						"items",
						{
							"item_code": item.item_code,
							"qty": alloc["qty"],
							"uom": item.uom,
							"s_warehouse": s_wh,
							"t_warehouse": self.refining_warehouse,
							"batch_no": alloc["batch_no"],
							"serial_no": item.serial_no,
							"use_serial_batch_fields": 1,
						},
					)
			else:
				se.append(
					"items",
					{
						"item_code": item.item_code,
						"qty": item.qty,
						"uom": item.uom,
						"s_warehouse": s_wh,
						"t_warehouse": self.refining_warehouse,
						"batch_no": item.batch_no,
						"serial_no": item.serial_no,
						"use_serial_batch_fields": 1,
					},
				)

		if se.items:
			se.insert()
			se.submit()
			self.db_set("material_transfer_se", se.name)
			self.log_audit_action(
				"Stock Entry Created", None, None, stock_entry_ref=se.name
			)

	def create_repack_se(self):
		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Repack"
		se.purpose = "Repack"
		se.company = self.company
		se.custom_refining_entry = self.name

		# Input items (consumed)
		for item in self.material_items:
			se.append(
				"items",
				{
					"item_code": item.item_code,
					"qty": item.qty,
					"uom": item.uom,
					"s_warehouse": self.refining_warehouse,
					"batch_no": item.batch_no,
					"serial_no": item.serial_no,
					"use_serial_batch_fields": 1,
				},
			)

		# Output items (produced - Pure Gold, Diamond, Gemstone)
		for gold in self.refined_gold:
			new_batch = self.auto_create_batch(gold.item_code)
			se.append(
				"items",
				{
					"item_code": gold.item_code,
					"qty": gold.refining_gold_weight,
					"uom": "Gram",
					"t_warehouse": self.refining_warehouse,
					"batch_no": new_batch,
					"is_finished_item": 1,
					"use_serial_batch_fields": 1,
				},
			)
			self.append(
				"batch_tracking",
				{
					"output_item": gold.item_code,
					"output_batch": new_batch,
					"output_warehouse": self.refining_warehouse,
					"output_qty": gold.refining_gold_weight,
				},
			)

		for dia in self.recovered_diamond:
			conv_item = self.convert_diamond_item_code(dia.item)
			new_batch = self.auto_create_batch(conv_item)
			se.append(
				"items",
				{
					"item_code": conv_item,
					"qty": dia.weight,
					"uom": "Carat",
					"t_warehouse": self.refining_warehouse,
					"batch_no": new_batch,
					"is_finished_item": 1,
					"use_serial_batch_fields": 1,
				},
			)
			if new_batch:
				self.append(
					"batch_tracking",
					{
						"output_item": conv_item,
						"output_batch": new_batch,
						"output_warehouse": self.refining_warehouse,
						"output_qty": dia.weight,
					},
				)

		for gem in self.recovered_gemstone:
			new_batch = self.auto_create_batch(gem.item)
			se.append(
				"items",
				{
					"item_code": gem.item,
					"qty": gem.weight,
					"uom": "Carat",
					"t_warehouse": self.refining_warehouse,
					"batch_no": new_batch,
					"is_finished_item": 1,
					"use_serial_batch_fields": 1,
				},
			)
			if new_batch:
				self.append(
					"batch_tracking",
					{
						"output_item": gem.item,
						"output_batch": new_batch,
						"output_warehouse": self.refining_warehouse,
						"output_qty": gem.weight,
					},
				)

		se.insert()
		se.submit()
		self.db_set("repack_se", se.name)
		self.log_audit_action(
			"Stock Entry Created", None, None, stock_entry_ref=se.name
		)

	def create_dust_opening_receipt_se(self):
		dust_item = self.get_dust_item()
		dust_qty = self.get_dust_opening_qty(dust_item)
		if not dust_item or dust_qty <= 0:
			return
		dust_batch = self.get_dust_opening_batch(dust_item)

		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Receipt"
		se.purpose = "Material Receipt"
		se.company = self.company
		se.custom_refining_entry = self.name

		se.append(
			"items",
			{
				"item_code": dust_item,
				"qty": dust_qty,
				"t_warehouse": self.refining_warehouse,
				"batch_no": dust_batch,
				"use_serial_batch_fields": 1,
			},
		)
		se.insert()
		se.submit()
		self.db_set("receiving_se", se.name)
		self.log_audit_action(
			"Stock Entry Created", None, None, stock_entry_ref=se.name
		)

	def cancel_linked_stock_entries(self):
		ses = frappe.get_all(
			"Stock Entry", filters={"custom_refining_entry": self.name, "docstatus": 1}
		)
		for se in ses:
			doc = frappe.get_doc("Stock Entry", se.name)
			doc.cancel()
			self.log_audit_action(
				"Cancelled Stock Entry", None, None, stock_entry_ref=se.name
			)

	def create_scrap_transfer_se(self):
		scrap_warehouse = frappe.db.get_single_value(
			"Refining Configuration", "scrap_warehouse"
		)
		dust_item = frappe.db.get_single_value(
			"Refining Configuration", "default_dust_item"
		)
		if scrap_warehouse and dust_item:
			se = frappe.new_doc("Stock Entry")
			se.stock_entry_type = "Material Transfer"
			se.purpose = "Material Transfer"
			se.company = self.company
			se.custom_refining_entry = self.name

			se.append(
				"items",
				{
					"item_code": dust_item,
					"qty": self.refining_loss,
					"uom": "Gram",
					"s_warehouse": self.refining_warehouse,
					"t_warehouse": scrap_warehouse,
					"use_serial_batch_fields": 1,
				},
			)
			se.insert()
			se.submit()
			self.log_audit_action(
				"Scrap Transferred", None, None, stock_entry_ref=se.name
			)

	# --- Utils ---

	def auto_create_batch(self, item_code):
		if not frappe.db.get_single_value(
			"Refining Configuration", "auto_create_batch"
		):
			return None

		item = frappe.get_doc("Item", item_code)
		if not item.has_batch_no:
			return None

		batch = frappe.new_doc("Batch")
		batch.item = item_code
		# set batch ID based on naming series or let it auto name
		batch.insert()
		self.log_audit_action("Batch Created", None, None, batch_ref=batch.name)
		return batch.name

	def is_gold_item(self, item_code):
		variant_of = frappe.db.get_value("Item", item_code, "variant_of")
		item_group = frappe.db.get_value("Item", item_code, "item_group")
		return (
			variant_of == "M"
			or item_group in ("Metal", "Gold")
			or (item_code and item_code.upper().startswith(("M-", "GOLD")))
		)

	def is_diamond_item(self, item_code):
		variant_of = frappe.db.get_value("Item", item_code, "variant_of")
		item_group = frappe.db.get_value("Item", item_code, "item_group")
		return (
			variant_of in ("D", "DL")
			or item_group == "Diamond"
			or (item_code and item_code.upper().startswith(("D-", "DL-")))
		)

	def is_gemstone_item(self, item_code):
		variant_of = frappe.db.get_value("Item", item_code, "variant_of")
		item_group = frappe.db.get_value("Item", item_code, "item_group")
		return (
			variant_of in ("G", "GL")
			or item_group in ("Gemstone", "Gem Stone")
			or (item_code and item_code.upper().startswith(("G-", "GL-")))
		)

	def auto_classify_recoverable_non_metal(self):
		diamond_items = {row.item for row in self.recovered_diamond}
		gemstone_items = {row.item for row in self.recovered_gemstone}
		for item in self.material_items:
			if (
				self.is_diamond_item(item.item_code)
				and item.item_code not in diamond_items
			):
				self.append(
					"recovered_diamond",
					{"item": item.item_code, "weight": item.qty, "pcs": 1},
				)
				diamond_items.add(item.item_code)
			elif (
				self.is_gemstone_item(item.item_code)
				and item.item_code not in gemstone_items
			):
				self.append(
					"recovered_gemstone",
					{"item": item.item_code, "weight": item.qty, "pcs": 1},
				)
				gemstone_items.add(item.item_code)

	def get_recovered_gold_total(self, total_recovered_weight=None):
		if total_recovered_weight is not None:
			return flt(total_recovered_weight)
		if flt(self.actual_recovery):
			return flt(self.actual_recovery)
		refined_total = sum(flt(row.refining_gold_weight) for row in self.refined_gold)
		return refined_total

	def get_proportional_recovery_weight(
		self, input_weight, total_input_weight, total_recovered_weight
	):
		if total_input_weight <= 0 or total_recovered_weight <= 0:
			return 0.0
		return flt(total_recovered_weight) * (
			flt(input_weight) / flt(total_input_weight)
		)

	def get_purity_distribution_maps(self, input_purity_map):
		purity_maps = frappe.db.get_all(
			"Refining Purity Map",
			fields=[
				"karat",
				"purity_percentage",
				"item_template",
				"metal_touch",
				"metal_purity",
			],
		)
		mapped_percentages = {flt(row.purity_percentage) for row in purity_maps}
		for purity_percentage in input_purity_map:
			if purity_percentage in mapped_percentages:
				continue
			purity_maps.append(
				frappe._dict(
					{
						"karat": self.get_karat_from_percentage(purity_percentage),
						"purity_percentage": purity_percentage,
						"item_template": self.get_default_recovered_gold_item(),
						"metal_touch": None,
						"metal_purity": None,
					}
				)
			)
		return purity_maps

	def get_karat_from_percentage(self, purity_percentage):
		karat = round(flt(purity_percentage) * 24 / 100, 2)
		return ("{0:g}KT").format(karat)

	def get_default_recovered_gold_item(self):
		return (
			frappe.db.get_single_value(
				"Refining Configuration", "default_pure_gold_item"
			)
			or frappe.db.get_single_value(
				"Refining Configuration", "default_recovered_item"
			)
			or frappe.db.get_value("Item", {"variant_of": "M", "disabled": 0}, "name")
		)

	def populate_refined_gold_from_distribution(self):
		self.set("refined_gold", [])
		for row in self.gold_recovery_details:
			if flt(row.recovered_weight) <= 0:
				continue
			self.append(
				"refined_gold",
				{
					"item_code": row.item_code,
					"refining_gold_weight": row.recovered_weight,
					"pure_weight": row.recovered_weight,
					"metal_purity": row.karat,
				},
			)

	def get_dust_item(self):
		return self.dust_item or frappe.db.get_single_value(
			"Refining Configuration", "default_dust_item"
		)

	def is_dust_opening_item(self, item):
		dust_item = self.get_dust_item()
		return (
			self.refining_type == "Dust Refining"
			and dust_item
			and item.item_code == dust_item
		)

	def get_dust_opening_qty(self, dust_item):
		dust_qty = 0.0
		for item in self.material_items:
			if item.item_code == dust_item:
				dust_qty += flt(item.qty)
		if not dust_qty and flt(self.difference_quantity) > 0:
			dust_qty += flt(self.additional_dust_qty) or flt(self.difference_quantity)
		return dust_qty

	def ensure_dust_opening_material_row(self):
		dust_item = self.get_dust_item()
		if not dust_item:
			return
		if any(row.item_code == dust_item for row in self.material_items):
			return

		dust_qty = flt(self.additional_dust_qty) or max(
			flt(self.difference_quantity), 0
		)
		if dust_qty <= 0:
			return

		self.append(
			"material_items",
			{
				"item_code": dust_item,
				"warehouse": self.refining_warehouse,
				"qty": dust_qty,
				"uom": frappe.db.get_value("Item", dust_item, "stock_uom") or "Gram",
				"source_type": "Dust",
				"purity": self.get_item_purity(dust_item),
				"batch_no": self.get_dust_opening_batch(dust_item),
			},
		)

	def get_dust_opening_batch(self, dust_item):
		if not dust_item:
			return None
		if not frappe.db.get_value("Item", dust_item, "has_batch_no"):
			return None
		for row in self.material_items:
			if row.item_code == dust_item and row.batch_no:
				return row.batch_no
		batch_no = self.auto_create_batch(dust_item)
		if not batch_no:
			batch = frappe.new_doc("Batch")
			batch.item = dust_item
			batch.insert()
			batch_no = batch.name
			self.log_audit_action("Batch Created", None, None, batch_ref=batch_no)
		for row in self.material_items:
			if row.item_code == dust_item and not row.batch_no:
				row.batch_no = batch_no
		return batch_no

	def require_refining_role(self, allowed_roles, action):
		if frappe.session.user == "Administrator":
			return
		user_roles = set(frappe.get_roles(frappe.session.user))
		if user_roles.intersection(set(allowed_roles)):
			return
		frappe.throw(
			_("Only users with {0} can {1}.").format(", ".join(allowed_roles), action),
			frappe.PermissionError,
		)

	def convert_diamond_item_code(self, item_code):
		# Example: DL-NT-RO-4-+00-0 -> D-NT-RO-4-+00-0
		if item_code and item_code.startswith("DL-"):
			return "D-" + item_code[3:]
		return item_code

	def get_item_purity(self, item_code):
		purity = frappe.db.get_value(
			"Item Variant Attribute",
			{"parent": item_code, "attribute": "Purity"},
			"attribute_value",
		)
		return purity

	def allocate_fifo_batches(self, item_code, warehouse, required_qty):
		# Fetch batches with stock in FIFO order
		batches = frappe.db.sql(
			"""
			SELECT batch_no, actual_qty
			FROM `tabBin`
			WHERE item_code = %s AND warehouse = %s AND actual_qty > 0
			ORDER BY creation ASC
		""",
			(item_code, warehouse),
			as_dict=1,
		)

		allocations = []
		remaining_qty = flt(required_qty)
		for b in batches:
			if remaining_qty <= 0:
				break
			alloc_qty = min(flt(b.actual_qty), remaining_qty)
			allocations.append({"batch_no": b.batch_no, "qty": alloc_qty})
			remaining_qty -= alloc_qty

		if remaining_qty > 0:
			# Even after all batches, some qty is left.
			allocations.append({"batch_no": None, "qty": remaining_qty})

		return allocations

	@frappe.whitelist()
	def get_linked_stock_entries_html(self):
		html = "<h4>Raw Materials Consolidate</h4>"
		if not self.material_items:
			return html + "<p>No materials found.</p>"

		html += "<table class='table table-bordered'><thead><tr><th>Item Code</th><th>Warehouse</th><th>Qty</th><th>Batch</th></tr></thead><tbody>"
		for item in self.material_items:
			html += f"<tr><td>{item.item_code}</td><td>{item.warehouse}</td><td>{item.qty}</td><td>{item.batch_no or ''}</td></tr>"
		html += "</tbody></table>"
		return html

	def log_audit_action(
		self,
		action_type,
		from_state,
		to_state,
		stock_entry_ref=None,
		batch_ref=None,
		serial_no_ref=None,
	):
		if not frappe.db.get_single_value(
			"Refining Configuration", "enable_audit_logging"
		):
			return

		log = frappe.new_doc("Refining Audit Log")
		log.refining_entry = self.name
		log.action_type = action_type
		log.action_timestamp = now_datetime()
		log.action_by = frappe.session.user
		log.from_state = from_state
		log.to_state = to_state
		log.stock_entry_ref = stock_entry_ref
		log.batch_ref = batch_ref
		log.serial_no_ref = serial_no_ref
		log.ip_address = (
			frappe.local.request_ip
			if hasattr(frappe.local, "request_ip") and frappe.local.request_ip
			else ""
		)
		log.insert(ignore_permissions=True)
