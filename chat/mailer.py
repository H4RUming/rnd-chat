"""메일 보내기: 브리핑(마크다운)을 메일 본문(HTML + 글)으로 바꿔 SMTP 로 보낸다.

본문에 들어가는 글은 모두 도구가 찾아온 자료에서 온 것이라 믿을 수 없다. 그래서 전부 이스케이프하고, 링크는 출처 목록에 있는
http(s) 주소로만 만든다. 메일 프로그램은 스타일 시트를 잘 버리므로 꾸밈은 태그마다 직접 적는다.
"""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
import html
import re
import smtplib
from typing import Any

from . import docs
from .settings import Mail
from .sources import renumber

_CITE = re.compile(r'\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]')
_BOLD = re.compile(r'\*\*(.+?)\*\*')
_INK, _SOFT, _LINE, _ACCENT = '#18181b', '#55555d', '#e9e9ec', '#5a3df0'
_FONT = "font-family:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic','Noto Sans KR',sans-serif"


def _inline(text: str, links: dict[int, str]) -> str:
    """한 줄 안의 글: 이스케이프하고, 굵은 글과 출처 번호만 살린다."""
    safe = _BOLD.sub(r'<strong>\1</strong>', html.escape(text, quote=False))

    def cite(match: re.Match) -> str:
        marks = []
        for part in match.group(1).split(','):
            number = int(part)
            url = links.get(number)
            marks.append(f'<a href="{html.escape(url)}" style="color:{_ACCENT};text-decoration:none">{number}</a>' if url else str(number))
        return f'<sup style="color:{_SOFT};font-size:11px">[{", ".join(marks)}]</sup>'
    return _CITE.sub(cite, safe).replace('\n', '<br>')


def render(markdown: str, sources: list[dict[str, Any]], title: str, link: str = '') -> tuple[str, str]:
    """(HTML 본문, 글 본문). 답의 [번호] 는 처음 나온 순서로 다시 매기고 끝에 출처를 붙인다."""
    content, cited = renumber(markdown, sources)
    links = {number: source['url'] for number, source in cited if str(source.get('url', '')).startswith(('http://', 'https://'))}
    parts = []
    for block in docs.markdown_blocks(content):
        kind = block['type']
        if kind == 'heading':
            parts.append(f'<h3 style="margin:26px 0 8px;font-size:16px;color:{_INK}">{_inline(block["text"], links)}</h3>')
        elif kind == 'table':
            rows = []
            for number, row in enumerate(block['rows']):
                tag, weight = ('th', '600') if number < block.get('header_rows', 1) else ('td', '400')
                rows.append('<tr>' + ''.join(
                    f'<{tag} style="border:1px solid {_LINE};padding:7px 9px;text-align:left;vertical-align:top;font-weight:{weight};font-size:13px">'
                    f'{_inline(str(cell), links)}</{tag}>' for cell in row) + '</tr>')
            parts.append(f'<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;margin:8px 0 14px">{"".join(rows)}</table>')
        elif kind in ('bullets', 'numbered'):
            tag = 'ol' if kind == 'numbered' else 'ul'
            parts.append(f'<{tag} style="margin:6px 0 12px;padding-left:20px">' + ''.join(f'<li style="margin:3px 0">{_inline(item, links)}</li>' for item in block['items']) + f'</{tag}>')
        else:
            parts.append(f'<p style="margin:8px 0;line-height:1.65">{_inline(block["text"], links)}</p>')
    if cited:
        parts.append(f'<p style="margin:26px 0 6px;font-size:12px;color:{_SOFT}">출처</p><ol style="margin:0;padding-left:20px;font-size:12px;color:{_SOFT}">' + ''.join(
            f'<li style="margin:2px 0"><a href="{html.escape(source["url"])}" style="color:{_SOFT}">{html.escape(source["title"])}</a></li>' if number in links
            else f'<li style="margin:2px 0">{html.escape(source["title"])}</li>' for number, source in cited) + '</ol>')
    if link:
        parts.append(f'<p style="margin:28px 0 0"><a href="{html.escape(link)}" style="display:inline-block;padding:10px 18px;border-radius:20px;'
                     f'background:{_ACCENT};color:#ffffff;text-decoration:none;font-size:14px">이어서 묻기</a></p>')
    page = (f'<div style="{_FONT};color:{_INK};font-size:14px;max-width:760px;margin:0 auto;padding:8px 4px">'
            f'<h2 style="margin:0 0 14px;font-size:20px">{html.escape(title)}</h2>{"".join(parts)}</div>')
    plain = content + ('\n\n출처\n' + '\n'.join(f'[{number}] {source["title"]} — {source["url"]}' for number, source in cited) if cited else '') + (f'\n\n{link}' if link else '')
    return page, plain


