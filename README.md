# NOAA GFS 华南预报

以 **NOAA GFS** 为主的轻量预报图网站：16 类产品，每类另带一张 **19 帧总览**；每天 **00Z / 12Z 两个起报场**，从 +24h 到 +240h 每 6 小时一帧，共 **37 帧**。

- **数据源：NOAA NOMADS**（`nomads.ncep.noaa.gov` 的 filter CGI）；按区域、变量、层次做 GRIB2 子集下载，`eccodes` 解码
- 华南区域：**16–28°N、105–121°E**，用 GFS **0.25°** 场，0.5° 取点、0.25° 绘图
- 综合图与 500hPa 风场另有东亚版：**0–50°N、80–140°E**，用 GFS **1.0°** 场，2° 取点、0.5° 绘图
- 纯 Python 标准库后端，无数据库；图像为预生成 PNG，更新后热切换
- 内置反爬、内置自动更新器（只使用已出到 +240h 的完整场次）
- **不需要 API key，也没有调用额度**；NOMADS 只有“每分钟约 120 次请求”的软限制，一轮 40 次请求

## 快速开始

```bash
python server.py
```

浏览器打开 `http://127.0.0.1:8787`。Windows 也可双击 `start.bat`。

站点运行只需要 Python 标准库；`gfs/` 里是预生成的预报图。生成/更新图片需要：

```bash
python -m pip install numpy matplotlib cartopy eccodes
```

> 第一次出图时 cartopy 会自动下载 Natural Earth 的海岸线、省界等数据（约 40 MB），之后走 `.cartopy/` 本地缓存，单帧渲染不到 1 秒。

## 产品

| ID | 名称 |
| --- | --- |
| `gfs-southchina` | 华南 · 降雨 + 500hPa 高度 + 850hPa 风 |
| `gfs-southchina-ea` | 东亚 · 降雨 + 500hPa 高度 + 850hPa 风 |
| `gfs-t2m` | 2m 气温 |
| `gfs-h500` / `gfs-h500-ea` | 500hPa 风场（华南 / 东亚） |
| `gfs-rain24` | 24h 降水 |
| `gfs-mslp` | 海平面气压 |
| `gfs-wind10` | 地面风场 |
| `gfs-wind925` | 925hPa 风场 |
| `gfs-gust24` | 24h 阵风 |
| `gfs-cape` | CAPE |
| `gfs-kindex` | K 指数 |
| `gfs-pwat` | PWAT |
| `gfs-rh2m` | 2m 相对湿度 |
| `gfs-rh500` | 500hPa 相对湿度 |
| `gfs-cloud` | 总云量（成） |

### 19 帧总览

每个产品额外生成一张 5×4 的总览图（`gfs/<产品>-overview/overview.png`）：取 +24h…+240h 每 12 小时一帧，共 19 个小图拼在一起（最后留一个空格），带产品色标。

页面侧栏「显示模式（O）」按钮在 **单帧时间轴** 和 **总览** 之间切换，快捷键 `O`；总览模式下时间轴不可用，提示栏显示「19 帧总览 · 点“单帧”回到时间轴」。

### 图像样式约定

- 时次：+24h 至 +240h，每 6 小时一帧，共 37 帧
- 综合图：588 dagpm 线红色粗描；850hPa 风速 ≥ 24.5 m/s 的风矢标红
- 地面风场 / 24h 阵风：15 级风速色阶（0.3–56.1 m/s）
- 总云量：4–9 成蓝色渐变
- 广东省界黄色描边

## 更新图片

```bash
python auto_update.py            # 常驻，每 30 分钟检查
python auto_update.py --once     # 只检查并更新一次
python auto_update.py --dry-run  # 只看有没有新场次
```

更新器只使用已经出到 +240h 的完整场次；图片先写到 `gfs_build/`，校验通过后整体替换，本地只保留最新一组 16×37 张。

手动生成：

```bash
python gfs_chart.py                                  # 自动选最新完整场次
python gfs_chart.py --run 2026-11-17T06:00           # 指定场次
python gfs_chart.py --products cloud --jobs 1        # 只出云量
```

## 数据源：NOAA NOMADS

每一帧对应一个 `filter_gfs_0p25.pl` 请求：

```text
GET https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl
    ?dir=/gfs.20261117/06/atmos
    &file=gfs.t06z.pgrb2.0p25.f024
    &var_TMP=on&var_RH=on&var_UGRD=on&var_VGRD=on&var_GUST=on
    &var_PRMSL=on&var_APCP=on&var_CAPE=on&var_PWAT=on&var_TCDC=on&var_HGT=on
    &lev_2_m_above_ground=on&lev_10_m_above_ground=on&lev_surface=on
    &lev_mean_sea_level=on&lev_entire_atmosphere=on
    &lev_entire_atmosphere_(considered_as_a_single_layer)=on
    &lev_500_mb=on&lev_700_mb=on&lev_850_mb=on&lev_925_mb=on
    &subregion=&leftlon=105&rightlon=121&toplat=28&bottomlat=16
```

要点：

- **层次单位是 `mb` 不是 `hPa`**，写成 `lev_500_hPa` 会直接 500 错误
- **PWAT 的层次名是 `entire_atmosphere_(considered_as_a_single_layer)`**，写成 `entire_atmosphere` 返回空文件
- `APCP` 同时有 6 小时和 24 小时两条累计，代码按 `stepRange` 长度取最短的那条（即 6h）
- `TCDC` 同时有瞬时和 6 小时平均两条，同样取瞬时
- 解码用 `eccodes`，**它不是线程安全的**，代码里用全局锁串行化解码（下载仍然并发）
- 一轮 40 个时次、约 7 MB GRIB，华南 0.25° 子集单帧约 180 KB

### 历史数据

NOMADS 只保留最近几天；更早的场次可从 AWS 的 `noaa-gfs-bdp-pds` 公开桶按字节范围下载（需自己解析 `.idx`）。

## 反爬说明

`anti_bot.py` 已接入 `server.py`：会话 Cookie、每 IP 限速、UA 过滤、扫描器路径拦截、图片防盗链。配置项见 `server.py` 与 `anti_bot.py` 顶部。

## 目录结构

```text
GFS-nwp/
├─ server.py             # 网站后端
├─ anti_bot.py           # 反爬模块
├─ auto_update.py        # 自动更新器
├─ gfs_chart.py          # 数据下载 + 解码 + 出图
├─ start.bat / start_auto_update.bat
├─ install_auto_update_task.bat / uninstall_auto_update_task.bat
├─ public/               # 前端
└─ gfs/                  # 16 类产品，每类 37 张 PNG + manifest.json
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | 8787 | 监听端口 |
| `TRUST_PROXY` | `0` | 反向代理后设为 `1` |
| `ANTIBOT_SECRET` | 随机 | Cookie 签名密钥 |
| `GFS_FETCH_WORKERS` | `4` | GRIB 下载并发数 |
| `GFS_BUILD_RETRIES` | `3` | 自动更新失败重试次数 |

## 数据与声明

- 预报数据：NOAA/NCEP GFS，来自 NOMADS（美国联邦政府公开数据，公有领域）
- 图像：本地 Python 脚本生成，仅作预报参考
