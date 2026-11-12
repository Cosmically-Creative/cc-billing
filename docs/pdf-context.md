# Template context for billing/templates/billing/pdf/document.html

All values are already formatted strings (amounts like "$1,200.00", negative "–" amounts as "-$500.00").
The template must not compute anything. Example below is SAMPLE data.

```json
{
  "kind": "invoice",                       // "invoice" | "quote" | "credit_note"
  "kind_label": "INVOICE",                 // "INVOICE" | "QUOTE" | "CREDIT NOTE"
  "number": "CC-2026-0001",
  "business": {"name": "Cosmically Creative", "address_line1": "1444 1st Ave SE", "city_state_zip": "Sioux Center, IA 51250",
               "email": "blake@cosmicallycreative.com", "phone": "(712) 578-6203"},
  "bill_to": ["Sample Bakery Co. (sample)", "123 Example St", "Hull, IA 51239"],
  "meta": [{"label": "Issue date", "value": "Dec 10, 2026"}, {"label": "Due date", "value": "Jan 9, 2027"},
           {"label": "Terms", "value": "Net 30"}, {"label": "Invoice no.", "value": "CC-2026-0001"}],
  "lines": [{"description": "Website build", "detail": "", "qty": "1", "rate": "$1,200.00", "tax_mark": "T",
             "discount": null, "amount": "$1,200.00"},
            {"description": "Web hosting, 12 months", "detail": "", "qty": "1", "rate": "$60.00", "tax_mark": "–",
             "discount": {"label": "Family discount", "amount": "-$10.00"}, "amount": "$60.00"}],
  "totals": {"subtotal": "$1,260.00", "discount": "-$10.00", "taxable": "$1,200.00",
             "tax_label": "Sales tax (7%, Iowa + Hull)", "tax": "$84.00",
             "tax_split": "Iowa 6% $72.00 · Hull 1% $12.00", "show_tax": true, "total": "$1,334.00",
             "payments": [{"detail": "Check #1042, Dec 20, 2026", "amount": "-$500.00"}],
             "balance_due": "$834.00", "show_balance": true},
  "donated": null,                          // or {"value": "$650.00"}: show "Donated" and "Value of work"
  "out_of_state": false,
  "pay": {"show": true, "url": "https://billing.cosmicallycreative.com/pay/SAMPLE", "url_text": "billing.cosmicallycreative.com/pay/SAMPLE",
          "qr_svg": "<svg ...>...</svg>"},      // inline SVG markup, insert with |safe
  "check_text": "Payable to Cosmically Creative. Mail to 1444 1st Ave SE, Sioux Center, IA 51250, with CC-2026-0001 on the memo line.",
  "notes": "Thank you for your business!",
  "late_fee_text": "",                      // non-empty only when late fees are on
  "footer_left": "Cosmically Creative · 1444 1st Ave SE, Sioux Center, IA 51250 · (712) 578-6203",
  "stamp": null,                            // or {"kind": "PAID"|"VOID", "date": "Dec 20, 2026", "lines": ["$500.00 · 2026-12-20"]}
  "applied_to": null,                       // credit notes: {"invoice", "invoice_total", "credit", "remaining"}
  "page_note": "",
  "fonts_dir": "file:///.../static/billing/fonts",   // contains rubik-latin-400/500/600-normal.woff
  "logo_uri": "file:///.../static/billing/cc-logo-v11.svg"
}
```
