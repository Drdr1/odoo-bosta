import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.delivery_carrier import normalize_eg_phone

REQUEST = "odoo.addons.delivery_bosta.models.bosta_api.requests.request"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeBosta:
    """Records every call; answers like Bosta's API does (shape from the official SDKs)."""

    def __init__(self):
        self.calls, self.n = [], 0
        self.track_state = "Delivering"
        self.fail_next = None

    def __call__(self, method, url, json=None, timeout=None, headers=None):
        self.calls.append({"method": method, "url": url, "json": json, "headers": headers})
        if self.fail_next:
            status, body, self.fail_next = *self.fail_next, None
            return FakeResponse(status, body)
        if method == "POST" and url.endswith("/deliveries"):
            self.n += 1
            return FakeResponse(200, {"success": True, "data": {
                "_id": f"del-{self.n}", "trackingNumber": f"7{self.n:07d}", "message": "created"}})
        if method == "GET" and url.endswith("/tracking"):
            return FakeResponse(200, {"success": True, "data": {"state": {"value": self.track_state}}})
        if method == "DELETE":
            return FakeResponse(200, {"success": True, "message": "terminated"})
        if url.endswith("/cities"):
            return FakeResponse(200, {"success": True, "data": [
                {"_id": "c1", "name": "Cairo", "nameAr": "القاهرة", "code": "EG-01"},
                {"_id": "c2", "name": "Giza", "nameAr": "الجيزة", "code": "EG-25"},
            ]})
        return FakeResponse(404, {"success": False, "message": "not found"})

    def last(self, method):
        return next(c for c in reversed(self.calls) if c["method"] == method)


class BostaCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.egypt = cls.env.ref("base.eg")
        cls.cairo = cls.env.ref("base.state_eg_c")
        cls.alex = cls.env.ref("base.state_eg_alx")
        cls.ship_product = cls.env["product.product"].create(
            {"name": "Bosta delivery", "type": "service", "list_price": 55.0})
        cls.carrier = cls.env["delivery.carrier"].create({
            "name": "Bosta", "delivery_type": "bosta", "product_id": cls.ship_product.id,
            "bosta_api_key": "test-key", "integration_level": "rate_and_ship",
            "tracking_url": "https://example.test/track/<shipmenttrackingnumber>",
        })
        cls.customer = cls.env["res.partner"].create({
            "name": "Mona Adel Hassan", "street": "12 Road 9", "street2": "Flat 4", "city": "Maadi",
            "state_id": cls.cairo.id, "country_id": cls.egypt.id, "phone": "+20 10 1234 5678",
            "email": "mona@example.com",
        })
        cls.product = cls.env["product.product"].create(
            {"name": "Desk Lamp", "is_storable": True, "list_price": 450.0, "weight": 1.2})
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.env.company.id)], limit=1)
        cls.env["stock.quant"]._update_available_quantity(cls.product, cls.warehouse.lot_stock_id, 20)

    def setUp(self):
        super().setUp()
        self.bosta = FakeBosta()
        patcher = patch(REQUEST, self.bosta)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _import_cities(self):
        self.carrier.action_bosta_import_cities()

    def _order(self, qty=2, partner=None):
        so = self.env["sale.order"].create({
            "partner_id": (partner or self.customer).id,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": qty})],
        })
        so.set_delivery_line(self.carrier, 55.0)
        so.action_confirm()
        return so

    def _ship(self, so):
        picking = so.picking_ids
        picking.move_ids.quantity = picking.move_ids.product_uom_qty
        picking.button_validate()
        return picking


