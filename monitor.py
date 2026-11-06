import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import html as html_lib
import json
import os
import re
import smtplib
import ssl
import subprocess
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Set, Tuple

from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SEEN_PATH = BASE_DIR / "seen_posts.json"
DOCS_DIR = BASE_DIR / "docs"
DATA_JSON_PATH = DOCS_DIR / "data.json"
REPORT_PATH = BASE_DIR / "latest_report.html"

DASHBOARD_URL = "https://patrickmoon75.github.io/MBKmonitoring/"

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def fetch_text(url: str, data: bytes = None, extra_headers: Dict[str, str] = None, timeout: int = 15) -> str:
    """지정한 URL에서 HTML 또는 JSON 텍스트를 가져옵니다."""
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as resp:
        raw = resp.read()
        for enc in ("utf-8", "euc-kr", "cp949"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="ignore")


def match_keywords(text: str, keywords: List[str], exclude_keywords: List[str]) -> List[str]:
    """제목 텍스트에서 포함 키워드 및 제외 키워드를 검사합니다."""
    if not text:
        return []

    for ex in exclude_keywords:
        if ex and ex.lower() in text.lower():
            return []

    matched = []
    for kw in keywords:
        if not kw:
            continue
        if re.fullmatch(r"[A-Za-z0-9]+", kw) and len(kw) <= 3:
            pattern = rf"(?<![A-Za-z]){re.escape(kw)}(?![A-Za-z])"
            if re.search(pattern, text, flags=re.IGNORECASE):
                matched.append(kw)
        else:
            if kw.lower() in text.lower():
                matched.append(kw)
    return matched


def fetch_post_summary_and_detail(url: str, site_name: str = "") -> Tuple[str, str]:
    """
    공고 상세 페이지에 접속하여:
    1) 화면 표시용 간략 개요 (summary, 약 420자)
    2) AI 질문 답변용 상세 본문 (detail_text, 최대 2,500자)
    두 가지를 추출합니다.
    """
    default_msg = "상세 페이지에서 내용을 확인해 주세요."
    if not url or url.startswith("javascript:"):
        return default_msg, default_msg

    try:
        html = fetch_text(url, timeout=10)
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup.select(
            "script, style, header, footer, nav, .gnb, .lnb, #header, #footer, "
            ".location, .location_share, .share_box, .btn_wrap, .board_Page, .contents_info"
        ):
            tag.decompose()

        candidates = [
            ".information_list",   # K-Startup
            ".view_cont",          # 기업마당 (Bizinfo)
            ".tender_con",         # KOCCA 지원사업
            ".board_cont",         # KOCCA 공지사항
            ".pageWrap",           # 경기기업비서 (EGBIZ)
            ".board_view",
            ".bbs_view",
            ".view_body",
            ".view_con",
            "article",
            "#contents",
            "#content",
        ]

        extracted = ""
        for sel in candidates:
            el = soup.select_one(sel)
            if el:
                txt = " ".join(el.get_text(" ", strip=True).split())
                for noise in [
                    "지원사업신청 목록가기 공유하기 프린트",
                    "첨부파일 일괄다운로드",
                    "공유하기 페이스북 트위터",
                ]:
                    txt = txt.replace(noise, "").strip()
                if len(txt) >= 35:
                    extracted = txt
                    break

        if not extracted:
            body_el = soup.body or soup
            extracted = " ".join(body_el.get_text(" ", strip=True).split())

        if not extracted or len(extracted) < 15:
            msg = "본문이 첨부파일(HWP/PDF) 또는 이미지로 제공되는 공고입니다. [공고 보기]를 눌러 확인해 주세요."
            return msg, msg

        summary = extracted[:420].rstrip() + ("..." if len(extracted) > 420 else "")
        detail_text = extracted[:2500].rstrip()
        return summary, detail_text
    except Exception:
        msg = "상세 요약을 불러오지 못했습니다. [공고 보기]를 눌러 원문을 확인해 주세요."
        return msg, msg


