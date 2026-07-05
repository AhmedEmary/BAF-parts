{
    'name': "Inter Cars Integration",
    'summary': "Inter Cars (IC S.A.) REST API integration for BAF: "
               "credentials backend, live pricing/inventory, drop-ship "
               "requisitions, delivery/invoice reconciliation.",
    'category': 'Purchases/Purchase',
    'version': '1.0',
    'depends': [
        'base',
        'product',
        'purchase',
        'stock',
        'stock_dropshipping',
        'general_system_custom',
    ],
    'installable': True,
    'data': [
        'security/ir.model.access.csv',
        # Load the CSV wizard first so ic_backend_views.xml can
        # reference its action by xmlid.
        'views/ic_csv_import_wizard_views.xml',
        'views/ic_catalog_search_wizard_views.xml',
        'views/ic_backend_views.xml',
        'views/purchase_order_views.xml',
        'views/product_views.xml',
        'data/product_ribbon.xml',
        'data/ir_cron.xml',
    ],
    'author': 'Ahmed Elamery',
    'license': 'LGPL-3',
}