@tagged("post_install", "-at_install", "bosta")
class TestBostaShipping(BostaCase):
    def test_phone_normalisation(self):
        self.assertEqual(normalize_eg_phone("+20 10 1234 5678"), "01012345678")
        self.assertEqual(normalize_eg_phone("00201112345678"), "01112345678")
        self.assertEqual(normalize_eg_phone("1512345678"), "01512345678")
        self.assertIsNone(normalize_eg_phone("0225551234"))  # landline: Bosta needs a mobile
        self.assertIsNone(normalize_eg_phone(None))

    def test_webhook_url_is_shown_on_the_carrier(self):
        """Regression: computing this field crashed the carrier form on Odoo 20."""
        self.assertIn(f"/bosta/webhook/{self.carrier.id}?secret=", self.carrier.bosta_webhook_url)
        self.assertTrue(self.carrier.bosta_webhook_url.startswith(self.carrier.get_base_url()))

    def test_city_import_maps_governorates_by_name(self):
        self._import_cities()
        self.assertEqual(self.cairo.bosta_city_id.code, "EG-01")
        self.assertFalse(self.alex.bosta_city_id)  # not returned by Bosta: left for manual mapping
        self._import_cities()  # re-import updates, no duplicates
        self.assertEqual(self.env["bosta.city"].search_count([]), 2)

    def test_validating_delivery_books_bosta_with_cod(self):
        self._import_cities()
        so = self._order(qty=2)
        picking = self._ship(so)
        self.assertEqual(picking.carrier_tracking_ref, "70000001")
        self.assertEqual(picking.bosta_delivery_id, "del-1")
        sent = self.bosta.last("POST")
        self.assertEqual(sent["headers"]["Authorization"], "test-key")
        self.assertTrue(sent["url"].startswith("https://stg-app.bosta.co/api/v1/"))  # test mode -> staging
        p = sent["json"]
        self.assertEqual(p["type"], 10)
        self.assertEqual(p["cod"], round(so.amount_total, 2))  # nothing paid yet: collect everything
        self.assertEqual(p["dropOffAddress"]["cityCode"], "EG-01")
        self.assertEqual(p["dropOffAddress"]["firstLine"], "12 Road 9")
        self.assertEqual(p["dropOffAddress"]["district"], "Maadi")
        self.assertEqual(p["receiver"]["phone"], "01012345678")
        self.assertEqual((p["receiver"]["firstName"], p["receiver"]["lastName"]), ("Mona", "Adel Hassan"))
        self.assertEqual(p["specs"]["packageDetails"]["itemsCount"], 2)
        self.assertEqual(p["businessReference"], f"{so.name}/{picking.name}")
        self.assertTrue(any("Bosta delivery created: 70000001" in m.body for m in picking.message_ids))
        self.assertEqual(picking.carrier_tracking_url, "https://example.test/track/70000001")

    def test_production_toggle_uses_production_host(self):
        self._import_cities()
        self.carrier.prod_environment = True
        self._ship(self._order())
        self.assertTrue(self.bosta.last("POST")["url"].startswith("https://app.bosta.co/api/v1/"))

    def test_cod_can_be_overridden_before_booking(self):
        self._import_cities()
        so = self._order()
        picking = so.picking_ids
        picking.bosta_cod_amount = 0.0  # e.g. paid by card on Shopify
        self._ship(so)
        self.assertEqual(self.bosta.last("POST")["json"]["cod"], 0.0)

    def test_missing_city_mapping_blocks_with_clear_message(self):
        self.customer.state_id = self.alex  # no Bosta city mapped
        so = self._order()
        with self.assertRaisesRegex(UserError, "Alexandria.*has no Bosta city"):
            self._ship(so)
        self.assertFalse([c for c in self.bosta.calls if c["method"] == "POST"])

    def test_landline_only_customer_is_rejected_before_calling_bosta(self):
        self._import_cities()
        self.customer.phone = "02 2555 1234"
        with self.assertRaisesRegex(UserError, "not a valid Egyptian mobile"):
            self._ship(self._order())
        self.assertFalse([c for c in self.bosta.calls if c["method"] == "POST"])

    def test_bosta_error_is_shown_and_nothing_is_marked_shipped(self):
        self._import_cities()
        self.bosta.fail_next = (400, {"success": False, "message": "Invalid city"})
        so = self._order()
        with self.assertRaisesRegex(UserError, "Invalid city"):
            self._ship(so)

    def test_cancel_terminates_on_bosta(self):
        self._import_cities()
        picking = self._ship(self._order())
        picking.cancel_shipment()
        self.assertTrue(self.bosta.last("DELETE")["url"].endswith("/deliveries/del-1"))
        self.assertFalse(picking.carrier_tracking_ref)
        self.assertEqual(picking.bosta_state, "Terminated")

    def test_cron_polls_open_shipments_and_stops_at_final_state(self):
        self._import_cities()
        picking = self._ship(self._order())
        self.bosta.track_state = "Delivering"
        self.env["stock.picking"]._cron_bosta_sync()
        self.assertEqual(picking.bosta_state, "Delivering")
        self.bosta.track_state = "Delivered"
        self.env["stock.picking"]._cron_bosta_sync()
        self.assertEqual(picking.bosta_state, "Delivered")
        calls = len(self.bosta.calls)
        self.assertEqual(self.env["stock.picking"]._cron_bosta_sync(), 0)  # delivered: no longer polled
        self.assertEqual(len(self.bosta.calls), calls)


@tagged("post_install", "-at_install", "bosta")
class TestBostaWebhook(HttpCase, BostaCase):
    def _post(self, body, secret):
        url = f"/bosta/webhook/{self.carrier.id}?secret={secret}"
        return self.url_open(url, data=json.dumps(body), headers={"Content-Type": "application/json"})

    def test_webhook_updates_status_by_tracking_number(self):
        self._import_cities()
        picking = self._ship(self._order())
        r = self._post({"trackingNumber": picking.carrier_tracking_ref, "state": "Out for delivery"},
                       self.carrier.bosta_webhook_secret)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "matched": True, "changed": True})
        self.assertEqual(picking.bosta_state, "Out for delivery")

    def test_webhook_falls_back_to_business_reference(self):
        self._import_cities()
        picking = self._ship(self._order())
        r = self._post({"businessReference": picking.bosta_reference(), "state": {"value": "Delivered"}},
                       self.carrier.bosta_webhook_secret)
        self.assertTrue(r.json()["matched"])
        self.assertEqual(picking.bosta_state, "Delivered")

    def test_webhook_rejects_wrong_secret(self):
        self._import_cities()
        picking = self._ship(self._order())
        r = self._post({"trackingNumber": picking.carrier_tracking_ref, "state": "Delivered"}, "guess")
        self.assertEqual(r.status_code, 403)
        self.assertNotEqual(picking.bosta_state, "Delivered")

    def test_webhook_for_unknown_shipment_is_acknowledged_not_failed(self):
        r = self._post({"trackingNumber": "999", "state": "Delivered"}, self.carrier.bosta_webhook_secret)
        self.assertEqual(r.json(), {"ok": True, "matched": False})
