"""
new_parse-holdings.py — Extract grouped holdings from a brokerage PDF to CSV.

Usage:
    python new_parse-holdings.py <path/to/holdings.pdf> [output.csv]
    python new_parse-holdings.py holdings.pdf out.csv --price-verify

Optional --price-verify uses Yahoo Finance quotes (via yfinance) to reconcile
OCR ticker errors against the parsed last-price column. Install: pip install yfinance

OCR accuracy (when the PDF has no embedded text): use --ocr-accurate or raise
--ocr-dpi (e.g. 600), optional --ocr-upscale and --ocr-psm. Slower but often fewer
ticker misreads on dense brokerage screenshots.
"""

import re
import csv
import sys
import shutil
import difflib
import argparse
import time
from pathlib import Path
from pypdf import PdfReader
from PIL import Image
import pytesseract
from pdf2image import convert_from_path


# ---------------------------------------------------------------------------
# Row-block layout constants
# Each holding spans 7 consecutive lines in the extracted text:
#   i+0  symbol line   (AAPL  $192.35  +$1.20)
#   i+1  name / desc   (Apple Inc)          ← skipped
#   i+2  qty line      (100  $19,235.00  +$120.00)
#   i+3  blank / label                      ← skipped
#   i+4  cost-basis    (+$500.00  +$800.00)
#   i+5  blank / label                      ← skipped
#   i+6  portfolio %   (+0.45%)
# ---------------------------------------------------------------------------
QTY_OFFSET = 2
CG_OFFSET  = 4
PCT_OFFSET = 6
BLOCK_SIZE = 7

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
SYM_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9.\-]{0,9})\s+\$?([0-9,]+(?:\.\d+)?)\s+([+-]?\$?[0-9,]+(?:\.\d+)?)$"
)
QTY_RE  = re.compile(r"^([0-9,]+(?:\.\d+)?)\s+\$([0-9,]+(?:\.\d+)?)\s+([+-]?\$[0-9,]+(?:\.\d+)?)$")
CG_RE   = re.compile(r"^([+-]?\$[0-9,]+(?:\.\d+)?)\s+([+-]?\$[0-9,]+(?:\.\d+)?)$")
PCT_RE  = re.compile(r"^([+-]?\d+(?:\.\d+)?)%$")
ROW_RE  = re.compile(
    r"^([A-Za-z][A-Za-z0-9.\-]{0,9})\s+"                  # symbol
    r"\$([0-9]+(?:\.[0-9]+)?)\s+"                         # price
    r"([+-]?\$?[0-9]+(?:\.[0-9]+)?)\s+"                   # price change
    r"([0-9]+(?:\.[0-9]+)?)\s+"                           # quantity
    r"\$([0-9]+(?:\.[0-9]+)?)\s+"                         # market value
    r"([+-]?\$[0-9]+(?:\.[0-9]+)?)\s+"                    # day change
    r"([+-]?\$[0-9]+(?:\.[0-9]+)?)\s+"                    # cost basis
    r"([+-]?\$[0-9]+(?:\.[0-9]+)?)\s+"                    # gain/loss
    r"([+-]?\d+(?:\.\d+)?)%$"                             # portfolio pct
)

# Pattern used to find the first holding row (any ticker, not "ABEV" specifically)
FIRST_HOLDING_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}\s+\$")
SECTION_HEADINGS = ("STOCKS", "ETFS", "MUTUAL FUNDS", "UNITS")
SECTION_CANONICAL = {
    "STOCKS": "STOCKS",
    "ETFS": "ETFS",
    "MUTUALFUNDS": "MUTUAL FUNDS",
    "UNITS": "UNITS",
}

# Common OCR confusions when guessing alternate tickers for --price-verify
# Known OCR token → real ticker (applied before fuzzy reference matching).
CANONICAL_OCR_FIXUPS: dict[str, str] = {
    "AT": "AMAT",
    "AMY": "BMY",
    "QC0M": "QCOM",
    "QCCOM": "QCOM",
    "QCCOMM": "QCOM",
    "VI5N": "VISN",
    "V1SN": "VISN",
    # Common US Bank / brokerage PDF OCR swaps (sym-ocr → symbol); omit ambiguous pairs.
    "OCD": "CCJ",
    "EDX": "FDX",
    "GP": "GD",
    "JNU": "JNJ",
    "ECG": "FCG",
    "LAP": "LQD",
    "TIT": "TLT",
    "CAP": "CQP",
}

