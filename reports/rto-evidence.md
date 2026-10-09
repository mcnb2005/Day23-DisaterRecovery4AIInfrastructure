# RTO/RPO Evidence - Lab 23

Tat ca so lieu duoi day duoc tinh tu timestamp cua chinh lan drill ngay 2026-10-09.

## 1. Drill 1 - khong co DR

| Chi so | Gia tri | Cach do | Evidence |
|---|---:|---|---|
| t_outage | `2026-10-09T12:11:11Z` | Chaos kill Region A | `chaos/chaos-events.jsonl:1` |
| Request fail dau tien | `+0.5s` | `1791547871.492442 - 1791547871.0007706` | `reports/drill-1-nodr.jsonl:18` |
| Request thanh cong sau do | Khong co | Moi request con lai den cuoi drill deu loi | `reports/drill-1-nodr.jsonl:33` |
| RTO | `NO_RECOVERY` | Khong co `ok:true` sau request loi dau tien | `reports/drill-1-nodr.jsonl:18` |

Baseline co 16 request that bai va khong co co che nao tu doi edge khoi Region A.

## 2. Drill 2 - co DR

| Moc | +giay tu t_outage | Cach do | Evidence |
|---|---:|---|---|
| t_outage | `0.0s` | `action:kill`, Region A | `chaos/chaos-events.jsonl:3` |
| User thay loi dau tien | `0.3s` | Dong `ok:false` dau tien sau outage | `reports/drill-2-withdr.jsonl:22` |
| Health checker phat hien | `16.8s` | Region A thanh `UNHEALTHY` sau 3 lan fail lien tiep | `reports/health-events.jsonl:2` |
| Snapshot restore xong | `17.0s` | Hoan tat `2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region B ready | `23.3s` | `4_wait_ready`, warm-up mat 6.36s | `reports/failover-events.jsonl:4` |
| DNS/LB cutover | `23.3s` | Edge doi tu A sang B | `reports/failover-events.jsonl:5` |
| **Request dau tien phuc hoi** | **`24.6s`** | Request `ok:true`, `served_by:b` dau tien sau loi | `reports/drill-2-withdr.jsonl:34` |

| Chi so | Do duoc | Muc tieu | Verdict |
|---|---:|---:|---|
| RTO - Inference API | **`24.6s`** | 300s | **PASS** |
| RPO - Vector DB | **`2.0s` / `1` document** | 300s | **PASS** |

RPO la chenh lech `primary_latest_doc_ts` va `restored_latest_doc_ts`, kem so document o primary moi hon ban restore. Model version khoi phuc la `embed-model=vi-e5-base@v3` (`reports/failover-events.jsonl:2`).

## 3. RTO breakdown

| Thanh phan | Giay | Nguon | Cach giam |
|---|---:|---|---|
| Health-check detection | `16.77s` | `interval_s=5.0`, `threshold=3`, detect floor `15.0s`; phan con lai la timeout/lich poll (`reports/health-events.jsonl:2`) | Giam interval nhung giu threshold/circuit breaker de tranh flapping |
| Snapshot restore + verify | `0.19s` | Tu health alert den `2_restore_snapshot` (`reports/failover-events.jsonl:2`) | Dat snapshot gan compute va test restore dinh ky |
| GPU pool warm-up | `6.35s` | Tu restore/scale den target ready; log ghi `waited_s=6.36` (`reports/failover-events.jsonl:4`) | Giu warm standby hoac pre-warm mot phan capacity |
| DNS/LB TTL va request ke tiep | `1.29s` | Tu cutover den request thanh cong dau tien (`reports/failover-events.jsonl:5`, `reports/drill-2-withdr.jsonl:34`) | Giam TTL hoac dung global LB health routing |
| **Tong RTO** | **`24.60s`** | Tong bon thanh phan, khop RTO do tu loadgen | - |