def fetch_kstartup(max_pages: int = 3) -> List[Dict]:
    """1. K-Startup (창업지원포털) 진행중 공고 수집"""
    items = []
    for page in range(1, max_pages + 1):
        url = f"https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do?page={page}"
        try:
            html = fetch_text(url)
            soup = BeautifulSoup(html, "html.parser")
            rows = soup.select(".board_list-wrap li")
            if not rows:
                break

            for li in rows:
                tit_el = li.select_one("p.tit")
                a_el = li.select_one(".middle a")
                if not tit_el or not a_el:
                    continue

                title = tit_el.get_text(strip=True)
                href = a_el.get("href", "")
                m = re.search(r"go_view\((\d+)\)", href)
                if not m:
                    continue
                pbanc_sn = m.group(1)
                detail_url = (
                    f"https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do"
                    f"?schM=view&pbancSn={pbanc_sn}"
                )

                info_spans = [s.get_text(strip=True) for s in li.select(".bottom .list")]
                agency = info_spans[1] if len(info_spans) > 1 else "K-Startup"
                reg_date = ""
                period = ""
                start_dt, end_dt = "", ""
                for sp in info_spans:
                    if "등록일자" in sp:
                        reg_date = sp.replace("등록일자", "").strip()
                    elif "시작일자" in sp:
                        start_dt = sp.replace("시작일자", "").strip()
                    elif "마감일자" in sp:
                        end_dt = sp.replace("마감일자", "").strip()
                if start_dt or end_dt:
                    period = f"{start_dt} ~ {end_dt}"

                items.append({
                    "id": f"kstartup_{pbanc_sn}",
                    "site": "K-Startup",
                    "title": title,
                    "agency": agency,
                    "period": period,
                    "reg_date": reg_date,
                    "url": detail_url,
                })
        except Exception as e:
            print(f"[경고] K-Startup {page}페이지 수집 실패: {e}")
            break
    return items


def fetch_bizinfo(max_pages: int = 3) -> List[Dict]:
    """2. 기업마당 (Bizinfo) 지원사업 공고 수집"""
    items = []
    for page in range(1, max_pages + 1):
        url = (
            f"https://www.bizinfo.go.kr/web/lay1/bbs/S1T122C128/AS/74/list.do"
            f"?rows=15&cpage={page}"
        )
        try:
            html = fetch_text(url)
            soup = BeautifulSoup(html, "html.parser")
            rows = soup.select("table tbody tr")
            if not rows:
                break

            for tr in rows:
                tds = tr.select("td")
                if len(tds) < 7:
                    continue
                a_el = tds[2].select_one("a")
                if not a_el:
                    continue

                title = a_el.get_text(strip=True)
                href = a_el.get("href", "")
                m = re.search(r"pblancId=([A-Za-z0-9_]+)", href)
                pblanc_id = m.group(1) if m else title
                detail_url = urllib.parse.urljoin("https://www.bizinfo.go.kr", href)

                category = tds[1].get_text(strip=True)
                period = tds[3].get_text(strip=True)
                agency = f"{tds[4].get_text(strip=True)} ({category})"
                reg_date = tds[6].get_text(strip=True)

                items.append({
                    "id": f"bizinfo_{pblanc_id}",
                    "site": "기업마당",
                    "title": title,
                    "agency": agency,
                    "period": period,
                    "reg_date": reg_date,
                    "url": detail_url,
                })
        except Exception as e:
            print(f"[경고] 기업마당 {page}페이지 수집 실패: {e}")
            break
    return items


