# ic_intercars — Inter Cars integration

REST client, credentials backend, ordering hook, and reconciliation
crons for the Inter Cars (IC S.A.) API. Aftermarket alternatives are
sourced from Inter Cars and drop-shipped through the ordinary Odoo
purchase → drop-ship pipeline; this module provides the API plumbing
that pipeline needs.

## Setup

1. Create an ordinary vendor partner for Inter Cars
   (`res.partner`, `supplier_rank > 0`).
2. Go to **Purchases → Configuration → Inter Cars** and create a
   backend record. Fill:
   - **Inter Cars Vendor**: the partner from step 1.
   - **Client ID / Client Secret**: from the IC developer portal.
   - **Token URL**: OAuth2 token endpoint — **not** published in the
     Swagger, ask IC for the exact URL for your account.
   - **Base URL**: `https://api.webapi.intercars.eu` (default).
   - **Catalog Language**: `de` for BAF.
   - **Currency / Market**: BAF's is EUR / DE. Polish-only features
     (deferredPayment, KSeF, GTU) are gated on `market = pl`.
   - **shipTo / delivery method / payment method**: the codes IC gave
     you for your account. `shipTo` is an IC customer identifier, not
     an address.
3. Click **Test Connection**. This runs an OAuth2 handshake against
   the Token URL and calls `/ic/customer` with the resulting bearer.
4. Enable the reconciliation crons at
   **Settings → Technical → Scheduled Actions** once you're satisfied
   with the connection. They walk day-by-day (IC caps date searches at
   a 2-day window) through `/ic/delivery` and `/ic/invoice`.

## Ordering flow

- IC aftermarket products are materialised on the fly the first time
  they are sold — see `_baf_find_or_create_ic()` on `product.product`.
- On `purchase.order.button_confirm()` for a PO whose vendor is the IC
  vendor: this module submits an IC **requisition**, verifies the
  response `phaseCode` is `ACCEPTED`, then calls the `.../confirm`
  endpoint. The `id` and `requisitionId` are stored on the PO for
  later reconciliation.
- The `orderingAllowed` flag from `/ic/customer/finances` is consulted
  first; a `false` value blocks the requisition with a UserError
  explaining the overdue-balance situation.

## Open items (need input from Inter Cars)

- **OAuth2 token URL** — not in the Swagger. Backend config field.
- **End-customer drop-ship address** — the requisition schema has no
  free-form delivery address, only `shipTo` (an IC customer id) and
  `deliveryMethod`. Confirm with IC how a shipment can be routed to a
  BAF customer's own address (dedicated delivery method? per-customer
  IC customer ids?).
- **OEM → IC seed mapping** — how each BAF OEM template maps to an IC
  `sku`/`index`/`categoryId` for equivalence lookup. See
  `baf_oe_crossref` — that module owns the mapping fields.
- **CSV channel (F04217)** — the credentials are stored on the backend
  for future use; no importer is wired yet.
