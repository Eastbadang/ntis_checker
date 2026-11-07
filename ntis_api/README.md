# NTIS 국가R&D OpenAPI 작업 폴더 (ntis_api)

## 1. 구성 파일

| 파일 | 역할 |
| ----- | ----- |
| `.env.example` | `.env` 작성 예시 (복사 후 본인의 인증키 입력) |
| `.env` (직접 생성) | 인증키 `NTIS_APPRV_KEY=...` 보관 (공유·업로드 금지) |
| `ntis_client.py` | 연관 컨텐츠 조회 (과제번호별 유사 결과 수집) |
| `ntis_search_client.py` | 국가R&D 검색 API (과제검색, 논문, 특허, 시설장비, 보고서) |
| `ntis_enrich.py` | 연관 컨텐츠 결과 보강 스크립트 |
| `ntis_gui.py` | NTIS 검색 및 조회를 위한 GUI 도구 |
| `make_queries.py` | 검색 질의 생성 보조 도구 |
| `input_pjt_ids.csv` (사용자 준비) | 과제번호 목록 (선택 사항) |

## 2. 준비

1. `.env.example`을 복사해 `.env`를 만들고 키를 입력합니다.
   ```
   NTIS_APPRV_KEY=발급받은_키
   ```
2. 키는 하나로 모든 승인 서비스에 쓰입니다. 연구보고서 검색만 파라미터 이름이 `userKey`이며, 코드가 자동으로 처리합니다.
3. Python 3.8 이상. 추가 패키지는 필요 없습니다.
4. 호출은 승인된 IP에서만 됩니다.

## 3. 실행

**연관 컨텐츠 조회 (과제번호별 유사 결과 4종)**
```
python ntis_client.py                                  # input_pjt_ids.csv 사용
python ntis_client.py --pjt-id 1234567890 --collections project paper
python ntis_enrich.py                                  # KISTI 상세정보 보강 -> ntis_results_enriched.csv
```
`topN` 기본값은 100입니다. 원본 응답은 `ntis_results_raw.jsonl`에 남습니다.

**국가R&D 검색 (과제·성과·보고서)**

NTIS 매뉴얼은 같은 서비스인데도 요청명세 표와 예시 URL의 파라미터가 다릅니다. 그래서 요청 방식을 네 가지 프로필로 나눠 두었고, 먼저 진단으로 동작하는 방식을 찾은 뒤 사용합니다.

| 프로필 | 기준 | 특징 |
|---|---|---|
| `sample` (기본) | 매뉴얼 요청 예시 URL | `query`, `displayCount`, `searchField`, `naviCount`, `cmbnApiYn=Y` |
| `sample_nocmbn` | 위와 같음 | `cmbnApiYn` 제외 |
| `spec` | 요청명세 표 / NTIS TEST URL 화면 | 과제검색 `SRWR`, `searchFd`, `displayCnt` (연구보고서 키는 서버 확인 결과에 따라 `apprvKey`) |
| `spec_cmbn` | 위와 같음 | `cmbnApiYn=Y` 추가 |

```
python ntis_search_client.py --mode all --probe                 # 1) 서비스별·프로필별 동작 진단
python ntis_search_client.py --mode project --text 나노          # 2) 검색어 1건
python ntis_search_client.py --mode project --pjt-id 1234567890  #    과제번호 1건
python ntis_search_client.py --mode paper --queries 검색어.csv --count 50 --max-pages 2
```
- 진단 결과는 화면과 `search_out\진단결과.csv`에 남습니다. 인증키는 `***`로 가려집니다.
- 동작하는 프로필을 찾으면 `.env`에 `NTIS_SEARCH_PROFILE=프로필명`을 추가하거나 `--profile`로 지정합니다.
- `--mode`: `project`(과제검색), `paper`(논문), `patent`(특허), `equip`(연구시설장비), `report`(연구보고서), `all`(진단 전용)
- 검색어 CSV는 UTF-8로 저장하고 `검색어` 열(필요 시 `기준ID` 열)을 씁니다.
- 결과는 `search_out\ntis_<mode>_결과.csv`, 원본 응답은 `search_out\raw`에 저장됩니다.
- 페이지당 건수 `--count` 기본값은 10입니다(진단은 1).
- 연구책임자·참여연구원·저자·발명자 성명은 개인정보이므로 결과 CSV에서 기본 제외합니다. 꼭 필요할 때만 `--keep-personal`을 붙이세요. `search_out\raw`의 원본 응답에는 성명이 남아 있으니 공유하지 마세요.
- 연구보고서 응답은 XML 형식 오류(URL 속 `&`)와 실제 `<span>` 강조 태그가 있어, 코드가 자동으로 보정합니다.

## 4. 테스트

```
python -m unittest discover -s tests -v
```
응답 파싱, 서비스별 파라미터 이름, 보강 조인, 주제 검색, 비교 집계를 확인합니다. 실제 NTIS 호출은 하지 않습니다.

## 5. 제약 사항

- NTIS OpenAPI의 텍스트 응답(요약, 목표, 내용, 기대효과)은 200자까지만 제공됩니다.
- 2025년 이후 등록 과제는 KISTI 조회표에 없으므로 "미수록"으로 표시됩니다.
- 연관콘텐츠 추천 서비스의 매뉴얼은 아직 확보되지 않아 코드에 포함되지 않았습니다.

## 6. 보안 원칙

- 키는 코드에 쓰지 않고 `.env`에서만 읽습니다. 오류 메시지에도 키와 URL을 넣지 않습니다.
- 채팅이나 메신저로 키를 보내지 마세요. 키가 노출되면 NTIS에서 재발급을 신청하세요.
- 결과 파일에 개인정보가 없는지 확인하고, 외부 공유 전에는 기관 보안 규정을 따르세요.

## 7. 자주 나는 문제

| 증상 | 조치 |
|---|---|
| `NTIS_APPRV_KEY가 없습니다` | `.env` 위치(이 폴더)와 키 이름 확인 |
| `접근 허용 IP가 아닙니다` | NTIS에서 허용 IP 등록 상태 확인 |
| `HTTP 404` (검색 API) | `--mode all --probe`로 프로필별 동작을 확인. 모두 실패하면 `진단결과.csv`를 NTIS에 전달 |
| `HTTP 404` (연관 조회) | `NTIS_BASE_URL` 설정을 확인 (매뉴얼 예시는 `.do` 형식) |
| `오류응답` | NTIS가 보낸 `<error>` 메시지가 그대로 출력됩니다 (예: 접근 허용 IP 아님) |
| `XML아님` | 응답 앞부분이 출력됩니다. 서비스 승인 상태나 파라미터를 확인 |
| `결과없음` | 과제번호 조회는 `addQuery=CN=` 방식입니다. 결과가 없으면 `--text`로 검색어 방식을 사용 |
