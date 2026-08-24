# Canonical identity: one-time historical migration

Tai lieu nay chi mo ta cach migrate mot historical seed duoc cung cap tu ben
ngoai repository: gop cac record cung mot dia diem, giu ID cu lam
alias/tombstone, tim dia diem moi va materialize canonical dataset dau tien.
Historical source snapshot trong repository da duoc xoa; khong co master JSON
thu hai song song voi canonical.

Workflow hien hanh khong chay lai migration nay. `NEXTRIP_CANONICAL_DATASET`
pin mot publish-ready immutable canonical active dataset lam nguon `Place` duy
nhat cho Neo4j V8, traffic va Current Data. Crawl theo lich tao raw evidence,
normalize/validate/decision, sau do tao canonical patch va mot immutable dataset
version moi; chi readiness PASS moi duoc promote. Xem `airflow-google-maps.md`,
`pipeline-crawl.md` va `neo4j-v8-publishing.md` cho workflow hien hanh.

```text
audit evidence -> explicit merge decisions -> canonical manifest
                                               |
                                               v
                         bounded discovery/detail/distinct proposal
                                               |
                                      inspect PASS/REVIEW/REJECT
                                               |
                              selected approval -> immutable overlay
                                               |
                              rebuild manifest with previous manifest
```

Chi chay cac lenh ben duoi khi co historical import da duoc phe duyet. Dat cac
duong dan import ngoai repository; khong copy chung vao `data/canonical`:

```powershell
$legacyImport = "<legacy-import-dir>"
$legacyGoogleMappingImport = "<legacy-google-mapping-import-dir>"
$legacyGoogleRegistryReport = "<legacy-google-registry-report>"
```

Chay tu thu muc goc `NexTripAI-KB`. Cac tham so ben duoi da duoc doi chieu voi
`python -m nextrip_pipeline.cli <command> --help`.

## 1. Audit va ghi quyet dinh merge

Current `build-google-maps-registry` la canonical-only, vi vay khong dung lenh
do de bootstrap historical migration. Import mot frozen registry report da co
provenance cung historical corpus va kiem tra no ton tai:

```powershell
if (-not (Test-Path -LiteralPath $legacyGoogleRegistryReport)) {
  throw "Missing approved legacy Google registry report"
}
```

Audit master va Google Maps observations ma khong thay doi du lieu:

```powershell
python -m nextrip_pipeline.cli audit-canonical-identities `
  --candidate-groups $legacyGoogleRegistryReport `
  --master-dir $legacyImport `
  --observation-root data/normalized/entity=opening_status `
  --output data/reports/canonical/duplicate-evidence.json
```

Doc `groups`, `status`, `reason_codes`, Google identity, dia chi va toa do trong
report. Audit khong tu ghi quyet dinh. Chi dua nhom `confirmed`, hoac nhom da
duoc nguoi review xac minh doc lap, vao
`config/canonical-identity-decisions.json`:

```json
{
  "schema_version": "1.0.0",
  "decisions": [
    {
      "keeper_legacy_place_id": "cafe_dn_001",
      "duplicate_legacy_place_ids": ["night_dn_010"],
      "reason": "verified_same_google_place"
    }
  ],
  "distinct_decisions": [
    {
      "place_ids": ["cafe_dn_002", "night_dn_011"],
      "reason": "human_review_verified_different_google_places"
    }
  ],
  "replacements": []
}
```

`review` khong co nghia la duplicate va khong duoc merge tu dong. `distinct`
phai giu thanh hai identity. Mot nhom `review` chi duoc readiness gate cong nhan
la distinct neu co explicit `distinct_decisions` trung khop toan bo `place_ids`.
Quyet dinh va reason duoc ghi trong readiness artifact v1.1 va nam trong
content hash. Chon keeper co identity/provenance on dinh nhat;
ID con lai se thanh alias da retired va mo mot vacancy cung
`entity_type + city_id`.

## 2. Build canonical manifest

Build lan dau hoac build lai sau khi sua duplicate decisions:

```powershell
python -m nextrip_pipeline.cli build-canonical-manifest `
  --master-dir $legacyImport `
  --decisions config/canonical-identity-decisions.json `
  --approved-replacement-dir data/canonical/replacements `
  --output config/generated/canonical-identity-manifest.json `
  --report data/reports/canonical/manifest-build.json
