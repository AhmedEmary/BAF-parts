# baf_oe_crossref — OEM ↔ Aftermarket cross-reference

Depends on `ic_intercars`. Adds:

- **Website toggle** `enable_aftermarket_search` (per website; per public
  category as an override). When OFF the shop behaves exactly as today
  (OEM only).
- **OEM → IC seed mapping** on `product.template`
  (`ic_seed_sku`, `ic_seed_index`, `ic_seed_category_id`) — used to
  resolve aftermarket equivalents via IC's catalog.
- **Product-page block** — inheriting `website_sale.product`, a card
  grid rendered after `#o_wsale_product_details_content`. Cards show
  brand, quality badge, BAF's marked-up sale price (never IC's cost),
  availability (respecting IC's `stock_cap`), and an **Add to cart**
  button.
- **Short-TTL cache** (`ic.article.cache`) — a page view doesn't
  repeatedly hit IC. Default TTL 15 min; override with system parameter
  `baf.ic_cache_ttl_sec`.
- **Cart controller** `/shop/cart/add_aftermarket` — lazy-creates the
  aftermarket `product.product` (via `_baf_find_or_create_ic`), refreshes
  the IC cost, then delegates to the standard `_cart_add()`.

## Setup

1. Install `ic_intercars` first and configure the IC backend
   (see its README).
2. Turn ON **Show Inter Cars Aftermarket Alternatives** on the website
   record (Website → Configuration → Websites → your website).
3. On each OEM product template you want covered, fill at least one of
   `IC Seed SKU`, `IC Seed Index`, or `IC Category ID`. Templates
   without a seed render an **empty** aftermarket block (never an
   error).
4. Tune the sale-price markup with system parameter
   `baf.ic_markup_pct` (default 25 %).
