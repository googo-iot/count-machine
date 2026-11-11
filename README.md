# 계수기 리포트 PDF 뷰어 · 추출기

계수기(COU-P004, VIC55, VIC110 계열)가 배치마다 출력한 리포트 PDF 를 모아 PC 에서 보고 Excel 로 합친다.
루트 폴더 아래를 재귀로 찾아 아래 리포트 PDF 를 모두 읽는다. 원본 PDF 는 **읽기만** 한다.
Python 3.10+ 표준 라이브러리에 PDF 해석용 `pdfplumber`, Excel 용 `openpyxl` 만 쓴다.

```powershell
pip install pdfplumber openpyxl
```

## 읽는 리포트

파일명에 아래 단어가 **앞뒤로 다른 영문자 없이** 들어간 PDF 만 읽는다 (대소문자 무시, [auditpdf/scan.py](auditpdf/scan.py) 의 `KINDS`).
예: `20261007_110858_VIC55-REPORT-Batch[CNZ2F015A]__[니자티딘캡슐 150mg 30C].pdf` → 배치 리포트. `BatchReport.pdf` 는 읽지 않는다.

| 파일명 단어 | 종류 | 본문 |
|---|---|---|
| `AuditTrailLog` | 감사 추적 | 3열 표 TIME / LOG / USER (한글: 시간 / 로그 / 사용자) |
| `Batch` | 배치 리포트 | 요약 2열 표 (시작 사용자, 목표, 양품[병], 불량[병], 불량[제품]) + 배치 생산 결과 6열 표 |
| `BottleCountLog` | 병 계수 기록 | 5열 표 TIME / PASSED / REJECTED / PROD.COUNT / RESULT (한글: 시간 / 양품 / 불량 / 제품수 / 결과) |
| `NGImageALL` | NG 이미지 | 불량 제품 이미지(JPEG) + 시각 / [Ch n] / NG 유형 |

모든 리포트 1쪽 상단에 메타데이터 2열 표(REPORT TYPE … BATCH STOP TIME)가 있다. 영문/한글 키는 영문 표기로 통일한다.

## 사용법

```powershell
# 로컬 뷰어 (http://127.0.0.1:8766 에서만 열림)
python -m auditpdf serve

#   폴더: 화면 오른쪽 위 '폴더 설정'에서 경로 입력 또는 '찾아보기…' → 적용.
#   고른 폴더는 %APPDATA%\audit-pdf-viewer\config.json 에 저장되어 다음 실행과 report/export/diagnose 에서도 쓰인다.
#   PDF 는 별도 스레드에서 읽고 진행률을 보여 준다. 중지할 수 있다.
#   읽은 결과는 %APPDATA%\audit-pdf-viewer\cache 에 저장되어, 다시 켜도 새 파일과 바뀐 파일(경로·크기·수정 시각)만 읽는다.
#   auditpdf/pdf.py 를 고치면 기존 캐시는 자동으로 무시된다. 캐시를 비우려면 그 폴더를 지우면 된다.
#
#   감사 추적: '[...]' 로 시작하는 행 바로 뒤에 이어지는 '=' 행은 그 행의 상세 내역으로 묶어 '▸ 상세 N' 으로 펼친다.
#             ('[' 행 뒤가 아닌 곳의 '=' 행은 어느 행의 상세인지 알 수 없어 따로 둔다. Excel 은 원본 행 그대로.)
#             검색(상세 내역 포함), 배치/사용자/기간/알람 필터, 열 머리글 정렬, 지금 목록을 CSV 로.
#             행 클릭 → 선택. 'PDF 원본 보기' → 그 행이 나온 PDF 를 별도 팝업 창에서 그 쪽으로 연다 (`/pdf`, 창 위치·크기 기억).
#   배치: 폴더(배치)마다 한 줄. 검색 · 레시피 · 시작일/종료일 필터.
#         행 클릭 → 오른쪽에 병 계수 기록 그래프(양품 누계 대비 리젝트 누계), 시작 사용자, 배치 생산 결과.
#         '감사 추적' / 'NG 이미지' 버튼 → 그 배치 내용을 브라우저 별도 창(팝업)으로. 다른 모니터로 옮길 수 있고,
#         위치·크기는 기억한다. 처음 한 번 브라우저의 팝업 허용이 필요할 수 있다.
#         'PDF 원본 ▾' → 그 배치의 배치 리포트 / 감사 추적 / 병 계수 기록 / NG 이미지 PDF 중 하나를 팝업 창으로.
#         PDF 원본 창은 종류마다 따로 하나씩이다 (같은 종류를 다시 열면 그 창의 내용이 바뀐다). 감사 추적 탭 버튼은 감사 추적 창을 쓴다.
#         NG 이미지 창: 이미지 클릭 → 확대 (←/→ 이전/다음). PDF 안의 JPEG 를 그대로 보여 준다.
#   파일 목록: 종류, 상태, 쪽/항목/알람 수, TIME 형식 이상, USER 공란, SHA-256. 행 클릭 → 그 파일 (감사 추적 탭 / 배치 그래프 / NG 이미지 창).
#   모든 표: 열 머리글 오른쪽 끝을 끌어 너비 조절 (브라우저에 저장, 두 번 클릭하면 초기화). 한 쪽에 200행씩.
#   Excel 저장: 감사 추적을 한 시트로 통합하거나 폴더(배치)별 시트로 나눠 내려받는다.

# 파일 하나로 된 HTML 리포트 (NG 이미지는 목록만)
python -m auditpdf report --root "D:\...\Archive\COU-P002" -o audit_report.html

# Excel (시트: AuditTrail_All, BottleCount_All, NGImage_All, _배치정보, _배치생산결과, _파일목록, _추출실패)
python -m auditpdf export --root "D:\...\Archive\COU-P002" -o audit_trail_merged.xlsx [--sheet-per-folder] [--csv-also]

# 추출이 안 되는 PDF 의 원인 진단 (키워드 불일치 / 표 미인식 / 로그 표 헤더 불일치 / 본문 항목 0)
python -m auditpdf diagnose --root "D:\...\Archive\COU-P002" [--csv 진단결과.csv]
```

