"""OEM ↔ IC seed mapping + live equivalents resolver.

BAF's catalog is OEM-only. Each OEM template can optionally carry a
handle into IC's catalog — an IC ``sku``, ``index``, or ``categoryId`` —
which is what we use to look up the aftermarket equivalents at page
render time.

The resolver returns a small list of card dicts (brand, price, avail,
etc.) sorted OEM-first then by price. It caches the result via
``ic.article.cache`` so a page view doesn't repeatedly hit IC.
"""

import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

_MAX_CARDS = 24


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # ── OEM → IC seed mapping ────────────────────────────────────────────
    # One of these is enough to seed the equivalence search; when the
    # user filled none we render an empty block (not an error).
    ic_seed_sku = fields.Char(
        string="IC Seed SKU",
        help="An Inter Cars SKU that identifies the same 'part family' "
             "as this OEM product. Used to fetch the genericArticleId "
             "and enumerate aftermarket alternatives.",
    )
    ic_seed_index = fields.Char(
        string="IC Seed Index",
        help="Alternative to IC Seed SKU: an IC ``index`` value.",
    )
    ic_seed_category_id = fields.Char(
        string="IC Category ID",
        help="Fallback: enumerate a whole IC category as candidates.",
    )

    # ── Live equivalents resolver ────────────────────────────────────────
    def _baf_ic_backend(self):
        return self.env['ic.backend'].sudo()._get_default()

    def _baf_website_enable_aftermarket(self):
        website = self.env['website'].sudo().get_current_website()
        if not website or not website.enable_aftermarket_search:
            return False
        # Optional per-category kill-switch — if any category on the
        # template has the flag OFF, treat that as an explicit opt-out.
        for cat in self.public_categ_ids:
            if not cat.enable_aftermarket_search:
                return False
        return True

    def baf_ic_equivalents(self):
        """Return a list of card dicts to render on the product page.

        The list is capped at :data:`_MAX_CARDS` items, sorted OEM-first
        then by price ascending. An empty list is a valid — never
        error — response; the template shows nothing when there are no
        cards to render.
        """
        self.ensure_one()
        if not self._baf_website_enable_aftermarket():
            return []
        backend = self._baf_ic_backend()
        if not backend:
            return []
        # Auto-discovery: if the shop admin hasn't filled a manual seed,
        # try the OEM template's own identifiers as IC lookups. This
        # covers the common case where the OEM number is one of IC's
        # indexes / referenced SKUs. We cap the fallback at three
        # cheap catalog probes; misses are cached along with hits.
        if not (self.ic_seed_sku or self.ic_seed_index
                or self.ic_seed_category_id):
            _logger.info(
                "IC equivalents: no manual seed for template %s — "
                "falling back to SKU/barcode auto-discovery.", self.id,
            )

        language = backend.default_language or 'de'
        cache_key = self._baf_ic_cache_key(language)
        Cache = self.env['ic.article.cache'].sudo()
        cached = Cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            cards = self._baf_ic_resolve_equivalents(backend, language)
        except Exception as exc:  # noqa: BLE001 — never blow up the shop
            _logger.warning(
                "IC equivalents lookup failed for template %s: %s",
                self.id, exc,
            )
            return []

        Cache.put(cache_key, cards)
        return cards

    def _baf_ic_cache_key(self, language):
        self.ensure_one()
        # Curated links are the highest-priority source, so their SKUs
        # are part of the key — adding/archiving a link invalidates the
        # cached cards immediately instead of waiting out the TTL.
        link_part = ','.join(sorted(
            self.oe_link_ids.filtered('active').mapped('ic_sku'),
        ))
        seed = (
            self.ic_seed_sku
            or self.ic_seed_index
            or (f"cat:{self.ic_seed_category_id}"
                if self.ic_seed_category_id else '')
            # Fold auto-discovery inputs into the key so a barcode edit
            # invalidates the previous result — otherwise stale cards
            # would linger until the TTL expires.
            or f"auto:{self.sku or ''}:{self.barcode or ''}"
              f":{self.default_code or ''}"
        )
        return (f"tmpl:{self.id}|links:{link_part}|seed:{seed}"
                f"|lang:{language}")

    def _baf_ic_resolve_equivalents(self, backend, language):
        """Resolve aftermarket equivalents.

        Preferred path: hit the local ``ic.product.info`` cache — one
        indexed SQL query returns everything IC ships that shares the
        BAF SKU's ``tec_doc``. That's the equivalents set, and it
        needs no live catalog call. We then still call the live
        pricing + stock endpoints for real-time numbers.

        Fallback: if the local cache is empty (import not yet done) or
        the SKU isn't in it, we fall through to the original
        catalog-based resolver below.
        """
        self.ensure_one()
        cards = self._baf_ic_resolve_from_local_cache(backend)
        if cards is not None:
            return cards
        return self._baf_ic_resolve_from_live_catalog(backend, language)

    def _baf_ic_resolve_from_local_cache(self, backend):
        """Return equivalent cards from ic.product.info, or None to fall back."""
        self.ensure_one()
        Info = self.env['ic.product.info'].sudo()

        rows = []
        seen_sku = set()

        # 1st priority: curated links (manual first — that's their
        # sequence default). A link row is authoritative: it exists
        # because a person or the auto-map decided this exact IC part
        # substitutes this exact OEM part.
        links = self.oe_link_ids.filtered('active').sorted(
            key=lambda l: (l.sequence, l.id),
        )
        if links:
            link_rows = Info.search([
                ('tow_kod', 'in', [l.ic_sku for l in links]),
            ])
            by_sku = {r.tow_kod: r for r in link_rows}
            keys_fields = [
                'tow_kod', 'ic_index', 'tec_doc', 'tec_doc_prod',
                'article_number', 'manufacturer', 'short_description',
                'description', 'barcodes', 'package_weight',
                'package_length', 'package_width', 'package_height',
            ]
            for link in links:
                rec = by_sku.get(link.ic_sku)
                if not rec or link.ic_sku in seen_sku:
                    continue
                seen_sku.add(link.ic_sku)
                rows.append({f: getattr(rec, f) or '' for f in keys_fields})

        # 2nd priority: identifier-based discovery over the local cache.
        if not rows:
            # Cache empty → no point querying; fall back to live catalog.
            if not Info.search_count([], limit=1):
                return None
            keys = [x for x in (
                self.ic_seed_sku, self.sku, self.default_code,
            ) if x]
            for key in keys:
                for row in Info.find_equivalents(key, limit=_MAX_CARDS * 2):
                    if row['tow_kod'] in seen_sku:
                        continue
                    seen_sku.add(row['tow_kod'])
                    rows.append(row)
                if rows:
                    break
        # Never offer a product as an alternative to itself — relevant
        # on the page of a materialised aftermarket product, whose own
        # IC SKU comes straight back from the identifier search.
        if self.ic_sku:
            rows = [r for r in rows if r['tow_kod'] != self.ic_sku]
        if not rows:
            return None

        # Prices + availability come from the live API — those change
        # too often to cache in the CSV.
        skus = [r['tow_kod'] for r in rows][:_MAX_CARDS * 2]
        prices_by_sku, availability_by_sku = \
            self._baf_ic_fetch_live_price_and_stock(backend, skus)

        partner = self.env.user.partner_id
        stock_cap = backend.stock_cap or 10
        cards = []
        for r in rows:
            sku = r['tow_kod']
            cost = prices_by_sku.get(sku)
            if cost is None:
                continue
            sale_price = self._baf_ic_customer_price(
                float(cost.get('customerPriceNet') or 0.0),
            )
            availability = availability_by_sku.get(sku, 0)
            cards.append({
                'ic_sku': sku,
                'ic_index': r.get('ic_index') or '',
                'ic_generic_article_id': r.get('tec_doc') or '',
                'brand': r.get('manufacturer') or _("Aftermarket"),
                'article_number': r.get('article_number') or '',
                'short_description': (
                    r.get('short_description') or r.get('description') or ''
                ),
                'availability': availability,
                'availability_label': self._baf_ic_availability_label(
                    availability, stock_cap,
                ),
                'quality': 'aftermarket',
                'is_oem': False,
                'sale_price': sale_price,
                'currency_code': (cost.get('currencyCode') or 'EUR'),
            })

        oem_card = self._baf_ic_oem_card(partner)
        cards = ([oem_card] if oem_card else []) + sorted(
            cards, key=lambda c: c['sale_price'],
        )
        return cards[:_MAX_CARDS]

    def _baf_ic_fetch_live_price_and_stock(self, backend, skus):
        """Call /ic/pricing/quote and /ic/inventory/stock for a set of SKUs."""
        client = backend.get_client()
        prices_by_sku, availability_by_sku = {}, {}
        if not skus:
            return prices_by_sku, availability_by_sku
        try:
            price_res = client.get_price(
                lines=[{'sku': s, 'quantity': 1} for s in skus],
                ship_to=backend.ship_to or None,
            )
            for line in (price_res.get('lines') or []):
                if line.get('sku') and line.get('price'):
                    prices_by_sku[line['sku']] = line['price']
        except Exception as exc:  # noqa: BLE001
            _logger.warning("IC pricing quote (bulk) failed: %s", exc)
        try:
            stock_res = client.get_stock(
                skus=skus, ship_to=backend.ship_to or None,
            )
            for entry in stock_res or []:
                sku = entry.get('sku')
                if not sku:
                    continue
                availability_by_sku.setdefault(sku, 0)
                availability_by_sku[sku] += entry.get('availability') or 0
        except Exception as exc:  # noqa: BLE001
            _logger.warning("IC stock lookup (bulk) failed: %s", exc)
        return prices_by_sku, availability_by_sku

    def _baf_ic_resolve_from_live_catalog(self, backend, language):
        """Original live-catalog resolver — used when the local cache is empty."""
        self.ensure_one()
        client = backend.get_client()

        # 1) Look up the seed product and read its genericArticleId.
        generic_id = None
        seed_sku = self.ic_seed_sku
        seed_index = self.ic_seed_index
        seed_category = self.ic_seed_category_id

        # Auto-discovery fallback: probe IC's catalog with the OEM
        # template's own identifiers. We stop at the first hit and
        # remember which handle matched so the equivalent-enumeration
        # step below can use it.
        seed_res = None
        candidates_to_try = []
        if seed_sku:
            candidates_to_try.append(('sku', seed_sku))
        if seed_index:
            candidates_to_try.append(('index', seed_index))
        if not candidates_to_try:
            for handle, value in self._baf_ic_autodiscovery_handles():
                candidates_to_try.append((handle, value))

        for handle, value in candidates_to_try:
            try:
                kwargs = {handle: value, 'page_size': 1,
                          'language': language}
                seed_res = client.get_catalog_products(**kwargs)
            except Exception as exc:  # noqa: BLE001
                _logger.debug(
                    "IC catalog probe %s=%s failed: %s",
                    handle, value, exc,
                )
                seed_res = None
                continue
            if seed_res and (seed_res.get('products') or []):
                if handle == 'sku' and not seed_sku:
                    seed_sku = value
                elif handle == 'index' and not seed_index:
                    seed_index = value
                break

        if seed_res:
            for product in (seed_res.get('products') or []):
                for ref in (product.get('genericArticleReferences') or []):
                    if ref.get('primary') and ref.get('genericArticleId'):
                        generic_id = str(ref['genericArticleId'])
                        break
                    if not generic_id and ref.get('genericArticleId'):
                        generic_id = str(ref['genericArticleId'])
                if generic_id:
                    break

        # 2) Enumerate candidates from the seed's category (or the
        #    manually-configured one) and keep those that share the
        #    genericArticleId.
        candidates = []
        if seed_category or generic_id:
            page = 0
            while page <= 40 and len(candidates) < _MAX_CARDS * 4:
                params = {'page_number': page, 'page_size': 100,
                          'language': language}
                if seed_category:
                    params['category_id'] = seed_category
                else:
                    # Without a category we can't paginate by keyword,
                    # so fall back to searching by whichever handle
                    # matched during seed lookup.
                    if seed_sku:
                        params['sku'] = seed_sku
                    elif seed_index:
                        params['index'] = seed_index
                res = client.get_catalog_products(**params)
                for product in (res.get('products') or []):
                    if self._baf_ic_matches_seed(product, generic_id):
                        # Exclude the seed itself.
                        if (product.get('sku') and
                                product.get('sku') == self.ic_seed_sku):
                            continue
                        candidates.append(product)
                if not res.get('hasNextPage'):
                    break
                page += 1

        # 3) Prices + availability.
        skus = [c['sku'] for c in candidates if c.get('sku')][:_MAX_CARDS * 2]
        prices_by_sku = {}
        availability_by_sku = {}
        if skus:
            try:
                price_res = client.get_price(
                    lines=[{'sku': s, 'quantity': 1} for s in skus],
                    ship_to=backend.ship_to or None,
                )
                for line in (price_res.get('lines') or []):
                    if line.get('sku') and line.get('price'):
                        prices_by_sku[line['sku']] = line['price']
            except Exception as exc:  # noqa: BLE001
                _logger.warning("IC pricing quote failed: %s", exc)
            try:
                stock_res = client.get_stock(
                    skus=skus,
                    ship_to=backend.ship_to or None,
                )
                for entry in stock_res or []:
                    sku = entry.get('sku')
                    if not sku:
                        continue
                    availability_by_sku.setdefault(sku, 0)
                    availability_by_sku[sku] += entry.get('availability') or 0
            except Exception as exc:  # noqa: BLE001
                _logger.warning("IC stock lookup failed: %s", exc)

        # 4) Build cards with BAF's marked-up sale price. Exclude the
        #    seed itself — it stands for the OEM product whose page we
        #    are on.
        if seed_sku:
            candidates = [c for c in candidates if c.get('sku') != seed_sku]
        partner = self.env.user.partner_id
        stock_cap = backend.stock_cap or 10
        cards = []
        for cand in candidates:
            sku = cand.get('sku')
            cost = prices_by_sku.get(sku)
            if cost is None:
                # Skip candidates we can't price — showing them without
                # a price would be a footgun for the customer.
                continue
            sale_price = self._baf_ic_customer_price(
                float(cost.get('customerPriceNet') or 0.0),
            )
            availability = availability_by_sku.get(sku, 0)
            cards.append({
                'ic_sku': sku,
                'ic_index': cand.get('index'),
                'ic_generic_article_id': self._baf_ic_extract_generic_id(cand),
                'brand': cand.get('brand') or _("Aftermarket"),
                'article_number': cand.get('articleNumber') or '',
                'short_description': (
                    cand.get('shortDescription')
                    or cand.get('description') or ''
                ),
                'availability': availability,
                'availability_label': self._baf_ic_availability_label(
                    availability, stock_cap,
                ),
                'quality': 'aftermarket',
                'is_oem': False,
                'sale_price': sale_price,
                'currency_code': (cost.get('currencyCode') or 'EUR'),
            })

        # 5) Prepend the OEM product itself (so the customer can compare).
        oem_card = self._baf_ic_oem_card(partner)
        cards = ([oem_card] if oem_card else []) + sorted(
            cards, key=lambda c: c['sale_price'],
        )
        return cards[:_MAX_CARDS]

    # ── Helpers ─────────────────────────────────────────────────────────
    def _baf_ic_autodiscovery_handles(self):
        """Yield (handle, value) pairs to try against IC's catalog.

        Handle is 'sku' or 'index' — the two selectors IC's catalog
        actually accepts on a lookup. The values come from fields
        already on the OEM template that plausibly match IC's data:

          * ``sku`` (BAF's own SKU column — usually the raw OEM number)
          * ``barcode`` (EAN — IC's catalog exposes an ``eans[]`` list
            and its indexer treats the leading EAN as a queryable sku)
          * ``default_code`` (internal reference — 'BMW_XXXX' style;
            fed through both 'sku' and 'index' since IC accepts either)

        Order matters: the first hit wins. Empty / falsy values are
        skipped. Duplicates are removed so we don't burn probes.
        """
        self.ensure_one()
        seen = set()

        def _push(handle, value):
            if not value:
                return
            key = (handle, value)
            if key in seen:
                return
            seen.add(key)
            probes.append(key)

        probes = []
        # Prefer the raw OEM number over the prefixed internal ref.
        _push('sku', (self.sku or '').strip() or None)
        _push('sku', (self.barcode or '').strip() or None)
        _push('sku', (self.default_code or '').strip() or None)
        _push('index', (self.sku or '').strip() or None)
        _push('index', (self.default_code or '').strip() or None)
        return probes

    def _baf_ic_matches_seed(self, product, generic_id):
        if not generic_id:
            return True  # nothing to filter on — keep everything
        for ref in (product.get('genericArticleReferences') or []):
            if str(ref.get('genericArticleId') or '') == generic_id:
                return True
        return False

    def _baf_ic_extract_generic_id(self, product):
        for ref in (product.get('genericArticleReferences') or []):
            if ref.get('primary') and ref.get('genericArticleId'):
                return str(ref['genericArticleId'])
        for ref in (product.get('genericArticleReferences') or []):
            if ref.get('genericArticleId'):
                return str(ref['genericArticleId'])
        return ''

    def _baf_ic_availability_label(self, qty, cap):
        if qty <= 0:
            return _("Out of stock")
        if qty >= cap:
            return _("In stock (%s+)") % cap
        return _("In stock (%s)") % int(qty)

    def _baf_ic_customer_price(self, cost):
        """Turn IC's ``customerPriceNet`` into the sale price we display.

        Delegates the markup math to ``product.product._baf_ic_list_price``
        — the same helper that seeds ``list_price`` at materialisation —
        so the card price and the cart price can never diverge. Then
        applies the shop's tax display so the block matches the OEM
        price shown right above it.
        """
        price = self.env['product.product']._baf_ic_list_price(cost)
        return self._baf_apply_website_tax(self, price)

    def _baf_ic_oem_card(self, partner):
        """Return an 'OEM' card for the current template — the OEM we
        already sell in this shop. Empty when the OEM has no price."""
        self.ensure_one()
        try:
            price = self.baf_website_display_price()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("OEM price computation failed for %s: %s",
                            self.id, exc)
            return None
        return {
            'ic_sku': '',
            'ic_index': '',
            'ic_generic_article_id': '',
            'brand': self.brand.name if self.brand else '',
            'article_number': self.sku or self.default_code or '',
            'short_description': self.name or '',
            'availability': int(self.qty_available or 0),
            'availability_label': _("Original — from stock"),
            'quality': self.part_quality or 'oem',
            'is_oem': True,
            'sale_price': price,
            'currency_code': (
                self.currency_id.name if self.currency_id else 'EUR'
            ),
            # For the OEM card, add-to-cart uses the standard route.
            'oem_product_id': self.product_variant_id.id,
        }


class ProductPublicCategory(models.Model):
    _inherit = 'product.public.category'

    enable_aftermarket_search = fields.Boolean(
        string="Show Aftermarket Alternatives",
        default=True,
        help="When OFF, product pages under this category never show "
             "Inter Cars alternatives even if the website-level toggle "
             "is ON. Use this to keep specific categories OEM-only.",
    )
