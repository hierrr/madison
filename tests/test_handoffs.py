"""핸드오프 상태 전이 — 수신(delivered)과 착수(in_progress)는 별개, pickup 조회(mine=1)는 pending만 (2026-10-13 사용자 결정)."""
import json
import sqlite3
import unittest
from unittest.mock import patch

from fastapi import HTTPException, Request

from server import app as hub
from server import auth, db, state


class HandoffLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        # 기기 1=보낸 쪽, 2=받는 쪽, 3=무관한 제3자
        for device_id in (1, 2, 3):
            self.conn.execute(
                "INSERT INTO devices (id,name,token_hash,created_at) VALUES (?,?,?,?)",
                (device_id, f"device-{device_id}", auth.token_hash(f"token-{device_id}"), "2026-10-01T00:00:00Z"))
        self.db_patch = patch.object(db, "_conn", self.conn)   # 핸들러가 쓰는 모듈 커넥션을 메모리 DB로
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.conn.close()

    def request(self, body=None, *, device=2):
        # 핸들러를 직접 호출하기 위한 최소 Request — 기기 토큰 헤더와 JSON 본문만 싣는다
        payload = json.dumps(body or {}).encode()

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        return Request({
            "type": "http", "method": "PATCH" if body is not None else "GET",
            "path": "/api/handoffs", "query_string": b"",
            "headers": [(b"authorization", f"Bearer token-{device}".encode())],
            "client": ("127.0.0.1", 12345), "server": ("localhost", 8787), "scheme": "http",
        }, receive)

    async def create(self, *, repo="acme", recipient=2):
        created = await hub.create_handoff(self.request({
            "to": f"device-{recipient}", "repo": repo, "branch": "main",
            "summary": "조사 이어서 진행", "doc": "먼저 이 맥락을 읽을 것.",
            "patches": [{"repo": repo, "base": "abc123", "diff": "예시 패치"}],
        }, device=1))
        return created["id"]

    async def change(self, handoff_id, status, *, device=2):
        return await hub.patch_handoff(self.request({"status": status}, device=device), handoff_id)

    async def get(self, handoff_id):
        return await hub.get_handoff(self.request(), handoff_id)

    async def test_receipt_then_work_then_done(self):
        # pending → delivered(수신) → in_progress(착수) → done. 수신·작업 중까지는 현황에 남고 완료되면 빠진다
        hid = await self.create()
        self.assertEqual([h["id"] for h in await hub.list_handoffs(self.request(), mine="1", repo="acme")], [hid])

        with patch.object(state, "utcnow", return_value="2026-10-02T01:00:00Z"):
            await self.change(hid, "delivered")
        received = await self.get(hid)
        self.assertEqual((received["status"], received["delivered_at"]), ("delivered", "2026-10-02T01:00:00Z"))
        self.assertEqual(await hub.list_handoffs(self.request(), mine="1"), [])   # 수신한 건은 pickup 조회에서 빠진다

        await self.change(hid, "in_progress")
        working = await self.get(hid)
        self.assertEqual(working["status"], "in_progress")
        self.assertEqual((working["doc"], working["patches"]), (received["doc"], received["patches"]))   # 전이는 문서·패치를 건드리지 않는다
        self.assertEqual([(h["id"], h["status"]) for h in state.assemble(self.conn)["handoffs"]], [(hid, "in_progress")])
        history = await hub.list_handoffs(self.request(), page=1, status="in_progress")
        self.assertEqual([h["id"] for h in history["rows"]], [hid])

        await self.change(hid, "done")
        self.assertEqual(state.assemble(self.conn)["handoffs"], [])
        self.assertEqual((await self.get(hid))["status"], "done")

    async def test_redelivery_keeps_first_delivered_at(self):
        # 수신 처리를 다시 보내도(재시도·다른 세션) 최초 수신 시각은 유지된다
        hid = await self.create()
        with patch.object(state, "utcnow", return_value="2026-10-02T01:00:00Z"):
            await self.change(hid, "delivered")
        with patch.object(state, "utcnow", return_value="2026-10-03T02:00:00Z"):
            await self.change(hid, "delivered")
        self.assertEqual((await self.get(hid))["delivered_at"], "2026-10-02T01:00:00Z")

    async def test_pickup_lists_pending_only(self):
        # mine=1은 이 기기 앞으로 온 pending만 — 수신됨·작업 중·완료·취소도, 다른 기기 건도 제외
        ids = {}
        for status in ("pending", "delivered", "in_progress", "done", "cancelled"):
            ids[status] = await self.create()
            if status != "pending":
                await self.change(ids[status], "delivered")
                await self.change(ids[status], status)
        other_repo = await self.create(repo="acme-tools")
        await self.create(recipient=3)

        mine = await hub.list_handoffs(self.request(), mine="1", repo="acme")
        self.assertEqual([h["id"] for h in mine], [ids["pending"]])
        all_repos = await hub.list_handoffs(self.request(), mine="1")   # 리포 필터가 없으면 다른 리포의 pending까지
        self.assertEqual([h["id"] for h in all_repos], [ids["pending"], other_repo])
        self.assertEqual({h["status"] for h in state.assemble(self.conn)["handoffs"]}, {"pending", "delivered", "in_progress"})

    async def test_only_recipient_can_transition(self):
        # 보낸 기기(1)도 제3자(3)도 상태를 바꿀 수 없다 — 권한 없음은 404로 숨긴다
        hid = await self.create()
        for device in (1, 3):
            for status in ("delivered", "in_progress", "done", "cancelled"):
                with self.subTest(device=device, status=status):
                    with self.assertRaises(HTTPException) as raised:
                        await self.change(hid, status, device=device)
                    self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual((await self.get(hid))["status"], "pending")

    async def test_unknown_status_rejected(self):
        hid = await self.create()
        with self.assertRaises(HTTPException) as raised:
            await self.change(hid, "unknown")
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
