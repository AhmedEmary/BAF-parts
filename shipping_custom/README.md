# BAF Shipping Integration (`shipping_custom`)

FedEx and DHL shipping integration for **Stock Pickings**, using Odoo's native
`stock.package.type` as the pallet/packaging source. No custom pallet model is
introduced — the integration reuses the package types you already configure in
*Inventory → Configuration → Package Types*.

## Features

- **Carrier accounts per company** for FedEx (REST) and DHL Express (MyDHL API),
  with sandbox/production toggle and a *Test Connection* button.
- **Shipping Selection tab on `stock.picking`** with:
  - A pallet list backed by `stock.package.type` (length, width, height and
    weight defaults are pulled from the chosen package type and overridable
    per row).
  - *Fetch Shipping Rates* — calls every configured carrier account in
    parallel and lists the priced services with delivery time and cost.
  - *Select Rate* — creates a draft `shipping.delivery.order` pre-filled
    with shipper, recipient and packages.
  - *Void Label* — cancels the carrier label and clears the tracking data
    on the picking.
- **`shipping.delivery.order`** — full editable shipment record with shipper /
  recipient address, packages, customs declaration, planned shipping time,
  incoterm, and a *Preview Payload* dialog that shows the exact JSON about
  to be sent to the carrier API.
- **Label generation** writes the tracking number, selected service and used
  carrier account back onto the picking, posts the labels to the picking's
  chatter, and keeps a full API call log on the delivery order.

## What's different from `intelliwise_srl/shipping_custom`

- Shipping tab is on **`stock.picking`** instead of `account.move`.
- Pallets are **`stock.package.type`** records (Odoo native), not the custom
  `warehouse.pallet` model. There is no dependency on the `intelliwise_custom`
  or `delivery_fedex_rest` modules.
- Customs commodities are built from the picking's `stock.move` lines (with
  `sale_line_id.price_unit` when available, otherwise `product.list_price`)
  instead of invoice lines.
- The purchase-matching / inbound-reconciliation flow on `account.move` is
  not ported — this module focuses strictly on label generation.

## Installation

1. Place the module under your Odoo addons path (it lives at
   `BAF-parts/BAF-parts/shipping_custom`).
2. Update the module list and install **BAF Shipping Integration**.
3. Open *Settings → Shipping Integrations* and add at least one carrier
   account:
   - **FedEx**: API key (Client ID), API secret, account number, label
     stock type. Use *Test Connection* to verify credentials.
   - **DHL**: API key, API secret, account number, label format.

## Usage

1. Open a draft outgoing transfer (`stock.picking`).
2. Go to the **Shipping Selection** tab.
3. Add one row per pallet, pick a *Package Type* — dimensions and weight
   default from the type and can be overridden.
4. Optionally fill *Customs Value* (used as declared value for international
   shipments).
5. Click **Fetch Shipping Rates**. Each configured carrier account is
   queried; priced services appear under *Available Rates*.
6. Click **Select Rate** on the chosen service. A draft delivery order
   opens.
7. Review the shipper / recipient / packages, optionally click *Preview
   Payload* to inspect the request, then **Generate Label**.
8. The label PDF is attached to the delivery order (and to the picking's
   chatter), the tracking number is written back to the picking, and the
   delivery order moves to *Confirmed*.

To void a label, click **Void Label** on the picking — or *Cancel / Void*
on the delivery order. FedEx is voided via API; DHL has no cancellation
endpoint, so the tracking is cleared locally only.

## Models

| Model                                 | Purpose                                                |
| ------------------------------------- | ------------------------------------------------------ |
| `shipping.provider.account`           | FedEx/DHL credentials (per company).                   |
| `shipping.picking.package`            | Pallet/package row on a `stock.picking` (references `stock.package.type`). |
| `picking.shipping.option`             | One priced service returned by *Fetch Shipping Rates*. |
| `shipping.delivery.order`             | Drafted/confirmed shipment with shipper, recipient, packages, customs. |
| `shipping.delivery.order.package`     | Per-package line on a delivery order (references `stock.package.type`). |
| `shipping.delivery.order.api.log`     | Outbound API call log for diagnostics.                 |
| `shipping.delivery.order.preview.wizard` | Read-only "exact JSON to be sent" preview dialog.   |

`stock.picking` is extended with:
`shipping_package_ids`, `shipping_option_ids`, `customs_value`,
`shipping_currency_id` (computed: sale-order currency when present,
otherwise company currency), `tracking_number`,
`selected_shipping_service`, `provider_account_id`, `delivery_order_ids`.

## Architecture notes

- **Rate request** uses `packagingType=YOUR_PACKAGING` and tags each package
  with `subPackagingType=PALLET`. Other FedEx-branded packagings
  (FEDEX_BOX, FEDEX_PAK, …) are not used.
- **Pre-flight validation** rejects rate calls when shipper/recipient
  addresses are incomplete (missing country/street/city/zip) or when any
  pallet has zero weight or zero dimensions — the user gets a clear
  `UserError` listing every offending record instead of an opaque
  `SERVICE.PACKAGECOMBINATION.INVALID` from FedEx.
- **Logging**: FedEx 4xx responses log at INFO (the user already sees the
  carrier code in the `UserError`), 5xx responses log at WARNING. Both
  carry the request and response bodies for diagnostics.

## Tests

Tests live in `tests/test_shipping_logic.py`. Run them with:

```bash
odoo-bin -d <database> -i shipping_custom \
  --test-enable --test-tags shipping_custom --stop-after-init
```

The suite covers:

- Account connection success (mocked OAuth response).
- `stock.package.type` defaults flow into a new
  `shipping.picking.package` via the onchange.
- Fetching rates populates `picking.shipping_option_ids`.
- Fetching with no pallets or no carrier account raises `UserError`.
- *Select Rate* creates a draft delivery order with packages and
  pre-filled shipper/recipient.
- Generating a label updates the picking with tracking + provider account
  (uses a mocked carrier response with a valid minimal PDF).
- Voiding a confirmed shipment cancels the order and clears the picking
  tracking.
- FedEx pre-flight rejects zero-weight / zero-dim pallets and surfaces
  every bad pallet in a single error.
- FedEx rate request always uses `YOUR_PACKAGING` + `PALLET`.
- Multi-pallet rate request sends each pallet as its own package entry
  with its own weight and dimensions.
- `customs_value` flows into FedEx's `customsClearanceDetail.commodities`
  for international shipments.
- `build_shipment_payload` (label-time) always uses `YOUR_PACKAGING`.

## License

LGPL-3.
