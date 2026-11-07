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


def normalize_date_str(s: str) -> str:
    """다양한 날짜 표기(YYYY-MM-DD, YY.MM.DD 등)를 YYYY-MM-DD 형식으로 변환합니다."""
    if not s:
        return ""
    m = re.search(r"(20\d{2}|\d{2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})", str(s))
    if not m:
        return ""
    y_str, mo_str, d_str = m.group(1), m.group(2), m.group(3)
    year = int(y_str) if len(y_str) == 4 else 2000 + int(y_str)
    month = int(mo_str)
    day = int(d_str)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def extract_post_dates(period_str: str, reg_date_str: str) -> Tuple[str, str, str]:
    """
    공고의 period 및 reg_date 문자열에서 (start_date, end_date, reg_date_norm)을 추출합니다.
    period에 '~'가 포함된 기간 형식일 때만 두 번째 날짜를 마감일(end_date)로 인식합니다.
    """
    reg_norm = normalize_date_str(reg_date_str)
    p_str = str(period_str or "")
    matches = re.findall(r"(20\d{2}|\d{2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})", p_str)
    norm_dates = []
    for y_str, mo_str, d_str in matches:
        year = int(y_str) if len(y_str) == 4 else 2000 + int(y_str)
        month, day = int(mo_str), int(d_str)
        if 1 <= month <= 12 and 1 <= day <= 31:
            norm_dates.append(f"{year:04d}-{month:02d}-{day:02d}")

    start_date = ""
    end_date = ""
    if len(norm_dates) >= 2:
        start_date = norm_dates[0]
        end_date = norm_dates[-1]
    elif len(norm_dates) == 1:
        if "~" in p_str:
            end_date = norm_dates[0]
        else:
            start_date = norm_dates[0]

    if not reg_norm and start_date:
        reg_norm = start_date
    if not start_date and reg_norm:
        start_date = reg_norm

    return start_date, end_date, reg_norm


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


def format_krw_readable(amount_str: str) -> str:
    """'20,000천원', '130백만원' 같은 표기 뒤에 알기 쉬운 한글 금액(예: 2,000만원, 1억 3,000만원)을 덧붙입니다."""
    if not amount_str:
        return amount_str

    def _convert_thousand(m):
        raw_num = m.group(1).replace(",", "")
        try:
            val_man = int(float(raw_num)) // 10
            if val_man <= 0:
                return m.group(0)
            if val_man >= 10000:
                uk = val_man // 10000
                rem = val_man % 10000
                kr = f"{uk}억 {rem:,}만원" if rem else f"{uk}억원"
            else:
                kr = f"{val_man:,}만원"
            return f"{m.group(0)}({kr})"
        except Exception:
            return m.group(0)

    def _convert_million(m):
        raw_num = m.group(1).replace(",", "")
        try:
            val_man = int(float(raw_num) * 100)
            if val_man >= 10000:
                uk = val_man // 10000
                rem = val_man % 10000
                kr = f"{uk}억 {rem:,}만원" if rem else f"{uk}억원"
            else:
                kr = f"{val_man:,}만원"
            return f"{m.group(0)}({kr})"
        except Exception:
            return m.group(0)

    res = re.sub(r"(\d[\d,]*)\s*천\s*원(?!\s*\()", _convert_thousand, amount_str)
    res = re.sub(r"(\d[\d,.]*)\s*백만\s*원(?!\s*\()", _convert_million, res)
    return res


