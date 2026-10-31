"""cleaning: the single ledger of everything the pipeline dropped, repaired or assumed.

Every layer calls `note(kind, n=1, detail=None)`; `write(out)` renders <out>/cleaning_report.md with a count per
kind. Nothing is silent. Details are short and never contain message content (no secrets or e-mails are echoed).
"""
import os
from collections import Counter, OrderedDict

# kind -> (category, human text)
KINDS = {
    # ---- input decoding
    "bom_stripped": ("repair", "byte-order marks stripped"),
    "utf16_decoded": ("repair", "files decoded from UTF-16/UTF-32"),
    "encoding_fallback": ("repair", "files that were not valid UTF-8 and were decoded as Windows-1252/Latin-1"),
    "gzip_expanded": ("repair", "gzip/bz2/xz files decompressed"),
    "zip_expanded": ("repair", "zip archives opened (members read in memory)"),
    "gzip_partial": ("repair", "damaged compressed files recovered partially"),
    "unreadable_file": ("drop", "files ignored because they could not be read as text/JSON/CSV"),
    "binary_file": ("drop", "binary files ignored"),
    "empty_file": ("drop", "empty files ignored"),
    "no_records_file": ("drop", "files ignored because no records could be extracted from them"),
    "ignored_extension": ("drop", "files ignored by type (images, documents, executables)"),
    # ---- parsing
    "concatenated_json": ("repair", "files holding several back-to-back JSON values (read all of them)"),
    "truncated_tail": ("drop", "truncated final records (file cut off mid-record) dropped"),
    "array_salvaged": ("repair", "truncated JSON arrays from which the complete leading records were recovered"),
    "bad_line": ("drop", "lines/records that were not valid JSON and were skipped"),
    "embedded_json": ("repair", "JSON objects pulled out of plain-text log lines"),
    "noise_line": ("drop", "non-JSON log lines skipped (e.g. Lambda START/END/REPORT)"),
    "double_encoded": ("repair", "JSON values that were stored as JSON strings and were decoded again"),
    "non_record_value": ("drop", "values that were not objects (numbers, strings, nulls) and were skipped"),
    "csv_delimiter": ("repair", "tables whose delimiter was sniffed (not a plain comma)"),
    "csv_preamble_skipped": ("repair", "lines before the table header skipped"),
    "csv_headerless": ("assumption", "tables without a header row (columns named c1, c2, ...)"),
    "csv_bad_quote_repaired": ("repair", "table rows with broken quotes repaired"),
    "csv_ragged_row": ("repair", "table rows with too many/few cells padded or merged"),
    "csv_blank_row": ("drop", "blank table rows skipped"),
    "cloudwatch_message_unwrapped": ("repair", "CloudWatch @message / message JSON strings unwrapped into records"),
    "nan_to_null": ("repair", "NaN/Infinity values replaced by null"),
    "text_sanitized": ("repair", "lone surrogates, NUL and control characters removed from text"),
    "deep_nesting_truncated": ("repair", "absurdly deep JSON nesting cut off (the rest of the record is kept)"),
    "huge_number": ("repair", "numbers too large to be real (thousands of digits) treated as missing"),
    "line_endings_normalized": ("repair", "files with bare CR line endings converted to LF"),
    "nul_stripped": ("repair", "NUL bytes padded into text files removed"),
    "python_repr_decoded": ("repair", "Python dict/list reprs (single quotes, None/True) decoded as JSON"),
    "wrapper_unwrapped": ("repair", "records found nested inside an export envelope and unwrapped"),
    "columnar_transposed": ("repair", "column-oriented tables transposed into one record per row"),
    "csv_junk_row": ("drop", "table rows too broken to be a record (stray quote or text) dropped"),
    "csv_duplicate_header": ("repair", "duplicate table column names renamed"),
    "unsafe_zip_member": ("drop", "zip members with unsafe paths (../ or absolute) skipped"),
    "hidden_member_skipped": ("drop", "hidden or system files inside archives/folders skipped"),
    "non_run_row": ("drop", "rows that are not runs (feedback, dataset examples, metadata) ignored"),
    "implausible_timestamp": ("repair", "timestamps outside 2000..2100 (epoch 0, year 1, year 9999) treated as missing"),
    "implausible_duration": ("repair", "negative or absurd durations treated as missing"),
    "inverted_times": ("repair", "spans whose end is before their start: treated as having no duration"),
    "epoch_units_normalized": ("repair", "numeric timestamps converted from s/ms/us/ns epoch units"),
    "missing_end": ("repair", "spans with a start but no end or duration (kept without a duration)"),
    "degenerate_trace_id": ("repair", "all-zero trace ids treated as missing (runs grouped by call/conversation id instead)"),
    "latency_from_durations": ("assumption", "runs without timestamps: latency taken as the sum of the step durations"),
    "implausible_tokens": ("repair", "negative or absurd token counts rejected (estimated from text instead)"),
    "error_literal_ignored": ("repair", "error fields holding N/A, false, 0, {} or [] treated as no error"),
    "duplicate_ids_split": ("assumption", "records sharing one id but different content kept as separate runs"),
    "ordering_repaired": ("repair", "out-of-order events sorted by time before building runs"),
    "turns_split": ("assumption", "traces holding several user turns split into one run per turn"),
    "tool_calls_from_messages": ("repair", "tool calls recovered from chat message structures (no tool spans)"),
    "children_flattened": ("repair", "nested child runs flattened into separate spans"),
    "hierarchy_from_dotted_order": ("repair", "trace and parent ids derived from LangSmith dotted_order"),
    "id_collision": ("assumption", "ids reused by different records (kept apart by trace/content)"),
    # ---- schema / mapping
    "auto_mapping": ("assumption", "tables with an unknown schema mapped by the starter heuristics (draft: mapping_rescue/mapping.draft.json)"),
    "mapping_rescue": ("assumption", "mapping written by a model after it reviewed raw rows against the draft (mapping rescue)"),
    "format_lifted": ("repair", "provider-specific records converted to the canonical shape"),
    "renamed_columns": ("repair", "span tables with renamed columns mapped to the canonical fields"),
    "unknown_records": ("drop", "records that matched no known schema and no inferable mapping"),
    "noise_spans": ("drop", "non-agent spans (http, db, framework glue) dropped"),
    "duplicate_spans": ("drop", "duplicate spans/records removed"),
    "orphan_spans": ("repair", "spans whose parent is missing (kept; grouped by trace/time)"),
    "runs_without_root": ("assumption", "runs without a root agent span (partial export); latency from first..last span"),
    "unit_guess": ("assumption", "duration unit guessed from column name/magnitude"),
    "bad_timestamp": ("drop", "timestamp values that could not be parsed (treated as missing)"),
    # ---- run building
    "run_id_synthesized": ("assumption", "runs with no id that got a stable hash id"),
    "conversation_synthesized": ("assumption", "runs with no conversation id grouped by user and time gap (or one conversation)"),
    "thin_run": ("assumption", "runs with no user message and/or no final answer (marked thin)"),
    "user_message_inferred": ("assumption", "runs whose user message was taken from the best available text"),
    "answer_inferred": ("assumption", "runs whose final answer was taken from the best available text"),
    "tokens_estimated": ("assumption", "runs with no token counts; tokens estimated as characters/4 (flagged estimated)"),
    "errors_from_text": ("assumption", "tool calls marked failed from error patterns in their result text (no error field)"),
    "field_truncated": ("repair", "oversized text fields truncated"),
    "secrets_redacted": ("repair", "API keys/passwords/tokens replaced by [secret] in the run dossiers"),
    # ---- unusable runs (kept out of grading, the CIG and the scorecard; see normalized/unusable.csv)
    "unusable_no_ask": ("drop", "runs with an empty user message (unusable: no_ask)"),
    "unusable_model": ("drop", "runs the ingest check marked unusable because a field could not be trusted (reason in details)"),
    # ---- dimensions
    "dimension_dropped": ("assumption", "analysis dimension dropped because the data has nothing for it"),
}
ORDER = {"drop": 0, "repair": 1, "assumption": 2}


