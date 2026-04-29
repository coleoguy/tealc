import json
import os
import re
import sqlite3
import requests
import tempfile
from datetime import datetime, timezone
from langchain_core.tools import tool

# ---------------------------------------------------------------------------
# File-type sets and helpers for read_local_file / read_docx_with_comments
# ---------------------------------------------------------------------------
TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".r", ".R", ".csv", ".json",
    ".tex", ".bib", ".rst", ".yaml", ".yml", ".sh", ".html",
}


def _read_docx(path: str) -> str:
    """Convert a .docx file to markdown text using mammoth."""
    import mammoth
    with open(path, "rb") as f:
        result = mammoth.convert_to_markdown(f)
    return result.value  # result.messages contains warnings; ignore them


def _read_pdf(path: str) -> str:
    """Extract text from a PDF with [Page N] markers using pypdf."""
    from pypdf import PdfReader
    reader = PdfReader(path)
    parts = []
    for i, page in enumerate(reader.pages, 1):
        parts.append(f"[Page {i}]\n{page.extract_text() or ''}")
    return "\n\n".join(parts)


DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "agent.db")


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


@tool
def search_pubmed(query: str, max_results: int = 5) -> str:
    """Search PubMed and Europe PMC for peer-reviewed papers by keyword, author, or topic."""
    try:
        resp = requests.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": query, "format": "json", "pageSize": max_results, "resultType": "core"},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("resultList", {}).get("result", [])
        if not results:
            return "No papers found."
        lines = []
        for p in results:
            abstract = (p.get("abstractText") or "No abstract")[:400]
            lines.append(
                f"**{p.get('title', 'N/A')}**\n"
                f"Authors: {p.get('authorString', 'N/A')}\n"
                f"Journal: {p.get('journalTitle', 'N/A')} ({p.get('pubYear', 'N/A')})\n"
                f"PMID: {p.get('pmid', 'N/A')} | DOI: {p.get('doi', 'N/A')}\n"
                f"Abstract: {abstract}..."
            )
        return "\n\n---\n\n".join(lines)
    except Exception as e:
        return f"Error searching PubMed: {e}"


@tool
def search_biorxiv(query: str, days_back: int = 180) -> str:
    """Search bioRxiv for recent preprints by topic or keyword."""
    try:
        from datetime import timedelta
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        resp = requests.get(
            f"https://api.biorxiv.org/details/biorxiv/{start}/{end}/0/json",
            timeout=15,
        )
        resp.raise_for_status()
        all_papers = resp.json().get("collection", [])
        query_lower = query.lower()
        matched = [
            p for p in all_papers
            if query_lower in (p.get("title") or "").lower()
            or query_lower in (p.get("abstract") or "").lower()
        ][:5]
        if not matched:
            return f"No recent bioRxiv preprints found for '{query}'. Try search_pubmed instead."
        lines = []
        for p in matched:
            lines.append(
                f"**{p.get('title', 'N/A')}**\n"
                f"Authors: {p.get('authors', 'N/A')}\n"
                f"Date: {p.get('date', 'N/A')} | DOI: {p.get('doi', 'N/A')}\n"
                f"Abstract: {(p.get('abstract') or '')[:400]}..."
            )
        return "\n\n---\n\n".join(lines)
    except Exception as e:
        return f"Error searching bioRxiv: {e}"


@tool
def web_search(query: str, max_results: int = 6) -> str:
    """Search the web for any topic — news, protocols, people, software, anything."""
    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append(f"**{r['title']}**\n{r['href']}\n{r['body']}")
        return "\n\n---\n\n".join(results) if results else "No results found."
    except Exception as e:
        return f"Error: {e}"


@tool
def fetch_url(url: str, max_chars: int = 12000) -> str:
    """Fetch a web page and return its main text content (no scripts, no nav, no ads).
    Useful for reading journal pages, grant announcements, paper landing pages, NSF/NIH
    program guides, etc. Returns: the page title + the cleaned text. For PDFs at the URL,
    downloads + extracts via the same _read_pdf helper used by read_local_file.
    Truncates at max_chars."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 Tealc-Lab-Agent"},
            timeout=15,
            allow_redirects=True,
        )
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "application/pdf" in content_type:
            import tempfile
            suffix = ".pdf"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(resp.content)
                tmp_path = tmp.name
            try:
                text = _read_pdf(tmp_path)
            finally:
                import os as _os
                _os.unlink(tmp_path)
            total_len = len(text)
            truncated = text[:max_chars]
            result = f"**PDF from {url}**\nURL: {url}\n\n{truncated}"
            if total_len > max_chars:
                result += f"\n\n[Truncated at {max_chars} of {total_len} chars]"
            return result

        # HTML path
        html = resp.text

        # Try BeautifulSoup first; fall back to stdlib HTMLParser
        try:
            from bs4 import BeautifulSoup  # type: ignore
            soup = BeautifulSoup(html, "html.parser")
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else url
            for tag in soup(["script", "style", "nav", "footer", "aside"]):
                tag.decompose()
            raw_text = soup.get_text(separator=" ")
        except ImportError:
            from html.parser import HTMLParser as _HTMLParser
            import re as _re

            class _TextExtractor(_HTMLParser):
                SKIP_TAGS = {"script", "style", "nav", "footer", "aside"}

                def __init__(self):
                    super().__init__()
                    self._skip_depth = 0
                    self._parts = []
                    self._title_parts = []
                    self._in_title = False

                def handle_starttag(self, tag, attrs):
                    tag_lower = tag.lower()
                    if tag_lower in self.SKIP_TAGS:
                        self._skip_depth += 1
                    if tag_lower == "title":
                        self._in_title = True

                def handle_endtag(self, tag):
                    tag_lower = tag.lower()
                    if tag_lower in self.SKIP_TAGS and self._skip_depth > 0:
                        self._skip_depth -= 1
                    if tag_lower == "title":
                        self._in_title = False

                def handle_data(self, data):
                    if self._in_title:
                        self._title_parts.append(data)
                    elif self._skip_depth == 0:
                        self._parts.append(data)

                def get_title(self):
                    return "".join(self._title_parts).strip()

                def get_text(self):
                    return " ".join(self._parts)

            extractor = _TextExtractor()
            extractor.feed(html)
            title = extractor.get_title() or url
            raw_text = extractor.get_text()

        import re
        cleaned = re.sub(r"[ \t]+", " ", raw_text)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

        total_len = len(cleaned)
        truncated = cleaned[:max_chars]
        result = f"**{title}**\nURL: {url}\n\n{truncated}"
        if total_len > max_chars:
            result += f"\n\n[Truncated at {max_chars} of {total_len} chars]"
        return result

    except Exception as e:
        return f"Error fetching URL: {type(e).__name__}: {str(e)[:200]}"


@tool
def fetch_url_links(url: str, filter_substring: str = "") -> str:
    """Fetch a web page and return the list of outgoing links (href + visible text) without
    the page body. Useful for finding the right sub-page of a multi-page resource (e.g.,
    finding the actual deadline page on an NSF program site, or finding the paper PDF link
    on a journal landing page). Optional filter_substring narrows to links whose href or
    text contains the substring (case-insensitive)."""
    try:
        import urllib.parse as _urlparse

        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 Tealc-Lab-Agent"},
            timeout=15,
            allow_redirects=True,
        )
        resp.raise_for_status()

        html = resp.text
        links = []

        try:
            from bs4 import BeautifulSoup  # type: ignore
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = _urlparse.urljoin(url, a["href"])
                text = a.get_text(strip=True)
                links.append((href, text))
        except ImportError:
            from html.parser import HTMLParser as _HTMLParser

            class _LinkExtractor(_HTMLParser):
                def __init__(self, base_url):
                    super().__init__()
                    self._base = base_url
                    self._links = []
                    self._current_href = None
                    self._current_text = []

                def handle_starttag(self, tag, attrs):
                    if tag.lower() == "a":
                        attrs_dict = dict(attrs)
                        raw_href = attrs_dict.get("href", "")
                        if raw_href:
                            self._current_href = _urlparse.urljoin(self._base, raw_href)
                            self._current_text = []

                def handle_endtag(self, tag):
                    if tag.lower() == "a" and self._current_href:
                        text = "".join(self._current_text).strip()
                        self._links.append((self._current_href, text))
                        self._current_href = None
                        self._current_text = []

                def handle_data(self, data):
                    if self._current_href is not None:
                        self._current_text.append(data)

            extractor = _LinkExtractor(url)
            extractor.feed(html)
            links = extractor._links

        filter_lower = filter_substring.lower()
        if filter_lower:
            links = [
                (h, t) for h, t in links
                if filter_lower in h.lower() or filter_lower in t.lower()
            ]

        links = links[:100]

        if not links:
            return f"No links found on {url}" + (
                f" matching '{filter_substring}'" if filter_substring else ""
            )

        lines = [f"Links on {url}:\n"]
        for href, text in links:
            label = text if text else href
            lines.append(f"- [{label}]({href})")
        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching URL links: {type(e).__name__}: {str(e)[:200]}"


@tool
def save_note(title: str, content: str) -> str:
    """Save a research note, idea, to-do, or any text for later retrieval."""
    try:
        conn = _get_db()
        conn.execute(
            "INSERT INTO notes (title, content, created_at) VALUES (?, ?, ?)",
            (title, content, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        return f"Note saved: '{title}'"
    except Exception as e:
        return f"Error saving note: {e}"


@tool
def list_notes() -> str:
    """List all saved research notes with their IDs and titles."""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT id, title, created_at FROM notes ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
        if not rows:
            return "No notes saved yet."
        return "\n".join([f"[{r[0]}] {r[1]}  —  {r[2][:10]}" for r in rows])
    except Exception as e:
        return f"Error: {e}"


@tool
def read_note(note_id: int) -> str:
    """Read the full content of a saved note by its ID."""
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT title, content, created_at FROM notes WHERE id=?", (note_id,)
        ).fetchone()
        conn.close()
        if not row:
            return f"Note {note_id} not found."
        return f"**{row[0]}**\nSaved: {row[2][:10]}\n\n{row[1]}"
    except Exception as e:
        return f"Error: {e}"


@tool
def delete_note(note_id: int) -> str:
    """Delete a saved note by its ID."""
    try:
        conn = _get_db()
        conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
        conn.commit()
        conn.close()
        return f"Note {note_id} deleted."
    except Exception as e:
        return f"Error: {e}"


@tool
def get_datetime() -> str:
    """Get the current date and time."""
    return datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")


LAB_WEBSITE_PATH = "/Users/blackmon/Desktop/GitHub/coleoguy.github.io/llms-full.txt"
DRIVE_ROOT = "/Users/blackmon/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/My Drive"


@tool
def read_lab_website() -> str:
    """Load the full machine-readable snapshot of Heath's lab website — research programs,
    publications, databases, students, AI projects, and lab values. Use this when you need
    deep detail about the lab that isn't in your system prompt."""
    try:
        with open(LAB_WEBSITE_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        return content[:12000] + "\n\n[truncated — use read_local_file for specific sections]" \
            if len(content) > 12000 else content
    except Exception as e:
        return f"Could not read lab website: {e}"


@tool
def read_local_file(path: str, max_chars: int = 25000) -> str:
    """Read any file from Heath's computer. Supports plain text, .docx, and .pdf.
    For Google Drive files, path can be relative to the Drive root
    (e.g. '01-Grants/google_grant_draft.docx') or an absolute path.
    Use this to read grant drafts, manuscripts, notes, or any document Heath wants
    you to review or help edit."""
    try:
        # Allow relative paths from Drive root
        if not path.startswith("/"):
            path = os.path.join(DRIVE_ROOT, path)
        if not os.path.exists(path):
            return f"File not found: {path}\nDrive root is: {DRIVE_ROOT}"
        ext = os.path.splitext(path)[1].lower()
        if ext in TEXT_EXTENSIONS:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        elif ext == ".docx":
            content = _read_docx(path)
        elif ext == ".pdf":
            content = _read_pdf(path)
        else:
            return f"Unsupported file format: {ext}. Supported: text files, .docx, .pdf"
        if len(content) > max_chars:
            return (
                content[:max_chars]
                + f"\n\n[Truncated at {max_chars} of {len(content)} chars. "
                f"Use a smaller section or ask for specific content.]"
            )
        return content
    except Exception as e:
        return f"Error reading file: {e}"


@tool
def track_citations(author_name: str = "Heath Blackmon", max_results: int = 10) -> str:
    """Check who is citing Heath's work recently using OpenAlex. Returns papers that
    cite his work, useful for finding collaborators and tracking research impact."""
    try:
        # Find author ID
        resp = requests.get(
            "https://api.openalex.org/authors",
            params={"search": author_name, "per_page": 1},
            timeout=10,
        )
        authors = resp.json().get("results", [])
        if not authors:
            return f"Author '{author_name}' not found in OpenAlex."
        author_id = authors[0]["id"].split("/")[-1]
        display_name = authors[0]["display_name"]
        citation_count = authors[0].get("cited_by_count", "unknown")
        h_index = authors[0].get("summary_stats", {}).get("h_index", "unknown")

        # Get recent works cited by others
        resp2 = requests.get(
            "https://api.openalex.org/works",
            params={
                "filter": f"cites:author.id:{author_id}",
                "sort": "publication_date:desc",
                "per_page": max_results,
            },
            timeout=10,
        )
        citing_works = resp2.json().get("results", [])

        lines = [f"**{display_name}** — {citation_count} total citations, H-index: {h_index}\n"]
        lines.append(f"**Recent papers citing your work:**")
        for w in citing_works:
            authors_list = ", ".join([a["author"]["display_name"] for a in w.get("authorships", [])[:3]])
            lines.append(
                f"- {w.get('title', 'N/A')} ({w.get('publication_year', 'N/A')})\n"
                f"  {authors_list} | {w.get('primary_location', {}).get('source', {}).get('display_name', 'N/A')}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching citations: {e}"


@tool
def search_openalex(query: str, max_results: int = 5) -> str:
    """Search OpenAlex for papers — broader than PubMed, covers all disciplines,
    includes citation counts and open access links."""
    try:
        resp = requests.get(
            "https://api.openalex.org/works",
            params={"search": query, "per_page": max_results, "sort": "cited_by_count:desc"},
            timeout=10,
        )
        works = resp.json().get("results", [])
        if not works:
            return "No results found."
        lines = []
        for w in works:
            authors_list = ", ".join([a["author"]["display_name"] for a in w.get("authorships", [])[:4]])
            oa_url = w.get("open_access", {}).get("oa_url") or w.get("doi") or "N/A"
            lines.append(
                f"**{w.get('title', 'N/A')}**\n"
                f"Authors: {authors_list}\n"
                f"Year: {w.get('publication_year', 'N/A')} | "
                f"Citations: {w.get('cited_by_count', 0)} | "
                f"Journal: {w.get('primary_location', {}).get('source', {}).get('display_name', 'N/A')}\n"
                f"URL: {oa_url}"
            )
        return "\n\n---\n\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


CREDS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "google_credentials.json")
TOKEN_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "google_token.json")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "config.json")
KNOWN_SHEETS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "known_sheets.json")
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]


def _get_google_service(service_name: str, version: str):
    """Get an authenticated Google API service. Requires google_credentials.json."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if os.path.exists(TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, GOOGLE_SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(CREDS_PATH):
                    return None, "Google credentials not set up yet. See setup instructions."
                flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, GOOGLE_SCOPES)
                creds = flow.run_local_server(port=0)
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
        return build(service_name, version, credentials=creds), None
    except Exception as e:
        return None, str(e)


@tool
def list_recent_emails(max_results: int = 20, query: str = "") -> str:
    """Read recent emails from Gmail. Can filter by query (e.g. 'grant', 'student', 'review').
    Requires Google credentials to be set up first."""
    service, err = _get_google_service("gmail", "v1")
    if err:
        return f"Gmail not connected: {err}"
    try:
        q = query or "is:unread"
        result = service.users().messages().list(userId="me", q=q, maxResults=max_results).execute()
        messages = result.get("messages", [])
        if not messages:
            return "No messages found."
        lines = []
        for m in messages[:max_results]:
            msg = service.users().messages().get(userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]).execute()
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            snippet = msg.get("snippet", "")[:120]
            lines.append(
                f"ID: {m['id']}\n"
                f"From: {headers.get('From', 'N/A')}\n"
                f"Subject: {headers.get('Subject', 'N/A')}\n"
                f"Date: {headers.get('Date', 'N/A')}\n"
                f"Preview: {snippet}"
            )
        return "\n\n---\n\n".join(lines)
    except Exception as e:
        return f"Error reading Gmail: {e}"


@tool
def draft_email_reply(message_id: str, draft_body: str, subject_override: str = "") -> str:
    """Save a draft email reply in Gmail for Heath to review and send.
    NEVER sends automatically — always saves as draft only."""
    service, err = _get_google_service("gmail", "v1")
    if err:
        return f"Gmail not connected: {err}"
    try:
        import base64
        from email.mime.text import MIMEText

        # Get original message for threading
        orig = service.users().messages().get(userId="me", id=message_id, format="metadata",
            metadataHeaders=["From", "Subject", "Message-ID"]).execute()
        headers = {h["name"]: h["value"] for h in orig["payload"]["headers"]}
        to = headers.get("From", "")
        subject = subject_override or f"Re: {headers.get('Subject', '')}"
        thread_id = orig.get("threadId")

        msg = MIMEText(draft_body)
        msg["To"] = to
        msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        draft = service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw, "threadId": thread_id}}
        ).execute()
        return f"Draft saved (ID: {draft['id']}) — open Gmail to review and send."
    except Exception as e:
        return f"Error creating draft: {e}"


# ---------------------------------------------------------------------------
# Gmail trash tooling — Heath's auto-trash workflow
# ---------------------------------------------------------------------------

_JUNK_SUBJECT_RE = re.compile(
    r"(newsletter|digest|% off|\boff\b.*\bsale|promotion|promo|"
    r"unsubscribe|webinar|limited.time|exclusive.offer|deal|savings|"
    r"announcing|new.release|product.update|marketing|"
    r"your.weekly|your.monthly|weekly.update|monthly.update)",
    re.IGNORECASE,
)
_JUNK_FROM_RE = re.compile(
    r"(noreply|no-reply|no_reply|marketing|newsletter|notifications?@|"
    r"mailer|campaign|updates@|news@|info@|hello@|team@|support@)",
    re.IGNORECASE,
)

# Domains / patterns that always pass rule #1 (never junk on sender-domain grounds).
_PROTECTED_TLDS = (".edu", ".gov", ".edu.au", ".ac.uk", ".ac.jp")


def _load_vip_senders_set() -> set[str]:
    """Lowercased set of VIP email addresses + aliases."""
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               "data", "vip_senders.json")) as f:
            data = json.load(f)
        out: set[str] = set()
        for v in data.get("vip_senders", []):
            if v.get("email"):
                out.add(v["email"].lower())
            for a in v.get("aliases", []) or []:
                out.add(a.lower())
        return out
    except Exception:
        return set()


def _load_collaborator_emails() -> set[str]:
    """Lowercased set of collaborator emails known to the lab."""
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               "data", "collaborators.json")) as f:
            data = json.load(f)
        out: set[str] = set()
        for c in data.get("collaborators", []) or []:
            em = (c.get("email") or "").strip().lower()
            if em:
                out.add(em)
            for a in c.get("aliases", []) or []:
                if a:
                    out.add(a.strip().lower())
        return out
    except Exception:
        return set()


def _load_lab_people_names() -> list[str]:
    """Lab members' names — used to catch sender-display-name matches."""
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                               "data", "lab_people.json")) as f:
            data = json.load(f)
        return [n.lower() for n in data.get("names", []) if n]
    except Exception:
        return []


def _extract_from_address(from_header: str) -> tuple[str, str]:
    """Parse `From:` header into (display_name_lower, email_lower)."""
    if not from_header:
        return "", ""
    m = re.match(r'^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$', from_header)
    if m:
        return m.group(1).strip().lower(), m.group(2).strip().lower()
    # No angle-brackets — treat whole string as email
    return "", from_header.strip().lower()


def _sender_is_protected(from_header: str, vip: set[str],
                         collaborators: set[str], lab_names: list[str]) -> tuple[bool, str]:
    """Rule #1 check. Returns (is_protected, reason)."""
    display_name, email = _extract_from_address(from_header)
    if not email:
        return False, ""
    # VIP blocklist — strictest
    if email in vip:
        return True, "VIP sender"
    # Collaborator blocklist
    if email in collaborators:
        return True, "known collaborator"
    # Lab member in display name
    for name in lab_names:
        if name and name in display_name:
            return True, f"lab member ({name})"
    # Domain whitelist
    _, _, domain = email.partition("@")
    domain = domain.lower().strip()
    if domain.endswith(_PROTECTED_TLDS):
        return True, f"protected domain ({domain})"
    if "tamu.edu" in domain or "tamu.edu" == domain:
        return True, "TAMU domain"
    return False, ""


def _has_junk_signal(headers: dict, display_name: str, email: str) -> tuple[bool, str]:
    """Rule #2 check. Returns (looks_junk, reason)."""
    subject = headers.get("Subject", "") or ""
    # Strongest signal — List-Unsubscribe header is mandated by CAN-SPAM for
    # legitimate bulk mail; presence reliably indicates newsletter/marketing.
    if headers.get("List-Unsubscribe") or headers.get("List-Id"):
        return True, "List-Unsubscribe header present"
    precedence = (headers.get("Precedence") or "").lower()
    if precedence in ("bulk", "list", "junk"):
        return True, f"Precedence: {precedence}"
    if _JUNK_SUBJECT_RE.search(subject):
        return True, "subject matches junk pattern"
    if _JUNK_FROM_RE.search(email):
        return True, "sender-address matches junk pattern"
    if _JUNK_FROM_RE.search(display_name):
        return True, "sender-name matches junk pattern"
    return False, ""


def _heath_in_thread(service, thread_id: str) -> bool:
    """Rule #3 check. True if any message in the thread has the SENT label
    applied (i.e., Heath has sent a message in this thread)."""
    try:
        t = service.users().threads().get(
            userId="me", id=thread_id, format="minimal",
        ).execute()
        for m in t.get("messages", []) or []:
            if "SENT" in (m.get("labelIds") or []):
                return True
        return False
    except Exception:
        # Err on the side of caution — if we can't check the thread, skip trashing.
        return True


@tool
def find_trash_candidates(days_back: int = 7, max_candidates: int = 25,
                          extra_query: str = "") -> str:
    """Find recent Gmail messages that look like junk and are safe to trash.

    Applies all FOUR of Heath's auto-trash rules (all must be true):
      1. Sender is not a protected domain (.edu / .gov / tamu.edu), not a
         known collaborator, not a VIP, not a lab member
      2. Subject/sender matches a junk pattern (List-Unsubscribe header,
         Precedence: bulk, or regex on subject / from)
      3. The thread does NOT contain any message Heath has sent (i.e. he's
         never replied to anything in this thread — it's cold bulk mail)
      4. (enforced by caller) — this tool only finds; trash_emails() executes

    This is a PURE FIND — does NOT trash anything. The caller (chat agent or
    Heath directly) reviews the returned preview and then calls
    `trash_emails(ids, dry_run=False)` on only the IDs they approve.

    Args:
      days_back: how many days of mail to scan (default 7)
      max_candidates: cap on candidates returned (default 25)
      extra_query: additional Gmail search terms appended to the query

    Returns:
      Markdown table of candidates with message_id, From, Subject, snippet,
      rule-match reasons. Plus a one-line `trash_emails(...)` command Heath
      can copy.
    """
    service, err = _get_google_service("gmail", "v1")
    if err:
        return f"Gmail not connected: {err}"
    vip = _load_vip_senders_set()
    collaborators = _load_collaborator_emails()
    lab_names = _load_lab_people_names()
    try:
        base_q = f"newer_than:{int(days_back)}d -in:trash -in:sent"
        q = (base_q + " " + extra_query).strip() if extra_query else base_q
        listing = service.users().messages().list(
            userId="me", q=q, maxResults=max(max_candidates * 4, 50),
        ).execute()
        message_refs = listing.get("messages", [])
        if not message_refs:
            return f"No messages found in last {days_back} days."
        candidates: list[dict] = []
        protected_counts = {"VIP": 0, "collaborator": 0, "lab": 0, "domain": 0}
        rule_fail_counts = {"no_junk_signal": 0, "heath_replied": 0}
        for ref in message_refs:
            if len(candidates) >= max_candidates:
                break
            try:
                msg = service.users().messages().get(
                    userId="me", id=ref["id"], format="metadata",
                    metadataHeaders=["From", "Subject", "Date", "List-Unsubscribe",
                                     "List-Id", "Precedence"],
                ).execute()
            except Exception:
                continue
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            from_header = headers.get("From", "")
            display_name, email = _extract_from_address(from_header)
            # Rule 1
            protected, reason1 = _sender_is_protected(from_header, vip, collaborators, lab_names)
            if protected:
                if "VIP" in reason1:
                    protected_counts["VIP"] += 1
                elif "collaborator" in reason1:
                    protected_counts["collaborator"] += 1
                elif "lab" in reason1:
                    protected_counts["lab"] += 1
                else:
                    protected_counts["domain"] += 1
                continue
            # Rule 2
            is_junk, reason2 = _has_junk_signal(headers, display_name, email)
            if not is_junk:
                rule_fail_counts["no_junk_signal"] += 1
                continue
            # Rule 3
            thread_id = msg.get("threadId", "")
            if thread_id and _heath_in_thread(service, thread_id):
                rule_fail_counts["heath_replied"] += 1
                continue
            # Passed all four
            candidates.append({
                "id": ref["id"],
                "from": from_header[:80],
                "subject": headers.get("Subject", "")[:100],
                "date": headers.get("Date", "")[:30],
                "snippet": msg.get("snippet", "")[:120],
                "reason": reason2,
            })
        if not candidates:
            return (
                f"Scanned last {days_back} days — 0 candidates pass all 4 rules.\n"
                f"Protected by rule 1: {protected_counts}  "
                f"Rule 2 fail: {rule_fail_counts['no_junk_signal']}  "
                f"Rule 3 fail: {rule_fail_counts['heath_replied']}"
            )
        # Build preview
        lines = [
            f"## Auto-trash candidates — last {days_back} days",
            f"**{len(candidates)}** messages pass all 4 rules. "
            f"Review below; then call `trash_emails(\"<ids>\", dry_run=False)` to execute.",
            "",
        ]
        ids_csv = ",".join(c["id"] for c in candidates)
        for i, c in enumerate(candidates, 1):
            lines.append(
                f"**[{i}] id=`{c['id']}`**  \n"
                f"From: {c['from']}  \n"
                f"Subject: {c['subject']}  \n"
                f"Date: {c['date']}  |  Rule-2: {c['reason']}  \n"
                f"Preview: {c['snippet']}"
            )
        lines.append("")
        lines.append(f"**Stats:** protected={protected_counts}  "
                     f"rule2_fail={rule_fail_counts['no_junk_signal']}  "
                     f"rule3_fail={rule_fail_counts['heath_replied']}")
        lines.append("")
        lines.append(f"**To trash ALL {len(candidates)}:** "
                     f"`trash_emails(\"{ids_csv}\", dry_run=False)`")
        return "\n".join(lines)
    except Exception as e:
        return f"Error scanning for trash candidates: {type(e).__name__}: {e}"


