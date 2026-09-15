"""Render the REAL docs through the real renderer and check the result.

The renderer lives in app/app.js and only ever has to handle two files, so those two files are the
test corpus. A synthetic markdown fixture would test a document that does not exist -- and the whole
risk here is the opposite: a construct that IS in help.md and that the renderer mishandles.

Needs node on PATH. Skips (does not fail) without it, because node is not a runtime dependency of
the app -- it is only how this test reaches the browser's own JS.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from html import unescape

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(BASE, 'app', 'app.js')
DOCS = {'help': os.path.join(BASE, 'docs', 'help.md')}
# The id prefixes are NOT repeated here — the node script below looks them up in the real
# HELP_SOURCES. A copy of them in this file drifted from the code within an hour of being written,
# and the test then passed the old arrangement while the app shipped the new one.

# app.js is one browser script -- no modules, and it touches the DOM at load. So lift just the
# renderer out of it by name and run that. Brittle by design: if a function is renamed, this fails
# loudly rather than quietly testing nothing.
WANT = ['esc', 'HELP_SOURCES', 'mdSlug', 'mdInline', 'mdToHtml']


def extract(src, name):
    """The text of a top-level `function <name>(…) {…}` or `const <name> = {…}/[…]`.

    Matched by delimiter depth from whichever of `{` or `[` opens the body, so it handles both the
    functions and the HELP_SOURCES array they close over.
    """
    m = re.search(r'^(?:function %s\s*\(|const %s\s*=)' % (name, name), src, re.M)
    if not m:
        raise AssertionError('%s not found in app/app.js' % name)
    cands = [src.index(c, m.start()) for c in '{[' if c in src[m.start():m.start() + 400]]
    i = min(cands)
    open_c, close_c = src[i], {'{': '}', '[': ']'}[src[i]]
    depth, j = 0, i
    while j < len(src):
        if src[j] == open_c:
            depth += 1
        elif src[j] == close_c:
            depth -= 1
            if depth == 0:
                break
        j += 1
    end = src.index('\n', j)
    return src[m.start():end]


class HelpRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('node'):
            raise unittest.SkipTest('node not on PATH')
        with open(APP_JS, encoding='utf-8') as f:
            src = f.read()
        js = '\n'.join(extract(src, n) for n in WANT)
        cls.md = {}
        for key, path in DOCS.items():
            with open(path, encoding='utf-8') as f:
                cls.md[key] = f.read()
        out = {}
        for key, md in cls.md.items():
            # Markdown goes in on stdin, not argv: a doc can be large and Windows caps a command
            # line at 8191 characters, so an argv version would work on the guide and mysteriously
            # fail on the file that actually matters.
            script = (js + '\nlet md = "";\n'
                      + 'const src = HELP_SOURCES.find(s => s.key === %s);\n' % json.dumps(key)
                      + 'if (!src) throw new Error("no HELP_SOURCES entry for %s");\n' % key
                      + 'process.stdin.setEncoding("utf8");\n'
                      + 'process.stdin.on("data", d => md += d);\n'
                      + 'process.stdin.on("end", () => process.stdout.write(mdToHtml(md, src.prefix)));')
            r = subprocess.run([shutil.which('node'), '-e', script],
                               input=md.encode('utf-8'), capture_output=True, timeout=120)
            if r.returncode != 0:
                raise AssertionError('node failed on %s: %s' % (key, r.stderr.decode('utf-8', 'replace')))
            out[key] = r.stdout.decode('utf-8')
        cls.html = out

    def test_no_script_injection(self):
        """Nothing in the docs may become an executable tag or a handler attribute."""
        for key, html in self.html.items():
            self.assertNotIn('<script', html.lower(), key)
            self.assertNotIn('javascript:', html.lower(), key)
            self.assertIsNone(re.search(r'\son\w+\s*=', html), key)

    def test_no_leftover_sentinels(self):
        """The code-span placeholder must never survive into the output."""
        for key, html in self.html.items():
            self.assertNotIn('', html, key)
            self.assertNotIn('\x00', html, key)

    def test_tags_balance(self):
        for key, html in self.html.items():
            for tag in ('p', 'li', 'ul', 'ol', 'table', 'tr', 'td', 'th', 'code', 'pre',
                        'strong', 'em', 'blockquote', 'a', 'h1', 'h2', 'h3'):
                op = len(re.findall(r'<%s[\s>]' % tag, html))
                cl = len(re.findall(r'</%s>' % tag, html))
                self.assertEqual(op, cl, '%s: <%s> %d open vs %d close' % (key, tag, op, cl))

    def test_every_anchor_link_resolves(self):
        """The docs' own cross-links are the reason mdSlug copies GitHub. Prove they land."""
        ids = {'doc-guide', 'doc-reference'}   # the two <section> wrappers loadHelp() adds
        for html in self.html.values():
            ids.update(re.findall(r'<h[1-6] id="([^"]+)"', html))
        missing = []
        for key, html in self.html.items():
            for target in re.findall(r'data-jump="([^"]+)"', html):
                if target not in ids:
                    missing.append('%s -> #%s' % (key, target))
        self.assertEqual(missing, [], 'anchor links with no matching heading: %s' % missing)

    def test_heading_ids_do_not_collide_with_the_app(self):
        """A heading id that is already an element of the app breaks the jump SILENTLY.

        `## Settings` slugs to `settings`, and #settings is the Settings dialog. Rendered without a
        prefix, the contents list highlighted the right entry and scrolled nowhere, because
        getElementById found the dialog first. `## Sidebar` and `## Grid` collide the same way.
        """
        with open(os.path.join(BASE, 'app', 'index.html'), encoding='utf-8') as f:
            page_ids = set(re.findall(r'\bid="([^"]+)"', f.read()))
        clashes = []
        for key, html in self.html.items():
            for hid in re.findall(r'<h[1-6] id="([^"]+)"', html):
                if hid in page_ids:
                    clashes.append('%s: #%s' % (key, hid))
        self.assertEqual(clashes, [], 'heading ids that collide with app elements: %s' % clashes)

    def test_heading_ids_are_unique(self):
        """Two headings sharing an id means one of them is unreachable from the contents list."""
        seen, dupes = set(), []
        for html in self.html.values():
            for hid in re.findall(r'<h[1-6] id="([^"]+)"', html):
                (dupes.append(hid) if hid in seen else seen.add(hid))
        self.assertEqual(dupes, [], 'duplicate heading ids: %s' % dupes)

    def test_structure_survives(self):
        """Counts from the source, so a renderer that silently drops blocks is caught."""
        for key, html in self.html.items():
            md = self.md[key]
            heads = len(re.findall(r'^#{1,6} ', md, re.M))
            self.assertEqual(len(re.findall(r'<h[1-6][\s>]', html)), heads, key + ' headings')
            fences = len(re.findall(r'^```', md, re.M)) // 2
            self.assertEqual(len(re.findall(r'<pre>', html)), fences, key + ' code blocks')
            # Every table in the source is a separator row; each becomes exactly one <table>.
            seps = len(re.findall(r'^\s*\|[\s:|-]+\|\s*$', md, re.M))
            self.assertEqual(len(re.findall(r'<table>', html)), seps, key + ' tables')

    def test_images_and_raw_html_are_not_executable(self):
        """The docs carry two images and one raw HTML tag; neither may vanish or run."""
        combined = ''.join(self.html.values())
        imgs = sum(len(re.findall(r'!\[', md)) for md in self.md.values())
        self.assertEqual(len(re.findall(r'<img ', combined)), imgs)

    def test_no_unconsumed_lines(self):
        """A non-blank source line that produced no output means a branch swallowed content."""
        # Both sides get the SAME normalisation, or the test measures its own cleanup rather than
        # the renderer: an earlier version stripped brackets from the source only, and flagged a
        # perfectly rendered line whose parentheses had survived into the output.
        def norm(s):
            s = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', s)   # links keep their label
            s = re.sub(r'[`*_\[\]()<>#|]', '', s)
            return re.sub(r'\s+', ' ', s).strip()

        for key, html in self.html.items():
            # Tags come out to nothing, not to a space: an inline <a> sits tight against the
            # punctuation after it, and a space there turned "the reference;" into "the reference ;"
            # and failed a correctly rendered line. Blocks stay separated by the \n mdToHtml joins on.
            # Entities decoded AFTER the tags come out, or &lt; would turn into a < the tag
            # stripper has already run past. esc() escapes " as &quot;, so the docs' quoted
            # phrases need this to compare against their source at all.
            text = norm(unescape(re.sub(r'<[^>]+>', '', html)))
            for line in self.md[key].splitlines():
                s = line.strip()
                if len(s) < 40 or s.startswith(('|', '#', '>', '`')):
                    continue
                # A link wrapped across two source lines leaves each half unbalanced, and neither
                # half exists in the output as written — the renderer joins the paragraph before
                # resolving links, which is correct. Skip the halves rather than the paragraph.
                if s.count('[') != s.count(']'):
                    continue
                # List markers are consumed by <ul>/<ol>, so drop the marker and check the item's
                # text — skipping list lines entirely would leave a quarter of the guide untested.
                probe = norm(re.sub(r'^(?:[-*]|\d+\.)\s+', '', s))[:36].strip()
                if probe and probe not in text:
                    self.fail('%s: source line missing from output: %r' % (key, s[:70]))


if __name__ == '__main__':
    unittest.main(verbosity=2)
