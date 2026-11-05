"""Bring the saved results of formulas up to date after cells change, the way Excel would on save.

Excel keeps each formula's last result in the file (<v>). After a fix those results are stale until someone
recalculates in Excel, but previews, phone viewers and the verifier's own checks read the saved results. This
evaluates the formulas end-of-shift reports use (cell math, SUM/COUNT/AVERAGE/MIN/MAX, IF, IFERROR, ROUND,
COUNTIF/SUMIF, references to other tabs) and rewrites their results. A formula it can't evaluate keeps its
old result; Excel still recalculates everything when the file is opened.
"""
import io
import re
import zipfile
from xml.etree import ElementTree as ET

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
CELL = re.compile(r'<c\b([^>]*?)(?:/>|>(.*?)</c>)', re.S)
REF = re.compile(r"(?:(?:'((?:[^']|'')+)'|([A-Za-z_][\w.]*))!)?(\$?)([A-Z]{1,3})(\$?)(\d+)(?![\w(])")


class Unsupported(Exception):
    pass


def _col(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _letters(n):
    s = ''
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _shift(formula, drow, dcol):
    """A shared formula as it applies to a cell drow rows / dcol columns from its master."""
    out, i = [], 0
    for m in re.finditer(r'"[^"]*"|' + REF.pattern, formula):
        out.append(formula[i:m.start()])
        if m.group(0).startswith('"'):
            out.append(m.group(0))
        else:
            sheet = m.group(0)[:m.group(0).find('!') + 1] if '!' in m.group(0) else ''
            col = m.group(4) if m.group(3) else _letters(_col(m.group(4)) + dcol)
            row = m.group(6) if m.group(5) else str(int(m.group(6)) + drow)
            out.append(f'{sheet}{m.group(3)}{col}{m.group(5)}{row}')
        i = m.end()
    return ''.join(out) + formula[i:]


# ---------------------------------------------------------------- formula evaluation
TOKEN = re.compile(r"""\s*(?:
    (?P<str>"(?:[^"]|"")*")|
    (?P<ref>(?:(?:'(?:[^']|'')+'|[A-Za-z_][\w.]*)!)?\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?)(?![\w(])|
    (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|
    (?P<func>[A-Za-z][A-Za-z0-9.]*)\s*\(|
    (?P<bool>TRUE|FALSE)\b|
    (?P<op><>|<=|>=|[-+*/^&=<>(),:%])
)""", re.X)


def _tokens(text):
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise Unsupported(text[pos:pos + 20])
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


class _Eval:
    def __init__(self, book, sheet):
        self.book, self.sheet = book, sheet

    def run(self, formula):
        self.t, self.i = _tokens(formula), 0
        v = self.compare()
        if self.i != len(self.t):
            raise Unsupported(formula)
        return v

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self, value=None):
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise Unsupported(f'expected {value}')
        self.i += 1
        return tok

    def compare(self):
        a = self.concat()
        while self.peek()[1] in ('=', '<>', '<', '>', '<=', '>='):
            op = self.take()[1]
            b = self.concat()
            a = _cmp(op, _scalar(a), _scalar(b))
        return a

    def concat(self):
        a = self.add()
        while self.peek()[1] == '&':
            self.take()
            a = _text(_scalar(a)) + _text(_scalar(self.add()))
        return a

    def add(self):
        a = self.mul()
        while self.peek()[1] in ('+', '-'):
            op = self.take()[1]
            b = self.mul()
            a = _num(a) + _num(b) if op == '+' else _num(a) - _num(b)
        return a

    def mul(self):
        a = self.unary()
        while self.peek()[1] in ('*', '/'):
            op = self.take()[1]
            b = self.unary()
            if op == '/' and _num(b) == 0:
                raise Unsupported('#DIV/0!')
            a = _num(a) * _num(b) if op == '*' else _num(a) / _num(b)
        return a

    def unary(self):
        if self.peek()[1] in ('-', '+'):
            op = self.take()[1]
            v = _num(self.unary())
            return -v if op == '-' else v
        v = self.primary()
        while self.peek()[1] in ('^', '%'):
            if self.take()[1] == '%':
                v = _num(v) / 100
            else:
                v = _num(v) ** _num(self.primary())
        return v

    def primary(self):
        kind, val = self.take()
        if kind == 'num':
            return float(val)
        if kind == 'str':
            return val[1:-1].replace('""', '"')
        if kind == 'bool':
            return val == 'TRUE'
        if kind == 'ref':
            return self.ref(val)
        if kind == 'func':
            return self.func(val.upper())
        if val == '(':
            v = self.compare()
            self.take(')')
            return v
        raise Unsupported(str(val))

    def ref(self, text):
        sheet = self.sheet
        if '!' in text:
            name, text = text.rsplit('!', 1)
            sheet = name[1:-1].replace("''", "'") if name.startswith("'") else name
        text = text.replace('$', '')
        if ':' in text:
            a, b = text.split(':')
            (c1, r1), (c2, r2) = _split(a), _split(b)
            return [self.book.value(sheet, c, r) for r in range(min(r1, r2), max(r1, r2) + 1) for c in range(min(c1, c2), max(c1, c2) + 1)]
        c, r = _split(text)
        return self.book.value(sheet, c, r)

    def args(self):
        out = []
        if self.peek()[1] == ')':
            self.take()
            return out
        while True:
            if self.peek()[1] in (',', ')'):
                out.append(None)
            else:
                out.append(self.compare())
            sep = self.take()[1]
            if sep == ')':
                return out
            if sep != ',':
                raise Unsupported('args')

    def func(self, name):
        if name in ('IF', 'IFERROR'):
            return self.lazy(name)
        a = self.args()
        flat = [x for v in a for x in (v if isinstance(v, list) else [v])]
        nums = [x for x in flat if isinstance(x, (int, float)) and not isinstance(x, bool)]
        if name == 'SUM':
            return float(sum(nums))
        if name == 'COUNT':
            return float(len(nums))
        if name == 'COUNTA':
            return float(sum(1 for x in flat if x not in (None, '')))
        if name == 'COUNTBLANK':
            return float(sum(1 for x in flat if x in (None, '')))
        if name in ('AVERAGE', 'MIN', 'MAX'):
            if not nums:
                if name == 'AVERAGE':
                    raise Unsupported('#DIV/0!')
                return 0.0
            return {'AVERAGE': sum(nums) / len(nums), 'MIN': min(nums), 'MAX': max(nums)}[name]
        if name in ('ROUND', 'ROUNDUP', 'ROUNDDOWN'):
            import math
            v, d = _num(a[0]), int(_num(a[1]) if len(a) > 1 else 0)
            if name == 'ROUND':
                return float(round(v + (1e-12 if v >= 0 else -1e-12), d))
            f = 10 ** d
            return (math.ceil if (name == 'ROUNDUP') == (v >= 0) else math.floor)(v * f) / f
        if name == 'ABS':
            return abs(_num(a[0]))
        if name == 'AND':
            return all(_bool(x) for x in flat)
        if name == 'OR':
            return any(_bool(x) for x in flat)
        if name == 'NOT':
            return not _bool(a[0])
        if name in ('COUNTIF', 'SUMIF'):
            rng = a[0] if isinstance(a[0], list) else [a[0]]
            test = _criteria(_scalar(a[1]))
            if name == 'COUNTIF':
                return float(sum(1 for x in rng if test(x)))
            vals = a[2] if len(a) > 2 and isinstance(a[2], list) else rng
            return float(sum(_num(v) for x, v in zip(rng, vals) if test(x) and isinstance(v, (int, float))))
        if name in ('TRIM', 'UPPER', 'LOWER', 'LEN'):
            s = _text(_scalar(a[0]))
            return {'TRIM': ' '.join(s.split()), 'UPPER': s.upper(), 'LOWER': s.lower(), 'LEN': float(len(s))}[name]
        raise Unsupported(name)

    def lazy(self, name):
        start = self.i
        try:
            first = self.compare()
            failed = False
        except Unsupported:
            if name != 'IFERROR':
                raise
            failed, first = True, None
            depth = 0
            while self.i < len(self.t):  # skip the rest of the first argument
                tok = self.t[self.i][1]
                if tok == '(' or self.t[self.i][0] == 'func':
                    depth += 1
                elif tok == ')':
                    if depth == 0:
                        break
                    depth -= 1
                elif tok == ',' and depth == 0:
                    break
                self.i += 1
        rest = self.args() if self.take()[1] == ',' else []
        if name == 'IFERROR':
            return (rest[0] if rest else 0.0) if failed else first
        cond = _bool(_scalar(first))
        if cond:
            return rest[0] if rest else True
        return rest[1] if len(rest) > 1 else False


