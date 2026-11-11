"""계수기 리포트 PDF 뷰어 / 추출기 (COU-P004 / VIC55 / VIC110 계열).

리포트: AuditTrailLog(감사 추적), Batch(배치 리포트), BottleCountLog(병 계수 기록), NGImageALL(NG 이미지)

    python -m auditpdf serve    [--root DIR] [--port 8766]   로컬 뷰어 (폴더는 화면에서 고를 수 있다)
    python -m auditpdf report   [--root DIR] [-o audit_report.html]
    python -m auditpdf export   [--root DIR] [-o audit_trail_merged.xlsx] [--sheet-per-folder] [--csv-also]
    python -m auditpdf diagnose [--root DIR] [--csv 진단결과.csv]

--root 를 생략하면 뷰어 화면에서 마지막으로 설정한 폴더를 쓴다.
루트 아래를 재귀로 찾아 파일명에 위 리포트 단어가 들어간 PDF 를 모두 읽는다. 원본 PDF 는 읽기만 한다.
"""

import argparse
import datetime
import json
import sys
import webbrowser
from collections import Counter
from pathlib import Path

from .config import check_root, configured_root
from .excel import _num, batches
from .pdf import META_KEYS, SUMMARY_KEYS
from .scan import FILENAME_KEYWORD, KIND_NAMES, scan

TEMPLATE = Path(__file__).parent / "report_template.html"


def fmt_now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def group_details(result, index):
    """'[...]' 로 시작하는 행 바로 뒤에 이어지는 '=' 행들을 그 행의 상세 내역으로 묶는다.

    [파일 번호, 시간, 로그, 사용자, 쪽, 행, 글자색, [[시간, 상세, 글자색], ...]]
    '=' 행이 '[' 행 뒤가 아닌 곳(로그인/알람 행 뒤, 파일 첫 행)에 나오면 어느 행의 상세인지 알 수 없으므로 따로 둔다.
    상세 행은 부모와 시각이 몇 초 다를 수 있어 시각을 그대로 둔다.
    """
    out, parent, parent_file = [], None, None
    for info, row in result.records:
        text = row.log.lstrip()
        if text.startswith("=") and parent is not None and parent_file is info:
            parent[7].append([row.time, text[1:].strip(), row.color])
            continue
        entry = [index[id(info)], row.time, row.log, row.user, row.page, row.line, row.color, []]
        out.append(entry)
        parent, parent_file = (entry, info) if text.startswith("[") else (None, None)
    return out


def build_data(result, live=False):
    """화면에 넣을 JSON. 각 행은 파일 번호로 파일 목록을 가리킨다."""
    index = {id(f): i for i, f in enumerate(result.files)}
    audit = group_details(result, index)
    batch_rows = []
    for folder, meta, summary, trials in batches(result):
        kinds = {f.kind: index[id(f)] for f in result.files if f.folder == folder and not f.dup_of}
        batch_rows.append([folder, [meta.get(k, "") for k in META_KEYS], [summary.get(k, "") for k in SUMMARY_KEYS],
                           trials, kinds])
    return {
        "generated": fmt_now(),
        "source": str(result.root),
        "live": live,
        "keyword": FILENAME_KEYWORD,
        "meta_keys": META_KEYS,
        "summary_keys": SUMMARY_KEYS,
        "kind_names": KIND_NAMES,
        "files": [[f.seq, f.folder, f.name, f.status, f.pages, f.rows, f.alarms, f.bad_time, f.no_user,
                   f.size, f.mtime, f.sha256, [f.meta[k] for k in META_KEYS], int(f.failed), f.kind, f.dup_of]
                  for f in result.files],
        "audit": audit,
        "raw_rows": len(result.records),
        # [파일 번호, 시간, 양품, 불량, 제품수, 결과, 쪽]
        "bottle": [[index[id(f)], c[0], _num(c[1]), _num(c[2]), _num(c[3]), c[4], c[5]]
                   for f in result.of("bottle") for c in f.data.counts],
        # [파일 번호, 파일 안 번호, 시간, 채널, 유형, 쪽]
        "ng": [[index[id(f)], n, g[0], _num(g[1]), g[2], g[3]] for f in result.of("ng") for n, g in enumerate(f.data.ngs)],
        "batches": batch_rows,
    }


