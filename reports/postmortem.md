# Postmortem - DR Drill Lab 23

Day la postmortem blameless: muc tieu la tim khoang trong cua he thong va quy trinh, khong quy loi cho nguoi van hanh.

## 1. Timeline

| ISO time (UTC) | Su kien | Evidence |
|---|---|---|
| 2026-10-09T12:16:39Z | Outage Region A bat dau | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T12:16:39Z (+0.3s) | User dau tien nhan loi 503 | `reports/drill-2-withdr.jsonl:22` |
| 2026-10-09T12:16:55Z (+16.8s) | Health checker danh dau A `UNHEALTHY` | `reports/health-events.jsonl:2` |
| 2026-10-09T12:16:56Z (+16.9s) | Operator xac nhan incident va cho phep failover | `reports/runbook-run.jsonl:2` |
| 2026-10-09T12:17:02Z (+23.3s) | Region B ready va DNS/LB cutover xong | `reports/failover-events.jsonl:4` |
| 2026-10-09T12:17:03Z (+24.6s) | Request dau tien thanh cong tu Region B | `reports/drill-2-withdr.jsonl:34` |

## 2. RTO/RPO va gap analysis

- RTO target: 300s; measured: **24.6s**; gap/headroom: **275.4s**.
- RPO target: 300s; measured: **2.0s**, tuong duong **1 document** bi mat; gap/headroom: **298.0s**.
- Buoc ton nhieu thoi gian nhat la health-check detection: 16.77s, trong do policy floor la 15.0s. No chiem 68.2% RTO neu tinh toan bo detection, hoac 61.0% neu chi tinh floor.
- Snapshot restore sau alert khoang 0.19s; GPU warm-up khoang 6.35s; cutover/TTL den request ke tiep khoang 1.29s.

## 3. Root cause - 5 whys

1. User nhan 503 vi edge van gui request toi Region A sau khi serving process cua A ngung phan hoi.
2. Edge tiep tuc dung A vi active-region pointer khong tu doi chi dua tren liveness.
3. Pointer chi duoc doi sau health alert co threshold va quyet dinh failover ban tu dong, nham ngan flapping.
4. Region B chua nhan traffic ngay duoc vi ban dau khong co vector snapshot/model weights va GPU pool chi o trang thai `warm`.
5. Kha nang phuc hoi phu thuoc ba lop doc lap: replication con moi, target restore duoc, va runbook chi cutover sau readiness. Drill xac nhan ca ba lop hoat dong; rui ro lon nhat con lai la thoi gian phat hien va kha nang snapshot/restore chua duoc test lien tuc ngoai game day.

## 4. Action items

| # | Action item | Owner | Deadline | Tac dong du kien |
|---|---|---|---|---|
| 1 | Thu policy `interval=2s`, `threshold=3` trong 7 ngay va theo doi false positive/flapping | SRE on-call lead | 2026-10-16 | Giam detect floor tu 15s xuong 6s, tiet kiem toi da khoang 9s RTO |
| 2 | Chay restore-canary hang ngay, kiem tra vector count, model version va 10 golden requests | AI platform owner | 2026-10-23 | Phat hien snapshot hong truoc incident, giam rui ro `NO_RECOVERY` |
| 3 | Danh gia warm standby co mot worker luon `full` tai Region B | Capacity owner | 2026-10-30 | Co the bo khoang 6.35s GPU warm-up, doi lai chi phi capacity cao hon |

## 5. Ba cau hoi bat buoc

1. `interval x threshold = 5s x 3 = 15.0s`. Detect floor chiem **61.0%** RTO 24.6s; detection thuc te 16.77s chiem 68.2%.
2. Neu ha interval xuong 1s va giu threshold 3, detect floor giam tu 15s xuong 3s, nen RTO ly tuong giam khoang 12s, tu 24.6s xuong khoang 12.6s. Doi lai la probe tang 5 lan, nhay hon voi nhieu mang, tang alert noise va nguy co flapping; van can threshold, cooldown va circuit breaker.
3. `docs_lost=1` nghia la mot ticket/document da co o primary nhung chua vao snapshot duoc restore. Khach hang co the khong tim thay du lieu do o Region B; can replay tu durable event log hoac re-ingest idempotent roi doi soat sau failover.
