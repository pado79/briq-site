#!/usr/bin/env python3
"""Copy and markup checks for the briq.ie legal site.

Python 3 standard library only, no dependencies. Exits 0 when every check
passes and 1 when any check fails, printing one line per failure as

    path:line: reason

Run it from anywhere:

    python tools/check-site.py            # checks the repo this script lives in
    python tools/check-site.py <folder>   # checks another copy of the tree

Pass the app checkout to compare this site against the app's legal text, which
is the source of truth for what these pages may say:

    python tools/check-site.py --app ../briq
    python tools/check-site.py <folder> --app <app folder>

Without --app the site is checked on its own, exactly as before, and the
cross-checks print a line saying they were skipped. With it, the controller
identity, the contact addresses, the privacy date, the processor list, the
data categories and the purposes are read OUT OF THE APP SOURCE and compared.
Nothing the app states is hard-coded here: every expected value is parsed from

    constants/legalEntity.ts      the controller, address, registration number
    constants/legalDocuments.ts   PRIVACY_LAST_UPDATED
    app/privacy-policy.tsx        processors, data categories, purposes, emails

so a change in the app turns into a failure here rather than into a silent
disagreement between the two surfaces.

Why each check exists is written beside it. The legal text on this site has to
say exactly what the app says, so these checks guard the mistakes that have
actually been made here rather than style preferences.
"""

import html
import json
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
#
# THIS IS THE FALLBACK, used only when --app is not given. With --app the list
# is parsed out of the app's privacy policy instead, so it cannot go stale.
#
# ⚠️ AND IT DID GO STALE, WHICH IS WHY THE NOTE ABOVE IS NOT THE WHOLE STORY. The fallback
# carried "Postmark" until 24 November 2026, and Postmark was never a processor: it was disclosed
# on the strength of a stale pm-bounces DNS record on briq.ie, and the app's own
# constants/processors.ts admitted at the time that the finding was not provable from the repo.
# Resend is the confirmed sender of the sign-in mail, and it was already on this list for the
# mail a user chooses to send, so the authentication purpose merges into its entry and THE COUNT
# FALLS FROM NINE TO EIGHT. Nothing is added.
#
# ⚠️ THE LESSON FOR THIS CONSTANT, NOT ONLY THE CORRECTION. A hardcoded list in the fallback path
# is invisible for as long as CI uses --app, so it can disagree with the app for weeks and only
# fail for a developer running the script bare. That is exactly what happened here: --app passed
# and the bare run was the only thing that caught it. Anyone editing the app's recipient list
# must edit this too, or delete the fallback and make --app mandatory.
EXPECTED_RECIPIENTS = [
    "Supabase",
    "Apple",
    "Google",
    "RevenueCat",
    "Expo",
    "Sentry",
    "Resend",
    "Microsoft",
]