# Never auto-swap these to a different Yahoo ticker via price-verify (short / fragile OCR).
TRUSTED_PRICE_VERIFY_TICKERS: frozenset[str] = frozenset({"D", "QCOM", "VISN"})

_OCR_CHAR_SWAPS: list[tuple[str, str]] = [
    ("0", "O"), ("O", "0"),
    ("1", "I"), ("I", "1"),
    ("5", "S"), ("S", "5"),
    ("8", "B"), ("B", "8"),
    ("2", "Z"), ("Z", "2"),
    ("6", "G"), ("G", "6"),
]


def clean(value: str) -> str:
    """Strip $ and thousands commas so numbers load cleanly in Excel / pandas."""
    return value.replace("$", "").replace(",", "")


def load_reference_symbols(base_dir: Path, source_pdf: Path, current_output: Path) -> set[str]:
    symbols: set[str] = set()
    source_stem = source_pdf.stem.lower()
    seen: set[Path] = set()
    for pattern in ("*-Holdings-structured.csv", "*-holdings-structured.csv"):
        for csv_path in base_dir.glob(pattern):
            resolved = csv_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved == current_output.resolve():
                continue
            # Avoid training corrections from prior outputs of the same source PDF.
            csv_stem = csv_path.stem.lower()
            if source_stem in csv_stem:
                continue
            try:
                with csv_path.open("r", encoding="utf-8", newline="") as f:
                    reader = csv.DictReader(f)
                    if "symbol" not in (reader.fieldnames or []):
                        continue
                    for row in reader:
                        sym = (row.get("symbol") or "").strip().upper()
                        if sym:
                            symbols.add(sym)
            except Exception:
                continue
    return symbols


def sanitize_ocr_symbol(symbol: str) -> str:
    """Strip junk characters and fix frequent OCR misreads in the ticker token."""
    s = symbol.upper().strip()
    s = s.replace(":", "F")
    s = s.replace("|", "I").replace("!", "I")
    s = re.sub(r"[^A-Z0-9.\-]", "", s)
    return s


def ocr_symbol_variants(symbol: str) -> list[str]:
    """Single-edit variants for common OCR letter/digit swaps (bounded)."""
    s = sanitize_ocr_symbol(symbol)
    out: list[str] = []
    for i, ch in enumerate(s):
        for a, b in _OCR_CHAR_SWAPS:
            if ch == a:
                t = s[:i] + b + s[i + 1 :]
                if t not in out:
                    out.append(t)
            elif ch == b:
                t = s[:i] + a + s[i + 1 :]
                if t not in out:
                    out.append(t)
    return out[:24]


def normalize_symbol(symbol: str, reference_symbols: set[str]) -> str:
    s = sanitize_ocr_symbol(symbol)
    if s in CANONICAL_OCR_FIXUPS:
        s = CANONICAL_OCR_FIXUPS[s]
    if not reference_symbols or s in reference_symbols:
        return s
    # Very short tickers (e.g. D, T, F) are easily mangled by fuzzy matching; keep OCR as-is.
    if len(s) <= 2:
        return s
    matches = difflib.get_close_matches(s, reference_symbols, n=1, cutoff=0.75)
    if not matches:
        return s
    candidate = matches[0]
    # Keep corrections conservative to avoid bad substitutions.
    if abs(len(candidate) - len(s)) > 1:
        return s
    return candidate


def normalize_numeric_line(line: str) -> str:
    """
    Normalize OCR number formatting:
    - remove thousands separators (comma/space/dot between digit groups)
    - keep decimal points (final .xx)
    """
    s = line.replace("\u00a0", " ").replace("−", "-").replace("—", "-")
    raw_tokens = [tok.replace(",", "") for tok in s.split()]
    tokens = collapse_split_numeric_tokens(raw_tokens)
    return " ".join(tokens)


