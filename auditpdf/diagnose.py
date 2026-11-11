"""`python -m auditpdf diagnose` - 리포트 PDF 마다 왜 추출되지 않았는지 분류한다.

1. 파일명 키워드 : 폴더 안 PDF 중 몇 개가 리포트 종류 단어(scan.KINDS)에 걸리는가
2. 감사 추적      : 표 인식 → 로그 표 판정(3열 + TIME/LOG/USER · 시간/로그/사용자) → 행 복원
3. 그 외 리포트   : 본문 항목(생산 결과, 계수 행, NG 이미지)이 몇 개 나오는가
"""

import csv
from collections import Counter
from pathlib import Path

import pdfplumber

from . import pdf as P
from .scan import FILENAME_KEYWORD, KIND_NAMES, find_pdfs, kind_of


def scan_other(path, kind):
    result = P.read_pdf(path, kind)
    return {"파일명": path.name, "종류": KIND_NAMES[kind], "폴더": str(path.parent), "페이지수": result.pages,
            "표총수": "", "로그표수": "", "메타표유무": "Y" if result.meta.get("REPORT TYPE") else "N",
            "추출행수": result.items, "발견된_표머리": "",
            "사유": "정상" if result.items else "본문 항목이 없음 (빈 리포트이거나 양식이 다름)"}


def scan_file(path, deep_pages=3):
    kind = kind_of(path.name)
    if kind != "audit":
        return scan_other(path, kind)
    info = {"파일명": path.name, "종류": KIND_NAMES[kind], "폴더": str(path.parent), "페이지수": 0, "표총수": 0, "로그표수": 0,
            "메타표유무": "N", "추출행수": 0, "사유": "", "발견된_표머리": ""}
    signatures = []
    with pdfplumber.open(path) as doc:
        info["페이지수"] = len(doc.pages)
        for pno, page in enumerate(doc.pages, start=1):
            meta, logs, others = P.classify_tables(page)
            info["표총수"] += (meta is not None) + len(logs) + len(others)
            info["로그표수"] += len(logs)
            if meta is not None:
                info["메타표유무"] = "Y"
            if pno <= deep_pages:
                for t in others:  # 로그 표로 인정받지 못한 표의 첫 행
                    head = P.first_row(t)
                    sig = f"{len(head)}열 {head[:4]}"
                    if sig not in signatures:
                        signatures.append(sig)
            info["추출행수"] += sum(len(P.log_rows(page, t)) for t in logs)
    info["발견된_표머리"] = " | ".join(signatures[:4])
    if info["표총수"] == 0:
        info["사유"] = "표 자체가 인식되지 않음 (괘선 없음/스캔본/이미지 PDF 가능성)"
    elif info["로그표수"] == 0:
        info["사유"] = "표는 있으나 3열 로그 헤더(TIME/LOG/USER · 시간/로그/사용자)로 판정되지 않음"
    elif info["추출행수"] == 0:
        info["사유"] = "로그 표는 찾았으나 행 복원 결과가 0 (좌표/열 경계 문제)"
    else:
        info["사유"] = "정상"
    return info


def run(root, csv_path=None, detail=3, sample_rows=0):
    root = Path(root).resolve()
    all_pdfs = sorted(p for p in root.rglob("*.pdf") if p.is_file())
    targets = find_pdfs(root)
    skipped = [p for p in all_pdfs if p not in set(targets)]

    print("=" * 78)
    print(f"루트                    : {root}")
    print(f"폴더 안 PDF 전체        : {len(all_pdfs)}")
    print(f"키워드 '{FILENAME_KEYWORD}' 통과 : {len(targets)}")
    print(f"키워드 불일치로 제외    : {len(skipped)}")
    print("=" * 78)
    for p in skipped[:10]:
        print(f"    제외 - {p.name}")
    if len(skipped) > 10:
        print(f"    ... 외 {len(skipped) - 10}건")
    if not targets:
        print("\n[결론] 파일명 키워드가 하나도 맞지 않습니다. auditpdf/scan.py 의 KINDS 를 확인하세요.")
        return 1

    rows = []
    for seq, path in enumerate(targets, start=1):
        try:
            info = scan_file(path)
        except Exception as e:
            info = {"파일명": path.name, "종류": KIND_NAMES[kind_of(path.name)], "폴더": str(path.parent), "페이지수": 0, "표총수": 0, "로그표수": 0,
                    "메타표유무": "N", "추출행수": 0, "사유": f"예외 {type(e).__name__}: {e}", "발견된_표머리": ""}
        rows.append(info)
        mark = "OK  " if info["사유"] == "정상" else "FAIL"
        print(f"[{seq}/{len(targets)}] {mark} {info['종류']:<7} p{info['페이지수']:>3} 표{info['표총수']:>3} "
              f"로그표{info['로그표수']:>3} 항목{info['추출행수']:>5}  {info['파일명'][:52]}", flush=True)

    print("\n" + "=" * 78 + "\n사유별 집계\n" + "=" * 78)
    for reason, n in Counter(r["사유"] for r in rows).most_common():
        print(f"  {n:>4}건  {reason}")

    failed = [r for r in rows if r["사유"] != "정상"]
    for r in failed[:detail]:
        print(f"\n  파일  : {r['파일명']}\n  사유  : {r['사유']}\n  표머리: {r['발견된_표머리'] or '(없음)'}")

    for path in [p for p in targets if kind_of(p.name) == "audit"][:1] if sample_rows else []:
        result = P.read_pdf(path)
        print(f"\n[샘플] {path.name}")
        for k in P.META_KEYS:
            if k in result.meta:
                print(f"  {k:<20}: {result.meta[k]}")
        for r in result.rows[:sample_rows]:
            print(f"  {'*' if r.alarm else ' '} {r.time} | {r.log[:60]} | {r.user}")

    if csv_path:
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\n저장: {Path(csv_path).resolve()}")
    return 2 if failed else 0
