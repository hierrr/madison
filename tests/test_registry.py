import sqlite3
import unittest

from server import db, registry, report
from server.config import CFG


def _mem():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.migrate(c)
    return c


class SeedTests(unittest.TestCase):
    def setUp(self):
        self._saved = (CFG.report_service_map, CFG.report_known_services, CFG.report_weak_projects)
        CFG.report_service_map = {"web": "Acme", "api": "Acme", "scratch": "Env"}
        CFG.report_known_services = {"Acme Pro": "acme-pro, ap- 접두"}
        CFG.report_weak_projects = ("scratch",)

    def tearDown(self):
        CFG.report_service_map, CFG.report_known_services, CFG.report_weak_projects = self._saved

    def test_seed_once_from_env_then_db_is_source_of_truth(self):
        c = _mem()
        self.assertEqual(registry.seed_from_env(c), 3)            # Acme, Env, Acme Pro
        self.assertEqual(registry.seed_from_env(c), 0)            # 두 번째는 no-op
        reg = registry.snapshot(c)
        self.assertEqual(reg.names(), ["Acme", "Acme Pro", "Env"])
        self.assertEqual(reg.service("web"), "Acme")
        self.assertEqual(reg.strength("web"), "strong")
        self.assertEqual(reg.strength("scratch"), "weak")
        self.assertEqual(reg.strength("unknown"), "none")
        self.assertEqual(reg.service("unknown"), "unknown")
        self.assertEqual(reg.lookup("Acme Pro")["description"], "acme-pro, ap- 접두")
        # .env가 바뀌어도 DB가 정본
        CFG.report_service_map = {"web": "Other"}
        self.assertEqual(registry.snapshot(c).service("web"), "Acme")
        out = registry.export_env(c)
        self.assertIn("REPORT_SERVICE_MAP=api=Acme, scratch=Env, web=Acme", out)
        self.assertIn("REPORT_WEAK_PROJECTS=scratch", out)
        self.assertIn("REPORT_KNOWN_SERVICES=Acme Pro=acme-pro, ap- 접두", out)


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.c = _mem()
        self.acme = registry.upsert_service(self.c, "Acme", description="제품")

    def test_propose_confirm_flow(self):
        sid = registry.propose(self.c, "GitHub", kind="ops", description="저장소 운영",
                               cues=["커밋 계정"], evidence=[{"day": "2026-08-28", "quote": "S3"}])
        self.assertIsNotNone(sid)
        reg = registry.snapshot(self.c)
        self.assertEqual(reg.proposed_names(), ["GitHub"])
        self.assertNotIn("GitHub", reg.names())
        # 같은 이름 재제안은 근거만 덧붙는다
        self.assertEqual(registry.propose(self.c, "GitHub", evidence=[{"day": "2026-08-29", "quote": "S1"}]), sid)
        self.assertEqual(len(registry.all_services(self.c, "proposed")[0]["evidence"]), 2)
        registry.decide(self.c, sid, "confirmed")
        self.assertIn("GitHub", registry.snapshot(self.c).names())

    def test_rejected_name_is_not_reproposed_and_merge_redirects_projects(self):
        sid = registry.propose(self.c, "Junk", description="x")
        registry.decide(self.c, sid, "rejected")
        self.assertIsNone(registry.propose(self.c, "Junk", description="again"))
        self.assertEqual(registry.snapshot(self.c).proposed_names(), [])
        dup = registry.propose(self.c, "Acme 웹", description="같은 것")
        registry.set_project(self.c, "web", dup, "strong")
        registry.decide(self.c, dup, "merged", merged_into=self.acme)
        reg = registry.snapshot(self.c)
        self.assertEqual(reg.service("web"), "Acme")            # 병합 대상 이름으로 해석
        self.assertEqual(reg.names(), ["Acme"])

    def test_confirmed_or_existing_names_are_not_proposed(self):
        self.assertIsNone(registry.propose(self.c, "Acme", description="이미 있음"))
        self.assertIsNone(registry.propose(self.c, "", description="빈 이름"))
        with self.assertRaises(ValueError):
            registry.decide(self.c, self.acme, "merged")           # merged_into 없음

    def test_metrics_groups_turns_by_service(self):
        c = self.c
        c.execute("INSERT INTO devices (id,name,token_hash,created_at) VALUES (1,'w','x','2026-08-27T00:00:00Z')")
        registry.set_project(c, "web", self.acme, "strong")
        for i, proj in enumerate(("web", "web", "other")):
            c.execute("INSERT INTO events (device_id, agent, session_id, event_id, event, ts_device, ts_hub, project, payload)"
                      " VALUES (1,'claude-code','s',?, 'turn_done', ?, ?, ?, '{}')",
                      (f"e{i}", f"2026-08-27T0{i}:00:00Z", f"2026-08-27T0{i}:00:00Z", proj))
        m = report.metrics(c, "day", "2026-08-27", registry.snapshot(c))
        self.assertEqual(m["per_service"], [{"service": "Acme", "turns": 2}, {"service": "other", "turns": 1}])


if __name__ == "__main__":
    unittest.main()


class RenameTests(unittest.TestCase):
    def test_rename_propagates_to_top_level_bullets_only(self):
        c = _mem()
        md = "- Acme\n    - Acme 결제 화면 수정\n- MADISON\n    - x"
        c.execute("INSERT INTO reports (range, day, markdown, generated_at) VALUES ('day','2026-08-27',?,'t')", (md,))
        c.execute("INSERT INTO report_versions (range, day, generated_at, markdown) VALUES ('day','2026-08-27','t',?)", (md,))
        c.execute("INSERT INTO report_assignments (range, day, session_key, service, created_at) VALUES ('day','2026-08-27','S1','Acme','t')")
        self.assertEqual(registry.rename_in_reports(c, "Acme", "Acme Web"), 1)
        out = c.execute("SELECT markdown FROM reports").fetchone()["markdown"]
        self.assertEqual(out, "- Acme Web\n    - Acme 결제 화면 수정\n- MADISON\n    - x")   # 서술 속 이름은 그대로
        self.assertEqual(c.execute("SELECT markdown FROM report_versions").fetchone()["markdown"], out)
        self.assertEqual(c.execute("SELECT service FROM report_assignments").fetchone()["service"], "Acme Web")
        self.assertEqual(registry.rename_in_reports(c, "Nope", "X"), 0)
