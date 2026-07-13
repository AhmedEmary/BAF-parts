# BAF B2B Website (static export)

Reproduces the BAF Parts B2B website. Pages (in menu order):
- /b2b-baf   -> Home
- /b2b       -> B2B (order tool; static markup only until wired up)
- /pricefile -> Pricefile
- /uber-uns  -> Über Uns
- /contactus -> Kontakt
- /hilfe     -> Hilfe

## Install
1. Copy the `baf_b2b_website` folder into the target Odoo instance's addons path.
2. Restart Odoo.
3. Enable Developer Mode, Apps -> Update Apps List, search "BAF B2B Website", Install.

## Notes
- Header/footer use the standard `website.layout` (target DB theme).
- Each page's original CSS is inlined per template for a 1:1 look.
- The /b2b order tool is captured as static markup; its JS/data must be wired up
  when you make the site dynamic.
