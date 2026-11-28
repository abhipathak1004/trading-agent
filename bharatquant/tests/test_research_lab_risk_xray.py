"""Risk X-Ray: drawdown episodes and signed volatility contributions."""

import pytest

from bq.research_lab.risk_xray import (
    RiskXRayError,
    compute_risk_xray,
    drawdown_episodes,
    risk_contributions,
)


def test_two_drawdowns_are_ranked_by_depth() -> None:
    equity = [100.0, 120.0, 90.0, 125.0, 140.0, 70.0, 150.0]
    report = drawdown_episodes(equity)
    episodes = report["episodes"]
    assert len(episodes) == 2
    assert episodes[0]["depth_pct"] == pytest.approx(-50.0)
    assert episodes[0]["recovery_index"] == 6
    assert episodes[1]["depth_pct"] == pytest.approx(-25.0)
    assert episodes[1]["duration_bars"] == 2
    assert not any(item["is_open"] for item in episodes)


def test_open_drawdown_is_reported_and_flagged() -> None:
    report = drawdown_episodes([100.0, 110.0, 80.0])
    (episode,) = report["episodes"]
    assert episode["is_open"] is True
    assert episode["recovery_index"] is None
    assert episode["depth_pct"] == pytest.approx(-300.0 / 11.0)
    assert episode["duration_bars"] == 1


def test_open_drawdown_is_kept_outside_the_deepest_five() -> None:
    equity = [100.0]
    for depth in (5, 10, 15, 20, 25, 30):
        peak = equity[-1]
        equity.extend([peak, peak * (1 - depth / 100.0), peak])
    peak = equity[-1]
    equity.append(peak * 0.99)
    report = drawdown_episodes(equity)
    episodes = report["episodes"]
    assert report["observed_count"] == 7
    # Five deepest plus the open one, which is the shallowest.
    assert len(episodes) == 6
    open_items = [item for item in episodes if item["is_open"]]
    assert len(open_items) == 1
    assert open_items[0]["depth_pct"] == pytest.approx(-1.0)


def test_float_dust_does_not_create_a_phantom_episode() -> None:
    equity = [100.0, 90.0, 100.0 + 1e-14]
    report = drawdown_episodes(equity)
    assert report["observed_count"] == 1
    assert report["episodes"][0]["is_open"] is False


def test_invalid_equity_is_refused() -> None:
    with pytest.raises(RiskXRayError):
        drawdown_episodes([100.0])
    with pytest.raises(RiskXRayError):
        drawdown_episodes([100.0, float("nan")])
    with pytest.raises(RiskXRayError):
        drawdown_episodes([100.0, 0.0])


def test_contributions_sum_to_total_volatility() -> None:
    returns = {
        "AAA": [0.02, 0.01, 0.03, 0.00, 0.04],
        "BBB": [-0.01, 0.00, -0.02, 0.01, -0.01],
    }
    weights = {"AAA": 0.6, "BBB": 0.4}
    report = risk_contributions(returns, weights)
    total = float(report["total_volatility_annualized"])
    summed = sum(
        float(item["risk_contribution"])
        for item in report["contributions"]
        if item["risk_contribution"] is not None
    )
    assert summed == pytest.approx(total, rel=1e-9)
    hedge = next(item for item in report["contributions"] if item["symbol"] == "BBB")
    assert float(hedge["risk_contribution"]) < 0


def test_constant_holding_is_unestimable_but_hides_nothing() -> None:
    returns = {
        "AAA": [0.02, 0.01, 0.03, 0.00, 0.04],
        "BBB": [-0.01, 0.00, -0.02, 0.01, -0.01],
        "CASHY": [0.0, 0.0, 0.0, 0.0, 0.0],
    }
    weights = {"AAA": 0.5, "BBB": 0.4, "CASHY": 0.1}
    report = risk_contributions(returns, weights)
    by_symbol = {item["symbol"]: item for item in report["contributions"]}
    assert by_symbol["CASHY"]["risk_contribution"] is None
    assert by_symbol["CASHY"]["contribution_pct"] is None
    # The other two remain estimable: one constant series does not poison them.
    assert by_symbol["AAA"]["risk_contribution"] is not None
    assert by_symbol["BBB"]["risk_contribution"] is not None
    assert report["estimable"] == 2


def test_zero_portfolio_volatility_is_refused() -> None:
    with pytest.raises(RiskXRayError, match="zero"):
        risk_contributions({"AAA": [0.0, 0.0, 0.0]}, {"AAA": 1.0})


def test_combined_xray_payload_has_both_sections() -> None:
    payload = compute_risk_xray(
        [100.0, 120.0, 90.0, 125.0],
        {"AAA": [0.02, 0.01, 0.03, 0.00]},
        {"AAA": 1.0},
    )
    assert set(payload) == {"drawdown", "risk"}
    assert payload["drawdown"]["episodes"]
    assert payload["risk"]["contributions"]