@tool
def trash_emails(message_ids: str, dry_run: bool = True) -> str:
    """Move the listed Gmail messages to Trash. REVERSIBLE for 30 days via
    Gmail's Trash folder — NOT permanent deletion.

    Applies a last-line defense: for each message, re-checks VIP /
    collaborator / lab / protected-domain. If ANY of those match, the message
    is skipped with a "blocked" status even if the caller asked to trash it.
    Every action (trashed, blocked, skipped, errored) is logged to
    output_ledger with kind='email_trash'.

    Args:
      message_ids: comma-separated Gmail message IDs (from list_recent_emails
        or find_trash_candidates)
      dry_run: if True (default) reports what WOULD happen without trashing.
        Call with dry_run=False to actually execute.

    Returns: per-message status table + summary counts.
    """
    service, err = _get_google_service("gmail", "v1")
    if err:
        return f"Gmail not connected: {err}"
    ids = [i.strip() for i in (message_ids or "").split(",") if i.strip()]
    if not ids:
        return "Error: no message IDs provided."
    vip = _load_vip_senders_set()
    collaborators = _load_collaborator_emails()
    lab_names = _load_lab_people_names()
    rows: list[dict] = []
    trashed = blocked = errored = 0
    for mid in ids:
        try:
            msg = service.users().messages().get(
                userId="me", id=mid, format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            from_h = headers.get("From", "")
            subj = headers.get("Subject", "")[:90]
            protected, reason = _sender_is_protected(from_h, vip, collaborators, lab_names)
            if protected:
                rows.append({"id": mid, "from": from_h[:60], "subject": subj,
                             "status": f"blocked ({reason})"})
                blocked += 1
                _log_trash(mid, from_h, subj, f"blocked: {reason}", dry_run)
                continue
            if dry_run:
                rows.append({"id": mid, "from": from_h[:60], "subject": subj,
                             "status": "dry-run (would trash)"})
                _log_trash(mid, from_h, subj, "dry_run", dry_run=True)
                continue
            service.users().messages().trash(userId="me", id=mid).execute()
            rows.append({"id": mid, "from": from_h[:60], "subject": subj,
                         "status": "trashed"})
            trashed += 1
            _log_trash(mid, from_h, subj, "trashed", dry_run=False)
        except Exception as e:
            rows.append({"id": mid, "from": "", "subject": "",
                         "status": f"error: {type(e).__name__}: {e}"})
            errored += 1
    summary = (
        f"{'DRY-RUN — nothing actually moved' if dry_run else 'LIVE RUN'}  |  "
        f"trashed={trashed}  blocked={blocked}  errored={errored}  total={len(rows)}"
    )
    lines = [f"## trash_emails result", summary, ""]
    for r in rows:
        lines.append(f"- `{r['id']}`  {r['status']}  |  {r['from']}  |  {r['subject']}")
    return "\n".join(lines)


def _log_trash(mid: str, from_h: str, subject: str, outcome: str, dry_run: bool) -> None:
    """Write one output_ledger row per trash attempt."""
    try:
        from agent.ledger import record_output  # noqa: PLC0415
        record_output(
            kind="email_trash",
            job_name="chat_session" if not dry_run else "chat_session_dry_run",
            model="none",
            project_id=None,
            content_md=f"{outcome} | from={from_h[:80]} | subject={subject[:120]}",
            tokens_in=0, tokens_out=0,
            provenance={
                "message_id": mid, "from": from_h[:200], "subject": subject[:200],
                "outcome": outcome, "dry_run": dry_run,
            },
        )
    except Exception as e:
        print(f"[trash_emails] ledger write failed: {e}")


@tool
def list_upcoming_events(days_ahead: int = 7) -> str:
    """List upcoming calendar events. Requires Google credentials to be set up."""
    service, err = _get_google_service("calendar", "v3")
    if err:
        return f"Calendar not connected: {err}"
    try:
        from datetime import timezone, timedelta
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days_ahead)
        events_result = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = events_result.get("items", [])
        if not events:
            return f"No events in the next {days_ahead} days."
        lines = []
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date", "N/A"))
            lines.append(f"- {start}: {e.get('summary', 'No title')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading calendar: {e}"


@tool
def search_drive(query: str, max_results: int = 10) -> str:
    """Search Google Drive for files by name or content. Returns file names, types,
    and links. Use this to find grant drafts, manuscripts, data files, or any lab document."""
    service, err = _get_google_service("drive", "v3")
    if err:
        return f"Drive not connected: {err}"
    try:
        results = service.files().list(
            q=f"fullText contains '{query}' and trashed=false",
            pageSize=max_results,
            fields="files(id, name, mimeType, modifiedTime, webViewLink, parents)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
        ).execute()
        files = results.get("files", [])
        if not files:
            return f"No files found matching '{query}'."
        lines = []
        for f in files:
            mime = f.get("mimeType", "").replace("application/vnd.google-apps.", "")
            modified = f.get("modifiedTime", "N/A")[:10]
            lines.append(
                f"**{f['name']}** ({mime})\n"
                f"Modified: {modified} | {f.get('webViewLink', 'no link')}"
            )
        return "\n\n".join(lines)
    except Exception as e:
        return f"Error searching Drive: {e}"


@tool
def read_drive_file(file_id: str) -> str:
    """Read the text content of a Google Drive file by its ID (from search_drive results).
    Works with Google Docs, plain text, and exported formats."""
    service, err = _get_google_service("drive", "v3")
    if err:
        return f"Drive not connected: {err}"
    try:
        # Get file metadata
        meta = service.files().get(fileId=file_id, fields="name,mimeType", supportsAllDrives=True).execute()
        mime = meta.get("mimeType", "")

        # Export Google Docs as plain text
        if mime == "application/vnd.google-apps.document":
            content = service.files().export(fileId=file_id, mimeType="text/plain").execute()
            text = content.decode("utf-8") if isinstance(content, bytes) else content
        elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            raw = service.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
            with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp_f:
                tmp_f.write(raw)
                tmp_path = tmp_f.name
            try:
                text = _read_docx(tmp_path)
            finally:
                os.unlink(tmp_path)
        elif mime.startswith("text/") or mime == "application/json":
            content = service.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
            text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
        else:
            return f"File '{meta['name']}' is type {mime} — can read Google Docs, .docx, and text files."

        if len(text) > 15000:
            return text[:15000] + f"\n\n[Truncated. Full length: {len(text)} chars]"
        return f"**{meta['name']}**\n\n{text}"
    except Exception as e:
        return f"Error reading file: {e}"


@tool
def list_pdfs_in_drive_folder(folder_id: str, max_results: int = 100) -> list:
    """List all PDF files inside a specific Google Drive folder by folder ID.

    Use this to enumerate PDFs before downloading them. The folder_id comes from
    the URL of a Drive folder, e.g.:
      https://drive.google.com/drive/folders/1RWYmpFJT6ApIfMmfTATDjR-250wi3ZZM
    here the folder_id is '1RWYmpFJT6ApIfMmfTATDjR-250wi3ZZM'.

    Returns a list of dicts with keys: file_id, name, modified_time, size_bytes,
    web_view_link. Returns an empty list on error (permission denied, folder not
    found, etc.) and prints a warning rather than raising.
    """
    service, err = _get_google_service("drive", "v3")
    if err:
        print(f"[list_pdfs_in_drive_folder] Drive not connected: {err}")
        return []
    try:
        q = (
            f"'{folder_id}' in parents "
            f"and mimeType='application/pdf' "
            f"and trashed=false"
        )
        fields = "nextPageToken, files(id, name, mimeType, modifiedTime, size, webViewLink)"
        collected = []
        page_token = None
        while len(collected) < max_results:
            page_size = min(100, max_results - len(collected))
            kwargs = dict(
                q=q,
                pageSize=page_size,
                fields=fields,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            if page_token:
                kwargs["pageToken"] = page_token
            response = service.files().list(**kwargs).execute()
            for f in response.get("files", []):
                collected.append({
                    "file_id": f["id"],
                    "name": f.get("name", ""),
                    "modified_time": f.get("modifiedTime", ""),
                    "size_bytes": int(f["size"]) if f.get("size") else None,
                    "web_view_link": f.get("webViewLink", ""),
                })
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return collected
    except Exception as e:
        print(f"[list_pdfs_in_drive_folder] Warning: {e}")
        return []


@tool
def download_drive_pdf(file_id: str, output_path: str) -> dict:
    """Download a PDF from Google Drive by file ID and save it to output_path.

    Uses streaming MediaIoBaseDownload so large PDFs don't exhaust memory.
    Returns a dict with keys: file_id, output_path, sha256, size_bytes.
    The sha256 is TraitTrawler-style provenance — store it alongside the file
    so you can verify integrity and avoid re-downloading duplicates.

    If the file is not a PDF, returns {"error": "not a PDF", "file_id": file_id}
    without writing anything to disk.
    """
    import io
    import hashlib
    from googleapiclient.http import MediaIoBaseDownload

    service, err = _get_google_service("drive", "v3")
    if err:
        return {"error": f"Drive not connected: {err}", "file_id": file_id}
    try:
        meta = service.files().get(
            fileId=file_id,
            fields="name,mimeType",
            supportsAllDrives=True,
        ).execute()
        if meta.get("mimeType") != "application/pdf":
            return {"error": "not a PDF", "file_id": file_id}

        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        data = buf.getvalue()

        with open(output_path, "wb") as fh:
            fh.write(data)

        sha256 = hashlib.sha256(data).hexdigest()
        return {
            "file_id": file_id,
            "output_path": output_path,
            "sha256": sha256,
            "size_bytes": len(data),
        }
    except Exception as e:
        return {"error": str(e), "file_id": file_id}


@tool
def ingest_paper_to_wiki(
    source_type: str,
    source_value: str,
    title: str = "",
    source_doi: str = "",
    dry_run: bool = True,
    force: bool = False,
) -> str:
    """Ingest one paper into the lab wiki (/knowledge/ on coleoguy.github.io).

    source_type must be one of:
      "drive"  — source_value is a Google Drive file ID (use list_pdfs_in_drive_folder
                 to find IDs). Optional: source_doi attaches a DOI for DB keying.
      "local"  — source_value is an absolute path to a PDF on Heath's Mac.
                 Optional: source_doi attaches a DOI for DB keying.
      "doi"    — source_value is the DOI (e.g. "10.1093/sysbio/syy031"). The paper
                 is fetched via Europe PMC open-access XML. Falls back to run_on_local_pdf
                 or run_on_drive_pdf if the paper is not OA on Europe PMC.

    title: optional — override the title inferred from the PDF or DOI metadata.
    source_doi: optional — attach a DOI to a drive or local source (ignored for "doi").
    dry_run: when True (default), returns a diff preview WITHOUT committing to GitHub.
             Heath reviews the diff and calls again with dry_run=False to commit.
             When False, commits and pushes to origin/main with a [tealc] prefix.
    force:   when True, re-ingests even if the paper's DOI or PDF fingerprint
             is already in the database. Defaults to False so batch runs don't
             waste API credits re-extracting papers already ingested.

    Returns a multi-line human-readable summary including:
      - one-line pipeline status (findings proposed/accepted/rejected, cost)
      - bullet list of /knowledge/ paths written
      - any errors
      - diff preview (first 2000 chars) if dry_run=True
      - commit SHA if dry_run=False and the commit succeeded

    Examples:
      ingest_paper_to_wiki(source_type="doi", source_value="10.1093/sysbio/syy031")
      ingest_paper_to_wiki(source_type="drive", source_value="1Abc...XYZ", dry_run=False)
      ingest_paper_to_wiki(source_type="local",
                           source_value="/Users/blackmon/Downloads/paper.pdf",
                           source_doi="10.1111/evo.12345")
    """
    try:
        from agent.jobs.wiki_pipeline import (  # noqa: PLC0415
            run_on_drive_pdf, run_on_local_pdf, run_on_doi,
        )

        # Normalize empty strings to None so pipeline treats them as "not supplied"
        title_arg = title.strip() if title and title.strip() else None
        doi_arg = source_doi.strip() if source_doi and source_doi.strip() else None

        stype = source_type.strip().lower()
        if stype == "drive":
            result = run_on_drive_pdf(
                file_id=source_value,
                doi=doi_arg,
                title=title_arg,
                dry_run=dry_run,
                force=force,
            )
        elif stype == "local":
            result = run_on_local_pdf(
                pdf_path=source_value,
                doi=doi_arg,
                title=title_arg,
                dry_run=dry_run,
                force=force,
            )
        elif stype == "doi":
            result = run_on_doi(
                doi=source_value,
                dry_run=dry_run,
                force=force,
            )
        else:
            return (
                f"Error: unknown source_type '{source_type}'. "
                "Must be 'drive', 'local', or 'doi'."
            )

        # Build human-readable output
        lines = [result.to_summary_str()]

        # Paths written under /knowledge/
        if result.paths_written:
            lines.append("\nFiles written:")
            for p in result.paths_written:
                lines.append(f"  - {p}")
        else:
            lines.append("\n(No files written.)")

        # Errors section
        if result.errors:
            lines.append("\n⚠ Errors:")
            for e in result.errors:
                lines.append(f"  - {e}")

        # Diff preview (dry_run mode)
        if dry_run and result.diff:
            diff_preview = result.diff[:2000]
            truncated = len(result.diff) > 2000
            lines.append("\nDiff preview:")
            lines.append(diff_preview)
            if truncated:
                lines.append("[diff truncated at 2000 chars]")

        # Commit SHA (live mode)
        if not dry_run and result.committed_sha:
            lines.append(
                f"\nCommitted as {result.committed_sha[:12]} and pushed to origin/main."
            )

        return "\n".join(lines)

    except Exception as exc:
        return (
            f"Error running wiki pipeline (source_type={source_type!r}, "
            f"source_value={source_value!r}): {type(exc).__name__}: {exc}"
        )


@tool
def send_ntfy_to_heath(
    message: str,
    urgency: str = "high",
    title: str = "",
    click_url: str = "",
    tags: str = "",
) -> str:
    """Send a push notification to Heath's phone via ntfy.sh.

    Heath's phone has the free `ntfy` app installed and is subscribed to the
    private topic stored in env var `NTFY_TOPIC`. Tealc POSTs to
    `https://ntfy.sh/<topic>`; the ntfy servers deliver as a push to every
    device subscribed to that topic.

    Use ONLY for time-sensitive alerts — deadline <24h with artifact not
    ready, VIP email needing same-day response, student flagged blocked,
    Tealc operational failure (e.g. API credit <$5). Do NOT use for routine
    informational signals, morning briefings, or anything that already
    reaches Heath via the dashboard or email.

    message: the notification body. Keep under 400 chars. Summarize.
    urgency: "critical" | "high". Only these tiers may text; "medium"/"low"
        must stay in briefings. "critical" maps to ntfy priority 5 (bypasses
        DND); "high" maps to priority 4. Quiet hours block "high" but
        "critical" overrides them.
    title: optional title shown above the message.
    click_url: optional URL; tapping the notification opens this link. Good
        for Gmail thread URLs, Docs links, draft documents.
    tags: optional comma-separated ntfy emoji tags, e.g.
        "warning,envelope" or "rotating_light" for critical alerts.
        Full list: https://docs.ntfy.sh/emojis/

    Rate limits (configurable in data/config.json):
      - ntfy_max_per_day (default 10)
      - ntfy_min_interval_minutes (default 5)
      - ntfy_quiet_hours_central (default 22:00–07:00)

    Every send logged to data/ntfy_log.jsonl (topic redacted).

    Returns: "sent" | "skipped: <reason>" | "error: <reason>"
    """
    import json as _json  # noqa: PLC0415
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td  # noqa: PLC0415

    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if not topic or "PASTE" in topic or "PLACEHOLDER" in topic:
        return (
            "skipped: NTFY_TOPIC not set in .env. Pick a random topic name "
            "(e.g. tealc-blackmon-<random hex>), add it to .env, and "
            "subscribe to that topic in the ntfy app on your phone."
        )

    if urgency not in ("critical", "high"):
        return f"skipped: urgency={urgency!r} — only 'critical' or 'high' may notify"

    # Load config
    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "config.json",
    )
    try:
        with open(cfg_path) as f:
            cfg = _json.load(f)
    except Exception as e:
        return f"error: config.json unreadable: {e}"

    max_per_day = int(cfg.get("ntfy_max_per_day", 10))
    min_interval_minutes = int(cfg.get("ntfy_min_interval_minutes", 5))
    qh = cfg.get("ntfy_quiet_hours_central") or {"start": 22, "end": 7}
    quiet_start = int(qh.get("start", 22))
    quiet_end = int(qh.get("end", 7))

    log_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "ntfy_log.jsonl",
    )

    # Rate-limit checks (read the audit log)
    try:
        recent_sends: list[dict] = []
        if os.path.exists(log_path):
            with open(log_path) as f:
                for line in f:
                    try:
                        recent_sends.append(_json.loads(line))
                    except Exception:
                        pass
    except Exception:
        recent_sends = []

    now = _dt.now(_tz.utc)
    today_sends = [
        s for s in recent_sends
        if s.get("sent_at") and
        _dt.fromisoformat(s["sent_at"]).date() == now.date() and
        s.get("status") == "sent"
    ]
    if len(today_sends) >= max_per_day and urgency != "critical":
        return (
            f"skipped: already sent {len(today_sends)}/{max_per_day} ntfy "
            f"notifications today; urgency={urgency!r} does not override "
            "(only 'critical' can)."
        )

    if recent_sends:
        last = max(
            (s for s in recent_sends if s.get("status") == "sent"),
            key=lambda s: s.get("sent_at", ""),
            default=None,
        )
        if last:
            last_dt = _dt.fromisoformat(last["sent_at"])
            if now - last_dt < _td(minutes=min_interval_minutes):
                if urgency != "critical":
                    return (
                        f"skipped: last ntfy was "
                        f"{int((now - last_dt).total_seconds() / 60)} min ago; "
                        f"min-interval is {min_interval_minutes} min."
                    )

    # Quiet-hours check (Central time)
    try:
        import zoneinfo  # noqa: PLC0415
        central = zoneinfo.ZoneInfo("America/Chicago")
    except Exception:
        central = _tz.utc
    central_hour = _dt.now(central).hour
    in_quiet = (
        central_hour >= quiet_start or central_hour < quiet_end
        if quiet_start > quiet_end
        else quiet_start <= central_hour < quiet_end
    )
    if in_quiet and urgency != "critical":
        return (
            f"skipped: currently quiet hours ({quiet_start:02d}:00–"
            f"{quiet_end:02d}:00 Central); urgency={urgency!r} does not "
            "override (only 'critical' can)."
        )

    # POST to ntfy.sh
    priority_map = {"critical": "5", "high": "4"}
    ntfy_headers = {"Priority": priority_map[urgency]}
    if title:
        ntfy_headers["Title"] = title
    if click_url:
        ntfy_headers["Click"] = click_url
    if tags:
        ntfy_headers["Tags"] = tags

    url = f"https://ntfy.sh/{topic}"
    status = "sent"
    error = None
    try:
        resp = requests.post(
            url, data=message.encode("utf-8"), headers=ntfy_headers, timeout=10,
        )
        if resp.status_code >= 300:
            status = "error"
            error = f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        status = "error"
        error = f"{type(e).__name__}: {e}"

    # Log (topic redacted for privacy)
    log_row = {
        "sent_at": now.isoformat(),
        "topic": f"<redacted:{topic[:6]}…>",
        "urgency": urgency,
        "title": title,
        "message": message,
        "click_url": click_url,
        "tags": tags,
        "status": status,
    }
    if error:
        log_row["error"] = error[:500]
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(log_row) + "\n")
    except Exception:
        pass

    if status == "sent":
        return f"sent ({urgency}) to ntfy topic <redacted:{topic[:6]}…>"
    return f"error: {error[:200] if error else 'unknown'}"


