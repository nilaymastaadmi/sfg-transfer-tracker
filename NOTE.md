# SFG Transfer Tracker: design note

Nilay Toshniwal, BITS Pilani Goa. Indus International AI assignment, 14 Nov 2026.  
Live demo: <https://mastaadmi.pythonanywhere.com> · Code: <https://github.com/nilaymastaadmi/sfg-transfer-tracker>

**The problem as I read it.** Semi-finished parts from Headers and Sheet Metal move between three plants with no record. When something goes missing, it's one plant's word against the other's, and the build waits. So the goal isn't a dashboard. It's a record both plants trust: who sent what and when, who received it, and what didn't match.

### Assumptions

- Three plants (1100 Headers, 1200 Sheet Metal, 1300 Coil Assembly) and parts can move between any two. Codes and parts are dummy but shaped like SAP data.
- Every movement belongs to a job order. Parts go from the plant that makes them to the job order's assembly plant, and never more than the job order still needs; the app enforces both. The job order system keeps owning job orders; the tracker only reads them and reports back.
- Someone at each end has a phone, tablet or PC on the plant network. A paper slip still travels with the parts.
- Quantities are in pieces. Batch numbers are optional.

### Key decisions and trade-offs

1. **The trail is the product.** Every action writes an event in the same transaction as the change itself. The database refuses edits and deletes on the event table, each event stores a hash of the one before it, and the trail page replays the log against the working tables, so a quantity or status edited directly in the database gets flagged. Someone with full database access could still rebuild the whole chain; sending the latest hash outside the system each day would close that. Trade-off: mistakes are fixed with a new event, never an edit. Slightly more work, but it's what makes the record hold up in a dispute.
2. **Blind count at receipt.** The receiver doesn't see the sent quantity, on screen or on the printed slip, so they have to count. Any difference becomes a discrepancy automatically (short, excess or damaged), and both plants see it until someone records how it was settled. Trade-off: a few extra seconds at the dock.
3. **"Sent but never received" can't stay silent.** Anything in transit for more than 4 hours (configurable) shows as overdue, and either plant can flag it as not received. If the parts turn up later, receiving them closes the flag.
4. **Job order link through a file.** `job_orders.json` stands in for an export from the job order system. The tracker answers back, per job order, how many of each SFG are required, in transit, received good at the assembly plant and still missing, through an API endpoint. Replacing the file with an API means changing one function.
5. **SAP-ready, not SAP-connected.** Fields use SAP terms (plant, material, batch, unit). A send maps to movement type 303 and a receipt to 305 (SAP's two-step plant-to-plant transfer), exported as a goods-movement CSV, and each transfer has empty slots for the SAP document numbers. If Indus uses 301 or stock transport orders instead, the export mapping changes and, for orders, the tracker also stores the order number. I'd confirm this with the SAP team first.
6. **Plain stack on purpose.** Python, Flask and SQLite, started with one command. Fine for a pilot; a three-plant rollout would move to Postgres, which changes the database layer, not the business rules.

### Shortcuts I took

- No login. People type their name. In production this would be plant logins or badge scans.
- Hosted on a free plan with sample data. Data persists in one SQLite file, both hosted and locally.
- No returns or partial-receipt flow yet. A mismatch is recorded and settled with a note.
- I built it with AI coding tools, which the brief allowed. 15 automated tests cover the core rules.

### What I'd build next

1. Walk the real route with the people who move the parts, and check where the slip and the count actually happen before changing anything.
2. QR or barcode scanning on a phone for send and receive, so nobody types part numbers.
3. Plant and role logins, and a WhatsApp or email alert when a transfer goes overdue.
4. A live link to the job order system, then posting 303/305 to SAP through its API and saving the document numbers.
5. A weekly view of transit time per route and discrepancy rate per department, to check the problem is actually shrinking.
