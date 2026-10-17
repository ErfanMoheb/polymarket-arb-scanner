#!/usr/bin/env python3
"""
Polymarket multi-outcome arbitrage scanner + paper trader.
PAPER ONLY: this script never places real orders and never needs a wallet or key.

What it looks for
-----------------
In mutually exclusive ("negRisk") events like "Who will win X?", exactly one
outcome resolves YES. That creates two guaranteed-payout bundles:

  YES set: buy YES on every open outcome -> pays exactly $1 per set.
           Profitable if the total cost is below $1.
  NO set:  buy NO on every open outcome  -> pays exactly $(N-1) per set.
           Profitable if the total cost is below $(N-1).
           (Still pays $N if somehow nothing wins, so this side is the safer one.)

It walks real order-book depth, so the size it reports is liquidity that
actually exists, and it shows days until resolution plus an annualized return,
because your money is locked until the event settles.

Usage (Python 3.9+, no installs needed)
-----
  python3 polymarket_arb_scanner.py                          # one scan
  python3 polymarket_arb_scanner.py --loop 120               # rescan every 2 min
  python3 polymarket_arb_scanner.py --paper --loop 120       # $300 paper, events ending within 30 days
  python3 polymarket_arb_scanner.py --paper --out-dir run1   # keep each run's files in its own folder
  python3 polymarket_arb_scanner.py --help                   # all options

Files it writes (in --out-dir, default: the current folder)
-----
  opportunities.csv     every opportunity found, one row per scan; first_seen shows
                        when that event+side's current open streak began
  scans.csv             one row per completed scan (so gaps in the above mean "closed")
  opportunity_tracking.json  state behind first_seen
  paper_portfolio.json  paper cash, open paper positions, realized profit
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (paper-arb-scanner)", "Accept": "application/json"}
OPP_LOG = "opportunities.csv"
PAPER_FILE = "paper_portfolio.json"
SCAN_LOG = "scans.csv"
TRACK_FILE = "opportunity_tracking.json"


# ---------------------------------------------------------------- HTTP helpers

def _request(url, data=None, retries=3):
    body, headers = None, dict(HEADERS)
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise
        except (OSError, ValueError) as e:
            # OSError covers URLError, socket timeouts and dropped connections during
            # read(); ValueError covers a truncated or non-JSON body.
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Request failed after {retries} tries: {url} ({last_err})")


def get(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _request(url)


def as_list(v):
    """Gamma returns some list fields as JSON strings, e.g. '["Yes","No"]'."""
    if v is None:
        return []
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return []
    return list(v)


def fnum(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def days_until(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max((dt - datetime.now(timezone.utc)).total_seconds() / 86400, 0.0)
    except ValueError:
        return None


# ---------------------------------------------------------------- Data fetching

def fetch_events(max_events, max_days=None, page=100):
    """Gamma rejects offset > 2000, so page with /events/keyset and its next_cursor.
    max_days lets the server drop events that end too far out."""
    out, cursor = [], None
    base = {"active": "true", "closed": "false", "archived": "false", "limit": page}
    if max_days is not None:
        base["end_date_max"] = (datetime.now(timezone.utc) + timedelta(days=max_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    while len(out) < max_events:
        params = dict(base)
        if cursor:
            params["after_cursor"] = cursor
        res = get(f"{GAMMA}/events/keyset", params)
        batch = res.get("events") or [] if isinstance(res, dict) else res
        if not batch:
            break
        out.extend(batch)
        cursor = res.get("next_cursor") if isinstance(res, dict) else None
        if not cursor or len(batch) < page:
            break
        time.sleep(0.05)
    return out[:max_events]


def _fetch_chunk(part):
    """One /books batch call, falling back to one-by-one if the batch fails."""
    books = {}
    try:
        for b in _request(f"{CLOB}/books", data=[{"token_id": t} for t in part]) or []:
            books[str(b.get("asset_id"))] = b
    except Exception:
        for t in part:
            try:
                books[t] = get(f"{CLOB}/book", {"token_id": t})
            except Exception:
                pass
            time.sleep(0.1)
    return books


def fetch_books(token_ids, chunk=50, workers=6):
    """Order books keyed by token id, fetched a few batches at a time."""
    ids = list(dict.fromkeys(token_ids))
    parts = [ids[i:i + chunk] for i in range(0, len(ids), chunk)]
    books = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(_fetch_chunk, parts):
            books.update(res)
    return books


def asks_of(book):
    """Ask levels sorted cheapest first, as (price, size)."""
    levels = [(fnum(a.get("price")), fnum(a.get("size"))) for a in (book or {}).get("asks", [])]
    return sorted(x for x in levels if 0 < x[0] < 1 and x[1] > 0)


def no_asks_of(yes_book):
    """The NO book mirrors the YES book: a YES bid at p is a NO ask at 1-p, same size.
    Deriving it halves the number of books we need to fetch."""
    levels = [(round(1 - fnum(b.get("price")), 6), fnum(b.get("size"))) for b in (yes_book or {}).get("bids", [])]
    return sorted(x for x in levels if 0 < x[0] < 1 and x[1] > 0)


# ---------------------------------------------------------------- Core logic

def build_candidate(ev, include_augmented=False):
    """Return (legs, reason). Each leg: {'name', 'yes', 'no'} token ids for one open outcome."""
    if not (ev.get("negRisk") or ev.get("enableNegRisk")):
        return None, "not mutually exclusive"
    if ev.get("negRiskAugmented") and not include_augmented:
        return None, "augmented (new outcomes can be added)"
    legs = []
    for m in ev.get("markets") or []:
        outcomes = [str(o).strip().lower() for o in as_list(m.get("outcomes"))]
        if m.get("closed"):
            prices = [fnum(p) for p in as_list(m.get("outcomePrices"))]
            if "yes" in outcomes and len(prices) == len(outcomes) and prices[outcomes.index("yes")] >= 0.99:
                return None, "an outcome already resolved YES"
            continue  # resolved NO, drops out of the set
        if m.get("enableOrderBook") is False or m.get("acceptingOrders") is False or m.get("active") is False:
            return None, "an open outcome isn't tradeable"
        tokens = as_list(m.get("clobTokenIds"))
        if len(tokens) != 2 or len(outcomes) != 2 or "yes" not in outcomes:
            return None, "unexpected market format"
        yi = outcomes.index("yes")
        legs.append({"name": m.get("groupItemTitle") or m.get("question") or "?",
                     "yes": str(tokens[yi]), "no": str(tokens[1 - yi]), "market_id": str(m.get("id"))})
    if len(legs) < 2:
        return None, "fewer than 2 open outcomes"
    return legs, "ok"


def walk_sets(ask_books, payout, fee, min_margin, max_cost=None):
    """Buy complete sets level by level while each extra set still clears min_margin.
    ask_books: one cheapest-first [(price, size)] list per outcome. Returns (sets, cost)."""
    n = len(ask_books)
    if n == 0 or any(not b for b in ask_books):
        return 0.0, 0.0
    idx, left = [0] * n, [b[0][1] for b in ask_books]
    qty = cost = 0.0
    while True:
        unit = sum(ask_books[i][idx[i]][0] for i in range(n)) * (1 + fee)
        if unit <= 0 or payout / unit - 1 < min_margin:
            break
        take = min(left)
        if max_cost is not None:
            take = min(take, (max_cost - cost) / unit)
        if take <= 1e-9:
            break
        qty += take
        cost += take * unit
        exhausted = False
        for i in range(n):
            left[i] -= take
            if left[i] <= 1e-9:
                idx[i] += 1
                if idx[i] >= len(ask_books[i]):
                    exhausted = True
                    break
                left[i] = ask_books[i][idx[i]][1]
        if exhausted:
            break
    return qty, cost


def evaluate(ev, legs, books, args, max_cost):
    """Returns (opportunities that pass every filter, count found but filtered out)."""
    opps, rejected, n = [], 0, len(legs)
    days = days_until(ev.get("endDate"))
    for side, payout in (("YES", 1.0), ("NO", float(n - 1))):
        if side == "YES":
            asks = [asks_of(books.get(leg["yes"])) for leg in legs]
        else:
            asks = [no_asks_of(books.get(leg["yes"])) for leg in legs]
        if any(not a for a in asks):
            continue
        qty, cost = walk_sets(asks, payout, args.fee, args.min_margin, max_cost)
        if qty < args.min_shares or cost <= 0:
            continue
        profit = qty * payout - cost
        margin = profit / cost
        ann = None
        if days is not None:
            ann = min((1 + margin) ** (365 / max(days, 1.0)) - 1, 99.99)
        if ann is None or ann * 100 < args.min_annualized or days > args.max_days:
            rejected += 1
            continue
        opps.append({
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "first_seen": None,  # filled in by track_first_seen
            "event_id": str(ev.get("id")), "title": ev.get("title") or "?",
            "slug": ev.get("slug") or "", "side": side, "outcomes": n,
            "sets": round(qty, 2), "cost": round(cost, 2), "payout": round(qty * payout, 2),
            "profit": round(profit, 2), "margin_pct": round(margin * 100, 2),
            "end_date": ev.get("endDate") or "",
            "days_to_end": round(days, 1), "annualized_pct": round(ann * 100, 1),
            "_market_ids": [leg["market_id"] for leg in legs],  # for settlement; not written to CSV
        })
    return opps, rejected


def scan(args, max_cost):
    events = fetch_events(args.max_events, args.max_days)
    cands, skipped = [], {}
    for ev in events:
        legs, why = build_candidate(ev, args.include_augmented)
        days = days_until(ev.get("endDate"))
        if legs is not None and days is None:
            legs, why = None, "unknown end date"
        elif legs is not None and days <= 0 and not args.include_overdue:
            # Overdue events have no known lock-up; treating them as "ends in a day"
            # made tiny gaps look like huge annualized returns.
            legs, why = None, "end date already passed (overdue or in play)"
        elif legs is not None and days > args.max_days:
            legs, why = None, f"ends more than {args.max_days:g} days out"
        if legs is None:
            skipped[why] = skipped.get(why, 0) + 1
        else:
            cands.append((ev, legs))
    books = fetch_books([leg["yes"] for _, legs in cands for leg in legs])
    opps, rejected = [], 0
    for ev, legs in cands:
        found, rej = evaluate(ev, legs, books, args, max_cost)
        opps += found
        rejected += rej
    if rejected:
        skipped["gap found but below --min-annualized"] = rejected
    opps.sort(key=lambda o: -o["annualized_pct"])
    return opps, len(events), len(cands), skipped


def track_first_seen(opps, scan_time):
    """Set first_seen to when this event+side's current streak began. A streak ends as soon as
    one completed scan doesn't show it, so a gap that closes and reopens starts fresh."""
    state = {}
    if os.path.exists(TRACK_FILE):
        try:
            with open(TRACK_FILE) as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            state = {}
    prev_scan, prev_open = state.get("last_scan"), state.get("open", {})
    now_open = {}
    for o in opps:
        key = f"{o['event_id']}|{o['side']}"
        prev = prev_open.get(key)
        first = prev["first_seen"] if prev and prev.get("last_seen") == prev_scan else scan_time
        o["first_seen"] = first
        now_open[key] = {"first_seen": first, "last_seen": scan_time}
    with open(TRACK_FILE, "w") as f:
        json.dump({"last_scan": scan_time, "open": now_open}, f, indent=2)


