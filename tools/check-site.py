#!/usr/bin/env python3
"""Copy and markup checks for the briq.ie legal site.

Python 3 standard library only, no dependencies. Exits 0 when every check
passes and 1 when any check fails, printing one line per failure as

    path:line: reason

Run it from anywhere:

    python tools/check-site.py            # checks the repo this script lives in
    python tools/check-site.py <folder>   # checks another copy of the tree

Why each check exists is written beside it. The legal text on this site has to
say exactly what the app says, so these checks guard the mistakes that have
actually been made here rather than style preferences.
"""

import html
import os
import re
import sys
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# Banned strings, with the reason each one is banned. A match in a page's raw
# markup OR in its rendered text fails the check.
# ---------------------------------------------------------------------------
BANNED = [
    ("ipify",
     "The app stopped looking up an IP address on 2026-11-10, so ipify is no "
     "longer a recipient of personal data and must not be named as one."),
    ("ElevenLabs",
     "Kids Zone narration is read by Microsoft, not ElevenLabs. Naming a "
     "superseded vendor misstates the recipient list under Art. 13(1)(e)."),
    ("9(2)(a)",
     "The medical card answer never leaves the device, so there is no "
     "server-side processing to claim an Art. 9(2)(a) explicit-consent basis "
     "for. The app says we do not process it on our systems."),
    ("Settings →",
     "There is no Settings screen in the app. It was deleted at 6768697. The "
     "real path is Me, then Privacy & data."),
    ("Settings &rarr;",
     "Same as above, written as an HTML entity."),
    ("Privacy & Security",
     "No Privacy & Security group has ever existed in the app. The group is "
     "Security & privacy and the row is Privacy & data."),
    ("Privacy &amp; Security",
     "Same as above, written as an HTML entity."),
    ("GDPR Compliance",
     "The privacy hub screen is titled Privacy & data. No screen in the app "
     "is called GDPR Compliance."),
    ("Data Subject Rights",
     "The card a user taps is called See what data we hold. No card in the "
     "app is called Data Subject Rights."),
    ("security audits",
     "Nothing in the app or its repo backs a claim of regular security "
     "audits, and the app's own security section does not make it."),
    ("whether you have a partner",
     "The app's list of circumstances does not collect this, so disclosing "
     "it over-claims a collection we do not perform."),
    ("marketing emails",
     "No code writes a marketing consent: the only consent_type writer is "
     "app/kids/parental-consent.tsx. Naming it as a legal basis over-claims."),
]

# The complete recipient list in section 6 of the privacy page, in the order
# the app lists them (constants/processors.ts and app/privacy-policy.tsx).
# Section 6 promises this is the complete list of our processors, so an extra
# or a missing name is a GDPR Art. 13(1)(e) defect, not a copy defect.
EXPECTED_RECIPIENTS = [
    "Supabase",
    "Postmark",
    "Apple",
    "Google",
    "RevenueCat",
    "Expo",
    "Sentry",
    "Resend",
    "Microsoft",
]

# Dash entities spelled out. The rendered-text scan catches any other spelling,
# because unescaping turns every form into the literal character.
DASH_ENTITIES = re.compile(
    r"&mdash;|&ndash;|&#8212;|&#8211;|&#x2014;|&#x2013;", re.IGNORECASE
)
DASH_LITERALS = {chr(0x2014): "em dash", chr(0x2013): "en dash"}

# Tags that never carry a closing tag.
VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# A token is exempt from the fada check when it is a URL, a domain or an email
# address: briq.ie and privacy@briq.ie are correct as written.
EXEMPT_TOKEN = re.compile(
    r"://|@|\b[A-Za-z0-9][A-Za-z0-9._-]*\.(?:ie|com|net|org|eu|io|dev)\b",
    re.IGNORECASE,
)
BARE_BRIQ = re.compile(r"(?<![A-Za-z])briq(?![A-Za-z])", re.IGNORECASE)

DATE_RE = re.compile(r"\b(\d{1,2} [A-Z][a-z]+ \d{4})\b")

failures = []


def fail(path, line, reason):
    failures.append((path, line, reason))


def mask_comments(text):
    """Blank out HTML comments, keeping line numbering intact."""
    def blank(match):
        return re.sub(r"[^\n]", " ", match.group(0))
    return re.sub(r"(?s)<!--.*?-->", blank, text)


def strip_tags_per_line(line):
    """Rendered text of one line: tags removed, entities resolved.

    The content attribute of a meta description is kept, because it ships to
    readers even though it is not in the body.
    """
    kept = ""
    meta = re.search(
        r"""<meta\s+name=["']description["']\s+content=["']([^"']*)["']""",
        line, re.IGNORECASE,
    )
    if meta:
        kept = " " + meta.group(1)
    visible = re.sub(r"<[^>]*>", " ", line) + kept
    return html.unescape(visible)


