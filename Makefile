.PHONY: help install dev test check list run dry prune clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## 의존성 설치
	pip install -r requirements.txt

dev:  ## 개발용 설치 (테스트 포함)
	pip install -e ".[dev]"

test:  ## 테스트 실행
	python -m pytest -q

check:  ## 토큰 / rclone / 대상 레포 점검
	python -m checkpoint check

list:  ## 백업 대상 레포 목록
	python -m checkpoint list

dry:  ## 실제 백업 없이 계획만 출력
	python -m checkpoint run --dry-run

run:  ## 백업 실행
	python -m checkpoint run

prune:  ## 보존 규칙에 따라 오래된 스냅샷 정리
	python -m checkpoint prune --dry-run

clean:
	rm -rf work __pycache__ .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
