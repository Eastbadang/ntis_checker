#!/usr/bin/env python3
"""NTIS 통합 OpenAPI - 연관 컨텐츠 조회 서비스(전체용) 클라이언트.

- 인증키(apprvKey)는 코드에 넣지 않고 환경변수 NTIS_APPRV_KEY 또는
  이 파일과 같은 폴더의 .env 파일에서 읽습니다.
- 외부 패키지 없이 표준 라이브러리만 사용합니다.

사용 예:
    python ntis_client.py --input input_pjt_ids.csv --output ntis_results.csv
    python ntis_client.py --pjt-id 1425118980 --output one.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 메뉴얼 기준 서비스 URL. (메뉴얼 예시는 ConnectionContent.do 형식이므로, 404가 나면 NTIS_BASE_URL로 변경)
BASE_URL = os.environ.get(
    "NTIS_BASE_URL", "https://www.ntis.go.kr/rndopen/openApi/ConnectionContent"
)
COLLECTIONS = ("project", "paper", "patent", "researchreport")
NAME_FIELD = {  # 컬렉션별 결과명 필드 (메뉴얼 2.3절)
    "project": "KOR_PJT_NM",
    "paper": "PAPER_NM",
    "patent": "IPR_INVENTION_NM",
    "researchreport": "KOR_RPT_TITLE_NM",
}
ID_FIELD = {"project": "PJT_ID"}  # 그 외 컬렉션은 RST_ID
REQUEST_INTERVAL = 0.1  # 초당 최대 30tps 한도 내 유지
TIMEOUT = 15
TOP_N = 100  # 메뉴얼 허용 범위 1~100 (이전 기본값 10)
HERE = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """.env의 KEY=VALUE 쌍을 환경변수로 읽음 (이미 설정된 값은 덮어쓰지 않음)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():  # 메모장 BOM 대응
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_api_key() -> str:
    preset = "NTIS_APPRV_KEY" in os.environ  # .env 보다 먼저 설정된 시스템/PyCharm 환경변수
    _load_dotenv(HERE / ".env")
    source = "시스템 또는 PyCharm 환경변수 (.env 보다 우선)" if preset else f".env 파일 ({HERE / '.env'})"
    key = os.environ.get("NTIS_APPRV_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "NTIS_APPRV_KEY가 없습니다. .env 파일(또는 환경변수)에 설정하세요."
        )
    if any(not ch.isascii() or ch.isspace() or ch in "*<>\"'" for ch in key):
        # "삭제", "***", 자리표시자 문구처럼 키가 아닌 값 (값 자체는 출력하지 않음)
        raise RuntimeError(f"NTIS_APPRV_KEY 값이 인증키 형식이 아닙니다 (길이 {len(key)}자). 읽은 위치: {source}")
    return key


def fetch(pjt_id: str, collection: str, api_key: str, top_n: int = TOP_N) -> dict:
    """연관 컨텐츠 1건(과제ID × 컬렉션) 조회. 예외 메시지에는 인증키/URL을 넣지 않음."""
    params = {
        "apprvKey": api_key,
        "pjtId": pjt_id,
        "collection": collection,
        "topN": top_n,
    }
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            text = res.read().decode("utf-8")
            # NTIS 오류 메시지는 인증키를 그대로 돌려주므로, 출력·저장 전에 가림
            if api_key:
                text = text.replace(api_key, "***")
            return _decode(json.loads(text))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"HTTP {e.code} (pjtId={pjt_id}, collection={collection})"
        ) from None
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"네트워크 오류 (pjtId={pjt_id}, collection={collection}): {e.reason}"
        ) from None


def _decode(obj):
    """JSON이 문자열로 한 번(또는 두 번) 더 감싸져 온 경우 풀어줌."""
    for _ in range(2):
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except json.JSONDecodeError:
                break
    return obj


