"""설정 파일(config.toml) 읽기.

API 키처럼 저장소에 올리면 안 되는 값은 `config.toml` 에 둔다(예시는 `config.example.toml`, 실제 파일은 gitignore).
찾는 순서: 인자로 준 경로 → 환경변수 `RND_CONFIG` → 저장소 루트의 `config.toml`. 파일이 없으면 빈 설정이다.
"""
from __future__ import annotations

import os
from pathlib import Path
import tomllib
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parent / 'config.toml'


def config_path(path: str | os.PathLike | None = None) -> Path:
    return Path(path or os.environ.get('RND_CONFIG') or DEFAULT_PATH).expanduser()


def load(path: str | os.PathLike | None = None) -> dict[str, Any]:
    """설정 전체. 파일이 없으면 {}. 문법이 틀린 파일은 어디가 틀렸는지 알 수 있게 ValueError 로 알린다."""
    target = config_path(path)
    try:
        with open(target, 'rb') as stream:
            return tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f'설정 파일 문법 오류({target}): {exc}') from None


def section(name: str, path: str | os.PathLike | None = None) -> dict[str, Any]:
    value = load(path).get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f'설정 파일의 [{name}] 은 표(table)여야 합니다')
    return value


def modified_at(path: str | os.PathLike | None = None) -> tuple[int, int]:
    """설정 파일이 바뀌었는지 알아보는 값(수정 시각과 크기, 없으면 (0, 0))."""
    try:
        info = config_path(path).stat()
    except OSError:
        return (0, 0)
    return (info.st_mtime_ns, info.st_size)


def permission_warning(path: str | os.PathLike | None = None) -> str:
    """다른 계정이 읽을 수 있는 설정 파일이면 경고 문구를 돌려준다."""
    try:
        mode = config_path(path).stat().st_mode
    except OSError:
        return ''
    return '' if not mode & 0o077 else f'{config_path(path)} 을 다른 계정이 읽을 수 있습니다. chmod 600 을 권합니다.'
