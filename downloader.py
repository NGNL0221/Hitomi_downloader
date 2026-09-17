#!/usr/bin/env python3
"""Concurrent downloader for Hitomi galleries.

Usage:
    python hitomi_downloader.py <gallery-url>
    python hitomi_downloader.py <gallery-url> --workers 12 --output book.zip
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


DEFAULT_ASSET_HOST = "ltn.gold-usergeneratedcontent.net"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"


def fetch_bytes(url: str, referer: str | None = None) -> bytes:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def fetch_text(url: str, referer: str | None = None) -> str:
    return fetch_bytes(url, referer).decode("utf-8")


def gallery_id_from_url(url: str) -> str:
    match = re.search(r"(?:-|/)(\d+)(?:\.html)?(?:$|[#?])", url)
    if not match:
        raise ValueError("Could not find a numeric gallery id in the URL")
    return match.group(1)


def parse_gallery_info(source: str) -> dict[str, Any]:
    match = re.search(r"var\s+galleryinfo\s*=\s*(\{.*\})\s*;?\s*$", source, re.DOTALL)
    if not match:
        raise ValueError("The gallery metadata was not found")
    return json.loads(match.group(1))


def parse_gg(source: str) -> tuple[str, set[int]]:
    base_match = re.search(r"b:\s*'([^']+)'", source)
    if not base_match:
        raise ValueError("The CDN base path was not found in gg.js")
    mapped = {int(value) for value in re.findall(r"case\s+(\d+)\s*:", source)}
    return base_match.group(1), mapped


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip().rstrip(".")
    return value or "hitomi-gallery"


def image_url(file_info: dict[str, Any], cdn_domain: str, base_path: str, mapped: set[int]) -> str:
    image_hash = file_info["hash"]
    tail_value = int(image_hash[-1] + image_hash[-3:-1], 16)
    cdn_number = 2 if tail_value in mapped else 1
    path_value = str(tail_value) + "/" + image_hash
    return f"https://w{cdn_number}.{cdn_domain}/{base_path}{path_value}.webp"


def alternate_image_url(file_info: dict[str, Any], cdn_domain: str, base_path: str, mapped: set[int]) -> str:
    primary = image_url(file_info, cdn_domain, base_path, mapped)
    alternate_number = "2" if ".w1." in primary else "1"
    return re.sub(r"https://w[12]\.", f"https://w{alternate_number}.", primary)


def download_one(
    index: int,
    file_info: dict[str, Any],
    part_dir: Path,
    cdn_domain: str,
    base_path: str,
    mapped: set[int],
    referer: str,
) -> tuple[int, Path, str]:
    filename = re.sub(r"\.[^.]+$", ".webp", file_info["name"])
    destination = part_dir / f"{index:04d}_{filename}"
    primary = image_url(file_info, cdn_domain, base_path, mapped)
    candidates = [primary, alternate_image_url(file_info, cdn_domain, base_path, mapped)]
    last_error: Exception | None = None

    for attempt in range(3):
        for candidate in candidates:
            try:
                data = fetch_bytes(candidate, referer)
                if not data:
                    raise ValueError("empty response")
                temporary = destination.with_suffix(destination.suffix + ".part")
                temporary.write_bytes(data)
                temporary.replace(destination)
                return index, destination, candidate
            except (OSError, urllib.error.URLError, ValueError) as error:
                last_error = error
        time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"{file_info['name']}: {last_error}")


def load_gallery(page_url: str) -> tuple[dict[str, Any], list[dict[str, Any]], str, str, str, set[int]]:
    gallery_id = gallery_id_from_url(page_url)
    page_source = fetch_text(page_url)
    host_match = re.search(r"(?:https?:)?//([^/]+)/galleries/" + re.escape(gallery_id) + r"\.js", page_source)
    asset_host = host_match.group(1) if host_match else DEFAULT_ASSET_HOST
    domain_match = re.match(r"(?:[^.]+\.)?(.+)$", asset_host)
    cdn_domain = domain_match.group(1) if domain_match else "gold-usergeneratedcontent.net"

    gallery_source = fetch_text(f"https://{asset_host}/galleries/{gallery_id}.js", page_url)
    gallery = parse_gallery_info(gallery_source)
    gg_source = fetch_text(f"https://{asset_host}/gg.js", page_url)
    base_path, mapped = parse_gg(gg_source)
    files = gallery.get("files") or []
    if not files:
        raise ValueError("The gallery contains no downloadable files")
    title = safe_name(gallery.get("japanese_title") or gallery.get("title") or gallery_id)
    return gallery, files, title, cdn_domain, base_path, mapped


def run_download(
    page_url: str,
    workers: int,
    output_zip: Path,
    keep_images: bool = False,
    progress_callback: Any = None,
    status_callback: Any = None,
    stop_event: threading.Event | None = None,
) -> Path:
    if workers < 1 or workers > 32:
        raise ValueError("Workers must be between 1 and 32")

    def progress(done: int, total: int) -> None:
        if progress_callback:
            progress_callback(done, total)

    def status(message: str) -> None:
        if status_callback:
            status_callback(message)

    status("Reading gallery metadata...")
    _gallery, files, title, cdn_domain, base_path, mapped = load_gallery(page_url)
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    part_dir = output_zip.parent / f".{output_zip.stem}.parts"
    part_dir.mkdir(parents=True, exist_ok=True)
    status(f"Gallery: {title} | {len(files)} files | {workers} workers")

    completed = 0
    completed_lock = threading.Lock()
    downloaded: dict[int, Path] = {}

    def task(file_number: int, file_info: dict[str, Any]) -> tuple[int, Path, str]:
        if stop_event and stop_event.is_set():
            raise RuntimeError("Download stopped by user")
        return download_one(
            file_number,
            file_info,
            part_dir,
            cdn_domain,
            base_path,
            mapped,
            page_url,
        )

    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(task, index, file_info) for index, file_info in enumerate(files, start=1)]
        for future in as_completed(futures):
            try:
                index, path, _url = future.result()
                downloaded[index] = path
                with completed_lock:
                    completed += 1
                progress(completed, len(files))
            except Exception as error:
                failures.append(str(error))

    if stop_event and stop_event.is_set():
        raise RuntimeError("Download stopped by user")
    if failures:
        raise RuntimeError("Failed files: " + " | ".join(failures[:5]))

    status("Creating ZIP archive...")
    if output_zip.exists():
        output_zip.unlink()
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, file_info in enumerate(files, start=1):
            archive.write(downloaded[index], arcname=re.sub(r"\.[^.]+$", ".webp", file_info["name"]))

    if not keep_images:
        for path in downloaded.values():
            path.unlink(missing_ok=True)
        part_dir.rmdir()
    return output_zip


def cli_main() -> int:
    parser = argparse.ArgumentParser(description="Download a Hitomi gallery with concurrent workers")
    parser.add_argument("url", nargs="?", help="Hitomi gallery URL")
    parser.add_argument("-w", "--workers", type=int, default=8, help="parallel downloads (default: 8)")
    parser.add_argument("-o", "--output", type=Path, help="output ZIP path")
    parser.add_argument("--keep-images", action="store_true", help="keep the downloaded WEBP files")
    parser.add_argument("-y", "--yes", action="store_true", help="start without confirmation")
    args = parser.parse_args()

    if args.workers < 1 or args.workers > 32:
        parser.error("--workers must be between 1 and 32")

    page_url = args.url or input("Gallery URL: ").strip()
    print("Reading gallery metadata...")
    _gallery, files, title, _cdn_domain, _base_path, _mapped = load_gallery(page_url)
    output_zip = args.output or (Path.cwd() / "hitomi-downloads" / f"{title}.zip")

    print(f"Gallery: {title}")
    print(f"Files: {len(files)} | Workers: {args.workers}")
    print(f"Output: {output_zip}")
    if not args.yes:
        answer = input("Start downloading? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled before downloading.")
            return 0

    run_download(
        page_url,
        args.workers,
        output_zip,
        keep_images=args.keep_images,
        progress_callback=lambda done, total: print(f"\rDownloading: {done}/{total}", end="", flush=True),
        status_callback=print,
    )
    print(f"\nDone: {output_zip}")
    return 0


DEFAULT_URL = ""


def gui_main() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("Hitomi 多线程下载器")
    root.geometry("720x390")
    root.minsize(620, 340)

    url_var = tk.StringVar(value=DEFAULT_URL)
    workers_var = tk.IntVar(value=8)
    output_var = tk.StringVar(value=str(Path.home() / "Downloads"))
    status_var = tk.StringVar(value="Ready")
    progress_var = tk.DoubleVar(value=0)
    detail_var = tk.StringVar(value="")
    events: queue.Queue[tuple[str, Any]] = queue.Queue()
    stop_event = threading.Event()
    running = False

    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(1, weight=1)

    ttk.Label(frame, text="链接").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=7)
    url_entry = ttk.Entry(frame, textvariable=url_var)
    url_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=7)

    ttk.Label(frame, text="并发数").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=7)
    worker_spin = ttk.Spinbox(frame, from_=1, to=32, textvariable=workers_var, width=8)
    worker_spin.grid(row=1, column=1, sticky="w", pady=7)
    ttk.Label(frame, text="建议 8~16，最大 32").grid(row=1, column=2, sticky="w", padx=10, pady=7)

    ttk.Label(frame, text="输出目录").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=7)
    output_entry = ttk.Entry(frame, textvariable=output_var)
    output_entry.grid(row=2, column=1, sticky="ew", pady=7)

    def choose_output() -> None:
        selected = filedialog.askdirectory(initialdir=output_var.get() or str(Path.home()))
        if selected:
            output_var.set(selected)

    browse_button = ttk.Button(frame, text="选择...", command=choose_output)
    browse_button.grid(row=2, column=2, sticky="w", padx=(10, 0), pady=7)

    progress = ttk.Progressbar(frame, variable=progress_var, maximum=100, mode="determinate")
    progress.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(22, 6))
    ttk.Label(frame, textvariable=detail_var).grid(row=4, column=0, columnspan=3, sticky="w", pady=3)
    ttk.Label(frame, textvariable=status_var, wraplength=680).grid(row=5, column=0, columnspan=3, sticky="w", pady=3)

    button_row = ttk.Frame(frame)
    button_row.grid(row=6, column=0, columnspan=3, sticky="e", pady=(22, 0))
    start_button = ttk.Button(button_row, text="开始下载")
    start_button.pack(side="left", padx=5)
    stop_button = ttk.Button(button_row, text="停止", state="disabled")
    stop_button.pack(side="left", padx=5)

    def set_running(value: bool) -> None:
        nonlocal running
        running = value
        state = "disabled" if value else "normal"
        url_entry.configure(state=state)
        worker_spin.configure(state=state)
        output_entry.configure(state=state)
        browse_button.configure(state=state)
        start_button.configure(state="disabled" if value else "normal")
        stop_button.configure(state="normal" if value else "disabled")

    def start_download() -> None:
        if running:
            return
        page_url = url_var.get().strip()
        output_dir = output_var.get().strip()
        try:
            gallery_id_from_url(page_url)
            workers = int(workers_var.get())
            if not 1 <= workers <= 32:
                raise ValueError("并发数必须在 1 到 32 之间，建议16")
            if not output_dir:
                raise ValueError("请选择输出目录")
        except (ValueError, tk.TclError) as error:
            messagebox.showerror("输入有误", str(error))
            return

        stop_event.clear()
        progress_var.set(0)
        detail_var.set("准备中...")
        status_var.set("正在读取页面信息...")
        set_running(True)

        def worker() -> None:
            try:
                # Metadata is loaded once here to determine the archive name.
                _gallery, _files, title, _domain, _base, _mapped = load_gallery(page_url)
                output_zip = Path(output_dir) / f"{title}.zip"
                events.put(("status", f"目标文件：{output_zip}"))
                result = run_download(
                    page_url,
                    workers,
                    output_zip,
                    progress_callback=lambda done, total: events.put(("progress", (done, total))),
                    status_callback=lambda message: events.put(("status", message)),
                    stop_event=stop_event,
                )
                events.put(("done", result))
            except Exception as error:
                events.put(("error", str(error)))

        threading.Thread(target=worker, name="hitomi-download", daemon=True).start()

    def stop_download() -> None:
        if running:
            stop_event.set()
            status_var.set("正在停止，等待当前请求结束...")
            stop_button.configure(state="disabled")

    start_button.configure(command=start_download)
    stop_button.configure(command=stop_download)

    def poll_events() -> None:
        try:
            while True:
                event, value = events.get_nowait()
                if event == "progress":
                    done, total = value
                    progress_var.set(done / total * 100)
                    detail_var.set(f"已完成 {done}/{total} ({done / total * 100:.1f}%)")
                elif event == "status":
                    status_var.set(str(value))
                elif event == "done":
                    progress_var.set(100)
                    detail_var.set("下载完成")
                    status_var.set(f"已保存：{value}")
                    set_running(False)
                    messagebox.showinfo("下载完成", f"ZIP 已保存到：\n{value}")
                elif event == "error":
                    detail_var.set("任务结束")
                    status_var.set(str(value))
                    set_running(False)
                    if "stopped by user" not in str(value):
                        messagebox.showerror("下载失败", str(value))
        except queue.Empty:
            pass
        root.after(100, poll_events)

    def close_window() -> None:
        if running:
            stop_event.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close_window)
    poll_events()
    root.mainloop()
    return 0


def main() -> int:
    return cli_main() if len(sys.argv) > 1 else gui_main()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
