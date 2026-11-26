"""anthropic-cuentas hook tests against the core's REAL credential pool, in a temporary HERMES_HOME.

Run with the Python interpreter of a live Hermes install (see README > Tests). Never touches
~/.hermes or the network: per-account usage is simulated by replacing ``store.fetch_info``.
"""
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

HERMES_AGENT = Path(os.environ.get("HERMES_AGENT_DIR") or Path.home() / ".hermes" / "hermes-agent")
sys.path.insert(0, str(HERMES_AGENT))
# Import the core BEFORE switching HERMES_HOME: with an empty home, startup relaunches another interpreter.
import agent.agent_runtime_helpers  # noqa: E402,F401
import agent.credential_pool  # noqa: E402,F401
import agent.anthropic_credentials  # noqa: E402,F401
import agent.client_lifecycle  # noqa: E402,F401

TMP = tempfile.mkdtemp(prefix="ac-test-", dir=os.environ.get("TMPDIR"))
os.environ["HERMES_HOME"] = TMP
from hermes_constants import get_hermes_home  # noqa: E402
assert str(get_hermes_home()) == TMP, f"HERMES_HOME did not change: {get_hermes_home()}"
for var in ("ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
    os.environ.pop(var, None)

PLUGIN = Path(__file__).resolve().parents[1]
BASE = "https://api.anthropic.com"
NOW = time.time()
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"

# Fake, non-functional OAuth-shaped tokens.
# A = one@, B/C/D = two@ (duplicates), E = three@
ENTRIES = [("aaaaaa", "sk-ant-oat01-FAKE-A"), ("bbbbbb", "sk-ant-oat01-FAKE-B"), ("cccccc", "sk-ant-oat01-FAKE-C"),
           ("dddddd", "sk-ant-oat01-FAKE-D"), ("eeeeee", "sk-ant-oat01-FAKE-E")]
EMAIL = {"sk-ant-oat01-FAKE-A": "one@example.com", "sk-ant-oat01-FAKE-B": "two@example.com",
         "sk-ant-oat01-FAKE-C": "two@example.com", "sk-ant-oat01-FAKE-D": "two@example.com",
         "sk-ant-oat01-FAKE-E": "three@example.com"}
USED = {}      # email -> weekly % used (what "Anthropic answers")


def fake_fetch_info(token):
    email = EMAIL.get(token)
    reset = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW + 3 * 86400))
    return {"email": email, "plan": "pro", "error": None, "fetched_at": time.time(),
            "five_hour": {"used_pct": 0.0, "resets_at": None},
            "seven_day": {"used_pct": USED.get(email, 10.0), "resets_at": reset},
            "seven_day_opus": None, "seven_day_sonnet": None}


def write_auth():
    rows = []
    for i, (eid, tok) in enumerate(ENTRIES):
        rows.append({"id": eid, "label": f"oauth-{eid}", "auth_type": "oauth", "priority": i,
                     "source": "manual:hermes_pkce", "access_token": tok, "refresh_token": "fake-refresh-" + eid,
                     "base_url": BASE, "expires_at_ms": int((NOW + 8 * 3600) * 1000), "request_count": 0})
    Path(TMP, "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"anthropic": rows},
                                                  "suppressed_sources": {"anthropic": ["claude_code"]}}))


def load_plugin():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ac_plugin", PLUGIN / "__init__.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    mod.register(None)
    return mod


plugin = load_plugin()
store = plugin.store
store.fetch_info = fake_fetch_info
import agent.agent_runtime_helpers as helpers  # noqa: E402
from agent.credential_pool import load_pool  # noqa: E402
from agent.error_classifier import FailoverReason  # noqa: E402


def fresh_state(used=None, order=None):
    write_auth()
    d = Path(TMP, "plugin-data", "anthropic-cuentas")
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.json"):
        f.unlink()
    store._RECENT.clear()
    USED.clear()
    USED.update(used or {})
    if order:
        store.save_order(order)