@tool
def read_wiki_handoff() -> str:
    """Read the lab wiki authoring spec (WIKI_HANDOFF.md).

    Use this BEFORE doing any ad-hoc wiki operation that doesn't go through
    ingest_paper_to_wiki — e.g. hand-editing a topic page, adding cross-links,
    renaming a paper, creating a new topic from scratch. The handoff defines
    the required frontmatter fields for every page type (title rule,
    category: map on topic pages, tier: on paper pages, h1-matching rule,
    explicit <a id="finding-N"> anchors, the forbidden sub-index files at
    /knowledge/{papers,topics,repos}/index.md, etc.).

    If you're using the ingest_paper_to_wiki tool instead, you don't need to
    call this — the pipeline encodes all the rules.

    Returns the full handoff markdown. Large (~260 lines) but cached in
    Heath's Google Drive mirror, so reads are fast."""
    handoff_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "WIKI_HANDOFF.md",
    )
    try:
        with open(handoff_path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return (
            f"WIKI_HANDOFF.md not found at {handoff_path}. This means the "
            "wiki-authoring spec is missing — escalate to Heath rather than "
            "writing to /knowledge/ without it."
        )
    except Exception as e:
        return f"Error reading wiki handoff: {type(e).__name__}: {e}"


_WIKI_TOPICS_DIR = os.path.expanduser(
    "~/Desktop/GitHub/coleoguy.github.io/knowledge/topics"
)


def _parse_wiki_frontmatter(text: str) -> dict:
    """Pull title + category + last_updated from a topic page's YAML frontmatter."""
    meta = {}
    if not text.startswith("---"):
        return meta
    end = text.find("\n---", 3)
    if end == -1:
        return meta
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


@tool
def list_wiki_topics() -> str:
    """List every lab-wiki topic page with its title and category.

    The wiki at ~/Desktop/GitHub/coleoguy.github.io/knowledge/topics/ is Heath's
    structured claim graph — each topic page aggregates findings from his papers
    (and related literature) with anchored cross-links to specific results.

    ALWAYS call this before proposing a hypothesis so you can identify which
    topic pages are relevant to the claim you're about to make — then use
    read_wiki_topic on the most relevant slug(s) to check whether an existing
    finding already supports, refutes, or supersedes the hypothesis.

    Returns a markdown table of slug | title | category, sorted by category
    then slug.
    """
    try:
        if not os.path.isdir(_WIKI_TOPICS_DIR):
            return f"Wiki topics dir not found at {_WIKI_TOPICS_DIR}"
        entries = []
        for fname in sorted(os.listdir(_WIKI_TOPICS_DIR)):
            if not fname.endswith(".md") or fname == "index.md":
                continue
            slug = fname[:-3]
            path = os.path.join(_WIKI_TOPICS_DIR, fname)
            try:
                with open(path, encoding="utf-8") as fh:
                    head = fh.read(2000)
                meta = _parse_wiki_frontmatter(head)
            except Exception:
                meta = {}
            entries.append((
                meta.get("category", "—") or "—",
                slug,
                meta.get("title", slug) or slug,
            ))
        entries.sort()
        if not entries:
            return f"No topic pages under {_WIKI_TOPICS_DIR}."
        lines = [
            f"## Wiki topics ({len(entries)}) — {_WIKI_TOPICS_DIR}",
            "",
            "| slug | title | category |",
            "|------|-------|----------|",
        ]
        for cat, slug, title in entries:
            lines.append(f"| `{slug}` | {title} | {cat} |")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing wiki topics: {type(e).__name__}: {e}"


def _list_wiki_topic_slugs() -> list[str]:
    """Return raw topic slug list — plain helper used by scheduled jobs."""
    if not os.path.isdir(_WIKI_TOPICS_DIR):
        return []
    return sorted(
        f[:-3] for f in os.listdir(_WIKI_TOPICS_DIR)
        if f.endswith(".md") and f != "index.md"
    )


def pick_relevant_wiki_topics(query_text: str, k: int = 3) -> list[str]:
    """Pick up to k topic slugs whose underscored words appear in query_text.

    Used by scheduled jobs (hypothesis generator, etc.) to consult the wiki
    before the Anthropic SDK call, since those jobs don't use tool calling.
    Scoring is simple word-overlap — good enough for the 71-topic wiki.
    """
    query_lower = (query_text or "").lower()
    slugs = _list_wiki_topic_slugs()
    scored = []
    for slug in slugs:
        words = [w for w in slug.replace("_", " ").split() if len(w) >= 4]
        hits = sum(1 for w in words if w in query_lower)
        if hits > 0:
            scored.append((hits, slug))
    scored.sort(reverse=True)
    return [s for _, s in scored[:k]]


def read_wiki_topics_block(slugs: list[str], max_chars_per_topic: int = 8000) -> str:
    """Read multiple topic pages and concatenate as an EXISTING-CLAIMS block.

    Returns '' if no slugs match files. Per-topic content is capped to
    max_chars_per_topic to keep the injected context bounded.
    """
    parts = []
    for slug in slugs:
        path = os.path.join(_WIKI_TOPICS_DIR, f"{slug}.md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            if len(content) > max_chars_per_topic:
                content = content[:max_chars_per_topic] + "\n\n[... truncated ...]"
            parts.append(f"=== EXISTING CLAIMS: {slug} ===\n{content}")
        except Exception:
            continue
    return "\n\n".join(parts)


def known_data_resources_summary() -> str:
    """Return a formatted summary of configured lab data resources.

    Used by the comparative-analysis job to inject available paths/IDs into
    the R-writer's user prompt so it doesn't fabricate file paths or
    reference unset Sheet IDs.
    """
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "known_sheets.json"
    )
    try:
        with open(config_path) as fh:
            known = json.load(fh)
    except Exception:
        return ""
    UNSET = {"", "PASTE_ID", "<TO-BE-FILLED>", "TO-BE-FILLED", "TBD"}
    lines = [
        "AVAILABLE LAB DATA RESOURCES — use these exact paths/IDs. "
        "Do NOT invent file paths or use placeholders like PASTE_ID."
    ]
    for key in sorted(known.keys()):
        entry = known[key]
        if isinstance(entry, str):
            if entry.strip() not in UNSET:
                lines.append(f"  - `{key}` (google_sheet): {entry.strip()}")
        elif isinstance(entry, dict):
            kind = entry.get("kind", "unknown")
            notes = (entry.get("notes") or "")[:100]
            if kind in ("local_csv", "local_json"):
                path = (entry.get("path") or "").strip()
                if path:
                    rows = entry.get("rows")
                    cols = entry.get("cols")
                    meta = f" ({rows}×{cols})" if rows and cols else ""
                    lines.append(f"  - `{key}`{meta}: {path}  — {notes}")
            elif kind == "google_sheet":
                sid = (entry.get("id") or "").strip()
                if sid not in UNSET:
                    lines.append(f"  - `{key}` (google_sheet): {sid}  — {notes}")
            # kind='unknown' is intentionally omitted — do not list unresolved resources
    return "\n".join(lines)


@tool
def retrieve_voice_exemplars(query: str, k: int = 4) -> str:
    """Retrieve Heath's own prose exemplars from the curated voice corpus for
    style-matching when drafting extended prose.

    Use this BEFORE writing any extended prose meant to read as Heath's voice:
    grant sections, addenda, emails to collaborators, manuscript drafts,
    lab-website updates, rebuttals, cover letters. The retrieved exemplars
    come from 169 curated passages drawn from Heath's published-paper
    Discussion/Methods/Conclusion sections, lab-website pages, and active
    grant narratives (Google.org, NIH MIRA, NSF STAR DEB). Each exemplar is
    tagged by register (discussion_section, methods_section, conclusion_section,
    grant_aims, grant_approach, grant_narrative_generic, public_explanatory,
    pedagogical, lab_positional, recruiting) so you can pick the right target.

    Match the exemplars' density, hedging level, and quantitative specificity
    when you write. Do not quote them directly — they are style references.

    Args:
      query: natural-language description of what you're about to write
        (e.g., "grant addendum about autonomous AI scientist infrastructure")
      k: number of exemplars to retrieve (default 4, max 10)

    Returns the formatted exemplar block with register + year tags, or a
    short message if the voice corpus is missing.
    """
    try:
        from agent.voice_index import voice_system_prompt_addendum  # noqa: PLC0415
        k = max(1, min(k, 10))
        block = voice_system_prompt_addendum(query, k=k)
        if not block:
            return (
                "No voice exemplars retrieved. Either data/voice_passages.json "
                "is missing or the query matched nothing. Try a broader query "
                "or check data/voice_passages.json exists."
            )
        return block
    except Exception as e:
        return f"Error retrieving voice exemplars: {type(e).__name__}: {e}"


@tool
def read_wiki_topic(slug: str) -> str:
    """Read a lab-wiki topic page by slug (e.g. 'fragile_y_hypothesis').

    The page contains the current synthesized understanding of the topic, a
    list of anchored findings from Heath's papers, and an explicit
    contradictions section. Read this before proposing a hypothesis in the
    topic's area — many hypotheses worth proposing have already been tested
    and the result is captured here.

    If the slug isn't found, returns a list of available slugs whose name
    contains any word in the requested slug, so you can retry.
    """
    try:
        if not os.path.isdir(_WIKI_TOPICS_DIR):
            return f"Wiki topics dir not found at {_WIKI_TOPICS_DIR}"
        slug_clean = slug.strip().lstrip("/").rstrip("/").replace(".md", "")
        path = os.path.join(_WIKI_TOPICS_DIR, f"{slug_clean}.md")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        # Not found — suggest near matches
        all_slugs = [
            f[:-3] for f in os.listdir(_WIKI_TOPICS_DIR)
            if f.endswith(".md") and f != "index.md"
        ]
        tokens = [t for t in slug_clean.lower().replace("-", "_").split("_") if t]
        candidates = sorted({
            s for s in all_slugs
            if any(tok in s.lower() for tok in tokens)
        })
        if candidates:
            return (
                f"No topic page at slug '{slug_clean}'. "
                f"Near matches: {', '.join(candidates[:15])}. "
                "Call list_wiki_topics for the full list."
            )
        return (
            f"No topic page at slug '{slug_clean}' and no near matches. "
            "Call list_wiki_topics for the full list of {n} slugs.".format(
                n=len(all_slugs)
            )
        )
    except Exception as e:
        return f"Error reading wiki topic: {type(e).__name__}: {e}"


@tool
def read_docx_with_comments(path: str) -> str:
    """Read a .docx file and include all reviewer comments inline as
    [COMMENT by NAME: text] markers. Use this for grant drafts or manuscripts
    under collaborative review so you can see both the body and reviewer notes."""
    try:
        if not path.startswith("/"):
            path = os.path.join(DRIVE_ROOT, path)
        from docx import Document
        from docx.oxml.ns import qn
        doc = Document(path)
        # Build comments dict from the comments part if it exists
        comments: dict = {}
        try:
            # Walk rels to find the comments part
            comments_rel_id = next(
                rid for rid, r in doc.part.rels.items()
                if "comments" in r.reltype
            )
            cpart = doc.part.related_parts[comments_rel_id]
            for c in cpart.element.findall(qn("w:comment")):
                cid = c.get(qn("w:id"))
                author = c.get(qn("w:author"), "?")
                text = "".join(t.text or "" for t in c.iter(qn("w:t")))
                comments[cid] = (author, text)
        except (StopIteration, KeyError):
            # No comments part, or rels structure differs — graceful fallback
            pass
        out = []
        for p in doc.paragraphs:
            line = p.text
            for cref in p._element.iter(qn("w:commentReference")):
                cid = cref.get(qn("w:id"))
                if cid in comments:
                    author, ctext = comments[cid]
                    line += f" [COMMENT by {author}: {ctext}]"
            out.append(line)
        result = "\n".join(out)
        if not comments:
            result += "\n\n[No reviewer comments found in this document.]"
        return result
    except Exception as e:
        # Fallback: basic body extraction via mammoth if python-docx fails
        try:
            return _read_docx(path) + "\n\n[Note: comment extraction not yet implemented]"
        except Exception:
            return f"Error reading docx: {e}"


@tool
def notify_heath(level: str, title: str, body: str) -> str:
    """Push a notification to Heath.
    level: 'info' (log only), 'warn' (desktop notification banner),
    'critical' (desktop banner + email to blackmon@tamu.edu).
    Reserve 'critical' for true emergencies — overnight grant collapse,
    missed-deadline-imminent. Default to 'warn' for things he should see today.
    Do NOT use as a substitute for chat — notifications are for async reach."""
    from agent.notify import notify as _notify
    return _notify(level, title, body)


# ---------------------------------------------------------------------------
# Task 5 — Google Docs write-back
# ---------------------------------------------------------------------------

def _docs_service():
    return _get_google_service("docs", "v1")


def _drive_service():
    return _get_google_service("drive", "v3")


def _insert_markdown(docs, doc_id: str, text: str, index: int = 1):
    """Insert markdown text into a Google Doc starting at index.
    Handles # HEADING_1, ## HEADING_2, blank lines, and plain text.
    Simple version: no bold/italic/lists — under 30 lines total."""
    requests_list = []
    pos = index
    for line in text.split("\n"):
        if line.startswith("# "):
            content = line[2:] + "\n"
            style = "HEADING_1"
        elif line.startswith("## "):
            content = line[3:] + "\n"
            style = "HEADING_2"
        else:
            content = line + "\n"
            style = "NORMAL_TEXT"
        requests_list.append({"insertText": {"location": {"index": pos}, "text": content}})
        if style != "NORMAL_TEXT":
            requests_list.append({"updateParagraphStyle": {
                "range": {"startIndex": pos, "endIndex": pos + len(content)},
                "paragraphStyle": {"namedStyleType": style},
                "fields": "namedStyleType",
            }})
        pos += len(content)
    if requests_list:
        docs.documents().batchUpdate(documentId=doc_id,
                                     body={"requests": requests_list}).execute()


@tool
def create_google_doc(title: str, body_markdown: str = "", parent_folder_id: str = "") -> str:
    """Create a new Google Doc with optional initial markdown content.
    parent_folder_id defaults to the Tealc Drafts folder from config.
    Returns 'doc_id|url'."""
    docs, err = _docs_service()
    if err:
        return f"Docs not connected: {err}"
    drive, drive_err = _drive_service()
    try:
        doc = docs.documents().create(body={"title": title}).execute()
        doc_id = doc["documentId"]
        if not parent_folder_id:
            try:
                cfg = json.load(open(CONFIG_PATH))
                parent_folder_id = cfg.get("tealc_drafts_folder_id", "")
            except Exception:
                parent_folder_id = ""
        if parent_folder_id and parent_folder_id != "PASTE_DRIVE_FOLDER_ID_HERE":
            if drive_err:
                return f"Error: Drive not connected (could not move doc to folder): {drive_err}"
            drive.files().update(
                fileId=doc_id,
                addParents=parent_folder_id,
                fields="id, parents",
                supportsAllDrives=True,
            ).execute()
        if body_markdown:
            _insert_markdown(docs, doc_id, body_markdown, index=1)
        url = f"https://docs.google.com/document/d/{doc_id}/edit"
        return f"{doc_id}|{url}"
    except Exception as e:
        return f"Error ({type(e).__name__}): {str(e)[:200]}"


@tool
def append_to_google_doc(doc_id: str, text: str, heading: str = "") -> str:
    """Append text to the end of a Google Doc. If heading is given, insert as
    Heading 2 first."""
    docs, err = _docs_service()
    if err:
        return f"Docs not connected: {err}"
    try:
        doc = docs.documents().get(documentId=doc_id).execute()
        end_idx = doc["body"]["content"][-1]["endIndex"] - 1
        requests_list = []
        if heading:
            requests_list += [
                {"insertText": {"location": {"index": end_idx}, "text": f"\n{heading}\n"}},
                {"updateParagraphStyle": {
                    "range": {"startIndex": end_idx + 1,
                              "endIndex": end_idx + 1 + len(heading)},
                    "paragraphStyle": {"namedStyleType": "HEADING_2"},
                    "fields": "namedStyleType",
                }},
            ]
            end_idx += 2 + len(heading)
        requests_list.append({"insertText": {"location": {"index": end_idx},
                                              "text": "\n" + text}})
        docs.documents().batchUpdate(documentId=doc_id,
                                      body={"requests": requests_list}).execute()
        return f"Appended {len(text)} chars to doc {doc_id}"
    except Exception as e:
        return f"Error ({type(e).__name__}): {str(e)[:200]}"


@tool
def replace_in_google_doc(doc_id: str, find: str, replace: str,
                           all_occurrences: bool = False,
                           confirmed: bool = False) -> str:
    """Replace text in a Google Doc. Returns count of replacements made.
    DO NOT use blindly — always re-read the doc first to confirm `find`
    is still present and unique.
    First call (confirmed=False) returns a PREVIEW; call again with confirmed=True to execute."""
    docs, err = _docs_service()
    if err:
        return f"Docs not connected: {err}"
    if not confirmed:
        try:
            doc = docs.documents().get(documentId=doc_id).execute()
            title = doc.get("title", doc_id)
            # Extract plain text from the doc body
            body_text = ""
            for elem in doc.get("body", {}).get("content", []):
                for pe in elem.get("paragraph", {}).get("elements", []):
                    body_text += pe.get("textRun", {}).get("content", "")
            n = body_text.count(find)
            if n == 0:
                return (f"PREVIEW: No occurrences of {find!r} found in doc '{title}' — "
                        f"nothing to replace. No confirmation needed.")
            return (f"PREVIEW: Would replace {n} occurrence(s) of {find!r} with {replace!r} "
                    f"in doc '{title}'. To proceed, call again with confirmed=True. "
                    f"To cancel, do nothing.")
        except Exception as e:
            return f"Error fetching preview ({type(e).__name__}): {str(e)[:200]}"
    try:
        res = docs.documents().batchUpdate(documentId=doc_id, body={"requests": [{
            "replaceAllText": {
                "containsText": {"text": find, "matchCase": True},
                "replaceText": replace,
            }
        }]}).execute()
        n = res["replies"][0]["replaceAllText"].get("occurrencesChanged", 0)
        return f"Replaced {n} occurrence(s) of {find[:40]!r}"
    except Exception as e:
        return f"Error ({type(e).__name__}): {str(e)[:200]}"


@tool
def insert_comment_in_google_doc(doc_id: str, anchor_text: str, comment: str) -> str:
    """Add a margin comment anchored to the first occurrence of anchor_text.
    Use for suggestions Heath should consider rather than direct edits."""
    try:
        return "comment insertion deferred — use replace_in_google_doc to edit instead"
    except Exception as e:
        return f"Error ({type(e).__name__}): {str(e)[:200]}"


# ---------------------------------------------------------------------------
# Task 6 — Calendar write access
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    try:
        return json.load(open(CONFIG_PATH))
    except Exception:
        return {"working_hours_central": {"start": "09:00", "end": "18:00"}}


@tool
def create_calendar_event(
    title: str, start_iso: str, end_iso: str,
    description: str = "", location: str = "",
    attendee_emails: str = "",
    send_invitations: bool = False,
) -> str:
    """Create a Google Calendar event. start_iso/end_iso are ISO 8601 with timezone
    (e.g. '2026-04-22T14:00:00-05:00'). attendee_emails is a comma-separated string.
    send_invitations MUST stay False unless Heath has explicitly approved sending invites."""
    cal, err = _get_google_service("calendar", "v3")
    if err:
        return f"Calendar not connected: {err}"
    body = {
        "summary": title,
        "description": description,
        "location": location,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    if attendee_emails:
        body["attendees"] = [{"email": e.strip()}
                             for e in attendee_emails.split(",") if e.strip()]
    try:
        e = cal.events().insert(
            calendarId="primary", body=body,
            sendUpdates="all" if send_invitations else "none",
        ).execute()
        return f"{e['id']}|{e.get('htmlLink', '')}"
    except Exception as e:
        return f"Error ({type(e).__name__}): {str(e)[:200]}"


@tool
def update_calendar_event(event_id: str, title: str = "", start_iso: str = "",
                           end_iso: str = "", description: str = "") -> str:
    """Update an existing calendar event. Only non-empty args overwrite existing fields."""
    cal, err = _get_google_service("calendar", "v3")
    if err:
        return f"Calendar not connected: {err}"
    try:
        e = cal.events().get(calendarId="primary", eventId=event_id).execute()
        if title:
            e["summary"] = title
        if description:
            e["description"] = description
        if start_iso:
            e["start"] = {"dateTime": start_iso}
        if end_iso:
            e["end"] = {"dateTime": end_iso}
        cal.events().update(calendarId="primary", eventId=event_id, body=e,
                            sendUpdates="none").execute()
        return f"Updated event {event_id}"
    except Exception as e:
        return f"Error ({type(e).__name__}): {str(e)[:200]}"


@tool
def delete_calendar_event(event_id: str, confirmed: bool = False) -> str:
    """Delete a calendar event. Never sends cancellation notices to attendees.
    First call (confirmed=False) returns a PREVIEW; call again with confirmed=True to execute."""
    cal, err = _get_google_service("calendar", "v3")
    if err:
        return f"Calendar not connected: {err}"
    if not confirmed:
        try:
            e = cal.events().get(calendarId="primary", eventId=event_id).execute()
            summary = e.get("summary", "(no title)")
            start = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", "unknown")
            attendees = e.get("attendees", [])
            n_att = len(attendees)
            return (f"PREVIEW: Would delete event '{summary}' starting {start} "
                    f"({n_att} attendee(s)). To proceed, call again with confirmed=True. "
                    f"To cancel, do nothing.")
        except Exception as e:
            return f"Error fetching preview ({type(e).__name__}): {str(e)[:200]}"
    try:
        cal.events().delete(calendarId="primary", eventId=event_id,
                            sendUpdates="none").execute()
        return f"Deleted event {event_id}"
    except Exception as e:
        return f"Error ({type(e).__name__}): {str(e)[:200]}"


@tool
def find_free_slots(duration_minutes: int, earliest_iso: str, latest_iso: str,
                    working_hours_only: bool = True) -> str:
    """Return up to 10 free time slots of the requested length within the window.
    Respects working hours (09:00-18:00 Central) when working_hours_only is True."""
    from datetime import timezone, timedelta
    cal, err = _get_google_service("calendar", "v3")
    if err:
        return f"Calendar not connected: {err}"
    try:
        fb = cal.freebusy().query(body={
            "timeMin": earliest_iso,
            "timeMax": latest_iso,
            "items": [{"id": "primary"}],
        }).execute()
        busy = fb["calendars"]["primary"]["busy"]

        cfg = _load_config()
        wh = cfg.get("working_hours_central", {"start": "09:00", "end": "18:00"})
        wh_start_h, wh_start_m = (int(x) for x in wh["start"].split(":"))
        wh_end_h, wh_end_m = (int(x) for x in wh["end"].split(":"))

        # Parse busy intervals
        busy_intervals = []
        for b in busy:
            s = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
            e = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
            busy_intervals.append((s, e))
        busy_intervals.sort(key=lambda x: x[0])

        # Central = UTC-5 (standard) / UTC-6 (daylight) — use fixed UTC-5 approximation
        central_offset = timedelta(hours=-5)

        window_start = datetime.fromisoformat(earliest_iso.replace("Z", "+00:00"))
        window_end = datetime.fromisoformat(latest_iso.replace("Z", "+00:00"))
        slot_len = timedelta(minutes=duration_minutes)

        free_slots = []
        cursor = window_start
        for bs, be in busy_intervals + [(window_end, window_end)]:
            # Gap between cursor and next busy block
            if bs > cursor and bs - cursor >= slot_len:
                # Enumerate slots within the gap
                s = cursor
                while s + slot_len <= bs:
                    if working_hours_only:
                        local = s + central_offset
                        day_start = local.replace(hour=wh_start_h, minute=wh_start_m,
                                                  second=0, microsecond=0)
                        day_end = local.replace(hour=wh_end_h, minute=wh_end_m,
                                                second=0, microsecond=0)
                        slot_local_end = (s + slot_len) + central_offset
                        if local >= day_start and slot_local_end <= day_end:
                            free_slots.append((s, s + slot_len))
                    else:
                        free_slots.append((s, s + slot_len))
                    if len(free_slots) >= 10:
                        break
                    s += slot_len
            if len(free_slots) >= 10:
                break
            cursor = max(cursor, be)

        if not free_slots:
            return "No free slots found in the requested window."
        lines = []
        for s, e in free_slots:
            lines.append(f"- {s.isoformat()} → {e.isoformat()}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error ({type(e).__name__}): {str(e)[:200]}"


# ---------------------------------------------------------------------------
# Task 8 — Google Sheets read/write
# ---------------------------------------------------------------------------

def _sheet_id(name_or_id: str) -> str:
    """Resolve a friendly sheet name to its spreadsheet ID."""
    if "/" in name_or_id or len(name_or_id) > 30:
        return name_or_id  # looks like a real spreadsheet ID
    try:
        known = json.load(open(KNOWN_SHEETS_PATH))
        return known.get(name_or_id, name_or_id)
    except Exception:
        return name_or_id


@tool
def list_sheets_in_spreadsheet(spreadsheet_id: str) -> str:
    """Return the tab names and dimensions for a spreadsheet.
    spreadsheet_id can be a friendly name from data/known_sheets.json."""
    svc, err = _get_google_service("sheets", "v4")
    if err:
        return f"Sheets not connected: {err}"
    try:
        sid = _sheet_id(spreadsheet_id)
        meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
        return "\n".join(
            f"- {s['properties']['title']} "
            f"({s['properties']['gridProperties']['rowCount']} rows × "
            f"{s['properties']['gridProperties']['columnCount']} cols)"
            for s in meta["sheets"]
        )
    except Exception as e:
        return f"Error ({type(e).__name__}): {str(e)[:200]}"


@tool
def read_sheet(spreadsheet_id: str, range_a1: str, max_rows: int = 500) -> str:
    """Read a range like 'Coleoptera!A1:N500'. Returns JSON list-of-lists.
    spreadsheet_id can be a friendly name from data/known_sheets.json."""
    svc, err = _get_google_service("sheets", "v4")
    if err:
        return f"Sheets not connected: {err}"
    try:
        sid = _sheet_id(spreadsheet_id)
        res = svc.spreadsheets().values().get(spreadsheetId=sid, range=range_a1).execute()
        values = res.get("values", [])[:max_rows]
        return json.dumps(values, indent=2)[:15000]
    except Exception as e:
        return f"Error ({type(e).__name__}): {str(e)[:200]}"


@tool
def append_rows_to_sheet(spreadsheet_id: str, sheet_name: str, rows_json: str) -> str:
    """Append rows to a sheet. rows_json is a JSON-encoded list of lists.
    Uses USER_ENTERED so formulas work.
    spreadsheet_id can be a friendly name from data/known_sheets.json."""
    svc, err = _get_google_service("sheets", "v4")
    if err:
        return f"Sheets not connected: {err}"
    try:
        sid = _sheet_id(spreadsheet_id)
        rows = json.loads(rows_json)
        svc.spreadsheets().values().append(
            spreadsheetId=sid, range=f"{sheet_name}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        ).execute()
        return f"Appended {len(rows)} row(s) to {sheet_name}"
    except Exception as e:
        return f"Error ({type(e).__name__}): {str(e)[:200]}"


@tool
def update_sheet_cells(spreadsheet_id: str, range_a1: str, values_json: str,
                       confirmed: bool = False) -> str:
    """Overwrite specific cells. NEVER call without first reading the range
    and showing Heath what will change.
    spreadsheet_id can be a friendly name from data/known_sheets.json.
    First call (confirmed=False) returns a PREVIEW; call again with confirmed=True to execute."""
    svc, err = _get_google_service("sheets", "v4")
    if err:
        return f"Sheets not connected: {err}"
    if not confirmed:
        try:
            sid = _sheet_id(spreadsheet_id)
            new_values = json.loads(values_json)
            n_cells = sum(len(r) for r in new_values)
            # Derive sheet name from range_a1 (e.g. "Sheet1!A1:B3" → "Sheet1")
            sheet_name = range_a1.split("!")[0] if "!" in range_a1 else range_a1
            # Read current values in the target range
            res = svc.spreadsheets().values().get(
                spreadsheetId=sid, range=range_a1
            ).execute()
            current_values = res.get("values", [])
            def _fmt(rows):
                preview = rows[:3]
                s = json.dumps(preview)
                if len(rows) > 3:
                    s += " ..."
                return s[:300]
            return (f"PREVIEW: Would overwrite {n_cells} cell(s) in "
                    f"'{sheet_name}'!{range_a1.split('!')[-1] if '!' in range_a1 else range_a1}. "
                    f"Current values: {_fmt(current_values)}. "
                    f"New values: {_fmt(new_values)}. "
                    f"To proceed, call again with confirmed=True. To cancel, do nothing.")
        except Exception as e:
            return f"Error fetching preview ({type(e).__name__}): {str(e)[:200]}"
    try:
        sid = _sheet_id(spreadsheet_id)
        values = json.loads(values_json)
        svc.spreadsheets().values().update(
            spreadsheetId=sid, range=range_a1,
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()
        return f"Updated {range_a1} with {sum(len(r) for r in values)} cell(s)"
    except Exception as e:
        return f"Error ({type(e).__name__}): {str(e)[:200]}"


@tool
def search_sheet(spreadsheet_id: str, sheet_name: str, query: str,
                 column: str = "") -> str:
    """Find rows containing query (optionally in one column letter like 'B').
    Returns row numbers + matched content. Searches up to 5000 rows.
    spreadsheet_id can be a friendly name from data/known_sheets.json."""
    svc, err = _get_google_service("sheets", "v4")
    if err:
        return f"Sheets not connected: {err}"
    try:
        sid = _sheet_id(spreadsheet_id)
        res = svc.spreadsheets().values().get(
            spreadsheetId=sid, range=f"{sheet_name}!A1:Z5000"
        ).execute()
        rows = res.get("values", [])
        matches = []
        for i, row in enumerate(rows, 1):
            if column and len(row) > (ord(column.upper()) - 65):
                cells = [row[ord(column.upper()) - 65]]
            else:
                cells = row
            if any(query.lower() in str(c).lower() for c in cells):
                matches.append(f"Row {i}: {row[:8]}")
            if len(matches) >= 30:
                break
        return "\n".join(matches) if matches else "No matches."
    except Exception as e:
        return f"Error ({type(e).__name__}): {str(e)[:200]}"


# ---------------------------------------------------------------------------
# Task 7 — R script execution
# ---------------------------------------------------------------------------

import subprocess
from pathlib import Path

R_RUNS_DIR = Path(os.path.dirname(__file__)).parent / "data" / "r_runs"
R_PREAMBLE = Path(os.path.dirname(__file__)) / "r_runtime" / "preamble.R"


@tool
def run_r_script(code: str, libraries: str = "",
                 working_dir: str = "", timeout_seconds: int = 300) -> str:
    """Execute R code. libraries is a comma-separated string of packages
    to load (e.g. 'ape,phytools'). If working_dir is empty, a fresh
    timestamped dir under data/r_runs/ is created. Returns JSON with
    stdout, stderr, exit_code, working_dir, plot_paths, created_files."""
    # Prefer homebrew Rscript; fall back to which
    rbin = None
    if os.path.isfile("/opt/homebrew/bin/Rscript") and os.access("/opt/homebrew/bin/Rscript", os.X_OK):
        rbin = "/opt/homebrew/bin/Rscript"
    else:
        probe = subprocess.run(["which", "Rscript"], capture_output=True, text=True)
        if probe.returncode == 0 and probe.stdout.strip():
            rbin = probe.stdout.strip()
    if not rbin:
        return json.dumps({"error": "Rscript not found. Run setup_r.sh first."})

    if working_dir:
        wd = Path(working_dir)
    else:
        wd = R_RUNS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
    wd.mkdir(parents=True, exist_ok=True)

    # Compose script
    libs = [l.strip() for l in libraries.split(",") if l.strip()]
    lib_block = "\n".join([f'suppressPackageStartupMessages(library({l}))'
                            for l in libs])
    preamble = R_PREAMBLE.read_text() if R_PREAMBLE.exists() else ""
    script = f'setwd("{wd}")\n{preamble}\n{lib_block}\n\n{code}\n'
    script_path = wd / "script.R"
    script_path.write_text(script)

    files_before = set(p.name for p in wd.iterdir())
    try:
        proc = subprocess.run([rbin, str(script_path)],
            capture_output=True, text=True, timeout=timeout_seconds, cwd=str(wd))
        stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"Timed out after {timeout_seconds}s",
                           "working_dir": str(wd)})

    files_after = [p for p in wd.iterdir() if p.name not in files_before]
    plots = [str(p) for p in files_after if p.suffix in {".png", ".pdf", ".svg", ".jpg"}]
    others = [str(p) for p in files_after if p not in [Path(x) for x in plots]]

    return json.dumps({
        "stdout": stdout[-3000:], "stderr": stderr[-2000:],
        "exit_code": rc, "working_dir": str(wd),
        "plot_paths": plots, "created_files": others,
    }, indent=2)


# ---------------------------------------------------------------------------
# Task 10 — Student milestone tracker
# ---------------------------------------------------------------------------

def _get_student_db():
    """Return a connection with the student tables guaranteed to exist."""
    from agent.scheduler import _migrate  # noqa: PLC0415
    _migrate()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _resolve_student(conn, name: str):
    """Return (id, full_name) for the closest name match, or (None, None)."""
    name_l = name.strip().lower()
    # Exact full_name match
    row = conn.execute(
        "SELECT id, full_name FROM students WHERE LOWER(full_name)=?", (name_l,)
    ).fetchone()
    if row:
        return row
    # Exact short_name match
    row = conn.execute(
        "SELECT id, full_name FROM students WHERE LOWER(short_name)=?", (name_l,)
    ).fetchone()
    if row:
        return row
    # Substring on full_name
    row = conn.execute(
        "SELECT id, full_name FROM students WHERE LOWER(full_name) LIKE ?",
        (f"%{name_l}%",),
    ).fetchone()
    if row:
        return row
    return None, None


@tool
def list_students(role: str = "", status: str = "active") -> str:
    """List students in the lab. Filter by role (PhD, PostBacc, Staff, UG, Alumni)
    and/or status (active, graduated, left). Leave role empty to list all roles."""
    try:
        conn = _get_student_db()
        query = "SELECT full_name, role, status, primary_project FROM students WHERE 1=1"
        params = []
        if status:
            query += " AND status=?"
            params.append(status)
        if role:
            query += " AND role=?"
            params.append(role)
        query += " ORDER BY role, full_name"
        rows = conn.execute(query, params).fetchall()
        conn.close()
        if not rows:
            return "No students found matching those filters."
        lines = []
        current_role = None
        for full_name, r, s, project in rows:
            if r != current_role:
                lines.append(f"\n**{r}**")
                current_role = r
            proj = f" — {project}" if project else ""
            lines.append(f"  • {full_name} ({s}){proj}")
        return "\n".join(lines).strip()
    except Exception as e:
        return f"Error listing students: {e}"


@tool
def student_dashboard(name: str) -> str:
    """Full picture for one student: role, project, milestones (done + upcoming),
    recent interactions, and days since last interaction. Pass first name or full name."""
    try:
        conn = _get_student_db()
        sid, full_name = _resolve_student(conn, name)
        if sid is None:
            conn.close()
            return f"Student '{name}' not found. Try list_students to see names."

        row = conn.execute(
            "SELECT role, status, joined_iso, primary_project, notes_md FROM students WHERE id=?",
            (sid,),
        ).fetchone()
        role, status, joined, project, notes = row

        # Milestones
        milestones = conn.execute(
            "SELECT kind, target_iso, completed_iso, notes FROM milestones "
            "WHERE student_id=? ORDER BY target_iso",
            (sid,),
        ).fetchall()

        # Recent interactions (last 10)
        interactions = conn.execute(
            "SELECT occurred_iso, channel, topic, action_items FROM interactions "
            "WHERE student_id=? ORDER BY occurred_iso DESC LIMIT 10",
            (sid,),
        ).fetchall()

        conn.close()

        lines = [f"## {full_name}"]
        lines.append(f"**Role:** {role} | **Status:** {status}")
        if joined:
            lines.append(f"**Joined:** {joined[:10]}")
        if project:
            lines.append(f"**Project:** {project}")

        # Days since last interaction
        if interactions:
            last_iso = interactions[0][0]
            try:
                from datetime import timezone as _tz  # noqa: PLC0415
                last_dt = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
                delta = (datetime.now(_tz.utc) - last_dt).days
                lines.append(f"**Last interaction:** {last_iso[:10]} ({delta} days ago)")
            except Exception:
                lines.append(f"**Last interaction:** {last_iso[:10]}")
        else:
            lines.append("**Last interaction:** none recorded")

        lines.append("\n### Milestones")
        if milestones:
            for kind, target, completed, mnotes in milestones:
                done = f" (completed {completed[:10]})" if completed else ""
                tgt = f" (target: {target[:10]})" if target else ""
                note = f" — {mnotes}" if mnotes else ""
                lines.append(f"  - {kind}{tgt}{done}{note}")
        else:
            lines.append("  None recorded yet.")

        lines.append("\n### Recent Interactions")
        if interactions:
            for occ, channel, topic, actions in interactions:
                ch = f" [{channel}]" if channel else ""
                top = f": {topic}" if topic else ""
                act = f" → {actions}" if actions else ""
                lines.append(f"  - {occ[:10]}{ch}{top}{act}")
        else:
            lines.append("  None recorded yet.")

        if notes:
            lines.append(f"\n### Notes\n{notes}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error loading dashboard: {e}"


@tool
def log_milestone(
    student_name: str,
    kind: str,
    target_iso: str = "",
    completed_iso: str = "",
    notes: str = "",
) -> str:
    """Record a milestone for a student.
    kind: qualifier, proposal, committee, chapter_draft, defense, paper_submission, paper_acceptance, etc.
    target_iso: planned date (YYYY-MM-DD). completed_iso: actual completion date."""
    try:
        conn = _get_student_db()
        sid, full_name = _resolve_student(conn, student_name)
        if sid is None:
            conn.close()
            return f"Student '{student_name}' not found."
        conn.execute(
            "INSERT INTO milestones(student_id, kind, target_iso, completed_iso, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, kind, target_iso or None, completed_iso or None, notes or None),
        )
        conn.commit()
        conn.close()
        status_str = (
            f"completed {completed_iso[:10]}" if completed_iso
            else f"target {target_iso[:10]}" if target_iso
            else "recorded"
        )
        return f"Milestone '{kind}' logged for {full_name} ({status_str})."
    except Exception as e:
        return f"Error logging milestone: {e}"


@tool
def log_interaction(
    student_name: str,
    channel: str,
    topic: str,
    action_items: str = "",
    occurred_iso: str = "",
) -> str:
    """Record a specific interaction with a student.
    channel: 1on1, email, lab_meeting, slack, chat_mention, other.
    occurred_iso: defaults to now if omitted (YYYY-MM-DD or full ISO)."""
    try:
        from datetime import timezone as _tz  # noqa: PLC0415
        conn = _get_student_db()
        sid, full_name = _resolve_student(conn, student_name)
        if sid is None:
            conn.close()
            return f"Student '{student_name}' not found."
        ts = occurred_iso or datetime.now(_tz.utc).isoformat()
        conn.execute(
            "INSERT INTO interactions(student_id, occurred_iso, channel, topic, action_items) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, ts, channel, topic, action_items or None),
        )
        conn.commit()
        conn.close()
        return f"Interaction logged for {full_name} [{channel}]: {topic}"
    except Exception as e:
        return f"Error logging interaction: {e}"


@tool
def students_needing_attention() -> str:
    """Return students who need Heath's attention: (a) no interaction in 14+ days,
    OR (b) milestone due in <30 days, OR (c) overdue milestone. Sorted by urgency.
    Returns human-readable markdown suitable for chat or a briefing."""
    try:
        from datetime import timezone as _tz, timedelta  # noqa: PLC0415
        conn = _get_student_db()
        today = datetime.now(_tz.utc).date()

        # Active students only (PhD, PostBacc, Staff, UG)
        active = conn.execute(
            "SELECT id, full_name, role FROM students WHERE status='active'"
        ).fetchall()

        flags = []  # (urgency_rank, full_name, role, reasons[])

        for sid, full_name, role in active:
            reasons = []

            # Last interaction
            last_row = conn.execute(
                "SELECT occurred_iso FROM interactions WHERE student_id=? "
                "ORDER BY occurred_iso DESC LIMIT 1",
                (sid,),
            ).fetchone()
            if last_row:
                last_iso = last_row[0]
                try:
                    last_date = datetime.fromisoformat(
                        last_iso.replace("Z", "+00:00")
                    ).date()
                    days_ago = (today - last_date).days
                    if days_ago >= 14:
                        reasons.append(f"no interaction in {days_ago} days")
                except Exception:
                    pass
            else:
                reasons.append("no interactions recorded")

            # Milestones
            ms_rows = conn.execute(
                "SELECT kind, target_iso, completed_iso FROM milestones "
                "WHERE student_id=?",
                (sid,),
            ).fetchall()
            for kind, target_iso, completed_iso in ms_rows:
                if completed_iso:
                    continue  # already done
                if not target_iso:
                    continue
                try:
                    target_date = datetime.fromisoformat(target_iso).date()
                    days_until = (target_date - today).days
                    if days_until < 0:
                        reasons.append(f"OVERDUE milestone: {kind} (was {target_iso[:10]})")
                    elif days_until < 30:
                        reasons.append(f"milestone in {days_until} days: {kind} (due {target_iso[:10]})")
                except Exception:
                    pass

            if reasons:
                has_overdue = any("OVERDUE" in r for r in reasons)
                rank = 0 if has_overdue else 1
                flags.append((rank, full_name, role, reasons))

        conn.close()

        if not flags:
            return "No students currently need immediate attention. All recent interactions are up to date."

        flags.sort(key=lambda x: x[0])
        lines = ["## Students Needing Attention\n"]
        for rank, full_name, role, reasons in flags:
            lines.append(f"### {full_name} ({role})")
            for r in reasons:
                lines.append(f"  - {r}")
            lines.append("")

        return "\n".join(lines)
    except Exception as e:
        return f"Error checking student attention: {e}"


def _auto_log_student_mentions(text: str):
    """Internal helper — NOT a tool. Called by app.py after each chat exchange.
    Logs a 'chat_mention' interaction for any active student whose name appears
    (case-insensitive substring) in the combined message + response text."""
    try:
        from datetime import timezone as _tz  # noqa: PLC0415
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        students = conn.execute(
            "SELECT id, full_name, short_name FROM students WHERE status='active'"
        ).fetchall()
        today = datetime.now(_tz.utc).isoformat()
        seen: set = set()
        text_lower = text.lower()
        for sid, full_name, short_name in students:
            for needle in [full_name, short_name or ""]:
                if needle and needle.lower() in text_lower and sid not in seen:
                    conn.execute(
                        "INSERT INTO interactions(student_id, occurred_iso, channel, topic) "
                        "VALUES (?, ?, 'chat_mention', 'mentioned in chat')",
                        (sid, today),
                    )
                    seen.add(sid)
                    break
        conn.commit()
        conn.close()
    except Exception:
        pass  # Never raise — this is a background hook


# ---------------------------------------------------------------------------
# Task 9 — Grant opportunity radar (chat tools)
# ---------------------------------------------------------------------------

@tool
def list_grant_opportunities(min_fit: float = 0.5, days_until_deadline: int = 180) -> str:
    """List grant opportunities surfaced by the weekly grant radar.
    min_fit: minimum fit score 0-1 (default 0.5 = moderate fit).
    days_until_deadline: only show opps with a deadline within this many days,
    OR with no deadline recorded (default 180 = 6 months).
    Returns a markdown table sorted by fit score descending."""
    try:
        from datetime import timezone as _tz, timedelta  # noqa: PLC0415
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        today = datetime.now(_tz.utc).date()
        cutoff = (today + timedelta(days=days_until_deadline)).isoformat()

        rows = conn.execute(
            "SELECT id, source, title, deadline_iso, url, fit_score, fit_reasoning "
            "FROM grant_opportunities "
            "WHERE dismissed=0 AND fit_score >= ? "
            "AND (deadline_iso IS NULL OR deadline_iso <= ?) "
            "ORDER BY fit_score DESC LIMIT 20",
            (min_fit, cutoff),
        ).fetchall()
        conn.close()

        if not rows:
            return (
                f"No grant opportunities found with fit >= {min_fit} "
                f"and deadline within {days_until_deadline} days. "
                "Run `python -m agent.jobs.grant_radar` to refresh."
            )

        lines = [
            f"## Grant Opportunities (fit ≥ {min_fit}, deadline within {days_until_deadline} days)\n"
        ]
        for row_id, source, title, deadline, url, fit, reasoning in rows:
            deadline_str = deadline[:10] if deadline else "unknown"
            lines.append(
                f"### [{row_id}] {title}\n"
                f"**Source:** {source} | **Fit:** {fit:.2f} | **Deadline:** {deadline_str}\n"
                f"**Reasoning:** {reasoning}\n"
                f"**URL:** {url}\n"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing grant opportunities: {e}"


@tool
def dismiss_grant_opportunity(opportunity_id: int, reason: str) -> str:
    """Dismiss a grant opportunity so it no longer appears in future lists.
    opportunity_id: the [N] id shown in list_grant_opportunities.
    reason: brief note on why (e.g. 'not a fit', 'already submitted', 'deadline passed')."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT title FROM grant_opportunities WHERE id=?", (opportunity_id,)
        ).fetchone()
        if not row:
            conn.close()
            return f"Opportunity ID {opportunity_id} not found."
        title = row[0]
        conn.execute(
            "UPDATE grant_opportunities SET dismissed=1 WHERE id=?",
            (opportunity_id,),
        )
        conn.commit()
        conn.close()
        return f"Dismissed opportunity [{opportunity_id}]: '{title[:60]}' — reason: {reason}"
    except Exception as e:
        return f"Error dismissing opportunity: {e}"


# ---------------------------------------------------------------------------
# Pending Intentions Queue — foundation for the always-on executive loop
# ---------------------------------------------------------------------------

_VALID_KINDS = {'follow_up', 'draft', 'research', 'check', 'reminder', 'analysis', 'other'}
_VALID_PRIORITIES = {'low', 'normal', 'high', 'critical'}
_VALID_STATUSES = {'pending', 'in_progress', 'done', 'abandoned'}


@tool
def add_intention(kind: str, description: str, target_iso: str = "",
                  priority: str = "normal", context_json: str = "") -> str:
    """Save something to do later. kind: follow_up | draft | research | check | reminder | analysis | other.
    target_iso: ISO 8601 date or datetime (e.g. '2026-04-25' or '2026-04-25T10:00:00-05:00'). Empty = no target date.
    priority: low | normal | high | critical.
    Returns the new intention ID."""
    try:
        if kind not in _VALID_KINDS:
            return f"Error: invalid kind '{kind}'. Must be one of: {', '.join(sorted(_VALID_KINDS))}"
        if priority not in _VALID_PRIORITIES:
            return f"Error: invalid priority '{priority}'. Must be one of: {', '.join(sorted(_VALID_PRIORITIES))}"
        now = datetime.now().isoformat()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.execute(
            "INSERT INTO intentions(kind, description, target_iso, priority, status, created_by, context_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', 'chat', ?, ?, ?)",
            (kind, description, target_iso or None, priority, context_json or None, now, now),
        )
        new_id = cur.lastrowid
        conn.commit()
        conn.close()
        return f"intention_{new_id} added"
    except Exception as e:
        return f"Error: {e}"


@tool
def list_intentions(status: str = "pending", limit: int = 25) -> str:
    """List intentions filtered by status (pending|in_progress|done|abandoned|all).
    Sorted by priority desc, then target_iso asc (NULLs last)."""
    try:
        priority_order = "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 WHEN 'low' THEN 3 ELSE 4 END"
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        if status == "all":
            rows = conn.execute(
                f"SELECT id, kind, description, target_iso, priority, status, created_by, created_at, completed_at, notes "
                f"FROM intentions ORDER BY {priority_order}, target_iso ASC NULLS LAST LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            if status not in _VALID_STATUSES:
                conn.close()
                return f"Error: invalid status '{status}'. Must be one of: pending, in_progress, done, abandoned, all"
            rows = conn.execute(
                f"SELECT id, kind, description, target_iso, priority, status, created_by, created_at, completed_at, notes "
                f"FROM intentions WHERE status=? ORDER BY {priority_order}, target_iso ASC NULLS LAST LIMIT ?",
                (status, limit),
            ).fetchall()
        conn.close()
        if not rows:
            return f"No intentions with status '{status}'."
        lines = [f"## Intentions — {status} ({len(rows)} shown)\n"]
        for iid, kind, desc, target, priority, st, created_by, created_at, completed_at, notes in rows:
            target_str = f" · due {target[:10]}" if target else ""
            done_str = f" · completed {completed_at[:10]}" if completed_at else ""
            notes_str = f"\n  _Notes: {notes}_" if notes else ""
            lines.append(
                f"**[{iid}]** `{kind}` [{priority.upper()}]{target_str}{done_str}\n"
                f"  {desc}\n"
                f"  _from {created_by} on {created_at[:10]}_"
                f"{notes_str}"
            )
        return "\n\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@tool
def complete_intention(intention_id: int, notes: str = "") -> str:
    """Mark an intention done. Optionally add completion notes."""
    try:
        now = datetime.now().isoformat()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT description, notes FROM intentions WHERE id=?", (intention_id,)
        ).fetchone()
        if not row:
            conn.close()
            return f"Error: intention {intention_id} not found."
        desc, existing_notes = row
        merged_notes = existing_notes or ""
        if notes:
            merged_notes = (merged_notes + "\n" + notes).strip() if merged_notes else notes
        conn.execute(
            "UPDATE intentions SET status='done', completed_at=?, updated_at=?, notes=? WHERE id=?",
            (now, now, merged_notes or None, intention_id),
        )
        conn.commit()
        conn.close()
        return f"intention_{intention_id} marked done: '{desc[:60]}'"
    except Exception as e:
        return f"Error: {e}"


@tool
def abandon_intention(intention_id: int, reason: str) -> str:
    """Mark an intention abandoned. reason is required so we know why."""
    try:
        if not reason or not reason.strip():
            return "Error: reason is required."
        now = datetime.now().isoformat()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT description, notes FROM intentions WHERE id=?", (intention_id,)
        ).fetchone()
        if not row:
            conn.close()
            return f"Error: intention {intention_id} not found."
        desc, existing_notes = row
        merged_notes = (existing_notes + "\nAbandoned: " + reason).strip() if existing_notes else f"Abandoned: {reason}"
        conn.execute(
            "UPDATE intentions SET status='abandoned', updated_at=?, notes=? WHERE id=?",
            (now, merged_notes, intention_id),
        )
        conn.commit()
        conn.close()
        return f"intention_{intention_id} abandoned: '{desc[:60]}' — reason: {reason}"
    except Exception as e:
        return f"Error: {e}"


@tool
def update_intention(intention_id: int, description: str = "", target_iso: str = "",
                     priority: str = "", status: str = "", notes: str = "") -> str:
    """Update fields on an existing intention. Empty args mean don't change."""
    try:
        if priority and priority not in _VALID_PRIORITIES:
            return f"Error: invalid priority '{priority}'. Must be one of: {', '.join(sorted(_VALID_PRIORITIES))}"
        if status and status not in _VALID_STATUSES:
            return f"Error: invalid status '{status}'. Must be one of: {', '.join(sorted(_VALID_STATUSES))}"
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT description, target_iso, priority, status, notes FROM intentions WHERE id=?",
            (intention_id,),
        ).fetchone()
        if not row:
            conn.close()
            return f"Error: intention {intention_id} not found."
        cur_desc, cur_target, cur_priority, cur_status, cur_notes = row
        new_desc = description if description else cur_desc
        new_target = target_iso if target_iso else cur_target
        new_priority = priority if priority else cur_priority
        new_status = status if status else cur_status
        new_notes = notes if notes else cur_notes
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE intentions SET description=?, target_iso=?, priority=?, status=?, notes=?, updated_at=? WHERE id=?",
            (new_desc, new_target, new_priority, new_status, new_notes, now, intention_id),
        )
        conn.commit()
        conn.close()
        return f"intention_{intention_id} updated"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Rolling context snapshot tools
# ---------------------------------------------------------------------------

@tool
def get_idle_class() -> str:
    """Return the current idle classification: active | engaged | idle | deep_idle.
    Used by the executive loop and by chat to understand Heath's current availability.
    active = last chat <30 min; engaged = 30 min–4 hr; idle = 4–24 hr; deep_idle = >24 hr."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT idle_class, hours_since_last_chat FROM current_context WHERE id=1"
        ).fetchone()
        conn.close()
        if row is None:
            return "unknown (context not yet refreshed)"
        idle_class, hours = row
        h_str = f"{hours:.1f}h ago" if hours is not None else "unknown"
        return f"{idle_class or 'unknown'} (last chat: {h_str})"
    except Exception as e:
        return f"Error reading idle class: {e}"


@tool
def get_current_context() -> str:
    """Read the current rolling context snapshot. Returns JSON with: unread briefings count + top items,
    pending high-priority intentions, next deadline, students needing attention, hours since last chat,
    open grant opportunities, current local time/day. Use this when you want a fast situational read
    without running multiple separate queries."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute("SELECT * FROM current_context WHERE id=1").fetchone()
        conn.close()
        if not row is None:
            col_names = [
                "id", "refreshed_at",
                "unsurfaced_briefings_count", "unsurfaced_briefings_top",
                "pending_intentions_count", "pending_intentions_top",
                "next_deadline_name", "next_deadline_iso", "next_deadline_days_remaining",
                "students_needing_attention_count", "students_needing_attention_names",
                "hours_since_last_chat", "open_grant_opportunities_count",
                "current_local_hour", "current_local_day", "is_working_hours", "notes",
            ]
            data = dict(zip(col_names, row))
            # Parse JSON fields for readability
            for key in ("unsurfaced_briefings_top", "pending_intentions_top",
                        "students_needing_attention_names"):
                try:
                    if data.get(key):
                        data[key] = json.loads(data[key])
                except Exception:
                    pass
            return json.dumps(data, indent=2)
        return "context not yet refreshed — run refresh_context_now() or wait up to 10 minutes"
    except Exception as e:
        return f"Error reading context: {e}"


@tool
def refresh_context_now() -> str:
    """Force an immediate refresh of the context snapshot. Useful after major changes
    (e.g., logging a new milestone, adding an intention) to ensure subsequent reads are fresh.
    Normally context refreshes every 10 minutes automatically."""
    try:
        from agent.jobs.refresh_context import job  # noqa: PLC0415
        result = job()
        return result or "context refreshed"
    except Exception as e:
        return f"Error refreshing context: {e}"


@tool
def list_executive_decisions(hours_back: int = 24, limit: int = 30) -> str:
    """Review what Haiku's executive loop has been deciding. Returns recent decisions
    with action, reasoning, and confidence — so Heath can verify Haiku is making sensible
    calls before any actions get promoted to autonomous execution."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cutoff = datetime.utcnow().isoformat()
        # Compute cutoff by subtracting hours_back hours from now
        from datetime import timedelta
        cutoff_dt = datetime.utcnow() - timedelta(hours=hours_back)
        cutoff_iso = cutoff_dt.isoformat()
        rows = conn.execute(
            "SELECT decided_at, action, reasoning, confidence, parse_error, executed "
            "FROM executive_decisions "
            "WHERE decided_at >= ? "
            "ORDER BY decided_at DESC "
            "LIMIT ?",
            (cutoff_iso, limit),
        ).fetchall()
        conn.close()
        if not rows:
            return f"No executive decisions in the last {hours_back} hours."
        lines = [f"## Executive Loop Decisions — last {hours_back}h ({len(rows)} shown)\n"]
        for r in rows:
            decided_at, action, reasoning, confidence, parse_error, executed = r
            conf_str = f"{confidence:.2f}" if confidence is not None else "n/a"
            exec_str = "EXECUTED" if executed else "advisor-only"
            lines.append(
                f"**{decided_at[:19]}**  |  action=`{action}`  |  confidence={conf_str}  |  {exec_str}\n"
                f"> {reasoning}\n"
                + (f"> _parse_error: {parse_error}_\n" if parse_error else "")
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading executive decisions: {e}"


@tool
def list_email_triage_decisions(hours_back: int = 24, classification: str = "") -> str:
    """Review what the email triage subagent has been deciding. Filter by classification
    (ignore/file/drafts_reply/notify/requires_human) or leave empty for all. Returns
    markdown table sorted newest first."""
    try:
        from datetime import timedelta  # noqa: PLC0415
        conn = sqlite3.connect(DB_PATH)
        cutoff_dt = datetime.utcnow() - timedelta(hours=hours_back)
        cutoff_iso = cutoff_dt.isoformat()
        if classification:
            rows = conn.execute(
                "SELECT decided_at, from_email, subject, classification, reasoning, confidence, draft_id, would_notify "
                "FROM email_triage_decisions "
                "WHERE decided_at >= ? AND classification = ? "
                "ORDER BY decided_at DESC",
                (cutoff_iso, classification),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT decided_at, from_email, subject, classification, reasoning, confidence, draft_id, would_notify "
                "FROM email_triage_decisions "
                "WHERE decided_at >= ? "
                "ORDER BY decided_at DESC",
                (cutoff_iso,),
            ).fetchall()
        conn.close()
        if not rows:
            label = f" (classification={classification})" if classification else ""
            return f"No triage decisions in the last {hours_back}h{label}."
        header = f"## Email Triage — last {hours_back}h ({len(rows)} rows)\n\n"
        header += "| Time | From | Subject | Classification | Conf | Draft | Would-Notify | Reasoning |\n"
        header += "|------|------|---------|---------------|------|-------|-------------|----------|\n"
        lines = [header]
        for r in rows:
            decided_at, from_email, subject, cls, reasoning, confidence, draft_id, would_notify = r
            ts = (decided_at or "")[:16]
            frm = (from_email or "")[:30]
            subj = (subject or "")[:40]
            rsn = (reasoning or "")[:60]
            conf_str = f"{confidence:.2f}" if confidence is not None else "n/a"
            draft_str = draft_id or "-"
            notify_str = "yes" if would_notify else "no"
            lines.append(f"| {ts} | {frm} | {subj} | {cls} | {conf_str} | {draft_str} | {notify_str} | {rsn} |")
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading triage decisions: {e}"


@tool
def list_pending_service_requests(days_back: int = 7) -> str:
    """Recent service-request emails Tealc identified, with the NAS-test recommendation
    (accept/decline), reasoning, and the draft Heath can review/edit/send in Gmail Drafts.
    Heath should glance at this list before opening Gmail to make sure no service ask
    slipped through unaddressed."""
    try:
        from datetime import timedelta  # noqa: PLC0415
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        cutoff_dt = datetime.utcnow() - timedelta(days=days_back)
        cutoff_iso = cutoff_dt.isoformat()
        rows = conn.execute(
            "SELECT decided_at, from_email, subject, reasoning, draft_id, service_recommendation "
            "FROM email_triage_decisions "
            "WHERE classification='service_request' AND decided_at >= ? "
            "ORDER BY decided_at DESC",
            (cutoff_iso,),
        ).fetchall()
        conn.close()
        if not rows:
            return f"No service requests identified in the last {days_back} days."
        lines = [f"## Pending Service Requests — last {days_back} days ({len(rows)} found)\n"]
        lines.append(
            "_These all have Gmail drafts ready to review. "
            "Open Gmail Drafts to edit and send (or delete to decline without reply)._\n"
        )
        for decided_at, from_email, subject, reasoning, draft_id, recommendation in rows:
            ts = (decided_at or "")[:16]
            rec = (recommendation or "unknown").upper()
            rec_label = "ACCEPT" if rec == "ACCEPT" else "DECLINE"
            draft_str = f"Draft ID: {draft_id}" if draft_id else "No draft created"
            rsn_short = (reasoning or "")[:200]
            lines.append(
                f"**[{rec_label}]** {ts}\n"
                f"  From: {from_email or 'unknown'}\n"
                f"  Subject: {subject or '(no subject)'}\n"
                f"  NAS reasoning: {rsn_short}\n"
                f"  {draft_str}"
            )
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading service requests: {e}"


@tool
def review_recent_drafts(limit: int = 10) -> str:
    """List recent Gmail drafts Tealc created via email triage so Heath can review and send.
    Returns each draft's ID, recipient, subject, first 200 chars of body, and the original
    message it's replying to."""
    try:
        import base64  # noqa: PLC0415
        service, err = _get_google_service("gmail", "v1")
        if err:
            return f"Gmail not connected: {err}"
        result = service.users().drafts().list(userId="me", maxResults=limit).execute()
        drafts = result.get("drafts", [])
        if not drafts:
            return "No drafts found."

        # Also pull triage DB for context
        conn = sqlite3.connect(DB_PATH)
        triage_rows = conn.execute(
            "SELECT draft_id, message_id, from_email, subject FROM email_triage_decisions "
            "WHERE draft_id IS NOT NULL ORDER BY decided_at DESC LIMIT 50"
        ).fetchall()
        conn.close()
        triage_by_draft = {r[0]: r for r in triage_rows}

        lines = [f"## Recent Tealc Drafts ({len(drafts)} found)\n"]
        for d in drafts:
            draft_id = d["id"]
            try:
                full = service.users().drafts().get(userId="me", id=draft_id, format="full").execute()
                msg = full.get("message", {})
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                to = headers.get("To", "unknown")
                subject = headers.get("Subject", "(no subject)")
                # Extract body
                payload = msg.get("payload", {})
                body_data = payload.get("body", {}).get("data", "")
                if not body_data and payload.get("parts"):
                    body_data = payload["parts"][0].get("body", {}).get("data", "")
                body_text = ""
                if body_data:
                    try:
                        body_text = base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
                    except Exception:
                        body_text = "(undecodable)"
                body_preview = body_text[:200].replace("\n", " ")

                triage_context = ""
                if draft_id in triage_by_draft:
                    _, orig_msg_id, from_email, orig_subject = triage_by_draft[draft_id]
                    triage_context = "\n  Replying to: " + from_email + ' -- "' + orig_subject[:60] + '"'

                lines.append(
                    f"**Draft ID:** {draft_id}\n"
                    f"  To: {to}\n"
                    f"  Subject: {subject}"
                    f"{triage_context}\n"
                    f"  Body preview: {body_preview}..."
                )
            except Exception as exc:
                lines.append(f"**Draft ID:** {draft_id} — error reading: {exc}")
        return "\n\n".join(lines)
    except Exception as e:
        return f"Error reading drafts: {e}"


# ---------------------------------------------------------------------------
# Paper of the day tools
# ---------------------------------------------------------------------------

@tool
def get_paper_of_the_day(date_iso: str = "") -> str:
    """Get today's paper-of-the-day summary, or for a specific date (YYYY-MM-DD).
    Returns title, authors, journal, link, and Tealc's 'why it matters to Heath' summary."""
    try:
        from datetime import timezone as _tz  # noqa: PLC0415
        target = date_iso.strip() if date_iso.strip() else datetime.now(_tz.utc).date().isoformat()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            """SELECT date_iso, title, authors, journal, publication_year,
                      open_access_url, doi, citations_count,
                      why_it_matters_md, topic_matched
               FROM papers_of_the_day WHERE date_iso=?""",
            (target,),
        ).fetchone()
        conn.close()
        if not row:
            return f"No paper of the day found for {target}. The job runs at 6am Central."
        date_iso_r, title, authors, journal, pub_year, oa_url, doi, citations, why, topic = row
        doi_url = f"https://doi.org/{doi}" if doi else ""
        link = oa_url or doi_url or ""
        lines = [
            f"## Paper of the Day -- {date_iso_r}",
            f"**{title}**",
            f"Authors: {authors or 'N/A'}",
            f"Journal: {journal or 'N/A'} ({pub_year or 'N/A'}) | Citations: {citations or 0} | Topic: {topic or 'N/A'}",
        ]
        if link:
            lines.append(f"Link: {link}")
        lines.append("")
        lines.append("**Why this matters to Heath:**")
        lines.append(why)
        return "\n".join(lines)
    except Exception as e:
        return f"Error retrieving paper of the day: {e}"


@tool
def list_recent_papers_of_the_day(days_back: int = 7) -> str:
    """List the past N days of paper-of-the-day picks with one-line summaries each."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            """SELECT date_iso, title, journal, topic_matched, why_it_matters_md
               FROM papers_of_the_day
               ORDER BY date_iso DESC LIMIT ?""",
            (days_back,),
        ).fetchall()
        conn.close()
        if not rows:
            return "No papers of the day found yet. The job runs at 6am Central."
        lines = [f"## Papers of the Day -- last {days_back} days\n"]
        for date_iso_r, title, journal, topic, why in rows:
            first_sentence = (why or "").split(".")[0].strip() + "."
            lines.append(f"**{date_iso_r}** -- *{title[:70]}*")
            lines.append(f"  {journal or 'N/A'} | topic: {topic or 'N/A'}")
            lines.append(f"  {first_sentence[:120]}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing papers of the day: {e}"


@tool
def recall_past_conversations(query: str, days_back: int = 30, limit: int = 5) -> str:
    """Search summaries of past Tealc chat sessions for relevant prior discussions.
    Uses full-text search over the session_summaries table. Returns up to `limit` matches
    sorted by relevance, with date, topics, and a quoted excerpt. Use this when Heath
    references something he discussed before but you don't see it in the current thread."""
    try:
        from datetime import timedelta
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        # Date cutoff
        cutoff_iso = (datetime.utcnow() - timedelta(days=days_back)).isoformat()

        # FTS5 match query
        rows = conn.execute(
            """
            SELECT s.thread_id, s.summary_md, s.topics, s.ended_at
            FROM session_summaries_fts f
            JOIN session_summaries s ON s.id = f.rowid
            WHERE session_summaries_fts MATCH ?
              AND (s.ended_at IS NULL OR s.ended_at >= ?)
              AND s.summary_md NOT LIKE '[%'
            ORDER BY rank
            LIMIT ?
            """,
            (query, cutoff_iso, limit),
        ).fetchall()
        conn.close()

        if not rows:
            return f"No past session summaries matched '{query}' in the last {days_back} days."

        lines = [f"## Past session summaries matching '{query}'\n"]
        for thread_id, summary_md, topics, ended_at in rows:
            date_str = (ended_at or "unknown date")[:10]
            excerpt = summary_md[:400].replace("\n", " ")
            lines.append(f"**{date_str}** | topics: {topics or 'N/A'}")
            lines.append(f"> {excerpt}...")
            lines.append(f"_(thread: {thread_id[:12]}...)_")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Error searching past conversations: {e}"


@tool
def list_recent_sessions(limit: int = 10) -> str:
    """List the most recent Tealc chat sessions with date, topic tags, and one-line summary.
    Useful for 'what did we work on this week'."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            """
            SELECT thread_id, ended_at, message_count, topics, summary_md
            FROM session_summaries
            WHERE summary_md NOT LIKE '[%'
            ORDER BY ended_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()

        if not rows:
            return "No session summaries yet. Sessions are summarized 30+ minutes after they go idle."

        lines = ["## Recent Tealc sessions\n"]
        for thread_id, ended_at, msg_count, topics, summary_md in rows:
            date_str = (ended_at or "unknown")[:10]
            first_sentence = (summary_md or "").split(".")[0].strip() + "."
            lines.append(f"**{date_str}** | {msg_count or '?'} messages | topics: {topics or 'N/A'}")
            lines.append(f"  {first_sentence[:150]}")
            lines.append(f"  _(thread: {thread_id[:12]}...)_")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing recent sessions: {e}"


@tool
def get_latest_weekly_review() -> str:
    """Read the most recent weekly self-review briefing — Tealc's analysis of what worked
    and what didn't over the past week, with recommended rule changes."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT title, content_md, created_at FROM briefings "
            "WHERE kind='weekly_review' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return (
                "No weekly self-review briefing found yet. "
                "The job runs every Sunday at 7pm Central. "
                "Run `python -m agent.jobs.weekly_review` to generate one now."
            )
        title, content_md, created_at = row
        return f"# {title}\n_Generated: {created_at[:19]} UTC_\n\n{content_md}"
    except Exception as e:
        return f"Error reading weekly review: {e}"


@tool
def get_latest_quarterly_retrospective() -> str:
    """Read the most recent quarterly retrospective — Tealc's deep review of the past
    quarter's goal portfolio with recommendations."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT title, content_md, created_at FROM briefings "
            "WHERE kind='quarterly_retrospective' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return (
                "No quarterly retrospective found yet. "
                "The job runs on the first Sunday of Jan/Apr/Jul/Oct at 8pm Central. "
                "Run `python -m agent.jobs.quarterly_retrospective` to generate one now."
            )
        title, content_md, created_at = row
        return f"# {title}\n_Generated: {created_at[:19]} UTC_\n\n{content_md}"
    except Exception as e:
        return f"Error reading quarterly retrospective: {e}"


@tool
def get_latest_nas_metrics() -> str:
    """Read the most recent NAS-metric snapshot: total citations, h-index, i10-index, top recent papers.
    Pulls from the nas_metrics table populated by the weekly track_nas_metrics job."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT snapshot_iso, total_citations, citations_since_2021, "
            "h_index, i10_index, works_count, top_3_recent_papers_json "
            "FROM nas_metrics ORDER BY snapshot_iso DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return (
                "No NAS-metric snapshot yet. "
                "The job runs every Monday at 5:30am Central. "
                "Run `python -m agent.jobs.track_nas_metrics` to generate one now."
            )
        snap_iso, total, since_2021, h, i10, works, top3_json = row
        lines = [
            f"## NAS Metrics snapshot: {snap_iso}",
            f"- Total citations: {total}",
            f"- Citations since 2021: {since_2021}",
            f"- H-index: {h}",
            f"- i10-index: {i10}",
            f"- Works count: {works}",
        ]
        if top3_json:
            try:
                papers = json.loads(top3_json)
                if papers:
                    lines.append("\n**Top 3 most-cited papers (past 3 years):**")
                    for i, p in enumerate(papers, 1):
                        lines.append(
                            f"{i}. {p.get('title', 'N/A')} "
                            f"({p.get('year', '?')}) — {p.get('citations', 0)} citations"
                        )
            except Exception:
                pass
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading NAS metrics: {e}"


@tool
def nas_metrics_trend(weeks_back: int = 12) -> str:
    """Show citation/h-index/i10-index trends over the past N weeks. Returns markdown
    showing snapshot date, total_citations, h_index, i10_index, and the delta vs prior snapshot."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT snapshot_iso, total_citations, h_index, i10_index "
            "FROM nas_metrics ORDER BY snapshot_iso DESC LIMIT ?",
            (weeks_back,),
        ).fetchall()
        conn.close()
        if not rows:
            return (
                "No NAS-metric snapshots yet. "
                "The job runs every Monday at 5:30am Central. "
                "Run `python -m agent.jobs.track_nas_metrics` to generate one now."
            )
        # Reverse to chronological order for delta calculation
        rows = list(reversed(rows))
        lines = [
            f"## NAS Metrics trend (past {len(rows)} week{'s' if len(rows) != 1 else ''})\n",
            "| Date | Citations | Δ cites | H-index | Δ H | i10 | Δ i10 |",
            "|------|-----------|---------|---------|-----|-----|-------|",
        ]
        prev = None
        for snap_iso, total, h, i10 in rows:
            if prev is None:
                d_cites = d_h = d_i10 = "—"
            else:
                p_total, p_h, p_i10 = prev
                d_cites = f"+{total - p_total}" if total - p_total >= 0 else str(total - p_total)
                d_h = f"+{h - p_h}" if (h or 0) - (p_h or 0) >= 0 else str((h or 0) - (p_h or 0))
                d_i10 = f"+{(i10 or 0) - (p_i10 or 0)}" if (i10 or 0) - (p_i10 or 0) >= 0 else str((i10 or 0) - (p_i10 or 0))
            lines.append(
                f"| {snap_iso} | {total} | {d_cites} | {h} | {d_h} | {i10} | {d_i10} |"
            )
            prev = (total, h, i10)
        # Summary sentence
        if len(rows) > 1:
            first_total = rows[0][1] or 0
            last_total = rows[-1][1] or 0
            net = last_total - first_total
            lines.append(
                f"\n_Net change over {len(rows)} weeks: "
                f"{'+'if net >= 0 else ''}{net} citations, "
                f"H-index {rows[0][2]} → {rows[-1][2]}, "
                f"i10 {rows[0][3]} → {rows[-1][3]}_"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading NAS metrics trend: {e}"


# ---------------------------------------------------------------------------
# Task 11 — Goals Sheet tools
# ---------------------------------------------------------------------------

def _get_goals_db():
    """Return a WAL connection with goals tables guaranteed to exist."""
    from agent.jobs.sync_goals_sheet import _migrate_goals_tables  # noqa: PLC0415
    _migrate_goals_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _next_goal_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT id FROM goals ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return "g_001"
    last = row[0]  # e.g. "g_042"
    try:
        num = int(last.split("_")[1]) + 1
    except (IndexError, ValueError):
        num = 1
    return f"g_{num:03d}"


def _next_milestone_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT id FROM milestones_v2 ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return "m_001"
    last = row[0]
    try:
        num = int(last.split("_")[1]) + 1
    except (IndexError, ValueError):
        num = 1
    return f"m_{num:03d}"


@tool
def list_goals(status: str = "active", time_horizon: str = "", min_importance: int = 0) -> str:
    """Read goals from the Goals Sheet (via SQLite mirror). Filter by status (active/proposed/paused/done/dropped/all),
    time_horizon (week/month/quarter/year/career), and minimum importance (1-5, default 0 = all).
    Returns a markdown table sorted by importance descending."""
    try:
        conn = _get_goals_db()
        clauses = []
        params = []
        if status and status != "all":
            clauses.append("status=?")
            params.append(status)
        if time_horizon:
            clauses.append("time_horizon=?")
            params.append(time_horizon)
        if min_importance:
            clauses.append("importance >= ?")
            params.append(min_importance)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT id, name, time_horizon, importance, nas_relevance, status, owner, "
            f"last_touched_iso, success_metric FROM goals {where} "
            f"ORDER BY importance DESC",
            params,
        ).fetchall()
        conn.close()
        if not rows:
            return f"No goals found (status={status}, time_horizon={time_horizon or 'any'}, min_importance={min_importance})."
        header = f"## Goals (status={status}, importance>={min_importance})\n\n"
        header += "| id | name | horizon | imp | NAS | status | owner | last_touched |\n"
        header += "|----|------|---------|-----|-----|--------|-------|-------------|\n"
        lines = [header]
        for gid, name, horizon, imp, nas, gstatus, owner, touched, _ in rows:
            touched_str = (touched or "")[:10]
            lines.append(
                f"| {gid} | {name} | {horizon or ''} | {imp or ''} | {nas or ''} "
                f"| {gstatus or ''} | {owner or ''} | {touched_str} |"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing goals: {e}"


@tool
def get_goal(goal_id: str) -> str:
    """Read full detail of one goal including its milestones and recent decisions linked to it."""
    try:
        conn = _get_goals_db()
        row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not row:
            conn.close()
            return f"Goal {goal_id} not found. Use list_goals() to see available goals."
        col_names = [d[0] for d in conn.execute("SELECT * FROM goals LIMIT 0").description]
        g = dict(zip(col_names, row))

        milestones = conn.execute(
            "SELECT id, milestone, target_iso, status, notes FROM milestones_v2 "
            "WHERE goal_id=? ORDER BY target_iso ASC NULLS LAST",
            (goal_id,),
        ).fetchall()

        decisions = conn.execute(
            "SELECT decided_iso, decision, reasoning, decided_by FROM decisions_log "
            "WHERE linked_goal_id=? ORDER BY decided_iso DESC LIMIT 10",
            (goal_id,),
        ).fetchall()
        conn.close()

        lines = [
            f"## Goal: {g['name']} ({goal_id})",
            f"**Status:** {g.get('status', 'unknown')} | **Horizon:** {g.get('time_horizon', '')} "
            f"| **Importance:** {g.get('importance', '')} | **NAS relevance:** {g.get('nas_relevance', '')}",
            f"**Owner:** {g.get('owner', '')} | **Last touched by:** {g.get('last_touched_by', '')} "
            f"on {(g.get('last_touched_iso') or '')[:10]}",
            "",
            f"**Why:** {g.get('why', '')}",
            f"**Success metric:** {g.get('success_metric', '')}",
        ]
        if g.get("notes"):
            lines.append(f"**Notes:** {g['notes']}")

        if milestones:
            lines.append("\n### Milestones")
            for mid, milestone, target, mstatus, mnotes in milestones:
                target_str = f" (due {target[:10]})" if target else ""
                notes_str = f" — {mnotes}" if mnotes else ""
                lines.append(f"- [{mid}] [{mstatus}] {milestone}{target_str}{notes_str}")

        if decisions:
            lines.append("\n### Recent Decisions")
            for dec_iso, decision, reasoning, dec_by in decisions:
                lines.append(
                    f"- **{dec_iso[:10]}** ({dec_by}): {decision}"
                    + (f"\n  > {reasoning}" if reasoning else "")
                )

        return "\n".join(lines)
    except Exception as e:
        return f"Error getting goal {goal_id}: {e}"


@tool
def add_goal(name: str, time_horizon: str, importance: int, nas_relevance: str = "med",
             success_metric: str = "", why: str = "", owner: str = "joint", notes: str = "") -> str:
    """Add a new goal. Tealc-created goals start as status='proposed'; Heath promotes to 'active' in the Sheet.
    time_horizon: week|month|quarter|year|career. importance: 1-5. nas_relevance: low|med|high."""
    try:
        valid_horizons = {"week", "month", "quarter", "year", "career"}
        if time_horizon not in valid_horizons:
            return f"Error: time_horizon must be one of {sorted(valid_horizons)}"
        if not 1 <= importance <= 5:
            return "Error: importance must be 1-5"
        if nas_relevance not in ("low", "med", "high"):
            return "Error: nas_relevance must be low|med|high"

        conn = _get_goals_db()
        new_id = _next_goal_id(conn)
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO goals
               (id, name, time_horizon, importance, nas_relevance, status, success_metric,
                why, owner, last_touched_by, last_touched_iso, notes, synced_at, tealc_dirty)
               VALUES (?,?,?,?,?,'proposed',?,?,?,?,?,?,?,1)""",
            (new_id, name, time_horizon, importance, nas_relevance,
             success_metric, why, owner, "Tealc", now_iso, notes, now_iso),
        )
        conn.commit()
        conn.close()
        return (
            f"Goal added: {new_id} — '{name}' (status=proposed; Heath promotes to active in the Sheet). "
            f"Will sync to Sheet within 5 minutes."
        )
    except Exception as e:
        return f"Error adding goal: {e}"


@tool
def propose_goal_from_idea(name: str, time_horizon: str, importance: int,
                           success_metric: str = "", why: str = "",
                           nas_relevance: str = "med") -> str:
    """Capture a research-idea-as-proposed-goal. Use AFTER Heath has answered the 3
    capture questions in chat (time_horizon, importance, success_metric).
    Adds to the Goals Sheet as status='proposed' so it appears in his next Sheet view
    where he can promote to 'active' or drop. Returns the new goal_id."""
    try:
        result = add_goal.invoke({
            "name": name,
            "time_horizon": time_horizon,
            "importance": importance,
            "nas_relevance": nas_relevance,
            "success_metric": success_metric,
            "why": why,
            "owner": "joint",
            "notes": "Captured from chat — pending Heath promote",
        })
        # Extract goal_id from the add_goal return string (format: "Goal added: g_NNN — ...")
        import re as _re
        m = _re.search(r"(g_\d+)", result)
        goal_id = m.group(1) if m else "unknown"
        if "Error" in result:
            return result
        return (
            f"Proposed goal {goal_id}: {name}. "
            f"Heath can promote to active in the Goals Sheet."
        )
    except Exception as e:
        return f"Error proposing goal: {e}"


@tool
def update_goal(goal_id: str, name: str = "", time_horizon: str = "", importance: int = 0,
                nas_relevance: str = "", status: str = "", success_metric: str = "",
                why: str = "", owner: str = "", notes: str = "") -> str:
    """Update fields on an existing goal. Only non-empty arguments are changed.
    Sets last_touched_by='Tealc' and last_touched_iso=now. Will sync to Sheet within 5 min."""
    try:
        conn = _get_goals_db()
        row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not row:
            conn.close()
            return f"Goal {goal_id} not found."
        col_names = [d[0] for d in conn.execute("SELECT * FROM goals LIMIT 0").description]
        g = dict(zip(col_names, row))

        # Apply updates — only if argument was provided (non-empty / non-zero)
        if name:
            g["name"] = name
        if time_horizon:
            g["time_horizon"] = time_horizon
        if importance:
            g["importance"] = importance
        if nas_relevance:
            g["nas_relevance"] = nas_relevance
        if status:
            g["status"] = status
        if success_metric:
            g["success_metric"] = success_metric
        if why:
            g["why"] = why
        if owner:
            g["owner"] = owner
        if notes:
            g["notes"] = notes

        now_iso = datetime.now(timezone.utc).isoformat()
        g["last_touched_by"] = "Tealc"
        g["last_touched_iso"] = now_iso

        conn.execute(
            """UPDATE goals SET name=?, time_horizon=?, importance=?, nas_relevance=?,
               status=?, success_metric=?, why=?, owner=?, last_touched_by=?,
               last_touched_iso=?, notes=?, synced_at=?, tealc_dirty=1 WHERE id=?""",
            (
                g["name"], g["time_horizon"], g["importance"], g["nas_relevance"],
                g["status"], g["success_metric"], g["why"], g["owner"],
                g["last_touched_by"], g["last_touched_iso"], g["notes"], now_iso, goal_id,
            ),
        )
        conn.commit()
        conn.close()
        return f"Goal {goal_id} updated. Changes will sync to Sheet within 5 minutes."
    except Exception as e:
        return f"Error updating goal {goal_id}: {e}"


@tool
def add_milestone_to_goal(goal_id: str, milestone: str, target_iso: str = "", notes: str = "") -> str:
    """Add a milestone to an existing goal. target_iso is an optional ISO date (YYYY-MM-DD).
    Milestone starts with status='pending'."""
    try:
        conn = _get_goals_db()
        existing_goal = conn.execute("SELECT name FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not existing_goal:
            conn.close()
            return f"Goal {goal_id} not found. Use list_goals() to see available goals."
        new_id = _next_milestone_id(conn)
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO milestones_v2
               (id, goal_id, milestone, target_iso, status, notes, last_touched_iso,
                synced_at, tealc_dirty)
               VALUES (?,?,?,?,'pending',?,?,?,1)""",
            (new_id, goal_id, milestone, target_iso or None, notes or None, now_iso, now_iso),
        )
        conn.commit()
        conn.close()
        return f"Milestone {new_id} added to goal {goal_id} ({existing_goal[0]}). Will sync to Sheet within 5 min."
    except Exception as e:
        return f"Error adding milestone: {e}"



