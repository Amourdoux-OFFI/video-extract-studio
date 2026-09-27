# 开发记录

这份文档记录本项目在设计、验证、排错过程中的关键结论。所有数据都来自本机实测，
不是推测；对应的验证脚本保留在 `tools/` 下，可以复现。

---

## 一、平台链路验证

### 抖音

| 环节 | 结论 |
|---|---|
| 短链 302 | `v.douyin.com/xxxx` → `iesdouyin.com/share/video/{aweme_id}`，游客状态可达 |
| 详情接口 | `www.douyin.com/aweme/v1/web/aweme/detail/`，需 `a_bogus` 签名 |
| 裸请求 | 返回空，或 `Blocked by ArgusSecurityPlugin Uifid Not Found` |
| 旧版 iteminfo | `iesdouyin.com/web/api/v2/aweme/iteminfo/` 已失效，返回空 |
| share 页 SSR | `_ROUTER_DATA` 只有约 1.4 KB，不含播放地址 |
| f2 + 游客 Token | **可用**，无需登录；单作品返回 **19 档**清晰度 |
| CDN 直链 | **必须带 `Referer: https://www.douyin.com/`**，否则 403 |

游客 Token 由 `TokenManager.gen_real_msToken()` / `gen_ttwid()` / `gen_webid()` /
`VerifyFpManager.gen_s_v_web_id()` 组成。生成一次约 20 秒，因此加了 10 分钟缓存。

### B站

`b23.tv` 短链 302 到 BV 号；`x/web-interface/nav` 无需登录就能拿到 WBI 密钥。

**关键发现**：`playurl` 的画质档位由 `try_look` 参数决定，与 WBI 签名无关。
10 组对照实验见 README 的技术说明一节，结论是
`免签名 + try_look=1 + fnver=0` 拿到 `[80,64,32,16]`，其余组合只有 `[32,16]`。

DASH 结构：每档含 AVC(`codecid=7`) / HEVC(`12`) / AV1(`13`) 三套编码；
音轨 3 档（`30280` 192K / `30232` 132K / `30216` 64K），另有 `dolby` 与 `flac` 节点。
下载分片必须带 `Referer`。

### 网络环境的坑

| 目标 | 结果 |
|---|---|
| GitHub | **不通** —— 无法 clone 参考项目，也无法从 release 下载 FFmpeg |
| PyPI | 通，安装依赖正常 |
| gh-proxy / ghproxy.net | 通但只有约 0.1 MB/s，186 MB 的 FFmpeg 要下半小时 |
| **npmmirror** | **13 MB/s** —— 最终选它作为 FFmpeg 来源 |
| gyan.dev | 通，但实测约 1 KB/s |

因此 `tools/fetch_ffmpeg.py` 把 npmmirror 放在首选源。

---

## 二、水印问题的完整排查过程

### 第一步：确认水印从哪来

抓取三个 B站样片不同时间点的帧，肉眼比对：

| 样片 | 位置 | 形态 |
|---|---|---|
| 驾考宝典 | 左上 | `qzc17` + `bilibili` |
| 东北泰坦尼克号 | 右上 | `神威-狗剩` + `bilibili` |
| 东北泰坦尼克号 | 画面中部，**随时间移动** | `bilibili神威-狗剩` |
| 苦练半年的绕口令（竖屏） | 右上 | `高雅的邱千` + `bilibili` |

位置随视频变化 → 说明是**上传者设置**决定的平台投稿水印。

### 第二步：穷举码流，验证能否规避

对同一稿件取全部可获取码流，各抓一帧后按角落裁切拼图 + 计算与参考帧的平均绝对差：

| 码流 | 条数 | 带水印 |
|---|---|---|
| DASH 4 档 × 3 编码 | 12 | 全部 |
| `fnval=1` durl | 1 | 是 |
| `platform=html5` | 1 | 是 |
| TV 端 `api.snm0516.aisee.tv` | 0 | 接口返回 `-400` |

数值差异只随分辨率变化，没有任何一条显著更低 —— 说明不存在干净码流。
**结论：「换接口拿无水印」不可行。**

### 第三步：改用后期处理

自动检测经历了三次算法迭代，前两次都失败，记录下来避免重走：

1. **「帧 − 全片时域中值」** ❌
   多场景视频里，中值本身就是几个镜头的混合，差异被镜头切换淹没。
   实测把字幕误判成水印，真正的右上角水印反而漏检。

2. **「边缘时域持续性」** ❌
   用 `edgedetect` 统计边缘在多少帧里出现。水印压在亮天空或近白墙上时
   对比度不足，持续性达不到阈值。

