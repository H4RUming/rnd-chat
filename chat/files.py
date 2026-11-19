"""사용자가 올린 파일과 모델이 만든 파일의 보관, 미리보기용 PDF 만들기, PDF 읽기(쪽 그림 + 글).

- 파일은 계정별 폴더에 무작위 이름으로 둔다. 사용자가 준 파일 이름은 표시에만 쓰고 경로에는 쓰지 않는다.
- 남의 파일은 id 를 알아도 열 수 없다(계정 폴더가 다르다).
- 올린 문서는 원본 그대로 둔다(고칠 때 원본을 고친다). 화면과 모델이 보는 쪽 그림은 원본에서 만든 PDF 로 그린다.
- PDF 는 poppler(pdfinfo, pdftotext, pdftoppm)를 별도 프로세스로, 제한 시간을 두고 돌린다.
- HWP·HWPX·DOCX·XLSX 는 LibreOffice(headless)로 PDF 로 바꾼다. HWP·HWPX 는 H2Orestart 확장이 읽는다.
- 대화가 메모리에만 있으므로 서버를 띄울 때 보관 폴더를 비운다.
"""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import zipfile
from typing import Any

MAX_UPLOAD = 15 * 1024 * 1024
MAX_PAGES_SHOWN = 8            # 모델에 그림으로 보이는 쪽 수
MAX_PREVIEW_PAGES = 60         # 화면의 미리보기에서 넘겨 볼 수 있는 쪽 수
MAX_PDF_TEXT = 40000           # 모델에 넘기는 추출 글의 상한(글자)
PER_USER_FILES, PER_USER_BYTES = 200, 300 * 1024 * 1024
MIME = {'pdf': 'application/pdf', 'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'hwp': 'application/x-hwp', 'hwpx': 'application/hwp+zip'}
OFFICE = {'hwp': '한글(HWP)', 'hwpx': '한글(HWPX)', 'docx': '워드(DOCX)', 'xlsx': '엑셀(XLSX)'}
ACCEPTED = 'PDF, HWP, HWPX, DOCX'
# 변환에 쓰는 LibreOffice 프로필에 미리 넣는 설정: 매크로를 실행하지 않고, 문서가 가리키는 바깥 자료를 불러오지 않는다.
_OFFICE_SETTINGS = '''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop></item>
</oor:items>
'''


class FileError(ValueError):
    """올린 파일을 받을 수 없을 때. 메시지는 사용자에게 그대로 보여 준다."""


class FileStore:
    def __init__(self, root: Path, clean: bool = True):
        self.root = Path(root)
        if clean and self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def _folder(self, user: str) -> Path:
        return self.root / hashlib.sha256(user.encode()).hexdigest()[:20]

    def save(self, user: str, name: str, data: bytes, kind: str, **extra: Any) -> dict[str, Any]:
        """kind: pdf | hwp | hwpx | docx | xlsx. 돌려주는 것은 화면과 대화에 싣는 정보(id, name, size, kind, …)."""
        if kind not in MIME:
            raise FileError('다룰 수 없는 파일 종류입니다')
        folder = self._folder(user)
        folder.mkdir(exist_ok=True)
        folder.chmod(0o700)
        file_id = secrets.token_urlsafe(12)
        meta = {'id': file_id, 'name': name, 'size': len(data), 'kind': kind, 'created': time.time(), **extra}
        (folder / file_id).write_bytes(data)
        (folder / f'{file_id}.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        self._trim(folder)
        return meta

    def get(self, user: str, file_id: str) -> tuple[dict[str, Any], bytes] | None:
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,40}', file_id or ''):        # 경로로 쓰이므로 형식을 먼저 본다
            return None
        path = self._folder(user) / file_id
        try:
            return json.loads(path.with_name(f'{file_id}.json').read_text(encoding='utf-8')), path.read_bytes()
        except (OSError, ValueError):
            return None

    def delete(self, user: str, file_id: str) -> None:
        if re.fullmatch(r'[A-Za-z0-9_-]{8,40}', file_id or ''):
            for path in self._folder(user).glob(f'{file_id}*'):
                if path.name == file_id or path.name.startswith(f'{file_id}.'):
                    path.unlink(missing_ok=True)

    def update(self, user: str, file_id: str, **extra: Any) -> dict[str, Any] | None:
        """저장해 둔 파일 정보에 값을 더한다(쪽 수처럼 나중에 알게 되는 것)."""
        found = self.get(user, file_id)
        if found is None:
            return None
        meta = {**found[0], **extra}
        (self._folder(user) / f'{file_id}.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        return meta

    def derived(self, user: str, file_id: str, suffix: str) -> bytes | None:
        """파일에서 만들어 둔 것(미리보기 PDF 등). 없으면 None."""
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,40}', file_id or '') or not re.fullmatch(r'[a-z0-9]{1,8}', suffix):
            return None
        try:
            return (self._folder(user) / f'{file_id}.{suffix}').read_bytes()
        except OSError:
            return None

    def save_derived(self, user: str, file_id: str, suffix: str, data: bytes) -> None:
        if re.fullmatch(r'[A-Za-z0-9_-]{8,40}', file_id or '') and re.fullmatch(r'[a-z0-9]{1,8}', suffix) and (self._folder(user) / file_id).is_file():
            (self._folder(user) / f'{file_id}.{suffix}').write_bytes(data)

    def _trim(self, folder: Path) -> None:
        """계정당 파일 수와 용량의 상한을 넘으면 오래된 것부터 지운다(딸린 파일도 함께)."""
        files = sorted((path for path in folder.iterdir() if '.' not in path.name), key=lambda path: path.stat().st_mtime)
        total = sum(path.stat().st_size for path in folder.iterdir())
        while files and (len(files) > PER_USER_FILES or total > PER_USER_BYTES):
            oldest = files.pop(0)
            for path in [oldest, *folder.glob(f'{oldest.name}.*')]:
                total -= path.stat().st_size
                path.unlink(missing_ok=True)


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise FileError('PDF 를 읽는 데 시간이 너무 오래 걸립니다') from None
    except FileNotFoundError:
        raise FileError('서버에 PDF 도구(poppler-utils)가 없습니다') from None


def sniff(data: bytes) -> str | None:
    """파일 내용으로 종류를 가린다(pdf, hwp, hwpx, docx). 확장자는 믿지 않는다."""
    if b'%PDF-' in data[:1024]:
        return 'pdf'
    if data[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':                    # OLE 복합 문서: HWP 5 는 이 안에 서명이 있다
        return 'hwp' if b'HWP Document File' in data else None
    if data[:4] == b'PK\x03\x04':
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 5000 or sum(entry.file_size for entry in entries) > 300 * 1024 * 1024:
                    raise FileError('압축을 풀면 너무 커지는 파일입니다')
                names = {entry.filename for entry in entries}
                if 'word/document.xml' in names:
                    return 'docx'
                if 'Contents/content.hpf' in names or any(name.startswith('Contents/section') for name in names):
                    return 'hwpx'
        except zipfile.BadZipFile:
            return None
    return None


def office_to_pdf(data: bytes, kind: str, timeout: float = 120.0) -> bytes:
    """HWP·HWPX·DOCX·XLSX 를 LibreOffice 로 PDF 로 바꾼다. 매번 새 임시 프로필로, 매크로와 바깥 자료 불러오기를 끈 채 돌린다."""
    if kind not in OFFICE:
        raise FileError('바꿀 수 없는 파일 종류입니다')
    if len(data) > MAX_UPLOAD:
        raise FileError(f'파일이 너무 큽니다({MAX_UPLOAD // 1024 // 1024}MB 이하)')
    program = shutil.which('soffice') or shutil.which('libreoffice')
    if not program:
        raise FileError(f'서버에 문서 변환 도구(LibreOffice)가 없어 {OFFICE[kind]} 파일의 쪽 모양을 그릴 수 없습니다.')
    with tempfile.TemporaryDirectory(prefix='rndchat-office-') as folder:
        work = Path(folder)
        (work / 'profile' / 'user').mkdir(parents=True)
        (work / 'profile' / 'user' / 'registrymodifications.xcu').write_text(_OFFICE_SETTINGS, encoding='utf-8')
        (work / f'input.{kind}').write_bytes(data)
        # 환경을 비우고, 혹시 바깥으로 나가려는 요청은 닫힌 포트의 프록시로 보내 실패하게 한다
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(work), 'LANG': 'ko_KR.UTF-8', 'LC_ALL': 'C.UTF-8',
               'http_proxy': 'http://127.0.0.1:9', 'https_proxy': 'http://127.0.0.1:9', 'ftp_proxy': 'http://127.0.0.1:9', 'no_proxy': ''}
        command = [program, '--headless', '--norestore', '--nolockcheck', '--nodefault', '--nologo', '--nofirststartwizard',
                   f'-env:UserInstallation=file://{work}/profile', '--convert-to', 'pdf', '--outdir', str(work), str(work / f'input.{kind}')]
        try:
            subprocess.run(command, capture_output=True, timeout=timeout, check=False, env=env, cwd=str(work), start_new_session=True)
        except subprocess.TimeoutExpired:
            raise FileError('문서를 PDF 로 바꾸는 데 시간이 너무 오래 걸립니다') from None
        result = work / 'input.pdf'
        if not result.is_file() or result.stat().st_size < 100:
            hint = ' (서버에 HWP 확장 H2Orestart 가 있는지 확인하세요)' if kind in ('hwp', 'hwpx') else ''
            raise FileError(f'{OFFICE[kind]} 파일을 PDF 로 바꾸지 못했습니다{hint}')
        return result.read_bytes()


def to_pdf(data: bytes, kind: str) -> bytes:
    """미리보기용 PDF. 한글 문서는 고칠 때 쓰는 엔진(rhwp)의 조판으로 그리고, 엔진이 없거나 못 그리면 LibreOffice 로 그린다."""
    if kind in ('hwp', 'hwpx'):
        from . import hwp
        from .editing import EditError
        if hwp.available():
            try:
                return hwp.to_pdf(data, kind)
            except EditError:
                pass
    return office_to_pdf(data, kind)


def read_pdf(data: bytes, max_pages: int = MAX_PAGES_SHOWN, timeout: float = 40.0) -> dict[str, Any]:
    """PDF 의 쪽 수, 추출한 글, 앞쪽 몇 쪽의 그림(JPEG, base64). 모델이 양식의 모양을 보도록 그림을 함께 준다."""
    if len(data) > MAX_UPLOAD:
        raise FileError(f'파일이 너무 큽니다({MAX_UPLOAD // 1024 // 1024}MB 이하)')
    if b'%PDF-' not in data[:1024]:
        raise FileError('PDF 파일이 아닙니다')
    with tempfile.TemporaryDirectory(prefix='rndchat-pdf-') as folder:
        source = Path(folder) / 'input.pdf'
        source.write_bytes(data)
        info = _run(['pdfinfo', str(source)], timeout)
        text = info.stdout.decode('utf-8', 'replace')
        pages = re.search(r'^Pages:\s+(\d+)', text, re.M)
        if info.returncode != 0 or not pages:
            raise FileError('PDF 를 열 수 없습니다(손상되었거나 암호가 걸려 있습니다)')
        if re.search(r'^Encrypted:\s+yes', text, re.M):
            raise FileError('암호가 걸린 PDF 는 읽을 수 없습니다')
        count, shown = int(pages.group(1)), min(int(pages.group(1)), max_pages)
        extracted = _run(['pdftotext', '-layout', '-l', str(shown), str(source), '-'], timeout).stdout.decode('utf-8', 'replace')
        _run(['pdftoppm', '-jpeg', '-jpegopt', 'quality=82', '-scale-to', '1400', '-f', '1', '-l', str(shown), str(source), str(Path(folder) / 'page')], timeout)
        images = [base64.b64encode(path.read_bytes()).decode() for path in sorted(Path(folder).glob('page*.jpg'))]
        if not images:
            raise FileError('PDF 의 쪽을 그림으로 바꾸지 못했습니다')
    return {'pages': count, 'shown': len(images), 'text': re.sub(r'\n{3,}', '\n\n', extracted).strip()[:MAX_PDF_TEXT], 'images': images}


class PdfCache:
    """올린 직후 읽어 둔 쪽 그림을 잠깐 기억한다(질문을 보낼 때 다시 그리지 않게). 없으면 다시 읽으면 된다."""

    def __init__(self, size: int = 16):
        self.size, self._items = size, OrderedDict()

    def put(self, file_id: str, parsed: dict[str, Any]) -> None:
        self._items[file_id] = parsed
        self._items.move_to_end(file_id)
        while len(self._items) > self.size:
            self._items.popitem(last=False)

    def get(self, file_id: str) -> dict[str, Any] | None:
        return self._items.get(file_id)


def pdf_pages(data: bytes, timeout: float = 20.0) -> int:
    with tempfile.TemporaryDirectory(prefix='rndchat-pdf-') as folder:
        source = Path(folder) / 'input.pdf'
        source.write_bytes(data)
        found = re.search(r'^Pages:\s+(\d+)', _run(['pdfinfo', str(source)], timeout).stdout.decode('utf-8', 'replace'), re.M)
    if not found:
        raise FileError('PDF 를 열 수 없습니다')
    return int(found.group(1))


def render_page(data: bytes, page: int, width: int = 1400, timeout: float = 30.0) -> bytes:
    """PDF 의 한 쪽을 JPEG 로(page 는 1 부터)."""
    with tempfile.TemporaryDirectory(prefix='rndchat-pdf-') as folder:
        source = Path(folder) / 'input.pdf'
        source.write_bytes(data)
        _run(['pdftoppm', '-jpeg', '-jpegopt', 'quality=82', '-scale-to', str(width), '-f', str(page), '-l', str(page), str(source),
              str(Path(folder) / 'page')], timeout)
        images = sorted(Path(folder).glob('page*.jpg'))
        if not images:
            raise FileError('그 쪽을 그림으로 바꾸지 못했습니다')
        return images[0].read_bytes()


def pages_with(data: bytes, needles: list[str], limit: int = 3, timeout: float = 30.0) -> list[int]:
    """needles(고친 글) 가 많이 나오는 쪽 번호(1 부터) 몇 개. 고친 곳을 모델과 사용자에게 보여 줄 쪽을 고르는 데 쓴다."""
    needles = [''.join(needle.split())[:24] for needle in needles if needle and len(''.join(needle.split())) >= 2][:60]
    with tempfile.TemporaryDirectory(prefix='rndchat-pdf-') as folder:
        source = Path(folder) / 'input.pdf'
        source.write_bytes(data)
        text = _run(['pdftotext', '-layout', str(source), '-'], timeout).stdout.decode('utf-8', 'replace')
    pages = [''.join(page.split()) for page in text.split('\f')]
    scored = sorted(((sum(needle in page for needle in needles), -index) for index, page in enumerate(pages)), reverse=True)
    chosen = sorted(-index + 1 for hits, index in scored[:limit] if hits)
    return chosen or [1]


def locate(data: bytes, needles: list[str], timeout: float = 30.0) -> list[int]:
    """needles 각각이 처음 나오는 쪽 번호(1 부터, 못 찾으면 0). 고친 곳을 미리보기에서 바로 찾아가게 하는 데 쓴다."""
    with tempfile.TemporaryDirectory(prefix='rndchat-pdf-') as folder:
        source = Path(folder) / 'input.pdf'
        source.write_bytes(data)
        text = _run(['pdftotext', '-layout', str(source), '-'], timeout).stdout.decode('utf-8', 'replace')
    pages = [''.join(page.split()) for page in text.split('\f')]
    found = []
    for needle in needles:
        key = ''.join((needle or '').split())[:24]
        found.append(next((index + 1 for index, page in enumerate(pages) if key in page), 0) if len(key) >= 2 else 0)
    return found


class Previews:
    """파일을 눈으로 보는 길: 미리보기 PDF(없으면 만들어 보관), 모델에 줄 쪽 그림과 글, 화면에 줄 쪽 그림.

    LibreOffice 변환은 한 번에 하나만 돌린다(무겁고, 동시에 여럿이면 장비가 느려진다).
    """

    def __init__(self, store: FileStore):
        self.store, self._converting, self._parsed, self._pages = store, asyncio.Lock(), PdfCache(), PdfCache(48)

    async def pdf(self, user: str, meta: dict[str, Any], data: bytes | None = None) -> bytes:
        if data is None:
            found = self.store.get(user, meta['id'])
            if found is None:
                raise FileError('더 이상 보관되어 있지 않은 파일입니다')
            data = found[1]
        if meta['kind'] == 'pdf':
            return data
        ready = self.store.derived(user, meta['id'], 'pdf')
        if ready is None:
            async with self._converting:
                ready = self.store.derived(user, meta['id'], 'pdf') or await asyncio.to_thread(to_pdf, data, meta['kind'])
            self.store.save_derived(user, meta['id'], 'pdf', ready)
        return ready

    async def parsed(self, user: str, meta: dict[str, Any], data: bytes | None = None) -> dict[str, Any]:
        """모델에 줄 것: 쪽 수, 추출한 글, 앞쪽 몇 쪽의 그림."""
        parsed = self._parsed.get(meta['id'])
        if parsed is None:
            parsed = await asyncio.to_thread(read_pdf, await self.pdf(user, meta, data))
            self._parsed.put(meta['id'], parsed)
        return parsed

    async def page(self, user: str, meta: dict[str, Any], page: int, width: int) -> bytes:
        key = f'{meta["id"]}:{page}:{width}'
        image = self._pages.get(key)
        if image is None:
            image = await asyncio.to_thread(render_page, await self.pdf(user, meta), page, width)
            self._pages.put(key, image)
        return image
