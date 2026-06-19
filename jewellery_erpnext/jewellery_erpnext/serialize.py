"""Serialize conflicting background work by key, to convert deadlock-prone
concurrency into orderly queuing.

Wraps Frappe's cross-process ``filelock`` so two jobs that would contend on the
same hot resource (a series prefix, an ``(item_code, warehouse)`` Bin, or an MWO
cluster) run one-at-a-time instead of racing into a 1213/1205.

FRAPPE CLOUD CAVEAT (must validate before relying on this for correctness):
``filelock`` is a *file* lock under the site directory, so mutual exclusion holds
only across workers that share that filesystem. On a single-container site that is
all workers; if Frappe Cloud ever distributes a site's workers across hosts, this
degrades to per-host locking and a Redis-based lock would be required instead.
Because of that, this primitive is used as a *contention smoother*, always paired
with an idempotency guard (existence check / status flag / ``deduplicate`` job_id)
that guarantees correctness even if the lock is not globally exclusive — never as
the sole correctness mechanism.
"""

from contextlib import contextmanager

from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

# Keep keys filesystem-safe and bounded in length.
_SAFE = str.maketrans({c: "_" for c in ' /\\:*?"<>|\t\n'})


def conflict_key(*parts):
	"""Build a stable, filesystem-safe lock name from arbitrary parts."""
	raw = "jewl_conflict_" + "__".join(str(p) for p in parts if p not in (None, ""))
	return raw.translate(_SAFE)[:200]


@contextmanager
def conflict_lock(*parts, timeout=30):
	"""Serialize a critical section by key across workers.

	Usage::

	    with conflict_lock("bin", item_code, warehouse, timeout=20):
	        ...  # idempotent work on that (item, warehouse)

	Raises ``LockTimeoutError`` if the lock can't be acquired within ``timeout`` —
	callers that run in a background job should let it propagate (so the job retries
	via its own ``deduplicate`` job_id) or re-enqueue, rather than proceeding
	unserialised.
	"""
	with filelock(conflict_key(*parts), timeout=timeout):
		yield


__all__ = ["conflict_lock", "conflict_key", "LockTimeoutError"]
