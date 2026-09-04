"""
CypraMatrixStudio desktop launcher.

Starts local FastAPI server, then opens a native WebView2 window.
Safe under python.exe and pythonw.exe (logs to data/launch.log).
"""

from __future__ import annotations

import os
import hashlib
import atexit
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

# Favor Ollama's memory-efficient attention path unless an operator explicitly
# supplied a different value before launching Studio.
os.environ.setdefault("OLLAMA_FLASH_ATTENTION", "1")
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD_ID = "1.1.15-files-consent-hardening-20260904"
APP_ID = "cypra-local-bv-chat"
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Running from a USB/removable drive: don't let the interpreter write .pyc
# bytecode cache files back to the stick on every import. Pure write-reduction,
# no behavior change — Python just recompiles from source in memory each run,
# which is negligible for a project this size.
sys.dont_write_bytecode = True

DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)
LOG_PATH = DATA / "launch.log"
_LOG_LOCK = threading.Lock()
_LOG_FH = None

# Both launch.log and server.log are append-only and were never capped, so on
# a long-lived USB install they grow forever and every appended line is a
# small disk write. Trim each back to its last ~2000 lines at startup (before
# anything else opens them) so total size stays bounded without losing recent
# history — same log content going forward, just not an unbounded file.
def _trim_log(path: Path, keep_lines: int = 2000) -> None:
    try:
        if not path.exists() or path.stat().st_size < 512_000:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= keep_lines:
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines[-keep_lines:]) + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


_trim_log(LOG_PATH)
_trim_log(DATA / "server.log")

# Clean up any orphaned .tmp files from an atomic write (settings.json,
# session files) that was interrupted by a crash or the drive being pulled
# mid-save. The real file was never touched in that case (that's the whole
# point of the atomic-write pattern) — this just clears the leftover temp
# file instead of letting it sit on disk indefinitely.
def _sweep_stray_tmp_files() -> None:
    try:
        for p in DATA.rglob("*.tmp"):
            try:
                p.unlink()
            except OSError:
                pass
    except Exception:
        pass


_sweep_stray_tmp_files()


def _open_log():
    global _LOG_FH
    if _LOG_FH is None:
        _LOG_FH = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    return _LOG_FH


def _log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n"
    try:
        with _LOG_LOCK:
            fh = _open_log()
            fh.write(line)
            fh.flush()
    except Exception:
        pass


def _fix_stdio() -> None:
    """Prevent uvicorn/logging from hanging under pythonw."""
    try:
        fh = _open_log()
        # Always tee stdio to the log under pythonw (no real console)
        if sys.executable.lower().endswith("pythonw.exe") or sys.stdout is None:
            sys.stdout = fh
            sys.stderr = fh
        else:
            # Keep console, but ensure stderr exists
            if sys.stderr is None:
                sys.stderr = fh
    except Exception:
        pass


_fix_stdio()

from engine.vault import load_settings  # noqa: E402

SETTINGS = load_settings(DATA / "settings.json")
PORT = int(os.environ.get("CYPRA_PORT") or os.environ.get("BRAIN_PORT") or SETTINGS.get("port") or 8765)
INSTANCE_ID = os.environ.get("CYPRA_INSTANCE_ID") or (
    "cypra-" + hashlib.sha256(str(ROOT).lower().encode("utf-8")).hexdigest()[:16]
)
os.environ.setdefault("CYPRA_INSTANCE_ID", INSTANCE_ID)
# Studio is intentionally loopback-only; host overrides are ignored.
HOST = "127.0.0.1"
ICON = ROOT / "app.ico"
def _env_flag(primary: str, legacy: str = "") -> bool:
    raw = os.environ.get(primary)
    if raw is None and legacy:
        raw = os.environ.get(legacy)
    return str(raw or "").strip().lower() in ("1", "true", "yes")


# CYPRA_* names are authoritative; BRAIN_* fallbacks remain for old launch scripts.
FORCE_BROWSER = _env_flag("CYPRA_BROWSER", "BRAIN_BROWSER")
USE_TRAY = _env_flag("CYPRA_TRAY", "BRAIN_TRAY")


