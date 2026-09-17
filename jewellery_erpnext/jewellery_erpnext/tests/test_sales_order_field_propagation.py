# Copyright (c) 2023, Nirali and Contributors
# See license.txt

"""Tests for propagation of sales_type / order_type / flow_type through the manufacturing
chain: Quotation -> Sales Order -> Manufacturing Plan -> Parent Manufacturing Order ->
Manufacturing Work Order -> Serial Number Creator -> Serial No.

The hops are exercised with a recording-doc harness so the tests do not need the full
Order Form / BOM / Stock Entry fixture tree. Each case pins down the requirement's rules:

1. Full propagation   -- every hop copies all three values from its immediate predecessor.
2. MP fills its data  -- ``set_order_dimensions`` stamps the Manufacturing Plan's read-only
   fields from the linked Sales Order(s); blanks / disagreements leave the field untouched.
3. Partial values     -- only the populated fields are copied; gaps stay untouched.
4. Empty values       -- nothing is copied and nothing is invented as a default.
5. Mapping validation -- propagation reads the immediate predecessor only; no Sales Order /
   Order Form query at any downstream hop.
6. Regression         -- the creation functions still set the fields they did before, and the
   doctypes carry the expected field shapes (no new required fields).
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from jewellery_erpnext.jewellery_erpnext.doc_events.sales_order import validate_sales_type
from jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order import (
    make_manufacturing_order,
)
from jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator import (
    get_operation_details,
    update_new_serial_no,
)

DIMENSIONS = {
    "sales_type": "Retail",
    "order_type": "Stock Order",
    "flow_type": "MTO",
}


class DocRecorder:
    """Stand-in for frappe.new_doc that records every attribute set on it."""

    def __init__(self, doctype, **kwargs):
        object.__setattr__(self, "_data", dict(kwargs))
        object.__setattr__(self, "_children", {})
        self.doctype = doctype
        self.name = None

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value

    def __getattr__(self, key):
        try:
            return self._data[key]
        except KeyError:
            raise AttributeError(key)

    def __setattr__(self, key, value):
        self._data[key] = value

    def append(self, parentfield, row):
        self._children.setdefault(parentfield, []).append(row)
        self._data.setdefault(parentfield, []).append(row)

    def save(self):
        self.name = self.name or f"{self.doctype}-TEST-0001"

    def insert(self, *args, **kwargs):
        self.name = f"{self.doctype}-TEST-0001"


class TestSalesOrderFieldPropagation(FrappeTestCase):
    def setUp(self):
        # Convert every "Serial Number Creator" existence call into a miss so get_operation_details
        # always takes the creation path.
        patcher_get_all = patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator.frappe.db.get_all",
            return_value=[],
        )
        self.p_get_all = patcher_get_all.start()
        self.addCleanup(patcher_get_all.stop)

    # --------------------------------------------------- MP fills its data from Sales Order
    def _mp_with_sales_orders(self, so_rows, has_flow_column=True):
        mp = frappe.new_doc("Manufacturing Plan")
        for so in so_rows:
            mp.append("sales_order", {"sales_order": so["name"]})
        # Mirror the query's field selection: when the flow column does not exist it cannot
        # be selected, so it must not appear in the returned rows either.
        selected = ["sales_type", "order_type"] + (["custom_flow_type"] if has_flow_column else [])
        with patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.frappe.db.get_all",
            return_value=[
                {field: so.get(field) for field in selected if field in so} for so in so_rows
            ],
        ), patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.frappe.db.has_column",
            return_value=has_flow_column,
        ):
            mp.set_order_dimensions()
        return mp

    def test_mp_fills_from_sales_order(self):
        mp = self._mp_with_sales_orders(
            [
                {
                    "name": "SO-0001",
                    "sales_type": "Retail",
                    "order_type": "Stock Order",
                    "custom_flow_type": "MTO",
                }
            ]
        )
        self.assertEqual(mp.custom_sales_type, "Retail")
        self.assertEqual(mp.custom_order_type, "Stock Order")
        self.assertEqual(mp.custom_flow_type, "MTO")

    def test_mp_keeps_manual_values_when_no_sales_order(self):
        mp = frappe.new_doc("Manufacturing Plan")
        mp.custom_sales_type = "Wholesale"
        # No sales_order / plan rows -> must return without querying any Sales Order.
        with patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.frappe.db.get_all",
            side_effect=AssertionError("queried Sales Order with no link"),
        ):
            mp.set_order_dimensions()
        self.assertEqual(mp.custom_sales_type, "Wholesale")
        # Blank fields stay blank (a Select field reads back as "" / a Data field as None).
        self.assertFalse(mp.get("custom_order_type"))
        self.assertFalse(mp.get("custom_flow_type"))

    def test_mp_leaves_blank_when_sales_orders_disagree(self):
        mp = self._mp_with_sales_orders(
            [
                {"name": "SO-0001", "sales_type": "Retail", "order_type": None, "custom_flow_type": None},
                {"name": "SO-0002", "sales_type": "Wholesale", "order_type": None, "custom_flow_type": None},
            ]
        )
        self.assertEqual(mp.get("custom_sales_type"), None)
        self.assertEqual(mp.get("custom_order_type"), None)
        self.assertFalse(mp.get("custom_flow_type"))

    def test_mp_skips_flow_when_column_missing(self):
        mp = self._mp_with_sales_orders(
            [{"name": "SO-0001", "sales_type": "Retail", "order_type": "Sales", "custom_flow_type": "MTO"}],
            has_flow_column=False,
        )
        self.assertEqual(mp.custom_sales_type, "Retail")
        self.assertEqual(mp.custom_order_type, "Sales")
        self.assertFalse(mp.get("custom_flow_type"))

    def test_mp_fills_from_plan_table_rows(self):
        mp = frappe.new_doc("Manufacturing Plan")
        mp.append("manufacturing_plan_table", {"sales_order": "SO-0001"})
        with patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.frappe.db.get_all",
            return_value=[
                {"sales_type": "Retail", "order_type": "Stock Order", "custom_flow_type": "MTO"}
            ],
        ), patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.manufacturing_plan.manufacturing_plan.frappe.db.has_column",
            return_value=True,
        ):
            mp.set_order_dimensions()
        self.assertEqual(mp.custom_sales_type, "Retail")
        self.assertEqual(mp.custom_flow_type, "MTO")

    # ------------------------------------------------------------------ MP -> PMO
    def _make_pmo(self, mp_dimensions):
        with patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order.frappe.new_doc",
            return_value=DocRecorder("Parent Manufacturing Order"),
        ) as new_doc:
            source = frappe._dict(
                name="MP-0001",
                company="Test Company",
                select_manufacture_order="Manufacturing",
                **{f"custom_{k}": v for k, v in mp_dimensions.items()},
            )
            row = frappe._dict(
                sales_order="SO-0001",
                docname="SOI-0001",
                item_code="M-ITEM-001",
                customer_sample=None,
                customer_voucher_no=None,
                customer_gold="No",
                customer_diamond="No",
                customer_stone="No",
                customer_good="No",
                customer_weight=0,
                repair_type=None,
                product_type=None,
                name="MPT-0001",
                qty_per_manufacturing_order=1,
            )
            make_manufacturing_order(source, row, master_bom=None, so_det={}, service_type=[])
            return new_doc.return_value

    def test_full_propagation_mp_to_pmo(self):
        pmo = self._make_pmo(DIMENSIONS)
        self.assertEqual(pmo.sales_type, "Retail")
        self.assertEqual(pmo.order_type, "Stock Order")
        self.assertEqual(pmo.flow_type, "MTO")

    def test_partial_values_mp_to_pmo(self):
        pmo = self._make_pmo({"sales_type": "Retail"})
        self.assertEqual(pmo.sales_type, "Retail")
        self.assertEqual(pmo.get("order_type"), None)
        self.assertEqual(pmo.get("flow_type"), None)

    def test_empty_values_mp_to_pmo(self):
        pmo = self._make_pmo({})
        self.assertEqual(pmo.get("sales_type"), None)
        self.assertEqual(pmo.get("order_type"), None)
        self.assertEqual(pmo.get("flow_type"), None)

    def test_no_order_form_query_mp_to_pmo(self):
        # The MP->PMO hop must resolve dimensions from the Manufacturing Plan itself.
        seen = set()
        with patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.parent_manufacturing_order.parent_manufacturing_order.frappe.db.get_value",
            side_effect=lambda doctype, *a, **k: seen.add(doctype or ""),
        ):
            self._make_pmo(DIMENSIONS)
        self.assertNotIn("Sales Order", seen)
        self.assertNotIn("Order Form", seen)

    def test_regression_mp_to_pmo(self):
        pmo = self._make_pmo(DIMENSIONS)
        self.assertEqual(pmo.company, "Test Company")
        self.assertEqual(pmo.sales_order, "SO-0001")
        self.assertEqual(pmo.sales_order_item, "SOI-0001")
        self.assertEqual(pmo.item_code, "M-ITEM-001")
        self.assertEqual(pmo.qty, 1)
        self.assertEqual(pmo.manufacturing_plan, "MP-0001")

    # ------------------------------------------------------------------ MP -> PO
    def _make_po(self, mp_dimensions):
        from jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order import make_subcontracting_order
        with patch(
            "jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.new_doc",
            return_value=DocRecorder("Purchase Order", items=[]),
        ) as new_doc, patch(
            "jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order.frappe.db.get_single_value",
            return_value="SERVICE-001"
        ), patch(
            "jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order._source_gold_rate",
            return_value=0
        ):
            source = frappe._dict(
                name="MP-0001",
                company="Test Company",
                manufacturing_plan_table=[
                    frappe._dict(
                        supplier="Test Supplier",
                        customer="Test Customer",
                        purchase_type="FG Purchase",
                        customer_po="PO-123",
                        item_code="M-ITEM-001",
                        subcontracting_qty=1,
                        manufacturing_bom="BOM-001",
                        copy_bom=None,
                        diamond_quality="VVS",
                        child_po=None,
                        name="MPT-0001"
                    )
                ],
                **{f"custom_{k}": v for k, v in mp_dimensions.items()},
            )
            make_subcontracting_order(source)
            return new_doc.return_value

    def test_full_propagation_mp_to_po(self):
        po = self._make_po(DIMENSIONS)
        self.assertEqual(po.custom_sales_type, "Retail")
        self.assertEqual(po.custom_order_type, "Stock Order")
        self.assertEqual(po.custom_flow_type, "MTO")

    def test_partial_values_mp_to_po(self):
        po = self._make_po({"sales_type": "Retail"})
        self.assertEqual(po.custom_sales_type, "Retail")
        self.assertEqual(po.get("custom_order_type"), None)
        self.assertEqual(po.get("custom_flow_type"), None)

    # ----------------------------------------------------------------- PMO -> MWO
    def test_pmo_to_mwo_fields_are_mappable(self):
        # get_mapped_doc copies same-named, non-no_copy fields (frappe.model.mapper.map_fields);
        # order_type already ships as Data on PMO/MWO/SNC, so it is asserted on the meta.
        from jewellery_erpnext.patches.add_chain_sales_order_fields import CUSTOM_FIELDS as CF

        for doctype in ("Parent Manufacturing Order", "Manufacturing Work Order"):
            fields = {f["fieldname"]: f for f in CF[doctype]}
            for field in ("sales_type", "flow_type"):
                self.assertIn(field, fields, f"{doctype} missing {field} in patch")
                self.assertFalse(fields[field].get("no_copy"), f"{doctype}.{field} must not be no_copy")
            for field in ("sales_type", "order_type", "flow_type"):
                df = frappe.get_meta(doctype).get_field(field)
                self.assertIsNotNone(df, f"{doctype}.{field} missing on meta")
                self.assertFalse(df.no_copy, f"{doctype}.{field} must not be no_copy")

    # ----------------------------------------------------------------- MWO -> SNC
    def _make_snc(self, mwo_dimensions):
        with patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator.frappe.new_doc",
            return_value=DocRecorder("Serial Number Creator"),
        ) as new_doc, patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator.frappe.get_doc",
            return_value=DocRecorder("Manufacturing Operation"),
        ), patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator.frappe.db.get_value",
            side_effect=lambda doctype, *a, **k: mwo_dimensions
            if doctype == "Manufacturing Work Order"
            else None,
        ), patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator.frappe.msgprint",
            return_value=None,
        ):
            data = json.dumps(
                [
                    [
                        {
                            "item_code": "M-ITEM-001",
                            "batch_no": None,
                            "qty": 1,
                            "uom": "g",
                            "gross_wt": 1,
                            "inventory_type": "Regular Stock",
                            "name": "SED-LINE-0001",
                            "pcs": 1,
                        }
                    ],
                    [],
                    1,
                    1,
                ]
            )
            get_operation_details(
                data=data,
                docname="MOP-0001",
                mwo="MWO-0001",
                pmo="PMO-0001",
                company="Test Company",
                mnf="MANUFACTURER-0001",
                dpt="DEPARTMENT-0001",
                for_fg=1,
                design_id_bom=None,
            )
            return new_doc.return_value

    def test_full_propagation_mwo_to_snc(self):
        snc = self._make_snc(DIMENSIONS)
        self.assertEqual(snc.sales_type, "Retail")
        self.assertEqual(snc.order_type, "Stock Order")
        self.assertEqual(snc.flow_type, "MTO")

    def test_partial_and_empty_mwo_to_snc(self):
        for dims in ({"sales_type": "B2B"}, {}):
            snc = self._make_snc(dims)
            self.assertEqual(snc.sales_type, dims.get("sales_type"))
            self.assertEqual(snc.get("order_type"), dims.get("order_type"))
            self.assertEqual(snc.get("flow_type"), dims.get("flow_type"))

    def test_mwo_to_snc_no_order_form_query(self):
        seen = set()

        def spy(doctype, *args, **kwargs):
            seen.add(doctype or "")
            return DIMENSIONS if doctype == "Manufacturing Work Order" else None

        with patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator.frappe.new_doc",
            return_value=DocRecorder("Serial Number Creator"),
        ), patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator.frappe.get_doc",
            return_value=DocRecorder("Manufacturing Operation"),
        ), patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator.frappe.db.get_value",
            side_effect=spy,
        ), patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator.frappe.msgprint",
            return_value=None,
        ):
            data = json.dumps(
                [
                    [
                        {
                            "item_code": "M-ITEM-001",
                            "batch_no": None,
                            "qty": 1,
                            "uom": "g",
                            "gross_wt": 1,
                            "inventory_type": "Regular Stock",
                            "name": "SED-LINE-0001",
                            "pcs": 1,
                        }
                    ],
                    [],
                    1,
                    1,
                ]
            )
            get_operation_details(
                data=data,
                docname="MOP-0001",
                mwo="MWO-0001",
                pmo="PMO-0001",
                company="Test Company",
                mnf="MANUFACTURER-0001",
                dpt="DEPARTMENT-0001",
                for_fg=1,
                design_id_bom=None,
            )
        self.assertNotIn("Sales Order", seen)
        self.assertNotIn("Order Form", seen)

    # ----------------------------------------------------------------- SNC -> Serial No
    def _make_serial_no(self, snc_fields):
        snc = frappe._dict(
            fg_serial_no="SN-RECORD-0001",
            parent_manufacturing_order="PMO-0001",
            serial_no=None,
            **snc_fields,
        )
        with patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator.frappe.get_doc",
            return_value=DocRecorder("Serial No", huid=[]),
        ) as get_doc:
            update_new_serial_no(snc)
            return get_doc.return_value

    def test_full_propagation_snc_to_serial_no(self):
        serial_no = self._make_serial_no(DIMENSIONS)
        self.assertEqual(serial_no.sales_type, "Retail")
        self.assertEqual(serial_no.order_type, "Stock Order")
        self.assertEqual(serial_no.flow_type, "MTO")

    def test_partial_and_empty_snc_to_serial_no(self):
        for dims in ({"sales_type": "B2B"}, {}):
            serial_no = self._make_serial_no(dims)
            self.assertEqual(serial_no.sales_type, dims.get("sales_type"))
            self.assertEqual(serial_no.get("order_type"), dims.get("order_type"))
            self.assertEqual(serial_no.get("flow_type"), dims.get("flow_type"))

    def test_snc_to_serial_no_no_order_form_query(self):
        seen = set()
        with patch(
            "jewellery_erpnext.jewellery_erpnext.doctype.serial_number_creator.serial_number_creator.frappe.get_doc",
            side_effect=lambda doctype, *a, **k: (
                seen.add(doctype) or DocRecorder("Serial No", huid=[])
            ),
        ):
            self._make_serial_no(DIMENSIONS)
        self.assertNotIn("Sales Order", seen)
        self.assertNotIn("Order Form", seen)

    # --------------------------------------------- Quotation -> Sales Order
    def _make_so(self, quotation_dimensions):
        so = frappe.new_doc("Sales Order")
        so.append("items", {"prevdoc_docname": "QUO-0001"})
        # frappe.db.get_value(..., as_dict=True) returns a frappe._dict, so mirror that here.
        with patch(
            "jewellery_erpnext.jewellery_erpnext.doc_events.sales_order.frappe.db.get_value",
            return_value=frappe._dict(quotation_dimensions),
        ):
            validate_sales_type(so)
        return so

    def test_full_propagation_quotation_to_sales_order(self):
        so = self._make_so({"custom_sales_type": "Retail", "custom_flow_type": "MTO"})
        self.assertEqual(so.sales_type, "Retail")
        self.assertEqual(so.custom_flow_type, "MTO")

    def test_partial_quotation_to_sales_order(self):
        # Sales type present, flow type blank -> flow stays empty, nothing invented.
        so = self._make_so({"custom_sales_type": "Retail"})
        self.assertEqual(so.sales_type, "Retail")
        self.assertFalse(so.get("custom_flow_type"))

    def test_empty_quotation_to_sales_order_requires_sales_type(self):
        with self.assertRaises(frappe.ValidationError):
            self._make_so({})

    # ------------------------------------------------ field-shape regression (config)
    def test_chain_field_shapes(self):
        # Every propagated field — Quotation flow type, Sales Order flow type, MP, PMO, MWO,
        # SNC, Serial No — is read-only; the chain stamps it from the immediate predecessor.
        read_only = {
            "Quotation": {"custom_flow_type": "Select"},
            "Sales Order": {"custom_flow_type": "Select"},
            "Manufacturing Plan": {
                "custom_sales_type": "Link",
                "custom_order_type": "Data",
                "custom_flow_type": "Select",
            },
            "Parent Manufacturing Order": {"sales_type": "Link", "flow_type": "Select"},
            "Manufacturing Work Order": {"sales_type": "Link", "flow_type": "Select"},
            "Serial Number Creator": {"sales_type": "Link", "flow_type": "Select"},
            "Serial No": {
                "sales_type": "Link",
                "order_type": "Data",
                "flow_type": "Select",
            },
            "Purchase Order": {
                "custom_sales_type": "Link",
                "custom_order_type": "Data",
                "custom_flow_type": "Select",
            },
        }
        for doctype, fields in read_only.items():
            meta = frappe.get_meta(doctype)
            for fieldname, fieldtype in fields.items():
                df = meta.get_field(fieldname)
                self.assertIsNotNone(df, f"{doctype}.{fieldname} missing after patch")
                self.assertEqual(df.fieldtype, fieldtype, f"{doctype}.{fieldname} type")
                self.assertFalse(df.reqd, f"{doctype}.{fieldname} must not be required")
                self.assertTrue(df.read_only, f"{doctype}.{fieldname} must be read-only")

        # Quotation sales type is the entry point the salesperson fills in.
        quo_sales = frappe.get_meta("Quotation").get_field("custom_sales_type")
        self.assertIsNotNone(quo_sales, "Quotation.custom_sales_type missing after patch")
        self.assertEqual(quo_sales.fieldtype, "Link")
        self.assertFalse(quo_sales.read_only, "Quotation.custom_sales_type must stay editable")

        # order_type already shipped as Data on the three down-chain docs; keep it.
        for doctype in ("Parent Manufacturing Order", "Manufacturing Work Order", "Serial Number Creator"):
            self.assertEqual(frappe.get_meta(doctype).get_field("order_type").fieldtype, "Data")