```

Kiem tra output CLI `canonical=... retired=... vacant=...` va build report truoc
khi discovery. Khong dung `--ignore-master-duplicate-tags` trong van hanh binh
thuong; option nay chi danh cho audit/migration co chu dich.

## 3. Propose replacement theo bounded batch

Canh bao compatibility: `--current-mapping-dir` trong buoc nay chi doc
historical mapping projection tu thu muc import ben ngoai da khai bao o dau tai
lieu. Projection nay khong duoc publish, khong duoc dung lam fallback va khong
nam trong normal production workflow.

Nen chay theo tung entity type va city. Vi du tim toi da 5 cafe moi tai Da Nang:

```powershell
python -m nextrip_pipeline.cli propose-canonical-replacements `
  --manifest config/generated/canonical-identity-manifest.json `
  --master-dir $legacyImport `
  --current-mapping-dir $legacyGoogleMappingImport `
  --approved-replacement-dir data/canonical/replacements `
  --entity-type cafe `
  --city-id city_da_nang `
  --result-limit 20 `
  --max-detail-candidates 5 `
  --max-vacancies 5
```

Co the lap `--vacancy-id` de chay dung cac vacancy da chon. Chuong trinh in
`summary=<path>`; raw, discovery stage, detail va summary deu la immutable
evidence. Moi `entity_type + city_id` chi search mot lan trong batch; candidates
duoc xet theo result position va dia diem da chon khong the duoc chon lai cho
vacancy sau.

## 4. Inspect summary

Lay summary moi nhat va liet ke proposal:

```powershell
$batch = Get-ChildItem data/runs/canonical_replacements/run=*.json |
  Sort-Object LastWriteTimeUtc -Descending |
  Select-Object -First 1 -ExpandProperty FullName
$summary = Get-Content -LiteralPath $batch -Raw | ConvertFrom-Json
$summary | Select-Object batch_id, vacant_count, proposed_count, unresolved_count, failed_count
$summary.results |
  Where-Object { $null -ne $_.proposal } |
  Select-Object @{n='vacancy_id';e={$_.vacancy.vacancy_id}},
                @{n='proposal_id';e={$_.proposal.proposal_id}},
                @{n='place_id';e={$_.proposal.proposed_place_id}},
                @{n='name';e={$_.proposal.candidate.name}},
                @{n='google_url';e={$_.proposal.candidate.external_identities[0].external_url}}
```

Truoc khi apply, doi chieu it nhat: Google name, city, entity type, toa do,
stable Google token/URL, `detail_path` va `discovery_stage_path`. Kiem tra
`results[].failures`: candidate `review` hoac `reject` khong phai proposal va
khong duoc apply. Loi cua mot vacancy khong lam mat ket qua cua vacancy khac.

## 5. Apply proposal da chon

Apply deterministic mot hoac nhieu `proposal_id`; lap option de chon nhieu
proposal:

```powershell
python -m nextrip_pipeline.cli apply-canonical-replacements `
  --batch $batch `
  --proposal-id "<proposal-id-1>" `
  --proposal-id "<proposal-id-2>" `
  --method deterministic `
  --reviewer canonical-deterministic-gate `
  --output-dir data/canonical/replacements
```

`deterministic` khong bo qua gate: CLI doc lai detail, doi chieu hash va chi
nhan candidate `PASS` co Google token/toa do hop le. Dung
`--method human --reviewer "<reviewer>"` neu quyet dinh do nguoi duyet. Neu bo
tat ca `--proposal-id`, CLI se apply moi PASS proposal trong batch; chi lam vay
khi policy tu dong da duoc phe duyet.

Apply tao record trong `data/canonical/replacements/records/...`; day la
overlay, khong ghi de historical source trong `$legacyImport`.

## 6. Rebuild voi previous manifest

Sao luu manifest hien tai, sau do build lai voi ca previous manifest va overlay:

