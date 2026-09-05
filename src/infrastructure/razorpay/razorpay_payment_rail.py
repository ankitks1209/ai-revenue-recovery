"""Milestone 5 — Razorpay Test Mode adapter foundation behind PaymentRailPort."""
from __future__ import annotations

import base64  # noqa: used in tests to verify auth; kept for explicitness
from typing import Optional

import httpx

from src.domain.models import RailResponse
from src.infrastructure.ports import PaymentRailPort
from src import config


_DEFAULT_BASE_URL = "https://api.razorpay.com/v1"
_REDACTED = "***"


class RazorpayPaymentRail(PaymentRailPort):
    """Test Mode adapter implementing PaymentRailPort.

    - Base URL defaults to Razorpay Test Mode (https://api.razorpay.com/v1).
    - Authenticates via HTTP Basic Auth (key_id / key_secret).
    - Credentials read from config when not passed explicitly; never hard-coded.
    - Never logs key_secret, Authorization header, webhook secret, or raw payload.
    - Maps all provider/network failures to RailResponse(success=False, ...).
    - Truthful, inspection-only semantics: this foundation performs ONLY
      GET /v1/payments/{txn_id} and NEVER claims to have executed a recovery
      action. No retry/order/capture endpoint is invoked yet. Every 2xx
      response therefore maps to success=False — even status=captured means
      "already captured, no retry executed here" (observed state, not caused
      recovery). Real retry / order / capture is deferred to next milestone.
    """

    def __init__(
        self,
        key_id: Optional[str] = None,
        key_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._key_id = key_id if key_id is not None else config.RAZORPAY_KEY_ID
        self._key_secret = key_secret if key_secret is not None else config.RAZORPAY_KEY_SECRET
        self._base_url = (base_url if base_url is not None else config.RAZORPAY_BASE_URL).rstrip("/")
        if not self._base_url:
            self._base_url = _DEFAULT_BASE_URL
        self._timeout = float(timeout)
        self._client = client

        if not self._key_id or not self._key_secret:
            raise ValueError("Missing Razorpay credentials: RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set")

    def __repr__(self) -> str:
        return f"RazorpayPaymentRail(base_url={self._base_url!r}, key_id={self._key_id!r}, key_secret={_REDACTED!r})"

    __str__ = __repr__

    def execute_attempt(self, txn_id: str, amount: float, action_type: str, attempt_number: int) -> RailResponse:
        url = f"{self._base_url}/payments/{txn_id}"
        auth = (self._key_id, self._key_secret)

        client = self._client
        created = False
        if client is None:
            client = httpx.Client(timeout=self._timeout, auth=auth)
            created = True

        try:
            try:
                response = client.get(url, auth=auth, timeout=self._timeout)
            except httpx.TimeoutException:
                return RailResponse(success=False, error_message="Razorpay rail timeout", gateway_reference=txn_id)
            except httpx.RequestError:
                return RailResponse(success=False, error_message="Razorpay rail network error", gateway_reference=txn_id)
            except Exception:
                return RailResponse(success=False, error_message="Razorpay rail error", gateway_reference=txn_id)

            # 2xx -> inspect provider payload
            if 200 <= response.status_code < 300:
                try:
                    data = response.json()
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                payment_id = data.get("id")
                status = data.get("status")
                gateway_ref = payment_id if isinstance(payment_id, str) and payment_id else txn_id

                if status == "captured":
                    return RailResponse(
                        success=False,
                        error_message="Razorpay: payment already captured — inspection only, no retry executed (adapter foundation: no failed-payment retry endpoint)",
                        gateway_reference=gateway_ref,
                    )

                if status == "authorized":
                    return RailResponse(
                        success=False,
                        error_message="Razorpay: payment authorized but not captured",
                        gateway_reference=gateway_ref,
                    )

                # provider-declared failure or other non-captured status
                # try error fields, fall back to status-based message
                err_code = data.get("error_code")
                err_desc = data.get("error_description")
                # nested error object shape used by Razorpay error responses
                err_obj = data.get("error")
                if isinstance(err_obj, dict):
                    err_code = err_code or err_obj.get("code")
                    err_desc = err_desc or err_obj.get("description")

                msg = err_desc or err_code
                if not msg:
                    if status:
                        msg = f"Razorpay: payment status {status}"
                    else:
                        msg = "Razorpay: payment not capturable"
                return RailResponse(success=False, error_message=str(msg), gateway_reference=gateway_ref)

            # non-2xx -> provider error
            try:
                data = response.json()
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            err_obj = data.get("error")
            msg = None
            if isinstance(err_obj, dict):
                msg = err_obj.get("description") or err_obj.get("code")
            if not msg:
                msg = data.get("error_description") or data.get("error_code")
            if not msg:
                msg = f"Razorpay error: HTTP {response.status_code}"

            # gateway reference may still carry payment id even on error
            gateway_ref = txn_id
            pid = data.get("id")
            if isinstance(pid, str) and pid:
                gateway_ref = pid
            return RailResponse(success=False, error_message=str(msg), gateway_reference=gateway_ref)

        finally:
            if created:
                try:
                    client.close()
                except Exception:
                    pass
