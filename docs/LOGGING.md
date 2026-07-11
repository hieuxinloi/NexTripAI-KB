# Logging

KB dung Loguru theo pattern cua Orchestrator va nhan `X-Request-ID` tu BE.

Format:

```text
timestamp | level | nextrip-kb | request_id=<id> | component event key=value elapsed_ms=<n>
```

Mot search request co cac moc:

```text
HTTP request start
KB search start
Retrieval start
Retrieval step end: text_unit_vector_search
Retrieval step end: text_unit_keyword_search
Retrieval step end: graph_filter_search
Retrieval end: result IDs va candidate counts
KB search end
HTTP request end
```

Log chi ghi query da compact/truncate, filters, counts, IDs, status va latency. Khong log embedding,
article text, credentials hay full answer payload.

Xem log realtime:

```powershell
Get-Content kb-api.err.log -Wait
```

Dieu chinh level trong `.env`:

```text
LOG_LEVEL=INFO
```
