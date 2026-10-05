"""Helmod exchange strings: base64(zlib(serpent.dump(model))), read without executing Lua.

Helmod writes models with ``serpent.dump`` and Factorio's ``helpers.encode_string``
(zlib + base64). A dump is a Lua chunk ``do local _=<table>;<fixups>;return _;end``:
tables reachable twice are written once, the second place holds the string
"SERPENT PLACEHOLDER" and a fixup statement ``_.a["b"]=_.c[1]`` restores the
shared reference. The parser accepts exactly that subset.
"""

import base64
import binascii
import math
import re
import zlib
from typing import Any

MAX_DECODED = 16 * 1024 * 1024
PLACEHOLDER = 'SERPENT PLACEHOLDER'
IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
NUMBER = re.compile(r'0[xX][0-9A-Fa-f]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?')
KEYWORDS = {
    'and', 'break', 'do', 'else', 'elseif', 'end', 'false', 'for', 'function', 'goto', 'if', 'in',
    'local', 'nil', 'not', 'or', 'repeat', 'return', 'then', 'true', 'until', 'while',
}  # fmt: skip
ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', 'a': '\a', 'b': '\b', 'f': '\f', 'v': '\v', '\\': '\\', '"': '"', "'": "'"}

Lua = Any


class LuaError(ValueError):
    pass


class Parser:
    def __init__(self, text: str) -> None:
        self.s, self.i = text, 0

    def fail(self, msg: str) -> LuaError:
        return LuaError(f'{msg} at offset {self.i}: {self.s[self.i : self.i + 40]!r}')

    def ws(self) -> None:
        while self.i < len(self.s):
            if self.s[self.i].isspace():
                self.i += 1
            elif self.s.startswith('--', self.i):
                end = self.s.find('\n', self.i)
                self.i = len(self.s) if end < 0 else end + 1
            else:
                break

    def peek(self, token: str) -> bool:
        self.ws()
        return self.s.startswith(token, self.i)

    def take(self, token: str) -> None:
        if not self.peek(token):
            raise self.fail(f'expected {token!r}')
        self.i += len(token)

    def word(self) -> str | None:
        self.ws()
        m = IDENT.match(self.s, self.i)
        return m.group() if m else None

    def keyword(self, word: str) -> None:
        if self.word() != word:
            raise self.fail(f'expected {word}')
        self.i += len(word)

    def string(self) -> str:
        quote = self.s[self.i]
        self.i += 1
        out: list[str] = []
        while True:
            if self.i >= len(self.s):
                raise self.fail('unterminated string')
            c = self.s[self.i]
            if c == quote:
                self.i += 1
                return ''.join(out)
            if c == '\\':
                self.i += 1
                e = self.s[self.i]
                if e in ESCAPES:
                    out.append(ESCAPES[e])
                    self.i += 1
                elif e == '\n':
                    out.append('\n')
                    self.i += 1
                elif e.isdigit():
                    m = re.compile(r'\d{1,3}').match(self.s, self.i)
                    assert m
                    out.append(chr(int(m.group())))
                    self.i = m.end()
                else:
                    raise self.fail(f'unsupported escape \\{e}')
            else:
                out.append(c)
                self.i += 1

    def number(self) -> float | int:
        m = NUMBER.match(self.s, self.i)
        if not m:
            raise self.fail('expected a value')
        self.i = m.end()
        text = m.group()
        if text[:2] in ('0x', '0X'):
            return int(text, 16)
        value = float(text)
        return int(text) if text.isdigit() else value

    def value(self) -> Lua:
        self.ws()
        if self.i >= len(self.s):
            raise self.fail('unexpected end')
        c = self.s[self.i]
        if c in '"\'':
            return self.string()
        if c == '{':
            return self.table()
        if c == '-':
            self.i += 1
            v = self.value()
            if not isinstance(v, (int, float)):
                raise self.fail('negated non-number')
            return -v
        word = self.word()
        if word in ('true', 'false'):
            self.i += len(word)
            return word == 'true'
        if word == 'nil':
            self.i += 3
            return None
        if word == 'math':
            self.i += 4
            self.take('.')
            self.keyword('huge')
            return math.inf
        v = self.number()
        if self.peek('/'):  # serpent writes NaN as 0/0
            self.take('/')
            d = self.number()
            return math.nan if v == 0 and d == 0 else v / d
        return v

    def table(self) -> dict[Lua, Lua]:
        """A Lua table as a dict; positional entries get integer keys 1..n."""
        self.take('{')
        out: dict[Lua, Lua] = {}
        n = 0
        while not self.peek('}'):
            if self.peek('['):
                self.take('[')
                key = self.value()
                self.take(']')
                self.take('=')
                out[key] = self.value()
            else:
                save = self.i
                word = self.word()
                if word and word not in KEYWORDS:
                    self.i += len(word)
                    if self.peek('=') and not self.peek('=='):
                        self.take('=')
                        out[word] = self.value()
                    else:
                        self.i = save
                        word = None
                if word is None or word in KEYWORDS:
                    n += 1
                    v = self.value()
                    if v is not None:
                        out[n] = v
            if self.peek(',') or self.peek(';'):
                self.i += 1
            elif not self.peek('}'):
                raise self.fail('expected , or }')
        self.take('}')
        return out

    def path(self, root: dict[Lua, Lua]) -> tuple[dict[Lua, Lua], Lua]:
        """`_.a["b"][1]` -> (container, last key)."""
        self.keyword('_')
        keys: list[Lua] = []
        while True:
            if self.peek('.'):
                self.take('.')
                w = self.word()
                if not w:
                    raise self.fail('expected a field name')
                self.i += len(w)
                keys.append(w)
            elif self.peek('['):
                self.take('[')
                keys.append(self.value())
                self.take(']')
            else:
                break
        if not keys:
            raise self.fail('fixup must assign a field')
        node: Any = root
        for k in keys[:-1]:
            if not isinstance(node, dict) or k not in node:
                raise self.fail(f'fixup path {keys} missing {k!r}')
            node = node[k]
        if not isinstance(node, dict):
            raise self.fail(f'fixup path {keys} is not a table')
        return node, keys[-1]

    def chunk(self) -> dict[Lua, Lua]:
        self.ws()
        if self.peek('{'):
            root = self.table()
            self.end()
            return root
        self.keyword('do')
        self.keyword('local')
        self.keyword('_')
        self.take('=')
        root = self.value()
        if not isinstance(root, dict):
            raise self.fail('the dump must return a table')
        while True:
            if self.peek(';'):
                self.take(';')
                continue
            word = self.word()
            if word == 'return':
                self.i += len(word)
                self.keyword('_')
                if self.peek(';'):
                    self.take(';')
                self.keyword('end')
                self.end()
                return root
            if word == 'local':  # `local __={}` holds table keys, which Helmod models never use
                self.i += len(word)
                self.keyword('__')
                self.take('=')
                self.take('{')
                self.take('}')
                continue
            target, key = self.path(root)
            self.take('=')
            if self.word() == '_':
                container, k = self.path(root)
                if k not in container:
                    raise self.fail('fixup source missing')
                target[key] = container[k]
            else:
                target[key] = self.value()

    def end(self) -> None:
        self.ws()
        if self.i != len(self.s):
            raise self.fail('trailing data')


