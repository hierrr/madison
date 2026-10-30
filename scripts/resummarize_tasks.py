#!/usr/bin/env python3
"""다중 턴 사람 세션의 태스크 요약 재생성 (일회성, 허브 로컬).

예전 요약기는 세션의 마지막 지시 한 줄만 보고 요약해서, '진행'·'커밋 푸시' 같은 짧은 후속 지시가
세션 요약을 덮어썼다. 또 백그라운드 완료 알림·다른 세션 메시지(주입 메시지, state.is_injected)도
사람 지시처럼 last_prompt를 덮어 요약을 틀어 놓았다. 이 스크립트가 둘 다 바로잡는다:

1. 복구 — 사람 세션 중 last_prompt가 주입 메시지인 세션은, events의 마지막 비주입 prompt(ingest와 같이
   앞 state.PROMPT_CAP자)로 last_prompt를 되돌리고(없으면 NULL) task_summary를 비운다.
2. 재요약 — 지금의 summary.build_input(앞 지시·마지막 응답을 맥락으로 붙임)과 워커가 쓰는 공용 저장 경로
   summary.summarize_rows로 task_summary를 다시 만든다.

  python3 scripts/resummarize_tasks.py [--days N] [--limit M]            # dry-run: 대상 수와 입력 예시만
  python3 scripts/resummarize_tasks.py [--days N] [--limit M] --apply    # 복구 + LLM 호출 + 저장

- 대상: 사람 세션(frontend가 NULL이거나 'auto'가 아님) 중 비주입 prompt가 2개 이상인 세션 + 복구 대상 세션.
- --days N: last_seen_hub가 N일 이내인 세션만 (0=전체, 기본 0). --limit: 재요약 대상을 최근 순으로 M개까지 —
  복구는 비파괴·소량이라 --limit과 무관하게 항상 전체(--days 범위)를 한다.
- dry-run과 --apply는 같은 plan()으로 대상을 고르므로 집합이 같다(복구된 세션은 지시 수와 무관하게 재요약 대상).
- dry-run은 DB를 읽기 전용으로 열어 마이그레이션·쓰기를 하지 않고 LLM도 부르지 않는다 — 복구 대상 수·예시만 출력.
  WAL 모드라 읽기 전용 연결도 -shm이 필요해서, 허브가 떠 있지 않으면 열 수 없다(그때는 안내 후 종료).
- 재실행 가능: 같은 입력은 summary_cache가 재사용하고, 저장은 워커와 같은 가드(그 사이 지시가 바뀌면 건너뜀)를 쓴다.
- DB 경로는 서버 설정(.env의 DB_PATH, 기본 data/madison.db)을 그대로 따른다.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import db, state, summary  # noqa: E402
from server.config import CFG  # noqa: E402

BATCH = 30          # --apply 진행 표시 단위
SAMPLES = 5         # dry-run에서 보여줄 입력 예시 수
_KEY = "s.device_id, s.agent, s.session_id, s.last_prompt, s.last_summary"
_HUMAN = "(s.frontend IS NULL OR s.frontend != 'auto')"


def recoveries(c, days: int) -> list[dict]:
    """last_prompt가 주입 메시지인 사람 세션 → 되돌릴 last_prompt(비주입 prompt가 없으면 None)."""
    where = [_HUMAN, "LTRIM(s.last_prompt, ' ' || char(9) || char(10) || char(13)) LIKE '<%'"]
    args: list = []
    if days > 0:
        where.append("s.last_seen_hub >= datetime('now', ?)")
        args.append(f"-{days} days")
    out = []
    for r in c.execute(f"SELECT {_KEY} FROM sessions s WHERE " + " AND ".join(where)
                       + " ORDER BY s.last_seen_hub DESC", args):
        if not state.is_injected(r["last_prompt"]):
            continue
        prompts = summary.session_prompts(c, r["device_id"], r["agent"], r["session_id"])
        out.append({**dict(r), "new_last_prompt": prompts[-1][:state.PROMPT_CAP] if prompts else None})
    return out


def targets(c, days: int, limit: int | None, recovered: dict) -> list[dict]:
    """재요약 대상. recovered = {(device_id, agent, session_id): 되돌린 last_prompt} — dry-run이 복구 후 상태를
    메모리에서 흉내 낼 때 쓴다(apply는 복구를 DB에 쓴 뒤 호출하므로 비어 있다)."""
    where = [_HUMAN]
    args: list = []
    if days > 0:
        where.append("s.last_seen_hub >= datetime('now', ?)")
        args.append(f"-{days} days")
    out = []
    for r in c.execute(f"SELECT {_KEY} FROM sessions s WHERE " + " AND ".join(where)
                       + " ORDER BY s.last_seen_hub DESC", args):
        r = dict(r)
        key = (r["device_id"], r["agent"], r["session_id"])
        if key in recovered:
            r["last_prompt"] = recovered[key]
        if not (r["last_prompt"] or r["last_summary"]):
            continue
        if key in recovered or len(summary.session_prompts(c, *key)) >= 2:
            out.append(r)
            if limit and len(out) >= limit:
                break
    return out


def plan(c, days: int, limit: int | None):
    """(복구 목록, 재요약 대상 행) — dry-run과 apply가 함께 쓰는 유일한 선정 경로. 복구된 세션은 복구 뒤 last_prompt로
    대상에 들어가므로(지시 수와 무관) apply가 복구 전 상태에서 이 함수를 불러도 dry-run과 같은 집합이다."""
    recs = recoveries(c, days)
    recovered = {(r["device_id"], r["agent"], r["session_id"]): r["new_last_prompt"] for r in recs}
    return recs, targets(c, days, limit, recovered)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="실제로 복구·요약을 호출하고 저장 (기본은 dry-run)")
    ap.add_argument("--days", type=int, default=0, help="last_seen_hub 기준 최근 N일 (0=전체, 기본 0)")
    ap.add_argument("--limit", type=int, help="재요약 대상 수 상한(최근 순) — 복구는 이 값과 무관하게 전체")
    args = ap.parse_args()

    if not args.apply:
        # 읽기 전용 연결 — db.conn()은 스키마 마이그레이션을 돌리므로 dry-run에서는 쓰지 않는다
        try:
            c = sqlite3.connect(Path(CFG.db_path).resolve().as_uri() + "?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
            try:
                recs, rows = plan(c, args.days, args.limit)
                inputs = [summary.build_input(c, r) for r in rows]
            finally:
                c.close()
        except sqlite3.OperationalError as e:
            sys.exit(f"dry-run용 읽기 전용 연결을 열 수 없다({e}) — 허브가 떠 있지 않으면(WAL -shm 없음) "
                     "dry-run을 열 수 없다. 허브를 켠 뒤 다시 하거나 DB 사본으로 실행할 것")
        none_left = sum(1 for r in recs if r["new_last_prompt"] is None)
        print(f"복구 대상 {len(recs)}개 (되돌릴 지시가 없어 NULL이 되는 세션 {none_left}개) — dry-run: 쓰지 않음")
        for r in recs[:SAMPLES]:
            print(f"  {r['agent']} {r['session_id'][:8]}: {' '.join(r['last_prompt'].split())[:60]!r}"
                  f" -> {' '.join((r['new_last_prompt'] or '(NULL)').split())[:60]!r}")
        with_ctx = sum(1 for i, r in zip(inputs, rows) if i != r["last_prompt"])
        print(f"\n재요약 대상 세션 {len(rows)}개 (맥락을 붙인 입력 {with_ctx}개) — dry-run: LLM 호출·저장 없음")
        for r, inp in list(zip(rows, inputs))[:SAMPLES]:
            print(f"\n--- {r['agent']} {r['session_id'][:8]}\n{inp}")
        return

    with db.tx() as c:
        recs, rows = plan(c, args.days, args.limit)   # dry-run과 같은 대상 — 복구 전 상태에서 한 번에 고른다
        for r in recs:
            c.execute("UPDATE sessions SET last_prompt=?, task_summary=NULL"
                      " WHERE device_id=? AND agent=? AND session_id=? AND last_prompt=?",
                      (r["new_last_prompt"], r["device_id"], r["agent"], r["session_id"], r["last_prompt"]))
        print(f"복구 {len(recs)}개 세션의 last_prompt를 되돌림")
        inputs = [summary.build_input(c, r) for r in rows]
    items = list(zip(rows, inputs))
    print(f"대상 세션 {len(items)}개 재요약 시작")
    done = 0
    for i in range(0, len(items), BATCH):
        done += summary.summarize_rows(items[i:i + BATCH])
        print(f"  {done}/{len(items)}", flush=True)
    print("완료")


if __name__ == "__main__":
    main()
