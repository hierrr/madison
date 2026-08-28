"""LLM 호출 — 설정 탭의 provider/model/effort로 로컬 CLI(claude -p / codex exec)를 실행한다.

- 사이트(summary/digest/report)별 실효 설정: settings 테이블 → 없으면 .env/기본값.
- 위생 플래그: 허브의 호출은 사용자의 전역 CLAUDE.md·플러그인·훅·MCP·세션 저장과 무관해야 한다
  (2026-08-21~24 플러그인 스탬프가 리포트에 섞인 사고, 요약기 세션 전사본 56MB 누적).
- 프롬프트는 stdin으로 넘긴다 — 긴 일일 프롬프트가 argv 상한(macOS 1MB)에 걸리지 않게.
- 구조화 출력: claude `--json-schema` / codex `--output-schema`. 결과는 dict.
- 모든 호출을 llm_runs에 기록한다(사이트·모델·소요·반환 코드·stderr 꼬리). 실패는 삼키지 않고
  Result.ok=False로 돌려준다 — 호출측이 "저장하지 않음"을 선택할 수 있게.
"""
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db, state
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
    codex: 항상 -o 파일로 마지막 메시지를 받는다(stdout에는 훅·토큰 로그가 섞인다)."""
    if cf["provider"] == "codex":
        cmd = [cf["codex_bin"], "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only"]
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
    cmd = [cf["claude_bin"], "-p", "--output-format", "json" if schema_json else "text", *CLAUDE_HYGIENE]
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


def _record(site, cf, prompt, res: Result, started_at, ref, schema: bool):
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
    except Exception:
        log.exception("llm_runs 기록 실패")


def _parse_claude_envelope(stdout: str) -> tuple[dict | None, str]:
    """claude --output-format json 봉투 → (structured_output, error)."""
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError:
        return None, "json 봉투 파싱 실패"
    if env.get("is_error") or env.get("subtype") not in (None, "success"):
        return None, f"claude {env.get('subtype')}: {str(env.get('result') or '')[:200]}"
    data = env.get("structured_output")
    if data is None and isinstance(env.get("result"), str):
        try:
            data = json.loads(env["result"])
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        return None, "structured_output 없음"
    return data, ""


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
        if out.returncode != 0:
            res.error = f"exit {out.returncode}: {(out.stderr or '')[-400:].strip()}"
        elif cf["provider"] == "codex":
            text = Path(tmp_out).read_text(encoding="utf-8").strip() if tmp_out and Path(tmp_out).exists() else ""
            if not text:
                text = (out.stdout or "").strip().splitlines()[-1] if (out.stdout or "").strip() else ""
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
                res.data, res.error = _parse_claude_envelope(out.stdout or "")
                res.ok = res.data is not None
            else:
                res.text = (out.stdout or "").strip()
                res.ok = bool(res.text)
                if not res.ok:
                    res.error = "빈 출력"
    except subprocess.TimeoutExpired:
        res.error = f"timeout {timeout}s"
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
    _record(site, cf, prompt, res, started_at, ref, bool(schema))
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
