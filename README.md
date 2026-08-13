# Checkpoint

GitHub 계정의 여러 레포를 주기적으로 통째로 긁어서 **Google Drive** 에 자동 백업합니다.

Git 히스토리만이 아니라 GitHub 안에만 존재하는 것들 — 이슈, 코멘트, PR, 리뷰,
릴리스와 첨부파일, Discussions, 라벨, 마일스톤, 위키, gist — 까지 함께 가져옵니다.
계정이 잠기거나 레포가 사라져도 백업 하나로 코드와 맥락을 모두 되살릴 수 있는 상태가 목표입니다.

```
GitHub API + git clone --mirror
        │
        ▼
  스냅샷 디렉터리 (레포별 tar.gz + manifest.json + SHA256SUMS)
        │  rclone
        ▼
  Google Drive: Checkpoint/github-backups/2026-08-13T18-00-00Z/
```

---

## 무엇을 백업하나

| 분류 | 내용 |
| --- | --- |
| **Git** | 전체 히스토리를 `repo.bundle` 하나로 (모든 브랜치·태그·노트). 선택적으로 `refs/pull/*`, LFS 객체 |
| **위키** | `wiki.bundle` |
| **이슈** | 본문 + 코멘트 + 이벤트(라벨/할당/클로즈 이력) |
| **PR** | 본문 + 코멘트 + 리뷰 + 리뷰 코멘트 + 커밋 목록 + 머지 상태 |
| **릴리스** | 릴리스 노트 + 첨부파일 바이너리 |
| **Discussions** | 글 + 답글 + 채택 답변 (GraphQL) |
| **메타데이터** | 라벨, 마일스톤, 태그, 브랜치, 기여자, 콜라보레이터, 언어, 토픽, README, 워크플로 정의 |
| **계정** | 프로필, gist(본문 포함), 스타, 팔로잉/팔로워, 조직, 공개키 |
| **선택** | stargazers, forks, 웹훅, 워크플로 실행 이력, 배포, Projects v2, 트래픽 통계 |

백업된 스냅샷은 **자체 완결형** 입니다. 이 도구가 없어져도 `tar` 와 `git` 만으로 복원됩니다
→ [docs/restore.md](docs/restore.md)

## 빠른 시작

```bash
git clone https://github.com/Mangom72/Checkpoint.git && cd Checkpoint
pip install -r requirements.txt

cp config.example.yaml config.yaml     # 대상 레포와 수집 항목 조정
export GITHUB_TOKEN=ghp_...            # 토큰 발급: docs/google-drive.md 5번

python -m checkpoint check             # 토큰 / rclone / 대상 레포 점검
python -m checkpoint run --dry-run     # 무엇이 백업될지만 확인
python -m checkpoint run               # 실제 백업
```

Google Drive 연결(rclone)은 → [docs/google-drive.md](docs/google-drive.md)

먼저 Drive 없이 동작만 보고 싶다면 `config.yaml` 에서:

```yaml
storage:
  backend: local
  local: { path: ./remote-backups }
```

## 자동 실행

### 방법 A. GitHub Actions (서버 불필요, 권장)

[`.github/workflows/backup.yml`](.github/workflows/backup.yml) 이 매일 한국시간 새벽 3시에 돕니다.
레포 시크릿 두 개만 넣으면 끝입니다.

| 시크릿 | 내용 |
| --- | --- |
| `BACKUP_GITHUB_TOKEN` | 백업할 레포들을 읽을 수 있는 PAT (Actions 기본 `GITHUB_TOKEN` 은 현재 레포만 접근 가능해서 못 씁니다) |
| `RCLONE_CONFIG_BASE64` | `base64 -w0 ~/.config/rclone/rclone.conf` 결과 |

Actions 탭에서 **Run workflow** 로 수동 실행도 되고, `dry_run` / `full` / 특정 레포만 지정할 수 있습니다.

주기를 바꾸려면 워크플로의 `cron` 을 수정하세요 (UTC 기준, 한국시간 = UTC+9):

```yaml
- cron: "0 18 * * *"   # 매일 03:00 KST
- cron: "0 18 * * 0"   # 매주 일요일 03:00 KST
```

> 무료 러너 디스크는 약 14GB 입니다. 레포 총량이 그보다 크면 `--repo` 로 나눠 돌리거나
> `runtime.incremental: true` 또는 self-hosted 러너를 쓰세요.

#### 이 레포를 public 으로 둘 때

Actions 사용량이 무료라는 장점이 있지만, **실행 로그도 공개됩니다.**
백업 진행 로그에는 대상 레포 이름이 한 줄씩 찍히므로
(`[3/20] mangom72/private-thing done in 12s`) **비공개 레포의 존재와 이름이 노출됩니다.**
백업 내용 자체는 Drive 로만 가고 로그에 남지 않으며, 시크릿은 GitHub 가 자동으로 마스킹합니다.

- 비공개 레포가 하나라도 있다면 → 이 레포도 **private 으로 두세요.**
  private 레포의 Actions 는 무료 2,000분/월이며, 하루 한 번 백업은 보통 그 안에 들어옵니다.
