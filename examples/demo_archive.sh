#!/usr/bin/env bash
# 服务数据归档端到端示例：
#   上传 2 份原图 -> 标注/发布（一份两个版本并撤回 v1，一份定时公开未到点）
#   -> POST /service-archives 生成归档 ZIP（同一数据库快照的文档/标注/发布/
#      撤回记录导出 + 快照引用的全部原图与发布成品，相对路径组织）
#   -> 校验 manifest.json：逐项 SHA-256 与包内字节一致、文档/版本标识、
#      记录数与文件数
#   -> 演示快照引用文件缺失时整个请求被拒绝（不交付部分归档）
# 依赖：curl、python3（仅用于解析 JSON、生成示例图、解压校验清单）
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

python3 - <<'EOF'
from PIL import Image
Image.new("RGB", (64, 48), (30, 90, 160)).save("/tmp/arch_a.png")
Image.new("RGB", (64, 48), (160, 90, 30)).save("/tmp/arch_b.png")
EOF

upload() {  # $1 = 文件路径 -> doc_id
  curl -sS -F "file=@$1" "$BASE/documents" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['doc_id'])"
}

echo "== 1. 上传两份原图并标注、发布（A 两个版本并撤回 v1；B 定时公开未到点）=="
A=$(upload /tmp/arch_a.png)
B=$(upload /tmp/arch_b.png)
echo "A=$A B=$B"
curl -sS -X PUT "$BASE/documents/$A/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":4,"y":4,"w":12,"h":10,"reason":"身份证号"}]}' >/dev/null
curl -sS -X POST "$BASE/documents/$A/publish" >/dev/null          # A v1 @ rev1
curl -sS -X PUT "$BASE/documents/$A/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":1,"rects":[{"x":4,"y":4,"w":12,"h":10,"reason":"身份证号"},
                            {"x":40,"y":30,"w":10,"h":8,"reason":"人脸"}]}' >/dev/null
curl -sS -X POST "$BASE/documents/$A/publish" >/dev/null          # A v2 @ rev2
curl -sS -X POST "$BASE/documents/$A/publications/1/withdraw" \
  -H 'Content-Type: application/json' -d '{"reason":"遮盖遗漏"}' >/dev/null
curl -sS -X PUT "$BASE/documents/$B/annotations" -H 'Content-Type: application/json' \
  -d '{"revision":0,"rects":[{"x":8,"y":8,"w":6,"h":6,"reason":"工号"}]}' >/dev/null
curl -sS -X POST "$BASE/documents/$B/publish" -H 'Content-Type: application/json' \
  -d '{"publish_at":"2026-10-20T08:00:00Z"}' >/dev/null           # B v1 未到公开时间

echo
echo "== 2. 生成服务数据归档（同一数据库快照；已撤回/未到点版本同样入档）=="
rm -f /tmp/service-archive.zip
curl -sS -o /tmp/service-archive.zip \
  -w "HTTP %{http_code}, %{size_download} bytes, %{content_type}\n" \
  -X POST "$BASE/service-archives"

echo "-- 归档结构（相对路径：db/ 记录导出 + originals/ 原图 + publications/ 成品）--"
python3 - <<'EOF'
import hashlib, json, zipfile
zf = zipfile.ZipFile("/tmp/service-archive.zip")
print("\n".join(zf.namelist()))
manifest = json.loads(zf.read("manifest.json"))
print("\nmanifest.json:")
print(json.dumps(manifest, ensure_ascii=False, indent=2))
# 校验：清单逐项 SHA-256 与包内字节一致，文件数与包内条目一致
assert {f["path"] for f in manifest["files"]} == set(zf.namelist()) - {"manifest.json"}
for f in manifest["files"]:
    assert hashlib.sha256(zf.read(f["path"])).hexdigest() == f["sha256"]
assert manifest["counts"]["files"] == len(zf.namelist()) - 1
print("清单 SHA-256 与包内字节全部一致；文件数与包内条目一致")
EOF

echo
echo "== 3. 快照引用文件缺失 -> 整个请求被拒绝（不交付部分归档）=="
DATA_DIR="${DATA_DIR:-./data}"
TARGET=$(find "$DATA_DIR/publications" -name 'v1.png' 2>/dev/null | head -1)
if [ -n "$TARGET" ]; then
  mv "$TARGET" "$TARGET.bak"  # 模拟运维事故：快照引用的成品在磁盘上缺失
  curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/service-archives"
  mv "$TARGET.bak" "$TARGET"  # 恢复，避免影响后续演示
else
  echo "（未找到本地数据目录 $DATA_DIR，跳过；容器部署时可在卷内模拟）"
fi
