import re
import secrets

from odoo import api, fields, models
from odoo.exceptions import UserError

from .bosta_api import TYPE_DELIVER, BostaClient, BostaError

EG_MOBILE = re.compile(r"^01[0125]\d{8}$")


def normalize_eg_phone(raw: str | None) -> str | None:
    """+20 10 1234 5678 / 0020101... / 01012345678 -> 01012345678. Returns None if not an Egyptian mobile."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0020"):
        digits = "0" + digits[4:]
    elif digits.startswith("20") and len(digits) == 12:
        digits = "0" + digits[2:]
    elif len(digits) == 10 and digits.startswith("1"):
        digits = "0" + digits
    return digits if EG_MOBILE.match(digits) else None


class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    delivery_type = fields.Selection(selection_add=[("bosta", "Bosta")], ondelete={"bosta": "set default"})
    bosta_api_key = fields.Char(string="Bosta API Key", groups="base.group_system", copy=False)
    bosta_api_version = fields.Char(string="Bosta API Version", default="v1", required=True)
    bosta_webhook_secret = fields.Char(
        string="Webhook Secret", groups="base.group_system", copy=False,
        default=lambda self: secrets.token_urlsafe(24),
        help="Put this secret in the webhook URL you register in the Bosta dashboard.",
    )
    bosta_webhook_url = fields.Char(compute="_compute_bosta_webhook_url", string="Webhook URL")
    bosta_allow_open_package = fields.Boolean(
        string="Allow Opening Package", help="Let the customer open the package before paying.")

    @api.depends("delivery_type")
    def _compute_supports_cash_on_delivery(self):
        super()._compute_supports_cash_on_delivery()
        for carrier in self.filtered(lambda c: c.delivery_type == "bosta"):
            carrier.supports_cash_on_delivery = True

    def _compute_bosta_webhook_url(self):
        base = self.get_base_url()
        for carrier in self:
            secret = carrier.sudo().bosta_webhook_secret if carrier.delivery_type == "bosta" else False
            carrier.bosta_webhook_url = f"{base}/bosta/webhook/{carrier.id}?secret={secret}" if secret else False

    # ------------------------------------------------------------------ helpers
    def _bosta_client(self) -> BostaClient:
        self.ensure_one()
        return BostaClient(
            self.sudo().bosta_api_key, production=self.prod_environment, api_version=self.bosta_api_version,
            logger=lambda text, label: self.log_xml(text, label),
        )

    def _bosta_payload(self, picking) -> dict:
        partner = picking.partner_id
        problems = []
        if partner.country_id and partner.country_id.code != "EG":
            problems.append(f"{partner.display_name} is not in Egypt")
        city = partner.state_id.bosta_city_id
        if not city:
            problems.append(
                f"governorate '{partner.state_id.name or 'missing'}' has no Bosta city "
                "(Inventory > Configuration > Bosta Cities)")
        phone = normalize_eg_phone(partner.phone)
        if not phone:
            problems.append(f"'{partner.phone or 'no phone'}' is not a valid Egyptian mobile number")
        if not partner.street:
            problems.append("street address is missing")
        if problems:
            raise UserError(self.env._("Cannot book %(picking)s with Bosta: %(why)s.",
                                       picking=picking.name, why="; ".join(problems)))
        first, _, last = (partner.name or "").strip().partition(" ")
        items = int(sum(picking.move_ids.mapped("quantity"))) or 1
        description = ", ".join(
            f"{m.product_id.display_name} x{int(m.quantity)}" for m in picking.move_ids if m.quantity)[:250]
        payload = {
            "type": TYPE_DELIVER,
            "specs": {
                "packageDetails": {"itemsCount": items, "description": description},
                "weight": round(picking.shipping_weight or picking.weight or 0.0, 2) or None,
            },
            "cod": round(picking.bosta_cod_amount, 2),
            "dropOffAddress": {
                "cityCode": city.code or city.name,
                "firstLine": partner.street,
                **({"secondLine": partner.street2} if partner.street2 else {}),
                **({"district": partner.city} if partner.city else {}),
            },
            "receiver": {
                "firstName": first or partner.name,
                "lastName": last or first,
                "phone": phone,
                **({"email": partner.email} if partner.email else {}),
            },
            "businessReference": picking.bosta_reference(),
            "notes": (picking.note and re.sub(r"<[^>]+>", " ", str(picking.note)).strip()) or None,
            "allowToOpenPackage": self.bosta_allow_open_package,
        }
        payload["specs"] = {k: v for k, v in payload["specs"].items() if v is not None}
        return {k: v for k, v in payload.items() if v is not None}

    # ------------------------------------------------------------------ provider API
    def bosta_rate_shipment(self, order):
        # Bosta prices come from the merchant's contract, not a public rate API.
        return {
            "success": True,
            "price": self.product_id.list_price,
            "error_message": False,
            "warning_message": False,
        }

    def bosta_send_shipping(self, pickings):
        results = []
        client = self._bosta_client()
        for picking in pickings:
            payload = self._bosta_payload(picking)
            try:
                data = client.create_delivery(payload)
            except BostaError as e:
                raise UserError(self.env._("Bosta rejected %(p)s: %(e)s", p=picking.name, e=e)) from e
            picking.write({
                "bosta_delivery_id": data.get("_id"),
                "bosta_state": data.get("state", {}).get("value") if isinstance(data.get("state"), dict)
                else "Pickup requested",
                "bosta_state_date": fields.Datetime.now(),
            })
            picking.message_post(body=self.env._(
                "Bosta delivery created: %(tn)s (cash on delivery: %(cod)s EGP)",
                tn=data["trackingNumber"], cod=payload["cod"]))
            results.append({"exact_price": self.product_id.list_price, "tracking_number": data["trackingNumber"]})
        return results

    def bosta_get_tracking_link(self, picking):
        if self.tracking_url and picking.carrier_tracking_ref:
            return self.tracking_url.replace("<shipmenttrackingnumber>", picking.carrier_tracking_ref)
        return False

    def bosta_cancel_shipment(self, pickings):
        client = self._bosta_client()
        for picking in pickings:
            if not picking.bosta_delivery_id:
                raise UserError(self.env._("%s has no Bosta delivery to cancel.", picking.name))
            try:
                client.terminate(picking.bosta_delivery_id)
            except BostaError as e:
                raise UserError(self.env._("Bosta could not cancel %(p)s: %(e)s", p=picking.name, e=e)) from e
            picking.message_post(body=self.env._("Bosta delivery %s cancelled.", picking.carrier_tracking_ref))
            picking.write({"carrier_tracking_ref": False, "bosta_state": "Terminated",
                           "bosta_state_date": fields.Datetime.now()})

    def action_bosta_import_cities(self):
        self.ensure_one()
        try:
            cities = self._bosta_client().cities()
        except BostaError as e:
            raise UserError(str(e)) from e
        City = self.env["bosta.city"].with_context(active_test=False)
        created = 0
        for c in cities:
            bosta_id = c.get("_id")
            if not bosta_id:
                continue
            vals = {"name": c.get("name") or bosta_id, "name_ar": c.get("nameAr"),
                    "code": c.get("code"), "bosta_id": bosta_id, "active": True}
            existing = City.search([("bosta_id", "=", bosta_id)], limit=1)
            if existing:
                existing.write(vals)
            else:
                City.create(vals)
                created += 1
        self._bosta_auto_map_states()
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"message": self.env._("%(n)s Bosta cities imported (%(new)s new).",
                                             n=len(cities), new=created), "type": "success"},
        }

    def _bosta_auto_map_states(self):
        """Match Odoo's Egyptian governorates to Bosta cities by name; leave the rest for manual mapping."""
        cities = {c.name.strip().lower(): c for c in self.env["bosta.city"].search([])}
        states = self.env["res.country.state"].search([("country_id.code", "=", "EG"), ("bosta_city_id", "=", False)])
        for state in states:
            city = cities.get(state.name.strip().lower())
            if city:
                state.bosta_city_id = city
