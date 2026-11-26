"""구독 한도 수집의 폴더 슬롯 — 탐색·키체인 후보·깨우기 env·API 슬롯·이메일 이력·Codex 슬롯별·영속·엔드포인트.

실제 키체인·네트워크·claude/codex CLI·실제 홈 폴더는 쓰지 않는다: 홈은 임시 폴더(HOME 대체), subprocess.run·
claude_credentials·claude_get·codex_rpc는 전부 대체하고, DB는 :memory: 또는 임시 파일(data/madison.db는 건드리지 않는다).
이메일은 example.com, 토큰·키는 명백한 가짜."""
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import accounts, db, llm, usage

ALICE, BOB, CAROL = "alice@example.com", "bob@example.com", "carol@example.com"

USAGE_BODY = {"limits": [
    {"kind": "session", "percent": 10, "resets_at": "2099-01-01T00:00:00Z"},
    {"kind": "weekly_all", "percent": 20, "resets_at": "2099-01-05T00:00:00Z"}]}
PROFILE_BODY = {"organization": {"plan_display_name": "Max", "uuid": "org-1"}}


def iso(offset=0.0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))


def ms(offset=0.0):
    """지금 + offset초의 밀리초 epoch (자격증명의 expiresAt 형식)."""
    return int((time.time() + offset) * 1000)


def jwt(claims):
    seg = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@contextlib.contextmanager
def captured_logs():
    """루트 로거로 올라오는 모든 로그 메시지를 모은다(이메일·토큰 누출 검사용)."""
    handler, root = _Capture(), logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler.lines
    finally:
        root.removeHandler(handler)
        root.setLevel(old)


def slot(provider, key, account, label=None):
    return {"key": key, "label": label or Path(key).name.split("-", 1)[-1], "account": account}


# ── 공통 하네스: 임시 홈·상태 초기화·인메모리 DB ─────────────

