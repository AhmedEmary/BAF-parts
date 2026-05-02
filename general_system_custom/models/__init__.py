from sys import dont_write_bytecode

# 1. Base / Core Models MUST load first
from . import baf_pricing
from . import baf_product_pricing
from . import partner
from . import auto_product
from . import discount_code
from . import baf_delivery_rule

# 2. Then load Order Lines and logic that depends on the base models
from . import sales_order_line
from . import sales
from . import purchase
from . import purchase_order_line
from . import stock_move
from . import stock_rule
from . import stock_picking
from . import import_so_lines
from . import delivery_list
from . import warehouse_checking
from . import warehouse_pallet
from . import account_move
from . import import_invoice_lines
from . import import_dropship
from . import credit_note_wizard
from . import down_payment_wizard
from . import base_document_layout
from . import corrispettivi_report
from . import corrispettivi_report_handler
from . import mass_vendor_wizard
from . import discount_import_wizard
from .import website