def notice(title: str, rows: list[tuple[str, str]], link: str = '', link_label: str = '',
           guide: list[tuple[str, list[str], list[str]]] | None = None) -> tuple[str, str]:
    """짧은 알림 메일(가입 신청, 승인). rows: (이름표, 값). 값은 사람이 넣은 글이라 모두 이스케이프한다.
    guide: 그 아래에 붙이는 안내 — (제목, 차례대로 할 일들, 그대로 쓸 명령들)의 목록. 명령은 고정폭 글로 한 줄씩 싣는다."""
    lines = ''.join(f'<tr><td style="padding:5px 18px 5px 0;color:{_SOFT};white-space:nowrap">{html.escape(label)}</td>'
                    f'<td style="padding:5px 0">{html.escape(value)}</td></tr>' for label, value in rows)
    button = (f'<p style="margin:24px 0 0"><a href="{html.escape(link)}" style="display:inline-block;padding:10px 18px;border-radius:20px;'
              f'background:{_ACCENT};color:#ffffff;text-decoration:none;font-size:14px">{html.escape(link_label or link)}</a></p>') if link else ''
    help_page, help_plain = '', ''
    for heading, steps, commands in guide or []:
        help_page += f'<h3 style="margin:26px 0 6px;font-size:14px;color:{_INK}">{html.escape(heading)}</h3>'
        if steps:
            help_page += '<ol style="margin:0;padding-left:20px;line-height:1.7">' + ''.join(f'<li>{html.escape(step)}</li>' for step in steps) + '</ol>'
        for command in commands:
            help_page += (f'<pre style="margin:8px 0 0;padding:10px 12px;border:1px solid {_LINE};border-radius:8px;background:#f6f6f8;'
                          f'font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;white-space:pre-wrap;word-break:break-all">{html.escape(command)}</pre>')
        help_plain += f'\n\n{heading}\n' + '\n'.join([*(f'{number}. {step}' for number, step in enumerate(steps, 1)), *(f'  {command}' for command in commands)])
    page = (f'<div style="{_FONT};color:{_INK};font-size:14px;max-width:560px;margin:0 auto;padding:8px 4px">'
            f'<h2 style="margin:0 0 14px;font-size:20px">{html.escape(title)}</h2>'
            f'<table cellspacing="0" cellpadding="0" style="border-collapse:collapse">{lines}</table>{button}{help_page}</div>')
    plain = title + '\n\n' + '\n'.join(f'{label}: {value}' for label, value in rows) + (f'\n\n{link}' if link else '') + help_plain
    return page, plain


def message(mail: Mail, to: str, subject: str, page: str, plain: str) -> EmailMessage:
    letter = EmailMessage()
    letter['From'] = formataddr((mail.sender_name, mail.username)) if mail.sender_name else mail.username
    letter['To'] = to
    letter['Subject'] = ' '.join(subject.split())[:200]
    letter['Date'] = formatdate(localtime=True)          # 날짜가 없는 메일은 스팸으로 의심받는다
    letter['Message-ID'] = make_msgid(domain=mail.username.rpartition('@')[2] or None)
    letter.set_content(plain)
    letter.add_alternative(page, subtype='html')
    return letter


def send(mail: Mail, to: str, subject: str, page: str, plain: str, timeout: float = 30.0) -> None:
    """한 통 보낸다. 실패하면 smtplib 의 예외가 그대로 나온다(부르는 쪽이 기록한다)."""
    if not (mail.username and mail.password):
        raise ValueError('보내는 계정이 설정되지 않았습니다([mail] username·password)')
    if not re.fullmatch(r'[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+', to or ''):
        raise ValueError('받는 주소가 메일 주소가 아닙니다')
    with smtplib.SMTP(mail.host, mail.port, timeout=timeout) as server:
        server.starttls()
        server.login(mail.username, mail.password)
        server.send_message(message(mail, to, subject, page, plain))
