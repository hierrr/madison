"""최소 크론 매처 — 5필드(분 시 일 월 요일), `*`·목록(a,b)·범위(a-b)·간격(*/n, a-b/n) 지원. 요일 0·7=일요일."""
import datetime

_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _field(expr: str, lo: int, hi: int) -> set:
    out = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError("step")
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = int(a_s), int(b_s)
        else:
            a = b = int(part)
            if step != 1 and "/" in expr:            # "5/10" 형태 → 5부터 hi까지
                b = hi
        if a < lo or b > hi or a > b:
            raise ValueError(f"range {part}")
        out.update(range(a, b + 1, step))
    return out


def parse(expr: str) -> tuple:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron은 5필드")
    sets = tuple(_field(f, lo, hi) for f, (lo, hi) in zip(fields, _RANGES))
    dow = set(sets[4])
    if 7 in dow:
        dow.add(0)
    return sets[:4] + (dow,)


def matches(expr: str, when: datetime.datetime) -> bool:
    minute, hour, dom, month, dow = parse(expr)
    return (when.minute in minute and when.hour in hour and when.day in dom and when.month in month
            and (when.weekday() + 1) % 7 in dow)      # python 월=0 → cron 일=0


def valid(expr: str) -> bool:
    try:
        parse(expr)
        return True
    except ValueError:
        return False