# Placeholder tokens a draft may carry and a published page may not. Lane A
# fills PUBLISH_DATE with the day it publishes; this is what stops an unfilled
# draft reaching production.
PLACEHOLDER_TOKENS = [
    ("PUBLISH_DATE",
     "the effective date is still the literal placeholder; fill it with the "
     "publication date, written as '18 November 2026'"),
    ("TODO", "a TODO marker must not ship in legal copy"),
    ("TBC", "a TBC marker must not ship in legal copy"),
    ("XXX", "a placeholder marker must not ship in legal copy"),
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

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

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


def check_placeholders(rel, raw_lines):
    """(c2) No unfilled placeholder token survives into a published page."""
    for n, line in enumerate(raw_lines, 1):
        for token, reason in PLACEHOLDER_TOKENS:
            if token in line:
                fail(rel, n, "placeholder %s: %s" % (token, reason))


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


#: The auth forwarder is served at these three paths, and the two deeper ones are
#: the URLs Supabase actually sends. /auth/ alone is reachable but is NOT a route
#: the forwarder forwards: its ALLOWED list names only the two below, so the bare
#: path shows the "open this on your phone" message and goes nowhere.
AUTH_FORWARDER_PATHS = (
    "auth/index.html",
    "auth/reset-password/index.html",
    "auth/callback/index.html",
)

#: Fragments that together ARE the forwarding logic. Each is load-bearing: without
#: the allow-list the page is an open redirector into the app's deep-link surface,
#: without the pathname read it cannot tell the two routes apart, and without the
#: scheme target it forwards nowhere at all.
AUTH_FORWARDER_MARKERS = (
    ("var ALLOWED = ['reset-password', 'callback']", "the route allow-list"),
    ("window.location.pathname", "the pathname read that picks the route"),
    ("'briq://auth/' + path", "the custom-scheme target"),
)


def check_auth_forwarders(root, html_files):
    """(g) The auth forwarder exists at every path Supabase sends, is not the 404
    page, and still carries its forwarding logic.

    THIS EXISTS BECAUSE THE PAGE WAS PUBLISHED AND REACHED BY NOBODY. The
    forwarder was added at /auth/index.html only, while Supabase sends
    /auth/reset-password?code=... and /auth/callback?code=... - neither of which
    was a file, so GitHub Pages served 404.html. Somebody resetting a password on
    a desktop got "Page not found" instead of the instruction this page exists to
    give them, and every check passed throughout: the page WAS live, at a URL
    nothing links to.

    SO THE CHECK IS PER-PATH RATHER THAN PER-PAGE, which is the whole point. The
    site-wide walk sees whatever .html files exist and validates those; it cannot
    notice a file that was never created. This names the three paths and fails on
    absence, which is the only way a missing page is louder than a present one.

    AND IT ASSERTS THE THREE ARE BYTE-IDENTICAL, NOT MERELY PRESENT. They are the
    same page by design - the script reads location.pathname and decides from
    that, so one file serves all three URLs. Copies that drift are the likely
    failure here: a fix applied to one path and not the others would leave a route
    quietly broken, which is the shape of the original defect.
    """
    present = {}
    for rel in AUTH_FORWARDER_PATHS:
        path = os.path.join(root, *rel.split("/"))
        if not os.path.exists(path):
            fail(rel, 1,
                 "MISSING. Supabase sends this URL, so a path that is not a file "
                 "is served 404.html by GitHub Pages. Copy auth/index.html here.")
            continue
        raw = open(path, encoding="utf-8").read()
        present[rel] = raw

        # The 404 page, served or copied here by mistake, is the defect this check
        # was written for; it must never pass as the forwarder.
        if "Page not found" in raw or "does not exist on briq.ie" in raw:
            fail(rel, 1, "serves the 404 page rather than the auth forwarder")
            continue

        for marker, what in AUTH_FORWARDER_MARKERS:
            if marker not in raw:
                line = 1
                for n, l in enumerate(raw.splitlines(), 1):
                    if "<script" in l:
                        line = n
                        break
                fail(rel, line,
                     "has lost %s (%r). Without it this page is not a forwarder."
                     % (what, marker))

    # Byte-identical, with /auth/index.html as the reference.
    reference = present.get("auth/index.html")
    if reference is not None:
        for rel, raw in present.items():
            if rel != "auth/index.html" and raw != reference:
                fail(rel, 1,
                     "differs from auth/index.html. The three paths are one page "
                     "by design and must stay byte-identical, or a fix lands on "
                     "one route and not the others.")


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
            # A page still carrying PUBLISH_DATE is reported by the
            # placeholder check, which says what to do about it; do not
            # report the same line twice under a vaguer heading.
            if "PUBLISH_DATE" not in masthead.group(1):
                fail(rel, 1, "the masthead prints no date; readers and the "
                             "stores need to see when this page was last "
                             "updated")


def check_recipients(root, html_files, expected, source):
    """(f) Section 6 names exactly the app's processors, in the app's order."""
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
    if names == expected:
        return
    missing = [n for n in expected if n not in names]
    extra = [n for n in names if n not in expected]
    parts = []
    if missing:
        parts.append("missing %s" % ", ".join(missing))
    if extra:
        parts.append("unexpected %s" % ", ".join(extra))
    if not parts:
        parts.append("the order differs from the app's: found %s"
                     % ", ".join(names))
    fail(rel, line, "section 6 recipient list must be exactly the app's "
                    "processors, %s (%s): %s"
                    % (source, ", ".join(expected), "; ".join(parts)))


# ---------------------------------------------------------------------------
# Reading the app. Everything below parses values OUT OF THE APP SOURCE. None
# of it is hard-coded, so when the app's legal text changes, this script
# starts failing instead of quietly agreeing with a stale page.
# ---------------------------------------------------------------------------

class AppReadError(Exception):
    """A file the comparison needs is missing or does not parse."""


def read_app_file(app_root, rel):
    path = os.path.join(app_root, *rel.split("/"))
    if not os.path.isfile(path):
        raise AppReadError("%s not found under the app path (looked in %s)"
                           % (rel, path))
    return open(path, encoding="utf-8").read()


def parse_legal_entity(app_root):
    """constants/legalEntity.ts -> the LEGAL_ENTITY literal, as a dict.

    A null stays None, so the caller can tell 'no address published' from
    'an address we forgot to copy across'.
    """
    raw = read_app_file(app_root, "constants/legalEntity.ts")
    body = re.search(
        r"(?s)export\s+const\s+LEGAL_ENTITY\s*:\s*LegalEntity\s*=\s*\{(.*?)\n\};",
        raw,
    )
    if not body:
        raise AppReadError("cannot find the LEGAL_ENTITY object literal in "
                           "constants/legalEntity.ts")
    entity = {}
    for key, value in re.findall(
        r"(\w+)\s*:\s*(null|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")\s*,",
        body.group(1),
    ):
        if value == "null":
            entity[key] = None
        else:
            entity[key] = value[1:-1].replace("\\'", "'").replace('\\"', '"')
    for required in ("name", "contactEmail"):
        if required not in entity:
            raise AppReadError("LEGAL_ENTITY has no %s" % required)
    return entity


