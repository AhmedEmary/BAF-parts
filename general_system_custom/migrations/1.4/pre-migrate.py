"""Pre-create and bulk-fill product_template.baf_sales_column_key.

baf_sales_column_key is a new *stored computed* field. When Odoo's _auto_init
first creates its column it schedules a per-row recompute of EVERY product
(SELECT id FROM product_template -> add_to_compute), which on a large catalogue
(~1.9M rows) stalls the whole upgrade before b2b_custom can even load.

Creating and filling the column here, in one set-based SQL statement, means the
column already exists when _auto_init runs, so field.update_db returns new=False
and the mass recompute is skipped. Products edited later recompute normally.

The fill mirrors _compute_baf_column_key:
  - type-split families (bmw_mini) keep a per-brand + type sales column, which
    equals the (brand-based) purchase key baf_column_key (BMW_T12, MINI_T39);
  - other families share one column keyed by the family's normalized name;
  - a product with no brand/family falls back to its brand-based key.
"""


def migrate(cr, version):
    cr.execute("""
        ALTER TABLE product_template
        ADD COLUMN IF NOT EXISTS baf_sales_column_key varchar
    """)
    cr.execute(r"""
        UPDATE product_template pt
        SET baf_sales_column_key = CASE
            WHEN pt.baf_brand_family = 'bmw_mini' THEN pt.baf_column_key
            WHEN src.base IS NOT NULL AND src.base <> '' THEN src.base
            ELSE pt.baf_column_key
        END
        FROM (
            SELECT p.id AS pid,
                   trim(both '_' from
                        regexp_replace(upper(bf.name), '[-_/[:space:]]+', '_', 'g')) AS base
            FROM product_template p
            LEFT JOIN product_brand pb ON pb.id = p.brand
            LEFT JOIN baf_brand_family bf ON bf.id = pb.family_id
        ) src
        WHERE src.pid = pt.id
    """)
