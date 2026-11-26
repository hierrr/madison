import base64
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "collector/account.py"
spec = importlib.util.spec_from_file_location("collector_account", SCRIPT)
account = importlib.util.module_from_spec(spec)
spec.loader.exec_module(account)

# Fake credentials only.
FAKE_TOKEN = "fake-auth-token-0001"
FAKE_KEY = "sk-ant-fake-key-AAAAAAAAAAAAAAAAAAAA-tail0123456789abcd"
FAKE_OPENAI_KEY = "sk-fake-openai-key-0001"
SCRUBBED = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENAI_API_KEY")


def sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()


def b64url(data, pad=False):
    s = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    return s if pad else s.rstrip("=")


def jwt(claims, pad=False):
    return ".".join([b64url({"alg": "none"}, pad), b64url(claims, pad), "c2ln"])


def clean_env(**extra):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in SCRUBBED and not k.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_"))
    }
    env.update(extra)
    return env


class AccountTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        # Never let a real environment value reach resolve().
        self.env = {}

    def write_login(self, data, directory=None, raw=None):
        path = Path(directory or self.home) / ".claude.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw if raw is not None else json.dumps(data))
        return path

    def claude(self, env=None, **kw):
        return account.resolve("claude-code", self.env if env is None else env, home=self.home, **kw)

    def subscription(self, email="User@Example.com", org_type="claude_max", name="Org"):
        return {
            "oauthAccount": {
                "emailAddress": email,
                "organizationType": org_type,
                "organizationName": name,
                "billingType": "stripe_subscription",
            }
        }

    def oauth_login(self, **fields):
        """oauthAccount with exactly the given keys (fake values only)."""
        return {"oauthAccount": dict(fields)}


class NormalizeTests(unittest.TestCase):
    def test_non_string(self):
        for v in (None, 1, b"a@b.c", ["a@b.c"], {}):
            self.assertEqual(account.normalize(v), "")

    def test_email_lowercased_and_spaces_removed(self):
        self.assertEqual(account.normalize("  User @ Example.COM\t\n"), "user@example.com")

    def test_no_at_sign(self):
        self.assertEqual(account.normalize("plainname"), "")
        self.assertEqual(account.normalize(""), "")

    def test_api_keeps_case_collapses_spaces(self):
        self.assertEqual(account.normalize("api:  My   Org\tName "), "api:My Org Name")
        self.assertEqual(account.normalize("api:Bedrock"), "api:Bedrock")

    def test_api_empty_rest(self):
        self.assertEqual(account.normalize("api:"), "")
        self.assertEqual(account.normalize("api:   "), "")

    def test_api_does_not_need_at_sign(self):
        self.assertEqual(account.normalize("api:abcd1234"), "api:abcd1234")

    def test_truncates_to_200(self):
        self.assertEqual(len(account.normalize("api:" + "x" * 500)), 200)
        self.assertEqual(len(account.normalize("a" * 500 + "@example.com")), 200)

    def test_sha8(self):
        self.assertEqual(account.sha8("abc"), sha256("abc")[:8])
        self.assertRegex(account.sha8("Z"), r"^[0-9a-f]{8}$")

    def test_predicates(self):
        self.assertTrue(account.is_subscription("a@example.com"))
        self.assertFalse(account.is_subscription("api:x"))
        self.assertFalse(account.is_subscription(""))
        self.assertFalse(account.is_subscription(None))
        self.assertTrue(account.is_api("api:x"))
        self.assertFalse(account.is_api("a@example.com"))
        self.assertFalse(account.is_api(""))
        self.assertFalse(account.is_api(None))