# ---------------------------------------------------------------- Output

def log_opps(opps):
    if not opps:
        return
    new = not os.path.exists(OPP_LOG)
    with open(OPP_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[k for k in opps[0] if not k.startswith("_")], extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(opps)


def log_scan(scan_time, n_events, n_cands, n_opps):
    """One row per completed scan, so an opportunity missing from a scan means it was gone."""
    new = not os.path.exists(SCAN_LOG)
    with open(SCAN_LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "events", "candidates", "opportunities"])
        w.writerow([scan_time, n_events, n_cands, n_opps])


def print_opps(opps, n_events, n_cands, skipped, args):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n=== {stamp} | {n_events} events scanned, {n_cands} mutually exclusive checked ===")
    print(f"Filters: min margin {args.min_margin * 100:.1f}%, min {args.min_annualized:g}%/yr, "
          f"ends within {args.max_days:g} days, fee {args.fee * 100:.2f}%, min {args.min_shares:g} sets")
    if skipped:
        print("Skipped: " + ", ".join(f"{v} {k}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])))
    if not opps:
        print("No opportunities right now. That's normal; rescan later.")
        return
    for k, o in enumerate(opps[:args.show], 1):
        print(f"\n{k:>2}. {o['title'][:70]}")
        print(f"    BUY ALL {o['side']} across {o['outcomes']} outcomes")
        print(f"    {o['sets']:g} sets: cost ${o['cost']:,.2f} -> payout ${o['payout']:,.2f} "
              f"= +${o['profit']:,.2f} ({o['margin_pct']:g}%)")
        print(f"    ends in ~{o['days_to_end']:g} days (~{o['annualized_pct']:g}%/yr) | "
              f"open since {o['first_seen']} UTC")
        if o["slug"]:
            print(f"    https://polymarket.com/event/{o['slug']}")
    print("\nBefore trusting any line above: open the event and read its rules to confirm "
          "exactly one outcome resolves YES.")


# ---------------------------------------------------------------- Paper trading

def load_paper(bankroll):
    if os.path.exists(PAPER_FILE):
        with open(PAPER_FILE) as f:
            return json.load(f)
    return {"start": bankroll, "cash": bankroll, "realized": 0.0, "savings_realized": 0.0, "positions": []}


def save_paper(p):
    with open(PAPER_FILE, "w") as f:
        json.dump(p, f, indent=2)


def _days_between(a, b):
    """Days from ISO time a to ISO time b (either may be 'YYYY-MM-DD HH:MM:SS' or ISO with Z)."""
    try:
        da, db = (datetime.fromisoformat(str(x).replace("Z", "+00:00").replace(" ", "T")) for x in (a, b))
        da, db = (d if d.tzinfo else d.replace(tzinfo=timezone.utc) for d in (da, db))
        return max((db - da).total_seconds() / 86400, 0.0)
    except (TypeError, ValueError):
        return None


def savings_interest(cost, days, rate):
    return cost * ((1 + rate) ** (days / 365) - 1)


def actual_payout(pos, ev):
    """What the position really paid, from each bought market's final price, or None if any
    of them hasn't resolved yet. A YES set pays nothing if no outcome resolves YES; a
    50-50 resolution pays half per share."""
    if not isinstance(ev, dict):
        return None
    markets = {str(m.get("id")): m for m in ev.get("markets") or []}
    ids = pos.get("market_ids")
    legacy = not ids
    if legacy:  # positions opened before market ids were recorded
        if not ev.get("closed"):
            return None
        ids = list(markets)
    total = 0.0
    for mid in ids:
        m = markets.get(mid)
        if m is None or not m.get("closed"):
            return None
        outcomes = [str(o).strip().lower() for o in as_list(m.get("outcomes"))]
        prices = [fnum(x, None) for x in as_list(m.get("outcomePrices"))]
        side = pos["side"].lower()
        if side not in outcomes or len(prices) != len(outcomes) or prices[outcomes.index(side)] is None:
            return None
        total += prices[outcomes.index(side)]
    if legacy and pos["side"] == "NO" and pos.get("sets"):
        # Markets that had already resolved NO before the buy weren't part of the set,
        # but each shows a NO price of 1 here. A NO set over n legs pays n-1, so the
        # recorded payout tells us n and how many extra markets to take back out.
        legs = round(pos["payout"] / pos["sets"]) + 1
        total -= max(len(markets) - legs, 0)
    return round(pos["sets"] * total, 2)


def settle_paper(p, savings_rate):
    still = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for pos in p["positions"]:
        try:
            ev = get(f"{GAMMA}/events/{pos['event_id']}")
        except Exception:
            still.append(pos)
            continue
        payout = actual_payout(pos, ev)
        if payout is None:
            still.append(pos)
            continue
        p["cash"] = round(p["cash"] + payout, 2)
        p["realized"] = round(p["realized"] + payout - pos["cost"], 2)
        held = _days_between(pos["time"], now) or 0.0
        p["savings_realized"] = p.get("savings_realized", 0.0) + savings_interest(pos["cost"], held, savings_rate)
        note = "" if payout >= pos["payout"] - 0.005 else f"  (expected ${pos['payout']:.2f}: outcome didn't resolve as assumed)"
        print(f"  [paper] settled: {pos['title'][:50]}  paid ${payout:.2f} for ${pos['cost']:.2f} "
              f"= {payout - pos['cost']:+.2f}{note}")
    p["positions"] = still


def paper_trade(p, opps, max_frac, min_shares):
    """Scale each opportunity down to what the paper bankroll allows. Scaling at the average
    price is conservative: the cheapest levels are bought first, so a smaller fill costs less."""
    held = {(pos["event_id"], pos["side"]) for pos in p["positions"]}
    for o in opps:
        if (o["event_id"], o["side"]) in held:
            continue
        budget = min(p["cash"], p["start"] * max_frac, o["cost"])
        if budget <= 0 or o["cost"] <= 0:
            continue
        scale = budget / o["cost"]
        sets = o["sets"] * scale
        if sets < min_shares:
            continue
        cost, payout = round(o["cost"] * scale, 2), round(o["payout"] * scale, 2)
        p["cash"] = round(p["cash"] - cost, 2)
        p["positions"].append({"event_id": o["event_id"], "title": o["title"], "side": o["side"],
                               "sets": round(sets, 2), "cost": cost, "payout": payout, "time": o["time"],
                               "end_date": o["end_date"], "market_ids": o["_market_ids"]})
        held.add((o["event_id"], o["side"]))
        print(f"  [paper] bought: {o['title'][:50]} ({o['side']} set)  "
              f"${cost:.2f} -> ${payout:.2f}")


def print_paper(p, savings_rate):
    locked = sum(pos["cost"] for pos in p["positions"])
    pending = sum(pos["payout"] - pos["cost"] for pos in p["positions"])
    print(f"\n[paper] cash ${p['cash']:.2f} | locked ${locked:.2f} in {len(p['positions'])} positions | "
          f"realized +${p['realized']:.2f} | pending +${pending:.2f} | started with ${p['start']:.2f}")
    # Same dollars, same holding periods (buy -> settle, or buy -> scheduled end for open ones).
    sav_pending = sum(savings_interest(pos["cost"], _days_between(pos["time"], pos.get("end_date")) or 0.0, savings_rate)
                      for pos in p["positions"])
    sav_real = p.get("savings_realized", 0.0)
    print(f"[paper] vs {savings_rate * 100:g}% savings on the same money for the same time: "
          f"realized +${sav_real:.2f} (arb +${p['realized']:.2f}), "
          f"open positions to their end dates +${sav_pending:.2f} (arb +${pending:.2f})")
    print("[paper] Note: assumes every leg fills instantly at the shown price. Real fills are worse.")


# ---------------------------------------------------------------- Main

def main():
    ap = argparse.ArgumentParser(description="Polymarket multi-outcome arbitrage scanner (paper only).")
    ap.add_argument("--fee", type=float, default=0.0,
                    help="taker fee as a fraction of cost, e.g. 0.01 for 1 percent. Check Polymarket's current fees.")
    ap.add_argument("--min-margin", type=float, default=0.01,
                    help="minimum profit per set as a fraction, default 0.01 (1 percent)")
    ap.add_argument("--min-shares", type=float, default=5, help="ignore opportunities smaller than this many sets")
    ap.add_argument("--max-events", type=int, default=1_000_000,
                    help="cap on active events to scan (default: all of them)")
    ap.add_argument("--min-annualized", type=float, default=10.0,
                    help="skip opportunities returning less than this many percent per year, "
                         "or with no known end date (default 10)")
    ap.add_argument("--max-days", type=float, default=30.0,
                    help="skip events that resolve more than this many days from now (default 30)")
    ap.add_argument("--include-overdue", action="store_true",
                    help="also check events whose end date has already passed (lock-up unknown, "
                         "annualized return is unreliable)")
    ap.add_argument("--savings-rate", type=float, default=0.04,
                    help="savings-account rate the paper summary compares against (default 0.04 = 4 percent)")
    ap.add_argument("--max-cost", type=float, default=None, help="cap the dollar size shown per opportunity")
    ap.add_argument("--show", type=int, default=15, help="how many opportunities to print")
    ap.add_argument("--include-augmented", action="store_true",
                    help="also check events where new outcomes can still be added (riskier)")
    ap.add_argument("--loop", type=int, default=0, help="rescan every N seconds (0 = scan once)")
    ap.add_argument("--paper", action="store_true", help="simulate buying the opportunities with fake money")
    ap.add_argument("--bankroll", type=float, default=300.0,
                    help="starting paper money, default 300 (only used when starting a new paper portfolio)")
    ap.add_argument("--out-dir", default=".",
                    help="folder for the CSV/JSON files, so separate runs don't mix (default: current folder)")
    ap.add_argument("--max-frac", type=float, default=0.25,
                    help="max share of starting bankroll per paper trade, default 0.25")
    args = ap.parse_args()

    global OPP_LOG, PAPER_FILE, SCAN_LOG, TRACK_FILE
    os.makedirs(args.out_dir, exist_ok=True)
    OPP_LOG, PAPER_FILE, SCAN_LOG, TRACK_FILE = (
        os.path.join(args.out_dir, f) for f in (OPP_LOG, PAPER_FILE, SCAN_LOG, TRACK_FILE))

    print("Polymarket arbitrage scanner. PAPER ONLY: no real orders are placed.")
    paper = load_paper(args.bankroll) if args.paper else None
    try:
        while True:
            try:
                if paper is not None:
                    settle_paper(paper, args.savings_rate)
                # Scan at full size so the log reflects real liquidity, not leftover paper cash.
                opps, n_ev, n_c, skipped = scan(args, args.max_cost)
                scan_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                for o in opps:
                    o["time"] = scan_time
                track_first_seen(opps, scan_time)
                print_opps(opps, n_ev, n_c, skipped, args)
                log_opps(opps)
                log_scan(scan_time, n_ev, n_c, len(opps))
                if paper is not None:
                    paper_trade(paper, opps, args.max_frac, args.min_shares)
                    save_paper(paper)
                    print_paper(paper, args.savings_rate)
            except Exception as e:
                print(f"\nScan failed: {e}\nIf this keeps happening, copy this error and share it.")
            if args.loop <= 0:
                break
            print(f"\nNext scan in {args.loop}s (Ctrl+C to stop)...")
            time.sleep(args.loop)
    except KeyboardInterrupt:
        print("\nStopped.")
        if paper is not None:
            save_paper(paper)


if __name__ == "__main__":
    sys.exit(main())