def collapse_split_numeric_tokens(tokens: list[str]) -> list[str]:
    """
    Fix OCR splits like '$9 847.19' or '3 266.977' into one token.
    """
    collapsed: list[str] = []
    i = 0
    while i < len(tokens):
        current = tokens[i]
        if i + 1 < len(tokens):
            nxt = tokens[i + 1]
            merge_money = bool(
                re.fullmatch(r"[+-]?\$?\d{1,3}", current)
                and re.fullmatch(r"\d{3}(?:\.\d+)?", nxt)
                and "." not in current
            )
            merge_plain = bool(
                re.fullmatch(r"\d{1,3}", current)
                and re.fullmatch(r"\d{3}(?:\.\d+)?", nxt)
            )
            if merge_money or merge_plain:
                collapsed.append(current + nxt)
                i += 2
                continue
        # OCR sometimes emits malformed thousands as extra dots (e.g. 2.013.60).
        cleaned = current
        while cleaned.count(".") > 1:
            first_dot = cleaned.find(".")
            cleaned = cleaned[:first_dot] + cleaned[first_dot + 1 :]
        collapsed.append(cleaned)
        i += 1
    return collapsed


def extract_text(pdf_path: Path) -> str:
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:
        sys.exit(f"Error: could not open PDF — {exc}")
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def configure_tesseract_if_needed() -> None:
    """Try PATH first, then common Windows install locations."""
    if shutil.which("tesseract"):
        return
    candidates = [
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        Path.home() / r"AppData\Local\Programs\Tesseract-OCR\tesseract.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            pytesseract.pytesseract.tesseract_cmd = str(candidate)
            return


def _preprocess_page_for_ocr(
    page: Image.Image, *, upscale: float, aggressive: bool
) -> Image.Image:
    """Grayscale, optional upscale, autocontrast, optional sharpen for ticker-heavy tables."""
    from PIL import ImageOps, ImageEnhance, ImageFilter

    img = page.convert("RGB") if page.mode not in ("RGB", "L") else page
    if img.mode != "L":
        img = img.convert("L")
    if upscale > 1.001:
        w, h = img.size
        img = img.resize(
            (max(1, int(w * upscale)), max(1, int(h * upscale))),
            Image.Resampling.LANCZOS,
        )
    img = ImageOps.autocontrast(img, cutoff=1 if aggressive else 2)
    if aggressive:
        img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=130, threshold=2))
        img = ImageEnhance.Contrast(img).enhance(1.15)
    return img


def extract_text_with_ocr(
    pdf_path: Path,
    *,
    dpi: int = 300,
    psm: int = 6,
    oem: int = 1,
    upscale: float = 1.0,
    preprocess: bool = True,
    aggressive_preprocess: bool = False,
) -> str:
    """
    Rasterize each PDF page and run Tesseract.

    Higher *dpi* (400–600) and *upscale* (>1) improve small-type tickers at the cost
    of runtime and memory. *psm* 4 can work better for single-column tables; 6 is the
    default block layout used by many brokerage PDFs.
    """
    configure_tesseract_if_needed()
    try:
        pages = convert_from_path(str(pdf_path), dpi=dpi)
    except Exception as exc:
        sys.exit(f"Error: OCR setup failed while rendering PDF pages — {exc}")

    tess_cfg = f"--oem {oem} --psm {psm}"
    ocr_parts: list[str] = []
    for page in pages:
        img = page
        try:
            if preprocess:
                img = _preprocess_page_for_ocr(
                    page,
                    upscale=upscale,
                    aggressive=aggressive_preprocess,
                )
            elif upscale > 1.001:
                w, h = page.size
                img = page.resize(
                    (max(1, int(w * upscale)), max(1, int(h * upscale))),
                    Image.Resampling.LANCZOS,
                )
            text = pytesseract.image_to_string(img, config=tess_cfg)
            ocr_parts.append(text or "")
        finally:
            if img is not page:
                try:
                    img.close()
                except Exception:
                    pass
            page.close()
    return "\n".join(ocr_parts)


def find_holdings_slice(lines: list[str]) -> list[str]:
    """Return only the lines between the first holding row and 'Disclosures'."""
    start = next(
        (i for i, ln in enumerate(lines) if FIRST_HOLDING_RE.match(ln)), 0
    )
    end = next(
        (i for i, ln in enumerate(lines) if ln.startswith("Disclosures")),
        len(lines),
    )
    return lines[start:end]


