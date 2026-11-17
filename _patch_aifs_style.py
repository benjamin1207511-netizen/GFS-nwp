# -*- coding: utf-8 -*-
"""把 GFS 的总览改成 AIFS 风格：19 帧（+24…+240 每 12h），不再单列产品。"""
from pathlib import Path

p = Path("gfs_chart.py")
s = p.read_text(encoding="utf-8")

# 1) 时效回到 +24 起（37 帧），总览 19 帧
old = '''LEADS = list(range(12, 241, 6))          # +12h … +240h，每 6 小时一帧，共 39 帧
OVERVIEW_STEPS = list(range(12, 241, 12))  # 总览：每 12 小时一个小图，共 20 个'''
new = '''LEADS = list(range(24, 241, 6))           # +24h … +240h，每 6 小时一帧，共 37 帧
OVERVIEW_STEPS = list(range(24, 241, 12))  # 总览：每 12 小时一个小图，共 19 个'''
assert old in s
s = s.replace(old, new, 1)

# 2) 去掉“每个产品一个总览产品”的注入
block = '''

# 每个产品配一个「20 帧总览」产品：4×5 拼图，单独一个页面
_BASE_PRODUCTS = dict(PRODUCTS)
for _pid, _spec in _BASE_PRODUCTS.items():
    PRODUCTS[_pid + "_overview"] = {
        "subdir": _spec["subdir"] + "-overview",
        "prefix": "gfs_%s_overview_" % _pid,
        "title": _spec["title"] + " · 20-frame Overview",
        "kind": "overview",
        "domain": _spec.get("domain", "sc"),
        "base": _pid,
    }
'''
assert block in s
s = s.replace(block, "", 1)

# 3) 整个总览代码块重写
start = s.index("# ---------------------------------------------------------------- overview")
end = s.index("def main() -> None:")

new_block = '''# ---------------------------------------------------------------- overview

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
    levels, colors, extend, label = spec[product_id]
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

    fig = Figure(figsize=(13.2, 10.2), dpi=150, facecolor="white")
    FigureCanvasAgg(fig)
    fig.text(0.035, 0.975,
             "NOAA GFS · " + spec["title"],
             fontsize=14, fontweight="bold", ha="left", va="top")
    fig.text(0.975, 0.975,
             "%s UTC  ·  +24–240 h" % run_dt.strftime("%Y-%m-%d %H"),
             fontsize=10.5, ha="right", va="top")

    # 单帧图 1400×1008，地图轴 rect=[0.065, 0.075, 0.80, 0.80]
    x0, x1 = int(0.065 * 1400), int(0.865 * 1400)
    y0, y1 = int((1 - 0.875) * 1008), int((1 - 0.075) * 1008)

    cols, rows = 5, 4
    left, right = 0.035, 0.975
    gap = 0.012
    width = (right - left - gap * (cols - 1)) / cols
    height = width * (13.2 / 10.2) / ((x1 - x0) / (y1 - y0))
    top_y = 0.90
    row_gap = 0.025
    for index, (step, path) in enumerate(panels[:cols * rows]):
        row, col = divmod(index, cols)
        y = top_y - (row + 1) * height - row * row_gap
        ax = fig.add_axes([left + col * (width + gap), y, width, height])
        ax.imshow(mpimg.imread(path)[y0:y1, x0:x1])
        ax.set_axis_off()
        ax.set_title("+%dh" % step, fontsize=9.5, loc="left", pad=2, color="#111111")

    cmap, levels, ticks, label, extend = _legend(product_id)
    norm = BoundaryNorm(levels, cmap.N)
    sm = ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    cax = fig.add_axes([0.28, 0.09, 0.44, 0.012])
    cb = fig.colorbar(sm, cax=cax, orientation="horizontal", ticks=levels, extend=extend)
    cb.ax.tick_params(labelsize=8, length=2)
    cb.set_ticklabels(ticks)
    cb.set_label(label, fontsize=8, labelpad=3)
    fig.text(0.975, 0.015, "NOAA GFS · NOMADS", fontsize=9, ha="right", va="bottom")
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
            "west": DOMAINS[spec["domain"]]["west"],
            "south": DOMAINS[spec["domain"]]["south"],
            "east": DOMAINS[spec["domain"]]["east"],
            "north": DOMAINS[spec["domain"]]["north"],
        },
        "frames": [{
            "s": stamp,
            "valid_utc": run_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "lead": 0,
            "file": "overview.png",
        }],
    }
    (subdir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    log("已更新 %s" % (subdir / "manifest.json"))


'''
s = s[:start] + new_block + s[end:]

# 4) main 末尾：按产品生成总览
old_tail = '''    for product_id in selected:
        ov_id = product_id + "_overview"
        if ov_id in PRODUCTS:
            if make_overview(ov_id, run_dt, out_dir) is not None:
                write_overview_manifest(ov_id, run_dt, out_dir)'''
new_tail = '''    for product_id in selected:
        if make_overview(product_id, run_dt, out_dir) is not None:
            write_overview_manifest(product_id, run_dt, out_dir)'''
assert old_tail in s
s = s.replace(old_tail, new_tail, 1)

p.write_text(s, encoding="utf-8")
print("chart rewritten")
