# Checkpoint

GitHub 계정의 여러 레포를 주기적으로 통째로 긁어서 **Google Drive** 에 자동 백업합니다.

Git 히스토리만이 아니라 GitHub 안에만 존재하는 것들 — 이슈, 코멘트, PR, 리뷰,
릴리스와 첨부파일, Discussions, 라벨, 마일스톤, 위키, gist — 까지 함께 가져옵니다.
계정이 잠기거나 레포가 사라져도 백업 하나로 코드와 맥락을 모두 되살리는 것이 목표입니다.

```
GitHub API + git clone --mirror
        │
        ▼  레포별로 만들자마자 업로드
  Google Drive: Checkpoint/github-backups/2026-08-13T18-00-00Z/
        ├── manifest.json      무엇이 언제 백업됐는지 + 경고
        ├── SHA256SUMS
        ├── account.tar.gz
        └── repos/*.tar.gz
```

만들어진 스냅샷은 **자체 완결형** 입니다. 이 도구가 없어져도 `tar` 와 `git` 만으로 복원됩니다.

---

## 무엇을 백업하나

| 분류 | 내용 |
| --- | --- |
| **Git** | 전체 히스토리를 `repo.bundle` 하나로 (모든 브랜치·태그·노트). 선택적으로 `refs/pull/*`, LFS 객체 |
| **위키** | `wiki.bundle` |
| **이슈** | 본문 + 코멘트 + 이벤트(라벨/할당/클로즈 이력) |
| **PR** | 본문 + 코멘트 + 리뷰 + 리뷰 코멘트 + 커밋 목록 + 머지 상태 |
| **릴리스** | 릴리스 노트 + 첨부파일 바이너리 |
| **Discussions** | 글 + 답글 + 채택 답변 |
| **메타데이터** | 라벨, 마일스톤, 태그, 브랜치, 기여자, 콜라보레이터, 언어, 토픽, README, 워크플로 정의 |
| **계정** | 프로필, gist(본문 포함), 스타, 팔로잉/팔로워, 조직, 공개키 |
| **선택** | stargazers, forks, 웹훅, 워크플로 실행 이력, 배포, Projects v2, 트래픽 통계 |

## 설정 (자동 실행 기준)

`config.yaml` 과 워크플로는 이미 들어 있습니다. 아래 네 가지만 하면 됩니다.

### 1. rclone 으로 Drive 연결 — 로컬에서 한 번

자동화로 대체할 수 없는 유일한 단계입니다. Google OAuth 는 브라우저가 필요합니다.

```bash
brew install rclone          # 또는 curl https://rclone.org/install.sh | sudo bash
rclone config                # name=gdrive, storage=drive
rclone lsd gdrive:           # 확인
base64 -w0 ~/.config/rclone/rclone.conf   # macOS: base64 -i ~/.config/rclone/rclone.conf
```

⚠️ Google Cloud Console 의 **OAuth 동의 화면을 "게시(In production)"** 로 바꾸세요.
`Testing` 이면 refresh token 이 7일 뒤 만료돼 백업이 조용히 멈춥니다.
자세한 절차와 문제 해결은 → [docs/google-drive.md](docs/google-drive.md)

### 2. GitHub 토큰 발급