@tool
def decompose_goal(goal_id: str, confirmed: bool = False, milestones_json: str = "") -> str:
    """Decompose a high-level goal into concrete milestones with target dates.

    First call (proposal): pass goal_id only. Returns a Sonnet-generated proposal as JSON
    in the response and asks Heath to review.

    Confirmation call: pass confirmed=True and the milestones_json (which Tealc copies
    from the proposal after Heath edits/approves in chat). The milestones are written
    to the Sheet via add_milestone_to_goal."""
    try:
        import anthropic as _anthropic
        import os as _os
        from dotenv import load_dotenv as _load_dotenv

        _HERE = _os.path.dirname(_os.path.abspath(__file__))
        _ENV_PATH = _os.path.normpath(_os.path.join(_HERE, "..", ".env"))
        _load_dotenv(_ENV_PATH)

        # -- Fetch the goal from the DB ----------------------------------------
        conn = _get_goals_db()
        row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not row:
            conn.close()
            return f"Goal {goal_id} not found. Use list_goals() to see available goals."
        col_names = [d[0] for d in conn.execute("SELECT * FROM goals LIMIT 0").description]
        g = dict(zip(col_names, row))
        goal_name = g.get("name", goal_id)

        # -- CONFIRMATION PATH -------------------------------------------------
        if confirmed:
            if not milestones_json.strip():
                conn.close()
                return "Error: milestones_json is required when confirmed=True."
            try:
                parsed = json.loads(milestones_json)
                milestone_list = parsed.get("milestones", parsed) if isinstance(parsed, dict) else parsed
            except Exception as parse_err:
                conn.close()
                return f"Error parsing milestones_json: {parse_err}"

            conn.close()
            written = 0
            errors = []
            for item in milestone_list:
                milestone_text = item.get("milestone", "").strip()
                target_iso = item.get("target_iso", "")
                notes_text = item.get("notes", "")
                if not milestone_text:
                    continue
                result = add_milestone_to_goal.invoke({
                    "goal_id": goal_id,
                    "milestone": milestone_text,
                    "target_iso": target_iso,
                    "notes": notes_text,
                })
                if result.startswith("Error"):
                    errors.append(result)
                else:
                    written += 1
            if errors:
                return f"WROTE {written} milestones to '{goal_name}'. Errors: {'; '.join(errors)}"
            return f"WROTE {written} milestones to '{goal_name}'. They will sync to the Goals Sheet within 5 minutes."

        # -- PROPOSAL PATH -----------------------------------------------------
        existing_milestones = conn.execute(
            "SELECT milestone, target_iso, status FROM milestones_v2 "
            "WHERE goal_id=? ORDER BY target_iso ASC NULLS LAST",
            (goal_id,),
        ).fetchall()
        conn.close()

        existing_text = ""
        if existing_milestones:
            ex_lines = ["Existing milestones (do not duplicate):"]
            for m, t, s in existing_milestones:
                ex_lines.append(f"  - [{s}] {m}" + (f" (due {t[:10]})" if t else ""))
            existing_text = "\n".join(ex_lines)

        today_iso = datetime.now(timezone.utc).date().isoformat()

        user_content = (
            f"Goal: {goal_name}\n"
            f"Goal ID: {goal_id}\n"
            f"Time horizon: {g.get('time_horizon', 'unknown')}\n"
            f"Success metric: {g.get('success_metric', '')}\n"
            f"Why: {g.get('why', '')}\n"
            f"Today's date: {today_iso}\n"
        )
        if existing_text:
            user_content += f"\n{existing_text}\n"

        system_prompt = (
            "Decompose Heath Blackmon's goal into 6-10 concrete milestones. "
            "Each milestone must be: a single deliverable Heath can verify done/not-done; "
            "have a target date that respects the goal's time_horizon; "
            "and be ordered by dependency (earliest first). "
            "Output ONLY valid JSON with no prose, no preamble, no trailing text:\n"
            '{"milestones": [{"milestone": str, "target_iso": "YYYY-MM-DD", "notes": str}, ...]}'
        )

        client = _anthropic.Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            fenced_lines = raw.splitlines()
            inner = fenced_lines[1:-1] if fenced_lines[-1].strip() == "```" else fenced_lines[1:]
            raw = "\n".join(inner).strip()

        # Validate JSON
        try:
            parsed = json.loads(raw)
            milestone_list = parsed.get("milestones", parsed) if isinstance(parsed, dict) else parsed
        except Exception as parse_err:
            return f"Sonnet returned invalid JSON: {parse_err}\n\nRaw output:\n{raw}"

        # Format for display
        display_lines = [f"PROPOSAL for '{goal_name}' ({goal_id}):\n"]
        for i, item in enumerate(milestone_list, 1):
            display_lines.append(
                f"{i}. {item.get('milestone', '')}\n"
                f"   Target: {item.get('target_iso', 'TBD')}\n"
                f"   Notes: {item.get('notes', '')}"
            )

        display_lines.append(
            f"\nTo confirm and write these to the Sheet, call:\n"
            f"decompose_goal(goal_id='{goal_id}', confirmed=True, milestones_json='{raw}')"
        )

        return "\n".join(display_lines)

    except Exception as e:
        return f"Error in decompose_goal: {e}"


