# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""Finding repack on an Employee IR Receive (the F-series).

``finding_repack`` turns the finding item/weight pairs on an Employee IR Operation row into a
``Repack`` Stock Entry drawn from the work order's casting tree, then hands the produced rows to
the Material Transfer (WORK ORDER) leg so their weight reaches the Manufacturing Operation.

These tests pin the parts that can be exercised without posting stock: the gate, the row reading,
the validation rules, the allocation-to-produce-row mapping (including the mixed-ownership split),
the ledger charge / reversal arithmetic, and the batch-level idempotency the Material Transfer
uses. The stock posting itself is covered by the manual end-to-end pass, not here -- building a
submittable Stock Entry needs Bins, Batches and an SRE-complete site.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.exceptions import ValidationError
from frappe.tests import IntegrationTestCase

from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	finding_repack as fr,
)
from jewellery_erpnext.jewellery_erpnext.doctype.employee_ir.doc_events import (
	main_slip_inject as msi,
)

METAL = "M-G-18KT-75.4-Y"
FIND_A = "F-CLASP-18KT-75.4-Y"
FIND_B = "F-JUMP-18KT-75.4-Y"
COMPANY_OWNER = ("Regular Stock", None)
CUSTOMER_OWNER = ("Customer Goods", "CUST-0001")


def row(idx=1, item1=None, wt1=0, item2=None, wt2=0, mwo="MWO-0001", tree=None):
	"""An Employee IR Operation row whose ``.get`` reads its own attributes.

	``finding_repack`` reads rows through ``.get`` the way a Frappe Document child row behaves,
	so the stub has to as well -- a plain SimpleNamespace would silently answer ``None``.
	"""
	r = SimpleNamespace(
		idx=idx,
		name=f"row{idx}",
		manufacturing_work_order=mwo,
		manufacturing_operation=f"MOP-{idx:04d}",
		tree_number=tree,
		finding_item1=item1,
		finding_wt1=wt1,
		finding_item2=item2,
		finding_wt2=wt2,
	)
	r.get = lambda field, default=None: getattr(r, field, default)
	return r


def stub_variant_of(value):
	"""Patch ONLY the ``Item.variant_of`` lookup, delegating every other read to the real one.

	A blanket ``patch("frappe.db.get_value")`` is a trap in this suite: loading DocType meta goes
	through ``frappe.db.get_value`` too, so ``tree_material_balance.qty_precision`` (which resolves
	``Stock Entry Detail`` meta) would receive the canned value and blow up far from the call the
	test meant to control.
	"""
	original = frappe.db.get_value

	def fake(doctype, filters=None, fieldname="name", *args, **kwargs):
		if doctype == "Item" and fieldname == "variant_of":
			return value
		return original(doctype, filters, fieldname, *args, **kwargs)

	return patch.object(frappe.db, "get_value", side_effect=fake)


def eir(rows, eir_type="Receive", name="EMP-IR-0001", **kwargs):
	doc = SimpleNamespace(
		name=name,
		type=eir_type,
		operation="Casting",
		company="Gurukrupa Export Private Limited",
		department="Casting - GEPL",
		employee="HR-EMP-00001",
		subcontracting="No",
		subcontractor=None,
		employee_ir_operations=rows,
		**kwargs,
	)
	doc.get = lambda field, default=None: getattr(doc, field, default)
	return doc


