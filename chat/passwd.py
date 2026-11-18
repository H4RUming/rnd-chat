"""설정 파일에 적을 비밀번호 해시를 만든다:  python -m chat.passwd

비밀번호를 두 번 물어 보고(화면에 보이지 않음) `password_hash = "…"` 한 줄을 내놓는다. 이 줄을 config.toml 의
해당 `[[users]]` 항목에 붙여 넣는다.
"""
from __future__ import annotations

import getpass
import sys

from .auth import MAX_PASSWORD, hash_password


def main() -> None:
    first = getpass.getpass('비밀번호: ')
    if len(first) < 10:
        sys.exit('10자 이상으로 정하세요.')
    if len(first) > MAX_PASSWORD:
        sys.exit(f'{MAX_PASSWORD}자 이하로 정하세요.')
    if getpass.getpass('한 번 더: ') != first:
        sys.exit('두 입력이 다릅니다.')
    print(f'password_hash = "{hash_password(first)}"')


if __name__ == '__main__':
    main()
