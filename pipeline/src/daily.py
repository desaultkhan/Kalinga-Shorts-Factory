"""
Queue helpers (used by kalinga.py).

queue.csv lives in the channel folder (channels/<name>/queue.csv).
Format:  TOPIC,status,date,concept,show   (status: pending|done|failed;
concept = the creative concept used, recorded so the next video doesn't repeat
it; the trailing `show` column is unused in this build and kept blank)

The active entry point is `kalinga.py ship`, which picks the next pending row.
"""
from __future__ import annotations
import csv
import sys
from datetime import date

import config


def load_queue():
    with config.channel().queue.open() as f:
        return [row for row in csv.reader(f) if row]


def save_queue(rows):
    with config.channel().queue.open("w", newline="") as f:
        csv.writer(f).writerows(rows)


def next_pending(rows):
    for row in rows:
        if len(row) < 2 or row[1].strip() == "pending":
            return row
    return None


def mark(topic: str, status: str):
    topic = config.channel().normalize_topic(topic)
    rows = load_queue()
    for row in rows:
        if config.channel().normalize_topic(row[0]) == topic:
            while len(row) < 3:               # keep col 3 (concept) intact
                row.append("")
            row[1], row[2] = status, date.today().isoformat()
            save_queue(rows)
            print(f"{topic} marked {status}")
            return
    print(f"{topic} not in queue", file=sys.stderr)


def queue_topic(topic: str, show: str = "") -> bool:
    """Append one PENDING row (tagged with its show, col 5) unless the topic
    is already queued. Returns True when added."""
    ch = config.channel()
    rows = load_queue()
    t = ch.normalize_topic(topic)
    if any(ch.normalize_topic(r[0]) == t for r in rows if r):
        return False
    rows.append([t, "pending", "", "", show or ""])
    save_queue(rows)
    return True


def set_concept(topic: str, concept: str) -> None:
    """Record the creative concept used for a topic in its queue row (col 4).
    Appends a row if the topic isn't in the queue (an ad-hoc make)."""
    topic = config.channel().normalize_topic(topic)
    rows = load_queue()
    for row in rows:
        if config.channel().normalize_topic(row[0]) == topic:
            while len(row) < 4:
                row.append("")
            row[3] = (concept or "").replace("\n", " ").strip()
            save_queue(rows)
            return
    rows.append([topic, "pending", "",
                 (concept or "").replace("\n", " ").strip()])
    save_queue(rows)


def concepts(exclude: str = None):
    """[(topic, concept)] for every queue row that has a concept recorded —
    what's already been done, so the next concept doesn't repeat it. `exclude`
    drops that topic (the one we're suggesting for)."""
    ex = config.channel().normalize_topic(exclude) if exclude else None
    out = []
    for row in load_queue():
        if len(row) >= 4 and row[3].strip():
            t = row[0].strip()
            if ex and config.channel().normalize_topic(t) == ex:
                continue
            out.append((t, row[3].strip()))
    return out


def pick() -> "str | None":
    """The next pending topic in the queue (None if the queue is empty). The
    real entry point is `kalinga.py ship`, which uses this."""
    row = next_pending(load_queue())
    if not row:
        print("queue empty — add topics to the channel's queue.csv")
        return None
    return row[0].strip()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pick":
        t = pick()
        if t:
            print(f"NEXT={t}")
        sys.exit(0 if t else 1)
    if len(sys.argv) > 1 and sys.argv[1] == "mark":
        sys.exit(mark(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "done"))
    print("usage: daily.py pick | mark <topic> [status]")
