#!/usr/bin/env python3
"""把 assets 中的视频批量压缩成 360P、单个最长 5 分钟的轻量版，输出到 assets_light。

需要系统已安装 ffmpeg（不在 PATH 中时用 --ffmpeg 指定可执行文件）。

用法示例：
    python compress_assets.py                      # assets -> assets_light
    python compress_assets.py --dry-run            # 只打印将要执行的命令
    python compress_assets.py --jobs 4 --crf 28    # 4 个任务并行，画质更低、体积更小
    python compress_assets.py --overwrite          # 重新压缩已存在的输出
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 与 main.py 保持一致，保证 assets_light 里的文件仍能被程序识别
VIDEO_SUFFIXES = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v"}

DEFAULT_SRC = ROOT / "assets"
DEFAULT_DST = ROOT / "assets_light"
DEFAULT_HEIGHT = 360
DEFAULT_MAX_SECONDS = 5 * 60
DEFAULT_CRF = 26
DEFAULT_PRESET = "medium"
DEFAULT_AUDIO_BITRATE = "96k"
OUTPUT_SUFFIX = ".mp4"


@dataclass
class Result:
    video: Path
    dst: Path | None
    status: str  # ok / skip / fail / dry
    seconds: float = 0.0
    detail: str = ""


def setup_console() -> None:
    """Windows 控制台默认用 GBK，遇到带 emoji 的文件名会直接抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except (AttributeError, OSError):
            pass


def default_jobs() -> int:
    """ffmpeg 自身已是多线程，并行度过高反而会互相抢 CPU。"""
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def format_seconds(seconds: int) -> str:
    return f"{seconds // 60}分{seconds % 60:02d}秒" if seconds >= 60 else f"{seconds}秒"


def build_scale_filter(height: int) -> str:
    """横屏按高度缩放，竖屏按宽度缩放；不放大原始分辨率，尺寸保证为偶数。"""
    return (
        "scale="
        f"'if(gte(iw,ih),-2,trunc(min(iw,{height})/2)*2)':"
        f"'if(gte(iw,ih),trunc(min(ih,{height})/2)*2,-2)'"
    )


def find_videos(src: Path) -> list[Path]:
    videos = [
        path
        for path in src.rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
    ]
    return sorted(videos, key=lambda p: str(p).lower())


def output_path_for(src: Path, dst_root: Path, video: Path) -> Path:
    relative = video.relative_to(src)
    return (dst_root / relative).with_suffix(OUTPUT_SUFFIX)


def build_command(ffmpeg: str, video: Path, dst: Path, args: argparse.Namespace) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-y",
        "-i", str(video),
        "-map", "0:v:0",
        "-map", "0:a:0?",  # 没有音频轨的视频也能正常处理
        "-vf", build_scale_filter(args.height),
        "-c:v", "libx264",
        "-preset", args.preset,
        "-crf", str(args.crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", args.audio_bitrate,
        "-ac", "2",
        "-t", str(args.max_seconds),
        "-movflags", "+faststart",
        str(dst),
    ]


