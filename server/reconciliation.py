"""기기별 설치 상태와 세션 사용량 대조."""
import hashlib
import json
from datetime import datetime, timezone, timedelta

from . import tokens
from .config import REPO_ROOT


def release():
    return json.loads((REPO_ROOT / "collector/version.json").read_text())


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise ValueError("유효한 사용 시각이 필요합니다") from None


def counts(value):
    if not isinstance(value, dict) or any(
        isinstance(value.get(k, 0), bool) or not isinstance(value.get(k, 0), int)
        or not 0 <= value.get(k, 0) <= tokens.MAX_COUNT for k in tokens.KINDS
    ):
        raise ValueError("토큰 수는 범위 내의 음이 아닌 정수여야 합니다")
    return {k: value.get(k, 0) for k in tokens.KINDS}


def merge_max(left, right):
    merged = {m: dict(v) for m, v in left.items()}
    for m, value in right.items():
        old = merged.setdefault(m, {})
        for k in tokens.KINDS:
            old[k] = max(old.get(k, 0), value.get(k, 0))
    return merged


def update_status(c, device_id, body):
    action = body.get("action")
    if action not in ("installed", "audit_started", "audit_finished"):
        raise ValueError("알 수 없는 수집기 상태")
    version = str(body.get("version") or "")[:40]
    run_id = str(body.get("run_id") or "")[:80]
    if not version or (action != "installed" and not run_id):
        raise ValueError("버전과 대조 작업 ID가 필요합니다")
    c.execute("INSERT OR IGNORE INTO collector_status(device_id) VALUES (?)", (device_id,))
    if action == "installed":
        c.execute("UPDATE collector_status SET version=?, installed_at=? WHERE device_id=?",
                  (version, now(), device_id))
    elif action == "audit_started":
        c.execute("UPDATE collector_status SET audit_id=?, audit_state='running', started_at=?,"
                  " finished_at=NULL, inventory=NULL WHERE device_id=?", (run_id, now(), device_id))
    else:
        inventory = body.get("inventory") or {}
        if not isinstance(inventory, dict) or len(json.dumps(inventory)) > 16000:
            raise ValueError("로컬 자료 요약이 너무 큽니다")
        c.execute("UPDATE collector_status SET audit_state='finished', finished_at=?, inventory=?"
                  " WHERE device_id=? AND audit_id=?", (now(), json.dumps(inventory), device_id, run_id))
    return {"ok": True}


def status(c, device_id=None):
    rows = c.execute(
        "SELECT d.id,d.name,s.* FROM devices d LEFT JOIN collector_status s ON s.device_id=d.id"
        " WHERE d.revoked=0" + (" AND d.id=?" if device_id is not None else "") + " ORDER BY d.id",
        (device_id,) if device_id is not None else ()).fetchall()
    devices = []
    for row in rows:
        item = dict(row)
        item["inventory"] = json.loads(item.get("inventory") or "{}")
        devices.append(item)
    return {**release(), "devices": devices}


