import base64
import io
import time

import openpyxl

from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


def _xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return base64.b64encode(buf.getvalue())


@tagged('post_install', '-at_install')
class TestVendorImportProgress(TransactionCase):
    """A direct import runs for seconds with no output; it must report itself."""

    def setUp(self):
        super().setUp()
        self.brand = self.env['product.brand'].create({'name': 'ZZPROG'})
        self.vendor = self.env['res.partner'].create({
            'name': 'ZZ Progress Vendor',
            'baf_is_vendor': True,
            'baf_purchase_method': 'direct',
        })
        self.templates = self.env['product.template'].create([{
            'name': 'ZZ Prog Part %d' % i,
            'default_code': 'ZZPG_%03d' % i,
            'sku': 'ZZPGSKU%03d' % i,
            'brand': self.brand.id,
            'list_price': 100.0,
        } for i in range(6)])

    def _run_import(self):
        self.vendor.baf_pricing_file = _xlsx(
            [['SKU', 'Discounted Price']]
            + [[t.sku, '50'] for t in self.templates])
        self.vendor.baf_pricing_filename = 'prices.xlsx'
        return self.vendor.action_import_vendor_pricing_file()

    def _capture_progress(self):
        """Every toast funnels through the mixin, whatever phrased it."""
        pushed = []
        self.patch(type(self.env['baf.progress.notifier']), '_baf_notify',
                   lambda records, title, message, immediate=False:
                       pushed.append((message, immediate)))
        return pushed

    def test_each_sql_phase_reports_itself(self):
        """The SQL half is a few single statements with no progress inside
        them; each must announce itself so the wait is not one silent gap."""
        pushed = self._capture_progress()
        self._run_import()
        messages = [m for m, _i in pushed]
        for phase in ('Reading', 'duplicate SKUs', 'Matching SKUs',
                      'case-insensitively'):
            self.assertTrue(
                any(phase in m for m in messages),
                "no toast for phase %r: %s" % (phase, messages))

    def test_import_reports_start_and_matching(self):
        pushed = self._capture_progress()
        self._run_import()
        self.assertTrue(pushed, "import produced no feedback at all")
        self.assertIn('prices.xlsx', pushed[0][0])
        self.assertTrue(
            any('Matching SKUs' in m for m, _i in pushed),
            "no toast for the SKU-matching phase: %s" % pushed)
        # The import cannot commit, so every toast must go out immediately.
        self.assertTrue(all(immediate for _m, immediate in pushed), pushed)

    def test_row_progress_is_throttled(self):
        # Real threshold is 100k rows; drop it so 6 rows cross it twice.
        self.patch(type(self.env['res.partner']),
                   '_BAF_IMPORT_PROGRESS_EVERY', 3)
        pushed = self._capture_progress()
        self._run_import()
        staged = [m for m, _i in pushed if 'rows so far' in m]
        self.assertEqual(len(staged), 2, "expected one toast per 3 rows: %s" % pushed)
        self.assertIn('3 rows so far', staged[0])
        self.assertIn('6 rows so far', staged[1])

    def test_progress_is_sent_to_the_current_user(self):
        sent = []
        self.patch(type(self.env['bus.bus']), '_sendone',
                   lambda records, target, kind, payload:
                       sent.append((target, kind, payload)))
        self.vendor._baf_push_import_progress('ZZ probe message')
        self.assertEqual(len(sent), 1)
        target, kind, payload = sent[0]
        self.assertEqual(target, self.env.user.partner_id)
        self.assertEqual(kind, 'simple_notification')
        self.assertEqual(payload['message'], 'ZZ probe message')

    def test_progress_does_not_commit_the_caller(self):
        """The direct importer stages into an ON COMMIT DROP temp table, so a
        toast that commits the caller would destroy the rows mid-import."""
        cr = self.env.cr
        cr.execute("DROP TABLE IF EXISTS zz_progress_probe")
        cr.execute("CREATE TEMP TABLE zz_progress_probe (x int) ON COMMIT DROP")
        self.vendor._baf_push_import_progress('probe')
        cr.execute("SELECT to_regclass('zz_progress_probe')")
        self.assertIsNotNone(
            cr.fetchone()[0],
            "the toast committed the import transaction and dropped staged rows")

    def test_progress_message_reports_percent_and_eta(self):
        sent = []
        self.patch(type(self.env['baf.progress.notifier']), '_baf_notify',
                   lambda records, title, message, immediate=False:
                       sent.append((title, message)))
        # 25 of 100 done after 10s -> 30s left.
        self.vendor._baf_notify_progress(
            'T', 'Imported', 'rows', 25, 100, time.time() - 10)
        title, message = sent[0]
        self.assertEqual(title, 'T')
        self.assertIn('Imported 25 / 100 rows (25.0%)', message)
        self.assertIn('ETA ~0m 30s', message)

    def test_progress_message_omits_eta_when_total_unknown(self):
        sent = []
        self.patch(type(self.env['baf.progress.notifier']), '_baf_notify',
                   lambda records, title, message, immediate=False:
                       sent.append((title, message)))
        self.vendor._baf_notify_progress(
            'T', 'Read', 'rows', 1234567, 0, time.time() - 5)
        message = sent[0][1]
        self.assertIn('Read 1,234,567 rows so far', message)
        self.assertNotIn('ETA', message)

    def test_import_survives_a_broken_bus(self):
        def boom(records, target, kind, payload):
            raise RuntimeError('bus down')

        self.patch(type(self.env['bus.bus']), '_sendone', boom)
        with mute_logger(
                'odoo.addons.general_system_custom.models.baf_progress_notifier'):
            res = self._run_import()
        self.assertEqual(res['params']['type'], 'success')
        self.assertEqual(
            self.env['product.supplierinfo'].search_count(
                [('partner_id', '=', self.vendor.id)]), 6)
