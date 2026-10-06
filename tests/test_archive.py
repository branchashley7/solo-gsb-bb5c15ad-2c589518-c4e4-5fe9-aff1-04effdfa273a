"""服务数据归档测试：同一快照的记录导出 + 引用文件 ZIP。

覆盖：
- 成功归档：包内相对路径结构、db/ 四类记录导出内容、原图与成品字节、
  清单 SHA-256 与文档/版本标识、记录数与文件数；已撤回与未到公开时间的
  版本同样入档（运维归档不做公开面门控）；
- 空库归档：四类导出为空、文件数为零；
- 同一一致时点：快照建立后并发提交的写入不进入本次归档，且归档生成
  （只读快照）不阻塞并发写入；
- 快照引用文件缺失/摘要不符：整个请求拒绝（原图/成品），不交付部分归档；
- 临时 ZIP 在交付后与失败后均清理。
"""
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import Settings
from app.main import create_app
from tests.test_api import jpeg_bytes, png_bytes, upload  # noqa: E402,F401


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _new_doc(client, color=(10, 20, 30), filename="img.png"):
    return upload(client, png_bytes(color=color), filename).json()["doc_id"]


def _annotate(client, doc_id, revision, rects):
    r = client.put(
        f"/documents/{doc_id}/annotations",
        json={"revision": revision, "rects": rects},
    )
    assert r.status_code == 200, r.text


def _publish(client, doc_id, **kwargs):
    r = client.post(f"/documents/{doc_id}/publish", json=kwargs or None)
    assert r.status_code == 201, r.text
    return r.json()


def _withdraw(client, doc_id, version, reason="遮盖遗漏：右下角工号未遮盖"):
    return client.post(
        f"/documents/{doc_id}/publications/{version}/withdraw",
        json={"reason": reason},
    )


def _unzip(response) -> zipfile.ZipFile:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    return zipfile.ZipFile(io.BytesIO(response.content))


def _db_value(client, sql, args):
    settings = client.app.state.settings
    db = sqlite3.connect(settings.db_path)
    db.row_factory = sqlite3.Row
    try:
        return db.execute(sql, args).fetchone()[0]
    finally:
        db.close()


# ---------------- 成功归档 ----------------