def find_section_slices(lines: list[str]) -> list[tuple[str, list[str]]]:
    """
    Return holdings lines grouped by target section headings.

    Sections are bounded by the next known heading (or CASH / Disclosures / EOF):
    STOCKS, ETFS, MUTUAL FUNDS, UNITS.
    """
    heading_positions = []
    for i, ln in enumerate(lines):
        normalized_heading = re.sub(r"[^A-Z]", "", ln.upper())
        if normalized_heading in SECTION_CANONICAL:
            heading_positions.append(i)
    if not heading_positions:
        return []

    slices: list[tuple[str, list[str]]] = []
    for idx, start_pos in enumerate(heading_positions):
        normalized_heading = re.sub(r"[^A-Z]", "", lines[start_pos].upper())
        heading = SECTION_CANONICAL.get(normalized_heading, lines[start_pos].upper())
        start = start_pos + 1
        if idx + 1 < len(heading_positions):
            end = heading_positions[idx + 1]
        else:
            end = next(
                (
                    i for i, ln in enumerate(lines[start:], start)
                    if re.sub(r"[^A-Z]", "", ln.upper()).startswith("CASH")
                    or ln.startswith("Disclosures")
                ),
                len(lines),
            )
        section_data = lines[start:end]
        if section_data:
            slices.append((heading, section_data))
    return slices


def parse_rows(
    data: list[str], reference_symbols: set[str]
) -> tuple[list[list[str]], set[str]]:
    rows_single, unknown_single = parse_rows_single_line(data, reference_symbols)
    rows_block, unknown_block = parse_rows_block(data, reference_symbols)
    combined_rows: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows_single + rows_block:
        key = tuple(row)
        if key in seen:
            continue
        seen.add(key)
        combined_rows.append(row)
    return combined_rows, unknown_single | unknown_block


def parse_rows_single_line(
    data: list[str], reference_symbols: set[str]
) -> tuple[list[list[str]], set[str]]:
    rows = []
    unknown_symbols: set[str] = set()
    i = 0
    while i < len(data):
        line = data[i]
        normalized = normalize_numeric_line(line)
        tokens = normalized.split()
        parsed_tokens = tokens if len(tokens) == 9 else parse_tokens_flexible(tokens)
        if parsed_tokens is None:
            symbol_from_next = extract_symbol_only_line(data, i + 1)
            parsed_tokens = parse_tokens_missing_symbol(tokens, symbol_from_next)
        if not parsed_tokens:
            i += 1
            continue
        symbol = parsed_tokens[0]
        price, price_change, quantity = parsed_tokens[1], parsed_tokens[2], parsed_tokens[3]
        market_value, day_change, cost_basis, gain_loss, portfolio_pct = (
            parsed_tokens[4],
            parsed_tokens[5],
            parsed_tokens[6],
            parsed_tokens[7],
            parsed_tokens[8],
        )
        money_or_dash = r"(?:[+-]?\$?\d+(?:\.\d+)?|-)"
        if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", portfolio_pct):
            i += 1
            continue
        if not re.fullmatch(money_or_dash, price):
            i += 1
            continue
        if not re.fullmatch(money_or_dash, price_change):
            i += 1
            continue
        if not re.fullmatch(r"\d+(?:\.\d+)?", quantity):
            i += 1
            continue
        if not re.fullmatch(money_or_dash, market_value):
            i += 1
            continue
        if not re.fullmatch(money_or_dash, day_change):
            i += 1
            continue
        if not re.fullmatch(money_or_dash, cost_basis):
            i += 1
            continue
        if not re.fullmatch(money_or_dash, gain_loss):
            i += 1
            continue
        fixed_symbol = normalize_symbol(symbol, reference_symbols)
        if reference_symbols and fixed_symbol not in reference_symbols:
            unknown_symbols.add(fixed_symbol)
        rows.append([
            fixed_symbol,
            clean(price),
            clean(price_change),
            clean(quantity),
            clean(market_value),
            clean(day_change),
            clean(cost_basis),
            clean(gain_loss),
            portfolio_pct,
        ])
        i += 1
    return rows, unknown_symbols


def parse_tokens_flexible(tokens: list[str]) -> list[str] | None:
    """
    Recover the 9 expected row tokens when OCR introduces extra token splits.
    """
    if len(tokens) < 9:
        return None
    work = tokens[:]
    pct = work.pop()
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", pct):
        return None

    def pop_field() -> str | None:
        if not work:
            return None
        last = work.pop()
        if re.fullmatch(r"[+-]?\$?\d+(?:\.\d+)?", last):
            return last
        if (
            work
            and re.fullmatch(r"\d{3}(?:\.\d+)?", last)
            and re.fullmatch(r"[+-]?\$?\d{1,3}", work[-1])
            and "." not in work[-1]
        ):
            return work.pop() + last
        return None

    reverse_fields: list[str] = []
    for _ in range(7):
        field = pop_field()
        if field is None:
            return None
        reverse_fields.append(field)

    if len(work) != 1:
        return None
    symbol = work[0]
    price, price_change, quantity, market_value, day_change, cost_basis, gain_loss = (
        reverse_fields[6],
        reverse_fields[5],
        reverse_fields[4],
        reverse_fields[3],
        reverse_fields[2],
        reverse_fields[1],
        reverse_fields[0],
    )
    return [symbol, price, price_change, quantity, market_value, day_change, cost_basis, gain_loss, pct]


