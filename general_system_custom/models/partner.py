from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import baf_import_utils as bafutil


class ResPartner(models.Model):
    _inherit = 'res.partner'

    contact_number = fields.Char(
        string="Contact Number",
        copy=False,
        index=True,
        help="Unique sequential number assigned automatically when the contact "
             "is created. Address records (delivery/invoice/other) are skipped.",
    )

    _contact_number_uniq = models.Constraint(
        'unique(contact_number)',
        "The Contact Number must be unique.",
    )

    is_trusted_vendor = fields.Boolean(
        string="Trusted Vendor",
        help="If checked, the Customer Name column will be included in the PO Excel export sent to this vendor."
    )

    baf_is_vendor = fields.Boolean(
        string='Is a Vendor',
        help="Tick to mark this contact as a purchase vendor and show the "
             "Vendor Pricing tab (method + per-vendor pricing data).",
    )

    baf_purchase_method = fields.Selection(
        selection=[
            ('matrix', 'Matrix Table'),
            ('codes',  'Discount Codes'),
            ('direct', 'Direct Prices'),
        ],
        string='Purchase Pricing Method',
        help=(
            "How this vendor's purchase prices are resolved: "
            "matrix = 2-D discount table (code x brand/type column); "
            "codes = discount code -> %; "
            "direct = net price per SKU (product.supplierinfo)."
        ),
    )

    baf_sb_surcharge_pct = fields.Float(
        string='SB Extra Discount %',
        help="Optional. For the matrix method only: extra discount applied to "
             "products whose Mod is 'sb'. Leave 0 to disable.",
    )

    baf_purchase_line_ids = fields.One2many(
        'baf.discount.line', 'partner_id',
        string='Matrix Purchase Lines',
        domain=[('table_type', '=', 'purchase')],
    )
    baf_code_value_ids = fields.One2many(
        'discount.code.value', 'partner_id',
        string='Discount Code Values',
    )
    baf_supplierinfo_ids = fields.One2many(
        'product.supplierinfo', 'partner_id',
        string='Vendor SKU Prices',
    )

    baf_pricing_file = fields.Binary(string='Pricing File')
    baf_pricing_filename = fields.Char(string='Pricing File Name')

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

    @api.model_create_multi
    def create(self, vals_list):
        partners = super().create(vals_list)
        # Only number real contacts/companies — skip delivery/invoice/other
        # address records, and never overwrite a number set explicitly.
        new_contacts = partners.filtered(
            lambda p: p.type == 'contact' and not p.contact_number
        )
        if new_contacts:
            # Derive the next number from the current MAX in the table, same
            # pattern Odoo uses for website_sequence / pos_sequence. Deleted
            # numbers are reused and manually inserted ones are respected.
            # Flush pending contact_number writes so back-to-back create()
            # calls in one transaction (e.g. company + child contact) see
            # each other's numbers and don't collide on the UNIQUE index.
            self.env['res.partner'].flush_model(['contact_number'])
            self.env.cr.execute("""
                SELECT MAX(contact_number::bigint)
                FROM res_partner
                WHERE contact_number ~ '^[0-9]+$'
            """)
            max_number = self.env.cr.fetchone()[0]
            next_number = (max_number or 9999) + 1
            for partner in new_contacts:
                partner.contact_number = str(next_number)
                next_number += 1
        return partners

    def write(self, vals):
        # Unticking "Is a Vendor" drops this contact's saved per-vendor pricing
        # (matrix rows, discount code values, direct prices) and its method/file.
        clearing = self.filtered('baf_is_vendor') if vals.get('baf_is_vendor') is False else self.browse()
        res = super().write(vals)
        if clearing:
            clearing._baf_wipe_vendor_pricing()
            super(ResPartner, clearing).write({
                'baf_purchase_method': False,
                'baf_pricing_file': False,
                'baf_pricing_filename': False,
            })
        return res

    def _baf_wipe_vendor_pricing(self):
        """Delete this contact's per-vendor pricing across all three stores."""
        self.baf_purchase_line_ids.unlink()
        self.baf_code_value_ids.unlink()
        self.baf_supplierinfo_ids.unlink()

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

    # ── Per-vendor pricing file import (inline on the Vendor Pricing tab) ──────

    @api.onchange('baf_purchase_method')
    def _onchange_baf_purchase_method_reset_file(self):
        # Changing the method clears any staged file; the user re-uploads.
        self.baf_pricing_file = False
        self.baf_pricing_filename = False

    def action_import_vendor_pricing_file(self):
        """Parse the uploaded file for this vendor using its chosen pricing
        method and REPLACE this vendor's pricing data with only what the file
        contains (all of this vendor's prior per-vendor data is wiped first)."""
        self.ensure_one()
        if not self.baf_purchase_method:
            raise UserError(_("Set a Purchase Pricing Method first."))
        if not self.baf_pricing_file:
            raise UserError(_("Upload a file first."))
        try:
            sheets = bafutil.read_workbook(self.baf_pricing_filename or '', self.baf_pricing_file)
        except Exception as e:
            raise UserError(_(
                "Could not read the file. Ensure it is a valid CSV or Excel file. "
                "Details: %s") % e)
        rows = bafutil.first_sheet(sheets)

        # Full replace: drop this vendor's existing per-vendor pricing across
        # all three stores (so switching method leaves nothing stale), then
        # load only what the uploaded file contains.
        self._baf_wipe_vendor_pricing()

        importer = {
            'matrix': self._baf_import_matrix,
            'codes':  self._baf_import_codes,
            'direct': self._baf_import_direct,
        }[self.baf_purchase_method]
        created, updated, warnings = importer(rows)

        # One-shot upload field: clear it once processed.
        self.baf_pricing_file = False
        self.baf_pricing_filename = False

        message = _("%(c)d created, %(u)d updated.") % {'c': created, 'u': updated}
        if warnings:
            message += "\n" + _(
                "Skipped %(n)d ambiguous SKU(s) (add a Brand column to disambiguate): %(skus)s"
            ) % {'n': len(warnings), 'skus': ", ".join(warnings)}
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Vendor Pricing Imported"),
                'message': message,
                'type': 'warning' if warnings else 'success',
                'sticky': bool(warnings),
                # Refresh the current form (cleared file + refreshed data lists)
                # via a lightweight soft reload instead of reopening the whole
                # heavy contact form with a new act_window.
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    # Canonical column headers per method (single accepted name per column).
    _BAF_TEMPLATE_ROWS = {
        'direct': [['sku', 'discounted price', 'brand']],
        'codes':  [['dc', 'discount in %']],
        'matrix': [
            ['#', 'BMW TA 1-2-4-6-8', 'BMW TA 3-5-7-9',
             'MINI TA 1-2-4-6-8', 'MINI TA 3-5-7-9', 'MOTO'],
            ['RG', 'Discount in %', 'Discount in %',
             'Discount in %', 'Discount in %', 'Discount in %'],
        ],
    }

    def action_download_pricing_template(self):
        """Download the xlsx column template for the selected pricing method."""
        self.ensure_one()
        if not self.baf_purchase_method:
            raise UserError(_("Set a Purchase Pricing Method first."))
        import base64
        import io
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Template'
        for row in self._BAF_TEMPLATE_ROWS[self.baf_purchase_method]:
            ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        attachment = self.env['ir.attachment'].create({
            'name': 'vendor_%s_template.xlsx' % self.baf_purchase_method,
            'datas': base64.b64encode(buf.getvalue()),
            'res_model': 'res.partner',
            'res_id': self.id,
            'mimetype': 'application/vnd.openxmlformats-officedocument'
                        '.spreadsheetml.sheet',
        })
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%d?download=true' % attachment.id,
            'target': 'self',
        }

    def _baf_import_matrix(self, rows):
        """Matrix (BMW/MINI/MOTO): header row = DC, <col>, <col>...; each data
        row = code, pct, pct... Upserts purchase baf.discount.line for this
        vendor with canonical column keys. Returns (created, updated, warnings)."""
        Disc = self.env['baf.discount.line']
        created = updated = 0
        if not rows:
            return 0, 0, []
        header = [bafutil.clean_cell(c).strip() for c in rows[0]]
        col_keys = {idx: bafutil.normalize_matrix_header(k)
                    for idx, k in enumerate(header) if idx >= 1 and k}
        for row in rows[1:]:
            if not row:
                continue
            code = bafutil.clean_cell(row[0]).strip()
            # Skip the code column header and the template's second header row
            # ("RG | Discount in % | ...").
            if not code or code.upper() in ('DC', 'RG', '#'):
                continue
            for idx, column_key in col_keys.items():
                if idx >= len(row):
                    continue
                pct = bafutil.parse_float(row[idx])
                if pct is None:
                    continue
                existing = Disc.search([
                    ('partner_id', '=', self.id),
                    ('table_type', '=', 'purchase'),
                    ('column_key', '=', column_key),
                    ('discount_code', '=', code),
                ], limit=1)
                if existing:
                    if existing.discount_pct != pct:
                        existing.discount_pct = pct
                    updated += 1
                else:
                    Disc.create({
                        'partner_id': self.id, 'table_type': 'purchase',
                        'column_key': column_key, 'discount_code': code,
                        'discount_pct': pct,
                    })
                    created += 1
        return created, updated, []

    def _baf_import_codes(self, rows):
        """Discount codes (brandless): rows = code, percentage. Upserts
        discount.code (by name) + discount.code.value for this vendor."""
        Code = self.env['discount.code']
        Value = self.env['discount.code.value']
        created = updated = 0
        for row in rows:
            if not row:
                continue
            name = bafutil.clean_cell(row[0]).strip()
            if not name or name.upper() in ('CODE', 'DC'):
                continue
            pct = bafutil.parse_float(row[1]) if len(row) > 1 else None
            if pct is None:
                continue
            code = Code.search([('name', '=', name)], limit=1) or Code.create({'name': name})
            val = Value.search([
                ('code_id', '=', code.id), ('partner_id', '=', self.id)], limit=1)
            if val:
                if val.percentage != pct:
                    val.percentage = pct
                updated += 1
            else:
                Value.create({
                    'code_id': code.id, 'partner_id': self.id, 'percentage': pct})
                created += 1
        return created, updated, []

    def _baf_import_direct(self, rows):
        """Direct prices: columns SKU, PRICE and an OPTIONAL BRAND (named header).
        Match products by sku (+ brand when given). A SKU that matches several
        products without a brand to disambiguate is skipped and reported.
        Falls back to positional [sku, price] when no recognizable header."""
        Tmpl = self.env['product.template']
        Seller = self.env['product.supplierinfo']
        created = updated = 0
        warnings = []
        if not rows:
            return 0, 0, []

        header = [bafutil.clean_cell(c).strip().lower() for c in rows[0]]

        def _find(names):
            for i, h in enumerate(header):
                if h in names:
                    return i
            return None

        sku_idx = _find({'sku'})
        price_idx = _find({'discounted price'})
        if sku_idx is not None and price_idx is not None:
            brand_idx = _find({'brand'})
            data_rows = rows[1:]
        else:
            # No recognizable header -> positional [sku, price] (no brand).
            sku_idx, price_idx, brand_idx = 0, 1, None
            data_rows = rows

        for row in data_rows:
            if not row:
                continue
            sku = bafutil.clean_cell(row[sku_idx]).strip() if sku_idx < len(row) else ''
            if not sku or sku.lower() == 'sku':
                continue
            price = bafutil.parse_float(row[price_idx]) if price_idx < len(row) else None
            if price is None:
                continue
            domain = [('sku', '=', sku)]
            if brand_idx is not None and brand_idx < len(row):
                brand_name = bafutil.clean_cell(row[brand_idx]).strip()
                if brand_name:
                    domain.append(('brand.name', '=', brand_name))
            products = Tmpl.search(domain)
            if not products:
                continue
            if len(products) > 1:
                # Ambiguous even after any brand filter -> skip and report.
                warnings.append(sku)
                continue
            product = products
            seller = Seller.search([
                ('partner_id', '=', self.id),
                ('product_tmpl_id', '=', product.id)], limit=1)
            if seller:
                if seller.price != price:
                    seller.price = price
                updated += 1
            else:
                Seller.create({
                    'partner_id': self.id, 'product_tmpl_id': product.id,
                    'price': price})
                created += 1
        return created, updated, sorted(set(warnings))

    @api.constrains('sales_group_ids')
    def _check_sales_group_ids_unique_family(self):
        family_labels = dict(self.env['baf.sales.group']._fields['brand_family'].selection)
        for partner in self:
            groups_by_key = {}
            for group in partner.sales_group_ids:
                key = (group.brand_family, group._is_moto_group())
                bucket = groups_by_key.setdefault(key, self.env['baf.sales.group'])
                groups_by_key[key] = bucket | group

            duplicates = {
                key: groups
                for key, groups in groups_by_key.items()
                if len(groups) > 1
            }
            if duplicates:
                tier_label_moto = _("motorcycle")
                tier_label_car = _("car")
                detail_tpl = _("%(family)s (%(tier)s): %(groups)s")
                details = '; '.join(
                    detail_tpl % {
                        'family': family_labels.get(family, family),
                        'tier': tier_label_moto if is_moto else tier_label_car,
                        'groups': ', '.join(groups.mapped('name')),
                    }
                    for (family, is_moto), groups in duplicates.items()
                )
                raise ValidationError(_(
                    "A customer can only belong to one car group + one motorcycle group per brand family. "
                    "Conflicts found: %(details)s"
                ) % {'details': details})