def parse_privacy_last_updated(app_root):
    """constants/legalDocuments.ts -> the date the app prints, as '18 November 2026'."""
    raw = read_app_file(app_root, "constants/legalDocuments.ts")
    match = re.search(
        r"export\s+const\s+PRIVACY_LAST_UPDATED\s*(?::[^=]*)?=\s*'(\d{4})-(\d{2})-(\d{2})'",
        raw,
    )
    if not match:
        raise AppReadError("cannot find PRIVACY_LAST_UPDATED in "
                           "constants/legalDocuments.ts")
    year, month, day = (int(g) for g in match.groups())
    if not 1 <= month <= 12:
        raise AppReadError("PRIVACY_LAST_UPDATED has month %d" % month)
    return "%d %s %d" % (day, MONTHS[month - 1], year)


def tsx_bullet_list(raw, heading):
    """The '- ' bullets of the paragraph that follows a heading in the policy.

    The app writes a list as one <Text> whose lines each begin with '- ' and
    end with {'\\n'}. Returns the bullets as plain text.
    """
    at = raw.find(heading)
    if at == -1:
        raise AppReadError("cannot find %r in app/privacy-policy.tsx" % heading)
    block = re.search(r"(?s)<Text style=\{styles\.paragraph\}>(.*?)</Text>",
                      raw[at:])
    if not block:
        raise AppReadError("no paragraph follows %r in "
                           "app/privacy-policy.tsx" % heading)
    bullets = []
    for line in block.group(1).splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        line = line[2:]
        line = re.sub(r"\{'\\n'\}\s*$", "", line).strip()
        line = re.sub(r"\{'\s*'\}", " ", line)
        line = re.sub(r"<[^>]*>", "", line)
        line = html.unescape(line)
        bullets.append(re.sub(r"\s+", " ", line).strip())
    if not bullets:
        raise AppReadError("the paragraph after %r has no '- ' bullets"
                           % heading)
    return bullets


def parse_app_processors(raw):
    """The bold processor names in the app's section 6 recipient list."""
    at = raw.find("This is the complete list of our processors.")
    if at == -1:
        raise AppReadError("cannot find the complete-list sentence that "
                           "introduces the app's processor list")
    names = re.findall(
        r"•\s*<Text style=\{styles\.bold\}>([^<]+)</Text>\s*:", raw[at:]
    )
    if not names:
        raise AppReadError("the app's section 6 lists no processors")
    return [html.unescape(n).strip() for n in names]


def parse_app_emails(raw, entity):
    """Every address the app's privacy policy publishes to readers.

    LEGAL_ENTITY.contactEmail is rendered through the constant rather than
    written out, so it is added from there; any other address is a literal in
    the policy text.
    """
    found = set(EMAIL_RE.findall(raw))
    found.add(entity["contactEmail"])
    return found


