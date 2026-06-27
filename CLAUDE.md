# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`jewellery_erpnext` is a **Frappe v16 custom app** that customizes ERPNext for jewellery manufacturing. It is one app inside a bench (`/home/dhinesh/bench/v16/frappe-bench`), installed alongside `erpnext`, `hrms`, `india_compliance`, `payments`, and Gurukrupa-specific apps (`gke_customization`, `gurukrupa_customizations`, `gurukrupa_biometric`). All commands run from the bench directory, not the app directory.

## Commands

All `bench` commands run from `~/frappe-bench` (the bench root), against a site. CI uses the site `test_site`; a real data site referenced in notes is `gk`.

```bash
# Apply schema/patches after pulling or editing doctype JSON
bench --site <site> migrate

# Seed masters + provision custom fields/precision (idempotent; run after migrate on fresh sites)
bench --site <site> execute jewellery_erpnext.create_test_data.setup_data

# Run a whole doctype's tests (test_<doctype>.py next to the doctype)
bench --site <site> run-tests --app jewellery_erpnext --doctype "Manufacturing Work Order"

# Run a single test module under jewellery_erpnext/tests/
bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_employee_ir_loss_baseline

# Narrow to one test case/method
bench --site <site> run-tests --module jewellery_erpnext.jewellery_erpnext.tests.test_repack --test test_name

# Ad-hoc inspection — prefer the console over scripts for poking at data
bench --site <site> console
```

CI (`.github/workflows/test.yml`, runs on PRs to `v16_develop_aerele`) does `migrate` → `setup_data` → ~28 separate `run-tests` invocations. Tests are split into many small modules under `jewellery_erpnext/jewellery_erpnext/tests/` plus per-doctype `test_*.py`; run the specific module you touched rather than the whole app.

Note: `bench run-tests` on the `gk` site aborts at a global `before_tests` hook (erpnext/india_compliance company setup) — that's environmental, not your test. Verify against `gk` data via `bench --site gk console` instead.

### Lint / format

```bash
pre-commit run --all-files     # ruff (lint+format+import sort), prettier, eslint, ast/json/toml checks
ruff check . && ruff format .  # Python only
```

Style (`pyproject.toml`): line length **99**, **tab** indentation, **double-quoted** strings. `pre-commit` blocks commits to `develop`. There is no `package.json`; JS lints via the pinned eslint/prettier pre-commit hooks.

## Architecture

### Two parallel stock ledgers (the central idea)

Manufacturing tracks material in **two** ledgers that must stay consistent:

- **MOP Log** (`doctype/mop_log/`) — the *logical/manufacturing* ledger. A virtual per-`(item, batch, warehouse)` snapshot of what each **Manufacturing Operation** holds. On `validate` it locks its Manufacturing Operation row and recomputes that operation's weight buckets (item-code prefix routes to a bucket: `M`→net, `F`→finding, `D`→diamond+pcs, `G`→gemstone+pcs, `O`→other).
- **Stock Ledger Entry (SLE) / Serial and Batch Bundle (SBB)** — the *physical* ledger. In v16, `SLE.batch_no` is **NULL**; real per-batch stock lives in the **Serial and Batch Bundle** and is read via `qty_after_transaction_batch_based`. Never query `tabStock Ledger Entry.batch_no` for batch stock.
- **Stock Reservation Entry (SRE)** — the source of truth for *who reserved which physical batch where*. Manufacturing flows (Department IR) are often logical-only (MOP Log) and trust the SRE for the real warehouse; resolve a physical source warehouse from SBB stock, not from a MOP Log `from_warehouse`.

EOD sync (`doctype/mop_settings/mop_eod_sync.py`) reconciles MOP Log → physical SLE at a configured time. While it runs it sets `frappe.flags.in_eod_mop_sync`; the `eod_lock` validator (wired on Employee IR, Department IR, MOP Log, Stock Entry, Stock Reconciliation `before_save/submit/cancel`) blocks competing transactions and self-bypasses for the sync.

### Manufacturing flow (lifecycle)

`Manufacturing Plan` → spawns `Parent Manufacturing Order` → `Manufacturing Work Order (MWO)` → creates `Manufacturing Operation` rows (one per department step). Material moves between operations/departments via:

- **Department IR** (`doctype/department_ir/`) — Issue/Receive *between departments*; a Receive must reconcile against a matching Issue (`receive_against` lineage); writes MOP Log rows.
- **Employee IR** (`doctype/employee_ir/`) — Issue/Receive *to/from a worker*. On submit it can create a **Process Loss** Stock Entry, inject extra metal via **Main Slip**, create moulds, raise subcontracting, and drive **Tree Number** casting state.
- **Main Slip** (`doctype/main_slip/`) — the central metal sheet per casting batch (tree↔gold KT conversion, loss/batch tracking).
- **Tree Number** (`doctype/tree_number/`) — a casting batch that groups MWOs; auto-created on a Casting Employee IR Issue, advances Issued→Partially→Received.

