"""OEM ↔ Aftermarket cross-reference links.

One row = "IC part <ic_sku> is an aftermarket alternative for OEM
template <oem_template_id>". Because a row is a pair, the model gives a
true **many-to-many**:

  * one OEM template may have many links (many IC alternatives);
  * one IC SKU may appear in links of many OEM templates (fits BMW
    *and* Mercedes, etc.).

``aftermarket_template_id`` stays empty until the IC part is
*materialised* as a real ``product.product`` (which happens lazily at
first add-to-cart). After that, the link also connects the two Odoo
templates directly, so back-office users can hop between an OEM part
and its bought alternatives.

Rows come from three sources (``source``):
  * ``auto``   — created by the CSV auto-map (high-confidence tiers);
  * ``manual`` — created/curated by a person (always wins over auto);
  * ``shop``   — recorded when a customer's cart click materialised
                 the product and no link existed yet.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BafOeLink(models.Model):
    _name = 'baf.oe.link'
    _description = 'OEM ↔ IC Aftermarket Link'
    _order = 'oem_template_id, sequence, id'
    _rec_name = 'ic_sku'

    oem_template_id = fields.Many2one(
        'product.template', string="OEM Product", required=True,
        index=True, ondelete='cascade',
        help="The original (OEM) product in BAF's catalog.",
    )
    ic_sku = fields.Char(
        string="IC SKU", required=True, index=True,
        help="Inter Cars SKU (tow_kod) of the aftermarket alternative.",
    )
    aftermarket_template_id = fields.Many2one(
        'product.template', string="Aftermarket Product",
        index=True, ondelete='set null',
        help="Filled once the IC part has been materialised as a real "
             "Odoo product (first purchase). Empty means the part is "
             "only known through the IC catalog so far.",
    )
    source = fields.Selection(
        [
            ('auto', 'Auto (CSV cross-match)'),
            ('manual', 'Manual'),
            ('shop', 'Shop (materialised at cart)'),
        ],
        default='manual', required=True, index=True,
        help="Where this link came from. Manual links are never touched "
             "by the auto-map and sort first on the product page.",
    )
    sequence = fields.Integer(
        default=10,
        help="Display order on the product page — lower first. Manual "
             "links default to 5 so they outrank auto links.",
    )
    active = fields.Boolean(
        default=True,
        help="Archive instead of delete to remember rejected matches: "
             "an archived link stops the auto-map from re-proposing "
             "the same pair.",
    )

    # ── Convenience info from the local IC cache ─────────────────────────
    ic_brand = fields.Char(
        string="IC Brand", compute='_compute_ic_info',
    )
    ic_description = fields.Char(
        string="IC Description", compute='_compute_ic_info',
    )
    ic_tec_doc = fields.Char(
        string="TecDoc ArtNr", compute='_compute_ic_info',
    )

    _oem_ic_sku_uniq = models.Constraint(
        'unique(oem_template_id, ic_sku)',
        "This OEM product is already linked to that IC SKU.",
    )

    @api.depends('ic_sku')
    def _compute_ic_info(self):
        Info = self.env['ic.product.info'].sudo()
        by_sku = {}
        skus = [l.ic_sku for l in self if l.ic_sku]
        if skus:
            for rec in Info.search([('tow_kod', 'in', skus)]):
                by_sku[rec.tow_kod] = rec
        for link in self:
            rec = by_sku.get(link.ic_sku)
            link.ic_brand = rec.manufacturer if rec else ''
            link.ic_description = rec.short_description if rec else ''
            link.ic_tec_doc = rec.tec_doc if rec else ''

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # Manual links outrank auto ones on the shop page.
            if vals.get('source') == 'manual' and 'sequence' not in vals:
                vals['sequence'] = 5
        return super().create(vals_list)

    # ── Materialisation hook (called by the cart controller) ────────────
    @api.model
    def _record_materialisation(self, oem_template, ic_sku, product):
        """Attach a freshly-materialised product to its link.

        Upserts the (oem_template, ic_sku) pair: if a link exists its
        ``aftermarket_template_id`` is filled; otherwise a ``shop``
        link is created. Also backfills every *other* link that points
        to the same IC SKU from another OEM — the product is the same
        physical part everywhere.
        """
        if not ic_sku:
            return self.browse()
        tmpl_id = product.product_tmpl_id.id if product else False
        link = self.sudo().with_context(active_test=False).search([
            ('oem_template_id', '=', oem_template.id),
            ('ic_sku', '=', ic_sku),
        ], limit=1)
        if link:
            if tmpl_id and not link.aftermarket_template_id:
                link.write({'aftermarket_template_id': tmpl_id})
        else:
            link = self.sudo().create({
                'oem_template_id': oem_template.id,
                'ic_sku': ic_sku,
                'aftermarket_template_id': tmpl_id,
                'source': 'shop',
            })
        if tmpl_id:
            others = self.sudo().search([
                ('ic_sku', '=', ic_sku),
                ('aftermarket_template_id', '=', False),
            ])
            others.write({'aftermarket_template_id': tmpl_id})
        return link

    # ── Seed migration helper ────────────────────────────────────────────
    @api.model
    def _populate_from_seeds(self):
        """Create auto links for templates that only carry ic_seed_sku.

        Idempotent — existing pairs (active or archived) are skipped, so
        re-running never resurrects a manually-rejected match.
        """
        # Raw SQL below reads product_template directly — pending ORM
        # writes (e.g. an ic_seed_sku just set in the same transaction)
        # must hit the DB first.
        self.env.flush_all()
        cr = self.env.cr
        cr.execute(
            """
            INSERT INTO baf_oe_link
                   (oem_template_id, ic_sku, source, sequence, active)
            SELECT t.id, t.ic_seed_sku, 'auto', 10, TRUE
            FROM product_template t
            WHERE t.ic_seed_sku IS NOT NULL AND t.ic_seed_sku <> ''
              AND NOT EXISTS (
                  SELECT 1 FROM baf_oe_link l
                  WHERE l.oem_template_id = t.id
                    AND l.ic_sku = t.ic_seed_sku
              )
            """
        )
        created = cr.rowcount
        _logger.info("baf.oe.link._populate_from_seeds: %d links created",
                     created)
        # INSERT bypassed the ORM — invalidate so o2m fields on
        # product.template pick the new rows up.
        self.env.invalidate_all()
        return created

    # ── Row actions ──────────────────────────────────────────────────────
    def action_open_ic_product(self):
        self.ensure_one()
        rec = self.env['ic.product.info'].sudo().search(
            [('tow_kod', '=', self.ic_sku)], limit=1)
        if not rec:
            raise UserError(_(
                "IC SKU %s is not in the local cache — re-import the "
                "ProductInformation CSV."
            ) % self.ic_sku)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ic.product.info',
            'res_id': rec.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_open_aftermarket_product(self):
        self.ensure_one()
        if not self.aftermarket_template_id:
            raise UserError(_(
                "This IC part has not been materialised as an Odoo "
                "product yet — it will be, the first time a customer "
                "buys it."
            ))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'product.template',
            'res_id': self.aftermarket_template_id.id,
            'view_mode': 'form',
            'target': 'current',
        }


class ProductTemplateOeLinks(models.Model):
    _inherit = 'product.template'

    # As the OEM side: my aftermarket alternatives.
    oe_link_ids = fields.One2many(
        'baf.oe.link', 'oem_template_id',
        string="Aftermarket Alternatives",
    )
    oe_link_count = fields.Integer(compute='_compute_oe_link_counts')

    # As the aftermarket side: which OEM products I substitute.
    aftermarket_link_ids = fields.One2many(
        'baf.oe.link', 'aftermarket_template_id',
        string="Substitutes OEM Products",
    )
    aftermarket_link_count = fields.Integer(
        compute='_compute_oe_link_counts',
    )

    def _compute_oe_link_counts(self):
        for tmpl in self:
            tmpl.oe_link_count = len(tmpl.oe_link_ids)
            tmpl.aftermarket_link_count = len(tmpl.aftermarket_link_ids)

    def action_view_oe_links(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Aftermarket Alternatives"),
            'res_model': 'baf.oe.link',
            'view_mode': 'list,form',
            'domain': [('oem_template_id', '=', self.id)],
            'context': {'default_oem_template_id': self.id,
                        'default_source': 'manual'},
        }

    def action_view_aftermarket_links(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Substitutes OEM Products"),
            'res_model': 'baf.oe.link',
            'view_mode': 'list,form',
            'domain': [('aftermarket_template_id', '=', self.id)],
        }
