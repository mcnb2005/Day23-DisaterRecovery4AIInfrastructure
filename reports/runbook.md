# Runbook — Region chính down

Phạm vi: bare-mode local lab, primary `a`, target `b`, snapshot backend `fs`. Luồng bán tự động chuẩn là:

```bash
python dr/runbook.py --primary a --target b --backend fs
```

Không dùng `--auto` trong incident thật; operator phải xác nhận `y`. Chỉ CI/game day dùng `--auto`.

| # | Bước | Lệnh copy-paste | Biết là xong khi | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | Region A không ready ở 3 probe liên tiếp; Region B vẫn `alive:true` | SRE on-call |
| 2 | Mở incident, bắt đầu clock | `python -c "import time; print('SEV-1 opened, operator_ts=', time.time())"` | Incident có timestamp và Incident Commander (IC) được chỉ định; runbook log có `thong_bao_incident` | SRE on-call |
| 3 | Restore state ở B | `python state/snapshot.py get --region b --backend fs` | Output có `embed_model_version`, `snapshot_at`, `restored_at`; không có lỗi thiếu `MANIFEST.json` | AI platform |
| 4 | Scale pool và chờ readiness | `printf full > state/region-b/pool_state && until curl -sf http://127.0.0.1:8002/readyz; do sleep 1; done` | `/readyz` của B trả HTTP 200, vector count > 0, weights có mặt | Capacity/GPU on-call |
| 5 | DNS/LB cutover | `printf b > edge/active_region && curl -s http://127.0.0.1:8080/edge/state` | `active_region` là `b`; chỉ thực hiện sau bước 4 | SRE on-call, IC phê duyệt |
| 6 | Verify golden signals | `for i in $(seq 1 10); do curl -sf http://127.0.0.1:8002/v1/infer || break; done` | 10/10 request HTTP 200, response có `region:b`; p95 < 100ms, error rate = 0% | Service owner |
| 7 | Đo RTO/RPO và mở postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, `warnings:[]`, `rto_verdict:PASS`, RPO có cả giây và `docs_lost` | IC + SRE |

## Điều kiện dừng và rollback

- Dừng failover trước cutover nếu restore lỗi, model version không khớp, vector count bằng 0, hoặc B không trả `/readyz` 200 trong 60s. Không đổi `edge/active_region` trong các trường hợp này.
- Sau cutover, rollback/failback về A chỉ khi A đã `/readyz` 200 ở 3 probe liên tiếp, state từ B đã được đồng bộ và đối soát, và B có error rate > 1% hoặc IC phê duyệt planned failback.
- Chỉ Incident Commander có quyền ra quyết định rollback. SRE on-call thực thi; AI platform owner xác nhận state trước khi chuyển traffic.
- Trước planned failback, chụp state mới nhất từ B bằng `python state/snapshot.py put --region b --backend fs`, khởi động và kiểm tra Region A, rồi dùng `python dr/failover.py --target a --backend fs`. Nếu A không ready, lệnh phải abort mà không cutover.
