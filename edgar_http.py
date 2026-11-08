# -*- coding: utf-8 -*-
"""
edgar_http.py — SEC EDGAR 共用連線(2026-11-08,美股法說擴充 Phase 1)
============================================================
規則(規格第 0 節):
  - User-Agent 只讀環境變數 EDGAR_USER_AGENT(GitHub Secret,「名稱 聯絡方式」);缺少 → MissingUA,呼叫端停止。
    不得自行編造,也不寫入任何輸出或 log。
  - 自我限速 ≤ 4 次/秒(官方上限 10);gzip。
  - 403 / 429 → 指數退避(1、2、4 秒);連續 3 次失敗 → EdgarAbort,停止整個執行並回報,不換 header、不繞過。
  - 先讀 robots.txt。
"""
import os
import time
from urllib import robotparser
from urllib.parse import urlsplit

import requests

MIN_GAP = 0.25


class MissingUA(Exception):
    pass


class EdgarAbort(Exception):
    pass


class Edgar:
    def __init__(self, ua=None, session=None, sleep=time.sleep, clock=time.time):
        ua = ua if ua is not None else os.environ.get("EDGAR_USER_AGENT", "").strip()
        if not ua:
            raise MissingUA("缺 EDGAR_USER_AGENT(GitHub Secret,格式「名稱 聯絡方式」),停止;不自行編造")
        self.ua = ua
        self.s = session or requests.Session()
        self.sleep, self.clock = sleep, clock
        self.last = None
        self.fail = 0
        self.count = 0
        self.robots = {}

    def _raw(self, url):
        if self.last is not None:
            w = MIN_GAP - (self.clock() - self.last)
            if w > 0:
                self.sleep(w)
        try:
            return self.s.get(url, headers={"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"}, timeout=60)
        finally:
            self.last = self.clock()
            self.count += 1

    def _robots_ok(self, url):
        host = urlsplit(url).netloc
        if host not in self.robots:
            r = self._raw("https://%s/robots.txt" % host)
            if r.status_code == 200 and "user-agent" in r.text.lower():
                rp = robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
                self.robots[host] = rp
            else:
                self.robots[host] = None
        rp = self.robots[host]
        return True if rp is None else (rp.can_fetch(self.ua, url) and rp.can_fetch("*", url))

    def get(self, url):
        """→ Response(200 / 404 等由呼叫端判斷);403/429 退避,連續 3 次 → EdgarAbort"""
        if not self._robots_ok(url):
            raise EdgarAbort("robots.txt 不允許:" + url)
        for i in range(3):
            try:
                r = self._raw(url)
            except requests.RequestException as e:
                self.fail += 1
                if self.fail >= 3:
                    raise EdgarAbort("連線失敗 3 次:%s %s" % (type(e).__name__, url))
                self.sleep(2 ** i)
                continue
            if r.status_code in (403, 429):
                self.fail += 1
                if self.fail >= 3:
                    raise EdgarAbort("EDGAR 連續 3 次 HTTP %s,停止:%s" % (r.status_code, url))
                self.sleep(2 ** i)
                continue
            self.fail = 0
            return r
        raise EdgarAbort("EDGAR 重試 3 次仍失敗:" + url)
