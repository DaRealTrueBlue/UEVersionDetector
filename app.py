from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Optional

try:
    import psutil
except ImportError:
    psutil = None

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None

try:
    import win32gui
    import win32process
except ImportError:
    win32gui = None
    win32process = None


BUILD_VERSION_RELATIVE_PATH = Path("Engine") / "Build" / "Build.version"
APP_VERSION = "1.0.0"
GITHUB_REPOSITORY = "DaRealTrueBlue/UEVersionDetector"
GITHUB_RELEASES_LATEST_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_REPOSITORY}/releases"
UPDATE_TIMEOUT_SECONDS = 20
UNREAL_RELEASE_REGEX = re.compile(rb"\+\+UE(?P<major>[45])\+Release-(?P<release>\d+(?:\.\d+){0,2})")
UNREAL_TEXT_REGEX = re.compile(rb"Unreal Engine\s*(?P<major>[45])\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?", re.IGNORECASE)
UNREAL_SHORT_REGEX = re.compile(rb"UE(?P<major>[45])(?:[\._\-])(?P<minor>\d+)(?:[\._\-](?P<patch>\d+))?")
UNREAL_UNREALENGINE_REGEX = re.compile(rb"UnrealEngine[-_ ]?(?P<major>[45])\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?", re.IGNORECASE)
UNREAL_PREFIX_VERSION_REGEX = re.compile(rb"(?P<major>[45])\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?\+\+UE(?P=major)\+", re.IGNORECASE)
UNREAL_CONTEXT_VERSION_REGEX = re.compile(
    rb"(?:UE|UnrealEngine|Unreal Engine)[^0-9]{0,24}(?P<major>[45])\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?",
    re.IGNORECASE,
)
UNREAL_MAJOR_ONLY_REGEXES = (
    re.compile(rb"\+\+UE(?P<major>[45])\+", re.IGNORECASE),
    re.compile(rb"\bUE(?P<major>[45])\b", re.IGNORECASE),
    re.compile(rb"Unreal Engine\s*(?P<major>[45])\b", re.IGNORECASE),
)

ProgressCallback = Optional[Callable[[str], None]]


@dataclass
class DetectionResult:
    version: Optional[str]
    confidence: str
    source: str
    details: str


@dataclass
class ReleaseInfo:
    version: str
    tag_name: str
    name: str
    html_url: str
    asset_name: Optional[str]
    asset_url: Optional[str]


def normalize_version_text(version_text: str) -> str:
    normalized = version_text.strip()
    if normalized.lower().startswith("v"):
        normalized = normalized[1:]
    match = re.search(r"\d+(?:\.\d+){0,3}", normalized)
    return match.group(0) if match else "0.0.0"


def version_key(version_text: str) -> tuple[int, ...]:
    normalized = normalize_version_text(version_text)
    values: list[int] = []
    for token in normalized.split("."):
        try:
            values.append(int(token))
        except ValueError:
            values.append(0)
    while len(values) < 4:
        values.append(0)
    return tuple(values)


def is_newer_version(candidate: str, baseline: str) -> bool:
    return version_key(candidate) > version_key(baseline)


def pick_release_asset(assets: list[dict]) -> tuple[Optional[str], Optional[str]]:
    if not assets:
        return None, None

    preferred = [
        asset
        for asset in assets
        if str(asset.get("name", "")).lower().endswith(".exe")
        and "ueversiondetector" in str(asset.get("name", "")).lower()
    ]
    generic_exe = [asset for asset in assets if str(asset.get("name", "")).lower().endswith(".exe")]
    selection = preferred[0] if preferred else (generic_exe[0] if generic_exe else assets[0])
    return selection.get("name"), selection.get("browser_download_url")


def fetch_latest_github_release() -> Optional[ReleaseInfo]:
    request = urllib.request.Request(
        GITHUB_RELEASES_LATEST_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "UEVersionDetector-Updater",
        },
    )
    with urllib.request.urlopen(request, timeout=UPDATE_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))

    tag_name = str(payload.get("tag_name") or "")
    name = str(payload.get("name") or tag_name or "Latest release")
    html_url = str(payload.get("html_url") or GITHUB_RELEASES_PAGE)
    version = normalize_version_text(tag_name or name)
    asset_name, asset_url = pick_release_asset(payload.get("assets") or [])

    return ReleaseInfo(
        version=version,
        tag_name=tag_name,
        name=name,
        html_url=html_url,
        asset_name=asset_name,
        asset_url=asset_url,
    )


def download_release_asset(url: str, destination: Path, progress: Optional[Callable[[str], None]] = None) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "UEVersionDetector-Updater",
        },
    )
    with urllib.request.urlopen(request, timeout=UPDATE_TIMEOUT_SECONDS) as response:
        total = int(response.headers.get("Content-Length", "0") or "0")
        downloaded = 0
        with destination.open("wb") as file_handle:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                file_handle.write(chunk)
                downloaded += len(chunk)
                if progress and total > 0:
                    percent = min(int((downloaded / total) * 100), 100)
                    progress(f"Downloading update... {percent}%")


def create_self_replace_script(pid: int, current_exe: Path, downloaded_exe: Path) -> Path:
    script_path = Path(tempfile.gettempdir()) / f"ueversiondetector_update_{pid}.bat"
    script_body = f"""@echo off
setlocal
set "TARGET_EXE={current_exe}"
set "DOWNLOAD_EXE={downloaded_exe}"
set "WAIT_PID={pid}"

:wait_process
tasklist /FI "PID eq %WAIT_PID%" 2>NUL | find "%WAIT_PID%" >NUL
if not errorlevel 1 (
    timeout /t 1 /nobreak >NUL
    goto wait_process
)

copy /Y "%DOWNLOAD_EXE%" "%TARGET_EXE%" >NUL
start "" "%TARGET_EXE%"
del "%DOWNLOAD_EXE%" >NUL 2>&1
del "%~f0"
endlocal
"""
    script_path.write_text(script_body, encoding="utf-8")
    return script_path


