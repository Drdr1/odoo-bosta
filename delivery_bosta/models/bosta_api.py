"""Thin client for the Bosta REST API.

Endpoints and field names follow Bosta's official SDKs (bostaapp/bosta-nodejs, bostaSDK on PyPI):
  base       https://app.bosta.co (production) / https://stg-app.bosta.co (staging)
  auth       `Authorization: <api key>` header (no "Bearer" prefix)
  POST   /api/v1/deliveries                 create  -> data._id, data.trackingNumber
  GET    /api/v1/deliveries/<tracking>/tracking
  DELETE /api/v1/deliveries/<id>            terminate (cancel)
  GET    /api/v1/cities
"""

from __future__ import annotations

import logging

import requests

_logger = logging.getLogger(__name__)

PRODUCTION = "https://app.bosta.co"
STAGING = "https://stg-app.bosta.co"
TIMEOUT = 30

# Bosta delivery type codes (SDK `deliveryTypes`)
TYPE_DELIVER = 10
TYPE_CASH_COLLECTION = 15

# Final states: once reached, the shipment is no longer polled (SDK `deliveryStates`)
FINAL_STATES = {
    "Delivered", "Delivery confirmed", "Returned to business", "Terminated",
    "Canceled", "Lost", "Damaged",
}


class BostaError(Exception):
    pass


class BostaClient:
    def __init__(self, api_key: str, production: bool, api_version: str = "v1", logger=None):
        if not api_key:
            raise BostaError("Bosta API key is not set on the delivery method.")
        self.base = f"{PRODUCTION if production else STAGING}/api/{api_version}"
        self.api_key = api_key
        self.log = logger  # callable(text, label) used for Odoo's debug logging

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base}/{path.lstrip('/')}"
        if self.log:
            self.log(f"{method} {url}\n{payload}", "bosta_request")
        try:
            resp = requests.request(
                method, url, json=payload, timeout=TIMEOUT,
                headers={"Authorization": self.api_key, "X-Requested-By": "odoo-delivery_bosta"},
            )
        except requests.RequestException as e:
            raise BostaError(f"Could not reach Bosta: {e}") from e
        try:
            body = resp.json()
        except ValueError:
            body = {"message": resp.text[:500]}
        if self.log:
            self.log(f"{resp.status_code}\n{body}", "bosta_response")
        if resp.status_code >= 400 or body.get("success") is False:
            raise BostaError(f"Bosta returned {resp.status_code}: {body.get('message') or body}")
        return body

    def create_delivery(self, payload: dict) -> dict:
        data = self._call("POST", "deliveries", payload).get("data") or {}
        if not data.get("trackingNumber"):
            raise BostaError(f"Bosta accepted the request but returned no tracking number: {data}")
        return data

    def track(self, tracking_number: str) -> dict:
        return self._call("GET", f"deliveries/{tracking_number}/tracking").get("data") or {}

    def terminate(self, delivery_id: str) -> None:
        self._call("DELETE", f"deliveries/{delivery_id}")

    def cities(self) -> list[dict]:
        data = self._call("GET", "cities").get("data")
        if isinstance(data, dict):  # some API versions wrap the list
            data = data.get("list") or data.get("cities") or []
        return data or []


def state_of(tracking: dict) -> str | None:
    """Pull a human-readable state out of a tracking/webhook payload, whichever shape it has."""
    state = tracking.get("state") or tracking.get("currentState")
    if isinstance(state, dict):
        state = state.get("value") or state.get("name") or state.get("code")
    return str(state) if state not in (None, "") else None
