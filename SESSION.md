# Session handoff

Open this file at the start of a new Cursor session to resume context.

## Project focus

- **Diagrams:** see [ARCHITECTURE.md](ARCHITECTURE.md) (component + sequence charts).
- **US Bank / brokerage holdings:** PDF → CSV using `new_parse-holdings.py` (grouped sections: STOCKS, ETFS, MUTUAL FUNDS, UNITS).
- **Raw work log:** `notes-log.md` (**newest entries at the top** after the first `---`).
- **Recent example output:** `my-out1.csv` (from `new-next-4-29-holdings.pdf` + `--price-verify`). Default output pattern: `<pdf-stem>-structured.csv`.

## Useful commands

```bash
cd "c:\Users\oscar\OneDrive\Documentos\johnmjohna"

# Slower, often better OCR on textless PDFs
python new_parse-holdings.py "new-next-4-29-holdings.pdf" "my-out1.csv" --price-verify --ocr-accurate

# Same run with explicit OCR tunables instead of --ocr-accurate
python new_parse-holdings.py "new-next-4-29-holdings.pdf" "my-out1.csv" --price-verify --ocr-dpi 600 --ocr-upscale 1.2 --ocr-psm 4

# Tunables: --ocr-dpi, --ocr-psm, --ocr-upscale, --no-ocr-preprocess
```

## Separate thread (not this handoff)

- Chase **order status** screenshots → `extract_orders_to_csv.py` / `orders.csv` (different workflow).

## When you continue

1. Read the newest block in `notes-log.md`.
2. Update this `SESSION.md` if the goal or key files change.
