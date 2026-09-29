"""REST 传输层：签名头、离线拒绝、错误码映射与重试次数。"""

import io
import unittest
import urllib.error
from email.message import Message
from unittest import mock

from description_pipeline.sources.onshape.client import OnshapeClient
from description_pipeline.sources.onshape.errors import (
    API_ERROR,
    API_UNAVAILABLE,
    CREDENTIALS_MISSING,
    OnshapeSourceError,
)


class _Opener:
    """替身 opener：open() 直接抛出预置异常。"""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.attempts = 0

    def open(self, request, timeout=None):  # noqa: ANN001, ARG002 - 与 urllib 一致
        self.attempts += 1
        raise self.error


class HeaderTests(unittest.TestCase):
    def test_bearer_token_skips_hmac_fields(self):
        headers = OnshapeClient(bearer="token").headers("GET", "/api/x", {}, "application/json")
        self.assertEqual(headers["Authorization"], "Bearer token")
        self.assertNotIn("On-Nonce", headers)

    def test_hmac_signature_shape(self):
        headers = OnshapeClient(access_key="ak", secret_key="sk").headers(
            "GET", "/api/x", {"a": "1"}, "application/json"
        )
        self.assertTrue(headers["Authorization"].startswith("On ak:HmacSHA256:"))
        self.assertEqual(len(headers["On-Nonce"]), 25)
        self.assertIn("Date", headers)

    def test_credentials_can_come_from_environment(self):
        with mock.patch.dict(
            "os.environ",
            {"ONSHAPE_ACCESS_KEY": "ak", "ONSHAPE_SECRET_KEY": "sk", "ONSHAPE_API": "https://cad.example.com"},
            clear=True,
        ):
            client = OnshapeClient.from_env()
        self.assertTrue(client.has_credentials)
        self.assertEqual(client.stack, "https://cad.example.com")


class RequestTests(unittest.TestCase):
    def test_offline_request_is_rejected_before_network(self):
        with self.assertRaises(OnshapeSourceError) as caught:
            OnshapeClient(offline=True).request("GET", "/api/x")
        self.assertEqual(caught.exception.code, API_UNAVAILABLE)

    def test_missing_credentials_are_reported(self):
        with mock.patch("description_pipeline.sources.onshape.client.load_credentials", return_value=("s", "", "", "")):
            client = OnshapeClient.from_env()
        self.assertFalse(client.has_credentials)
        with self.assertRaises(OnshapeSourceError) as caught:
            client.request("GET", "/api/x")
        self.assertEqual(caught.exception.code, CREDENTIALS_MISSING)

    def test_http_error_maps_to_api_error_with_quota_hint(self):
        error = urllib.error.HTTPError("http://x", 402, "Payment Required", Message(), io.BytesIO(b"quota"))
        opener = _Opener(error)
        with (
            mock.patch("urllib.request.build_opener", return_value=opener),
            self.assertRaises(OnshapeSourceError) as caught,
        ):
            OnshapeClient(access_key="ak", secret_key="sk").request("GET", "/api/x")
        self.assertEqual(caught.exception.code, API_ERROR)
        self.assertEqual(caught.exception.detail["status"], 402)
        self.assertIn("配额", caught.exception.detail["hint"])
        self.assertEqual(opener.attempts, 1)

    def test_transport_failure_retries_then_reports_unavailable(self):
        opener = _Opener(urllib.error.URLError("boom"))
        with (
            mock.patch("urllib.request.build_opener", return_value=opener),
            mock.patch("description_pipeline.sources.onshape.client.time.sleep"),
            self.assertRaises(OnshapeSourceError) as caught,
        ):
            OnshapeClient(access_key="ak", secret_key="sk").request("GET", "/api/x", retries=2)
        self.assertEqual(caught.exception.code, API_UNAVAILABLE)
        self.assertEqual(opener.attempts, 3)


if __name__ == "__main__":
    unittest.main()