# ---------------------------------------------------------------------------
# F01-F05: reading the rows
# ---------------------------------------------------------------------------
class TestFindingPairs(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_f01_no_findings(self):
		self.assertEqual(fr.finding_pairs(row()), [])

	def test_f02_one_finding(self):
		self.assertEqual(fr.finding_pairs(row(item1=FIND_A, wt1=2)), [(FIND_A, 2.0)])

	def test_f03_two_findings(self):
		pairs = fr.finding_pairs(row(item1=FIND_A, wt1=1.25, item2=FIND_B, wt2=0.48))
		self.assertEqual(pairs, [(FIND_A, 1.25), (FIND_B, 0.48)])

	def test_f04_second_slot_alone(self):
		"""Slots are independent -- slot 2 filled while slot 1 is empty still counts."""
		self.assertEqual(fr.finding_pairs(row(item2=FIND_B, wt2=0.5)), [(FIND_B, 0.5)])

	def test_f05_zero_weight_is_not_a_pair(self):
		self.assertEqual(fr.finding_pairs(row(item1=FIND_A, wt1=0)), [])

	def test_f06_total_is_rounded_to_ledger_precision(self):
		total = fr._row_finding_total(
			row(item1=FIND_A, wt1=1.0005, item2=FIND_B, wt2=2.0004)
		)
		self.assertAlmostEqual(total, 3.001, places=3)


# ---------------------------------------------------------------------------
# F10-F16: validation
# ---------------------------------------------------------------------------
class TestValidation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_f10_issue_type_with_findings_is_rejected(self):
		doc = eir([row(item1=FIND_A, wt1=2)], eir_type="Issue")
		with self.assertRaises(ValidationError) as cm:
			fr.validate_finding_repack(doc)
		self.assertIn("Receive", str(cm.exception))

	def test_f11_issue_type_without_findings_passes(self):
		fr.validate_finding_repack(eir([row()], eir_type="Issue"))

	def test_f12_flag_off_with_findings_is_rejected(self):
		# Silently ignoring the values would lose stock: the operator believes the findings were
		# booked, the tree keeps the metal, and the operation never gains the weight.
		with patch.object(fr, "is_finding_repack_eir", return_value=False):
			with self.assertRaises(ValidationError) as cm:
				fr.validate_finding_repack(eir([row(item1=FIND_A, wt1=2)]))
		self.assertIn("Is Finding Repack Requirement", str(cm.exception))

	def test_f13_flag_off_without_findings_is_a_no_op(self):
		with patch.object(fr, "is_finding_repack_eir", return_value=False):
			fr.validate_finding_repack(eir([row()]))

	def test_f14_item_without_weight_is_rejected(self):
		with patch.object(fr, "is_finding_repack_eir", return_value=True):
			with self.assertRaises(ValidationError) as cm:
				fr.validate_finding_repack(
					eir([row(item1=FIND_A, wt1=0, item2=FIND_B, wt2=1)])
				)
		self.assertIn("weight is zero", str(cm.exception))

	def test_f15_weight_without_item_is_rejected(self):
		with patch.object(fr, "is_finding_repack_eir", return_value=True):
			with self.assertRaises(ValidationError) as cm:
				fr.validate_finding_repack(eir([row(wt1=1.5)]))
		self.assertIn("no finding item", str(cm.exception))

	def test_f16_row_without_a_tree_is_rejected(self):
		with patch.object(fr, "is_finding_repack_eir", return_value=True), patch.object(
			fr, "row_tree_name", return_value=None
		):
			with self.assertRaises(ValidationError) as cm:
				fr.validate_finding_repack(eir([row(item1=FIND_A, wt1=2)]))
		self.assertIn("not on one", str(cm.exception))


class TestFindingItemMustBeAVariantOfF(IntegrationTestCase):
	"""mop_log.FIELD_MAP buckets by the item code's FIRST CHARACTER, so a non-F item here would
	land in the wrong weight bucket on the Manufacturing Operation."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_f17_metal_item_is_rejected(self):
		with stub_variant_of("M"):
			with self.assertRaises(ValidationError) as cm:
				fr._validate_finding_item(row(), METAL)
		self.assertIn("not a Finding item", str(cm.exception))

	def test_f18_item_with_no_variant_of_is_rejected(self):
		with stub_variant_of(None):
			with self.assertRaises(ValidationError):
				fr._validate_finding_item(row(), "SOMETHING")

	def test_f19_finding_item_passes(self):
		with stub_variant_of("F"):
			fr._validate_finding_item(row(), FIND_A)


# ---------------------------------------------------------------------------
# F20-F24: allocation -> produce rows
# ---------------------------------------------------------------------------
def ranks_for(mapping):
	"""``{batch: (inventory_type, customer)}`` -> the rank map ``_owner_of`` reads."""
	return {
		batch: SimpleNamespace(inventory_type=inv, customer=cust)
		for batch, (inv, cust) in mapping.items()
	}


class TestAppendFindingRows(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _append(self, pairs, alloc, owners):
		"""Drive _append_finding_rows with the batch minting and ownership rules stubbed out.

		``frappe.db.get_value`` is deliberately left alone: the only read left in this path is the
		produce row's ``Item.stock_uom``, which resolves to None for these fixture item codes and
		falls back to "Gram" on its own. Patching it wholesale would also intercept the DocType
		meta load behind ``qty_precision``.
		"""
		se = SimpleNamespace(items=[])
		se.append = lambda _table, d: se.items.append(d)
		with patch.object(
			fr, "_create_finding_batch", side_effect=self._fake_batch
		), patch.object(
			fr, "normalize_ownership", side_effect=lambda inv, cust, **kw: (inv, cust)
		):
			self._minted = []
			produced = fr._append_finding_rows(
				se, pairs, METAL, "MSL - GEPL", alloc, ranks_for(owners), 3
			)
		return se, produced

	def _fake_batch(self, se, item_code, inventory_type, customer):
		name = f"BATCH-{item_code}-{len(self._minted)}"
		self._minted.append(name)
		return name

	def test_f20_single_finding_single_batch(self):
		se, produced = self._append(
			[(FIND_A, 2.0)], [("B1", 2.0)], {"B1": COMPANY_OWNER}
		)
		self.assertEqual(len(produced), 1)
		self.assertAlmostEqual(produced[0]["qty"], 2.0, places=3)
		self.assertEqual(produced[0]["item_code"], FIND_A)
		# consume(metal) + produce(finding)
		self.assertEqual(len(se.items), 2)
		self.assertEqual(se.items[0]["item_code"], METAL)
		self.assertEqual(se.items[0]["s_warehouse"], "MSL - GEPL")
		self.assertIsNone(se.items[0]["t_warehouse"])
		self.assertEqual(se.items[1]["item_code"], FIND_A)
		self.assertEqual(se.items[1]["t_warehouse"], "MSL - GEPL")
		self.assertIsNone(se.items[1]["s_warehouse"])

	def test_f21_two_findings_split_one_allocation(self):
		"""The allocation is a stream: finding 1 takes its weight off the front, finding 2 the
		rest. 14 g pending, 2 g of findings -> two produce rows summing to exactly 2 g."""
		se, produced = self._append(
			[(FIND_A, 1.25), (FIND_B, 0.75)],
			[("B1", 2.0)],
			{"B1": COMPANY_OWNER},
		)
		self.assertEqual([p["item_code"] for p in produced], [FIND_A, FIND_B])
		self.assertAlmostEqual(produced[0]["qty"], 1.25, places=3)
		self.assertAlmostEqual(produced[1]["qty"], 0.75, places=3)
		self.assertAlmostEqual(sum(p["qty"] for p in produced), 2.0, places=3)

	def test_f22_one_finding_spanning_two_batches_of_one_owner(self):
		se, produced = self._append(
			[(FIND_A, 3.0)],
			[("B1", 1.0), ("B2", 2.0)],
			{"B1": COMPANY_OWNER, "B2": COMPANY_OWNER},
		)
		# Two consume rows, ONE produce row: same owner, so one batch is enough.
		self.assertEqual(len(produced), 1)
		self.assertAlmostEqual(produced[0]["qty"], 3.0, places=3)
		consumes = [i for i in se.items if i["s_warehouse"]]
		self.assertEqual(len(consumes), 2)
		self.assertAlmostEqual(sum(c["qty"] for c in consumes), 3.0, places=3)

	def test_f23_mixed_ownership_splits_the_produce_row(self):
		"""A minted batch reads its owner off its produce row, so a finding poured from both a
		customer's metal and the company's must produce one batch per owner -- folding them would
		silently change who owns the stock."""
		se, produced = self._append(
			[(FIND_A, 3.0)],
			[("B1", 1.0), ("B2", 2.0)],
			{"B1": CUSTOMER_OWNER, "B2": COMPANY_OWNER},
		)
		self.assertEqual(len(produced), 2)
		self.assertAlmostEqual(sum(p["qty"] for p in produced), 3.0, places=3)
		owners = {(p["inventory_type"], p["customer"]) for p in produced}
		self.assertEqual(owners, {CUSTOMER_OWNER, COMPANY_OWNER})
		# Distinct batches, one per owner.
		self.assertEqual(len({p["batch_no"] for p in produced}), 2)

	def test_f24_short_allocation_is_refused(self):
		with self.assertRaises(ValidationError) as cm:
			self._append([(FIND_A, 5.0)], [("B1", 2.0)], {"B1": COMPANY_OWNER})
		self.assertIn("could be allocated", str(cm.exception))

	def test_f25_produce_row_carries_the_repack_flags(self):
		se, _produced = self._append(
			[(FIND_A, 2.0)], [("B1", 2.0)], {"B1": COMPANY_OWNER}
		)
		produce = se.items[1]
		self.assertEqual(produce["is_finished_item"], 1)
		self.assertEqual(produce["set_basic_rate_manually"], 1)
		self.assertEqual(produce["use_serial_batch_fields"], 1)


# ---------------------------------------------------------------------------
# F30-F33: the tree ledger charge and its reversal
# ---------------------------------------------------------------------------
def md(issue=14.0, receive=0.0, loss=0.0, wo_receive=0.0, item=METAL):
	return SimpleNamespace(
		item_code=item,
		issue_qty=issue,
		receive_qty=receive,
		loss_qty=loss,
		wo_receive_qty=wo_receive,
		pending_qty=issue - receive - loss,
	)


def tree_doc(rows, name="2026-09-17-0001"):
	t = SimpleNamespace(
		name=name, material_details=list(rows), status="Issued", flags=SimpleNamespace()
	)
	t.save = lambda **kw: None
	t.get = lambda field, default=None: getattr(t, field, default)
	return t


class TestTreeLedgerCharge(IntegrationTestCase):
	"""The user's worked example: 14 g pending, 2 g of findings -> 12 g pending."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_f30_charge_drops_pending_by_the_finding_weight(self):
		tree = tree_doc([md(issue=14.0)])
		with patch.object(
			fr.tree_balance, "tree_status", return_value="Partially Received"
		):
			fr._charge_tree(tree, METAL, 2.0, 3)
		row_ = tree.material_details[0]
		self.assertAlmostEqual(row_.receive_qty, 2.0, places=3)
		self.assertAlmostEqual(row_.wo_receive_qty, 2.0, places=3)
		self.assertAlmostEqual(
			fr.tree_balance.calculate_pending(
				row_.issue_qty, row_.receive_qty, row_.loss_qty
			),
			12.0,
			places=3,
		)

	def test_f31_charge_does_not_write_the_derived_columns(self):
		"""pending_qty and manual_receive_qty have exactly one writer (TreeNumber.validate); a
		second one is what made the four call paths drift apart before."""
		tree = tree_doc([md(issue=14.0)])
		with patch.object(
			fr.tree_balance, "tree_status", return_value="Partially Received"
		):
			fr._charge_tree(tree, METAL, 2.0, 3)
		self.assertFalse(hasattr(tree.material_details[0], "manual_receive_qty"))
		# pending_qty is left at the value the row was loaded with, not recomputed here.
		self.assertAlmostEqual(tree.material_details[0].pending_qty, 14.0, places=3)

	def test_f32_zero_charge_is_a_no_op(self):
		tree = tree_doc([md(issue=14.0)])
		fr._charge_tree(tree, METAL, 0, 3)
		self.assertAlmostEqual(tree.material_details[0].receive_qty, 0.0, places=3)

	def test_f33_charge_accumulates_across_rows_on_one_tree(self):
		tree = tree_doc([md(issue=14.0)])
		with patch.object(
			fr.tree_balance, "tree_status", return_value="Partially Received"
		):
			fr._charge_tree(tree, METAL, 2.0, 3)
			fr._charge_tree(tree, METAL, 1.5, 3)
		self.assertAlmostEqual(tree.material_details[0].receive_qty, 3.5, places=3)
		self.assertAlmostEqual(tree.material_details[0].wo_receive_qty, 3.5, places=3)


class TestFindingDrawByTree(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def test_f40_aggregates_rows_sharing_a_tree(self):
		rows = [
			row(idx=1, item1=FIND_A, wt1=2.0, mwo="MWO-1"),
			row(idx=2, item1=FIND_B, wt1=1.5, mwo="MWO-2"),
		]
		with patch.object(fr, "is_finding_repack_eir", return_value=True), patch.object(
			fr, "row_tree_name", return_value="TREE-1"
		), patch.object(fr, "_metal_item", return_value=METAL), patch(
			"frappe.get_cached_doc", return_value=SimpleNamespace()
		):
			draws = fr.finding_draw_by_tree(eir(rows))
		self.assertEqual(draws, {"TREE-1": {METAL: 3.5}})

	def test_f41_flag_off_draws_nothing(self):
		with patch.object(fr, "is_finding_repack_eir", return_value=False):
			self.assertEqual(
				fr.finding_draw_by_tree(eir([row(item1=FIND_A, wt1=2.0)])), {}
			)

	def test_f42_issue_draws_nothing(self):
		with patch.object(fr, "is_finding_repack_eir", return_value=True):
			self.assertEqual(
				fr.finding_draw_by_tree(
					eir([row(item1=FIND_A, wt1=2.0)], eir_type="Issue")
				),
				{},
			)


# ---------------------------------------------------------------------------
# F50-F53: Material Transfer idempotency for the carried rows
# ---------------------------------------------------------------------------
class TestPendingExtraRows(IntegrationTestCase):
	"""Idempotency is keyed on the BATCH, not on "does a transfer exist": on the Main Slip path a
	transfer always exists yet carries none of these rows, and on a retry the Repack's own
	idempotency means a type-level skip would strand the findings in the MSL warehouse."""

	@classmethod
	def setUpClass(cls):
		pass

	def _extras(self):
		return [
			{"item_code": FIND_A, "qty": 1.25, "batch_no": "BA"},
			{"item_code": FIND_B, "qty": 0.75, "batch_no": "BB"},
		]

	def test_f50_nothing_carried_yet_returns_all(self):
		with patch.object(msi, "_already_transferred_batches", return_value=set()):
			pending = msi._pending_extra_rows("EMP-IR-1", "row1", self._extras())
		self.assertEqual(len(pending), 2)

	def test_f51_already_carried_rows_are_dropped(self):
		with patch.object(
			msi, "_already_transferred_batches", return_value={(FIND_A, "BA")}
		):
			pending = msi._pending_extra_rows("EMP-IR-1", "row1", self._extras())
		self.assertEqual([p["item_code"] for p in pending], [FIND_B])

	def test_f52_all_carried_returns_empty(self):
		with patch.object(
			msi,
			"_already_transferred_batches",
			return_value={(FIND_A, "BA"), (FIND_B, "BB")},
		):
			self.assertEqual(
				msi._pending_extra_rows("EMP-IR-1", "row1", self._extras()), []
			)

	def test_f53_no_extras_short_circuits(self):
		self.assertEqual(msi._pending_extra_rows("EMP-IR-1", "row1", []), [])


class TestExtraRowsRideTheTransfer(IntegrationTestCase):
	"""An extra row must be indistinguishable from a metal segment once appended -- that is what
	makes the MOP Log bridge treat it the same way."""

	@classmethod
	def setUpClass(cls):
		pass

	def test_f60_extra_row_carries_the_mop_link_and_its_batch(self):
		se = SimpleNamespace(items=[])
		se.append = lambda _table, d: se.items.append(d)
		r = row(idx=1)
		extras = [
			{
				"item_code": FIND_A,
				"qty": 2.0,
				"batch_no": "BA",
				"inventory_type": "Regular Stock",
				"customer": None,
			}
		]
		with patch("frappe.new_doc", return_value=se), patch.object(
			msi, "_stamp_se_header"
		):
			msi._build_material_transfer_from_segments(
				eir([r]), r, [], "MSL - GEPL", "MFG - GEPL", extras
			)
		self.assertEqual(len(se.items), 1)
		item = se.items[0]
		self.assertEqual(item["item_code"], FIND_A)
		self.assertEqual(item["s_warehouse"], "MSL - GEPL")
		self.assertEqual(item["t_warehouse"], "MFG - GEPL")
		self.assertEqual(item["batch_no"], "BA")
		# The two fields the MOP Log bridge keys on.
		self.assertEqual(item["manufacturing_operation"], r.manufacturing_operation)
		self.assertEqual(
			item["custom_manufacturing_work_order"], r.manufacturing_work_order
		)

	def test_f61_metal_segments_and_extras_share_one_transfer(self):
		se = SimpleNamespace(items=[])
		se.append = lambda _table, d: se.items.append(d)
		r = row(idx=1)
		segments = [{"item_code": METAL, "qty": 0.5}]
		extras = [{"item_code": FIND_A, "qty": 2.0, "batch_no": "BA"}]
		with patch("frappe.new_doc", return_value=se), patch.object(
			msi, "_stamp_se_header"
		):
			msi._build_material_transfer_from_segments(
				eir([r]), r, segments, "MSL - GEPL", "MFG - GEPL", extras
			)
		self.assertEqual([i["item_code"] for i in se.items], [METAL, FIND_A])