#: The keys read_app returns, and therefore the keys a vendored extract must carry.
APP_KEYS = ("entity", "date", "processors", "categories", "purposes", "emails")


def read_vendored_app(path):
    """The same values as read_app, loaded from a vendored JSON extract.

    ⚠️ WHY THIS EXISTS, AND IT IS NOT A CONVENIENCE. The app repository is
    PRIVATE and this one is PUBLIC, so GitHub Actions here cannot check the app
    out: the default GITHUB_TOKEN is scoped to this repository, and a token that
    could reach the app would be a cross-repository credential sitting in a
    public repo's CI. Without this, `--app` works on a developer's machine and
    silently never runs in CI, which is the "tool nobody invokes" failure the
    comparison was written to replace.
    *
    * So CI compares against a committed extract of the app's legal text instead.
    * The extract is GENERATED BY read_app ITSELF (tools/dump-app-legal.py in the
    * app repository calls this module's read_app and writes the result), so the
    * producer and the consumer cannot disagree about shape or parsing.
    *
    * ⚠️ WHAT IS VENDORED IS THE LEGAL TEXT, NOT THE APP SOURCE, AND THE
    * DIFFERENCE MATTERS BECAUSE THIS REPOSITORY IS PUBLIC. Every value in the
    * extract is already published on these pages. The app's .ts and .tsx files
    * are NOT copied here: they carry internal engineering commentary that has no
    * business in a public repo.
    *
    * ⚠️ THE DRIFT THIS FILE CANNOT DETECT IS APP-TO-EXTRACT DRIFT, and that
    * check lives in the app repository, which is the only side that can see
    * both. Nothing here can tell a faithful extract from a stale one.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            app = json.load(fh)
    except (OSError, ValueError) as error:
        raise AppReadError("cannot read the vendored extract %s: %s"
                           % (path, error))
    missing = [k for k in APP_KEYS if k not in app]
    if missing:
        raise AppReadError("the vendored extract %s is missing %s"
                           % (path, ", ".join(missing)))
    if not isinstance(app.get("entity"), dict) or "name" not in app["entity"]:
        raise AppReadError("the vendored extract %s has no entity.name" % path)
    for key in ("processors", "categories", "purposes", "emails"):
        if not isinstance(app[key], list) or not app[key]:
            raise AppReadError("the vendored extract %s has an empty or "
                               "non-list %s" % (path, key))
    # read_app hands back a set; the JSON carries a sorted list.
    app["emails"] = set(app["emails"])
    return app


def read_app(app_root):
    """Everything the comparison needs, parsed out of the app."""
    policy = read_app_file(app_root, "app/privacy-policy.tsx")
    entity = parse_legal_entity(app_root)
    return {
        "entity": entity,
        "date": parse_privacy_last_updated(app_root),
        "processors": parse_app_processors(policy),
        "categories": tsx_bullet_list(policy, "3.1 Information You Give Us"),
        "purposes": tsx_bullet_list(policy, "5. How We Use Your Information"),
        "emails": parse_app_emails(policy, entity),
    }


def site_list_after(raw, needle, what, rel):
    """The <li> texts of the first <ul> after a marker, as plain text."""
    at = raw.find(needle)
    if at == -1:
        fail(rel, 1, "cannot find %s on this page (looked for %r)"
             % (what, needle))
        return None, 1
    block = re.search(r"(?is)<ul>(.*?)</ul>", raw[at:])
    if not block:
        fail(rel, raw[:at].count("\n") + 1, "no list follows %s" % what)
        return None, raw[:at].count("\n") + 1
    line = raw[:at + block.start()].count("\n") + 1
    items = []
    for item in re.findall(r"(?is)<li>(.*?)</li>", block.group(1)):
        text = html.unescape(re.sub(r"<[^>]*>", "", item))
        items.append(re.sub(r"\s+", " ", text).strip())
    return items, line


def compare_lists(rel, line, label, site_items, app_items, where):
    """Report a site list that does not match the app's, item by item."""
    if site_items is None or site_items == app_items:
        return
    missing = [i for i in app_items if i not in site_items]
    extra = [i for i in site_items if i not in app_items]
    if not missing and not extra:
        fail(rel, line, "%s has the app's items in a different order: the app "
                        "(%s) has %s" % (label, where, " | ".join(app_items)))
        return
    for item in missing:
        fail(rel, line, "%s is missing an item the app states at %s: %r"
             % (label, where, item))
    for item in extra:
        fail(rel, line, "%s states an item the app does not: %r. The app's "
                        "legal text is the source of truth, so either the app "
                        "gained it and this page is behind, or this page "
                        "claims something the app does not do." % (label, item))


