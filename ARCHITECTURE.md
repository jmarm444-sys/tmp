# johnmjohna — architecture

Personal scripts at repo root: **holdings PDF → CSV** (main), **Chase order screenshots → CSV**, plus CSV outputs and handoff notes (`SESSION.md`, `notes-log.md`). There is no `src/` package layout.

## Component diagram

```mermaid
flowchart TB
  subgraph inputs["Inputs"]
    PDF_H["Holdings PDFs"]
    PDF_O["Chase order screenshots PDF"]
  end

  subgraph stack["Runtime stack"]
    PyPDF["pypdf"]
    PDF2IMG["pdf2image"]
    OCR["pytesseract + PIL"]
    YF["yfinance\n(optional)"]
  end

  subgraph holdings["Holdings pipeline"]
    NEW["new_parse-holdings.py\nPRIMARY"]
    PARSE["parse_holdings.py"]
    PREV["prev_parse_holdings.py"]
  end

  subgraph orders["Orders pipeline"]
    OLDEXT["older-extract_orders_to_csv.py"]
  end

  subgraph debug["Debug"]
    AN["_analyze_missing.py"]
  end

  subgraph outputs["Outputs"]
    CSV_H["*-structured.csv,\n*-grouped*.csv, my-out*.csv"]
    CSV_O["orders.csv"]
  end

  subgraph docs["Notes"]
    SESS["SESSION.md"]
    NOTES["notes-log.md"]
  end

  PDF_H --> NEW
  PDF_H --> PARSE
  PDF_H --> PREV
  NEW --> PyPDF & PDF2IMG & OCR
  NEW -.-> YF
  PARSE --> PyPDF & PDF2IMG & OCR
  PREV --> PyPDF & PDF2IMG & OCR
  NEW --> CSV_H
  PARSE --> CSV_H
  PREV --> CSV_H

  PDF_O --> OLDEXT
  OLDEXT --> OCR
  OLDEXT --> CSV_O

  NEW -.->|"importlib"| AN

  SESS --- NEW
  NOTES --- SESS
```

## Sequence: holdings (`new_parse-holdings.py`)

End-to-end flow from `main()`: embedded text first, OCR if the PDF has no text, then sectioning, row parsing, optional Yahoo price reconciliation, then grouped CSV write.

```mermaid
sequenceDiagram
  participant U as User
  participant M as new_parse-holdings main()
  participant R as pypdf extract_text
  participant O as extract_text_with_ocr
  participant S as find_section_slices
  participant P as parse_rows
  participant Y as yfinance price verify
  participant W as write_grouped_csv
  participant F as Output CSV

  U->>M: CLI pdf path, flags
  M->>M: load_reference_symbols
  M->>R: extract_text(pdf)
  alt PDF has extractable text
    R-->>M: raw string
  else empty text
    M->>O: rasterize + Tesseract
    O-->>M: OCR string
  end
  M->>M: split lines, strip empties
  M->>S: find_section_slices(lines)
  S-->>M: section name + line blocks
  loop each section
    M->>P: parse_rows(section_data, refs)
    P-->>M: rows, unknown symbols
  end
  opt --price-verify
    M->>Y: reconcile tickers vs last close
    Y-->>M: symbol corrections
  end
  M->>W: write_grouped_csv(grouped_rows, out)
  W->>F: grouped CSV
  M-->>U: stdout counts / warnings
```

## Sequence: orders (`older-extract_orders_to_csv.py`)

OCR-heavy path: PDF pages to images, Tesseract text, regex extraction into tabular columns (`date`, `symbol`, side, qty, limit, TIF, status). Output shape is documented in `order-stat-cols.txt` and `SESSION.md`.

```mermaid
sequenceDiagram
  participant U as User
  participant E as older-extract_orders_to_csv
  participant I as pdf2image + PIL
  participant T as pytesseract
  participant X as regex column extract
  participant C as orders.csv

  U->>E: CLI PDF path
  E->>I: convert_from_path
  I->>T: image_to_string per page
  T-->>E: full text
  E->>X: match order lines / dates
  X->>C: csv.writer rows
```

## Related files

| File | Role |
|------|------|
| `SESSION.md` | One-page resume: commands, focus, Chase vs USB |
| `notes-log.md` | Raw log; newest sections after first `---` |
| `_analyze_missing.py` | Loads `new_parse-holdings.py` via `importlib` to debug parse failures on a fixed PDF |