class ClaudeRuleTests(AccountTestCase):
    def test_unknown_agent(self):
        self.assertEqual(account.resolve("other", {}, home=self.home), "")
        self.assertEqual(account.resolve(None, {}, home=self.home), "")

    def test_cloud_flags_truthy_values(self):
        for name, label in (
            ("CLAUDE_CODE_USE_BEDROCK", "api:bedrock"),
            ("CLAUDE_CODE_USE_VERTEX", "api:vertex"),
            ("CLAUDE_CODE_USE_FOUNDRY", "api:foundry"),
        ):
            for value in ("1", "true", "TRUE", " Yes ", "on"):
                with self.subTest(name=name, value=value):
                    self.assertEqual(self.claude({name: value}), label)

    def test_cloud_flags_falsy_values(self):
        self.write_login(self.subscription())
        for value in ("0", "false", "", "  ", "off", "no", "2"):
            with self.subTest(value=value):
                self.assertEqual(self.claude({"CLAUDE_CODE_USE_BEDROCK": value}), "user@example.com")

    def test_cloud_beats_token_and_key(self):
        env = {
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_AUTH_TOKEN": FAKE_TOKEN,
            "ANTHROPIC_API_KEY": FAKE_KEY,
        }
        self.assertEqual(self.claude(env), "api:vertex")

    def test_cloud_order(self):
        env = {"CLAUDE_CODE_USE_FOUNDRY": "1", "CLAUDE_CODE_USE_BEDROCK": "1"}
        self.assertEqual(self.claude(env), "api:bedrock")

    def test_auth_token_beats_api_key(self):
        env = {"ANTHROPIC_AUTH_TOKEN": FAKE_TOKEN, "ANTHROPIC_API_KEY": FAKE_KEY}
        self.assertEqual(self.claude(env), "api:" + account.sha8(FAKE_TOKEN))

    def test_auth_token_beats_login(self):
        self.write_login(self.subscription())
        self.assertEqual(
            self.claude({"ANTHROPIC_AUTH_TOKEN": FAKE_TOKEN}), "api:" + account.sha8(FAKE_TOKEN)
        )

    def test_empty_token_falls_through_to_key(self):
        env = {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": FAKE_KEY}
        self.assertEqual(self.claude(env), "api:" + account.sha8(FAKE_KEY))

    def test_api_key(self):
        self.assertEqual(
            self.claude({"ANTHROPIC_API_KEY": FAKE_KEY}), "api:" + account.sha8(FAKE_KEY)
        )

    def test_api_key_beats_login(self):
        self.write_login(self.subscription())
        self.assertEqual(
            self.claude({"ANTHROPIC_API_KEY": FAKE_KEY}), "api:" + account.sha8(FAKE_KEY)
        )

    def rejected_login(self):
        data = self.subscription()
        data["customApiKeyResponses"] = {"approved": [], "rejected": [FAKE_KEY[-20:]]}
        return data

    def test_rejected_key_ignored_when_interactive(self):
        self.write_login(self.rejected_login())
        self.assertEqual(self.claude({"ANTHROPIC_API_KEY": FAKE_KEY}), "user@example.com")

    def test_rejected_key_still_used_when_headless(self):
        self.write_login(self.rejected_login())
        self.assertEqual(
            self.claude({"ANTHROPIC_API_KEY": FAKE_KEY}, headless=True),
            "api:" + account.sha8(FAKE_KEY),
        )

    def test_other_key_not_rejected(self):
        self.write_login(self.rejected_login())
        other = "sk-ant-fake-other-key-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
        self.assertEqual(self.claude({"ANTHROPIC_API_KEY": other}), "api:" + account.sha8(other))

    def test_approved_key_is_used(self):
        data = self.subscription()
        data["customApiKeyResponses"] = {"approved": [FAKE_KEY[-20:]], "rejected": []}
        self.write_login(data)
        self.assertEqual(
            self.claude({"ANTHROPIC_API_KEY": FAKE_KEY}), "api:" + account.sha8(FAKE_KEY)
        )

    def test_rejected_key_without_login_oauth(self):
        self.write_login({"customApiKeyResponses": {"rejected": [FAKE_KEY[-20:]]}})
        self.assertEqual(self.claude({"ANTHROPIC_API_KEY": FAKE_KEY}), "")

    def test_key_without_login_file_is_api(self):
        self.assertEqual(
            self.claude({"ANTHROPIC_API_KEY": FAKE_KEY}), "api:" + account.sha8(FAKE_KEY)
        )

    def test_subscription_claude_max(self):
        self.write_login(self.subscription())
        self.assertEqual(self.claude(), "user@example.com")

    def test_subscription_other_claude_prefix(self):
        self.write_login(self.subscription(org_type="claude_team"))
        self.assertEqual(self.claude(), "user@example.com")

    def test_subscription_empty_organization_type(self):
        self.write_login(self.subscription(org_type=""))
        self.assertEqual(self.claude(), "user@example.com")

    def test_subscription_missing_organization_type(self):
        self.write_login({"oauthAccount": {"emailAddress": "a@example.com"}})
        self.assertEqual(self.claude(), "a@example.com")

    def test_console_organization(self):
        self.write_login(self.subscription(org_type="api", name="  Example   Console Org "))
        self.assertEqual(self.claude(), "api:Example Console Org")

    def test_console_organization_without_name(self):
        self.write_login(self.subscription(org_type="api", name=""))
        self.assertEqual(self.claude(), "")

    def test_console_login_without_organization_type(self):
        self.write_login(self.oauth_login(
            emailAddress="dev@example.com", organizationName="Acme", billingType="prepaid"))
        self.assertEqual(self.claude(), "api:Acme")

    def test_console_login_without_organization_type_or_name(self):
        self.write_login(self.oauth_login(emailAddress="dev@example.com", billingType="prepaid"))
        self.assertEqual(self.claude(), "")

    def test_console_login_billing_type_is_trimmed_and_lowercased(self):
        self.write_login(self.oauth_login(
            emailAddress="dev@example.com", organizationName="Acme", billingType="  PrePaid "))
        self.assertEqual(self.claude(), "api:Acme")

    def test_missing_organization_type_stripe_subscription(self):
        self.write_login(self.oauth_login(
            emailAddress="Dev@Example.com", organizationName="Acme", billingType="stripe_subscription"))
        self.assertEqual(self.claude(), "dev@example.com")

    def test_missing_organization_type_apple_subscription(self):
        self.write_login(self.oauth_login(
            emailAddress="dev@example.com", organizationName="Acme", billingType=" Apple_Subscription "))
        self.assertEqual(self.claude(), "dev@example.com")

    def test_missing_organization_type_and_billing_type(self):
        self.write_login(self.oauth_login(emailAddress="dev@example.com", organizationName="Acme"))
        self.assertEqual(self.claude(), "dev@example.com")

    def test_blank_organization_type_and_billing_type(self):
        self.write_login(self.oauth_login(
            emailAddress="dev@example.com", organizationType="  ", billingType=" "))
        self.assertEqual(self.claude(), "dev@example.com")

    def test_organization_type_beats_billing_type(self):
        # A present organizationType decides on its own, whatever billingType says.
        self.write_login(self.oauth_login(
            emailAddress="dev@example.com", organizationName="Acme",
            organizationType="claude_team", billingType="prepaid"))
        self.assertEqual(self.claude(), "dev@example.com")
        self.write_login(self.oauth_login(
            emailAddress="dev@example.com", organizationName="Acme",
            organizationType="api", billingType="stripe_subscription"))
        self.assertEqual(self.claude(), "api:Acme")

    def test_api_organization_types_are_api(self):
        # api, api_team, api_individual, ... all start with "api" (trimmed, any case).
        for org_type in ("api", "api_team", "api_individual", " API_Team "):
            with self.subTest(org_type=org_type):
                self.write_login(self.oauth_login(
                    emailAddress="dev@example.com", organizationName="Acme", organizationType=org_type))
                self.assertEqual(self.claude(), "api:Acme")

    def test_api_team_with_prepaid(self):
        self.write_login(self.oauth_login(
            emailAddress="dev@example.com", organizationName="Acme",
            organizationType="api_team", billingType="prepaid"))
        self.assertEqual(self.claude(), "api:Acme")

    def test_api_individual(self):
        self.write_login(self.oauth_login(
            emailAddress="dev@example.com", organizationName="Acme", organizationType="api_individual"))
        self.assertEqual(self.claude(), "api:Acme")

    def test_api_organization_type_without_name(self):
        for org_type in ("api", "api_team"):
            with self.subTest(org_type=org_type):
                self.write_login(self.oauth_login(emailAddress="dev@example.com", organizationType=org_type))
                self.assertEqual(self.claude(), "")

    def test_claude_team_and_enterprise_are_subscriptions(self):
        for org_type in ("claude_team", "claude_enterprise", " Claude_Pro "):
            with self.subTest(org_type=org_type):
                self.write_login(self.oauth_login(
                    emailAddress="Dev@Example.com", organizationName="Acme",
                    organizationType=org_type, billingType="prepaid"))
                self.assertEqual(self.claude(), "dev@example.com")

    def test_unknown_organization_type_with_subscription_billing(self):
        for org_type in ("console", " Enterprise_API ", "team", "something_new"):
            with self.subTest(org_type=org_type):
                self.write_login(self.oauth_login(
                    emailAddress="Dev@Example.com", organizationName="Acme",
                    organizationType=org_type, billingType="stripe_subscription"))
                self.assertEqual(self.claude(), "dev@example.com")

    def test_unknown_organization_type_with_prepaid_billing(self):
        for org_type in ("console", "something_new"):
            with self.subTest(org_type=org_type):
                self.write_login(self.oauth_login(
                    emailAddress="dev@example.com", organizationName="Acme",
                    organizationType=org_type, billingType="prepaid"))
                self.assertEqual(self.claude(), "api:Acme")

    def test_unknown_organization_type_with_prepaid_billing_without_name(self):
        self.write_login(self.oauth_login(
            emailAddress="dev@example.com", organizationType="something_new", billingType="prepaid"))
        self.assertEqual(self.claude(), "")

    def test_unknown_organization_type_without_billing_type(self):
        for org_type in ("console", "something_new"):
            with self.subTest(org_type=org_type):
                self.write_login(self.oauth_login(
                    emailAddress="Dev@Example.com", organizationName="Acme", organizationType=org_type))
                self.assertEqual(self.claude(), "dev@example.com")

    def test_oauth_not_dict(self):
        for value in ("a@example.com", None, [], 3):
            with self.subTest(value=value):
                self.write_login({"oauthAccount": value})
                self.assertEqual(self.claude(), "")

    def test_non_string_fields(self):
        self.write_login({"oauthAccount": {"emailAddress": 5, "organizationType": None}})
        self.assertEqual(self.claude(), "")

    def test_login_file_missing(self):
        self.assertEqual(self.claude(), "")

    def test_login_json_broken(self):
        self.write_login(None, raw="{not json")
        self.assertEqual(self.claude(), "")

    def test_login_json_not_object(self):
        self.write_login(None, raw="[1, 2]")
        self.assertEqual(self.claude(), "")

    def test_broken_json_does_not_hide_key(self):
        self.write_login(None, raw="{not json")
        self.assertEqual(
            self.claude({"ANTHROPIC_API_KEY": FAKE_KEY}), "api:" + account.sha8(FAKE_KEY)
        )

    def test_claude_config_dir(self):
        config = self.root / "alt-config"
        self.write_login(self.subscription(email="alt@example.com"), directory=config)
        self.write_login(self.subscription(email="home@example.com"))
        self.assertEqual(self.claude({"CLAUDE_CONFIG_DIR": str(config)}), "alt@example.com")
        self.assertEqual(self.claude(), "home@example.com")

    def test_claude_config_dir_does_not_fall_back_to_home(self):
        self.write_login(self.subscription(email="home@example.com"))
        self.assertEqual(self.claude({"CLAUDE_CONFIG_DIR": str(self.root / "missing")}), "")

    def test_home_defaults_to_path_home(self):
        self.write_login(self.subscription(email="default@example.com"))
        with patch.object(account.Path, "home", return_value=self.home):
            self.assertEqual(account.resolve("claude-code", {}), "default@example.com")

    def test_home_accepts_str(self):
        self.write_login(self.subscription())
        self.assertEqual(
            account.resolve("claude-code", {}, home=str(self.home)), "user@example.com"
        )

    def test_never_raises(self):
        class Boom(dict):
            def get(self, *a, **k):
                raise RuntimeError("boom")

        self.assertEqual(account.resolve("claude-code", Boom(), home=self.home), "")
        self.assertEqual(account.resolve("codex-cli", Boom(), home=self.home), "")
        self.assertEqual(account.resolve("claude-code", None), "")


class CodexRuleTests(AccountTestCase):
    def write_auth(self, data, directory=None, raw=None):
        path = Path(directory or self.home / ".codex") / "auth.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw if raw is not None else json.dumps(data))
        return path

    def codex(self, env=None):
        return account.resolve("codex-cli", self.env if env is None else env, home=self.home)

    def chatgpt(self, id_token, **extra):
        return {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": id_token,
                "access_token": "fake-access",
                "refresh_token": "fake-refresh",
                "account_id": "fake-account",
            },
            "last_refresh": "2026-01-01T00:00:00Z",
            **extra,
        }

    def test_chatgpt_email_claim(self):
        self.write_auth(self.chatgpt(jwt({"email": "Codex.User@Example.com"}, pad=True)))
        self.assertEqual(self.codex(), "codex.user@example.com")

    def test_jwt_without_padding(self):
        # Pick claims whose encoding needs padding so stripping it matters.
        for n in range(1, 5):
            claims = {"email": "u@example.com", "pad": "x" * n}
            token = jwt(claims)
            self.assertNotIn("=", token)
            with self.subTest(n=n):
                self.write_auth(self.chatgpt(token))
                self.assertEqual(self.codex(), "u@example.com")

    def test_profile_claim_fallback(self):
        claims = {"https://api.openai.com/profile": {"email": "Profile@Example.com"}}
        self.write_auth(self.chatgpt(jwt(claims)))
        self.assertEqual(self.codex(), "profile@example.com")

    def test_email_claim_beats_profile(self):
        claims = {"email": "a@example.com", "https://api.openai.com/profile": {"email": "b@example.com"}}
        self.write_auth(self.chatgpt(jwt(claims)))
        self.assertEqual(self.codex(), "a@example.com")

    def test_no_email_anywhere(self):
        self.write_auth(self.chatgpt(jwt({"sub": "x"})))
        self.assertEqual(self.codex(), "")

    def test_apikey_mode(self):
        self.write_auth({"auth_mode": "apikey", "OPENAI_API_KEY": FAKE_OPENAI_KEY})
        self.assertEqual(self.codex(), "api:" + account.sha8(FAKE_OPENAI_KEY))

    def test_apikey_mode_beats_tokens(self):
        data = self.chatgpt(jwt({"email": "a@example.com"}), auth_mode="apikey", OPENAI_API_KEY=FAKE_OPENAI_KEY)
        self.write_auth(data)
        self.assertEqual(self.codex(), "api:" + account.sha8(FAKE_OPENAI_KEY))

    def test_apikey_mode_without_key(self):
        self.write_auth({"auth_mode": "apikey", "OPENAI_API_KEY": None})
        self.assertEqual(self.codex(), "")

    def test_key_without_tokens(self):
        self.write_auth({"OPENAI_API_KEY": FAKE_OPENAI_KEY})
        self.assertEqual(self.codex(), "api:" + account.sha8(FAKE_OPENAI_KEY))

    def test_key_with_tokens_uses_tokens(self):
        data = self.chatgpt(jwt({"email": "a@example.com"}), OPENAI_API_KEY=FAKE_OPENAI_KEY)
        self.write_auth(data)
        self.assertEqual(self.codex(), "a@example.com")

    def test_no_tokens_no_key(self):
        self.write_auth({"auth_mode": "chatgpt"})
        self.assertEqual(self.codex(), "")

    def test_bad_id_token(self):
        for token in (None, 5, "", "onlyonepart", "a.%%%.c", "a." + b64url("not a dict") + ".c"):
            with self.subTest(token=token):
                self.write_auth(self.chatgpt(token))
                self.assertEqual(self.codex(), "")

    def test_payload_not_json(self):
        bad = base64.urlsafe_b64encode(b"\xff\xfe nope").decode().rstrip("=")
        self.write_auth(self.chatgpt("a." + bad + ".c"))
        self.assertEqual(self.codex(), "")

    def test_tokens_not_dict(self):
        self.write_auth({"auth_mode": "chatgpt", "tokens": "oops"})
        self.assertEqual(self.codex(), "")

    def test_codex_home(self):
        custom = self.root / "custom-codex"
        self.write_auth(self.chatgpt(jwt({"email": "custom@example.com"})), directory=custom)
        self.write_auth(self.chatgpt(jwt({"email": "home@example.com"})))
        self.assertEqual(self.codex({"CODEX_HOME": str(custom)}), "custom@example.com")
        self.assertEqual(self.codex(), "home@example.com")

    def test_file_missing(self):
        self.assertEqual(self.codex(), "")
        self.assertEqual(self.codex({"CODEX_HOME": str(self.root / "nope")}), "")

    def test_broken_json_and_non_object(self):
        self.write_auth(None, raw="{broken")
        self.assertEqual(self.codex(), "")
        self.write_auth(None, raw="[]")
        self.assertEqual(self.codex(), "")

    def test_claude_env_does_not_affect_codex(self):
        self.write_auth(self.chatgpt(jwt({"email": "a@example.com"})))
        self.assertEqual(self.codex({"ANTHROPIC_API_KEY": FAKE_KEY}), "a@example.com")


class CacheTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.cache = self.root / "cache"
        self.key_env = {"ANTHROPIC_API_KEY": FAKE_KEY}

    def login_data(self, email="user@example.com", rejected=None):
        data = self.subscription(email=email)
        data["customApiKeyResponses"] = {"rejected": rejected or []}
        return data

    def resolve(self, env=None, **kw):
        return self.claude(env, cache_dir=self.cache, **kw)

    def cache_files(self):
        return sorted(self.cache.glob("claude-login-*.json"))

    def count_login_parses(self, path, fn):
        calls = []
        real = json.loads

        def spy(s, *a, **k):
            calls.append(len(s) if hasattr(s, "__len__") else 0)
            return real(s, *a, **k)

        size = path.stat().st_size
        with patch.object(account.json, "loads", side_effect=spy):
            result = fn()
        parses = sum(1 for n in calls if n >= size)
        return result, parses

    def test_cache_file_name_and_content(self):
        path = self.write_login(self.login_data())
        self.assertEqual(self.resolve(), "user@example.com")
        files = self.cache_files()
        self.assertEqual(len(files), 1)
        digest = sha256(os.path.abspath(path))[:12]
        self.assertEqual(files[0].name, f"claude-login-{digest}.json")
        saved = json.loads(files[0].read_text())
        self.assertEqual(saved["email"], "user@example.com")
        self.assertEqual(saved["organizationType"], "claude_max")
        self.assertEqual(saved["organizationName"], "Org")

    def test_cache_stores_billing_type(self):
        self.write_login(self.oauth_login(
            emailAddress="dev@example.com", organizationName="Acme", billingType="prepaid"))
        self.assertEqual(self.resolve(), "api:Acme")
        saved = json.loads(self.cache_files()[0].read_text())
        self.assertEqual(saved["billingType"], "prepaid")
        self.assertEqual(saved["version"], account._CACHE_VERSION)
        # The decision is the same when it comes from the cache.
        self.assertEqual(self.resolve(), "api:Acme")

    def test_old_cache_without_billing_type_is_reparsed(self):
        data = self.oauth_login(
            emailAddress="dev@example.com", organizationName="Acme", billingType="prepaid")
        # Pad the file so the login parse is distinguishable from the cache read.
        data["projects"] = {f"/p{i}": {"history": ["x" * 50] * 5} for i in range(40)}
        path = self.write_login(data)
        self.assertEqual(self.resolve(), "api:Acme")
        cache_file = self.cache_files()[0]
        # Rewrite the cache the way the previous release stored it: version 1, no
        # billingType, same stamp.  The old rule read this as a subscription e-mail.
        saved = json.loads(cache_file.read_text())
        old = {k: saved[k] for k in ("stamp", "email", "organizationType", "organizationName", "rejected")}
        old["version"] = 1
        cache_file.write_text(json.dumps(old))
        result, parses = self.count_login_parses(path, self.resolve)
        self.assertEqual(result, "api:Acme")
        self.assertEqual(parses, 1)
        rewritten = json.loads(cache_file.read_text())
        self.assertEqual(rewritten["version"], account._CACHE_VERSION)
        self.assertEqual(rewritten["billingType"], "prepaid")

    def test_current_version_cache_missing_billing_type_is_reparsed(self):
        self.write_login(self.oauth_login(
            emailAddress="dev@example.com", organizationName="Acme", billingType="prepaid"))
        self.resolve()
        cache_file = self.cache_files()[0]
        saved = json.loads(cache_file.read_text())
        del saved["billingType"]
        cache_file.write_text(json.dumps(saved))
        self.assertEqual(self.resolve(), "api:Acme")

    def test_cache_still_hides_rejected_key_tail(self):
        self.write_login(self.login_data(rejected=[FAKE_KEY[-20:]]))
        self.resolve()
        self.assertNotIn(FAKE_KEY[-20:], self.cache_files()[0].read_text())

    def test_cache_hit_skips_parsing(self):
        # Pad the file so the login parse is distinguishable from the cache read.
        data = self.login_data(rejected=[FAKE_KEY[-20:]])
        data["projects"] = {f"/p{i}": {"history": ["x" * 50] * 5} for i in range(40)}
        path = self.write_login(data)
        first, parses1 = self.count_login_parses(path, self.resolve)
        self.assertEqual(first, "user@example.com")
        self.assertEqual(parses1, 1)
        second, parses2 = self.count_login_parses(path, self.resolve)
        self.assertEqual(second, "user@example.com")
        self.assertEqual(parses2, 0)

    def test_cache_hit_does_not_read_login_file(self):
        path = self.write_login(self.login_data())
        self.resolve()
        real = Path.read_text

        def guard(p, *a, **k):
            if Path(p) == path:
                raise AssertionError("login file was read despite a valid cache")
            return real(p, *a, **k)

        with patch.object(account.Path, "read_text", guard):
            self.assertEqual(self.resolve(), "user@example.com")

    def test_invalidated_by_mtime(self):
        path = self.write_login(self.login_data(email="old@example.com"))
        self.assertEqual(self.resolve(), "old@example.com")
        # Same size, different content, different mtime.
        path.write_text(json.dumps(self.login_data(email="new@example.com")))
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        self.assertEqual(self.resolve(), "new@example.com")

    def test_invalidated_by_size(self):
        path = self.write_login(self.login_data(email="old@example.com"))
        self.assertEqual(self.resolve(), "old@example.com")
        old = path.stat()
        path.write_text(json.dumps(self.login_data(email="much.longer.address@example.com")))
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))  # restore mtime, only size differs
        self.assertNotEqual(path.stat().st_size, old.st_size)
        self.assertEqual(self.resolve(), "much.longer.address@example.com")

    def test_cache_rewritten_after_invalidation(self):
        path = self.write_login(self.login_data(email="old@example.com"))
        self.resolve()
        path.write_text(json.dumps(self.login_data(email="new2@example.com")))
        os.utime(path, ns=(1, 1))
        self.resolve()
        self.assertEqual(json.loads(self.cache_files()[0].read_text())["email"], "new2@example.com")

    def test_cache_file_mode_0600(self):
        self.write_login(self.login_data())
        self.resolve()
        mode = stat.S_IMODE(self.cache_files()[0].stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_no_temp_files_left(self):
        self.write_login(self.login_data())
        self.resolve()
        self.assertEqual([p.name for p in self.cache.iterdir()], [self.cache_files()[0].name])

    def test_rejected_tail_not_stored_in_plaintext(self):
        self.write_login(self.login_data(rejected=[FAKE_KEY[-20:]]))
        self.assertEqual(self.resolve(self.key_env), "user@example.com")
        text = self.cache_files()[0].read_text()
        self.assertNotIn(FAKE_KEY[-20:], text)
        self.assertNotIn(FAKE_KEY, text)
        self.assertIn(sha256(FAKE_KEY[-20:]), json.loads(text)["rejected"])

    def test_rejection_works_from_cache(self):
        self.write_login(self.login_data(rejected=[FAKE_KEY[-20:]]))
        self.assertEqual(self.resolve(self.key_env), "user@example.com")
        self.assertEqual(self.resolve(self.key_env), "user@example.com")  # cache hit
        self.assertEqual(
            self.resolve(self.key_env, headless=True), "api:" + account.sha8(FAKE_KEY)
        )

    def test_cache_not_needed_for_env_only_decisions(self):
        self.write_login(self.login_data())
        self.assertEqual(self.resolve({"CLAUDE_CODE_USE_BEDROCK": "1"}), "api:bedrock")
        self.assertEqual(self.resolve({"ANTHROPIC_AUTH_TOKEN": FAKE_TOKEN}), "api:" + account.sha8(FAKE_TOKEN))
        self.assertEqual(self.resolve(self.key_env, headless=True), "api:" + account.sha8(FAKE_KEY))
        self.assertEqual(self.cache_files(), [])

    def test_separate_cache_per_login_path(self):
        other = self.root / "other"
        self.write_login(self.login_data(email="a@example.com"))
        self.write_login(self.login_data(email="b@example.com"), directory=other)
        self.assertEqual(self.resolve(), "a@example.com")
        self.assertEqual(self.resolve({"CLAUDE_CONFIG_DIR": str(other)}), "b@example.com")
        self.assertEqual(len(self.cache_files()), 2)

    def test_corrupt_cache_is_ignored_and_replaced(self):
        self.write_login(self.login_data())
        self.resolve()
        cache_file = self.cache_files()[0]
        for junk in ("{broken", "[]", json.dumps({"version": 1, "stamp": [0, 0, 0]})):
            cache_file.write_text(junk)
            with self.subTest(junk=junk):
                self.assertEqual(self.resolve(), "user@example.com")
                self.assertEqual(json.loads(cache_file.read_text())["email"], "user@example.com")

    def test_tampered_cache_with_matching_stamp_but_bad_types(self):
        path = self.write_login(self.login_data())
        self.resolve()
        cache_file = self.cache_files()[0]
        saved = json.loads(cache_file.read_text())
        saved["email"] = 7
        cache_file.write_text(json.dumps(saved))
        self.assertEqual(self.resolve(), "user@example.com")
        self.assertTrue(path.exists())

    def test_unwritable_cache_dir_still_works(self):
        self.write_login(self.login_data())
        blocker = self.root / "blocker"
        blocker.write_text("a file, not a directory")
        result = self.claude(cache_dir=blocker / "sub")
        self.assertEqual(result, "user@example.com")

    def test_cache_write_failure_still_works(self):
        self.write_login(self.login_data())
        with patch.object(account.os, "replace", side_effect=OSError("denied")):
            self.assertEqual(self.resolve(), "user@example.com")
        self.assertEqual(self.cache_files(), [])
        self.assertEqual([p for p in self.cache.iterdir()], [])  # temp file cleaned up

    def test_no_cache_dir_writes_nothing(self):
        self.write_login(self.login_data())
        self.assertEqual(self.claude(), "user@example.com")
        self.assertFalse(self.cache.exists())

    def test_broken_login_not_cached(self):
        self.write_login(None, raw="{broken")
        self.assertEqual(self.resolve(), "")
        self.assertEqual(self.cache_files(), [])

    def test_codex_is_not_cached(self):
        auth = self.home / ".codex" / "auth.json"
        auth.parent.mkdir()
        auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"id_token": jwt({"email": "a@example.com"})}}))
        result = account.resolve("codex-cli", {}, home=self.home, cache_dir=self.cache)
        self.assertEqual(result, "a@example.com")
        self.assertFalse(self.cache.exists())