class UnrealVersionDetector:
    @staticmethod
    def format_version(major: str | int, minor: str | int, patch: str | int | None = None) -> str:
        if patch is None or str(patch) == "":
            return f"{major}.{minor}"
        return f"{major}.{minor}.{patch}"

    @staticmethod
    def parse_build_version(build_version_file: Path) -> Optional[DetectionResult]:
        try:
            data = json.loads(build_version_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None

        major = data.get("MajorVersion")
        minor = data.get("MinorVersion")
        patch = data.get("PatchVersion")
        if major is None or minor is None:
            return None

        return DetectionResult(
            version=UnrealVersionDetector.format_version(major, minor, patch),
            confidence="high",
            source=str(build_version_file),
            details="Detected from Engine/Build/Build.version",
        )

    @staticmethod
    def parse_uproject(uproject_file: Path) -> Optional[DetectionResult]:
        try:
            data = json.loads(uproject_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None

        association = data.get("EngineAssociation")
        if not association:
            return None

        version_match = re.search(r"([45]\.\d+(?:\.\d+)?)", str(association))
        if not version_match:
            return None

        return DetectionResult(
            version=version_match.group(1),
            confidence="medium",
            source=str(uproject_file),
            details="Detected from .uproject EngineAssociation",
        )

    @staticmethod
    def read_binary_windows(binary_file: Path, max_window_bytes: int = 32_000_000) -> bytes:
        try:
            file_size = binary_file.stat().st_size
        except OSError:
            return b""

        if file_size <= 0:
            return b""

        windows: list[bytes] = []
        offsets = [0]

        if file_size > max_window_bytes:
            offsets.append(max((file_size // 2) - (max_window_bytes // 2), 0))
            offsets.append(max(file_size - max_window_bytes, 0))

        try:
            with binary_file.open("rb") as file_handle:
                for offset in offsets:
                    file_handle.seek(offset)
                    windows.append(file_handle.read(max_window_bytes))
        except OSError:
            return b""

        return b"\n".join(windows)

    @staticmethod
    def collapse_utf16_like_ascii(data: bytes) -> bytes:
        collapsed = bytearray()
        index = 0
        data_len = len(data)

        while index + 1 < data_len:
            char = data[index]
            null_byte = data[index + 1]
            if 32 <= char <= 126 and null_byte == 0:
                collapsed.append(char)
                index += 2
                continue
            index += 1

        return bytes(collapsed)

    @staticmethod
    def build_scan_streams(data: bytes) -> list[bytes]:
        streams = [data]
        utf16_like = UnrealVersionDetector.collapse_utf16_like_ascii(data)
        if len(utf16_like) >= 64:
            streams.append(utf16_like)
        return streams

    @staticmethod
    def _binary_confidence_for_match(regex: re.Pattern[bytes], version: str) -> str:
        parts = version.split(".")
        has_patch = len(parts) >= 3

        if regex in (UNREAL_RELEASE_REGEX, UNREAL_PREFIX_VERSION_REGEX):
            return "high" if has_patch else "medium"

        if regex in (UNREAL_TEXT_REGEX, UNREAL_UNREALENGINE_REGEX, UNREAL_CONTEXT_VERSION_REGEX):
            return "medium" if has_patch else "low"

        return "low"

    @staticmethod
    def sniff_binary_for_unreal_version(binary_file: Path, progress: ProgressCallback = None) -> Optional[DetectionResult]:
        if progress:
            progress(f"Scanning binary: {binary_file.name}")

        data = UnrealVersionDetector.read_binary_windows(binary_file)
        if not data:
            return None

        scan_streams = UnrealVersionDetector.build_scan_streams(data)

        for regex, label in (
            (UNREAL_RELEASE_REGEX, "release signature"),
            (UNREAL_TEXT_REGEX, "engine text signature"),
            (UNREAL_SHORT_REGEX, "UE short signature"),
            (UNREAL_UNREALENGINE_REGEX, "UnrealEngine signature"),
            (UNREAL_PREFIX_VERSION_REGEX, "version prefix signature"),
            (UNREAL_CONTEXT_VERSION_REGEX, "contextual version signature"),
        ):
            match = None
            matched_stream_label = "binary"

            for stream in scan_streams:
                maybe = regex.search(stream)
                if maybe:
                    match = maybe
                    if stream is not data:
                        matched_stream_label = "utf16-like strings"
                    break

            if not match:
                continue

            major = match.group("major").decode("ascii", errors="ignore")
            if regex is UNREAL_RELEASE_REGEX:
                release_bytes = match.groupdict().get("release")
                release_text = release_bytes.decode("ascii", errors="ignore") if release_bytes else ""
                if release_text.startswith(f"{major}."):
                    version = release_text
                elif release_text:
                    version = f"{major}.{release_text}"
                else:
                    continue
            else:
                minor = match.group("minor").decode("ascii", errors="ignore")
                patch_bytes = match.groupdict().get("patch")
                patch = patch_bytes.decode("ascii", errors="ignore") if patch_bytes else None
                version = UnrealVersionDetector.format_version(major, minor, patch)

            confidence = UnrealVersionDetector._binary_confidence_for_match(regex, version)

            return DetectionResult(
                version=version,
                confidence=confidence,
                source=str(binary_file),
                details=f"Detected from executable {label} ({matched_stream_label})",
            )

        for regex in UNREAL_MAJOR_ONLY_REGEXES:
            match = None
            matched_stream_label = "binary"
            for stream in scan_streams:
                maybe = regex.search(stream)
                if maybe:
                    match = maybe
                    if stream is not data:
                        matched_stream_label = "utf16-like strings"
                    break

            if not match:
                continue

            major = match.group("major").decode("ascii", errors="ignore")
            return DetectionResult(
                version=f"{major}.x",
                confidence="low",
                source=str(binary_file),
                details=f"Detected major Unreal generation only ({matched_stream_label})",
            )

        return None

    @staticmethod
    def prioritize_module_paths(paths: list[Path]) -> list[Path]:
        priority: list[Path] = []
        secondary: list[Path] = []

        for path in paths:
            lowered = path.name.lower()
            full_lowered = str(path).lower()
            if any(token in lowered for token in ("unreal", "ue4", "ue5", "engine")) or "engine" in full_lowered:
                priority.append(path)
            else:
                secondary.append(path)

        return priority + secondary

    @staticmethod
    def find_candidate_binaries(search_root: Path) -> list[Path]:
        if search_root.is_file() and search_root.suffix.lower() == ".exe":
            return [search_root]

        if not search_root.exists() or not search_root.is_dir():
            return []

        candidates: list[Path] = []
        if search_root.name.lower() == "win64":
            candidates.extend(sorted(search_root.glob("*.exe")))
            candidates.extend(sorted(search_root.glob("*.dll")))
            return candidates[:120]

        common_dirs = [
            search_root,
            search_root / "Binaries" / "Win64",
            search_root / "Engine" / "Binaries" / "Win64",
            search_root / "Plugins",
        ]

        for directory in common_dirs:
            if directory.exists() and directory.is_dir():
                candidates.extend(sorted(directory.glob("*.exe")))
                candidates.extend(sorted(directory.glob("*.dll")))

        if candidates:
            return candidates[:120]

        unreal_named: list[Path] = []
        generic: list[Path] = []
        for root, _, files in os.walk(search_root):
            root_path = Path(root)
            depth = len(root_path.parts) - len(search_root.parts)
            if depth > 6:
                continue

            for file_name in files:
                lowered = file_name.lower()
                if not (lowered.endswith(".exe") or lowered.endswith(".dll")):
                    continue

                candidate = root_path / file_name
                if "unreal" in lowered or "ue4" in lowered or "ue5" in lowered:
                    unreal_named.append(candidate)
                else:
                    generic.append(candidate)

                if len(unreal_named) >= 50 and len(generic) >= 80:
                    break

        return (unreal_named + generic)[:120]

    @staticmethod
    def detect_from_folder(folder_path: str, progress: ProgressCallback = None) -> DetectionResult:
        root = Path(folder_path)
        if progress:
            progress(f"Analyzing folder: {root}")

        if not root.exists() or not root.is_dir():
            return DetectionResult(None, "none", folder_path, "Folder does not exist or is not a directory")

        if progress:
            progress("Checking Engine/Build/Build.version...")
        build_result = UnrealVersionDetector.parse_build_version(root / BUILD_VERSION_RELATIVE_PATH)
        if build_result:
            return build_result

        if progress:
            progress("Checking .uproject EngineAssociation...")
        for project_file in root.glob("*.uproject"):
            project_result = UnrealVersionDetector.parse_uproject(project_file)
            if project_result:
                return project_result

        if progress:
            progress("Scanning candidate binaries for Unreal signatures...")
        candidates = UnrealVersionDetector.find_candidate_binaries(root)
        if progress:
            progress(f"Binary candidates discovered: {len(candidates)}")

        for index, binary_path in enumerate(candidates, start=1):
            result = UnrealVersionDetector.sniff_binary_for_unreal_version(binary_path, progress if index <= 20 else None)
            if result:
                return result

        return DetectionResult(None, "none", folder_path, "No Unreal Engine version signature found")

    @staticmethod
    def detect_from_process(pid: int, progress: ProgressCallback = None) -> DetectionResult:
        if psutil is None:
            return DetectionResult(None, "none", str(pid), "psutil is not installed; process detection unavailable")

        try:
            process = psutil.Process(pid)
            exe_path = Path(process.exe())
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError) as error:
            return DetectionResult(None, "none", str(pid), f"Could not access process executable: {error}")

        if progress:
            progress(f"Scanning main executable: {exe_path.name}")
        direct_result = UnrealVersionDetector.sniff_binary_for_unreal_version(exe_path, progress)
        if direct_result:
            return direct_result

        try:
            memory_maps = process.memory_maps(grouped=True)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            memory_maps = []

        module_paths: list[Path] = []
        seen: set[str] = set()
        for region in memory_maps:
            mapped_path = getattr(region, "path", "")
            if not mapped_path:
                continue
            lowered = str(mapped_path).lower()
            if not (lowered.endswith(".dll") or lowered.endswith(".exe")):
                continue
            if lowered in seen:
                continue
            seen.add(lowered)
            module_paths.append(Path(mapped_path))

        prioritized_modules = UnrealVersionDetector.prioritize_module_paths(module_paths)[:180]
        if progress:
            progress(f"Scanning loaded modules: {len(prioritized_modules)}")

        for index, module_path in enumerate(prioritized_modules, start=1):
            module_result = UnrealVersionDetector.sniff_binary_for_unreal_version(module_path, progress if index <= 30 else None)
            if module_result:
                module_result.details = f"Detected from loaded process module: {module_result.details}"
                return module_result

        search_roots = [
            exe_path.parent,
            exe_path.parent.parent if exe_path.parent.parent != exe_path.parent else exe_path.parent,
        ]

        if progress:
            progress("Falling back to nearby folder scan...")
        for root in search_roots:
            folder_result = UnrealVersionDetector.detect_from_folder(str(root), progress)
            if folder_result.version:
                return folder_result

        return DetectionResult(None, "none", str(exe_path), "Process found, but Unreal Engine version could not be detected")


class UnrealVersionDetectorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Unreal Engine Version Detector")
        self.root.geometry("1180x760")
        self.root.minsize(860, 560)

        self.task_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.scan_running = False
        self.update_check_running = False
        self.update_download_running = False
        self.latest_release: Optional[ReleaseInfo] = None

        self.selected_folder = tk.StringVar()
        self.scan_mode = tk.StringVar(value="folder")
        self.status_text = tk.StringVar(value="Ready")
        self.version_text = tk.StringVar(value="-")
        self.confidence_text = tk.StringVar(value="-")
        self.source_text = tk.StringVar(value="-")
        self.process_filter_text = tk.StringVar(value="")
        self.folder_preset_text = tk.StringVar(value="Auto (Smart)")
        self.last_result: Optional[DetectionResult] = None

        self.folder_presets: dict[str, list[str]] = {
            "Auto (Smart)": [],
            "Binaries Win64": ["Binaries/Win64", "Win64"],
            "Engine Win64": ["Engine/Binaries/Win64"],
            "Plugins": ["Plugins"],
            "Parent Folder": [".."],
        }

        self.process_options: list[str] = []
        self.process_lookup: dict[str, int] = {}

        self.primary_bg = "#0e131d"
        self.panel_bg = "#161f2e"
        self.input_bg = "#0c1421"
        self.text_fg = "#e8eef9"
        self.muted_fg = "#91a4c3"
        self.accent = "#3d8bfd"
        self.border = "#223149"
        self.success = "#21c06c"
        self.warning = "#f2a63b"
        self.error = "#e74c3c"

        self.folder_entry: Optional[ttk.Entry] = None
        self.process_listbox: Optional[tk.Listbox] = None
        self.process_search_entry: Optional[ttk.Entry] = None
        self.preset_button_by_label: dict[str, tk.Button] = {}
        self.filtered_process_display_values: list[str] = []
        self.folder_panel: Optional[ttk.Frame] = None
        self.process_panel: Optional[ttk.Frame] = None
        self.mode_folder_button: Optional[tk.Button] = None
        self.mode_process_button: Optional[tk.Button] = None
        self.badge: Optional[tk.Label] = None
        self.details_text: Optional[tk.Text] = None
        self.log_widget: Optional[scrolledtext.ScrolledText] = None
        self.progress: Optional[ttk.Progressbar] = None

        self.folder_scan_button: Optional[ttk.Button] = None
        self.folder_browse_button: Optional[ttk.Button] = None
        self.process_scan_button: Optional[ttk.Button] = None
        self.process_refresh_button: Optional[ttk.Button] = None
        self.update_button: Optional[ttk.Button] = None

        self._set_window_icon()
        self._configure_style()
        self._build_ui()
        self.refresh_processes()
        self.root.after(120, self._poll_queue)
        self.root.after(1400, lambda: self.check_for_updates(user_initiated=False))

    def _set_window_icon(self) -> None:
        if not Image or not ImageTk:
            return

        icon_path = Path(__file__).parent / "icon.png"
        if not icon_path.exists():
            return

        try:
            icon = Image.open(icon_path)
            photo = ImageTk.PhotoImage(icon)
            self.root.iconphoto(True, photo)
        except Exception:
            return

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.configure(bg=self.primary_bg)

        style.configure("App.TFrame", background=self.primary_bg)
        style.configure("Panel.TFrame", background=self.panel_bg)
        style.configure("Panel.TLabelframe", background=self.panel_bg, foreground=self.text_fg, borderwidth=0, relief="flat")
        style.configure("Panel.TLabelframe.Label", background=self.panel_bg, foreground=self.text_fg, font=("Segoe UI Semibold", 10, "bold"))
        style.configure("Title.TLabel", background=self.primary_bg, foreground=self.text_fg, font=("Segoe UI Semibold", 19, "bold"))
        style.configure("Subtitle.TLabel", background=self.primary_bg, foreground=self.muted_fg, font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=self.panel_bg, foreground=self.text_fg, font=("Segoe UI", 10))
        style.configure("Hint.TLabel", background=self.panel_bg, foreground=self.muted_fg, font=("Segoe UI", 9))
        style.configure("Section.TLabel", background=self.panel_bg, foreground="#d8e6ff", font=("Segoe UI Semibold", 10, "bold"))
        style.configure("Value.TLabel", background=self.panel_bg, foreground=self.text_fg, font=("Segoe UI", 11, "bold"))
        style.configure("TButton", padding=(11, 7), font=("Segoe UI Semibold", 10, "bold"), borderwidth=0)
        style.configure("Accent.TButton", foreground="#ffffff", background=self.accent)
        style.map("Accent.TButton", background=[("active", "#2a79ed"), ("pressed", "#1f63c8")])
        style.configure("TEntry", fieldbackground=self.input_bg, foreground=self.text_fg, borderwidth=0, relief="flat")
        style.configure("Export.TButton", foreground="#cfe0ff", background="#1b2a40")
        style.map("Export.TButton", background=[("active", "#243b5d"), ("pressed", "#1b2f4d")])
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor=self.input_bg,
            background=self.accent,
            bordercolor=self.input_bg,
            lightcolor=self.accent,
            darkcolor=self.accent,
        )
        style.configure("Status.TLabel", background=self.primary_bg, foreground="#b7cae7", font=("Consolas", 10))

    def _build_ui(self) -> None:
        root_frame = ttk.Frame(self.root, style="App.TFrame", padding=14)
        root_frame.pack(fill="both", expand=True)
        root_frame.columnconfigure(0, weight=1)
        root_frame.rowconfigure(1, weight=1)

        header = ttk.Frame(root_frame, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)

        self.update_button = ttk.Button(
            header,
            text="Check for Updates",
            style="Export.TButton",
            command=lambda: self.check_for_updates(user_initiated=True),
        )
        self.update_button.pack(side="right", padx=(8, 0))

        ttk.Label(header, text="Unreal Engine Version Detector", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text=f"Version {APP_VERSION}", style="Subtitle.TLabel").pack(anchor="w", pady=(1, 0))

        body = ttk.Frame(root_frame, style="App.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=4)
        body.columnconfigure(1, weight=7)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body, style="Panel.TFrame", padding=14)
        right = ttk.Frame(body, style="Panel.TFrame", padding=14)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right.grid(row=0, column=1, sticky="nsew")

        controls = ttk.LabelFrame(left, text="Scan Controls", style="Panel.TLabelframe", padding=10)
        controls.pack(fill="x")

        mode_row = ttk.Frame(controls, style="Panel.TFrame")
        mode_row.pack(fill="x", pady=(0, 8))

        self.mode_folder_button = tk.Button(
            mode_row,
            text="Folder",
            font=("Segoe UI Semibold", 10, "bold"),
            bd=0,
            relief="flat",
            highlightthickness=0,
            highlightbackground=self.input_bg,
            padx=8,
            command=lambda: self._set_scan_mode("folder"),
        )
        self.mode_folder_button.pack(side="left", fill="x", expand=True, padx=(0, 4), ipady=6)

        self.mode_process_button = tk.Button(
            mode_row,
            text="Process",
            font=("Segoe UI Semibold", 10, "bold"),
            bd=0,
            relief="flat",
            highlightthickness=0,
            highlightbackground=self.input_bg,
            padx=8,
            command=lambda: self._set_scan_mode("process"),
        )
        self.mode_process_button.pack(side="left", fill="x", expand=True, padx=(4, 0), ipady=6)

        panel_host = ttk.Frame(controls, style="Panel.TFrame", height=250)
        panel_host.pack(fill="x")
        panel_host.pack_propagate(False)

        self.folder_panel = ttk.Frame(panel_host, style="Panel.TFrame")
        self.process_panel = ttk.Frame(panel_host, style="Panel.TFrame")
        self.folder_panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.process_panel.place(relx=0, rely=0, relwidth=1, relheight=1)

        for tab in (self.folder_panel, self.process_panel):
            tab.columnconfigure(0, weight=1)
            tab.rowconfigure(0, weight=0)
            tab.rowconfigure(1, weight=0)
            tab.rowconfigure(2, weight=0)
            tab.rowconfigure(3, weight=0)
            tab.rowconfigure(4, weight=0)
            tab.rowconfigure(5, weight=1)
            tab.grid_propagate(False)

        ttk.Label(self.folder_panel, text="Game install directory", style="Hint.TLabel").grid(row=0, column=0, sticky="w", pady=(10, 0))
        self.folder_entry = ttk.Entry(self.folder_panel, textvariable=self.selected_folder)
        self.folder_entry.grid(row=1, column=0, sticky="ew")

        ttk.Label(self.folder_panel, text="Scan preset", style="Hint.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))
        preset_row = ttk.Frame(self.folder_panel, style="Panel.TFrame")
        preset_row.grid(row=3, column=0, sticky="ew")
        for col in range(3):
            preset_row.columnconfigure(col, weight=1)
        for idx, label in enumerate(self.folder_presets.keys()):
            row = idx // 3
            col = idx % 3
            button = tk.Button(
                preset_row,
                text=label,
                font=("Segoe UI", 8, "bold"),
                bd=0,
                relief="flat",
                padx=8,
                pady=4,
                highlightthickness=0,
                highlightbackground=self.input_bg,
                command=lambda value=label: self._set_folder_preset(value),
            )
            button.grid(row=row, column=col, padx=(0, 5), pady=(0, 4), sticky="ew")
            self.preset_button_by_label[label] = button

        folder_buttons = ttk.Frame(self.folder_panel, style="Panel.TFrame")
        folder_buttons.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.folder_browse_button = ttk.Button(folder_buttons, text="Browse", width=12, command=self._browse_folder)
        self.folder_browse_button.pack(side="left")
        self.folder_scan_button = ttk.Button(folder_buttons, text="Scan Folder", width=14, style="Accent.TButton", command=self._scan_folder_clicked)
        self.folder_scan_button.pack(side="right")

        ttk.Label(self.folder_panel, text="", style="Hint.TLabel").grid(row=5, column=0, sticky="w")

        ttk.Label(self.process_panel, text="Running game process", style="Hint.TLabel").grid(row=0, column=0, sticky="w", pady=(10, 0))
        process_list_frame = ttk.Frame(self.process_panel, style="Panel.TFrame")
        process_list_frame.grid(row=1, column=0, sticky="nsew")
        process_list_frame.columnconfigure(0, weight=1)
        process_list_frame.rowconfigure(0, weight=1)

        process_scroll = ttk.Scrollbar(process_list_frame, orient="vertical")
        self.process_listbox = tk.Listbox(
            process_list_frame,
            height=6,
            bg=self.input_bg,
            fg=self.text_fg,
            selectbackground=self.accent,
            selectforeground="#ffffff",
            highlightthickness=0,
            highlightbackground=self.input_bg,
            relief="flat",
            borderwidth=0,
            activestyle="none",
            exportselection=False,
            font=("Segoe UI", 9),
            yscrollcommand=process_scroll.set,
        )
        self.process_listbox.grid(row=0, column=0, sticky="nsew")
        process_scroll.grid(row=0, column=1, sticky="ns")
        process_scroll.configure(command=self.process_listbox.yview)
        ttk.Label(self.process_panel, text="Filter list", style="Hint.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.process_search_entry = ttk.Entry(self.process_panel, textvariable=self.process_filter_text)
        self.process_search_entry.grid(row=3, column=0, sticky="ew")
        self.process_search_entry.bind("<KeyRelease>", lambda _event: self._filter_processes())

        process_buttons = ttk.Frame(self.process_panel, style="Panel.TFrame")
        process_buttons.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.process_refresh_button = ttk.Button(process_buttons, text="Refresh", width=12, command=self.refresh_processes)
        self.process_refresh_button.pack(side="left")
        self.process_scan_button = ttk.Button(process_buttons, text="Scan Process", width=14, style="Accent.TButton", command=self._scan_process_clicked)
        self.process_scan_button.pack(side="right")

        process_footer_text = "psutil required for process mode" if psutil is None else ""
        ttk.Label(self.process_panel, text=process_footer_text, style="Hint.TLabel").grid(row=5, column=0, sticky="w")

        self._refresh_preset_buttons()
        self._apply_scan_mode_ui()

        

        result_frame = ttk.LabelFrame(right, text="Detection Result", style="Panel.TLabelframe", padding=12)
        result_frame.pack(fill="x", pady=(0, 10))

        top = ttk.Frame(result_frame, style="Panel.TFrame")
        top.pack(fill="x")
        tk.Label(
            top,
            textvariable=self.version_text,
            bg=self.panel_bg,
            fg=self.text_fg,
            font=("Segoe UI Semibold", 32, "bold"),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)

        self.badge = tk.Label(top, text="NONE", bg="#4b5563", fg="#ffffff", font=("Segoe UI Semibold", 10, "bold"), padx=11, pady=5)
        self.badge.pack(side="right")

        meta = ttk.Frame(result_frame, style="Panel.TFrame")
        meta.pack(fill="x", pady=(10, 0))
        ttk.Label(meta, text="Confidence", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(meta, textvariable=self.confidence_text, style="Value.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 8))
        ttk.Label(meta, text="Source", style="Section.TLabel").grid(row=0, column=1, sticky="w", padx=(20, 0))
        ttk.Label(meta, textvariable=self.source_text, style="Hint.TLabel", wraplength=480).grid(row=1, column=1, sticky="w", padx=(20, 0), pady=(2, 8))
        meta.columnconfigure(1, weight=1)

        ttk.Label(result_frame, text="Details", style="Section.TLabel").pack(anchor="w", pady=(4, 4))
        self.details_text = tk.Text(
            result_frame,
            height=4,
            wrap="word",
            bg=self.input_bg,
            fg="#d9e6fb",
            relief="flat",
            bd=0,
            highlightthickness=0,
            insertbackground="#d9e6fb",
            font=("Consolas", 10),
        )
        self.details_text.pack(fill="x")
        self.details_text.configure(state="disabled")

        export_row = ttk.Frame(result_frame, style="Panel.TFrame")
        export_row.pack(fill="x", pady=(8, 0))
        ttk.Button(export_row, text="Export TXT", style="Export.TButton", command=self._export_result_txt).pack(side="left")
        ttk.Button(export_row, text="Export JSON", style="Export.TButton", command=self._export_result_json).pack(side="left", padx=(8, 0))

        log_frame = ttk.LabelFrame(right, text="Live Console", style="Panel.TLabelframe", padding=10)
        log_frame.pack(fill="both", expand=True)
        self.progress = ttk.Progressbar(log_frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 8))
        self.log_widget = scrolledtext.ScrolledText(
            log_frame,
            wrap="word",
            bg=self.input_bg,
            fg="#dce8fc",
            relief="flat",
            bd=0,
            highlightthickness=0,
            insertbackground="#dce8fc",
            font=("Consolas", 10),
        )
        self.log_widget.pack(fill="both", expand=True)
        self.log_widget.configure(state="disabled")

        status_bar = ttk.Frame(root_frame, style="App.TFrame")
        status_bar.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(status_bar, textvariable=self.status_text, style="Status.TLabel").pack(side="left")
        ttk.Label(status_bar, text="Made by DaRealTrueBlue", style="Subtitle.TLabel").pack(side="right")

    def _log(self, message: str) -> None:
        if not self.log_widget:
            return

        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_widget.configure(state="normal")
        self.log_widget.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_widget.see(tk.END)
        self.log_widget.configure(state="disabled")

    def _set_details(self, text: str) -> None:
        if not self.details_text:
            return

        self.details_text.configure(state="normal")
        self.details_text.delete("1.0", tk.END)
        self.details_text.insert(tk.END, text)
        self.details_text.configure(state="disabled")

    def _set_badge(self, confidence: str) -> None:
        if not self.badge:
            return

        normalized = confidence.lower().strip()
        if normalized == "high":
            color = self.success
        elif normalized == "medium":
            color = self.warning
        elif normalized == "low":
            color = "#f97316"
        else:
            color = "#4b5563"

        self.badge.configure(text=normalized.upper(), bg=color)

    def _reset_result(self) -> None:
        self.last_result = None
        self.version_text.set("-")
        self.confidence_text.set("-")
        self.source_text.set("-")
        self._set_badge("none")
        self._set_details("")

    def _set_scan_mode(self, mode: str) -> None:
        self.scan_mode.set(mode)
        self._apply_scan_mode_ui()

    def _set_folder_preset(self, label: str) -> None:
        self.folder_preset_text.set(label)
        self._refresh_preset_buttons()

    def _refresh_preset_buttons(self) -> None:
        current = self.folder_preset_text.get().strip() or "Auto (Smart)"
        for label, button in self.preset_button_by_label.items():
            selected = label == current
            button.configure(
                bg=self.accent if selected else self.input_bg,
                fg="#ffffff" if selected else self.muted_fg,
                activebackground=self.accent if selected else self.input_bg,
                activeforeground="#ffffff" if selected else self.muted_fg,
                highlightbackground=self.input_bg,
            )

    def _apply_scan_mode_ui(self) -> None:
        mode = self.scan_mode.get()

        if self.folder_panel and self.process_panel:
            if mode == "folder":
                self.folder_panel.lift()
            else:
                self.process_panel.lift()

        selected_bg = self.accent
        selected_fg = "#ffffff"
        idle_bg = self.input_bg
        idle_fg = self.muted_fg

        if self.mode_folder_button:
            folder_selected = mode == "folder"
            self.mode_folder_button.configure(
                bg=selected_bg if folder_selected else idle_bg,
                fg=selected_fg if folder_selected else idle_fg,
                activebackground=selected_bg if folder_selected else idle_bg,
                activeforeground=selected_fg if folder_selected else idle_fg,
                highlightbackground=self.input_bg,
            )

        if self.mode_process_button:
            process_selected = mode == "process"
            self.mode_process_button.configure(
                bg=selected_bg if process_selected else idle_bg,
                fg=selected_fg if process_selected else idle_fg,
                activebackground=selected_bg if process_selected else idle_bg,
                activeforeground=selected_fg if process_selected else idle_fg,
                highlightbackground=self.input_bg,
            )

    def _resolve_preset_folder(self, folder: str) -> tuple[str, str]:
        preset = self.folder_preset_text.get().strip() or "Auto (Smart)"
        candidates = self.folder_presets.get(preset, [])

        if not candidates:
            return folder, preset

        base = Path(folder)
        for relative in candidates:
            candidate = (base / relative).resolve()
            if candidate.exists() and candidate.is_dir():
                return str(candidate), preset

        return folder, f"{preset} (fallback to selected folder)"

    def _set_running_state(self, running: bool, status: str) -> None:
        self.scan_running = running
        self.status_text.set(status)

        state = "disabled" if running else "normal"
        for button in [self.folder_scan_button, self.folder_browse_button, self.process_scan_button, self.process_refresh_button]:
            if button:
                button.configure(state=state)

        for button in self.preset_button_by_label.values():
            button.configure(state=state)

        if self.mode_folder_button:
            self.mode_folder_button.configure(state=state)
        if self.mode_process_button:
            self.mode_process_button.configure(state=state)

        if self.process_search_entry:
            self.process_search_entry.configure(state=state)
        if self.process_listbox:
            self.process_listbox.configure(state=state)

        if self.progress:
            if running:
                self.progress.start(10)
            else:
                self.progress.stop()

    def check_for_updates(self, user_initiated: bool) -> None:
        if self.update_check_running or self.update_download_running:
            if user_initiated:
                self.status_text.set("Update check already running")
            return

        self.update_check_running = True
        if self.update_button:
            self.update_button.configure(state="disabled")
        self._log("Checking GitHub for updates...")
        if user_initiated:
            self.status_text.set("Checking for updates...")

        def worker() -> None:
            try:
                release = fetch_latest_github_release()
                self.task_queue.put(("update-check-complete", (release, user_initiated)))
            except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError, OSError) as error:
                self.task_queue.put(("update-check-error", (str(error), user_initiated)))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_update_check_result(self, release: Optional[ReleaseInfo], user_initiated: bool) -> None:
        self.update_check_running = False
        if self.update_button and not self.update_download_running:
            self.update_button.configure(state="normal")

        if not release:
            if user_initiated:
                messagebox.showinfo("Update check", "Could not fetch release information from GitHub.")
            return

        self.latest_release = release
        if is_newer_version(release.version, APP_VERSION):
            self._log(f"Update available: {release.version} (current {APP_VERSION})")
            self.status_text.set(f"Update available: {release.version}")
            should_install = messagebox.askyesno(
                "Update available",
                f"Version {release.version} is available on GitHub.\n"
                f"You are currently on {APP_VERSION}.\n\n"
                "Download and install now?",
            )
            if should_install:
                self._download_and_install_update(release)
        else:
            self._log("Application is up to date.")
            self.status_text.set("App is up to date")
            if user_initiated:
                messagebox.showinfo("Update check", f"You're up to date (v{APP_VERSION}).")

    def _download_and_install_update(self, release: ReleaseInfo) -> None:
        if self.update_download_running:
            return

        if not release.asset_url:
            self._log("No downloadable release asset found, opening releases page.")
            self.status_text.set("No release asset found")
            webbrowser.open(release.html_url or GITHUB_RELEASES_PAGE)
            return

        self.update_download_running = True
        if self.update_button:
            self.update_button.configure(state="disabled")
        self.status_text.set("Downloading update...")

        def worker() -> None:
            try:
                asset_name = release.asset_name or "UEVersionDetector_update.exe"
                destination = Path(tempfile.gettempdir()) / asset_name
                download_release_asset(
                    release.asset_url,
                    destination,
                    progress=lambda text: self.task_queue.put(("update-download-progress", text)),
                )
                self.task_queue.put(("update-download-complete", (destination, release)))
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as error:
                self.task_queue.put(("update-download-error", str(error)))

        threading.Thread(target=worker, daemon=True).start()

    def _install_downloaded_update(self, downloaded_exe: Path, release: ReleaseInfo) -> None:
        self.update_download_running = False

        if not getattr(sys, "frozen", False):
            self._log("Downloaded update in source mode; opening release page for manual install.")
            self.status_text.set("Update downloaded (manual install)")
            messagebox.showinfo(
                "Update downloaded",
                f"Downloaded {downloaded_exe.name}.\n\n"
                "You're running from source, so automatic replace is disabled.\n"
                "The GitHub releases page will open for manual update steps.",
            )
            webbrowser.open(release.html_url or GITHUB_RELEASES_PAGE)
            if self.update_button:
                self.update_button.configure(state="normal")
            return

        current_exe = Path(sys.executable)
        updater_script = create_self_replace_script(os.getpid(), current_exe, downloaded_exe)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(["cmd", "/c", str(updater_script)], creationflags=creationflags)
        self.status_text.set("Installing update and restarting...")
        self._log("Update installer started. Application will close to complete install.")
        self.root.after(250, self.root.destroy)

    def _start_scan_task(self, mode: str, value: str | int) -> None:
        if self.scan_running:
            return

        self._reset_result()
        if self.log_widget:
            self.log_widget.configure(state="normal")
            self.log_widget.delete("1.0", tk.END)
            self.log_widget.configure(state="disabled")

        self._set_running_state(True, "Scanning...")
        self._log(f"Starting {mode} scan...")

        def worker() -> None:
            try:
                if mode == "folder":
                    result = UnrealVersionDetector.detect_from_folder(
                        str(value),
                        progress=lambda msg: self.task_queue.put(("progress", msg)),
                    )
                else:
                    result = UnrealVersionDetector.detect_from_process(
                        int(value),
                        progress=lambda msg: self.task_queue.put(("progress", msg)),
                    )
                self.task_queue.put(("complete", result))
            except Exception as error:
                self.task_queue.put(("error", str(error)))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def _poll_queue(self) -> None:
        try:
            while True:
                event, payload = self.task_queue.get_nowait()
                if event == "progress":
                    self._log(str(payload))
                elif event == "complete":
                    result = payload
                    if isinstance(result, DetectionResult):
                        self.last_result = result
                        self.version_text.set(result.version or "Not detected")
                        self.confidence_text.set(result.confidence)
                        self.source_text.set(result.source)
                        self._set_badge(result.confidence)
                        self._set_details(result.details)
                        self._log("Scan complete.")
                        self._set_running_state(False, "Scan complete")
                elif event == "error":
                    self._log(f"Scan failed: {payload}")
                    self._set_running_state(False, "Scan failed")
                    messagebox.showerror("Scan failed", str(payload))
                elif event == "update-check-complete":
                    release, user_initiated = payload if isinstance(payload, tuple) else (None, False)
                    self._handle_update_check_result(release, bool(user_initiated))
                elif event == "update-check-error":
                    error_text, user_initiated = payload if isinstance(payload, tuple) else (str(payload), False)
                    self.update_check_running = False
                    if not self.update_download_running and self.update_button:
                        self.update_button.configure(state="normal")
                    self._log(f"Update check failed: {error_text}")
                    if user_initiated:
                        self.status_text.set("Update check failed")
                        messagebox.showerror("Update check failed", str(error_text))
                elif event == "update-download-progress":
                    self.status_text.set(str(payload))
                elif event == "update-download-complete":
                    destination, release = payload if isinstance(payload, tuple) else (None, None)
                    if isinstance(destination, Path) and isinstance(release, ReleaseInfo):
                        self._log(f"Update downloaded: {destination}")
                        self._install_downloaded_update(destination, release)
                elif event == "update-download-error":
                    self.update_download_running = False
                    if self.update_button:
                        self.update_button.configure(state="normal")
                    self._log(f"Update download failed: {payload}")
                    self.status_text.set("Update download failed")
                    messagebox.showerror("Update download failed", str(payload))
        except queue.Empty:
            pass

        self.root.after(120, self._poll_queue)

    def _browse_folder(self) -> None:
        selected = filedialog.askdirectory(title="Select Unreal game folder")
        if selected:
            self.selected_folder.set(selected)
            self.status_text.set("Folder selected")

    def _scan_folder_clicked(self) -> None:
        folder = self.selected_folder.get().strip()
        if not folder:
            messagebox.showwarning("Missing folder", "Please choose a game folder first.")
            return

        resolved_folder, preset_label = self._resolve_preset_folder(folder)
        self._log(f"Preset selected: {preset_label}")
        self._log(f"Scanning path: {resolved_folder}")
        self._start_scan_task("folder", resolved_folder)

    def _scan_process_clicked(self) -> None:
        if not self.process_listbox:
            return

        selection = self.process_listbox.curselection()
        if not selection:
            messagebox.showwarning("Missing process", "Please select a process first.")
            return

        selected_index = int(selection[0])
        if selected_index < 0 or selected_index >= len(self.filtered_process_display_values):
            messagebox.showwarning("Missing process", "Please select a valid process first.")
            return

        display = self.filtered_process_display_values[selected_index]

        pid = self.process_lookup.get(display)
        if pid is None:
            messagebox.showerror("Invalid process", "The selected process is no longer available. Refresh and try again.")
            return

        self._start_scan_task("process", pid)

    def _collect_processes(self) -> list[tuple[str, int]]:
        if psutil is None:
            return []

        window_pids: set[int] = set()
        if win32gui and win32process:
            def enum_window_callback(hwnd: int, _param: int) -> bool:
                if win32gui.IsWindowVisible(hwnd):
                    try:
                        _, pid = win32process.GetWindowThreadProcessId(hwnd)
                        window_pids.add(pid)
                    except Exception:
                        return True
                return True

            try:
                win32gui.EnumWindows(enum_window_callback, 0)
            except Exception:
                pass

        entries: list[tuple[str, int]] = []
        for process in psutil.process_iter(attrs=["pid", "name", "exe"]):
            pid = process.info.get("pid")
            name = process.info.get("name") or "<unknown>"
            exe = process.info.get("exe")
            if not pid or not exe:
                continue
            if not str(exe).lower().endswith(".exe"):
                continue
            if window_pids and int(pid) not in window_pids:
                continue

            entries.append((f"{name} (PID {pid})", int(pid)))

        entries.sort(key=lambda item: item[0].lower())
        return entries

    def _filter_processes(self) -> None:
        if not self.process_listbox:
            return

        search = self.process_filter_text.get().strip().lower()
        filtered = [item for item in self.process_options if search in item.lower()]
        self.filtered_process_display_values = filtered

        self.process_listbox.delete(0, tk.END)
        for display in filtered:
            self.process_listbox.insert(tk.END, display)

        if filtered:
            self.process_listbox.selection_set(0)
            self.process_listbox.activate(0)

    def refresh_processes(self) -> None:
        if self.scan_running:
            return

        self.status_text.set("Refreshing process list...")
        self.process_lookup.clear()
        self.process_options.clear()

        entries = self._collect_processes()
        for display, pid in entries:
            self.process_options.append(display)
            self.process_lookup[display] = pid

        self._filter_processes()
        if self.process_options:
            self.status_text.set(f"Process list ready: {len(self.process_options)} entries")
        else:
            if psutil is None:
                self.status_text.set("Process mode unavailable: install psutil")
            else:
                self.status_text.set("No visible runnable process found")

    def _export_result_txt(self) -> None:
        if not self.last_result:
            messagebox.showwarning("No result", "Run a scan before exporting.")
            return

        file_path = filedialog.asksaveasfilename(
            title="Export result as text",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not file_path:
            return

        lines = [
            "Unreal Engine Version Detector Result",
            f"Timestamp: {datetime.now().isoformat(timespec='seconds')}",
            f"Version: {self.last_result.version or 'Not detected'}",
            f"Confidence: {self.last_result.confidence}",
            f"Source: {self.last_result.source}",
            f"Details: {self.last_result.details}",
        ]

        try:
            Path(file_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.status_text.set("Result exported to TXT")
        except OSError as error:
            messagebox.showerror("Export failed", f"Could not export TXT file: {error}")

    def _export_result_json(self) -> None:
        if not self.last_result:
            messagebox.showwarning("No result", "Run a scan before exporting.")
            return

        file_path = filedialog.asksaveasfilename(
            title="Export result as JSON",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not file_path:
            return

        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "version": self.last_result.version,
            "confidence": self.last_result.confidence,
            "source": self.last_result.source,
            "details": self.last_result.details,
        }

        try:
            Path(file_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self.status_text.set("Result exported to JSON")
        except OSError as error:
            messagebox.showerror("Export failed", f"Could not export JSON file: {error}")


def main() -> None:
    root = tk.Tk()
    UnrealVersionDetectorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
