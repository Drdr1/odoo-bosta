from odoo import fields, models


class ResCountryState(models.Model):
    _inherit = "res.country.state"

    bosta_city_id = fields.Many2one(
        "bosta.city", string="Bosta City",
        help="Which Bosta city deliveries to this governorate are booked under.",
    )