def check_against_app(root, html_files, app):
    """(g) The site says what the app says: controller, address, emails, date,
    processors, data categories and purposes.

    Every expected value here was parsed out of the app source by read_app.
    """
    privacy = os.path.join(root, "privacy", "index.html")
    entity = app["entity"]

    # --- the controller's name and address, on every page that names one ---
    # The app omits the address line entirely while postalAddress is null, and
    # constants/legalEntity.ts forbids a placeholder or a bare country standing
    # in for an address we have not committed to. The site may not do what the
    # app is forbidden to do.
    name = entity["name"]
    stand_in = re.compile(
        re.escape(name) + r"\s*,\s*([A-Z][A-Za-z.\-]*(?:\s+[A-Z][A-Za-z.\-]*)*)"
    )
    # A line that states who the controller is must state the app's
    # controller. Without this, renaming the controller in the app (which is
    # what happens the day Bríq Limited is incorporated) leaves every page
    # naming a person who is no longer responsible, and nothing notices.
    states_controller = re.compile(
        r"data controller|<strong>Developer:</strong>", re.IGNORECASE
    )
    for path in html_files:
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        raw = open(path, encoding="utf-8").read()
        for n, line in enumerate(mask_comments(raw).splitlines(), 1):
            rendered = strip_tags_per_line(line)
            # A section heading ("2. Data Controller") announces the subject
            # rather than stating who it is, so it is not a claim to check.
            if re.search(r"<h[1-6]\b", line, re.IGNORECASE):
                continue
            if states_controller.search(line) and name not in rendered:
                fail(rel, n, "this line states who the controller is but does "
                             "not name %r, which is LEGAL_ENTITY.name in "
                             "constants/legalEntity.ts: %r"
                     % (name, re.sub(r"\s+", " ", rendered).strip()[:120]))
            match = stand_in.search(rendered)
            if match:
                if entity.get("postalAddress"):
                    continue
                fail(rel, n,
                     "the controller is written %r, but LEGAL_ENTITY."
                     "postalAddress is null: constants/legalEntity.ts says a "
                     "legal surface must never render a placeholder or a bare "
                     "country standing in for an address we have not "
                     "committed to. Write just %r."
                     % (match.group(0), name))
        if entity.get("postalAddress") and name in raw:
            if entity["postalAddress"] not in raw:
                fail(rel, 1, "this page names the controller but not the "
                             "published address %r from LEGAL_ENTITY."
                             "postalAddress" % entity["postalAddress"])
        if entity.get("registrationNumber") and name in raw:
            if entity["registrationNumber"] not in raw:
                fail(rel, 1, "this page names the controller but not the "
                             "registration number %r from LEGAL_ENTITY."
                             "registrationNumber"
                     % entity["registrationNumber"])

    # --- contact addresses ---
    # An address the site publishes that the app does not is an address nobody
    # is answering; one the app publishes that the privacy page omits leaves a
    # reader without the contact point Art. 13(1)(b) requires.
    for path in html_files:
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        raw = mask_comments(open(path, encoding="utf-8").read())
        for n, line in enumerate(raw.splitlines(), 1):
            for address in sorted(set(EMAIL_RE.findall(line))):
                if address not in app["emails"]:
                    fail(rel, n, "contact address %s does not appear anywhere "
                                 "in the app's privacy policy; the app "
                                 "publishes %s"
                         % (address, ", ".join(sorted(app["emails"]))))
    if privacy in html_files:
        rel = os.path.relpath(privacy, root).replace(os.sep, "/")
        raw = open(privacy, encoding="utf-8").read()
        for address in sorted(app["emails"]):
            if address not in raw:
                fail(rel, 1, "the app's privacy policy publishes %s and this "
                             "page does not" % address)

    if privacy not in html_files:
        return
    rel = os.path.relpath(privacy, root).replace(os.sep, "/")
    raw = open(privacy, encoding="utf-8").read()

    # --- the privacy date, against PRIVACY_LAST_UPDATED ---
    shown, _ = printed_date(raw)
    line = 1
    for n, l in enumerate(raw.splitlines(), 1):
        if "class=\"updated\"" in l:
            line = n
            break
    if shown is not None and shown != app["date"]:
        fail(rel, line, "printed date %r does not match the app: "
                        "PRIVACY_LAST_UPDATED in constants/legalDocuments.ts "
                        "is %r, which is what app/privacy-policy.tsx prints"
             % (shown, app["date"]))
    meta = re.search(
        r"""(?is)<meta\s+name=["']description["']\s+content=["']([^"']*)["']""",
        raw,
    )
    if meta:
        found = DATE_RE.search(html.unescape(meta.group(1)))
        if found and found.group(1) != app["date"]:
            fail(rel, 7, "the meta description date %r does not match the "
                         "app's PRIVACY_LAST_UPDATED %r, so a search result "
                         "shows the wrong date"
                 % (found.group(1), app["date"]))

    # --- the data categories of section 3.1 ---
    items, at = site_list_after(
        raw, "3.1 Information You Give Us", "the section 3.1 list", rel
    )
    compare_lists(rel, at, "the section 3.1 data category list", items,
                  app["categories"], "app/privacy-policy.tsx 3.1")

    # --- the purposes of section 5 ---
    items, at = site_list_after(
        raw, "5. How We Use Your Information", "the section 5 list", rel
    )
    compare_lists(rel, at, "the section 5 purpose list", items,
                  app["purposes"], "app/privacy-policy.tsx 5")


