# Inter Cars Website Catalog Search

Customer-facing search over the `ic.product.info` cache (~2M rows)
with on-demand product materialisation — the webshop can offer the
whole Inter Cars catalog without pre-creating two million products.

## Visitor flow

1. `/aftermarket/search?q=…` (also linked from the main website menu).
2. Matches among already-materialised, published aftermarket products
   link straight to their product pages.
3. Remaining matches come from the IC cache. "Check price & order"
   quotes IC live, materialises the product via the standard
   `_baf_find_or_create_ic` flow (live cost, markup price, drop-ship
   route, supplier line, Aftermarket ribbon), publishes it, and
   redirects to the product page where the visitor buys normally.
4. SKUs IC declines to quote show a friendly "cannot be ordered right
   now" message; no product is ever created without a price.

## Why it stays fast on 2M rows

Identifier queries (IC index, TecDoc/article number, EAN) are answered
from normalised key columns — upper-cased, separators stripped, so
`op 520` = `OP520` = `OP-520` — each backed by a
`varchar_pattern_ops` index serving equality *and* prefix lookups.
The keys are rebuilt by one SQL `UPDATE` at the end of every CSV
import (the loader COPYes around the ORM, so computed fields can't do
it). Description search is a bounded fallback that only runs when no
identifier matched.

## Abuse control

Materialisation is the single write a visitor can trigger; it is
capped per session (30/hour) so a bot walking SKUs cannot inflate the
product table.

## Unpublishing

The lazy flow re-publishes a product each time a visitor requests it.
To remove a part from the shop permanently, archive the product —
archived products are only revived by an actual re-order.