3. **「近白掩膜的跨帧持续性」** ✅
   水印是白色半透明文字：压在暗背景上时它是画面里最亮的，
   压在彩色背景上时它是画面里最不饱和的。两个条件合起来就能把它从画面内容里分出来；
   画面里的白色物体（云、衣服、灯光）会移动或变形，持续不下来的被自然排除。

   亮度/饱和度阈值走**三档自适应**（严格 200/34 → 标准 150/72 → 宽松 132/92），
   先用严格档，不成再放宽。这样「白字压近白墙」和「白字压亮彩天空」都能命中。

   实测三个样片全部检出。中间还踩了一个坑：形状过滤里的「镂空度上限」设成 0.55 时，
   合并后的连通域（镂空度 0.78~0.83）会被误杀，放宽到 0.82 才正常。

---

## 三、踩过的坑

### 1. `delogo` 的 `enable` 用的是输出时间轴 ⚠️

**这是本项目最隐蔽的一个 bug。**

ffmpeg 只要用了 `-ss`（无论放在 `-i` 前还是后），时间戳都会重置为从 0 开始，
而滤镜里的 `t` 是输出时间轴上的时间。

对照实验（源片 t=74s，水印框时间窗分别设为不同值）：

| 条件 | 与原始帧的平均绝对差 | 结论 |
|---|---|---|
| 不带 `enable` | 1.30 | 滤镜生效 |
| `enable='between(t,66,82)'`（t=74 应在窗口内） | **0.00** | **没生效** ✗ |
| `enable='between(t,0,10)'`（t=74 应在窗口外） | **1.30** | **反而生效了** ✗ |

完全符合「时间戳被重置为 0」的特征。

**影响**：只要「裁剪」和「去水印」同时启用，所有水印时间窗都会整体错位。

**修法**：`build_vf()` 增加 `time_offset` 参数，所有时间窗统一减去起始偏移；
调用方传裁剪起点（`edit.py`）或取帧时间（`/api/frame`）。

### 2. f2 会在当前工作目录建 `logs/`

`f2/log/logger.py` 里写死 `Path("./logs")`，并且在 **import 时**就 `mkdir()`，
路径相对**当前工作目录**。只要从别的目录启动程序，它就在那里凭空建目录。

**修法**：在 import f2 之前先给 `"f2"` logger 挂上指向项目内 `logs/f2/` 的 handler。
f2 的 `log_setup()` 开头有 `if logger.hasHandlers(): return`，于是它直接返回，不再建目录。

### 3. yt-dlp 的 `cookiesfrombrowser` 会硬失败

Chrome 的 Cookie 数据库被占用时（浏览器正在运行），yt-dlp 直接抛错终止解析：

```
ERROR: Could not copy Chrome cookie database.
```

**修法**：改成两次尝试 —— 先带 Cookie，失败则去掉 Cookie 重试。

### 4. 纯 CRF 会把文件压得更大

源码率本就很低时，CRF 重编码反而增大体积：

| 场景 | 修复前 | 修复后 |
|---|---|---|
| CRF21（x264 medium） | 14.6 → **17.1 MB** | 14.6 → **12.6 MB** |
| NVENC（cq23） | 14.6 → **25.6 MB** | 14.6 → **13.2 MB** |
| CRF24 + 限 480P | 14.6 → 12.0 MB | 14.6 → **10.5 MB** |

**修法**：改用**受限 CRF** —— `-crf N -maxrate {源码率} -bufsize 2×`；
NVENC 侧要让 `maxrate` 生效必须同时给 `-b:v` 目标码率。

### 5. PyInstaller 排除 `distutils` 会直接中断打包

```
ValueError: Target module "distutils" already imported as "ExcludedModule('distutils',)"
```

原因是 PyInstaller 自带的 `hook-distutils.py` 需要把 `setuptools._distutils`
别名成 `distutils`，排除后就冲突了。**不要排除 `setuptools` / `distutils` / `pip`。**

### 6. 打包入口不能放在子目录

入口若是 `app/main.py`，PyInstaller 的模块搜索根就变成 `app/`，
`from app import ...` 会解析失败。**入口必须放项目根**（本项目用 `run.py`），
并加 `--paths <项目根>`。

### 7. `pythonw` 下 `sys.stdout` 是 `None`

无控制台模式启动时，任何往 `sys.stdout` 写东西的第三方库都会静默崩溃。
需要兜底：`stdout` 指向 `os.devnull`，`stderr` 落到日志文件。

---

## 四、测试覆盖

`tools/` 下的脚本都可在本机复跑：

| 脚本 | 用途 |
|---|---|
| `test_extractors.py` | 5 条真实链接的解析链路 |
| `test_api.py` | 走真实 HTTP 接口跑「解析 → 下载 → 合流」 |
| `test_edit.py` | 剪辑 7 项：无损 / 精确 / CRF / NVENC / 目标体积 / 静音 |
| `test_watermark.py` | 水印检测与去除，生成标注图与对比图 |
| `test_moving_watermark.py` | 移动水印的逐点取样工作流 |
| `test_api_edit.py` | 剪辑链路的 HTTP 接口 + SSE 事件核对 |
| `check_contract.py` | 前后端接口契约一致性核对 |
| `probe_*.py` | 各平台接口探测（画质对照、水印取证等） |

运行前需要先 `python tools/fetch_ffmpeg.py`。