def _split(a):
    m = re.fullmatch(r'([A-Z]{1,3})(\d+)', a)
    return _col(m.group(1)), int(m.group(2))


def _scalar(v):
    return v[0] if isinstance(v, list) and v else (None if isinstance(v, list) else v)


def _num(v):
    v = _scalar(v)
    if v is None or v == '':
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(',', ''))
    except ValueError:
        raise Unsupported('#VALUE!') from None


def _text(v):
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'TRUE' if v else 'FALSE'
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else repr(v)
    return str(v)


def _bool(v):
    if isinstance(v, str):
        if v.upper() in ('TRUE', 'FALSE'):
            return v.upper() == 'TRUE'
        if v == '':
            return False
        raise Unsupported('#VALUE!')
    return bool(_num(v))


def _cmp(op, a, b):
    if a is None:
        a = '' if isinstance(b, str) else 0.0
    if b is None:
        b = '' if isinstance(a, str) else 0.0
    if isinstance(a, str) and isinstance(b, str):
        a, b = a.lower(), b.lower()
    elif isinstance(a, str) != isinstance(b, str):
        return op == '<>'
    return {'=': a == b, '<>': a != b, '<': a < b, '>': a > b, '<=': a <= b, '>=': a >= b}[op]


def _criteria(c):
    m = re.fullmatch(r'(<>|<=|>=|<|>|=)?(.*)', _text(c))
    op, val = m.group(1) or '=', m.group(2)
    try:
        target = float(val)
    except ValueError:
        target = val.lower()
    def test(x):
        if isinstance(target, float):
            return isinstance(x, (int, float)) and not isinstance(x, bool) and _cmp(op, float(x), target)
        return _cmp(op, _text(x).lower(), target)
    return test


