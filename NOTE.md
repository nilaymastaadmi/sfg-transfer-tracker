# SFG Transfer Tracker: design note

Nilay Toshniwal, BITS Pilani Goa. Indus International AI assignment, 14 Nov 2026.  
Live demo: <https://mastaadmi.pythonanywhere.com> · Code: <https://github.com/nilaymastaadmi/sfg-transfer-tracker>

**The problem as I read it.** Headers and Sheet Metal parts move between three plants with no record. When one goes missing, it's one plant's word against the other's, and the build waits. So the thing to build is a record both plants trust, one that settles "we sent it" against "we never got it" in seconds.

### Assumptions

- Three plants (1100 Headers, 1200 Sheet Metal, 1300 Coil Assembly). Codes and parts are dummy but shaped like SAP data.
- Every movement belongs to a job order. Parts go from the plant that makes them to the job order's assembly plant, never more than it still needs; the app enforces both.
- Someone at each end has a phone or PC. A paper slip still travels with the parts. Quantities are in pieces.

### Key decisions and trade-offs

1. **Append-only trail.** Every action writes an event in the same transaction as the change. The database blocks edits and deletes on events, each carries a hash of the one before, and the trail is replayed against the working tables, so a casual database edit gets flagged. Someone with full file access could still rebuild it; a daily hash sent outside the system would close that. Trade-off: mistakes are fixed with a new event, never an edit.
2. **Blind count at the dock.** The receiver doesn't see the sent quantity on the receive screen or the slip, so they have to count. Any difference becomes a discrepancy both plants see until it's settled. Trade-off: a few seconds per receipt.
3. **Late parts get flagged.** Anything not received in 4 hours (a setting, my guess) goes red, either plant can flag "not received", and *Settle a dispute* gives a plain verdict: who sent what, who confirmed it, who last had the parts.
4. **Fast enough that people use it.** Send is prefilled from the job order with what the plant still owes; the slip's QR code opens the receive screen.
5. **Job order link through a file.** `job_orders.json` stands in for the job order system's export. The tracker answers back, per job order, what has arrived and what is still missing, through an API.
6. **Shaped for SAP, connected later.** SAP field names; a send maps to movement type 303 and a receipt to 305, exported as a goods-movement CSV, with slots for the SAP document numbers. Movement types to confirm with the SAP team (details in the README).
7. **Plain stack on purpose.** Python, Flask and SQLite, one command to run. Postgres later changes the database layer, not the rules.

### Where AI fits, and where it doesn't

The record settles disputes, so it stays rule-based and exact. AI belongs at the edges: a photo of today's paper delivery note or a WhatsApp message turned into a draft send, a daily exceptions summary for plant heads, and reading discrepancy notes for repeat causes, 8D style. I built this with AI coding tools; I directed the design and checked it, and 20 automated tests cover the core rules.

### Shortcuts I took

- No login: people type their name. Production would use plant logins or badge scans.
- Sample data, including two weeks of past transfers so the Insights page has something to measure.
- No returns or partial-receipt flow; a mismatch is recorded and settled with a note.

### What I'd do next: pilot it and prove it

1. Week 0: walk one route (Sheet Metal to Coil Assembly) with the people who move the parts, and count disputes per week and hours lost by hand. That's the baseline.
2. Weeks 1 and 2: pilot on that route only, then compare on the Insights page: % received within 4 hours, transit time, discrepancy rate, time to settle.
3. Then logins and phone scanning, overdue alerts, the live job order link, and posting 303/305 to SAP.
