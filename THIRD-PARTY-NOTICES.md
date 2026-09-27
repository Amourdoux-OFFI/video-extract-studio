# 第三方组件与许可

本项目的**自有代码**以 MIT 许可发布（见 `LICENSE`）。但它依赖的第三方组件各有自己的许可，
分发时**必须一并遵守**。下面逐项说明。

---

## 一、FFmpeg —— GPLv3（最重要，容易被忽略）

### 事实

本项目使用、并在打包成品中**内置**了 FFmpeg 与 FFprobe 的可执行文件。
经实际检查构建参数确认：

```
--enable-gpl          ← 启用 GPL 代码
--enable-version3     ← 采用 GPLv3
--enable-libx264      ← x264 是 GPL，链入后整个构建即为 GPL
--enable-libx265      ← x265 同样是 GPL
```

因此该构建是 **GPLv3**，不是 LGPL。

### 这意味着什么

FFmpeg 是**独立可执行程序**，本项目通过 `subprocess` 以子进程方式调用它，
属于 GPL 所称的「聚合作品（mere aggregation）」，**不会**导致本项目的 MIT 代码被迫 GPL 化。

但分发二进制时你仍然必须履行 GPLv3 的义务：

1. **随附许可证全文** —— 见 `vendor/ffmpeg/LICENSE-GPLv3.txt`
2. **提供对应源码的获取方式** —— FFmpeg 源码：https://ffmpeg.org/download.html
   本项目所用构建的来源：https://www.gyan.dev/ffmpeg/builds/
3. **不得声称内置的 FFmpeg 是你独占版权的闭源组件**

### 如果你要闭源分发

那就必须换掉这个构建。可选方案：

- 改用 **LGPL** 构建（不含 x264/x265），代价是失去 `libx264` 软件编码，
  只能依赖 `mpeg4` 或硬件编码器（NVENC / AMF / QSV）
- 或改为**要求用户自行安装 FFmpeg**，本程序仅在运行时调用，不随包分发

---

## 二、Python 依赖

下表由 `importlib.metadata` 实际读取各包元数据生成，非人工整理：

| 包 | 版本 | 许可 |
|---|---|---|
| fastapi | 0.141.1 | MIT |
| starlette | 1.7.0 | BSD-3-Clause |
| uvicorn | 0.54.0 | BSD-3-Clause |
| pydantic | 2.9.2 | MIT |
| httpx / httpcore | 0.27.2 / 1.0.9 | BSD-3-Clause |
| anyio | 4.15.1 | MIT |
| python-multipart | 0.0.32 | Apache-2.0 |
| aiofiles | 24.1.0 | Apache-2.0 |
| websockets | 12.0 | BSD-3-Clause |
| **f2** | 0.0.1.7 | **Apache-2.0** |
| yt-dlp | 2026.8.19 | Unlicense（公有领域） |
| **browser-cookie3** | 0.20.1 | **LGPL** |
| pywebview | 6.2.1 | BSD-3-Clause |
| pythonnet | 3.1.0 | MIT |
| bottle | 0.13.4 | MIT |
| protobuf | 5.28.3 | BSD-3-Clause |
| gmssl | 3.2.2 | BSD |
| PyExecJS | 1.5.1 | MIT |
| jsonpath-ng | 1.6.1 | Apache-2.0 |
| m3u8 | 3.6.0 | MIT |
| aiosqlite | 0.20.0 | MIT |
| rich | 13.9.3 | MIT |
| click | 8.1.7 | BSD-3-Clause |
| qrcode | 8.0 | BSD |
| cryptography | 44.0.0 | Apache-2.0 OR BSD-3-Clause |
| pycryptodomex | 3.23.0 | BSD / Public Domain |
| certifi | 2026.7.22 | MPL-2.0 |
| lz4 | 4.4.5 | BSD-3-Clause |
| watchfiles | 1.3.0 | MIT |
| PyYAML | 6.0.2 | MIT |
| h11 / idna / sniffio / six | — | MIT / BSD |
| colorama | 0.4.6 | BSD-3-Clause |

全部为宽松许可（MIT / BSD / Apache-2.0 / MPL-2.0 / Unlicense），
唯一的例外：

- **browser-cookie3 是 LGPL**。本项目以**库导入**方式使用它（非静态链接、非修改分发），
  LGPL 允许这样做，无需开放本项目源码。但如果你修改了 browser-cookie3 本身并分发，
  那部分修改必须按 LGPL 开放。

---

## 三、需要特别说明的两项

### f2（Apache-2.0）

抖音的 `a_bogus` 签名算法与 Token 生成由 f2 提供。本项目**未修改、未内联**其代码，
仅作为普通依赖调用。Apache-2.0 要求保留其版权声明 —— 已在 `README.md` 的致谢部分注明。

### yt-dlp（Unlicense）

公有领域，无任何限制。

---

## 四、发布前建议

```bash
# 复核依赖许可（比人工整理可靠）
pip install pip-licenses
pip-licenses --format=markdown --with-urls
```

如果后续新增依赖，**务必重跑一次**再发版。
