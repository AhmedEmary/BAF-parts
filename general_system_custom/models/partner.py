import itertools
import logging
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import baf_import_utils as bafutil

_logger = logging.getLogger(__name__)


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

    baf_direct_sale_markup_pct = fields.Float(
        string='Sales Markup % (Direct)',
        help="For the 'direct' method only: extra percentage added on top of "
             "this vendor's direct purchase price to obtain the sales price. "
             "Must be >= 0.",
    )

    @api.constrains('baf_direct_sale_markup_pct')
    def _check_baf_direct_sale_markup_pct(self):
        for partner in self:
            if partner.baf_direct_sale_markup_pct < 0:
                raise ValidationError(_("Sales Markup % (Direct) cannot be negative."))

    baf_delivery_weeks = fields.Integer(
        string='Delivery Time Frame',
        help="Lower bound N of the delivery time frame, entered as a single "
             "number and read as an 'N to N+1 weeks' range (e.g. 2 -> '2-3 "
             "weeks'). Drives auto-vendor selection: the vendor with the "
             "shortest delivery wins, ties break by price. Leave 0/empty to "
             "rank this vendor as slowest.",
    )

    baf_max_delivery_weeks = fields.Integer(
        string='Max Delivery Weeks (Customer)',
        help="Customer-side cap on the delivery frame of the supplier auto-"
             "selection: only vendors whose Delivery Time Frame is between 1 "
             "and this value are considered for this customer's orders. "
             "Empty / 0 = no cap (default behaviour, every vendor is "
             "eligible and the fastest wins). See task #51.",
    )

    @api.constrains('baf_max_delivery_weeks')
    def _check_baf_max_delivery_weeks(self):
        for partner in self:
            if partner.baf_max_delivery_weeks < 0:
                raise ValidationError(_(
                    "Max Delivery Weeks (Customer) cannot be negative."))

    def _baf_customer_delivery_cap(self):
        """The delivery-weeks cap this partner (or its commercial parent)
        imposes on the vendor selection. Falls back to the commercial partner
        when a child contact has none set, mirroring
        `_baf_effective_sales_groups`. Returns 0 for "no cap"."""
        self.ensure_one()
        if self.baf_max_delivery_weeks:
            return self.baf_max_delivery_weeks
        company = self.commercial_partner_id
        if company and company != self and company.baf_max_delivery_weeks:
            return company.baf_max_delivery_weeks
        return 0

    baf_delivery_weeks_upper = fields.Integer(
        string='Delivery Upper Bound',
        compute='_compute_baf_delivery_weeks_upper',
        help="Auto-calculated upper bound (N+1) of the delivery time frame.",
    )

    @api.depends('baf_delivery_weeks')
    def _compute_baf_delivery_weeks_upper(self):
        for partner in self:
            weeks = partner.baf_delivery_weeks
            partner.baf_delivery_weeks_upper = weeks + 1 if weeks and weeks > 0 else 0

    @api.constrains('baf_delivery_weeks')
    def _check_baf_delivery_weeks(self):
        # 0/empty means "no delivery time frame" (ranked slowest). The smallest
        # real frame is 1 -> "1-2 weeks"; negatives are meaningless.
        for partner in self:
            if partner.baf_delivery_weeks < 0:
                raise ValidationError(_("Delivery Time Frame cannot be negative."))

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

    def _baf_effective_sales_groups(self):
        """Sales groups that price this partner. A child contact with its own
        groups uses them (the company's are ignored); a child with none
        inherits its parent company's groups."""
        self.ensure_one()
        if self.sales_group_ids:
            return self.sales_group_ids
        company = self.commercial_partner_id
        if company and company != self:
            return company.sales_group_ids
        return self.sales_group_ids

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
        untick = 'baf_is_vendor' in vals and not vals['baf_is_vendor']
        clearing = self.filtered('baf_is_vendor') if untick else self.browse()
        res = super().write(vals)
        if clearing:
            clearing._baf_wipe_vendor_pricing()
            super(ResPartner, clearing).write({
                'baf_purchase_method': False,
                'baf_pricing_file': False,
                'baf_pricing_filename': False,
                'baf_delivery_weeks': 0,
                'baf_direct_sale_markup_pct': 0.0,
                'baf_sb_surcharge_pct': 0.0,
            })
        return res

    def _baf_wipe_vendor_pricing(self):
        """Delete this contact's per-vendor pricing across all three stores."""
        if not self.ids:
            return
        self.baf_purchase_line_ids.unlink()
        self.baf_code_value_ids.unlink()
        # Direct prices reach hundreds of thousands of rows per vendor; ORM
        # unlink would load and cascade every one of them.
        Seller = self.env['product.supplierinfo']
        Seller.flush_model()
        self.env.cr.execute(
            "DELETE FROM product_supplierinfo WHERE partner_id IN %s",
            (tuple(self.ids),))
        Seller.invalidate_model()

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
        contains (all of this vendor's prior per-vendor data is wiped first).

        Importers return a stats dict: created/updated, plus (direct only)
        ambiguous/unmatched/invalid for the rows that were dropped."""
        self.ensure_one()
        if not self.baf_purchase_method:
            raise UserError(_("Set a Purchase Pricing Method first."))
        if not self.baf_pricing_file:
            raise UserError(_("Upload a file first."))
        try:
            if self.baf_purchase_method == 'direct':
                # Direct price lists run to hundreds of thousands of rows: pull
                # them row by row instead of holding the whole sheet in memory.
                rows = bafutil.stream_first_sheet(
                    self.baf_pricing_filename or '', self.baf_pricing_file)
            else:
                rows = bafutil.first_sheet(bafutil.read_workbook(
                    self.baf_pricing_filename or '', self.baf_pricing_file))
        except Exception as e:
            raise UserError(_(
                "Could not read the file. Ensure it is a valid CSV or Excel file. "
                "Details: %s") % e)

        # Full replace: drop this vendor's existing per-vendor pricing across
        # all three stores (so switching method leaves nothing stale), then
        # load only what the uploaded file contains.
        self._baf_wipe_vendor_pricing()

        importer = {
            'matrix': self._baf_import_matrix,
            'codes':  self._baf_import_codes,
            'direct': self._baf_import_direct,
        }[self.baf_purchase_method]
        stats = importer(rows)

        # One-shot upload field: clear it once processed.
        self.baf_pricing_file = False
        self.baf_pricing_filename = False

        skipped = bool(stats.get('ambiguous') or stats.get('unmatched')
                       or stats.get('invalid'))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Vendor Pricing Imported"),
                'message': self._baf_import_message(stats),
                'type': 'warning' if skipped else 'success',
                'sticky': skipped,
                # Refresh the current form (cleared file + refreshed data lists)
                # via a lightweight soft reload instead of reopening the whole
                # heavy contact form with a new act_window.
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _baf_import_message(self, stats):
        """One line per outcome, so every row of the uploaded file is accounted
        for: what landed, and what was dropped and why."""
        lines = [_("%(c)d created, %(u)d updated.") % {
            'c': stats.get('created', 0), 'u': stats.get('updated', 0)}]
        if stats.get('unmatched'):
            lines.append(_(
                "%d row(s) skipped: SKU not found in the catalogue."
            ) % stats['unmatched'])
        if stats.get('invalid'):
            lines.append(_(
                "%d row(s) skipped: empty SKU or unreadable price."
            ) % stats['invalid'])
        ambiguous = stats.get('ambiguous') or []
        if ambiguous:
            lines.append(_(
                "%d SKU(s) skipped: the same SKU exists under several brands "
                "in the catalogue, so the file row cannot be assigned to one product."
            ) % len(ambiguous))
        return "\n".join(lines)

    # Canonical column headers per method (single accepted name per column).
    # The direct template intentionally has no Brand column: the brand is
    # assigned automatically from the internal catalogue (task #53) and any
    # brand value in the file is ignored.
    _BAF_TEMPLATE_ROWS = {
        'direct': [['sku', 'discounted price']],
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
        vendor with canonical column keys."""
        Disc = self.env['baf.discount.line']
        created = updated = 0
        if not rows:
            return {'created': 0, 'updated': 0}
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
        return {'created': created, 'updated': updated}

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
        return {'created': created, 'updated': updated}

    # Chunk size for the streaming COPY into pg_temp. Purely a memory knob —
    # 20k rows is ~1 MB of CSV, and Postgres consumes each chunk before we
    # allocate the next one.
    _BAF_DIRECT_COPY_CHUNK = 20_000

    def _baf_import_direct(self, rows):
        """Direct prices: columns SKU, DISCOUNTED PRICE. Only products whose
        SKU already exists in the catalogue are imported; the brand of the
        matched row comes from the internal database. Any BRAND column in the
        file is intentionally ignored (task #53): brand assignment is
        internal-only, so a wrong or unknown brand in the source must never
        create products or change matching. Falls back to positional
        [sku, price] when no recognizable header.

        SKU matching is case-insensitive: a file lowercased against uppercase
        catalogue rows still matches. The exact match runs first as a single
        indexed JOIN; anything not resolved that way falls back to a
        case-insensitive JOIN over just the residual SKUs.

        Rows are streamed straight into a `pg_temp` table via COPY and the
        entire import happens as three SQL statements (ambiguous scan, exact-
        match INSERT, residual case-insensitive INSERT). That keeps peak
        memory at O(chunk) even for six-figure files and avoids the ORM
        overhead — no per-batch `invalidate_model`, no per-batch ORM read —
        that made the previous implementation take hours on ~500k-row lists.

        Every data row ends up in exactly one counter (created / unmatched /
        invalid / ambiguous), so the file always adds up."""
        rows = iter(rows)
        first_row = next(rows, None)
        if first_row is None:
            return {'created': 0, 'updated': 0}

        header = [bafutil.clean_cell(c).strip().lower() for c in first_row]

        def _find(names):
            for i, h in enumerate(header):
                if h in names:
                    return i
            return None

        sku_idx = _find({'sku'})
        price_idx = _find({'discounted price'})
        if sku_idx is None or price_idx is None:
            # No recognizable header -> positional [sku, price] and the row
            # just consumed is data. A stray Brand column, if any, will be
            # ignored (its index isn't looked at).
            sku_idx, price_idx = 0, 1
            rows = itertools.chain([first_row], rows)

        stats = {'created': 0, 'updated': 0, 'unmatched': 0, 'invalid': 0,
                 'ambiguous': []}
        cr = self.env.cr
        t0 = time.monotonic()

        # 1) Stage the file into pg_temp. ON COMMIT DROP means it dies with the
        # request's transaction and there's nothing to clean up on error.
        cr.execute("""
            CREATE TEMP TABLE baf_direct_import (
                sku_raw   TEXT NOT NULL,
                sku_upper TEXT NOT NULL,
                price     NUMERIC NOT NULL
            ) ON COMMIT DROP
        """)
        file_row_count = self._baf_copy_direct_rows_to_temp(
            rows, sku_idx, price_idx, stats)
        _logger.info(
            "Direct import for vendor %s: staged %d row(s) in %.2fs",
            self.id, file_row_count, time.monotonic() - t0,
        )
        if not file_row_count:
            return stats

        # 2) SKUs that appear under multiple product templates: unresolvable
        # from the file alone. Report and skip. `product_template.sku` is
        # indexed so this is an index-only scan.
        cr.execute("""
            SELECT DISTINCT f.sku_raw
            FROM baf_direct_import f
            WHERE f.sku_raw IN (
                SELECT sku FROM product_template
                WHERE sku IS NOT NULL
                GROUP BY sku
                HAVING COUNT(*) > 1
            )
            ORDER BY f.sku_raw
        """)
        stats['ambiguous'] = [r[0] for r in cr.fetchall()]

        # 3) Exact-match INSERT. A btree hit on product_template.sku is the
        # fast path for six-figure imports — no per-row Python.
        company = self.env.company
        params = {
            'partner': self.id,
            'currency': company.currency_id.id,
            'company': company.id,
            'uid': self.env.uid,
        }
        cr.execute("""
            WITH deduped AS (
                -- Two rows for the same SKU: last one wins. `ctid DESC` gives
                -- us the physically-latest row, which is the last one COPY'd
                -- and matches "later rows overwrite earlier" from the old code.
                SELECT DISTINCT ON (sku_raw) sku_raw, price
                FROM baf_direct_import
                ORDER BY sku_raw, ctid DESC
            ),
            ambiguous AS (
                SELECT sku FROM product_template
                WHERE sku IS NOT NULL
                GROUP BY sku HAVING COUNT(*) > 1
            )
            INSERT INTO product_supplierinfo (
                partner_id, product_tmpl_id, price, product_uom_id, currency_id,
                company_id, min_qty, discount, delay, sequence,
                create_uid, create_date, write_uid, write_date)
            SELECT
                %(partner)s, pt.id, d.price, pt.uom_id,
                %(currency)s, %(company)s, 0.0, 0.0, 1, 1,
                %(uid)s, NOW(), %(uid)s, NOW()
            FROM deduped d
            JOIN product_template pt ON pt.sku = d.sku_raw
            WHERE d.sku_raw NOT IN (SELECT sku FROM ambiguous)
        """, params)
        exact_matches = cr.rowcount

        # 4) Case-insensitive fallback for the file rows that didn't match by
        # exact SKU. Skipped when there are no residuals (the normal case
        # for imports produced by the pricefile export, which preserves
        # casing). When residuals exist, we still run a single query rather
        # than one per SKU.
        cr.execute("""
            SELECT COUNT(DISTINCT f.sku_upper)
            FROM baf_direct_import f
            WHERE NOT EXISTS (
                SELECT 1 FROM product_template pt WHERE pt.sku = f.sku_raw
            )
        """)
        residual_upper_skus = cr.fetchone()[0]
        residual_matches = 0
        if residual_upper_skus:
            cr.execute("""
                WITH residuals AS (
                    SELECT DISTINCT ON (sku_upper) sku_upper, price
                    FROM baf_direct_import f
                    WHERE NOT EXISTS (
                        SELECT 1 FROM product_template pt
                        WHERE pt.sku = f.sku_raw
                    )
                    ORDER BY sku_upper, ctid DESC
                ),
                ambiguous_upper AS (
                    SELECT UPPER(sku) AS su FROM product_template
                    WHERE sku IS NOT NULL
                    GROUP BY UPPER(sku) HAVING COUNT(*) > 1
                )
                INSERT INTO product_supplierinfo (
                    partner_id, product_tmpl_id, price, product_uom_id,
                    currency_id, company_id, min_qty, discount, delay,
                    sequence, create_uid, create_date, write_uid, write_date)
                SELECT
                    %(partner)s, pt.id, r.price, pt.uom_id,
                    %(currency)s, %(company)s, 0.0, 0.0, 1, 1,
                    %(uid)s, NOW(), %(uid)s, NOW()
                FROM residuals r
                JOIN product_template pt ON UPPER(pt.sku) = r.sku_upper
                WHERE r.sku_upper NOT IN (SELECT su FROM ambiguous_upper)
                  AND NOT EXISTS (
                    SELECT 1 FROM product_supplierinfo si
                    WHERE si.partner_id = %(partner)s
                      AND si.product_tmpl_id = pt.id
                  )
            """, params)
            residual_matches = cr.rowcount

        stats['created'] = exact_matches + residual_matches
        # Unmatched = file rows whose SKU is not in the catalogue at all,
        # excluding those already accounted for as ambiguous. Computed from
        # counts to avoid another scan.
        stats['unmatched'] = max(
            0, file_row_count - stats['created'] - len(stats['ambiguous']))

        # One invalidate at the end covers every row inserted above. The old
        # per-batch invalidate is what made the previous version take hours
        # on large files.
        self.env['product.supplierinfo'].invalidate_model()
        _logger.info(
            "Direct import for vendor %s done in %.2fs — "
            "created=%d exact=%d fallback=%d ambiguous=%d unmatched=%d invalid=%d",
            self.id, time.monotonic() - t0, stats['created'], exact_matches,
            residual_matches, len(stats['ambiguous']), stats['unmatched'],
            stats['invalid'],
        )
        return stats

    def _baf_copy_direct_rows_to_temp(self, rows, sku_idx, price_idx, stats):
        """Stream (sku, upper(sku), price) triples into `baf_direct_import`
        via `COPY ... FROM STDIN`. Returns the number of rows staged.

        Uses COPY TEXT format (tab-separated, backslash escapes) so we can
        write a tight `_escape` helper without pulling in a CSV writer. Empty
        SKUs and unparseable prices are counted in `stats` and dropped."""
        cr = self.env.cr
        row_count = 0
        chunk_size = self._BAF_DIRECT_COPY_CHUNK
        parts = []

        def _escape(cell):
            # COPY TEXT format: literal tabs / newlines / backslashes must be
            # backslash-escaped, otherwise Postgres reads them as separators.
            return (cell.replace('\\', '\\\\')
                        .replace('\t', '\\t')
                        .replace('\n', '\\n')
                        .replace('\r', '\\r'))

        def _flush():
            if not parts:
                return
            payload = ''.join(parts).encode('utf-8')
            parts.clear()
            import io as _io
            cr.copy_expert(
                "COPY baf_direct_import (sku_raw, sku_upper, price) FROM STDIN",
                _io.BytesIO(payload),
            )

        for row in rows:
            if not row:
                continue
            sku = bafutil.clean_cell(row[sku_idx]).strip() if sku_idx < len(row) else ''
            if not sku or sku.lower() == 'sku':
                continue
            price = bafutil.parse_float(row[price_idx]) if price_idx < len(row) else None
            if price is None:
                stats['invalid'] += 1
                continue
            parts.append('%s\t%s\t%s\n' % (
                _escape(sku), _escape(sku.upper()), float(price),
            ))
            row_count += 1
            if len(parts) >= chunk_size:
                _flush()
        _flush()
        return row_count

    @api.constrains('sales_group_ids')
    def _check_sales_group_ids_unique_family(self):
        for partner in self:
            # Two groups clash when they price the same family at the same tier
            # (see baf.sales.group._baf_prices_same_family). Groups of different
            # families don't clash, so a JLR group + a Mercedes group is allowed.
            clashes = []
            groups = list(partner.sales_group_ids)
            for i, group in enumerate(groups):
                for peer in groups[i + 1:]:
                    if (group._is_moto_group() == peer._is_moto_group()
                            and group._baf_prices_same_family(peer)):
                        clashes.append((group, peer))

            if clashes:
                tier_label_moto = _("motorcycle")
                tier_label_car = _("car")
                detail_tpl = _("%(scope)s (%(tier)s): %(groups)s")
                details = '; '.join(
                    detail_tpl % {
                        'scope': group._baf_scope_label(),
                        'tier': tier_label_moto if group._is_moto_group() else tier_label_car,
                        'groups': ', '.join([group.name, peer.name]),
                    }
                    for group, peer in clashes
                )
                raise ValidationError(_(
                    "A customer can only belong to one car group + one motorcycle group per family. "
                    "Conflicts found: %(details)s"
                ) % {'details': details})