@tool
def update_milestone(milestone_id: str, status: str = "", notes: str = "") -> str:
    """Update a milestone status (pending|in_progress|done|blocked) and/or add notes.
    Sets last_touched_iso=now. Will sync to Sheet within 5 min."""
    try:
        valid_statuses = {"pending", "in_progress", "done", "blocked"}
        if status and status not in valid_statuses:
            return f"Error: status must be one of {sorted(valid_statuses)}"
        conn = _get_goals_db()
        row = conn.execute("SELECT * FROM milestones_v2 WHERE id=?", (milestone_id,)).fetchone()
        if not row:
            conn.close()
            return f"Milestone {milestone_id} not found."
        col_names = [d[0] for d in conn.execute("SELECT * FROM milestones_v2 LIMIT 0").description]
        m = dict(zip(col_names, row))
        if status:
            m["status"] = status
        if notes:
            m["notes"] = notes
        now_iso = datetime.now(timezone.utc).isoformat()
        m["last_touched_iso"] = now_iso
        conn.execute(
            """UPDATE milestones_v2 SET status=?, notes=?, last_touched_iso=?,
               synced_at=?, tealc_dirty=1 WHERE id=?""",
            (m["status"], m["notes"], now_iso, now_iso, milestone_id),
        )
        conn.commit()
        conn.close()
        return f"Milestone {milestone_id} updated (status={m['status']}). Will sync to Sheet within 5 min."
    except Exception as e:
        return f"Error updating milestone: {e}"


@tool
def list_milestones_for_goal(goal_id: str) -> str:
    """List all milestones for a goal, sorted by target_iso ascending (soonest first)."""
    try:
        conn = _get_goals_db()
        goal = conn.execute("SELECT name FROM goals WHERE id=?", (goal_id,)).fetchone()
        if not goal:
            conn.close()
            return f"Goal {goal_id} not found."
        rows = conn.execute(
            "SELECT id, milestone, target_iso, status, notes FROM milestones_v2 "
            "WHERE goal_id=? ORDER BY target_iso ASC NULLS LAST",
            (goal_id,),
        ).fetchall()
        conn.close()
        if not rows:
            return f"No milestones yet for goal {goal_id} ({goal[0]})."
        lines = [f"## Milestones for {goal[0]} ({goal_id})\n"]
        for mid, ms, target, mstatus, mnotes in rows:
            target_str = f" — due {target[:10]}" if target else ""
            notes_str = f"\n  Notes: {mnotes}" if mnotes else ""
            lines.append(f"**[{mid}]** [{mstatus}] {ms}{target_str}{notes_str}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing milestones: {e}"


@tool
def write_today_plan(items: str) -> str:
    """Replace today's plan with a new list of priority items.
    items is a JSON list of objects: [{\"rank\": 1, \"description\": \"...\", \"linked_goal_id\": \"g_001\", \"status\": \"pending\"}]
    linked_goal_id and status are optional. Will sync to Sheet within 5 min."""
    try:
        today_iso = datetime.now(timezone.utc).date().isoformat()
        item_list = json.loads(items)
        if not isinstance(item_list, list):
            return "Error: items must be a JSON array"

        conn = _get_goals_db()
        now_iso = datetime.now(timezone.utc).isoformat()

        # Clear existing today plan rows (mark as tealc_dirty so they get overwritten)
        conn.execute("DELETE FROM today_plan WHERE date_iso=?", (today_iso,))

        for item in item_list:
            rank = item.get("rank") or item.get("priority_rank")
            desc = item.get("description", "").strip()
            if not desc:
                continue
            conn.execute(
                """INSERT INTO today_plan
                   (date_iso, priority_rank, description, linked_goal_id, status, notes,
                    synced_at, tealc_dirty)
                   VALUES (?,?,?,?,?,?,?,1)""",
                (
                    today_iso, rank, desc,
                    item.get("linked_goal_id") or None,
                    item.get("status") or "pending",
                    item.get("notes") or None,
                    now_iso,
                ),
            )
        conn.commit()
        conn.close()
        return f"Today's plan updated with {len(item_list)} items for {today_iso}. Will sync to Sheet within 5 min."
    except json.JSONDecodeError as e:
        return f"Error: invalid JSON in items: {e}"
    except Exception as e:
        return f"Error writing today's plan: {e}"


@tool
def get_today_plan(date_iso: str = "") -> str:
    """Read today's plan (or a specific date if date_iso provided as YYYY-MM-DD)."""
    try:
        target = date_iso.strip() if date_iso.strip() else datetime.now(timezone.utc).date().isoformat()
        conn = _get_goals_db()
        rows = conn.execute(
            "SELECT priority_rank, description, linked_goal_id, status, notes "
            "FROM today_plan WHERE date_iso=? ORDER BY priority_rank ASC NULLS LAST",
            (target,),
        ).fetchall()
        conn.close()
        if not rows:
            return f"No plan for {target}. Use write_today_plan() to create one."
        lines = [f"## Today's Plan — {target}\n"]
        for rank, desc, linked_id, status, notes in rows:
            rank_str = f"#{rank}" if rank else "#?"
            goal_str = f" [→{linked_id}]" if linked_id else ""
            status_str = f" [{status}]" if status else ""
            notes_str = f"\n  {notes}" if notes else ""
            lines.append(f"{rank_str}{status_str} {desc}{goal_str}{notes_str}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading today's plan: {e}"


@tool
def log_decision(decision: str, reasoning: str = "", linked_goal_id: str = "",
                 decided_by: str = "Tealc") -> str:
    """Append a row to the Decisions audit log. Use when Heath or Tealc make a meaningful choice.
    linked_goal_id should reference a goal (e.g. 'g_001'). Will sync to Sheet within 5 min."""
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = _get_goals_db()
        conn.execute(
            """INSERT INTO decisions_log
               (decided_iso, decision, reasoning, linked_goal_id, decided_by,
                synced_at, tealc_dirty)
               VALUES (?,?,?,?,?,?,1)""",
            (now_iso, decision, reasoning or None, linked_goal_id or None, decided_by, now_iso),
        )
        conn.commit()
        conn.close()
        return f"Decision logged: '{decision[:80]}' (by {decided_by}). Will sync to Sheet within 5 min."
    except Exception as e:
        return f"Error logging decision: {e}"


@tool
def get_nas_impact_trend(weeks_back: int = 12) -> str:
    """Show NAS-impact percentages over the past N weeks. Returns markdown table:
    week | nas_trajectory% | service_drag% | maintenance% | unattributed% | top goal advanced."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT week_start_iso, nas_trajectory_pct, service_drag_pct, "
            "maintenance_pct, unattributed_pct, goal_breakdown_json, total_activity_count "
            "FROM nas_impact_weekly "
            "ORDER BY week_start_iso DESC LIMIT ?",
            (weeks_back,),
        ).fetchall()
        conn.close()
        if not rows:
            return (
                "No NAS-impact data yet. "
                "The job runs every Sunday at 8pm Central. "
                "Run `python -m agent.jobs.nas_impact_score` to generate one now."
            )
        lines = [
            f"## NAS-Impact Trend (past {len(rows)} week{'s' if len(rows) != 1 else ''})\n",
            "| week | nas_trajectory% | service_drag% | maintenance% | unattributed% | top goal | items |",
            "|------|-----------------|---------------|-------------|---------------|----------|-------|",
        ]
        for week_start, nas_pct, drag_pct, maint_pct, unattr_pct, goal_json, total in rows:
            top_goal = "—"
            if goal_json:
                try:
                    breakdown = json.loads(goal_json)
                    if breakdown:
                        top_gid = max(breakdown, key=lambda k: breakdown[k])
                        # Try to resolve goal name from DB
                        try:
                            conn2 = sqlite3.connect(DB_PATH)
                            name_row = conn2.execute(
                                "SELECT name FROM goals WHERE id=?", (top_gid,)
                            ).fetchone()
                            conn2.close()
                            top_goal = (name_row[0] if name_row else top_gid)[:30]
                        except Exception:
                            top_goal = top_gid[:30]
                except Exception:
                    pass
            lines.append(
                f"| {week_start} "
                f"| {nas_pct or 0:.0f}% "
                f"| {drag_pct or 0:.0f}% "
                f"| {maint_pct or 0:.0f}% "
                f"| {unattr_pct or 0:.0f}% "
                f"| {top_goal} "
                f"| {total or 0} |"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading NAS-impact trend: {e}"


@tool
def list_goal_conflicts(unacknowledged_only: bool = True, days_back: int = 30) -> str:
    """List recent goal-portfolio conflicts Tealc detected — stale high-priority goals,
    low-priority work overdriving, imminent milestones with no activity, service-drag spikes.
    Filter by unacknowledged or include all."""
    try:
        from datetime import timedelta  # noqa: PLC0415
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        if unacknowledged_only:
            rows = conn.execute(
                "SELECT id, detected_iso, conflict_type, severity, involved_goal_ids, "
                "description, recommendation, acknowledged_at "
                "FROM goal_conflicts "
                "WHERE acknowledged_at IS NULL AND detected_iso >= ? "
                "ORDER BY severity DESC, detected_iso DESC",
                (cutoff,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, detected_iso, conflict_type, severity, involved_goal_ids, "
                "description, recommendation, acknowledged_at "
                "FROM goal_conflicts "
                "WHERE detected_iso >= ? "
                "ORDER BY severity DESC, detected_iso DESC",
                (cutoff,),
            ).fetchall()
        conn.close()
        if not rows:
            label = "unacknowledged " if unacknowledged_only else ""
            return f"No {label}goal conflicts in the past {days_back} days."
        lines = [f"## Goal-Portfolio Conflicts (past {days_back}d, {'unacked only' if unacknowledged_only else 'all'})\n"]
        for cid, detected, ctype, severity, ids, desc, rec, acked in rows:
            acked_str = f" [acked {acked[:10]}]" if acked else " [OPEN]"
            lines.append(f"**[#{cid}] [{severity.upper()}] {ctype}**{acked_str}")
            lines.append(f"  Detected: {detected[:10]}")
            if ids:
                lines.append(f"  Goals/refs: {ids}")
            lines.append(f"  {desc}")
            if rec:
                lines.append(f"  _Rec: {rec}_")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing goal conflicts: {e}"


@tool
def acknowledge_goal_conflict(conflict_id: int, response: str = "") -> str:
    """Mark a goal-conflict as acknowledged. Optionally include Heath's response/decision
    (e.g., 'Yes, intentionally dropping NAS focus this week to support student crisis')."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT id, conflict_type, severity FROM goal_conflicts WHERE id=?",
            (conflict_id,),
        ).fetchone()
        if not row:
            conn.close()
            return f"Conflict #{conflict_id} not found."
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE goal_conflicts SET acknowledged_at=?, human_response=? WHERE id=?",
            (now_iso, response or None, conflict_id),
        )
        conn.commit()
        conn.close()
        ctype = row[1]
        severity = row[2]
        msg = f"Conflict #{conflict_id} ({ctype}, {severity}) acknowledged."
        if response:
            msg += f" Response recorded: '{response[:120]}'"
        return msg
    except Exception as e:
        return f"Error acknowledging conflict: {e}"


# ---------------------------------------------------------------------------
# Research Project tools
# ---------------------------------------------------------------------------

def _get_projects_db():
    """Return a WAL connection with research_projects table guaranteed to exist."""
    from agent.jobs.sync_goals_sheet import _migrate_goals_tables  # noqa: PLC0415
    _migrate_goals_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _next_project_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT id FROM research_projects ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return "p_001"
    last = row[0]  # e.g. "p_042"
    try:
        num = int(last.split("_")[1]) + 1
    except (IndexError, ValueError):
        num = 1
    return f"p_{num:03d}"


@tool
def list_research_projects(status: str = "active", project_type: str = "paper") -> str:
    """List research projects from the SQLite mirror. Returns markdown table with
    id, name, status, current_hypothesis (truncated to 80 chars), next_action, keywords.

    Args:
      status: active | paused | done | dropped | all
      project_type: paper | database | teaching | all (default 'paper' — in
        Heath's lab a 'project' means a student-led paper project with a
        subfolder in the shared Drive 'Blackmon Lab/Projects' tree).  Grants
        moved to their own `grants` table on 2026-04-24; call `list_grants`
        for those.
    """
    try:
        conn = _get_projects_db()
        where = []
        params: list = []
        if status and status != "all":
            where.append("status=?")
            params.append(status)
        if project_type and project_type != "all":
            # Permit NULL for legacy rows pending audit by sync_lab_projects
            if project_type == "paper":
                where.append("(project_type='paper' OR project_type IS NULL)")
            else:
                where.append("project_type=?")
                params.append(project_type)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            "SELECT id, name, status, current_hypothesis, next_action, keywords "
            f"FROM research_projects{clause} ORDER BY id",
            params,
        ).fetchall()
        conn.close()
        if not rows:
            label = f"status={status}, project_type={project_type}"
            return f"No research projects found ({label}). Heath's paper projects live as subfolders under 'Blackmon Lab/Projects' in the shared Drive; sync_lab_projects mirrors them nightly."
        lines = [
            f"## Research Projects (status={status}, project_type={project_type})\n",
            "| id | name | status | hypothesis (truncated) | next_action | keywords |",
            "|----|------|--------|------------------------|-------------|----------|",
        ]
        for pid, name, pstatus, hyp, nxt, kw in rows:
            hyp_short = (hyp or "")[:80].replace("|", "/")
            nxt_short = (nxt or "")[:60].replace("|", "/")
            kw_short = (kw or "")[:40]
            lines.append(
                f"| {pid} | {name} | {pstatus or ''} "
                f"| {hyp_short} | {nxt_short} | {kw_short} |"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing research projects: {e}"


@tool
def get_research_project(project_id: str) -> str:
    """Full detail of one research project including all fields and any literature
    notes recently linked to it (if the literature synthesis table exists yet)."""
    try:
        conn = _get_projects_db()
        row = conn.execute(
            "SELECT * FROM research_projects WHERE id=?", (project_id,)
        ).fetchone()
        if not row:
            conn.close()
            return f"Project {project_id} not found. Use list_research_projects() to see available projects."
        col_names = [
            d[0] for d in conn.execute(
                "SELECT * FROM research_projects LIMIT 0"
            ).description
        ]
        p = dict(zip(col_names, row))
        conn.close()

        lines = [
            f"## Project: {p['name']} ({project_id})",
            f"**Status:** {p.get('status', '')} | **Last touched by:** {p.get('last_touched_by', '')} "
            f"on {(p.get('last_touched_iso') or '')[:10]}",
            "",
            f"**Description:** {p.get('description', '')}",
            f"**Linked goals:** {p.get('linked_goal_ids', '') or 'none'}",
            f"**Keywords:** {p.get('keywords', '') or 'none'}",
            "",
            f"**Current hypothesis:**",
            f"{p.get('current_hypothesis', '') or '(not set)'}",
            "",
            f"**Next action:**",
            f"{p.get('next_action', '') or '(not set)'}",
            "",
            f"**Data dir:** {p.get('data_dir', '') or '(not set)'}",
            f"**Output dir:** {p.get('output_dir', '') or '(not set)'}",
            f"**Primary artifact ID:** {p.get('linked_artifact_id', '') or '(not set)'}",
        ]
        if p.get("notes"):
            lines.append(f"\n**Notes:** {p['notes']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error getting research project {project_id}: {e}"


@tool
def add_research_project(name: str, description: str = "", linked_goal_ids: str = "",
                         data_dir: str = "", output_dir: str = "",
                         current_hypothesis: str = "", next_action: str = "",
                         keywords: str = "", linked_artifact_id: str = "") -> str:
    """Add a new research project. Status starts as 'active'. Tealc-created projects
    appear in the Sheet at the next sync cycle (within 5 min)."""
    try:
        conn = _get_projects_db()
        new_id = _next_project_id(conn)
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO research_projects
               (id, name, description, status, linked_goal_ids, data_dir,
                output_dir, current_hypothesis, next_action, keywords,
                linked_artifact_id, last_touched_by, last_touched_iso, synced_at)
               VALUES (?,?,?,'active',?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id, name, description or None, linked_goal_ids or None,
                data_dir or None, output_dir or None,
                current_hypothesis or None, next_action or None,
                keywords or None, linked_artifact_id or None,
                "Tealc", now_iso, now_iso,
            ),
        )
        conn.commit()
        conn.close()
        return (
            f"Research project added: {new_id} — '{name}' (status=active). "
            f"Will appear in the Goals Sheet → Projects tab within 5 minutes."
        )
    except Exception as e:
        return f"Error adding research project: {e}"


@tool
def update_research_project(project_id: str, name: str = "", description: str = "",
                             status: str = "", linked_goal_ids: str = "",
                             data_dir: str = "", output_dir: str = "",
                             current_hypothesis: str = "", next_action: str = "",
                             keywords: str = "", linked_artifact_id: str = "",
                             notes: str = "") -> str:
    """Update fields on an existing project. Stamps last_touched_by='Tealc' and
    last_touched_iso=now. Only non-empty arguments are changed."""
    try:
        conn = _get_projects_db()
        row = conn.execute(
            "SELECT * FROM research_projects WHERE id=?", (project_id,)
        ).fetchone()
        if not row:
            conn.close()
            return f"Project {project_id} not found."
        col_names = [
            d[0] for d in conn.execute(
                "SELECT * FROM research_projects LIMIT 0"
            ).description
        ]
        p = dict(zip(col_names, row))

        if name:
            p["name"] = name
        if description:
            p["description"] = description
        if status:
            valid_statuses = {"active", "paused", "done", "dropped"}
            if status not in valid_statuses:
                conn.close()
                return f"Error: status must be one of {sorted(valid_statuses)}"
            p["status"] = status
        if linked_goal_ids:
            p["linked_goal_ids"] = linked_goal_ids
        if data_dir:
            p["data_dir"] = data_dir
        if output_dir:
            p["output_dir"] = output_dir
        if current_hypothesis:
            p["current_hypothesis"] = current_hypothesis
        if next_action:
            p["next_action"] = next_action
        if keywords:
            p["keywords"] = keywords
        if linked_artifact_id:
            p["linked_artifact_id"] = linked_artifact_id
        if notes:
            p["notes"] = notes

        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE research_projects SET
               name=?, description=?, status=?, linked_goal_ids=?,
               data_dir=?, output_dir=?, current_hypothesis=?, next_action=?,
               keywords=?, linked_artifact_id=?, notes=?,
               last_touched_by='Tealc', last_touched_iso=?, synced_at=?
               WHERE id=?""",
            (
                p["name"], p.get("description"), p.get("status"),
                p.get("linked_goal_ids"), p.get("data_dir"), p.get("output_dir"),
                p.get("current_hypothesis"), p.get("next_action"),
                p.get("keywords"), p.get("linked_artifact_id"), p.get("notes"),
                now_iso, now_iso, project_id,
            ),
        )
        conn.commit()
        conn.close()
        return f"Project {project_id} updated. Changes will sync to Sheet within 5 minutes."
    except Exception as e:
        return f"Error updating project {project_id}: {e}"


@tool
def set_project_next_action(project_id: str, next_action: str) -> str:
    """Set the next queued action for a project. This is what the nightly science
    jobs (literature synthesis, drafter, etc.) will execute on the next deep-idle window."""
    try:
        if not next_action.strip():
            return "Error: next_action cannot be empty."
        conn = _get_projects_db()
        row = conn.execute(
            "SELECT name FROM research_projects WHERE id=?", (project_id,)
        ).fetchone()
        if not row:
            conn.close()
            return f"Project {project_id} not found."
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE research_projects SET next_action=?,
               last_touched_by='Tealc', last_touched_iso=?, synced_at=?
               WHERE id=?""",
            (next_action.strip(), now_iso, now_iso, project_id),
        )
        conn.commit()
        conn.close()
        return (
            f"Next action set for {project_id} ({row[0]}): '{next_action[:120]}'. "
            f"Will sync to Sheet within 5 minutes."
        )
    except Exception as e:
        return f"Error setting next action for {project_id}: {e}"


@tool
def complete_project_next_action(project_id: str, completion_notes: str = "") -> str:
    """Mark the current next_action complete. Optionally add notes describing what
    was done. Clears next_action so the project waits for a new action."""
    try:
        conn = _get_projects_db()
        row = conn.execute(
            "SELECT name, next_action, notes FROM research_projects WHERE id=?",
            (project_id,),
        ).fetchone()
        if not row:
            conn.close()
            return f"Project {project_id} not found."
        name, prev_action, existing_notes = row
        now_iso = datetime.now(timezone.utc).isoformat()
        # Append completion note to notes field
        note_entry = f"[{now_iso[:10]}] Completed: '{(prev_action or '(none)')[:100]}'"
        if completion_notes:
            note_entry += f" — {completion_notes}"
        updated_notes = (existing_notes or "").strip()
        updated_notes = f"{updated_notes}\n{note_entry}".strip() if updated_notes else note_entry
        conn.execute(
            """UPDATE research_projects SET next_action=NULL, notes=?,
               last_touched_by='Tealc', last_touched_iso=?, synced_at=?
               WHERE id=?""",
            (updated_notes, now_iso, now_iso, project_id),
        )
        conn.commit()
        conn.close()
        return (
            f"Completed next action for {project_id} ({name}). "
            f"next_action cleared. Notes updated. Will sync to Sheet within 5 minutes."
        )
    except Exception as e:
        return f"Error completing next action for {project_id}: {e}"


@tool
def get_recent_literature_for_project(project_id: str, days_back: int = 14, limit: int = 20) -> str:
    """Read literature notes Tealc generated for a research project. Returns markdown
    with title, year, journal, citation count, the extracted findings, and relevance
    assessment per paper."""
    try:
        from datetime import timedelta  # noqa: PLC0415
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        rows = conn.execute(
            """SELECT title, publication_year, journal, citations_count,
                      extracted_findings_md, relevance_to_project, doi, created_at
               FROM literature_notes
               WHERE project_id=? AND created_at >= ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (project_id, since, limit),
        ).fetchall()
        conn.close()
        if not rows:
            return f"No literature notes found for project '{project_id}' in the past {days_back} days."
        lines = []
        for r in rows:
            title, year, journal, cites, findings, relevance, doi, created_at = r
            doi_str = f"DOI: {doi}" if doi else ""
            lines.append(
                f"### {title}\n"
                f"**{year or '?'} | {journal or 'unknown journal'} | "
                f"{cites or 0} citations** {doi_str}\n"
                f"*Added: {(created_at or '')[:10]}*\n\n"
                f"**Findings:**\n{findings or '(none)'}\n\n"
                f"**Relevance:** {relevance or '(none)'}"
            )
        return "\n\n---\n\n".join(lines)
    except Exception as e:
        return f"Error reading literature notes: {e}"


@tool
def list_recent_literature_notes(days_back: int = 7, limit: int = 30) -> str:
    """All literature notes Tealc generated recently across all projects, newest first.
    Useful for 'what has Tealc been reading lately?'"""
    try:
        from datetime import timedelta  # noqa: PLC0415
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        rows = conn.execute(
            """SELECT project_id, title, publication_year, journal,
                      citations_count, relevance_to_project, created_at
               FROM literature_notes
               WHERE created_at >= ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (since, limit),
        ).fetchall()
        conn.close()
        if not rows:
            return f"No literature notes in the past {days_back} days."
        lines = []
        for r in rows:
            proj, title, year, journal, cites, relevance, created_at = r
            proj_label = f"[{proj}]" if proj else "[no project]"
            lines.append(
                f"{proj_label} **{title}** "
                f"({year or '?'}, {journal or '?'}, {cites or 0} cites) — "
                f"{(created_at or '')[:10]}\n"
                f"  {(relevance or '')[:200]}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing literature notes: {e}"


@tool
def list_overnight_drafts(unreviewed_only: bool = True, limit: int = 10) -> str:
    """List grant/manuscript section drafts Tealc produced overnight, with links to
    review. Filter by unreviewed or include all."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        if unreviewed_only:
            rows = conn.execute(
                "SELECT id, project_id, source_artifact_title, drafted_section, "
                "draft_doc_url, reasoning, created_at, outcome "
                "FROM overnight_drafts "
                "WHERE reviewed_at IS NULL "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, project_id, source_artifact_title, drafted_section, "
                "draft_doc_url, reasoning, created_at, outcome "
                "FROM overnight_drafts "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        conn.close()
        if not rows:
            label = "unreviewed " if unreviewed_only else ""
            return f"No {label}overnight drafts found."
        lines = [f"## Overnight Drafts ({'unreviewed only' if unreviewed_only else f'last {limit}'})\n"]
        for did, pid, src_title, section, url, reasoning, created, outcome in rows:
            status = f"outcome={outcome}" if outcome else "PENDING REVIEW"
            lines.append(
                f"**[#{did}]** {src_title or pid or 'unknown'} — **{section}**\n"
                f"  Created: {created[:10]}  |  {status}\n"
                f"  Why: {(reasoning or '')[:120]}\n"
                f"  Draft: {url}\n"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing overnight drafts: {e}"


@tool
def list_hypothesis_proposals(project_id: str = "", status: str = "proposed", limit: int = 20) -> str:
    """List Tealc-proposed hypotheses. Filter by project_id or status (proposed|adopted|rejected).
    Returns markdown with hypothesis, rationale, proposed test, novelty + feasibility scores."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hypothesis_proposals'"
        ).fetchone()
        if not tbl:
            conn.close()
            return "hypothesis_proposals table not found. Run _migrate() in agent/scheduler.py."
        params: list = []
        where_clauses = []
        if project_id:
            where_clauses.append("project_id=?")
            params.append(project_id)
        if status:
            where_clauses.append("status=?")
            params.append(status)
        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"SELECT id, project_id, proposed_iso, hypothesis_md, rationale_md, "
            f"proposed_test_md, cited_paper_dois, novelty_score, feasibility_score, status, human_review "
            f"FROM hypothesis_proposals {where_sql} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        conn.close()
        if not rows:
            label = f"project={project_id}, " if project_id else ""
            return f"No hypothesis proposals found ({label}status={status})."
        lines = [f"## Hypothesis Proposals (status={status or 'all'})\n"]
        for hid, pid, proposed_iso, hyp_md, rat_md, test_md, dois, nov, feas, st, review in rows:
            nov_str = f"{nov:.2f}" if nov is not None else "?"
            feas_str = f"{feas:.2f}" if feas is not None else "?"
            lines.append(
                f"### [#{hid}] [{st.upper()}] Project: {pid}\n"
                f"*Proposed: {(proposed_iso or '')[:10]}*  "
                f"novelty={nov_str}  feasibility={feas_str}\n\n"
                f"**Hypothesis:** {hyp_md}\n\n"
                f"**Rationale:** {rat_md or '(none)'}\n\n"
                f"**Proposed test:** {test_md or '(none)'}\n\n"
                f"**Cited DOIs:** {dois or '(none)'}"
            )
            if review:
                lines.append(f"\n*Review note: {review}*")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing hypothesis proposals: {e}"


