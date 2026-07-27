---
name: verify
description: Build, launch and drive this Frappe/ERPNext bench to observe a change at runtime.
---

# Verifying a jewellery_erpnext change

Bench root is `/home/dhinesh/bench/v16/frappe-bench`. Sites: `gk` (jewellery dev,
**holds real production-like data**) and `alfarsi` (default).

## Handles

**CLI** — whitelisted functions are a real operator surface:

```bash
cd /home/dhinesh/bench/v16/frappe-bench
bench --site gk execute <dotted.path.to.func> --kwargs "{'limit': 50}"
```

**HTTP/REST** — the strongest surface. A dev server may already hold **stale code**
(the reloader misses lazily-imported modules), so start your own on a spare port:

```bash
cd /home/dhinesh/bench/v16/frappe-bench/sites          # MUST be sites/, else "apps.txt Not Found"
nohup ../env/bin/python -m frappe.utils.bench_helper frappe \
      --site gk serve --port 8001 --noreload --nothreading > /tmp/serve.log 2>&1 &
```

Auth: mint a token, then **revoke it when done**.

```python
u = frappe.get_doc("User", "Administrator")
u.api_key = u.api_key or frappe.generate_hash(length=15)
u.api_secret = frappe.generate_hash(length=15)
u.save(ignore_permissions=True); frappe.db.commit()
```

```bash
curl -H "Authorization: token <key>:<secret>" -H "Content-Type: application/json" \
     -X POST http://127.0.0.1:8001/api/method/<dotted.path>   -d '{...}'
curl ... -X POST http://127.0.0.1:8001/api/resource/<DocType> -d '{...}'   # create -> runs validate()
curl ... -X POST http://127.0.0.1:8001/api/method/run_doc_method \
     -d '{"dt":"X","dn":"Y","method":"<whitelisted>","args":{...}}'        # doctype buttons
```

## Driving real documents without mutating real data

`bench console` piped from a file **silently swallows `try/except/finally` blocks** —
IPython execs line by line. Put the driver in a temp module and use `bench execute`,
then delete it:

```python
def drive():
    try:
        doc = frappe.get_doc("X", "Y")
        doc.some_method()
    finally:
        frappe.db.rollback()     # ALWAYS; gk is real data
```

Rollback is reliable for document saves. Do **not** drive paths that submit Stock
Entries live — check the guard throws before any SE is inserted instead.

## Replay gotcha

Re-validating a historical submitted doc as a draft (`docstatus = 0`) makes
`before_validate`/`validate` **refresh `gross_wt` from the current Manufacturing
Operation**, which has since moved on — the original gain disappears and guards
that should fire don't. Keep `docstatus = 1` to preserve the inputs the document
was actually submitted with.

## Gotchas

- Stored columns can be stale. Code written before a formula change may have
  persisted a floored/rounded value; re-derive before judging it.
- `flt(x, precision)` returns 0.0 with no site bound.
- Frappe allocates the autoname **before** `validate`, so a rejected insert still
  burns a naming-series number.
