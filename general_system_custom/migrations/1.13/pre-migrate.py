"""Drop the res_partner.is_trusted_vendor column.

The Trusted Vendor flag gated the extra "Customer" / "Customer #" columns
in the vendor PO Excel export. Those columns were dropped, so the field
no longer has a consumer. Removing the column here keeps the schema clean
after the upgrade.
"""


def migrate(cr, version):
    cr.execute(
        "ALTER TABLE res_partner DROP COLUMN IF EXISTS is_trusted_vendor"
    )
