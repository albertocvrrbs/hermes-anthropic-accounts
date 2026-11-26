"""Backend tests (order / use first / remove / alias / per-chat account) in a temporary HERMES_HOME.

Run with the Python interpreter of a live Hermes install (see README > Tests). Never touches
~/.hermes: everything happens under a temporary HERMES_HOME, and usage lookups are faked.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERMES_AGENT = Path(os.environ.get("HERMES_AGENT_DIR") or Path.home() / ".hermes" / "hermes-agent")
sys.path.insert(0, str(HERMES_AGENT))
import agent.credential_pool  # noqa: E402,F401  (import before switching HERMES_HOME)
import hermes_cli.auth  # noqa: E402,F401
import agent.credential_sources  # noqa: E402,F401

TMP = tempfile.mkdtemp(prefix="ac-api-", dir=os.environ.get("TMPDIR"))
os.environ["HERMES_HOME"] = TMP
from hermes_constants import get_hermes_home  # noqa: E402
assert str(get_hermes_home()) == TMP

PLUGIN = Path(__file__).resolve().parents[1]
NOW = time.time()
# Fake, non-functional tokens: the pool only needs well-formed rows.
rows = [{"id": i, "label": f"oauth-{i}", "auth_type": "oauth", "priority": n, "source": "manual:hermes_pkce",
         "access_token": "fake-access-" + i, "refresh_token": "fake-refresh-" + i,
         "base_url": "https://api.anthropic.com",
         "expires_at_ms": int((NOW + 8 * 3600) * 1000)} for n, i in enumerate(["aaaaaa", "bbbbbb", "cccccc"])]
rows[1]["model_cooldowns"] = {"claude-opus-5-5": NOW + 3600}
Path(TMP, "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"anthropic": rows},
                                              "suppressed_sources": {"anthropic": ["claude_code"]}}))

sys.path.insert(0, str(PLUGIN / "dashboard"))
import plugin_api as api  # noqa: E402
from agent.credential_pool import load_pool  # noqa: E402

FAKE = {"aaaaaa": ("one@example.com", 20.0), "bbbbbb": ("two@example.com", 10.0),
        "cccccc": ("three@example.com", 100.0)}


def fake_fetch(pool, entry, fresh):
    email, week = FAKE[entry.id]
    return {"email": email, "plan": "pro", "error": None, "fetched_at": time.time(),
            "five_hour": {"used_pct": 5.0, "resets_at": None},
            "seven_day": {"used_pct": week, "resets_at": "2099-01-01T00:00:00+00:00"},
            "seven_day_opus": None, "seven_day_sonnet": None}


api._fetch_account = fake_fetch
results = []


def check(name, cond, detail: object = ""):
    results.append(bool(cond))
    print(("OK   " if cond else "FAIL ") + name + (f"  -> {detail}" if detail else ""))


order = lambda: [e.id for e in sorted(load_pool("anthropic").entries(), key=lambda e: e.priority)]  # noqa: E731

d = api.build_accounts(False, None)
st = {a["id"]: a["status"]["state"] for a in d["accounts"]}
check("readable states", st == {"aaaaaa": "ok", "bbbbbb": "paused", "cccccc": "limit"}, str(st))
codes = {a["id"]: a["status"]["code"] for a in d["accounts"]}
check("stable status codes for the UI", codes == {"aaaaaa": None, "bbbbbb": "paused", "cccccc": "limit_week"},
      str(codes))
check("next = first one with quota", d["next_id"] == "aaaaaa", d["next_id"])

api.apply_order(["cccccc", "aaaaaa", "bbbbbb"])
check("order saved to auth.json", order() == ["cccccc", "aaaaaa", "bbbbbb"], str(order()))

r = api.apply_use("bbbbbb")
b = next(e for e in load_pool("anthropic").entries() if e.id == "bbbbbb")
check("use first moves it to the top", order()[0] == "bbbbbb", str(order()))
check("with confirmed quota it lifts the pause", r["lifted_pause"] and not b.model_cooldowns, str(r))

r = api.apply_use("cccccc")
check("at the cap: moves up but promises no quota",
      order()[0] == "cccccc" and not r["lifted_pause"] and r["blocked_until"], str(r))

r = api.apply_remove("aaaaaa")
check("remove takes it out of the pool", "aaaaaa" not in order(), str(order()))
check("remove leaves a backup", Path(r["backup"]).exists(), r["backup"])
check("remove records it so it does not come back", "aaaaaa" in api.store.desired_order()["removed"])

api.store.set_alias("two@example.com", "Work")
d = api.build_accounts(False, None)
check("alias applied", next(a for a in d["accounts"] if a["id"] == "bbbbbb")["alias"] == "Work")

# account per chat
api.store.record_session("chat-x", "cccccc", "claude-opus-5-5")
r = api.apply_session("chat-x", "bbbbbb")
rec = api.store.session_record("chat-x") or {}
check("pick an account for one chat", rec.get("entry_id") == "bbbbbb" and rec.get("manual"), str(rec))
check("stores the manual reason as a code", rec.get("reason") == api.store.REASON_MANUAL, str(rec))
check("does not change the global order", order()[0] == "cccccc", str(order()))
d = api.build_accounts(False, "chat-x")
check("the panel knows which account that chat uses", (d["session"] or {}).get("entry_id") == "bbbbbb")
check("counts chats per account", next(a for a in d["accounts"] if a["id"] == "bbbbbb")["chats"] == 1)
check("where it would go if it runs out (not to the full one)", d["best_id"] not in ("bbbbbb", "cccccc"),
      str(d["best_id"]))
try:
    api.apply_session("", "bbbbbb")
    check("no open chat: does not assign", False)
except Exception as exc:
    check("no open chat: does not assign", getattr(exc, "status_code", None) == 400)

try:
    api.apply_remove("bbbbbb")
    api.apply_remove("cccccc")
    check("refuses to remove the last account", False)
except Exception as exc:
    check("refuses to remove the last account", getattr(exc, "status_code", None) == 400,
          str(getattr(exc, "detail", exc)))

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} OK")
sys.exit(0 if all(results) else 1)
