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
        # customer_diamond_data removed for memory efficiency
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

        # Bulk Fetching Data
        cache_data = self.get_manufacturing_plan_data()
        frappe.flags.is_manufactur_order_created = False
        for row in self.manufacturing_plan_table:
            if row.docname:
                create_manufacturing_order(self, row, cache_data)
                frappe.flags.is_manufactur_order_created = True
                if row.subcontracting:
                    is_subcontracting = True
                    # create_subcontracting_order(self, row)
                if row.manufacturing_bom is None:
                    frappe.throw(f"Row:{row.idx} Manufacturing Bom Missing")
        if frappe.flags.is_manufactur_order_created:
            frappe.msgprint(_("Manufacturing Orders Created Successfully"))

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

    def get_manufacturing_plan_data(self):
        so_items = set()
        mwo_items = set()
        bom_names = set()
        customer_names = set()
        item_codes = set()
        customer_diamond_keys = set()

        for row in self.manufacturing_plan_table:
            if row.docname:
                so_items.add(row.docname)
            if row.mwo:
                mwo_items.add(row.mwo)
            if row.manufacturing_bom:
                bom_names.add(row.manufacturing_bom)
            if row.serial_id_bom:
                bom_names.add(row.serial_id_bom)
            if row.customer:
                customer_names.add(row.customer)
            if row.item_code:
                item_codes.add(row.item_code)
            if row.customer and row.diamond_quality:
                customer_diamond_keys.add((row.customer, row.diamond_quality))

        so_data_map = fetch_doc_map(
            "Sales Order Item",
            so_items,
            ["name", "metal_type", "metal_touch", "metal_colour", "diamond_grade"],
        )

        mwo_data_map = fetch_doc_map(
            "Manufacturing Work Order",
            mwo_items,
            ["name", "metal_type", "metal_touch", "metal_colour", "master_bom"],
        )

        bom_data_map = fetch_doc_map(
            "BOM", bom_names, ["name", "metal_type_", "metal_colour", "metal_touch"]
        )

        customer_data_map = fetch_doc_map(
            "Customer", customer_names, ["name", "is_internal_customer"]
        )

        # Fetch Customer Diamond Grades
        customer_diamond_grade_map = {}
        if customer_diamond_keys:
            # We filter by 'parent' IN customer_names.
            cust_grades = frappe.get_all(
                "Customer Diamond Grade",
                filters={"parent": ["in", list(customer_names)]},
                fields=[
                    "parent",
                    "diamond_quality",
                    "diamond_grade_1",
                    "diamond_grade_2",
                    "diamond_grade_3",
                    "diamond_grade_4",
                ],
            )
            for cg in cust_grades:
                customer_diamond_grade_map[(cg.parent, cg.diamond_quality)] = cg

        item_data_map = fetch_doc_map("Item", item_codes, ["name", "has_batch_no"])

        # We can fetch all attribute values that are customer diamond qualities just in case.
        attr_values = frappe.get_all(
            "Attribute Value",
            filters={"is_customer_diamond_quality": 1},
            fields=["name"],
        )
        attribute_value_set = {d.name for d in attr_values}

        return {
            "so_data": so_data_map,
            "mwo_data": mwo_data_map,
            "bom_data": bom_data_map,
            "customer_data": customer_data_map,
            "item_data": item_data_map,
            "customer_diamond_grade": customer_diamond_grade_map,
            "attribute_value_set": attribute_value_set,
        }

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


def fetch_doc_map(doctype, names, fields, key_field="name"):
    if not names:
        return {}

    data = frappe.get_all(
        doctype,
        filters={"name": ["in", list(names)]},
        fields=fields,
    )

    return {d[key_field]: d for d in data}


