# Google Drive 연결하기 (rclone)

Checkpoint 는 업로드를 [rclone](https://rclone.org/) 에 맡깁니다.
한 번만 설정해두면 GitHub Actions·서버·개인 PC 어디서든 같은 설정을 씁니다.

---

## 1. rclone 설치

```bash
# macOS
brew install rclone
# Linux
curl https://rclone.org/install.sh | sudo bash
# Windows
winget install Rclone.Rclone
```

## 2. 내 Google OAuth 클라이언트 만들기 (권장)

건너뛰고 rclone 내장 클라이언트를 써도 되지만, 공용이라 속도 제한이 자주 걸립니다.

1. [Google Cloud Console](https://console.cloud.google.com/) → 프로젝트 생성
2. **API 및 서비스 → 라이브러리** → `Google Drive API` 사용 설정
3. **OAuth 동의 화면** → External → 앱 이름 입력 → 본인 계정을 테스트 사용자로 추가
4. ⚠️ **동의 화면을 반드시 "게시(In production)" 상태로 전환하세요.**
   `Testing` 상태로 두면 **refresh token 이 7일 만에 만료**되어 자동 백업이 조용히 멈춥니다.
5. **사용자 인증 정보 → OAuth 클라이언트 ID → 데스크톱 앱** → client ID / secret 확보

## 3. remote 설정

```bash
rclone config
```

| 질문 | 입력 |
| --- | --- |
| `n/s/q` | `n` (새 remote) |
| `name` | `gdrive` ← `config.yaml` 의 `storage.rclone.remote` 와 같아야 합니다 |
| `Storage` | `drive` |
| `client_id` / `client_secret` | 2번에서 만든 값 (없으면 그냥 Enter) |
| `scope` | `1` (`drive`) — 안전하게 가려면 `3` (`drive.file`, rclone 이 만든 파일만 접근) |
| `root_folder_id` | Enter (특정 폴더에만 넣고 싶으면 그 폴더 ID) |
| `service_account_file` | Enter |
| `Edit advanced config?` | `n` |
| `Use web browser to automatically authenticate?` | 브라우저 있으면 `y` |

**브라우저가 없는 서버라면**: 데스크톱에서 `rclone authorize "drive"` 를 실행하고
출력된 토큰 JSON 을 서버 쪽 질문에 붙여넣습니다.

확인:

```bash
rclone lsd gdrive:
rclone mkdir gdrive:Checkpoint/github-backups
```

## 4. GitHub Actions 에 넣기

설정 파일을 통째로 base64 로 만들어 시크릿에 넣습니다.

```bash
base64 -w0 ~/.config/rclone/rclone.conf   # macOS 는 -w0 대신 base64 -i ~/.config/rclone/rclone.conf
```

레포 → **Settings → Secrets and variables → Actions → New repository secret**

| 이름 | 값 |
| --- | --- |
| `RCLONE_CONFIG_BASE64` | 위 명령 출력 |
| `BACKUP_GITHUB_TOKEN` | 아래 5번에서 만든 토큰 |

> `rclone.conf` 에는 Drive refresh token 이 들어 있습니다. 절대 커밋하지 마세요
> (`.gitignore` 에 이미 등록돼 있습니다).

## 5. GitHub 토큰

Actions 기본 `GITHUB_TOKEN` 은 **현재 레포만** 접근할 수 있으므로 쓸 수 없습니다.
[Personal access token](https://github.com/settings/tokens) 을 따로 만드세요.

**Classic (계정 전체를 한 번에 백업하려면 이쪽이 편합니다)**

- `repo` — 비공개 레포 포함 전체 읽기
- `read:org` — 조직 레포 목록
- `gist` — gist 백업
- `read:user` — 프로필 / 팔로잉 / 스타
- `read:discussion` — Discussions
- (선택) `read:project` — Projects v2

**Fine-grained** 을 쓴다면 대상 레포를 모두 선택하고 Repository permissions 에서
Metadata / Contents / Issues / Pull requests / Discussions / Actions / Administration(웹훅용) 을
**Read-only** 로 주세요. 새 레포가 생길 때마다 토큰에 추가해야 하는 점만 유의하세요.

## 6. 대안: 서비스 계정 (공유 드라이브 전용)

개인 Drive 는 서비스 계정에 저장 용량이 없어서 업로드가 실패합니다.
**공유 드라이브(Shared Drive)** 를 쓰는 경우에만 아래가 성립합니다.

1. 서비스 계정 생성 → JSON 키 다운로드
2. 공유 드라이브에 서비스 계정 이메일을 **콘텐츠 관리자**로 초대
3. `rclone config` 에서 `service_account_file` 에 JSON 경로, `team_drive` 에 공유 드라이브 ID 입력

## 문제 해결

| 증상 | 원인/해결 |
| --- | --- |
| `couldn't fetch token: invalid_grant` | 동의 화면이 `Testing` 상태 → 게시로 전환 후 `rclone config reconnect gdrive:` |
| `storageQuotaExceeded` | 개인 Drive + 서비스 계정 조합. 6번 참고 |
| `rclone remote 'gdrive:' is not configured` | `config.yaml` 의 remote 이름과 `rclone listremotes` 결과가 다름 |
| 업로드가 매우 느림 | rclone 내장 client ID 사용 중. 2번대로 본인 것 발급 |
