import base64

import httpx
import pytest

from src.infrastructure.ports import PaymentRailPort
from src.infrastructure.razorpay.razorpay_recovery_rail import RazorpayRecoveryRail

KEY_ID = "rzp_test_dummy_id"
KEY_SECRET = "rzp_test_dummy_secret"


def _client(handler, auth=None):
    return httpx.Client(transport=httpx.MockTransport(handler), auth=auth)


def test_post_path():
    captured = {}

    def handler(request: httpx.Request):
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["path"] = request.url.path
        return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.io/i/abc"})

    client = _client(handler, auth=(KEY_ID, KEY_SECRET))
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, base_url="https://api.razorpay.com/v1", client=client)
    resp = rail.execute_attempt("txn_1", 100.0, "retry", 1)
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.razorpay.com/v1/payment_links"
    assert captured["path"] == "/v1/payment_links"
    assert resp.success is True


def test_basic_auth():
    captured = {}

    def handler(request: httpx.Request):
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.io/i/abc"})

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    rail.execute_attempt("txn_1", 100.0, "retry", 1)
    expected = "Basic " + base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode()
    assert captured["auth"] == expected


def test_amount_conversion_to_paise():
    cases = [(100.0, 10000), (123.45, 12345), (0.01, 1), (10.5, 1050)]

    for amt, expected_paise in cases:
        captured = {}

        def handler(request: httpx.Request, _exp=expected_paise):
            import json as _j

            body = _j.loads(request.content.decode())
            captured["amount"] = body["amount"]
            captured["currency"] = body["currency"]
            return httpx.Response(200, json={"id": "plink_x", "short_url": "https://rzp.io/i/x"})

        client = _client(handler)
        rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
        rail.execute_attempt("txn_amt", amt, "retry", 1)
        assert captured["amount"] == expected_paise, f"{amt} -> {captured['amount']} != {expected_paise}"
        assert captured["currency"] == "INR"


def test_deterministic_reference_id():
    refs = []

    def handler(request: httpx.Request):
        import json as _j

        body = _j.loads(request.content.decode())
        refs.append(body["reference_id"])
        return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.io/i/a"})

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    rail.execute_attempt("txn_det", 10.0, "retry", 2)
    rail.execute_attempt("txn_det", 10.0, "retry", 2)
    assert refs[0] == refs[1]
    assert "txn_det" in refs[0]
    # different attempt yields different reference
    refs.clear()
    rail.execute_attempt("txn_det", 10.0, "retry", 3)
    assert refs[0] != "txn_det_2" or "3" in refs[0]


def test_successful_creation_returns_success():
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"id": "plink_123", "short_url": "https://rzp.io/i/abc123", "status": "created"})

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("txn_ok", 50.0, "retry", 1)
    assert resp.success is True
    assert isinstance(rail, PaymentRailPort)
    # success means recovery action created, not payment recovered — error_message should be None/empty
    assert resp.error_message is None or "recovered" not in resp.error_message.lower()


def test_gateway_reference_contains_link_or_id():
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"id": "plink_999", "short_url": "https://rzp.io/i/xyz"})

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("txn_link", 20.0, "dunning", 1)
    assert resp.success is True
    assert resp.gateway_reference in ("https://rzp.io/i/xyz", "plink_999")
    # when only id present
    def handler2(request: httpx.Request):
        return httpx.Response(200, json={"id": "plink_only"})

    client2 = _client(handler2)
    rail2 = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client2)
    resp2 = rail2.execute_attempt("txn_link2", 20.0, "retry", 1)
    assert resp2.gateway_reference == "plink_only"


def test_provider_declared_failure():
    def handler(request: httpx.Request):
        return httpx.Response(400, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "amount invalid"}})

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("txn_fail", 50.0, "retry", 1)
    assert resp.success is False
    assert "amount invalid" in resp.error_message.lower() or "bad_request" in resp.error_message.lower()


def test_timeout_mapping():
    def handler(request: httpx.Request):
        raise httpx.ConnectTimeout("timeout")

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("txn_to", 10.0, "retry", 1)
    assert resp.success is False
    assert "timeout" in resp.error_message.lower()
    assert resp.gateway_reference == "txn_to"


def test_network_failure():
    def handler(request: httpx.Request):
        raise httpx.ConnectError("network down")

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("txn_net", 10.0, "retry", 1)
    assert resp.success is False
    assert "network" in resp.error_message.lower()


def test_non_2xx_failure():
    def handler(request: httpx.Request):
        return httpx.Response(500, text="Internal Server Error")

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("txn_500", 10.0, "retry", 1)
    assert resp.success is False
    assert "500" in resp.error_message


