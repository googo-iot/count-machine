"""Excel / CSV 내보내기.

시트: AuditTrail_All (또는 폴더별), BottleCount_All, NGImage_All, _배치정보, _배치생산결과, _파일목록, _추출실패
"""

import csv
import re

from .pdf import META_KEYS, SUMMARY_KEYS
from .scan import KIND_NAMES

EXCEL_MAX_ROWS = 1_048_575  # 머리글 1행을 뺀 시트 최대 행 수

BATCH_COLS = ["소스폴더", "파일명", "MACHINE ID", "BATCH NUMBER", "RECIPE NAME"]
LOG_COLUMNS = ["순번"] + BATCH_COLS + ["TIME", "LOG", "USER", "페이지", "페이지내행번호", "강조표시", "글자색상"]
BOTTLE_COLUMNS = ["순번"] + BATCH_COLS + ["TIME", "PASSED", "REJECTED", "PROD.COUNT", "RESULT", "페이지"]
NG_COLUMNS = ["순번"] + BATCH_COLS + ["TIME", "CHANNEL", "NG TYPE", "페이지"]
TRIAL_COLUMNS = ["소스폴더", "BATCH NUMBER", "#", "Start Time", "Stop Time", "Passed Bottle", "Rejected Bottle", "NG Rej. Prod."]
FILE_COLUMNS = ["순번", "소스폴더", "파일명", "종류", "전체경로", "상태", "페이지수", "추출행수", "강조(알람)행",
                "TIME형식이상", "USER공란", "파일크기(byte)", "원본수정일시", "SHA-256", "중복원본순번"]
WIDTHS = {"순번": 8, "소스폴더": 42, "파일명": 52, "전체경로": 60, "MACHINE ID": 12, "BATCH NUMBER": 16,
          "RECIPE NAME": 26, "TIME": 20, "LOG": 70, "USER": 12, "페이지": 8, "페이지내행번호": 14,
          "강조표시": 10, "글자색상": 12, "상태": 14, "SHA-256": 66, "원본수정일시": 20, "오류": 60, "파일경로": 60,
          "RESULT": 26, "NG TYPE": 30, "종류": 14, "Start Time": 20, "Stop Time": 20, "BATCH START TIME": 20,
          "BATCH STOP TIME": 20, "PRINTED DATE": 20}


def _num(v):
    """숫자로만 된 글자는 숫자로 (Excel 에서 합계/정렬이 되도록)."""
    return int(v) if isinstance(v, str) and v.isdigit() else v


def batch_cols(info):
    return [info.folder, info.name, info.meta["MACHINE ID"], info.meta["BATCH NUMBER"], info.meta["RECIPE NAME"]]


def log_row(seq, info, row):
    return [seq] + batch_cols(info) + [row.time, row.log, row.user, row.page, row.line,
                                       "Y" if row.alarm else "", row.color]


def file_row(f):
    return [f.seq, f.folder, f.name, KIND_NAMES.get(f.kind, f.kind), f.path, f.status, f.pages, f.rows, f.alarms,
            f.bad_time, f.no_user, f.size, f.mtime, f.sha256, f.dup_of or ""]


def batches(scan):
    """폴더(배치)별 [폴더, 메타데이터, 요약, 생산 결과]. 메타데이터는 배치 리포트 것을 먼저 쓴다."""
    groups = {}
    for f in scan.files:
        if f.data is None or f.dup_of:
            continue
        g = groups.setdefault(f.folder, {"meta": None, "summary": {}, "trials": []})
        if f.kind == "batch":
            g["meta"], g["summary"], g["trials"] = f.meta, f.data.summary, f.data.trials
        elif g["meta"] is None:
            g["meta"] = f.meta
    return [(folder, g["meta"], g["summary"], g["trials"]) for folder, g in groups.items()]


def sheet_name(name, used):
    """Excel 시트명 제약(31자, 특수문자, 중복)에 맞춘다."""
    name = (re.sub(r"[:\\/?*\[\]]", "_", str(name)).strip() or "Sheet")[:31]
    candidate, n = name, 2
    while candidate.lower() in {u.lower() for u in used}:
        suffix = f"_{n}"
        candidate, n = name[:31 - len(suffix)] + suffix, n + 1
    used.add(candidate)
    return candidate