def parse_lua(text: str) -> dict[Lua, Lua]:
    """Parse a serpent dump or a bare table literal; any other Lua is rejected."""
    return Parser(text).chunk()


def decode(text: str) -> dict[Lua, Lua]:
    """A Helmod export string (or its plain serpent dump) to the model table."""
    text = text.strip()
    if not (text.startswith('do local') or text.startswith('{')):
        try:
            raw = base64.b64decode(re.sub(r'\s+', '', text), validate=True)
            inflater = zlib.decompressobj()
            data = inflater.decompress(raw, MAX_DECODED)
            if inflater.unconsumed_tail:
                raise ValueError(f'decoded data exceeds {MAX_DECODED} bytes')
        except (binascii.Error, zlib.error) as e:
            raise LuaError(f'Not a Helmod export string (base64 + zlib): {e}') from e
        text = data.decode('utf-8')
    return parse_lua(text)


def dump_value(v: Lua) -> str:
    if v is None:
        return 'nil'
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return '0/0'
        if math.isinf(v):
            return 'math.huge' if v > 0 else '-math.huge'
        return repr(int(v)) if v.is_integer() and abs(v) < 2**53 else repr(v)
    if isinstance(v, str):
        out = ['"']
        for c in v:
            if c in '"\\':
                out.append('\\' + c)
            elif c == '\n':
                out.append('\\n')
            elif c == '\r':
                out.append('\\r')
            elif ord(c) < 32 or ord(c) == 127:
                out.append(f'\\{ord(c):03d}')
            else:
                out.append(c)
        out.append('"')
        return ''.join(out)
    if isinstance(v, (list, tuple)):
        return '{' + ','.join(dump_value(x) for x in v) + '}'
    if isinstance(v, dict):
        parts = []
        for k, x in v.items():
            if x is None:
                continue
            if isinstance(k, str) and IDENT.fullmatch(k) and k not in KEYWORDS:
                parts.append(f'{k}={dump_value(x)}')
            else:
                parts.append(f'[{dump_value(k)}]={dump_value(x)}')
        return '{' + ','.join(parts) + '}'
    raise TypeError(f'Cannot write {type(v).__name__} as Lua')


def encode(model: dict[str, Lua]) -> str:
    """A Helmod model table to an export string Helmod's Download dialog reads."""
    chunk = f'do local _={dump_value(model)};return _;end'
    return base64.b64encode(zlib.compress(chunk.encode('utf-8'), 9)).decode('ascii')
