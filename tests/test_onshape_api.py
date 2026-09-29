"""Onshape HTTP 客户端：签名头、重试、离线与错误提示、缓存包装、端点 URL。"""

import base64
import hashlib
import hmac
import http.client
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from onshape_export.api import OnshapeClient, OnshapeError  # noqa: E402
from onshape_export.cache import ResponseCache  # noqa: E402
from onshape_export.url import DocumentRef  # noqa: E402

REF = DocumentRef(
    stack="https://cad.onshape.com",
    document_id="did",
    workspace_id="wid",
    element_id="eid",
)


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class QuotaHTTPError(urllib.error.HTTPError):
    """带响应体的 402：验证错误包装时读得到 message。"""

    def read(self, amt=None) -> bytes:
        return b'{"message":"quota"}'


def client(**kwargs) -> OnshapeClient:
    options = {
        "stack": "https://cad.onshape.com",
        "access_key": "ak",
        "secret_key": "sk",
    }
    options.update(kwargs)
    return OnshapeClient(**options)


class HeaderTests(unittest.TestCase):
    def test_hmac_signature_matches_the_documented_recipe(self):
        api = client()
        headers = api._headers("GET", "/api/v1/foo", {"a": "1"}, "application/json")
        self.assertIn("Authorization", headers)
        prefix, digest = headers["Authorization"].split(":HmacSHA256:")
        self.assertEqual(prefix, "On ak")
        payload = (
            "GET\n"
            + headers["On-Nonce"]
            + "\n"
            + headers["Date"]
            + "\n"
            + "application/json\n"
            + "/api/v1/foo\n"
            + "a=1\n"
        ).lower()
        expected = base64.b64encode(hmac.new(b"sk", payload.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(digest, expected)

    def test_bearer_token_skips_hmac(self):
        headers = client(bearer="token")._headers("GET", "/x", {}, "application/json")
        self.assertEqual(headers["Authorization"], "Bearer token")
        self.assertNotIn("On-Nonce", headers)

    def test_has_credentials_accepts_either_scheme(self):
        self.assertTrue(client().has_credentials)
        self.assertTrue(client(access_key="", secret_key="", bearer="t").has_credentials)
        self.assertFalse(client(access_key="", secret_key="").has_credentials)


class RequestTests(unittest.TestCase):
    def test_json_and_raw_responses(self):
        api = client()
        with mock.patch("urllib.request.OpenerDirector.open", return_value=FakeResponse(b'{"ok": true}')):
            self.assertEqual(api.request("GET", "/api/x"), {"ok": True})
        with mock.patch("urllib.request.OpenerDirector.open", return_value=FakeResponse(b"bytes")):
            self.assertEqual(api.request("GET", "/api/x", raw=True), b"bytes")

    def test_query_merges_path_and_dict(self):
        api = client()
        seen: dict = {}

        def open_url(request, timeout=None):
            seen["url"] = request.full_url
            return FakeResponse(b"{}")

        with mock.patch("urllib.request.OpenerDirector.open", side_effect=open_url):
            api.request("GET", "/api/x?b=2", {"a": "1"})
        self.assertEqual(seen["url"].split("?")[0], "https://cad.onshape.com/api/x")
        self.assertIn("a=1", seen["url"])
        self.assertIn("b=2", seen["url"])

    def test_http_error_is_wrapped_with_quota_hint(self):
        api = client()
        error = QuotaHTTPError("url", 402, "Payment Required", http.client.HTTPMessage(), None)
        with (
            mock.patch("urllib.request.OpenerDirector.open", side_effect=error),
            self.assertRaises(OnshapeError) as caught,
        ):
            api.request("GET", "/api/x")
        self.assertEqual(caught.exception.status, 402)
        self.assertIn("配额", caught.exception.hint)
        self.assertIn("配额", str(caught.exception))

    def test_transient_failures_are_retried_then_succeed(self):
        api = client()
        attempts = {"count": 0}

        def flaky(request, timeout=None):
            attempts["count"] += 1
            if attempts["count"] < 2:
                raise TimeoutError("network hiccup")
            return FakeResponse(b"{}")

        with (
            mock.patch("urllib.request.OpenerDirector.open", side_effect=flaky),
            mock.patch("time.sleep", return_value=None),
        ):
            self.assertEqual(api.request("GET", "/api/x", retries=2), {})
        self.assertEqual(attempts["count"], 2)

    def test_retries_are_exhausted_with_a_clear_error(self):
        api = client()
        with (
            mock.patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError("down")),
            mock.patch("time.sleep", return_value=None),
            self.assertRaises(OnshapeError) as caught,
        ):
            api.request("GET", "/api/x", retries=1)
        self.assertIn("网络错误", str(caught.exception))

    def test_offline_and_missing_credentials_are_explicit(self):
        with self.assertRaises(OnshapeError) as offline:
            client(offline=True).request("GET", "/api/x")
        self.assertIn("离线模式", str(offline.exception))
        with self.assertRaises(OnshapeError) as anonymous:
            client(access_key="", secret_key="").request("GET", "/api/x")
        self.assertEqual(anonymous.exception.status, 401)


class EndpointTests(unittest.TestCase):
    def _record(self):
        calls: list[dict] = []

        def open_url(request, timeout=None):
            calls.append({"url": request.full_url, "method": request.get_method()})
            return FakeResponse(b'{"ok": true}')

        return calls, open_url

    def test_document_endpoints_hit_expected_paths(self):
        api = client()
        calls, open_url = self._record()
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=open_url):
            api.get_assembly(REF)
            api.get_assembly_features(REF)
            api.get_mate_values(REF)
            api.get_studio_mass_properties(REF, "studio")
            api.get_part_mass_properties(REF, "studio", "part")
            api.get_part_studio_gltf(REF, "studio")
            api.get_part_stl(REF, "studio", "part")
            api.whoami()
            api.list_elements(REF)
        paths = [urllib.parse.urlparse(entry["url"]).path for entry in calls]
        self.assertTrue(all(path.startswith("/api/") for path in paths), paths)
        self.assertIn("/api/assemblies/d/did/w/wid/e/eid", paths)
        self.assertIn("/api/assemblies/d/did/w/wid/e/eid/features", paths)
        self.assertIn("/api/assemblies/d/did/w/wid/e/eid/matevalues", paths)
        self.assertIn("/api/users/sessioninfo", paths)
        self.assertIn("/api/documents/d/did/w/wid/elements", paths)
        self.assertIn("/api/partstudios/d/did/w/wid/e/studio/massproperties", paths)
        self.assertTrue(any(path.endswith("/partid/part/massproperties") for path in paths), paths)
        self.assertTrue(any(path.endswith("/partid/part/stl") for path in paths), paths)

    def test_cached_json_and_bytes_round_trip(self):
        api = client()
        with tempfile.TemporaryDirectory() as folder:
            api.cache = ResponseCache(Path(folder))
            with mock.patch("urllib.request.OpenerDirector.open", return_value=FakeResponse(b'{"value": 1}')):
                self.assertEqual(api.cached_json("thing", "GET", "/api/thing"), {"value": 1})
            # 第二次命中缓存：不再发请求
            with mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("no network")):
                self.assertEqual(api.cached_json("thing", "GET", "/api/thing"), {"value": 1})
            with mock.patch("urllib.request.OpenerDirector.open", return_value=FakeResponse(b"binary")):
                self.assertEqual(api.cached_bytes("blob", "/api/blob"), b"binary")
            with mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("no network")):
                self.assertEqual(api.cached_bytes("blob", "/api/blob"), b"binary")


if __name__ == "__main__":
    unittest.main()