def extract_symbol_only_line(data: list[str], index: int) -> str | None:
    if index >= len(data):
        return None
    candidate = data[index].strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-]{0,9}", candidate):
        return candidate.upper()
    return None


def parse_tokens_missing_symbol(tokens: list[str], symbol: str | None) -> list[str] | None:
    """
    Recover rows where OCR dropped the symbol from the start of the data line
    but emitted it on the next line.
    """
    if symbol is None or len(tokens) != 8:
        return None
    price, price_change, quantity, market_value, day_change, cost_basis, gain_loss, pct = tokens
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?%", pct):
        return None
    money_or_dash = r"(?:[+-]?\$?\d+(?:\.\d+)?|-)"
    if not re.fullmatch(money_or_dash, price):
        return None
    if not re.fullmatch(money_or_dash, price_change):
        return None
    if not re.fullmatch(r"\d+(?:\.\d+)?", quantity):
        return None
    if not re.fullmatch(money_or_dash, market_value):
        return None
    if not re.fullmatch(money_or_dash, day_change):
        return None
    if not re.fullmatch(money_or_dash, cost_basis):
        return None
    if not re.fullmatch(money_or_dash, gain_loss):
        return None
    return [symbol, price, price_change, quantity, market_value, day_change, cost_basis, gain_loss, pct]


def _yahoo_last_close(
    symbol: str, cache: dict[str, float | None], throttle_s: float
) -> float | None:
    if symbol in cache:
        return cache[symbol]
    try:
        import yfinance as yf
    except ImportError:
        return None
    if throttle_s:
        time.sleep(throttle_s)
    try:
        hist = yf.Ticker(symbol).history(period="5d")
        if hist is not None and not hist.empty:
            v = float(hist["Close"].iloc[-1])
            cache[symbol] = v
            return v
    except Exception:
        pass
    cache[symbol] = None
    return None


def _relative_price_error(ocr_price: float, market_price: float | None) -> float:
    if market_price is None or ocr_price <= 0:
        return 1.0
    return abs(market_price - ocr_price) / max(ocr_price, 1e-6)


def _price_verify_candidates(symbol: str, reference_symbols: set[str]) -> list[str]:
    base = sanitize_ocr_symbol(symbol)
    cands: list[str] = []
    for c in (symbol.upper().strip(), base, *ocr_symbol_variants(base)):
        if c and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", c, flags=re.IGNORECASE) and c not in cands:
            cands.append(c.upper())
    if reference_symbols:
        for m in difflib.get_close_matches(base, sorted(reference_symbols), n=10, cutoff=0.72):
            if m not in cands:
                cands.append(m)
    return cands[:18]


def price_verify_row_symbol(
    symbol: str,
    ocr_price: float,
    reference_symbols: set[str],
    tolerance: float,
    cache: dict[str, float | None],
    throttle_s: float,
) -> tuple[str, bool]:
    """
    If Yahoo's last close matches the OCR price for a better ticker candidate,
    return that ticker; otherwise return sanitize_ocr_symbol(symbol).

    Returns (new_symbol, changed_flag).
    """
    primary = sanitize_ocr_symbol(symbol)
    if primary in TRUSTED_PRICE_VERIFY_TICKERS:
        _yahoo_last_close(primary, cache, throttle_s)
        # Do not substitute a different ticker; OCR fixups already applied in normalize_symbol.
        return primary, False
    px_primary = _yahoo_last_close(primary, cache, throttle_s)
    err_primary = _relative_price_error(ocr_price, px_primary)
    if err_primary <= tolerance:
        return primary, primary != symbol

    best_sym = primary
    best_err = err_primary
    for cand in _price_verify_candidates(symbol, reference_symbols):
        if cand == primary:
            continue
        err = _relative_price_error(ocr_price, _yahoo_last_close(cand, cache, throttle_s))
        if err < best_err:
            best_err, best_sym = err, cand

    # Require a clear win vs the OCR ticker to avoid near-duplicate confusions (e.g. ET vs EQT).
    if (
        best_sym != primary
        and best_err <= tolerance
        and best_err < err_primary - max(0.02, 0.12 * err_primary)
    ):
        return best_sym, True
    return primary, primary != symbol


