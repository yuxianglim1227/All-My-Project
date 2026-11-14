from datetime import datetime, timedelta, timezone

import pytest

from socialagent.config import BrandProfile, Settings
from socialagent.models import ContentPlan, PLAN_SCHEMA
from socialagent.store import Store
from socialagent.timeutil import iso, parse_iso, parse_when, tz


# ---------------------------------------------------------------- settings
def test_settings_defaults_are_safe():
    s = Settings.from_env({})
    assert s.dry_run is True            # never posts until explicitly enabled
    assert s.edit_level == "enhance"                # light polish by default; originals are one tap away
    assert s.tiktok_privacy == "SELF_ONLY"
    assert s.owner_telegram_id is None


def test_settings_parse_and_validate():
    s = Settings.from_env({
        "DRY_RUN": "false", "OWNER_TELEGRAM_ID": "12345", "TIMEZONE": "Asia/Tokyo",
        "TIKTOK_MODE": "INBOX", "CLAUDE_EFFORT": "High", "PUBLIC_BASE_URL": "https://x.example/",
    })
    assert s.dry_run is False and s.owner_telegram_id == 12345
    assert s.tiktok_mode == "inbox" and s.claude_effort == "high"
    assert s.public_base_url == "https://x.example"      # trailing slash stripped


@pytest.mark.parametrize("env", [
    {"TIKTOK_MODE": "bogus"}, {"CLAUDE_EFFORT": "turbo"}, {"TIMEZONE": "Mars/Base"}, {"HTTP_PORT": "abc"},
])
def test_settings_reject_bad_values(env):
    with pytest.raises(ValueError):
        Settings.from_env(env)


# ---------------------------------------------------------------- brand
def test_brand_profile_loads_shipped_yaml(tmp_path):
    from pathlib import Path
    brand = BrandProfile.load(Path(__file__).parents[1] / "config" / "brand.yaml")
    assert brand.pillar_names and brand.highlight_names
    assert abs(sum(p.weight for p in brand.pillars) - 100) < 1e-6
    assert brand.posting_windows["instagram"] == ["12:30", "19:30"]
    assert "Highlight collections" in brand.prompt_block()


def test_brand_missing_file_uses_defaults(tmp_path):
    b = BrandProfile.load(tmp_path / "nope.yaml")
    assert b.account_name == "My Account" and b.pillars == []


def test_brand_hashtag_normalisation():
    b = BrandProfile.from_dict({"hashtags": {"always": ["food", "#yum"], "banned": ["spam"]}})
    assert b.hashtags_always == ["#food", "#yum"] and b.hashtags_banned == ["#spam"]


