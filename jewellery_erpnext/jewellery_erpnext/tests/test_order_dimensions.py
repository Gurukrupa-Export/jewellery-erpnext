"""Unanimity rules for the order dimensions stamped onto a Manufacturing Plan / Purchase Order.

The behaviour under test is `SOFT_ORDER_DIMENSIONS`: design type must never block a subcontracting
plan submit the way order / sales / flow type do, because one plan legitimately spans several
designs -- and because every Sales Order predating the `custom_design_type` column reads back blank.

DB-free: `frappe.get_all` is mocked, so no fixtures are built and no site data is read.
"""

import unittest
from unittest.mock import patch

import frappe

from jewellery_erpnext.jewellery_erpnext.doc_events.purchase_order import (
    ORDER_DIMENSION_MAP,
    SOFT_ORDER_DIMENSIONS,
    source_order_dimensions,
)

# Values every source order agrees on, so a test only varies the one dimension it cares about.
AGREED = {"order_type": "Sales", "sales_type": "Domestic", "custom_flow_type": "MTO"}


def plan(row_count=2):
    return frappe._dict(
        manufacturing_plan_table=[
            frappe._dict(sales_order=f"SO-{i + 1}") for i in range(row_count)
        ]
    )


def sales_orders(**varying):
    """Build source Sales Order rows; each kwarg is a field with one value per order.

    A kwarg naming a field in `AGREED` overrides it, which is how a test makes exactly one
    dimension disagree.
    """
    count = len(next(iter(varying.values())))
    return [
        frappe._dict(
            {
                "name": f"SO-{i + 1}",
                **AGREED,
                **{f: values[i] for f, values in varying.items()},
            }
        )
        for i in range(count)
    ]


class TestSourceOrderDimensions(unittest.TestCase):
    def dimensions(self, rows, strict):
        with patch.object(frappe, "get_all", return_value=rows):
            return source_order_dimensions(plan(len(rows)), strict=strict)

    def test_design_type_is_the_only_soft_dimension(self):
        # Guards the constant itself: de-stricting an existing dimension by accident would let a
        # wrong order/sales/flow type ride onto a supplier's Purchase Order unchallenged.
        self.assertEqual(SOFT_ORDER_DIMENSIONS, {"custom_design_type"})
        self.assertIn("custom_design_type", ORDER_DIMENSION_MAP.values())

    def test_unanimous_design_type_is_stamped(self):
        rows = sales_orders(custom_design_type=["New Design", "New Design"])
        self.assertEqual(
            self.dimensions(rows, strict=True).get("custom_design_type"), "New Design"
        )

    def test_mixed_design_type_does_not_throw_and_stays_blank(self):
        # The acceptance test for the whole change.
        rows = sales_orders(custom_design_type=["New Design", "Sketch Design"])
        values = self.dimensions(rows, strict=True)
        self.assertNotIn("custom_design_type", values)
        # The dimensions that do agree are still stamped.
        self.assertEqual(values.get("custom_sales_type"), "Domestic")

    def test_legacy_blank_design_type_does_not_throw(self):
        # A plan mixing pre-patch Sales Orders (blank) with new ones must still submit.
        rows = sales_orders(custom_design_type=[None, "New Design"])
        self.assertNotIn("custom_design_type", self.dimensions(rows, strict=True))

    def test_mixed_sales_type_still_throws(self):
        rows = sales_orders(
            sales_type=["Domestic", "Export"],
            custom_design_type=["New Design", "New Design"],
        )
        with self.assertRaises(frappe.ValidationError):
            self.dimensions(rows, strict=True)

    def test_nothing_throws_on_save(self):
        rows = sales_orders(
            sales_type=["Domestic", "Export"],
            custom_design_type=["New Design", "Sketch Design"],
        )
        values = self.dimensions(rows, strict=False)
        self.assertNotIn("custom_sales_type", values)
        self.assertNotIn("custom_design_type", values)

    def test_plan_with_no_sales_orders_returns_empty(self):
        with patch.object(frappe, "get_all", return_value=[]):
            self.assertEqual(
                source_order_dimensions(
                    frappe._dict(manufacturing_plan_table=[]), strict=True
                ),
                {},
            )