def test_archive_success_structure_records_and_manifest(client, monkeypatch):
    # 固定时钟：定时公开时间可预期
    clock = {"now": datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(main_module, "_now_dt", lambda: clock["now"])
    future = "2026-10-20T08:00:00Z"

    # d1：两个标注修订、两个发布版本，v1 已撤回
    d1_data = png_bytes(color=(10, 20, 30))
    d1 = upload(client, d1_data, "scan-a.png").json()["doc_id"]
    _annotate(client, d1, 0, [
        {"x": 0, "y": 0, "w": 4, "h": 4, "reason": "甲"},
        {"x": 8, "y": 8, "w": 2, "h": 2, "reason": "乙"},
    ])
    p11 = _publish(client, d1)                      # v1 @ rev1
    _annotate(client, d1, 1, [{"x": 1, "y": 1, "w": 3, "h": 3, "reason": "丙"}])
    p12 = _publish(client, d1)                      # v2 @ rev2
    assert _withdraw(client, d1, 1).status_code == 201

    # d2：JPEG 原图，定时公开（未到点）——运维归档不做公开面门控，照常入档
    d2_data = jpeg_bytes(color=(40, 50, 60))
    d2 = upload(client, d2_data, "scan-b.jpg").json()["doc_id"]
    _annotate(client, d2, 0, [{"x": 2, "y": 2, "w": 3, "h": 3, "reason": "丁"}])
    p21 = _publish(client, d2, publish_at=future)   # v1 @ rev1，未到公开时间

    zf = _unzip(client.post("/service-archives"))
    names = zf.namelist()
    # db/ 导出固定在前、清单固定在最后；原图与成品按相对路径组织
    assert names[:4] == [
        "db/documents.json",
        "db/annotations.json",
        "db/publications.json",
        "db/withdrawals.json",
    ]
    assert names[-1] == "manifest.json"
    assert set(names[4:-1]) == {
        f"originals/{d1}.png",
        f"originals/{d2}.jpg",
        f"publications/{d1}/v1.png",
        f"publications/{d1}/v2.png",
        f"publications/{d2}/v1.png",
    }
    # 全部为相对路径，无目录穿越
    for name in names:
        assert not name.startswith("/") and ".." not in name.split("/")

    # db/documents.json：文档记录与归档内原图相对路径
    documents = json.loads(zf.read("db/documents.json"))
    assert documents["count"] == 2
    by_id = {d["doc_id"]: d for d in documents["documents"]}
    assert set(by_id) == {d1, d2}
    assert by_id[d1]["file"] == f"originals/{d1}.png"
    assert by_id[d1]["media_type"] == "image/png"
    assert by_id[d1]["annotation_revision"] == 2
    assert by_id[d1]["original_sha256"] == hashlib.sha256(d1_data).hexdigest()
    assert by_id[d2]["file"] == f"originals/{d2}.jpg"
    assert by_id[d2]["media_type"] == "image/jpeg"
    assert by_id[d2]["original_sha256"] == hashlib.sha256(d2_data).hexdigest()

    # db/annotations.json：全部历史修订矩形（追加存储、永不改写）
    annotations = json.loads(zf.read("db/annotations.json"))
    assert annotations["count"] == 4
    per_doc_rev = {}
    for a in annotations["annotations"]:
        per_doc_rev.setdefault((a["doc_id"], a["revision"]), []).append(a)
    assert [a["reason"] for a in per_doc_rev[(d1, 1)]] == ["甲", "乙"]
    assert [a["reason"] for a in per_doc_rev[(d1, 2)]] == ["丙"]
    assert [a["reason"] for a in per_doc_rev[(d2, 1)]] == ["丁"]
    assert per_doc_rev[(d1, 1)][0] == {
        "doc_id": d1, "revision": 1, "idx": 0,
        "x": 0, "y": 0, "w": 4, "h": 4, "reason": "甲",
    }

    # db/publications.json：含已撤回与未到公开时间的版本（运维归档全量入档）
    publications = json.loads(zf.read("db/publications.json"))
    assert publications["count"] == 3
    by_ver = {(p["doc_id"], p["version"]): p for p in publications["publications"]}
    assert set(by_ver) == {(d1, 1), (d1, 2), (d2, 1)}
    assert by_ver[(d1, 1)]["file"] == f"publications/{d1}/v1.png"
    assert by_ver[(d1, 1)]["output_sha256"] == p11["output_sha256"]
    assert by_ver[(d1, 1)]["annotation_revision"] == 1
    assert by_ver[(d1, 1)]["batch_id"] is None
    assert by_ver[(d1, 1)]["publish_at"] is None
    assert by_ver[(d1, 2)]["annotation_revision"] == 2
    assert by_ver[(d2, 1)]["publish_at"] == future

    # db/withdrawals.json：撤回记录（含原因与时间）
    withdrawals = json.loads(zf.read("db/withdrawals.json"))
    assert withdrawals["count"] == 1
    assert withdrawals["withdrawals"][0]["doc_id"] == d1
    assert withdrawals["withdrawals"][0]["version"] == 1
    assert withdrawals["withdrawals"][0]["reason"] == "遮盖遗漏：右下角工号未遮盖"
    assert withdrawals["withdrawals"][0]["withdrawn_at"]

    # 原图与成品字节：与上传字节/发布记录摘要一致
    assert zf.read(f"originals/{d1}.png") == d1_data
    assert zf.read(f"originals/{d2}.jpg") == d2_data
    for pub, expected in (
        ((d1, 1), p11), ((d1, 2), p12), ((d2, 1), p21),
    ):
        raw = zf.read(f"publications/{pub[0]}/v{pub[1]}.png")
        assert hashlib.sha256(raw).hexdigest() == expected["output_sha256"]

    # manifest.json：逐项 SHA-256 与文档/版本标识，及记录数与文件数
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["created_at"]
    assert manifest["counts"] == {
        "documents": 2,
        "annotation_rects": 4,
        "publications": 3,
        "withdrawals": 1,
        "record_files": 4,
        "original_files": 2,
        "publication_files": 3,
        "files": 9,
    }
    files = {f["path"]: f for f in manifest["files"]}
    # 清单覆盖包内除自身外的全部文件，摘要与包内字节一致
    assert set(files) == set(names) - {"manifest.json"}
    for path, entry in files.items():
        assert hashlib.sha256(zf.read(path)).hexdigest() == entry["sha256"]
    # 文档/版本标识：原图带 doc_id，成品带 doc_id+version，db/ 导出不带
    assert files[f"originals/{d1}.png"]["doc_id"] == d1
    assert "version" not in files[f"originals/{d1}.png"]
    assert files[f"publications/{d1}/v2.png"]["doc_id"] == d1
    assert files[f"publications/{d1}/v2.png"]["version"] == 2
    assert "doc_id" not in files["db/documents.json"]


def test_archive_empty_database(client):
    zf = _unzip(client.post("/service-archives"))
    assert zf.namelist() == [
        "db/documents.json",
        "db/annotations.json",
        "db/publications.json",
        "db/withdrawals.json",
        "manifest.json",
    ]
    for key in ("documents", "annotations", "publications", "withdrawals"):
        payload = json.loads(zf.read(f"db/{key}.json"))
        assert payload["count"] == 0
        assert payload[key] == []
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["counts"] == {
        "documents": 0,
        "annotation_rects": 0,
        "publications": 0,
        "withdrawals": 0,
        "record_files": 4,
        "original_files": 0,
        "publication_files": 0,
        "files": 4,
    }
    assert {f["path"] for f in manifest["files"]} == set(zf.namelist()) - {
        "manifest.json"
    }


# ---------------- 同一一致时点：并发写入可继续，但不进入本次归档 ----------------

def test_archive_single_snapshot_and_concurrent_writes_continue(client):
    d1 = _new_doc(client, (10, 20, 30))
    _annotate(client, d1, 0, [{"x": 0, "y": 0, "w": 4, "h": 4, "reason": "甲"}])
    _publish(client, d1)  # v1 @ rev1

    snapshot_taken = threading.Event()
    release = threading.Event()
    outcome = {}

    def hook():
        snapshot_taken.set()
        release.wait(timeout=10)

    def archiver():
        outcome["r"] = client.post("/service-archives")

    main_module.ARCHIVE_SNAPSHOT_HOOK = hook
    try:
        t = threading.Thread(target=archiver)
        t.start()
        assert snapshot_taken.wait(timeout=10)
        # 归档生成进行中（只读快照已建立）：并发写入不被阻塞、立即成功
        _annotate(client, d1, 1, [])  # 修订 1 -> 2（清空遮盖）
        d2 = _new_doc(client, (1, 2, 3))
        _annotate(client, d2, 0, [{"x": 1, "y": 1, "w": 2, "h": 2, "reason": "乙"}])
        _publish(client, d2)
        assert _withdraw(client, d1, 1).status_code == 201
        release.set()
        t.join(timeout=20)
    finally:
        main_module.ARCHIVE_SNAPSHOT_HOOK = None

    # 归档只包含快照时点已提交的数据：新文档、新修订、新发布、新撤回均不入档
    zf = _unzip(outcome["r"])
    documents = json.loads(zf.read("db/documents.json"))
    assert documents["count"] == 1
    assert documents["documents"][0]["doc_id"] == d1
    assert documents["documents"][0]["annotation_revision"] == 1
    annotations = json.loads(zf.read("db/annotations.json"))
    assert annotations["count"] == 1
    assert annotations["annotations"][0]["revision"] == 1
    publications = json.loads(zf.read("db/publications.json"))
    assert [(p["doc_id"], p["version"]) for p in publications["publications"]] == [
        (d1, 1)
    ]
    withdrawals = json.loads(zf.read("db/withdrawals.json"))
    assert withdrawals["count"] == 0
    names = zf.namelist()
    assert f"originals/{d1}.png" in names
    assert f"publications/{d1}/v1.png" in names
    assert not any(n.startswith(f"originals/{d2}") for n in names)
    assert not any(n.startswith(f"publications/{d2}/") for n in names)
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["counts"]["documents"] == 1
    assert manifest["counts"]["publications"] == 1
    assert manifest["counts"]["withdrawals"] == 0

    # 归档生成期间的并发写入确实已生效（快照结束后可见）
    doc = client.get(f"/documents/{d1}").json()
    assert doc["annotation_revision"] == 2
    assert len(doc["publications"]) == 1
    w = client.get(f"/documents/{d1}/publications/1/withdrawal").json()
    assert w["withdrawn"] is True
    assert client.get(f"/documents/{d2}").status_code == 200


# ---------------- 快照引用文件缺失/摘要不符：拒绝整个请求 ----------------

def test_archive_missing_original_rejects_whole_request(client):
    d = _new_doc(client)
    _publish(client, d)
    path = _db_value(
        client, "SELECT original_path FROM documents WHERE id = ?", (d,)
    )
    os.unlink(path)  # 快照引用的原图缺失

    r = client.post("/service-archives")
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "original_missing"
    assert detail["doc_id"] == d
    assert r.headers["content-type"] != "application/zip"  # 不交付部分归档


def test_archive_corrupt_original_rejects_whole_request(client):
    d = _new_doc(client)
    _publish(client, d)
    path = _db_value(
        client, "SELECT original_path FROM documents WHERE id = ?", (d,)
    )
    with open(path, "wb") as f:
        f.write(b"tampered-original")  # 摘要与文档记录不符

    r = client.post("/service-archives")
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "original_corrupt"
    assert detail["doc_id"] == d
    assert r.headers["content-type"] != "application/zip"


def test_archive_missing_artifact_rejects_whole_request(client):
    d = _new_doc(client)
    _publish(client, d)
    path = _db_value(
        client,
        "SELECT output_path FROM publications WHERE doc_id = ? AND version = 1",
        (d,),
    )
    os.unlink(path)  # 快照引用的发布成品缺失

    r = client.post("/service-archives")
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "artifact_missing"
    assert detail["doc_id"] == d and detail["version"] == 1
    assert r.headers["content-type"] != "application/zip"


def test_archive_corrupt_artifact_rejects_whole_request(client):
    d = _new_doc(client)
    _publish(client, d)
    path = _db_value(
        client,
        "SELECT output_path FROM publications WHERE doc_id = ? AND version = 1",
        (d,),
    )
    with open(path, "wb") as f:
        f.write(b"not-the-original-artifact")  # 摘要与发布记录不符

    r = client.post("/service-archives")
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "artifact_corrupt"
    assert detail["doc_id"] == d and detail["version"] == 1
    assert r.headers["content-type"] != "application/zip"


# ---------------- 临时文件清理 ----------------

def test_archive_temp_zip_is_cleaned_up_after_delivery(client):
    _new_doc(client)
    r = client.post("/service-archives")
    assert r.status_code == 200
    # BackgroundTasks 在 TestClient 响应返回后执行
    leftovers = [n for n in os.listdir(tempfile.gettempdir())
                 if n.startswith("svc-archive-")]
    assert leftovers == []


def test_archive_temp_zip_is_cleaned_up_on_failure(client):
    d = _new_doc(client)
    path = _db_value(
        client, "SELECT original_path FROM documents WHERE id = ?", (d,)
    )
    os.unlink(path)
    r = client.post("/service-archives")
    assert r.status_code == 500
    leftovers = [n for n in os.listdir(tempfile.gettempdir())
                 if n.startswith("svc-archive-")]
    assert leftovers == []
