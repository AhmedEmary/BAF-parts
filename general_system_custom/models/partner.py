from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_trusted_vendor = fields.Boolean(
        string="Trusted Vendor", 
        help="If checked, the Customer Name column will be included in the PO Excel export sent to this vendor."
    )
    sales_group_ids = fields.Many2many(
        'baf.sales.group',
        'baf_sales_group_partner_rel',
        'partner_id',
        'group_id',
        string='Sales Pricing Groups',
        help=(
            "Controls which pricing method and discount table columns "
            "apply to this customer. "
            "Leave empty → customer sees full UPE (MSRP = guest price). "
            "Assign at most one group per brand family "
            "(for example one BMW/MINI group and one JLR group)."
        ),
    )

    @api.constrains('sales_group_ids')
    def _check_sales_group_ids_unique_family(self):
        for partner in self:
            groups_by_family = {}
            for group in partner.sales_group_ids:
                family_groups = groups_by_family.setdefault(group.brand_family, self.env['baf.sales.group'])
                groups_by_family[group.brand_family] = family_groups | group

            duplicate_families = {
                family: groups
                for family, groups in groups_by_family.items()
                if len(groups) > 1
            }
            if duplicate_families:
                details = '; '.join(
                    _("%(family)s: %(groups)s") % {
                        'family': dict(self.env['baf.sales.group']._fields['brand_family'].selection).get(family, family),
                        'groups': ', '.join(groups.mapped('name')),
                    }
                    for family, groups in duplicate_families.items()
                )
                raise ValidationError(_(
                    "A customer can only belong to one sales pricing group per brand family. "
                    "Conflicts found: %(details)s"
                ) % {'details': details})