def extract_structured_info(raw_text: str, detail_text: str = "", title: str = "") -> Dict[str, str]:
    """
    공고 본문에서:
    1) 사업내용 (project_content) - 개요 첫 줄에 표시할 핵심 사업 설명
    2) 지원수 / 선정규모 (support_count)
    3) 기업당 지원금 / 지원규모 (support_amount)
    4) 불필요한 메타데이터를 제거하고 재구성한 개요 텍스트 (cleaned_body)
    를 추출합니다.
    """
    combined = f"{raw_text or ''} {detail_text or ''}".strip()
    combined = re.sub(r"\s+", " ", combined)

    # 1. 사업내용(project_content) 추출
    project_content = ""

    # 패턴 A: K-Startup 등에서 이미 추출된 [사업내용] 블록이 있는 경우
    m_ov = re.search(r"\[사업내용\]\s*(.+?)(?=\s*\[|\s*(?:신청방법 및 대상|신청기간|모집개요|입주모집개요|지원내용|제출서류)|$)", combined)
    if m_ov:
        project_content = m_ov.group(1).strip()

    # 패턴 B: 경기기업비서(EGBIZ) - '사업담당' 뒤에 실제 사업내용 문단이 위치
    if not project_content:
        m_eg = re.search(
            r"사업담당\s+(?:[가-힣A-Za-z0-9·()_\-\s]{1,25}\d{2,4}-\d{3,4}-\d{4}\s+)?(.+?)(?=\s*(?:☞|신청자격|사업신청|접수\(신청\)기간|문의처|$))",
            combined,
        )
        if m_eg:
            project_content = m_eg.group(1).strip()

    # 패턴 C: 기업마당(Bizinfo) - '사업개요' 뒤 (단, '사업개요 지원사업명' 제외)
    if not project_content:
        m_bz = re.search(
            r"사업개요\s+(?!지원사업명)(.+?)(?=\s*(?:☞|사업신청|신청자격|문의처|$))",
            combined,
        )
        if m_bz:
            project_content = m_bz.group(1).strip()

    # 패턴 D: 원문 내 '~모집하오니 많은 신청 바랍니다' 또는 '~모집합니다' 안내 문장 추출
    if not project_content:
        m_intro = re.search(
            r"([가-힣A-Za-z0-9\s,·()「」『』【】\[\]\"'ㆍ\-]{20,280}?(?:모집하오니|모집합니다|추진하오니|공고합니다|안내합니다)[^.!?☞□■]{0,60}[.!?]?)",
            combined,
        )
        if m_intro:
            cand = m_intro.group(1).strip()
            cand = re.sub(r"^.*?(?:신청기간|접수기간|신청대상|지원대상|사업담당|사업개요)\s*", "", cand).strip()
            if len(cand) >= 18:
                project_content = cand

    # 패턴 E: K-Startup 등에서 별도 인사말이 없을 때 지원내용/교육안내 또는 신청대상을 요약
    if not project_content:
        m_sup = re.search(
            r"(?:지원내용|교육안내|세부내용|모집개요|신청대상)\s+(.+?)(?=\s*(?:제외대상|신청 시 요청하는 정보|제출서류|선정절차|문의처|상기 일정은|$))",
            combined,
        )
        if m_sup:
            project_content = m_sup.group(1).strip()[:220]
        else:
            project_content = (title or combined[:150]).strip()

    # 불필요한 앞뒤 문구 정리
    project_content = re.sub(r"^\[?공고\s*제?\s*[\d\-]+호?\]?\s*", "", project_content).strip()
    if title and project_content.startswith(title) and len(project_content) > len(title) + 10:
        project_content = project_content[len(title):].strip()
    # 문장 맨 끝에 붙는 공고일자 및 기관명 서명만 제거 (문장 중간의 행사일정 '2027년 4월 5일 ~' 등은 보존)
    project_content = re.sub(r"\s*202\d년\s*\d{1,2}월\s*\d{1,2}일\s*(?![~부터까지(])[^.~!?]{0,30}$", "", project_content).strip()
    if not project_content:
        project_content = f"{title} (세부내용은 원문 공고문 및 첨부파일 참조)"
    if len(project_content) > 260:
        project_content = project_content[:260].rstrip() + "..."

    # 제외대상/제외요건 구간을 제거한 텍스트에서 지원수 및 지원금을 탐색 (벌금형 100만원, 업력 7년 이내 등 오탐 방지)
    search_zone = re.sub(
        r"(?:제외대상|제외요건|지원제외).*?(?=\s*(?:지원내용|모집개요|입주모집개요|교육안내|신청방법|사업신청|문의처|\[원문상세\]|$))",
        " ",
        combined,
    )

    # 2. 지원수 / 선정규모(support_count) 추출
    support_count = ""
    scale_label_patterns = [
        r"(?:지원규모|선정규모|모집규모|선발규모|모집인원|참가규모|입주규모)\s*[:：]?\s*((?:총\s*|최대\s*|약\s*)?\d+\s*(?:~\s*\d+\s*)?(?:개사|개\s*기업|개\s*팀|개실|곳|개\s*과제|과제|개\s*기관|개소|팀|명|기업)(?:\s*(?:내외|이내|이하|선정|모집|지원|선발))?(?:\s*\(최대\s*\d+[명팀개사곳]+\))?)",
        r"(?<!창업\s)(?<!업력\s)(?<!설립\s)(?<!최소\s)(?<!관내\s)(?<!인천\s)((?:총\s*|모집\s*|선정\s*|선발\s*)?\d+\s*(?:~\s*\d+\s*)?(?:개사|개\s*기업|개\s*팀|개실|곳|개\s*과제|개소)\s*(?:내외|이내|이하|선정|모집|지원|선발)?)",
        r"((?:총\s*|모집\s*|선정\s*|선발\s*)\d+\s*(?:~\s*\d+\s*)?(?:명|팀)\s*(?:내외|이내|이하|선정|모집|선발)?(?:\s*\(최대\s*\d+[명팀]\))?)",
        r"(\d+\s*(?:명|팀)\s*내외(?:\s*\(최대\s*\d+[명팀]\))?)",
    ]
    for pat in scale_label_patterns:
        m_sc = re.search(pat, search_zone)
        if m_sc:
            cand_sc = m_sc.group(1).strip()
            # '9개 대학', '7대 거점', '2개의 단체', '1팀(3~4인 1팀)' 등 제외
            if any(bad in cand_sc for bad in ["대학", "거점", "단체", "1팀", "호실"]):
                continue
            support_count = cand_sc
            break

    if not support_count:
        support_count = "공고문 참조 (본문 미기재)"

    # 3. 기업당 지원금(support_amount) 추출
    support_amount = ""
    amount_patterns = [
        # '기업당 최대 20,000천원', '과제 당 지원금 120,000천원', '1인당 150,000원' 등
        r"((?:기업당|과제\s*당|과제당|팀당|1인당|인당|기업별|연\s*임대료|직접\s*투자|지원금|사업화\s*자금|창업\s*자금|최대)\s*(?:\([^)]{1,18}\)\s*)?(?:지원금\s*)?(?:최대\s*|총\s*|약\s*)?\d[\d,.]*\s*(?:~\s*\d[\d,.]*\s*)?(?:백만원|천만원|억원|억\s*원|만원|만\s*원|천원|천\s*원|원)(?:\s*(?:이내|이하|이상|지원|지급|한도|상당))?)",
        # '지원규모 : 5개사 내외 (기업당 최대 20,000천원)' 안의 금액이나 일반 금액 표기
        r"((?:최대\s*|총\s*|약\s*)?\d[\d,.]*\s*(?:~\s*\d[\d,.]*\s*)?(?:백만원|천만원|억원|억\s*원|만원|만\s*원|천원|천\s*원)(?:\s*(?:이내|이하|이상|지원|지급|한도|상당))?)",
    ]
    for pat in amount_patterns:
        for m_am in re.finditer(pat, search_zone):
            cand_am = m_am.group(1).strip()
            # 벌금형 등 오탐 제외
            ctx_start = max(0, m_am.start() - 15)
            ctx_after = min(len(search_zone), m_am.end() + 15)
            ctx = search_zone[ctx_start:ctx_after]
            if "벌금" in ctx or "과태료" in ctx or "보증금" in ctx:
                continue
            support_amount = format_krw_readable(cand_am)
            break
        if support_amount:
            break

    if not support_amount:
        if any(k in combined for k in ["구축 비용", "비용 지원", "사업화 자금", "사업화자금", "지원금", "사업비", "시제품", "재료비", "제작비"]):
            support_amount = "사업비/자금 지원 (세부금액 공고문 참조)"
        elif any(k in combined for k in ["입주기업 모집", "입주자 모집", "창업보육센터", "입주공간"]):
            support_amount = "입주공간·보육 지원 (임대료 등 공고문 참조)"
        elif any(k in combined for k in ["교육", "아카데미", "워크숍", "멘토링", "컨설팅", "법률상담", "해커톤", "페스티벌", "포럼", "데모데이"]):
            support_amount = "비예산 (교육·멘토링·행사·연계 지원)"
        else:
            support_amount = "공고문 참조 (본문 미기재)"

    # 4. 나머지 주요 항목 정리 (중복 헤더·노이즈 제거)
    cleaned_body = raw_text or ""
    cleaned_body = re.sub(r"\[사업내용\]\s*.+?(?=\s*(?:신청방법 및 대상|신청기간|지원내용|입주모집개요|모집개요|$))", "", cleaned_body)
    cleaned_body = re.sub(
        r"^사업개요\s+지원사업명\s+.+?(?:사업담당\s+(?:[가-힣A-Za-z0-9·()_\-\s]{1,25}\d{2,4}-\d{3,4}-\d{4}\s+)?)",
        "",
        cleaned_body,
    )
    cleaned_body = re.sub(
        r"^소관부처·지자체\s+.+?사업개요\s+",
        "",
        cleaned_body,
    )
    if project_content and len(project_content) > 25:
        prefix_check = project_content[:35].rstrip(".")
        if prefix_check in cleaned_body:
            idx_p = cleaned_body.find(prefix_check)
            # project_content 문단 이후(예: ☞ 뒤)부터 남김
            m_after = re.search(r"(☞|신청기간|신청방법|신청대상|지원대상|지원내용|입주모집개요|모집개요|교육안내)", cleaned_body[idx_p + 10:])
            if m_after:
                cleaned_body = cleaned_body[idx_p + 10 + m_after.start():].strip()

    for footer_noise in [
        "신청 시 요청하는 정보(개인정보포함)는 사업운영기관에서 관리되오니 이점 반드시 유의하여 주시기 바랍니다.",
        "본 지원사업에 대한 문의˙신청 등의 모든 절차는 수행기관에서 전담하고 있음을 안내드립니다.",
        "상기 일정은 사업운영기관의 내부사정으로 변경될 수 있습니다.",
        "제출하신 서류는 사업운영기관에서 관리되오니 서류 반환 등 문의는 해당 기관으로 하시기 바랍니다.",
    ]:
        cleaned_body = cleaned_body.replace(footer_noise, "").strip()

    cleaned_body = re.sub(r"^신청방법 및 대상\s*", "", cleaned_body).strip()
    if len(cleaned_body) > 420:
        cleaned_body = cleaned_body[:420].rstrip() + "..."

    structured_summary = (
        f"사업내용 {project_content} "
        f"지원수 {support_count} "
        f"기업당 지원금 {support_amount} "
        f"{cleaned_body}"
    ).strip()

    return {
        "project_content": project_content,
        "support_count": support_count,
        "support_amount": support_amount,
        "summary": structured_summary,
    }


