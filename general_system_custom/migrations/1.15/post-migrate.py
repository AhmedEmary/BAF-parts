"""Move Kalkan PO routing from hardcoded name keywords to the vendor contact.

Vendors used to be routed to the Kalkan template and mailbox when their name
(or their company's) contained arnold / euler / brass / kalkan. That routing
now lives in "PO Mail Template" and "PO Sender Email" on the contact, so fill
both on the companies that matched, keeping existing behavior. Only partners
that are vendors (flagged, or with a purchase order) are touched, so customers
whose names happen to match stay as they are.
"""
from odoo import SUPERUSER_ID, api

KALKAN_KEYWORDS = ('arnold', 'euler', 'brass', 'kalkan')
KALKAN_FROM = 'Kalkan Automobile <b.oezleblebici@kalkan-auto.de>'


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    template = env.ref(
        'general_system_custom.email_template_kalkan_purchase',
        raise_if_not_found=False)
    cr.execute(
        """
        SELECT p.id
          FROM res_partner p
         WHERE p.parent_id IS NULL
           AND p.name ~* %s
           AND (p.baf_is_vendor
                OR EXISTS (SELECT 1
                             FROM purchase_order po
                             JOIN res_partner v ON v.id = po.partner_id
                            WHERE v.commercial_partner_id = p.id))
        """,
        ['|'.join(KALKAN_KEYWORDS)],
    )
    partners = env['res.partner'].browse([row[0] for row in cr.fetchall()])
    vals = {'baf_po_email_from': KALKAN_FROM}
    if template:
        vals['baf_po_mail_template_id'] = template.id
    partners.write(vals)
