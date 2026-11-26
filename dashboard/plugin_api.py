"""anthropic-cuentas backend. Mounted at /api/plugins/anthropic-cuentas/.

GET  /accounts[?fresh=1][&session=<stored id>]  pool accounts with real usage (5 h and weekly),
                                                 alias, status and which account that session uses.
POST /order  {ids: [...]}        order for new chats (top one first).
POST /use    {entry_id}          moves it to the top; if Anthropic confirms it has quota, clears
                                 the estimated cooldown Hermes may have put on it.
POST /session {session_id, entry_id}  account for ONE chat (from its next message).
POST /remove {entry_id}          removes it from Hermes (backs up auth.json first).
POST /alias  {email, alias}      short name for an account (empty = remove it).

The order and the removed entries are also stored in plugin-data (store.py): the agent hook
re-applies them at the start of every turn in case a live process rewrote auth.json.
"""
from __future__ import annotations

import importlib.util
import logging
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel
except Exception:  # pragma: no cover - tests outside the dashboard
    class APIRouter:  # type: ignore
        def get(self, *_a, **_k):
            return lambda fn: fn

        def post(self, *_a, **_k):
            return lambda fn: fn

    class BaseModel:  # type: ignore
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class HTTPException(Exception):  # type: ignore
        def __init__(self, status_code: int, detail: str = "") -> None:
            self.status_code = status_code
            self.detail = detail

logger = logging.getLogger("anthropic-cuentas")
router = APIRouter()


def _load_store():
    name = "anthropic_cuentas_store"
    path = Path(__file__).resolve().parent.parent / "store.py"
    mod = sys.modules.get(name)
    if mod is not None and getattr(mod, "__file__", None) == str(path):
        return mod
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


store = _load_store()

PROVIDER = "anthropic"
CACHE_TTL = 60          # s: Anthropic is not asked again before this
FRESH_FLOOR = 15        # s: a manual "refresh" never goes below this
_CACHE: Dict[str, tuple] = {}
_CACHE_LOCK = threading.Lock()
_MUTATE_LOCK = threading.Lock()


def _fetch_account(pool, entry, fresh: bool) -> Dict[str, Any]:
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(entry.id)
    if hit and now - hit[0] < (FRESH_FLOOR if fresh else CACHE_TTL):
        return hit[1]
    info = store.fetch_info(store.entry_token(pool, entry))
    if info.get("error") and hit and (info.get("http_status") == 429 or not info.get("http_status")) \
            and info.get("error_code") != store.ERR_NO_TOKEN:
        stale = dict(hit[1])  # Anthropic overloaded or network down: keep the last good data
        stale["stale"] = True
        return stale
    with _CACHE_LOCK:
        _CACHE[entry.id] = (now, info)
    return info


def _cooldowns(entry) -> list:
    out = []
    now = time.time()
    for model, until in (getattr(entry, "model_cooldowns", None) or {}).items():
        try:
            if float(until) > now:
                out.append({"model": model, "until": float(until)})
        except Exception:
            continue
    return sorted(out, key=lambda c: c["until"])


def _default_model() -> Optional[str]:
    try:
        from hermes_cli.config import load_config_readonly
        model = (load_config_readonly() or {}).get("model") or {}
        if isinstance(model, dict):
            return model.get("default") or model.get("name")
        return str(model) or None
    except Exception:
        return None


def _status(info: Dict[str, Any], cooldowns: list, model: Optional[str], now: float) -> Dict[str, Any]:
    """Readable status: ok / limit (with until when) / paused / error.

    ``code`` is stable for the UI to translate; ``why`` is the English text.
    """
    if info.get("error") and not info.get("stale"):
        return {"state": "error", "until": None, "why": info["error"],
                "code": info.get("error_code") or "error"}
    until = store.account_blocked_until(info, model, now)
    if until:
        five = store.window_blocked_until(info.get("five_hour"), now)
        if five and five >= until - 1:
            return {"state": "limit", "until": until, "why": "The 5-hour limit ran out", "code": "limit_5h"}
        return {"state": "limit", "until": until, "why": "The weekly limit ran out", "code": "limit_week"}
    cd = [c for c in cooldowns if not model or c["model"] == model]
    if cd:
        return {"state": "paused", "until": cd[-1]["until"],
                "why": "Hermes paused it after a limit warning", "code": "paused"}
    return {"state": "ok", "until": None, "why": None, "code": None}


