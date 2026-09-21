from psycopg2 import IntegrityError

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


@tagged('post_install', '-at_install')
class TestAltAccountNumberFormat(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Partner = self.env['res.partner']

    def test_valid_format_accepted(self):
        # Padded (E01BF), unpadded legacy (E1BF) and multi-digit values all pass.
        for value in ('E01BF', 'E09BF', 'E1BF', 'E7BF', 'E42BF', 'E1234BF'):
            partner = self.Partner.create(
                {'name': 'Fmt %s' % value,
                 'baf_alt_account_number': value})
            self.assertEqual(partner.baf_alt_account_number, value)

    def test_empty_value_accepted(self):
        partner = self.Partner.create({'name': 'Empty Alt'})
        self.assertFalse(partner.baf_alt_account_number)

    def test_lowercase_rejected(self):
        with self.assertRaises(ValidationError):
            self.Partner.create(
                {'name': 'Lower', 'baf_alt_account_number': 'e1bf'})

    def test_missing_bf_suffix_rejected(self):
        with self.assertRaises(ValidationError):
            self.Partner.create(
                {'name': 'NoSuffix', 'baf_alt_account_number': 'E1'})

    def test_wrong_prefix_rejected(self):
        with self.assertRaises(ValidationError):
            self.Partner.create(
                {'name': 'V-prefix', 'baf_alt_account_number': 'V1BF'})

    def test_zero_number_rejected(self):
        for value in ('E0BF', 'E00BF'):
            with self.assertRaises(ValidationError):
                with self.env.cr.savepoint():
                    self.Partner.create(
                        {'name': 'Zero %s' % value,
                         'baf_alt_account_number': value})

    def test_free_text_rejected(self):
        with self.assertRaises(ValidationError):
            self.Partner.create(
                {'name': 'Text', 'baf_alt_account_number': '10012'})

    def test_duplicate_rejected(self):
        self.Partner.create(
            {'name': 'First', 'baf_alt_account_number': 'E5BF'})
        with self.assertRaises(IntegrityError), \
                mute_logger('odoo.sql_db'):
            with self.env.cr.savepoint():
                self.Partner.create(
                    {'name': 'Second', 'baf_alt_account_number': 'E5BF'})

    def test_multiple_empty_allowed(self):
        # NULL is not considered equal to NULL by the unique constraint,
        # so many customers can keep the field empty at once.
        for i in range(3):
            self.Partner.create({'name': 'NullAlt %d' % i})

    def test_assign_next_picks_one_past_max(self):
        for value in ('E1BF', 'E3BF', 'E7BF'):
            self.Partner.create(
                {'name': 'Seed %s' % value,
                 'baf_alt_account_number': value})
        target = self.Partner.create({'name': 'Target'})
        target.action_baf_assign_express_account_number()
        # Generated numbers are zero-padded to two digits.
        self.assertEqual(target.baf_alt_account_number, 'E08BF')

    def test_assign_next_counts_padded_and_unpadded_together(self):
        # A padded E08BF and a legacy E7BF share the same numeric space, so the
        # next value is one past the highest number regardless of padding.
        self.env.cr.execute(
            "UPDATE res_partner SET baf_alt_account_number = NULL "
            "WHERE baf_alt_account_number ~ '^E0*[1-9][0-9]*BF$'"
        )
        self.Partner.create(
            {'name': 'Padded', 'baf_alt_account_number': 'E08BF'})
        self.Partner.create(
            {'name': 'Legacy', 'baf_alt_account_number': 'E7BF'})
        target = self.Partner.create({'name': 'Target'})
        target.action_baf_assign_express_account_number()
        self.assertEqual(target.baf_alt_account_number, 'E09BF')

    def test_assign_next_starts_from_one_when_empty(self):
        # Wipe existing E<N>BF values (padded or not) so the counter starts fresh.
        self.env.cr.execute(
            "UPDATE res_partner SET baf_alt_account_number = NULL "
            "WHERE baf_alt_account_number ~ '^E0*[1-9][0-9]*BF$'"
        )
        target = self.Partner.create({'name': 'FirstAssign'})
        target.action_baf_assign_express_account_number()
        self.assertEqual(target.baf_alt_account_number, 'E01BF')

    def test_assign_next_leaves_existing_untouched(self):
        partner = self.Partner.create(
            {'name': 'Keep', 'baf_alt_account_number': 'E9BF'})
        partner.action_baf_assign_express_account_number()
        self.assertEqual(partner.baf_alt_account_number, 'E9BF')

    def test_assign_next_exceeds_two_digits(self):
        # Numbers past 99 keep their natural length (no truncation of padding).
        self.env.cr.execute(
            "UPDATE res_partner SET baf_alt_account_number = NULL "
            "WHERE baf_alt_account_number ~ '^E0*[1-9][0-9]*BF$'"
        )
        self.Partner.create(
            {'name': 'Big', 'baf_alt_account_number': 'E99BF'})
        target = self.Partner.create({'name': 'Target'})
        target.action_baf_assign_express_account_number()
        self.assertEqual(target.baf_alt_account_number, 'E100BF')

    def test_assign_next_is_unique_across_batch(self):
        a = self.Partner.create({'name': 'BatchA'})
        b = self.Partner.create({'name': 'BatchB'})
        a.action_baf_assign_express_account_number()
        b.action_baf_assign_express_account_number()
        self.assertNotEqual(
            a.baf_alt_account_number, b.baf_alt_account_number)
