"""
生成「华南 NOAA GFS 预报图」系列（16 类产品 × 37 帧）：

  - combined: 500hPa 高度 + 850hPa 风 + 6 小时降雨
  - t2m:      2m 气温
  - h500:     500hPa 高度 + 500hPa 风
  - rain24:   24 小时降雨
  - mslp:     海平面气压
  - wind10:   10m 风场
  - wind925:  925hPa 风场
  - gust24:   24 小时极大阵风
  - cape:     CAPE
  - kindex:   K 指数
  - pwat:     可降水量（PWAT）
  - rh2m:     2m 相对湿度
  - rh500:    500hPa 相对湿度
  - cloud:    总云量

数据来自 NOAA NOMADS 的 GFS：华南用 0.25°、东亚用 1.0°，
通过 NOMADS filter CGI 按区域和字段做子集下载，eccodes 解码。
输出为静态 PNG 和每个产品自己的 manifest.json，供 server.py 直接读取。

用法：
  python gfs_chart.py                         # 自动选择最新可用起报场并抓取
  python gfs_chart.py --run 2026-11-17T00:00  # 指定起报场
  python gfs_chart.py --products t2m,h500     # 只更新部分产品

依赖（仅生成时需要）：
  numpy matplotlib cartopy eccodes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "gfs"
TZ_CN = timezone(timedelta(hours=8))
UTC = timezone.utc
# NOMADS 要求客户端在 UA 里标明身份和联系方式；用浏览器 UA 抓数据容易被判滥用。
UA = "bamc-gfs/1.0 (https://gfs.bamc.top; contact benjamin1207511@gmail.com)"
MIN_INTERVAL = float(os.environ.get("GFS_MIN_INTERVAL", "1.0"))  # 两次请求的最小间隔（秒）
_RATE_LOCK = threading.Lock()
_LAST_REQUEST = [0.0]

DOMAINS = {
    # 默认华南范围：16–28°N，105–121°E（0.5° 输入、0.25° 绘图，精度翻倍）
    "sc": {
        "west": 105.0, "south": 16.0, "east": 121.0, "north": 28.0,
        "fetch_step": 0.5, "fine_step": 0.25, "gfs": "0p25",
    },
    # 东亚范围：0–50°N，80–140°E
    "ea": {
        "west": 80.0, "south": 0.0, "east": 140.0, "north": 50.0,
        "fetch_step": 2.0, "fine_step": 0.5, "gfs": "1p00",
    },
}
DEFAULT_DOMAIN = "sc"
WEST, SOUTH, EAST, NORTH = 105.0, 16.0, 121.0, 28.0
FINE_STEP = 0.25
# 240h 数据量较大，下载网格按 domain 的 fetch_step；绘图用 fine_step。
LEADS = list(range(24, 241, 6))           # +24h … +240h，每 6 小时一帧，共 37 帧
OVERVIEW_STEPS = list(range(24, 241, 12))  # 总览：每 12 小时一个小图，共 19 个
GFS_FILTER = {
    "0p25": "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl",
    "1p00": "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_1p00.pl",
}
STEP_HOURS = 6
MAX_LEAD = 384

PRODUCTS = {
    "combined": {
        "subdir": "combined",
        "prefix": "gfs_southchina_",
        "title": "500hPa Height & 850hPa Wind & 6h Rainfall",
        "kind": "combined",
        "domain": "sc",
    },
    "combined_ea": {
        "subdir": "combined-ea",
        "prefix": "gfs_southchina_ea_",
        "title": "East Asia · 500hPa Height & 850hPa Wind & 6h Rainfall",
        "kind": "combined",
        "domain": "ea",
    },
    "t2m": {
        "subdir": "t2m",
        "prefix": "gfs_t2m_",
        "title": "2m Temperature",
        "kind": "t2m",
    },
    "h500": {
        "subdir": "h500",
        "prefix": "gfs_h500_",
        "title": "Wind & Height at 500hPa",
        "kind": "h500",
        "domain": "sc",
    },
    "h500_ea": {
        "subdir": "h500-ea",
        "prefix": "gfs_h500_ea_",
        "title": "East Asia · Wind & Height at 500hPa",
        "kind": "h500",
        "domain": "ea",
    },
    "rain24": {
        "subdir": "rain24",
        "prefix": "gfs_rain24_",
        "title": "24h Precipitation (mm)",
        "kind": "rain24",
    },
    "mslp": {
        "subdir": "mslp",
        "prefix": "gfs_mslp_",
        "title": "Sea-level Pressure",
        "kind": "mslp",
    },
    "wind10": {
        "subdir": "wind10",
        "prefix": "gfs_wind10_",
        "title": "10m Wind",
        "kind": "wind10",
    },
    "wind925": {
        "subdir": "wind925",
        "prefix": "gfs_wind925_",
        "title": "925hPa Wind",
        "kind": "wind925",
    },
    "gust24": {
        "subdir": "gust24",
        "prefix": "gfs_gust24_",
        "title": "24h Max Wind Gust",
        "kind": "gust24",
    },
    "cape": {
        "subdir": "cape",
        "prefix": "gfs_cape_",
        "title": "CAPE",
        "kind": "cape",
    },
    "kindex": {
        "subdir": "kindex",
        "prefix": "gfs_kindex_",
        "title": "K Index",
        "kind": "kindex",
    },
    "pwat": {
        "subdir": "pwat",
        "prefix": "gfs_pwat_",
        "title": "PWAT",
        "kind": "pwat",
    },
    "rh2m": {
        "subdir": "rh2m",
        "prefix": "gfs_rh2m_",
        "title": "2m Relative Humidity",
        "kind": "rh2m",
    },
    "rh500": {
        "subdir": "rh500",
        "prefix": "gfs_rh500_",
        "title": "500hPa Relative Humidity",
        "kind": "rh500",
    },
    "cloud": {
        "subdir": "cloud",
        "prefix": "gfs_cloud_",
        "title": "Total Cloud Cover",
        "kind": "cloud",
    },
}

RAIN_6H_LEVELS = [0.1, 1, 4, 13, 25, 60, 120, 250]
RAIN_6H_COLORS = [
    "#f4f9f0", "#b9e3ad", "#77c96d", "#36a35a",
    "#5d8bd9", "#294cb0", "#b2188f",
]
RAIN_24H_LEVELS = [0.1, 1, 10, 25, 50, 100, 250]
RAIN_24H_COLORS = ["#f7f7f7", "#b7e6b0", "#4daf4a", "#377eb8", "#00008b", "#ff00ff"]

TEMP_LEVELS = [-5, 0, 5, 10, 15, 20, 25, 27, 29, 31, 33, 35]
TEMP_COLORS = [
    "#313695", "#4575b4", "#74add1", "#abd9e9", "#e0f3f8", "#ffffbf",
    "#fee090", "#fdae61", "#f46d43", "#d73027", "#a50026",
]

HEIGHT_FILL_LEVELS = [5760, 5800, 5840, 5880, 5920, 5960]
HEIGHT_FILL_COLORS = ["#d9f0a3", "#ffffbf", "#fee391", "#fec44f", "#fe9929"]

MSLP_LEVELS = [985, 990, 992.5, 995, 997.5, 1000, 1002.5, 1005,
               1007.5, 1010, 1012.5, 1015, 1017.5, 1020, 1022.5,
               1025, 1027.5, 1030]

# 10m 风场 / 24h 阵风统一使用参考图右侧的 15 级风速色阶。
WIND_SPEED_LEVELS = [0.3, 3.4, 8, 10.8, 13.9, 17.2, 20.8, 24.5,
                     28.5, 32.7, 37.2, 41.5, 46.2, 51, 56.1]
WIND_SPEED_LABELS = ["0.3", "3.4", "8", "10.8", "13.9", "17.2", "20.8",
                     "24.5", "28.5", "32.7", "37.2", "41.5", "46.2", "51", "56.1"]
WIND_SPEED_COLORS = [
    "#d6e2ff", "#b5c9ff", "#8eb2ff", "#7f96ff", "#6370f7",
    "#0063ff", "#009696", "#63ff00", "#96ff00", "#c6ff33",
    "#ffff00", "#ffc600", "#ffa000", "#ff7c00",
]
WIND925_LEVELS = [0, 5, 10, 15, 20, 25, 30, 35, 40, 50]
CAPE_LEVELS = [0, 100, 250, 500, 1000, 1500, 2000, 2500, 3000, 4000, 5000]
VORT_LEVELS = [-20, -15, -12, -9, -6, -4, -2, 0, 2, 4, 6, 9, 12, 15, 20]
T850_LEVELS = [-30, -25, -20, -15, -10, -5, 0, 5, 10, 15, 20, 25, 30]
K_LEVELS = [-10, 0, 10, 15, 20, 25, 30, 35, 40, 45, 50]
PWAT_LEVELS = [0, 5, 10, 15, 20, 25, 30, 35, 40, 50, 60, 70]
RH_LEVELS = [0, 20, 30, 40, 50, 60, 70, 80, 85, 90, 95, 100]

# 总云量：0–100% 映射到 0–10 成；色阶参考「Total Cloud Cover」参考图的 4–9 级蓝色渐变。
CLOUD_LEVELS = [40, 50, 60, 70, 80, 90]
CLOUD_LABELS = ["4", "5", "6", "7", "8", "9"]
CLOUD_COLORS = ["#EAFCFC", "#BAF6FC", "#96EAFC", "#78D2FC", "#54AEFC"]
CLOUD_OVER = "#3684FC"


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def grid_points(step: float = FINE_STEP, domain: str | None = None):
    d = DOMAINS[domain or DEFAULT_DOMAIN]
    lats = []
    y = d["south"]
    while y <= d["north"] + 1e-9:
        lats.append(round(y, 2))
        y += step
    lons = []
    x = d["west"]
    while x <= d["east"] + 1e-9:
        lons.append(round(x, 2))
        x += step
    return lats, lons


def _throttle() -> None:
    """全局限速：所有线程合计每 MIN_INTERVAL 秒最多发一个请求。"""
    with _RATE_LOCK:
        wait = MIN_INTERVAL - (time.time() - _LAST_REQUEST[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST[0] = time.time()


def fetch(url: str, timeout: int = 180) -> bytes:
    _throttle()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Referer": "https://nomads.ncep.noaa.gov/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


RETRY_DELAYS = (5, 10, 20, 30, 45, 60)
FETCH_WORKERS = max(1, int(os.environ.get("GFS_FETCH_WORKERS", "2")))
_ECC_LOCK = threading.Lock()


def _fmt_run(run_dt: datetime) -> str:
    return run_dt.strftime("%Y-%m-%dT%H:%M")


def _parse_run(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)


def _gfs_params(run_dt: datetime, domain: str, step: int, light: bool = False) -> dict:
    d = DOMAINS[domain]
    res = d["gfs"]
    params = {
        "dir": "/gfs.%s/%02d/atmos" % (run_dt.strftime("%Y%m%d"), run_dt.hour),
        "file": "gfs.t%02dz.pgrb2.%s.f%03d" % (run_dt.hour, res, step),
        "subregion": "",
        "leftlon": "%g" % d["west"], "rightlon": "%g" % d["east"],
        "toplat": "%g" % d["north"], "bottomlat": "%g" % d["south"],
    }
    if light:
        params.update({"var_TMP": "on", "lev_2_m_above_ground": "on"})
        return params
    for var in ("TMP", "RH", "UGRD", "VGRD", "GUST", "PRMSL", "APCP",
                "CAPE", "PWAT", "TCDC", "HGT"):
        params["var_" + var] = "on"
    for lev in ("2_m_above_ground", "10_m_above_ground", "surface",
                "mean_sea_level", "entire_atmosphere",
                "entire_atmosphere_(considered_as_a_single_layer)",
                "500_mb", "700_mb", "850_mb", "925_mb"):
        params["lev_" + lev] = "on"
    return params


def probe_run(run_iso: str, step: int = 240) -> bool:
    run_dt = _parse_run(run_iso)
    params = _gfs_params(run_dt, "sc", step, light=True)
    url = GFS_FILTER["0p25"] + "?" + urllib.parse.urlencode(params)
    try:
        raw = fetch(url, timeout=90)
    except urllib.error.HTTPError:
        return False
    except Exception:  # noqa: BLE001
        return False
    return len(raw) > 500


def latest_run() -> str:
    """最新的、已经出到 +240h 的 GFS 起报场（只取 00Z / 12Z）。"""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    block = now.replace(hour=(now.hour // 12) * 12)
    for offset in range(0, 96, 12):
        cand = block - timedelta(hours=offset)
        run = _fmt_run(cand)
        log("检查 GFS 起报场 %s" % run)
        if probe_run(run):
            log("使用 GFS 起报场 %s" % run)
            return run
    raise RuntimeError("没有找到可用的 GFS 起报场")


def _cache_path(run_iso: str, domain: str, step: int) -> Path:
    safe = run_iso.replace(":", "").replace("-", "")
    return BASE_DIR / "_cache" / "grib" / safe / ("%s_%03d.grib" % (domain, step))


def _download_step(run_dt: datetime, domain: str, step: int) -> Path:
    run_iso = _fmt_run(run_dt)
    path = _cache_path(run_iso, domain, step)
    if path.is_file() and path.stat().st_size > 1024:
        return path
    params = _gfs_params(run_dt, domain, step)
    url = GFS_FILTER[DOMAINS[domain]["gfs"]] + "?" + urllib.parse.urlencode(params)
    last_error = None
    for delay in (*RETRY_DELAYS, None):
        try:
            raw = fetch(url, timeout=300)
            if len(raw) < 1000 or raw[:4] != b"GRIB":
                raise RuntimeError("返回内容不是 GRIB（%d 字节）" % len(raw))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            return path
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            text = str(exc)
            limited = "403" in text or "429" in text or "Too Many" in text
            log("  %s step %3dh 第 %d 次失败：%s%s"
                % (domain, step, 1, exc, "（疑似被限流，等待 5 分钟）" if limited else ""))
            if delay is None:
                break
            time.sleep(300 if limited else delay)
    raise RuntimeError("%s step %d 下载失败：%s" % (domain, step, last_error))


def _span_hours(step_range: str) -> int:
    if "-" in step_range:
        left, right = step_range.split("-", 1)
        try:
            return int(right) - int(left)
        except ValueError:
            return 0
    return 0


def _decode(path: Path) -> dict:
    # ecCodes 的定义表初始化不是线程安全的，多线程并发解码会直接断言失败，这里串行化。
    with _ECC_LOCK:
        return _decode_locked(path)


def _decode_locked(path: Path) -> dict:
    import eccodes
    import numpy as np

    out: dict = {}
    with path.open("rb") as fh:
        while True:
            gid = eccodes.codes_grib_new_from_file(fh)
            if gid is None:
                break
            try:
                short = eccodes.codes_get(gid, "shortName")
                ltype = eccodes.codes_get(gid, "typeOfLevel")
                level = int(eccodes.codes_get(gid, "level"))
                step_range = eccodes.codes_get(gid, "stepRange")
                ni = eccodes.codes_get(gid, "Ni")
                nj = eccodes.codes_get(gid, "Nj")
                lat0 = eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
                lon0 = eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
                values = np.asarray(eccodes.codes_get_values(gid), dtype="float32").reshape(nj, ni)
            finally:
                eccodes.codes_release(gid)
            key = (short, ltype, level)
            span = _span_hours(step_range)
            if key not in out or span < out[key][0]:
                out[key] = (span, values, lat0, lon0)
    return out


def _sample(field, lat0, lon0, step_deg, lats, lons):
    import numpy as np

    j = np.rint((np.asarray(lats, dtype=float) - lat0) / step_deg).astype(int)
    i = np.rint((np.asarray(lons, dtype=float) - lon0) / step_deg).astype(int)
    return field[j, i]


def _dewpoint(t_c, rh):
    import numpy as np

    rh = np.clip(np.asarray(rh, dtype=float), 1.0, 100.0)
    a, b = 17.67, 243.5
    gamma = np.log(rh / 100.0) + a * t_c / (b + t_c)
    return b * gamma / (a - gamma)


def _wind(u, v):
    import numpy as np

    speed = np.hypot(u, v) * 1.943844     # m/s -> kn
    direction = (270.0 - np.degrees(np.arctan2(v, u))) % 360.0
    return speed, direction


def _step_values(run_dt: datetime, domain: str, step: int, lats, lons) -> dict:
    import numpy as np

    path = _download_step(run_dt, domain, step)
    rec = _decode(path)
    step_deg = 0.25 if DOMAINS[domain]["gfs"] == "0p25" else 1.0

    def get(short, ltype, level):
        item = rec.get((short, ltype, level))
        if item is None:
            raise RuntimeError("%s step %d 缺少字段 %s/%s/%s"
                               % (domain, step, short, ltype, level))
        return _sample(item[1], item[2], item[3], step_deg, lats, lons).astype(float)

    t2m = get("2t", "heightAboveGround", 2) - 273.15
    rh2m = get("2r", "heightAboveGround", 2)
    u10 = get("10u", "heightAboveGround", 10)
    v10 = get("10v", "heightAboveGround", 10)
    gust = get("gust", "surface", 0)
    msl = get("prmsl", "meanSea", 0) / 100.0
    rain = get("tp", "surface", 0)
    cape = get("cape", "surface", 0)
    pwat = get("pwat", "atmosphereSingleLayer", 0)
    tcc = get("tcc", "atmosphere", 0) * 100.0
    gh500 = get("gh", "isobaricInhPa", 500)
    t500 = get("t", "isobaricInhPa", 500) - 273.15
    t700 = get("t", "isobaricInhPa", 700) - 273.15
    t850 = get("t", "isobaricInhPa", 850) - 273.15
    r500 = get("r", "isobaricInhPa", 500)
    r700 = get("r", "isobaricInhPa", 700)
    r850 = get("r", "isobaricInhPa", 850)
    u500 = get("u", "isobaricInhPa", 500)
    v500 = get("v", "isobaricInhPa", 500)
    u850 = get("u", "isobaricInhPa", 850)
    v850 = get("v", "isobaricInhPa", 850)
    u925 = get("u", "isobaricInhPa", 925)
    v925 = get("v", "isobaricInhPa", 925)

    ws500, wd500 = _wind(u500, v500)
    ws850, wd850 = _wind(u850, v850)
    ws925, wd925 = _wind(u925, v925)
    ws10, wd10 = _wind(u10, v10)

    return {
        "temperature_2m": t2m,
        "relative_humidity_2m": rh2m,
        "precipitation": np.maximum(rain, 0.0),
        "pressure_msl": msl,
        "geopotential_height_500hPa": gh500,
        "wind_speed_500hPa": ws500, "wind_direction_500hPa": wd500,
        "wind_speed_850hPa": ws850, "wind_direction_850hPa": wd850,
        "wind_speed_925hPa": ws925, "wind_direction_925hPa": wd925,
        "wind_speed_10m": ws10, "wind_direction_10m": wd10,
        "wind_gusts_10m": gust * 1.943844,
        "cape": cape,
        "total_column_integrated_water_vapour": pwat,
        "cloud_cover": np.clip(tcc, 0.0, 100.0),
        "temperature_500hPa": t500,
        "temperature_700hPa": t700,
        "temperature_850hPa": t850,
        "relative_humidity_500hPa": r500,
        "dew_point_700hPa": _dewpoint(t700, r700),
        "dew_point_850hPa": _dewpoint(t850, r850),
    }


def fetch_grid(run: str, domain_name: str) -> dict:
    """下载一个 GFS 起报场，返回 {"run":..., "points":[...]}（6 小时间隔）。"""
    run_dt = _parse_run(run)
    d = DOMAINS[domain_name]
    lats, lons = grid_points(d["fetch_step"], domain_name)
    points = [(la, lo) for la in lats for lo in lons]
    p_lats = [p[0] for p in points]
    p_lons = [p[1] for p in points]
    steps = list(range(STEP_HOURS, 241, STEP_HOURS))
    times = [(run_dt + timedelta(hours=s)).strftime("%Y-%m-%dT%H:%M") for s in steps]

    log("请求 NOAA GFS（%s）：%d 个点 × %d 个时次，%d 并发"
        % (domain_name, len(points), len(steps), FETCH_WORKERS))
    started = time.time()
    per_step: dict[int, dict] = {}

    with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(steps))) as pool:
        futures = {pool.submit(_step_values, run_dt, domain_name, s, p_lats, p_lons): s
                   for s in steps}
        for future in as_completed(futures):
            step = futures[future]
            per_step[step] = future.result()
            log("  step %3dh 完成（%.0fs）" % (step, time.time() - started))

    keys = list(per_step[steps[0]].keys())
    out_points = []
    for k, (la, lo) in enumerate(points):
        hourly = {"time": times}
        for key in keys:
            hourly[key] = [float(per_step[s][key][k]) for s in steps]
        out_points.append({"latitude": la, "longitude": lo, "hourly": hourly})
    log("%s 网格完成：%d 个点，%.1fs" % (domain_name, len(out_points), time.time() - started))
    return {"run": run, "points": out_points}


def prepare_grid(data: dict, domain: str | None = None):
    import numpy as np

    d = DOMAINS[domain or DEFAULT_DOMAIN]
    points = data["points"]
    lat = np.asarray([p["latitude"] for p in points], dtype=float)
    lon = np.asarray([p["longitude"] for p in points], dtype=float)
    times = points[0]["hourly"]["time"]
    lats, lons = grid_points(d["fine_step"], domain or DEFAULT_DOMAIN)
    target_lon, target_lat = np.meshgrid(np.asarray(lons), np.asarray(lats))

    # 源网格是规则的 0.5° / 2.5° 经纬网格，用双线性插值即可（比 griddata 快，且不需要 scipy）。
    src_lats, src_lons = grid_points(d["fetch_step"], domain or DEFAULT_DOMAIN)
    src_lats = np.asarray(src_lats, dtype=float)
    src_lons = np.asarray(src_lons, dtype=float)
    ys = np.asarray(lats, dtype=float)
    xs = np.asarray(lons, dtype=float)

    def interpolate(values):
        field = np.asarray(values, dtype=float).reshape(src_lats.size, src_lons.size)
        along_lon = np.empty((src_lats.size, xs.size), dtype=float)
        for i in range(src_lats.size):
            along_lon[i] = np.interp(xs, src_lons, field[i])
        out = np.empty((ys.size, xs.size), dtype=float)
        for j in range(xs.size):
            out[:, j] = np.interp(ys, src_lats, along_lon[:, j])
        return out

    return {
        "points": points,
        "lat": lat,
        "lon": lon,
        "times": times,
        "target_lon": target_lon,
        "target_lat": target_lat,
        "interpolate": interpolate,
    }


def safe_values(values, idx):
    out = values[idx]
    return float(out) if out is not None else 0.0


def accumulate(points, key: str, idx: int, hours: int, first_idx: int = 0):
    n = max(1, hours // STEP_HOURS)
    start = max(0, first_idx, idx - n + 1)
    total = []
    for p in points:
        vals = p["hourly"][key][start:idx + 1] or []
        total.append(sum(float(v) for v in vals if v is not None))
    return total


def maximum_over(points, key: str, idx: int, hours: int):
    values = []
    for p in points:
        vals = [float(v) for v in (p["hourly"][key][max(0, idx - hours + 1):idx + 1] or []) if v is not None]
        values.append(max(vals) if vals else 0.0)
    return values


def wind_uv(points, speed_key: str, direction_key: str, idx: int):
    import numpy as np

    speed = np.asarray([safe_values(p["hourly"][speed_key], idx) for p in points])
    direction = np.asarray([safe_values(p["hourly"][direction_key], idx) for p in points])
    u = -speed * np.sin(np.deg2rad(direction))
    v = -speed * np.cos(np.deg2rad(direction))
    return u, v


def base_map(ax, ccrs, cfeature, domain: str = DEFAULT_DOMAIN):
    import numpy as np

    d = DOMAINS[domain]
    ax.set_extent(
        [d["west"], d["east"], d["south"], d["north"]],
        crs=ccrs.PlateCarree(),
    )
    # 10m 地图包含更完整的海岸线、岛屿、湖泊和省界。
    ax.add_feature(cfeature.OCEAN.with_scale("10m"), facecolor="#f4f8fb", edgecolor="none", zorder=0)
    ax.add_feature(cfeature.LAND.with_scale("10m"), facecolor="#f5f0e4", edgecolor="none", zorder=0)
    try:
        ax.add_feature(
            cfeature.LAKES.with_scale("10m"),
            facecolor="#f4f8fb", edgecolor="#7e8b93", linewidth=0.25, zorder=1,
        )
    except Exception:
        pass
    ax.add_feature(
        cfeature.BORDERS.with_scale("10m"),
        linewidth=0.55, linestyle=":", edgecolor="#9d978b", zorder=4,
    )
    try:
        ax.add_feature(
            cfeature.STATES.with_scale("50m"),
            linewidth=0.32, edgecolor="#aaa49a", zorder=4,
        )
    except Exception:
        pass
    ax.coastlines("10m", linewidth=0.6, color="#4d5a52", zorder=5)

    # 广东省界单独加粗标黄（先画一层暗色底衬，保证浅色底图上仍然清楚）。
    try:
        from cartopy.feature import ShapelyFeature
        from cartopy.io import shapereader

        provinces = shapereader.natural_earth(
            resolution="50m", category="cultural",
            name="admin_1_states_provinces",
        )
        guangdong = []
        for record in shapereader.Reader(provinces).records():
            attrs = record.attributes
            labels = [
                str(attrs.get(key) or "")
                for key in ("name", "name_en", "name_local", "iso_3166_2")
            ]
            if any("guangdong" in label.lower() or label == "CN-GD" for label in labels):
                guangdong.append(record.geometry)
        if guangdong:
            ax.add_feature(
                ShapelyFeature(
                    guangdong, ccrs.PlateCarree(), facecolor="none",
                    edgecolor="#6b5a00", linewidth=4.0, alpha=0.35, zorder=9,
                )
            )
            ax.add_feature(
                ShapelyFeature(
                    guangdong, ccrs.PlateCarree(), facecolor="none",
                    edgecolor="#FFE000", linewidth=2.4, zorder=10,
                )
            )
    except Exception:
        pass

    step = 1 if domain == "sc" else (10 if (d["east"] - d["west"]) > 30 else 5)
    xlocs = np.arange(np.ceil(d["west"] / step) * step, d["east"] + 0.1, step)
    ylocs = np.arange(np.ceil(d["south"] / step) * step, d["north"] + 0.1, step)
    gl = ax.gridlines(
        draw_labels=True, xlocs=xlocs, ylocs=ylocs,
        linewidth=0.35, color="#9aa4ad", linestyle="--", alpha=0.5, zorder=2,
    )
    gl.top_labels = False
    gl.right_labels = False
    gl.xlabel_style = {"size": 8}
    gl.ylabel_style = {"size": 8}
    cities = {
        "Guangzhou": (113.26, 23.13),
        "Hong Kong": (114.17, 22.32),
        "Macau": (113.54, 22.20),
        "Haikou": (110.20, 20.04),
    }
    if domain == "ea":
        cities.update({
            "Beijing": (116.40, 39.90),
            "Shanghai": (121.47, 31.23),
            "Seoul": (126.98, 37.57),
            "Tokyo": (139.69, 35.68),
            "Taipei": (121.56, 25.03),
            "Manila": (120.98, 14.60),
            "Hanoi": (105.85, 21.03),
        })
    for name, (x, y) in cities.items():
        ax.plot(x, y, "o", ms=3, color="#222", transform=ccrs.PlateCarree(), zorder=11)
        ax.text(
            x + 0.06, y + 0.06, name,
            fontsize=6.5, color="#222", transform=ccrs.PlateCarree(), zorder=11,
        )
    return ax


def finish_figure(fig, ax, cf, colorbar_ticks, colorbar_labels, colorbar_title,
                  title: str, valid: datetime, run_dt: datetime, lead: int, out_path: Path):
    fig.text(
        0.065, 0.955, title,
        fontsize=16.5, fontweight="bold", ha="left", va="top",
    )
    fig.text(
        0.955, 0.955, valid.strftime("%Y/%m/%d %H(UTC)"),
        fontsize=11, ha="right", va="top",
    )
    fig.text(
        0.065, 0.923,
        "NOAA GFS 0.25°",
        fontsize=9.5, color="#555", ha="left", va="top",
    )
    fig.text(
        0.065, 0.032,
        "Init: %s  ·  Lead +%d h" % (run_dt.strftime("%Y-%m-%d %H UTC"), lead),
        fontsize=8, color="#666", ha="left", va="bottom",
    )
    fig.text(
        0.955, 0.032, "www.bamc.top",
        fontsize=9, color="#000000", ha="right", va="bottom",
    )
    cax = fig.add_axes([0.885, 0.16, 0.022, 0.62])
    cb = fig.colorbar(cf, cax=cax, ticks=colorbar_ticks)
    cb.ax.tick_params(labelsize=7.5, length=2)
    cb.set_ticklabels(colorbar_labels)
    cb.set_label(colorbar_title, fontsize=8, labelpad=6)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140, facecolor="white")
    log("已生成 %s（+%dh，%s）" % (out_path.name, lead, valid.strftime("%Y/%m/%d %H UTC")))


def render_product(data: dict, run_dt: datetime, lead: int, spec: dict, out_path: Path) -> None:
    import cartopy
    cartopy.config["data_dir"] = str(BASE_DIR / ".cartopy")

    import matplotlib
    matplotlib.use("Agg")
    import numpy as np
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    valid = run_dt + timedelta(hours=lead)
    valid_key = valid.strftime("%Y-%m-%dT%H:%M")
    domain = spec.get("domain", DEFAULT_DOMAIN)
    barb_step = 3 if domain == "ea" else 2  # 东亚 1.5°，华南 0.5°（0.25° 网格每 2 格一个）
    barb_slice = (slice(None, None, barb_step), slice(None, None, barb_step))
    grid = prepare_grid(data, domain)
    if valid_key not in grid["times"]:
        raise RuntimeError("网格中缺少有效时间 %s" % valid_key)
    idx = grid["times"].index(valid_key)
    # 序列从 +6h 开始，每个降水值本身就是 6 小时累计，直接按 6h 步长求和即可。
    first_idx = 0
    points = grid["points"]
    LON, LAT = grid["target_lon"], grid["target_lat"]
    kind = spec["kind"]

    fig = Figure(figsize=(10, 7.2), dpi=140, facecolor="white")
    FigureCanvasAgg(fig)
    ax = fig.add_axes([0.065, 0.075, 0.80, 0.80], projection=ccrs.PlateCarree())
    base_map(ax, ccrs, cfeature, domain)

    if kind == "combined":
        rain = grid["interpolate"](accumulate(points, "precipitation", idx, 6, first_idx))
        height = grid["interpolate"]([safe_values(p["hourly"]["geopotential_height_500hPa"], idx) for p in points])
        u, v = wind_uv(points, "wind_speed_850hPa", "wind_direction_850hPa", idx)
        u = grid["interpolate"](u)
        v = grid["interpolate"](v)
        cf = ax.contourf(
            LON, LAT, rain, levels=RAIN_6H_LEVELS, colors=RAIN_6H_COLORS,
            extend="max", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
        )
        cs = ax.contour(
            LON, LAT, height,
            levels=[lv for lv in np.arange(5640, 5941, 20) if lv != 5880],
            colors="#1b1b1b",
            linewidths=1.15, transform=ccrs.PlateCarree(), zorder=6,
        )
        ax.clabel(cs, inline=True, fontsize=7, fmt=lambda x: f"{x / 10:.0f}", colors="#1b1b1b")

        # 588 位势高度线单独标红，标签放在红线之上；没有 588 线时自动跳过。
        if np.nanmin(height) <= 5880 <= np.nanmax(height):
            cs588 = ax.contour(
                LON, LAT, height, levels=[5880], colors="#d62728",
                linewidths=2.4, transform=ccrs.PlateCarree(), zorder=8,
            )
            ax.clabel(cs588, inline=False, fontsize=8.5, fmt=lambda x: "588", colors="#d62728")

        # 850hPa 风速 >= 24.5 m/s（约 47.62 kn）的风矢标红，其余保持蓝色。
        speed_kn = np.hypot(u, v)
        high_wind = speed_kn >= 47.62
        barb_style = dict(
            transform=ccrs.PlateCarree(), length=5.8, linewidth=0.38,
            barb_increments={"half": 5, "full": 10, "flag": 50}, zorder=7,
        )
        u_barb = u[barb_slice]
        v_barb = v[barb_slice]
        high_barb = high_wind[barb_slice]
        lon_barb = LON[barb_slice]
        lat_barb = LAT[barb_slice]
        ax.barbs(lon_barb, lat_barb,
                 np.ma.masked_where(high_barb, u_barb),
                 np.ma.masked_where(high_barb, v_barb),
                 color="#2847c7", **barb_style)
        ax.barbs(lon_barb, lat_barb,
                 np.ma.masked_where(~high_barb, u_barb),
                 np.ma.masked_where(~high_barb, v_barb),
                 color="#e31a1c", **barb_style)
        finish_figure(
            fig, ax, cf, RAIN_6H_LEVELS,
            ["0.1", "1", "4", "13", "25", "60", "120", "250"],
            "6h rainfall (mm)", spec["title"], valid, run_dt, lead, out_path,
        )

    elif kind == "t2m":
        temp = grid["interpolate"]([safe_values(p["hourly"]["temperature_2m"], idx) for p in points])
        cf = ax.contourf(
            LON, LAT, temp, levels=TEMP_LEVELS, colors=TEMP_COLORS,
            extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
        )
        cs = ax.contour(
            LON, LAT, temp, levels=np.arange(0, 36, 2), colors="#7a5b34",
            linewidths=0.55, transform=ccrs.PlateCarree(), zorder=6,
        )
        ax.clabel(cs, inline=True, fontsize=6, fmt="%d", colors="#684c2b")
        finish_figure(
            fig, ax, cf, TEMP_LEVELS,
            [str(x) for x in TEMP_LEVELS], "2m temperature (°C)",
            spec["title"], valid, run_dt, lead, out_path,
        )

    elif kind == "h500":
        height = grid["interpolate"]([safe_values(p["hourly"]["geopotential_height_500hPa"], idx) for p in points])
        u, v = wind_uv(points, "wind_speed_500hPa", "wind_direction_500hPa", idx)
        u = grid["interpolate"](u)
        v = grid["interpolate"](v)
        cf = ax.contourf(
            LON, LAT, height, levels=HEIGHT_FILL_LEVELS, colors=HEIGHT_FILL_COLORS,
            extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
        )
        cs = ax.contour(
            LON, LAT, height, levels=np.arange(5700, 6001, 20), colors="#1b1b1b",
            linewidths=1.0, transform=ccrs.PlateCarree(), zorder=6,
        )
        ax.clabel(cs, inline=True, fontsize=7, fmt=lambda x: f"{x / 10:.0f}", colors="#1b1b1b")
        ax.barbs(
            LON[barb_slice], LAT[barb_slice], u[barb_slice], v[barb_slice],
            transform=ccrs.PlateCarree(), color="#14a3b8",
            length=5.8, linewidth=0.38,
            barb_increments={"half": 5, "full": 10, "flag": 50}, zorder=7,
        )
        finish_figure(
            fig, ax, cf, [5800, 5840, 5880, 5920, 5960],
            ["580", "584", "588", "592", "596"],
            "500hPa height (dam)", spec["title"], valid, run_dt, lead, out_path,
        )

    elif kind == "rain24":
        rain = grid["interpolate"](accumulate(points, "precipitation", idx, 24, first_idx))
        cf = ax.contourf(
            LON, LAT, rain, levels=RAIN_24H_LEVELS, colors=RAIN_24H_COLORS,
            extend="max", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
        )
        cs = ax.contour(
            LON, LAT, rain, levels=[1, 10, 25, 50, 100], colors="#385a3b",
            linewidths=0.55, transform=ccrs.PlateCarree(), zorder=6,
        )
        ax.clabel(cs, inline=True, fontsize=6, fmt="%g", colors="#244426")
        finish_figure(
            fig, ax, cf, RAIN_24H_LEVELS,
            ["0.1", "1", "10", "25", "50", "100", "250"],
            "24h rainfall (mm)", spec["title"], valid, run_dt, lead, out_path,
        )

    elif kind == "mslp":
        pressure = grid["interpolate"]([safe_values(p["hourly"]["pressure_msl"], idx) for p in points])
        cf = ax.contourf(
            LON, LAT, pressure, levels=MSLP_LEVELS, cmap="RdYlBu",
            extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
        )
        cs = ax.contour(
            LON, LAT, pressure, levels=MSLP_LEVELS, colors="#2a2a2a",
            linewidths=0.7, transform=ccrs.PlateCarree(), zorder=6,
        )
        ax.clabel(cs, inline=True, fontsize=6, fmt="%g", colors="#222222")
        finish_figure(
            fig, ax, cf, [990, 995, 1000, 1005, 1010, 1015, 1020, 1025, 1030],
            ["990", "995", "1000", "1005", "1010", "1015", "1020", "1025", "1030"],
            "MSLP (hPa)", spec["title"], valid, run_dt, lead, out_path,
        )

    elif kind in ("wind10", "wind100", "wind925"):
        if kind == "wind10":
            speed_key, dir_key = "wind_speed_10m", "wind_direction_10m"
            levels, labels, unit = WIND_SPEED_LEVELS, WIND_SPEED_LABELS, "10m wind (m/s)"
        elif kind == "wind100":
            speed_key, dir_key = "wind_speed_100m", "wind_direction_100m"
            levels, labels, unit = WIND_SPEED_LEVELS, WIND_SPEED_LABELS, "100m wind (m/s)"
        else:
            speed_key, dir_key = "wind_speed_925hPa", "wind_direction_925hPa"
            levels, labels, unit = WIND925_LEVELS, [str(x) for x in WIND925_LEVELS], "925hPa wind (m/s)"
        speed_kn = np.asarray([safe_values(p["hourly"][speed_key], idx) for p in points])
        speed_ms = grid["interpolate"](speed_kn * 0.514444)
        u, v = wind_uv(points, speed_key, dir_key, idx)
        u = grid["interpolate"](u)
        v = grid["interpolate"](v)
        if kind in ("wind10", "wind100"):
            cf = ax.contourf(
                LON, LAT, speed_ms, levels=levels, colors=WIND_SPEED_COLORS,
                extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
            )
            cf.cmap.set_over("#ff1900")
            cf.cmap.set_under("#ffffff")
        else:
            cf = ax.contourf(
                LON, LAT, speed_ms, levels=levels, cmap="YlGnBu",
                extend="max", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
            )
        ax.barbs(
            LON[barb_slice], LAT[barb_slice], u[barb_slice], v[barb_slice],
            transform=ccrs.PlateCarree(), color="#08306b",
            length=5.8, linewidth=0.38,
            barb_increments={"half": 5, "full": 10, "flag": 50}, zorder=7,
        )
        finish_figure(
            fig, ax, cf, levels, labels, unit,
            spec["title"], valid, run_dt, lead, out_path,
        )

    elif kind == "vort500":
        u, v = wind_uv(points, "wind_speed_500hPa", "wind_direction_500hPa", idx)
        u = grid["interpolate"](u) * 0.514444
        v = grid["interpolate"](v) * 0.514444
        # 相对涡度 ζ = ∂v/∂x − ∂u/∂y，换算成 1e-5 s⁻¹
        dx = 6371000.0 * np.cos(np.deg2rad(LAT)) * np.deg2rad(DOMAINS[domain]["fine_step"])
        dy = 6371000.0 * np.deg2rad(DOMAINS[domain]["fine_step"])
        with np.errstate(invalid="ignore"):
            vort = (np.gradient(v, axis=1) / dx - np.gradient(u, axis=0) / dy) * 1e5
        cf = ax.contourf(
            LON, LAT, vort, levels=VORT_LEVELS, cmap="RdBu_r",
            extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
        )
        cs = ax.contour(
            LON, LAT, vort, levels=[-10, -5, 5, 10],
            colors="#2b2b2b", linewidths=0.6, linestyles=["--", "--", "-", "-"],
            transform=ccrs.PlateCarree(), zorder=6,
        )
        ax.clabel(cs, inline=True, fontsize=6, fmt="%d", colors="#1f1f1f")
        finish_figure(
            fig, ax, cf, VORT_LEVELS, [str(x) for x in VORT_LEVELS],
            "500hPa relative vorticity (1e-5 s⁻¹)", spec["title"], valid, run_dt, lead, out_path,
        )

    elif kind == "t850":
        temp = grid["interpolate"]([safe_values(p["hourly"]["temperature_850hPa"], idx) for p in points])
        cf = ax.contourf(
            LON, LAT, temp, levels=T850_LEVELS, cmap="RdYlBu_r",
            extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
        )
        cs = ax.contour(
            LON, LAT, temp, levels=np.arange(-30, 31, 5), colors="#3a3a3a",
            linewidths=0.55, transform=ccrs.PlateCarree(), zorder=6,
        )
        ax.clabel(cs, inline=True, fontsize=6, fmt="%d", colors="#2a2a2a")
        finish_figure(
            fig, ax, cf, T850_LEVELS, [str(x) for x in T850_LEVELS],
            "850hPa temperature (°C)", spec["title"], valid, run_dt, lead, out_path,
        )

    elif kind == "gust24":
        gust_kn = np.asarray(maximum_over(points, "wind_gusts_10m", idx, 24))
        gust = grid["interpolate"](gust_kn * 0.514444)
        cf = ax.contourf(
            LON, LAT, gust, levels=WIND_SPEED_LEVELS, colors=WIND_SPEED_COLORS,
            extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
        )
        cf.cmap.set_over("#ff1900")
        cf.cmap.set_under("#ffffff")
        finish_figure(
            fig, ax, cf, WIND_SPEED_LEVELS, WIND_SPEED_LABELS,
            "24h gust (m/s)", spec["title"], valid, run_dt, lead, out_path,
        )

    elif kind == "cape":
        cape = grid["interpolate"]([safe_values(p["hourly"]["cape"], idx) for p in points])
        cf = ax.contourf(
            LON, LAT, cape, levels=CAPE_LEVELS, cmap="YlOrRd",
            extend="max", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
        )
        cs = ax.contour(
            LON, LAT, cape, levels=[250, 500, 1000, 2000, 3000, 4000],
            colors="#5a2b16", linewidths=0.45,
            transform=ccrs.PlateCarree(), zorder=6,
        )
        ax.clabel(cs, inline=True, fontsize=5.5, fmt="%d", colors="#4a220f")
        finish_figure(
            fig, ax, cf, CAPE_LEVELS, [str(x) for x in CAPE_LEVELS],
            "CAPE (J/kg)", spec["title"], valid, run_dt, lead, out_path,
        )

    elif kind == "pwat":
        pwat = grid["interpolate"](
            [safe_values(p["hourly"]["total_column_integrated_water_vapour"], idx) for p in points]
        )
        cf = ax.contourf(
            LON, LAT, pwat, levels=PWAT_LEVELS, cmap="YlGnBu",
            extend="max", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
        )
        cs = ax.contour(
            LON, LAT, pwat, levels=[20, 30, 40, 50, 60],
            colors="#174f5c", linewidths=0.5,
            transform=ccrs.PlateCarree(), zorder=6,
        )
        ax.clabel(cs, inline=True, fontsize=6, fmt="%d", colors="#123f49")
        finish_figure(
            fig, ax, cf, PWAT_LEVELS, [str(x) for x in PWAT_LEVELS],
            "PWAT (mm)", spec["title"], valid, run_dt, lead, out_path,
        )

    elif kind == "kindex":
        t850 = np.asarray([safe_values(p["hourly"]["temperature_850hPa"], idx) for p in points])
        td850 = np.asarray([safe_values(p["hourly"]["dew_point_850hPa"], idx) for p in points])
        t700 = np.asarray([safe_values(p["hourly"]["temperature_700hPa"], idx) for p in points])
        td700 = np.asarray([safe_values(p["hourly"]["dew_point_700hPa"], idx) for p in points])
        t500 = np.asarray([safe_values(p["hourly"]["temperature_500hPa"], idx) for p in points])
        k_index = (t850 - t500) + td850 - (t700 - td700)
        k_index = grid["interpolate"](k_index)
        cf = ax.contourf(
            LON, LAT, k_index, levels=K_LEVELS, cmap="Spectral_r",
            extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
        )
        cs = ax.contour(
            LON, LAT, k_index, levels=[20, 25, 30, 35, 40],
            colors="#3b2a26", linewidths=0.5,
            transform=ccrs.PlateCarree(), zorder=6,
        )
        ax.clabel(cs, inline=True, fontsize=6, fmt="%d", colors="#2c1f1c")
        finish_figure(
            fig, ax, cf, K_LEVELS, [str(x) for x in K_LEVELS],
            "K index (°C)", spec["title"], valid, run_dt, lead, out_path,
        )

    elif kind in ("rh2m", "rh500", "rh850"):
        key = {"rh2m": "relative_humidity_2m",
               "rh850": "relative_humidity_850hPa",
               "rh500": "relative_humidity_500hPa"}[kind]
        rh = grid["interpolate"]([safe_values(p["hourly"][key], idx) for p in points])
        cf = ax.contourf(
            LON, LAT, rh, levels=RH_LEVELS, cmap="YlGnBu",
            extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
        )
        cs = ax.contour(
            LON, LAT, rh, levels=[50, 70, 80, 90, 95],
            colors="#234b55", linewidths=0.45,
            transform=ccrs.PlateCarree(), zorder=6,
        )
        ax.clabel(cs, inline=True, fontsize=6, fmt="%d", colors="#173942")
        finish_figure(
            fig, ax, cf, RH_LEVELS, [str(x) for x in RH_LEVELS],
            "relative humidity (%)", spec["title"], valid, run_dt, lead, out_path,
        )

    elif kind == "cloud":
        cloud = grid["interpolate"](
            [safe_values(p["hourly"]["cloud_cover"], idx) for p in points]
        )
        cf = ax.contourf(
            LON, LAT, cloud, levels=CLOUD_LEVELS, colors=CLOUD_COLORS,
            extend="both", transform=ccrs.PlateCarree(), zorder=2, antialiased=True,
        )
        cf.cmap.set_under("#ffffff")
        cf.cmap.set_over(CLOUD_OVER)
        cs = ax.contour(
            LON, LAT, cloud, levels=[40, 50, 60, 70, 80, 90],
            colors="#2f5bd0", linewidths=0.55,
            transform=ccrs.PlateCarree(), zorder=6,
        )
        ax.clabel(cs, inline=True, fontsize=6, fmt=lambda x: f"{x / 10:.0f}", colors="#1b3fa8")
        finish_figure(
            fig, ax, cf, CLOUD_LEVELS, CLOUD_LABELS,
            "total cloud cover (tenths)", spec["title"], valid, run_dt, lead, out_path,
        )


_RENDER_STATE: dict = {"grid_path": "", "grids": {}}
_RENDER_LOCK = threading.Lock()


def _render_worker_init(grid_path: str) -> None:
    _RENDER_STATE["grid_path"] = grid_path
    _RENDER_STATE["grids"] = {}


def _render_domain(domain: str) -> dict:
    grids = _RENDER_STATE["grids"]
    if domain not in grids:
        with _RENDER_LOCK:
            if domain not in grids:
                loaded = json.loads(Path(_RENDER_STATE["grid_path"]).read_text(encoding="utf-8"))
                grids[domain] = loaded[domain]
    return grids[domain]


def _render_product_job(product_id: str, run_iso: str, out_dir_str: str) -> str:
    """子进程任务：渲染一个产品的全部时次。"""
    spec = PRODUCTS[product_id]
    domain = spec.get("domain", DEFAULT_DOMAIN)
    data = _render_domain(domain)
    run_dt = datetime.fromisoformat(run_iso)
    out_dir = Path(out_dir_str)
    subdir = out_dir / spec["subdir"]
    for lead in LEADS:
        valid = run_dt + timedelta(hours=lead)
        name = spec["prefix"] + valid.strftime("%Y%m%d%H") + ".png"
        render_product(data, run_dt, lead, spec, subdir / name)
    return product_id



# ---------------------------------------------------------------- overview

def _legend(product_id: str):
    """返回该产品色标的 (cmap, levels, ticks, label, extend)。"""
    import matplotlib
    from matplotlib.colors import ListedColormap

    spec = {
        "combined": (RAIN_6H_LEVELS, RAIN_6H_COLORS, "max", "6h rainfall (mm)"),
        "t2m": (TEMP_LEVELS, TEMP_COLORS, "both", "2m temperature (°C)"),
        "h500": (HEIGHT_FILL_LEVELS, HEIGHT_FILL_COLORS, "both", "500hPa height (dam)"),
        "rain24": (RAIN_24H_LEVELS, RAIN_24H_COLORS, "max", "24h rainfall (mm)"),
        "mslp": (MSLP_LEVELS, "RdYlBu", "both", "MSLP (hPa)"),
        "wind10": (WIND_SPEED_LEVELS, WIND_SPEED_COLORS, "both", "10m wind (m/s)"),
        "wind925": (WIND925_LEVELS, "YlGnBu", "max", "925hPa wind (m/s)"),
        "gust24": (WIND_SPEED_LEVELS, WIND_SPEED_COLORS, "both", "24h gust (m/s)"),
        "cape": (CAPE_LEVELS, "YlOrRd", "max", "CAPE (J/kg)"),
        "kindex": (K_LEVELS, "Spectral_r", "both", "K index (°C)"),
        "pwat": (PWAT_LEVELS, "YlGnBu", "max", "PWAT (mm)"),
        "rh2m": (RH_LEVELS, "YlGnBu", "both", "relative humidity (%)"),
        "rh500": (RH_LEVELS, "YlGnBu", "both", "relative humidity (%)"),
        "cloud": (CLOUD_LEVELS, CLOUD_COLORS, "both", "total cloud cover (tenths)"),
    }
    key = product_id[:-3] if product_id.endswith("_ea") else product_id
    levels, colors, extend, label = spec[key]
    cmap = ListedColormap(colors) if isinstance(colors, list) else matplotlib.colormaps[colors]
    return cmap, levels, [str(x) for x in levels], label, extend


def make_overview(product_id: str, run_dt: datetime, out_dir: Path) -> Path | None:
    """把某个产品 +24…+240h 每 12h 的 19 张单帧图拼成 5×4 总览。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.image as mpimg
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.colors import BoundaryNorm
    from matplotlib.cm import ScalarMappable
    from matplotlib.figure import Figure

    spec = PRODUCTS[product_id]
    panel_dir = out_dir / spec["subdir"]
    panels = []
    for step in OVERVIEW_STEPS:
        valid = run_dt + timedelta(hours=step)
        path = panel_dir / (spec["prefix"] + valid.strftime("%Y%m%d%H") + ".png")
        if path.exists():
            panels.append((step, path))
    if not panels:
        log("总览：%s 没有可用的单帧图" % product_id)
        return None
    if len(panels) < len(OVERVIEW_STEPS):
        log("总览：%s 缺 %d 个时次，按已有 %d 个拼图"
            % (product_id, len(OVERVIEW_STEPS) - len(panels), len(panels)))

    # 单帧图 1400×1008，只裁地图轴那一块（不要标题、坐标标签和右侧色标）。
    first = mpimg.imread(panels[0][1])
    h, w = first.shape[:2]
    x0, x1 = int(0.065 * w) + 3, int(0.865 * w) - 1
    y0, y1 = int((1 - 0.875) * h), int((1 - 0.075) * h)

    fig = Figure(figsize=(13.2, 10.2), dpi=150, facecolor="white")
    FigureCanvasAgg(fig)
    fig.text(0.035, 0.985, spec["title"], fontsize=15, fontweight="bold",
             ha="left", va="top")
    fig.text(0.975, 0.985,
             "%s UTC  ·  +%dh → +%dh  ·  %d frames"
             % (run_dt.strftime("%Y-%m-%d %H"), OVERVIEW_STEPS[0],
                OVERVIEW_STEPS[-1], len(panels)),
             fontsize=10.5, ha="right", va="top")
    fig.text(0.035, 0.955, "NOAA GFS 0.25°",
             fontsize=9, color="#555", ha="left", va="top")

    cols, rows = 5, 4
    left, right = 0.035, 0.975
    gap, row_gap = 0.012, 0.022
    top_y = 0.91
    aspect = (x1 - x0) / (y1 - y0)
    slot = (right - left - gap * (cols - 1)) / cols
    max_h = (top_y - 0.16 - row_gap * (rows - 1)) / rows
    height = min(slot * (13.2 / 10.2) / aspect, max_h)
    width = height * (10.2 / 13.2) * aspect
    grid_w = cols * width + (cols - 1) * gap
    x_start = left + (right - left - grid_w) / 2
    for index, (step, path) in enumerate(panels[: cols * rows]):
        row, col = divmod(index, cols)
        y = top_y - (row + 1) * height - row * row_gap
        ax = fig.add_axes([x_start + col * (width + gap), y, width, height])
        ax.imshow(mpimg.imread(path)[y0:y1, x0:x1])
        ax.set_axis_off()
        ax.set_title("+%dh" % step, fontsize=9.5, loc="left", pad=2, color="#111111")

    cmap, levels, ticks, label, extend = _legend(product_id)
    norm = BoundaryNorm(levels, cmap.N)
    sm = ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    cax = fig.add_axes([0.28, 0.075, 0.44, 0.012])
    cb = fig.colorbar(sm, cax=cax, orientation="horizontal", ticks=levels, extend=extend)
    cb.ax.tick_params(labelsize=8, length=2)
    cb.set_ticklabels(ticks)
    cb.set_label(label, fontsize=8, labelpad=3)

    fig.text(0.975, 0.015, "www.bamc.top", fontsize=10, ha="right", va="bottom")
    fig.text(0.035, 0.015, "Init: %s UTC" % run_dt.strftime("%Y-%m-%d %H"),
             fontsize=7.5, color="#666", ha="left", va="bottom")

    out_path = out_dir / (spec["subdir"] + "-overview") / "overview.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, facecolor="white")
    fig.clf()
    log("总览图 → %s" % out_path.name)
    return out_path