def _load_pool():
    from agent.credential_pool import load_pool
    pool = load_pool(PROVIDER)
    try:
        if store.enforce_order(pool):
            pool = load_pool(PROVIDER)
    except Exception:
        logger.debug("enforce_order failed", exc_info=True)
    return pool


def build_accounts(fresh: bool = False, session: Optional[str] = None) -> Dict[str, Any]:
    pool = _load_pool()
    entries = sorted(pool.entries(), key=lambda e: e.priority)
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(entries) or 1))) as ex:
        infos = list(ex.map(lambda e: _fetch_account(pool, e, fresh), entries))
    try:
        store.save_snapshot({e.id: i for e, i in zip(entries, infos) if not i.get("stale")})
    except Exception:
        logger.debug("could not save the usage snapshot", exc_info=True)

    sess = store.session_record(session)
    model = (sess or {}).get("model") or _default_model()
    info_by_id = {e.id: i for e, i in zip(entries, infos)}
    chats = store.chats_by_entry()
    aliases = store.aliases()
    now = time.time()
    seen: Dict[str, str] = {}
    accounts = []
    for entry, info in zip(entries, infos):
        email = info.get("email")
        dup_of = None
        if email:
            dup_of = seen.get(email)
            seen.setdefault(email, entry.id)
        cds = _cooldowns(entry)
        accounts.append({
            "id": entry.id,
            "label": entry.label,
            "source": getattr(entry, "source", ""),
            "priority": entry.priority,
            "email": email,
            "alias": aliases.get(email) if email else None,
            "plan": info.get("plan"),
            "five_hour": info.get("five_hour"),
            "seven_day": info.get("seven_day"),
            "seven_day_opus": info.get("seven_day_opus"),
            "seven_day_sonnet": info.get("seven_day_sonnet"),
            "remaining": store.remaining_pct(info, model) if not info.get("stale") else store.remaining_pct(
                {**info, "error": None}, model),
            "error": info.get("error"),
            "stale": bool(info.get("stale")),
            "cooldowns": cds,
            "duplicate_of": dup_of,
            "chats": chats.get(entry.id, 0),
            "status": _status(info, cds, model, now),
        })
    try:
        order = store.rank_entries(pool, model, "order", infos=info_by_id)
        best = store.rank_entries(pool, model, "remaining", infos=info_by_id,
                                  exclude_ids={sess["entry_id"]} if sess else ())
    except Exception:
        logger.debug("ranking failed", exc_info=True)
        order, best = [], []
    return {
        "accounts": accounts,
        "next_id": order[0].id if order else None,   # the one a new chat takes
        "best_id": best[0].id if best else None,     # where this chat goes if its own runs out
        "model": model,
        "session": ({"id": session, **sess} if sess else None),
        "now": now,
    }


@router.get("/accounts")
def get_accounts(fresh: int = 0, session: Optional[str] = None):
    try:
        return build_accounts(bool(fresh), session)
    except Exception as exc:
        logger.warning("accounts failed", exc_info=True)
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------ actions


class OrderBody(BaseModel):
    ids: List[str] = []


class EntryBody(BaseModel):
    entry_id: str = ""


class SessionBody(BaseModel):
    session_id: str = ""
    entry_id: str = ""


class AliasBody(BaseModel):
    email: str = ""
    alias: Optional[str] = None


def apply_order(ids: List[str]) -> Dict[str, Any]:
    with _MUTATE_LOCK:
        from agent.credential_pool import load_pool
        pool = load_pool(PROVIDER)
        current = [e.id for e in sorted(pool.entries(), key=lambda e: e.priority)]
        wanted = [i for i in ids if i in current]
        if not wanted:
            raise HTTPException(status_code=400, detail="empty order or unknown accounts")
        target = wanted + [i for i in current if i not in wanted]
        store.save_order(target)
        store.enforce_order(pool)
        return {"ids": target}


def apply_use(entry_id: str) -> Dict[str, Any]:
    with _MUTATE_LOCK:
        from agent.credential_pool import load_pool
        pool = load_pool(PROVIDER)
        entry = next((e for e in pool.entries() if e.id == entry_id), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="that account is no longer in Hermes")
        current = [e.id for e in sorted(pool.entries(), key=lambda e: e.priority)]
        target = [entry_id] + [i for i in current if i != entry_id]
        store.save_order(target)
        store.enforce_order(pool)
        # Only when Anthropic confirms it has quota is the pause Hermes estimated cleared.
        info = _fetch_account(pool, entry, True)
        lifted = False
        if not info.get("error") and not store.account_blocked_until(info, None):
            if entry.model_cooldowns or entry.last_status:
                pool.reset_status(entry_id)
                lifted = True
        return {"ids": target, "lifted_pause": lifted,
                "blocked_until": store.account_blocked_until(info, None) if not info.get("error") else None}


