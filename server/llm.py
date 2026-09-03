"""LLM 호출 — 설정 탭의 provider/model/effort로 로컬 CLI(claude -p / codex exec)를 실행한다.

- 사이트(summary/digest/report)별 실효 설정: settings 테이블 → 없으면 .env/기본값.
- 위생 플래그: 허브의 호출은 사용자의 전역 CLAUDE.md·플러그인·훅·MCP·세션 저장과 무관해야 한다
  (2026-08-21~24 플러그인 스탬프가 리포트에 섞인 사고, 요약기 세션 전사본 56MB 누적).
- 프롬프트는 stdin으로 넘긴다 — 긴 일일 프롬프트가 argv 상한(macOS 1MB)에 걸리지 않게.
- 구조화 출력: claude `--json-schema` / codex `--output-schema`. 결과는 dict.
- 모든 호출을 llm_runs에 기록한다(사이트·모델·소요·반환 코드·stderr 꼬리). 실패는 삼키지 않고
  Result.ok=False로 돌려준다 — 호출측이 "저장하지 않음"을 선택할 수 있게.
"""
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db, state, tokens
from .config import CFG

log = logging.getLogger("madison.llm")

SITES = ("summary", "digest", "report")       # 세션 한 줄 요약 · 긴 세션 압축 · 업무 리포트
FIELDS = ("provider", "model", "effort")

# claude -p 위생 플래그 — 사용자 커스터마이즈(CLAUDE.md·훅·플러그인·MCP·스킬) 비활성, 내장 도구 없음,
# 세션 저장 안 함. 인증(OAuth)은 유지된다(--bare와 다름, 2026-08-28 실측).
CLAUDE_HYGIENE = ("--safe-mode", "--tools", "", "--no-session-persistence", "--disable-slash-commands")


def defaults(site: str) -> dict:
    model = {"summary": CFG.task_summary_model, "digest": CFG.digest_model,
             "report": CFG.report_model}.get(site, CFG.report_model)
    return {"provider": "claude", "model": model, "effort": ""}


