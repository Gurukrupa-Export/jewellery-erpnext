# Copyright (c) 2026, Nirali and contributors
# See license.txt

"""The ledger-vs-scale invariant: does an operation hold more than it weighed?

``negative_balance_findings`` only sees a job whose OWN key went negative. When a return
is booked against a batch belonging to a DIFFERENT job -- what warehouse FIFO does in a
shared department WIP warehouse -- this job's own batches are simply left overstated,
with nothing negative in its own ledger. That is a positive divergence, and it is what
made MOP-3DP57 reach tagging two weeks later reading 16.720 g against an operator-weighed
16.440 g.

    divergence_g = clamped_ledger_gross + min(loss_wt, 0) - received_gross_wt

Pure-logic: the ledger sweep and the header fetch are patched, so these run with no
fixtures and no site data.
"""

from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from jewellery_erpnext import mop_lineage_audit as audit

GOLD = "M-G-22KT-91.75-Y"
DIAMOND = "D-NT-RO-6B-+6.5-7"


def _row(mop, item_code, batch_no, qty, mwo="MWO-1"):
	return {
		"mop": mop,
		"mwo": mwo,
		"item_code": item_code,
		"batch_no": batch_no,
		"qty": qty,
	}


def _header(name, received, loss=0.0, for_fg=0, **attrs):
	base = {
		"name": name,
		"manufacturing_work_order": "MWO-1",
		"manufacturing_order": "PMO-1",
		"department": "Model Making - KGJPL",
		"status": "Finished",
		"for_fg": for_fg,
		"gross_wt": 0.0,
		"received_gross_wt": received,
		"received_net_wt": 0.0,
		"loss_wt": loss,
	}
	base.update(attrs)
	return base


class _ScaleTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, rows, headers, **kwargs):
		with patch.object(audit.frappe, "get_all", return_value=headers):
			return audit.ledger_vs_scale_findings(rows=rows, **kwargs)

	def _divergence(self, rows, headers, mop="MOP-1"):
		res = self._run(rows, headers)
		for f in res["findings"]:
			if f["manufacturing_operation"] == mop:
				return f["divergence_g"]
		return None


class TestLedgerVsScaleInvariant(_ScaleTestCase):
	def test_positive_overstatement_is_found(self):
		"""The incident: MOP-49T4D, ledger 19.070, loss -0.610, weighed 18.180."""
		rows = [
			_row("MOP-1", GOLD, "B-12L9U", 18.7),
			_row("MOP-1", GOLD, "B-1U6V7", 0.15),
			_row("MOP-1", GOLD, "B-2S9L7", 0.138),
			_row("MOP-1", GOLD, "B-5IB55", 0.043),
			_row("MOP-1", GOLD, "B-JR944", 0.039),
		]
		self.assertEqual(
			self._divergence(rows, [_header("MOP-1", received=18.18, loss=-0.61)]), 0.28
		)

	def test_a_booked_loss_is_not_a_divergence(self):
		"""``ledger - received`` alone flags every operation that ever booked a loss."""
		rows = [_row("MOP-1", GOLD, "B-1", 18.79)]
		res = self._run(rows, [_header("MOP-1", received=18.18, loss=-0.61)])
		self.assertEqual(res["findings"], [])

	def test_a_gain_is_not_double_counted(self):
		"""``min(loss_wt, 0)``: a positive loss_wt is a gain already in the ledger.

		The Waxing shape -- first operation in the chain, so ``gross_wt`` was 0 when the
		receive computed ``loss_wt = received - gross``. Subtracting it again would flag
		2,031 Waxing operations on kg-gk.
		"""
		rows = [_row("MOP-1", GOLD, "B-1", 14.23)]
		res = self._run(rows, [_header("MOP-1", received=14.23, loss=14.23)])
		self.assertEqual(res["findings"], [])

	def test_negative_balance_is_clamped_like_the_writer(self):
		"""A -0.28 key must not subtract from ledger_gross.

		The header writer clamps; replaying raw here would report a divergence on data
		the writer handled correctly.
		"""
		rows = [
			_row("MOP-1", GOLD, "B-OWN", 18.7),
			_row("MOP-1", GOLD, "B-PHANTOM", -0.28),
		]
		self.assertEqual(
			self._divergence(rows, [_header("MOP-1", received=18.42)]), 0.28
		)

	def test_carats_convert_once_per_family(self):
		"""Per-row conversion would invent a sub-milligram divergence.

		0.497 ct + 0.067 ct = 0.564 ct -> 0.113 g, not 0.099 + 0.013 = 0.112 g.
		"""
		rows = [
			_row("MOP-1", DIAMOND, "B-1", 0.497),
			_row("MOP-1", DIAMOND, "B-2", 0.067),
		]
		res = self._run(rows, [_header("MOP-1", received=0.113)])
		self.assertEqual(res["findings"], [])