def write_overview_manifest(product_id: str, run_dt: datetime, out_dir: Path) -> None:
    spec = PRODUCTS[product_id]
    subdir = out_dir / (spec["subdir"] + "-overview")
    stamp = run_dt.strftime("%Y%m%d%H")
    payload = {
        "product": product_id,
        "run": run_dt.strftime("%Y-%m-%d %H:%M UTC"),
        "run_iso": run_dt.isoformat(),
        "generated": datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M HKT"),
        "source": "NOAA GFS via NOMADS",
        "region": {
            "west": DOMAINS[spec.get("domain", DEFAULT_DOMAIN)]["west"],
            "south": DOMAINS[spec.get("domain", DEFAULT_DOMAIN)]["south"],
            "east": DOMAINS[spec.get("domain", DEFAULT_DOMAIN)]["east"],
            "north": DOMAINS[spec.get("domain", DEFAULT_DOMAIN)]["north"],
        },
        "frames": [{
            "s": stamp,
            "valid_utc": run_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "lead": 0,
            "file": "overview.png",
        }],
    }
    (subdir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log("已更新 %s" % (subdir / "manifest.json"))


def main() -> None:
    ap = argparse.ArgumentParser(description="生成华南 NOAA GFS 预报图")
    ap.add_argument("--run", default="", help="起报场，例如 2026-11-08T00:00")
    ap.add_argument("--input", default="", help="已有网格 JSON（调试/避免重复下载）")
    ap.add_argument("--out", default=str(OUT_DIR), help="输出目录")
    ap.add_argument(
        "--products",
        default="combined,combined_ea,t2m,h500,h500_ea,rain24,mslp,wind10,wind925,gust24,cape,kindex,pwat,rh2m,rh500,cloud",
        help="要生成的产品，逗号分隔",
    )
    ap.add_argument(
        "--jobs", type=int, default=0,
        help="并行渲染进程数；0=自动（min(4, CPU-1)），1=单进程",
    )
    args = ap.parse_args()

    run = args.run or latest_run()
    run_dt = datetime.strptime(run, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)
    out_dir = Path(args.out)
    selected = [x.strip() for x in args.products.split(",") if x.strip()]
    unknown = [x for x in selected if x not in PRODUCTS]
    if unknown:
        raise SystemExit("未知产品：%s" % ", ".join(unknown))

    required_domains = {PRODUCTS[p].get("domain", DEFAULT_DOMAIN) for p in selected}
    data_by_domain: dict[str, dict] = {}
    grid_path: Path | None = None

    if args.input:
        loaded = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and "sc" in loaded and "ea" in loaded:
            data_by_domain = loaded
            grid_path = Path(args.input)
            log("使用已有双区域网格：%s" % args.input)
        else:
            if isinstance(loaded, list):
                loaded = {"run": run, "points": loaded}
            if required_domains != {"sc"}:
                raise SystemExit("该输入只包含单区域数据，请提供包含 sc/ea 的 JSON")
            data_by_domain["sc"] = loaded
            log("使用已有网格：%s（%d 点）" % (args.input, len(loaded["points"])))
    else:
        for domain_name in sorted(required_domains):
            data_by_domain[domain_name] = fetch_grid(run, domain_name)

    cpu = os.cpu_count() or 4
    jobs = args.jobs if args.jobs > 0 else max(1, min(4, cpu - 1))
    jobs = max(1, min(jobs, len(selected)))
    mode = os.environ.get("GFS_RENDER_MODE", "auto").strip().lower()  # auto|process|thread|serial
    if jobs <= 1:
        mode = "serial"

    generated = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M HKT")

    tmp_grid: Path | None = None

    def render_serial() -> None:
        for product_id in selected:
            spec = PRODUCTS[product_id]
            domain_name = spec.get("domain", DEFAULT_DOMAIN)
            data = data_by_domain[domain_name]
            subdir = out_dir / spec["subdir"]
            for lead in LEADS:
                valid = run_dt + timedelta(hours=lead)
                name = spec["prefix"] + valid.strftime("%Y%m%d%H") + ".png"
                render_product(data, run_dt, lead, spec, subdir / name)

    def prepare_grid_file() -> Path:
        nonlocal grid_path, tmp_grid
        if grid_path is None:
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp_grid = out_dir / ".grids.json"
            log("写入临时网格供并行渲染：%s" % tmp_grid)
            tmp_grid.write_text(json.dumps(data_by_domain), encoding="utf-8")
            grid_path = tmp_grid
        return Path(grid_path)

    def run_tasks(pool) -> None:
        list(pool.map(
            _render_product_job,
            selected,
            [run_dt.isoformat()] * len(selected),
            [str(out_dir)] * len(selected),
        ))

    rendered = False
    try:
        if mode in ("auto", "process"):
            path = prepare_grid_file()
            log("并行渲染（进程）：%d 个进程，%d 个产品" % (jobs, len(selected)))
            try:
                with ProcessPoolExecutor(
                    max_workers=jobs,
                    initializer=_render_worker_init,
                    initargs=(str(path),),
                ) as pool:
                    run_tasks(pool)
                rendered = True
            except (PermissionError, OSError) as exc:
                log("多进程不可用（%s），改用线程" % exc)
                mode = "thread"

        if not rendered and mode in ("auto", "thread"):
            path = prepare_grid_file()
            log("并行渲染（线程）：%d 个线程，%d 个产品" % (jobs, len(selected)))
            _render_worker_init(str(path))
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                run_tasks(pool)
            rendered = True
    finally:
        if tmp_grid is not None and tmp_grid.exists():
            try:
                tmp_grid.unlink()
            except OSError:
                pass

    if not rendered:
        log("单进程渲染：%d 个产品" % len(selected))
        render_serial()

    for product_id in selected:
        spec = PRODUCTS[product_id]
        domain_name = spec.get("domain", DEFAULT_DOMAIN)
        subdir = out_dir / spec["subdir"]
        frames = []
        for lead in LEADS:
            valid = run_dt + timedelta(hours=lead)
            stamp = valid.strftime("%Y%m%d%H")
            frames.append(
                {
                    "s": stamp,
                    "valid_utc": valid.strftime("%Y-%m-%d %H:%M UTC"),
                    "lead": lead,
                    "file": spec["prefix"] + stamp + ".png",
                }
            )
        manifest = {
            "product": product_id,
            "run": run_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "run_iso": run_dt.isoformat(),
            "generated": generated,
            "source": "NOAA GFS via NOMADS",
            "region": {
                "west": DOMAINS[domain_name]["west"],
                "south": DOMAINS[domain_name]["south"],
                "east": DOMAINS[domain_name]["east"],
                "north": DOMAINS[domain_name]["north"],
            },
            "frames": frames,
        }
        (subdir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log("已更新 %s" % (subdir / "manifest.json"))

    for product_id in selected:
        if make_overview(product_id, run_dt, out_dir) is not None:
            write_overview_manifest(product_id, run_dt, out_dir)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
