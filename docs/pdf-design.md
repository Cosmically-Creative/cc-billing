# PDF design spec (from Prism's Penpot file "Billing App Design v0.2", page "02 PDF Templates")

Read from Penpot on 2026-11-12. All names, amounts and links in the design are SAMPLE data.
Renderer: WeasyPrint (no JavaScript, no box-shadow/text-shadow, no filters or blend modes).

## Page
- US Letter, 816 × 1056 px at 96 dpi (`@page { size: Letter; margin: 0 }`, content padded 48 px left/right).
- Font: Rubik (embedded with @font-face; weights 400, 500, 600). Sizes 10–30 px.
- Multi-page: page 1 keeps the full header; later pages a short header (number + "continued").
  The table header row repeats (`thead { display: table-header-group }`), rows `break-inside: avoid`.
  Footer "Page X of Y" (bold) via `counter(page)` / `counter(pages)` in `@bottom-right`.
  Totals, pay box and check details only on the last page. Page 1 ends with "Subtotal so far".
  **No "Continued on page 2" line** (Blake, spec v0.7).

## Colors (tokens)
blue #2D54C7 · blue-dark #1F3A89 · blue-50 #E6EEFF · yellow #FFE642 · navy (text.primary) #0B1532 ·
text.secondary #5A6478 · neutral-400 #8A93A8 · neutral-25 #F7F8FB · border #E4E8F0 · border-strong #CDD3DF.

## Invoice layout (y positions in px from the top of page 1)
| Block | Position / size | Content and style |
|---|---|---|
| Header | 0–128, white | Logo v11 at x40 y16, 160×101. Right-aligned at x476 w292: "INVOICE" 12/600 blue (y34); number 30/600 navy (y52); "email · phone" 11/400 secondary (y96, from x400 w368) |
| Rules | blue 4 px at y128, yellow 4 px at y132 | full width |
| From | x48 y168 | label "FROM" 10/600 secondary; then 12/400 navy, 5 lines: name, address line, city/state/zip, email, phone |
| Bill to | x288 y168 | label "BILL TO"; customer name, contact, address lines (from the customer snapshot) |
| Meta box | x528 y160, 240×112, fill #F7F8FB, 1 px #E4E8F0, radius 6 | rows every 24 px from y168: label 12/400 secondary left (x544), value 12/500 navy right (to x752): Issue date, Due date, Terms (Net 30), Invoice no. |
| Table header | x48 y300, 720×30, fill #E6EEFF, radius 4 | 10/600 blue-dark caps: DESCRIPTION (x60 w380) · QTY (right, x440 w56) · RATE (right, x500 w96) · TAX (center, x604 w48) · AMOUNT (right, x656 w100) |
| Line rows | from y340, 52 px pitch, 1 px #E4E8F0 rule under each | description 12/500 navy + optional detail line 11/400 secondary; qty 12/400; rate 12/400; tax mark "T" 12/600 navy or "–" 12/400 #8A93A8; amount 12/500 |
| Tax footnote | x48 below table, w420 | 10/400 secondary: "T = taxable: Iowa 6% state tax plus the local tax where the client is. Hosting, domains and platform renewals are not taxed." |
| Totals | label x488, value right to x756, 22 px pitch | Subtotal · Taxable amount · tax label (document.tax_label, e.g. "Sales tax (7%, Iowa + Hull)") · small gray split line 10/400 #8A93A8 "Iowa 6% $90.00 · Hull 1% $15.00" (only if local tax > 0) · 1 px #CDD3DF rule · **Total** 13/600 · Amount paid 12/400 with payment detail 10/400 gray ("Check #1042, Jan 30, 2027"), value shown negative "–$500.00" |
| Balance due | x476, 292×44, fill #E6EEFF, 1.5 px blue, radius 6 | "BALANCE DUE" 11/600 blue-dark left; amount 18/600 navy right |
| Pay online | x48 y772, 720×128, white, 1 px blue, radius 8, 6 px blue bar on the left | "Pay online" 16/600; "Pay all or part of the balance by card or bank transfer (ACH), securely through Stripe." 12/400 secondary; pay link 14/600 blue; "Type the link, or scan the code." 11/400 gray; QR code 100×100 at the right (generated). Hidden when nothing can be paid online (balance under $0.50, donated, void) |
| Pay by check | x48 y920 | label "PAY BY CHECK"; "Payable to Cosmically Creative. Mail to <address>, with <number> on the memo line." 11/400 |
| Notes | x408 y920 w360 | label "NOTES"; document notes 11/400 |
| Late fee line | x48 y976 | only when late fees are on: "A late fee of 2% per month applies to balances unpaid after the due date." 10/400 gray |
| Footer | rule y1008; text y1018 10/400 gray | left: "Cosmically Creative · address · phone"; right: "<number> · Page X of Y" |

## Variants
- **Quote:** label "QUOTE"; "Valid until" replaces Due date; an Accept area replaces Pay online; no paid/balance rows.
- **Credit note:** label "CREDIT NOTE"; amounts negative; tax credited at the original rates; meta box and an
  "Applied to" box link the original invoice; no pay box.
- **Stamps** (PAID copy, VOID): outline only, rotated −12°, 90% opacity, placed in empty space, never over numbers.
  PAID: paid-in-full date + list of payments, balance $0.00. VOID: void date + replacement invoice number, amount due $0.00, pay link off.
- **Grayscale:** everything must stay readable; only the decorative yellow rule fades.
