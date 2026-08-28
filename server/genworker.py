"""리포트 생성 워커 — 허브와 별개의 프로세스로 돈다.

python -m server.genworker <day|week|month> <YYYY-MM-DD>

허브 프로세스 안의 스레드로 생성하면 허브 재시작(배포·설정 반영)이 진행 중인 LLM 호출 결과를 버린다.
워커는 자기 DB 커넥션(WAL)으로 재료를 읽고 결과를 쓰므로 허브가 죽거나 재시작돼도 끝까지 간다.
작업 표시는 report_jobs 테이블(pid)로 하고, 끝나면 지운다.
"""
import logging
import os
import sys

from . import db, reporting

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("madison.genworker")


def main(argv):
    if len(argv) != 2:
        print("usage: python -m server.genworker <day|week|month> <YYYY-MM-DD>", file=sys.stderr)
        return 2
    range_, day = reporting.norm_range(argv[0]), argv[1]
    day = reporting.norm_day(range_, day)
    db.conn()
    try:
        out = reporting.generate_inline(range_, day)
        log.info("worker %s %s: %s", range_, day, "failed: " + out["failed"] if out.get("failed") else "ok")
        return 1 if out.get("failed") else 0
    except Exception:
        log.exception("worker %s %s 예외", range_, day)
        return 1
    finally:
        with db.tx() as c:
            c.execute("DELETE FROM report_jobs WHERE range=? AND day=? AND pid=?", (range_, day, os.getpid()))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
