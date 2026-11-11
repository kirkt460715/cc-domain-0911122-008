import unittest
from lab.http import serving
from lab.provider import Provider
from lab.demo import request
class SourceProtocol(unittest.TestCase):
    def test_source_roundtrip(self):
        with serving(Provider()) as base:
            result=request(base,'/requests',None)
            self.assertIsNotNone(result)

    def test_real_time_windows_span_dst_and_budget(self):
        from urllib.parse import urlencode
        with serving(Provider()) as base:
            args={'meter':'m1','segment':'segment1','start':'2026-11-01T00:00:00-04:00','end':'2026-11-02T00:00:00-05:00'}
            data=request(base,'/readings?'+urlencode(args))
            self.assertEqual(len(data['rows']),25)
            self.assertTrue(data['complete'])
            args['end']='2026-11-10T00:00:00-05:00';data=request(base,'/readings?'+urlencode(args))
            self.assertEqual(len(data['rows']),100)
            self.assertFalse(data['complete']);self.assertIsNone(data['coverage'])