def fetch_egbiz(max_pages: int = 3) -> List[Dict]:
    """3. 경기기업비서 (이지비즈) 지원사업 공고 수집 (AJAX JSON)"""
    items = []
    page_unit = 15 * max_pages
    url = "https://www.egbiz.or.kr/sp/selectSupportPrjListAjax.do"
    payload = urllib.parse.urlencode({
        "pageIndex": "1",
        "bizNm": "",
        "areaCode": "",
        "categoryId": "",
        "sortCd": "bizCyclId",
        "prjStatus": "apply",
        "gginsttYn": "Y",
        "part": "category",
        "pageUnit": str(page_unit),
    }).encode("utf-8")

    try:
        res_text = fetch_text(
            url,
            data=payload,
            extra_headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        data = json.loads(res_text)
        for row in data.get("value", []) or []:
            biz_id = row.get("bizCyclId")
            title = (row.get("bizNm") or "").strip()
            if not biz_id or not title:
                continue

            agency = row.get("outsdInstNm") or "경기기업비서"
            cat_nm = row.get("categoryNm") or ""
            if cat_nm:
                agency = f"{agency} ({cat_nm})"

            start_dt = row.get("aplyBgngDt") or ""
            end_dt = row.get("aplyEndDt") or ""
            period = f"{start_dt} ~ {end_dt}".strip(" ~")
            reg_date = row.get("mdfcnDt") or start_dt
            detail_url = (
                f"https://www.egbiz.or.kr/sp/supportPrjDtl.do"
                f"?listUrl=supportPrjCatList&bizCyclId={biz_id}"
            )

            items.append({
                "id": f"egbiz_{biz_id}",
                "site": "경기기업비서(이지비즈)",
                "title": title,
                "agency": agency,
                "period": period,
                "reg_date": reg_date,
                "url": detail_url,
            })
    except Exception as e:
        print(f"[경고] 경기기업비서 수집 실패: {e}")

    return items


def fetch_kocca(max_pages: int = 3) -> List[Dict]:
    """4. 한국콘텐츠진흥원 (KOCCA) 지원사업 및 주요 공지사항 수집"""
    items = []
    seen_local = set()

    for page in range(1, max_pages + 1):
        url = f"https://www.kocca.kr/kocca/pims/list.do?menuNo=204104&pageIndex={page}"
        try:
            html = fetch_text(url)
            soup = BeautifulSoup(html, "html.parser")
            rows = soup.select("table tbody tr")
            if not rows:
                break

            for tr in rows:
                tds = tr.select("td")
                if len(tds) < 4:
                    continue
                a_el = tds[1].select_one("a")
                if not a_el:
                    continue

                title = a_el.get_text(strip=True)
                href = a_el.get("href", "")
                m = re.search(r"intcNo=([A-Za-z0-9_]+)", href)
                intc_no = m.group(1) if m else title
                post_id = f"kocca_pims_{intc_no}"
                if post_id in seen_local:
                    continue
                seen_local.add(post_id)

                detail_url = urllib.parse.urljoin("https://www.kocca.kr", href)
                reg_date = tds[2].get_text(strip=True)
                period = tds[3].get_text(strip=True)

                items.append({
                    "id": post_id,
                    "site": "한국콘텐츠진흥원(KOCCA)",
                    "title": title,
                    "agency": "한국콘텐츠진흥원 (지원사업)",
                    "period": period,
                    "reg_date": reg_date,
                    "url": detail_url,
                })
        except Exception as e:
            print(f"[경고] 한국콘텐츠진흥원(지원사업) {page}페이지 수집 실패: {e}")
            break

    for page in range(1, max_pages + 1):
        url = f"https://www.kocca.kr/kocca/bbs/list/B0000137.do?menuNo=204102&pageIndex={page}"
        try:
            html = fetch_text(url)
            soup = BeautifulSoup(html, "html.parser")
            rows = soup.select("table tbody tr")
            if not rows:
                break

            for tr in rows:
                tds = tr.select("td")
                if len(tds) < 3:
                    continue
                a_el = tds[1].select_one("a")
                if not a_el:
                    continue

                title = a_el.get_text(strip=True)
                href = a_el.get("href", "")
                m = re.search(r"/B0000137/(\d+)\.do", href)
                ntt_id = m.group(1) if m else title
                post_id = f"kocca_bbs_{ntt_id}"
                if post_id in seen_local:
                    continue
                seen_local.add(post_id)

                detail_url = urllib.parse.urljoin("https://www.kocca.kr", href)
                reg_date = tds[2].get_text(strip=True)

                items.append({
                    "id": post_id,
                    "site": "한국콘텐츠진흥원(KOCCA)",
                    "title": title,
                    "agency": "한국콘텐츠진흥원 (공지/모집)",
                    "period": reg_date,
                    "reg_date": reg_date,
                    "url": detail_url,
                })
        except Exception as e:
            print(f"[경고] 한국콘텐츠진흥원(공지사항) {page}페이지 수집 실패: {e}")
            break

    return items


def fetch_custom_site(site_cfg: Dict) -> List[Dict]:
    """사용자가 추가한 커스텀 검색 사이트 수집."""
    items = []
    name = site_cfg.get("name", "추가 사이트")
    list_url = site_cfg.get("list_url") or site_cfg.get("url", "")
    if not list_url:
        return items

    base_url = site_cfg.get("base_url") or list_url
    row_sel = site_cfg.get("row_selector", "")
    title_sel = site_cfg.get("title_selector", "")
    date_sel = site_cfg.get("date_selector", "")

    try:
        html = fetch_text(list_url)
        soup = BeautifulSoup(html, "html.parser")

        rows = []
        if row_sel:
            rows = soup.select(row_sel)
        else:
            for candidate in [
                "table tbody tr",
                ".board_list li",
                ".bbs_list li",
                ".list_wrap li",
                "ul.board li",
                "table tr",
            ]:
                found = soup.select(candidate)
                if len(found) >= 2:
                    rows = found
                    break

        seen_titles = set()
        for row in rows:
            a_el = None
            if title_sel:
                a_el = row.select_one(title_sel)
            else:
                links = row.select("a")
                best_len = 0
                for link in links:
                    t = link.get_text(strip=True)
                    if len(t) > best_len and len(t) >= 8:
                        best_len = len(t)
                        a_el = link

            if not a_el:
                continue

            title = " ".join(a_el.get_text(" ", strip=True).split())
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)

            href = a_el.get("href", "")
            if href and not href.startswith("javascript:"):
                detail_url = urllib.parse.urljoin(base_url, href)
            else:
                detail_url = list_url

            reg_date = "-"
            if date_sel:
                d_el = row.select_one(date_sel)
                if d_el:
                    reg_date = d_el.get_text(strip=True)
            else:
                row_text = row.get_text(" ", strip=True)
                m_date = re.search(r"(20\d{2}[-./]\d{2}[-./]\d{2})", row_text)
                if m_date:
                    reg_date = m_date.group(1)

            safe_id = re.sub(r"[^A-Za-z0-9가-힣_]", "", title)[:30]
            post_id = f"custom_{name}_{safe_id}"
            items.append({
                "id": post_id,
                "site": name,
                "title": title,
                "agency": name,
                "period": reg_date,
                "reg_date": reg_date,
                "url": detail_url,
            })
    except Exception as e:
        print(f"[경고] 커스텀 사이트({name}) 수집 실패: {e}")

    return items