class CliTests(AccountTestCase):
    def run_cli(self, *args, env=None, home=None):
        full = clean_env(HOME=str(home or self.home))
        if env:
            full.update(env)
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *args],
            env=full,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=self.root,
        )

    def test_plain_output(self):
        self.write_login(self.subscription())
        p = self.run_cli()
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "user@example.com\n", ""))

    def test_ts_output_format(self):
        self.write_login(self.subscription())
        p = self.run_cli("--ts")
        self.assertEqual(p.returncode, 0)
        self.assertRegex(p.stdout, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z\tuser@example\.com\n$")

    def test_ts_with_empty_account(self):
        p = self.run_cli("--ts")
        self.assertEqual(p.returncode, 0)
        self.assertRegex(p.stdout, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z\t\n$")

    def test_empty_account(self):
        p = self.run_cli()
        self.assertEqual((p.returncode, p.stdout), (0, "\n"))

    def test_agent_codex(self):
        auth = self.home / ".codex" / "auth.json"
        auth.parent.mkdir()
        auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"id_token": jwt({"email": "c@example.com"})}}))
        self.assertEqual(self.run_cli("--agent", "codex-cli").stdout, "c@example.com\n")
        self.assertEqual(self.run_cli("--agent", "claude-code").stdout, "\n")

    def test_unknown_agent(self):
        self.write_login(self.subscription())
        p = self.run_cli("--agent", "nope")
        self.assertEqual((p.returncode, p.stdout), (0, "\n"))

    def test_env_key_used(self):
        p = self.run_cli(env={"ANTHROPIC_API_KEY": FAKE_KEY})
        self.assertEqual(p.stdout, "api:" + account.sha8(FAKE_KEY) + "\n")

    def test_headless_flag(self):
        data = self.subscription()
        data["customApiKeyResponses"] = {"rejected": [FAKE_KEY[-20:]]}
        self.write_login(data)
        env = {"ANTHROPIC_API_KEY": FAKE_KEY}
        self.assertEqual(self.run_cli(env=env).stdout, "user@example.com\n")
        self.assertEqual(
            self.run_cli("--headless", env=env).stdout, "api:" + account.sha8(FAKE_KEY) + "\n"
        )

    def test_cache_dir_flag(self):
        self.write_login(self.subscription())
        cache = self.root / "cli-cache"
        p = self.run_cli("--cache-dir", str(cache))
        self.assertEqual(p.stdout, "user@example.com\n")
        self.assertEqual(len(list(cache.glob("claude-login-*.json"))), 1)

    def test_bad_arguments_still_exit_zero(self):
        for args in (["--bogus"], ["--agent"], ["--cache-dir"], ["-h"], ["--ts", "--agent", "x", "extra"]):
            with self.subTest(args=args):
                p = self.run_cli(*args)
                self.assertEqual(p.returncode, 0)
                self.assertTrue(p.stdout.endswith("\n"))

    def test_broken_login_exits_zero(self):
        self.write_login(None, raw="{broken")
        p = self.run_cli()
        self.assertEqual((p.returncode, p.stdout), (0, "\n"))

    def test_missing_home_exits_zero(self):
        p = self.run_cli(home=self.root / "does-not-exist")
        self.assertEqual((p.returncode, p.stdout), (0, "\n"))

    def test_import_has_no_side_effects(self):
        code = (
            "import importlib.util;"
            f"s=importlib.util.spec_from_file_location('a',{str(SCRIPT)!r});"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)"
        )
        p = subprocess.run(
            [sys.executable, "-I", "-c", code], env=clean_env(HOME=str(self.home)),
            capture_output=True, text=True, timeout=30, cwd=self.root,
        )
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