### Stock-layer overrides (`override_doctype_class` in hooks.py)

ERPNext stock classes are subclassed in `jewellery_erpnext/jewellery_erpnext/customization/<doctype>/`:
`CustomStockEntry`, `CustomStockLedgerEntry`, `CustomStockReservationEntry`, `CustomSerialandBatchBundle`, `CustomStockReconciliation`, `CustomSubmissionQueue`. Key behaviors: FIFO batch allocation with a shared cross-row `consumed` tally (avoids double-booking a batch across duplicate item rows); SRE auto-reserve preserves operator-picked MWO+MOP `sb_entries` instead of re-FIFOing.

### Concurrency / lock ordering (`jewellery_erpnext/lock_order.py`)

Deadlocks (MariaDB 1213) and lock-wait timeouts (1205) here come from different code paths locking the same rows in different orders. The fix is **one canonical acquisition order**, enforced via helpers — read the module docstring before touching any submit/cascade:

```
Parent control row → tabSeries → tabBin → Batch/SBB → SRE → SLE → MOP Log → Manufacturing Operation
```

- **RULE A**: any loop that locks `tabBin` must iterate a *copy* sorted by `(item_code, warehouse, batch_no)` (`sorted_stock_rows`) — never mutate `self.items`.
- **RULE B**: acquire all needed `tabBin` locks up front in sorted order with `lock_bins(...)`.
- `preallocate_series_for_docs(...)` pins the `tabSeries` counter lock to canonical position 2 for series-named docs. High-churn ledgers (SLE, SRE) are autonamed by **hash** to avoid the shared series row entirely. `bounded_retry.py` wraps idempotent ops.

### The "migrate-time config is dead" problem — two self-healing guards

`after_migrate` is **disabled** in `hooks.py`, and on real/CI sites `install-app` marks patches done without running them and fixtures get overwritten. So config that *only* exists in `custom_fields/*.json` or `property_setter/*.json` never reaches the database, causing `1054 Unknown column` errors at runtime. Two idempotent guards close the gap, each wired in **two** places — a `post_model_sync` patch and `create_test_data.setup_data`:

- **`fetch_from_guard.py`** — purely additive: provisions every missing `custom_*` column targeted by an app `fetch_from`. Safe to run anytime.
- **`property_setter_guard.py`** — deliberately narrow (it is *destructive* — Property Setter validate delete-then-inserts). Pins field **precision to 3** on `Stock Entry Detail.transfer_qty`, `Serial and Batch Entry.qty`, and `Stock Reservation Entry.reserved_qty`. This is essential: with System Settings `float_precision = 2`, a real sub-0.01 loss/reserve (`flt(0.001, 2) = 0.0`) rounds to zero and aborts the submit ("Qty is mandatory for the batch" / "Cannot reserve more than Allowed Qty 0.0"). When adding precision-sensitive fractional flows, confirm the field's precision here.

### Server-side wiring (`hooks.py`)

`hooks.py` is the wiring hub. Two organizational patterns:
- **`jewellery_erpnext/jewellery_erpnext/doc_events/<doctype>.py`** — cross-doctype event handlers (`stock_entry.py`, `bom.py`, `item.py`, `sales_order.py`, …).
- **`doctype/<doctype>/doc_events/`** — feature-scoped helpers for a complex doctype (e.g. `employee_ir/doc_events/` has `loss_stock_entry.py`, `main_slip_inject.py`, `tree_casting.py`, `subcontracting_utils.py`, `precision.py`).
Stock Entry submit is a deliberate cascade in `before_submit`/`on_submit` (EOD lock → `prelock_bins` → app logic → batch rename → subcontracting log/repack) — order matters; preserve it.

Other modules: `refining/` (precious-metal refining), `customer_subcontracting/` (external subcontracting, parent/child batch rename, gold repack), `gurukrupa_exports/`.

## Conventions

- **Patches**: add to `patches.txt` (almost all under `[post_model_sync]`); the file is the source of truth for migrations. Schema/precision fixes that must survive the dead-`after_migrate` problem go through a guard called from both a patch *and* `setup_data`, not a fixture.
- **Tests**: granular modules in `tests/` and per-doctype `test_*.py`; new behavior gets its own small module and a CI step. `create_test_data.setup_data` must self-bootstrap every master (CI runs it on a bare site with no setup wizard).
- **Precision**: weights/qtys are sub-gram/sub-carat — never assume precision 2; route fractional quantities through the precision-3 fields above.
- **Locking**: any new multi-doctype write must follow the canonical order in `lock_order.py` (RULE A + RULE B).