def load_server_db() -> Dict:
    """docs/data.json에 저장된 누적 공고 DB를 불러옵니다."""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    if DATA_JSON_PATH.exists():
        try:
            with open(DATA_JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "updated_at": "",
        "items": [],
        "deleted_ids": []
    }


def save_server_db(db_data: Dict, config: Dict) -> None:
    """누적 공고 목록과 현재 설정을 docs/data.json에 저장합니다."""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    db_data["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_data["keywords"] = config.get("keywords", [])
    db_data["exclude_keywords"] = config.get("exclude_keywords", [])
    db_data["sites"] = config.get("sites", {})
    db_data["custom_sites"] = config.get("custom_sites", [])
    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(db_data, f, ensure_ascii=False, indent=2)


def build_html_report(matched_items: List[Dict], keywords: List[str], today_str: str) -> str:
    """
    이메일 본문 및 로컬 확인용 HTML 리포트를 생성합니다.
    - '📄 개요' 버튼 클릭 시 확장 + '🤖 AI 질문하기' 연동 포함
    """
    rows_html = ""
    for item in matched_items:
        badges = " ".join(
            f'<span style="display:inline-block;background:#eef2ff;color:#3730a3;'
            f'padding:2px 8px;border-radius:12px;font-size:12px;font-weight:600;margin-right:4px;">'
            f'#{kw}</span>'
            for kw in item.get("matched_keywords", [])
        )
        encoded_id = urllib.parse.quote(item["id"])
        delete_url = f"{DASHBOARD_URL}?delete={encoded_id}"
        ask_url = f"{DASHBOARD_URL}?ask={encoded_id}"
        summary_text = html_lib.escape(item.get("summary") or "상세 내용은 공고 보기를 눌러 확인해 주세요.")

        rows_html += f"""
        <tr>
            <td style="padding:12px 10px;border-bottom:1px solid #e5e7eb;font-size:13px;color:#4b5563;white-space:nowrap;vertical-align:top;">
                <strong>{item['site']}</strong><br>
                <span style="font-size:12px;color:#6b7280;">{item['agency']}</span>
            </td>
            <td style="padding:12px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top;">
                <div style="margin-bottom:4px;">{badges}</div>
                <a href="{item['url']}" target="_blank" style="color:#111827;font-size:15px;font-weight:600;text-decoration:none;">
                    {item['title']}
                </a>
                <details style="margin-top:8px;">
                    <summary style="display:inline-block;cursor:pointer;background:#f3f4f6;color:#374151;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:600;border:1px solid #d1d5db;user-select:none;">
                        📄 개요
                    </summary>
                    <div style="margin-top:8px;padding:12px 14px;background:#f8fafc;border-left:3px solid #2563eb;border-radius:6px;font-size:13px;color:#374151;line-height:1.6;">
                        <div style="margin-bottom:10px;">{summary_text}</div>
                        <a href="{ask_url}" target="_blank"
                           style="display:inline-block;background:#7c3aed;color:#ffffff;padding:6px 12px;border-radius:6px;font-size:12px;text-decoration:none;font-weight:600;">
                           🤖 이 공고에 대해 AI에게 질문하기
                        </a>
                    </div>
                </details>
            </td>
            <td style="padding:12px 10px;border-bottom:1px solid #e5e7eb;font-size:13px;color:#374151;white-space:nowrap;vertical-align:top;">
                {item['period'] or '-'}
            </td>
            <td style="padding:12px 10px;border-bottom:1px solid #e5e7eb;text-align:center;white-space:nowrap;vertical-align:top;">
                <a href="{ask_url}" target="_blank"
                   style="display:inline-block;background:#f3e8ff;color:#6d28d9;padding:6px 10px;border-radius:6px;font-size:12px;text-decoration:none;font-weight:600;margin-right:4px;">
                   🤖 AI 질문
                </a>
                <a href="{item['url']}" target="_blank"
                   style="display:inline-block;background:#2563eb;color:#ffffff;padding:6px 10px;border-radius:6px;font-size:12px;text-decoration:none;font-weight:600;margin-right:4px;">
                   공고 보기
                </a>
                <a href="{delete_url}" target="_blank"
                   style="display:inline-block;background:#fee2e2;color:#dc2626;padding:6px 10px;border-radius:6px;font-size:12px;text-decoration:none;font-weight:600;">
                   🗑️ 삭제
                </a>
            </td>
        </tr>
        """

    if not rows_html:
        rows_html = """
        <tr>
            <td colspan="4" style="padding:30px;text-align:center;color:#6b7280;font-size:14px;">
                오늘 새로 등록된 키워드 매칭 지원사업이 없습니다.
            </td>
        </tr>
        """

    kw_str = ", ".join(keywords)
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>IT/창업 지원사업 모니터링 리포트 ({today_str})</title>
</head>
<body style="margin:0;padding:20px;background-color:#f3f4f6;font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;">
    <div style="max-width:980px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06);">
        <div style="background:#1e3a8a;color:#ffffff;padding:24px 28px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
            <div>
                <h2 style="margin:0 0 8px 0;font-size:20px;">🚀 맞춤형 지원사업 데일리 리포트 ({today_str})</h2>
                <p style="margin:0;font-size:13px;color:#bfdbfe;">
                    설정 키워드: <strong>{kw_str}</strong> | 신규 매칭: <strong>{len(matched_items)}건</strong>
                </p>
            </div>
            <div>
                <a href="{DASHBOARD_URL}" target="_blank"
                   style="display:inline-block;background:#ffffff;color:#1e3a8a;padding:8px 14px;border-radius:8px;font-size:13px;font-weight:700;text-decoration:none;">
                   📋 웹 대시보드 (AI 질문 / 사이트·키워드 관리) 열기
                </a>
            </div>
        </div>
        <div style="padding:20px 28px;">
            <table style="width:100%;border-collapse:collapse;">
                <thead>
                    <tr style="background:#f9fafb;border-bottom:2px solid #e5e7eb;text-align:left;font-size:13px;color:#374151;">
                        <th style="padding:10px;">출처 / 기관</th>
                        <th style="padding:10px;">지원사업 공고명 ('📄 개요' 클릭 시 내용 확장)</th>
                        <th style="padding:10px;">접수기간</th>
                        <th style="padding:10px;text-align:center;">AI 질문 / 원문 / 삭제</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
        </div>
        <div style="background:#f9fafb;padding:14px 28px;font-size:12px;color:#6b7280;border-top:1px solid #e5e7eb;">
            • 각 공고의 <strong>[📄 개요]</strong>를 누르면 핵심 요약이 펼쳐지며, <strong>[🤖 AI 질문]</strong>을 누르면 해당 공고 내용을 바탕으로 AI와 질의응답할 수 있습니다.<br>
            • <strong>[🗑️ 삭제]</strong> 버튼을 누르면 서버 보관함에서 삭제되며, 검색 사이트 및 키워드 추가는 <a href="{DASHBOARD_URL}" target="_blank" style="color:#2563eb;font-weight:600;">웹 대시보드({DASHBOARD_URL})</a>에서 관리할 수 있습니다.
        </div>
    </div>
</body>
</html>
"""


def send_email(subject: str, html_body: str) -> Tuple[bool, str]:
    """환경 변수에 설정된 SMTP 계정 정보로 이메일을 발송합니다."""
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    # 구글 앱 비밀번호 복사 시 포함될 수 있는 공백 자동 제거
    smtp_pass = os.environ.get("SMTP_PASS", "").replace(" ", "").strip()
    recipient_raw = os.environ.get("RECIPIENT_EMAIL", smtp_user).strip()

    if not smtp_user or not smtp_pass or not recipient_raw:
        msg_str = "이메일 환경변수(SMTP_USER, SMTP_PASS, RECIPIENT_EMAIL) 미설정"
        print(f"[안내] {msg_str} -> 이메일 발송을 건너뜁니다.")
        return False, msg_str

    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    if not smtp_host:
        if smtp_user.lower().endswith("@naver.com"):
            smtp_host = "smtp.naver.com"
        elif smtp_user.lower().endswith("@daum.net") or smtp_user.lower().endswith("@hanmail.net"):
            smtp_host = "smtp.daum.net"
        else:
            smtp_host = "smtp.gmail.com"

    smtp_port = int(os.environ.get("SMTP_PORT", "587") or "587")
    recipients = [r.strip() for r in recipient_raw.split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=25) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, recipients, msg.as_string())
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status_msg = f"발송 성공 ({now_str})"
        print(f"[성공] 이메일 발송 완료 -> {', '.join(recipients)}")
        return True, status_msg
    except Exception as e:
        err_msg = f"발송 실패: {type(e).__name__} - {e}"
        print(f"[오류] 이메일 {err_msg}")
        return False, err_msg


def load_seen_ids() -> Set[str]:
    if SEEN_PATH.exists():
        try:
            with open(SEEN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("seen_ids", []))
        except Exception:
            pass
    return set()


def save_seen_ids(seen_ids: Set[str]) -> None:
    trimmed = sorted(list(seen_ids))[-5000:]
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "updated_at": datetime.datetime.now().isoformat(),
                "seen_ids": trimmed,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def open_in_chrome(report_path: Path) -> None:
    """클라우드(GitHub Actions) 환경이 아닐 때 크롬 브라우저로 리포트를 엽니다."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return

    chrome_candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for chrome_exe in chrome_candidates:
        if os.path.exists(chrome_exe):
            try:
                subprocess.Popen([chrome_exe, str(report_path)])
                print(f"[브라우저 실행] 크롬에서 리포트를 열었습니다: {report_path}")
                return
            except Exception as e:
                print(f"[경고] 크롬 실행 실패: {e}")

    try:
        os.startfile(str(report_path))
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="지원사업 모니터링 및 이메일 알림 봇")
    parser.add_argument("--dry-run", action="store_true", help="이메일을 보내지 않고 리포트 및 DB 파일만 갱신")
    parser.add_argument("--ignore-seen", action="store_true", help="이미 확인한 공고도 포함하여 검색")
    args = parser.parse_args()

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    keywords = config.get("keywords", [])
    exclude_keywords = config.get("exclude_keywords", [])
    max_pages = int(config.get("max_pages_per_site", 3))
    sites_cfg = config.get("sites", {})

    print(f"=== 지원사업 모니터링 시작 (키워드: {', '.join(keywords)}) ===")

    all_posts: List[Dict] = []
    if sites_cfg.get("kstartup", {}).get("enabled", True):
        posts = fetch_kstartup(max_pages)
        print(f"- K-Startup 수집: {len(posts)}건")
        all_posts.extend(posts)

    if sites_cfg.get("bizinfo", {}).get("enabled", True):
        posts = fetch_bizinfo(max_pages)
        print(f"- 기업마당 수집: {len(posts)}건")
        all_posts.extend(posts)

    if sites_cfg.get("egbiz", {}).get("enabled", True):
        posts = fetch_egbiz(max_pages)
        print(f"- 경기기업비서(이지비즈) 수집: {len(posts)}건")
        all_posts.extend(posts)

    if sites_cfg.get("kocca", {}).get("enabled", True):
        posts = fetch_kocca(max_pages)
        print(f"- 한국콘텐츠진흥원(KOCCA) 수집: {len(posts)}건")
        all_posts.extend(posts)

    for c_site in config.get("custom_sites", []):
        if c_site.get("enabled", False):
            posts = fetch_custom_site(c_site)
            print(f"- {c_site.get('name')} 수집: {len(posts)}건")
            all_posts.extend(posts)

    server_db = load_server_db()
    deleted_ids = set(server_db.get("deleted_ids", []))
    existing_items_map = {
        item["id"]: item
        for item in server_db.get("items", [])
        if item.get("id") not in deleted_ids
    }

    seen_ids = set() if args.ignore_seen else load_seen_ids()
    seen_ids.update(deleted_ids)

    matched_items: List[Dict] = []
    new_seen_ids = set(seen_ids)
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    posts_needing_summary: List[Dict] = []

    for post in all_posts:
        if post["id"] in deleted_ids:
            continue

        target_text = post["title"]
        matched_kws = match_keywords(target_text, keywords, exclude_keywords)
        if not matched_kws:
            continue

        prev_item = existing_items_map.get(post["id"], {})
        post["matched_keywords"] = matched_kws
        post["collected_date"] = prev_item.get("collected_date", today_str)

        if prev_item.get("summary") and prev_item.get("detail_text"):
            post["summary"] = prev_item["summary"]
            post["detail_text"] = prev_item["detail_text"]
        else:
            posts_needing_summary.append(post)

        existing_items_map[post["id"]] = post

        if post["id"] not in seen_ids:
            matched_items.append(post)
            new_seen_ids.add(post["id"])

    if posts_needing_summary:
        print(f"- 신규 공고 상세 개요 및 AI 분석용 본문 추출 중 ({len(posts_needing_summary)}건)...")
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                (p, executor.submit(fetch_post_summary_and_detail, p["url"], p["site"]))
                for p in posts_needing_summary
            ]
            for p, fut in futures:
                try:
                    summary_txt, detail_txt = fut.result()
                    p["summary"] = summary_txt
                    p["detail_text"] = detail_txt
                except Exception:
                    p["summary"] = "상세 내용은 [공고 보기]를 눌러 확인해 주세요."
                    p["detail_text"] = p["summary"]

    updated_items_list = list(existing_items_map.values())
    server_db["items"] = updated_items_list
    save_server_db(server_db, config)
    print(f"[서버 DB 갱신] 현재 보관 중인 누적 공고: {len(updated_items_list)}건 (삭제 제외: {len(deleted_ids)}건)")

    print(f"=== 필터링 결과: 총 {len(all_posts)}건 중 신규 키워드 매칭 {len(matched_items)}건 ===")
    for idx, item in enumerate(matched_items, 1):
        kws = ", ".join(item["matched_keywords"])
        print(f"  {idx}. [{item['site']}] ({kws}) {item['title']} ({item['period']})")

    # 신규 공고가 없더라도 매일 리포트를 받아볼 수 있도록 보관함 최신 목록(최대 30건)을 이메일 본문에 포함
    report_items = matched_items if matched_items else updated_items_list[:30]
    html_report = build_html_report(report_items, keywords, today_str)

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html_report)
    print(f"[저장 완료] 리포트 미리보기 파일: {REPORT_PATH}")
    open_in_chrome(REPORT_PATH)

    if args.dry_run:
        print("[Dry-Run 모드] 이메일 발송 및 seen_posts.json 업데이트를 건너뜁니다.")
        return

    send_only_when_new = config.get("email", {}).get("send_only_when_new", False)
    if not matched_items and send_only_when_new:
        print("[안내] 신규 매칭 공고가 없어 이메일을 발송하지 않습니다.")
        return

    subject_prefix = config.get("email", {}).get("subject_prefix", "[IT/창업 지원사업 알림]")
    if matched_items:
        subject = f"{subject_prefix} {today_str} 신규 매칭 {len(matched_items)}건"
    else:
        subject = f"{subject_prefix} {today_str} 진행중 지원사업 브리핑 ({len(report_items)}건)"

    sent, email_status = send_email(subject, html_report)
    server_db["last_email_status"] = email_status
    save_server_db(server_db, config)

    if sent:
        save_seen_ids(new_seen_ids)


if __name__ == "__main__":
    main()
