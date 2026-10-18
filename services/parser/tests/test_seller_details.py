import json
from datetime import UTC, datetime

import pytest
from test_item_api_details import REQUEST, data
from test_item_details import fixture, request

from vpars_parser.sources.vinted_details import parse_api_details, parse_details

NOW = datetime.now(UTC)


def seller_record(**updates):
    # Public FR item-page shape observed 2026-09-26; account identity anonymized.
    return {
        "item_id": "10001",
        "seller_id": "987",
        "name": "test_seller",
        "feedback_count": 190,
        "feedback_reputation": 0.98,
    } | updates


def html_with(*records):
    flight = "2:" + json.dumps(list(records)) + "\n"
    return fixture() + "<script>self.__next_f.push(" + json.dumps([1, flight]) + ");</script>"


def test_rating_uses_requested_item_seller_and_five_star_scale():
    html = html_with(seller_record(), seller_record(item_id="99999", feedback_count=999))
    result = parse_details(html, request(), NOW)
    assert result.seller.model_dump() == {
        "source_user_id": "987",
        "username": "test_seller",
        "rating": 4.9,
        "feedback_count": 190,
    }
    assert result.photos_complete and len(result.photo_urls) == 5


@pytest.mark.parametrize(
    "records",
    [
        [],
        [seller_record(item_id="99999")],
        [seller_record(seller_id="not-an-id")],
        [seller_record(), {"item_id": "10001", "seller_id": "988"}],
        [seller_record(), seller_record(feedback_count=191)],
    ],
)
def test_missing_or_conflicting_seller_never_borrows_another_account(records):
    result = parse_details(html_with(*records), request(), NOW)
    assert result.seller is None and result.photos_complete


@pytest.mark.parametrize(
    "reputation", [None, True, "0.98", -0.1, 1.1, 10**500, float("nan"), float("inf")]
)
def test_invalid_rating_is_unknown_without_blocking_count_or_photos(reputation):
    result = parse_details(html_with(seller_record(feedback_reputation=reputation)), request(), NOW)
    assert result.seller.rating is None
    assert result.seller.feedback_count == 190 and result.photos_complete


@pytest.mark.parametrize(
    "count,expected", [(0, 0), (None, None), (-1, None), (True, None), ("190", None)]
)
def test_zero_and_unknown_review_counts_are_distinct(count, expected):
    result = parse_details(html_with(seller_record(feedback_count=count)), request(), NOW)
    assert result.seller.feedback_count == expected
    if count == 0:
        assert result.seller.rating is None


def test_api_seller_identity_and_missing_fields():
    user = {"id": 987, "login": "test_seller", "feedback_count": 190, "feedback_reputation": 0.98}
    result = parse_api_details(data(user_id=987, user=user), REQUEST, NOW)
    assert result.seller.rating == 4.9 and result.seller.feedback_count == 190
    assert result.seller.username == "test_seller"
    assert parse_api_details(data(user_id=988, user=user), REQUEST, NOW).seller is None
    result = parse_api_details(data(user={"id": 987, "login": "new_seller"}), REQUEST, NOW)
    assert result.seller.rating is None and result.seller.feedback_count is None
