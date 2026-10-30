# Cards and estimated rewards

## Cards

**Cards** (step 2) lists every card ending your notifications state, for example
**DBS/POSB card ending NNNN**, scoped to the connected mailbox, the issuer named in the
notice and the four digits. The same ending at another mailbox or issuer is a different
card. A card stays **Pending** until you pick the product from the **Card** list and
**Save**; you can pick again to correct it. Each choice is kept with a time stamp and
earlier choices stay in the history. Repeated alerts do not create more entries, and
neither reprocessing nor a later Refresh changes a choice.

The list shows products already in the ledger plus the three product names supplied in
the original brief (names only, no terms). Choosing a name the ledger lacks adds that
product record, identical to the one in the bundled example. A suffix, sender or
product name never selects a product by itself, and the server rejects any product,
card or form that is not part of the connected account.

Wallets are not cards. A PayLah! wallet or mobile ending is shown only in spending as
the wallet that paid a transfer.

## Rewards

**Optimise spending** shows **Card rewards**: per chosen card, spend in the viewed
period and an **Estimate** computed from saved reward rules for the chosen product.
It is a calculation from saved facts, never an amount a bank confirmed; no
bank-confirmed reward data exists in the ledger, so none is shown.

A figure appears only when everything needed is known:

- exactly one standard rule revision for the product, effective over the dates, with
  a known date basis (`transaction_date`, or `posting_date` with a known posting date);
- known eligibility, minimum spend, caps, exclusions and rounding (the rule may say
  `not_applicable` for none);
- one earning rate that applies, in the purchase's currency, with every condition it
  depends on (merchant, MCC, channel, currency) known for the purchase;
- supported periods: calendar-month minimum spend and caps in Asia/Singapore, per
  transaction rounding.

Otherwise the card says why: **Choose card**, **No reward terms for this card**, **No
purchases**, or **Terms incomplete: ...** naming the missing term. A purchase the terms
say earns nothing contributes nothing and the card says **Terms give nothing for these
purchases**, not 0. Refunds or reversals in the period withhold the estimate because
they are not netted. Minimum spend and caps use the whole calendar month on that card,
even when only part of it is viewed. Campaign and sign-up rules are not applied.

## Known gaps

- There is no source of current bank terms in the repository; rules must come from the
  existing import or are absent. None are invented.
- Statement-cycle periods, campaign rules, tiers that depend on other cards, and
  netting of refunds are unsupported and withhold the estimate.
- Only the `From:` card source of the supported completed-payment notice yields a card
  reference. Other issuers need their own explicit contract.
