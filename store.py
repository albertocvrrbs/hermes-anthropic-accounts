"""Shared state for anthropic-cuentas (read by the agent hooks and the dashboard backend).

Lives in ``$HERMES_HOME/plugin-data/anthropic-cuentas/`` (file names are kept from v0.1 so
existing installs keep their state):

- ``uso.json``      usage snapshot per pool entry (email, 5-hour / weekly windows). Written by
                    the panel and the agent whenever they ask Anthropic.
- ``orden.json``    the order the user chose (for new chats) and the entries they removed.
- ``alias.json``    short display name per account (key = email).
- ``sesiones.json`` account assigned to each chat: {entry_id, model, at, manual, parent, reason}.
                    This is the source of truth for "which account does this chat use": each
                    chat keeps its own until it runs out; then it moves to the account with the
                    most usage left, and that one is saved for it.

Everything is written with tmp + os.replace: several processes read these files.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PROVIDER = "anthropic"
SNAPSHOT_MAX_AGE = 20 * 60      # s: an older snapshot cannot declare an account exhausted
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_LOCK = threading.Lock()
_RECENT: Dict[str, tuple] = {}  # entry_id -> (t, info): this process's lookups, errors included
RECENT_TTL = 45

# Stable codes for why a chat changed account (stored in sesiones.json; the UI translates them).
# Older installs stored Spanish sentences there; the UI still understands those.
REASON_MANUAL = "manual"            # picked by hand for this chat
REASON_EXHAUSTED = "exhausted"      # the chat's account ran out at turn start
REASON_GONE = "gone"                # the chat's account is no longer in Hermes
REASON_MIDTASK = "midtask"          # the previous account ran out mid-task
REASON_TEXT = {
    REASON_MANUAL: "picked by hand",
    REASON_EXHAUSTED: "this chat's account ran out",
    REASON_GONE: "this chat's account is no longer in Hermes",
    REASON_MIDTASK: "the previous account ran out mid-task",
}

# Stable codes for a failed usage lookup (``error_code``); ``error`` is the English text.
ERR_NO_TOKEN = "no_token"
ERR_EXPIRED = "expired"
ERR_HTTP = "http"
ERR_OFFLINE = "offline"
ERR_STALE = "stale"


def home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        val = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(val) if val else Path.home() / ".hermes"


def data_dir() -> Path:
    d = home() / "plugin-data" / "anthropic-cuentas"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read(name: str, default: Any) -> Any:
    try:
        return json.loads((data_dir() / name).read_text())
    except Exception:
        return default


def write(name: str, data: Any) -> None:
    path = data_dir() / name
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)


# ---------------------------------------------------------------- usage windows


def _iso_to_epoch(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value) / (1000.0 if value > 1e12 else 1.0)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _used(win: Any) -> Optional[float]:
    if not isinstance(win, dict) or win.get("used_pct") is None:
        return None
    try:
        return float(win["used_pct"])
    except (TypeError, ValueError):
        return None


def window_blocked_until(win: Any, now: Optional[float] = None) -> Optional[float]:
    """Epoch until which a window at 100 % blocks the account, or None if it has room."""
    used = _used(win)
    if used is None or used < 99.5:
        return None
    reset = _iso_to_epoch(win.get("resets_at"))
    now = time.time() if now is None else now
    if reset is None:
        return now + 15 * 60  # at the cap with no reset time: treat it as busy for a while
    return reset if reset > now else None


def _model_windows(info: Dict[str, Any], model: Optional[str]) -> list:
    wins = [info.get("five_hour"), info.get("seven_day")]
    m = (model or "").lower()
    if "opus" in m:
        wins.append(info.get("seven_day_opus"))
    if "sonnet" in m:
        wins.append(info.get("seven_day_sonnet"))
    return wins


def account_blocked_until(info: Dict[str, Any], model: Optional[str] = None,
                          now: Optional[float] = None) -> Optional[float]:
    """Until when the snapshot says this account cannot serve *model* (None = it can)."""
    until = [u for u in (window_blocked_until(w, now) for w in _model_windows(info, model)) if u]
    return max(until) if until else None


def remaining_pct(info: Optional[Dict[str, Any]], model: Optional[str] = None) -> Optional[float]:
    """% left = 100 - the fullest window that applies to the model. None without data."""
    if not info or info.get("error"):
        return None
    now = time.time()
    vals = []
    for w in _model_windows(info, model):
        u = _used(w)
        if u is None:
            continue
        reset = _iso_to_epoch((w or {}).get("resets_at"))
        vals.append(0.0 if (reset and reset <= now) else u)  # window already renewed = empty
    if not vals:
        return None
    return max(0.0, 100.0 - max(vals))


# ---------------------------------------------------------------- usage snapshot


def snapshot() -> Dict[str, Any]:
    data = read("uso.json", {})
    return data if isinstance(data, dict) else {}


def save_snapshot(entries: Dict[str, Dict[str, Any]]) -> None:
    with _LOCK:
        prev = snapshot().get("entries") or {}
        merged = dict(prev)
        for eid, info in entries.items():
            old = prev.get(eid) or {}
            if info.get("error") and old:
                # a network error must not lose the email or the last good data
                merged[eid] = {**old, "last_error": info.get("error")}
            else:
                merged[eid] = info
        write("uso.json", {"at": time.time(), "entries": merged})


def email_of(entry_id: Optional[str], snap: Optional[Dict[str, Any]] = None) -> Optional[str]:
    if not entry_id:
        return None
    snap = snapshot() if snap is None else snap
    info = (snap.get("entries") or {}).get(entry_id) or {}
    return info.get("email") or None


def blocked_by_snapshot(entry_id: str, model: Optional[str], snap: Optional[Dict[str, Any]] = None,
                        now: Optional[float] = None) -> Optional[float]:
    """Block according to the snapshot, counting duplicate entries (same email = same quota)."""
    snap = snapshot() if snap is None else snap
    now = time.time() if now is None else now
    entries = snap.get("entries") or {}
    info = entries.get(entry_id)
    if not info:
        return None
    email = info.get("email")
    best = None
    for eid, other in entries.items():
        if eid != entry_id and (not email or other.get("email") != email):
            continue
        if now - float(other.get("fetched_at") or 0) > SNAPSHOT_MAX_AGE:
            continue
        until = account_blocked_until(other, model, now)
        if until and (best is None or until > best):
            best = until
    return best


def _http_json(url: str, token: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01",
        "User-Agent": "claude-code/2.1.280",
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _window(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict) or raw.get("utilization") is None:
        return None
    return {"used_pct": float(raw["utilization"]), "resets_at": raw.get("resets_at")}


def fetch_info(token: Optional[str]) -> Dict[str, Any]:
    """Profile + usage of one account (2 calls). Never raises; errors go in ``error``/``error_code``."""
    info: Dict[str, Any] = {"email": None, "plan": None, "five_hour": None, "seven_day": None,
                            "seven_day_opus": None, "seven_day_sonnet": None, "error": None,
                            "error_code": None, "fetched_at": time.time()}
    if not token:
        info["error"], info["error_code"] = "No token", ERR_NO_TOKEN
        return info
    try:
        acct = (_http_json(PROFILE_URL, token).get("account") or {})
        info["email"] = acct.get("email")
        info["plan"] = "max" if acct.get("has_claude_max") else ("pro" if acct.get("has_claude_pro") else None)
        use = _http_json(USAGE_URL, token)
        for key in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
            info[key] = _window(use.get(key))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            info["error"], info["error_code"] = "The session expired: reconnect it", ERR_EXPIRED
        else:
            info["error"], info["error_code"] = f"Anthropic answered {exc.code}", ERR_HTTP
        info["http_status"] = exc.code
    except Exception as exc:
        info["error"], info["error_code"] = f"No connection ({type(exc).__name__})", ERR_OFFLINE
    return info


def claude_code_token() -> Optional[str]:
    try:
        data = json.loads((Path.home() / ".claude" / ".credentials.json").read_text())
        return (data.get("claudeAiOauth") or {}).get("accessToken")
    except Exception:
        return None


def entry_token(pool, entry, refresh: bool = True) -> Optional[str]:
    """The entry's current token; if it already expired and *refresh*, let the pool renew it first."""
    exp = getattr(entry, "expires_at_ms", None)
    if refresh and pool is not None and isinstance(exp, (int, float)) and exp < (time.time() + 60) * 1000:
        try:
            refreshed = pool._refresh_entry(entry, force=False)
            if refreshed is not None:
                entry = refreshed
        except Exception:
            pass
    tok = getattr(entry, "runtime_api_key", None) or getattr(entry, "access_token", None)
    if not tok and getattr(entry, "source", "") == "claude_code":
        tok = claude_code_token()
    return tok or None