# ---------------------------------------------------------------- workbook
class _Book:
    def __init__(self, cells, formulas):
        self.cells, self.formulas, self.done, self.busy, self.failed = cells, formulas, {}, set(), set()

    def value(self, sheet, col, row):
        key = (sheet, col, row)
        if key in self.formulas:
            return self.result(key)
        return self.cells.get(key)

    def result(self, key):
        if key in self.done:
            return self.done[key]
        if key in self.failed or key in self.busy:
            raise Unsupported('circular or failed')
        self.busy.add(key)
        try:
            v = _Eval(self, key[0]).run(self.formulas[key])
            v = _scalar(v)
            self.done[key] = v
            return v
        except (Unsupported, RecursionError, ZeroDivisionError, OverflowError, TypeError, IndexError):
            self.failed.add(key)
            raise Unsupported('failed')
        finally:
            self.busy.discard(key)


def _sheets(z):
    """[(name, path)] in workbook order."""
    wb = z.read('xl/workbook.xml').decode('utf-8')
    rels = z.read('xl/_rels/workbook.xml.rels').decode('utf-8')
    targets = {m.group(1): m.group(2) for m in re.finditer(r'<Relationship\b[^>]*\bId="([^"]+)"[^>]*\bTarget="([^"]+)"', rels)}
    targets.update({m.group(2): m.group(1) for m in re.finditer(r'<Relationship\b[^>]*\bTarget="([^"]+)"[^>]*\bId="([^"]+)"', rels)})
    out = []
    for m in re.finditer(r'<sheet\b[^>]*?\bname="([^"]+)"[^>]*?\br:id="([^"]+)"', wb):
        t = targets.get(m.group(2), '')
        path = t.lstrip('/') if t.startswith('/') else 'xl/' + t
        out.append((_unxml(m.group(1)), path))
    return out


