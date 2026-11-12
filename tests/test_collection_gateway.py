import unittest
from urllib.parse import urlencode

from lab.collection_provider import CollectionProvider
from lab.http import serving


def get(base, path, args=None):
    import json
    import urllib.request

    url = base + path
    if args:
        url += "?" + urlencode(args)
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def post(base, path, body):
    import json
    import urllib.request

    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


class CollectionProviderTests(unittest.TestCase):
    def test_rows_without_full_coverage_have_null_coverage(self):
        # 网关时钟固定在区间中点：只能给出半段数据，complete=false
        provider = CollectionProvider(clock=lambda: 1773007200)
        with serving(provider) as base:
            data = get(base, "/readings", {
                "meter": "m", "segment": "seg",
                "start": "2026-03-08T10:00:00Z",
                "end": "2026-03-09T10:00:00Z",
            })
            self.assertTrue(data["rows"])
            self.assertFalse(data["complete"])
            self.assertIsNone(data["coverage"])

    def test_complete_window_advertises_coverage(self):
        provider = CollectionProvider(clock=lambda: 1773140000)
        with serving(provider) as base:
            data = get(base, "/readings", {
                "meter": "m",
                "start": "2026-03-08T10:00:00Z",
                "end": "2026-03-08T13:00:00Z",
            })
            self.assertEqual(len(data["rows"]), 3)
            self.assertTrue(data["complete"])
            self.assertIsNotNone(data["coverage"])

    def test_declared_gap_prevents_coverage_evidence(self):
        provider = CollectionProvider(clock=lambda: 1773140000)
        with serving(provider) as base:
            post(base, "/admin/gap", {
                "start": "2026-03-08T12:00:00Z",
                "end": "2026-03-08T13:00:00Z",
            })
            data = get(base, "/readings", {
                "meter": "m",
                "start": "2026-03-08T10:00:00Z",
                "end": "2026-03-08T14:00:00Z",
            })
            # 缺口那一小时没有读数，覆盖在缺口前停止，complete=false
            self.assertFalse(data["complete"])
            self.assertIsNone(data["coverage"])
            self.assertEqual(len(data["rows"]), 2)

    def test_downtime_returns_503_and_requests_are_logged(self):
        provider = CollectionProvider(clock=lambda: 1773140000)
        with serving(provider) as base:
            post(base, "/admin/down", {
                "start": "2026-03-08T00:00:00Z",
                "end": "2026-03-10T00:00:00Z",
            })
            import urllib.error

            with self.assertRaises(urllib.error.HTTPError) as caught:
                get(base, "/readings", {
                    "meter": "m",
                    "start": "2026-03-08T10:00:00Z",
                    "end": "2026-03-08T12:00:00Z",
                })
            self.assertEqual(caught.exception.code, 503)
            requests = get(base, "/requests")
            self.assertEqual(requests[0]["meter"], "m")

    def test_limit_caps_rows_and_blocks_completeness(self):
        provider = CollectionProvider(clock=lambda: 1773140000)
        with serving(provider) as base:
            data = get(base, "/readings", {
                "meter": "m", "limit": "2",
                "start": "2026-03-08T10:00:00Z",
                "end": "2026-03-08T14:00:00Z",
            })
            self.assertEqual(len(data["rows"]), 2)
            self.assertFalse(data["complete"])


if __name__ == "__main__":
    unittest.main()
