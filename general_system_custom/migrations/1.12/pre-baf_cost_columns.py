def migrate(cr, version):
    """Create the line-cost columns before Odoo would.

    Odoo schedules a computation pass only for columns it creates itself, so
    pre-creating these keeps every existing sale order line at cost 0 instead
    of recosting it at today's vendor prices. Existing lines are marked
    'legacy' so the views suppress their stale margin without rewriting it.
    """
    cr.execute("""
        ALTER TABLE sale_order_line
            ADD COLUMN IF NOT EXISTS baf_cost_status varchar
    """)
    cr.execute("""
        UPDATE sale_order_line SET baf_cost_status = 'legacy'
         WHERE baf_cost_status IS NULL
    """)