- 그래도 public 으로 두겠다면 → `config.yaml` 에 `runtime: { log_level: WARNING }` 을 넣어
  레포별 진행 로그를 끄세요. 다만 오류 메시지에는 여전히 이름이 남을 수 있습니다.

#### 스케줄 자동 비활성화 방지

public 레포는 **60일간 활동이 없으면 GitHub 가 스케줄 워크플로를 자동으로 끕니다.**
백업 실행은 활동으로 치지 않습니다.
[`.github/workflows/keepalive.yml`](.github/workflows/keepalive.yml) 이 매주 확인해서
50일 이상 조용했을 때만 커밋 하나를 남겨 시계를 되돌립니다. 평소에는 아무 일도 하지 않습니다.
default branch 에 브랜치 보호 규칙이 걸려 있으면 `github-actions[bot]` 의 푸시를 허용해야 합니다.

### 방법 B. 서버 / 개인 PC 의 cron

```cron
0 3 * * * cd /opt/checkpoint && GITHUB_TOKEN=ghp_... /usr/bin/python3 -m checkpoint run >> /var/log/checkpoint.log 2>&1
```

systemd timer 를 쓴다면:

```ini
# /etc/systemd/system/checkpoint.service
[Service]
Type=oneshot
WorkingDirectory=/opt/checkpoint
Environment=GITHUB_TOKEN=ghp_...
ExecStart=/usr/bin/python3 -m checkpoint run

# /etc/systemd/system/checkpoint.timer
[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
[Install]
WantedBy=timers.target
```

## 명령어

| 명령 | 설명 |
| --- | --- |
| `checkpoint run` | 백업 실행. `--dry-run`, `--full`, `--repo owner/name`, `--json` |
| `checkpoint check` | 토큰·스코프·API 잔여량·rclone remote·대상 레포 수 점검 |
| `checkpoint list` | 백업 대상 레포 목록 (private/fork/archived 표시) |
| `checkpoint prune` | 보존 규칙만 적용해 오래된 스냅샷 정리 (`--dry-run`) |
| `checkpoint restore <archive> <dir>` | 스냅샷 아카이브를 풀고 번들에서 작업 클론 생성 |

## Drive 용량 계산

Google 계정의 무료 15GB 는 Gmail·Drive·Photos 가 함께 씁니다.
Checkpoint 가 쓰는 양은 **스냅샷 1개 크기 × 보존되는 스냅샷 수** 입니다
(증분이 꺼져 있으면 스냅샷 하나하나가 전체 사본입니다).

`config.example.yaml` 의 기본값(매일 실행 + `keep_last 7 / daily 7 / weekly 4 / monthly 6`)은
정상 상태에서 **스냅샷 13~14개** 를 유지합니다. 주 1회 + `keep_last 4 / weekly 4 / monthly 6` 이면 **8개** 입니다.

용량이 넉넉하다면(Google One 등) 반대로 늘리는 편이 낫습니다.
이 레포의 [`config.yaml`](config.yaml) 은 `14 / 30 / 12 / 24` 로 **59개(약 2년치)** 를 보존하고,
기본적으로 꺼져 있는 수집 항목까지 전부 켜둔 설정입니다.

| 레포 총합 (압축 후) | 기본 설정 (13개) | 주 1회 설정 (8개) |
| --- | --- | --- |
| 100MB | 1.3GB | 0.8GB |
| 500MB | 6.5GB | 4GB |
| 1GB | 13GB | 8GB |
| 3GB | 39GB ← 무료 15GB 초과 | 24GB |

내 총량을 미리 재려면:

```bash
python -m checkpoint list                  # 레포별 크기와 합계
```

한 번 돌린 뒤 실측하려면:

```bash
jq -r '.size_human' backups/*/manifest.json    # 스냅샷 1개 실제 크기
rclone size gdrive:Checkpoint/github-backups   # Drive 에서 실제 사용 중인 양
```

현재 Drive 사용량은 <https://drive.google.com/settings/storage> 에서 확인합니다.
용량이 빠듯하면 순서대로: cron 을 주 1회로 → `keep_daily: 0` → `keep_monthly` 축소 →
`runtime.incremental: true`.

### 러너 디스크와 `stream_upload`

Drive 용량이 충분해지면 다음 병목은 **러너 디스크(약 14GB)** 입니다.
증분 백업을 쓰지 않는 한 매 실행이 전체 백업이므로, 이 부담은 회차가 지나도 줄지 않습니다.

`output.stream_upload: true` 를 켜면 레포 아카이브를 만드는 즉시 업로드하고 로컬에서 지웁니다.
디스크 최대 사용량이 **전체 레포 합계** 에서 **동시에 처리 중인 레포** 로 바뀌어,
레포가 몇 개든 일정해집니다. 레포당 6MB 짜리로 실측한 값:

| 레포 수 | 스냅샷 총량 | `stream_upload: false` | `stream_upload: true` |
| ---: | ---: | ---: | ---: |
| 2 | 12MB | 23MB | 24MB |
| 4 | 24MB | 34MB | 23MB |
| 8 | 48MB | 57MB | 24MB |
| 12 | 72MB | 83MB | 23MB |

