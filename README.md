# 🚀 IT/창업 지원사업 데일리 모니터링 & 이메일 알림 봇

지정한 정부·지자체·진흥원 사이트를 매일 자동으로 모니터링하여 설정한 키워드(`AI`, `AX`, `로봇`, `창업`, `벤처` 등)에 부합하는 신규 지원사업만 이메일로 발송해 주는 클라우드 자동화 프로그램입니다.

---

## 📌 1. 기본 모니터링 대상 사이트
1. **K-Startup (창업지원포털)**: https://www.k-startup.go.kr/
2. **기업마당 (Bizinfo)**: https://www.bizinfo.go.kr/web/index.do
3. **경기기업비서 (구 이지비즈)**: https://www.egbiz.or.kr/index.do
4. **한국콘텐츠진흥원 (KOCCA)**: https://www.kocca.kr/kocca/main.do (지원사업 + 공지/모집 게시판)

---

## ✏️ 2. 키워드 및 사이트 추가 방법 (`config.json`)
코드를 건드릴 필요 없이 [`config.json`](./config.json) 파일만 열어서 언제든 수정할 수 있습니다. (GitHub에 올린 뒤에는 스마트폰이나 웹 브라우저에서도 바로 수정 가능)

```json
{
  "keywords": [
    "AI",
    "인공지능",
    "AX",
    "로봇",
    "창업",
    "벤처",
    "스타트업"
  ],
  "exclude_keywords": [
    "어업용",
    "일반음식점"
  ]
}
```
* **`keywords`**: 제목에 이 중 하나라도 포함되면 수집합니다. 생각나는 키워드를 큰따옴표(`"키워드"`)로 자유롭게 추가하세요.
* **`exclude_keywords`**: 관심 없는 분야(예: `"농산업"`, `"어업용"`, `"요식업"`)를 넣으면 해당 단어가 들어간 공고는 제외합니다.

---

## ☁️ 3. 클라우드(GitHub Actions) 무료 자동 실행 설정법

컴퓨터를 꺼두어도 **매일 아침 한국시간 오전 8시**에 자동으로 돌아가도록 설정되어 있습니다 (`.github/workflows/daily_monitor.yml`).

### Step 1. Gmail 앱 비밀번호 발급 (1분 소요)
1. [Google 계정 보안 설정](https://myaccount.google.com/security) 접속 → **2단계 인증** 활성화 확인
2. 상단 검색창에 **앱 비밀번호(App passwords)** 검색 후 이동
3. 앱 이름에 `지원사업모니터링` 입력 후 생성 → **16자리 영문 비밀번호** 복사

### Step 2. GitHub 저장소 생성 및 파일 업로드
1. [GitHub](https://github.com/)에서 새 저장소(Repository)를 생성합니다 (Private으로 만들어도 무료로 작동합니다).
2. 현재 폴더의 파일들(`monitor.py`, `config.json`, `seen_posts.json`, `requirements.txt`, `.github/workflows/daily_monitor.yml`)을 업로드합니다.

### Step 3. GitHub Secrets(비밀번호 환경변수) 등록
GitHub 저장소 상단 **Settings** → 좌측 메뉴 **Secrets and variables** → **Actions** → **New repository secret** 버튼을 눌러 아래 3가지를 등록합니다:

| Name (이름) | Secret (값) |
| :--- | :--- |
| `SMTP_USER` | 발송에 사용할 본인 Gmail 주소 (예: `myname@gmail.com`) |
| `SMTP_PASS` | Step 1에서 발급받은 16자리 **Gmail 앱 비밀번호** (공백 제외) |
| `RECIPIENT_EMAIL` | 알림을 받을 이메일 주소 (여러 명이면 쉼표 `,`로 구분) |

### Step 4. 즉시 테스트 실행해 보기
1. GitHub 저장소 상단 **Actions** 탭 클릭
2. 좌측 **Daily Support Program Monitor** 클릭
3. 우측 **Run workflow** 버튼 클릭 → 약 30초 후 이메일함에 리포트가 도착합니다!
   * 한 번 발송된 공고의 고유 ID는 `seen_posts.json`에 자동 기록되어 다음 날에는 **새로 올라온 공고만** 발송됩니다.

---

## 💻 4. 내 컴퓨터에서 바로 테스트 실행하는 방법
이메일 발송 없이 현재 어떤 공고가 잡히는지 확인하려면 터미널에서 아래 명령어를 실행한 뒤 생성된 `latest_report.html` 파일을 더블클릭해 브라우저로 열어보세요.

```bash
python monitor.py --dry-run
```