Actions 기본 `GITHUB_TOKEN` 은 현재 레포만 접근할 수 있어 쓸 수 없습니다.
[Personal access token (classic)](https://github.com/settings/tokens) 을 만들고 스코프를 주세요:

`repo`, `read:org`, `gist`, `read:user`, `read:discussion`, `read:project`

자동 실행 전용이면 **만료 없음** 을 권합니다. 만료되면 알림 없이 멈춥니다.

### 3. 시크릿 두 개 등록

**Settings → Secrets and variables → Actions**

| 이름 | 값 |
| --- | --- |
| `BACKUP_GITHUB_TOKEN` | 2번에서 만든 토큰 |
| `RCLONE_CONFIG_BASE64` | 1번 `base64` 명령의 출력 |

### 4. public / private 정하기 → [아래 참고](#public-으로-둘까-private-으로-둘까)

끝입니다. [`.github/workflows/backup.yml`](.github/workflows/backup.yml) 이
매일 한국시간 새벽 3시에 돕니다.

### 첫 실행은 수동으로

Actions 탭 → **Backup GitHub → Google Drive** → **Run workflow** → `dry_run: true`.
대상 레포 목록이 로그에 뜨는지 확인한 뒤, `dry_run` 없이 한 번 더 돌려 Drive 에
실제로 올라가는지 봅니다. 이걸 건너뛰면 첫 실패를 며칠 뒤에 알게 됩니다.

성공했다면 Drive 에 이렇게 생깁니다:

```
Checkpoint/github-backups/
├── state.json
└── 2026-08-14T18-00-00Z/
    ├── manifest.json     ← failed: 0, warnings: [] 인지 확인
    └── repos/*.tar.gz    ← 레포 수만큼
```

`manifest.json` 의 `warnings` 에 `unavailable (403)` 이 많으면 토큰 스코프가 모자란 것입니다.

## public 으로 둘까, private 으로 둘까

이 레포를 어디에 두느냐로 **Actions 분** 과 **로그 공개 여부** 가 갈립니다.

| | public | private |
| --- | --- | --- |
| Actions 분 | **무제한 무료** | 플랜별 무료 분 소모 |
| 실행 로그 | 공개 | 비공개 |
| 60일 무활동 시 스케줄 | 자동 비활성화 (아래 대비책 있음) | 해당 없음 |

**public 이 기본적으로 유리합니다.** 유일한 문제였던 "로그에 대상 레포 이름이 찍혀
비공개 레포가 드러나는 것" 은 `runtime.redact_repo_names: true` 로 해결되며,
`config.yaml` 에 이미 켜져 있습니다.

```
[3/20] repo#07 done in 12s (4.1 MiB)        ← 켠 상태
[3/20] mangom72/private-thing done in 12s   ← 끈 상태
```

로깅 필터로 처리하기 때문에 `owner/name` 뿐 아니라 아카이브 슬러그(`owner__name`),
rclone 이 찍는 원격 경로, 예외 메시지 안의 이름까지 전부 걸러집니다.
실제 이름은 `manifest.json` 의 `log_aliases` 대응표에 남으므로 나중에 되짚을 수 있습니다
(manifest 는 Drive 로만 가고 로그에는 찍히지 않습니다).

시크릿은 public 이어도 공개되지 않으며 GitHub 가 로그에서 자동 마스킹합니다.

### private 으로 둔다면: 분 예산

| 플랜 | 무료 분/월 | 매일 실행 시 1회 예산 |
| --- | ---: | ---: |
| Free | 2,000 | 66분 |
| Pro / Team | 3,000 | 100분 |
| Enterprise Cloud | 50,000 | 1,666분 |

Linux 러너는 배수 1배이고 (Windows 2배, macOS 10배), 작업 시간은 분 단위로 올림됩니다.
**다른 워크플로와 공유하는 예산** 이므로, 이미 봇을 여럿 돌리고 있다면 남은 여유부터 보세요
(<https://github.com/settings/billing>).

⚠️ 무료 분을 다 쓰면 결제 수단이 없는 계정은 **실행이 차단** 됩니다.
에러가 아니라 그냥 안 돌아가고, 다음 달에 알아서 되살아납니다.
빠듯하면 cron 을 격일(`0 18 */2 * *`)이나 주 1회(`0 18 * * 0`)로 낮추는 것이 가장 간단합니다.

### 스케줄 자동 비활성화 방지

public 레포는 60일간 활동이 없으면 GitHub 가 스케줄 워크플로를 자동으로 끕니다.
백업 실행은 활동으로 치지 않습니다.
[`.github/workflows/keepalive.yml`](.github/workflows/keepalive.yml) 이 매주 확인해
50일 이상 조용했을 때만 커밋 하나를 남겨 시계를 되돌립니다. 평소에는 아무 일도 하지 않습니다.
default branch 에 보호 규칙이 있다면 `github-actions[bot]` 의 푸시를 허용해야 합니다.

## 얼마나 자주 받을까

용량이 아니라 **잃어도 되는 시간** 으로 정하는 문제입니다.

Git 히스토리는 보통 로컬 클론에도 있어서 손실이 제한적이지만,
**이슈·PR 코멘트·Discussions 는 GitHub 에만 있습니다.** 주 1회면 최대 7일치 대화가 사라집니다.
트래픽 통계는 GitHub 가 14일치를 보관하므로 주 1회로도 빠짐없이 이어집니다.

- **매일** — 최대 1일치 손실. private 이면 분 예산을 먼저 확인하세요.
- **주 1회** — 최대 7일치 손실. 분·시간 여유가 넉넉해집니다.

Drive 용량은 이 선택에 사실상 영향을 주지 않습니다 (아래 표 참고).

## 용량과 디스크

### Drive

**스냅샷 1개 크기 × 보존되는 스냅샷 수** 입니다. 증분이 꺼져 있으면 하나하나가 전체 사본입니다.

`config.yaml` 의 보존 규칙(`keep_last 14 / daily 30 / weekly 12 / monthly 24`)은
매일 실행 시 정상 상태에서 **59개(약 2년치)** 를 유지합니다.

| 레포 총합 | 59개 보존 시 |
| --- | --- |
| 500MB | 30GB |
| 1GB | 59GB |
| 5GB | 295GB |

무료 15GB 계정이라면 `config.example.yaml` 의 기본값(13~14개)을 쓰거나 주 1회로 낮추세요.
빠듯할 때 순서대로: cron 을 주 1회로 → `keep_daily: 0` → `keep_monthly` 축소 →
`runtime.incremental: true`.

미리 재보려면 `python -m checkpoint list`, 한 번 돌린 뒤라면
`jq -r '.size_human' backups/*/manifest.json` 또는 `rclone size gdrive:Checkpoint/github-backups`.

### 러너 디스크

GitHub 호스티드 러너는 문서상 **14GB SSD** 입니다 (public 4vCPU/16GB RAM, private 2vCPU/8GB RAM).
증분을 쓰지 않는 한 매 실행이 전체 백업이므로 **이 부담은 회차가 지나도 줄지 않습니다.**

`output.stream_upload: true` (기본으로 켜져 있음) 는 레포 아카이브를 만드는 즉시 업로드하고
로컬에서 지웁니다. 디스크 최대 사용량이 **전체 레포 합계** 에서 **동시에 처리 중인 레포** 로
바뀌어 레포가 몇 개든 일정해집니다. 레포당 6MB 로 실측한 값:

| 레포 수 | 스냅샷 총량 | `stream_upload: false` | `stream_upload: true` |
| ---: | ---: | ---: | ---: |
| 2 | 12MB | 23MB | 24MB |
| 4 | 24MB | 34MB | 23MB |
| 8 | 48MB | 57MB | 24MB |
| 12 | 72MB | 83MB | 23MB |

대략적인 최대치는 **`concurrency` × (가장 큰 레포 × 3)** 입니다 (미러 + 번들 + tar 가 잠깐 공존).
빠듯하면 `runtime.concurrency` 를 낮추거나 `collect.git_pull_refs` 를 끄세요.
워크플로가 실행 전후로 `df -h` 를 찍으니 첫 실행 로그에서 실제 여유를 확인할 수 있습니다.

원격 결과물은 스트리밍 여부와 무관하게 동일합니다. 다만 스냅샷이 조금씩 채워지므로
실행이 중간에 죽으면 미완성 스냅샷이 남습니다. **`manifest.json` 이 있으면 완성된 스냅샷** 입니다.

## 그 밖의 제한

| 항목 | 값 |
| --- | --- |
| 작업(job) 실행 시간 | **6시간** — 초과 시 강제 종료 |
| PAT API 한도 | **5,000 요청/시간** (Actions 기본 토큰의 1,000 제한은 해당 없음) |
| 레포당 예상 API 호출 | 약 `30 + (PR 수 × 2)` |

PR 이 많은 레포에서 한도를 넘기면 클라이언트가 리셋까지 자동 대기하는데,
반복되면 6시간에 닿을 수 있습니다. 로그에 `rate limited, sleeping` 이 보이면
`collect.pull_commits: false` 부터 끄세요 — PR 커밋 목록은 git 번들 안에 이미 들어 있습니다.

## 명령어

| 명령 | 설명 |
| --- | --- |
| `checkpoint run` | 백업 실행. `--dry-run`, `--full`, `--repo owner/name`, `--json` |
| `checkpoint check` | 토큰·스코프·API 잔여량·rclone remote·대상 레포 수 점검 |
| `checkpoint list` | 백업 대상 레포 목록 (private/fork/archived 표시) |
| `checkpoint prune` | 보존 규칙만 적용해 오래된 스냅샷 정리 (`--dry-run`) |
| `checkpoint restore <archive> <dir>` | 스냅샷을 풀고 번들에서 작업 클론 생성 |

`python -m checkpoint ...` 로도 동일하게 실행됩니다.

로컬에서 돌려보려면:

```bash
pip install -r requirements.txt
export GITHUB_TOKEN=ghp_...
python -m checkpoint check
python -m checkpoint run --dry-run
```

## 설정

전체 항목은 [`config.example.yaml`](config.example.yaml) 에 설명과 함께 있습니다.
[`config.yaml`](config.yaml) 이 실제로 쓰이는 설정입니다. 자주 만지는 것들:

```yaml
github:
  include: []                    # 비워두면 내 레포 전부 (새 레포도 자동 포함)
  exclude: ["*/scratch-*"]
  include_orgs: ["my-org"]
  include_forks: false

collect:
  git_pull_refs: true            # 삭제된 PR 브랜치까지. 용량이 늘어나는 주범
  release_asset_max_mb: 0        # 0 = 제한 없음
  workflow_runs_limit: 500

retention:                       # 각 규칙의 합집합을 보존
  keep_last: 14
  keep_daily: 30
  keep_weekly: 12
  keep_monthly: 24

runtime:
  concurrency: 4                 # 러너 디스크가 빠듯하면 낮추세요
  incremental: false             # true = 안 바뀐 레포 건너뜀
  redact_repo_names: true        # 로그에서 레포 이름 가리기
```

**보존 규칙** 은 restic/borg 와 같은 방식입니다. 타임스탬프 형식이 아닌 이름의 파일·폴더는
절대 지우지 않습니다.

**증분 백업** 은 기본 꺼져 있습니다. 켜면 마지막 백업 이후 바뀌지 않은 레포를 건너뛰어
빨라지지만, 그 스냅샷 하나만으로는 복원할 수 없고 `manifest.json` 의 `in_snapshot` 이
가리키는 이전 스냅샷이 함께 남아 있어야 합니다.

## 복원

스냅샷은 특별한 도구 없이 열립니다.

```bash
sha256sum -c SHA256SUMS
tar xzf repos/mangom72__Checkpoint.tar.gz
git clone mangom72__Checkpoint/git/repo.bundle Checkpoint
```

이슈·PR 은 `api/*.json` 에 GitHub API 원본 그대로 들어 있습니다
(`_comments`, `_events`, `_reviews`, `_review_comments`, `_commits` 필드가 추가됩니다).

자세한 절차와 다른 계정으로의 이관은 → [docs/restore.md](docs/restore.md)

## 서버 / 개인 PC 에서 돌리기

```cron
0 3 * * * cd /opt/checkpoint && GITHUB_TOKEN=ghp_... /usr/bin/python3 -m checkpoint run >> /var/log/checkpoint.log 2>&1
```

systemd timer 를 쓴다면 `OnCalendar=*-*-* 03:00:00` 에 `Persistent=true` 를 함께 두세요.
이 경우 `output.keep_local` 과 `stream_upload` 는 디스크 사정에 맞게 조정하면 됩니다.

## 동작 방식과 신뢰성

- **Git 히스토리**: `git clone --mirror` 후 `git bundle create --all`.
  번들 하나에 모든 ref 가 들어가고 `git clone repo.bundle` 로 바로 복원됩니다.
- **API 호출 절약**: 이슈 코멘트·이벤트·리뷰 코멘트는 레포 단위 엔드포인트로 한 번에 받아
  번호별로 묶습니다. 이슈 N개에 N번씩 호출하지 않습니다.
- **레이트 리밋**: 남은 요청 수를 헤더로 추적해 소진 직전에 대기하고,
  secondary rate limit(403/429) 은 `Retry-After` 를 따릅니다. 5xx 는 지수 백오프로 재시도합니다.
- **부분 실패 허용**: 권한이 없어 못 읽는 항목은 건너뛰고 `manifest.json` 의 `warnings` 에
  남깁니다. 레포 하나가 실패해도 나머지는 계속 진행되며, 실패가 있으면 종료 코드 1 입니다.
- **토큰 노출 방지**: git 인증은 argv 대신 `http.extraheader` 환경변수로 전달하고,
  로그·오류 메시지에서 토큰 형태의 문자열을 마스킹합니다.
- **증분 상태 파일**: `state.json` 은 Drive 에도 올라가므로 매번 새로 만들어지는
  CI 러너에서도 증분 모드가 동작합니다.

## 한계

- **이슈·PR 은 JSON 으로만 복원됩니다.** GitHub 서버가 만드는 리소스라 다른 계정으로
  되살리려면 API 로 다시 생성해야 하고, 작성자·작성 시각은 보존되지 않습니다.
- Discussions 의 코멘트는 글당 50개까지 가져오고, 넘치면 `warnings` 에 기록합니다.
- 트래픽 통계는 GitHub 가 14일치만 제공합니다.
- `redact_repo_names` 는 로그에만 적용됩니다. `--json` 으로 manifest 를 출력하면
  실제 이름이 그대로 나옵니다.

## 개발

```bash
pip install -e ".[dev]"
python -m pytest -q      # 42 tests
```

테스트는 실제 GitHub 를 호출하지 않습니다. 로컬에 띄운 가짜 API 서버와 진짜 git 저장소로
`전체 백업 → 아카이브 → 업로드 → 번들에서 클론` 까지 왕복 검증하고,
스트리밍 업로드와 이름 마스킹도 함께 고정합니다.

## 라이선스

MIT
