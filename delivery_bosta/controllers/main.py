import hmac
import json
import logging

from odoo import http
from odoo.http import request

from ..models.bosta_api import state_of

_logger = logging.getLogger(__name__)


class BostaWebhook(http.Controller):
    """Bosta POSTs a JSON body on every state change. Bosta webhooks carry no signature,
    so the URL holds a per-carrier secret, compared in constant time."""

    @http.route("/bosta/webhook/<int:carrier_id>", type="http", auth="public", methods=["POST"], csrf=False)
    def webhook(self, carrier_id, secret=None, **kw):
        env = request.env(su=True)
        carrier = env["delivery.carrier"].browse(carrier_id).exists()
        expected = carrier.bosta_webhook_secret if carrier and carrier.delivery_type == "bosta" else None
        if not expected or not secret or not hmac.compare_digest(str(secret), expected):
            return request.make_json_response({"ok": False}, status=403)
        try:
            body = json.loads(request.httprequest.get_data() or b"{}")
        except ValueError:
            return request.make_json_response({"ok": False, "error": "invalid json"}, status=400)

        tracking = str(body.get("trackingNumber") or "")
        reference = body.get("businessReference")
        domain = [("carrier_id", "=", carrier.id)]
        if tracking:
            picking = env["stock.picking"].search(domain + [("carrier_tracking_ref", "=", tracking)], limit=1)
        else:
            picking = env["stock.picking"].browse()
        if not picking and reference:
            candidates = env["stock.picking"].search(domain + [("carrier_tracking_ref", "!=", False)])
            picking = candidates.filtered(lambda p: p.bosta_reference() == reference)[:1]
        if not picking:
            _logger.info("Bosta webhook for unknown shipment %s / %s", tracking, reference)
            return request.make_json_response({"ok": True, "matched": False})

        changed = picking._bosta_apply_state(state_of(body), "webhook")
        return request.make_json_response({"ok": True, "matched": True, "changed": changed})