def compress_one(ffmpeg: str, video: Path, dst: Path, args: argparse.Namespace) -> Result:
    if dst.exists() and not args.overwrite:
        return Result(video, dst, "skip", detail="输出已存在")

    if args.dry_run:
        return Result(video, dst, "dry", detail=" ".join(build_command(ffmpeg, video, dst, args)))

    dst.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    proc = subprocess.run(
        build_command(ffmpeg, video, dst, args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.monotonic() - started

    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip().splitlines()
        return Result(video, dst, "fail", elapsed, message[-1] if message else "ffmpeg 返回非零状态")

    return Result(video, dst, "ok", elapsed)


def resolve_ffmpeg(explicit: str | None) -> str:
    if explicit:
        path = Path(explicit).expanduser()
        if path.exists():
            return str(path)
        found = shutil.which(explicit)
        if found:
            return found
        raise SystemExit(f"找不到 ffmpeg：{explicit}")

    found = shutil.which("ffmpeg")
    if not found:
        raise SystemExit("找不到 ffmpeg，请先安装并加入 PATH，或用 --ffmpeg 指定其路径。")
    return found


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 assets 中的视频压缩成 360P、最长 5 分钟的轻量版，输出到 assets_light。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC, help="源视频目录")
    parser.add_argument("--dst", type=Path, default=DEFAULT_DST, help="输出目录")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help="360P 的目标短边像素")
    parser.add_argument(
        "--max-seconds", type=int, default=DEFAULT_MAX_SECONDS, help="单个视频最长保留的秒数"
    )
    parser.add_argument("--crf", type=int, default=DEFAULT_CRF, help="x264 画质，越大越小越糊")
    parser.add_argument("--preset", default=DEFAULT_PRESET, help="x264 preset")
    parser.add_argument("--audio-bitrate", default=DEFAULT_AUDIO_BITRATE, help="AAC 音频码率")
    parser.add_argument("--jobs", type=int, default=default_jobs(), help="并行压缩的任务数")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出文件")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令，不实际压缩")
    parser.add_argument("--ffmpeg", default=None, help="ffmpeg 可执行文件路径")
    args = parser.parse_args(argv)

    if args.height <= 0:
        parser.error("--height 必须大于 0")
    if args.max_seconds <= 0:
        parser.error("--max-seconds 必须大于 0")
    if args.jobs <= 0:
        parser.error("--jobs 必须大于 0")
    return args


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = parse_args(argv)
    src = args.src.resolve()
    dst_root = args.dst.resolve()

    if not src.is_dir():
        print(f"源目录不存在：{src}", file=sys.stderr)
        return 1
    if src == dst_root:
        print("输出目录不能和源目录相同。", file=sys.stderr)
        return 1

    videos = find_videos(src)
    if not videos:
        print(f"{src} 中没有找到视频文件。")
        return 0

    ffmpeg = resolve_ffmpeg(args.ffmpeg)
    total = len(videos)
    print(f"找到 {total} 个视频：{src} -> {dst_root}")
    print(
        f"目标 {args.height}P，最长 {format_seconds(args.max_seconds)}，"
        f"CRF {args.crf}，preset {args.preset}，并行 {args.jobs}"
    )

    started = time.monotonic()
    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(compress_one, ffmpeg, video, output_path_for(src, dst_root, video), args): video
            for video in videos
        }
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            prefix = f"[{index}/{total}]"
            if result.detail and result.status == "dry":
                print(f"{prefix} {result.video.name}: {result.detail}")
            elif result.status == "ok":
                print(f"{prefix} 完成 {result.video.name}（{result.seconds:.1f}s）")
            elif result.status == "skip":
                print(f"{prefix} 跳过 {result.video.name}：{result.detail}")
            else:
                print(f"{prefix} 失败 {result.video.name}：{result.detail}", file=sys.stderr)

    elapsed = time.monotonic() - started

    if args.dry_run:
        print(f"\n（dry-run）共 {total} 个视频，用时 {elapsed:.1f}s。")
        return 0

    succeeded = [r for r in results if r.status == "ok"]
    skipped = [r for r in results if r.status == "skip"]
    failed = [r for r in results if r.status == "fail"]

    def total_size(paths: list[Path]) -> int:
        return sum(p.stat().st_size for p in paths if p.exists())

    before = total_size([r.video for r in succeeded])
    after = total_size([r.dst for r in succeeded if r.dst])
    ratio = f"，体积降至 {after / before * 100:.0f}%" if before else ""

    print(
        f"\n完成：成功 {len(succeeded)}，跳过 {len(skipped)}，失败 {len(failed)}，"
        f"用时 {format_seconds(int(elapsed))}。"
    )
    if succeeded:
        print(
            f"压缩后从 {before / 1024 / 1024:.1f} MB 变为 {after / 1024 / 1024:.1f} MB{ratio}，"
            f"输出目录：{dst_root}"
        )
    if failed:
        print("以下文件压缩失败：", file=sys.stderr)
        for result in failed:
            print(f"  {result.video}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        sys.exit(130)