def create_manufacturing_order(doc, row, cache_data=None):
    if cache_data is None:
        cache_data = {}

    cnt = int(row.manufacturing_order_qty / row.qty_per_manufacturing_order)

    if not cnt:
        return

    so_data_map = cache_data.get("so_data", {})
    mwo_data_map = cache_data.get("mwo_data", {})
    bom_data_map = cache_data.get("bom_data", {})
    customer_data_map = cache_data.get("customer_data", {})
    item_data_map = cache_data.get("item_data", {})
    attribute_value_set = cache_data.get("attribute_value_set", set())
    customer_diamond_grade_map = cache_data.get("customer_diamond_grade", {})

    so_det = {}
    # Use plain dict copy instead of frappe._dict for memory/speed
    if row.sales_order and row.docname in so_data_map:
        so_det = so_data_map[row.docname].copy()
    elif row.mwo and row.mwo in mwo_data_map:
        so_det = mwo_data_map[row.mwo].copy()
    else:
        # Fallback
        doc_type, docname = (
            ("Sales Order Item", row.docname)
            if row.sales_order
            else ("Manufacturing Work Order", row.mwo)
        )
        fields = ["metal_type", "metal_touch", "metal_colour"]
        if row.mwo:
            fields.append("master_bom")

        fetched_val = frappe.get_value(doc_type, docname, fields, as_dict=1)
        if fetched_val:
            so_det = fetched_val

    master_bom = None
    if doc.select_manufacture_order == "Manufacturing":
        master_bom = row.manufacturing_bom
    elif (
        doc.select_manufacture_order == "Repair"
        and row.order_form_type == "Repair Order"
    ):
        master_bom = row.serial_id_bom

    if master_bom:
        # caching check
        bom_details = bom_data_map.get(master_bom)
        if not bom_details:
            bom_details = frappe.db.get_value(
                "BOM",
                master_bom,
                ["metal_type_", "metal_colour", "metal_touch"],
                as_dict=1,
            )

        if bom_details:
            # Update dictionary values using subscript notation
            so_det["metal_type"] = bom_details.get("metal_type_") or so_det.get(
                "metal_type_"
            )
            so_det["metal_colour"] = bom_details.get("metal_colour") or so_det.get(
                "metal_colour"
            )
            so_det["metal_touch"] = bom_details.get("metal_touch") or so_det.get(
                "metal_touch"
            )

    # Check for internal customer
    customer_info = customer_data_map.get(row.customer)
    is_internal_customer = (
        customer_info.get("is_internal_customer")
        if customer_info
        else frappe.db.get_value("Customer", row.customer, "is_internal_customer")
    )

    if row.diamond_quality and not is_internal_customer:
        key = (row.customer, row.diamond_quality)
        diamond_grade = None

        if row.customer_diamond == "Yes":
            # Use cached customer_diamond_grade_map
            diamond_grade_data = customer_diamond_grade_map.get(key)
            if diamond_grade_data:
                grades_to_check = [
                    diamond_grade_data.get("diamond_grade_1"),
                    diamond_grade_data.get("diamond_grade_2"),
                    diamond_grade_data.get("diamond_grade_3"),
                    diamond_grade_data.get("diamond_grade_4"),
                ]
                for grade in grades_to_check:
                    if grade and grade in attribute_value_set:
                        diamond_grade = grade
                        break
                    # We trust attribute_value_set contains all relevant values, no fallback needed

        else:
            diamond_grade_data = customer_diamond_grade_map.get(key)
            if diamond_grade_data:
                diamond_grade = diamond_grade_data.get("diamond_grade_1")
            else:
                # Minimal fallback
                diamond_grade = frappe.db.get_value(
                    "Customer Diamond Grade",
                    {"parent": row.customer, "diamond_quality": row.diamond_quality},
                    "diamond_grade_1",
                )

        so_det["diamond_grade"] = diamond_grade

        has_batch_no = False
        item_info = item_data_map.get(row.item_code)
        if item_info:
            has_batch_no = item_info.get("has_batch_no")
        else:
            has_batch_no = frappe.db.get_value("Item", row.item_code, "has_batch_no")

        if not so_det.get("diamond_grade") and not has_batch_no:
            frappe.throw(
                _("Diamond Grade is not mentioned in customer {0}").format(row.customer)
            )

    for i in range(0, cnt):
        make_manufacturing_order(doc, row, master_bom=master_bom, so_det=so_det)


def create_subcontracting_order(doc):
    pass


# for row in doc.manufacturing_plan_table:
# make_subcontracting_order(doc)