class Report(object):
    def __init__(self):
        self.counts = Counter()
        self.details = OrderedDict()
        self.dimensions = OrderedDict()
        self.mapping_notes = []
        self.lines = []

    def reset(self):
        self.__init__()

    def note(self, kind, n=1, detail=None):
        if not n:
            return
        self.counts[kind] += n
        if detail:
            detail = _safe(detail)
            d = self.details.setdefault(kind, [])
            if detail not in d and len(d) < 8:
                d.append(detail)

    def drop_dimension(self, name, why):
        self.dimensions[name] = why
        self.counts["dimension_dropped"] += 1

    def info(self, text):
        text = _safe(text)
        if text not in self.lines:
            self.lines.append(text)

    def to_markdown(self, summary=None):
        L = ["# Cleaning report", "",
             "Everything the pipeline dropped, repaired or assumed to get from your raw export to the CIG. "
             "Nothing here needs your action; it is the audit trail.", ""]
        if summary:
            L += ["## Result", ""] + ["- %s" % s for s in summary] + [""]
        if self.dimensions:
            L += ["## Dimensions dropped", ""] + ["- **%s**: %s" % kv for kv in self.dimensions.items()] + [""]
        if self.counts:
            L += ["## Drops, repairs and assumptions", "", "| kind | what | count |", "|---|---|---|"]
            rows = sorted(self.counts.items(), key=lambda kv: (ORDER.get(KINDS.get(kv[0], ("repair",))[0], 1), kv[0]))
            for k, n in rows:
                cat, text = KINDS.get(k, ("repair", k))
                L.append("| %s: %s | %s | %d |" % (cat, k, text, n))
            L.append("")
            dets = [(k, v) for k, v in self.details.items() if v]
            if dets:
                L += ["## Details (file names and field names only)", ""]
                for k, v in dets:
                    L.append("- %s: %s" % (k, "; ".join(v)))
                L.append("")
        else:
            L += ["No drops, repairs or assumptions were needed.", ""]
        if self.lines:
            L += ["## Notes", ""] + ["- %s" % x for x in self.lines] + [""]
        return "\n".join(L)

    def write(self, out, summary=None):
        os.makedirs(out, exist_ok=True)
        path = os.path.join(out, "cleaning_report.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.to_markdown(summary))
        return path


def _safe(text):
    """Details may contain file names: never let secrets or e-mails through."""
    try:
        from common import redact
        return redact(str(text))
    except Exception:  # noqa: BLE001
        return "[detail withheld]"


CR = Report()


def note(kind, n=1, detail=None):
    CR.note(kind, n, detail)


def unusable_kind(reason, source):
    """Ledger kind for one unusable run: the code rule `no_ask` has its own kind, model reasons share one."""
    return "unusable_no_ask" if (source == "auto" and reason == "no_ask") else "unusable_model"


def write_section(out, title, report):
    """Replace (or append) the section `title` of <out>/cleaning_report.md with the counts in `report` (a Report).
    Later stages (grade-prepare) add their numbers this way; the rest of the file is left alone."""
    path = os.path.join(out, "cleaning_report.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = "# Cleaning report\n\n"
    start, end = "<!-- section: %s -->" % title, "<!-- /section: %s -->" % title
    i = text.find(start)
    if i >= 0:
        j = text.find(end, i)
        text = text[:i].rstrip("\n") + "\n" + (text[j + len(end):].lstrip("\n") if j >= 0 else "")
    lines = [start, "## %s" % title, ""]
    if report.counts:
        lines += ["| kind | what | count |", "|---|---|---|"]
        for k, n in sorted(report.counts.items()):
            cat, what = KINDS.get(k, ("repair", k))
            lines.append("| %s: %s | %s | %d |" % (cat, k, what, n))
        lines.append("")
        for k, v in report.details.items():
            if v:
                lines.append("- %s: %s" % (k, "; ".join(v)))
        lines.append("")
    else:
        lines += ["Nothing to report.", ""]
    lines.append(end)
    from checks import atomic_write
    atomic_write(path, text.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n")
    return path
