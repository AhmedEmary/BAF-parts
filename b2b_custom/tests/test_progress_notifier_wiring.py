from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestProgressNotifierWiring(TransactionCase):
    """Both bulk jobs must toast through the shared mixin, not their own copy."""

    def test_progress_calls_reach_the_shared_helper(self):
        sent = []
        self.patch(type(self.env['baf.progress.notifier']), '_baf_notify',
                   lambda records, title, message, immediate=False:
                       sent.append((title, message, immediate)))
        self.env['mass.product.import']._push_import_progress(5, 10, 0)
        self.env['product.mass.update']._push_progress(5, 10, 0)
        self.assertEqual([title for title, _m, _i in sent],
                         ['Mass Product Import', 'Mass Product Update'])
        self.assertIn('Imported 5 / 10 rows (50.0%)', sent[0][1])
        self.assertIn('Updated 5 / 10 products (50.0%)', sent[1][1])
        # Both jobs commit per batch, so they must not pay for a side cursor.
        self.assertEqual([immediate for _t, _m, immediate in sent],
                         [False, False])


@tagged('post_install', '-at_install')
class TestMassImportSummary(TransactionCase):
    """The completion toast is the only place the counts surface, so it has to
    stay on screen and actually carry them."""

    def test_summary_lists_every_outcome(self):
        message = self.env['mass.product.import']._import_summary({
            'rows': 1088220, 'skipped': 15, 'links': 3,
            'placeholders': 2, 'seconds': 754.0,
        })
        self.assertIn('1,088,220 row(s) imported in 12m 34s', message)
        self.assertIn('15 row(s) skipped', message)
        self.assertIn('3 replacement link(s) set', message)
        self.assertIn('2 replacement placeholder(s) created', message)

    def test_summary_omits_empty_buckets(self):
        message = self.env['mass.product.import']._import_summary(
            {'rows': 10, 'skipped': 0, 'links': 0, 'placeholders': 0, 'seconds': 3.0})
        self.assertEqual(message, '10 row(s) imported in 0m 3s.')

    def test_summary_falls_back_when_stats_missing(self):
        self.assertIn('successfully imported',
                      self.env['mass.product.import']._import_summary({}))
