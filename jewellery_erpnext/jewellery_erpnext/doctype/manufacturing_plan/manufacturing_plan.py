# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class ManufacturingPlan(Document):
    def on_submit(self):
        is_subcontracting = False
        customer_diamond_data = frappe._dict()
        for row in self.manufacturing_plan_table:
            if row.docname:
                frappe.db.sql(
                    """
					UPDATE `tabSales Order Item`
					SET manufacturing_order_qty = COALESCE(manufacturing_order_qty, 0) + %s,
					modified = NOW(), modified_by = %s
					WHERE name = %s
					""",
                    (
                        (
                            cint(row.manufacturing_order_qty)
                            + cint(row.subcontracting_qty)
                        ),
                        frappe.session.user,
                        row.docname,
                    ),
                )
            create_manufacturing_order(self, row, customer_diamond_data)
            if row.subcontracting:
                is_subcontracting = True
                # create_subcontracting_order(self, row)
            if row.manufacturing_bom is None:
                frappe.throw(f"Row:{row.idx} Manufacturing Bom Missing")

        if is_subcontracting:
            create_subcontracting_order(self)

    def validate(self):
        self.validate_qty_with_bom_creation()

    def validate_qty_with_bom_creation(self):
        total = 0
        for row in self.manufacturing_plan_table:
            # Validate Qty
            if not row.subcontracting:
                row.subcontracting_qty = 0
                row.supplier = None
            if (row.manufacturing_order_qty + row.subcontracting_qty) > row.pending_qty:
                error_message = _(
                    "Row #{0}: Total Order qty cannot be greater than {1}"
                ).format(row.idx, row.pending_qty)
                frappe.throw(error_message)
            total += cint(row.manufacturing_order_qty) + cint(row.subcontracting_qty)
            if row.qty_per_manufacturing_order == 0:
                frappe.throw(_("Qty per Manufacturing Order Can not  be 0"))

            # Set Manufacturing BOM if not set
            if not row.manufacturing_bom and frappe.db.exists("BOM", row.bom):
                row.manufacturing_bom = row.bom
        self.total_planned_qty = total

    @frappe.whitelist()
    def get_items_for_production(self):
        if self.select_manufacture_order in ["Manufacturing", "Repair"]:
            SalesOrderItem = frappe.qb.DocType("Sales Order Item")
            Item = frappe.qb.DocType("Item")
            SalesOrder = frappe.qb.DocType("Sales Order")

            query = (
                frappe.qb.from_(SalesOrderItem)
                .join(Item)
                .on(SalesOrderItem.item_code == Item.name)
                .join(SalesOrder)
                .on(SalesOrderItem.parent == SalesOrder.name)
                .select(
                    SalesOrderItem.name.as_("docname"),
                    SalesOrderItem.parent.as_("sales_order"),
                    SalesOrderItem.item_code,
                    SalesOrderItem.bom,
                    SalesOrder.customer,
                    Item.mould.as_("mould_no"),
                    Item.master_bom.as_("master_bom"),
                    SalesOrderItem.diamond_quality,
                    SalesOrderItem.custom_customer_sample.as_("customer_sample"),
                    SalesOrderItem.custom_customer_voucher_no.as_(
                        "customer_voucher_no"
                    ),
                    SalesOrderItem.custom_customer_gold.as_("customer_gold"),
                    SalesOrderItem.custom_customer_diamond.as_("customer_diamond"),
                    SalesOrderItem.custom_customer_stone.as_("customer_stone"),
                    SalesOrderItem.custom_customer_good.as_("customer_good"),
                    SalesOrderItem.custom_customer_weight.as_("customer_weight"),
                    (SalesOrderItem.qty - SalesOrderItem.manufacturing_order_qty).as_(
                        "pending_qty"
                    ),
                    SalesOrderItem.order_form_type,
                    SalesOrderItem.custom_repair_type.as_("repair_type"),
                    SalesOrderItem.custom_product_type.as_("product_type"),
                    SalesOrderItem.serial_no,
                    SalesOrderItem.serial_id_bom,
                )
                .where(
                    (SalesOrderItem.parent.isin(self.docs_to_append))
                    & (SalesOrderItem.qty > SalesOrderItem.manufacturing_order_qty)
                )
            )

            if self.setting_type:
                query = query.where(SalesOrderItem.setting_type == self.setting_type)

            items = query.run(as_dict=True)

            self.manufacturing_plan_table = []
            for item_row in items:
                bom = item_row.get("bom") or item_row.get("master_bom")
                if bom:
                    item_row["manufacturing_order_qty"] = item_row.get("pending_qty")
                    if self.is_subcontracting:
                        item_row["subcontracting"] = self.is_subcontracting
                        item_row["subcontracting_qty"] = item_row.get("pending_qty")
                        item_row["supplier"] = self.supplier
                        item_row["estimated_delivery_date"] = self.estimated_date
                        item_row["purchase_type"] = self.purchase_type
                        item_row["manufacturing_order_qty"] = 0

                    item_row["qty_per_manufacturing_order"] = 1
                    item_row["bom"] = bom
                    item_row["order_form_type"] = item_row.get("order_form_type")
                    self.append("manufacturing_plan_table", item_row)
                else:
                    frappe.throw(
                        _(
                            f"Sales Order BOM Not Found.</br>Please Set Master BOM for <b>{item_row.get("item_code")}</b> into Item Master"
                        )
                    )
        else:
            self.manufacturing_plan_table = []

        mwo_data = frappe.db.sql(
            """
			SELECT
				item_code,
				SUM(qty) AS total_qty,
				MIN(name) AS mwo
			FROM `tabManufacturing Work Order`
			WHERE name IN %(docs)s
			GROUP BY item_code
		""",
            {"docs": tuple(self.docs_to_append)},
            as_dict=True,
        )

        for row in mwo_data:
            qty = row["total_qty"]
            self.append(
                "manufacturing_plan_table",
                {
                    "item_code": row["item_code"],
                    "pending_qty": qty,
                    "manufacturing_order_qty": qty,
                    "qty_per_manufacturing_order": qty,
                    "mwo": row["mwo"],
                },
            )


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_pending_ppo_sales_order(doctype, txt, searchfield, start, page_len, filters):
    SalesOrder = frappe.qb.DocType("Sales Order")
    SalesOrderItem = frappe.qb.DocType("Sales Order Item")

    conditions = (
        (SalesOrderItem.qty > SalesOrderItem.manufacturing_order_qty)
        & (SalesOrderItem.order_form_type != "Repair Order")
        & (SalesOrder.custom_repair_order_form.isnull())
    )

    if txt:
        conditions &= SalesOrder.name.like(f"%{txt}%")

    if customer := filters.get("customer"):
        conditions &= SalesOrder.customer == customer

    if company := filters.get("company"):
        conditions &= SalesOrder.company == company

    if branch := filters.get("branch"):
        conditions &= SalesOrder.branch == branch

    if txn_date := filters.get("transaction_date"):
        conditions &= SalesOrder.transaction_date == txn_date

    query = (
        frappe.qb.from_(SalesOrder)
        .distinct()
        .from_(SalesOrderItem)
        .select(
            SalesOrder.name,
            SalesOrder.transaction_date,
            SalesOrder.company,
            SalesOrder.customer,
        )
        .where(
            (SalesOrder.name == SalesOrderItem.parent)
            & (SalesOrder.docstatus == 1)
            & conditions
        )
        .orderby(SalesOrder.transaction_date, order=frappe.qb.desc)
        .limit(page_len)
        .offset(start)
    )
    so_data = query.run(as_dict=True)

    return so_data


@frappe.whitelist()
def get_details_to_append(source_names, target_doc=None):
    if not target_doc:
        target_doc = frappe.new_doc("Manufacturing Plan")
    elif isinstance(target_doc, str):
        target_doc = frappe.get_doc(json.loads(target_doc))
    target_doc.docs_to_append = json.loads(source_names)
    target_doc.get_items_for_production()
    return target_doc


@frappe.whitelist()
def map_docs(method, source_names, target_doc, args=None):
    method = frappe.get_attr(frappe.override_whitelisted_method(method))
    if method not in frappe.whitelisted:
        raise frappe.PermissionError
    _args = (
        (source_names, target_doc, json.loads(args))
        if args
        else (source_names, target_doc)
    )
    target_doc = method(*_args)
    return target_doc


def create_manufacturing_order(doc, row, customer_diamond_data):
    cnt = int(row.manufacturing_order_qty / row.qty_per_manufacturing_order)

    if not cnt:
        return

    frappe.msgprint(_("Parent Manufacturing Order Created"))


def create_subcontracting_order(doc):
    pass


# for row in doc.manufacturing_plan_table:
# make_subcontracting_order(doc)
