"""响应缓存与取数通道：命中缓存、缺失上报、live 写回。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from description_pipeline.sources.onshape.cache import CachedFetcher, ResponseCache
from description_pipeline.sources.onshape.errors import CACHE_MISS, OnshapeSourceError

from .helpers import ROOT_ELEMENT, StubClient, write_cache


class ResponseCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="onshape-cache-"))

    def test_json_and_bytes_round_trip(self):
        cache = ResponseCache(self.root)
        cache.save_json("payload", {"a": 1})
        cache.save_bytes("mesh.stl", b"binary")
        self.assertEqual(cache.load_json("payload"), {"a": 1})
        self.assertEqual(cache.load_bytes("mesh.stl"), b"binary")
        self.assertTrue(cache.json_path("payload").is_file())
        self.assertTrue(cache.bytes_path("mesh.stl").is_file())

    def test_missing_entry_reads_as_none_and_require_reports_code(self):
        cache = ResponseCache(self.root, read_only=True)
        self.assertIsNone(cache.load_json("payload"))
        with self.assertRaises(OnshapeSourceError) as caught:
            cache.require_json("payload")
        self.assertEqual(caught.exception.code, CACHE_MISS)
        self.assertEqual(caught.exception.detail["name"], "payload")

    def test_read_only_cache_does_not_touch_the_filesystem(self):
        cache = ResponseCache(self.root / "nested", read_only=True)
        cache.save_json("payload", {"a": 1})
        cache.save_bytes("mesh.stl", b"binary")
        self.assertFalse((self.root / "nested").exists())


class CachedFetcherTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="onshape-fetch-"))
        self.cache = ResponseCache(write_cache(self.root / "cache"))

    def test_cache_hit_never_calls_the_client(self):
        client = StubClient()
        fetcher = CachedFetcher(client, self.cache)
        payload = fetcher.json(f"assembly_{ROOT_ELEMENT}", "/api/assemblies/x")
        self.assertIn("rootAssembly", payload)
        self.assertEqual(client.calls, [])
        self.assertFalse(fetcher.used_network)
        self.assertEqual(fetcher.origin, "cache_replay")

    def test_missing_entry_without_client_reports_cache_miss(self):
        fetcher = CachedFetcher(None, ResponseCache(self.root / "cache", read_only=True))
        with self.assertRaises(OnshapeSourceError) as caught:
            fetcher.json("assembly_missing", "/api/assemblies/missing")
        self.assertEqual(caught.exception.code, CACHE_MISS)
        self.assertEqual(fetcher.origin, "cache_replay")

    def test_live_fetch_is_marked_and_written_back(self):
        empty = ResponseCache(self.root / "empty")
        client = StubClient()
        fetcher = CachedFetcher(client, empty)
        payload = fetcher.json(f"assembly_{ROOT_ELEMENT}", "/api/assemblies/root")
        self.assertIn("rootAssembly", payload)
        self.assertEqual(client.calls, ["/api/assemblies/root"])
        self.assertTrue(fetcher.used_network)
        self.assertEqual(fetcher.origin, "live_api")
        self.assertTrue(empty.json_path(f"assembly_{ROOT_ELEMENT}").is_file())

    def test_live_workspace_head_is_refreshed_but_offline_replay_keeps_the_probe(self):
        cache = ResponseCache(self.root / "mutable")
        path = "/api/assemblies/d/document/w/workspace/e/assembly"
        old = {"rootAssembly": {"documentMicroversion": "old"}}
        new = {"rootAssembly": {"documentMicroversion": "new"}}
        cache.save_json("head", old)
        self.assertEqual(CachedFetcher(None, cache).json("head", path), old)
        client = Mock()
        client.request.return_value = new
        live = CachedFetcher(client, cache)
        self.assertEqual(live.json("head", path), new)
        client.request.assert_called_once()
        self.assertEqual(live.bindings["head"]["source"], "network")
        self.assertEqual(cache.load_json("head"), new)


if __name__ == "__main__":
    unittest.main()