```powershell
$stamp = Get-Date -Format 'yyyyMMddTHHmmss'
New-Item -ItemType Directory -Force data/canonical/manifests | Out-Null
$previous = "data/canonical/manifests/canonical-identity-$stamp.json"
Copy-Item config/generated/canonical-identity-manifest.json $previous

python -m nextrip_pipeline.cli build-canonical-manifest `
  --master-dir $legacyImport `
  --decisions config/canonical-identity-decisions.json `
  --previous-manifest $previous `
  --approved-replacement-dir data/canonical/replacements `
  --output config/generated/canonical-identity-manifest.json `
  --report data/reports/canonical/manifest-build.json
```

Build phai cho thay vacancy da duyet chuyen sang `filled`, identity moi dung ID
monotonic va quota khong doi.

## 7. Materialize dataset va kiem tra publish-readiness

Tao mot active dataset duy nhat, dong thoi doi chieu tat ca duplicate evidence:

```powershell
python -m nextrip_pipeline.cli materialize-canonical-dataset `
  --master-dir $legacyImport `
  --manifest config/generated/canonical-identity-manifest.json `
  --decisions config/canonical-identity-decisions.json `
  --approved-replacement-dir data/canonical/replacements `
  --duplicate-evidence data/reports/canonical/duplicate-evidence.json `
  --output-dir data/canonical/datasets `
  --readiness-output-dir data/canonical/readiness
```

Lenh van ghi dataset va readiness report de audit, nhung tra exit code `1` neu
con group chua xu ly. Neo4j V8 chi duoc doc dataset khi output co
`publish_ready=true`. Option `--allow-unresolved-review` chi dung de tao shadow
dataset/parity report; khong dung option nay trong task publish V8.

Sau khi gate pass, pin chinh xac artifact moi cho moi production consumer:

```text
NEXTRIP_CANONICAL_DATASET=data/canonical/datasets/dataset=<dataset-id>/canonical-active-dataset.json
```

Khong truyen bat ky historical import/projection nao cho Neo4j, traffic hoac
Current Data. Mot canonical version cu van la immutable audit artifact;
promotion chi doi path/version duoc chon, khong ghi de artifact cu.

Gate kiem tra ba truong hop: `confirmed` phai resolve ve cung mot canonical ID,
`distinct` phai resolve ve cac canonical ID khac nhau, va `review` chi duoc coi
la da xu ly khi explicit decision da merge no hoac explicit distinct decision
giu moi ID thanh mot canonical rieng. Vi vay du quota khong dong nghia voi du
dieu kien publish.

## Invariants bat buoc

- Moi physical place chi co mot `canonical_place_id`; moi legacy ID chi co mot
  owner.
- Retired ID la tombstone vinh vien: khong kich hoat lai va khong cap cho dia
  diem moi.
- Moi retired ID co dung mot vacancy; replacement phai giu
  `entity_type + city_id` va dung ID moi cao hon high-water mark.
- `target_count = active_count + open_vacancy_count`; apply/rebuild chi chuyen
  mot slot tu vacant sang filled, khong doi target quota.
- Google external token va allocated place ID phai unique toan cuc, ke ca cac
  overlay da apply.
- `REVIEW` khong merge, khong fill vacancy va khong publish. Chi explicit
  duplicate decision hoac replacement `PASS` da approve moi doi manifest.
- Manifest, proposal, detail va approval phai qua schema/hash validation; khong
  sua JSON immutable artifact bang tay.

## Gioi han Google Maps/Playwright

Google Maps web crawling khong phai API va khong mac nhien la nguon "free" duoc
tu do su dung. Truoc khi van hanh, can danh gia theo
[Google Maps/Google Earth Additional Terms](https://www.google.com/help/terms_maps/)
va [Google Maps Platform Terms](https://cloud.google.com/maps-platform/terms/),
cung policy phap ly/retention cua du an.

Playwright implementation hien tai chi doc public page, chay bounded va khong
login, khong stealth, khong bypass CAPTCHA/automated-traffic block. Khi bi chan,
batch phai fail/giam tai va chay lai sau; khong them co che ne chan. Giu nho cac
gioi han `result-limit`, `max-detail-candidates` va `max-vacancies`; `--headed`
chi dung de debug co giam sat. DOM/URL Google co the thay doi, vi vay phai giu
raw evidence, parser version va review queue; khong coi Playwright la production
SLA.
