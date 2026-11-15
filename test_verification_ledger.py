# -*- coding: utf-8 -*-
import unittest

import verification_ledger as V


def claim(**kw):
    c = {"code": "1101", "theme": "x", "detection_type": "keyword_surge", "status": "refuted",
         "flagged_date": "20261001", "flagged_price": 100.0, "judged_date": "20261031", "return_pct": -5.0}
    c.update(kw)
    return c


class T(unittest.TestCase):
    def test_row_date(self):
        self.assertEqual(V._row_date({"Date": "1151113"}), "20261113")
        self.assertEqual(V._row_date({"Date": "115/11/13"}), "20261113")
        self.assertEqual(V._row_date({"Date": "20261113"}), "20261113")
        self.assertIsNone(V._row_date({}))

    def test_index_returns(self):
        idx = [("20260930", 100.0), ("20261001", 100.0), ("20261030", 90.0), ("20261102", 95.0)]
        L = [claim(),                                                       # 10/31 非交易日 → 用 10/30
             claim(flagged_price_date="20260930", judged_date="20261102"),
             claim(judged_date="20261110"),                                 # 指數快取還沒到 → 先不填
             claim(status="pending", return_pct=None)]
        self.assertEqual(V.add_index_returns(L, idx), 2)
        self.assertEqual((L[0]["idx_return_pct"], L[0]["excess_pct"]), (-10.0, 5.0))
        self.assertEqual(L[1]["idx_return_pct"], -5.0)
        self.assertNotIn("idx_return_pct", L[2])
        self.assertEqual(V.add_index_returns(L, idx), 0)                  # 已填過不重算
        self.assertEqual(V.add_index_returns(L, []), 0)

    def test_summary_by_date(self):
        L = [claim(flagged_date="d1", excess_pct=2.0, return_pct=12.0, status="confirmed"),
             claim(flagged_date="d1", excess_pct=4.0),
             claim(flagged_date="d2", excess_pct=-6.0),
             claim(flagged_date="d3")]                                      # 沒有超額報酬
        s = V.build_summary(L)["by_detection_type"]["keyword_surge"]
        self.assertEqual((s["total_judged"], s["n_dates"], s["n_excess"]), (4, 3, 3))
        self.assertEqual(s["excess_median_pct"], 2.0)
        self.assertEqual(s["excess_date_mean_pct"], -1.5)                   # (3 + −6) / 2:依標記日等權
        self.assertEqual(s["win_rate_pct"], 25.0)


if __name__ == "__main__":
    unittest.main()
