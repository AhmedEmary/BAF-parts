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
- **Unmatched SKUs stay visible** — a position whose `supplier_item_number` has
  no product becomes a text-only note line carrying the SKU, name, qty and net
  price, so the order still imports and nothing is silently lost.
- **Per-order isolation** — each order is imported inside its own savepoint, so
  a failure rolls back that order (including any partner created for it) and
  the batch continues with the next one.
- **Marketplace prices are authoritative** — imported lines are exempt from the
  BAF catalog repricing (`_baf_skip_repricing`), so the order keeps the net
  prices Alzura charged the buyer.
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
| `cart_order_id` (Alzura internal order number) | `alzura_internal_number` (form field visible on Alzura orders, optional list column, searchable) |
| (constant)                            | `so_source` → **Alzura**                   |
| `reference_number`                    | `customer_po` + `client_order_ref` (falls back to `cart_order_id`, then `order`) |
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
position whose SKU has no product is kept as a text-only note line
(`Unmatched SKU <sku>: <name> (qty …, net …)`) so the order still imports.

Line taxes come from the payload, not the product: `price.vat` (`0.19` → 19 %)
is resolved to a `percent` sale tax of the company at that rate, preferring the
company's configured `account_sale_tax_id` when several match. With no matching
rate the product default is left in place and a warning is logged — the rate is
never created here, since that is accounting configuration.

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

Buyers are matched in order by `contact.phone`, then by a real email (Alzura
masks the contact email behind a message URL, so only values containing `@` are
trusted), then by VAT, then by `res.partner.ref = ALZURA-<buyer id>`. When none
matches, a partner is created on first import capturing **all** available buyer
data:

| Alzura buyer field | Odoo `res.partner` |
| ------------------ | ------------------ |
| `address.name` (billing recipient; falls back to `contact.name`) | `name` |
| `address.name_additional`          | `street2` |
| `address.street/city/zip/country`  | `street` / `city` / `zip` / `country_id` |
| `contact.email` (real) / `contact.phone` | `email` / `phone` |
| `tax.sales_tax_identification_number` | `vat` |
| `bank` (`iban` / `owner` / `bic_swift` / `bank`) | `res.partner.bank` (+ `res.bank`) |
| `contact.name` (when ≠ `address.name`), `status_name`, `recipient_code`, `tax.tax_number`, `credit_reform` | `comment` (internal notes) |

Every buyer touched by an import is tagged **Alzura / Tyre24**
(`res.partner.category`, get-or-create); when the matched contact belongs to a
company, the company is tagged as well. (Up to v1.0 the tag was named *Alzura
Buyer* — the 1.1 migration moves those partners to *Alzura / Tyre24* and drops
the old tag.)

When the buyer sits inside a `cooperation`, that cooperation is created (or
matched on `ref = ALZURA-COOP-<number>`, then name) as a company partner, the
buyer becomes its child contact, and the **cooperation** is the order's
`partner_id`.

Bank-account creation is guarded: a malformed IBAN is logged and skipped
rather than rejecting the order.

### Intentionally not mapped

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
`alzura_charge` reconciliation, tax resolution from `price.vat`, exemption from
the BAF repricing, full buyer/partner extraction (address, VAT, bank account,
cooperation, masked-email rejection, enrichment notes), idempotency, unmatched
SKUs kept as note lines, and the full batch via `_alzura_fetch_orders`.

Every fixture order is asserted end-to-end: `amount_untaxed` must equal
`total_sum.net` and `amount_total` must equal `total_sum.gross`. The gross check
needs `tax_calculation_rounding_method = round_per_line`, since Alzura rounds
VAT per line.

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
| `sale.order`        | `alzura_internal_number`, `is_alzura_order` (computed, drives view visibility) + order-import methods (`_cron_fetch_alzura_orders`, `_alzura_fetch_orders`, …) — otherwise reuses `b2b_so` / `customer_po` / `so_source` from `general_system_custom` |
| `sale.order.line`   | `_baf_skip_repricing()` returns True on Alzura orders, plus `_compute_price_unit` / `_compute_discount` guards so the imported price survives a qty/product/partner write |

`is_alzura_order` matches **any** `so.source` named *Alzura*, not just the
xmlid-backed one, because databases carry hand-made duplicates of the source
and an order stamped with one must not escape the repricing guard.

## Migrations

| Version   | Does                                                          |
| --------- | ------------------------------------------------------------- |
| `1.1.0`   | Renames the *Alzura Buyer* tag to *Alzura / Tyre24* and drops the old one |
| `1.2.0`   | Repairs Alzura lines that the BAF catalog lookup had repriced: clears the `baf_applied_column_key` / `baf_applied_discount_pct` stamps and recomputes the subtotals. `price_unit` is **not** rewritten — lines whose `price_unit` and `technical_price_unit` disagree are only logged for manual review against the Alzura payload. |

## License

LGPL-3.