def make_agent(pool, entry_id, sid, model=OPUS, parent=None):
    entry = next(e for e in pool.entries() if e.id == entry_id)
    ag = types.SimpleNamespace(provider="anthropic", model=model, base_url=BASE, api_key=entry.runtime_api_key,
                               _credential_pool=pool, _credential_pool_entry_id=entry_id,
                               _credential_pool_revert_id=None, session_id=sid, swaps=[],
                               _fallback_activated=False, _fallback_index=0)
    if parent is not None:
        ag._delegate_parent_ref = (lambda p=parent: p)

    def _swap(e):
        ag._credential_pool_entry_id = e.id
        ag.api_key = e.runtime_api_key
        ag.swaps.append(e.id)
        return True

    ag._swap_credential = _swap
    return ag


def turn(ag):
    helpers.restore_primary_runtime(ag)
    return ag._credential_pool_entry_id


def hit_429(ag, wait=600):
    return helpers.recover_with_credential_pool(
        ag, status_code=429, has_retried_429=False, classified_reason=FailoverReason.rate_limit,
        error_context={"reset_at": time.time() + wait,
                       "message": "This request would exceed your account's rate limit."})


results = []


def check(name, cond, detail: object = ""):
    results.append(bool(cond))
    print(("OK   " if cond else "FAIL ") + name + (f"  -> {detail}" if detail else ""))


helpers._ra()  # lazy import of run_agent (already loaded in the live process)

# 1) New chat: takes the first account in the order that has usage.
fresh_state()
pool = load_pool("anthropic")
ag = make_agent(pool, "bbbbbb", "new-chat")
check("new chat takes the first in the order", turn(ag) == "aaaaaa", ag.swaps)
check("and saves it for that chat", (store.session_record("new-chat") or {}).get("entry_id") == "aaaaaa")

# 2) Each chat keeps ITS account: S1 on E and S2 on A, even though A is first in the order.
fresh_state()
store.record_session("s1", "eeeeee", OPUS)
store.record_session("s2", "aaaaaa", OPUS)
pool = load_pool("anthropic")
a1 = make_agent(pool, "aaaaaa", "s1")
a2 = make_agent(pool, "aaaaaa", "s2")
check("chat 1 stays on its account (E)", turn(a1) == "eeeeee", a1.swaps)
check("chat 2 stays on its own (A)", turn(a2) == "aaaaaa", a2.swaps)

# 3) The chat's account runs out -> moves to the one with MOST usage left, not the next in order.
fresh_state(used={"one@example.com": 100.0, "two@example.com": 60.0, "three@example.com": 15.0})
store.record_session("s3", "aaaaaa", OPUS)
pool = load_pool("anthropic")
store.infos_for(pool, pool.entries(), max_age=0)  # fresh snapshot, as the panel leaves it
ag = make_agent(pool, "aaaaaa", "s3")
check("at turn start: moves to the one with most usage (E 85% > B 40%)", turn(ag) == "eeeeee", ag.swaps)
rec = store.session_record("s3") or {}
check("saved with the reason code", rec.get("entry_id") == "eeeeee" and rec.get("reason") == store.REASON_EXHAUSTED,
      rec)

# 4) Mid-task (subagents working, you away): long 429 -> switches on its own and the turn continues.
fresh_state(used={"one@example.com": 20.0, "two@example.com": 50.0, "three@example.com": 5.0})
store.record_session("s4", "aaaaaa", OPUS)
pool = load_pool("anthropic")
ag = make_agent(pool, "aaaaaa", "s4")
turn(ag)
USED["one@example.com"] = 100.0
store._RECENT.clear()
t0 = time.time()
rec_, retried = hit_429(ag)
check("long 429: recovers without waiting", rec_ is True and time.time() - t0 < 2, f"{rec_} {time.time() - t0:.2f}s")
check("and goes to the one with most usage (E), not the next in order (B)",
      ag._credential_pool_entry_id == "eeeeee", ag.swaps)
rec = store.session_record("s4") or {}
check("the chat is saved on E", rec.get("entry_id") == "eeeeee" and rec.get("reason") == store.REASON_MIDTASK, rec)
check("next turn: stays on E (does not drift back to A)", turn(ag) == "eeeeee", ag.swaps)

# 5) Another chat on A with ANOTHER model is not moved by the Opus pause.
USED["one@example.com"] = 20.0  # the general window has room; only Opus got paused in the pool
store._RECENT.clear()
store.infos_for(pool, pool.entries(), max_age=0)
store.record_session("s5", "aaaaaa", SONNET)
a_son = make_agent(pool, "aaaaaa", "s5", model=SONNET)
check("a Sonnet chat on the same account does not move", turn(a_son) == "aaaaaa", a_son.swaps)

