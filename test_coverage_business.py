# -*- coding: utf-8 -*-
import os
import shutil
import tempfile
import unittest

import coverage_business as C

MD = """# 2408 - 南亞科

## 業務簡介
**板塊:** Technology
**產業:** Semiconductors
**市值:** 701,839 百萬台幣

南亞科技隸屬於**[[台塑]]集團**。公司是台灣最大的 **DRAM** 製造商,專注於利基型 [[DRAM|動態記憶體]]。

## 供應鏈位置
**上游 (設備/原料):**
- **[[矽晶圓]]:** 台勝科.

## 財務概況
| x |
"""


class T(unittest.TestCase):
    def test_parse(self):
        r = C.parse_report(MD)
        self.assertEqual(r["industry"], "Semiconductors")
        self.assertEqual(r["biz"], "南亞科技隸屬於台塑集團。公司是台灣最大的 DRAM 製造商,專注於利基型 動態記憶體。")
        self.assertEqual(r["supply"], "上游 (設備/原料):; 矽晶圓: 台勝科.")
        self.assertIsNone(C.parse_report("# x\n## 財務概況\n"))

    def test_build(self):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "Semiconductors"))
            with open(os.path.join(d, "Semiconductors", "2408_南亞科.md"), "w", encoding="utf-8") as f:
                f.write(MD)
            with open(os.path.join(d, "Semiconductors", "README.md"), "w", encoding="utf-8") as f:
                f.write(MD)
            out = C.build(d, "abc")
            self.assertEqual(list(out["stocks"]), ["2408"])
            self.assertEqual((out["stocks"]["2408"]["name"], out["commit"]), ("南亞科", "abc"))
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