class TestLedgerVsScaleGates(_ScaleTestCase):
	def test_zero_received_is_skipped_not_flagged(self):
		"""Tagging books no receive weight -- MOP-A463A / MOP-3DP57's shape.

		There is genuinely no scale reading to compare against, so these are counted
		and reported separately rather than flagged or silently dropped.
		"""
		rows = [_row("MOP-1", GOLD, "B-1", 16.236)]
		res = self._run(rows, [_header("MOP-1", received=0.0)])
		self.assertEqual(res["findings"], [])
		self.assertEqual(res["totals"]["not_yet_received"], 1)

	def test_fg_operation_is_skipped(self):
		"""An FG header is force-written MWO-wide by sync_mwo_weights."""
		rows = [_row("MOP-1", GOLD, "B-1", 16.236)]
		res = self._run(rows, [_header("MOP-1", received=15.956, for_fg=1)])
		self.assertEqual(res["findings"], [])
		self.assertEqual(res["totals"]["not_yet_received"], 0)

	def test_finished_operations_are_not_skipped(self):
		"""Skipping Finished is a WRITE-safety rule, not a detection rule.

		All eleven operations in the incident are Finished; skipping them would blind
		this detector to the entire class of defect it exists to find.
		"""
		rows = [_row("MOP-1", GOLD, "B-1", 18.7)]
		self.assertEqual(
			self._divergence(
				rows, [_header("MOP-1", received=18.42, status="Finished")]
			),
			0.28,
		)

	def test_min_divergence_filters_the_carat_residue_family(self):
		"""1 mg differences are real but not actionable -- an explicit, tunable floor."""
		rows = [_row("MOP-1", GOLD, "B-1", 4.461)]
		headers = [_header("MOP-1", received=4.46)]
		self.assertEqual(
			self._run(rows, headers, min_divergence_g=0.0005)["findings"][0][
				"divergence_g"
			],
			0.001,
		)
		self.assertEqual(self._run(rows, headers)["findings"], [])


