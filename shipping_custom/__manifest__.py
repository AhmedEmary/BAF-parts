{
    'name': 'BAF Shipping Integration',
    'version': '1.0',
    'category': 'Inventory/Delivery',
    'summary': 'FedEx and DHL shipping integration on Stock Pickings using stock.package.type',
    'description': """
        Adds a Shipping Selection tab on Stock Pickings, fetches rates from FedEx/DHL,
        and generates labels. Uses Odoo native stock.package.type as the pallet/packaging source.
    """,
    'author': 'Ahmed Elamery',
    'depends': [
        'base',
        'mail',
        'stock',
        'sale_stock',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_sequence.xml',
        'views/shipping_delivery_order_view.xml',
        'wizard/shipping_delivery_order_preview_wizard_view.xml',
        'views/stock_picking_view.xml',
        'views/res_config_settings_views.xml',
    ],
    'installable': True,
    'license': 'LGPL-3',
}