def test_duplicate_reference_conflict_handling():
    # provider echoes same deterministic reference_id -> idempotent success
    def handler(request: httpx.Request):
        return httpx.Response(400, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "reference_id already exists: txn_dup_1", "reference_id": "txn_dup_1"}})

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("txn_dup", 10.0, "retry", 1)
    # deterministic idempotent success, not a failure
    assert resp.success is True
    assert resp.gateway_reference is not None
    assert "txn_dup" in resp.gateway_reference

    # also via 409 shape with matching reference_id
    def handler2(request: httpx.Request):
        return httpx.Response(409, json={"error": {"description": "reference_id duplicate: txn_dup2_2", "reference_id": "txn_dup2_2"}})

    client2 = _client(handler2)
    rail2 = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client2)
    resp2 = rail2.execute_attempt("txn_dup2", 10.0, "retry", 2)
    assert resp2.success is True


def test_duplicate_generic_409_without_matching_ref_is_failure():
    # generic 409 + "reference_id conflict" without matching reference -> must stay failure
    def handler(request: httpx.Request):
        return httpx.Response(409, json={"error": {"description": "reference_id conflict"}})

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("txn_nomatch409", 10.0, "retry", 1)
    assert resp.success is False
    assert resp.gateway_reference == "txn_nomatch409"


def test_duplicate_generic_400_without_matching_ref_is_failure():
    def handler(request: httpx.Request):
        return httpx.Response(400, json={"error": {"description": "duplicate reference"}})

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("txn_nomatch400", 10.0, "retry", 1)
    assert resp.success is False


def test_duplicate_matching_ref_preserves_provider_link():
    # matching ref + provider short_url/id -> gateway_reference must be real link, not synthetic ref
    def handler(request: httpx.Request):
        return httpx.Response(400, json={
            "error": {"description": "reference_id already exists: txn_linkdup_1", "reference_id": "txn_linkdup_1", "id": "plink_existing_123", "short_url": "https://rzp.io/i/existing_link"},
            "id": "plink_existing_123",
            "short_url": "https://rzp.io/i/existing_link",
        })

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("txn_linkdup", 10.0, "retry", 1)
    assert resp.success is True
    assert resp.gateway_reference == "https://rzp.io/i/existing_link"

    # without short_url, falls back to id
    def handler2(request: httpx.Request):
        return httpx.Response(400, json={"error": {"description": "duplicate: txn_linkdup2_1", "reference_id": "txn_linkdup2_1", "id": "plink_only_dup"}})

    client2 = _client(handler2)
    rail2 = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client2)
    resp2 = rail2.execute_attempt("txn_linkdup2", 10.0, "retry", 1)
    assert resp2.success is True
    assert resp2.gateway_reference == "plink_only_dup"


def test_reauth_not_silently_mapped():
    # REAUTH must not be mapped to Payment Link
    def handler(request: httpx.Request):
        # if this were called, the test would incorrectly pass — ensure not called
        assert False, "REAUTH should not invoke Razorpay"

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("txn_reauth", 10.0, "re-auth", 1)
    assert resp.success is False
    assert "reauth" in resp.error_message.lower()
    resp2 = rail.execute_attempt("txn_reauth2", 10.0, "Request mandate re-authorization from customer", 1)
    assert resp2.success is False


def test_missing_credentials():
    with pytest.raises(ValueError, match="Missing Razorpay credentials"):
        RazorpayRecoveryRail(key_id="", key_secret="", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    with pytest.raises(ValueError, match="Missing Razorpay credentials"):
        RazorpayRecoveryRail(key_id=KEY_ID, key_secret="", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))


def test_secret_not_leaked():
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.io/i/a"})

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    text = repr(rail) + str(rail)
    assert KEY_SECRET not in text
    assert "Authorization" not in text

    def fail_handler(request: httpx.Request):
        return httpx.Response(400, json={"error": {"description": "boom"}})

    client2 = _client(fail_handler)
    rail2 = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client2)
    resp = rail2.execute_attempt("txn_x", 10.0, "retry", 1)
    assert KEY_SECRET not in (resp.error_message or "")
    assert KEY_SECRET not in (resp.gateway_reference or "")


def test_no_live_network():
    # proves transport is mocked — handler is called, no DNS
    called = {}

    def handler(request: httpx.Request):
        called["yes"] = True
        return httpx.Response(200, json={"id": "plink_live", "short_url": "https://rzp.io/i/live"})

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("txn_live", 5.0, "retry", 1)
    assert called.get("yes") is True
    assert resp.success is True


def test_success_does_not_mean_recovered():
    """Creating a Payment Link is not payment recovery."""
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"id": "plink_777", "short_url": "https://rzp.io/i/777"})

    client = _client(handler)
    rail = RazorpayRecoveryRail(key_id=KEY_ID, key_secret=KEY_SECRET, client=client)
    resp = rail.execute_attempt("txn_not_rec", 99.0, "retry", 1)
    assert resp.success is True
    msg = (resp.error_message or "").lower()
    assert "recovered" not in msg
    # gateway reference is link, not a claim that original payment recovered
    assert resp.gateway_reference == "https://rzp.io/i/777"