대략적인 최대치는 **`concurrency` × (가장 큰 레포 × 3)** 입니다
(미러 + 번들 + tar 가 잠깐 공존). 디스크가 빠듯하면 `runtime.concurrency` 를 낮추는 것이
가장 직접적인 대응입니다.

원격에 남는 결과물은 켜든 끄든 **완전히 동일** 합니다 (테스트로 검증). 다만 스트리밍 중에는
스냅샷이 조금씩 채워지므로, 실행이 중간에 죽으면 미완성 스냅샷이 남습니다.
**`manifest.json` 이 있으면 완성된 스냅샷** 이라고 보면 됩니다.

> Actions 워크플로는 이 옵션을 실행 시 자동으로 켭니다.

`python -m checkpoint ...` 로도 동일하게 실행됩니다.

## 설정

전체 항목과 설명은 [`config.example.yaml`](config.example.yaml) 에 있습니다. 자주 만지는 것들:

```yaml
github:
  include: ["mangom72/*"]        # 비워두면 접근 가능한 내 레포 전부
  exclude: ["*/scratch-*"]
  include_orgs: ["my-org"]
  include_forks: false

collect:
  git_pull_refs: false           # PR 브랜치까지 (용량 증가)
  release_asset_max_mb: 200      # 큰 첨부파일 건너뛰기
  workflow_runs: false           # Actions 실행 이력은 기본 제외

retention:                       # 각 규칙의 합집합을 보존
  keep_last: 7
  keep_daily: 7
  keep_weekly: 4
  keep_monthly: 6

runtime:
  concurrency: 4                 # 동시에 처리할 레포 수
  incremental: false             # true = 안 바뀐 레포 건너뜀 (아래 참고)
```

**보존 규칙** 은 restic/borg 방식과 같습니다. 위 설정이면 최근 7개 + 최근 7일 + 최근 4주 +
최근 6개월의 마지막 스냅샷이 남고 나머지는 로컬·Drive 양쪽에서 지워집니다.
타임스탬프 형식이 아닌 이름의 파일·폴더는 절대 지우지 않습니다.

**증분 백업** 은 기본 꺼져 있습니다. 켜면 마지막 백업 이후 `pushed_at`/`updated_at` 이
그대로인 레포를 건너뛰어 빨라지지만, 그 스냅샷 하나만으로는 복원할 수 없고
`manifest.json` 의 `in_snapshot` 이 가리키는 이전 스냅샷이 함께 남아 있어야 합니다.
스냅샷 하나로 전부 복원되는 편이 안전하므로 기본값은 `false` 입니다.

## 동작 방식과 신뢰성

- **Git 히스토리**: `git clone --mirror` 후 `git bundle create --all`.
  번들 하나에 모든 ref 가 들어가고, `git clone repo.bundle` 로 바로 복원됩니다.
- **API 호출 절약**: 이슈 코멘트·이벤트·리뷰 코멘트는 레포 단위 엔드포인트로 한 번에 받아
  번호별로 묶습니다. 이슈 N개에 N번씩 호출하지 않습니다.
- **레이트 리밋**: 남은 요청 수를 헤더로 추적해 소진 직전에 대기하고,
  secondary rate limit(403/429) 은 `Retry-After` 를 따릅니다. 5xx 는 지수 백오프로 재시도합니다.
- **부분 실패 허용**: 권한이 없어 못 읽는 항목(예: 웹훅)은 건너뛰고 `manifest.json` 의
  `warnings` 에 남깁니다. 레포 하나가 실패해도 나머지는 계속 진행되며,
  실패가 있으면 종료 코드 1 을 반환합니다.
- **토큰 노출 방지**: git 인증은 argv 대신 `http.extraheader` 환경변수로 전달하고,
  로그·오류 메시지에서 토큰 형태의 문자열을 마스킹합니다.
- **증분 상태 파일**: `state.json` 은 Drive 에도 올라가므로, 매번 새로 만들어지는
  CI 러너에서도 증분 모드가 동작합니다.

## 한계

- **이슈·PR 은 JSON 으로만 복원됩니다.** GitHub 서버가 만드는 리소스라 다른 계정으로
  그대로 되살리려면 API 로 다시 생성해야 합니다 (작성자·시각은 보존되지 않음).
- Discussions 의 코멘트는 글당 50개까지 가져오고, 넘치면 `warnings` 에 기록합니다.
- 트래픽 통계는 GitHub 가 14일치만 제공합니다. 장기 보관하려면 자주 돌려야 합니다.
- Projects v2 는 토큰에 `read:project` 가 필요해 기본 꺼져 있습니다.

## 개발

```bash
pip install -e ".[dev]"
python -m pytest -q      # 31 tests
```

테스트는 실제 GitHub 를 호출하지 않습니다. 로컬에 띄운 가짜 API 서버와 진짜 git 저장소로
`전체 백업 → 아카이브 → 업로드 → 번들에서 클론` 까지 왕복 검증합니다.

## 라이선스

MIT