def fetch_post_summary_and_detail(url: str, site_name: str = "", title: str = "") -> Tuple[str, str, Dict[str, str]]:
    """
    공고 상세 페이지(및 연결된 원문 출처 페이지)에 접속하여:
    1) 사업내용이 첫 줄에 배치되고 지원수·기업당 지원금이 포함된 개요 (summary)
    2) AI 질문 답변용 상세 본문 (detail_text, 최대 3,000자)
    3) 구조화 필드 사전 (project_content, support_count, support_amount)
    을 추출합니다.
    """
    default_msg = "상세 페이지에서 내용을 확인해 주세요."
    empty_info = {
        "project_content": default_msg,
        "support_count": "공고문 참조",
        "support_amount": "공고문 참조",
    }
    if not url or url.startswith("javascript:"):
        return default_msg, default_msg, empty_info

    try:
        html = fetch_text(url, timeout=10)
        soup = BeautifulSoup(html, "html.parser")

        # EGBIZ나 기업마당에서 연결된 원문(출처 바로가기) URL 미리 탐색
        bizinfo_link = None
        orig_source_link = None
        if "egbiz.or.kr" in url:
            for a in soup.select("a[href]"):
                href = a.get("href", "").strip()
                if "bizinfo.go.kr" in href:
                    bizinfo_link = href
                    break
        elif "bizinfo.go.kr" in url:
            for a in soup.select("a[href]"):
                if "출처 바로가기" in a.get_text(strip=True):
                    href = a.get("href", "").strip()
                    if href.startswith("http"):
                        orig_source_link = href
                        break

        for tag in soup.select(
            "script, style, header, footer, nav, .gnb, .lnb, #header, #footer, "
            ".location, .location_share, .share_box, .btn_wrap, .board_Page, .contents_info"
        ):
            tag.decompose()

        extracted = ""
        # 1) K-Startup: 상단 .box_inner(사업내용) + 모든 .information_list 섹션을 통합 추출
        if "k-startup.go.kr" in url:
            parts = []
            for box in soup.select(".box_inner"):
                b_txt = " ".join(box.get_text(" ", strip=True).split())
                if "개인정보 수집/이용동의" in b_txt or "이메일 도메인" in b_txt:
                    continue
                if len(b_txt) >= 20:
                    parts.append(f"[사업내용] {b_txt}")
                    break

            info_lists = []
            priority_lists = []
            for il in soup.select(".information_list"):
                i_txt = " ".join(il.get_text(" ", strip=True).split())
                if not i_txt:
                    continue
                if any(i_txt.startswith(k) for k in ["지원내용", "입주모집개요", "모집개요", "교육안내", "세부내용"]):
                    priority_lists.append(i_txt)
                else:
                    info_lists.append(i_txt)
            parts.extend(priority_lists)
            parts.extend(info_lists)
            extracted = " ".join(parts).strip()

        # 2) 그 외 사이트 (기업마당, 경기기업비서, KOCCA 등)
        if not extracted:
            candidates = [
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

        # EGBIZ -> 기업마당 -> 출처 바로가기 원문 텍스트 보강 (지원수, 기업당 지원금 등 세부 수치 확보)
        extra_orig_text = ""
        try:
            if bizinfo_link and not orig_source_link:
                b_html = fetch_text(bizinfo_link, timeout=6)
                b_soup = BeautifulSoup(b_html, "html.parser")
                for a in b_soup.select("a[href]"):
                    if "출처 바로가기" in a.get_text(strip=True):
                        href = a.get("href", "").strip()
                        if href.startswith("http"):
                            orig_source_link = href
                            break

            if orig_source_link and not any(
                ext in orig_source_link.lower() for ext in [".pdf", ".hwp", ".zip", ".doc", ".xls", "pdfviewer"]
            ):
                o_html = fetch_text(orig_source_link, timeout=6)
                o_soup = BeautifulSoup(o_html, "html.parser")
                for tag in o_soup.select("script, style, header, footer, nav, .gnb, .lnb, #header, #footer"):
                    tag.decompose()
                o_txt = " ".join(o_soup.get_text(" ", strip=True).split())
                # '사업개요' 또는 '지원규모' 또는 '모집공고' 부근 텍스트 발췌
                m_sec = re.search(r"(?:사업개요|모집개요|지원규모|모집공고).{0,1200}", o_txt)
                if m_sec:
                    extra_orig_text = m_sec.group(0)
                elif len(o_txt) > 100:
                    extra_orig_text = o_txt[:1200]
        except Exception:
            pass

        if not extracted or len(extracted) < 15:
            msg = "본문이 첨부파일(HWP/PDF) 또는 이미지로 제공되는 공고입니다. [공고 보기]를 눌러 확인해 주세요."
            return msg, msg, empty_info

        full_detail = extracted
        if extra_orig_text:
            full_detail = f"{extracted} [원문상세] {extra_orig_text}"

        info = extract_structured_info(extracted, full_detail, title=title)
        summary = info["summary"]
        detail_text = full_detail[:3000].rstrip()
        return summary, detail_text, info
    except Exception:
        msg = "상세 요약을 불러오지 못했습니다. [공고 보기]를 눌러 원문을 확인해 주세요."
        return msg, msg, empty_info


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
    db_data["search_days"] = int(config.get("search_days", 30))
    db_data["auto_archive_expired"] = bool(config.get("auto_archive_expired", True))
    db_data["sites"] = config.get("sites", {})
    db_data["custom_sites"] = config.get("custom_sites", [])
    with open(DATA_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(db_data, f, ensure_ascii=False, indent=2)


def format_summary_html(raw_text: str) -> str:
    """개요 텍스트를 주요 항목별로 줄바꿈하고 사업내용·지원수·기업당 지원금 라벨을 강조합니다."""
    if not raw_text:
        return "상세 내용은 공고 보기를 눌러 확인해 주세요."
    text = html_lib.escape(raw_text)
    text = re.sub(r"^신청방법 및 대상\s*", "", text)

    # 1) 핵심 3대 라벨(사업내용, 지원수, 기업당 지원금) 강조
    primary_labels = [
        ("사업내용", "#dbeafe", "#1e3a8a"),
        ("지원수", "#dcfce7", "#166534"),
        ("기업당 지원금", "#fef3c7", "#92400e"),
    ]
    for label, bg, fg in primary_labels:
        pattern = rf"(?:^|\s+)({re.escape(label)})(?=\s)"
        badge_html = (
            f'<br><span style="display:inline-block;background:{bg};color:{fg};'
            f'font-weight:800;padding:2px 7px;border-radius:4px;margin-right:6px;font-size:12.5px;">\\1</span>'
        )
        text = re.sub(pattern, badge_html, text)

    # 2) 일반 섹션 라벨 강조
    labels = [
        "신청기간", "접수기간", "공고기간", "신청방법", "접수방법", "신청대상", "지원대상",
        "제외대상", "신청 시 요청하는 정보", "사업개요", "공고 개요", "지원내용", "지원규모",
        "입주모집개요", "모집개요", "교육안내", "세부내용",
        "소관부처·지자체", "소관부처", "사업수행기관", "수행기관", "문의처", "진행구분", "관련사이트"
    ]
    for label in labels:
        pattern = rf"(?:^|\s+)({re.escape(label)})(?=\s)"
        badge_html = (
            f'<br><span style="display:inline-block;background:#eff6ff;color:#1e40af;'
            f'font-weight:700;padding:1px 6px;border-radius:4px;margin-right:6px;font-size:12px;">\\1</span>'
        )
        text = re.sub(pattern, badge_html, text)

    text = re.sub(r"\s+(※|☞|□|■|ㅇ(?=\s))", r"<br>\1", text)
    text = re.sub(r"\s+(-\s)", r"<br>&nbsp;&nbsp;\1", text)
    text = re.sub(r"^(<br>\s*)+", "", text)
    return text


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
        summary_text = format_summary_html(item.get("summary") or "")

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
    search_days = int(config.get("search_days", 30))
    auto_archive_expired = bool(config.get("auto_archive_expired", True))
    sites_cfg = config.get("sites", {})

    today_date = datetime.date.today()
    today_str = today_date.strftime("%Y-%m-%d")
    cutoff_date_str = (
        (today_date - datetime.timedelta(days=search_days)).strftime("%Y-%m-%d")
        if search_days > 0 else ""
    )

    print(f"=== 지원사업 모니터링 시작 (키워드: {', '.join(keywords)} | 검색기간: 최근 {search_days if search_days > 0 else '전체'}일) ===")

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
    existing_items_map: Dict[str, Dict] = {}
    expired_auto_moved = 0

    for item in server_db.get("items", []):
        item_id = item.get("id")
        if not item_id:
            continue
        s_date, e_date, r_date = extract_post_dates(item.get("period", ""), item.get("reg_date", ""))
        item["start_date"] = s_date
        item["end_date"] = e_date
        item["reg_date_norm"] = r_date
        if auto_archive_expired and e_date and e_date < today_str:
            item["expired"] = True
            if item_id not in deleted_ids:
                deleted_ids.add(item_id)
                expired_auto_moved += 1
        existing_items_map[item_id] = item

    seen_ids = set() if args.ignore_seen else load_seen_ids()
    seen_ids.update(deleted_ids)

    matched_items: List[Dict] = []
    new_seen_ids = set(seen_ids)

    posts_needing_summary: List[Dict] = []

    for post in all_posts:
        target_text = post["title"]
        matched_kws = match_keywords(target_text, keywords, exclude_keywords)
        if not matched_kws:
            continue

        s_date, e_date, r_date = extract_post_dates(post.get("period", ""), post.get("reg_date", ""))
        post["start_date"] = s_date
        post["end_date"] = e_date
        post["reg_date_norm"] = r_date

        # 검색기간(search_days) 필터: 등록일(또는 시작일)이 검색기간 이전이면 제외 (단, 기존 보관 항목은 유지)
        ref_date = r_date or s_date
        if cutoff_date_str and ref_date and ref_date < cutoff_date_str and post["id"] not in existing_items_map:
            continue

        # 신청 마감일이 지난 공고는 자동으로 삭제(제외)함으로 이동
        is_expired = bool(auto_archive_expired and e_date and e_date < today_str)
        if is_expired:
            post["expired"] = True
            if post["id"] not in deleted_ids:
                deleted_ids.add(post["id"])
                expired_auto_moved += 1

        prev_item = existing_items_map.get(post["id"], {})
        post["matched_keywords"] = matched_kws
        post["collected_date"] = prev_item.get("collected_date", today_str)

        if (
            prev_item.get("summary")
            and prev_item.get("detail_text")
            and prev_item.get("project_content")
            and prev_item.get("support_count")
            and prev_item.get("support_amount")
        ):
            post["summary"] = prev_item["summary"]
            post["detail_text"] = prev_item["detail_text"]
            post["project_content"] = prev_item["project_content"]
            post["support_count"] = prev_item["support_count"]
            post["support_amount"] = prev_item["support_amount"]
        elif post["id"] not in deleted_ids:
            posts_needing_summary.append(post)
        else:
            info = extract_structured_info(
                prev_item.get("summary", ""),
                prev_item.get("detail_text", ""),
                title=post["title"],
            )
            post["project_content"] = info["project_content"]
            post["support_count"] = info["support_count"]
            post["support_amount"] = info["support_amount"]
            post["summary"] = info["summary"]
            post["detail_text"] = prev_item.get("detail_text", post["summary"])

        existing_items_map[post["id"]] = post

        if post["id"] not in deleted_ids and post["id"] not in seen_ids:
            matched_items.append(post)
            new_seen_ids.add(post["id"])

    # 기존 보관함 항목 중 아직 구조화 필드(project_content 등)가 없는 항목도 함께 보강
    for ex_id, ex_item in existing_items_map.items():
        if ex_id not in deleted_ids and not ex_item.get("project_content"):
            if ex_item not in posts_needing_summary:
                posts_needing_summary.append(ex_item)

    if posts_needing_summary:
        print(f"- 공고 상세 개요(사업내용·지원수·기업당 지원금) 및 AI 분석용 본문 추출 중 ({len(posts_needing_summary)}건)...")
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                (p, executor.submit(fetch_post_summary_and_detail, p["url"], p["site"], p.get("title", "")))
                for p in posts_needing_summary
            ]
            for p, fut in futures:
                try:
                    summary_txt, detail_txt, info = fut.result()
                    p["summary"] = summary_txt
                    p["detail_text"] = detail_txt
                    p["project_content"] = info.get("project_content", "")
                    p["support_count"] = info.get("support_count", "공고문 참조")
                    p["support_amount"] = info.get("support_amount", "공고문 참조")
                except Exception:
                    p["summary"] = "상세 내용은 [공고 보기]를 눌러 확인해 주세요."
                    p["detail_text"] = p["summary"]
                    p["project_content"] = p.get("title", "")
                    p["support_count"] = "공고문 참조"
                    p["support_amount"] = "공고문 참조"

    # 전체 아이템(활성 + 삭제/만료 보관분 최대 500건) 저장
    all_stored_items = list(existing_items_map.values())[:500]
    active_items_list = [item for item in all_stored_items if item.get("id") not in deleted_ids]
    server_db["items"] = all_stored_items
    server_db["deleted_ids"] = list(deleted_ids)
    save_server_db(server_db, config)
    print(
        f"[서버 DB 갱신] 진행중 공고: {len(active_items_list)}건 | "
        f"삭제/기간만료 보관함: {len(deleted_ids)}건 (금회 만료 자동이동: {expired_auto_moved}건)"
    )

    print(f"=== 필터링 결과: 총 {len(all_posts)}건 중 신규 키워드 매칭 {len(matched_items)}건 ===")
    for idx, item in enumerate(matched_items, 1):
        kws = ", ".join(item["matched_keywords"])
        print(f"  {idx}. [{item['site']}] ({kws}) {item['title']} ({item['period']})")

    # 신규 공고가 없더라도 매일 리포트를 받아볼 수 있도록 진행중 보관함 최신 목록(최대 30건)을 이메일 본문에 포함
    report_items = matched_items if matched_items else active_items_list[:30]
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
