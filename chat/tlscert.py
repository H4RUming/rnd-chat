"""내부망용 자체 서명 인증서를 만든다:  python -m chat.tlscert --name orin --ip 192.168.0.10

접속에 쓸 이름과 주소를 모두 적는다(여러 번 줄 수 있다). `certs/cert.pem` 과 `certs/key.pem` 이 생기며,
설정 파일의 `[web] tls_cert` / `tls_key` 에 이 경로를 적으면 https 로 열린다.

자체 서명 인증서라 브라우저가 처음 접속할 때 경고를 보여 준다. 경고를 없애려면 접속하는 기기에 `cert.pem` 을
신뢰하는 인증서로 등록하거나, 공인 인증서(예: `tailscale cert`)를 받아 같은 설정에 넣는다.
`openssl` 명령이 필요하다.
"""
from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path
import subprocess
import sys


def subject_alt_names(names: list[str], ips: list[str]) -> str:
    entries = []
    for name in dict.fromkeys(['localhost'] + names):
        if not name or any(mark in name for mark in ', \n'):
            raise ValueError(f'이름 형식이 아닙니다: {name!r}')
        entries.append(f'DNS:{name}')
    for ip in dict.fromkeys(['127.0.0.1'] + ips):
        entries.append(f'IP:{ipaddress.ip_address(ip)}')        # 주소가 아니면 여기서 ValueError
    return ','.join(entries)


def main() -> None:
    parser = argparse.ArgumentParser(description='내부망용 자체 서명 TLS 인증서 만들기')
    parser.add_argument('--name', action='append', default=[], help='접속에 쓸 호스트 이름(여러 번 가능)')
    parser.add_argument('--ip', action='append', default=[], help='접속에 쓸 IP 주소(여러 번 가능)')
    parser.add_argument('--out', type=Path, default=Path('certs'))
    parser.add_argument('--days', type=int, default=825, help='유효 기간(일). 825일을 넘기면 받지 않는 브라우저가 있다')
    parser.add_argument('--force', action='store_true', help='이미 있는 인증서를 덮어쓴다')
    args = parser.parse_args()
    try:
        names = subject_alt_names(args.name, args.ip)
    except ValueError as exc:
        sys.exit(str(exc))
    cert, key = args.out / 'cert.pem', args.out / 'key.pem'
    if (cert.exists() or key.exists()) and not args.force:
        sys.exit(f'{args.out} 에 이미 인증서가 있습니다. 다시 만들려면 --force 를 주세요.')
    args.out.mkdir(parents=True, exist_ok=True)
    args.out.chmod(0o700)
    common = (args.name or ['localhost'])[0]
    command = ['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes', '-sha256',
               '-days', str(args.days), '-subj', f'/CN={common}', '-addext', f'subjectAltName={names}',
               '-addext', 'keyUsage=digitalSignature', '-addext', 'extendedKeyUsage=serverAuth',
               '-keyout', str(key), '-out', str(cert)]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit('openssl 명령이 없습니다.')
    except subprocess.CalledProcessError as exc:
        sys.exit(f'인증서를 만들지 못했습니다: {exc.stderr.strip()[-300:]}')
    key.chmod(0o600)
    print(f'{cert}\n{key}\n포함된 이름: {names}\n설정 파일의 [web] 에 다음을 넣으세요:\n  tls_cert = "{cert}"\n  tls_key = "{key}"')


if __name__ == '__main__':
    main()