# ---------------------------------------------------------------- time
def test_time_helpers_roundtrip_and_when_parsing():
    now = datetime(2026, 11, 11, 12, 0, 5, 999, tzinfo=timezone.utc)
    assert parse_iso(iso(now)) == now.replace(microsecond=0)
    with pytest.raises(ValueError):
        iso(datetime(2026, 1, 1))
    zone = tz("Asia/Tokyo")
    assert parse_when("now", zone) is None and parse_when("next_slot", zone) is None
    assert parse_when("garbage", zone) is None
    assert parse_when("2026-11-12T18:30", zone) == datetime(2026, 11, 12, 9, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------- plan
def test_plan_schema_is_strict_and_every_property_required():
    def walk(node):
        if isinstance(node, dict) and node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for v in node["properties"].values():
                walk(v)
        elif isinstance(node, dict) and node.get("type") == "array":
            walk(node["items"])
    walk(PLAN_SCHEMA)


def test_plan_from_dict_tolerates_missing_and_odd_values():
    plan = ContentPlan.from_dict({"pillar": "Daily life", "posts": [
        {"platform": " Instagram ", "kind": "REEL", "trim_end": "3.5", "media_indexes": ["0"], "hashtags": ["#a"]}]})
    p = plan.posts[0]
    assert (p.platform, p.kind, p.trim_end, p.media_indexes) == ("instagram", "reel", 3.5, [0])
    assert p.when == "next_slot"


# ---------------------------------------------------------------- store
@pytest.fixture()
def store():
    s = Store(":memory:")
    yield s
    s.close()


def _post(store, sid, when, status="scheduled", platform="instagram", **kw):
    return store.add_post(submission_id=sid, platform=platform, kind="photo", caption="c", hashtags=["#t"],
                          media=[{"path": "/x.jpg", "kind": "image"}], publish_at=when, status=status, **kw)


def test_kv_roundtrip(store):
    assert store.kv_get("a") is None and store.kv_get("a", "d") == "d"
    store.kv_set("a", "1"); store.kv_set("a", "2")
    assert store.kv_get("a") == "2"


def test_due_claim_cancel_flow(store):
    sid = store.add_submission(source="t", instruction="hi", media=[])
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    early = _post(store, sid, now - timedelta(minutes=1))
    later = _post(store, sid, now + timedelta(hours=1))
    handed = _post(store, sid, now - timedelta(minutes=5), status="handed_off", method="manual")

    due = store.due_posts(now)
    assert [p["id"] for p in due] == [early]              # not the future one, not a manual hand-off
    assert due[0]["hashtags"] == ["#t"] and due[0]["media"][0]["kind"] == "image"

    assert store.claim_post(early) is True
    assert store.claim_post(early) is False              # second claimer loses the race
    assert store.cancel_post(early) is False             # already publishing
    assert store.cancel_post(later) is True
    assert store.get_post(later)["status"] == "cancelled"
    assert store.due_posts(now) == []

    # manual method: the owner confirms they posted it themselves
    assert store.get_post(handed)["method"] == "manual"
    assert store.confirm_posted(handed) is True and store.get_post(handed)["status"] == "published"
    assert store.confirm_posted(handed) is False and store.confirm_posted(early) is False   # only hand-offs can be confirmed


def test_set_post_rejects_unknown_columns(store):
    sid = store.add_submission(source="t", instruction="", media=[])
    pid = _post(store, sid, datetime(2026, 1, 1, tzinfo=timezone.utc))
    store.set_post(pid, status="published", remote_id="42", attempts=1)
    got = store.get_post(pid)
    assert (got["status"], got["remote_id"], got["attempts"]) == ("published", "42", 1)
    with pytest.raises(KeyError):
        store.set_post(pid, hashtags_json="[]")


def test_trends_inspiration_and_highlights(store):
    store.replace_trends([{"platform": "tiktok", "name": "A"}, {"platform": "both", "name": "B"}])
    store.replace_trends([{"platform": "tiktok", "name": "C"}])
    assert [t["name"] for t in store.list_trends()] == ["C"]

    store.add_inspiration(source_url="https://t/1", title="x", analysis={"hook": "h"})
    assert store.has_inspiration_url("https://t/1") and not store.has_inspiration_url("https://t/2")
    assert store.list_inspiration()[0]["analysis"] == {"hook": "h"}

    sid = store.add_submission(source="t", instruction="", media=[])
    p1 = _post(store, sid, datetime(2026, 1, 1, tzinfo=timezone.utc))
    p2 = _post(store, sid, datetime(2026, 1, 2, tzinfo=timezone.utc))
    store.add_highlight_pending(p1, "Travel"); store.add_highlight_pending(p2, "Food")
    assert {h["collection"] for h in store.pending_highlights()} == {"Travel", "Food"}
    assert store.mark_highlights_done("travel") == 1       # case-insensitive
    assert [h["collection"] for h in store.pending_highlights()] == ["Food"]
    assert store.mark_highlights_done() == 1 and store.pending_highlights() == []


# ---------------------------------------------------------------- docs <-> code drift guard
def test_env_example_matches_the_settings_the_code_actually_reads():
    """Every variable documented in .env.example must be consumed by config.py (no typos / dead options),
    and loading the example as-is must yield a valid, SAFE configuration."""
    from pathlib import Path
    from socialagent.config import load_dotenv

    root = Path(__file__).parents[1]
    env: dict[str, str] = {}
    load_dotenv(root / ".env.example", env)
    config_src = (root / "src" / "socialagent" / "config.py").read_text(encoding="utf-8")
    # ANTHROPIC_API_KEY is read by the Anthropic SDK itself, not by Settings.
    unread = [k for k in env if k != "ANTHROPIC_API_KEY" and f'"{k}"' not in config_src]
    assert unread == [], f".env.example documents variables the code never reads: {unread}"

    s = Settings.from_env(env)
    assert s.dry_run is True and s.edit_level == "enhance"          # the shipped example is safe by default
    assert s.tiktok_privacy == "SELF_ONLY" and s.tiktok_mode == "direct"
    assert s.claude_model == "claude-opus-5-5" and s.owner_telegram_id is None
    assert s.undo_window_seconds == 120 and s.collect_window_seconds == 3 and s.max_reel_seconds == 90


# ---------------------------------------------------------------- drafts table
def test_draft_roundtrip_update_list_and_card_lookup(store):
    sid = store.add_submission(source="t", instruction="", media=[])
    did = store.add_draft(submission_id=sid, tone="funny", lang="zh", instruction="hi", state="drafting")
    d = store.get_draft(did)
    assert (d["state"], d["tone"], d["lang"], d["brief"], d["targets"]) == ("drafting", "funny", "zh", None, [])
    store.update_draft(did, state="ready", brief={"summary": "s"}, targets=[{"id": "t1"}], card_ref="777", tone="aesthetic")
    d = store.get_draft(did)
    assert (d["state"], d["tone"], d["brief"], d["targets"], d["card_ref"]) == ("ready", "aesthetic", {"summary": "s"}, [{"id": "t1"}], "777")
    assert store.find_draft_by_card("777")["id"] == did and store.find_draft_by_card("nope") is None
    other = store.add_draft(submission_id=sid, tone="funny", lang="en", instruction="", state="discarded")
    assert [x["id"] for x in store.list_drafts(["ready"])] == [did]
    assert {x["id"] for x in store.list_drafts()} == {did, other}
    with pytest.raises(KeyError):
        store.update_draft(did, bogus="x")
    assert store.get_draft("missing") is None