def _msgbox(text: str, title: str = "CypraMatrixStudio", error: bool = True) -> None:
    try:
        import ctypes

        flags = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(0, str(text), str(title), flags)
    except Exception:
        _log(f"MSG: {text}")


def _port_free(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


def _health_ok(host: str, port: int, require_build: bool = True) -> bool:
    try:
        import urllib.request, json as _json

        req = urllib.request.Request(
            f"http://{host}:{port}/api/health",
            headers={"User-Agent": "CypraMatrixStudio/1.0"},
        )
        with urllib.request.urlopen(req, timeout=1.0) as r:
            if r.status != 200:
                return False
            try:
                payload = _json.loads(r.read().decode("utf-8"))
            except Exception:
                return False
            return (
                payload.get("app_id") == APP_ID
                and payload.get("instance_id") == INSTANCE_ID
                and (not require_build or payload.get("build_id") == BUILD_ID)
            )
    except Exception:
        return False


def _pick_port() -> int:
    base = PORT
    if os.environ.get("CYPRA_PORT"):
        return base
    if _health_ok(HOST, base, require_build=True):
        _log(f"Reusing matching CypraMatrixStudio server on {base}")
        return base
    if not _health_ok(HOST, base, require_build=False):
        _log(f"Port {base} is not serving CypraMatrixStudio")
    else:
        _log(f"Port {base} has an older CypraMatrixStudio build; will not reuse it")
    if _port_free(HOST, base):
        return base
    for p in range(base + 1, base + 40):
        if _port_free(HOST, p):
            _log(f"Port {base} busy — using {p}")
            return p
    return base


def _wait_for_server(host: str, port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _health_ok(host, port):
            return True
        time.sleep(0.25)
    return False


def _run_server(host: str, port: int) -> None:
    try:
        import logging

        logging.disable(logging.WARNING)
        import uvicorn

        config = uvicorn.Config(
            "server:app",
            host=host,
            port=port,
            log_level="error",
            access_log=False,
            reload=False,
            use_colors=False,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        _log(f"Uvicorn thread running {host}:{port}")
        server.run()
        _log("Uvicorn thread exit")
    except Exception:
        _log("Server thread crashed:\n" + traceback.format_exc())


def _python_console_exe() -> str:
    exe = sys.executable
    low = exe.lower()
    if low.endswith("pythonw.exe"):
        cand = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.isfile(cand):
            return cand
    return exe


def _start_server(host: str, port: int) -> tuple[bool, subprocess.Popen | None]:
    if _health_ok(host, port):
        _log("Server already healthy")
        return True, None
    # Own process so a WebView crash does not take the API down.
    exe = _python_console_exe()
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    cmd = [
        exe,
        "-m",
        "uvicorn",
        "server:app",
        "--host",
        host,
        "--port",
        str(int(port)),
        "--log-level",
        "error",
    ]
    try:
        server_log_path = DATA / "server.log"
        server_log = open(server_log_path, "a", encoding="utf-8", buffering=1)
        process = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=server_log,
            stderr=server_log,
            creationflags=flags,
            close_fds=True,
        )
        _log(f"Server stdout/stderr -> {server_log_path}")
        _log(f"Server process spawned {exe} -m uvicorn server:app --host {host} --port {port}")
    except Exception:
        _log("Server process spawn failed — thread fallback:\n" + traceback.format_exc())
        t = threading.Thread(
            target=_run_server,
            args=(host, port),
            name="studio-uvicorn",
            daemon=True,
        )
        t.start()
        process = None
    _log("Waiting for /api/health …")
    ok = _wait_for_server(host, port)
    _log(f"Health wait result: {ok}")
    if not ok:
        server_log_path = DATA / "server.log"
        if server_log_path.exists():
            try:
                tail = server_log_path.read_text(encoding="utf-8", errors="replace")[-6000:]
                _log("Server log tail:\n" + tail)
            except Exception:
                pass
    return ok, process


def _stop_owned_server(process: subprocess.Popen | None) -> None:
    """Stop only the uvicorn child process created by this launcher."""
    if process is None or process.poll() is not None:
        return
    _log(f"Stopping owned server process pid={process.pid}")
    try:
        process.terminate()
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        _log(f"Owned server pid={process.pid} did not exit; forcing it closed")
        process.kill()
        try:
            process.wait(timeout=3)
        except Exception:
            pass
    except Exception:
        _log("Owned server shutdown failed:\n" + traceback.format_exc())


def _open_desktop_window(url: str) -> bool:
    try:
        import webview
    except ImportError:
        _log("pywebview missing — pip install pywebview")
        return False

    width = int(SETTINGS.get("window_width") or 1440)
    height = int(SETTINGS.get("window_height") or 900)

    try:
        window = webview.create_window(
            "CypraMatrixStudio",
            url,
            width=width,
            height=height,
            min_size=(960, 640),
            background_color="#050505",
            text_select=True,
            confirm_close=False,
        )
    except Exception:
        _log("create_window failed:\n" + traceback.format_exc())
        return False

    # Expose a single file-picker function. Do NOT pass js_api= into
    # create_window — that walks WinForms window.native and freezes the UI.
    def pick_review_file() -> dict:
        try:
            import webview as _wv
            chosen = window.create_file_dialog(
                _wv.OPEN_DIALOG,
                allow_multiple=False,
                file_types=(
                    "Review files (*.txt;*.md;*.json;*.py;*.js;*.ts;*.html;*.css;*.xml;*.yaml;*.yml;*.csv;*.log;*.pdf;*.docx;*.xlsx)",
                    "All files (*.*)",
                ),
            )
        except Exception as e:
            _log(f"Native file dialog failed: {e}")
            return {"ok": False, "error": str(e)}
        if not chosen:
            return {"ok": False, "cancelled": True}
        path = Path(str(chosen[0]))
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        return {"ok": True, "path": str(path), "name": path.name, "size": int(size)}

    def listen_for_speech() -> dict:
        """Use Windows' installed speech recognizer without an API key."""
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$r = New-Object System.Speech.Recognition.SpeechRecognitionEngine; "
            "$r.SetInputToDefaultAudioDevice(); "
            "$r.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar)); "
            "$result = $r.Recognize([TimeSpan]::FromSeconds(12)); "
            "if ($null -ne $result) { [Console]::OutputEncoding = [Text.Encoding]::UTF8; $result.Text }; "
            "$r.Dispose()"
        )
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=16,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            text = (completed.stdout or "").strip()
            if completed.returncode != 0:
                return {"ok": False, "error": (completed.stderr or "Windows speech recognition failed").strip()}
            return {"ok": bool(text), "text": text}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "No speech detected before the listening timeout."}
        except Exception as e:
            _log(f"Native speech recognition failed: {e}")
            return {"ok": False, "error": str(e)}

    try:
        window.expose(pick_review_file, listen_for_speech)
        _log("Exposed pick_review_file and listen_for_speech")
    except Exception:
        _log("window.expose failed:\n" + traceback.format_exc())

    def _tray_thread() -> None:
        """Optional system tray (CYPRA_TRAY=1; legacy BRAIN_TRAY is accepted)."""
        if not USE_TRAY:
            return
        try:
            import pystray
            from PIL import Image, ImageDraw

            img = Image.new("RGBA", (64, 64), (0, 26, 77, 255))
            d = ImageDraw.Draw(img)
            d.ellipse((12, 12, 52, 52), fill=(103, 232, 249, 255))
            d.ellipse((24, 24, 40, 40), fill=(255, 255, 255, 255))

            def show(_icon=None, _item=None):
                try:
                    window.show()
                    window.restore()
                except Exception:
                    pass

            def quit_app(icon, _item=None):
                icon.stop()
                try:
                    window.destroy()
                except Exception:
                    pass

            menu = pystray.Menu(
                pystray.MenuItem("Show Studio", show, default=True),
                pystray.MenuItem("Quit", quit_app),
            )
            icon = pystray.Icon("cypra-studio", img, "CypraMatrixStudio", menu)
            _log("Tray icon started")
            icon.run()
        except Exception:
            _log("Tray unavailable (pip install pystray):\n" + traceback.format_exc())

    if USE_TRAY:
        threading.Thread(target=_tray_thread, name="studio-tray", daemon=True).start()

    # Try Edge WebView2, then default
    attempts = [
        {"gui": "edgechromium", "debug": False, "private_mode": False},
        {"gui": "edgechromium", "debug": False},
        {"debug": False, "private_mode": False},
        {"debug": False},
    ]
    if ICON.exists():
        for a in attempts:
            a["icon"] = str(ICON)

    for i, kw in enumerate(attempts):
        try:
            _log(f"webview.start attempt {i + 1}: {list(kw.keys())}")
            webview.start(**kw)
            _log("Window closed by user")
            return True
        except TypeError as e:
            _log(f"attempt {i + 1} TypeError: {e}")
            kw.pop("icon", None)
            try:
                webview.start(**kw)
                _log("Window closed by user (no icon)")
                return True
            except Exception:
                _log("retry failed:\n" + traceback.format_exc())
        except Exception:
            _log(f"attempt {i + 1} failed:\n" + traceback.format_exc())
    return False


