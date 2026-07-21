import gzip
import io
from datetime import date

from odoo import http
from odoo.addons.general_system_custom.models.baf_product_pricing import resolve_baf_brand_info
from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.http import content_disposition, request


def visible_brands(env, partner):
    """Brands the partner may pull a price file for: publicly available brands,
    plus his own visible brands, plus — for a child contact — the brands visible
    to his company."""
    brand_ids = set(partner.visible_brand_ids.ids)
    commercial = partner.commercial_partner_id
    if commercial and commercial != partner:
        brand_ids |= set(commercial.visible_brand_ids.ids)
    return env['product.brand'].sudo().search(
        ['|', ('is_public', '=', True), ('id', 'in', list(brand_ids))],
        order='name',
    )


_UPE = "COALESCE(pt.list_price, 0)"

# Mirrors product_template.baf_get_sales_price_details(). The brand is fixed for
# a whole download, so the customer's applicable group is resolved once in Python
# and only the per-row lookup is left to Postgres.
#
# `dl` dedupes baf_discount_line with DISTINCT ON (column_key, discount_code),
# keeping the lowest id per key pair (ORDER BY ... id). This mirrors
# baf.discount.line.get_discount_pct()'s search(...).limit(1) with Odoo's
# default id-ascending order, which is the row it picks when duplicates exist.
# Deduping first means the join to `pt` is a single hash join (one row per key
# pair) instead of a LATERAL subquery re-probed per product row, and it also
# guarantees the join can never fan a product row out into more than one
# result row, even if duplicate (table_type, column_key, discount_code) rows
# exist.
_PRICEFILE_SQL = """
    SELECT
        pt.sku AS "SKU",
        COALESCE(pt.name->>%(lang)s, pt.name->>'en_US') AS "Description",
        round((COALESCE(pt.surcharge, 0) + CASE
            WHEN %(moto_split)s AND pt.baf_mod = 'motorcycle' THEN {moto_expr}
            ELSE {car_expr}
        END)::numeric, 2) AS "Discounted Price"
    FROM product_template pt
    LEFT JOIN (
        SELECT DISTINCT ON (column_key, discount_code)
               column_key, discount_code, discount_pct
        FROM baf_discount_line
        WHERE table_type = 'sales' AND partner_id IS NULL
        ORDER BY column_key, discount_code, id
    ) dl
      ON dl.discount_code = btrim(pt.baf_discount_code)
     AND dl.column_key = pt.baf_sales_column_key || '_' || CASE
             WHEN %(moto_split)s AND pt.baf_mod = 'motorcycle' THEN %(moto_suffix)s
             ELSE %(car_suffix)s
         END
    WHERE pt.active AND pt.sale_ok AND pt.brand = %(brand)s
"""


def _price_expr(group, eu_vat, markup_param, params):
    """SQL expression for the sales price before surcharge, under `group`."""
    if eu_vat:
        return "%s * 0.95" % _UPE
    if not group:
        return _UPE
    if group.pricing_method == 'markup_pct':
        params[markup_param] = group.markup_pct or 0.0
        return "%s * (1 + %%(%s)s / 100.0)" % (_UPE, markup_param)
    return "%s * (1 - COALESCE(dl.discount_pct, 0) / 100.0)" % _UPE


