"""테스트 안전장치 — 어떤 테스트도 운영 DB(data/madison.db)를 열지 못하게 한다.

설정은 .env 값이 MADISON_* 환경변수보다 우선이라(.env에 DB_PATH가 있으면) 환경변수로는 테스트 DB를 바꿀 수 없다.
db.conn()을 직접·간접으로 부르는 테스트가 패치를 빠뜨리면 운영 DB에 migrate·쓰기가 그대로 돌았다
(2026-11-26: 수집 루프 테스트가 과거 이력 편입을 실행). 패키지를 불러오는 순간 공용 CFG의 DB 경로를
세션 임시 폴더로 돌려 두어, 개별 테스트가 패치를 잊어도 운영 DB에는 닿지 않게 한다.
테스트가 db.CFG.db_path를 따로 패치하면 그 값이 우선한다(같은 CFG 객체).
"""
import atexit
import shutil
import tempfile
from pathlib import Path

from server import config

_TMP = tempfile.mkdtemp(prefix="madison-tests-")
config.CFG.db_path = Path(_TMP) / "madison.db"
atexit.register(shutil.rmtree, _TMP, True)
