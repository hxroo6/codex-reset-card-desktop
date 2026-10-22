"""Per-card postponement. No HTTP requests or reset-credit consumption commands."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import ctypes
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from panel_logic import default_binary, parse_time, utcnow
from app_config import MIN_EXPIRY_RESERVE_SECONDS
from upstream_compatibility import inspect_upstream


def _paths():
    root = Path(os.environ["APPDATA"]) / "codex-reset-card"
    lock_path = Path(os.environ["LOCALAPPDATA"]) / "codex-reset-card" / "daemon.lock"
    return root / "state.json", root / "config.json", lock_path


def _read(path):
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("不支持的本地调度文件版本")
    return value


def _effective(card, lead):
    expiry = parse_time(card.get("expiresAt"))
    retry = parse_time(card.get("nextAttemptAt"))
    if expiry is None or (card.get("nextAttemptAt") is not None and retry is None):
        raise ValueError("卡片计划时间无效")
    base = expiry - timedelta(seconds=lead)
    return expiry, max(base, retry) if retry is not None else base


def postpone_document(state, cfg, fingerprint, expected_trigger, now=None):
    """Validate against the latest document and return a copy; never mutate inputs."""
    now = now or utcnow()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("当前时间必须包含时区")
    if type(state.get("version")) is not int or type(cfg.get("version")) is not int or state["version"] != 1 or cfg["version"] != 1:
        raise ValueError("不支持的调度配置版本")
    if cfg.get("armed") is not True:
        raise ValueError("自动使用未授权，不能推迟")
    lead = cfg.get("leadTimeSeconds")
    if not isinstance(lead, int) or isinstance(lead, bool) or not 10 <= lead <= 3600:
        raise ValueError("提前量配置无效")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{10}", fingerprint) is None:
        raise ValueError("卡片指纹无效")
    cards = state.get("cards")
    if not isinstance(cards, list):
        raise ValueError("没有可调整的卡片计划")
    matching = [card for card in cards if isinstance(card, dict)
                and isinstance(card.get("fingerprint"), str) and card["fingerprint"].startswith(fingerprint)]
    if len(matching) != 1 or re.fullmatch(r"[0-9a-f]{64}", matching[0]["fingerprint"]) is None:
        raise ValueError("卡片不存在或短指纹不唯一，请刷新后重试")
    target = matching[0]
    expiry, trigger = _effective(target, lead)
    if expiry <= now or trigger <= now + timedelta(seconds=30):
        raise ValueError("卡片已过期或即将执行，不能推迟")
    if parse_time(expected_trigger) != trigger:
        raise ValueError("后台计划已改变，请刷新后重试")
    for card in cards:
        other_expiry, other_trigger = _effective(card, lead)
        if other_expiry <= now:
            continue
        if card.get("attemptKey") or card.get("lastOutcome"):
            raise ValueError("已有卡片进入执行或重试流程，不能修改计划")
        if other_trigger <= now + timedelta(seconds=30):
            raise ValueError("卡片即将执行或已进入执行窗口，不能推迟")
    new_trigger = trigger + timedelta(minutes=2)
    if new_trigger > expiry - timedelta(seconds=MIN_EXPIRY_RESERVE_SECONDS):
        raise ValueError("不能再推迟：最迟只能到有效期前 2 分钟")
    updated = copy.deepcopy(state)
    for card in updated["cards"]:
        if card["fingerprint"] == target["fingerprint"]:
            card["nextAttemptAt"] = new_trigger.isoformat()
            break
    return updated, {"fingerprint": fingerprint, "triggerAt": new_trigger.isoformat(),
                     "expiresAt": expiry.isoformat(), "delayedSeconds": 120}


def add_schedule_timing(status):
    """Expose only matched timing fields, never raw state or idempotency keys."""
    result = copy.deepcopy(status)
    result["timingReady"] = False
    try:
        state_path, _, _ = _paths()
        current = _read(state_path)
        raw_cards = current.get("cards", [])
        ready = True
        for card in result.get("cards", []):
            matches = [raw for raw in raw_cards if isinstance(raw, dict)
                       and raw.get("fingerprint", "").startswith(card.get("fingerprint", ""))
                       and parse_time(raw.get("expiresAt")) == parse_time(card.get("expiresAt"))]
            if len(matches) != 1:
                ready = False
                continue
            raw = matches[0]
            card["attemptInProgress"] = bool(raw.get("attemptKey"))
            if raw.get("nextAttemptAt"):
                if parse_time(raw["nextAttemptAt"]) is None:
                    ready = False
                else:
                    card["nextAttemptAt"] = raw["nextAttemptAt"]
        result["timingReady"] = ready
    except (OSError, ValueError, KeyError, TypeError):
        result["timingReadError"] = "本地执行计划读取失败，延期时间待确认"
    return result


def _cli(*arguments):
    allowed = (("status", "--json"), ("service", "stop"), ("service", "start"))
    if arguments not in allowed:
        raise ValueError("不支持的后台控制命令")
    process = subprocess.run([str(default_binary()), *arguments], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=12,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if process.returncode:
        # Do not copy untrusted CLI response bodies into the UI or history.
        raise RuntimeError("后台控制命令失败：" + " ".join(arguments))
    if arguments == ("status", "--json"):
        return json.loads(process.stdout)
    return None


@contextmanager
def named_guard(name, timeout=8):
    if os.name != "nt":
        raise RuntimeError("延期控制仅支持 Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    end = time.monotonic() + timeout
    handle = None
    while time.monotonic() < end:
        ctypes.set_last_error(0)
        candidate = kernel.CreateMutexW(None, True, name)
        error = ctypes.get_last_error()
        if not candidate:
            raise RuntimeError("无法锁定后台调度状态")
        if error != 183:
            handle = candidate
            break
        kernel.CloseHandle(candidate)
        time.sleep(0.05)
    if handle is None:
        raise RuntimeError("后台仍在运行或另一个操作尚未完成，请稍后再试")
    try:
        yield
    finally:
        kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def _daemon_name():
    _, _, lock_path = _paths()
    digest = hashlib.sha256(str(lock_path).encode("utf-8")).hexdigest()[:24]
    return "Local\\CodexResetCard-" + digest


def daemon_guard():
    return named_guard(_daemon_name())


def _daemon_running():
    # Open an existing object only. Creating a probe mutex during /Run could
    # cause the real daemon's singleton acquisition to fail at startup.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_wchar_p]
    kernel.OpenMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenMutexW(0x00100000, False, _daemon_name())
    if not handle:
        return False
    kernel.CloseHandle(handle)
    return True


def atomic_write_state(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix="state-postpone-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _restart_and_confirm():
    _cli("service", "start")
    end = time.monotonic() + 8
    while time.monotonic() < end:
        if _daemon_running():
            return
        time.sleep(0.15)
    raise RuntimeError("后台任务未恢复运行")


def postpone_card(fingerprint, expected_trigger):
    """User-clicked action: stop, hold daemon mutex, edit one card, restart."""
    _require_compatible()
    with named_guard("Local\\CodexResetCardPanel-Postpone", timeout=0.5):
        service = _cli("status", "--json").get("service", {})
        if service.get("installed") is not True or service.get("running") is not True:
            raise RuntimeError("后台未运行，不能推迟计划")
        state_path, cfg_path, _ = _paths()
        # Validate before stopping anything, then revalidate under the daemon lock.
        postpone_document(_read(state_path), _read(cfg_path), fingerprint, expected_trigger)
        committed = False
        result, failure, restart_failure = None, None, None
        try:
            _cli("service", "stop")
            with daemon_guard():
                _require_compatible()
                updated, result = postpone_document(_read(state_path), _read(cfg_path), fingerprint, expected_trigger)
                atomic_write_state(state_path, updated)
                committed = True
        except Exception as exc:
            failure = exc
        finally:
            # Release the daemon lock before asking Task Scheduler to start it.
            try:
                _restart_and_confirm()
            except Exception as exc:
                restart_failure = exc
        if restart_failure is not None:
            message = "推迟计划已保存，但后台未恢复，请检查后台服务" if committed else "推迟未完成，且后台未恢复，请检查后台服务"
            raise RuntimeError(message) from restart_failure
        if failure is not None:
            raise RuntimeError(str(failure)) from failure
        return result


def _require_compatible():
    result = inspect_upstream(default_binary())
    if not result["supported"]:
        raise RuntimeError(result["message"])
