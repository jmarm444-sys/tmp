from pathlib import Path
import importlib.util


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("new_parser", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load parser module")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def try_parse_line(parser, line: str):
    normalized = parser.normalize_numeric_line(line)
    tokens = normalized.split()
    parsed_tokens = tokens if len(tokens) == 9 else parser.parse_tokens_flexible(tokens)
    return normalized, tokens, parsed_tokens


def main() -> None:
    parser = load_module(Path("new_parse-holdings.py"))
    pdf = Path("next-4-29-holdings.pdf")
    text = parser.extract_text(pdf)
    if not text.strip():
        text = parser.extract_text_with_ocr(pdf)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    section_slices = parser.find_section_slices(lines)

    total_candidates = 0
    total_ok = 0
    for heading, section_lines in section_slices:
        print(f"\n== {heading} ==")
        section_candidates = [ln for ln in section_lines if ln.endswith("%")]
        print("candidate lines:", len(section_candidates))
        ok = 0
        for ln in section_candidates:
            normalized, tokens, parsed_tokens = try_parse_line(parser, ln)
            if parsed_tokens is not None:
                ok += 1
                continue
            print("FAIL:", ln)
            print("  norm:", normalized)
            print("  tokens:", tokens)
            idx = section_lines.index(ln)
            lo = max(0, idx - 2)
            hi = min(len(section_lines), idx + 3)
            print("  context:")
            for c in section_lines[lo:hi]:
                print("   ", c)
        print("parsed:", ok)
        total_candidates += len(section_candidates)
        total_ok += ok

    print("\nTOTAL candidates:", total_candidates)
    print("TOTAL parsed:", total_ok)


if __name__ == "__main__":
    main()
