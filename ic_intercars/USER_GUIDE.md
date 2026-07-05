# Inter Cars integration — Operator's guide

Everything an operator needs to run the Inter Cars (IC) integration:
import the IC catalogue, create sellable aftermarket products from it,
and order from IC through the normal purchase flow.

## 1. What the integration does

- Keeps a **local cache** of IC's full product feed (~1.7M rows,
  refreshed from IC's daily `ProductInformation` CSV).
- Lets staff **create normal Odoo products** from that cache (or from a
  live catalog search). Created products are ordinary catalog products:
  they show up in shop search, quotations, and reports like any other
  product. They are marked in three ways:
  - **Part Quality = Aftermarket** (field on the Inter Cars tab);
  - product category **Aftermarket**;
  - an **"Aftermarket" ribbon** on the webshop.
- Each IC product carries a **Manufacturer** (Bosch, Valeo, Filtron…).
  This is deliberately *not* a Brand — Brands are car makes (BMW,
  Mercedes…) and drive the discount/pricing system; aftermarket parts
  stay out of that system.
- On confirming a purchase order for the IC vendor, the module submits
  an **IC requisition** via the API and stores IC's order ids on the PO.

There is **no automatic OEM ↔ aftermarket mapping**. IC confirmed the
data needed for that (TecDoc OE numbers) is not available through their
API or CSV. Staff choose which IC parts to sell.

## 2. Setup — do this once

1. Create an ordinary vendor partner for Inter Cars
   (`res.partner`, `supplier_rank > 0`).
2. Go to **Purchases → Configuration → Inter Cars** and create a
   backend record: vendor, OAuth2 Client ID/Secret, Token URL (ask IC),
   Base URL, currency/market, and the `shipTo` / delivery method /
   payment method codes IC gave you. Click **Test Connection**.
3. Fill the **CSV Login / Password** (the F04217 channel) on the same
   backend record.
4. Import the catalogue: **Purchases → Configuration → Import IC CSV**
   → *Fetch from Inter Cars* → Import. Takes a few seconds via COPY;
   re-run any time (each import replaces the previous snapshot).

## 3. Creating sellable aftermarket products

Products are created by hand-picking, in either of two places:

### 3.1. From the local cache (bulk)

**Purchases → Configuration → IC Products (cache)**. Search or filter
(e.g. group by Manufacturer), tick the rows you want to sell, and click
**Create Products**.

For every selected row the system:

1. asks IC for a **live price** (`customerPriceNet` — BAF's cost);
2. creates the product with:
   - `sku` = IC SKU, internal reference `IC_<sku>`;
   - name, EAN, weight and dimensions from the feed;
   - Manufacturer, Part Quality = Aftermarket, category Aftermarket,
     webshop ribbon;
   - the **drop-ship route** and a **supplier line** on the IC vendor
     at the live cost;
   - **sales price = cost × (1 + markup)** — see §5.
3. SKUs IC refuses to quote (discontinued, not orderable) are
   **skipped** and listed in the result message — a product is never
   created with a zero price.

Re-running on rows already created **refreshes** cost and sales price
instead of duplicating. Archived IC products are re-activated.

### 3.2. From a live search (one-off)

**Purchases → Configuration → Search IC Catalog (Live)**: search by IC
SKU, IC index, or category; results show live price and availability.
Click **Create Product** on a row.

## 4. Selling and ordering

Created IC products sell like any other product. Because they carry the
drop-ship route and an IC supplier line, a confirmed sale creates a PO
on the IC vendor automatically.

On `purchase.order` confirmation for the IC vendor, the module:

1. checks IC's `orderingAllowed` flag (blocks if BAF's account is
   overdue);
2. checks live availability;
3. re-quotes the live price and syncs the PO line;
4. submits the requisition and requires `phaseCode = ACCEPTED`, then
   confirms it. IC's ids are stored on the PO for reconciliation.

**Delivery address**: IC cannot ship to a free-form per-order address —
`shipTo` must be a recipient pre-registered at IC. Until that changes,
IC deliveries arrive at BAF and are forwarded to the customer.

## 5. Markup / customer price

System parameter `baf.ic_markup_pct` (default `25`). Sales price is set
to IC cost × (1 + markup/100) at creation and refreshed on every
re-create/refresh of the same SKU.

## 6. Reconciliation crons

Two crons (disabled by default, enable under **Settings → Technical →
Scheduled Actions**) walk IC's `/ic/delivery` and `/ic/invoice`
day-by-day and post summaries on the matching PO's chatter.

## 7. Troubleshooting

- **"IC declined to quote SKU …"** — the part is discontinued or not
  orderable on BAF's account. Pick another part.
- **"Unknown columns in ProductInformation CSV header"** — IC changed
  the feed schema; the importer refuses rather than silently dropping
  data. Update `ic.product.info` accordingly.
- **Requisition blocked: ordering not allowed** — IC reports an
  overdue balance on the account; clear it with IC.
- **Token errors on Test Connection** — check the Token URL (not in
  IC's Swagger; must come from IC support).