def pricefile_query(partner, brand, lang):
    """Build the (sql, params) that produce the price-file rows for `brand`,
    priced exactly like product_template.baf_get_sales_price(partner)."""
    # The name-derived string family still drives the two name-based rules: the
    # BMW/MINI motorcycle split and the JLR EU-VAT tier.
    family = resolve_baf_brand_info(brand.name)[1]
    # Group-to-product matching is by brand family RECORD (baf.brand.family): a
    # group prices this product when its family_id equals the product brand's
    # family_id; a group with no family_id is the wildcard fallback. Mirrors
    # baf_get_sales_price_details after the brand-family refactor.
    product_bfam = brand.family_id
    groups = partner._baf_effective_sales_groups().filtered(lambda g: g.active)
    family_groups = groups.filtered(lambda g: product_bfam and g.family_id == product_bfam)
    wildcard = groups.filtered(lambda g: not g.family_id)[:1]

    car_group = family_groups.filtered(lambda g: not g._is_moto_group())[:1] or wildcard
    moto_group = family_groups.filtered(lambda g: g._is_moto_group())[:1] or car_group

    # Flat -5 % on JLR for EU-VAT customers, but only when no group already
    # covers this product (a family-matching or wildcard group always wins).
    eu_vat = bool(
        family == 'jlr'
        and partner.is_b2b_eu_vat
        and not groups.filtered(lambda g: not g.family_id or g.family_id == product_bfam)
    )

    params = {
        'lang': lang,
        'brand': brand.id,
        # Only BMW/MINI products have a motorcycle tier.
        'moto_split': family == 'bmw_mini',
        'car_suffix': car_group.group_column_suffix or 'GR1',
        'moto_suffix': 'MOTO',
    }
    sql = _PRICEFILE_SQL.format(
        car_expr=_price_expr(car_group, eu_vat, 'car_markup', params),
        moto_expr=_price_expr(moto_group, False, 'moto_markup', params),
    )
    return sql, params


class PriceFile(CustomerPortal):

    @http.route(['/pricefile'], type='http', auth='user', website=True)
    def pricefile_page(self, **kw):
        brands = visible_brands(request.env, request.env.user.partner_id)
        return request.render('b2b_custom.pricefile_page', {'brands': brands})

    @http.route(
        ['/pricefile/download'], type='http', auth='user', website=True,
        multilang=False, sitemap=False,
    )
    def pricefile_download(self, brand_id=None, **kw):
        partner = request.env.user.partner_id
        try:
            brand = visible_brands(request.env, partner).filtered(
                lambda b: b.id == int(brand_id)
            )
        except (TypeError, ValueError):
            brand = None
        if not brand:
            return request.redirect('/pricefile')

        lang = request.env.context.get('lang') or 'en_US'
        sql, params = pricefile_query(partner, brand, lang)

        # Only compress when the client actually advertises support for it
        # (a plain curl request sends no Accept-Encoding and must get a
        # plain .csv, not a gzip blob saved with a .csv extension). Odoo's
        # HTTPRequest wrapper only proxies a fixed attribute whitelist that
        # excludes accept_encodings, so read the raw header instead.
        gz_ok = 'gzip' in request.httprequest.headers.get('Accept-Encoding', '')

        # Raw cursor bypasses the ORM's pending-write buffer: flush so stored
        # computed fields (baf_sales_column_key, baf_brand_family) and any pending
        # write() are visible to the COPY below.
        request.env.flush_all()

        # COPY streams the CSV straight out of Postgres, so there is no Python
        # per-row loop even on a six-figure catalogue. When gzip is wanted,
        # compress on the fly as COPY writes into the GzipFile instead of
        # buffering the full plain CSV and then gzip-compressing a second
        # full copy of it.
        buf = io.BytesIO()
        raw_cur = request.env.cr._cnx.cursor()
        try:
            # JIT costs ~0.9s on this query and buys nothing. The raw cursor
            # shares the ORM's connection, so SET LOCAL stays in effect for
            # the rest of this request's transaction, not just this cursor.
            raw_cur.execute("SET LOCAL jit = off")
            select_sql = raw_cur.mogrify(sql, params).decode()
            copy_sql = "COPY (%s) TO STDOUT WITH (FORMAT CSV, HEADER true)" % select_sql
            # utf-8-sig BOM so Excel reads accented text correctly, in both
            # branches.
            if gz_ok:
                with gzip.GzipFile(fileobj=buf, mode='wb') as gz:
                    gz.write(b"\xef\xbb\xbf")
                    raw_cur.copy_expert(copy_sql, gz)
            else:
                buf.write(b"\xef\xbb\xbf")
                raw_cur.copy_expert(copy_sql, buf)
        finally:
            raw_cur.close()

        data = buf.getvalue()
        buf.close()

        filename = "PriceList_%s_%s.csv" % (brand.name or 'Brand', date.today().isoformat())
        headers = [
            ('Content-Type', 'text/csv; charset=utf-8'),
            ('Content-Length', str(len(data))),
            ('Content-Disposition', content_disposition(filename)),
        ]
        if gz_ok:
            headers.append(('Content-Encoding', 'gzip'))
        return request.make_response(data, headers=headers)
