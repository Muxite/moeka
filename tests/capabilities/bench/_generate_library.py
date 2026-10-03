"""Regenerate the fixture library: ``python tests/capabilities/bench/_generate_library.py``.

The output (``library/``) is committed; edit the table below and rerun to change it.
"""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).parent / "library"

# (name, description, io-yaml or None)
L = []


def s(name, desc, io=None):
    L.append((name, desc, io))


R, W, N, X = "read-workspace", "write-workspace", "network", "exec"

def io(inputs=None, outputs=None, effects=None, any_=None):
    parts = []
    if inputs is not None or any_ is not None:
        if any_ is not None:
            parts.append(f"  inputs: {{all: [{', '.join(inputs or [])}], any: [{', '.join(any_)}]}}")
        else:
            parts.append(f"  inputs: [{', '.join(inputs)}]")
    if outputs is not None:
        parts.append(f"  outputs: [{', '.join(outputs)}]")
    if effects is not None:
        parts.append(f"  effects: [{', '.join(effects)}]")
    return "io:\n" + "\n".join(parts) + "\n"

# --- PDF
s("pdf-summarize", "Summarise a PDF document into concise markdown notes with key points.", io(["file:pdf"], ["text/markdown"], [R]))
s("pdf-extract-tables", "Extract tables from a PDF into CSV files, one per table.", io(["file:pdf"], ["data/csv"], [R, W]))
s("pdf-merge", "Merge several PDF files into one PDF, keeping bookmarks.", io(["file:pdf"], ["file:pdf"], [R, W]))
s("pdf-split", "Split a PDF into separate pages or page ranges.", io(["file:pdf"], ["file:pdf"], [R, W]))
s("pdf-ocr", "Run OCR on scanned PDFs or images to recover searchable text.", io(None, ["text"], [R, X], any_=["file:pdf", "file:image"]))
s("pdf-redact", "Redact names, emails and phone numbers from a PDF before sharing.", io(["file:pdf"], ["file:pdf"], [R, W]))
s("pdf-form-fill", "Fill in the fields of a PDF form from provided values.", io(["file:pdf"], ["file:pdf"], [R, W]))
s("pdf-compress", "Shrink a PDF's file size by recompressing images.", io(["file:pdf"], ["file:pdf"], [R, W, X]))
# --- images
s("image-resize", "Resize or crop images to given dimensions.", io(["file:image"], ["file:image"], [R, W]))
s("image-caption", "Describe what an image or photo shows in a short caption.", io(["file:image"], ["text"], [R]))
s("image-ocr", "Read the text printed in a screenshot or photo.", io(["file:image"], ["text"], [R]))
s("image-convert", "Convert images between formats such as png, jpg and webp.", io(["file:image"], ["file:image"], [R, W]))
s("image-compress", "Compress photos for the web with minimal quality loss.", io(["file:image"], ["file:image"], [R, W]))
s("photo-exif", "Show or strip EXIF metadata (camera, GPS location) of photos.", io(["file:image"], ["data/json"], [R, W]))
s("image-background-remove", "Remove the background from a product photo.", io(["file:image"], ["file:image"], [R, W, N]))
s("chart-render", "Render a chart or plot from tabular data.", io(None, ["file:image"], [R, W, X], any_=["data/csv", "data/json"]))
# --- audio
s("audio-transcribe", "Transcribe speech in an audio recording to text.", io(["file:audio"], ["text/markdown"], [R, N]))
s("audio-trim", "Trim or cut an audio file between two timestamps.", io(["file:audio"], ["file:audio"], [R, W, X]))
s("podcast-notes", "Write show notes and chapters for a podcast episode recording.", io(["file:audio"], ["text/markdown"], [R, N]))
s("audio-normalize", "Normalise loudness of audio tracks.", io(["file:audio"], ["file:audio"], [R, W, X]))
s("voice-memo-todo", "Turn a voice memo into a todo list.", io(["file:audio"], ["text/markdown"], [R, N]))
# --- video
s("video-transcribe", "Transcribe the speech in a video or online video link.", io(None, ["text/markdown"], [R, N, X], any_=["file:video", "url/video"]))
s("video-clip", "Cut a clip out of a video file between timestamps.", io(["file:video"], ["file:video"], [R, W, X]))
s("youtube-download", "Download a YouTube or Vimeo video for offline viewing.", io(["url/video"], ["file:video"], [N, W, X]))
s("video-thumbnail", "Grab thumbnail frames from a video.", io(["file:video"], ["file:image"], [R, W, X]))
s("video-subtitles", "Generate subtitles (srt) for a video.", io(None, ["text"], [R, W, N], any_=["file:video", "url/video"]))
s("video-compress", "Re-encode a video file to a smaller size.", io(["file:video"], ["file:video"], [R, W, X]))
# --- archives
s("archive-extract", "Extract a zip, tar or 7z archive into a folder.", io(["file:archive"], ["path/dir"], [R, W, X]))
s("archive-create", "Pack a folder into a zip or tar archive.", io(["path"], ["file:archive"], [R, W, X]))
s("archive-inspect", "List the contents of an archive without extracting it.", io(["file:archive"], ["text"], [R, X]))
# --- office
s("docx-to-markdown", "Convert a Word document to markdown.", io(["file:office"], ["text/markdown"], [R]))
s("xlsx-to-csv", "Export the sheets of an Excel workbook to CSV.", io(["file:office"], ["data/csv"], [R, W]))
s("pptx-summary", "Summarise a PowerPoint slide deck.", io(["file:office"], ["text/markdown"], [R]))
s("office-to-pdf", "Convert Word, Excel or PowerPoint files to PDF.", io(["file:office"], ["file:pdf"], [R, W, X]))
s("spreadsheet-formulas", "Explain or fix formulas in an Excel spreadsheet.", io(["file:office"], ["text/markdown"], [R]))
# --- data
s("csv-clean", "Clean a CSV file: trim whitespace, fix dates, drop duplicate rows.", io(["data/csv"], ["data/csv"], [R, W]))
s("csv-stats", "Compute summary statistics (mean, median, counts) of CSV columns.", io(["data/csv"], ["text/markdown", "data/csv"], [R]))
s("csv-plot", "Plot columns of a CSV as a line or bar chart image.", io(["data/csv"], ["file:image"], [R, W, X]))
s("csv-to-json", "Convert CSV rows to a JSON array.", io(["data/csv"], ["data/json"], [R, W]))
s("csv-join", "Join two CSV files on a key column.", io(["data/csv"], ["data/csv"], [R, W]))
s("json-validate", "Validate JSON against a JSON schema and report errors.", io(["data/json"], ["text"], [R]))
s("json-query", "Query and reshape JSON with jq-style filters.", io(["data/json"], ["data/json"], [R, X]))
s("json-to-csv", "Flatten JSON records into a CSV table.", io(["data/json"], ["data/csv"], [R, W]))
s("yaml-lint", "Lint YAML files and point out indentation errors.", io(["data/yaml"], ["text"], [R]))
s("yaml-to-json", "Convert YAML to JSON.", io(["data/yaml"], ["data/json"], [R]))
s("data-diff", "Diff two datasets (csv or json) and report changed rows.", io(None, ["text/markdown"], [R], any_=["data/csv", "data/json"]))
s("sql-from-csv", "Load a CSV into SQLite and answer questions with SQL.", io(["data/csv"], ["data/csv", "text"], [R, X]))
# --- code
s("python-lint", "Lint Python code with ruff and explain the warnings.", io(None, ["text"], [R, X], any_=["text/code:python", "path"]))
s("python-test-runner", "Run pytest on a Python project and summarise failures.", io(["path"], ["text/log"], [R, X]))
s("python-profile", "Profile slow Python code and find hot spots.", io(["text/code:python"], ["text/markdown"], [R, X]))
s("python-type-check", "Type-check Python code with pyright and explain errors.", io(None, ["text"], [R, X], any_=["text/code:python", "path"]))
s("shell-explain", "Explain what a shell command or bash script does, flag by flag.", io(["text/code:shell"], ["text/markdown"], ["none"]))
s("shell-lint", "Check bash scripts with shellcheck.", io(["text/code:shell"], ["text"], [X]))
s("code-review", "Review a code snippet for bugs, style and security issues.", io(["text/code"], ["text/markdown"], ["none"]))
s("code-explain", "Explain a piece of code line by line for a beginner.", io(["text/code"], ["text/markdown"], ["none"]))
s("code-translate", "Translate code from one programming language to another.", io(["text/code"], ["text/code"], ["none"]))
s("regex-builder", "Build and test regular expressions from examples.", io(None, ["text"], ["none"]))
s("sql-format", "Format and explain SQL queries.", io(["text/code"], ["text/code"], ["none"]))
# --- logs
s("log-triage", "Triage a log file: group errors, find the first failure and likely cause.", io(["text/log"], ["text/markdown"], [R]))
s("log-summarize", "Summarise long service logs into a timeline of events.", io(["text/log"], ["text/markdown"], [R]))
s("stacktrace-explain", "Explain a Python or Java stack trace and suggest a fix.", io(None, ["text/markdown"], ["none"], any_=["text/log", "text/code"]))
s("journal-errors", "Show recent errors from the systemd journal of this machine.", io(["host"], ["text/log"], [X]))
# --- markdown
s("markdown-toc", "Generate a table of contents for a markdown document.", io(["text/markdown"], ["text/markdown"], [R]))
s("markdown-lint", "Lint markdown files for broken formatting.", io(["text/markdown"], ["text"], [R]))
s("markdown-to-pdf", "Render a markdown document to a styled PDF.", io(["text/markdown"], ["file:pdf"], [R, W, X]))
s("markdown-to-html", "Render markdown to a standalone HTML page.", io(["text/markdown"], ["text"], [R, W]))
s("readme-writer", "Write or improve a README for a project directory.", io(["path"], ["text/markdown"], [R, W]))
# --- web
s("web-page-summarize", "Summarise the content of a web page or article link.", io(["url/web"], ["text/markdown"], [N]))
s("web-scrape-table", "Scrape an HTML table from a web page into CSV.", io(["url/web"], ["data/csv"], [N, W]))
s("link-check", "Check a website for broken links.", io(["url/web"], ["text/markdown"], [N]))
s("rss-digest", "Build a digest of the latest posts from RSS feeds.", io(["url"], ["text/markdown"], [N]))
s("web-archive", "Save a snapshot of a web page as PDF for archiving.", io(["url/web"], ["file:pdf"], [N, W]))
s("seo-audit", "Audit a web page for SEO problems (titles, meta tags, headings).", io(["url/web"], ["text/markdown"], [N]))
s("site-uptime", "Check whether a website or URL is up and how fast it responds.", io(["url"], ["text"], [N]))
s("price-tracker", "Track the price of a product page link over time.", io(["url/web"], ["data/csv"], [N, W]))
# --- git
s("git-repo-analyze", "Clone a Git repository from a link and analyse its structure and languages.", io(["url/git"], ["text/markdown"], [N, X, W]))
s("pr-review", "Review a GitHub pull request link: summarise changes and risks.", io(["url/git"], ["text/markdown"], [N]))
s("github-issues", "List and triage the open issues of a GitHub repository.", io(["url/git"], ["text/markdown"], [N]))
s("git-changelog", "Write a changelog from the git history of a local repository.", io(["path"], ["text/markdown"], [R, X]))
s("repo-stats", "Count lines of code, commits and contributors of a local repository.", io(["path"], ["text/markdown"], [R, X]))
s("dependency-audit", "Audit a project's dependencies for known vulnerabilities.", io(["path"], ["text/markdown"], [R, N, X]))
s("git-blame-explain", "Explain who changed a line of a file and why, using git blame.", io(["path"], ["text/markdown"], [R, X]))
s("release-notes", "Draft release notes from the commits of a GitHub repository link.", io(["url/git"], ["text/markdown"], [N]))
# --- paths
s("disk-usage", "Show which folders under a directory use the most disk space.", io(["path"], ["text"], [R, X]))
s("find-duplicates", "Find duplicate files under a directory by content hash.", io(["path"], ["text"], [R]))
s("batch-rename", "Rename many files in a directory by a pattern.", io(["path"], ["none"], [R, W]))
s("dir-tree", "Print the tree of a directory with sizes.", io(["path"], ["text"], [R]))
s("file-checksum", "Compute sha256 checksums of files and verify them.", io(["path"], ["text"], [R]))
s("photo-organize", "Sort a folder of photos into year/month folders.", io(["path"], ["none"], [R, W]))
# --- host
s("systemd-status", "Check the status of systemd services on this host and restart failed ones.", io(["host"], ["text"], [X]))
s("docker-cleanup", "Prune unused docker images, containers and volumes on this host.", io(["host"], ["text"], [X]))
s("gpu-status", "Show GPU utilisation, memory and running processes on this host.", io(["host"], ["text"], [X]))
s("backup-run", "Run the configured backup job of this host and verify it.", io(["host"], ["text"], [X, W]))
s("apt-install", "Install system packages with apt on this host.", io(["host"], ["none"], ["sudo", X]))
s("firewall-rule", "Add or remove ufw firewall rules on this host.", io(["host"], ["none"], ["sudo", X]))
s("network-diagnose", "Diagnose this host's network: wifi, DNS and routing problems.", io(["host"], ["text/markdown"], [X, N]))
# --- workspace
s("notes-search", "Search the notes in the workspace for a topic.", io(["workspace"], ["text/markdown"], [R]))
s("todo-manager", "Add, complete and list todo items kept in the workspace.", io(["workspace"], ["text/markdown"], [R, W]))
s("journal-entry", "Write a dated journal entry into the workspace journal.", io(["workspace"], ["none"], [W]))
s("bookmark-save", "Save a link with tags into the workspace bookmarks file.", io(["url"], ["none"], [W]))
# --- text-only
s("translate-text", "Translate text between languages, keeping tone and formatting.", io(None, ["text"], ["none"]))
s("unit-convert", "Convert units: lengths, weights, temperatures, cooking measures.", io(None, ["text"], ["none"]))
s("timezone-convert", "Convert a meeting time between time zones.", io(None, ["text"], ["none"]))
s("weather-forecast", "Get the weather forecast for a city.", io(None, ["text"], [N]))
s("currency-convert", "Convert amounts between currencies at today's exchange rate.", io(None, ["text"], [N]))
s("define-word", "Define a word and give example sentences.", io(None, ["text"], ["none"]))
s("recipe-suggest", "Suggest recipes from the ingredients you have.", io(None, ["text/markdown"], ["none"]))
s("email-draft", "Draft a polite email reply from a few bullet points.", io(None, ["text/markdown"], ["none"]))
s("tweet-draft", "Draft a short social media post or thread.", io(None, ["text"], ["none"]))
s("text-summarize", "Summarise pasted text into a few bullet points.", io(["text/query"], ["text/markdown"], ["none"]))
s("grammar-fix", "Fix grammar and spelling of a paragraph.", io(None, ["text"], ["none"]))
s("meeting-agenda", "Draft an agenda for a meeting from its goals.", io(None, ["text/markdown"], ["none"]))
s("trip-planner", "Plan a trip itinerary with days, places and travel times.", io(None, ["text/markdown"], [N]))
s("flashcards", "Make study flashcards from notes or a topic.", io(None, ["data/csv", "text/markdown"], ["none"]))
s("stock-quote", "Look up the latest stock price of a ticker symbol.", io(None, ["text"], [N]))
s("math-solve", "Solve math problems step by step.", io(None, ["text/markdown"], ["none"]))
# --- untyped legacy skills (always survive the filter)
s("brainstorm", "Brainstorm ideas for a project, name or gift.")
s("meeting-notes", "Turn raw meeting notes into decisions and action items.")
s("habit-coach", "Coach the user on building a daily habit.")
s("poem-writer", "Write a short poem on any theme.")
s("decision-matrix", "Compare options with a weighted decision matrix.")
s("interview-prep", "Prepare questions and answers for a job interview.")
s("workout-plan", "Create a weekly workout plan.")
s("budget-planner", "Plan a monthly budget from income and expenses.")

assert len({n for n, *_ in L}) == len(L)
shutil.rmtree(ROOT, ignore_errors=True)
for name, desc, block in L:
    d = ROOT / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {json.dumps(desc)}\n{block or ''}---\n\n# {name}\n\n{desc}\n",
        encoding="utf-8",
    )
print(len(L))