def settings_all(c) -> dict:
    return {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")}


def conf(site: str) -> dict:
    """사이트별 실효 설정: settings 테이블 → 없으면 .env/기본값."""
    with db.tx() as c:
        stored = settings_all(c)
    out = defaults(site)
    for f in FIELDS:
        v = (stored.get(f"llm.{site}.{f}") or "").strip()
        if v:
            out[f] = v
    out["claude_bin"] = (stored.get("llm.claude_bin") or "").strip() or CFG.task_summary_bin
    out["codex_bin"] = (stored.get("llm.codex_bin") or "").strip() or CFG.codex_bin
    return out


def command(cf: dict, *, schema_path: str | None = None, schema_json: str | None = None,
            out_file: str | None = None) -> list[str]:
    """실행 명령. 프롬프트는 argv가 아니라 stdin으로 준다.
    codex: 항상 -o 파일로 마지막 메시지를 받고, --json의 stdout JSONL 이벤트 스트림이
    토큰 집계(_codex_usage)와 -o 폴백(_codex_message)의 출처다 — stdout을 버리지 말 것."""
    if cf["provider"] == "codex":
        # --json: stdout이 JSONL 이벤트가 되고 turn.completed에 토큰 사용량이 실린다
        # (--ephemeral이라 롤아웃이 안 남으므로 이것이 유일한 토큰 출처). -o 최종 메시지와 공존 확인됨.
        cmd = [cf["codex_bin"], "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "--json"]
        if cf.get("model"):
            cmd += ["--model", cf["model"]]
        if cf.get("effort"):
            cmd += ["-c", f'model_reasoning_effort="{cf["effort"]}"']
        if schema_path:
            cmd += ["--output-schema", schema_path]
        if out_file:
            cmd += ["-o", out_file]
        cmd.append("-")                                   # 프롬프트 = stdin
        return cmd
    # 텍스트 호출도 json 봉투로 — 봉투의 usage/modelUsage가 유일한 토큰 출처
    # (--no-session-persistence라 전사본이 안 남는다). 본문은 봉투의 result에서 꺼낸다.
    cmd = [cf["claude_bin"], "-p", "--output-format", "json", *CLAUDE_HYGIENE]
    if schema_json:
        cmd += ["--json-schema", schema_json]
    if cf.get("model"):
        cmd += ["--model", cf["model"]]
    if cf.get("effort"):
        cmd += ["--effort", cf["effort"]]
    return cmd


@dataclass
class Result:
    ok: bool
    text: str = ""
    data: dict | None = None
    error: str = ""
    returncode: int | None = None
    duration: float = 0.0
    provider: str = ""
    model: str = ""
    effort: str = ""
    run_id: int | None = None
    extra: dict = field(default_factory=dict)

    def __bool__(self):
        return self.ok


def _cwd() -> Path:
    p = CFG.llm_cwd
    p.mkdir(parents=True, exist_ok=True)
    return p


def _record(site, cf, prompt, res: Result, started_at, ref, schema: bool, usage_map: dict | None = None):
    try:
        with db.tx() as c:
            cur = c.execute(
                "INSERT INTO llm_runs (site, provider, model, effort, prompt_sha, prompt_chars, output_chars,"
                " ok, returncode, duration_s, started_at, error, ref, structured)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (site, cf["provider"], cf.get("model") or "", cf.get("effort") or "",
                 hashlib.sha256(prompt.encode()).hexdigest()[:16], len(prompt),
                 len(res.text) if res.text else (len(json.dumps(res.data)) if res.data else 0),
                 1 if res.ok else 0, res.returncode, round(res.duration, 1), started_at,
                 (res.error or "")[:500], ref[:80], 1 if schema else 0))
            res.run_id = cur.lastrowid
            c.execute("DELETE FROM llm_runs WHERE started_at < datetime('now','-30 days')")
            # 워커 토큰 원장 기록 — llm_runs와 같은 트랜잭션 (호출당 커밋 1회)
            _account_tokens("codex-cli" if cf["provider"] == "codex" else "claude-code",
                            usage_map or {}, c=c)
    except Exception:
        log.exception("llm_runs 기록 실패")


def _envelope(stdout: str) -> dict | None:
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return env if isinstance(env, dict) else None


def _claude_usage(env: dict | None, fallback_model: str = "") -> dict:
    """claude -p json 봉투 → {model: {in,out,cr,cw,th}} (modelUsage 우선 — 모델별 분해).
    modelUsage가 없는 구버전 봉투는 집계 usage를 설정된 모델명으로 귀속."""
    if not isinstance(env, dict):
        return {}
    out: dict = {}
    mu = env.get("modelUsage")
    if isinstance(mu, dict) and mu:
        for m, u in mu.items():
            if isinstance(u, dict):
                out[str(m)[:60]] = {
                    "in": int(u.get("inputTokens") or 0), "out": int(u.get("outputTokens") or 0),
                    "cr": int(u.get("cacheReadInputTokens") or 0),
                    "cw": int(u.get("cacheCreationInputTokens") or 0),
                    "th": int(u.get("thinkingTokens") or 0)}
        return out
    u = env.get("usage")
    if isinstance(u, dict):
        out[(fallback_model or "unknown")[:60]] = tokens.claude_counts(u)
    return out


def _codex_usage(stdout: str, model: str) -> dict:
    """codex exec --json 이벤트 스트림의 turn.completed usage 합산 → {model: counts}."""
    tot = {"in": 0, "out": 0, "cr": 0, "cw": 0, "th": 0}
    for line in (stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "turn.completed":
            continue
        u = ev.get("usage")
        if not isinstance(u, dict):
            continue
        for k, v in tokens.codex_counts(u).items():
            tot[k] += v
    return {(model or "unknown")[:60]: tot} if any(tot.values()) else {}


def _codex_message(stdout: str) -> str:
    """--json 이벤트 스트림의 마지막 agent_message 텍스트 — -o 파일이 빈 경우의 폴백.
    (--json 이후 stdout 마지막 줄은 프로토콜 이벤트라 그대로 쓰면 안 된다.)"""
    msg = ""
    for line in (stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "item.completed":
            item = ev.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
                msg = str(item["text"])
    return msg.strip()


_warned_no_device = False


def _account_tokens(agent: str, usage_map: dict, c=None):
    """워커 호출의 토큰을 원장에 직접 가산 — 워커는 전사본을 안 남기므로(--no-session-persistence /
    --ephemeral) 응답에 실린 사용량이 유일한 출처다. 실패해도 호출 결과에는 영향 없다."""
    global _warned_no_device
    if not usage_map:
        return
    try:
        with (contextlib.nullcontext(c) if c is not None else db.tx()) as conn:
            device_id = tokens.hub_device_id(conn)
            if device_id is None:
                if not _warned_no_device:
                    _warned_no_device = True
                    log.warning("worker 토큰 기록 비활성 — 이 허브 기기의 MADISON_DEVICE를 못 찾음")
                return
            day = time.strftime("%Y-%m-%d")
            for model, v in usage_map.items():
                if any(v.values()):
                    tokens.add_daily(conn, day, device_id, agent, CFG.llm_cwd.name, model,
                                     "auto", "events", v)
    except Exception:
        log.exception("worker 토큰 기록 실패")


def _envelope_error(env: dict) -> str:
    """봉투의 실패 판정 — schema/text 두 경로가 같은 기준·문구를 쓴다."""
    if env.get("is_error") or env.get("subtype") not in (None, "success"):
        return f"claude {env.get('subtype')}: {str(env.get('result') or '')[:200]}"
    return ""


def _claude_text(env: dict | None, raw: str) -> tuple[str, str]:
    """텍스트 호출의 (본문, 에러) — 봉투면 result, 아니면 원문 그대로(구버전 CLI 호환)."""
    if env is None:
        return raw.strip(), ""
    err = _envelope_error(env)
    if err:
        return "", err
    return str(env.get("result") or "").strip(), ""


def _parse_claude_env(env: dict | None) -> tuple[dict | None, str]:
    """claude json 봉투 → (structured_output, error)."""
    if env is None:
        return None, "json 봉투 파싱 실패"
    err = _envelope_error(env)
    if err:
        return None, err
    data = env.get("structured_output")
    if data is None and isinstance(env.get("result"), str):
        try:
            data = json.loads(env["result"])
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        return None, "structured_output 없음"
    return data, ""


def _parse_claude_envelope(stdout: str) -> tuple[dict | None, str]:
    return _parse_claude_env(_envelope(stdout))


def run(site: str, prompt: str, *, schema: dict | None = None, timeout: int | None = None,
        ref: str = "") -> Result:
    """전용 cwd(비-git → 새어도 project='llm-cwd'로 자명) + 독립 프로세스 그룹
    (허브 kickstart 재시작이 진행 중인 호출을 죽여 잔해를 남기지 않도록)."""
    cf = conf(site)
    timeout = timeout or CFG.llm_timeout(site)
    started_at = state.utcnow()
    t0 = time.monotonic()
    res = Result(ok=False, provider=cf["provider"], model=cf.get("model") or "", effort=cf.get("effort") or "")
    tmp_schema = tmp_out = None
    usage_map: dict = {}
    try:
        if cf["provider"] == "codex":
            fd, tmp_out = tempfile.mkstemp(prefix="madison-llm-", suffix=".out"); os.close(fd)
            if schema:
                fd, tmp_schema = tempfile.mkstemp(prefix="madison-llm-", suffix=".schema.json"); os.close(fd)
                Path(tmp_schema).write_text(json.dumps(schema), encoding="utf-8")
            cmd = command(cf, schema_path=tmp_schema, out_file=tmp_out)
        else:
            cmd = command(cf, schema_json=json.dumps(schema) if schema else None)
        out = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
            cwd=str(_cwd()), start_new_session=True,
            # CLI 디렉터리를 PATH 앞에 — launchd 최소 PATH에서 shebang(env node) 해석 실패 방지
            env={**os.environ, "MADISON_SUPPRESS": "1",
                 "PATH": os.path.dirname(cmd[0]) + os.pathsep + os.environ.get("PATH", "")},
        )
        res.returncode = out.returncode
        env = _envelope(out.stdout or "") if cf["provider"] != "codex" else None
        # 토큰 집계는 성패와 무관하게(실패한 호출도 쿼터는 소모) — 파싱 예외가 호출 결과를 오염시키지 않게 격리.
        # 실제 기록은 _record가 llm_runs와 같은 트랜잭션에서 한다.
        try:
            if cf["provider"] == "codex":
                usage_map = _codex_usage(out.stdout or "", cf.get("model") or "")
            else:
                usage_map = _claude_usage(env, cf.get("model") or "")
        except Exception:
            log.exception("worker 토큰 집계 실패")
        if out.returncode != 0:
            res.error = f"exit {out.returncode}: {(out.stderr or '')[-400:].strip()}"
        elif cf["provider"] == "codex":
            text = Path(tmp_out).read_text(encoding="utf-8").strip() if tmp_out and Path(tmp_out).exists() else ""
            if not text:
                text = _codex_message(out.stdout or "")
            if schema:
                try:
                    res.data = json.loads(text)
                    res.ok = isinstance(res.data, dict)
                    if not res.ok:
                        res.error = "출력이 JSON 객체가 아님"
                except json.JSONDecodeError:
                    res.error = "출력 JSON 파싱 실패"
            else:
                res.text, res.ok = text, bool(text)
                if not text:
                    res.error = "빈 출력"
        else:
            if schema:
                res.data, res.error = _parse_claude_env(env)
                res.ok = res.data is not None
            else:
                res.text, res.error = _claude_text(env, out.stdout or "")
                res.ok = bool(res.text)
                if not res.ok and not res.error:
                    res.error = "빈 출력"
    except subprocess.TimeoutExpired as e:
        res.error = f"timeout {timeout}s"
        # 타임아웃도 쿼터는 소모 — codex는 죽기 전까지의 turn.completed가 stdout에 남는다
        # (claude 봉투는 종료 시에만 나와 복구 불가)
        try:
            if cf["provider"] == "codex":
                so = e.stdout.decode("utf-8", "ignore") if isinstance(e.stdout, bytes) else (e.stdout or "")
                usage_map = _codex_usage(so, cf.get("model") or "")
        except Exception:
            log.exception("worker 토큰 집계 실패(timeout)")
    except Exception as e:  # 실행 파일 없음 등
        res.error = f"{type(e).__name__}: {e}"[:300]
    finally:
        for p in (tmp_schema, tmp_out):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
    res.duration = time.monotonic() - t0
    _record(site, cf, prompt, res, started_at, ref, bool(schema), usage_map)
    if res.ok:
        out_len = len(res.text) if res.text else (len(json.dumps(res.data, ensure_ascii=False)) if res.data else 0)
        log.info("llm %s ok %s/%s %.0fs in=%d out=%d%s ref=%s", site, res.provider, res.model, res.duration,
                 len(prompt), out_len, " (structured)" if res.data is not None else "", ref)
    else:
        log.warning("llm %s FAIL %s/%s %.0fs ref=%s: %s", site, res.provider, res.model, res.duration, ref, res.error)
    return res


def strip_fences(md: str) -> str:
    """모델이 전체를 ```…```로 감싸 반환하는 경우 바깥 펜스만 벗긴다."""
    t = md.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1 and t.rstrip().endswith("```"):
            t = t[first_nl + 1:].rstrip()
            t = t[: t.rfind("```")].rstrip()
    return t
