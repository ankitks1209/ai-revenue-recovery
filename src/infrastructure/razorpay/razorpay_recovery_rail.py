"""Milestone 5.2 — Razorpay Payment Link recovery rail behind PaymentRailPort."""

from __future__ import annotations

import hashlib
from typing import Optional

import httpx

from src import config
from src.domain.models import RailResponse
from src.infrastructure.ports import PaymentRailPort


_DEFAULT_BASE_URL = "https://api.razorpay.com/v1"
_REDACTED = "***"


def _reference_id(txn_id: str, attempt_number: int) -> str:
    base = f"{txn_id}_{attempt_number}"
    # Razorpay reference_id is typically limited; keep deterministic and short
    if len(base) <= 40:
        return base
    # hash long ids deterministically to fit limit, keep attempt suffix readable
    h = hashlib.sha256(txn_id.encode()).hexdigest()[:16]
    suffix = f"_{attempt_number}"
    # ensure total <=40
    keep = 40 - len(suffix) - len(h) - 1
    if keep > 0:
        # truncate txn prefix before hashing? keep hashed core
        return f"{txn_id[:keep]}_{h}{suffix}"
    return f"{h}{suffix}"


class RazorpayRecoveryRail(PaymentRailPort):
    """Test Mode Payment Link adapter implementing PaymentRailPort.

    - POST /v1/payment_links with HTTP Basic Auth (key_id/key_secret).
    - Amount converted to paise (smallest INR unit).
    - Currency INR unless caller semantics require otherwise.
    - Deterministic reference_id from (txn_id, attempt_number) for idempotency;
      duplicate reference_id conflict is handled deterministically as
      idempotent success.
    - RETRY/DUNNING map to Payment Link creation; REAUTH is explicitly
      not supported via this rail (returns deterministic failure).
    - Success means "recovery action (Payment Link) created" — NOT
      "customer has paid" / RECOVERED. Distinction is explicit.
    - Never logs key_secret, Authorization header, or raw provider payload.
    - All network/timeout/non-2xx/provider failures map to RailResponse(success=False).
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
        return f"RazorpayRecoveryRail(base_url={self._base_url!r}, key_id={self._key_id!r}, key_secret={_REDACTED!r})"

    __str__ = __repr__

    def execute_attempt(self, txn_id: str, amount: float, action_type: str, attempt_number: int) -> RailResponse:
        # REAUTH is not silently mapped to Payment Link
        at = (action_type or "").lower()
        if "re-auth" in at or "reauth" in at:
            return RailResponse(
                success=False,
                error_message="REAUTH not supported via Payment Link recovery rail — requires mandate re-authorization flow",
                gateway_reference=txn_id,
            )

        url = f"{self._base_url}/payment_links"
        auth = (self._key_id, self._key_secret)
        reference_id = _reference_id(txn_id, attempt_number)
        try:
            amount_paise = int(round(float(amount) * 100))
        except Exception:
            return RailResponse(success=False, error_message="Invalid amount for Payment Link", gateway_reference=txn_id)
        if amount_paise <= 0:
            return RailResponse(success=False, error_message="Invalid amount for Payment Link", gateway_reference=txn_id)

        body = {
            "amount": amount_paise,
            "currency": "INR",
            "reference_id": reference_id,
            "description": f"Recovery for {txn_id} attempt {attempt_number}",
            "notes": {"txn_id": txn_id, "attempt_number": str(attempt_number), "action_type": action_type},
            "notify": {"sms": False, "email": False},
        }

        client = self._client
        created = False
        if client is None:
            client = httpx.Client(timeout=self._timeout, auth=auth)
            created = True

        try:
            try:
                response = client.post(url, json=body, auth=auth, timeout=self._timeout)
            except httpx.TimeoutException:
                return RailResponse(success=False, error_message="Razorpay rail timeout", gateway_reference=txn_id)
            except httpx.RequestError:
                return RailResponse(success=False, error_message="Razorpay rail network error", gateway_reference=txn_id)
            except Exception:
                return RailResponse(success=False, error_message="Razorpay rail error", gateway_reference=txn_id)

            if 200 <= response.status_code < 300:
                try:
                    data = response.json()
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                # Razorpay returns id like plink_... and short_url
                link_id = data.get("id") if isinstance(data.get("id"), str) else None
                short_url = data.get("short_url") if isinstance(data.get("short_url"), str) else None
                gateway_ref = short_url or link_id or reference_id
                # success means recovery action created, not payment recovered
                return RailResponse(success=True, gateway_reference=gateway_ref)

            # non-2xx
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
            msg_str = str(msg)
            lower = msg_str.lower()

            # --- strict duplicate idempotency ---
            # Only treat as idempotent success when response proves the SAME
            # deterministic reference_id already exists.
            has_duplicate_signal = False
            if "already" in lower or "exist" in lower or "duplicate" in lower or "conflict" in lower:
                has_duplicate_signal = True

            if has_duplicate_signal:
                # collect any reference_id present in the payload (deep search)
                def _collect_refs(obj):
                    refs: list[str] = []
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            if k == "reference_id" and isinstance(v, str):
                                refs.append(v)
                            if isinstance(v, (dict, list)):
                                refs.extend(_collect_refs(v))
                    elif isinstance(obj, list):
                        for it in obj:
                            refs.extend(_collect_refs(it))
                    return refs

                candidate_refs = _collect_refs(data)
                # also check if provider echoed reference_id inside description text
                has_matching_ref = reference_id in candidate_refs or reference_id in msg_str
                if has_matching_ref:
                    # prefer real provider link evidence if present
                    short_url = None
                    link_id = None
                    if isinstance(data.get("short_url"), str):
                        short_url = data.get("short_url")
                    if isinstance(data.get("id"), str):
                        link_id = data.get("id")
                    if isinstance(err_obj, dict):
                        if not short_url and isinstance(err_obj.get("short_url"), str):
                            short_url = err_obj.get("short_url")
                        if not link_id and isinstance(err_obj.get("id"), str):
                            link_id = err_obj.get("id")
                        # nested error data
                        err_data = err_obj.get("data")
                        if isinstance(err_data, dict):
                            if not short_url and isinstance(err_data.get("short_url"), str):
                                short_url = err_data.get("short_url")
                            if not link_id and isinstance(err_data.get("id"), str):
                                link_id = err_data.get("id")
                    gateway_ref = short_url or link_id or reference_id
                    return RailResponse(success=True, gateway_reference=gateway_ref)
                # insufficient evidence -> fall through to truthful failure

            pid = data.get("id")
            gateway_ref = pid if isinstance(pid, str) and pid else txn_id
            # ensure secret never leaks
            if self._key_secret and self._key_secret in msg_str:
                msg_str = msg_str.replace(self._key_secret, _REDACTED)
            return RailResponse(success=False, error_message=msg_str, gateway_reference=gateway_ref)
        finally:
            if created:
                try:
                    client.close()
                except Exception:
                    pass