class TagMatcher(HTMLParser):
    """Reports unmatched and stray closing tags, with line numbers."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.problems = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_ELEMENTS:
            self.stack.append((tag, self.getpos()[0]))

    def handle_endtag(self, tag):
        if tag in VOID_ELEMENTS:
            return
        line = self.getpos()[0]
        if not self.stack:
            self.problems.append((line, "stray closing tag </%s>" % tag))
            return
        open_tag, open_line = self.stack[-1]
        if open_tag != tag:
            self.problems.append((
                line,
                "</%s> closes <%s> which was opened at line %d"
                % (tag, open_tag, open_line),
            ))
        else:
            self.stack.pop()

    def unclosed(self):
        return self.stack


def check_dashes(rel, raw_lines):
    """(a) No em or en dash anywhere, literal or as an entity."""
    for n, line in enumerate(raw_lines, 1):
        entities = {m.group(0) for m in DASH_ENTITIES.finditer(line)}
        for entity in sorted(entities):
            fail(rel, n, "dash entity %s is not allowed in site copy; the "
                         "app's copy carries no dashes" % entity)
        if entities:
            # The rendered scan below would only repeat what we just reported.
            continue
        rendered = strip_tags_per_line(line)
        for char, name in DASH_LITERALS.items():
            if char in line or char in rendered:
                fail(rel, n, "%s is not allowed in site copy; the app's copy "
                             "carries no dashes" % name)


def check_fada(rel, raw_lines):
    """(b) Briq is always written Bríq outside domains, URLs and emails."""
    for n, line in enumerate(raw_lines, 1):
        rendered = strip_tags_per_line(line)
        title = re.search(r"(?is)<title>(.*?)</title>", line)
        if title:
            rendered += " " + html.unescape(title.group(1))
        for token in rendered.split():
            if EXEMPT_TOKEN.search(token):
                continue
            if BARE_BRIQ.search(token):
                fail(rel, n, "'%s' is missing the fada: write Bríq (domains, "
                             "URLs and email addresses are exempt)" % token)


def check_banned(rel, raw_lines):
    """(c) None of the banned claims or stale screen names appear."""
    for n, line in enumerate(raw_lines, 1):
        rendered = strip_tags_per_line(line)
        haystacks = (line.lower(), rendered.lower())
        for needle, reason in BANNED:
            if any(needle.lower() in hay for hay in haystacks):
                fail(rel, n, "banned string '%s': %s" % (needle, reason))


def check_structure(rel, path, raw, raw_lines, html_files, root):
    """(d) Links resolve, one skip link, one main landmark, tags match."""
    skips = len(re.findall(r"<a\b[^>]*class=[\"']skip[\"']", raw, re.IGNORECASE))
    if skips != 1:
        fail(rel, 1, "expected exactly 1 skip link, found %d" % skips)

    mains = len(re.findall(r"<main\b", raw, re.IGNORECASE))
    if mains != 1:
        fail(rel, 1, "expected exactly 1 main landmark, found %d" % mains)
    elif not re.search(r"<main\b[^>]*id=[\"']main[\"']", raw, re.IGNORECASE):
        fail(rel, 1, "the main landmark has no id=\"main\" for the skip link "
                     "to target")

    for n, line in enumerate(raw_lines, 1):
        for href in re.findall(r"href=[\"']([^\"']*)[\"']", line):
            if href.startswith(("http://", "https://", "mailto:", "tel:")):
                continue
            if href.startswith("#"):
                anchor = href[1:]
                if anchor and ("id=\"%s\"" % anchor) not in raw \
                        and ("id='%s'" % anchor) not in raw:
                    fail(rel, n, "anchor %s has no matching id in this page"
                         % href)
                continue
            if not href:
                fail(rel, n, "empty href")
                continue
            if href.startswith("/"):
                target = os.path.join(root, href.lstrip("/"))
            else:
                target = os.path.join(os.path.dirname(path), href)
            if href.endswith("/") or os.path.isdir(target):
                target = os.path.join(target, "index.html")
            if not os.path.isfile(target):
                fail(rel, n, "internal link %s resolves to %s, which does not "
                             "exist" % (href, os.path.relpath(target, root)))

    matcher = TagMatcher()
    matcher.feed(raw)
    matcher.close()
    for line, problem in matcher.problems:
        fail(rel, line, "markup is not well formed: %s" % problem)
    for tag, line in matcher.unclosed():
        fail(rel, line, "markup is not well formed: <%s> is never closed" % tag)


def printed_date(raw):
    match = re.search(r"(?is)<p[^>]*class=[\"']updated[\"'][^>]*>(.*?)</p>", raw)
    if not match:
        return None, None
    text = html.unescape(re.sub(r"<[^>]*>", " ", match.group(1)))
    found = DATE_RE.search(text)
    return (found.group(1) if found else None), text.strip()


def check_dates(root, html_files):
    """(e) The privacy date agrees with its meta description; delete-account
    prints a date in its masthead."""
    privacy = os.path.join(root, "privacy", "index.html")
    if privacy in html_files:
        raw = open(privacy, encoding="utf-8").read()
        rel = os.path.relpath(privacy, root).replace(os.sep, "/")
        shown, text = printed_date(raw)
        meta = re.search(
            r"""(?is)<meta\s+name=["']description["']\s+content=["']([^"']*)["']""",
            raw,
        )
        meta_date = None
        if meta:
            found = DATE_RE.search(html.unescape(meta.group(1)))
            meta_date = found.group(1) if found else None
        line = 1
        for n, l in enumerate(raw.splitlines(), 1):
            if "class=\"updated\"" in l:
                line = n
                break
        if shown is None:
            fail(rel, line, "no printed date found in a <p class=\"updated\">")
        elif meta_date is None:
            fail(rel, line, "the meta description carries no date to compare "
                            "the printed date against")
        elif shown != meta_date:
            fail(rel, line, "printed date %r does not match the meta "
                            "description date %r" % (shown, meta_date))

    deletion = os.path.join(root, "delete-account", "index.html")
    if deletion in html_files:
        raw = open(deletion, encoding="utf-8").read()
        rel = os.path.relpath(deletion, root).replace(os.sep, "/")
        masthead = re.search(
            r"(?is)<header[^>]*class=[\"']masthead[\"'][^>]*>(.*?)</header>", raw
        )
        if not masthead:
            fail(rel, 1, "no <header class=\"masthead\"> to carry a date")
        elif not DATE_RE.search(html.unescape(masthead.group(1))):
            fail(rel, 1, "the masthead prints no date; readers and the stores "
                         "need to see when this page was last updated")


def check_recipients(root, html_files):
    """(f) Section 6 names exactly the app's nine processors."""
    privacy = os.path.join(root, "privacy", "index.html")
    if privacy not in html_files:
        return
    raw = open(privacy, encoding="utf-8").read()
    rel = os.path.relpath(privacy, root).replace(os.sep, "/")
    anchor = raw.find("Service providers")
    if anchor == -1:
        fail(rel, 1, "cannot find the 'Service providers.' paragraph that "
                     "introduces the recipient list")
        return
    block = re.search(r"(?is)<ul>(.*?)</ul>", raw[anchor:])
    if not block:
        fail(rel, raw[:anchor].count("\n") + 1,
             "no recipient list follows the 'Service providers.' paragraph")
        return
    line = raw[:anchor + block.start()].count("\n") + 1
    names = re.findall(r"(?is)<li>\s*<strong>(.*?)</strong>", block.group(1))
    names = [html.unescape(n).strip() for n in names]
    if names == EXPECTED_RECIPIENTS:
        return
    missing = [n for n in EXPECTED_RECIPIENTS if n not in names]
    extra = [n for n in names if n not in EXPECTED_RECIPIENTS]
    parts = []
    if missing:
        parts.append("missing %s" % ", ".join(missing))
    if extra:
        parts.append("unexpected %s" % ", ".join(extra))
    if not parts:
        parts.append("the order differs from the app's: found %s"
                     % ", ".join(names))
    fail(rel, line, "section 6 recipient list must be exactly the app's nine "
                    "processors (%s): %s"
                    % (", ".join(EXPECTED_RECIPIENTS), "; ".join(parts)))


def main(argv):
    # Failure messages contain Bríq, so do not let a legacy console codepage
    # turn a real failure into a UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    root = os.path.abspath(
        argv[1] if len(argv) > 1
        else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    if not os.path.isdir(root):
        print("check-site: %s is not a directory" % root, file=sys.stderr)
        return 2

    html_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.lower().endswith(".html"):
                html_files.append(os.path.join(dirpath, name))

    if not html_files:
        print("check-site: no .html files found under %s" % root,
              file=sys.stderr)
        return 2

    for path in html_files:
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        raw = open(path, encoding="utf-8").read()
        masked = mask_comments(raw)
        lines = masked.splitlines()
        check_dashes(rel, lines)
        check_fada(rel, lines)
        check_banned(rel, lines)
        check_structure(rel, path, raw, lines, html_files, root)

    check_dates(root, html_files)
    check_recipients(root, html_files)

    if failures:
        for path, line, reason in sorted(failures):
            print("%s:%d: %s" % (path, line, reason))
        print("\ncheck-site: %d failure(s) across %d page(s)"
              % (len(failures), len(html_files)), file=sys.stderr)
        return 1

    print("check-site: %d page(s) checked, all checks passed" % len(html_files))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
