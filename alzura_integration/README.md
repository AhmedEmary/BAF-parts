# Alzura B2B Integration (`alzura_integration`)

Connects Odoo to the [Alzura](https://www.alzura.com) B2B automotive
marketplace (tyres, rims & spare parts) REST API. It authenticates per
company, stores the auth token securely, and imports the latest marketplace
orders into native `sale.order` records — manually from Settings or on a
twice-daily schedule. Imported orders are **confirmed** (not left as draft
quotations).

No new sales models are introduced: incoming orders are mapped onto the
existing `sale.order` / `sale.order.line`, reusing the custom `b2b_so`,
`customer_po` and `so_source` fields from `general_system_custom` (the source
is set to **Alzura**).

## Features

- **Per-company authentication** against `POST`-style Basic-auth login
  (`/common/login`). The password is **never stored** — only the returned
  token and its expiry are persisted on `res.company`.
- **Token management** from *Settings → Alzura B2B*: *Get Token*, a live
  *Token Status* badge (None / Active / Expired) with expiry date, and
  *Delete Token*.
- **Order import** from `/common/latestorders`:
  - *Fetch Orders Now* button for an on-demand pull.
  - A scheduled action that runs **twice a day** (every 12h).
  - Relies on Alzura's "since the last call" tagging, so each run only
    returns orders not yet retrieved.
- **Idempotent** — orders are de-duplicated on the Alzura order number stored
  in `b2b_so`; re-running never creates duplicates.
- **All-or-nothing import** — if any position references a SKU with no matching
  product, the whole order is rejected (rolled back via a per-order savepoint)
  rather than imported with missing lines. The batch continues with the next
  order.
- **Auto-confirmed** — each imported order is confirmed into a sale order via
  `action_confirm()` (which creates its delivery picking). Confirmation reserves
  only what is on hand; full reservation is left to the warehouse.

## Dependencies

`base`, `base_setup`, `general_system_custom` (for the `sale.order`
`b2b_so` / `customer_po` / `so_source` fields and the `product.product.sku`
used for product matching).

## Installation

1. Place the module under your Odoo addons path (it lives alongside the other
   BAF custom modules in `BAF-parts/`).
2. Update the apps list and install **Alzura B2B Integration**.
3. Open *Settings → Alzura B2B*.

## Configuration & Usage

### Authenticate

1. *Settings → Alzura B2B → Alzura B2B API Credentials*.
2. Enter your **Alzura ID** and **Password**, set the **Country**
   (ISO 3166-1 alpha-2, lowercase — defaults to `de`).
3. Click **Get Token**. On success the Token Status badge turns *Active* and
   the expiry date is filled in. The password is discarded.
4. **Delete Token** clears the stored token at any time.

### Import orders

- Click **Fetch Orders Now** (*Orders* block) to pull immediately, or
- Leave the scheduled action to run twice a day.

Either path calls `/common/latestorders` and creates a confirmed `sale.order`
per new Alzura order.

## Order mapping

| Alzura field                          | Odoo `sale.order`                          |
| ------------------------------------- | ------------------------------------------ |
| `order` (e.g. `PAC1234567890719`)     | `b2b_so` — **de-dup key**                  |
| (constant)                            | `so_source` → **Alzura**                   |
| `reference_number`                    | `customer_po` + `client_order_ref`         |
| `date`                                | `date_order`                               |
| `buyer`                               | `partner_id` (see below)                   |
| `positions[]`                         | `order_line`                               |
| `shipping.deliveryDate` / `tracking[].deliveryDate` | `commitment_date`            |
| `shipping.delivery_address`           | `partner_shipping_id` (if alternative)     |
| `shipping.method.price` + `handling_fee` | **Shipping fee** `order_line` (if ≠ 0)  |
| `payment.method.price` + `price_additional` | **Payment fee** `order_line` (if ≠ 0) |
| reconciliation to `total_sum.net`     | **alzura_charge** `order_line` (if ≠ 0)    |
| `comment`, shipping/tracking, payment, `currency`, `documents` | `note`         |

Per position: product matched on `product.product.sku` against
`supplier_item_number`; `quantity` → ordered qty, `price.net` → unit price. A
position whose SKU has no product raises an error that rejects the whole order.

Fee lines use a get-or-create `Alzura Charge` service product:

- **Shipping fee** = `shipping.method.price.net` + `shipping.handling_fee.net`
- **Payment fee** = `payment.method.price.net` + `payment.price_additional.net`

Each fee is added only when non-zero. After positions and fees, any remaining
gap to `total_sum.net` is booked as a single `alzura_charge` line, so the order
net always matches Alzura's `total_sum`. The delivery date populates
`commitment_date`; an alternative delivery address becomes a `delivery`-type
child contact under the buyer, set as `partner_shipping_id`. Remaining
informational fields (order comment, shipping method/flags, tracking, payment
method, currency conversion and document links) are summarised into the order
`note`.

### Buyer (partner) mapping

Buyers are matched by `res.partner.ref = ALZURA-<buyer id>`, then by a real
email (Alzura masks the contact email behind a message URL, so only values
containing `@` are trusted). When not found, a partner is created on first
import capturing **all** available buyer data:

| Alzura buyer field | Odoo `res.partner` |
| ------------------ | ------------------ |
| `address.name` (or `contact.name`) | `name` |
| `address.name_additional`          | `street2` |
| `address.street/city/zip/country`  | `street` / `city` / `zip` / `country_id` |
| `contact.email` (real) / `contact.phone` | `email` / `phone` |
| `tax.sales_tax_identification_number` | `vat` |
| `bank` (`iban` / `owner` / `bic_swift` / `bank`) | `res.partner.bank` (+ `res.bank`) |
| `status_name`, `tax.tax_number`, `credit_reform` | `comment` (internal notes) |

Bank-account creation is guarded: a malformed IBAN is logged and skipped
rather than rejecting the order.

### Intentionally not mapped

Buyer `cooperation` / `recipient_code` (empty Alzura-internal data),
`contact.firstname` / `lastname` / `fax` (no dedicated Odoo 19 partner field —
`name` covers the name; `fax` was removed), the per-position `seller` block (the
seller is this Odoo instance), per-position `attributes` / `check_options`, and
`currency` (Odoo derives the order currency from the pricelist — the Alzura
currency is recorded in the note instead).

## Scheduled action

| Field          | Value                              |
| -------------- | ---------------------------------- |
| Name           | *Alzura: Fetch Latest Orders*      |
| Model          | `sale.order`                       |
| Code           | `model._cron_fetch_alzura_orders()`|
| Interval       | every 12 hours (2× / day)          |

It iterates every company holding a token and imports orders for each. Adjust
or disable it under *Settings → Technical → Scheduled Actions*.

## Tests

Unit tests live in `tests/test_alzura_import.py` and exercise the import logic
against the bundled fixture `tests/fixtures/latest_orders.json` (no API call) —
order confirmation, `so_source`, position/charge-line mapping, the
`alzura_charge` reconciliation, full buyer/partner extraction (address, VAT,
bank account, masked-email rejection, enrichment notes), idempotency, rejection
of unmatched SKUs, and the full batch via `_alzura_fetch_orders`.

```bash
odoo-bin -d <db> -i alzura_integration --test-enable --stop-after-init
# or, against an installed module:
odoo-bin -d <db> -u alzura_integration --test-enable --stop-after-init
```

The fetch entrypoint is driven in tests by patching `_alzura_orders_payload` to
return the fixture, so no token or network access is needed.

## Notes

- **Rate limit**: Alzura allows 2 requests per 300 seconds on
  `/common/latestorders`. The twice-daily cron is well within budget; the
  manual button surfaces a clear "try again later" message on HTTP 429.
- Token validity follows the API's reported `expire_date`; a 24h fallback is
  used only if the API omits it.
- Authentication failures (HTTP 401) surface as a "Refresh the token" message.

## Models

No new models. Extensions only:

| Model               | Added                                                          |
| ------------------- | ------------------------------------------------------------- |
| `res.company`       | `alzura_token`, `alzura_token_expiry`, `alzura_country`, `_alzura_request_headers()` |
| `res.config.settings` | UI for credentials/country + token & fetch buttons          |
| `sale.order`        | order-import methods (`_cron_fetch_alzura_orders`, `_alzura_fetch_orders`, …) — **methods only; reuses `b2b_so` / `customer_po` / `so_source` from `general_system_custom`** |

## License

LGPL-3.
