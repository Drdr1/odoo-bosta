import logging

from odoo import api, fields, models

from .bosta_api import FINAL_STATES, BostaError, state_of

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = "stock.picking"

    bosta_delivery_id = fields.Char(string="Bosta Delivery ID", copy=False, readonly=True)
    bosta_state = fields.Char(string="Bosta Status", copy=False, readonly=True, tracking=True)
    bosta_state_date = fields.Datetime(string="Bosta Status Updated", copy=False, readonly=True)
    bosta_cod_amount = fields.Monetary(
        string="Cash on Delivery", currency_field="bosta_currency_id",
        compute="_compute_bosta_cod_amount", store=True, readonly=False, copy=False,
        help="Amount Bosta collects from the customer. Defaults to what is still unpaid on the sales order.",
    )
    bosta_currency_id = fields.Many2one(related="company_id.currency_id")
    is_bosta = fields.Boolean(compute="_compute_is_bosta")

    @api.depends("carrier_id")
    def _compute_is_bosta(self):
        for p in self:
            p.is_bosta = p.carrier_id.delivery_type == "bosta"

    @api.depends("sale_id.amount_unpaid", "carrier_id")
    def _compute_bosta_cod_amount(self):
        for p in self:
            if p.carrier_tracking_ref:  # already booked: never change the amount Bosta will collect
                continue
            p.bosta_cod_amount = p.sale_id.amount_unpaid if p.sale_id else 0.0

    def bosta_reference(self) -> str:
        """Stable reference Bosta stores with the delivery, used to match webhooks back to this picking."""
        self.ensure_one()
        return f"{self.sale_id.name}/{self.name}" if self.sale_id else self.name

    def _bosta_apply_state(self, state: str, source: str) -> bool:
        self.ensure_one()
        if not state or state == self.bosta_state:
            return False
        old = self.bosta_state
        self.write({"bosta_state": state, "bosta_state_date": fields.Datetime.now()})
        self.message_post(body=self.env._("Bosta status: %(old)s → %(new)s (%(src)s)",
                                          old=old or "-", new=state, src=source))
        return True

    @api.model
    def _cron_bosta_sync(self, limit=50):
        """Fallback when webhooks are missed: poll open Bosta shipments, oldest update first."""
        pickings = self.search([
            ("carrier_id.delivery_type", "=", "bosta"),
            ("carrier_tracking_ref", "!=", False),
            ("bosta_state", "not in", list(FINAL_STATES)),
        ], order="bosta_state_date asc", limit=limit)
        for picking in pickings:
            try:
                data = picking.carrier_id._bosta_client().track(picking.carrier_tracking_ref)
            except BostaError as e:
                _logger.warning("Bosta tracking failed for %s: %s", picking.name, e)
                continue
            picking._bosta_apply_state(state_of(data), "poll")
        return len(pickings)
