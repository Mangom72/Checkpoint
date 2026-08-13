# 복원하기

스냅샷은 특별한 도구 없이도 열립니다. `tar` 와 `git` 만 있으면 됩니다.

## 스냅샷 구조

```
2026-08-13T18-00-00Z/
├── manifest.json          # 무엇이 언제 백업됐는지, 건너뛴 항목과 경고
├── SHA256SUMS             # 모든 파일 체크섬
├── account.tar.gz         # 프로필 / gist / 스타 / 팔로잉
└── repos/
    ├── mangom72__Checkpoint.tar.gz
    └── mangom72__other-repo.tar.gz
```

레포 아카이브 하나를 풀면:

```
mangom72__Checkpoint/
├── git/
│   ├── repo.bundle        # 전체 Git 히스토리 (모든 브랜치·태그·노트)
│   ├── refs.txt           # 백업 시점의 ref 목록
│   ├── HEAD               # 기본 브랜치
│   ├── wiki.bundle        # 위키 (있는 경우)
│   └── lfs-objects.tar    # LFS 객체 (git_lfs: true 인 경우)
└── api/
    ├── repo.json  issues.json  pull_requests.json  releases.json
    ├── discussions.json  labels.json  milestones.json  tags.json
    ├── branches.json  contributors.json  collaborators.json ...
    └── release_assets/<태그>/<파일>
```

이슈·PR JSON 은 GitHub API 원본 그대로이며, 추가 필드는 `_` 로 시작합니다:
`_comments`, `_events`, `_reviews`, `_review_comments`, `_commits`.

## 1. 무결성 확인

```bash
cd 2026-08-13T18-00-00Z
sha256sum -c SHA256SUMS
```

## 2. 코드 되살리기

```bash
tar xzf repos/mangom72__Checkpoint.tar.gz
git clone mangom72__Checkpoint/git/repo.bundle Checkpoint
cd Checkpoint
git log --all --oneline
```

번들에는 모든 ref 가 들어 있습니다. 원격만 다시 붙이면 그대로 push 할 수 있습니다:

```bash
git remote set-url origin https://github.com/<새-계정>/<새-레포>.git
git push --mirror origin
```

내장 명령으로 한 번에 하려면:

```bash
python -m checkpoint restore repos/mangom72__Checkpoint.tar.gz ./restored
```

## 3. 위키 / LFS

```bash
git clone mangom72__Checkpoint/git/wiki.bundle Checkpoint.wiki
tar xf mangom72__Checkpoint/git/lfs-objects.tar -C Checkpoint/.git/
```

## 4. 이슈·PR 들여다보기

```bash
jq -r '.[] | "#\(.number) [\(.state)] \(.title)"' api/issues.json
jq -r '.[] | select(.merged_at) | "#\(.number) \(.title)"' api/pull_requests.json
jq -r '.[]._comments[]?.body' api/issues.json | head
```

## 다른 계정/서버로 이관

Git 히스토리는 위 2번으로 그대로 옮겨집니다. 이슈·PR 은 GitHub 가 만든 리소스라
API 로 새로 만들어야 합니다 (`api/issues.json` 을 입력으로
`POST /repos/{owner}/{repo}/issues` 를 호출). 작성자·작성 시각은 보존되지 않으므로
보통 원문을 본문에 인용하는 방식으로 옮깁니다.

## manifest.json 읽는 법

```bash
jq '{snapshot, created_at, repo_count, failed, size_human}' manifest.json
jq -r '.repos[] | select(.warnings | length > 0) | "\(.repo): \(.warnings[])"' manifest.json
jq -r '.repos[] | select(.status=="failed") | "\(.repo): \(.error)"' manifest.json
```

`warnings` 에는 권한 부족으로 건너뛴 항목(예: `webhooks: unavailable (403)`)이 남습니다.
백업이 조용히 비어 있는 상황을 여기서 확인할 수 있습니다.
