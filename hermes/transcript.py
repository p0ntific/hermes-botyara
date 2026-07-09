"""Dialog transparency CLI.

Usage:
    python -m hermes.transcript leads [--status in_dialog] [--limit 50]
    python -m hermes.transcript show <lead>
    python -m hermes.transcript queue
    python -m hermes.transcript llm [--limit 50]
"""

import os
import json
import argparse
import datetime

from dotenv import load_dotenv

from .store import Store


def _fmt_ts(ts):
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def cmd_leads(store, args):
    rows = store.list_leads(status=args.status, limit=args.limit)
    if not rows:
        print("Лидов нет.")
        return
    for r in rows:
        print(
            f"{_fmt_ts(r['timestamp'])}  @{r['lead_key']:<24} "
            f"account={r['account'] or '-':<10} status={r['status']:<16} "
            f"stage={r['last_stage'] or '-'} replies={r['reply_count']}"
        )


def cmd_show(store, args):
    lead_key = args.lead.lstrip("@")
    lead = store.get_lead(lead_key)
    if lead is None:
        print(f"Лид @{lead_key} не найден.")
        return
    print(
        f"@{lead_key} | account={lead['account'] or '-'} | status={lead['status']} | "
        f"stage={lead['last_stage'] or '-'} | action={lead['last_action'] or '-'}\n"
    )
    for m in store.get_transcript(lead_key):
        stamp = _fmt_ts(m["created_at"])
        if m["direction"] == "event":
            meta = json.loads(m["meta"]) if m["meta"] else {}
            details = ", ".join(f"{k}={v}" for k, v in meta.items() if v is not None)
            print(f"{stamp}  [решение] {details}")
        else:
            arrow = "<-" if m["direction"] == "in" else "->"
            print(f"{stamp}  {arrow} [{m['account'] or '-'}] {m['text']}")


def cmd_queue(store, args):
    rows = store.queue_snapshot(limit=args.limit)
    if not rows:
        print("Очередь пуста.")
        return
    for r in rows:
        print(
            f"{_fmt_ts(r['enqueued_at'])}  @{r['lead_key']:<24} status={r['status']:<10} "
            f"attempts={r['attempts']} account={r['assigned_account'] or '-'} "
            f"error={r['last_error'] or '-'}"
        )


def cmd_llm(store, args):
    rows = store.recent_llm_calls(limit=args.limit)
    if not rows:
        print("LLM-вызовов еще не было.")
        return
    for r in rows:
        status = "ok" if r["ok"] else "FAIL"
        print(
            f"{_fmt_ts(r['created_at'])}  {r['task']:<9} {r['provider']}:{r['model']:<40} "
            f"{status:<4} {r['latency_ms']}ms {('- ' + r['error']) if r['error'] else ''}"
        )


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Hermes dialog transparency")
    parser.add_argument("--db", default=os.getenv("HERMES_DB", "hermes.db"))
    sub = parser.add_subparsers(dest="command", required=True)

    p_leads = sub.add_parser("leads", help="список лидов")
    p_leads.add_argument("--status")
    p_leads.add_argument("--limit", type=int, default=50)

    p_show = sub.add_parser("show", help="полный транскрипт диалога с лидом")
    p_show.add_argument("lead")

    p_queue = sub.add_parser("queue", help="состояние общей очереди")
    p_queue.add_argument("--limit", type=int, default=50)

    p_llm = sub.add_parser("llm", help="последние LLM-вызовы (модель, латентность, ошибки)")
    p_llm.add_argument("--limit", type=int, default=50)

    args = parser.parse_args()
    store = Store(args.db)
    try:
        {"leads": cmd_leads, "show": cmd_show, "queue": cmd_queue, "llm": cmd_llm}[args.command](store, args)
    finally:
        store.close()


if __name__ == "__main__":
    main()