# 6) Short 429 (a 10 s bump): no account switch.
fresh_state()
pool = load_pool("anthropic")
ag = make_agent(pool, "aaaaaa", "s6")
rec_, retried = hit_429(ag, wait=10)
check("short 429 does not rotate", rec_ is False and retried is True and ag._credential_pool_entry_id == "aaaaaa",
      f"{rec_} {retried} {ag._credential_pool_entry_id}")

# 7) All exhausted: no made-up rotation.
fresh_state(used={"one@example.com": 100.0, "two@example.com": 100.0, "three@example.com": 100.0})
pool = load_pool("anthropic")
ag = make_agent(pool, "aaaaaa", "s7")
rec_, _ = hit_429(ag)
check("all at the cap: does not rotate", not rec_ or ag._credential_pool_entry_id == "aaaaaa",
      f"{rec_} {ag._credential_pool_entry_id}")

# 8) Subagent: inherits the parent chat's account (not the first in the order).
fresh_state()
store.record_session("parent", "eeeeee", OPUS)
pool = load_pool("anthropic")
parent = make_agent(pool, "eeeeee", "parent")
child = make_agent(pool, "bbbbbb", "child-1", parent=parent)
check("subagent inherits the parent's account", turn(child) == "eeeeee", child.swaps)
check("a subagent does not count as a chat", store.chats_by_entry().get("eeeeee") == 1, store.chats_by_entry())

# 9) If the subagent's account runs out, only it switches; the parent stays.
USED["three@example.com"] = 100.0
store._RECENT.clear()
hit_429(child)
check("only the subagent switches", child._credential_pool_entry_id != "eeeeee"
      and parent._credential_pool_entry_id == "eeeeee", f"child={child._credential_pool_entry_id}")

# 10) A hand-picked account for a chat (from the panel) is respected.
fresh_state()
store.record_session("s10", "dddddd", OPUS, manual=True, reason=store.REASON_MANUAL)
pool = load_pool("anthropic")
ag = make_agent(pool, "aaaaaa", "s10")
check("hand-picked account for the chat", turn(ag) == "dddddd", ag.swaps)

# 11) Compression changes the session id: the agent remembers its account and does not hop.
ag.session_id = "s10-continued"
check("keeps the account after compression", turn(ag) == "dddddd", ag.swaps)

# 12) The core's revert does not send the chat back to the previous account.
fresh_state()
store.record_session("s12", "eeeeee", OPUS)
pool = load_pool("anthropic")
ag = make_agent(pool, "eeeeee", "s12")
ag._credential_pool_revert_id = "aaaaaa"
check("no automatic revert", turn(ag) == "eeeeee" and "aaaaaa" not in ag.swaps, ag.swaps)

# 13) An account removed in the panel does not come back.
fresh_state()
store.save_order(["aaaaaa", "bbbbbb", "cccccc", "eeeeee"], removed=["dddddd"])
pool = load_pool("anthropic")
turn(make_agent(pool, "aaaaaa", "s13"))
ids = [e.id for e in load_pool("anthropic").entries()]
check("removed entry does not come back", "dddddd" not in ids, ids)

# 14) Per-request refresh: with a pool, the token of ITS entry.
fresh_state()
import agent.anthropic_credentials as creds  # noqa: E402
plugin._TLS.entry_id = "dddddd"
tok = creds.resolve_anthropic_token(model=OPUS)
plugin._TLS.entry_id = None
check("refresh does not hop accounts", tok == "sk-ant-oat01-FAKE-D")
check("outside the refresh the resolver is unchanged",
      creds.resolve_anthropic_token(model="x") == "sk-ant-oat01-FAKE-A")

# 15) Records written by v0.3 (Spanish reason sentences) still load and keep the chat's account.
fresh_state()
store.record_session("legacy", "cccccc", OPUS, manual=True, reason="elegida a mano")
pool = load_pool("anthropic")
check("legacy reason records still work", turn(make_agent(pool, "aaaaaa", "legacy")) == "cccccc")

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} OK")
sys.exit(0 if all(results) else 1)
