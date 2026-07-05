# Inter Cars integration — Operator's guide

**For BAF Handels GmbH.** How the Inter Cars (IC) aftermarket integration
works day-to-day, how to set it up once, and how to keep it running.

Two Odoo modules do the work:

- **`ic_intercars`** — the plumbing. Talks to Inter Cars' REST API,
  holds credentials, imports the nightly product catalogue, drives the
  drop-ship purchase orders.
- **`baf_oe_crossref`** — the shop side. Adds the "Aftermarket
  alternatives" block to OEM product pages and handles Add-to-cart for
  aftermarket items.

Install `baf_oe_crossref` — the other one comes with it as a dependency.

---

## 1. What the integration does

BAF's Odoo catalogue holds **only OEM parts** (Original Equipment Manufacturer —
BMW, Jaguar, Land Rover, Mercedes). Inter Cars is a distributor of
**aftermarket parts** — third-party brands (FILTRON, VALEO, MEAT & DORIA, …)
that fit the same cars.

When the integration is on:

1. A customer opens an OEM product page in the shop, e.g. Land Rover
   part `LR_231498`.
2. Below the OEM's own price/Add-to-cart, an **Aftermarket alternatives**
   section shows a card for each equivalent part IC distributes:
   VALEO radiator, MEAT & DORIA switch, CALORSTAT thermostat, and so
   on — each with brand, quality badge, **BAF's marked-up price**
   (never IC's cost), current warehouse availability, and an **Add to
   cart** button.
3. If the customer buys the aftermarket variant, Odoo processes the
   order like any other sale. Behind the scenes, a drop-ship purchase
   order is placed at Inter Cars and the parcel ships from IC's
   warehouse directly to BAF's customer — under BAF's branding.
4. Deliveries and invoices from IC are pulled nightly and attached to
   the matching purchase orders for reconciliation.

Aftermarket alternatives are opt-in per website; the shop reverts to
OEM-only browsing when the toggle is OFF.

---

## 2. Setup — do this once

### 2.1. Credentials from Inter Cars

Ask IC for a "BAF Handels GmbH" API access sheet. It has five things
you'll need:

| What | Where it goes | Example (do not use — for shape only) |
|---|---|---|
| Client ID (production) | Backend → Client ID | `xxxxxxxxxxxx…` |
| Client Secret (production) | Backend → Client Secret | `xxxxxxxx…` |
| OAuth2 Token URL | Backend → Token URL | `https://is.webapi.intercars.eu/oauth2/token` |
| CSV Login (F-code) | Backend → CSV Login | `F04217` |
| CSV Password | Backend → CSV Password | `xxxxxxxx…` |
| shipTo code | Backend → shipTo | `F17` (BAF's Customer Branch) |

Delivery method (`DIST`) and payment method (`14`) come from IC's own
customer configuration; the backend has fields for them.

### 2.2. Create the IC vendor in Odoo

**Purchase → Orders → Vendors → New** → Name `Inter Cars S.A.` → Save.

This is a normal Odoo vendor partner — every drop-ship PO the shop
generates for an aftermarket sale is placed against this partner, so
IC parts flow through the same purchase, receiving, and invoice
machinery you already use for other suppliers.

### 2.3. Configure the IC backend

**Purchase → Configuration → Inter Cars → New.**

| Field | What to put |
|---|---|
| Name | `Inter Cars` |
| Inter Cars Vendor | the partner created above |
| Company | your company (only visible in multi-company setups) |
| Active | ✔ |
| Client ID / Secret / Token URL | from IC |
| Base URL | leave the default (`https://api.webapi.intercars.eu`) |
| OAuth2 Scope | leave the default (`allinone`) |
| shipTo | `F17` (your Customer Branch) |
| Delivery Method | `DIST` |
| Payment Method | `14` |
| Catalog Language | `de` (or `en` if you'd rather see IC descriptions in English) |
| Currency | `EUR` |
| Market | Germany (DE) |
| Assumed VAT % | `19` |
| CSV Login / Password | from IC |

Save the record, then click **Test Connection**. A green *"Connection
Successful"* toast means auth is working. Any error message here is a
credentials / token URL problem — fix it before continuing.

### 2.4. Import the IC catalogue (one time to start)

Still on the backend form, click **Import ProductInformation CSV** in
the header. Two choices:

- **Fetch from Inter Cars** *(recommended once credentials are in)* —
  the wizard downloads today's `ProductInformation` file from IC's
  CSV server using the login/password from the backend. Handles the
  ZIP for you.
- **Upload File** — if IC's server isn't reachable from Odoo (some
  firewalls), download the file manually and drop it here.

Leave **Auto-populate IC Seed SKU on matching BAF templates** ticked.
Click **Import**.

Expect ~60 seconds for the load. Success toast will say something like:

> Imported 1,711,989 IC products in 58.0s.
> Auto-mapped ic_seed_sku on 1,907 BAF templates.

The 1.7M figure is IC's whole product range. The 1,907 figure is the
number of your OEM templates that Odoo could line up with an IC
identifier automatically — those are the templates that will render
aftermarket cards on the shop page.

### 2.5. Turn the shop feature on

**Website → Configuration → Websites** → open your website → tick
**Show Inter Cars Aftermarket Alternatives** → Save.

That's the master switch. When it's off, the shop is OEM-only.

You can also disable aftermarket for specific eCommerce categories:
**Website → Configuration → eCommerce Categories** → open a category →
untick **Show Aftermarket Alternatives**. Useful for categories you'd
prefer to keep OEM-only (e.g. rare vintage parts).

### 2.6. Enable the nightly CSV refresh (optional)

**Settings → Technical → Scheduled Actions** →
"BAF: Purge expired IC equivalents cache" — already active by default.

To automate CSV refresh, either:

- Add a scheduled action that calls `ic.product.info.bulk_load_csv` on
  a nightly cron (contact your Odoo admin), or
- Just run the wizard manually once a week by clicking "Fetch from
  Inter Cars".

---

## 3. Daily operations

### 3.1. What the customer sees

On any OEM product whose template has `IC Seed SKU` filled in:

- **Original (OEM)** card — green badge, BAF's own OEM part, priced as
  today.
- One card per aftermarket equivalent IC ships — each with
    - brand (FILTRON, VALEO, MEAT & DORIA, …),
    - "Aftermarket" badge,
    - BAF's sale price (IC's `customerPriceNet` × your markup),
    - availability (e.g. "In stock (4)", "In stock (10+)", "Out of stock" —
      IC caps availability at 10 = "10 or more"),
    - Add to cart button.

Cards are sorted OEM first, then by ascending price. Up to 24 cards.

When there are no aftermarket equivalents (either the template has no
seed, or IC has no cross-reference), the section is empty — never an
error. Guests and B2B customers see the same aftermarket cards.

### 3.2. Selling an aftermarket part

Nothing changes on the customer side — Add to cart → checkout →
confirm — just like any other sale.

Under the hood:

1. On Add to cart, if this is the first time anyone has ordered this
   specific IC SKU, Odoo lazily creates a `product.product` for it
   (SKU = IC's SKU, brand = the aftermarket manufacturer, drop-ship
   route on, `part_quality = aftermarket`, IC vendor priced from the
   live quote). No manual "add aftermarket product" step is needed.
2. On Confirm, Odoo's standard drop-ship route generates a purchase
   order at Inter Cars for that line only.
3. On Confirm Order on the PO, this integration submits an IC
   requisition, verifies IC accepted it (phase `ACCEPTED`), and calls
   the confirm endpoint. IC then fulfils the delivery directly to
   BAF's customer.

You'll see two chatter messages on the PO:

> Inter Cars requisition submitted — requisitionId=…, id=…, phase=ACCEPTED. Confirming next.
>
> Inter Cars requisition confirmed — status=…

If IC has blocked ordering (overdue balance beyond credit limit), the
PO confirm fails with a clear message pointing at your IC portal.
Nothing gets sent to IC — you fix the payment problem and retry.

### 3.3. Refreshing the IC catalogue

IC updates prices and available parts nightly. The local cache goes
stale within a day. Refresh by either:

- Running **Import ProductInformation CSV** from the backend (Fetch
  from Inter Cars, roughly weekly is fine),
- Or letting the scheduled cron do it if you enabled it.

Prices and stock shown on individual shop pages are **not** cached —
each page render calls IC's live pricing and stock endpoints. Only the
catalogue (which parts exist, brands, TecDoc numbers, descriptions,
EANs) comes from the CSV. So an out-of-date CSV means "the *list* of
alternatives may be slightly out of date" — not "the prices are
wrong". Very acceptable.

### 3.4. Reconciliation

Two crons under **Settings → Technical → Scheduled Actions**:

- **Inter Cars: Reconcile Deliveries** — hourly. Walks IC's
  `/ic/delivery` endpoint day by day (IC's search caps at a 2-day
  window), matches each shipment back to the originating PO via the
  requisition ID, and posts a chatter note on the PO.
- **Inter Cars: Reconcile Invoices** — every 6 hours. Same idea for
  `/ic/invoice`.

Both crons are **inactive by default** — turn them on once you're
happy with the manual flow. Cron activation only affects reconciliation
recording; ordering works either way.

---

## 4. Admin tasks

### 4.1. Browsing the IC catalogue in Odoo

**Purchase → Configuration → IC Products (cache)** shows the imported
CSV as a searchable list. Useful for:

- Looking up an IC SKU to confirm it's cataloged (`tow_kod = ADDFFF`).
- Finding equivalents of a TecDoc number (search on `TecDoc`, group by
  `Manufacturer`).
- Checking whether a specific OEM number IC ships (search on `IC SKU`
  or `Article Number` — IC's catalogue does **not** contain car-maker
  OEM numbers directly; see §5.2).

### 4.2. Browsing OEM templates that have aftermarket coverage

**Purchase → Configuration → Products with IC Aftermarket** opens the
filtered list of your BAF templates whose `ic_seed_sku` was auto-filled
by the last CSV import (1,909 today).

Alternatively, from any product template list, click **Filters → Has
IC Aftermarket** or **Filters → No IC Aftermarket**.

### 4.3. Fixing a wrong or missing seed by hand

Open the template → **Inter Cars** tab → edit:

- **IC Seed SKU** — the IC SKU that identifies the equivalent
  aftermarket part(s). Leaving this empty and setting **IC Seed
  Index** or **IC Category ID** instead also works.
- **Part Quality** — should be `OEM` for BAF's own templates. Lazy-
  created IC products get `Aftermarket`.

Save. The shop page picks up the change on next render (the local
cache is stamped with the seed key, so a stale card only lingers up to
15 minutes — configurable via system parameter `baf.ic_cache_ttl_sec`).

### 4.4. Changing the markup

BAF's sale price for an aftermarket item is `IC customerPriceNet ×
(1 + markup%)`. Default markup is **25 %**.

To change it, go to **Settings → Technical → System Parameters** →
find (or add) `baf.ic_markup_pct` → set to the number you want (e.g.
`30.0` for 30 %). Change takes effect at the next shop page render;
the short cache expires quickly on its own.

### 4.5. Reviewing an IC requisition on a PO

Open any drop-ship PO whose vendor is Inter Cars. In the **Inter
Cars** section on the form you'll see:

- **IC Requisition ID** — IC's business reference (visible on IC's
  own delivery paperwork).
- **IC Requisition UUID** — the internal id used to confirm/cancel via
  the API.
- **IC Status** — current phase from IC.

The PO's chatter has the full timeline: submitted → confirmed →
(later) delivered → invoiced.

---

## 5. Known limitations

### 5.1. Precision of the aftermarket match

The auto-map lines up a BAF template with any IC part that references
the BAF SKU in any of IC's identifier columns (`tow_kod`, `ic_index`,
`tec_doc`, `article_number`). Some short OEM numbers (e.g. `10002`)
appear in many aftermarket vendors' cross-references for **unrelated
parts**, producing false-positive cards.

Two ways to handle this:

- **Manually override the seed** on templates where the automatic
  match is wrong (§4.3).
- **Tighten the OEM template's category** — a per-category kill-switch
  (§2.5) means you can turn aftermarket off for whole sections of the
  catalogue where the noise is highest.

A stricter automatic filter (matching on IC generic-article group +
OEM category compatibility) is on the roadmap; ping the dev team.

### 5.2. Not every OEM number will find equivalents

IC's public API and CSV do not accept car-maker OEM numbers as a
lookup key. The only join keys they publish are IC's own SKU, IC's
index (the aftermarket brand's article number), and TecDoc article
numbers (the aftermarket brands' TecDoc ids — not the car makers').

Result: for BAF templates whose SKU happens to also appear as an
aftermarket brand's article number, we get real matches. For OEM
numbers that don't overlap that way (e.g. a Land Rover part number
never referenced by an aftermarket vendor's own catalogue), we get no
matches — the aftermarket block stays empty on those pages.

To close the remaining gap you need one of:

1. A **custom aggregation from IC** with an OE-cross-reference column
   (their platform supports custom aggregations — worth a support
   ticket to IC's API team).
2. **Licensed TecDoc OE-Numbers data** — TecDoc's cross-reference
   table maps every OEM number to the TecDoc ArtNr of every
   aftermarket equivalent; the ArtNr then joins directly against the
   `tec_doc` column we already have.

### 5.3. End-customer drop-ship address

The IC requisition schema exposes `shipTo` (an IC customer identifier)
and `deliveryMethod`, but **no free-form delivery address**. Today the
integration ships every IC drop-ship order to the address that IC has
on file for the `shipTo` code on the backend (`F17`).

If BAF wants IC to ship directly to the end customer's address, that
requires either:

- A dedicated IC delivery method that accepts an address override —
  ask IC's API team whether that exists on your account, or
- Per-customer IC `shipTo` codes that map to each end-customer address
  (unrealistic at scale).

Until this is clarified, drop-shipped IC parts route via BAF's own
address and BAF handles the last mile.

### 5.4. Sandbox vs production

The credentials shipped by IC are **production**. There's no sandbox
switch on the backend. `POST /ic/sales/requisition` on a confirmed PO
places a **real order** with IC. Test carefully with low-value SKUs.

The read-only paths (catalogue, pricing, stock, customer, finances,
search) are safe to hit as often as needed.

### 5.5. Polish-only fields

`deferredPayment` and KSeF metadata are only meaningful on IC's Polish
market. The backend has a **Market** dropdown; keep it on `Germany
(DE)` and these fields are ignored automatically.

---

## 6. Troubleshooting

**"Inter Cars token URL is not configured"** — Backend field is empty.
Set to `https://is.webapi.intercars.eu/oauth2/token`.

**"Inter Cars authentication failed (401)"** — Client ID / Secret
wrong or expired. Test the same values via IC's DevPortal or Postman
collection to isolate. Rotate via IC if needed.

**"Inter Cars reports ordering is not allowed"** — IC's `/finances`
endpoint returned `orderingAllowed=false`, usually because of overdue
balance. Log in to IC's customer portal, clear the overdue invoices,
then retry PO confirmation.

**Aftermarket block is empty on a product page that "should" have
alternatives** — Common causes:
1. Website aftermarket toggle is off (§2.5).
2. The product's public category has aftermarket disabled (§2.5).
3. The template has no `IC Seed SKU` (either the CSV import hasn't run
   yet, or auto-map didn't find a match). Set it manually (§4.3).
4. The local IC cache is empty. Run **Import ProductInformation CSV**.
5. IC's pricing / stock endpoints are unreachable — check the Odoo log
   and IC's status page.

**Shop cards render but "Add to cart" fails with "That aftermarket
alternative is no longer available"** — Cache expired between page
load and click. Refresh the product page and try again.

**"Ordering ICF206 Offset is not valid" on delivery/invoice
reconciliation** — Fixed in the current release; if you still see it,
the client is out of date. Ask your Odoo admin to `-u ic_intercars`.

**Duplicate `product.product` with the same IC SKU** — Shouldn't
happen (the lazy-create is idempotent on `ic_sku`), but if it does,
merge in Odoo's Products list and set `ic_sku` on the survivor.

---

## 7. Support

- Odoo module questions / bugs → BAF Odoo team.
- IC API questions (credentials, custom aggregations, KSeF, TecDoc) →
  IC API Support (email on the credentials sheet).
- Business questions (markup, category coverage, brand policy) → BAF
  purchasing team.

**Log locations for a bug report:**
- Odoo server log — the `INFO`/`WARNING` lines beginning with
  `IC ...` show every IC API call and every reconciliation step.
- PO chatter — every submission / confirmation is timestamped there.
- **Purchase → Configuration → IC Products (cache)** — the timestamp
  in the header shows when the CSV was last refreshed.

---

*Last updated: 2026-07-05.*
