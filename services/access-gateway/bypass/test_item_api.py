import io
import json
import unittest
import urllib.error
from unittest.mock import Mock

from grpc_server import VintedAccess
from item_api import (
    COOLDOWN,
    MAX_BYTES,
    ItemApiClient,
    NoRedirect,
    item_url,
    minimal_item,
)
from vinted import CatalogResult
from vinted_rpc import access_pb2 as pb


def payload(identity=123):
    return {"code": 0, "item": {"id": identity, "created_at_ts": "2026-09-25T00:00:00Z",
            "is_draft": False, "photos": [{"url": "https://images1.vinted.net/a.webp"}],
            "user": {"email": "not-for-transport"}}, "session": "not-for-transport"}


def response(data=b"", content_type="application/json"):
    result = Mock(status=200, headers={"Content-Type": content_type})
    result.read = io.BytesIO(data).read
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class ItemApiTests(unittest.TestCase):
    def test_description_and_public_reviews_survive_without_private_user_fields(self):
        raw = payload()
        raw["item"].update(
            description="Small hole on sleeve",
            user_id=50,
            user={"id": 50, "feedback_count": 255, "feedback_reputation": 0.98,
                  "email": "private", "access_token": "secret", "login": "not-needed"},
        )
        item = minimal_item(raw, "123")["item"]
        self.assertEqual(item["description"], "Small hole on sleeve")
        self.assertEqual(item["user"], {"id": 50, "feedback_count": 255, "feedback_reputation": 0.98})
        self.assertEqual(item["user_id"], 50)
        self.assertNotIn("secret", json.dumps(item))

    def test_incomplete_gallery_does_not_discard_publication_metadata(self):
        raw = payload()
        raw["item"]["photos"] = [None, {"url": None}]
        item = minimal_item(raw, "123")["item"]
        self.assertEqual(item["created_at_ts"], raw["item"]["created_at_ts"])
        self.assertEqual(item["photos"], [{}, {"url": None}])

    def test_anonymous_session_reused_and_payload_minimized(self):
        session = Mock()
        session.open.side_effect = [response(), response(json.dumps(payload()).encode()),
                                    response(json.dumps(payload()).encode())]
        client = ItemApiClient(lambda: session)
        first = client.fetch("GB", "123")
        self.assertEqual(json.loads(first)["item"]["created_at_ts"], "2026-09-25T00:00:00Z")
        self.assertNotIn(b"not-for-transport", first)
        self.assertEqual(client.fetch("UK", "123"), first)
        self.assertEqual(session.open.call_count, 3)
        self.assertEqual(session.open.call_args_list[1].args[0].full_url,
                         "https://www.vinted.co.uk/api/v2/items/123/details")

    def test_403_cools_down_region_without_login_or_repeated_requests(self):
        now = [100.0]
        session = Mock()
        session.open.side_effect = [response(), urllib.error.HTTPError("", 403, "secret", {}, None)]
        client = ItemApiClient(lambda: session, lambda: now[0])
        self.assertIsNone(client.fetch("FR", "123"))
        self.assertIsNone(client.fetch("FR", "456"))
        self.assertEqual(session.open.call_count, 2)
        self.assertNotIn("FR", client.sessions)
        now[0] += COOLDOWN
        session.open.side_effect = [response(), response(json.dumps(payload()).encode())]
        self.assertIsNotNone(client.fetch("FR", "123"))

    def test_invalid_and_oversized_responses_never_supply_item_data(self):
        cases = [json.dumps(payload(456)).encode(), b"not json", b" " * (MAX_BYTES + 1),
                 b'{"code":true,"item":{"id":123}}']
        for data in cases:
            with self.subTest(data_length=len(data)):
                session = Mock()
                session.open.side_effect = [response(), response(data)]
                self.assertIsNone(ItemApiClient(lambda session=session: session).fetch("FR", "123"))

    def test_429_retry_after_and_timeout_are_bounded(self):
        now = [100.0]
        session = Mock()
        session.open.side_effect = [response(), urllib.error.HTTPError("", 429, "", {"Retry-After": "900"}, None)]
        client = ItemApiClient(lambda: session, lambda: now[0])
        self.assertIsNone(client.fetch("FR", "123"))
        self.assertEqual(client.retry_after["FR"], 1000)
        session.open.side_effect = TimeoutError("secret")
        self.assertIsNone(client.fetch("DE", "123"))
        self.assertLessEqual(session.open.call_args.kwargs["timeout"], 3)

    def test_redirects_and_invalid_routes_are_not_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.org"))
        for region, identity in [("PL", "123"), ("FR", "../1"), ("FR", "0")]:
            with self.assertRaises(ValueError):
                item_url(region, identity)

    def test_rpc_uses_api_then_falls_back_to_existing_gallery(self):
        fetch = Mock(return_value=CatalogResult("FR", "https://www.vinted.fr/items/123",
                     outcome="page_loaded", body="gallery"))
        service = VintedAccess("http://127.0.0.1:8191", fetch=fetch)
        service.item_api = Mock()
        service.item_api.fetch.return_value = json.dumps(payload()).encode()
        context = Mock()
        context.is_active.return_value = True
        context.time_remaining.return_value = 90
        req = pb.FetchItemRequest(region="FR", item_id="123", include_html=True)
        result = service.FetchItem(req, context)
        self.assertTrue(result.item_json_utf8)
        self.assertFalse(result.html_utf8)
        fetch.assert_not_called()
        service.item_api.fetch.return_value = None
        result = service.FetchItem(req, context)
        self.assertEqual(result.html_utf8, b"gallery")
        self.assertFalse(result.item_json_utf8)
        self.assertEqual(fetch.call_args.kwargs["item_id"], "123")

    def test_forced_html_never_calls_item_api_again(self):
        fetch = Mock(return_value=CatalogResult("FR", "https://www.vinted.fr/items/123",
                     outcome="page_loaded", body="gallery"))
        service = VintedAccess("http://127.0.0.1:8191", fetch=fetch)
        service.item_api = Mock()
        context = Mock()
        context.is_active.return_value = True
        context.time_remaining.return_value = 90
        request = pb.FetchItemRequest(region="FR", item_id="123", include_html=True, force_html=True)
        result = service.FetchItem(request, context)
        self.assertEqual(result.html_utf8, b"gallery")
        service.item_api.fetch.assert_not_called()