def apply_price_verify_to_grouped_rows(
    grouped_rows: list[tuple[str, list[list[str]]]],
    reference_symbols: set[str],
    tolerance: float,
    throttle_s: float,
) -> list[tuple[str, str]]:
    try:
        import yfinance  # noqa: F401
    except ImportError:
        sys.exit(
            "Error: --price-verify requires the yfinance package.\n"
            "Install with: pip install yfinance"
        )
    cache: dict[str, float | None] = {}
    changes: list[tuple[str, str]] = []
    for _section_name, rows in grouped_rows:
        for row in rows:
            while len(row) < 10:
                row.append("")
            sym = row[0]
            try:
                ocr_px = float(row[1])
            except ValueError:
                row[0] = sanitize_ocr_symbol(sym)
                row[9] = "no price data"
                continue
            new_sym, _changed = price_verify_row_symbol(
                sym, ocr_px, reference_symbols, tolerance, cache, throttle_s
            )
            if new_sym != sym:
                changes.append((sym, new_sym))
            row[0] = new_sym
            yclose = _yahoo_last_close(new_sym, cache, throttle_s)
            row[9] = "no price data" if yclose is None else ""
    return changes


def parse_rows_block(
    data: list[str], reference_symbols: set[str]
) -> tuple[list[list[str]], set[str]]:
    rows = []
    unknown_symbols: set[str] = set()
    skipped = 0
    i = 0

    while i < len(data):
        m = SYM_RE.match(data[i])
        if not m:
            i += 1
            continue

        symbol, price, price_change = m.group(1).upper(), m.group(2), m.group(3)

        if i + PCT_OFFSET >= len(data):
            break

        q = QTY_RE.match(data[i + QTY_OFFSET])
        c = CG_RE.match(data[i + CG_OFFSET])
        p = PCT_RE.match(data[i + PCT_OFFSET])

        if q and c and p:
            quantity, market_value, day_change = q.groups()
            cost_basis, gain_loss = c.groups()
            portfolio_pct = p.group(1) + "%"
            fixed_symbol = normalize_symbol(symbol, reference_symbols)
            if reference_symbols and fixed_symbol not in reference_symbols:
                unknown_symbols.add(fixed_symbol)
            rows.append([
                fixed_symbol,
                clean(price),
                clean(price_change),
                clean(quantity),
                clean(market_value),
                clean(day_change),
                clean(cost_basis),
                clean(gain_loss),
                portfolio_pct,
            ])
            i += BLOCK_SIZE
        else:
            print(f"[skip] unmatched block at line {i}: {data[i]!r}")
            skipped += 1
            i += 1

    if skipped:
        print(f"Warning: {skipped} block(s) did not match — check the skipped lines above.")

    return rows, unknown_symbols