def infos_for(pool, entries: Iterable, max_age: float = 60.0, fetch: bool = True) -> Dict[str, Dict[str, Any]]:
    """Usage per entry: from the snapshot when newer than *max_age*; otherwise ask Anthropic in parallel."""
    entries = list(entries)
    now = time.time()
    snap_entries = snapshot().get("entries") or {}
    out: Dict[str, Dict[str, Any]] = {}
    need = []
    for e in entries:
        recent = _RECENT.get(e.id)
        if recent and now - recent[0] < RECENT_TTL:
            out[e.id] = recent[1]
            continue
        info = snap_entries.get(e.id)
        if info and not info.get("error") and now - float(info.get("fetched_at") or 0) < max_age:
            out[e.id] = info
        else:
            need.append(e)
    if need and fetch:
        with ThreadPoolExecutor(max_workers=min(6, len(need))) as ex:
            got = list(ex.map(lambda e: fetch_info(entry_token(pool, e)), need))
        fresh = {}
        for e, info in zip(need, got):
            _RECENT[e.id] = (time.time(), info)
            fresh[e.id] = info
            out[e.id] = info
        try:
            save_snapshot(fresh)
        except Exception:
            pass
    for e in need:
        if e.id not in out and e.id in snap_entries:
            out[e.id] = {**snap_entries[e.id], "error": "stale data", "error_code": ERR_STALE}
    # the email almost never changes: if the lookup failed, use the snapshot's to spot duplicates
    for e in entries:
        if e.id in out and not out[e.id].get("email") and (snap_entries.get(e.id) or {}).get("email"):
            out[e.id] = {**out[e.id], "email": snap_entries[e.id]["email"]}
    return out


