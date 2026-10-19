"""Central bot entrypoint: control bot (buttons) + one active user session."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from bot import build_app
from config import load_config, project_root_from_here
from session_manager import SessionManager


def _pid_alive(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return pid == os.getpid()
    if os.name == "nt":
        # os.kill(pid, 0) does NOT work on Windows (always WinError 87).
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not h:
                return False
            kernel32.CloseHandle(h)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def single_instance_guard() -> bool:
    """Atomic single-instance lock via a lock DIRECTORY (mkdir is atomic).

    Only one process can create it. Stale locks (dead pid) are cleared.
    """
    import shutil
    import time
    root = project_root_from_here()
    lockdir = root / "logs" / "bot.lock"
    lockdir.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(20):  # ~10s grace for a just-starting owner
        try:
            lockdir.mkdir(exist_ok=False)
            (lockdir / "pid").write_text(str(os.getpid()), encoding="ascii")
            try:
                (root / "logs" / "bot.pid").write_text(str(os.getpid()),
                                                       encoding="ascii")
            except Exception:
                pass
            return True
        except FileExistsError:
            pass
        try:
            old = int((lockdir / "pid").read_text(encoding="ascii").strip().split()[0])
        except Exception:
            old = 0
        if old and _pid_alive(old):
            if old == os.getpid():
                return True
            print(f"Another instance is already running (PID {old}). "
                  "Stop it first — two copies fight over Telegram updates.",
                  file=sys.stderr)
            return False
        if old and not _pid_alive(old):
            # stale lock: clear and retry
            try:
                shutil.rmtree(lockdir, ignore_errors=True)
            except Exception:
                pass
            time.sleep(0.5)
            continue
        if old and not _pid_alive(old):
            # stale lock: clear and retry
            try:
                shutil.rmtree(lockdir, ignore_errors=True)
            except Exception:
                pass
            time.sleep(0.5)
            continue
        # lock exists but owner hasn't written its pid yet: wait briefly,
        # then treat as orphaned and clear (a dead lock must not block forever)
        if not old:
            time.sleep(0.5)
            # re-read once more
            try:
                old = int((lockdir / "pid").read_text(encoding="ascii").strip().split()[0])
            except Exception:
                old = 0
            if not old:
                try:
                    shutil.rmtree(lockdir, ignore_errors=True)
                except Exception:
                    pass
                time.sleep(0.5)
                continue
            time.sleep(0.5)
            continue
    print("Could not acquire instance lock.", file=sys.stderr)
    return False


def main() -> int:
    if not single_instance_guard():
        return 3
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    try:
        cfg = load_config()
    except ValueError as e:
        print(f"Config error:\n{e}", file=sys.stderr)
        return 2
    logging.getLogger().setLevel(getattr(logging, cfg.log_level, logging.INFO))
    sm = SessionManager(cfg.api_id, cfg.api_hash, cfg.sessions_dir, cfg.db_path)
    app = build_app(cfg, sm)
    print(f"Central bot starting. OWNER_ID={cfg.owner_id} DB={cfg.db_path}",
          file=sys.stderr, flush=True)

    async def _watch_keeper(application):
        w = application.bot_data.get("watcher")
        sm = application.bot_data["sm"]
        while True:
            await asyncio.sleep(60)
            try:
                if w and w.is_enabled() and sm.active_client is not w.client:
                    await w.restart()
            except Exception:
                pass

    async def _notify_owner(application):
        try:
            await application.bot.send_message(
                cfg.owner_id,
                "✅ البوت المركزي شغال.\n"
                f"OWNER_ID={cfg.owner_id}\n"
                "إذا لم يصلك هذا، تحقق من OWNER_ID والتوكن وأعد التشغيل.")
        except Exception as e:
            print(f"notify owner failed: {e}", file=sys.stderr, flush=True)
        w = application.bot_data.get("watcher")
        if w and w.is_enabled():
            application.create_task(w.start())
        application.create_task(_watch_keeper(application))

    app.post_init = _notify_owner
    print(f"OWNER_ID={cfg.owner_id} BOT_TOKEN_SET=yes DB={cfg.db_path}",
          file=sys.stderr, flush=True)
    print("Central bot running. Open your control bot and send /start "
          "(owner only). Ctrl+C to stop.", flush=True)
    app.run_polling(allowed_updates=["message", "callback_query"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
