{
    "name": "Bosta Shipping (Egypt)",
    "version": "20.0.1.0.0",
    "category": "Inventory/Delivery",
    "summary": "Create Bosta deliveries with cash on delivery from Odoo, and keep their status in sync",
    "author": "Ahmed Darder (DerOps)",
    "website": "https://derops.dev",
    "license": "LGPL-3",
    "depends": ["stock_delivery", "sale_stock"],
    "data": [
        "security/ir.access.csv",
        "data/ir_cron.xml",
        "views/bosta_city_views.xml",
        "views/delivery_carrier_views.xml",
        "views/stock_picking_views.xml",
    ],
    "installable": True,
}
