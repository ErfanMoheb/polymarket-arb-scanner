# polymarket-arb-scanner

A single-file Python scanner that looks for risk-free bundles in Polymarket's
multi-outcome markets, and a paper trader that "buys" them with fake money to see
whether the edge survives contact with real order books.

**Paper only.** It never places orders and never needs a wallet, key or account.
It only reads Polymarket's public APIs. Not financial advice.

## The idea

In a mutually exclusive ("negRisk") event such as *"Who will win X?"*, exactly one
outcome resolves YES. That gives two bundles with a fixed payout:

| Bundle  | Buy                         | Pays per set | Arbitrage if total cost is below |
|---------|-----------------------------|--------------|----------------------------------|
| YES set | YES on every open outcome   | $1           | $1                               |
| NO set  | NO on every open outcome    | $(N − 1)     | $(N − 1)                         |

The scanner:

- pages through every active event (Gamma `/events/keyset`) and keeps the
  negRisk ones whose outcomes are all still tradeable;
- skips "augmented" events, where new outcomes can be added later and break the
  guarantee, and events that are overdue or end too far out;
- batch-fetches CLOB order books and **walks real depth level by level**, so the
  reported size is liquidity that actually exists, not just the top of book;
- derives the NO book from the YES bids (a YES bid at *p* is a NO ask at 1 − *p*),
  halving the number of books it fetches;
- reports margin, days to resolution and an **annualized return**, because the
  money is locked until the event settles;
- tracks how long each gap stays open across scans;
- in `--paper` mode, buys each opportunity with a capped slice of a fake bankroll,
  settles it from the markets' final prices, and compares the result with simply
  leaving the same money in a savings account for the same time.

## Usage

Python 3.9+ and the standard library only. Nothing to install.

```bash
python3 polymarket_arb_scanner.py                              # one scan
python3 polymarket_arb_scanner.py --loop 120                   # rescan every 2 minutes
python3 polymarket_arb_scanner.py --paper --loop 120 --out-dir run1   # paper trade, $300 by default
python3 polymarket_arb_scanner.py --help                       # every option
```

Useful options: `--max-days` (lock-up limit, default 30), `--min-margin`
(default 1%), `--min-annualized` (default 10%/yr), `--fee`, `--bankroll`,
`--max-frac` (largest share of the bankroll per trade).

Each run writes `opportunities.csv`, `scans.csv`, `opportunity_tracking.json`
and, with `--paper`, `paper_portfolio.json` into `--out-dir`.

## What I found

Two paper runs in late September 2026, scanning every active event (~15,000–18,000,
of which ~4,600–4,900 were mutually exclusive):

| Run | Bankroll | Window | Scans | Scans with ≥1 gap | Settled trades | Realized profit |
|-----|----------|--------|-------|-------------------|----------------|-----------------|
| Events ending within 30 days | $300 | Sep 27 → Oct 1 | 774 | 547 (71%) | 7, all profitable | +$1.84 |
| Events ending within 180 days | $100 | Sep 27, ~16 h | 219 | 216 (99%) | 1 | +$0.23 |

At the end of the 30-day run the whole $300 was locked in 15 open positions with
another +$5.16 pending.

**Takeaways**

- Gaps do exist almost all the time, but they are small (median margin 1.5–2%) and
  thin, so the dollar profit is tiny: settled trades made between $0.07 and $0.91.
- The real cost is lock-up. Capital sits until resolution, so the bankroll fills
  up within hours and then can't take new opportunities.
- Most "big" gaps are in events with long lock-ups or rules that don't really
  guarantee exactly one winner. That's why it skips augmented events and asks you
  to read each event's rules.
- The paper results are an upper bound. They assume every leg fills instantly at
  the quoted price; in practice the book moves while you buy the other legs.

## Tests

Offline unit tests for the order-book walk, book mirroring, event filtering and
settlement:

```bash
python3 -m unittest discover tests
```

## Licence

MIT. See [LICENSE](LICENSE).