def flatten(pjt_id: str, collection: str, payload) -> list[dict]:
    """응답 JSON의 items를 CSV용 행으로 변환. 형식이 다르면 경고 후 빈 목록."""
    payload = _decode(payload)
    if not isinstance(payload, dict):
        snippet = str(payload)[:300].replace("\n", " ")
        print(f"[경고] 예상과 다른 응답 형식 (pjtId={pjt_id}, collection={collection}): {type(payload).__name__}")
        print(f"       응답 내용(앞 300자): {snippet}")
        return []
    items = _decode(payload.get("items")) or []
    if not isinstance(items, list):
        print(f"[경고] items 형식 불일치 (pjtId={pjt_id}, collection={collection}): {type(items).__name__}")
        return []
    rows = []
    for item in items:
        item = _decode(item)
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "기준과제ID": pjt_id,
                "기준과제명": payload.get("KOR_PJT_NM", ""),
                "컬렉션": collection,
                "순위": item.get("rank", item.get("RANK", "")),
                "결과ID": item.get(ID_FIELD.get(collection, "RST_ID"), ""),
                "결과명": item.get(NAME_FIELD[collection], ""),
                "유사도": item.get("similarity_score", ""),
                "등록일": item.get("creat_dt", ""),
            }
        )
    return rows


def read_pjt_ids(path: Path) -> list[str]:
    """입력 CSV의 'pjtId' 열(없으면 첫 열)에서 과제 고유번호 목록을 읽음."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        col = "pjtId" if reader.fieldnames and "pjtId" in reader.fieldnames else None
        ids = []
        for row in reader:
            value = (row[col] if col else next(iter(row.values()), "")).strip()
            if value:
                ids.append(value)
    return list(dict.fromkeys(ids))  # 중복 제거, 순서 유지


def run(pjt_ids: list[str], output: Path, collections=COLLECTIONS, top_n=TOP_N) -> int:
    api_key = get_api_key()
    fieldnames = ["기준과제ID", "기준과제명", "컬렉션", "순위", "결과ID", "결과명", "유사도", "등록일", "조회일시"]
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    raw_path = output.with_name(output.stem + "_raw.jsonl")  # 원본 응답 보관 (검증·재처리용)
    total = 0
    with output.open("w", encoding="utf-8-sig", newline="") as f, \
            raw_path.open("w", encoding="utf-8", newline="") as raw:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for pjt_id in pjt_ids:
            for collection in collections:
                try:
                    payload = fetch(pjt_id, collection, api_key, top_n)
                except RuntimeError as e:
                    print(f"[경고] {e}")
                    continue
                raw.write(json.dumps({"pjtId": pjt_id, "collection": collection, "조회일시": stamp,
                                      "response": payload}, ensure_ascii=False) + "\n")
                rows = flatten(pjt_id, collection, payload)
                for r in rows:
                    r["조회일시"] = stamp
                writer.writerows(rows)
                total += len(rows)
                time.sleep(REQUEST_INTERVAL)
            print(f"[완료] pjtId={pjt_id}")
    print(f"원본 응답 저장: {raw_path}")
    print(f"총 {total}건 저장: {output}")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="NTIS 연관 컨텐츠 조회")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--input", type=Path, help="pjtId 열이 있는 CSV 경로 (기본: input_pjt_ids.csv)")
    src.add_argument("--pjt-id", help="과제 고유번호 1건")
    parser.add_argument("--output", type=Path, default=HERE / "ntis_results.csv")
    parser.add_argument(
        "--collections",
        nargs="+",
        default=list(COLLECTIONS),
        choices=COLLECTIONS,
        help="조회할 컬렉션 (기본: 전체)",
    )
    parser.add_argument("--top-n", type=int, default=TOP_N, help="컬렉션별 추출 개수 1~100")
    args = parser.parse_args()

    if args.pjt_id:
        pjt_ids = [args.pjt_id]
    else:
        input_path = args.input or (HERE / "input_pjt_ids.csv")
        if not input_path.exists():
            parser.print_help()
            raise SystemExit(
                f"\n입력 파일이 없습니다: {input_path}\n"
                "--input 또는 --pjt-id 를 지정하거나 input_pjt_ids.csv 를 만드세요."
            )
        pjt_ids = read_pjt_ids(input_path)
    if not pjt_ids:
        raise SystemExit("조회할 과제 고유번호가 없습니다.")
    run(pjt_ids, args.output, tuple(args.collections), args.top_n)


if __name__ == "__main__":
    main()
