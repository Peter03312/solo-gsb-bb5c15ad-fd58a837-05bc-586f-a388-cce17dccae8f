"""责任证据封存 × 探头故障隔离的交互测试。

覆盖本轮修复：已登记探头故障生效时，区间内、当前装卸窗口内被隔离的车载
读数不再被封存归集漏掉——

* 新封存包保留适用的原始车载读数（有效温度 / 版本），并标明封存时是否
  隔离（``quarantined``）、覆盖故障编号（``covering_fault_ids``）与是否
  采用（``adopted``）；同刻自有测温仍优先（被取代且被隔离时两者皆标）；
* 隔离读数不参与越温重建（区间仍由自有测温开闭），封存与故障登记 / 撤销
  按现有锁序看到一致视图；
* 对照：封存时**已隔离**的读数后续状态与覆盖集合变化（撤销一条 / 全部
  撤销）记为 ``changed``；封存后**才隔离**的读数记为 ``removed``（既有
  语义保持）；旧格式封存包（无隔离字段）查询与对照兼容、不产生伪变更；
* 封存原文与摘要不可改、同请求重传返回原包、区间消失 / 开放 / 快照过期 /
  案件编号冲突沿用现有拒绝且不留记录。
"""
import json

from app import sealing
from app.database import SessionLocal
from app.models import EvidenceSeal


def setup_quarantine_scenario(client, cargo="C1", trip="T1", limit="4.0"):
    """货物 + 车次 + 装卸窗口（08:00-20:00）；返回 None。"""
    r = client.post(
        "/cargo",
        json={
            "cargo_id": cargo,
            "started_at": "2026-01-01T00:00:00+00:00",
            "temp_upper_limit": limit,
            "initial_holder": "A",
        },
    )
    assert r.status_code == 201, r.text
    assert client.post(
        "/trips", json={"trip_id": trip, "vehicle_no": "京B-隔01"}
    ).status_code == 201
    assert client.post(
        f"/trips/{trip}/manifest",
        json={
            "items": [
                {
                    "cargo_id": cargo,
                    "loaded_at": "2026-01-01T08:00:00+00:00",
                    "unloaded_at": "2026-01-01T20:00:00+00:00",
                }
            ]
        },
    ).status_code == 201


def own(client, eid, hhmm, temp, cargo="C1"):
    r = client.post(
        "/events",
        json={
            "event_id": eid,
            "cargo_id": cargo,
            "occurred_at": f"2026-01-01T{hhmm}:00+00:00",
            "temperature": str(temp),
        },
    )
    assert r.status_code == 201, r.text


def reading(client, rid, hhmm, temp, trip="T1"):
    r = client.post(
        "/vehicle-readings",
        json={
            "reading_id": rid,
            "trip_id": trip,
            "read_at": f"2026-01-01T{hhmm}:00+00:00",
            "temperature": str(temp),
        },
    )
    assert r.status_code == 201, r.text


def fault(client, fid, start="10:05", end="10:25", trip="T1"):
    r = client.post(
        f"/trips/{trip}/probe-faults",
        json={
            "fault_id": fid,
            "fault_start": f"2026-01-01T{start}:00+00:00",
            "fault_end": f"2026-01-01T{end}:00+00:00",
            "reason": "探头校准漂移",
        },
    )
    assert r.status_code == 201, r.text


def revoke(client, rid, fid, trip="T1"):
    r = client.post(
        f"/trips/{trip}/probe-faults/revoke",
        json={"revocation_id": rid, "fault_id": fid, "expected_version": 0},
    )
    assert r.status_code == 201, r.text


def first_interval(client, cargo="C1"):
    r = client.get(f"/cargo/{cargo}/responsibilities")
    assert r.status_code == 200
    intervals = r.json()["intervals"]
    assert intervals, "预期至少一个已闭合区间"
    return intervals[0]


def seal(client, case_id, cargo, interval_id, snapshot, expected=201):
    r = client.post(
        "/evidence-seals",
        json={
            "case_id": case_id,
            "cargo_id": cargo,
            "interval_id": interval_id,
            "snapshot_id": snapshot,
        },
    )
    assert r.status_code == expected, r.text
    return r


