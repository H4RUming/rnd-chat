"""가입 신청을 터미널에서 다룬다(화면의 "계정" 메뉴와 같은 일).

    python -m chat.accounts list              # 기다리는 신청과 등록된 계정
    python -m chat.accounts approve 아이디     # 신청을 승인해 계정으로 등록한다(신청한 사람에게 승인 메일이 간다)
    python -m chat.accounts reject 아이디      # 신청을 거절한다(신청이 지워지고, 알리지 않는다)

서버를 다시 띄울 필요는 없다: 승인된 계정은 그 사람이 처음 로그인할 때 서버가 알아본다.
등록된 계정을 지우는 일(대화·문서까지 함께 지운다)은 돌고 있는 서버가 해야 하므로 화면의 "계정" 메뉴에서 한다.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

from . import mailer, storage
from .auth import Users
from .settings import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m chat.accounts', description='가입 신청 승인·거절')
    parser.add_argument('--config', type=Path, default=None)
    parser.add_argument('--no-mail', action='store_true', help='승인 메일을 보내지 않는다')
    parser.add_argument('action', choices=['list', 'approve', 'reject'])
    parser.add_argument('name', nargs='?', default='')
    args = parser.parse_args(argv)
    try:
        settings = Settings.load(args.config)
        users = Users(list(settings.users))
    except ValueError as exc:
        print(f'설정 오류: {exc}', file=sys.stderr)
        return 2
    database = storage.Database(Path(settings.data_dir) / 'chat.sqlite3')
    accounts = database.accounts()
    if args.action == 'list':
        for title, status in (('기다리는 신청', 'pending'), ('등록된 계정', 'active')):
            rows = [account for account in accounts if account['status'] == status]
            print(f'{title} {len(rows)}건')
            for account in rows:
                print(f'  {account["name"]}  {account["email"]}  {time.strftime("%Y-%m-%d %H:%M", time.localtime(account["created"]))}')
        return 0
    account = next((entry for entry in accounts if entry['name'] == args.name), None)
    if account is None or account['status'] != 'pending':
        waiting = ', '.join(entry['name'] for entry in accounts if entry['status'] == 'pending') or '없음'
        print(f'기다리는 신청 가운데 "{args.name}" 이(가) 없습니다. 기다리는 신청: {waiting}', file=sys.stderr)
        return 1
    if args.action == 'reject':
        database.delete_account(account['name'])
        print(f'{account["name"]} 의 신청을 거절했습니다(신청을 지웠고, 신청한 사람에게 알리지 않습니다).')
        return 0
    if users.taken(account['name']):
        print(f'설정 파일의 계정과 이름이 겹쳐 등록할 수 없습니다: {account["name"]}. 거절한 뒤 다른 아이디로 신청받으세요.', file=sys.stderr)
        return 1
    database.activate_account(account['name'], time.time())
    print(f'{account["name"]} 을(를) 등록했습니다. 서버를 다시 띄울 필요 없이 바로 로그인할 수 있습니다.')
    if args.no_mail or not settings.mail.username:
        print('승인 메일은 보내지 않았습니다.' + ('' if args.no_mail else ' ([mail] 설정이 없습니다)'))
        return 0
    try:
        page, plain = mailer.notice('가입이 승인되었습니다', [('아이디', account['name'])], settings.briefing.app_url, '로그인하러 가기')
        mailer.send(settings.mail, account['email'], '가입이 승인되었습니다', page, plain)
        print(f'승인 메일을 보냈습니다: {account["email"]}')
    except Exception as exc:
        print(f'계정은 등록했지만 승인 메일은 보내지 못했습니다: {type(exc).__name__}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
