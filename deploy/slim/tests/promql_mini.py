"""A deliberately small PromQL evaluator for instant-vector status expressions.

It exists so the dashboards' state expressions can be tested without a Prometheus
binary. It supports only what those expressions use: selectors with = != =~ !~
matchers, min/max aggregation, absent(), vector(), time(), + - arithmetic,
comparison filters (== != > < >= <=), `and on()` and `or`. Anything else raises
ValueError instead of being guessed at, so an expression that grows beyond this
subset fails loudly rather than being silently mis-evaluated.

Semantics follow Prometheus: a comparison between a vector and a scalar filters the
vector; `A and on() B` keeps A only if B is non-empty; `A or B` keeps every sample of
A plus the samples of B whose label set is not in A.
"""
import json
import re

TOKEN = re.compile(r'''\s*(?:
    (?P<num>\d+(?:\.\d+)?)
  | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<op>==|!=|>=|<=|=~|!~|[-+()<>{},=])
)''', re.X)
KEYWORDS = {'and', 'or', 'on'}


def tokenize(text):
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ValueError(f'cannot tokenize at: {text[pos:pos + 20]!r}')
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


class Series(dict):
    """Instant vector, retaining selector labels for absent() inference only."""
    def __init__(self, *args, absent_labels=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.absent_labels = frozenset(absent_labels)


def vec(pairs):
    return Series({frozenset(lbls.items()): float(v) for lbls, v in pairs})


class Evaluator:
    def __init__(self, samples, now):
        # samples: {metric_name: [(labels_dict, value), ...]}
        self.samples, self.now = samples, float(now)
        self.toks, self.i = [], 0

    def evaluate(self, expr):
        self.toks, self.i = tokenize(expr), 0
        result = self.parse_or()
        if self.i != len(self.toks):
            raise ValueError(f'trailing tokens: {self.toks[self.i:]}')
        if isinstance(result, float):
            raise ValueError('expression returned a scalar')
        return {tuple(sorted(k)): v for k, v in result.items()}

    # ---- token helpers
    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self, value=None):
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise ValueError(f'expected {value!r}, got {tok!r}')
        self.i += 1
        return tok

    # ---- grammar, lowest precedence first
    def parse_or(self):
        left = self.parse_and()
        while self.peek() == ('id', 'or'):
            self.take()
            right = self.parse_and()
            left = self.set_or(left, right)
        return left

    def parse_and(self):
        left = self.parse_cmp()
        while self.peek() == ('id', 'and'):
            self.take()
            self.take('on'); self.take('('); self.take(')')
            right = self.parse_cmp()
            left = left if (self.need_vec(right) and right) else Series()
            self.need_vec(left)
        return left

    def parse_cmp(self):
        left = self.parse_add()
        if self.peek()[1] in ('==', '!=', '>', '<', '>=', '<='):
            op = self.take()[1]
            right = self.parse_add()
            return self.compare(op, left, right)
        return left

    def parse_add(self):
        left = self.parse_primary()
        while self.peek()[1] in ('+', '-'):
            op = self.take()[1]
            right = self.parse_primary()
            left = self.arith(op, left, right)
        return left

    def parse_primary(self):
        kind, value = self.take()
        if kind == 'num':
            return float(value)
        if value == '(':
            inner = self.parse_or()
            self.take(')')
            return inner
        if kind != 'id':
            raise ValueError(f'unexpected token {value!r}')
        if value in ('min', 'max'):
            self.take('(')
            arg = self.need_vec(self.parse_or())
            self.take(')')
            if not arg:
                return Series()
            fn = min if value == 'min' else max
            return Series({frozenset(): fn(arg.values())})
        if value == 'vector':
            self.take('(')
            arg = self.parse_or()
            self.take(')')
            if not isinstance(arg, float):
                raise ValueError('vector() needs a scalar')
            return Series({frozenset(): arg})
        if value == 'time':
            self.take('('); self.take(')')
            return self.now
        if value == 'absent':
            self.take('(')
            arg = self.need_vec(self.parse_or())
            self.take(')')
            return Series() if arg else Series({arg.absent_labels: 1.0})
        if value in KEYWORDS:
            raise ValueError(f'unexpected keyword {value!r}')
        return self.selector(value)

    # ---- evaluation helpers
    def selector(self, name):
        matchers = []
        if self.peek()[1] == '{':
            self.take('{')
            while self.peek()[1] != '}':
                label = self.take()[1]
                if label == "__name__":
                    raise ValueError("metric name cannot be set twice in a named selector")
                op = self.take()[1]
                raw = self.take()[1]
                matchers.append((label, op, json.loads(raw)))
                if self.peek()[1] == ',':
                    self.take()
            self.take('}')
        inferred, seen = {}, set()
        for label, op, value in matchers:
            if label in seen:
                inferred.pop(label, None)
            elif label != '__name__' and op == '=':
                inferred[label] = value
            seen.add(label)
        out = Series(absent_labels=inferred.items())
        for labels, value in self.samples.get(name, []):
            if all(self.match(labels.get(l, ''), op, v) for l, op, v in matchers):
                # Prometheus drops the metric name from results only after operators;
                # these expressions always aggregate or filter, so keep labels as given.
                out[frozenset(labels.items())] = float(value)
        return out

    @staticmethod
    def match(actual, op, expected):
        if op == '=':
            return actual == expected
        if op == '!=':
            return actual != expected
        if op == '=~':
            return re.fullmatch(expected, actual) is not None
        if op == '!~':
            return re.fullmatch(expected, actual) is None
        raise ValueError(op)

    @staticmethod
    def need_vec(value):
        if isinstance(value, float):
            raise ValueError('expected a vector')
        return value

    @staticmethod
    def set_or(left, right):
        if isinstance(left, float) or isinstance(right, float):
            raise ValueError('or needs vectors')
        merged = Series(left)
        for k, v in right.items():
            merged.setdefault(k, v)
        return merged

    @staticmethod
    def arith(op, left, right):
        sign = 1 if op == '+' else -1
        if isinstance(left, float) and isinstance(right, float):
            return left + sign * right
        if isinstance(left, float):
            return Series({k: left + sign * v for k, v in right.items()})
        if isinstance(right, float):
            return Series({k: v + sign * right for k, v in left.items()})
        raise ValueError('vector-vector arithmetic is outside the supported subset')

    @staticmethod
    def compare(op, left, right):
        fns = {'==': lambda a, b: a == b, '!=': lambda a, b: a != b, '>': lambda a, b: a > b,
               '<': lambda a, b: a < b, '>=': lambda a, b: a >= b, '<=': lambda a, b: a <= b}
        if isinstance(right, float) and not isinstance(left, float):
            return Series({k: v for k, v in left.items() if fns[op](v, right)})
        raise ValueError('only vector-op-scalar comparisons are supported')
