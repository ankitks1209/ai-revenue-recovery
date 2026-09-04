import pytest
from src.infrastructure.mock_payment_rail import MockPaymentRail

def test_mock_payment_rail_explicit_fixtures():
    rail = MockPaymentRail()
    rail.set_fixture("TXN_SUCCESS", True)
    rail.set_fixture("TXN_FAIL", False)

    res1 = rail.execute_attempt("TXN_SUCCESS", 100.0, "retry", 1)
    assert res1.success is True

    res2 = rail.execute_attempt("TXN_FAIL", 100.0, "retry", 1)
    assert res2.success is False

def test_mock_payment_rail_force_global():
    rail = MockPaymentRail()
    rail.force_all(True)

    res = rail.execute_attempt("ANY_TXN", 50.0, "retry", 1)
    assert res.success is True

    rail.force_all(False)
    res_fail = rail.execute_attempt("ANY_TXN", 50.0, "retry", 1)
    assert res_fail.success is False

def test_mock_payment_rail_determinism():
    rail1 = MockPaymentRail(seed=42)
    rail2 = MockPaymentRail(seed=42)

    res1 = rail1.execute_attempt("TXN_123", 100.0, "retry", 1)
    res2 = rail2.execute_attempt("TXN_123", 100.0, "retry", 1)

    assert res1.success == res2.success
    assert res1.gateway_reference == res2.gateway_reference
