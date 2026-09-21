"""Zero-pad existing Express account numbers to at least two digits.

The Alternative Customer Account Number is now generated as E01BF, E02BF, …
(two-digit minimum) for a cleaner, consistent look. Legacy single-digit values
(E1BF … E9BF) were assigned before the change, so pad them in place. Values
already two digits or longer (E10BF, E100BF) keep their length.

Padding cannot collide: the old format rejected leading zeros, so no E0<N>BF
value exists to clash with.
"""


def migrate(cr, version):
    cr.execute(
        r"""
        UPDATE res_partner
        SET baf_alt_account_number =
            'E0' || substring(baf_alt_account_number from '^E([1-9])BF$') || 'BF'
        WHERE baf_alt_account_number ~ '^E[1-9]BF$'
        """
    )