def reconcile(c, device_id, body):
    agent, sid = body.get("agent"), str(body.get("session_id") or "")
    run_id = str(body.get("run_id") or "")
    if agent not in ("claude-code", "codex-cli") or not 0 < len(sid) <= 160 or not 0 < len(run_id) <= 80:
        raise ValueError("에이전트·세션·대조 작업 ID가 올바르지 않습니다")
    samples = body.get("samples") or []
    if not isinstance(samples, list) or len(samples) > 4000:
        raise ValueError("세션별 시각 표본은 4000개 이하입니다")
    clean = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("잘못된 시각 표본")
        model = str(sample.get("model") or "")
        if not model or len(model) > 60:
            raise ValueError("모델이 필요합니다")
        clean.append((stamp(sample.get("ts")), model, counts(sample.get("cum"))))
    if clean != sorted(clean, key=lambda p: p[0]):
        raise ValueError("사용 시각은 오름차순이어야 합니다")
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    previous = c.execute("SELECT fingerprint,result FROM token_reconciliations"
                         " WHERE device_id=? AND run_id=? AND agent=? AND session_id=?",
                         (device_id, run_id, agent, sid)).fetchone()
    if previous and previous["fingerprint"] == fingerprint:
        return json.loads(previous["result"])
    session = c.execute("SELECT * FROM sessions WHERE device_id=? AND agent=? AND session_id=?",
                        (device_id, agent, sid)).fetchone()
    result = {"status": "matched", "added": {k: 0 for k in tokens.KINDS}, "session_id": sid}
    final = {}
    regressed = False
    for _, model, value in clean:
        key = tokens._CODEX_KEY if agent == "codex-cli" else model
        old = final.get(key, {})
        regressed |= any(value[k] < old.get(k, 0) for k in tokens.KINDS)
        final[key] = value
    corrupt = False
    try:
        old = tokens._clean(json.loads(session["tokens_cum"] or "{}")) if session else {}
    except (ValueError, TypeError):
        old, corrupt = {}, True
    if not session:
        result["status"] = "untracked"
    elif corrupt:
        result["status"] = "invalid_baseline"
    elif body.get("source") != "transcript" or body.get("complete") is not True:
        result["status"] = str(body.get("issue") or "verification_only")[:60]
    elif not clean:
        result["status"] = "counter_conflict" if old else "no_usage"
    elif regressed or any(final.get(m, {}).get(k, 0) < v.get(k, 0) for m, v in old.items() for k in tokens.KINDS):
        result["status"] = "counter_conflict"
    else:
        first = c.execute("SELECT MIN(day) FROM token_daily WHERE device_id=? AND agent=? AND source='events'",
                          (device_id, agent)).fetchone()[0]
        if not old and first and tokens.local_hour(clean[0][0].isoformat())[:10] < first:
            result["status"] = "before_tracking"
        else:
            project, frontend = tokens.ledger_key(session["project"], session)
            baseline = old
            with tokens.savepoint(c, "reconcile_session"):
                for ts, model, value in clean:
                    key = tokens._CODEX_KEY if agent == "codex-cli" else model
                    delta = tokens._delta(baseline.get(key), value)
                    if any(delta.values()):
                        tokens.add_ledger(c, tokens.local_hour(ts.isoformat()), device_id, agent,
                                          project, model, frontend, "events", delta, turns=0)
                        for k in tokens.KINDS:
                            result["added"][k] += delta[k]
                    baseline = merge_max(baseline, {key: value})
                encoded = json.dumps(baseline)
                c.execute("UPDATE sessions SET tokens_cum=? WHERE device_id=? AND agent=? AND session_id=?",
                          (encoded, device_id, agent, sid))
                c.execute("INSERT INTO token_reconcile_floors(device_id,agent,session_id,cum) VALUES (?,?,?,?)"
                          " ON CONFLICT(device_id,agent,session_id) DO UPDATE SET cum=excluded.cum",
                          (device_id, agent, sid, encoded))
            if any(result["added"].values()):
                result["status"] = "corrected"
    result["added_total"] = sum(result["added"][k] for k in ("in", "out", "cr", "cw"))
    evidence = {"source": str(body.get("source") or "")[:40], "complete": body.get("complete") is True,
                "local_cum": final, "hub_cum_before": old,
                "first_ts": clean[0][0].isoformat() if clean else None,
                "last_ts": clean[-1][0].isoformat() if clean else None}
    if isinstance(body.get("local_total"), int):
        evidence["local_total"] = max(0, min(body["local_total"], tokens.MAX_COUNT))
    if isinstance(body.get("model_usage"), dict):
        evidence["model_usage"] = {
            str(m)[:60]: {k: v for k, v in u.items() if k in (
                "inputTokens", "outputTokens", "thinkingTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
                and isinstance(v, int) and 0 <= v <= tokens.MAX_COUNT}
            for m, u in list(body["model_usage"].items())[:100] if isinstance(u, dict)}
    c.execute("INSERT INTO token_reconciliations(device_id,run_id,agent,session_id,fingerprint,status,added_total,result,checked_at,evidence)"
              " VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(device_id,run_id,agent,session_id) DO UPDATE SET"
              " fingerprint=excluded.fingerprint,status=excluded.status,added_total=token_reconciliations.added_total+excluded.added_total,"
              " result=excluded.result,checked_at=excluded.checked_at,evidence=excluded.evidence",
              (device_id, run_id, agent, sid, fingerprint, result["status"], result["added_total"], json.dumps(result), now(), json.dumps(evidence)))
    return result
