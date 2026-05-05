from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_trusted_vendor = fields.Boolean(
        string="Trusted Vendor",
        help="If checked, the Customer Name column will be included in the PO Excel export sent to this vendor."
    )

    baf_supplier_code = fields.Selection(
        selection=[
            ('SUP1', 'Supplier 1 (DE table — SUP1)'),
            ('SUP2', 'Supplier 2 (DE table — SUP2)'),
            ('SUP3', 'Supplier 3 (DE table — SUP3 / Moto)'),
            ('SUP_JLR', 'JLR Supplier (DE table — SUP_JLR)'),
            ('EU_DIRECT', 'EU Direct (use vendor pricelist)'),
        ],
        string='BAF Supplier Code',
        help=(
            "Selects which discount-table column prefix to use for this vendor. "
            "BMW/MINI vendors share the same brand columns, so the prefix "
            "(SUP1/SUP2/SUP3) is the only thing that distinguishes their prices. "
            "Set to EU_DIRECT for vendors who quote net prices directly through "
            "the standard product.supplierinfo pricelist."
        ),
    )

    baf_brand_ids = fields.Many2many(
        'product.brand',
        'res_partner_baf_vendor_brand_rel',
        'partner_id',
        'brand_id',
        string='Brands Supplied',
        help=(
            "Brands that this vendor can deliver. Used by the auto-vendor "
            "selection on Sales Order lines: only vendors whose brand list "
            "contains the product's brand will be considered for that line."
        ),
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

    visible_brand_ids = fields.Many2many(
        'product.brand',
        'res_partner_product_brand_rel',
        'partner_id',
        'brand_id',
        string='Visible Brands',
        help="Specific brands this customer is allowed to see in the webshop. Brands marked 'Publicly Available' are visible regardless of this selection."
    )

    # ── B2B EU VAT flag ───────────────────────────────────────────────────────
    is_b2b_eu_vat = fields.Boolean(
        string='B2B EU VAT Customer',
        compute='_compute_is_b2b_eu_vat',
        store=True,
        help="Automatically True when the partner has a VAT number and is located "
             "in an EU member state. These customers receive a −5 %% discount on "
             "JLR products unless a specific JLR pricing group is assigned.",
    )

    @api.depends('vat', 'country_id')
    def _compute_is_b2b_eu_vat(self):
        eu_countries = self.env.ref('base.europe', raise_if_not_found=False)
        for partner in self:
            has_vat = bool(partner.vat and partner.vat.strip())
            in_eu = bool(
                eu_countries
                and partner.country_id
                and partner.country_id in eu_countries.country_ids
            )
            partner.is_b2b_eu_vat = has_vat and in_eu

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