def _unxml(s):
    return s.replace('&quot;', '"').replace('&apos;', "'").replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')


def _xml(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def recalc(raw):
    """xlsx bytes with formula results brought up to date. Returns raw unchanged if anything goes wrong."""
    try:
        return _recalc(raw)
    except Exception:
        return raw


def _recalc(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    names = set(z.namelist())
    shared = []
    if 'xl/sharedStrings.xml' in names:
        root = ET.fromstring(z.read('xl/sharedStrings.xml'))
        shared = [''.join(t.text or '' for t in si.iter(NS + 't')) for si in root.iter(NS + 'si')]
    sheets = [(n, p) for n, p in _sheets(z) if p in names]
    cells, formulas, xml_by_path = {}, {}, {}
    for name, path in sheets:
        xml = z.read(path).decode('utf-8')
        xml_by_path[path] = xml
        masters = {}
        for m in CELL.finditer(xml):
            attrs, body = m.group(1), m.group(2) or ''
            r = re.search(r'\br="([A-Z]+)(\d+)"', attrs)
            if not r:
                continue
            col, row = _col(r.group(1)), int(r.group(2))
            f = re.search(r'<f\b([^>]*?)(?:/>|>(.*?)</f>)', body, re.S)
            if f:
                fattrs, ftext = f.group(1), _unxml(f.group(2) or '')
                si = re.search(r'\bsi="(\d+)"', fattrs)
                if 't="shared"' in fattrs and si:
                    if ftext:
                        masters[si.group(1)] = (ftext, row, col)
                    elif si.group(1) in masters:
                        mf, mr, mc = masters[si.group(1)]
                        ftext = _shift(mf, row - mr, col - mc)
                if 't="array"' in fattrs or not ftext:
                    ftext = None
                if ftext:
                    formulas[(name, col, row)] = ftext
                    continue
            v = re.search(r'<v>(.*?)</v>', body, re.S)
            t = re.search(r'\bt="([^"]+)"', attrs)
            kind = t.group(1) if t else 'n'
            if kind == 'inlineStr':
                val = _unxml(''.join(re.findall(r'<t[^>]*>(.*?)</t>', body, re.S)))
            elif v is None:
                continue
            elif kind == 's':
                val = shared[int(v.group(1))]
            elif kind in ('str', 'e'):
                val = _unxml(v.group(1))
            elif kind == 'b':
                val = v.group(1) == '1'
            else:
                try:
                    val = float(v.group(1))
                except ValueError:
                    continue
            cells[(name, col, row)] = val
    if not formulas:
        return raw
    book = _Book(cells, formulas)
    path_of = dict(sheets)
    out_xml = {}
    for name, path in sheets:
        xml = xml_by_path[path]
        def fix(m, name=name):
            attrs, body = m.group(1), m.group(2) or ''
            r = re.search(r'\br="([A-Z]+)(\d+)"', attrs)
            key = (name, _col(r.group(1)), int(r.group(2))) if r else None
            if key not in formulas:
                return m.group(0)
            try:
                value = book.result(key)
            except Unsupported:
                return m.group(0)
            ftag = re.search(r'<f\b[^>]*?(?:/>|>.*?</f>)', body, re.S).group(0)
            attrs2 = re.sub(r'\s*\bt="[^"]*"', '', attrs)
            if isinstance(value, bool):
                return f'<c{attrs2} t="b">{ftag}<v>{1 if value else 0}</v></c>'
            if isinstance(value, (int, float)):
                v = int(value) if float(value).is_integer() and abs(value) < 1e15 else float(f'{value:.15g}')
                return f'<c{attrs2}>{ftag}<v>{v}</v></c>'
            if value is None:
                return f'<c{attrs2}>{ftag}</c>'
            return f'<c{attrs2} t="str">{ftag}<v>{_xml(str(value))}</v></c>'
        new = CELL.sub(fix, xml)
        if new != xml:
            out_xml[path] = new
    if not out_xml:
        return raw
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as dst:
        for info in z.infolist():
            data = out_xml[info.filename].encode('utf-8') if info.filename in out_xml else z.read(info.filename)
            dst.writestr(info, data)
    return out.getvalue()