`export` 종료 코드: 0 정상, 3 일부 PDF 읽기 실패 (`_추출실패` 시트 확인).

처음 읽을 때 병 계수 기록(배치당 수십~수백 쪽)이 대부분의 시간을 차지한다 (로컬 디스크 기준 27쪽 약 3초).
COU-P002 공유 폴더(배치 146개, PDF 584건)는 처음 약 40분, 그 뒤로는 캐시로 약 12초.

규모가 크다 (COU-P002: 계수 52만 행, NG 이미지 15만 장, 한 NG 리포트가 1,000쪽 240MB 인 것도 있다).
- 뷰어는 병 계수 기록과 NG 이미지 목록을 화면에 넣지 않고 처음 쓸 때(배치 그래프, NG 이미지 창) 받는다 (`/api/rows`, gzip).
- NG 이미지는 최근에 연 PDF 4개를 열어 둔 채로 꺼낸다 ([auditpdf/images.py](auditpdf/images.py)). 큰 PDF 는 처음 한 장만 몇 초 걸린다.
- Excel 은 write-only 모드로 쓴다 (COU-P002 전체 약 1분, 36MB).
- `report` 로 만드는 HTML 은 모든 목록을 넣으므로 이 규모에서는 40MB 가 넘는다.

## 해석 방법

- 여러 행이 셀 하나로 합쳐져 나오는 표(감사 추적, 병 계수 기록, 배치 생산 결과)는 단어 좌표로 행을 복원한다.
  열 경계는 머리글 행의 칸에서 잡는다 (배치 생산 결과는 위에 합쳐진 제목 칸이 있다).
- 감사 추적은 TIME 없이 LOG 만 있는 줄을 앞 행의 줄바꿈으로 붙이고, 글자색이 검정이 아니면 알람으로 본다.
- NG 이미지는 이미지마다 오른쪽(다음 이미지 전까지)에 있는 글자에서 시각, 채널, NG 유형을 읽는다. 시각이 없는 이미지(머리글 로고)는 건너뛴다.
- 배치 탭과 `_배치정보` 시트는 폴더 하나를 배치 하나로 보고, 메타데이터는 배치 리포트 것을 먼저 쓴다.
- 분기별 백업처럼 같은 PDF(SHA-256 동일)가 다른 폴더에 또 있으면 처음 것(폴더 이름순)만 쓴다.
  나머지는 파일 목록에 '중복 (#원본 번호)' 로 남기고 감사 추적·배치·합계·Excel 집계에서는 뺀다 (`_파일목록` 의 `중복원본순번`).
- Excel 에서 `=`, `+`, `-`, `@` 로 시작하는 글자는 수식이 되지 않도록 문자열로 저장한다. 숫자로만 된 수량은 숫자로 저장한다.
- 마지막 쪽 서명란(Checked By / Verified By)은 읽지 않는다.

## 구성

| 파일 | 역할 |
|---|---|
| [auditpdf/__main__.py](auditpdf/__main__.py) | 명령줄 (serve / report / export / diagnose), 화면 데이터 생성 |
| [auditpdf/pdf.py](auditpdf/pdf.py) | 리포트 종류별 PDF 해석 (고치면 읽기 캐시가 모두 무효가 된다) |
| [auditpdf/images.py](auditpdf/images.py) | NG 이미지 꺼내기 (열어 둔 PDF 재사용) |
| [auditpdf/scan.py](auditpdf/scan.py) | 파일명 규칙, 폴더 탐색, PDF 별 결과·해시, 메모리/디스크 캐시 |
| [auditpdf/excel.py](auditpdf/excel.py) | Excel / CSV |
| [auditpdf/server.py](auditpdf/server.py) | 로컬 뷰어 서버 (NG 이미지 `/img`) |
| [auditpdf/report_template.html](auditpdf/report_template.html) | 화면 (데이터를 JSON 으로 넣어 파일 하나로도 동작) |
| [auditpdf/config.py](auditpdf/config.py) | 설정 파일, 폴더 선택 창 |
| [auditpdf/diagnose.py](auditpdf/diagnose.py) | 추출 실패 원인 진단 |