def write_excel(scan, out, per_folder=False):
    """out 은 파일 경로 또는 쓰기 가능한 바이너리 스트림."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    head_font = Font(name="Arial", bold=True, size=10)
    head_fill = PatternFill("solid", fgColor="DDE5F0")
    head_align = Alignment(horizontal="center", vertical="center")
    alarm_fill = PatternFill("solid", fgColor="FCE8CF")

    # 병 계수 기록·NG 이미지가 수십만 행이라 행을 바로 파일로 흘려 쓰는 write-only 모드로 만든다
    wb = Workbook(write_only=True)
    used = set()

    def formula_like(v):
        # 감사 추적 로그는 '=' 로 시작하는 줄이 많다. 수식으로 해석되면 #NAME? 이 되므로 문자열로 둔다.
        return isinstance(v, str) and v[:1] in ("=", "+", "-", "@") and v != "-"

    def add(title, columns, rows, mark=None):
        """mark(row) 가 참인 행은 주황 배경."""
        chunks = [rows[i:i + EXCEL_MAX_ROWS] for i in range(0, len(rows), EXCEL_MAX_ROWS)] or [[]]
        for n, chunk in enumerate(chunks):
            ws = wb.create_sheet(sheet_name(title if n == 0 else f"{title}_{n + 1}", used))
            for i, col in enumerate(columns, 1):
                ws.column_dimensions[get_column_letter(i)].width = WIDTHS.get(col, max(10, len(col) + 4))
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(chunk) + 1}"
            head = []
            for col in columns:
                c = WriteOnlyCell(ws, col)
                c.font, c.fill, c.alignment = head_font, head_fill, head_align
                head.append(c)
            ws.append(head)
            for values in chunk:
                marked = mark is not None and mark(values)
                if not marked and not any(formula_like(v) for v in values):
                    ws.append(values)
                    continue
                cells = []
                for v in values:
                    c = WriteOnlyCell(ws, v)
                    if formula_like(v):
                        c.data_type, c.quotePrefix = "s", True
                    if marked:
                        c.fill = alarm_fill
                    cells.append(c)
                ws.append(cells)

    alarm_col = LOG_COLUMNS.index("강조표시")
    rows = [log_row(i, info, row) for i, (info, row) in enumerate(scan.records, 1)]
    if per_folder:
        groups = {}
        for r in rows:
            groups.setdefault(r[1], []).append(r)
        for folder, group in sorted(groups.items()):
            add(folder, LOG_COLUMNS, group, lambda v: v[alarm_col] == "Y")
    else:
        add("AuditTrail_All", LOG_COLUMNS, rows, lambda v: v[alarm_col] == "Y")

    result_col = BOTTLE_COLUMNS.index("RESULT")
    add("BottleCount_All", BOTTLE_COLUMNS,
        [[i] + batch_cols(f) + [c[0], _num(c[1]), _num(c[2]), _num(c[3]), c[4], c[5]]
         for i, (f, c) in enumerate(((f, c) for f in scan.of("bottle") for c in f.data.counts), 1)],
        lambda v: v[result_col] not in ("", "-"))
    add("NGImage_All", NG_COLUMNS,
        [[i] + batch_cols(f) + [g[0], _num(g[1]), g[2], g[3]]
         for i, (f, g) in enumerate(((f, g) for f in scan.of("ng") for g in f.data.ngs), 1)])

    groups = batches(scan)
    add("_배치정보", ["소스폴더"] + META_KEYS + SUMMARY_KEYS,
        [[folder] + [meta.get(k, "") for k in META_KEYS] + [_num(summary.get(k, "")) for k in SUMMARY_KEYS]
         for folder, meta, summary, _ in groups])
    add("_배치생산결과", TRIAL_COLUMNS,
        [[folder, meta.get("BATCH NUMBER", "")] + [_num(v) for v in t] for folder, meta, _, trials in groups for t in trials])
    add("_파일목록", FILE_COLUMNS, [file_row(f) for f in scan.files])
    failed = [f for f in scan.files if f.failed]
    if failed:
        add("_추출실패", ["파일경로", "오류"], [[f.path, f.status] for f in failed])
    wb.save(out)


def write_csv(scan, path):
    """감사 추적만 CSV 로."""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(LOG_COLUMNS)
        w.writerows(log_row(i, info, row) for i, (info, row) in enumerate(scan.records, 1))
