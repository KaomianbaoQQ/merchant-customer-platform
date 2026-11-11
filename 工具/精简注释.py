"""精简过度注释：删掉分节横幅与长篇解释，保留短小的说明。

用法：
    python3 工具/精简注释.py            # 只看统计
    python3 工具/精简注释.py --write    # 实际改写
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_KEEP = 20
PY_KEEP = 40
BANNER = re.compile(r'^[\s=*#\-_·—]*$')

HTML_DIRS = ['前端']
PY_DIRS = ['后端', '公共', '售后', '客服', '物流', '智能分析', '工具']
PY_FILES = ['main.py']


def is_banner(text):
    core = text.strip().strip('/*').strip().strip('=*-_# ').strip()
    return bool(BANNER.match(core)) or len(set(core)) <= 2


def keep(text, limit):
    body = text.strip().strip('/*').strip()
    if not body:
        return False
    if is_banner(body):
        return False
    return len(body) <= limit


def strip_css(block, limit=WEB_KEEP):
    out = []
    i, n = 0, len(block)
    while i < n:
        c = block[i]
        if c in '"\'':
            q = c
            j = i + 1
            while j < n and block[j] != q:
                j += 2 if block[j] == '\\' else 1
            out.append(block[i:j + 1])
            i = j + 1
            continue
        if c == '/' and i + 1 < n and block[i + 1] == '*':
            j = block.find('*/', i + 2)
            j = n if j < 0 else j + 2
            if keep(block[i:j], limit):
                out.append(block[i:j])
            i = j
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def strip_js(block, limit=WEB_KEEP):
    out = []
    i, n = 0, len(block)
    prev = ''
    while i < n:
        c = block[i]
        if c in '"\'':
            q = c
            j = i + 1
            while j < n:
                if block[j] == '\\':
                    j += 2
                elif block[j] == q or block[j] == '\n':
                    break
                else:
                    j += 1
            out.append(block[i:j + 1])
            i = j + 1
            continue
        if c == '`':
            j = i + 1
            while j < n:
                if block[j] == '\\':
                    j += 2
                elif block[j] == '`':
                    break
                else:
                    j += 1
            out.append(block[i:j + 1])
            i = j + 1
            continue
        if c == '/' and i + 1 < n and block[i + 1] == '/':
            j = block.find('\n', i)
            j = n if j < 0 else j
            if keep(block[i:j], limit):
                out.append(block[i:j])
            i = j
            continue
        if c == '/' and i + 1 < n and block[i + 1] == '*':
            j = block.find('*/', i + 2)
            j = n if j < 0 else j + 2
            if keep(block[i:j], limit):
                out.append(block[i:j])
            i = j
            continue
        if c == '/' and prev in '(,=:[!&|?{};+-*%<>~^':
            j = i + 1
            in_class = False
            while j < n:
                if block[j] == '\\':
                    j += 2
                    continue
                if block[j] == '[':
                    in_class = True
                elif block[j] == ']':
                    in_class = False
                elif block[j] == '/' and not in_class:
                    break
                elif block[j] == '\n':
                    break
                j += 1
            out.append(block[i:j + 1])
            i = j + 1
            continue
        out.append(c)
        if not c.isspace():
            prev = c
        i += 1
    return ''.join(out)


def count_comments(src):
    return len(re.findall(r'<!--|(?://|/\*)', src))


def strip_html(path):
    src = open(path, encoding='utf-8').read()
    before = count_comments(src)

    def repl_script(m):
        tag = re.match(r'<script[^>]*>', m.group(0)).group(0)
        body = re.sub(r'</script>\s*$', '', m.group(0)[len(tag):])
        return tag + strip_js(body) + '</script>'

    def repl_style(m):
        tag = re.match(r'<style[^>]*>', m.group(0)).group(0)
        body = re.sub(r'</style>\s*$', '', m.group(0)[len(tag):])
        return tag + strip_css(body) + '</style>'

    src = re.sub(r'<script[\s\S]*?</script>', repl_script, src, flags=re.I)
    src = re.sub(r'<style[\s\S]*?</style>', repl_style, src, flags=re.I)
    src = re.sub(
        r'<!--[\s\S]*?-->',
        lambda m: m.group(0) if keep(m.group(0).strip('<!->'), 14) and len(m.group(0)) < 40 else '',
        src,
    )
    src = re.sub(r'\n{3,}', '\n\n', src)
    return src, before, count_comments(src)


def strip_py(path):
    lines = open(path, encoding='utf-8').read().split('\n')
    out = []
    before = after = 0
    for line in lines:
        idx = _hash_outside_quotes(line) if '#' in line else None
        if idx is None:
            out.append(line)
            continue
        before += 1
        comment, code = line[idx:], line[:idx]
        if keep(comment, PY_KEEP):
            after += 1
            out.append(line)
        elif code.strip() == '':
            continue
        else:
            out.append(code.rstrip())
    src = re.sub(r'\n{3,}', '\n\n', '\n'.join(out))
    return src, before, after


def _hash_outside_quotes(line):
    q = None
    i = 0
    while i < len(line):
        c = line[i]
        if q:
            if c == '\\':
                i += 2
                continue
            if c == q:
                q = None
        else:
            if c in '"\'':
                q = c
            elif c == '#':
                return i
        i += 1
    return None


def main():
    write = '--write' in sys.argv
    targets = []
    for d in HTML_DIRS:
        p = os.path.join(BASE, d)
        for f in sorted(os.listdir(p)):
            if f.endswith('.html'):
                targets.append(('html', os.path.join(p, f)))
    for d in PY_DIRS:
        p = os.path.join(BASE, d)
        if not os.path.isdir(p):
            continue
        for root, _, files in os.walk(p):
            if '__pycache__' in root:
                continue
            for f in sorted(files):
                if f.endswith('.py'):
                    targets.append(('py', os.path.join(root, f)))
    for f in PY_FILES:
        p = os.path.join(BASE, f)
        if os.path.exists(p):
            targets.append(('py', p))

    total_before = total_after = 0
    for kind, path in targets:
        try:
            new, before, after = strip_html(path) if kind == 'html' else strip_py(path)
        except Exception as exc:                     # noqa: BLE001
            print('跳过', os.path.relpath(path, BASE), exc)
            continue
        total_before += before
        total_after += after
        if before != after:
            print('%-38s 注释 %3d → %3d' % (os.path.relpath(path, BASE), before, after))
            if write:
                open(path, 'w', encoding='utf-8').write(new)
    print('合计 %d → %d' % (total_before, total_after), '（已写入）' if write else '（预览，加 --write 生效）')


main()