def apply_session(session_id: str, entry_id: str) -> Dict[str, Any]:
    """Account for ONE chat. Applies from its next message; other chats are untouched."""
    if not session_id:
        raise HTTPException(status_code=400, detail="there is no open chat to assign it to")
    with _MUTATE_LOCK:
        from agent.credential_pool import load_pool
        pool = load_pool(PROVIDER)
        entry = next((e for e in pool.entries() if e.id == entry_id), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="that account is no longer in Hermes")
        old = store.session_record(session_id) or {}
        model = old.get("model") or _default_model()
        info = _fetch_account(pool, entry, True)
        blocked = store.account_blocked_until(info, model) if not info.get("error") else None
        lifted = False
        if not info.get("error") and not blocked and (entry.model_cooldowns or entry.last_status):
            pool.reset_status(entry_id)  # Anthropic confirms it has usage: drop the estimated pause
            lifted = True
        store.record_session(session_id, entry_id, model, manual=True, reason=store.REASON_MANUAL)
        return {"session_id": session_id, "entry_id": entry_id, "blocked_until": blocked, "lifted_pause": lifted}


def _backup_auth() -> str:
    src = store.home() / "auth.json"
    dst = src.with_name(f"auth.json.bak-cuentas-{datetime.now().strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(src, dst)
    return str(dst)


def apply_remove(entry_id: str) -> Dict[str, Any]:
    with _MUTATE_LOCK:
        from agent.credential_pool import load_pool
        from agent.credential_sources import find_removal_step
        from hermes_cli.auth import suppress_credential_source
        pool = load_pool(PROVIDER)
        ordered = sorted(pool.entries(), key=lambda e: e.priority)
        idx = next((i for i, e in enumerate(pool.entries(), start=1) if e.id == entry_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="that account is no longer in Hermes")
        if len(ordered) <= 1:
            raise HTTPException(status_code=400, detail="it is the only account; not removing it so Anthropic keeps working")
        backup = _backup_auth()
        removed = pool.remove_index(idx)
        source = getattr(removed, "source", "") or ""
        step = find_removal_step(PROVIDER, source)
        hints: List[str] = []
        # Manual entries have no removal step (they are not re-seeded). Borrowed ones (claude_code)
        # are only suppressed: ~/.claude/.credentials.json, used by the Claude Code CLI, is never touched.
        if step is not None and source == "claude_code":
            try:
                result = step.remove_fn(PROVIDER, removed)
                hints = list(result.hints)
            except Exception:
                logger.warning("cleanup of %s failed; suppressing it anyway", source, exc_info=True)
            suppress_credential_source(PROVIDER, source)
        elif source.startswith("env:"):
            suppress_credential_source(PROVIDER, source)
        want = store.desired_order()
        store.save_order([i for i in (want["ids"] or [e.id for e in ordered]) if i != entry_id],
                         removed=list(want["removed"]) + [entry_id])
        with _CACHE_LOCK:
            _CACHE.pop(entry_id, None)
        logger.info("account removed: %s (%s); backup %s", entry_id, getattr(removed, "label", "?"), backup)
        return {"removed": entry_id, "backup": backup, "hints": hints}


@router.post("/order")
def post_order(body: OrderBody):
    return apply_order(list(body.ids or []))


@router.post("/use")
def post_use(body: EntryBody):
    return apply_use(body.entry_id)


@router.post("/session")
def post_session(body: SessionBody):
    return apply_session(body.session_id, body.entry_id)


@router.post("/remove")
def post_remove(body: EntryBody):
    return apply_remove(body.entry_id)


@router.post("/alias")
def post_alias(body: AliasBody):
    if not body.email:
        raise HTTPException(status_code=400, detail="missing email")
    return {"aliases": store.set_alias(body.email, body.alias)}


# Compatibility with the old panel (v0.1): "pin" = move it to the top.
class PinBody(BaseModel):
    entry_id: Optional[str] = None


@router.post("/pin")
def post_pin(body: PinBody):
    if not body.entry_id:
        return {"ok": True}
    return apply_use(body.entry_id)