@tool
def adopt_hypothesis(proposal_id: int, notes: str = "", override_gate: bool = False) -> str:
    """Mark a proposed hypothesis as adopted.

    Adoption now requires the proposal to have passed the hypothesis gate (critic
    score >= 3 AND no fatal blocking issues). If the linked output_ledger row shows
    a blocked gate, adoption is refused unless override_gate=True. The gate state
    is sourced from output_ledger rows where kind='hypothesis' and the row's
    provenance.hypothesis_id matches proposal_id.

    Heath should follow up by updating the relevant project's current_hypothesis
    (manually or via update_research_project) if the new hypothesis replaces the old one.

    Args:
      proposal_id: int id from hypothesis_proposals
      notes: optional human review notes
      override_gate: set True to adopt despite a blocked gate (records the override
        reason in notes and tags the human_review field)
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT id, project_id, hypothesis_md FROM hypothesis_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if not row:
            conn.close()
            return f"Proposal #{proposal_id} not found."

        # Find the linked output_ledger row via provenance.hypothesis_id
        ledger_rows = conn.execute(
            "SELECT id, critic_score, provenance_json "
            "FROM output_ledger "
            "WHERE kind='hypothesis' AND (project_id=? OR project_id IS NULL)",
            (row[1],),
        ).fetchall()

        gate_passed: bool | None = None
        gate_score = 0
        gate_reasons: list = []
        gate_ledger_id = None
        for lid, cscore, prov_json in ledger_rows:
            try:
                prov = json.loads(prov_json or "{}")
            except Exception:
                continue
            if prov.get("hypothesis_id") == proposal_id:
                gate_ledger_id = lid
                hgate = prov.get("hypothesis_gate") or {}
                if hgate:
                    gate_passed = bool(hgate.get("passed", False))
                    gate_score = int(hgate.get("score", cscore or 0) or 0)
                    gate_reasons = list(hgate.get("block_reasons") or [])
                else:
                    gate_passed = (cscore or 0) >= 3
                    gate_score = int(cscore or 0)
                    gate_reasons = (
                        [f"pre-gate ledger row; critic_score={cscore} < 3"]
                        if (cscore or 0) < 3 else []
                    )
                break

        if gate_passed is False and not override_gate:
            conn.close()
            reasons_str = "; ".join(gate_reasons[:3]) if gate_reasons else "no specific reasons recorded"
            return (
                f"Proposal #{proposal_id} did NOT pass the hypothesis gate "
                f"(score={gate_score}, reasons: {reasons_str}; ledger row #{gate_ledger_id}). "
                f"Adoption refused. Options:\n"
                f"  - Refine and re-run via run_formal_hypothesis_pass\n"
                f"  - Reject via reject_hypothesis if it shouldn't proceed\n"
                f"  - Override with adopt_hypothesis(proposal_id={proposal_id}, "
                f"override_gate=True, notes='<your reason>')"
            )

        if gate_passed is None:
            # No linked ledger row found — could be a pre-pipeline proposal; allow with warning
            warning_prefix = (
                f"[NO GATE FOUND — pre-pipeline proposal] "
            )
        elif gate_passed is False and override_gate:
            warning_prefix = (
                f"[GATE OVERRIDE — score={gate_score}, "
                f"reasons={'; '.join(gate_reasons[:2])}] "
            )
        else:
            warning_prefix = ""

        merged_notes = (warning_prefix + (notes or "")).strip() or None
        conn.execute(
            "UPDATE hypothesis_proposals SET status='adopted', human_review=? WHERE id=?",
            (merged_notes, proposal_id),
        )
        conn.commit()
        conn.close()

        msg = f"Proposal #{proposal_id} (project={row[1]}) marked adopted."
        if gate_passed is False and override_gate:
            msg += " (gate-blocked; OVERRIDE)"
        elif gate_passed is None:
            msg += " (no gate record; pre-pipeline)"
        if merged_notes:
            msg += f" Notes: {merged_notes[:200]}"
        msg += " Update the project's current_hypothesis via update_research_project when ready."
        return msg
    except Exception as e:
        return f"Error adopting hypothesis: {e}"


@tool
def run_hypothesis_tournament(proposal_ids: str) -> str:
    """Run a Sonnet pairwise tournament on a set of hypothesis proposals.

    Pulls each proposal's hypothesis_md from hypothesis_proposals, runs round-robin
    pairwise judging on Sonnet (criteria: mechanism coherence, specificity/falsifiability,
    novelty, feasibility), and returns an Elo-ranked table. Cap of 6 proposals — pass
    fewer if you want to keep cost down. Cost ≈ $0.012 per pair (Sonnet).

    Use this after the weekly_hypothesis_generator produces ≥3 proposals to surface the
    strongest, or anytime Heath wants to compare a set of candidate hypotheses head-to-head.
    The tournament is independent of the gate — it ranks among the hypotheses you give it,
    it does NOT re-run gating.

    Args:
      proposal_ids: comma-separated proposal IDs from hypothesis_proposals (e.g. "12,13,15")
    """
    try:
        from agent.hypothesis_pipeline import pairwise_tournament  # noqa: PLC0415
        ids = [int(x.strip()) for x in proposal_ids.split(",") if x.strip()]
        if len(ids) < 2:
            return "Need at least 2 proposal IDs to run a tournament."
        if len(ids) > 6:
            return f"Tournament capped at 6 proposals; got {len(ids)}. Trim and retry."

        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        items = []
        for pid in ids:
            row = conn.execute(
                "SELECT id, project_id, hypothesis_md FROM hypothesis_proposals WHERE id=?",
                (pid,),
            ).fetchone()
            if not row:
                conn.close()
                return f"Proposal #{pid} not found."
            items.append({"id": row[0], "label": f"#{row[0]} (proj={row[1]})", "content_md": row[2]})
        conn.close()

        ranked = pairwise_tournament(items)

        lines = ["## Tournament results (Sonnet pairwise judging)", "",
                 "| Rank | Proposal | Elo | W-L | Last rationale |",
                 "|---|---|---|---|---|"]
        for i, r in enumerate(ranked, 1):
            rationale = (r.get("last_judge_rationale") or "").replace("\n", " ")[:120]
            lines.append(
                f"| {i} | {r['label']} | {r['elo']:.0f} | {r['wins']}-{r['losses']} | {rationale} |"
            )
        n = len(ranked)
        pairs = n * (n - 1) // 2
        lines.append("")
        lines.append(f"_{pairs} pairs judged. Approximate cost: ${pairs * 0.012:.3f} (Sonnet)._")
        return "\n".join(lines)
    except Exception as e:
        return f"Error running tournament: {e}"


@tool
def reject_hypothesis(proposal_id: int, reason: str) -> str:
    """Reject a proposed hypothesis with reason — feeds future quality of proposals."""
    if not reason or not reason.strip():
        return "Error: reason is required to reject a hypothesis."
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT id, project_id, hypothesis_md FROM hypothesis_proposals WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if not row:
            conn.close()
            return f"Proposal #{proposal_id} not found."
        conn.execute(
            "UPDATE hypothesis_proposals SET status='rejected', human_review=? WHERE id=?",
            (reason.strip(), proposal_id),
        )
        conn.commit()
        conn.close()
        return f"Proposal #{proposal_id} (project={row[1]}) rejected. Reason recorded."
    except Exception as e:
        return f"Error rejecting hypothesis: {e}"


@tool
def review_overnight_draft(draft_id: int, outcome: str, notes: str = "") -> str:
    """Mark an overnight draft as reviewed. outcome: accepted | rejected | rewritten.
    Notes optional. Heath uses this so Tealc learns which drafts were useful — feeds
    into the weekly self-review's evaluation of overnight work quality."""
    valid_outcomes = {"accepted", "rejected", "rewritten"}
    if outcome not in valid_outcomes:
        return f"Invalid outcome '{outcome}'. Must be one of: accepted, rejected, rewritten"
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT id, drafted_section, project_id FROM overnight_drafts WHERE id=?",
            (draft_id,),
        ).fetchone()
        if not row:
            conn.close()
            return f"Draft #{draft_id} not found."
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE overnight_drafts SET reviewed_at=?, outcome=? WHERE id=?",
            (now_iso, outcome, draft_id),
        )
        conn.commit()
        conn.close()
        msg = f"Draft #{draft_id} ({row[1]}) marked {outcome}."
        if notes:
            msg += f" Notes: {notes[:200]}"
        return msg
    except Exception as e:
        return f"Error reviewing draft: {e}"


# ---------------------------------------------------------------------------
# Database health tools
# ---------------------------------------------------------------------------

@tool
def list_database_flags(sheet_name: str = "", weeks_back: int = 2, category: str = "") -> str:
    """Review the most recent database health flags. Filter by sheet_name (e.g. 'coleoptera_karyotypes')
    or category (empty_critical_field | duplicate_primary | trailing_whitespace | placeholder_values |
    outlier_chromosome_counts). Returns markdown with row indices, snippets, and Sheet links."""
    try:
        cutoff = datetime.now(timezone.utc).isoformat()
        # Compute cutoff ISO (weeks_back weeks ago)
        from datetime import timedelta
        cutoff_dt = datetime.now(timezone.utc) - timedelta(weeks=weeks_back)
        cutoff = cutoff_dt.isoformat()

        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        query_sql = (
            "SELECT sheet_name, spreadsheet_id, run_iso, total_rows, "
            "flagged_count, flagged_summary_json, notes "
            "FROM database_health_runs "
            "WHERE run_iso >= ? "
        )
        params: list = [cutoff]
        if sheet_name:
            query_sql += "AND sheet_name = ? "
            params.append(sheet_name)
        query_sql += "ORDER BY run_iso DESC LIMIT 50"

        rows = conn.execute(query_sql, params).fetchall()
        conn.close()

        if not rows:
            return "No database health records found for the specified filters."

        lines = ["# Database Health Flags\n"]
        for row in rows:
            s_name, s_id, run_iso_val, total, flagged, summary_json, notes_val = row
            url = f"https://docs.google.com/spreadsheets/d/{s_id}"
            lines.append(f"\n## [{s_name}]({url})")
            lines.append(f"Run: {run_iso_val[:10]} | Rows: {total} | Flags: {flagged}")
            if notes_val:
                lines.append(f"Notes: {notes_val}")
            if not summary_json or summary_json == "{}":
                lines.append("No issues found.")
                continue
            try:
                summary = json.loads(summary_json)
            except Exception:
                lines.append("(Could not parse flag summary)")
                continue
            for cat, flag_list in summary.items():
                if category and cat != category:
                    continue
                lines.append(f"\n### {cat} ({len(flag_list)} rows)")
                for f in flag_list[:5]:
                    lines.append(f"  - Row {f['row_idx']}: `{f['snippet']}`")
                if len(flag_list) > 5:
                    lines.append(f"  - …and {len(flag_list) - 5} more")

        return "\n".join(lines)
    except Exception as e:
        return f"Error listing database flags: {e}"


@tool
def trigger_database_health_check(sheet_name: str = "") -> str:
    """Manually run the database health check now (instead of waiting for Saturday).
    Optionally restrict to one sheet. Useful when Heath has just edited a database
    and wants immediate feedback."""
    try:
        from agent.jobs.weekly_database_health import job as _health_job  # noqa: PLC0415
        result = _health_job(restrict_to_sheet=sheet_name)
        return f"Database health check complete: {result}"
    except Exception as e:
        return f"Error running database health check: {e}"


# ---------------------------------------------------------------------------
# Overnight comparative analysis read-back tools
# ---------------------------------------------------------------------------