def build_interval_with_quarantined_readings(client):
    """m1 10:00 超限开启（自有）-> 区间内车载读数被故障隔离 -> m2 10:30 恢复。

    返回 (interval_id, snapshot_id)。故障 pf-1 [10:05,10:25) 覆盖 vr-1/vr-2；
    vr-3 与自有测温 m-mid 同刻（10:12），既被取代又被隔离。
    """
    setup_quarantine_scenario(client)
    own(client, "m1", "10:00", 8)       # 8 > 4 开启区间（自有）
    reading(client, "vr-1", "10:10", 9)
    reading(client, "vr-3", "10:12", 9)
    own(client, "m-mid", "10:12", 9)    # 与 vr-3 同刻：自有优先
    reading(client, "vr-2", "10:20", 9)
    own(client, "m2", "10:30", 2)       # 闭合区间（自有）
    fault(client, "pf-1")               # 隔离 vr-1 / vr-3 / vr-2
    iv = first_interval(client)
    assert iv["interval_id"] == "m1"
    assert iv["start"].startswith("2026-01-01T10:00")
    assert iv["end"].startswith("2026-01-01T10:30")
    return iv["interval_id"], iv["snapshot_id"]


# ----------------------------------------------------------- 封存内容


def test_seal_keeps_quarantined_readings_with_isolation_flags(client):
    iid, snap = build_interval_with_quarantined_readings(client)
    r = seal(client, "CASE-Q1", "C1", iid, snap)
    content = r.json()["content"]

    vrs = {x["reading_id"]: x for x in content["vehicle_readings"]}
    # 区间内、当前装卸窗口内的全部被隔离读数都随包保留
    assert set(vrs) == {"vr-1", "vr-2", "vr-3"}
    for rid in ("vr-1", "vr-2"):
        item = vrs[rid]
        assert item["quarantined"] is True
        assert item["covering_fault_ids"] == ["pf-1"]
        assert item["adopted"] is False  # 隔离读数不参与越温重建
        assert item["temperature"] == "9.000"  # 原始读数保留
        assert item["effective_temperature"] == "9.000"
        assert item["version"] == 0
        assert item["trip_id"] == "T1"
    # 同刻自有测温仍优先：vr-3 既被取代又被隔离，两者皆标
    assert vrs["vr-3"]["adopted"] is False
    assert vrs["vr-3"]["quarantined"] is True
    assert vrs["vr-3"]["covering_fault_ids"] == ["pf-1"]

    # 查询封存包：与封存回执完全一致（原文与摘要不可改）
    got = client.get("/evidence-seals/CASE-Q1")
    assert got.status_code == 200
    assert got.json() == r.json()

    # 封存后未再变更：对照 unchanged 且无任何证据项增删变更
    d = client.get("/evidence-seals/CASE-Q1/diff").json()
    assert d["status"] == "unchanged"
    assert d["evidence_changes"]["vehicle_readings"] == []
    assert d["evidence_changes"]["own_events"] == []
    assert d["segment_changes"] == []


def test_seal_quarantine_replay_returns_original_package(client):
    iid, snap = build_interval_with_quarantined_readings(client)
    created = seal(client, "CASE-Q2", "C1", iid, snap).json()

    # 同案件编号同请求重传：即使此后故障被撤销（现况已变）也返回原包
    revoke(client, "rv-1", "pf-1")
    r = seal(client, "CASE-Q2", "C1", iid, snap, expected=200)
    body = r.json()
    assert body["replayed"] is True
    assert body["seal"]["content"] == created["content"]
    assert body["seal"]["content_digest"] == created["content_digest"]
    vr1 = next(
        x for x in body["seal"]["content"]["vehicle_readings"]
        if x["reading_id"] == "vr-1"
    )
    assert vr1["quarantined"] is True  # 原包保持封存时隔离状态

    # 异请求（不同快照）-> 冲突，原包不变
    r = seal(
        client, "CASE-Q2", "C1", iid, "sha256:" + "0" * 64, expected=409
    )
    assert r.json()["error"]["code"] == "case_id_conflict"
    assert client.get("/evidence-seals/CASE-Q2").json() == created


# ----------------------------------------------------------- 对照：隔离状态跟踪


