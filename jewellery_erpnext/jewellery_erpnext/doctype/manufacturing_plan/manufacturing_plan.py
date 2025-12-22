# Copyright (c) 2023, Nirali and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
    make_manufacturing_order,
)


class ManufacturingPlan(Document):
    def on_submit(self):
        is_subcontracting = False
        customer_diamond_data = frappe._dict()
        frappe.db.sql(
            """
					UPDATE `tabSales Order Item` soi
					JOIN `tabManufacturing Plan Table` mpt
						ON soi.name = mpt.docname
					JOIN `tabManufacturing Plan` mp
						ON (mpt.parent = mp.name AND mp.name = %(mp_name)s)
					JOIN `tabSales Order` so
						ON (soi.parent = so.name AND so.docstatus = 1)
					SET
						soi.manufacturing_order_qty =
							COALESCE(soi.manufacturing_order_qty, 0)
							+ COALESCE(mpt.manufacturing_order_qty, 0)
							+ COALESCE(mpt.subcontracting_qty, 0),
						so.modified = NOW(),
						so.modified_by = %(modified_by)s
				""",
            {
                "mp_name": self.name,
                "modified_by": frappe.session.user,
            },
        )
        for row in self.manufacturing_plan_table:
            if row.docname:
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
            if not row.manufacturing_bom:
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

    doc_type, docname = (
        ("Sales Order Item", row.docname)
        if row.sales_order
        else ("Manufacturing Work Order", row.mwo)
    )

    fields = ["metal_type", "metal_touch", "metal_colour"]
    if row.mwo:
        fields.append("master_bom")

    so_det = (
        frappe.get_value(doc_type, docname, fields, as_dict=1)
        if (row.sales_order or row.mwo)
        else {}
    )

    master_bom = None
    if doc.select_manufacture_order == "Manufacturing":
        master_bom = row.manufacturing_bom
    elif (
        doc.select_manufacture_order == "Repair"
        and row.order_form_type == "Repair Order"
    ):
        master_bom = row.serial_id_bom

    if master_bom:
        bom_details = frappe.db.get_value(
            "BOM", master_bom, ["metal_type_", "metal_colour", "metal_touch"], as_dict=1
        )
        if bom_details:
            so_det.metal_type = bom_details.get("metal_type_") or so_det.metal_type_
            so_det.metal_colour = bom_details.get("metal_colour") or so_det.metal_colour
            so_det.metal_touch = bom_details.get("metal_touch") or so_det.metal_touch

    if row.diamond_quality and not frappe.db.get_value(
        "Customer", row.customer, "is_internal_customer"
    ):
        key = (row.customer, row.diamond_quality)
        if row.customer_diamond == "Yes":
            if not customer_diamond_data.get(key):
                diamond_grade_data = frappe.db.get_value(
                    "Customer Diamond Grade",
                    {"parent": row.customer, "diamond_quality": row.diamond_quality},
                    [
                        "diamond_grade_1",
                        "diamond_grade_2",
                        "diamond_grade_3",
                        "diamond_grade_4",
                    ],
                )
                for grade in diamond_grade_data:
                    if frappe.db.get_value(
                        "Attribute Value", grade, "is_customer_diamond_quality"
                    ):
                        customer_diamond_data[key] = grade
                        break
        else:
            if not customer_diamond_data.get(key):
                customer_diamond_data[key] = frappe.db.get_value(
                    "Customer Diamond Grade",
                    {"parent": row.customer, "diamond_quality": row.diamond_quality},
                    "diamond_grade_1",
                )
        so_det.diamond_grade = customer_diamond_data.get(key)
        if not so_det.diamond_grade and not frappe.db.get_value(
            "Item", row.item_code, "has_batch_no"
        ):
            frappe.throw(
                _("Diamond Grade is not mentioned in customer {0}").format(row.customer)
            )

    for i in range(0, cnt):
        make_manufacturing_order(doc, row, master_bom=master_bom, so_det=so_det)
    frappe.msgprint(_("Parent Manufacturing Order Created"))


def create_subcontracting_order(doc):
    pass


# for row in doc.manufacturing_plan_table:
# make_subcontracting_order(doc)