def main(argv):
    # Failure messages contain Bríq, so do not let a legacy console codepage
    # turn a real failure into a UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = list(argv[1:])
    app_path = None
    positional = []
    while args:
        arg = args.pop(0)
        if arg == "--app":
            if not args:
                print("check-site: --app needs a path", file=sys.stderr)
                return 2
            app_path = args.pop(0)
        elif arg.startswith("--app="):
            app_path = arg.split("=", 1)[1]
        elif arg in ("-h", "--help"):
            print(__doc__)
            return 0
        elif arg.startswith("-"):
            print("check-site: unknown option %s" % arg, file=sys.stderr)
            return 2
        else:
            positional.append(arg)
    if len(positional) > 1:
        print("check-site: expected at most one folder, got %d"
              % len(positional), file=sys.stderr)
        return 2

    root = os.path.abspath(
        positional[0] if positional
        else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    if not os.path.isdir(root):
        print("check-site: %s is not a directory" % root, file=sys.stderr)
        return 2

    app = None
    if app_path is not None:
        app_root = os.path.abspath(app_path)
        # --app takes EITHER an app checkout OR a vendored JSON extract of its
        # legal text. CI here passes the extract, because the app repository is
        # private and this one is public; a developer passes the checkout. Both
        # produce the same dict, so every check below is identical either way.
        is_vendored = os.path.isfile(app_root) and app_root.lower().endswith(".json")
        if not is_vendored and not os.path.isdir(app_root):
            print("check-site: app path %s is neither a directory nor a .json "
                  "extract" % app_root, file=sys.stderr)
            return 2
        try:
            app = read_vendored_app(app_root) if is_vendored else read_app(app_root)
        except AppReadError as error:
            # A comparison that cannot read its source of truth must not pass
            # silently: that is how the two surfaces drifted in the first place.
            print("check-site: cannot read the app's legal text: %s" % error,
                  file=sys.stderr)
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
        check_placeholders(rel, lines)
        check_structure(rel, path, raw, lines, html_files, root)

    check_dates(root, html_files)
    check_auth_forwarders(root, html_files)
    if app is None:
        check_recipients(root, html_files, EXPECTED_RECIPIENTS,
                         "from this script's fallback list")
        print("check-site: no --app given, so the app comparison was SKIPPED "
              "(controller, address, emails, privacy date, data categories "
              "and purposes). Pass --app <path to the app checkout> to check "
              "them.", file=sys.stderr)
    else:
        check_recipients(root, html_files, app["processors"],
                         "read from the app's legal text")
        check_against_app(root, html_files, app)

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
