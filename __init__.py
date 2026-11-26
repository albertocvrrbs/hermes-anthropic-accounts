"""anthropic-cuentas: every chat keeps its own Claude account and only switches when it runs out.

Hooks installed in the agent process (they only act when provider == anthropic):

1. Turn start (wraps ``restore_primary_runtime``) - one account PER CHAT:
   - If the chat already has a saved account (``sesiones.json``) with usage left for ITS model,
     it stays there even if other chats use another one. Two chats can run on different accounts.
   - If that account ran out, it moves to the one with the MOST usage left and saves it for the chat.
   - A new chat takes the first account in the panel's order that has usage; a subagent inherits
     its parent chat's account.
   - The core's "revert" (going back to the original account once it renews) is disabled: the chat
     keeps the account it moved to, which is the one saved for it.
2. Limit mid-task (wraps ``recover_with_credential_pool``): on the first 429/402 that asks to wait
   >= 45 s, the core marks the account exhausted (for that model only) and this hook switches at
   once to the account with the most usage left (a real ~1 s lookup at Anthropic). The turn just
   continues: no need to send another message. Duplicates of the exhausted account are skipped.
3. Per-request token refresh: with a pool, only the token of THE SAME entry (the original could
   hop the session to another account between two calls of the same turn).
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("anthropic-cuentas")

PROVIDER = "anthropic"
FAST_ROTATE_MIN_WAIT = 45.0   # s: a 429 asking to wait less than this is a bump, not a limit
_TLS = threading.local()


def _file_stamp(path):
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _load_store():
    name = "anthropic_cuentas_store"
    mod = sys.modules.get(name)
    path = Path(__file__).resolve().parent / "store.py"
    stamp = _file_stamp(path)
    # Reuse the cached module only when it is the same file AND the same bytes on disk: a plugin
    # update rewrites store.py under the same path, and a long-lived process must not pair the
    # new hooks with the old store (AttributeError on constants added in the update).
    if mod is not None and getattr(mod, "__file__", None) == str(path) \
            and getattr(mod, "rank_entries", None) is not None \
            and getattr(mod, "_LOADED_STAMP", None) == stamp:
        return mod
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    mod._LOADED_STAMP = stamp
    return mod


store = _load_store()


def _is_anthropic(agent) -> bool:
    return str(getattr(agent, "provider", "") or "").strip().lower() == PROVIDER


def _session_id(agent):
    return getattr(agent, "session_id", None) or getattr(agent, "_session_id", None)


def _reason_name(reason) -> str:
    return str(getattr(reason, "value", reason) or "").lower()


def _parent(agent):
    ref = getattr(agent, "_delegate_parent_ref", None)
    try:
        parent = ref() if callable(ref) else None
    except Exception:
        parent = None
    return parent if parent is not None and _is_anthropic(parent) else None


def _remember(agent, entry_id, *, reason=None, manual=False) -> None:
    if not entry_id:
        return
    agent._ac_entry = entry_id
    parent = _parent(agent)
    try:
        store.record_session(_session_id(agent), entry_id, getattr(agent, "model", None), manual=manual,
                             parent=_session_id(parent) if parent is not None else None, reason=reason)
    except Exception:
        logger.debug("record_session failed", exc_info=True)


def _switch_to(agent, pool, entry_id, model) -> bool:
    """Put the agent on *entry_id*. True if it ended up there (already was, or switched)."""
    if getattr(agent, "_credential_pool_entry_id", None) == entry_id:
        return True
    got = pool.reclaim(entry_id, model=model)  # None while still paused; clears an expired pause
    if got is None:
        return False
    if agent._swap_credential(got) is False:
        return False
    agent._credential_pool_revert_id = None
    return True


def _entry_by_id(pool, entry_id):
    return next((e for e in pool.entries() if e.id == entry_id), None) if entry_id else None


def _label(pool, entry_id):
    e = _entry_by_id(pool, entry_id)
    return f"{entry_id} ({getattr(e, 'label', '?')}, {store.email_of(entry_id) or '?'})"


# ------------------------------------------------------------------ 1. turn start


def ensure_session_account(agent) -> str:
    """Keep the chat on its account (or the best one if its own ran out). Returns what it did, for tests."""
    pool = getattr(agent, "_credential_pool", None)
    if not _is_anthropic(agent) or pool is None:
        return "skip"
    try:
        store.enforce_order(pool)  # on the session's pool: its subagents share it
    except Exception:
        logger.debug("could not apply the saved order", exc_info=True)
    model = getattr(agent, "model", None)
    sid = _session_id(agent)
    rec = store.session_record(sid)
    parent = _parent(agent)
    desired = ((rec or {}).get("entry_id") or getattr(agent, "_ac_entry", None)
               or (getattr(parent, "_credential_pool_entry_id", None) if parent is not None else None))
    ids = {e.id for e in pool.entries()}
    now = time.time()

    if desired and desired in ids:
        entry = _entry_by_id(pool, desired)
        if store.entry_usable(entry, model, now) and not store.blocked_by_snapshot(desired, model, None, now):
            if _switch_to(agent, pool, desired, model):
                _remember(agent, desired)
                return "kept"
        mode, exclude = "remaining", {store.email_of(desired)}
        why = store.REASON_EXHAUSTED
    elif desired:
        mode, exclude, why = "remaining", set(), store.REASON_GONE
    else:
        mode, exclude, why = "order", set(), None

    max_age = store.SNAPSHOT_MAX_AGE if mode == "order" else 90
    infos = store.infos_for(pool, pool.entries(), max_age=max_age)
    for cand in store.rank_entries(pool, model, mode, exclude_emails=exclude, infos=infos):
        if _switch_to(agent, pool, cand.id, model):
            _remember(agent, cand.id, reason=why)
            if why:
                logger.info("chat %s: %s; moving to %s (most usage left) for %s",
                            sid, store.REASON_TEXT.get(why, why), _label(pool, cand.id), model)
            return "switched" if why else "assigned"
    # nothing with usage: stay put; the core will wait or use the fallback
    _remember(agent, getattr(agent, "_credential_pool_entry_id", None))
    return "none"


def _install_turn_start(helpers) -> None:
    current = helpers.restore_primary_runtime
    original = getattr(current, "_ac_original", current)

    def restore_primary_runtime(agent):
        if _is_anthropic(agent) and getattr(agent, "_credential_pool", None) is not None:
            agent._credential_pool_revert_id = None  # ensure_session_account decides the chat's account
        result = original(agent)
        try:
            ensure_session_account(agent)
        except Exception:
            logger.warning("ensure_session_account failed", exc_info=True)
        return result

    restore_primary_runtime._ac_original = original  # type: ignore[attr-defined]
    restore_primary_runtime.__doc__ = original.__doc__
    helpers.restore_primary_runtime = restore_primary_runtime


# ------------------------------------------------------------------ 2. limit mid-task


def _wait_from_context(error_context):
    if not isinstance(error_context, dict):
        return None
    reset = error_context.get("reset_at")
    if reset in (None, ""):
        return None
    try:
        from agent.credential_pool import _parse_absolute_timestamp
        at = _parse_absolute_timestamp(reset)
    except Exception:
        at = store._iso_to_epoch(reset)
    return (at - time.time()) if at else None


def _install_fast_rotation(helpers) -> None:
    current = helpers.recover_with_credential_pool
    original = getattr(current, "_ac_original", current)

    def recover_with_credential_pool(agent, *, status_code=None, has_retried_429=False,
                                     classified_reason=None, error_context=None, billing_unverified=False):
        kwargs = dict(status_code=status_code, classified_reason=classified_reason,
                      error_context=error_context, billing_unverified=billing_unverified)
        pool = getattr(agent, "_credential_pool", None)
        if not _is_anthropic(agent) or pool is None:
            return original(agent, has_retried_429=has_retried_429, **kwargs)
        reason = _reason_name(classified_reason) or (
            "rate_limit" if status_code == 429 else "billing" if status_code == 402 else "")
        if reason not in ("rate_limit", "billing"):
            return original(agent, has_retried_429=has_retried_429, **kwargs)
        wait = _wait_from_context(error_context)
        if reason == "rate_limit" and not has_retried_429 and wait is not None and wait < FAST_ROTATE_MIN_WAIT:
            return original(agent, has_retried_429=has_retried_429, **kwargs)  # short bump: retry as usual

        model = getattr(agent, "model", None)
        failed_id = getattr(agent, "_credential_pool_entry_id", None)
        infos = store.infos_for(pool, pool.entries(), max_age=90)
        failed_email = (infos.get(failed_id) or {}).get("email") or store.email_of(failed_id)
        ranked = store.rank_entries(pool, model, "remaining", exclude_emails={failed_email},
                                    infos=infos, exclude_ids={failed_id} if failed_id else ())
        if not ranked:
            logger.info("limit on %s and no other account has usage for %s", failed_id, model)
            return original(agent, has_retried_429=has_retried_429, **kwargs)

        # the core marks the exhausted one (for this model only) and rotates wherever; then go to the best
        recovered, retried = original(agent, has_retried_429=True, **kwargs)
        for cand in store.rank_entries(pool, model, "remaining", exclude_emails={failed_email},
                                       infos=infos, exclude_ids={failed_id} if failed_id else ()):
            if _switch_to(agent, pool, cand.id, model):
                _remember(agent, cand.id, reason=store.REASON_MIDTASK)
                logger.info("limit on %s (asked to wait %s s): continuing on %s, the one with most usage left",
                            _label(pool, failed_id), "?" if wait is None else int(wait), _label(pool, cand.id))
                return True, False
        if recovered:
            _remember(agent, getattr(agent, "_credential_pool_entry_id", None), reason=store.REASON_MIDTASK)
        return recovered, retried

    recover_with_credential_pool._ac_original = original  # type: ignore[attr-defined]
    recover_with_credential_pool.__doc__ = original.__doc__
    helpers.recover_with_credential_pool = recover_with_credential_pool


# ------------------------------------------------------------------ 3. refresh without hopping accounts


def _install_refresh_guard() -> None:
    import agent.anthropic_credentials as creds
    from agent.client_lifecycle import ClientLifecycleMixin

    cur_resolve = creds.resolve_anthropic_token
    orig_resolve = getattr(cur_resolve, "_ac_original", cur_resolve)

    def resolve_anthropic_token(*, model=None):
        entry_id = getattr(_TLS, "entry_id", None)
        if not entry_id:
            return orig_resolve(model=model)
        # Inside a pooled session's refresh: only the current token of its own entry.
        try:
            from agent.credential_pool import load_pool
            for e in load_pool(PROVIDER).entries():
                if e.id == entry_id:
                    tok = (getattr(e, "runtime_api_key", None) or getattr(e, "access_token", None) or "").strip()
                    return tok or None
        except Exception:
            logger.debug("could not read entry %s", entry_id, exc_info=True)
        return None

    resolve_anthropic_token._ac_original = orig_resolve  # type: ignore[attr-defined]
    creds.resolve_anthropic_token = resolve_anthropic_token

    cur_refresh = ClientLifecycleMixin._try_refresh_anthropic_client_credentials
    orig_refresh = getattr(cur_refresh, "_ac_original", cur_refresh)

    def _try_refresh_anthropic_client_credentials(self):
        entry_id = getattr(self, "_credential_pool_entry_id", None)
        if not (entry_id and getattr(self, "_credential_pool", None) is not None and _is_anthropic(self)):
            return orig_refresh(self)
        _TLS.entry_id = entry_id
        try:
            return orig_refresh(self)
        finally:
            _TLS.entry_id = None

    _try_refresh_anthropic_client_credentials._ac_original = orig_refresh  # type: ignore[attr-defined]
    ClientLifecycleMixin._try_refresh_anthropic_client_credentials = _try_refresh_anthropic_client_credentials


def _install() -> None:
    import agent.agent_runtime_helpers as helpers
    _install_turn_start(helpers)
    _install_fast_rotation(helpers)
    _install_refresh_guard()


def register(ctx):
    try:
        _install()
        logger.info("anthropic-cuentas: hooks installed (account per chat, switch to the one with most "
                    "usage left, per-entry refresh)")
    except Exception:
        logger.warning("anthropic-cuentas: could not install the hooks", exc_info=True)
