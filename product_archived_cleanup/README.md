# Product Archived Cleanup

Background scheduled action that permanently deletes archived
(`active = False`) `product.template` records that are no longer referenced
by any transactional document.

The job is **bounded per run** (wall-clock time budget) and **self-resuming**:
on each tick it pre-filters out variants that are still referenced by foreign
keys (invoices, sale order lines, stock moves, ...), deletes the deletable
templates in chunks via the ORM `unlink()`, commits after each chunk, and stops
when the time budget is hit. The next cron tick picks up where it left off.
Once nothing deletable remains the run is a no-op.

## Install

1. Drop the `product_archived_cleanup/` folder into the custom addons path
   (it ships alongside the other BAF custom modules).
2. Update the apps list and install **Product Archived Cleanup**.

The cron ships **disabled** and must be enabled deliberately.

## Enable

- UI: Settings → Technical → Scheduled Actions → *Cleanup archived products*
  → tick **Active**.
- Or, for a one-off purge from `odoo shell`:

  ```python
  env['product.template']._cron_cleanup_archived()
  env.cr.commit()
  ```

## Tune

Two system parameters control the run shape (Settings → Technical → System
Parameters):

| Key                                            | Default | Meaning                                     |
| ---------------------------------------------- | ------- | ------------------------------------------- |
| `product_archived_cleanup.batch_size`          | `1000`  | Templates `unlink`ed per chunk / commit     |
| `product_archived_cleanup.time_budget`         | `240`   | Wall-clock seconds before the run stops     |

Both are read on every cron tick, no restart needed.

## Notes

- Products referenced by transactional documents (invoices, SO/PO, stock
  moves, ...) are intentionally left archived. The FK pre-filter excludes
  them so the job never retries them and can correctly detect "nothing left".
- The module uses the ORM `unlink()` only — no raw `DELETE`/`TRUNCATE`
  against `product_product` / `product_template`. Bypassing the ORM here
  orphans `ir_model_data`, `ir_attachment`, `ir_property`,
  `mail_message`/`mail_followers`, `product_supplierinfo`, etc.
- Test on a staging branch loaded from a production dump first; the cron
  logs deletable vs. blocked counts so you can compare before flipping it on
  in production.

## Uninstall

Once the one-time purge is done, disable the cron (or uninstall the module).
The `ir.config_parameter` rows are kept (`noupdate="1"`) and can be removed
manually if desired.