@tool
def list_analysis_runs(project_id: str = "", weeks_back: int = 4) -> str:
    """List recent overnight R analyses Tealc ran. Filter by project. Returns markdown
    with date, project, exit_code, working_dir, and a 1-line interpretation excerpt."""
    try:
        from datetime import timedelta  # noqa: PLC0415
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        since = (datetime.now(timezone.utc) - timedelta(weeks=weeks_back)).isoformat()
        if project_id:
            rows = conn.execute(
                "SELECT id, project_id, run_iso, exit_code, working_dir, "
                "outcome, interpretation_md "
                "FROM analysis_runs "
                "WHERE project_id=? AND run_iso >= ? "
                "ORDER BY run_iso DESC LIMIT 20",
                (project_id, since),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, project_id, run_iso, exit_code, working_dir, "
                "outcome, interpretation_md "
                "FROM analysis_runs "
                "WHERE run_iso >= ? "
                "ORDER BY run_iso DESC LIMIT 20",
                (since,),
            ).fetchall()
        conn.close()
        if not rows:
            label = f" for project '{project_id}'" if project_id else ""
            return f"No analysis runs{label} in the past {weeks_back} weeks."
        lines = [f"## Analysis Runs (past {weeks_back} weeks)\n"]
        for rid, pid, run_iso, exit_code, wdir, outcome, interp in rows:
            date_str = (run_iso or "")[:10]
            status_icon = "OK" if exit_code == 0 else f"ERR({exit_code})"
            excerpt = (interp or "")[:120].replace("\n", " ")
            lines.append(
                f"**[#{rid}]** {date_str}  project={pid}  {status_icon}  outcome={outcome}\n"
                f"  dir: `{wdir or 'n/a'}`\n"
                f"  {excerpt}...\n"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing analysis runs: {e}"


@tool
def get_analysis_run_detail(analysis_id: int) -> str:
    """Full detail of one analysis: R code, stdout, stderr, working_dir, file list,
    full interpretation."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT id, project_id, run_iso, next_action_text, r_code, working_dir, "
            "exit_code, stdout_truncated, stderr_truncated, plot_paths, created_files, "
            "interpretation_md, outcome, human_review "
            "FROM analysis_runs WHERE id=?",
            (analysis_id,),
        ).fetchone()
        conn.close()
        if not row:
            return f"Analysis run #{analysis_id} not found."
        (rid, pid, run_iso, next_action, r_code, wdir, exit_code, stdout, stderr,
         plots, files, interp, outcome, human_review) = row
        sections = [
            f"## Analysis Run #{rid}",
            f"**Project:** {pid}",
            f"**Run:** {run_iso}",
            f"**Outcome:** {outcome}  |  **Exit code:** {exit_code}",
            f"**Next action that triggered this:** {next_action or 'n/a'}",
            f"**Working dir:** `{wdir or 'n/a'}`",
            f"**Files created:** {files or '[]'}",
            f"**Plots:** {plots or '[]'}",
            "",
            "### R Code",
            f"```r\n{r_code or '(none)'}\n```",
            "",
            "### stdout",
            f"```\n{stdout or '(empty)'}\n```",
            "",
            "### stderr",
            f"```\n{stderr or '(empty)'}\n```",
            "",
            "### Interpretation",
            interp or "(none)",
        ]
        if human_review:
            sections += ["", f"**Heath's review:** {human_review}"]
        return "\n".join(sections)
    except Exception as e:
        return f"Error fetching analysis run detail: {e}"


# ---------------------------------------------------------------------------
# v2: Output ledger + critic + preference learning + observability tools
# ---------------------------------------------------------------------------

@tool
def list_output_ledger(kind: str = "all", days: int = 7, limit: int = 20) -> str:
    """List recent research artifacts logged to the output ledger with critic scores.
    kind: 'all' or one of grant_draft/hypothesis/analysis/literature_synthesis.
    Shows id, kind, project, critic_score, and a content snippet."""
    try:
        from agent.ledger import query_outputs  # noqa: PLC0415
        from datetime import timedelta  # noqa: PLC0415
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        kind_filter = None if kind == "all" else kind
        rows = query_outputs(kind=kind_filter, since_iso=since, limit=limit)
        if not rows:
            label = f" (kind={kind})" if kind != "all" else ""
            return f"No ledger entries in the past {days} days{label}."
        lines = [f"## Output Ledger — past {days} days (kind={kind})\n"]
        for r in rows:
            score = r.get("critic_score")
            score_str = str(score) if score is not None else "—"
            snippet = (r.get("content_md") or "")[:120].replace("\n", " ")
            lines.append(
                f"**[#{r['id']}]** {r['kind']}  project={r.get('project_id') or '—'}  "
                f"critic={score_str}/5  {(r.get('created_at') or '')[:10]}\n"
                f"  {snippet}…"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading output ledger: {e}"


@tool
def get_output_ledger_entry(row_id: int) -> str:
    """Return the full output ledger row by id, including provenance as markdown.
    Shows content, critic notes, model, tokens, and the full provenance chain."""
    try:
        from agent.ledger import get_entry  # noqa: PLC0415
        r = get_entry(row_id)
        if r is None:
            return f"Ledger entry #{row_id} not found."
        sections = [
            f"## Ledger Entry #{r['id']} — {r['kind']}",
            f"**Project:** {r.get('project_id') or '—'}  |  **Job:** {r.get('job_name')}  |  **Model:** {r.get('model')}",
            f"**Created:** {r.get('created_at')}",
            f"**Tokens:** in={r.get('tokens_in')} out={r.get('tokens_out')} cache_read={r.get('cache_read_tokens')}",
            "",
            "### Content",
            r.get("content_md") or "(empty)",
            "",
            "### Critic",
            f"Score: {r.get('critic_score') or '—'}/5  |  Model: {r.get('critic_model') or '—'}  |  Ran: {r.get('critic_ran_at') or '—'}",
            r.get("critic_notes") or "(no critic notes)",
            "",
            "### User Action",
            f"Action: {r.get('user_action') or '—'}  |  At: {r.get('user_action_at') or '—'}",
            r.get("user_reason") or "(no reason)",
            "",
            "### Provenance",
        ]
        prov = r.get("provenance") or {}
        if prov:
            for k, v in prov.items():
                sections.append(f"- **{k}:** {v}")
        else:
            sections.append("(none recorded)")
        return "\n".join(sections)
    except Exception as e:
        return f"Error fetching ledger entry: {e}"


@tool
def record_chat_artifact(
    kind: str,
    content_md: str,
    project_id: str = "",
    doc_id: str = "",
    cited_dois: str = "",
    notes: str = "",
) -> str:
    """Log a research artifact produced in this chat turn to the output_ledger.

    Call this AFTER you produce a hypothesis, analysis interpretation,
    literature synthesis, or grant/manuscript draft section in chat.
    Scheduled jobs populate the ledger automatically; chat-surface artifacts
    bypass it unless you call this tool explicitly.

    For kind='hypothesis' the chat hypothesis pipeline runs automatically:
    Tier 0 smoke-test filter (free) → Haiku type classifier → Sonnet
    type-aware critic, with Opus escalation if Sonnet returns a borderline
    score. The artifact is ALWAYS recorded (audit trail) but tagged with
    provenance.hypothesis_gate.passed = False if blocked. Use
    run_formal_hypothesis_pass for Opus-grade evaluation up front.

    Args:
      kind: one of 'hypothesis', 'analysis', 'literature_synthesis', 'grant_draft'
      content_md: the artifact text itself (hypothesis statement, interpretation,
        synthesis paragraph, or draft section)
      project_id: project the artifact belongs to (e.g. 'p_072'), or "" if general
      doc_id: Google Doc ID if saved to a doc — captured in provenance
      cited_dois: comma-separated DOIs cited in the artifact — captured in provenance
      notes: free-text provenance note (retrieval source, tools used, constraints)

    Returns the ledger row id and (for hypothesis kind) the gate status.
    """
    try:
        from agent.ledger import record_output, update_critic  # noqa: PLC0415
        VALID_KINDS = {"hypothesis", "analysis", "literature_synthesis", "grant_draft"}
        if kind not in VALID_KINDS:
            return (
                f"Error: kind must be one of {sorted(VALID_KINDS)}; got '{kind}'. "
                "Use 'hypothesis' for preregistrations/hypothesis proposals, "
                "'analysis' for R or Python analysis interpretations, "
                "'literature_synthesis' for paper-level findings, "
                "'grant_draft' for grant or manuscript section drafts."
            )
        prov: dict = {
            "source": "chat",
            "doc_id": doc_id or None,
            "cited_dois": [d.strip() for d in cited_dois.split(",") if d.strip()],
            "notes": notes or None,
        }

        gate_result = None
        gate_passed = True  # non-hypothesis kinds skip the gate entirely
        if kind == "hypothesis":
            try:
                from agent.hypothesis_pipeline import run_pipeline  # noqa: PLC0415
                gate_result = run_pipeline(
                    content_md=content_md,
                    notes=notes,
                    mode="chat",
                    project_id=project_id or None,
                )
                gate_passed = bool(gate_result.get("gate_passed", False))
                cr = gate_result.get("critic_result") or {}
                tc = gate_result.get("type_classification") or {}
                prov["hypothesis_gate"] = {
                    "passed": gate_passed,
                    "score": gate_result.get("score", 0),
                    "type": tc.get("type"),
                    "type_confidence": tc.get("confidence"),
                    "block_reasons": gate_result.get("block_reasons", []),
                    "warnings": gate_result.get("warnings", []),
                    "cost_estimate_usd": gate_result.get("cost_estimate_usd", 0),
                    "tier_summary": gate_result.get("tier_summary", ""),
                    "alternative_explanation_md": cr.get("alternative_explanation_md", ""),
                    "recommendations": cr.get("recommendations", []),
                }
            except Exception as gate_exc:
                prov["hypothesis_gate"] = {"error": str(gate_exc), "passed": False}
                gate_passed = False

        cr_for_row = (gate_result or {}).get("critic_result") or {}
        row_id = record_output(
            kind=kind,
            job_name="chat_session",
            model=cr_for_row.get("model") or "claude-opus-4-7",
            project_id=project_id or None,
            content_md=content_md,
            tokens_in=int(cr_for_row.get("tokens_in") or 0),
            tokens_out=int(cr_for_row.get("tokens_out") or 0),
            provenance=prov,
        )

        if gate_result is not None:
            try:
                cr = gate_result.get("critic_result") or {}
                summary_notes = (
                    cr.get("type_specific_notes")
                    or cr.get("claim_coherence_notes")
                    or gate_result.get("tier_summary", "")
                )
                update_critic(
                    row_id,
                    int(cr.get("score") or 0),
                    summary_notes,
                    cr.get("model", ""),
                )
            except Exception:
                pass

        if kind == "hypothesis" and gate_result is not None:
            from agent.hypothesis_pipeline import format_result_md  # noqa: PLC0415
            gate_block = format_result_md(gate_result)
            status = "PASSED" if gate_passed else "BLOCKED"
            return (
                f"Recorded to output_ledger as row #{row_id} "
                f"(kind=hypothesis, project_id={project_id or '—'}). "
                f"Gate {status}.\n\n{gate_block}"
            )
        return (
            f"Recorded to output_ledger as row #{row_id} "
            f"(kind={kind}, project_id={project_id or '—'})."
        )
    except Exception as e:
        return f"Error recording chat artifact: {e}"


@tool
def run_formal_hypothesis_pass(
    claim_md: str,
    project_id: str = "",
    notes: str = "",
) -> str:
    """Run the full formal hypothesis pipeline on a candidate claim — the
    third entry point alongside the weekly scheduled job and chat artifact
    recording. Use this when Heath asks for a deep evaluation of a hypothesis,
    or when a chat conversation looks like it's converging on a new project
    and you want to gate the underlying claim before it gets promoted.

    Pipeline (formal mode):
      Tier 0 — free regex filter for smoke-test/placeholder markers
      Tier 1 — Haiku classifies the hypothesis type (directional, mechanistic,
               observational, methodological, synthesis, speculative) so the
               right rubric applies
      Tier 2 — Opus runs a type-aware critic with conditional rubric items
               (sign-coherence for directional, mechanism articulation for
               mechanistic, comparison-to-current for methodological, etc.)

    Always records the run to output_ledger with kind='hypothesis' and
    provenance.hypothesis_gate, regardless of pass/fail. Gate-blocked
    artifacts are preserved as regression-test fixtures.

    Args:
      claim_md: the hypothesis statement to evaluate (2-4 sentences typical;
        longer is OK if it carries the rationale and proposed test inline)
      project_id: optional project association (e.g. 'p_072')
      notes: optional provenance note (where the claim came from)

    Returns a multi-line string with: ledger row id, gate status, type
    classification, score, mechanism/sign critique, alternative explanation,
    and cost estimate.
    """
    try:
        from agent.ledger import record_output, update_critic  # noqa: PLC0415
        from agent.hypothesis_pipeline import run_pipeline, format_result_md  # noqa: PLC0415

        gate_result = run_pipeline(
            content_md=claim_md,
            notes=notes,
            mode="formal",
            project_id=project_id or None,
        )
        gate_passed = bool(gate_result.get("gate_passed", False))
        cr = gate_result.get("critic_result") or {}
        tc = gate_result.get("type_classification") or {}

        prov: dict = {
            "source": "formal_pass",
            "notes": notes or None,
            "hypothesis_gate": {
                "passed": gate_passed,
                "score": gate_result.get("score", 0),
                "type": tc.get("type"),
                "type_confidence": tc.get("confidence"),
                "block_reasons": gate_result.get("block_reasons", []),
                "warnings": gate_result.get("warnings", []),
                "cost_estimate_usd": gate_result.get("cost_estimate_usd", 0),
                "tier_summary": gate_result.get("tier_summary", ""),
                "alternative_explanation_md": cr.get("alternative_explanation_md", ""),
                "recommendations": cr.get("recommendations", []),
            },
        }

        row_id = record_output(
            kind="hypothesis",
            job_name="run_formal_hypothesis_pass",
            model=cr.get("model") or "claude-opus-4-7",
            project_id=project_id or None,
            content_md=claim_md,
            tokens_in=int(cr.get("tokens_in") or 0),
            tokens_out=int(cr.get("tokens_out") or 0),
            provenance=prov,
        )
        try:
            summary_notes = (
                cr.get("type_specific_notes")
                or cr.get("claim_coherence_notes")
                or gate_result.get("tier_summary", "")
            )
            update_critic(
                row_id,
                int(cr.get("score") or 0),
                summary_notes,
                cr.get("model", ""),
            )
        except Exception:
            pass

        gate_block = format_result_md(gate_result)
        status = "PASSED" if gate_passed else "BLOCKED"
        return (
            f"Formal hypothesis pass recorded to output_ledger as row #{row_id} "
            f"(project_id={project_id or '—'}). Gate {status}.\n\n{gate_block}"
        )
    except Exception as e:
        return f"Error running formal hypothesis pass: {e}"


@tool
def require_data_resource(key: str) -> str:
    """Resolve a lab data resource key to its current location (path or Sheet ID).

    Call this BEFORE generating R or Python code that reads a lab database.
    On success, returns 'OK|<path-or-id>' — use the returned string directly
    (e.g. read.csv(<path>) or Sheets API with <id>). On failure, returns
    'ERROR|<reason>' — do NOT emit analysis code; tell Heath what's missing.

    Supports both schema formats:
      Old:  {"key": "<string>"}   — treated as google_sheet ID
      New:  {"key": {"kind": "local_csv"|"local_json"|"google_sheet"|"unknown",
                     "path": "...", "id": "...", "notes": "..."}}

    Args:
      key: e.g. 'coleoptera_karyotypes', 'diptera_karyotypes', 'tree_of_sex',
        'cures_karyotype_database', 'epistasis_database', 'tau_database', ...

    Returns:
      'OK|<absolute_path>'    — for local_csv / local_json (path verified on disk)
      'OK|<spreadsheet_id>'   — for google_sheet
      'ERROR|<reason>'        — key missing, kind='unknown', path not on disk,
                                or Sheet ID unset. Do not proceed.
    """
    try:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data", "known_sheets.json"
        )
        if not os.path.exists(config_path):
            return f"ERROR|known_sheets.json not found at {config_path}"
        with open(config_path) as fh:
            known = json.load(fh)
        if key not in known:
            available = ", ".join(sorted(known.keys())) or "(none)"
            return (
                f"ERROR|key '{key}' not in known_sheets.json. "
                f"Available keys: {available}"
            )
        entry = known[key]
        UNSET_IDS = {"", "PASTE_ID", "<TO-BE-FILLED>", "TO-BE-FILLED", "TBD"}

        # Backward-compat: bare string → treat as google_sheet ID
        if isinstance(entry, str):
            value = entry.strip()
            if value in UNSET_IDS:
                return (
                    f"ERROR|resource '{key}' has no ID (value='{value or '(empty)'}'). "
                    f"Update data/known_sheets.json."
                )
            return f"OK|{value}"

        if not isinstance(entry, dict):
            return f"ERROR|resource '{key}' has unexpected schema: {type(entry).__name__}"

        kind = entry.get("kind", "unknown")
        notes = entry.get("notes", "")

        if kind == "unknown":
            return (
                f"ERROR|resource '{key}' is registered but not yet configured. "
                f"Notes: {notes or '(none)'}. Ask Heath to supply a path or Sheet ID."
            )

        if kind == "google_sheet":
            sheet_id = (entry.get("id") or "").strip()
            if sheet_id in UNSET_IDS:
                return (
                    f"ERROR|resource '{key}' (google_sheet) has no ID "
                    f"(id='{sheet_id or '(empty)'}'). Notes: {notes or '(none)'}."
                )
            return f"OK|{sheet_id}"

        if kind in ("local_csv", "local_json"):
            path = (entry.get("path") or "").strip()
            if not path:
                return f"ERROR|resource '{key}' ({kind}) has no path. Notes: {notes or '(none)'}."
            if not os.path.exists(path):
                return (
                    f"ERROR|resource '{key}' ({kind}) path not on disk: {path}. "
                    f"The file may have been moved or the repo not cloned."
                )
            return f"OK|{path}"

        return f"ERROR|resource '{key}' has unrecognized kind '{kind}'"
    except Exception as e:
        return f"ERROR|could not read known_sheets.json: {e}"


@tool
def list_retrieval_quality(days: int = 7) -> str:
    """Show retrieval quality scores over a window: mean score, per-project breakdown,
    and any entries with score <= 2 (low quality). Useful for diagnosing lit-search drift."""
    try:
        from datetime import timedelta  # noqa: PLC0415
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT project_id, source_job, relevance_score, paper_title, sampled_at, critic_reasoning "
            "FROM retrieval_quality "
            "WHERE sampled_at >= ? "
            "ORDER BY sampled_at DESC",
            (since,),
        ).fetchall()
        conn.close()
        if not rows:
            return f"No retrieval quality records in the past {days} days."
        scores = [r[2] for r in rows if r[2] is not None]
        mean_score = sum(scores) / len(scores) if scores else 0.0
        # Per-project breakdown
        by_project: dict = {}
        low_quality = []
        for pid, src_job, score, title, sampled_at, reasoning in rows:
            key = pid or "(no project)"
            if key not in by_project:
                by_project[key] = []
            if score is not None:
                by_project[key].append(score)
            if score is not None and score <= 2:
                low_quality.append((pid, src_job, score, title, sampled_at, reasoning))
        lines = [f"## Retrieval Quality — past {days} days\n",
                 f"**Mean score:** {mean_score:.2f}/5  over {len(scores)} samples\n",
                 "### Per-project"]
        for proj, proj_scores in sorted(by_project.items()):
            pm = sum(proj_scores) / len(proj_scores) if proj_scores else 0.0
            lines.append(f"- {proj}: {pm:.2f}/5 ({len(proj_scores)} samples)")
        if low_quality:
            lines.append("\n### Low-quality entries (score <= 2)")
            for pid, src_job, score, title, sampled_at, reasoning in low_quality:
                lines.append(
                    f"- [{sampled_at[:10]}] {pid or '—'} | {src_job} | score={score} | {(title or '')[:80]}\n"
                    f"  Reason: {(reasoning or '')[:120]}"
                )
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading retrieval quality: {e}"


@tool
def list_aquarium_audit(days: int = 30) -> str:
    """Show aquarium privacy audit history: scan counts and any leak incidents detected.
    Useful for checking whether the public activity feed has leaked private data."""
    try:
        from datetime import timedelta  # noqa: PLC0415
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT scanned_at, entries_scanned, leaks_found, incidents_json "
            "FROM aquarium_audit_log "
            "WHERE scanned_at >= ? "
            "ORDER BY scanned_at DESC LIMIT 60",
            (since,),
        ).fetchall()
        conn.close()
        if not rows:
            return f"No aquarium audit records in the past {days} days."
        total_leaks = sum(r[2] for r in rows)
        lines = [
            f"## Aquarium Privacy Audit — past {days} days",
            f"**Scans:** {len(rows)}  |  **Total leaks found:** {total_leaks}\n",
        ]
        incidents = [(r[0], r[1], r[2], r[3]) for r in rows if r[2] > 0]
        if incidents:
            lines.append("### Leak Incidents")
            for scanned_at, entries, leaks, incidents_json in incidents:
                lines.append(f"\n**{scanned_at[:16]}**  entries={entries}  leaks={leaks}")
                try:
                    import json as _json  # noqa: PLC0415
                    data = _json.loads(incidents_json or "[]")
                    for inc in data[:5]:
                        lines.append(f"  - {inc}")
                except Exception:
                    lines.append(f"  - (could not parse: {incidents_json[:200]})")
        else:
            lines.append("No leak incidents found in this window. Privacy looks clean.")
        lines.append("\n### Scan History (most recent 10)")
        for scanned_at, entries, leaks, _ in rows[:10]:
            icon = "LEAK" if leaks > 0 else "ok"
            lines.append(f"- {scanned_at[:16]}  scanned={entries}  leaks={leaks}  [{icon}]")
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading aquarium audit log: {e}"


@tool
def get_cost_summary(days: int = 7, job_name: str = "") -> str:
    """Summarize Anthropic API costs: total $, by model, by job, cache hit rate.
    Pass job_name to filter to one job. Useful for 'how much did last week cost?'"""
    try:
        from agent.cost_tracking import summarize_costs  # noqa: PLC0415
        from datetime import timedelta  # noqa: PLC0415
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        summary = summarize_costs(
            since_iso=since,
            job_name=job_name if job_name else None,
        )
        label = f" (job={job_name})" if job_name else ""
        lines = [
            f"## Cost Summary — past {days} days{label}",
            f"**Total:** ${summary.get('total_usd', 0.0):.4f}  |  "
            f"**Cache hit rate:** {summary.get('cache_hit_rate', 0.0)*100:.1f}%\n",
            "### By Model",
            "| Model | Calls | Tokens In | Tokens Out | USD |",
            "|-------|-------|-----------|------------|-----|",
        ]
        for model, stats in sorted(summary.get("by_model", {}).items()):
            lines.append(
                f"| {model} | {stats['calls']} | {stats['tokens_in']:,} | "
                f"{stats['tokens_out']:,} | ${stats['usd']:.4f} |"
            )
        lines += [
            "",
            "### By Job",
            "| Job | Calls | Tokens In | Tokens Out | USD |",
            "|-----|-------|-----------|------------|-----|",
        ]
        for job, stats in sorted(summary.get("by_job", {}).items(), key=lambda x: -x[1]["usd"]):
            lines.append(
                f"| {job} | {stats['calls']} | {stats['tokens_in']:,} | "
                f"{stats['tokens_out']:,} | ${stats['usd']:.4f} |"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching cost summary: {e}"


@tool
def list_preference_signals(days: int = 30) -> str:
    """List Heath's expressed preferences (dismissals, rejections, adoptions, praise)
    grouped by signal_type and target_kind. Feeds into weekly preference consolidation."""
    try:
        from datetime import timedelta  # noqa: PLC0415
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT id, captured_at, signal_type, target_kind, target_id, user_reason "
            "FROM preference_signals "
            "WHERE captured_at >= ? "
            "ORDER BY captured_at DESC",
            (since,),
        ).fetchall()
        conn.close()
        if not rows:
            return f"No preference signals in the past {days} days."
        # Group by (signal_type, target_kind)
        groups: dict = {}
        for sid, captured_at, signal_type, target_kind, target_id, user_reason in rows:
            key = (signal_type, target_kind)
            if key not in groups:
                groups[key] = []
            groups[key].append((sid, captured_at, target_id, user_reason))
        lines = [f"## Preference Signals — past {days} days ({len(rows)} total)\n"]
        for (signal_type, target_kind), entries in sorted(groups.items()):
            lines.append(f"### {signal_type} / {target_kind} ({len(entries)})")
            for sid, captured_at, target_id, user_reason in entries[:10]:
                target_str = f" target_id={target_id}" if target_id else ""
                reason_str = f": {user_reason[:120]}" if user_reason else ""
                lines.append(f"- [{captured_at[:10]}] #{sid}{target_str}{reason_str}")
            if len(entries) > 10:
                lines.append(f"  …and {len(entries) - 10} more")
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading preference signals: {e}"


@tool
def record_preference_signal(
    signal_type: str,
    target_kind: str,
    target_id: int = 0,
    user_reason: str = "",
) -> str:
    """Capture a preference signal when Heath dismisses, rejects, adopts, or praises something.
    signal_type: dismissal|rejection|adoption|praise
    target_kind: briefing|hypothesis|overnight_draft|grant_opportunity|other
    Call this immediately after Heath expresses a preference — one sentence is enough."""
    valid_signal_types = {"dismissal", "rejection", "adoption", "praise"}
    valid_target_kinds = {"briefing", "hypothesis", "overnight_draft", "grant_opportunity", "other"}
    if signal_type not in valid_signal_types:
        return f"Invalid signal_type '{signal_type}'. Must be one of: {', '.join(sorted(valid_signal_types))}"
    if target_kind not in valid_target_kinds:
        return f"Invalid target_kind '{target_kind}'. Must be one of: {', '.join(sorted(valid_target_kinds))}"
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO preference_signals (captured_at, signal_type, target_kind, target_id, user_reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (now_iso, signal_type, target_kind, target_id or None, user_reason.strip() or None),
        )
        conn.commit()
        conn.close()
        target_str = f" target_id={target_id}" if target_id else ""
        reason_str = f" Reason: {user_reason[:120]}" if user_reason else ""
        return f"Preference signal recorded: {signal_type} on {target_kind}{target_str}.{reason_str}"
    except Exception as e:
        return f"Error recording preference signal: {e}"


@tool
def export_state_to_sheet(tab_name: str = "all") -> str:
    """Push the current SQLite state of goals/milestones/today/decisions/projects to the
    'Tealc Goals' Google Sheet in a single batch write per tab. Use when Heath asks to
    'sync the sheet', 'push goals to the sheet', or 'update the sheet'. Valid tab_name:
    'all', 'goals', 'milestones', 'today', 'decisions', 'projects'.

    SQLite is the canonical source of truth. The Sheet is a read-only snapshot — edits
    made in the Sheet are NOT pulled back automatically (that bidirectional sync was
    removed). If Heath edits the Sheet manually, he should tell Tealc to re-import."""
    try:
        from agent.jobs.sync_goals_sheet import (  # noqa: PLC0415
            _get_google_service, _load_config, _migrate_goals_tables,
            GOALS_HEADERS, MILESTONES_HEADERS, TODAY_HEADERS,
            DECISIONS_HEADERS, PROJECTS_HEADERS,
        )
    except Exception as e:
        return f"Error importing sync helpers: {e}"

    tab_name = (tab_name or "all").strip().lower()
    valid = {"all", "goals", "milestones", "today", "decisions", "projects"}
    if tab_name not in valid:
        return f"Invalid tab_name '{tab_name}'. Use one of: {', '.join(sorted(valid))}."

    _migrate_goals_tables()
    cfg = _load_config()
    sid = cfg.get("goals_sheet_id", "")
    if not sid or sid in ("", "PASTE_GOALS_SHEET_ID"):
        return ("No Goals Sheet bootstrapped yet. Run the one-time bootstrap with "
                "`python -m agent.jobs.sync_goals_sheet` first.")

    sheets_svc, err = _get_google_service("sheets", "v4")
    if err:
        return f"Sheets API not connected: {err}"

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    def _rows(table: str, cols: list[str]) -> list[list]:
        query_cols = ", ".join(cols)
        rows = conn.execute(f"SELECT {query_cols} FROM {table}").fetchall()
        return [[("" if v is None else v) for v in r] for r in rows]

    plan = {
        "goals":       ("Goals",       "goals",             GOALS_HEADERS,      [c if c != "date" else "date_iso" for c in GOALS_HEADERS]),
        "milestones":  ("Milestones",  "milestones_v2",     MILESTONES_HEADERS, MILESTONES_HEADERS),
        "today":       ("Today",       "today_plan",        TODAY_HEADERS,      ["date_iso" if c == "date" else c for c in TODAY_HEADERS]),
        "decisions":   ("Decisions",   "decisions_log",     DECISIONS_HEADERS,  DECISIONS_HEADERS),
        "projects":    ("Projects",    "research_projects", PROJECTS_HEADERS,   PROJECTS_HEADERS),
    }
    targets = list(plan.keys()) if tab_name == "all" else [tab_name]

    results: list[str] = []
    from googleapiclient.errors import HttpError  # noqa: PLC0415
    try:
        for key in targets:
            sheet_tab, table, headers, sql_cols = plan[key]
            try:
                rows = _rows(table, sql_cols)
            except sqlite3.OperationalError as e:
                results.append(f"{sheet_tab}: skipped ({e})")
                continue

            n_cols = len(headers)
            n_rows = len(rows) + 1  # +1 for header

            data = [headers] + rows
            last_col_letter = chr(ord("A") + n_cols - 1)
            write_range = f"{sheet_tab}!A1:{last_col_letter}{n_rows}"

            # Clear the existing tab region first (so deleted rows in DB disappear from Sheet)
            try:
                sheets_svc.spreadsheets().values().clear(
                    spreadsheetId=sid, range=f"{sheet_tab}!A1:Z10000"
                ).execute()
                sheets_svc.spreadsheets().values().update(
                    spreadsheetId=sid, range=write_range,
                    valueInputOption="USER_ENTERED",
                    body={"values": data},
                ).execute()
                results.append(f"{sheet_tab}: {len(rows)} rows")
            except HttpError as e:
                status = getattr(getattr(e, "resp", None), "status", "?")
                results.append(f"{sheet_tab}: failed (HTTP {status})")
    finally:
        conn.close()

    return "Exported to Sheet: " + "; ".join(results)


@tool
def get_activity_report(hours: int = 24) -> str:
    """Consolidated report of what Tealc has been doing recently — scheduler status, job runs,
    output ledger, cost, retrieval quality, privacy audit, pending briefings. Use this when
    Heath asks "what have you been up to", "what did you do overnight", or for a system health
    check. Default window: 24 hours; widen or narrow via the hours arg."""
    try:
        from agent.activity_report import build_activity_report  # noqa: PLC0415
        return build_activity_report(hours=hours)
    except Exception as e:
        return f"Error building activity report: {e}"


@tool
def list_analysis_bundles() -> str:
    """List reproducibility bundles (tarballs with R code + data SHA256 + results + README).
    Useful when Heath asks about reproducibility, external replication, or a past analysis."""
    try:
        from agent.bundle import list_bundles  # noqa: PLC0415
        entries = list_bundles(limit=50)
        if not entries:
            return "No analysis bundles found in data/r_runs/bundles/."
        lines = [f"## Analysis Bundles ({len(entries)} total)\n"]
        for e in entries:
            size_kb = e["bytes"] / 1024
            run_id_str = f"run_id={e['run_id']}" if e["run_id"] is not None else "run_id=?"
            lines.append(
                f"- **{os.path.basename(e['path'])}**  "
                f"{run_id_str}  {size_kb:.1f} KB  "
                f"created={e['created_iso'][:10]}\n"
                f"  Path: `{e['path']}`"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing analysis bundles: {e}"


@tool
def respond_to_review_invitation(briefing_id: int = 0, decision: str = "review") -> str:
    """List pending review invitations OR respond to one.
    - briefing_id=0 (default): list all unacknowledged review_invitation briefings with the drafted reply preview.
    - briefing_id=<id>, decision='accept'|'decline'|'review':
      'accept' = show the accept draft; 'decline' = show the decline draft; 'review' = show both drafts for Heath to manually edit/send.
    Never auto-sends — always returns draft content for Heath to approve.
    Enforces Heath's service-protection rule: defaults to 'decline' posture unless Heath explicitly overrides."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")

        if briefing_id == 0:
            # List all unacknowledged review_invitation briefings
            rows = conn.execute(
                "SELECT id, title, content_md, created_at FROM briefings "
                "WHERE kind='review_invitation' AND surfaced_at IS NULL "
                "ORDER BY id DESC"
            ).fetchall()
            conn.close()
            if not rows:
                return "No pending review invitation briefings. Use list_email_triage_decisions(classification='review_invitation') to see all past ones."
            lines = [f"## Pending Review Invitations ({len(rows)} unacknowledged)\n"]
            lines.append(
                "_Heath's default posture: DECLINE unless topical fit is VERY high AND it's a top-tier journal._\n"
            )
            for row_id, title, content_md, created_at in rows:
                ts = (created_at or "")[:16]
                # Pull a brief excerpt from content_md for preview
                preview_lines = (content_md or "").splitlines()[:8]
                preview = "\n  ".join(preview_lines)
                lines.append(
                    f"**Briefing ID {row_id}** — {ts}\n"
                    f"  {title}\n"
                    f"  {preview}\n"
                    f"  → Call respond_to_review_invitation(briefing_id={row_id}, decision='review') to see full drafts.\n"
                )
            return "\n".join(lines)

        else:
            # Fetch the specific briefing
            row = conn.execute(
                "SELECT id, title, content_md, surfaced_at FROM briefings "
                "WHERE id=? AND kind='review_invitation'",
                (briefing_id,),
            ).fetchone()
            conn.close()
            if not row:
                return f"No review_invitation briefing found with id={briefing_id}."

            row_id, title, content_md, surfaced_at = row
            content = content_md or ""

            # Extract decline and accept drafts from content_md
            decline_draft = ""
            accept_draft = ""
            if "**Decline draft:**" in content:
                parts = content.split("**Decline draft:**", 1)
                after_decline = parts[1]
                if "**Accept draft:**" in after_decline:
                    decline_draft = after_decline.split("**Accept draft:**")[0].strip()
                    accept_draft = after_decline.split("**Accept draft:**", 1)[1].strip()
                else:
                    decline_draft = after_decline.strip()
            elif "**Accept draft:**" in content:
                accept_draft = content.split("**Accept draft:**", 1)[1].strip()

            decision_lower = (decision or "review").lower()

            if decision_lower == "decline":
                output = (
                    f"## Decline Draft for: {title}\n\n"
                    f"**Service-protection rule applied — default: DECLINE.**\n\n"
                    f"**Draft body (ready to copy into Gmail):**\n\n{decline_draft or '(no decline draft found in briefing)'}\n\n"
                    f"Open Gmail Drafts to review and send. Briefing ID: {row_id}"
                )
            elif decision_lower == "accept":
                output = (
                    f"## Accept Draft for: {title}\n\n"
                    f"**Override: ACCEPT — Heath explicitly chose to accept this review.**\n\n"
                    f"**Draft body (ready to copy into Gmail):**\n\n{accept_draft or '(no accept draft found in briefing)'}\n\n"
                    f"Open Gmail Drafts to review and send. Briefing ID: {row_id}"
                )
            else:  # 'review' or anything else
                output = (
                    f"## Review Invitation Detail — Briefing ID {row_id}\n\n"
                    f"{content}\n\n"
                    f"---\n"
                    f"**To proceed:** call respond_to_review_invitation(briefing_id={row_id}, decision='decline') "
                    f"or respond_to_review_invitation(briefing_id={row_id}, decision='accept').\n"
                    f"**Heath's default: DECLINE** (massive admin burden; only accept if fit is very high AND top-tier journal)."
                )

            return output
    except Exception as e:
        return f"Error in respond_to_review_invitation: {e}"


@tool
def run_python_script(code: str, working_dir: str = "", timeout_seconds: int = 300) -> str:
    """Run Python code in a sandboxed working dir. Returns stdout, stderr, exit code, plot paths,
    and any other files created. Packages available: pandas, numpy, matplotlib, scipy, statsmodels,
    seaborn, scikit-learn. Use when Heath asks to analyze data, make a plot, or run a quick
    computation. Code must save plots as .png (saved alongside the script).
    working_dir: leave empty for fresh dir; pass a path to run in an existing project data dir."""
    try:
        from agent.python_runtime.executor import run_python  # noqa: PLC0415
        wd = working_dir if working_dir.strip() else None
        r = run_python(code=code, working_dir=wd, timeout_seconds=timeout_seconds)
        # Format return as a human-readable markdown block
        out = [
            f"**run_id:** `{r['run_id']}`",
            f"**exit code:** {r['exit_code']}",
            f"**duration:** {r['duration_seconds']:.2f}s",
            f"**working_dir:** `{r['working_dir']}`",
        ]
        if r.get('plot_paths'):
            out.append("**plots:** " + ", ".join(f"`{p}`" for p in r['plot_paths']))
        if r.get('created_files'):
            out.append("**created files:** " + ", ".join(f"`{f}`" for f in r['created_files'] if f != 'script.py'))
        out.append(f"\n**stdout:**\n```\n{r['stdout'] or '(empty)'}\n```")
        if r.get('stderr'):
            out.append(f"\n**stderr:**\n```\n{r['stderr']}\n```")
        return "\n".join(out)
    except Exception as e:
        return f"Error running Python: {e}"


@tool
def inspect_project_data(project_id: str) -> str:
    """Walk a research project's data_dir and return a tree summary — file counts, sizes,
    modification dates, extension breakdown. Use when Heath asks what data a project has,
    or before running analysis on it."""
    try:
        from agent.data_introspect import inspect_project_data as _inspect  # noqa: PLC0415
        r = _inspect(project_id)
        if not r.get('data_dir'):
            return f"Project `{project_id}` has no data_dir set. Use `propose_data_dir` first."
        if not r.get('exists'):
            return f"Project `{project_id}` data_dir `{r['data_dir']}` does not exist on disk."
        out = [
            f"# {r['project_name']} — data inventory",
            f"**Path:** `{r['data_dir']}`",
            f"**Totals:** {r['total_files']} files · {r['total_bytes']:,} bytes · most recent: {r.get('most_recent_iso','-')}",
            f"\n**By extension:**",
        ]
        for ext, stats in sorted((r.get('summary_by_extension') or {}).items(), key=lambda x: -x[1]['count']):
            out.append(f"- `{ext or '(no ext)'}`: {stats['count']} files, {stats['bytes']:,} bytes")
        out.append(f"\n**Recent files (top 20):**")
        for item in (r.get('tree') or [])[:20]:
            out.append(f"- `{item['path']}` — {item.get('bytes',0):,} bytes, {item.get('modified_iso','')[:10]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error inspecting project data: {e}"


@tool
def propose_data_dir(project_id: str) -> str:
    """Scan likely storage locations and propose data_dir candidates for a project that has none.
    Returns ranked candidates with match reasons. Heath then confirms by calling
    update_research_project with the chosen path."""
    try:
        from agent.data_introspect import propose_data_dir as _propose  # noqa: PLC0415
        r = _propose(project_id)
        cands = r.get('candidates') or []
        if not cands:
            return f"No candidate data_dirs found for `{project_id}` ({r.get('project_name','?')})."
        out = [f"# data_dir candidates for **{r.get('project_name','?')}** (`{project_id}`)"]
        if r.get('current_data_dir'):
            out.append(f"_Current:_ `{r['current_data_dir']}`\n")
        for i, c in enumerate(cands, 1):
            out.append(f"**{i}.** `{c['path']}`")
            out.append(f"   — {c.get('match_reason','')} · {c.get('file_count',0)} files · modified {c.get('most_recent_iso','')[:10]}")
        out.append("\n_To adopt one:_ `update_research_project(project_id, data_dir=\"<chosen path>\")`")
        return "\n".join(out)
    except Exception as e:
        return f"Error proposing data_dir: {e}"


@tool
def pre_submission_review(doc_text: str, venue: str = "journal_generic") -> str:
    """Run 3 reviewer personas (methodologist, domain_expert, skeptic) on a draft before
    submission. Returns per-persona scores + concerns + blocking issues, plus a consensus
    summary. Use when Heath asks for a critique of a manuscript, grant section, or reviewer
    response. venue options: 'journal_generic', 'nature_tier', 'MIRA_study_section',
    'NSF_DEB', 'google_org_grant'."""
    try:
        from agent.submission_review import pre_submission_review as _review  # noqa: PLC0415
        r = _review(doc_text=doc_text, venue=venue)
        out = [f"# Pre-submission review — venue: **{r.get('venue','?')}**"]
        for rev in r.get('reviews', []):
            out.append(f"\n## {rev.get('persona','?')} — score **{rev.get('score','?')}/5**")
            if rev.get('blocking_issues'):
                out.append("**Blocking issues:**")
                for b in rev['blocking_issues']:
                    out.append(f"- {b}")
            if rev.get('concerns'):
                out.append("**Concerns:**")
                for c in rev['concerns']:
                    out.append(f"- {c}")
            if rev.get('suggested_revisions'):
                out.append("**Suggested revisions:**")
                for s in rev['suggested_revisions']:
                    out.append(f"- {s}")
            if rev.get('notes'):
                out.append(f"_{rev['notes']}_")
        out.append(f"\n## Consensus\n{r.get('consensus','')}")
        return "\n".join(out)
    except Exception as e:
        return f"Error running pre-submission review: {e}"


@tool
def enter_war_room(project_id: str) -> str:
    """Enter focused work mode on a single research project. Pulls the latest draft, recent
    literature notes, open hypotheses, decisions log, current next_action. Use when Heath
    says 'let's focus on the chromosomal stasis paper' or 'work on MIRA renewal'. Returns a
    consolidated context packet suitable for deep-focus work. Heath should follow up by
    saying 'exit war room' when done."""
    try:
        import sqlite3 as _sql  # noqa: PLC0415
        conn = _sql.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        proj = conn.execute(
            "SELECT id, name, description, current_hypothesis, next_action, data_dir, "
            "output_dir, linked_artifact_id, linked_goal_ids, keywords, notes "
            "FROM research_projects WHERE id=?", (project_id,)
        ).fetchone()
        if not proj:
            conn.close()
            return f"Project `{project_id}` not found."
        lit = conn.execute(
            "SELECT title, publication_year, extracted_findings_md, relevance_to_project "
            "FROM literature_notes WHERE project_id=? ORDER BY created_at DESC LIMIT 5",
            (project_id,)
        ).fetchall()
        hyps = conn.execute(
            "SELECT id, hypothesis_md, status FROM hypothesis_proposals "
            "WHERE project_id=? AND status='proposed' ORDER BY proposed_iso DESC LIMIT 5",
            (project_id,)
        ).fetchall()
        drafts = conn.execute(
            "SELECT id, drafted_section, draft_doc_url, reviewed_at FROM overnight_drafts "
            "WHERE project_id=? ORDER BY created_at DESC LIMIT 5",
            (project_id,)
        ).fetchall()
        conn.close()
        out = [f"# War room — **{proj[1]}** (`{proj[0]}`)\n"]
        out.append(f"**Description:** {proj[2] or '(empty)'}")
        out.append(f"**Current hypothesis:** {proj[3] or '(empty)'}")
        out.append(f"**Next action:** {proj[4] or '(empty — ask me to propose one)'}")
        out.append(f"**Data dir:** `{proj[5] or '(empty)'}`")
        out.append(f"**Linked artifact (Google Doc):** `{proj[7] or '(empty)'}`")
        if lit:
            out.append(f"\n## Recent literature notes ({len(lit)})")
            for t, y, _f, rel in lit:
                out.append(f"- **{t}** ({y}) — {rel or 'no relevance note'}")
        if hyps:
            out.append(f"\n## Open hypothesis proposals ({len(hyps)})")
            for hid, h, s in hyps:
                out.append(f"- [{hid}] ({s}) {h[:200]}")
        if drafts:
            out.append(f"\n## Recent overnight drafts ({len(drafts)})")
            for did, sect, url, rev in drafts:
                out.append(f"- [{did}] **{sect}** — {url} — {'reviewed' if rev else 'UNREVIEWED'}")
        out.append("\n_War room mode: I'm anchored to this project. Tell me the specific blocker and we'll work through it. Say 'exit war room' when done._")
        return "\n".join(out)
    except Exception as e:
        return f"Error entering war room: {e}"


# ─── V6: External Science API tools ──────────────────────────────────────────

# Europe PMC
@tool
def fetch_paper_full_text(pmcid: str) -> str:
    """Fetch full text (methods, results, discussion) of an open-access paper by PMCID (e.g. 'PMC1790863').
    Use when an abstract isn't enough and you need to read what the paper actually did."""
    try:
        from agent.apis.europe_pmc import fetch_and_extract  # noqa: PLC0415
        r = fetch_and_extract(pmcid)
        if not r:
            return f"Could not fetch full text for {pmcid}. Paper may not be open-access or PMCID is invalid."
        out = [f"# {pmcid}"]
        for k in ("abstract", "introduction", "methods", "results", "discussion", "conclusions"):
            v = r.get(k, "").strip()
            if v:
                out.append(f"\n## {k.title()}\n{v[:4000]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error fetching full text: {e}"


@tool
def search_literature_full_text(query: str, since_iso: str = "", limit: int = 10) -> str:
    """Search Europe PMC for open-access papers with full text available. Returns title, authors, PMCID, abstract.
    Prefer this over search_pubmed when you plan to read full text, not just abstract."""
    try:
        from agent.apis.europe_pmc import search_full_text  # noqa: PLC0415
        results = search_full_text(query, since_iso=since_iso or None, limit=limit)
        if not results:
            return f"No open-access full-text results for '{query}'."
        out = [f"Found {len(results)} open-access papers for '{query}':\n"]
        for r in results:
            out.append(f"- **{r.get('title','')}** ({r.get('pub_year','?')}) — {r.get('authors','')[:120]}")
            out.append(f"  PMCID: `{r.get('pmcid','')}` · Journal: {r.get('journal','')}")
            if r.get('abstract'):
                out.append(f"  _{r['abstract'][:300]}_")
        return "\n".join(out)
    except Exception as e:
        return f"Error searching Europe PMC: {e}"


# Semantic Scholar
@tool
def get_citation_contexts(doi_or_pmid: str, limit: int = 10) -> str:
    """Fetch citing papers for a paper, WITH the actual sentences that cite it.
    Use when Heath wants to know how his work is being cited (confirmation, extension, critique).
    Accepts DOI (e.g. '10.1038/xyz') or PMID (e.g. 'PMID:12345678')."""
    try:
        from agent.apis.semantic_scholar import get_citing_papers  # noqa: PLC0415
        paper_id = doi_or_pmid if doi_or_pmid.startswith(("DOI:", "PMID:", "PMCID:", "ARXIV:")) else f"DOI:{doi_or_pmid}"
        citations = get_citing_papers(paper_id, limit=limit)
        if not citations:
            return f"No citing papers found for {doi_or_pmid}."
        out = [f"Citation contexts for {doi_or_pmid} ({len(citations)} citing papers):\n"]
        for c in citations:
            cp = c.get("citingPaper", {})
            out.append(f"\n**{cp.get('title','')}** ({cp.get('year','?')}) — {cp.get('venue','')}")
            intents = c.get("intents", [])
            if intents:
                out.append(f"_Intent: {', '.join(intents)}_ · Influential: {c.get('isInfluential', False)}")
            for ctx in (c.get("contexts") or [])[:2]:
                out.append(f"> {ctx[:300]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error fetching citation contexts: {e}"


@tool
def get_paper_recommendations(seed_dois: str, limit: int = 10) -> str:
    """Given one or more seed papers (comma-separated DOIs or Semantic Scholar IDs), return recommended related papers.
    Useful for finding adjacent literature Heath might not have encountered."""
    try:
        from agent.apis.semantic_scholar import get_recommendations  # noqa: PLC0415
        positives = [s.strip() if s.strip().startswith(("DOI:","PMID:","PMCID:","ARXIV:")) else f"DOI:{s.strip()}"
                     for s in seed_dois.split(",") if s.strip()]
        recs = get_recommendations(positives, limit=limit)
        if not recs:
            return "No recommendations returned."
        out = [f"Recommendations anchored on {len(positives)} seed paper(s):\n"]
        for r in recs:
            tldr = (r.get("tldr") or {}).get("text") if isinstance(r.get("tldr"), dict) else r.get("tldr")
            out.append(f"- **{r.get('title','')}** ({r.get('year','?')}) — {r.get('citationCount','?')} cites")
            if tldr:
                out.append(f"  _{tldr[:300]}_")
        return "\n".join(out)
    except Exception as e:
        return f"Error fetching recommendations: {e}"


@tool
def get_my_author_profile() -> str:
    """Fetch Heath's own author profile from Semantic Scholar: paper list, h-index, citation counts,
    TLDRs for each paper. Uses his ORCID 0000-0002-5433-4036."""
    try:
        from agent.apis.semantic_scholar import author_search, get_author, get_author_papers  # noqa: PLC0415
        candidates = author_search("Heath Blackmon", limit=3)
        author_id = None
        for c in candidates:
            if "Blackmon" in (c.get("name") or ""):
                author_id = c.get("authorId")
                break
        if not author_id:
            return "Could not resolve Heath's Semantic Scholar author ID."
        a = get_author(author_id) or {}
        papers = get_author_papers(author_id, limit=30)
        out = [
            f"**{a.get('name','?')}** — h-index {a.get('hIndex','?')}, {a.get('paperCount','?')} papers, {a.get('citationCount','?')} citations total",
            f"\nTop {len(papers)} papers:",
        ]
        for p in papers[:30]:
            out.append(f"- **{p.get('title','')}** ({p.get('year','?')}) — {p.get('citationCount','?')} cites · {p.get('venue','')}")
        return "\n".join(out)
    except Exception as e:
        return f"Error fetching author profile: {e}"


# OpenTree + TimeTree
@tool
def get_phylogenetic_tree(taxon_names: str, ultrametric: bool = False) -> str:
    """Fetch a Newick-format tree covering the given taxa (comma-separated scientific names).
    If ultrametric=True, scales branches using TimeTree divergence estimates (rough).
    Use when starting a comparative analysis — no need to hand-build the tree."""
    try:
        from agent.apis.opentree import get_induced_subtree_by_names, ultrametricize_newick  # noqa: PLC0415
        names = [n.strip() for n in taxon_names.split(",") if n.strip()]
        if len(names) < 3:
            return "Need at least 3 taxa for a tree."
        r = get_induced_subtree_by_names(names)
        if not r.get("newick"):
            return f"Could not build tree. Resolved: {list((r.get('resolved') or {}).keys())}. Unresolved: {r.get('unresolved', [])}"
        newick = r["newick"]
        if ultrametric:
            scaled = ultrametricize_newick(newick, method="timetree_calibrated")
            if scaled:
                newick = scaled
        out = [f"Tree with {len(r.get('resolved',{}))} taxa (unresolved: {len(r.get('unresolved',[]))}):\n"]
        out.append(f"```newick\n{newick[:2500]}\n```")
        if r.get("unresolved"):
            out.append(f"\n_Unresolved names: {', '.join(r['unresolved'])}_")
        return "\n".join(out)
    except Exception as e:
        return f"Error building tree: {e}"


@tool
def get_divergence_time(taxon_a: str, taxon_b: str) -> str:
    """Get TimeTree divergence estimate (million years ago) between two taxa."""
    try:
        from agent.apis.opentree import get_divergence_time as _div  # noqa: PLC0415
        r = _div(taxon_a, taxon_b)
        if not r:
            return f"Could not resolve divergence time between {taxon_a} and {taxon_b}."
        return (f"**{taxon_a}** ↔ **{taxon_b}**: {r.get('mya_median','?')} MYA "
                f"(range {r.get('mya_min','?')}–{r.get('mya_max','?')}, from {r.get('study_count','?')} studies)")
    except Exception as e:
        return f"Error: {e}"


# NCBI Entrez
@tool
def resolve_taxonomy(name: str) -> str:
    """Resolve a species/genus name to canonical NCBI Taxonomy (tax_id, lineage, accepted name).
    Use to catch synonyms in the karyotype DBs and before any cross-database join."""
    try:
        from agent.apis.ncbi_entrez import taxonomy_search  # noqa: PLC0415
        r = taxonomy_search(name)
        if not r:
            return f"No NCBI taxonomy match for '{name}'."
        out = [
            f"**{r.get('scientific_name','?')}** (tax_id: `{r.get('tax_id','?')}`, rank: {r.get('rank','?')})",
            f"Lineage: {r.get('lineage_str','')[:400]}",
        ]
        if r.get('is_synonym'):
            out.append(f"_Synonym — accepted tax_id: {r.get('accepted_tax_id','?')}_")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


@tool
def search_sra_runs(organism: str = "", query: str = "", limit: int = 10) -> str:
    """Search NCBI SRA for sequencing runs. At least one of organism or query required.
    Returns run accessions (SRR/ERR), platform, library strategy."""
    try:
        from agent.apis.ncbi_entrez import sra_search  # noqa: PLC0415
        results = sra_search(organism=organism or None, query=query or None, limit=limit)
        if not results:
            return "No SRA runs found."
        out = [f"Found {len(results)} SRA run(s):\n"]
        for r in results:
            out.append(f"- `{r.get('run_id','?')}` — {r.get('library_strategy','?')}/{r.get('library_source','?')} · "
                      f"{r.get('platform','?')} · {r.get('bases','?')} bases")
            if r.get("title"):
                out.append(f"  _{r['title'][:200]}_")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


# NIH + NSF
@tool
def search_funded_grants(query: str, agency: str = "both", activity_code: str = "R35") -> str:
    """Search for FUNDED grants matching a topic with their abstracts.
    agency: 'nih', 'nsf', or 'both'. activity_code applies to NIH only ('R01','R35','R21', etc.).
    Use when drafting grants to find exemplar language from successful recent proposals."""
    try:
        from agent.apis.grants import nih_search_by_topic_and_mechanism, nsf_search_awards  # noqa: PLC0415
        out = []
        if agency in ("nih", "both"):
            nih = nih_search_by_topic_and_mechanism(query, activity_code=activity_code, fiscal_years=[2022, 2023, 2024], limit=5)
            if nih:
                out.append(f"## NIH {activity_code} awards matching '{query}' ({len(nih)} found)")
                for a in nih:
                    out.append(f"\n**{a.get('project_title','')}** — PI: {a.get('pi_names','')} · ${a.get('award_amount','?'):,} · FY{a.get('fiscal_year','?')}")
                    if a.get('abstract_text'):
                        out.append(f"_{a['abstract_text'][:600]}_")
        if agency in ("nsf", "both"):
            nsf = nsf_search_awards(query=query, limit=5)
            if nsf:
                out.append(f"\n## NSF awards matching '{query}' ({len(nsf)} found)")
                for a in nsf:
                    out.append(f"\n**{a.get('title','')}** — PI: {a.get('pi_first_name','')} {a.get('pi_last_name','')} · ${a.get('funding_amount','?'):,}")
        return "\n".join(out) if out else f"No funded grants found for '{query}'."
    except Exception as e:
        return f"Error: {e}"


# GBIF
@tool
def get_species_distribution(scientific_name: str) -> str:
    """Get geographic + temporal distribution summary for a species from GBIF occurrences.
    Returns country breakdown, sampling bias score, year range, top collections."""
    try:
        from agent.apis.gbif import geographic_summary  # noqa: PLC0415
        r = geographic_summary(scientific_name)
        out = [
            f"# {r.get('canonical_name','?')} distribution",
            f"**Total occurrences:** {r.get('total_occurrences','?'):,} · Years {r.get('year_range',('?','?'))[0]}–{r.get('year_range',('?','?'))[1]}",
            f"**Sampling bias score:** {r.get('sampling_bias_score',0):.2f} (1 = single-country; 0 = uniform)",
            f"\n**Top countries:**",
        ]
        for iso, n in (r.get("top_countries") or [])[:10]:
            out.append(f"- {iso}: {n:,}")
        basis = r.get("basis_summary") or {}
        if basis:
            out.append(f"\n**Record types:** " + ", ".join(f"{k} ({v:,})" for k, v in list(basis.items())[:5]))
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"


# Zenodo
@tool
def list_zenodo_deposits(limit: int = 20) -> str:
    """List Heath's Zenodo deposits (drafts + published). Returns DOI, title, state, dates."""
    try:
        from agent.apis.zenodo import is_configured, list_deposits  # noqa: PLC0415
        if not is_configured():
            return "Zenodo not configured. Set ZENODO_ACCESS_TOKEN in .env to enable deposition."
        results = list_deposits(limit=limit)
        if not results:
            return "No Zenodo deposits found."
        out = [f"Zenodo deposits ({len(results)}):\n"]
        for d in results:
            state = d.get("state", "?")
            doi = d.get("doi", "(not minted)")
            out.append(f"- `{d.get('deposit_id','?')}` — {state} — DOI: {doi}")
            out.append(f"  **{d.get('title','')}** · modified: {d.get('modified','')[:10]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error: {e}"



# ─────────────────────────────────────────────────────────────────────────────
# V7: Knowledge Map tools


@tool
def find_resource(query: str, kind: str = "") -> str:
    """Look up a resource (file, folder, Doc, Sheet, person, URL) in Heath's knowledge map.
    Use this BEFORE searching Drive, Gmail, or the filesystem when Heath mentions a project
    name, dataset, database, or person by name. kind filter: 'research_project', 'google_doc',
    'google_sheet', 'drive_folder', 'local_dir', 'grant', 'github_repo', 'email_contact',
    'external_url', or empty for any."""
    try:
        from agent.knowledge_map import find_resource as _find  # noqa: PLC0415
        kw = kind.strip() or None
        hits = _find(query, kind=kw, limit=8)
        if not hits:
            return (f"No resources matched '{query}'. If this is something Heath uses regularly, "
                    f"offer to add it via add_resource().")
        out = [f"Found {len(hits)} resource(s) matching '{query}':\n"]
        for r in hits:
            out.append(f"- **{r.get('display_name','?')}** ({r.get('kind','?')})")
            if r.get('purpose'):
                out.append(f"  _{r['purpose']}_")
            handle = r.get('handle','')
            if handle and not handle.startswith('<'):
                out.append(f"  `{handle}`")
            tags = r.get('tags') or []
            if tags:
                out.append(f"  tags: {', '.join(tags)}")
        return "\n".join(out)
    except Exception as e:
        return f"Error searching knowledge map: {e}"


@tool
def add_resource(kind: str, handle: str, display_name: str, purpose: str = "",
                 tags: str = "", linked_project_ids: str = "", notes: str = "") -> str:
    """Add a resource to Heath's knowledge map. Use when Heath mentions a new file/folder/Doc/
    Sheet/person/URL you don't have cataloged, OR when Heath asks you to 'remember' a location.
    kind must be one of: 'research_project','google_doc','google_sheet','drive_folder',
    'local_dir','grant','github_repo','email_contact','external_url','other'.
    handle: the URL/path/ID/email. tags + linked_project_ids: comma-separated."""
    try:
        from agent.knowledge_map import add_resource as _add  # noqa: PLC0415
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        project_list = [p.strip() for p in linked_project_ids.split(",") if p.strip()] if linked_project_ids else []
        r = _add(kind=kind, handle=handle, display_name=display_name, purpose=purpose,
                 tags=tag_list, linked_project_ids=project_list, notes=notes,
                 proposed_by='heath')
        return f"Added resource `{r.get('id','?')}` — **{display_name}** ({kind}) · status: {r.get('status','?')}"
    except Exception as e:
        return f"Error adding resource: {e}"


@tool
def list_resources(kind: str = "", status: str = "confirmed") -> str:
    """List Heath's cataloged resources. kind: filter to one kind or empty for all.
    status: 'confirmed' (default), 'proposed' (auto-seeded, not yet reviewed), 'dismissed',
    or 'all'."""
    try:
        from agent.knowledge_map import load_catalog  # noqa: PLC0415
        rows = load_catalog(status=None if status == 'all' else status)
        if kind.strip():
            rows = [r for r in rows if r.get('kind') == kind.strip()]
        if not rows:
            return f"No resources (kind={kind or 'any'}, status={status})."
        by_kind: dict[str, list] = {}
        for r in rows:
            by_kind.setdefault(r.get('kind','other'), []).append(r)
        out = [f"Found {len(rows)} resource(s):\n"]
        for k, items in sorted(by_kind.items()):
            out.append(f"\n## {k} ({len(items)})")
            for r in items[:10]:
                out.append(f"- **{r.get('display_name','?')}** — `{(r.get('handle') or '')[:80]}`")
            if len(items) > 10:
                out.append(f"  _(+{len(items)-10} more)_")
        return "\n".join(out)
    except Exception as e:
        return f"Error listing resources: {e}"


@tool
def confirm_resource(resource_id: str) -> str:
    """Mark a proposed resource as confirmed. Use when Heath says a resource's entry is correct
    (e.g., 'yes that's the Coleoptera DB' — confirm it)."""
    try:
        from agent.knowledge_map import confirm_resource as _c  # noqa: PLC0415
        r = _c(resource_id)
        if not r:
            return f"Resource `{resource_id}` not found."
        return f"Confirmed: **{r.get('display_name','?')}**"
    except Exception as e:
        return f"Error: {e}"


@tool
def update_resource(resource_id: str, handle: str = "", display_name: str = "",
                    purpose: str = "", tags: str = "", notes: str = "") -> str:
    """Update fields on an existing resource. Use when Heath corrects something (e.g., 'no the
    Sheet ID is actually X — update that'). Only pass the fields that change; leave others blank."""
    try:
        from agent.knowledge_map import update_resource as _u  # noqa: PLC0415
        fields: dict = {}
        if handle: fields['handle'] = handle
        if display_name: fields['display_name'] = display_name
        if purpose: fields['purpose'] = purpose
        if tags:
            fields['tags'] = [t.strip() for t in tags.split(",") if t.strip()]
        if notes: fields['notes'] = notes
        if not fields:
            return "No fields changed (all inputs empty)."
        r = _u(resource_id, **fields)
        if not r:
            return f"Resource `{resource_id}` not found."
        return f"Updated: **{r.get('display_name','?')}** ({', '.join(fields.keys())})"
    except Exception as e:
        return f"Error: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Grants — split out from research_projects on 2026-04-24
# ─────────────────────────────────────────────────────────────────────────────

@tool
def list_grants(status: str = "all") -> str:
    """List active grant applications from the `grants` table.

    Args:
      status: in_prep | submitted | awarded | declined | dropped | all
              (default 'all').  Use `list_grant_opportunities` for externally
              radar-scored leads Heath hasn't started writing yet — that's a
              different table.

    Returns a markdown table with id, name, agency, program, status,
    deadline, and linked Google Doc.
    """
    try:
        conn = _get_projects_db()  # re-uses data/agent.db
        where = ""
        params: tuple = ()
        if status and status != "all":
            where = " WHERE status=?"
            params = (status,)
        rows = conn.execute(
            "SELECT id, name, agency, program, status, deadline_iso, "
            "linked_artifact_id FROM grants" + where + " ORDER BY deadline_iso, id",
            params,
        ).fetchall()
        conn.close()
        if not rows:
            return (
                f"No grants found (status={status}). Active grant applications "
                "live in the `grants` table (moved out of research_projects on "
                "2026-04-24). Use list_grant_opportunities for externally-scored "
                "radar leads."
            )
        lines = [
            f"## Grants (status={status})\n",
            "| id | name | agency | program | status | deadline | doc |",
            "|----|------|--------|---------|--------|----------|-----|",
        ]
        for gid, name, agency, program, gstatus, deadline, doc in rows:
            doc_link = f"[doc](https://docs.google.com/document/d/{doc})" if doc else ""
            lines.append(
                f"| {gid} | {name} | {agency or ''} | {program or ''} "
                f"| {gstatus or ''} | {(deadline or '')[:10]} | {doc_link} |"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing grants: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Lab drive layout — fresh listing of the top-level of Blackmon Lab/
# ─────────────────────────────────────────────────────────────────────────────

@tool
def list_lab_drive_root() -> str:
    """Return the current top-level layout of Heath's shared `Blackmon Lab/`
    Drive.  The folder names are self-describing — this tool is the canonical
    answer to "what's in my Drive?" and the right first step before any
    search_drive call when Heath refers to a specific folder by name.

    The system prompt already ships a snapshot of this at chat start; call
    this tool only when Heath has just created/renamed a top-level folder
    and the baked-in snapshot is stale.
    """
    root = os.path.expanduser(
        "~/Library/CloudStorage/GoogleDrive-coleoguy@gmail.com/"
        "Shared drives/Blackmon Lab"
    )
    try:
        entries = sorted(os.listdir(root))
    except Exception as exc:
        return f"Could not read lab Drive root: {exc}"
    folders, root_files = [], []
    for e in entries:
        if e.startswith(".") or e.startswith("_"):
            continue
        full = os.path.join(root, e)
        (folders if os.path.isdir(full) else root_files).append(e)
    lines = ["## Blackmon Lab/ — top level", ""]
    if folders:
        lines.append(f"**Folders ({len(folders)}):**")
        for f in folders:
            lines.append(f"- {f}")
        lines.append("")
    if root_files:
        lines.append(f"**Files at root ({len(root_files)}):**")
        for f in root_files:
            lines.append(f"- {f}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Self-introspection + on-demand job execution
# ─────────────────────────────────────────────────────────────────────────────

@tool
def run_scheduled_job(
    name: str,
    verbose: bool = False,
    dry_run: bool | None = None,
    target: str | None = None,
) -> str:
    """Force-run a scheduled Tealc job right now, bypassing its working-hours
    guard.  Use when Heath asks "run X now", "trigger X", "kick off X", or
    "do X for me" where X is the name of a background job.

    Args:
      name: job module name (e.g. 'paper_of_the_day', 'wiki_janitor',
            'gloss_harvester', 'morning_briefing').  Must match a file in
            agent/jobs/<name>.py.  See describe_capabilities for the full list.
      verbose: passed through to jobs that accept it.  Default False.
      dry_run: optional override for jobs that accept a dry_run_override
               kwarg (gloss_harvester, method_promoter, surface_composer,
               improve_wiki).  Ignored for jobs that don't.
      target: optional slug/term filter for jobs that accept one
              (force_slug in method_promoter, force_term in gloss_harvester,
              force_slug in surface_composer).  Ignored for others.

    Returns the job's own summary string, or a clear error if the job fails
    or the name is unknown.  All runs are still recorded in the job_runs
    table by the @tracked decorator.
    """
    try:
        from agent.jobs import run_job_now  # noqa: PLC0415
    except Exception as exc:
        return f"import error: {exc}"
    try:
        result = run_job_now(name, verbose=verbose, dry_run=dry_run, target=target)
    except ValueError as exc:
        # Friendly message with the list of known jobs
        try:
            from agent.jobs import list_available_jobs  # noqa: PLC0415
            known = list_available_jobs()
            hint = f"  Known jobs: {', '.join(known)}"
        except Exception:
            hint = ""
        return f"{exc}.{hint}"
    except Exception as exc:
        return f"{name} failed: {type(exc).__name__}: {exc}"
    return result or f"{name} returned no summary (check job_runs table)."


def _load_or_rebuild_abilities() -> dict:
    """Return the abilities catalog dict.  Rebuilds synchronously if the file
    is missing or older than 14 days."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.normpath(os.path.join(here, "..", "data", "abilities.json"))
    stale = True
    data: dict | None = None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        ts = data.get("generated_at", "")
        if ts:
            try:
                gen = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - gen).total_seconds() / 86400.0
                stale = age_days > 14
            except Exception:
                stale = True
    except Exception:
        stale = True

    if stale or data is None:
        try:
            from agent.jobs.publish_abilities import job as _rebuild  # noqa: PLC0415
            _rebuild()
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            if data is None:
                return {"_error": f"abilities catalog unavailable: {exc}"}
    return data or {}


@tool
def describe_capabilities(verbose: bool = False) -> str:
    """Return a programmatic summary of what Tealc can do for Heath.

    Call this whenever Heath asks any variant of "what can you do?",
    "what are your capabilities?", "how can you help me?", "what tools do you
    have?", "what are the jobs?", "tell me what you can do for me",
    "describe yourself", or "help" at the top of a conversation.  DO NOT
    answer from memory — the catalog is regenerated weekly and drifts.

    Args:
      verbose: if True, include every tool's one-line summary (longer
        response, useful for thorough answers).  If False (default), show
        category counts with 3 example tools per category + the full list
        of scheduled jobs grouped by cadence.

    Reads data/abilities.json (rebuilt Wednesdays 5am by publish_abilities,
    or synchronously if that file is missing or older than 14 days).
    """
    data = _load_or_rebuild_abilities()
    if data.get("_error"):
        return f"Couldn't load capabilities catalog: {data['_error']}"

    counts = data.get("counts", {})
    tools_by_cat = data.get("tool_catalog", [])  # [{category, tools: [{name, summary, ...}]}]
    jobs = data.get("job_catalog", [])            # [{name, summary, schedule}]

    lines: list[str] = []
    lines.append("# What Tealc can do for Heath")
    lines.append("")
    lines.append(
        f"**As of {data.get('generated_at','unknown')[:10]}:** "
        f"{counts.get('tools','?')} chat tools, "
        f"{counts.get('jobs','?')} scheduled background jobs, "
        f"{counts.get('tables','?')} SQLite tables."
    )
    lines.append("")

    # ---- Tool categories ---------------------------------------------------
    lines.append("## Tool categories")
    for cat in tools_by_cat:
        name = cat.get("category", "?")
        items = cat.get("tools", [])
        if not items:
            continue
        examples = ", ".join(t.get("name", "") for t in items[:3])
        lines.append(f"- **{name}** ({len(items)} tools) — e.g. `{examples}`")
        if verbose:
            for t in items:
                lines.append(f"    - `{t.get('name','?')}` — {t.get('summary','')[:120]}")
    lines.append("")

    # ---- Jobs by cadence ---------------------------------------------------
    def _cadence(schedule: str) -> str:
        s = (schedule or "").lower()
        if "interval" in s or "minute" in s:
            return "Every few minutes"
        if "day_of_week" in s or any(d in s for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")):
            return "Weekly"
        if "crontrigger" in s and "day_of_week" not in s:
            return "Daily"
        if s:
            return "Scheduled"
        return "Unknown cadence"

    from collections import defaultdict
    by_cadence: dict[str, list[dict]] = defaultdict(list)
    for j in jobs:
        by_cadence[_cadence(j.get("schedule", ""))].append(j)

    lines.append("## Scheduled jobs")
    cadence_order = ["Every few minutes", "Daily", "Weekly", "Scheduled", "Unknown cadence"]
    for cad in cadence_order:
        group = by_cadence.get(cad, [])
        if not group:
            continue
        lines.append(f"### {cad} ({len(group)})")
        for j in sorted(group, key=lambda x: x.get("name", "")):
            nm = j.get("name", "?")
            sm = (j.get("summary", "") or "").strip()
            if sm:
                lines.append(f"- **{nm}** — {sm[:140]}")
            else:
                lines.append(f"- **{nm}**")
        lines.append("")

    # ---- How to drive them from chat --------------------------------------
    lines.append("## How to drive Tealc from chat")
    lines.append("")
    lines.append(
        "- \"Run <job_name> now\" → I'll call `run_scheduled_job` and force-run "
        "it, bypassing any working-hours guard (e.g. \"run wiki_janitor now\")."
    )
    lines.append(
        "- \"What can you do?\" / \"what are your capabilities?\" → I'll call "
        "`describe_capabilities` (this tool)."
    )
    lines.append(
        "- Any specific task — just describe it in plain English and I'll "
        "pick the right tool(s)."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tier 4 — Tier-1 — Tier-3 corpus support (added 2026-04-28)
# ---------------------------------------------------------------------------

# --- Zenodo write-side ---
@tool
def zenodo_create_deposit(
    title: str,
    description: str,
    creators_json: str,
    upload_type: str = "dataset",
    keywords: str = "",
    sandbox: bool = False,
) -> str:
    """Create a new Zenodo draft deposit (reserves a DOI, does NOT mint). creators_json: JSON list of {name, affiliation, orcid?}. sandbox=True → sandbox.zenodo.org. Idempotent on title."""
    import json as _json
    try:
        from agent.apis.zenodo import create_deposit, is_configured  # noqa: PLC0415
        if not is_configured(sandbox=sandbox):
            tok = "ZENODO_SANDBOX_TOKEN" if sandbox else "ZENODO_ACCESS_TOKEN"
            return f"Zenodo not configured. Set {tok}."
        try:
            creators = _json.loads(creators_json)
        except _json.JSONDecodeError as e:
            return f"creators_json must be valid JSON: {e}"
        kw = [k.strip() for k in keywords.split(",") if k.strip()]
        meta = {"title": title, "description": description, "creators": creators,
                "upload_type": upload_type, "access_right": "open", "license": "cc-by-4.0"}
        if kw: meta["keywords"] = kw
        dep = create_deposit(meta, sandbox=sandbox)
        if "error" in dep:
            return f"Error: {dep['error']}\n{dep.get('detail', '')}"
        dep_id = dep.get("id") or dep.get("deposit_id")
        pre_doi = dep.get("metadata", {}).get("prereserve_doi", {}).get("doi") or dep.get("doi_reserved")
        html = dep.get("links", {}).get("html") or dep.get("html_url")
        return f"Deposit created (state={dep.get('state','?')}).\n  deposit_id: {dep_id}\n  reserved DOI: {pre_doi}\n  URL: {html}"
    except Exception as e:
        return f"Error: {e}"

@tool
def zenodo_upload_file(deposit_id: int, file_path: str, sandbox: bool = False) -> str:
    """Upload a local file to a Zenodo draft deposit. Streams >10MB."""
    try:
        from agent.apis.zenodo import upload_zenodo_file, is_configured  # noqa: PLC0415
        if not is_configured(sandbox=sandbox):
            tok = "ZENODO_SANDBOX_TOKEN" if sandbox else "ZENODO_ACCESS_TOKEN"
            return f"Zenodo not configured. Set {tok}."
        result = upload_zenodo_file(deposit_id, file_path, sandbox=sandbox)
        if "error" in result:
            return f"Upload failed: {result['error']}\n{result.get('detail', '')}"
        return f"File uploaded.\n  filename: {result.get('filename')}\n  size_bytes: {result.get('size_bytes')}\n  checksum: {result.get('checksum')}\n  file_id: {result.get('file_id')}"
    except Exception as e:
        return f"Error: {e}"

@tool
def zenodo_publish_deposit(deposit_id: int, confirmed: bool = False, sandbox: bool = False) -> str:
    """IRREVERSIBLE. Publish a Zenodo draft, minting its DOI. confirmed must be True."""
    try:
        from agent.apis.zenodo import publish_deposit, is_configured  # noqa: PLC0415
        if not is_configured(sandbox=sandbox):
            tok = "ZENODO_SANDBOX_TOKEN" if sandbox else "ZENODO_ACCESS_TOKEN"
            return f"Zenodo not configured. Set {tok}."
        result = publish_deposit(deposit_id, confirmed=confirmed, sandbox=sandbox)
        if "error" in result:
            return f"Publish failed: {result['error']}\n{result.get('detail', '')}"
        return f"Deposit published.\n  DOI: {result.get('doi')}\n  URL: {result.get('html_url')}\n  published: {result.get('published_date')}"
    except ValueError as e:
        return f"Safety check: {e}"
    except Exception as e:
        return f"Error: {e}"

# --- API helpers (Tier 4 gaps) ---
@tool
def epmc_cache_full_text(pmcid: str, dest_dir: str = "/tmp/tealc_fulltext") -> dict:
    """Fetch+cache Europe PMC JATS XML; extract intro/methods/results/discussion."""
    try:
        from agent.apis.europe_pmc import cache_full_text  # noqa: PLC0415
        return cache_full_text(pmcid, dest_dir)
    except Exception as exc:
        return {"error": str(exc), "pmcid": pmcid}

@tool
def s2_search_papers(query: str, year_min: int | None = None, year_max: int | None = None, limit: int = 100) -> list:
    """Semantic Scholar paper search. Returns [{paperId, title, abstract, year, authors, doi}]."""
    try:
        from agent.apis.semantic_scholar import search_papers  # noqa: PLC0415
        return search_papers(query, year_min=year_min, year_max=year_max, limit=limit)
    except Exception as exc:
        return [{"error": str(exc)}]

@tool
def gbif_bulk_occurrence_centroid(species_list: list) -> dict:
    """GBIF occurrence centroid (lat/lon/bbox/n_records) per Latin binomial. <5 records → None."""
    try:
        from agent.apis.gbif import bulk_occurrence_centroid  # noqa: PLC0415
        return bulk_occurrence_centroid(species_list)
    except Exception as exc:
        return {"error": str(exc)}

@tool
def pubmed_batch_fetch(pmids: list) -> list:
    """Batch fetch PubMed records by PMID list."""
    try:
        from agent.apis.ncbi_entrez import entrez_efetch_pubmed  # noqa: PLC0415
        return entrez_efetch_pubmed(pmids)
    except Exception as exc:
        return [{"error": str(exc)}]

@tool
def ncbi_assembly_summary(taxon: str) -> list:
    """List GenBank assemblies for a taxon (name or TaxID)."""
    try:
        from agent.apis.ncbi_entrez import genbank_assembly_summary  # noqa: PLC0415
        return genbank_assembly_summary(taxon)
    except Exception as exc:
        return [{"error": str(exc)}]

@tool
def timetree_age_distribution(taxon_a: str, taxon_b: str) -> dict:
    """TimeTree full divergence-time distribution between two taxa."""
    try:
        from agent.apis.opentree import get_age_distribution  # noqa: PLC0415
        return get_age_distribution(taxon_a, taxon_b)
    except Exception as exc:
        return {"error": str(exc), "taxon_a": taxon_a, "taxon_b": taxon_b}

# --- Prereg-Replication Loop chat tools ---
@tool
def list_pending_preregs() -> str:
    """List preregistrations awaiting T+7 adjudication."""
    import sqlite3, json as _json
    try:
        from agent.scheduler import DB_PATH  # noqa: PLC0415
    except ImportError:
        from pathlib import Path
        DB_PATH = str(Path(__file__).parent.parent / "data" / "agent.db")
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, hypothesis_md, prereg_published_at, prereg_test_json FROM hypothesis_proposals "
        "WHERE prereg_published_at IS NOT NULL AND adjudicated_at IS NULL ORDER BY prereg_published_at ASC"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            test = _json.loads(r[3] or "{}").get("test_name")
        except Exception:
            test = None
        out.append({"id": r[0], "hypothesis_md": (r[1] or "")[:200], "prereg_published_at": r[2], "test": test})
    return _json.dumps(out)

@tool
def get_prereg_outcome(hypothesis_id: int) -> str:
    """Full prereg + verdict for a hypothesis_proposals row."""
    import sqlite3, json as _json
    try:
        from agent.scheduler import DB_PATH  # noqa: PLC0415
    except ImportError:
        from pathlib import Path
        DB_PATH = str(Path(__file__).parent.parent / "data" / "agent.db")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM hypothesis_proposals WHERE id=?", (hypothesis_id,)).fetchone()
    conn.close()
    if row is None:
        return _json.dumps({"error": "not found"})
    d = dict(row)
    try:
        d["prereg_test_json"] = _json.loads(d.get("prereg_test_json") or "{}")
    except Exception:
        pass
    return _json.dumps(d)

# --- Reviewer Circle chat tools ---
@tool
def list_reviewer_invitations(status: str = "") -> str:
    """List reviewer_invitations rows. Optional status filter ('draft'|'sent'|'replied'|'expired')."""
    import sqlite3, json as _json
    try:
        from agent.scheduler import DB_PATH  # noqa: PLC0415
    except ImportError:
        from pathlib import Path
        DB_PATH = str(Path(__file__).parent.parent / "data" / "agent.db")
    conn = sqlite3.connect(DB_PATH)
    if status:
        rows = conn.execute("SELECT id, reviewer_pseudonym, domain, status, sla_iso, sent_at FROM reviewer_invitations WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT id, reviewer_pseudonym, domain, status, sla_iso, sent_at FROM reviewer_invitations ORDER BY created_at DESC").fetchall()
    conn.close()
    return _json.dumps([{"id": r[0], "pseudonym": r[1], "domain": r[2], "status": r[3], "sla_iso": r[4], "sent_at": r[5]} for r in rows])

@tool
def get_reviewer_correlation(domain: str = "") -> str:
    """Latest Opus-critic-vs-human Spearman correlations. Optional domain filter."""
    import sqlite3, json as _json
    try:
        from agent.scheduler import DB_PATH  # noqa: PLC0415
    except ImportError:
        from pathlib import Path
        DB_PATH = str(Path(__file__).parent.parent / "data" / "agent.db")
    conn = sqlite3.connect(DB_PATH)
    if domain:
        rows = conn.execute("SELECT computed_at, domain, dimension, n_pairs, spearman_r, bootstrap_ci_lo, bootstrap_ci_hi FROM reviewer_correlations WHERE domain=? ORDER BY computed_at DESC LIMIT 20", (domain,)).fetchall()
    else:
        rows = conn.execute("SELECT computed_at, domain, dimension, n_pairs, spearman_r, bootstrap_ci_lo, bootstrap_ci_hi FROM reviewer_correlations ORDER BY computed_at DESC LIMIT 20").fetchall()
    conn.close()
    return _json.dumps([{"computed_at": r[0], "domain": r[1], "dimension": r[2], "n_pairs": r[3], "spearman_r": r[4], "ci_lo": r[5], "ci_hi": r[6]} for r in rows])


# ─────────────────────────────────────────────────────────────────────────────

def get_all_tools():
    return [
        search_pubmed,
        search_biorxiv,
        search_openalex,
        web_search,
        track_citations,
        list_recent_emails,
        draft_email_reply,
        find_trash_candidates,
        trash_emails,
        list_upcoming_events,
        search_drive,
        read_drive_file,
        list_pdfs_in_drive_folder,
        download_drive_pdf,
        ingest_paper_to_wiki,
        read_wiki_handoff,
        list_wiki_topics,
        read_wiki_topic,
        retrieve_voice_exemplars,
        send_ntfy_to_heath,
        read_lab_website,
        read_local_file,
        read_docx_with_comments,
        notify_heath,
        save_note,
        list_notes,
        read_note,
        delete_note,
        get_datetime,
        # Task 5 — Google Docs write-back
        create_google_doc,
        append_to_google_doc,
        replace_in_google_doc,
        insert_comment_in_google_doc,
        # Task 6 — Calendar write access
        create_calendar_event,
        update_calendar_event,
        delete_calendar_event,
        find_free_slots,
        # Task 7 — R script execution
        run_r_script,
        # Task 8 — Google Sheets read/write
        list_sheets_in_spreadsheet,
        read_sheet,
        append_rows_to_sheet,
        update_sheet_cells,
        search_sheet,
        # Task 9 — Grant opportunity radar
        list_grant_opportunities,
        dismiss_grant_opportunity,
        # Task 10 — Student milestone tracker
        list_students,
        student_dashboard,
        log_milestone,
        log_interaction,
        students_needing_attention,
        # Pending intentions queue
        add_intention,
        list_intentions,
        complete_intention,
        abandon_intention,
        update_intention,
        # Rolling context snapshot
        get_idle_class,
        get_current_context,
        refresh_context_now,
        # Executive loop audit
        list_executive_decisions,
        # Email triage subagent
        list_email_triage_decisions,
        list_pending_service_requests,
        review_recent_drafts,
        respond_to_review_invitation,
        # Paper of the day
        get_paper_of_the_day,
        list_recent_papers_of_the_day,
        # Long-term conversation memory
        recall_past_conversations,
        list_recent_sessions,
        # Weekly self-review
        get_latest_weekly_review,
        # Quarterly retrospective
        get_latest_quarterly_retrospective,
        # NAS-metric tracker
        get_latest_nas_metrics,
        nas_metrics_trend,
        # NAS impact scoring
        get_nas_impact_trend,
        # Task 11 — Goals Sheet
        list_goals,
        get_goal,
        add_goal,
        propose_goal_from_idea,
        update_goal,
        add_milestone_to_goal,
        decompose_goal,
        update_milestone,
        list_milestones_for_goal,
        write_today_plan,
        get_today_plan,
        log_decision,
        # Goal-conflict surfacing
        list_goal_conflicts,
        acknowledge_goal_conflict,
        # Research project abstraction
        list_research_projects,
        get_research_project,
        add_research_project,
        update_research_project,
        set_project_next_action,
        complete_project_next_action,
        # Nightly literature synthesis read-back
        get_recent_literature_for_project,
        list_recent_literature_notes,
        # Overnight grant drafter
        list_overnight_drafts,
        review_overnight_draft,
        # Database health
        list_database_flags,
        trigger_database_health_check,
        # Overnight comparative analysis read-back
        list_analysis_runs,
        get_analysis_run_detail,
        # Weekly hypothesis generator
        list_hypothesis_proposals,
        adopt_hypothesis,
        run_hypothesis_tournament,
        reject_hypothesis,
        # Web fetching
        fetch_url,
        fetch_url_links,
        # v2: output ledger + critic + preference learning + observability
        list_output_ledger,
        get_output_ledger_entry,
        record_chat_artifact,
        run_formal_hypothesis_pass,
        require_data_resource,
        list_retrieval_quality,
        list_aquarium_audit,
        get_cost_summary,
        list_preference_signals,
        record_preference_signal,
        export_state_to_sheet,
        get_activity_report,
        list_analysis_bundles,
        # v5: Python execution, data discovery, reviewer emulator, war room
        run_python_script,
        inspect_project_data,
        propose_data_dir,
        pre_submission_review,
        enter_war_room,
        # v6: External Science APIs
        fetch_paper_full_text,
        search_literature_full_text,
        get_citation_contexts,
        get_paper_recommendations,
        get_my_author_profile,
        get_phylogenetic_tree,
        get_divergence_time,
        resolve_taxonomy,
        search_sra_runs,
        search_funded_grants,
        get_species_distribution,
        list_zenodo_deposits,
        # Zenodo write tools
        zenodo_create_deposit, zenodo_upload_file, zenodo_publish_deposit,
        # Tier 4 corpus helpers
        epmc_cache_full_text, s2_search_papers, gbif_bulk_occurrence_centroid,
        pubmed_batch_fetch, ncbi_assembly_summary, timetree_age_distribution,
        # Prereg-Replication Loop
        list_pending_preregs, get_prereg_outcome,
        # Reviewer Circle
        list_reviewer_invitations, get_reviewer_correlation,
        # v7: Knowledge Map
        find_resource,
        add_resource,
        list_resources,
        confirm_resource,
        update_resource,
        # Self-introspection + on-demand job execution
        run_scheduled_job,
        describe_capabilities,
        # Grants (split from research_projects on 2026-04-24)
        list_grants,
        # Lab drive layout
        list_lab_drive_root,
    ]