def test_diff_tracks_covering_set_shrink_and_restore(client):
    """重叠故障撤销一条后仍隔离（覆盖集合收缩）；撤销最后一条后恢复采用。"""
    setup_quarantine_scenario(client)
    own(client, "m1", "10:00", 8)
    reading(client, "vr-1", "10:10", 9)
    own(client, "m2", "10:30", 2)
    fault(client, "pf-1", "10:05", "10:25")
    fault(client, "pf-2", "10:08", "10:28")
    iv = first_interval(client)
    created = seal(client, "CASE-Q3", "C1", iv["interval_id"], iv["snapshot_id"]).json()
    vr1_sealed = next(
        x for x in created["content"]["vehicle_readings"]
        if x["reading_id"] == "vr-1"
    )
    assert vr1_sealed["covering_fault_ids"] == ["pf-1", "pf-2"]

    # 撤销 pf-2：vr-1 仍被 pf-1 覆盖 -> 覆盖集合收缩记为 changed
    revoke(client, "rv-2", "pf-2")
    d = client.get("/evidence-seals/CASE-Q3/diff").json()
    assert d["status"] == "unchanged"  # 区间边界 / 片段未变
    vr1d = next(
        x for x in d["evidence_changes"]["vehicle_readings"] if x["key"] == "vr-1"
    )
    assert vr1d["change"] == "changed"
    assert vr1d["fields"] == ["covering_fault_ids"]
    assert vr1d["sealed"]["covering_fault_ids"] == ["pf-1", "pf-2"]
    assert vr1d["current"]["covering_fault_ids"] == ["pf-1"]
    assert vr1d["current"]["quarantined"] is True
    assert vr1d["current"]["adopted"] is False

    # 撤销最后一条覆盖故障 pf-1：vr-1 恢复采用 -> 隔离状态变化记为 changed
    revoke(client, "rv-1", "pf-1")
    d = client.get("/evidence-seals/CASE-Q3/diff").json()
    assert d["status"] == "unchanged"
    vr1d = next(
        x for x in d["evidence_changes"]["vehicle_readings"] if x["key"] == "vr-1"
    )
    assert vr1d["change"] == "changed"
    assert vr1d["fields"] == ["adopted", "covering_fault_ids", "quarantined"]
    assert vr1d["current"]["quarantined"] is False
    assert vr1d["current"]["covering_fault_ids"] == []
    assert vr1d["current"]["adopted"] is True

    # 原包不被改写：仍为封存时的隔离状态
    got = client.get("/evidence-seals/CASE-Q3").json()
    assert got["content"] == created["content"]
    assert got["content_digest"] == created["content_digest"]


def test_diff_interval_changes_when_quarantined_closer_restored(client):
    """被隔离的闭合读数恢复采用后区间边界变化：对照 status=changed。"""
    setup_quarantine_scenario(client)
    own(client, "m1", "10:00", 8)        # 开启区间
    reading(client, "vr-close", "10:20", 2)  # 被隔离的闭合读数
    own(client, "m2", "10:40", 2)        # 隔离期间实际闭合点
    fault(client, "pf-1", "10:05", "10:25")
    iv = first_interval(client)
    assert iv["end"].startswith("2026-01-01T10:40")
    seal(client, "CASE-Q4", "C1", iv["interval_id"], iv["snapshot_id"])

    revoke(client, "rv-1", "pf-1")
    d = client.get("/evidence-seals/CASE-Q4/diff").json()
    assert d["status"] == "changed"
    assert d["current_interval"]["end"] == "2026-01-01T10:20:00Z"
    seg = next(x for x in d["segment_changes"] if x["start"].endswith("10:00:00Z"))
    assert seg["change"] == "changed"
    vrd = next(
        x
        for x in d["evidence_changes"]["vehicle_readings"]
        if x["key"] == "vr-close"
    )
    assert vrd["change"] == "changed"
    assert vrd["current"]["quarantined"] is False
    assert vrd["current"]["adopted"] is True


def test_reading_quarantined_after_seal_marked_removed(client):
    """封存后才被隔离的读数在对照中记为 removed（既有语义保持）。"""
    setup_quarantine_scenario(client)
    own(client, "m1", "10:00", 8)
    reading(client, "vr-1", "10:10", 9)
    own(client, "m2", "10:30", 2)
    iv = first_interval(client)
    created = seal(client, "CASE-Q5", "C1", iv["interval_id"], iv["snapshot_id"]).json()
    vr1_sealed = next(
        x for x in created["content"]["vehicle_readings"]
        if x["reading_id"] == "vr-1"
    )
    assert vr1_sealed["quarantined"] is False
    assert vr1_sealed["adopted"] is True

    fault(client, "pf-1")  # 封存后登记故障，覆盖 vr-1
    d = client.get("/evidence-seals/CASE-Q5/diff").json()
    assert d["status"] == "unchanged"  # 区间由自有测温开闭，边界不变
    vr1d = next(
        x for x in d["evidence_changes"]["vehicle_readings"] if x["key"] == "vr-1"
    )
    assert vr1d["change"] == "removed"
    assert vr1d["sealed"]["quarantined"] is False
    assert vr1d["current"] is None

    # 撤销后读数恢复：对照回到无任何变更
    revoke(client, "rv-1", "pf-1")
    d = client.get("/evidence-seals/CASE-Q5/diff").json()
    assert d["status"] == "unchanged"
    assert d["evidence_changes"]["vehicle_readings"] == []