# ---------------------------------------------------------------- order and removed entries


def desired_order() -> Dict[str, Any]:
    data = read("orden.json", {})
    if not isinstance(data, dict):
        data = {}
    return {"ids": list(data.get("ids") or []), "removed": list(data.get("removed") or []),
            "seq": data.get("seq"), "at": data.get("at")}


def save_order(ids: list, removed: Optional[list] = None) -> Dict[str, Any]:
    with _LOCK:
        cur = desired_order()
        data = {"ids": list(ids), "removed": list(removed if removed is not None else cur["removed"])[-50:],
                "seq": time.time_ns(), "at": time.time()}
        write("orden.json", data)
        return data


def enforce_order(pool) -> bool:
    """Apply the chosen order to the pool and drop the entries the user removed. True if anything changed."""
    want = desired_order()
    changed = False
    removed = set(want["removed"])
    if removed:
        for idx, entry in reversed(list(enumerate(pool.entries(), start=1))):
            if entry.id in removed:
                pool.remove_index(idx)
                changed = True
    ids = [i for i in want["ids"] if i not in removed]
    if not ids:
        return changed
    current = [e.id for e in sorted(pool.entries(), key=lambda e: e.priority)]
    known = [i for i in ids if i in current]
    # entries missing from the saved order (newly added) go last, in their current order
    target = known + [i for i in current if i not in known]
    if target != current:
        for pos, eid in enumerate(target):
            pool.move_entry(eid, pos)
        changed = True
    return changed


# ---------------------------------------------------------------- aliases


def aliases() -> Dict[str, str]:
    data = read("alias.json", {})
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def set_alias(email: str, alias: Optional[str]) -> Dict[str, str]:
    with _LOCK:
        data = aliases()
        alias = (alias or "").strip()[:24]
        if alias:
            data[email] = alias
        else:
            data.pop(email, None)
        write("alias.json", data)
        return data


# ---------------------------------------------------------------- each chat's account


def sessions() -> Dict[str, Any]:
    data = read("sesiones.json", {})
    return data if isinstance(data, dict) else {}


