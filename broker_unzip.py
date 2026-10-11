#!/usr/bin/env python3
"""Unpack broker-report archives dropped in the Drive「壓縮檔」folder into「券商報告」.

For each archive (or multi-volume set) in SRC that has no same-named subfolder yet:
  1. download (by file ID) into an isolated work dir and extract (zip/7z/rar, Big5 names fixed)
  2. drop macOS junk; extract nested archives
  3. report files whose content (MD5) is not already anywhere under DST are uploaded
     to DST top level (name clash with different content -> "name (2).ext")
  4. everything else (duplicates, non-report files) goes to SRC/<archive name>/,
     which also marks the archive as processed (created last, so a crash is retried)
Original archives are never modified, moved or deleted.

usage: broker_unzip.py WORKDIR [--dry-run]
"""
import json, os, re, shutil, subprocess, sys, unicodedata, zipfile, hashlib, datetime

SRC = '11tcvmCveM7ObkGamyKadtWl5UX15IR4M'   # 壓縮檔
DST = '1q20TWa-htrEacFLOrkeA-JxeQ_i8V0nL'   # 券商報告
W = os.path.abspath(sys.argv[1]); DRY = '--dry-run' in sys.argv
REPORT_EXT = {'pdf', 'doc', 'docx', 'txt', 'xls', 'xlsx', 'ppt', 'pptx', 'md', 'rtf', 'htm', 'html', 'csv'}
ARCH = re.compile(r'\.(zip|7z|rar)$', re.I)
CJK = re.compile(r'[㐀-鿿＀-￯　-〿]')
LOG = os.path.join(W, 'broker_unzip_log.jsonl')


def run(cmd):
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True)


def rc(*a):
    r = run(['rclone', *a])
    if r.returncode:
        raise RuntimeError(f"rclone {a[0]}: {r.stderr.decode()[-300:]}")
    return r.stdout


def remote(fid): return f'gdrive,root_folder_id={fid}:'


def log(**r):
    r['time'] = datetime.datetime.now().isoformat(timespec='seconds')
    with open(LOG, 'a') as f: f.write(json.dumps(r, ensure_ascii=False) + '\n')
    print(json.dumps(r, ensure_ascii=False), flush=True)


# ---------- grouping (multi-volume) ----------
def group_key(name):
    """Return (set_stem, is_first_volume) or None if not an archive part."""
    n = name
    m = re.match(r'(.*)\.part0*(\d+)\.rar$', n, re.I)
    if m: return m.group(1), int(m.group(2)) == 1
    m = re.match(r'(.*)\.(7z|zip|rar)\.(\d{3})$', n, re.I)
    if m: return m.group(1), int(m.group(3)) == 1
    m = re.match(r'(.*)\.r\d\d$', n, re.I)
    if m: return m.group(1), False
    m = re.match(r'(.*)\.z\d\d$', n, re.I)
    if m: return m.group(1), False
    m = ARCH.search(n)
    if m: return n[:m.start()], True
    return None


# ---------- names ----------
def fix_name(b):
    try:
        s = b.decode('utf-8')
    except UnicodeDecodeError:
        for enc in ('cp950', 'big5hkscs', 'gbk'):
            try: return b.decode(enc)
            except UnicodeDecodeError: pass
        return b.decode('cp950', errors='replace')
    if not CJK.search(s) and re.search(r'[\u0080-ÿ─-◿]', s):
        for enc in ('cp437', 'latin-1'):
            try:
                t = s.encode(enc).decode('cp950')
                if CJK.search(t): return t
            except (UnicodeEncodeError, UnicodeDecodeError): pass
    return s


def cleanup_tree(root):
    rootb = os.fsencode(root)
    for dp, dns, fns in os.walk(rootb, topdown=False):
        for n in fns + dns:
            p = os.path.join(dp, n); name = n.decode('utf-8', 'surrogateescape')
            if os.path.islink(p): os.unlink(p); continue
            if name == '__MACOSX' and os.path.isdir(p): shutil.rmtree(p); continue
            if os.path.isfile(p) and (name.startswith('._') or name == '.DS_Store'): os.unlink(p); continue
            new = unicodedata.normalize('NFC', fix_name(n))
            if new.encode() != n:
                q = os.path.join(dp, new.encode())
                if os.path.exists(q):
                    b, e = os.path.splitext(new); q = os.path.join(dp, (b + '_1' + e).encode())
                os.rename(p, q)
    for dp, _, _ in os.walk(rootb, topdown=False):
        if dp != rootb and not os.listdir(dp): os.rmdir(dp)


# ---------- extraction ----------
def extract_zip(arc, dest):
    with zipfile.ZipFile(arc) as z:
        if any(i.flag_bits & 1 for i in z.infolist()): raise PermissionError('有密碼')
        for i in z.infolist():
            name = i.filename if i.flag_bits & 0x800 else fix_name(i.filename.encode('cp437'))
            parts = [p for p in name.replace('\\', '/').split('/') if p not in ('', '.', '..')]
            if not parts: continue
            t = os.path.join(dest, *parts)
            if i.is_dir(): os.makedirs(t, exist_ok=True); continue
            os.makedirs(os.path.dirname(t), exist_ok=True)
            with z.open(i) as s, open(t, 'wb') as o: shutil.copyfileobj(s, o)


