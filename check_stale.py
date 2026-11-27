#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查站点数据是否过期，供 cron / 监控调用。

用法：
    python check_stale.py                       # 默认检查 https://gfs.bamc.top
    python check_stale.py --url http://127.0.0.1:8787 --max-age 12
    python check_stale.py --product gfs-t2m --max-age 8

退出码：0 = 正常，1 = 数据过期，2 = 请求失败。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def main() -> int:
    ap = argparse.ArgumentParser(description="检查预报图站点数据是否过期")
    ap.add_argument("--url", default="https://gfs.bamc.top")
    ap.add_argument("--product", default="gfs-t2m")
    ap.add_argument("--max-age", type=float, default=12.0, help="最大允许延迟（小时）")
    ap.add_argument("--timeout", type=float, default=25.0)
    args = ap.parse_args()

    base = args.url.rstrip("/")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", UA)]

    try:
        opener.open(base + "/", timeout=args.timeout).read()
        raw = opener.open(
            "%s/api/frames?p=%s" % (base, args.product), timeout=args.timeout
        ).read()
        data = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print("HTTP %s: %s" % (exc.code, exc.reason))
        return 2
    except Exception as exc:  # noqa: BLE001
        print("请求失败：%r" % exc)
        return 2

    run_text = str(data.get("run") or "").replace(" UTC", "").strip()
    if not run_text:
        print("接口没有返回 run 字段")
        return 2
    try:
        run_dt = datetime.strptime(run_text, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        print("无法解析 run：%s" % run_text)
        return 2

    age_h = (datetime.now(timezone.utc) - run_dt).total_seconds() / 3600.0
    print("run=%s  age=%.1fh  frames=%s  limit=%.1fh"
          % (run_text, age_h, data.get("count"), args.max_age))
    if age_h > args.max_age:
        print("过期：数据已 %.1f 小时未更新" % age_h)
        return 1
    print("正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
