from odoo import fields, models


class BostaCity(models.Model):
    _name = "bosta.city"
    _description = "Bosta City"
    _order = "name"

    name = fields.Char(required=True)
    name_ar = fields.Char(string="Arabic Name")
    code = fields.Char(help="Bosta city code sent as dropOffAddress.city, e.g. EG-01")
    bosta_id = fields.Char(string="Bosta ID", index=True)
    state_ids = fields.One2many("res.country.state", "bosta_city_id", string="Odoo Governorates")
    active = fields.Boolean(default=True)

    _bosta_id_uniq = models.Constraint("unique(bosta_id)", "This Bosta city is already imported.")