def render(data):
    return TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))


def cli_progress(done, total, name):
    if name:
        print(f"[{done + 1}/{total}] {name}", flush=True)


def load(args):
    result = scan(args.root, progress=cli_progress)
    if not result.files:
        sys.exit(f"오류: 파일명에 '{FILENAME_KEYWORD}' 가 들어간 PDF 가 없습니다: {args.root}")
    for f in result.files:
        if f.failed:
            print(f"경고: {f.name}: {f.status}", file=sys.stderr)
    return result


def summary(result):
    alarms = sum(f.alarms for f in result.files if not f.dup_of)
    failed = sum(f.failed for f in result.files)
    dups = sum(bool(f.dup_of) for f in result.files)
    kinds = Counter(f.kind for f in result.files)
    per_kind = ", ".join(f"{name} {kinds[k]}" for k, name in KIND_NAMES.items() if kinds[k])
    return (f"(PDF {len(result.files)}건 [{per_kind}], 로그 {len(result.records):,}행, 알람 {alarms:,}행, "
            f"계수 {sum(len(f.data.counts) for f in result.of('bottle')):,}행, "
            f"NG 이미지 {sum(len(f.data.ngs) for f in result.of('ng')):,}장, 중복 {dups}건 제외, 실패 {failed}건)")


def cmd_export(args):
    from .excel import write_csv, write_excel

    result = load(args)
    out = Path(args.output or "audit_trail_merged.xlsx")
    write_excel(result, out, per_folder=args.sheet_per_folder)
    print(f"저장: {out.resolve()}  {summary(result)}")
    if args.csv_also:
        write_csv(result, out.with_suffix(".csv"))
        print(f"저장: {out.with_suffix('.csv').resolve()}")
    return 3 if any(f.failed for f in result.files) else 0


def cmd_report(args):
    result = load(args)
    out = Path(args.output or "audit_report.html")
    out.write_text(render(build_data(result)), encoding="utf-8")
    print(f"저장: {out.resolve()}  {summary(result)}")
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())


def cmd_diagnose(args):
    from .diagnose import run
    return run(args.root, args.csv, args.detail, args.rows)


def cmd_serve(args):
    from .server import serve
    serve(args, build_data, render)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m auditpdf", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, func in (("serve", cmd_serve), ("report", cmd_report), ("export", cmd_export),
                       ("diagnose", cmd_diagnose)):
        s = sub.add_parser(name)
        s.add_argument("--root", help="Audit Trail 리뷰 루트 폴더 (기본: 뷰어에서 설정한 폴더)")
        if name == "serve":
            s.add_argument("--port", type=int, default=8766)
            s.add_argument("--no-open", action="store_true", help="브라우저로 열지 않음")
        elif name == "diagnose":
            s.add_argument("--csv", help="진단 결과 CSV 경로")
            s.add_argument("--detail", type=int, default=3, help="상세 출력할 실패 파일 수")
            s.add_argument("--rows", type=int, default=10, help="첫 파일의 샘플 로그 행 수 (0: 생략)")
        else:
            s.add_argument("-o", "--output")
        if name == "report":
            s.add_argument("--no-open", action="store_true", help="브라우저로 열지 않음")
        if name == "export":
            s.add_argument("--sheet-per-folder", action="store_true", help="폴더(배치)별로 시트를 나눔")
            s.add_argument("--csv-also", action="store_true", help="같은 데이터를 CSV(UTF-8 BOM)로도 저장")
        s.set_defaults(func=func)
    args = p.parse_args(argv)
    args.root = str(Path(args.root).expanduser()) if args.root else configured_root()
    if args.cmd != "serve":  # serve 는 화면에서 폴더를 다시 고를 수 있다
        error = check_root(args.root)
        if error:
            sys.exit(f"오류: {error}\n--root 로 PDF 루트 폴더를 지정하세요.")
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