def session_record(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not session_id:
        return None
    rec = sessions().get(session_id)
    return rec if isinstance(rec, dict) else None


def record_session(session_id: Optional[str], entry_id: Optional[str], model: Optional[str] = None,
                   *, manual: bool = False, parent: Optional[str] = None, reason: Optional[str] = None) -> None:
    """Save the chat's account. Only writes when something changed (it runs on every turn)."""
    if not session_id or not entry_id:
        return
    with _LOCK:
        data = sessions()
        raw = data.get(session_id)
        old: Dict[str, Any] = raw if isinstance(raw, dict) else {}
        same_entry = old.get("entry_id") == entry_id
        if (same_entry and old.get("model") == model and not manual and not reason
                and time.time() - float(old.get("at") or 0) < 600):
            return
        rec = {"entry_id": entry_id, "model": model, "at": time.time(),
               # a hand-picked choice lasts as long as the chat stays on that account
               "manual": bool(manual or (same_entry and old.get("manual")))}
        if parent or old.get("parent"):
            rec["parent"] = parent or old.get("parent")
        if reason:
            rec["reason"] = reason
        elif same_entry and old.get("reason"):
            rec["reason"] = old["reason"]
        data[session_id] = rec
        if len(data) > 400:
            for sid, _ in sorted(data.items(), key=lambda kv: (kv[1] or {}).get("at") or 0)[: len(data) - 400]:
                data.pop(sid, None)
        write("sesiones.json", data)


def chats_by_entry(max_age: float = 6 * 3600) -> Dict[str, int]:
    """How many chats (subagents excluded) used each entry in the last *max_age* seconds."""
    now = time.time()
    out: Dict[str, int] = {}
    for rec in sessions().values():
        if not isinstance(rec, dict) or rec.get("parent") or now - float(rec.get("at") or 0) > max_age:
            continue
        eid = rec.get("entry_id")
        if eid:
            out[eid] = out.get(eid, 0) + 1
    return out


# ---------------------------------------------------------------- picking an account


def entry_usable(entry, model: Optional[str], now: Optional[float] = None) -> bool:
    """What Hermes knows about the entry: neither dead nor paused for *model*."""
    from agent.credential_pool_model_cooldowns import model_cooldown_until
    now = time.time() if now is None else now
    status = getattr(entry, "last_status", None)
    if status == "dead":
        return False
    if model_cooldown_until(entry, model) is not None:
        return False
    if status == "exhausted":
        reset = getattr(entry, "last_error_reset_at", None)
        try:
            if reset and float(reset) > now:
                return False
        except (TypeError, ValueError):
            pass
    return True


def rank_entries(pool, model: Optional[str], mode: str = "remaining",
                 exclude_emails: Iterable[Optional[str]] = (), infos: Optional[Dict[str, Dict[str, Any]]] = None,
                 exclude_ids: Iterable[str] = ()) -> List[Any]:
    """Candidates in the order worth trying them.

    - ``remaining``: the one with the MOST usage left first (ties: the user's order).
    - ``order``: the user's order, skipping exhausted ones.
    One entry per account (duplicates share the limit); if one entry of an account is paused,
    the whole account counts as paused. Entries with unknown usage go after the known ones.
    """
    infos = infos or {}
    snap = snapshot()
    now = time.time()
    excl_emails = {e for e in exclude_emails if e}
    excl_ids = set(exclude_ids)

    def email(e):
        return (infos.get(e.id) or {}).get("email") or email_of(e.id, snap)

    ordered = sorted(pool.entries(), key=lambda x: x.priority)
    blocked = set()
    for e in ordered:  # first: which accounts are out (paused or capped) through ANY of their entries
        em = email(e)
        if not em:
            continue
        info = infos.get(e.id)
        rem = remaining_pct(info, model) if info else None
        if not entry_usable(e, model, now) or (rem is not None and rem <= 0.5) \
                or (rem is None and blocked_by_snapshot(e.id, model, snap, now)):
            blocked.add(em)
    seen = set()
    cands = []
    for pos, e in enumerate(ordered):
        em = email(e)
        if e.id in excl_ids or (em and (em in excl_emails or em in blocked or em in seen)):
            continue
        if not entry_usable(e, model, now):
            continue
        info = infos.get(e.id)
        rem = remaining_pct(info, model) if info else None
        if rem is not None and rem <= 0.5:
            continue
        if em:
            seen.add(em)
        cands.append((pos, e, rem))
    if mode == "remaining":
        known = sorted((c for c in cands if c[2] is not None), key=lambda c: (-c[2], c[0]))
        unknown = [c for c in cands if c[2] is None]
        return [c[1] for c in known + unknown]
    return [c[1] for c in cands]