def extract(arc, dest):
    ext = arc.rsplit('.', 1)[-1].lower(); errs = []
    if ext == 'zip':
        try: return extract_zip(arc, dest)
        except PermissionError: raise
        except Exception as e:
            errs.append(f'zipfile: {e}'); shutil.rmtree(dest); os.makedirs(dest)
    tools = []
    if re.search(r'\.(rar|r\d\d)$', arc, re.I) or 'part' in arc.lower():
        tools.append(['unrar', 'x', '-p-', '-y', '-o+', '-idq', arc, dest + '/'])
    tools += [['7z', 'x', '-pNOPASSWORD__', '-y', '-o' + dest, arc],
              ['unar', '-q', '-f', '-D', '-p', 'NOPASSWORD__', '-o', dest, arc]]
    for cmd in tools:
        r = run(cmd)
        if r.returncode == 0: return
        msg = (r.stdout + r.stderr).decode('utf-8', 'replace').strip()
        errs.append(f'{cmd[0]}(rc={r.returncode}): {msg[-200:]}')
        if re.search(r'password|encrypted', msg, re.I): raise PermissionError('有密碼 — ' + errs[-1])
        shutil.rmtree(dest); os.makedirs(dest)
    raise RuntimeError('解壓失敗/可能損毀 — ' + ' | '.join(errs))


def extract_nested(dest, depth=0):
    """Extract archives found inside the output (in place), up to 3 levels."""
    if depth >= 3: return []
    notes = []
    for dp, _, fns in list(os.walk(dest)):
        for f in fns:
            if ARCH.search(f):
                p = os.path.join(dp, f); sub = os.path.join(dp, ARCH.sub('', f) + '_解壓')
                os.makedirs(sub, exist_ok=True)
                try:
                    extract(p, sub); os.unlink(p); notes.append(f'內層壓縮檔已解開: {f}')
                except Exception as e:
                    shutil.rmtree(sub, ignore_errors=True); notes.append(f'內層壓縮檔無法解開（保留原檔）: {f}: {e}')
    if notes: notes += extract_nested(dest, depth + 1)
    return notes


def md5(p):
    h = hashlib.md5()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''): h.update(b)
    return h.hexdigest()


# ---------- main ----------
def main():
    os.makedirs(W, exist_ok=True)
    src = json.loads(rc('lsjson', '--no-modtime', '--no-mimetype', remote(SRC)))
    dirs = {x['Name'] for x in src if x['IsDir']}
    sets = {}
    for f in src:
        if f['IsDir']: continue
        g = group_key(f['Name'])
        if g: sets.setdefault(g[0], []).append((f, g[1]))
    todo = {k: v for k, v in sets.items() if k not in dirs}
    log(event='scan', archives=len(sets), new=len(todo), names=sorted(todo))
    if not todo or DRY: return
    dst = json.loads(rc('lsjson', '-R', '--files-only', '--hash', '--no-modtime', '--no-mimetype', '--fast-list', remote(DST)))
    known = {(x.get('Hashes') or {}).get('md5') for x in dst} - {None}
    top = {x['Name'] for x in dst if '/' not in x['Path']}
    for stem, vols in sorted(todo.items()):
        firsts = [f for f, first in vols if first]
        base = dict(archive=stem, volumes=[f['Name'] for f, _ in vols])
        if len(firsts) != 1:
            log(**base, status='failed', reason='分卷不完整或找不到第一卷'); continue
        jd = os.path.join(W, 'job_' + hashlib.md5(stem.encode()).hexdigest()[:10])
        shutil.rmtree(jd, ignore_errors=True)
        dl, out, rest = (os.path.join(jd, d) for d in ('dl', 'out', 'rest'))
        for d in (dl, out, rest): os.makedirs(d)
        try:
            for f, _ in vols:
                rc('backend', 'copyid', 'gdrive:', f['ID'], os.path.join(dl, f['Name']))
                if os.path.getsize(os.path.join(dl, f['Name'])) != f['Size']: raise RuntimeError('下載大小不符: ' + f['Name'])
            extract(os.path.join(dl, firsts[0]['Name']), out)
            cleanup_tree(out); notes = extract_nested(out); cleanup_tree(out)
        except Exception as e:
            log(**base, status='failed', reason=str(e)[:500]); shutil.rmtree(jd); continue
        moved, dup, other = [], [], []
        for dp, _, fns in os.walk(out):
            for fn in sorted(fns):
                p = os.path.join(dp, fn); h = md5(p); ext = fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
                if ext not in REPORT_EXT or h in known:
                    (dup if h in known else other).append(os.path.relpath(p, out))
                    q = os.path.join(rest, os.path.relpath(p, out)); os.makedirs(os.path.dirname(q), exist_ok=True)
                    shutil.move(p, q); continue
                name = fn; b, e = os.path.splitext(fn); i = 2
                while name in top: name = f'{b} ({i}){e}'; i += 1
                rc('copyto', p, remote(DST) + name)
                chk = json.loads(rc('lsjson', '--hash', '--files-only', '--no-modtime', remote(DST) + name))
                if not chk or chk[0]['Hashes'].get('md5') != h: raise SystemExit(f'上傳核對失敗: {name}')
                known.add(h); top.add(name); moved.append(name)
        # leftovers + processed marker (created last)
        rc('mkdir', remote(SRC) + stem)
        if any(os.scandir(rest)): rc('copy', rest, remote(SRC) + stem)
        log(**base, status='ok', to_reports=moved, duplicates=dup, non_report=other, notes=notes)
        shutil.rmtree(jd)


main()