class AccountsBase(unittest.TestCase):
    """임시 HOME(실제 홈 폴더 미사용), 프로세스 전역 usage 상태·DB 대체. 허브 env의 인증 변수는 비운다."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name).resolve()
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_CODE_")
                       or k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENAI_API_KEY"))}
        env["HOME"] = str(self.home)
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start(); self.addCleanup(p.stop)
        with usage._lock:
            saved_snap = {k: dict(v) for k, v in usage._snap.items()}
            saved_slots = {k: list(v) for k, v in usage._slots.items()}
            usage._snap.update({"claude": {}, "codex": {}})
            usage._slots.update({"claude": [], "codex": []})
        saved_state = usage._slot_state
        usage._slot_state = {}
        self.addCleanup(self._restore, saved_snap, saved_slots, saved_state)
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)
        p = mock.patch.object(db, "tx", self._tx)
        p.start(); self.addCleanup(p.stop)
        accounts.clear_worker_cache()

    @staticmethod
    def _restore(snap, slots, state):
        with usage._lock:
            usage._snap.update(snap)
            usage._slots.update(slots)
        usage._slot_state = state

    @contextlib.contextmanager
    def _tx(self):
        yield self.conn
        self.conn.commit()

    @staticmethod
    def win(title, pct, resets=None):
        return {"title": title, "pct": pct, "resets_at": resets}

    # ── 임시 홈에 슬롯·기본 로그인 만들기 ──
    def claude_login(self, folder: Path, email=ALICE, org_type="claude_max", org_name="Org"):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "emailAddress": email, "organizationType": org_type, "organizationName": org_name}}))

    def add_claude(self, name, **kw) -> str:
        folder = self.home / f".claude-{name}"
        self.claude_login(folder, **kw)
        return str(folder)

    def codex_login(self, folder: Path, email=BOB, api_key=None):
        folder.mkdir(parents=True, exist_ok=True)
        if api_key:
            auth = {"auth_mode": "apikey", "OPENAI_API_KEY": api_key}
        else:
            auth = {"auth_mode": "chatgpt", "tokens": {"id_token": jwt({"email": email})}}
        (folder / "auth.json").write_text(json.dumps(auth))

    def add_codex(self, name, **kw) -> str:
        folder = self.home / f".codex-{name}"
        self.codex_login(folder, **kw)
        return str(folder)

    def default_claude(self, email=ALICE):
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "emailAddress": email, "organizationType": "claude_max"}}))

    def default_codex(self, email=BOB):
        self.codex_login(self.home / ".codex", email=email)


# ── 1. 슬롯 탐색 ──────────────────────────────────────

class DiscoverTests(AccountsBase):
    def test_claude_slots_are_named_folders_in_name_order(self):
        b, a = self.add_claude("b", email=BOB), self.add_claude("a", email=ALICE)
        found = usage._discover("claude")
        self.assertEqual([x["label"] for x in found], ["a", "b"])
        self.assertEqual([x["key"] for x in found], [a, b])
        self.assertEqual([x["account"] for x in found], [ALICE, BOB])
        for x in found:
            self.assertEqual(set(x), {"key", "label", "account"})

    def test_symlink_to_the_default_login_folder_is_not_a_slot(self):
        self.claude_login(self.home / ".claude")
        (self.home / ".claude-link").symlink_to(self.home / ".claude")
        self.codex_login(self.home / ".codex")
        (self.home / ".codex-link").symlink_to(self.home / ".codex")
        self.assertEqual(usage._discover("claude"), [])
        self.assertEqual(usage._discover("codex"), [])

    def test_symlink_chain_to_the_default_folder_is_excluded_too(self):
        self.claude_login(self.home / ".claude")
        (self.home / "hop").symlink_to(self.home / ".claude")
        (self.home / ".claude-link").symlink_to(self.home / "hop")
        self.assertEqual(usage._discover("claude"), [])

    def test_two_slots_with_the_same_real_folder_keep_only_the_first_by_name(self):
        real = self.home / "real-login"
        self.claude_login(real)
        (self.home / ".claude-b").symlink_to(real)
        (self.home / ".claude-a").symlink_to(real)
        other = self.add_claude("c", email=BOB)
        found = usage._discover("claude")
        self.assertEqual([x["label"] for x in found], ["a", "c"])
        self.assertEqual(found[1]["key"], other)

    def test_symlink_to_a_separate_real_folder_is_a_normal_slot(self):
        real = self.home / "real-login"
        self.claude_login(real)
        (self.home / ".claude-x").symlink_to(real)
        self.claude_login(self.home / ".claude")                    # 기본 폴더는 따로 있다
        self.assertEqual([x["label"] for x in usage._discover("claude")], ["x"])

    def test_key_is_canonical_absolute_path_without_trailing_slash(self):
        folder = self.add_claude("a")
        key = usage._discover("claude")[0]["key"]
        self.assertEqual(key, folder)
        self.assertTrue(os.path.isabs(key))
        self.assertFalse(key.endswith("/"))
        self.assertEqual(key, os.path.normpath(key))

    def test_key_and_label_are_nfc(self):
        nfd = unicodedata.normalize("NFD", "한글")
        self.claude_login(self.home / f".claude-{nfd}")
        found = usage._discover("claude")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["label"], unicodedata.normalize("NFC", "한글"))
        self.assertEqual(found[0]["key"], unicodedata.normalize("NFC", found[0]["key"]))

    def test_default_folders_and_unrelated_entries_are_not_slots(self):
        self.claude_login(self.home / ".claude")                       # 기본 폴더 — 사람의 로그인
        self.default_claude()                                           # ~/.claude.json
        (self.home / ".claude-").mkdir()                                # 이름 없는 접두사만
        (self.home / "claude-x").mkdir()
        (self.home / ".claude-file").write_text("not a folder")
        self.assertEqual(usage._discover("claude"), [])

    def test_claude_slot_requires_non_empty_oauth_account(self):
        (self.home / ".claude-nofile").mkdir()
        for name, body in (("empty", {"oauthAccount": {}}), ("string", {"oauthAccount": "x"}),
                           ("none", {"oauthAccount": None}), ("missing", {"projects": {}}),
                           ("list", [1, 2]), ("badjson", None)):
            folder = self.home / f".claude-{name}"
            folder.mkdir()
            (folder / ".claude.json").write_text("{not json" if body is None else json.dumps(body))
        good = self.add_claude("good")
        self.assertEqual([x["key"] for x in usage._discover("claude")], [good])

    def test_oauth_account_without_email_is_still_a_slot_with_unknown_account(self):
        folder = self.home / ".claude-x"
        folder.mkdir()
        (folder / ".claude.json").write_text(json.dumps({"oauthAccount": {"organizationType": "claude_max"}}))
        found = usage._discover("claude")
        self.assertEqual([(x["label"], x["account"]) for x in found], [("x", "")])

    def test_codex_slot_requires_auth_json_file(self):
        (self.home / ".codex-empty").mkdir()
        (self.home / ".codex-dir").mkdir()
        (self.home / ".codex-dir" / "auth.json").mkdir()
        (self.home / ".codex-file").write_text("x")
        self.codex_login(self.home / ".codex")                         # 기본 폴더
        good = self.add_codex("good", email=CAROL)
        found = usage._discover("codex")
        self.assertEqual([(x["key"], x["label"], x["account"]) for x in found], [(good, "good", CAROL)])

    def test_providers_do_not_see_each_others_folders(self):
        self.add_claude("a")
        self.add_codex("a")
        self.assertEqual([x["key"].rsplit("/", 1)[-1] for x in usage._discover("claude")], [".claude-a"])
        self.assertEqual([x["key"].rsplit("/", 1)[-1] for x in usage._discover("codex")], [".codex-a"])

    def test_missing_home_gives_empty_list(self):
        self.assertEqual(usage._discover("claude", home=self.home / "nope"), [])

    def test_slot_account_ignores_hub_environment(self):
        # 허브 프로세스 env(API 키·다른 설정 폴더)가 슬롯 계정 해석에 섞이면 안 된다
        folder = self.add_claude("a", email=ALICE)
        codex = self.add_codex("a", email=BOB)
        other = self.add_claude("other", email=CAROL)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-fake-key", "ANTHROPIC_AUTH_TOKEN": "fake-token",
                                          "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CONFIG_DIR": other,
                                          "CODEX_HOME": str(self.home / ".codex-zzz"), "OPENAI_API_KEY": "sk-fake"}):
            self.assertEqual(usage.slot_account("claude", folder), ALICE)
            self.assertEqual(usage.slot_account("codex", codex), BOB)

    def test_slot_account_passes_only_the_slot_folder(self):
        with mock.patch.object(usage.accounts, "resolve", return_value="x@example.com") as resolve:
            usage.slot_account("claude", "/h/.claude-a")
            usage.slot_account("codex", "/h/.codex-a")
        self.assertEqual(resolve.call_args_list[0], mock.call("claude-code", {"CLAUDE_CONFIG_DIR": "/h/.claude-a"}, headless=False))
        self.assertEqual(resolve.call_args_list[1], mock.call("codex-cli", {"CODEX_HOME": "/h/.codex-a"}, headless=False))

    def test_api_logins_resolve_to_api_accounts(self):
        self.add_claude("org", org_type="api_individual", org_name="Acme Console")
        self.add_codex("key", api_key="sk-test-fake-codex-key")
        c, x = usage._discover("claude")[0], usage._discover("codex")[0]
        self.assertEqual(c["account"], "api:Acme Console")
        self.assertTrue(x["account"].startswith("api:") and len(x["account"]) == 4 + 8)
        self.assertNotIn("sk-test", x["account"])

    def test_rescan_stores_last_result_and_keeps_it_when_discovery_raises(self):
        self.add_claude("a")
        self.assertEqual(len(usage._rescan("claude")), 1)
        self.assertEqual(len(usage._slots["claude"]), 1)
        with mock.patch.object(usage, "_discover", side_effect=RuntimeError("boom")), \
                self.assertLogs(usage.log, "ERROR"):
            self.assertEqual(len(usage._rescan("claude")), 1)           # 마지막 결과 유지
        (self.home / ".claude-a" / ".claude.json").unlink()
        self.assertEqual(usage._rescan("claude"), [])
        self.assertEqual(usage._slots["claude"], [])

    def test_no_env_setting_remains(self):
        from server import config
        self.assertFalse(hasattr(config, "parse_claude_accounts"))
        self.assertFalse(hasattr(config.CFG, "claude_accounts"))


# ── 2. 키체인 서비스 이름·자격증명 찾기 ─────────────────

def expected_service(folder: str) -> str:
    return "Claude Code-credentials-" + hashlib.sha256(unicodedata.normalize("NFC", folder).encode()).hexdigest()[:8]


class KeychainNamingTests(unittest.TestCase):
    def test_default_service_name(self):
        self.assertEqual(usage.claude_keychain_service(""), "Claude Code-credentials")
        self.assertEqual(usage.claude_keychain_service(), "Claude Code-credentials")

    def test_folder_service_name_matches_independent_hash(self):
        for folder in ("/Users/x/.claude-sub", "/opt/한글/폴더", "/Users/x/.claude-sub/"):
            self.assertEqual(usage.claude_keychain_service(folder), expected_service(folder), folder)

    def test_hash_is_8_hex_chars(self):
        suffix = usage.claude_keychain_service("/opt/c").removeprefix("Claude Code-credentials-")
        self.assertEqual(len(suffix), 8)
        int(suffix, 16)

    def test_nfd_input_hashes_as_nfc(self):
        nfc = unicodedata.normalize("NFC", "/opt/한글")
        nfd = unicodedata.normalize("NFD", "/opt/한글")
        self.assertNotEqual(nfc, nfd)
        self.assertEqual(usage.claude_keychain_service(nfd), expected_service(nfc))

    def test_trailing_slash_gives_a_different_service(self):
        self.assertNotEqual(usage.claude_keychain_service("/a"), usage.claude_keychain_service("/a/"))
        self.assertNotEqual(usage.claude_keychain_service("/a"), usage.claude_keychain_service("/b"))

    def test_credentials_file_path(self):
        self.assertEqual(usage.claude_credentials_file("/opt/c"), Path("/opt/c/.credentials.json"))


class ClaudeCredentialsTests(unittest.TestCase):
    """claude_credentials — subprocess.run(security …)과 파일 읽기만 쓴다. 실제 키체인은 부르지 않는다."""

    OAUTH = {"claudeAiOauth": {"accessToken": "SECRET-A", "refreshToken": "SECRET-R", "expiresAt": 1}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key = str(Path(self.tmp.name) / ".claude-sub")
        self.keychain = {}                # 서비스 이름 → 출력 문자열(없으면 빈 출력)
        self.failure = None
        self.calls = []
        p = mock.patch.object(usage.subprocess, "run", side_effect=self.fake_run)
        p.start(); self.addCleanup(p.stop)

    def fake_run(self, cmd, **kw):
        self.calls.append(cmd)
        if self.failure:
            raise self.failure
        return SimpleNamespace(stdout=self.keychain.get(cmd[3], ""), stderr="", returncode=0)

    def write_file(self, body):
        Path(self.key).mkdir(exist_ok=True)
        (Path(self.key) / ".credentials.json").write_text(json.dumps(body))

    def test_plain_candidate_is_tried_first_and_returned(self):
        self.keychain[expected_service(self.key)] = json.dumps(self.OAUTH)
        oauth, found = usage.claude_credentials(self.key)
        self.assertEqual((oauth["accessToken"], found), ("SECRET-A", self.key))
        self.assertEqual([c[3] for c in self.calls], [expected_service(self.key)])
        self.assertEqual(self.calls[0][:3], ["security", "find-generic-password", "-s"])

    def test_slash_candidate_is_found_second_and_returned_with_the_slash(self):
        self.keychain[expected_service(self.key + "/")] = json.dumps(self.OAUTH)
        oauth, found = usage.claude_credentials(self.key)
        self.assertEqual(found, self.key + "/")                         # 이 문자열을 깨우기의 CLAUDE_CONFIG_DIR로 쓴다
        self.assertEqual([c[3] for c in self.calls], [expected_service(self.key), expected_service(self.key + "/")])

    def test_both_present_prefers_plain(self):
        self.keychain[expected_service(self.key)] = json.dumps(self.OAUTH)
        self.keychain[expected_service(self.key + "/")] = json.dumps({"claudeAiOauth": {"accessToken": "OTHER"}})
        self.assertEqual(usage.claude_credentials(self.key)[0]["accessToken"], "SECRET-A")

    def test_only_restricts_the_search_to_that_string(self):
        self.keychain[expected_service(self.key)] = json.dumps({"claudeAiOauth": {"accessToken": "PLAIN"}})
        self.keychain[expected_service(self.key + "/")] = json.dumps(self.OAUTH)
        oauth, found = usage.claude_credentials(self.key, only=self.key + "/")
        self.assertEqual((oauth["accessToken"], found), ("SECRET-A", self.key + "/"))
        self.assertEqual([c[3] for c in self.calls], [expected_service(self.key + "/")])

    def test_empty_keychain_falls_back_to_folder_credentials_file_with_plain_key(self):
        self.write_file(self.OAUTH)
        oauth, found = usage.claude_credentials(self.key)
        self.assertEqual((oauth["accessToken"], found), ("SECRET-A", self.key))
        self.assertEqual(len(self.calls), 2)                            # 두 후보를 다 본 뒤에 파일

    def test_keychain_hit_does_not_read_file(self):
        self.write_file({"claudeAiOauth": {"accessToken": "FILE"}})
        self.keychain[expected_service(self.key)] = json.dumps(self.OAUTH)
        self.assertEqual(usage.claude_credentials(self.key)[0]["accessToken"], "SECRET-A")

    def test_security_failure_falls_back_to_file(self):
        self.failure = FileNotFoundError("security")
        self.write_file(self.OAUTH)
        self.assertEqual(usage.claude_credentials(self.key)[0]["accessToken"], "SECRET-A")

    def test_default_login_files_are_never_read(self):
        home = Path(self.tmp.name)
        (home / ".claude").mkdir()
        (home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "DEFAULT"}}))
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            with self.assertRaises(usage.UsageError):
                usage.claude_credentials(self.key)

    def test_missing_everywhere_names_both_services_and_hints_login(self):
        with self.assertRaises(usage.UsageError) as cm:
            usage.claude_credentials(self.key)
        msg = str(cm.exception)
        self.assertIn(expected_service(self.key), msg)
        self.assertIn(expected_service(self.key + "/"), msg)
        self.assertIn("CLAUDE_CONFIG_DIR", msg)
        self.assertNotIn("CLAUDE_ACCOUNTS", msg)

    def test_token_not_in_error_for_bad_payloads(self):
        for raw in ('{"claudeAiOauth": {"refreshToken": "SECRET-R"}}', "SECRET-R not json"):
            self.keychain = {expected_service(self.key): raw}
            with self.assertRaises(usage.UsageError) as cm:
                usage.claude_credentials(self.key)
            self.assertNotIn("SECRET", str(cm.exception))


# ── 3. 갱신 깨우기 ────────────────────────────────────

A_KEY, B_KEY = "/home/u/.claude-a", "/home/u/.claude-b"
SLOT_A = slot("claude", A_KEY, ALICE)
SLOT_B = slot("claude", B_KEY, BOB)


class WakeBase(AccountsBase):
    """claude_credentials(자격증명)·subprocess.run(claude)·claude_get(HTTP)을 가짜로 둔다.
    on_run(cmd, env)이 자격증명을 바꾸면 'claude가 토큰을 갱신한' 것을 흉내낸다."""

    def setUp(self):
        super().setUp()
        self.creds = {A_KEY: self.oauth("OLD-ACCESS-A"), B_KEY: self.oauth("OLD-ACCESS-B")}
        self.found = {}         # key → 찾은 CLAUDE_CONFIG_DIR 문자열(기본: key 그대로)
        self.cred_calls = []    # [(key, only)]
        self.runs = []          # [(cmd, env)]
        self.on_run = None
        self.run_error = None
        self.fetches = []       # [(label, token)]
        self.usage_responses = []   # 비면 USAGE_BODY. Exception이면 raise
        p = mock.patch.object(usage, "claude_credentials", side_effect=self.fake_creds)
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(usage.subprocess, "run", side_effect=self.fake_run)
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(llm, "conf", return_value={"claude_bin": "/fake/bin/claude"})
        p.start(); self.addCleanup(p.stop)
        self.cwd = tempfile.TemporaryDirectory()
        self.addCleanup(self.cwd.cleanup)
        p = mock.patch.object(llm, "_cwd", return_value=Path(self.cwd.name))
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(usage, "claude_get", side_effect=self.fake_get)
        p.start(); self.addCleanup(p.stop)

    @staticmethod
    def oauth(access, expires_in=3600, refresh_ends_in=86400 * 30):
        o = {"accessToken": access, "refreshToken": "SECRET-REFRESH-" + access, "expiresAt": ms(expires_in)}
        if refresh_ends_in is not None:
            o["refreshTokenExpiresAt"] = ms(refresh_ends_in)
        return o

    def fake_creds(self, key, only=None):
        self.cred_calls.append((key, only))
        return dict(self.creds[key]), (only or self.found.get(key, key))

    def fake_run(self, cmd, **kw):
        self.runs.append((cmd, kw.get("env") or {}))
        if self.run_error:
            raise self.run_error
        if self.on_run:
            self.on_run(cmd, kw.get("env") or {})
        return SimpleNamespace(returncode=0, stdout='{"result":"ok","note":"SECRET-STDOUT"}', stderr="SECRET-STDERR")

    def fake_get(self, url, token, label="usage", timeout=15, backoff=False, key=""):
        self.fetches.append((label, token))
        if label == "usage":
            r = self.usage_responses.pop(0) if self.usage_responses else USAGE_BODY
        else:
            r = PROFILE_BODY if label == "profile" else {}
        if isinstance(r, Exception):
            raise r
        return r

    def usage_fetches(self):
        return [t for lbl, t in self.fetches if lbl == "usage"]

    def refresher(self, key, new_token):
        """claude 실행 시 그 슬롯 자격증명의 접근 토큰을 갱신."""
        def run(cmd, env):
            self.creds[key] = self.oauth(new_token)
        return run

    def expire(self, key):
        self.creds[key]["expiresAt"] = ms(-5)


class WakeTests(WakeBase):
    def test_unexpired_token_never_runs_claude(self):
        data = usage.collect_claude(SLOT_A)
        self.assertEqual(self.runs, [])
        self.assertEqual(self.usage_fetches(), ["OLD-ACCESS-A"])
        self.assertEqual((data["key"], data["account"]), (A_KEY, ALICE))

    def test_missing_expires_at_does_not_wake(self):
        del self.creds[A_KEY]["expiresAt"]
        usage.collect_claude(SLOT_A)
        self.assertEqual(self.runs, [])

    def test_expired_token_runs_once_then_refetches_with_new_token(self):
        self.expire(A_KEY)
        self.on_run = self.refresher(A_KEY, "NEW-ACCESS-A")
        data = usage.collect_claude(SLOT_A)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.usage_fetches(), ["NEW-ACCESS-A"])      # 만료 토큰으로는 조회하지 않는다
        self.assertEqual(data["windows"][0]["title"], "5h")

    def test_token_within_slack_counts_as_expired(self):
        self.creds[A_KEY]["expiresAt"] = ms(usage.CLAUDE_EXPIRY_SLACK - 10)
        self.on_run = self.refresher(A_KEY, "NEW-ACCESS-A")
        usage.collect_claude(SLOT_A)
        self.assertEqual(len(self.runs), 1)

    def test_token_just_outside_slack_does_not_wake(self):
        self.creds[A_KEY]["expiresAt"] = ms(usage.CLAUDE_EXPIRY_SLACK + 120)
        usage.collect_claude(SLOT_A)
        self.assertEqual(self.runs, [])

    def test_wake_command_and_config_dir(self):
        self.expire(A_KEY)
        self.on_run = self.refresher(A_KEY, "NEW-ACCESS-A")
        usage.collect_claude(SLOT_A)
        cmd, env = self.runs[0]
        self.assertEqual(cmd[0], "/fake/bin/claude")
        self.assertIn("-p", cmd)
        self.assertIn("--safe-mode", cmd)
        self.assertEqual(env["MADISON_SUPPRESS"], "1")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], A_KEY)

    def test_wake_uses_the_found_string_as_config_dir_verbatim(self):
        # 키체인에서 찾은 서비스가 끝 슬래시 후보였다면 CLAUDE_CONFIG_DIR도 슬래시가 붙은 문자열이어야 같은 저장소가 갱신된다
        self.found[B_KEY] = B_KEY + "/"
        self.expire(B_KEY)
        self.on_run = self.refresher(B_KEY, "NEW-ACCESS-B")
        usage.collect_claude(SLOT_B)
        self.assertEqual(self.runs[0][1]["CLAUDE_CONFIG_DIR"], B_KEY + "/")
        self.assertEqual(self.usage_fetches(), ["NEW-ACCESS-B"])
        self.assertEqual(self.cred_calls[-1], (B_KEY, B_KEY + "/"))     # 깨우기 뒤엔 같은 문자열만 다시 읽는다

    def test_wake_env_drops_auth_variables_but_keeps_the_rest(self):
        self.expire(A_KEY)
        self.on_run = self.refresher(A_KEY, "NEW-ACCESS-A")
        leaky = {"ANTHROPIC_API_KEY": "sk-test-fake", "ANTHROPIC_AUTH_TOKEN": "fake-token",
                 "ANTHROPIC_BASE_URL": "https://fake.invalid", "CLAUDE_CODE_USE_BEDROCK": "1",
                 "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth",
                 "CLAUDE_CONFIG_DIR": "/leaky/env/value", "MADISON_KEEP_ME": "yes"}
        with mock.patch.dict(os.environ, leaky):
            usage.collect_claude(SLOT_A)
        env = self.runs[0][1]
        for name in leaky:
            if name not in ("MADISON_KEEP_ME", "CLAUDE_CONFIG_DIR"):
                self.assertNotIn(name, env, name)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], A_KEY)               # 허브 환경의 값이 아니라 슬롯 문자열
        self.assertEqual(env["MADISON_KEEP_ME"], "yes")
        self.assertEqual(env["HOME"], str(self.home))
        self.assertTrue(env["PATH"].startswith("/fake/bin"))

    def test_wake_does_not_use_shell_or_stdin(self):
        self.expire(A_KEY)
        self.on_run = self.refresher(A_KEY, "NEW")
        with mock.patch.object(usage.subprocess, "run", side_effect=self.fake_run) as run:
            usage.collect_claude(SLOT_A)
        kw = run.call_args.kwargs
        self.assertIsInstance(run.call_args.args[0], list)
        self.assertFalse(kw.get("shell", False))
        self.assertEqual(kw["stdin"], usage.subprocess.DEVNULL)
        self.assertEqual(kw["cwd"], self.cwd.name)
        self.assertLessEqual(kw["timeout"], 120)

    def test_unchanged_token_after_wake_is_error_and_cooldown_blocks_rerun(self):
        self.expire(A_KEY)                                   # on_run이 없으니 토큰이 안 바뀐다
        with self.assertRaises(usage.UsageError) as cm:
            usage.collect_claude(SLOT_A)
        self.assertIn("갱신", str(cm.exception))
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.usage_fetches(), [])           # 같은 토큰으로 재조회하지 않는다
        with self.assertRaises(usage.UsageError) as cm2:
            usage.collect_claude(SLOT_A)
        self.assertEqual(len(self.runs), 1, "쿨다운 중에는 claude를 다시 실행하지 않는다")
        self.assertIn("분 뒤", str(cm2.exception))

    def test_cooldown_is_ten_minutes(self):
        self.assertEqual(usage.CLAUDE_WAKE_COOLDOWN, 600)
        self.expire(A_KEY)
        with self.assertRaises(usage.UsageError):
            usage.collect_claude(SLOT_A)
        usage._st(A_KEY)["wake_at"] = time.time() - 599
        with self.assertRaises(usage.UsageError):
            usage.collect_claude(SLOT_A)
        self.assertEqual(len(self.runs), 1)
        usage._st(A_KEY)["wake_at"] = time.time() - 601
        with self.assertRaises(usage.UsageError):
            usage.collect_claude(SLOT_A)
        self.assertEqual(len(self.runs), 2)

    def test_recovers_after_cooldown_when_token_refreshes(self):
        self.expire(A_KEY)
        with self.assertRaises(usage.UsageError):
            usage.collect_claude(SLOT_A)
        usage._st(A_KEY)["wake_at"] = time.time() - 700
        self.on_run = self.refresher(A_KEY, "NEW-ACCESS-A")
        data = usage.collect_claude(SLOT_A)
        self.assertEqual(len(self.runs), 2)
        self.assertEqual(usage._st(A_KEY)["wake_error"], "")
        self.assertEqual(data["windows"][0]["title"], "5h")

    def test_cooldown_is_per_slot(self):
        self.expire(A_KEY)
        self.expire(B_KEY)
        with self.assertRaises(usage.UsageError):
            usage.collect_claude(SLOT_A)
        self.on_run = self.refresher(B_KEY, "NEW-ACCESS-B")
        usage.collect_claude(SLOT_B)                         # A의 쿨다운이 B를 막지 않는다
        self.assertEqual(len(self.runs), 2)

    def test_refresh_token_expired_means_no_run_and_relogin_error(self):
        self.creds[A_KEY] = self.oauth("OLD-ACCESS-A", expires_in=-60, refresh_ends_in=-60)
        with self.assertRaises(usage.UsageError) as cm:
            usage.collect_claude(SLOT_A)
        self.assertEqual(self.runs, [])
        self.assertEqual(str(cm.exception), usage.LOGIN_EXPIRED)

    def test_refresh_token_expired_on_401_also_no_run(self):
        self.creds[A_KEY] = self.oauth("OLD-ACCESS-A", expires_in=3600, refresh_ends_in=-60)
        self.usage_responses = [usage.UnauthorizedError("토큰 거절(401)")]
        with self.assertRaises(usage.UsageError) as cm:
            usage.collect_claude(SLOT_A)
        self.assertEqual(self.runs, [])
        self.assertIn("다시 로그인", str(cm.exception))

    def test_login_ends_is_recorded_from_credentials(self):
        usage.collect_claude(SLOT_A)
        ends = self.creds[A_KEY]["refreshTokenExpiresAt"] / 1000
        self.assertAlmostEqual(usage._st(A_KEY)["login_ends"], ends, places=2)

    def test_401_wakes_then_refetches_in_same_round(self):
        self.usage_responses = [usage.UnauthorizedError("토큰 거절(401)")]
        self.on_run = self.refresher(A_KEY, "NEW-ACCESS-A")
        data = usage.collect_claude(SLOT_A)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.usage_fetches(), ["OLD-ACCESS-A", "NEW-ACCESS-A"])
        self.assertEqual(data["windows"][0]["title"], "5h")

    def test_401_with_unchanged_token_is_error_mentioning_both(self):
        self.usage_responses = [usage.UnauthorizedError("토큰 거절(401)")]
        with self.assertRaises(usage.UsageError) as cm:
            usage.collect_claude(SLOT_A)
        self.assertIn("401", str(cm.exception))
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.usage_fetches(), ["OLD-ACCESS-A"])

    def test_401_again_after_wake_does_not_wake_twice(self):
        self.usage_responses = [usage.UnauthorizedError("토큰 거절(401)"), usage.UnauthorizedError("토큰 거절(401)")]
        self.on_run = self.refresher(A_KEY, "NEW-ACCESS-A")
        with self.assertRaises(usage.UnauthorizedError):
            usage.collect_claude(SLOT_A)
        self.assertEqual(len(self.runs), 1)

    def test_expired_wake_then_401_does_not_run_a_second_time(self):
        self.expire(A_KEY)
        self.usage_responses = [usage.UnauthorizedError("토큰 거절(401)")]
        self.on_run = self.refresher(A_KEY, "NEW-ACCESS-A")
        with self.assertRaises(usage.UnauthorizedError):
            usage.collect_claude(SLOT_A)
        self.assertEqual(len(self.runs), 1)

    def test_wake_exec_failure_is_usage_error_not_crash(self):
        self.expire(A_KEY)
        self.run_error = FileNotFoundError("/fake/bin/claude")
        with self.assertRaises(usage.UsageError) as cm:
            usage.collect_claude(SLOT_A)
        self.assertIn("실행 실패", str(cm.exception))

    def test_wake_timeout_is_usage_error(self):
        self.expire(A_KEY)
        self.run_error = usage.subprocess.TimeoutExpired(["claude"], 90)
        with self.assertRaises(usage.UsageError) as cm:
            usage.collect_claude(SLOT_A)
        self.assertIn("시간 초과", str(cm.exception))

    def test_429_backoff_is_checked_before_anything_else(self):
        usage._st(A_KEY)["backoff_until"] = time.time() + 300
        self.expire(A_KEY)
        with self.assertRaises(usage.UsageError):
            usage.collect_claude(SLOT_A)
        self.assertEqual(self.runs, [])
        self.assertEqual(self.cred_calls, [])

    def test_backoff_is_per_slot(self):
        usage._st(A_KEY)["backoff_until"] = time.time() + 300
        data = usage.collect_claude(SLOT_B)
        self.assertEqual(data["key"], B_KEY)

    def test_collect_result_carries_key_label_account_login_ends(self):
        d = usage.collect_claude(SLOT_B)
        self.assertEqual((d["key"], d["label"], d["account"]), (B_KEY, "b", BOB))
        self.assertIsNotNone(d["login_ends"])
        self.assertNotIn("email", d)

    def test_profile_cache_is_per_slot(self):
        usage.collect_claude(SLOT_A)
        usage.collect_claude(SLOT_B)
        self.assertEqual([t for lbl, t in self.fetches if lbl == "profile"], ["OLD-ACCESS-A", "OLD-ACCESS-B"])

    def test_profile_email_is_not_used(self):
        # 이메일은 로컬 파일(accounts.resolve)로만 얻는다 — profile 응답의 account는 읽지 않는다
        def get(url, token, label="usage", *args, **kw):
            if label == "profile":
                return {**PROFILE_BODY, "account": {"email": CAROL}}
            return USAGE_BODY if label == "usage" else {}
        with mock.patch.object(usage, "claude_get", side_effect=get):
            d = usage.collect_claude(SLOT_A)
        self.assertNotIn(CAROL, json.dumps(d))
        self.assertEqual(d["account"], ALICE)


# ── 4b. 같은 폴더에 다른 계정으로 다시 로그인 ─────────────

class ReloginTests(WakeBase):
    """폴더가 계정 A로 수집되던 중 계정 B로 다시 로그인하고 프로필·크레딧 조회가 실패해도, A의 플랜·크레딧이
    B의 값으로 표시·기록되지 않는다."""

    def setUp(self):
        super().setUp()
        self.profile = PROFILE_BODY            # Exception이면 raise
        self.credits = {"amount": 1234, "currency": "USD"}

    def fake_get(self, url, token, label="usage", timeout=15, backoff=False, key=""):
        if label == "profile":
            self.fetches.append((label, token))
            if isinstance(self.profile, Exception):
                raise self.profile
            return self.profile
        if label == "credits":
            self.fetches.append((label, token))
            if isinstance(self.credits, Exception):
                raise self.credits
            return self.credits
        return super().fake_get(url, token, label, timeout, backoff, key)

    def refresh_slot(self, slot_):
        with usage._lock:
            usage._slots["claude"] = [dict(slot_)]
        usage._refresh_claude_slot(slot_, set())
        return usage.snapshot()["claude"]

    def plans(self):
        return [(r["account"], r["plan"]) for r in self.conn.execute("SELECT * FROM usage_plan_history ORDER BY id")]

    def credits_extra(self, tile):
        return [x for x in tile["extras"] if x["title"] == "Credits"]

    def relogin_as_bob(self):
        self.creds[A_KEY] = self.oauth("NEW-ACCESS-BOB")
        self.profile = usage.UsageError("profile 429")
        self.credits = usage.UsageError("credits 429")
        self.slots_bob = slot("claude", A_KEY, BOB)

    def test_failed_profile_after_relogin_does_not_show_or_record_the_old_plan(self):
        self.refresh_slot(SLOT_A)                                   # 계정 A(Max)로 수집·기록
        self.assertEqual(self.plans(), [(ALICE, "Max")])
        self.relogin_as_bob()
        tile = self.refresh_slot(self.slots_bob)[0]
        self.assertEqual((tile["account"], tile["plan"]), (BOB, ""))
        self.assertEqual(self.credits_extra(tile), [])
        self.assertEqual(self.plans(), [(ALICE, "Max")])            # B 계정의 Max 행이 없다
        # 이후 조회가 성공해도 가짜 "Max → Pro" 변경이 생기지 않는다
        self.profile = {"organization": {"plan_display_name": "Pro", "uuid": "org-2"}}
        usage._st(A_KEY)["profile"]["failed_at"] = 0.0
        tile = self.refresh_slot(self.slots_bob)[0]
        self.assertEqual(tile["plan"], "Pro")
        self.assertEqual(self.plans(), [(ALICE, "Max"), (BOB, "Pro")])
        h = usage.history(self.conn, 30)
        self.assertEqual(h["claude"][0]["plan_changes"], [])        # B의 이력에는 Pro 하나뿐

    def test_failed_credits_after_relogin_do_not_carry_the_old_accounts_balance(self):
        tile = self.refresh_slot(SLOT_A)[0]
        self.assertEqual(len(self.credits_extra(tile)), 1)          # A의 크레딧이 스냅샷에 있다
        self.relogin_as_bob()
        self.profile = PROFILE_BODY                                 # 플랜·조직은 얻지만 크레딧 조회만 실패
        tile = self.refresh_slot(self.slots_bob)[0]
        self.assertEqual(self.credits_extra(tile), [])

    def test_same_account_token_rotation_keeps_the_fallback_for_display_only(self):
        self.refresh_slot(SLOT_A)
        self.creds[A_KEY] = self.oauth("ROTATED-ACCESS-A")          # 같은 계정의 토큰 회전 — 캐시는 비워진다
        self.profile = usage.UsageError("profile 429")
        self.credits = usage.UsageError("credits 429")
        usage._st(A_KEY)["credits"]["failed_at"] = 0.0
        tile = self.refresh_slot(SLOT_A)[0]
        self.assertEqual(tile["plan"], "Max")                       # 같은 계정이므로 표시는 폴백
        self.assertEqual(len(self.credits_extra(tile)), 1)
        # 폴백 플랜은 이력에 쓰지 않는다 — 이력을 비우고 다시 돌려도 새 행이 생기지 않는다
        self.conn.execute("DELETE FROM usage_plan_history")
        self.creds[A_KEY] = self.oauth("ROTATED-ACCESS-A2")
        usage._st(A_KEY)["profile"]["failed_at"] = 0.0
        self.refresh_slot(SLOT_A)
        self.assertEqual(self.plans(), [])

    def test_collect_reports_whether_the_plan_was_looked_up(self):
        self.assertTrue(usage.collect_claude(SLOT_A)["plan_live"])
        self.creds[A_KEY] = self.oauth("NEW-ACCESS-2")
        self.profile = usage.UsageError("profile 429")
        self.assertFalse(usage.collect_claude(SLOT_A)["plan_live"])

    def test_plan_live_is_not_stored_in_the_snapshot(self):
        self.refresh_slot(SLOT_A)
        self.assertNotIn("plan_live", usage._snap["claude"][A_KEY])

    def test_codex_plan_is_always_recorded(self):
        a = slot("codex", "/home/u/.codex-a", ALICE)
        with mock.patch.object(usage, "codex_rpc", return_value=rpc_result()):
            usage._refresh_codex_slot(a, set())
        self.assertEqual(self.plans(), [(ALICE, "Plus")])


class WakeSecretLeakTests(WakeBase):
    """토큰 값(접근·갱신·claude 출력)과 이메일이 오류 문구·로그·스냅샷 어디에도 새지 않아야 한다."""

    SECRETS = ("OLD-ACCESS-A", "NEW-ACCESS-A", "SECRET-REFRESH", "SECRET-STDOUT", "SECRET-STDERR")

    def assertClean(self, text):
        for s in self.SECRETS:
            self.assertNotIn(s, text)

    def setUp(self):
        super().setUp()
        with usage._lock:
            usage._slots["claude"] = [dict(SLOT_A)]

    def test_no_token_in_errors_logs_or_snapshot_across_failure_modes(self):
        scenarios = {
            "unchanged": lambda: self.expire(A_KEY),
            "401": lambda: setattr(self, "usage_responses", [usage.UnauthorizedError("토큰 거절(401)")]),
            "exec": lambda: (self.expire(A_KEY), setattr(self, "run_error", OSError("boom"))),
            "login": lambda: self.creds.update({A_KEY: self.oauth("OLD-ACCESS-A", -60, -60)}),
        }
        for name, setup in scenarios.items():
            with self.subTest(name):
                usage._slot_state.clear()
                self.run_error, self.usage_responses = None, []
                self.creds[A_KEY] = self.oauth("OLD-ACCESS-A")
                setup()
                with captured_logs() as lines:
                    usage._refresh_claude_slot(SLOT_A, set())
                    usage._refresh_claude_slot(SLOT_A, set())          # 쿨다운 경로도
                self.assertClean("\n".join(lines))
                self.assertClean(usage._st(A_KEY)["error"])
                self.assertClean(json.dumps(usage.snapshot(), ensure_ascii=False))

    def test_no_token_or_email_in_logs_on_successful_wake_and_record(self):
        self.expire(A_KEY)
        self.on_run = self.refresher(A_KEY, "NEW-ACCESS-A")
        with captured_logs() as lines:
            usage._refresh_claude_slot(SLOT_A, set())
        self.assertTrue(lines)           # 깨우기 로그는 남지만
        joined = "\n".join(lines)
        self.assertClean(joined)
        self.assertNotIn(ALICE, joined)
        self.assertNotIn("alice", joined)
        self.assertIn("'a'", joined)     # 슬롯은 라벨로 남긴다

    def test_wake_summary_has_exit_code_only(self):
        self.expire(A_KEY)
        self.on_run = self.refresher(A_KEY, "NEW-ACCESS-A")
        with captured_logs() as lines:
            usage.collect_claude(SLOT_A)
        joined = "\n".join(lines)
        self.assertIn("exit 0", joined)
        self.assertNotIn("SECRET-STDOUT", joined)       # 응답 본문은 담지 않는다


# ── 4. 이력: 이메일 계정 단위 ─────────────────────────

class HistoryByAccountTests(AccountsBase):
    def set_slots(self, claude=(), codex=()):
        with usage._lock:
            usage._slots["claude"] = [dict(s) for s in claude]
            usage._slots["codex"] = [dict(s) for s in codex]

    def test_record_history_and_plan_are_independent_per_account(self):
        c = self.conn
        self.assertEqual(usage.record_history(c, "claude", [self.win("5h", 10)], ts=iso(-300), account=ALICE), 1)
        self.assertEqual(usage.record_history(c, "claude", [self.win("5h", 10)], ts=iso(-290), account=BOB), 1)
        self.assertEqual(usage.record_history(c, "claude", [self.win("5h", 10)], ts=iso(-280), account=ALICE), 0)
        rows = c.execute("SELECT account, pct FROM usage_history ORDER BY id").fetchall()
        self.assertEqual([(r["account"], r["pct"]) for r in rows], [(ALICE, 10), (BOB, 10)])
        self.assertTrue(usage.record_plan(c, "claude", "Max", ts=iso(-300), account=ALICE))
        self.assertTrue(usage.record_plan(c, "claude", "Max", ts=iso(-290), account=BOB))
        self.assertFalse(usage.record_plan(c, "claude", "Max", ts=iso(-280), account=ALICE))

    def test_history_has_one_item_per_slot_in_name_order_with_contract_shape(self):
        usage.record_history(self.conn, "claude", [self.win("5h", 10)], ts=iso(-600), account=ALICE)
        usage.record_history(self.conn, "claude", [self.win("5h", 70), self.win("7d all models", 5)], ts=iso(-600), account=BOB)
        usage.record_history(self.conn, "codex", [self.win("7d", 33)], ts=iso(-600), account=CAROL)
        self.set_slots([SLOT_A, SLOT_B], [slot("codex", "/home/u/.codex-x", CAROL)])
        h = usage.history(self.conn, days=7)
        self.assertEqual(set(h), {"claude", "codex", "from", "to"})
        self.assertEqual([a["key"] for a in h["claude"]], [A_KEY, B_KEY])
        self.assertEqual([a["label"] for a in h["claude"]], ["a", "b"])
        self.assertEqual([a["account"] for a in h["claude"]], [ALICE, BOB])
        for a in h["claude"] + h["codex"]:
            self.assertEqual(set(a), {"key", "label", "account", "api", "windows", "resets", "plan_changes"})
        self.assertEqual(list(h["claude"][0]["windows"]), ["5h"])
        self.assertEqual(sorted(h["claude"][1]["windows"]), ["5h", "7d all models"])
        self.assertEqual(h["claude"][1]["windows"]["5h"][-1][1], 70)
        self.assertEqual(h["codex"][0]["windows"]["7d"][-1][1], 33)

    def test_codex_and_claude_rows_never_mix_even_for_the_same_email(self):
        usage.record_history(self.conn, "claude", [self.win("5h", 10)], ts=iso(-60), account=ALICE)
        usage.record_history(self.conn, "codex", [self.win("5h", 99)], ts=iso(-60), account=ALICE)
        self.set_slots([SLOT_A], [slot("codex", "/home/u/.codex-x", ALICE)])
        h = usage.history(self.conn, 7)
        self.assertEqual(h["claude"][0]["windows"]["5h"][-1][1], 10)
        self.assertEqual(h["codex"][0]["windows"]["5h"][-1][1], 99)

    def test_api_unknown_and_slotless_accounts_get_empty_series(self):
        usage.record_history(self.conn, "claude", [self.win("5h", 33)], ts=iso(-60), account="")
        usage.record_history(self.conn, "claude", [self.win("5h", 44)], ts=iso(-60), account="api:abcd1234")
        usage.record_history(self.conn, "claude", [self.win("5h", 55)], ts=iso(-60), account=CAROL)   # 슬롯 없는 이메일
        self.set_slots([slot("claude", A_KEY, "api:abcd1234"), slot("claude", B_KEY, "")])
        h = usage.history(self.conn, 7)
        self.assertEqual(len(h["claude"]), 2)
        for a in h["claude"]:
            self.assertEqual((a["windows"], a["resets"], a["plan_changes"]), ({}, {}, []))
        self.assertEqual([a["account"] for a in h["claude"]], ["api:abcd1234", ""])

    def test_no_slots_gives_empty_lists(self):
        h = usage.history(self.conn, 7)
        self.assertEqual((h["claude"], h["codex"]), ([], []))

    def test_slots_with_the_same_email_share_one_series(self):
        usage.record_history(self.conn, "claude", [self.win("5h", 12)], ts=iso(-60), account=ALICE)
        self.set_slots([SLOT_A, slot("claude", B_KEY, ALICE)])
        h = usage.history(self.conn, 7)
        self.assertEqual([a["windows"] for a in h["claude"]][0], [a["windows"] for a in h["claude"]][1])

    def test_resets_and_plan_changes_are_per_account(self):
        far = time.time() + 5 * 86400
        c = self.conn
        usage.record_history(c, "claude", [self.win("7d all models", 80, far)], ts=iso(-4000), account=ALICE)
        usage.record_history(c, "claude", [self.win("7d all models", 10, far)], ts=iso(-3900), account=BOB)
        usage.record_history(c, "claude", [self.win("7d all models", 5, far)], ts=iso(-3000), account=ALICE)
        usage.record_history(c, "claude", [self.win("7d all models", 70, far)], ts=iso(-2900), account=BOB)
        usage.record_plan(c, "claude", "Max", ts=iso(-5000), account=ALICE)
        usage.record_plan(c, "claude", "Ultra", ts=iso(-1000), account=ALICE)
        usage.record_plan(c, "claude", "Pro", ts=iso(-4900), account=BOB)
        self.set_slots([SLOT_A, SLOT_B])
        a, b = usage.history(c, 7)["claude"]
        self.assertEqual([(r["from"], r["to"]) for r in a["resets"]["7d all models"]], [(80, 5)])
        self.assertEqual(b["resets"], {})
        self.assertEqual([(p["from"], p["to"]) for p in a["plan_changes"]], [("Max", "Ultra")])
        self.assertEqual(b["plan_changes"], [])

    def test_endpoint_merges_plan_email_api_live_per_slot(self):
        from server import app as hub
        usage.record_history(self.conn, "claude", [self.win("5h", 10)], ts=iso(-60), account=ALICE)
        w = {"title": "7d all models", "pct": 5, "left": "", "resets_at": None, "value": "5%"}
        api = slot("claude", "/home/u/.claude-z", "api:abcd1234")
        self.set_slots([SLOT_A, SLOT_B, api], [slot("codex", "/home/u/.codex-x", CAROL)])
        with usage._lock:
            usage._snap["claude"] = {A_KEY: {"plan": "Pro", "account": ALICE, "windows": [w], "extras": [], "error": "",
                                            "updated": iso(), "login_ends": None}}
        with mock.patch.object(hub, "_require"), mock.patch.object(hub.db, "tx", self._tx):
            h = asyncio.run(hub.get_usage_history(mock.MagicMock(), 7))
        a, b, z = h["claude"]
        for item in h["claude"] + h["codex"]:
            self.assertEqual(set(item), {"key", "label", "account", "windows", "resets", "plan_changes",
                                         "plan", "email", "api", "live"})
        self.assertEqual((a["plan"], a["email"], a["api"], a["live"]), ("Pro", ALICE, False, ["7d all models"]))
        self.assertEqual((b["plan"], b["email"], b["api"], b["live"]), ("", BOB, False, None))     # 수집 전
        self.assertEqual((z["plan"], z["email"], z["api"], z["live"], z["windows"]), ("", "", True, None, {}))
        c = h["codex"][0]
        self.assertEqual((c["email"], c["api"], c["live"]), (CAROL, False, None))
        self.assertEqual(list(a["windows"]), ["5h"])


# ── 5. snapshot() 계약 ────────────────────────────────

class SnapshotContractTests(AccountsBase):
    FIELDS = {"key", "label", "account", "email", "api", "plan", "windows", "extras", "updated", "error", "login_ends"}

    def set_slots(self, claude=(), codex=()):
        with usage._lock:
            usage._slots["claude"] = [dict(s) for s in claude]
            usage._slots["codex"] = [dict(s) for s in codex]

    def put(self, provider, key, **over):
        d = {"plan": "Max", "account": None, "windows": [{"title": "5h", "pct": 7, "resets_at": time.time() + 3600, "value": "7%"}],
             "extras": [], "updated": iso(), "error": "", "login_ends": 123.0}
        d.update(over)
        with usage._lock:
            usage._snap[provider][key] = d

    def test_empty_when_no_slots(self):
        self.assertEqual(usage.snapshot(), {"claude": [], "codex": []})

    def test_uncollected_slots_are_listed_with_reason(self):
        self.set_slots([SLOT_A], [slot("codex", "/home/u/.codex-x", BOB)])
        s = usage.snapshot()
        item = s["claude"][0]
        self.assertEqual(set(item), self.FIELDS)
        self.assertEqual((item["key"], item["label"], item["account"], item["email"], item["api"]),
                         (A_KEY, "a", ALICE, ALICE, False))
        self.assertEqual((item["windows"], item["extras"], item["updated"], item["plan"]), ([], [], "", ""))
        self.assertEqual(item["error"], "수집 전")
        self.assertIsNone(s["codex"][0]["login_ends"])
        self.assertEqual(s["codex"][0]["error"], "수집 전")

    def test_uncollected_slot_shows_last_failure_and_login_ends(self):
        self.set_slots([SLOT_A])
        usage._st(A_KEY).update(error="Claude Code 자격증명 없음", login_ends=999.0)
        item = usage.snapshot()["claude"][0]
        self.assertEqual((item["error"], item["login_ends"]), ("Claude Code 자격증명 없음", 999.0))

    def test_collected_slot_fields_and_live_left_text(self):
        self.set_slots([SLOT_A])
        self.put("claude", A_KEY, account=ALICE)
        item = usage.snapshot()["claude"][0]
        self.assertEqual(set(item), self.FIELDS)
        self.assertEqual((item["plan"], item["login_ends"], item["email"]), ("Max", 123.0, ALICE))
        self.assertEqual(item["windows"][0]["left"], usage.format_remaining(item["windows"][0]["resets_at"]))
        item["windows"][0]["pct"] = 99                       # 스냅샷은 사본
        self.assertEqual(usage._snap["claude"][A_KEY]["windows"][0]["pct"], 7)

    def test_order_is_slot_order_not_snapshot_order(self):
        self.set_slots([SLOT_A, SLOT_B])
        self.put("claude", B_KEY, account=BOB)
        self.put("claude", A_KEY, account=ALICE)
        self.assertEqual([a["key"] for a in usage.snapshot()["claude"]], [A_KEY, B_KEY])

    def test_snapshot_of_a_vanished_slot_is_not_shown(self):
        self.set_slots([SLOT_A])
        self.put("claude", A_KEY, account=ALICE)
        self.put("claude", "/home/u/.claude-gone", account=CAROL, plan="Gone")
        s = usage.snapshot()
        self.assertEqual([a["key"] for a in s["claude"]], [A_KEY])
        self.assertNotIn("Gone", json.dumps(s))

    def test_snapshot_of_another_account_is_hidden_after_a_login_change(self):
        # 같은 폴더에 다른 계정으로 다시 로그인했다 — 이전 계정의 값을 새 계정의 것처럼 내지 않는다
        self.set_slots([slot("claude", A_KEY, BOB)])
        self.put("claude", A_KEY, account=ALICE, plan="Old")
        item = usage.snapshot()["claude"][0]
        self.assertEqual((item["plan"], item["windows"], item["error"]), ("", [], "수집 전"))
        self.assertEqual(item["account"], BOB)

    def test_api_slot_contract(self):
        self.set_slots([slot("claude", A_KEY, "api:Acme Console")], [slot("codex", "/home/u/.codex-x", "api:abcd1234")])
        self.put("claude", A_KEY, account="api:Acme Console")              # 있어도 내지 않는다
        s = usage.snapshot()
        for item in (s["claude"][0], s["codex"][0]):
            self.assertEqual(set(item), self.FIELDS)
            self.assertIs(item["api"], True)
            self.assertEqual(item["email"], "")
            self.assertEqual((item["windows"], item["extras"], item["plan"], item["updated"]), ([], [], "", ""))
            self.assertEqual(item["error"], "API 로그인 — 구독 한도 없음")
        self.assertEqual(s["claude"][0]["account"], "api:Acme Console")

    def test_unknown_account_slot_is_collectable_but_has_no_email(self):
        self.set_slots([slot("claude", A_KEY, "")])
        self.put("claude", A_KEY, account="")
        item = usage.snapshot()["claude"][0]
        self.assertEqual((item["email"], item["api"], item["plan"]), ("", False, "Max"))

    def test_codex_slots(self):
        xs = [slot("codex", "/home/u/.codex-a", ALICE), slot("codex", "/home/u/.codex-b", BOB)]
        self.set_slots(codex=xs)
        self.put("codex", xs[1]["key"], account=BOB, plan="Prolite", login_ends=None,
                 windows=[{"title": "7d", "pct": 1, "resets_at": None}])
        s = usage.snapshot()["codex"]
        self.assertEqual([x["label"] for x in s], ["a", "b"])
        self.assertEqual((s[0]["error"], s[1]["plan"], s[1]["windows"][0]["left"], s[1]["login_ends"]), ("수집 전", "Prolite", "", None))
        self.assertEqual(s[1]["email"], BOB)

    def test_stored_snapshot_without_account_key_is_trusted(self):
        self.set_slots([SLOT_A])
        with usage._lock:
            usage._snap["claude"][A_KEY] = {"plan": "Max", "windows": [], "extras": []}
        self.assertEqual(usage.snapshot()["claude"][0]["plan"], "Max")


# ── 6. 영속화/복원 ────────────────────────────────────

class PersistTests(AccountsBase):
    def stored(self, value):
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                          (usage._STORE_KEY, json.dumps(value, ensure_ascii=False)))
        self.conn.commit()

    def saved(self):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (usage._STORE_KEY,)).fetchone()
        return json.loads(row["value"])

    @staticmethod
    def snap(plan="Max", pct=3, account=ALICE, **extra):
        return {"plan": plan, "account": account,
                "windows": [{"title": "5h", "pct": pct, "resets_at": None, "value": f"{pct}%"}],
                "extras": [], "updated": "2026-11-20T00:00:00Z", "error": "", **extra}

    def test_persist_writes_both_providers_keyed_by_folder_and_only_current_slots(self):
        a, x = self.add_claude("a"), self.add_codex("x", email=CAROL)
        usage._rescan("claude"); usage._rescan("codex")
        with usage._lock:
            usage._snap["claude"] = {a: self.snap("Max", 1), "/home/u/.claude-gone": self.snap("Gone", 9)}
            usage._snap["codex"] = {x: self.snap("Plus", 2, CAROL), "/home/u/.codex-gone": self.snap("Gone", 9)}
        usage._persist()
        data = self.saved()
        self.assertEqual(set(data), {"claude", "codex"})
        self.assertEqual(set(data["claude"]), {a})
        self.assertEqual(set(data["codex"]), {x})
        self.assertNotIn("Gone", json.dumps(data))

    def test_roundtrip_restores_and_scans_slots_so_tiles_show_immediately(self):
        a, x = self.add_claude("a"), self.add_codex("x", email=CAROL)
        usage._rescan("claude"); usage._rescan("codex")
        with usage._lock:
            usage._snap["claude"] = {a: self.snap("Max", 1)}
            usage._snap["codex"] = {x: self.snap("Plus", 2, CAROL)}
        usage._persist()
        with usage._lock:
            usage._snap.update({"claude": {}, "codex": {}})
            usage._slots.update({"claude": [], "codex": []})
        usage.load_persisted()
        s = usage.snapshot()
        self.assertEqual([(i["key"], i["plan"], i["windows"][0]["pct"]) for i in s["claude"]], [(a, "Max", 1)])
        self.assertEqual([(i["key"], i["plan"]) for i in s["codex"]], [(x, "Plus")])

    def test_previous_formats_are_discarded(self):
        a, x = self.add_claude("a"), self.add_codex("x", email=CAROL)
        old_flat = {"plan": "Max", "windows": [{"title": "5h", "pct": 42}], "extras": [], "updated": "x", "error": ""}
        for stored in ({"claude": old_flat, "codex": old_flat},                      # 프로바이더당 스냅샷 하나
                       {"claude": {"": old_flat}, "codex": None},                    # 기본 계정 key ""
                       {"claude": {"": old_flat, a: "str"}, "codex": {"plan": "Plus", "windows": []}}):
            self.stored(stored)
            with usage._lock:
                usage._snap.update({"claude": {}, "codex": {}})
            usage.load_persisted()
            self.assertEqual((usage._snap["claude"], usage._snap["codex"]), ({}, {}), stored)
            s = usage.snapshot()
            self.assertEqual([i["error"] for i in s["claude"] + s["codex"]], ["수집 전", "수집 전"])

    def test_garbage_is_ignored(self):
        for raw in ("not json", json.dumps([1, 2]), json.dumps({"claude": "x"}),
                    json.dumps({"claude": {"/p": "str", "/q": 1}, "codex": {"/r": {"windows": "no"}}})):
            self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (usage._STORE_KEY, raw))
            self.conn.commit()
            usage.load_persisted()
            self.assertEqual((usage._snap["claude"], usage._snap["codex"]), ({}, {}), raw)

    def test_load_does_not_overwrite_fresher_in_memory_value(self):
        a = self.add_claude("a")
        with usage._lock:
            usage._snap["claude"][a] = self.snap("Live", 77)
        self.stored({"claude": {a: self.snap("Old", 1)}, "codex": {}})
        usage.load_persisted()
        self.assertEqual(usage._snap["claude"][a]["plan"], "Live")

    def test_restored_snapshot_of_a_changed_login_is_not_shown(self):
        a = self.add_claude("a", email=BOB)                               # 폴더는 이제 Bob의 로그인
        self.stored({"claude": {a: self.snap("Old", 1, account=ALICE)}, "codex": {}})
        usage.load_persisted()
        item = usage.snapshot()["claude"][0]
        self.assertEqual((item["account"], item["plan"], item["error"]), (BOB, "", "수집 전"))

    def test_refresh_persists_when_slots_change_even_if_nothing_collected(self):
        with mock.patch.object(usage, "_persist") as persist:
            usage.refresh(("claude",))
            persist.assert_not_called()
            self.add_claude("a")
            with mock.patch.object(usage, "collect_claude", side_effect=usage.UsageError("x")):
                usage.refresh(("claude",))
            persist.assert_called_once_with()


# ── 7. refresh: 슬롯 격리·이력 기록 규칙 ────────────────

class RefreshTests(AccountsBase):
    def data(self, plan="Max", pct=5, title="5h"):
        return {"plan": plan, "windows": [{"title": title, "pct": pct, "resets_at": None, "value": f"{pct}%"}],
                "extras": [], "updated": iso(), "login_ends": None}

    def collect_by(self, plans=None, fail=None):
        """슬롯 key별 결과 — fail: {key: 예외}."""
        plans = plans or {}
        fail = fail or {}

        def fn(slot_, now=None):
            if slot_["key"] in fail:
                raise fail[slot_["key"]]
            return {**self.data(plans.get(slot_["key"], "Max"), 5), "key": slot_["key"], "label": slot_["label"]}
        return fn

    def rows(self, table="usage_history"):
        return [dict(r) for r in self.conn.execute(f"SELECT * FROM {table} ORDER BY id")]

    def test_each_subscription_slot_is_recorded_under_its_email(self):
        a = self.add_claude("a", email=ALICE)
        b = self.add_claude("b", email=BOB)
        with mock.patch.object(usage, "collect_claude", side_effect=self.collect_by({a: "Max", b: "Pro"})):
            usage.refresh(("claude",))
        self.assertEqual([(r["provider"], r["account"], r["pct"]) for r in self.rows()],
                         [("claude", ALICE, 5), ("claude", BOB, 5)])
        self.assertEqual([(r["account"], r["plan"]) for r in self.rows("usage_plan_history")],
                         [(ALICE, "Max"), (BOB, "Pro")])

    def test_same_email_slots_record_once_per_round_first_by_name(self):
        a = self.add_claude("a", email=ALICE)
        b = self.add_claude("b", email=ALICE)
        calls = []

        def fn(slot_, now=None):
            calls.append(slot_["key"])
            return {**self.data("Max" if slot_["key"] == a else "Pro", 5 if slot_["key"] == a else 6)}
        with mock.patch.object(usage, "collect_claude", side_effect=fn):
            snap = usage.refresh(("claude",))
            usage.refresh(("claude",))
        self.assertEqual(calls, [a, b, a, b])                                # 둘 다 수집·표시한다
        self.assertEqual([i["plan"] for i in snap["claude"]], ["Max", "Pro"])
        self.assertEqual([(r["pct"], r["account"]) for r in self.rows()], [(5, ALICE)])       # 이력은 첫 슬롯만
        self.assertEqual([r["plan"] for r in self.rows("usage_plan_history")], ["Max"])

    def test_same_email_second_slot_records_when_first_slot_fails(self):
        a = self.add_claude("a", email=ALICE)
        b = self.add_claude("b", email=ALICE)
        with mock.patch.object(usage, "collect_claude",
                               side_effect=self.collect_by({b: "Pro"}, fail={a: usage.UsageError("x")})):
            usage.refresh(("claude",))
        self.assertEqual([r["account"] for r in self.rows()], [ALICE])
        self.assertEqual([r["plan"] for r in self.rows("usage_plan_history")], ["Pro"])

    def test_unknown_account_is_shown_but_not_recorded(self):
        folder = self.home / ".claude-x"
        folder.mkdir()
        (folder / ".claude.json").write_text(json.dumps({"oauthAccount": {"organizationType": "claude_max"}}))
        with mock.patch.object(usage, "collect_claude", side_effect=self.collect_by()):
            snap = usage.refresh(("claude",))
        self.assertEqual((snap["claude"][0]["plan"], snap["claude"][0]["email"]), ("Max", ""))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows("usage_plan_history"), [])

    def test_api_slots_are_never_collected_woken_or_recorded(self):
        self.add_claude("org", org_type="api_individual", org_name="Acme Console")
        self.add_codex("key", api_key="sk-test-fake-codex-key")
        good = self.add_claude("ok", email=ALICE)
        with mock.patch.object(usage, "collect_claude", side_effect=self.collect_by()) as cl, \
                mock.patch.object(usage, "collect_codex") as cx, \
                mock.patch.object(usage.subprocess, "run") as run, \
                mock.patch.object(usage, "codex_rpc") as rpc:
            snap = usage.refresh()
        self.assertEqual([c.args[0]["key"] for c in cl.call_args_list], [good])      # API 슬롯은 건너뜀
        cx.assert_not_called()
        run.assert_not_called()
        rpc.assert_not_called()
        api = next(i for i in snap["claude"] if i["label"] == "org")
        self.assertEqual((api["api"], api["windows"], api["error"]), (True, [], "API 로그인 — 구독 한도 없음"))
        self.assertEqual(snap["codex"][0]["error"], "API 로그인 — 구독 한도 없음")
        self.assertEqual({r["account"] for r in self.rows()}, {ALICE})

    def test_failure_of_one_slot_does_not_block_others_or_codex(self):
        a = self.add_claude("a", email=ALICE)
        b = self.add_claude("b", email=BOB)
        self.add_codex("x", email=CAROL)
        cdata = {**self.data("Prolite", 1, "7d")}
        with mock.patch.object(usage, "collect_claude",
                               side_effect=self.collect_by({b: "Pro"}, fail={a: usage.UsageError("키체인 없음")})), \
                mock.patch.object(usage, "collect_codex", return_value=cdata) as cx:
            snap = usage.refresh()
        by_key = {i["key"]: i for i in snap["claude"]}
        self.assertEqual((by_key[a]["error"], by_key[a]["windows"]), ("키체인 없음", []))
        self.assertEqual((by_key[b]["error"], by_key[b]["plan"]), ("", "Pro"))
        self.assertEqual(cx.call_count, 1)
        self.assertEqual(snap["codex"][0]["plan"], "Prolite")
        self.assertEqual({r["account"] for r in self.rows()}, {BOB, CAROL})

    def test_unexpected_exception_is_contained_and_keeps_old_value(self):
        a = self.add_claude("a", email=ALICE)
        b = self.add_claude("b", email=BOB)
        with mock.patch.object(usage, "collect_claude", side_effect=self.collect_by()):
            usage.refresh(("claude",))
        with mock.patch.object(usage, "collect_claude",
                               side_effect=self.collect_by(fail={a: ValueError("parse")})), \
                self.assertLogs(usage.log, "ERROR"):
            snap = usage.refresh(("claude",))
        by_key = {i["key"]: i for i in snap["claude"]}
        self.assertIn("내부 오류", by_key[a]["error"])
        self.assertEqual(by_key[a]["plan"], "Max")                         # 옛 값 유지
        self.assertEqual(by_key[b]["error"], "")

    def test_success_clears_error(self):
        a = self.add_claude("a")
        with mock.patch.object(usage, "collect_claude", side_effect=self.collect_by()):
            usage.refresh(("claude",))
        with mock.patch.object(usage, "collect_claude", side_effect=self.collect_by(fail={a: usage.UsageError("x")})):
            self.assertEqual(usage.refresh(("claude",))["claude"][0]["error"], "x")
        with mock.patch.object(usage, "collect_claude", side_effect=self.collect_by()):
            self.assertEqual(usage.refresh(("claude",))["claude"][0]["error"], "")

    def test_rescan_each_round_picks_up_new_and_removed_slots(self):
        a = self.add_claude("a")
        with mock.patch.object(usage, "collect_claude", side_effect=self.collect_by()):
            self.assertEqual([i["key"] for i in usage.refresh(("claude",))["claude"]], [a])
            b = self.add_claude("b", email=BOB)
            self.assertEqual([i["key"] for i in usage.refresh(("claude",))["claude"]], [a, b])
            (self.home / ".claude-a" / ".claude.json").unlink()
            self.assertEqual([i["key"] for i in usage.refresh(("claude",))["claude"]], [b])

    def test_collects_in_name_order(self):
        keys = [self.add_claude(n, email=f"{n}@example.com") for n in ("c", "a", "b")]
        order = []

        def fn(slot_, now=None):
            order.append(slot_["label"])
            return {**self.data(), "key": slot_["key"]}
        with mock.patch.object(usage, "collect_claude", side_effect=fn):
            usage.refresh(("claude",))
        self.assertEqual(order, ["a", "b", "c"])
        self.assertEqual(len(keys), 3)

    def test_history_write_failure_does_not_lose_snapshot(self):
        self.add_claude("a")
        with mock.patch.object(usage, "collect_claude", side_effect=self.collect_by()), \
                mock.patch.object(usage, "record_history", side_effect=sqlite3.OperationalError("locked")), \
                self.assertLogs(usage.log, "ERROR"):
            usage.refresh(("claude",))
        self.assertEqual(usage.snapshot()["claude"][0]["plan"], "Max")

    def test_refresh_passes_the_discovered_slot_to_collect(self):
        a = self.add_claude("a", email=ALICE)
        with mock.patch.object(usage, "collect_claude", side_effect=self.collect_by()) as cl:
            usage.refresh(("claude",))
        self.assertEqual(cl.call_args.args[0], {"key": a, "label": "a", "account": ALICE})

    def test_email_never_reaches_logs_or_errors(self):
        a = self.add_claude("a", email=ALICE)
        x = self.add_codex("x", email=BOB)
        with captured_logs() as lines:
            with mock.patch.object(usage, "collect_claude", side_effect=self.collect_by()), \
                    mock.patch.object(usage, "collect_codex", side_effect=usage.UsageError("timeout")):
                usage.refresh()
            with mock.patch.object(usage, "collect_claude", side_effect=self.collect_by(fail={a: RuntimeError("boom")})), \
                    mock.patch.object(usage, "collect_codex", side_effect=RuntimeError("boom")):
                usage.refresh()
        joined = "\n".join(lines) + json.dumps([usage._st(a)["error"], usage._st(x)["error"]])
        self.assertTrue(lines)
        for needle in (ALICE, BOB, "alice", "bob@"):
            self.assertNotIn(needle, joined)


# ── 8. Codex 슬롯별 수집 ──────────────────────────────

XA, XB = "/home/u/.codex-a", "/home/u/.codex-b"
CX_A, CX_B = slot("codex", XA, ALICE), slot("codex", XB, BOB)


def rpc_result(pct=10):
    return {"rateLimits": {"planType": "plus",
                           "primary": {"usedPercent": pct, "windowDurationMins": 10080, "resetsAt": time.time() + 3600}}}


class CodexSlotTests(AccountsBase):
    def test_rpc_runs_per_slot_with_that_codex_home(self):
        with mock.patch.object(usage, "codex_rpc", return_value=rpc_result()) as rpc:
            usage.collect_codex(CX_A)
            usage.collect_codex(CX_B)
        self.assertEqual([c.kwargs for c in rpc.call_args_list], [{"codex_home": XA}, {"codex_home": XB}])

    def test_collect_result_carries_slot_identity_and_no_email_field(self):
        with mock.patch.object(usage, "codex_rpc", return_value=rpc_result(12)):
            d = usage.collect_codex(CX_B)
        self.assertEqual((d["key"], d["label"], d["account"], d["plan"], d["login_ends"]), (XB, "b", BOB, "Plus", None))
        self.assertNotIn("email", d)
        self.assertEqual(d["windows"][0]["pct"], 12)

    def test_account_read_is_not_requested_and_email_comes_from_the_slot(self):
        # accountRead가 응답에 와도 이메일로 쓰지 않는다(이메일은 로컬 파일 규칙으로만)
        result = {**rpc_result(), "accountRead": {"account": {"email": CAROL}}}
        with mock.patch.object(usage, "codex_rpc", return_value=result):
            d = usage.collect_codex(CX_A)
        self.assertNotIn(CAROL, json.dumps(d))
        self.assertFalse(hasattr(usage, "parse_codex_account"))
        self.assertFalse(hasattr(usage, "CODEX_ACCOUNT_GRACE"))

    def test_backoff_is_per_slot(self):
        now = 1793770000.0
        with mock.patch.object(usage, "codex_rpc", side_effect=usage.UsageError("down")):
            with self.assertRaises(usage.UsageError):
                usage.collect_codex(CX_A, now)
        with mock.patch.object(usage, "codex_rpc", return_value=rpc_result()) as rpc:
            with self.assertRaisesRegex(usage.UsageError, "down"):
                usage.collect_codex(CX_A, now + 60)                    # A는 백오프 중
            rpc.assert_not_called()
            usage.collect_codex(CX_B, now + 60)                        # B는 영향 없음
        rpc.assert_called_once_with(codex_home=XB)
        self.assertEqual((usage._st(XA)["rpc_until"], usage._st(XB)["rpc_until"]), (now + usage.CODEX_BACKOFF_SEC, 0.0))

    def test_rpc_failure_never_falls_back_to_the_default_login(self):
        # 기본 로그인(~/.codex/auth.json)이 BOB이고 전사본이 있어도 슬롯 B의 RPC 실패는 대체 없이 오류가 된다
        self.default_codex(BOB)
        sessions = self.home / ".codex" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "rollout.jsonl").write_text(json.dumps({"timestamp": "2026-11-25T00:00:00Z", "payload": {
            "type": "token_count", "rate_limits": {"primary": {"used_percent": 40, "window_minutes": 10080,
                                                               "resets_at": time.time() + 3600}}}}) + "\n")
        with mock.patch.object(usage, "codex_rpc", side_effect=usage.UsageError("down")) as rpc:
            with self.assertRaisesRegex(usage.UsageError, "^Codex 읽기 실패: down — 5분마다 재시도$"):
                usage.collect_codex(CX_B)
        rpc.assert_called_once_with(codex_home=XB)

    def test_refresh_records_each_codex_email_once(self):
        self.add_codex("a", email=ALICE)
        self.add_codex("b", email=BOB)
        self.add_codex("c", email=ALICE)                                # 같은 이메일 — 한 회차 1회
        with mock.patch.object(usage, "codex_rpc", return_value=rpc_result(7)):
            snap = usage.refresh(("codex",))
        self.assertEqual([i["label"] for i in snap["codex"]], ["a", "b", "c"])
        rows = self.conn.execute("SELECT provider, account FROM usage_history ORDER BY id").fetchall()
        self.assertEqual([(r["provider"], r["account"]) for r in rows], [("codex", ALICE), ("codex", BOB)])
        plans = self.conn.execute("SELECT account, plan FROM usage_plan_history ORDER BY id").fetchall()
        self.assertEqual([(r["account"], r["plan"]) for r in plans], [(ALICE, "Plus"), (BOB, "Plus")])


class CodexRpcEnvTests(unittest.TestCase):
    """codex_rpc — Popen만 대체한다(실제 codex 실행 없음)."""

    def run_rpc(self, **kw):
        lines = iter([json.dumps({"id": 1, "result": {}}) + "\n",
                      json.dumps({"id": 2, "result": {"rateLimits": {"planType": "plus"}}}) + "\n"])
        proc = mock.Mock()
        proc.stdout = lines
        proc.wait.return_value = 0
        with mock.patch.object(usage, "_codex_bin", return_value="/fake/bin/codex"), \
                mock.patch.object(usage.subprocess, "Popen", return_value=proc) as popen:
            result = usage.codex_rpc(timeout=5, **kw)
        return result, popen, proc

    def test_codex_home_is_set_for_the_slot_and_hub_value_is_replaced(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/hub/leak"}):
            result, popen, proc = self.run_rpc(codex_home=XA)
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["CODEX_HOME"], XA)
        self.assertTrue(env["PATH"].startswith("/fake/bin"))
        self.assertEqual(popen.call_args.args[0], ["/fake/bin/codex", "app-server", "--stdio"])
        self.assertEqual(result, {"rateLimits": {"planType": "plus"}})
        proc.terminate.assert_called_once_with()

    def test_without_codex_home_the_environment_is_unchanged(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/hub/value"}):
            _, popen, _ = self.run_rpc()
        self.assertEqual(popen.call_args.kwargs["env"]["CODEX_HOME"], "/hub/value")

    def test_only_rate_limits_are_requested(self):
        _, _, proc = self.run_rpc(codex_home=XA)
        sent = [json.loads(c.args[0]) for c in proc.stdin.write.call_args_list]
        self.assertEqual([m["method"] for m in sent], ["initialize", "initialized", "account/rateLimits/read"])


# ── 9. 시작 순서 ─────────────────────────────────────

class LoopStartupTests(unittest.TestCase):
    class Stop(Exception):
        pass

    def test_loop_loads_persisted_then_collects_without_any_history_adoption(self):
        order = []
        with mock.patch.object(usage, "refresh", side_effect=lambda *a: order.append("refresh") or {}), \
                mock.patch.object(usage, "load_persisted", side_effect=lambda: order.append("load")):
            with self.assertRaises(self.Stop):
                usage.loop(sleep=mock.Mock(side_effect=self.Stop), clock=lambda: 0.0)
        self.assertEqual(order, ["load", "refresh"])
        self.assertFalse(hasattr(usage, "adopt_legacy_history"))


# ── 10. 엔드포인트·설정·수집기 파일 ─────────────────────

class HubSurfaceTests(unittest.TestCase):
    def test_account_rule_is_served_to_collectors(self):
        from server import app as hub
        self.assertIn("account.py", hub.COLLECTOR_FILES)
        self.assertTrue((hub.COLLECTOR_DIR / "account.py").is_file())

    def state_for(self, kind):
        from server import app as hub
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        db.migrate(conn)
        self.addCleanup(conn.close)

        @contextlib.contextmanager
        def tx():
            yield conn
        snap = {"claude": [{"key": "/Users/x/.claude-a", "email": ALICE}], "codex": []}
        with mock.patch.object(hub, "_require", return_value={"kind": kind, "device": None}), \
                mock.patch.object(hub.db, "tx", tx), \
                mock.patch("server.state.usage.snapshot", return_value=snap), \
                mock.patch.object(hub.CFG, "usage_enabled", True, create=True):
            return asyncio.run(hub.get_state(mock.MagicMock()))

    def test_device_token_gets_no_usage_slots_but_dashboard_does(self):
        out = self.state_for("device")
        self.assertIsNone(out["kpi"]["usage"])
        self.assertNotIn(ALICE, json.dumps(out))
        self.assertNotIn("/Users/x", json.dumps(out))
        self.assertEqual(self.state_for("admin")["kpi"]["usage"]["claude"][0]["email"], ALICE)
        self.assertEqual(self.state_for("local")["kpi"]["usage"]["claude"][0]["email"], ALICE)

    def test_state_kpi_usage_is_the_snapshot_shape(self):
        # /api/state의 kpi.usage는 usage.snapshot() 그대로 — {"claude": [Slot], "codex": [Slot]}
        with mock.patch.object(usage, "_slots", {"claude": [dict(SLOT_A)], "codex": []}), \
                mock.patch.object(usage, "_snap", {"claude": {}, "codex": {}}), \
                mock.patch.object(usage, "_slot_state", {}):
            snap = usage.snapshot()
        self.assertEqual(set(snap), {"claude", "codex"})
        self.assertEqual((len(snap["claude"]), snap["codex"]), (1, []))


# ── 11. DB 마이그레이션(usage 표) ──────────────────────

OLD_USAGE_SCHEMA = """
CREATE TABLE usage_history (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  provider TEXT NOT NULL,
  win TEXT NOT NULL,
  pct INTEGER NOT NULL,
  resets_at REAL
);
CREATE INDEX idx_usage_history ON usage_history(provider, win, ts);
CREATE TABLE usage_plan_history (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  provider TEXT NOT NULL,
  plan TEXT NOT NULL
);
CREATE INDEX idx_usage_plan_history ON usage_plan_history(provider, ts);
"""


class MigrationTests(unittest.TestCase):
    def make_old(self, c):
        c.executescript(OLD_USAGE_SCHEMA)
        c.executemany("INSERT INTO usage_history (ts, provider, win, pct, resets_at) VALUES (?,?,?,?,?)",
                      [("2026-11-01T00:00:00Z", "claude", "5h", 10, 4.0e9), ("2026-11-01T01:00:00Z", "claude", "5h", 20, None),
                       ("2026-11-01T00:00:00Z", "codex", "7d", 3, None)])
        c.executemany("INSERT INTO usage_plan_history (ts, provider, plan) VALUES (?,?,?)",
                      [("2026-10-01T00:00:00Z", "claude", "Max"), ("2026-10-01T00:00:00Z", "codex", "Prolite")])
        c.commit()

    @staticmethod
    def columns(c, table):
        return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]

    @staticmethod
    def indexes(c):
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}

    def assert_migrated(self, c):
        for table in ("usage_history", "usage_plan_history"):
            self.assertIn("account", self.columns(c, table))
            self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {table} WHERE account != ''").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM usage_history").fetchone()[0], 3)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM usage_plan_history").fetchone()[0], 2)
        idx = self.indexes(c)
        self.assertIn("idx_usage_history_acct", idx)
        self.assertIn("idx_usage_plan_history_acct", idx)
        self.assertNotIn("idx_usage_history", idx)            # 계정 열 없는 옛 인덱스는 대체됐다
        self.assertNotIn("idx_usage_plan_history", idx)

    def test_old_schema_is_migrated_in_memory(self):
        c = sqlite3.connect(":memory:")
        self.addCleanup(c.close)
        self.make_old(c)
        self.assertNotIn("account", self.columns(c, "usage_history"))
        db.migrate(c)
        self.assert_migrated(c)

    def test_migration_is_idempotent(self):
        c = sqlite3.connect(":memory:")
        self.addCleanup(c.close)
        self.make_old(c)
        db.migrate(c)
        before = (c.execute("SELECT * FROM usage_history ORDER BY id").fetchall(), self.indexes(c))
        db.migrate(c)
        self.assertEqual(before, (c.execute("SELECT * FROM usage_history ORDER BY id").fetchall(), self.indexes(c)))
        self.assert_migrated(c)

    def test_fresh_database_gets_account_columns_and_indexes(self):
        c = sqlite3.connect(":memory:")
        self.addCleanup(c.close)
        db.migrate(c)
        self.assertIn("account", self.columns(c, "usage_history"))
        self.assertIn("account", self.columns(c, "usage_plan_history"))
        self.assertTrue({"idx_usage_history_acct", "idx_usage_plan_history_acct"} <= self.indexes(c))

    def test_old_file_database_via_real_startup_path(self):
        """db.conn()(허브 기동 경로: 연결 → migrate) — 임시 파일 DB, 실제 data/madison.db는 쓰지 않는다."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            raw = sqlite3.connect(path)
            self.make_old(raw)
            raw.close()
            with mock.patch.object(db.CFG, "db_path", path), mock.patch.object(db, "_conn", None):
                try:
                    c = db.conn()
                    self.assertEqual(Path(c.execute("PRAGMA database_list").fetchone()[2]).resolve(), path.resolve())
                    self.assert_migrated(c)
                finally:
                    if db._conn is not None:
                        db._conn.close()

    def test_migrated_rows_keep_a_blank_account_and_nothing_adopts_them(self):
        """마이그레이션이 만든 account='' 행은 그대로 둔다 — 기본 로그인으로 편입하지 않는다(운영은 이미 편입 완료)."""
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        self.addCleanup(c.close)
        self.make_old(c)
        db.migrate(c)
        self.assertEqual([r["account"] for r in c.execute("SELECT account FROM usage_history WHERE provider='claude'")], ["", ""])
        self.assertFalse(hasattr(usage, "adopt_legacy_history"))

    def test_account_indexes_are_not_in_base_schema(self):
        """옛 DB에서 열이 생기기 전에 account 인덱스를 만들면 기동이 실패한다."""
        self.assertNotIn("idx_usage_history_acct", db._BASE_SCHEMA)
        self.assertNotIn("idx_usage_plan_history_acct", db._BASE_SCHEMA)
        self.assertIn("idx_usage_history_acct", db._USAGE_INDEXES)
        self.assertIn("idx_usage_plan_history_acct", db._USAGE_INDEXES)


if __name__ == "__main__":
    unittest.main()