def write_grouped_csv(section_rows: list[tuple[str, list[list[str]]]], out_path: Path) -> None:
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for idx, (section_name, rows) in enumerate(section_rows):
            writer.writerow([section_name])
            writer.writerow([
                "symbol", "price", "price_change", "quantity", "market_value",
                "day_change", "cost_basis", "gain_loss", "portfolio_pct", "yahoo_note",
            ])
            for row in rows:
                padded = (row + [""] * 10)[:10]
                writer.writerow(padded)
            if idx < len(section_rows) - 1:
                writer.writerow([])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract holdings table from a brokerage PDF into CSV."
    )
    parser.add_argument("pdf", type=Path, help="Input holdings PDF path.")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        help="Optional output CSV path (default: <pdf-stem>-structured.csv).",
    )
    parser.add_argument(
        "--strict-symbols",
        action="store_true",
        help="Warn if parsed symbols are not present in reference holdings CSVs.",
    )
    parser.add_argument(
        "--price-verify",
        action="store_true",
        help=(
            "After parsing, reconcile tickers using Yahoo last close vs the OCR price column "
            "(requires yfinance; network). Helps fix OCR symbol errors."
        ),
    )
    parser.add_argument(
        "--price-tolerance",
        type=float,
        default=0.12,
        help="Max relative error OCR price vs Yahoo close for --price-verify (default 0.12 = 12%%).",
    )
    parser.add_argument(
        "--yahoo-throttle",
        type=float,
        default=0.06,
        help="Seconds between Yahoo requests for --price-verify (default 0.06; use 0 to disable).",
    )
    parser.add_argument(
        "--ocr-dpi",
        type=int,
        default=300,
        metavar="N",
        help="PDF rasterization DPI for OCR (default 300; try 500–600 for smaller type).",
    )
    parser.add_argument(
        "--ocr-psm",
        type=int,
        default=6,
        metavar="N",
        help="Tesseract page segmentation mode: 4=single column, 6=uniform block (default), 11=sparse.",
    )
    parser.add_argument(
        "--ocr-upscale",
        type=float,
        default=1.0,
        metavar="F",
        help="Extra scale after render (e.g. 1.2); multiplies pixels, slower but sharper tickers.",
    )
    parser.add_argument(
        "--ocr-preprocess",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Autocontrast (and with --ocr-accurate, sharpen) before OCR (default: on).",
    )
    parser.add_argument(
        "--ocr-accurate",
        action="store_true",
        help="Slower OCR: 600 DPI, 1.15× upscale, stronger preprocess. Often fewer ticker errors.",
    )
    args = parser.parse_args()

    ocr_dpi = args.ocr_dpi
    ocr_upscale = args.ocr_upscale
    ocr_preprocess = args.ocr_preprocess
    ocr_aggressive = False
    if args.ocr_accurate:
        ocr_dpi = max(ocr_dpi, 600)
        ocr_upscale = max(ocr_upscale, 1.15)
        ocr_preprocess = True
        ocr_aggressive = True

    pdf = args.pdf
    if not pdf.exists():
        sys.exit(f"Error: file not found — {pdf}")

    out = args.output if args.output else pdf.with_name(pdf.stem + "-structured.csv")
    reference_symbols = load_reference_symbols(pdf.parent, pdf, out)

    text  = extract_text(pdf)
    if not text.strip():
        try:
            text = extract_text_with_ocr(
                pdf,
                dpi=ocr_dpi,
                psm=args.ocr_psm,
                upscale=ocr_upscale,
                preprocess=ocr_preprocess,
                aggressive_preprocess=ocr_aggressive,
            )
            print(
                "Notice: PDF has no extractable text, using OCR fallback "
                f"(dpi={ocr_dpi}, psm={args.ocr_psm}, upscale={ocr_upscale}, "
                f"preprocess={ocr_preprocess}, aggressive={ocr_aggressive})."
            )
        except pytesseract.pytesseract.TesseractNotFoundError:
            sys.exit(
                "Error: PDF has no extractable text and Tesseract was not found.\n"
                "Install Tesseract or run with pytesseract configured to its executable path."
            )
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    section_slices = find_section_slices(lines)
    all_sections = section_slices if section_slices else [("STOCKS", find_holdings_slice(lines))]

    grouped_rows: list[tuple[str, list[list[str]]]] = []
    rows: list[list[str]] = []
    unknown_symbols: set[str] = set()
    section_counts = {heading: 0 for heading in SECTION_HEADINGS}
    for section_name, section_data in all_sections:
        section_rows, section_unknown = parse_rows(section_data, reference_symbols)
        grouped_rows.append((section_name, section_rows))
        rows.extend(section_rows)
        unknown_symbols.update(section_unknown)
        if section_name in section_counts:
            section_counts[section_name] += len(section_rows)

    if args.price_verify:
        changes = apply_price_verify_to_grouped_rows(
            grouped_rows,
            reference_symbols,
            args.price_tolerance,
            args.yahoo_throttle,
        )
        if changes:
            print("Price-verify symbol updates:")
            for old, new in changes[:50]:
                print(f"  {old} -> {new}")
            if len(changes) > 50:
                print(f"  ... and {len(changes) - 50} more")
        else:
            print("Price-verify: no symbol changes.")

    write_grouped_csv(grouped_rows, out)

    if args.strict_symbols:
        if not reference_symbols:
            print("Warning: --strict-symbols requested but no reference symbols were found.")
        elif unknown_symbols:
            unknown_list = ", ".join(sorted(unknown_symbols))
            print(f"Warning: unknown symbols not in reference set: {unknown_list}")
        else:
            print("Strict symbols check: no unknown symbols found.")

    if section_slices:
        for heading in SECTION_HEADINGS:
            print(f"{heading}: {section_counts[heading]} rows")
    print(f"Done — wrote {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()
