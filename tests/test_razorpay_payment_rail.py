import base64

import httpx
import pytest

from src.infrastructure.razorpay.razorpay_payment_rail import RazorpayPaymentRail
from src.infrastructure.ports import PaymentRailPort

KEY_ID = "rzp_test_dummy_id"
KEY_SECRET = "rzp_test_dummy_secret"


def _client_with_handler(handler, auth=None):
    transport = httpx.MockTransport(handler)
    # pass auth explicitly to mirror adapter; transport handler can assert it
    return httpx.Client(transport=transport, auth=auth)


def test_base_url_and_path():
    captured = {}

    def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        captured["path"] = request.url.path
        return httpx.Response(200, json={"id": "pay_123", "status": "captured"})

    client = _client_with_handler(handler, auth=(KEY_ID, KEY_SECRET))
    rail = RazorpayPaymentRail(key_id=KEY_ID, key_secret=KEY_SECRET, base_url="https://api.razorpay.com/v1", client=client)
    resp = rail.execute_attempt("pay_123", 100.0, "retry", 1)
    # foundation is inspection-only: even captured must not claim executed recovery
    assert resp.success is False
    assert "inspection only" in resp.error_message.lower() or "no retry executed" in resp.error_message.lower()
    assert captured["url"] == "https://api.razorpay.com/v1/payments/pay_123"
    assert captured["path"] == "/v1/payments/pay_123"


def test_basic_auth_construction():
    captured = {}

    def handler(request: httpx.Request):
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"id": "pay_abc", "status": "captured"})

    client = _client_with_handler(handler)
    rail = RazorpayPaymentRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    rail.execute_attempt("pay_abc", 100.0, "retry", 1)
    expected = "Basic " + base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode()
    assert captured["auth"] == expected


def test_successful_provider_response_mapping():
    # even "captured" is inspection-only in the foundation — must not claim success
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"id": "pay_ok", "status": "captured"})

    client = _client_with_handler(handler)
    rail = RazorpayPaymentRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("pay_ok", 50.0, "retry", 1)
    assert resp.success is False
    assert "inspection only" in resp.error_message.lower() or "no retry executed" in resp.error_message.lower() or "already captured" in resp.error_message.lower()
    assert resp.gateway_reference == "pay_ok"
    assert isinstance(rail, PaymentRailPort)


def test_provider_declared_failure_mapping():
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"id": "pay_fail", "status": "failed", "error_code": "BAD_REQUEST_ERROR", "error_description": "Payment failed"})

    client = _client_with_handler(handler)
    rail = RazorpayPaymentRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("pay_fail", 50.0, "retry", 1)
    assert resp.success is False
    assert resp.gateway_reference == "pay_fail"
    assert "Payment failed" in resp.error_message or "BAD_REQUEST" in resp.error_message


def test_provider_authorized_maps_to_failure():
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"id": "pay_auth", "status": "authorized"})

    client = _client_with_handler(handler)
    rail = RazorpayPaymentRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("pay_auth", 50.0, "retry", 1)
    assert resp.success is False
    assert "authorized" in resp.error_message.lower()


def test_timeout_mapping():
    def handler(request: httpx.Request):
        raise httpx.ConnectTimeout("timeout")

    client = _client_with_handler(handler)
    rail = RazorpayPaymentRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("pay_to", 50.0, "retry", 1)
    assert resp.success is False
    assert "timeout" in resp.error_message.lower()
    assert resp.gateway_reference == "pay_to"


def test_network_exception_mapping():
    def handler(request: httpx.Request):
        raise httpx.ConnectError("network down")

    client = _client_with_handler(handler)
    rail = RazorpayPaymentRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("pay_net", 50.0, "retry", 1)
    assert resp.success is False
    assert "network" in resp.error_message.lower()


def test_non_2xx_mapping():
    def handler(request: httpx.Request):
        return httpx.Response(404, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "payment not found"}})

    client = _client_with_handler(handler)
    rail = RazorpayPaymentRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("pay_missing", 50.0, "retry", 1)
    assert resp.success is False
    assert "payment not found" in resp.error_message.lower() or "bad_request" in resp.error_message.lower()


def test_non_2xx_plain_text_fallback():
    def handler(request: httpx.Request):
        return httpx.Response(500, text="Internal Server Error")

    client = _client_with_handler(handler)
    rail = RazorpayPaymentRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("pay_500", 50.0, "retry", 1)
    assert resp.success is False
    assert "500" in resp.error_message


def test_missing_credentials_raises():
    with pytest.raises(ValueError, match="Missing Razorpay credentials"):
        RazorpayPaymentRail(key_id="", key_secret="", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))

    with pytest.raises(ValueError, match="Missing Razorpay credentials"):
        RazorpayPaymentRail(key_id=KEY_ID, key_secret="", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))

    # empty key_id also
    with pytest.raises(ValueError, match="Missing Razorpay credentials"):
        RazorpayPaymentRail(key_id="", key_secret=KEY_SECRET, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})) ))


def test_captured_does_not_imply_executed_retry():
    """Merely observing status=captured must not be reported as an executed retry success."""
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"id": "pay_cap", "status": "captured"})

    client = _client_with_handler(handler)
    rail = RazorpayPaymentRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("pay_cap", 75.0, "retry", 2)
    assert resp.success is False
    assert resp.error_message is not None
    msg = resp.error_message.lower()
    assert "inspection only" in msg
    assert "no retry executed" in msg
    # provider limitation must be documented in the message
    assert "no failed-payment retry endpoint" in msg or "adapter foundation" in msg
    assert KEY_SECRET not in resp.error_message


def test_inspection_only_captured_still_reports_gateway_ref():
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"id": "pay_cap2", "status": "captured"})

    client = _client_with_handler(handler)
    rail = RazorpayPaymentRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("pay_cap2", 10.0, "retry", 1)
    assert resp.success is False
    assert resp.gateway_reference == "pay_cap2"
    assert resp.error_message is not None


def test_secret_not_leaked():
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"id": "pay_ok", "status": "captured"})

    client = _client_with_handler(handler)
    rail = RazorpayPaymentRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    text = repr(rail) + str(rail)
    assert KEY_SECRET not in text
    assert "Authorization" not in text

    # error path also must not leak
    def fail_handler(request: httpx.Request):
        return httpx.Response(500, json={"error": {"description": "boom"}})

    client2 = _client_with_handler(fail_handler)
    rail2 = RazorpayPaymentRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client2)
    resp = rail2.execute_attempt("pay_x", 10.0, "retry", 1)
    assert KEY_SECRET not in (resp.error_message or "")
    assert KEY_SECRET not in (resp.gateway_reference or "")
