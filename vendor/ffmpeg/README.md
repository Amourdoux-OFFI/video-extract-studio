# FFmpeg 二进制目录

本目录用于放置 FFmpeg 与 FFprobe 的可执行文件。

## 为什么这两个 exe 不在仓库里

它们各约 79 MB，两个加起来 158 MB。把它们提交进 Git 会让克隆变得极其痛苦，
GitHub 也明确不建议仓库中出现这种体积的二进制。因此**已加入 `.gitignore`**。

本目录中的另外两个文件是**必须入库**的：

- `LICENSE-GPLv3.txt` —— FFmpeg 的许可证全文（法律义务，不可省略）
- `README.md` —— 本文件

## 怎么获取

在项目根目录执行：

```bash
python tools/fetch_ffmpeg.py
```

脚本会从 npmmirror 下载官方静态构建并解压到本目录（国内直连速度可达 10 MB/s 以上）。

## 手动获取

若脚本不可用，自行下载后按下表命名放入本目录即可：

| 下载地址 | 解压后重命名为 |
|---|---|
| `https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.1.1/ffmpeg-win32-x64.gz` | `ffmpeg.exe` |
| `https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.1.1/ffprobe-win32-x64.gz` | `ffprobe.exe` |

这是 `ffmpeg-static` 在 npmmirror 上的官方镜像，对应 gyan.dev 的 Windows 静态构建。

## 许可证：GPLv3，不是 LGPL

该构建启用了 `--enable-gpl --enable-version3 --enable-libx264 --enable-libx265`，
因此是 **GPLv3**。

本项目把它当作**独立程序**通过子进程调用（聚合作品 / mere aggregation），
所以本项目的 MIT 代码不会被要求 GPL 化。但分发时你仍须：

1. 附上 `LICENSE-GPLv3.txt`
2. 提供源码获取方式：<https://ffmpeg.org/download.html>
3. 不声称 FFmpeg 是自有闭源组件

细节见项目根目录的 `THIRD-PARTY-NOTICES.md`。

## 程序如何找到它

`app/media/ffmpeg_locate.py` 按以下顺序查找，全部失败才会报错：

1. `config.json` 中的 `ffmpeg_path`（可指向目录或 exe）
2. `<项目根>/vendor/ffmpeg/`
3. PyInstaller 解包目录下的 `vendor/ffmpeg/`
4. 系统 `PATH`

因此你完全可以**不放在这里**，改为装一个系统级 FFmpeg，程序一样能找到。