def _open_browser(url: str) -> None:
    _log(f"Browser fallback → {url}")
    try:
        webbrowser.open(url)
    except Exception:
        _log("webbrowser failed:\n" + traceback.format_exc())
        _msgbox(f"Open this URL manually:\n{url}")
        return
    # Keep server alive
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def _already_running() -> bool:
    """True if another CypraMatrixStudio process holds the single-instance lock."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # Include the build so an older still-running desktop window cannot
        # hijack a newly launched UI after files have been updated in place.
        mutex_name = (
            "Local\\CypraLocalBVChat."
            + INSTANCE_ID.replace("-", "_")
            + "."
            + BUILD_ID.replace("-", "_")
        )
        handle = kernel32.CreateMutexW(None, False, mutex_name)
        last = kernel32.GetLastError()
        # Keep the handle alive for this process lifetime
        global _INSTANCE_MUTEX
        _INSTANCE_MUTEX = handle
        return last == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return False


_INSTANCE_MUTEX = None


def main() -> None:
    _log("=== CypraMatrixStudio launch ===")
    _log(f"Python {sys.version.split()[0]}  exe={sys.executable}")
    _log(f"ROOT {ROOT}")

    port = _pick_port()
    host = HOST
    url = f"http://{host}:{port}"
    _log(f"URL {url}")

    if _already_running():
        _log("Another instance holds the lock — waiting for /api/health")
        if _wait_for_server(host, port, timeout=12.0) or _health_ok(host, port):
            if FORCE_BROWSER:
                _log("Already running — browser attach")
                try:
                    webbrowser.open(url)
                except Exception:
                    _log("webbrowser failed:\n" + traceback.format_exc())
                return
            _log("Already running — opening desktop window on existing server")
            if _open_desktop_window(url):
                return
            _log("Desktop attach failed")
            return
        _log("Lock held but health never came up — starting anyway")

    ok, owned_server = _start_server(host, port)
    if not ok:
        _log("ERROR: server failed to start")
        _msgbox(
            "Could not start the local server.\n\n"
            f"Port: {port}\nLog:\n{LOG_PATH}"
        )
        sys.exit(1)

    atexit.register(_stop_owned_server, owned_server)
    _log("Server ready")
    print(f"[+] Cypra server healthy: {url}", flush=True)

    if os.environ.get("CYPRA_STARTUP_CHECK") == "1":
        hold = max(0.0, min(60.0, float(os.environ.get("CYPRA_STARTUP_CHECK_SECONDS", "0"))))
        if hold:
            time.sleep(hold)
        return

    if FORCE_BROWSER:
        _open_browser(url)
        return

    if not _open_desktop_window(url):
        _log("Desktop window failed — browser fallback")
        _msgbox(
            "Desktop window failed. Opening in your browser instead.\n\n"
            f"Log:\n{LOG_PATH}",
            error=False,
        )
        _open_browser(url)
        return

    _log("Exit clean · local Ollama left running (Kill localhost to stop it)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _log("Fatal:\n" + traceback.format_exc())
        _msgbox(f"CypraMatrixStudio crashed.\n\nLog:\n{LOG_PATH}")
        sys.exit(1)
