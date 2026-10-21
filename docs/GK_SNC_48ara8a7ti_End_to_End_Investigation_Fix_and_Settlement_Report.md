# SNC 48ara8a7ti: investigation, fix, and customer-gold settlement

**Date:** 2026-09-28 · **App:** `jewellery_erpnext` · **Fix:** PR [#1362](https://github.com/Gurukrupa-Export/jewellery-erpnext/pull/1362)
(`fix/fg-bom-self-row` → `kggk_uat`, commit `032190e9`), ported to prod PR #1275 as `05186c87`
· **Test site:** kg-gk (a production data copy), run on 2026-09-28 from 12:12 to 12:40 IST

## 1. Summary

- **What failed.** Serial Number Creator **48ara8a7ti** (ring RI00650-006, customer GJCU0009, order
  SAL-ORD-2026-00412) failed four queue attempts on 26-09:
  - attempts 1–2 hit item-master gaps that a user fixed within a minute ("cannot have Batch", then "not
    allowed as Customer Goods"; `has_batch_no` and the customer-goods flag were ticked at 15:53);
  - attempts 3–4 (queue rows 6avjt34l0s, 6cepufih2q) failed with
    `BOM BOM-RI00650-006-008 must be submitted`, which is the defect fixed here.
- **Root cause.** The finished-goods (as-built) BOM was a copy of the design BOM, and it kept the design
  BOM's standard items. A design BOM made by the order form lists **the finished item itself** as its
  only standard item. ERPNext links that row to the item's default BOM and then refuses it:
  - a **draft** default gives "must be submitted" (this case);
  - a **submitted** default gives "BOM recursion", the commonest SNC failure (13 of the 27 failed SNC
    queue rows before this run).
- **Fix.** The as-built BOM now lists what the piece actually consumed:
  - each material once, with quantities summed across batches;
  - never the finished item;
  - never a sub-assembly BOM link.
- **Proof on kg-gk.**
  - The real SNC submitted through the Submission Queue path.
  - The piece was sold (Delivery Note, then Sales Invoice).
  - The customer-gold settlement Journal Entry posted automatically: **Rs 1,28,133.91** against an
    independent oracle of **Rs 1,28,129.91**. The Rs 4.00 gap is the 3-decimal rounding of the stored
    batch components (§6).
  - Retry, re-fire, return and cancel each behaved exactly once.
  - Every settlement document was then reversed, and the settings were restored.
- **Rollout blocker, not code.** Production's Customer Gold COGS-adjustment account is
  *Advances from Customers − KGJPL*, a **Liability**. The posting guard (F4) refuses it, so every
  customer-gold delivery under Nominal valuation will fail at Delivery Note submit until Finance remaps it
  (§9).

## 2. Root cause

1. **Design BOM BOM-RI00650-006-001** (Template) has one standard item: RI00650-006 itself, 1 Nos,
   with `bom_no` empty.
   - It was written by gke `Order.bom_creation` (`gke_order_forms/doctype/order/order.py:187-188`).
   - **13,811 Template BOMs** on kg-gk carry such a self row.
2. **`create_finished_goods_bom`** (`manufacturing_operation.py`) did `frappe.copy_doc(design_bom)`. It
   cleared the detail tables and operations but **not `items`**, so the self row was copied into the
   as-built BOM.
3. **At insert**, ERPNext `BOM.set_bom_material_details` fills an empty `bom_no` from `Item.default_bom`.
   For RI00650-006 that is **BOM-RI00650-006-008**:
   - a *draft* Finish Goods BOM with `is_default = 1`;
   - migrated history: SNC-02448, tag KLHG42E0108, created 2026-08-08, never modified.
4. **`validate_bom_no`** refuses a draft BOM, which gives "must be submitted".
   - ERPNext skips this check under `frappe.in_test`, so the test suite could never catch it.
5. **Second symptom, same root.** When the default BOM is *submitted*, the copied row links to it and
   `check_recursion` raises "BOM recursion". ERPNext's `manage_default_bom` makes an as-built BOM the item
   default whenever no *submitted* default exists, even with `is_default = 0` (F21 docstring). So the
   first successful SNC of a design sets the trap for the next one. On kg-gk,
   **BOM-RI00650-006-043 is now RI00650-006's default** for exactly this reason (§5).

**Exposure on kg-gk (read-only):**

- **Items whose default BOM is not submitted:** 6,768 in total:
  - 5,489 draft Finish Goods BOMs;
  - 1,278 draft Sales Order BOMs;
  - 1 Manufacturing Process BOM.
- **Failed SNC queue rows before this run:** 27 in total:
  - 13 "BOM recursion" (13 SNCs);
  - 3 "must be submitted" (2 SNCs: 48ara8a7ti and 3dbvgun1v9);
  - 11 other, such as item-master gaps.

  This run's retry test adds one more Failed row (01g2btskqb).
- **Item-master prerequisites** (not a code defect). A newly designed finished item needs `has_batch_no`
  and *Inventory Type Can be Customer Goods* before a customer-gold SNC can submit. 48ara8a7ti's first
  two attempts failed on exactly these.
- **Draft SNCs that fail today on the unfixed code:**

  | SNC | Item | Item default BOM | Unfixed result |
  |---|---|---|---|
  | cijrh9gud9 | BA00827-003 | BOM-BA00827-003-005 (draft) | must be submitted |
  | 25mggchcbt | BA00740-006 | BOM-BA00740-006-007 (submitted) | BOM recursion |
  | eiohukfm0r | BA00818-003 | BOM-BA00818-003-018 (submitted) | BOM recursion |
  | dl91nhl42s | EA40624-001 | BOM-EA40624-001-004 (submitted) | BOM recursion (already failed once) |

  Retry these after the fix is deployed. The other 11 draft SNCs have no self row, or no default BOM.

## 3. The fix (PR #1362)

`jewellery_erpnext/doctype/manufacturing_operation/manufacturing_operation.py`:

- `_consumed_bom_items(data, fg_item)` builds one row per consumed item:
  - quantities summed, with the stock UOM;
  - `do_not_explode = 1` and no `bom_no`;
  - the finished item is skipped.

  `do_not_explode` makes ERPNext blank `bom_no` instead of reading `Item.default_bom`, so neither
  `validate_bom_no` nor `check_recursion` has anything to see.
- `create_finished_goods_bom` replaces the copied `items` with those rows. It throws a readable error if
  nothing was consumed.
- Before submit, `items` is emptied when the detail tables will rebuild it. `doc_events/bom.py`
  `_set_bom_items_by_child_tables` *appends* the metal, diamond, gemstone and finding variants on submit,
  and otherwise every material would be listed twice.
- **No configuration and no data change are needed.** The app's designated placeholder item
  (`Jewellery Settings.defualt_item`) is NULL on kg-gk, which ruled out the placeholder approach.

**Tests.** `tests/test_fg_bom_items.py` has 9 tests and is registered in `manufacturing-tests.yml`. It runs
ERPNext's own `set_bom_material_details` and `validate_materials` on the new rows, rather than relying on
the `in_test`-skipped check:

- two batches of one item merge into a single row;
- the finished item is excluded;
- no `bom_no`;
- zero and blank rows are dropped;
- a draft item default never reaches a consumed row;
- **control:** the copied design row *does* pick up the default and fails validation;
- after the submit-time rebuild each material appears once. Without the clear it appears twice.

**One existing test changed.** `refining/.../test_refining_entry.py` `test_serial_no_refining` used to
drop the last refining line with `material_items.pop()`. That line was the finished item's own row (the
design code, 1 Nos), which as-built BOMs no longer carry, so on the fix the pop removed the gold line
instead: "Gold input weight is required for proportional recovery".
- This was confirmed by two instrumented CI runs, on base and on the fix.
- The test now asserts that the design-code line is absent. Its remaining lines are identical to the
  base run's lines after its pop.
- Commits: `ace48f56` (#1362) and `075a043d` (#1275).

**CI (GitHub Actions), 2026-09-28 13:28:** all 4 workflows are green on both PRs. Refining & Advanced
ran all 16 commands: 722 tests on #1362 and 725 on #1275, all OK.

**Regression on kg-gk** (35 suites, via the test guard, which restores Singles and gold rates):

- `kggk_uat` + fix: **32 pass**.
- The 3 failures (`test_serial_number_creator`, `test_mop_log`, `test_manufacturing_work_order`, 6
  errors) all stop at setup with `Could not find Company: Test_Company`. The same errors occur on the
  **unfixed** base code: they are fixture-dependent tests that kg-gk cannot host.
- The prod port shows the same pattern.

## 4. How the real run was done (safeguards)

- **Backup:** `sites/kg-gk/private/backups/20260928_120806-kg-gk-database.sql.gz` (564.7 MiB), taken
  before any write.
- **Settings for the run**, originals recorded to disk first and all restored and verified at the end:
  - `GST Settings.sandbox_mode` 0 → 1;
  - `mute_emails` 0 → 1;
  - Customer Gold COGS-adjustment account (row `af0g02cvkk`) *Advances from Customers − KGJPL* →
    **Cost of Goods Sold − KGJPL**, the user's choice.
  - No emails were queued during the run.
- **No `bench migrate`.** The fixed code ran from the worktree via `PYTHONPATH`, in-process.
- **Worker isolation.** `bench start` was running, with workers and a live scheduler on the unfixed code.
  - To keep our work off them, RQ `get_queue` was replaced by a recorder, and each captured job was
    replayed in-process through Frappe's own `execute_job` (the worker path).
  - The SNC submission therefore ran the real `SubmissionQueue.background_submission`, including
    jewellery's `CustomSubmissionQueue`.
  - The EOD sync, the only stock-moving scheduled job, fires at 19:00; the run finished by 12:40.
- **Acting user:** Administrator.

## 5. SNC submission

**Recovery.** The draft was stale. EOD transfer MAT-STE-19023 (26-09 19:02) had moved the materials to
**Tagging WO** and replaced the reservations. On submit, the SNC re-resolved its warehouses from the job's
active reservations with no manual edit:

| | Draft said | Submitted from |
|---|---|---|
| D-NT-RO-6B-+7-7.5 2.36 ct | Diamond Setting WO | Tagging WO (SRE 9naii3j6mu) |
| M-G-22KT-91.75-Y 2.021 g, batch -08-A | Waxing WO | Tagging WO (SRE 9naisailc7) |
| M-G-22KT-91.75-Y 6.929 g, batch -10-A | Waxing WO | Tagging WO (SRE 9najf808af) |

**Result.** Submission Queue **v0ts2n2fog** finished.

- **Manufacture entry MAT-STE-19059:**
  - consumed 2.36 ct company diamond (93,582.54), 2.021 g (28,900.24) and 6.929 g (99,275.45) of
    GJCU0009's 22KT, all from Tagging WO;
  - produced **1 × RI00650-006** into Tagging FG;
  - serial **KLHGX62F1139**, batch `GJCU0009-2F09-RI00650-006-A-A`, lane Customer Goods / GJCU0009;
  - FG valued **2,21,761.79**: consumed 2,21,758.23 plus 3.56 operating cost (SE additional cost), so not
    zeroed;
  - GL: Dr Tagging Finished Goods 2,21,761.79 / Cr Tagging Semi Finished Goods 2,21,758.23 / Cr
    Operating Expense 3.56.
- **As-built BOM BOM-RI00650-006-043:**
  - submitted and active;
  - items **gold 8.95 g + diamond 2.36 ct**, with no self row and no `bom_no`;
  - `tag_no` KLHGX62F1139;
  - Serial No `custom_bom_no` = BOM-043.
- **Reservations:** all three SREs consumed exactly once (Delivered), with no new or duplicate SREs.
  MWO-KGJPL-RI00650-006-16-01 is Completed.
- **Custody ledger:**
  - Release (RM): −2.021 g / −1.854 fine and −6.929 g / −6.357 fine;
  - Production (FG): the same quantities, positive.
  - The customer's fine gold did not change; it moved stage from raw material to finished goods.
- **FG batch components:**
  - customer 24KT-10 **6.357**, 24KT-09 **1.686**, 24KT-08 **0.169** (8.212 g);
  - company alloy M-Genia-221 0.739 g;
  - company diamond 2.36 ct.
- **Retry.** A second submission from a stale copy of the draft (a form opened before the first submit)
  failed with `TimestampMismatchError` (queue row **01g2btskqb**). Counts before and after were identical:
  1 Manufacture SE, 1 serial, 4 SLE, 3 GL.
- **Side effect, by ERPNext design:** RI00650-006's default BOM moved from draft BOM-008 to BOM-043
  (see §2 point 5).

## 6. Sale and settlement

**Delivery Note DN-26-00024:**

- mapped from SO-412 by gke's `make_delivery_note`;
- serial and batch set explicitly;
- from Tagging FG, like the recent customer-gold deliveries DN-26-00019…23.

SO-412 is priced at 0, and the flow sets no price, so the DN and the invoice carry 0. The settlement is
valued from the customer's receipts, not the selling price. The entitlement check (`before_submit`)
passed.

**Posted automatically on DN submit:**

| Document | Dr | Cr | Amount |
|---|---|---|---|
| DN-26-00024 (stock) | Cost of Goods Sold − KGJPL | Tagging Finished Goods − KGJPL | 2,21,761.79 |
| **KGJPL-JE-JE-26-00019** (settlement) | Customer Goods Receive − KGJPL − KGJPL | Cost of Goods Sold − KGJPL | **1,28,133.91** |

Custody event `1l65bl924t` (Delivery, Closed) records −1 piece, −8.212 g fine and −1,28,133.91, and it is
claimed by JE-19.

**Independent oracle.** Lineage comes from the conversion entries, and rates from the receipt entries;
no settlement code is involved.

| Receipt batch | Receipt | Rate / g | Share of consumed metal (g) | Value |
|---|---|---|---|---|
| 24KT-08 | KGJPL-SE-CGR-26-00012 | 15,747.0 | 2.021 × 1/11.989 = 0.1685712 | 2,654.49 |
| 24KT-09 | KGJPL-SE-CGR-26-00013 | 15,563.4 | 2.021 × 10/11.989 = 1.6857119 | 26,235.41 |
| 24KT-10 | KGJPL-SE-CGR-26-00014 | 15,610.0 | 6.929 × 20/21.798 = 6.3574640 | 99,240.01 |
| **Total** | | | **8.2117471** | **1,28,129.91** |

- The rates match the custody ledger's receipt events exactly.
- The oracle agrees with the stock ledger: the customer metal consumed was 28,900.24 + 99,275.45 =
  1,28,175.69. Less the company alloy (0.7382 g at Rs 62 = 45.77), that is 1,28,129.92.
- **JE − oracle = Rs 4.00 (0.003 %).** The settlement uses the FG batch components as stored, at 3
  decimals: 6.357 × 15,610 + 1.686 × 15,563.4 + 0.169 × 15,747 = **1,28,133.91**, which is the JE to the
  paisa. The stored components over-state fine gold by 0.00025 g in total. The effect per line is
  bounded by about 0.0005 g × rate (≤ Rs 8 at today's rates), and the signs vary. It is small, but a
  receipt batch's releases will not sum exactly to its receipt (§8, finding B).

**Sales Invoice SINV-26-00051** (from the DN, gke mapper, `update_stock = 0`): no GL (0 value), no custody
event, and no second settlement. The e-invoice was not generated (auto-generate off; sandbox on).

## 7. Scenario matrix

| # | Scenario | Result | Evidence |
|---|---|---|---|
| 1 | Root cause identified and proven from code and data | PASS | §2 |
| 2 | Fix in the owning app | PASS | #1362 (`032190e9`), prod `05186c87` |
| 3 | New unit tests pass; they exercise ERPNext's real BOM code | PASS | 9/9 on kg-gk |
| 4 | Existing suites unaffected | PASS | 32/35; the 3 fixture errors are identical on the unfixed base |
| 5 | Stale draft recovers without manual edits | PASS | warehouses re-resolved to Tagging WO |
| 6 | Submit through the Submission Queue path | PASS | v0ts2n2fog Finished |
| 7 | Manufacture entry: quantities, lanes, warehouses | PASS | MAT-STE-19059 |
| 8 | Finished piece valued (not zeroed) | PASS | 2,21,761.79 |
| 9 | As-built BOM submitted, material rows only, no self row | PASS | BOM-RI00650-006-043 |
| 10 | Serial No created and linked to the BOM | PASS | KLHGX62F1139 |
| 11 | Reservations consumed once, no duplicates | PASS | 3 SREs Delivered, 0 new |
| 12 | Release and Production custody events | PASS | 2 + 2 events, fine 8.211 |
| 13 | FG batch components (customer lineage) | PASS | 6.357 / 1.686 / 0.169 |
| 14 | SO line `produced_qty` +1 | N/A | the jewellery flow never maintains it: 0 of 990 SNC-completed SO lines have it; MWO Completed instead |
| 15 | Repeat submission is refused, nothing duplicated | PASS | 01g2btskqb Failed (TimestampMismatch); counts unchanged |
| 16 | Delivery Note from the SO via the gke mapper | PASS | DN-26-00024 |
| 17 | Entitlement check before stock moves | PASS | before_submit passed |
| 18 | Settlement JE posts automatically on delivery | PASS | KGJPL-JE-JE-26-00019 |
| 19 | Settlement amount equals the independent oracle | PASS, Rs 4.00 explained | 3-decimal component rounding (§6) |
| 20 | Sales Invoice from the DN; no double settlement | PASS | SINV-26-00051, 0 events |
| 21 | Delivery hook re-fired: no second JE | PASS | still 1 event, 1 JE |
| 22 | Sales return reverses the settlement | PASS | DN-26-00025 → KGJPL-JE-JE-26-00020 (mirror) |
| 23 | Cancelling the return cancels its JE and writes a Reversal | PASS | JE-20 cancelled; event 2pmn2kim9c |
| 24 | Cancelling SI then DN cancels the JE and writes a Reversal | PASS | JE-19 cancelled; event 30ad3nq2ba |
| 25 | Company-only piece settles nothing (control) | NOT RUN on kg-gk | it would need a real order's piece delivered; covered by unit tests (`test_customer_gold_fulfilment`: regular-stock and unknown batches are not customer-owned) |
| 26 | Settings restored | PASS | sandbox 0, `mute_emails` 0, account restored |
| 27 | Production account mapping allows the settlement | BLOCKED (Finance) | the current account is a Liability; F4 refuses it (§9) |

## 8. Findings outside the fix

- **A. Legacy self rows in existing as-built BOMs.** **411 submitted** SNC as-built BOMs, and 2,361 draft
  ones, list the finished item as their own material. They submitted because the item had no default BOM
  at that moment.
  - The fix stops new ones; the existing rows stay.
  - Every one of them has other rows too, so removing the self row would never leave a BOM empty.
  - No code requires the finished item to be in these rows; refining only filters it out.

  Readers skewed by the row (from a code sweep, with the figures checked on kg-gk):

  | Reader | Effect of the self row | Status |
  |---|---|---|
  | Copying a serial's BOM onto a Sales Order (`doc_events/sales_order.py` `create_serial_no_bom`) | 141 of the rows have `do_not_explode = 0`. ERPNext links them to the item default and raises BOM recursion; the error is swallowed, so the SO row is left without a BOM or rate. | **Live:** 4 logs, SAL-ORD-2026-00377 (KLHGX62F0920) and SAL-ORD-2026-00331 (KLHG42F0160) |
  | Internal Serial Number Refining (`refining_entry.py` BOM-component rows) | The row counts as about 1 g of metal at the item's purity, so input and pure weight are inflated and recovery reads low | Latent: no such entries on kg-gk yet |
  | ERPNext BOM cost / exploded items | The row is priced at the finished item's valuation: **Rs 4.25 crore** across the 411 BOMs (e.g. BOM-NE03693-001-003: 7.41 lakh of 15.22 lakh). | Latent: app pricing reads the detail tables, not `total_cost` |
  | gke Repair Order `create_bom`, Product Return Order `copy_doc(bom)` | Carry the row into new BOMs, with the same recursion risk | Occasional |

  **Other producers.** gke still writes Finish Goods BOMs with a self row: Product Return Order
  (`product_return_order.py:195-206`, fresh path without `do_not_explode`) and CAD repair
  (`repair_order.py:412-470`). Recommended as separate decisions, not done here: remove the self row from
  the 411 BOMs (a patch that also recomputes cost), or have the four readers skip
  `item_code == bom.item`, and align the gke producers.
- **B. Batch-component precision.** Components are stored at 3 decimals, and the settlement multiplies
  the stored values. Here that is Rs +4.00 on Rs 1.28 lakh. Over a receipt batch's life, the rounded
  releases will not sum exactly to the receipt, leaving a small residue on the liability.
  - Options: store components at 6 decimals, or settle from exact lineage.
  - Low priority; not changed here.
- **C. Default BOM drift.** Each first as-built BOM of a design becomes the item default (ERPNext rule,
  §2 point 5). 276 submitted as-built BOMs since 09-01 are item defaults. With this fix that no longer
  breaks SNCs, but flows that resolve `Item.default_bom` still get one piece's composition. That is the
  F21 concern, and F21 only partly addresses it.
- **D. Environment.** Two `Error Attaching File` logs were raised on BOM-043: design image
  `KXD24359_1.jpg` is missing from kg-gk's files. The copy refresh does not bring files. There are 41
  identical errors since 09-15, unrelated to this fix.
- **E. Optional gke follow-up.** Have `Order.bom_creation` (`order.py:188`) stop writing a live self row,
  or mark it `do_not_explode`. This fix does not need it.

## 9. Rollout

1. **Merge #1362 into `kggk_uat`** and deploy it to the cloud test site. Then resubmit SNC 48ara8a7ti
   from its form; its four failed queue rows stay as history.
2. **Finance decision (blocking for every customer-gold sale under Nominal).** Set the Customer Gold
   COGS-adjustment account for KGJPL (*Subcontracting Settings → Company Accounts*) to an Expense
   account.
   - Today's value, *Advances from Customers − KGJPL*, is a Liability. The F4 guard refuses it at
     posting, so the Delivery Note fails.
   - This test used *Cost of Goods Sold − KGJPL*.
3. **Retry the at-risk drafts:** cijrh9gud9, 25mggchcbt, eiohukfm0r, dl91nhl42s.
4. **Prod:** #1275 carries the same commit (`05186c87`).
5. No data cleanup is required for the fix. Treat findings A–C as separate decisions.

## 10. Documents created on kg-gk

All were created by Administrator between 12:12 and 12:40 on 2026-09-28. They stay visible until the next
refresh.

| Type | Name | Final state |
|---|---|---|
| Submission Queue | v0ts2n2fog | Finished (the real submission) |
| Submission Queue | 01g2btskqb | Failed (retry test) |
| Serial Number Creator | 48ara8a7ti | **Submitted** (was draft) |
| Stock Entry (Manufacture) | MAT-STE-19059 | Submitted |
| BOM | BOM-RI00650-006-043 | Submitted, now RI00650-006's default |
| Serial No | KLHGX62F1139 | Active, Tagging FG − KGJPL |
| Batch | GJCU0009-2F09-RI00650-006-A-A | 1 piece in Tagging FG |
| Delivery Note | DN-26-00024 | Cancelled |
| Delivery Note (return) | DN-26-00025 | Cancelled |
| Sales Invoice | SINV-26-00051 | Cancelled |
| Journal Entry | KGJPL-JE-JE-26-00019, KGJPL-JE-JE-26-00020 | Cancelled |
| Customer Gold Ledger Entry | v13gib31ik, v14dsrpsfo (Release); v1stg14lbp, v1st851nhr (Production); 1l65bl924t (Delivery); 2gg8aiehja (Delivery Return); 2pmn2kim9c, 30ad3nq2ba (Reversals) | Delivery-side net 0 |
| Error Log | v2179juhra, v21hpdaq1u | missing design image (finding D) |

Also: Subcontracting Settings was saved once during the switch (no Version row; the doctype does not
track changes). It was then restored below validation, because the original Liability account would
fail its own save validation.

**Net effect left on kg-gk:**

- the piece is manufactured and in stock, holding the customer's 8.212 g;
- the sale chain nets to zero in GL and in the custody ledger;
- the only live postings are MAT-STE-19059's.

## 11. The other drafts that hit the same defect (2026-09-28, 14:05–14:20)

**Bench state first.** The machine rebooted at 13:48. At 14:01 `apps/jewellery_erpnext` was re-cloned, shallow,
on `fix/fg-bom-self-row`, so the bench now runs the fix. The re-clone deleted the untracked `docs-customer-gold/`
evidence tree and `AGENTS.md`. At 14:14 a requirements reinstall pulled sqlglot down to 27.x, which broke every
erpnext import; this was repaired to 30.20.0 and the bench restarted at 14:17.

Each draft was first submitted inside a transaction and rolled back, with `frappe.db.commit` made to raise.
Then:
- backup `20260928_141200-kg-gk-database.sql.gz`;
- GST sandbox on and `mute_emails` on;
- real submission through the Submission Queue path, on the deployed code;
- settings restored. No email was queued.

| Draft | Owner | Result |
|---|---|---|
| eiohukfm0r | mansi_d | **Submitted.** Queue 8jcdovf2d6 · MAT-STE-19060 · BOM-BA00818-003-019 (gold 12.982 g, diamond 0.81 ct; no self row) · serial KLHGX62F1140. Previously failed with BOM recursion. |
| dl91nhl42s | mansi_d | **Submitted.** Queue 8nqhtmigis · MAT-STE-19061 · BOM-EA40624-001-005 (gold 13.2 g, 2 diamond lots, finding 1.1 g; no self row) · serial KLHGX62F1141. Previously failed with BOM recursion. |
| cijrh9gud9 | mansi_d | **Not submitted: item master.** BA00827-003 is not a stock item and has `has_serial_no = 0` ("does not have Serial No … check item master"). This check runs before the BOM step. The item has no stock ledger entries, so both flags can still be set; that is the owner's call. |
| 25mggchcbt | ruchi_p | **Not submittable.** Its operation MOP-2609-HQUJ51 has no MOP Log rows, so the draft has no materials ("Serial No None not found"). Recreate it or delete it. |

Also created: four `Error Attaching File` logs on BOM-BA00818-003-019 (design images KXD26673_* missing from
kg-gk's files; environment only).

**Independent verification** (read-only agents, one SNC × one lens each; a skeptic re-checked every finding).
All four verdicts are CORRECT.
- **Stock.** Every consumed row matches one of the job's own reservations, and each reservation was consumed
  once for its full quantity:
  - eiohukfm0r: SREs 8djkinaml4 and a7bigppus3 on SO-00322;
  - dl91nhl42s: 8slhopff2f, 3h8mb8chf4, 18amfcte28 and 18am6b3v0r on SO-00382.

  After each entry, every source batch holds exactly what other jobs still have reserved there. No other job's
  stock was taken, nothing went negative, and there are no duplicates.
- **dl91nhl42s drew its gold (13.2 g) and diamond (1.14 ct) from Hammer WIP WH 21. That was correct.** The job
  moved them in itself (MAT-STE-18488 and 18418), and its reservations sat there. The 09-22 EOD sync
  (MOP-EOD-SYNC-2026-00069, partially completed) never moved them to Tagging. Re-resolution did change the
  draft's warehouse for the other rows, and that was right too: the old Hammer rows would have eaten other jobs'
  reserved stock (0.81 ct for eiohukfm0r, 0.36 ct for dl91nhl42s).
- **Value.** FG value = consumed + operating cost, to the paisa: 2,14,690.81 + 117.27 and 2,47,372.57 + 520.05.
  GL balances.
- **Older issues, not caused by this change:**
  - both items' default BOMs are as-built BOMs made before the fix and still carry the self row
    (BOM-BA00818-003-018, BOM-EA40624-001-004; part of finding A);
  - the MOP Log and the stock ledger disagree on location after a failed EOD-sync step;
  - eiohukfm0r's `ref_customer` (AACU0001) differs from its order's customer (GJCU0009);
  - SNC `status` stays "Draft" after submit on all 1,328 submitted SNCs.