# ----------------------------------------------------------- 旧格式封存包兼容


def _strip_quarantine_fields(case_id: str):
    """把封存包内容改写成旧格式（车载读数无隔离字段），模拟本轮之前的包。"""
    db = SessionLocal()
    try:
        row = db.get(EvidenceSeal, case_id)
        content = json.loads(row.content)
        for item in content["vehicle_readings"]:
            item.pop("quarantined", None)
            item.pop("covering_fault_ids", None)
        canonical = sealing.canonical_json(content)
        row.content = canonical
        row.content_digest = sealing.digest_of(canonical)
        db.commit()
    finally:
        db.close()


def test_legacy_package_without_quarantine_fields_stays_compatible(client):
    setup_quarantine_scenario(client)
    own(client, "m1", "10:00", 8)
    reading(client, "vr-1", "10:10", 9)
    own(client, "m2", "10:30", 2)
    iv = first_interval(client)
    seal(client, "CASE-OLD", "C1", iv["interval_id"], iv["snapshot_id"])
    _strip_quarantine_fields("CASE-OLD")

    # 查询：旧格式包正常返回，新增隔离字段按缺省值（false / []）给出
    got = client.get("/evidence-seals/CASE-OLD")
    assert got.status_code == 200
    vr1 = next(
        x for x in got.json()["content"]["vehicle_readings"]
        if x["reading_id"] == "vr-1"
    )
    assert vr1["quarantined"] is False
    assert vr1["covering_fault_ids"] == []

    # 对照：新增字段不产生伪变更
    d = client.get("/evidence-seals/CASE-OLD/diff").json()
    assert d["status"] == "unchanged"
    assert d["evidence_changes"]["vehicle_readings"] == []

    # 封存后才隔离：旧格式包同样记为 removed（既有语义保持）
    fault(client, "pf-1")
    d = client.get("/evidence-seals/CASE-OLD/diff").json()
    vr1d = next(
        x for x in d["evidence_changes"]["vehicle_readings"] if x["key"] == "vr-1"
    )
    assert vr1d["change"] == "removed"


# ----------------------------------------------------------- 既有拒绝语义不变


def test_seal_rejections_unchanged_with_faults(client):
    """区间消失 / 开放 / 快照过期 / 案件编号冲突沿用现有拒绝且不留记录。"""
    setup_quarantine_scenario(client)
    own(client, "m1", "10:00", 8)
    reading(client, "vr-1", "10:10", 9)
    own(client, "m2", "10:30", 2)
    iv = first_interval(client)
    iid, snap = iv["interval_id"], iv["snapshot_id"]

    # 快照过期
    r = seal(client, "CASE-Q6", "C1", iid, "sha256:" + "0" * 64, expected=409)
    assert r.json()["error"]["code"] == "stale_snapshot"

    # 故障隔离开启点之外的读数不改变区间；更正开启测温使区间消失 -> 404
    fault(client, "pf-1")
    assert client.post(
        "/corrections",
        json={
            "correction_id": "c-m1",
            "target_type": "own_event",
            "target_id": "m1",
            "expected_version": 0,
            "new_temperature": "3",
        },
    ).status_code == 201
    r = seal(client, "CASE-Q7", "C1", iid, snap, expected=404)
    assert r.json()["error"]["code"] == "interval_not_found"

    # 开放区间 422
    own(client, "m3", "11:00", 9)
    r = seal(client, "CASE-Q8", "C1", "m3", "sha256:" + "0" * 64, expected=422)
    assert r.json()["error"]["code"] == "interval_open"

    db = SessionLocal()
    try:
        assert db.query(EvidenceSeal).count() == 0
    finally:
        db.close()