class TestSncVsHeaderClassification(IntegrationTestCase):
	"""A wrong TAG and a typed-over FIELD are not the same problem.

	``Serial No.custom_gross_wt`` has ``fetch_from = custom_bom_no.gross_weight``, so the
	tag is always BOM-derived and ``total_weight`` never reaches it. A large
	``divergence_g`` with a zero ``tag_divergence_g`` therefore means somebody typed over
	an editable field and the tag came out right anyway -- cosmetic. A non-zero
	``tag_divergence_g`` means a piece shipped with the wrong weight.

	Rows go in through the ``rows`` parameter. Do NOT go back to
	``patch.object(audit.frappe.db, "sql", ...)``: ``frappe.db`` is a global proxy, so that
	patches the database for the whole process, not just for this call. The first
	``flt(x, 3)`` inside then resolves the rounding method through ``get_system_settings``,
	which on a cold process lazily loads System Settings via ``db.sql`` and gets these fake
	rows instead. It raises, ``flt`` swallows it and returns 0.0, and every assertion below
	reads 0.0 -- passing under a mocked-db harness and failing only on a live site.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def _run(self, rows):
		return audit.snc_vs_header_findings(rows=rows)

	def _row(self, **attrs):
		base = {
			"serial_number_creator": "snc-1",
			"docstatus": 1,
			"total_weight": 40.99,
			"manufacturing_operation": "MOP-1",
			"manufacturing_work_order": "MWO-1",
			"fg_bom": "BOM-1",
			"fg_serial_no": "SN-1",
			"gross_wt": 19.18,
			"department": "Tagging",
			"status": "Finished",
			"fg_bom_gross_wt": 19.18,
		}
		base.update(attrs)
		return base

		# The three kg-gk rows: SNC field typed over, BOM (and therefore the tag) correct.

	def test_typed_over_field_with_correct_bom_is_cosmetic(self):
		res = self._run([self._row()])
		f = res["findings"][0]
		self.assertEqual(f["divergence_g"], 21.81)
		self.assertEqual(f["tag_divergence_g"], 0.0)
		self.assertTrue(f["field_only"])
		self.assertEqual(res["totals"]["tags_wrong"], 0)
		self.assertEqual(res["totals"]["field_only"], 1)

	def test_bom_disagreeing_with_header_is_a_wrong_tag(self):
		res = self._run([self._row(total_weight=19.18, fg_bom_gross_wt=16.72)])
		f = res["findings"][0]
		self.assertEqual(f["tag_divergence_g"], -2.46)
		self.assertFalse(f["field_only"])
		self.assertEqual(res["totals"]["tags_wrong"], 1)

	def test_draft_without_a_bom_reports_no_tag_verdict(self):
		"""3hpkt6ck9u's shape -- no FG BOM yet, so no tag exists to call wrong."""
		res = self._run(
			[
				self._row(
					docstatus=0,
					total_weight=16.72,
					gross_wt=16.44,
					fg_bom=None,
					fg_bom_gross_wt=None,
				)
			]
		)
		f = res["findings"][0]
		self.assertEqual(f["divergence_g"], 0.28)
		self.assertIsNone(f["tag_divergence_g"])
		self.assertFalse(f["field_only"])
		self.assertEqual(res["totals"]["tags_wrong"], 0)
		self.assertEqual(res["totals"]["field_only"], 0)


class TestSncVsHeaderQueryContract(IntegrationTestCase):
	"""The SQL must actually run against the site's schema.

	Every test in ``TestSncVsHeaderClassification`` injects its rows through the ``rows``
	parameter, so the query string itself is never executed there -- a column that does not
	exist is invisible to them. That matters here because ``bom.gross_weight`` is a
	**Custom Field** (``custom_fields/bom.json``), not a stock ERPNext BOM column: on a
	site where the custom-field bootstrap has not run, ``snc_vs_header_findings`` dies with
	``Unknown column 'bom.gross_weight'`` the first time a report calls it, and no test
	would have caught it.

	This one calls the function with no ``rows``, taking the real query branch, so MariaDB
	validates every column, join and placeholder in the statement. It asserts the contract,
	not the contents -- a fresh site returns nothing and that is a pass.

	``setUpClass`` is neutralised like the rest of this module -- no fixtures are needed.
	The live connection this test relies on comes from the test runner, not from
	``IntegrationTestCase.setUpClass``.
	"""

	@classmethod
	def setUpClass(cls):
		pass

	def test_query_executes_against_the_real_schema(self):
		res = audit.snc_vs_header_findings()

		self.assertIn("findings", res)
		self.assertIn("totals", res)
		self.assertIsInstance(res["findings"], list)
		for key in (
			"serial_number_creators",
			"submitted",
			"divergence_g",
			"tags_wrong",
		):
			self.assertIn(key, res["totals"])
