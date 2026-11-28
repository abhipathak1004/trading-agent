"""NSE/BSE cost stack arithmetic: product, venue, brokerage, GST base."""

from decimal import Decimal

import pytest

from bq.paper_charges import (
    ChargeError,
    ChargeSchedule,
    IndiaCostModel,
    compute_charges,
    product_for_timeframe,
)

QUANTITY = 100
PRICE = 10_000  # paise; notional is 10,00,000 paise (Rs 10,000)


def _total(**kwargs: object) -> int:
    return compute_charges(
        side=str(kwargs.pop("side")),
        product=str(kwargs.pop("product")),
        quantity=QUANTITY,
        price_paise=PRICE,
        **kwargs,  # type: ignore[arg-type]
    ).total_paise


def test_delivery_is_charged_differently_from_intraday() -> None:
    buy_delivery = compute_charges(
        side="BUY", product="CAS", quantity=QUANTITY, price_paise=PRICE
    )
    buy_intraday = compute_charges(
        side="BUY", product="MIS", quantity=QUANTITY, price_paise=PRICE
    )
    # Delivery buy pays STT; intraday buy does not.
    assert buy_delivery.amount("STT") == 1000
    assert buy_intraday.amount("STT") == 0
    assert buy_delivery.total_paise == 1187
    assert buy_intraday.total_paise == 67


def test_sell_legs_charge_stt_and_no_stamp_duty() -> None:
    sell_delivery = compute_charges(
        side="SELL", product="CAS", quantity=QUANTITY, price_paise=PRICE
    )
    sell_intraday = compute_charges(
        side="SELL", product="MIS", quantity=QUANTITY, price_paise=PRICE
    )
    assert sell_delivery.amount("STT") == 1000
    assert sell_delivery.amount("STAMP") == 0
    assert sell_intraday.amount("STT") == 250
    assert sell_intraday.amount("STAMP") == 0


def test_short_cover_buy_leg_carries_intraday_stamp_duty() -> None:
    cover = compute_charges(
        side="BUY", product="MIS", quantity=QUANTITY, price_paise=PRICE
    )
    assert cover.amount("STAMP") == 30


def test_brokerage_appears_and_is_in_the_gst_base() -> None:
    schedule = ChargeSchedule(brokerage_bps=Decimal("2.5"))
    breakdown = compute_charges(
        side="BUY",
        product="CAS",
        quantity=QUANTITY,
        price_paise=PRICE,
        schedule=schedule,
    )
    assert breakdown.amount("BROKERAGE") == 250
    # GST base is brokerage + exchange + SEBI + IPFT = 281, at 18% -> 51.
    assert breakdown.amount("GST") == 51
    without = ChargeSchedule.zero()
    assert (
        compute_charges(
            side="BUY",
            product="CAS",
            quantity=QUANTITY,
            price_paise=PRICE,
            schedule=without,
        ).total_paise
        == 0
    )


def test_brokerage_cap_is_applied_per_order() -> None:
    schedule = ChargeSchedule(brokerage_bps=Decimal("100"), brokerage_cap_paise=2_000)
    breakdown = compute_charges(
        side="BUY",
        product="CAS",
        quantity=QUANTITY,
        price_paise=PRICE,
        schedule=schedule,
    )
    assert breakdown.amount("BROKERAGE") == 2_000


def test_dp_charge_applies_to_delivery_sells_only() -> None:
    schedule = ChargeSchedule(dp_sell_paise=1_500)
    delivery = compute_charges(
        side="SELL",
        product="CAS",
        quantity=QUANTITY,
        price_paise=PRICE,
        schedule=schedule,
    )
    intraday = compute_charges(
        side="SELL",
        product="MIS",
        quantity=QUANTITY,
        price_paise=PRICE,
        schedule=schedule,
    )
    assert delivery.amount("DP") == 1_500
    assert intraday.amount("DP") == 0


def test_bse_venue_uses_its_own_rate() -> None:
    schedule = ChargeSchedule(exchange_bse=Decimal("0.000045"))
    nse = compute_charges(
        side="BUY",
        product="CAS",
        quantity=QUANTITY,
        price_paise=PRICE,
        schedule=schedule,
    )
    bse = compute_charges(
        side="BUY",
        product="CAS",
        quantity=QUANTITY,
        price_paise=PRICE,
        schedule=schedule,
        venue="BSE",
    )
    assert bse.amount("EXCHANGE") == 45
    assert nse.amount("EXCHANGE") == 30


def test_unknown_venue_and_product_are_refused() -> None:
    with pytest.raises(ChargeError):
        compute_charges(
            side="BUY", product="CAS", quantity=1, price_paise=1, venue="MCX"
        )
    with pytest.raises(ChargeError):
        compute_charges(side="BUY", product="CNC", quantity=1, price_paise=1)


def test_timeframe_maps_to_product() -> None:
    assert product_for_timeframe("intraday") == "MIS"
    assert product_for_timeframe("swing") == "CAS"


def test_cost_model_records_its_theta() -> None:
    model = IndiaCostModel()
    theta = model.theta()
    assert theta["venue"] == "NSE"
    assert theta["gst"] == "0.18"
    assert model.product_for("intraday") == "MIS"
    assert model.charges(
        side="BUY", timeframe="swing", quantity=QUANTITY, price_paise=PRICE
    ).total_paise == _total(side="BUY", product="CAS")
