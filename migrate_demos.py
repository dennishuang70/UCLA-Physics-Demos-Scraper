#!/usr/bin/env python3
"""
migrate_demos.py

Crawls the UCLA Lecture Demonstration Manual and, for every page, saves:
  - the page text (as .html and .txt)
  - the media it uses: JW Player videos, Flash (.swf) embeds, GIFs and other images
and writes two spreadsheets (CSV) listing everything it found, including problems.

It only READS from the old site. It never changes anything there.

Setup (once):
    pip install requests beautifulsoup4

Trial run (first 5 pages only, the default):
    python migrate_demos.py

Full run (all pages):
    python migrate_demos.py --limit 0

See --help for the other options.
"""

import argparse
import csv
import html as htmllib
import os
import re
import shutil
import sys
import time
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HOST = "demoweb.physics.ucla.edu"
# The top of the manual. Its Table of Contents lists every top-level section (1 to 9 and
# External Resources), so the crawl starts here and works down in the manual's own order.
START_URL = "https://demoweb.physics.ucla.edu/content/1-mechanics"
USER_AGENT = "Mozilla/5.0 (compatible; demo-site-migration-script)"

VIDEO_EXT = (".mp4", ".f4v", ".m4v", ".flv", ".webm", ".mov", ".m3u8")
AUDIO_EXT = (".mp3", ".m4a", ".ogg", ".wav")
PLAYLIST_EXT = (".xml", ".rss")
FILE_EXT = VIDEO_EXT + AUDIO_EXT + (".swf", ".gif", ".png", ".jpg", ".jpeg", ".pdf")
BROWSER_PLAYABLE = (".mp4", ".f4v", ".m4v", ".webm")  # .f4v gets a renamed .mp4 copy

# Where the page's main text lives. The script tries these in order and uses the first
# one that has text. If the saved text includes menus or misses the body, edit this list.
CONTENT_SELECTORS = [
    "div.field-name-body",
    "div.node .content",
    "article",
    "div#content",
    "div#main",
    "main",
]
# Page furniture to strip out of the saved text.
REMOVE_SELECTORS = [
    ".book-navigation", ".breadcrumb", ".print-link", "script", "style", "noscript",
]

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def normalize(url):
    """Drop #fragments, ?query strings and trailing slashes so each page is visited once."""
    url, _ = urldefrag(url)
    return urlparse(url)._replace(query="", fragment="").geturl().rstrip("/")


def ext_of(url):
    return os.path.splitext(urlparse(url).path)[1].lower()


def is_page_link(url, prefix):
    p = urlparse(url)
    return (
        p.netloc == HOST
        and p.path.startswith(prefix)
        and not p.path.lower().endswith(FILE_EXT)
        and not p.path.lower().startswith(("/print", "/book/export"))
    )


def safe_part(part):
    return re.sub(r'[<>:"|?*\\]', "_", part)


def local_path_for(url, media_dir):
    """Keep the original folder layout, e.g. media/demoweb.../sites/default/files/x.f4v"""
    p = urlparse(url)
    parts = [safe_part(x) for x in unquote(p.path).split("/") if x] or ["index"]
    return os.path.join(media_dir, safe_part(p.netloc), *parts)


def slug_for(url):
    seg = [x for x in urlparse(url).path.split("/") if x]
    return safe_part(seg[-1]) if seg else "index"


def parse_flashvars(fv):
    return {k: v[0] for k, v in parse_qs(fv or "").items() if v}


def get_title(soup):
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    if soup.title and soup.title.string:
        return soup.title.string.split("|")[0].strip()
    return "Untitled"


MATCHED = {"selector": "(whole page)"}  # remembers which container was used, for the log
USER_SELECTOR = {"value": ""}            # set by --selector
# The standard Drupal body field. On this site every page's text and images sit inside it,
# while the title, menus and previous/next links sit outside it.
PREFERRED_SELECTORS = ["div.field-name-body"]
# Words that mark menus and page furniture. A box whose class or id starts with one of
# these (or sits inside such a box) is never chosen as the page text.
NOISE_PREFIXES = ("nav", "menu", "sidebar", "breadcrumb", "footer", "header", "tabs",
                  "pager", "search", "skip", "toc", "book")


def _visible_text(el):
    parts = (s.strip() for s in el.find_all(string=True)
             if s.parent is not None and s.parent.name not in ("script", "style", "noscript"))
    return " ".join(p for p in parts if p)


def _is_noise(el):
    ident = " ".join(el.get("class", [])) + " " + (el.get("id") or "")
    tokens = re.split(r"[^a-z0-9]+", ident.lower())
    return any(t.startswith(NOISE_PREFIXES) for t in tokens if t)


def auto_content(soup):
    """Find the box that holds the main text without knowing its name.

    Every box is scored by how much readable text it holds, minus extra for text that sits
    inside links (menus are mostly links). Among the best-scoring boxes, the smallest one wins,
    so we get the text itself and not the whole page around it."""
    root = soup.body or soup
    cands = []
    for el in root.find_all(["div", "article", "section", "main", "td"]):
        if _is_noise(el) or any(_is_noise(p) for p in el.parents if getattr(p, "name", None)):
            continue
        text = _visible_text(el)
        if len(text) < 30:
            continue
        link_text = sum(len(_visible_text(a)) for a in el.find_all("a"))
        cands.append((len(text) - 2 * link_text, sum(1 for _ in el.descendants), el))
    if not cands:
        return None
    top = max(c[0] for c in cands)
    if top <= 0:
        return None
    good = sorted((c for c in cands if c[0] >= 0.9 * top), key=lambda c: c[1])
    return good[0][2]


def find_content(soup):
    # 1. a selector you gave with --selector
    if USER_SELECTOR["value"]:
        el = soup.select_one(USER_SELECTOR["value"])
        if el is not None and el.get_text(strip=True):
            MATCHED["selector"] = USER_SELECTOR["value"]
            return el
    # 2. the standard Drupal body field
    for sel in PREFERRED_SELECTORS:
        el = soup.select_one(sel)
        if el is not None and (el.get_text(strip=True)
                               or el.find(["img", "embed", "object", "video", "iframe"])):
            MATCHED["selector"] = sel
            return el
    # 3. automatic detection
    el = auto_content(soup)
    if el is not None:
        MATCHED["selector"] = "auto: <%s class=%r id=%r>" % (
            el.name, " ".join(el.get("class", [])), el.get("id") or "")
        return el
    # 3. the built-in guesses
    for sel in CONTENT_SELECTORS:
        el = soup.select_one(sel)
        if el is not None and el.get_text(strip=True):
            MATCHED["selector"] = sel
            return el
    MATCHED["selector"] = "(whole page)"
    return soup.body or soup


# ----------------------------------------------------------------------------
# Finding the media on a page
# ----------------------------------------------------------------------------

def flash_info(el):
    """Return (swf url, flashvars string, width, height) for an <embed> or <object>."""
    swf = el.get("src") or el.get("data")
    fv = el.get("flashvars")
    for p in el.find_all("param"):
        name = (p.get("name") or "").lower()
        if name in ("movie", "src") and not swf:
            swf = p.get("value")
        if name == "flashvars" and not fv:
            fv = p.get("value")
    child = el.find("embed")
    if child is not None:
        swf = swf or child.get("src")
        fv = fv or child.get("flashvars")
    return swf, fv, el.get("width"), el.get("height")


SWAP_RE = re.compile(r"""swap\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]\s*\)""")


def image_urls_by_name(content, page_url):
    """{name attribute: full url} for images that have a name, like <img name="bednails" ...>."""
    return {i["name"]: urljoin(page_url, i["src"])
            for i in content.find_all("img", attrs={"name": True}) if i.get("src")}


def hover_targets(content, page_url):
    """(image name, file, url of the still image) for each swap('name','file') call in the
    onmouseover / onmouseout handlers. The file lives in the same folder as the still image."""
    by_name = image_urls_by_name(content, page_url)
    found = []
    for el in content.find_all(True):
        for attr in ("onmouseover", "onmouseout"):
            for name, file in SWAP_RE.findall(el.get(attr) or ""):
                if name in by_name:
                    found.append((name, file, by_name[name]))
    return found


def collect_media(soup, content, page_url):
    """Return (items, flags). Each item is a dict describing one file the page uses."""
    items = []
    flags = []

    def add(kind, u, note="", el=None, w=None, h=None):
        if not u or u.strip().startswith(("data:", "javascript:", "mailto:", "#")):
            return
        items.append(dict(kind=kind, url=urljoin(page_url, u.strip()), note=note,
                          el=el, w=w, h=h))

    # 1. Flash embeds, including JW Player (player.swf + flashvars file=...)
    for el in content.find_all(["embed", "object"]):
        if el.name == "embed" and el.find_parent("object") is not None:
            continue  # already handled through its parent <object>
        swf, fv, w, h = flash_info(el)
        if not swf or ".swf" not in swf.lower():
            continue
        fvars = parse_flashvars(fv)
        target = (fvars.get("file") or fvars.get("src") or fvars.get("video")
                  or fvars.get("playlistfile") or fvars.get("playlist"))
        player_name = os.path.basename(urlparse(swf).path).lower()
        if target and ("player" in player_name or "file" in fvars):
            kind = "jwplayer-playlist" if ext_of(target) in PLAYLIST_EXT else "jwplayer-video"
            add(kind, target, "from Flash player embed (%s)" % player_name, el, w, h)
        else:
            add("flash-simulation", swf, "Flash content, needs review. flashvars: %s" % (fv or "none"),
                None, w, h)
            flags.append("Flash simulation (.swf)")
            for v in fvars.values():  # files the SWF is told to load
                if ext_of(v) in FILE_EXT + PLAYLIST_EXT:
                    add("flash-companion", v, "file named in flashvars")

    # 2. JW Player set up in JavaScript (newer pages)
    for s in soup.find_all("script"):
        text = s.string or s.get_text() or ""
        if "jwplayer" in text.lower():
            for m in re.finditer(
                    r"""["']([^"'\s]+\.(?:mp4|f4v|m4v|flv|webm|m3u8|mp3|m4a|xml))["']""", text, re.I):
                u = m.group(1)
                add("jwplayer-playlist" if ext_of(u) in PLAYLIST_EXT else "jwplayer-video",
                    u, "found in JW Player JavaScript")

    # 3. Plain HTML5 video/audio
    for el in content.find_all(["video", "audio", "source"]):
        if el.get("src"):
            add("html5-media", el["src"])

    # 4. Images and GIFs
    image_urls = []
    for img in content.find_all("img"):
        src = img.get("src")
        if src:
            add("gif" if ext_of(src) == ".gif" else "image", src)
            image_urls.append(urljoin(page_url, src))

    # 4b. Hover images: swap('name','file.gif') replaces a still picture with another file
    shown = set(image_urls)
    for name, file, still_url in hover_targets(content, page_url):
        target = urljoin(still_url, file)
        if target not in shown:
            shown.add(target)
            add("hover-image", target, "shown on mouse-over of image '%s' (from swap() call)" % name)

    # 5. Links to files (PDFs, extra videos, etc.) on the same site
    for a in content.find_all("a", href=True):
        full = urljoin(page_url, a["href"])
        if urlparse(full).netloc == HOST and ext_of(full) in FILE_EXT:
            add("linked-file", a["href"])

    # 6. Images named inside inline scripts (typical of frame-by-frame viewers)
    scripted = False
    for s in content.find_all("script"):
        text = s.string or ""
        for m in re.finditer(r"""["']([^"']+\.(?:png|jpe?g|gif))["']""", text, re.I):
            add("script-image", m.group(1), "image named in page script")
            image_urls.append(urljoin(page_url, m.group(1)))
            scripted = True

    # Frame-sequence check: many images with numbered names, or images loaded by script
    stems = {}
    for u in image_urls:
        path = urlparse(u).path
        m = re.match(r"(.*?)\d+\.(?:png|jpe?g|gif)$", path, re.I)
        if m:
            stems[m.group(1)] = stems.get(m.group(1), 0) + 1
    if any(c >= 5 for c in stems.values()) or scripted:
        flags.append("Possible frame-sequence animation or scripted viewer")

    # Remove duplicates, keeping the first (it may carry the element to replace)
    seen, unique = set(), []
    for it in items:
        key = (it["kind"], it["url"])
        if key not in seen:
            seen.add(key)
            unique.append(it)
    return unique, flags


# ----------------------------------------------------------------------------
# Downloading
# ----------------------------------------------------------------------------

def looks_real(path, ext):
    """Catch 'soft 404' pages that return HTML instead of the real file."""
    if os.path.getsize(path) == 0:
        return False
    with open(path, "rb") as f:
        head = f.read(512)
    if ext == ".swf":
        return head[:3] in (b"FWS", b"CWS", b"ZWS")
    low = head.lstrip().lower()
    return not (low.startswith(b"<!doctype") or low.startswith(b"<html"))


def download(url, dest, wayback_ts, delay):
    """Try the live site, then the Wayback Machine. Returns (status, source_url)."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return "already-downloaded", url
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    ext = ext_of(url)
    attempts = [("live", url),
                ("wayback", "https://web.archive.org/web/%sid_/%s" % (wayback_ts, url))]
    for label, u in attempts:
        try:
            r = session.get(u, timeout=60, stream=True)
            if r.status_code == 200:
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
                if looks_real(dest, ext):
                    time.sleep(delay)
                    return label, u
                os.remove(dest)
        except requests.RequestException:
            pass
        time.sleep(delay)
    return "FAILED", ""


# ----------------------------------------------------------------------------
# Processing one page
# ----------------------------------------------------------------------------

def process_page(url, html_text, args, pages_dir, media_dir):
    soup = BeautifulSoup(html_text, "html.parser")
    title = get_title(soup)
    slug = slug_for(url)
    content = find_content(soup)
    items, flags = collect_media(soup, content, url)
    still_by_name = image_urls_by_name(content, url)  # before any src is rewritten to a local path

    def rel(path):
        return os.path.relpath(path, pages_dir).replace(os.sep, "/")

    media_rows, local_map = [], {}
    queue = list(items)
    while queue:
        it = queue.pop(0)
        kind, murl = it["kind"], it["url"]
        row = dict(page_url=url, kind=kind, source_url=murl, local_file="", status="", note=it["note"])

        if urlparse(murl).netloc != HOST:
            row["status"] = "external (not downloaded)"
            media_rows.append(row)
            continue

        dest = local_path_for(murl, media_dir)
        status, used = download(murl, dest, args.wayback_ts, args.delay)
        row["status"] = status
        if status == "FAILED":
            flags.append("Missing file: " + os.path.basename(murl))
            media_rows.append(row)
            continue
        row["local_file"] = dest
        local_map[murl] = dest
        if status == "wayback":
            row["note"] = (row["note"] + " | recovered from Wayback Machine").strip(" |")

        ext = ext_of(murl)

        # Playlists name the real videos; fetch those too
        if kind == "jwplayer-playlist":
            with open(dest, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            found = set(re.findall(r"""[^"'<>\s=]+\.(?:mp4|f4v|m4v|flv|webm|mp3|m4a)""", text, re.I))
            for u in sorted(found):
                queue.append(dict(kind="jwplayer-video", url=urljoin(murl, u),
                                  note="from playlist " + os.path.basename(murl),
                                  el=None, w=None, h=None))
            if not found:
                flags.append("Playlist with no video found: " + os.path.basename(murl))

        # F4V is MP4-style: make a renamed .mp4 copy. Test that it plays.
        playable = dest
        if ext == ".f4v":
            playable = os.path.splitext(dest)[0] + ".mp4"
            if not os.path.exists(playable):
                shutil.copyfile(dest, playable)
            row["note"] = (row["note"] + " | .mp4 copy made by renaming, test playback").strip(" |")
        if ext == ".flv":
            flags.append("FLV video needs conversion to MP4: " + os.path.basename(murl))

        # Swap the old Flash player for an HTML5 <video> tag in the saved page
        if it.get("el") is not None and kind == "jwplayer-video" and ext in BROWSER_PLAYABLE:
            w, h = it.get("w"), it.get("h")
            size = ' width="%s" height="%s"' % (w, h) if w and h else ""
            new = BeautifulSoup('<video controls%s src="%s"></video>' % (size, rel(playable)),
                                "html.parser")
            it["el"].replace_with(new)
            local_map[murl] = playable

        media_rows.append(row)

    # Point images and links at the local copies
    for tag in content.find_all(["img", "source", "video", "audio"]):
        if tag.get("src"):
            full = urljoin(url, tag["src"])
            if full in local_map:
                tag["src"] = rel(local_map[full])
    for tag in content.find_all("a"):
        if tag.get("href"):
            full = urljoin(url, tag["href"])
            if full in local_map:
                tag["href"] = rel(local_map[full])

    # Replace swap('name','file') hover handlers with direct swaps to the local copies, so the
    # hover effect keeps working without the old image-hover.js script
    def make_repl():
        def repl(m):
            name, file = m.group(1), m.group(2)
            full = urljoin(still_by_name[name], file) if name in still_by_name else None
            local = local_map.get(full)
            return "document.images['%s'].src='%s'" % (name, rel(local)) if local else m.group(0)
        return repl

    for el in content.find_all(True):
        for attr in ("onmouseover", "onmouseout"):
            val = el.get(attr)
            if val and "swap(" in val:
                el[attr] = SWAP_RE.sub(make_repl(), val)

    # Strip menus and scripts, then save
    for sel in REMOVE_SELECTORS:
        for el in content.select(sel):
            el.decompose()
    for h in content.find_all("h1"):  # the title is saved separately, so don't repeat it
        if h.get_text(strip=True) == title:
            h.decompose()

    html_path = os.path.join(pages_dir, slug + ".html")
    txt_path = os.path.join(pages_dir, slug + ".txt")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write('<!doctype html>\n<html><head><meta charset="utf-8"><title>%s</title></head>\n'
                "<body>\n<h1>%s</h1>\n%s\n</body></html>\n"
                % (htmllib.escape(title), htmllib.escape(title), content.decode_contents()))
    body_text = content.get_text("\n", strip=True)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(title + "\n\n" + body_text + "\n")
    if len(body_text) < 40:
        flags.append("Very little text saved. Check --selector")

    page_row = dict(
        url=url, title=title, html_file=html_path, text_file=txt_path,
        text_chars=len(body_text),
        media_count=len(media_rows),
        needs_review="YES" if flags else "",
        notes="; ".join(sorted(set(flags))),
    )
    return page_row, media_rows, soup


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# ----------------------------------------------------------------------------
# The table of contents (the only thing the crawl follows)
# ----------------------------------------------------------------------------

TOC_SELECTOR = "#block-book-navigation"   # the "Table of Contents" box in the left sidebar
ALIASES = {}    # a page's other address (like .../node/381) -> its main address (.../content/name)
TOC = {}        # url -> {"title": ..., "parent": url or None}
CHILDREN = {}   # parent url (None for top level) -> [child urls in menu order]


def parse_toc(html_text, page_url, selector):
    """Read the contents box on one page. Returns [(url, title, parent_url), ...] in menu order,
    or None if the page has no such box. Each page only shows part of the tree, so the
    pieces are merged as pages are visited."""
    box = BeautifulSoup(html_text, "html.parser").select_one(selector)
    if box is None:
        return None
    entries = []

    def walk(ul, parent):
        for li in ul.find_all("li", recursive=False):
            a = li.find("a", href=True, recursive=False)
            if a is None:  # link wrapped in another tag; take the first one not inside a sub-list
                a = next((c for c in li.find_all("a", href=True) if c.find_parent("ul") is ul), None)
            this = None
            if a is not None:
                this = normalize(urljoin(page_url, a["href"]))
                entries.append((this, a.get_text(" ", strip=True), parent))
            sub = li.find("ul", recursive=False)
            if sub is not None:
                walk(sub, this or parent)

    top = box.find("ul")
    if top is not None:
        walk(top, None)
    return entries


def merge_toc(entries):
    for url, title, parent in entries:
        if url not in TOC:
            TOC[url] = {"title": title, "parent": parent}
            CHILDREN.setdefault(parent, []).append(url)


def toc_rows(saved):
    """Flatten the merged tree into rows, in the same order as the manual."""
    rows = []

    def walk(parent, trail):
        for u in CHILDREN.get(parent, []):
            path = trail + [TOC[u]["title"]]
            rows.append(dict(order=len(rows) + 1, depth=len(path), path=" > ".join(path),
                             title=TOC[u]["title"], url=u, saved="yes" if u in saved else ""))
            walk(u, path)

    walk(None, [])
    return rows


def node_urls(html_text, page_url):
    """The page's number address(es), like https://host/node/381. Drupal pages are reachable by
    name (/content/...) and by number (/node/381), and authors often linked by number."""
    soup = BeautifulSoup(html_text, "html.parser")
    found = set()
    for link in soup.find_all("link", href=True):
        if "shortlink" in " ".join(link.get("rel") or []):
            found.add(normalize(urljoin(page_url, link["href"])))
    if not found:  # fall back to <div id="node-381" ...>
        for el in soup.find_all(id=re.compile(r"^node-\d+$")):
            found.add(normalize(urljoin(page_url, "/node/" + el["id"].split("-")[1])))
    return found


def fix_links(page_rows):
    """Second pass, after the pages are saved: make links between saved pages work.

    The saved pages still contain the site's own addresses ("/content/c-second-law"), which
    lead nowhere when a page is opened from your computer. A link to a page that was saved now
    points at that saved file. A link to anything not saved is made a full address on the old
    site, so it still works while that site is up. Returns (links to saved pages, links left
    pointing at the old site)."""
    url_to_file = {p["url"]: p["html_file"] for p in page_rows if p.get("html_file")}
    to_local = to_old = 0
    for p in page_rows:
        path = p.get("html_file")
        if not path or not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        changed = False
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            # skip in-page anchors, mail links, and links already pointing at downloaded files
            if not href or href.startswith(("#", "mailto:", "javascript:", "tel:", "../media/")):
                continue
            full = urljoin(p["url"], href)
            target, _, fragment = full.partition("#")
            norm = normalize(target)
            saved = url_to_file.get(norm) or url_to_file.get(ALIASES.get(norm, ""))
            if not saved:  # a number link written another way, e.g. "node/381" or "../node/381"
                m_node = re.search(r"(?:^|/)node/(\d+)/?$", urlparse(target).path)
                if m_node:
                    saved = url_to_file.get(ALIASES.get("https://%s/node/%s" % (HOST, m_node.group(1)), ""))
            if saved:
                new = os.path.relpath(saved, os.path.dirname(path)).replace(os.sep, "/")
                a["href"] = new + ("#" + fragment if fragment else "")
                to_local += 1
                changed = True
            elif href != full:
                a["href"] = full
                to_old += 1
                changed = True
        if changed:
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(soup))
    return to_local, to_old


def main():
    ap = argparse.ArgumentParser(description="Export demo pages and their media for migration.")
    ap.add_argument("--start", default=START_URL, help="page to start crawling from")
    ap.add_argument("--toc-selector", default=TOC_SELECTOR,
                    help="CSS selector of the Table of Contents box. Only links inside it are followed.")
    ap.add_argument("--prefix", default="/", help="only follow contents links under this path")
    ap.add_argument("--limit", type=int, default=5,
                    help="max pages to process (default 5 as a trial; 0 = everything)")
    ap.add_argument("--selector", default="",
                    help="CSS selector of the box holding the page text, e.g. 'div.content'. "
                         "Tried before the built-in guesses.")
    ap.add_argument("--out", default="demo_export", help="output folder")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds to wait between requests")
    ap.add_argument("--wayback-ts", default="2023",
                    help="Wayback timestamp or year used when a file is missing (default 2023)")
    args = ap.parse_args()
    USER_SELECTOR["value"] = args.selector

    pages_dir = os.path.join(args.out, "pages")
    media_dir = os.path.join(args.out, "media")
    os.makedirs(pages_dir, exist_ok=True)
    os.makedirs(media_dir, exist_ok=True)

    page_rows, media_rows = [], []
    queue = [normalize(args.start)]
    seen = set(queue)
    saved_urls = set()
    done = 0
    stopped_early = False
    link_counts = (0, 0)

    try:
        while queue:
            if args.limit and done >= args.limit:
                stopped_early = True
                break
            url = queue.pop()  # last in, first out: finish a section's pages before the next one
            try:
                r = session.get(url, timeout=30)
            except requests.RequestException as e:
                print("  could not load %s (%s)" % (url, e))
                page_rows.append(dict(url=url, title="", html_file="", text_file="", text_chars=0,
                                      media_count=0,
                                      needs_review="YES", notes="page failed to load"))
                continue
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                print("  skipped %s (status %s)" % (url, r.status_code))
                continue

            done += 1
            prow, mrows, soup = process_page(url, r.text, args, pages_dir, media_dir)
            page_rows.append(prow)
            media_rows.extend(mrows)
            print("[%d] %s  (%d media%s)  text from: %s, %d characters"
                  % (done, prow["title"], prow["media_count"],
                     ", NEEDS REVIEW" if prow["needs_review"] else "",
                     MATCHED["selector"], prow["text_chars"]))

            saved_urls.add(url)
            for alias in node_urls(r.text, url):
                ALIASES[alias] = url

            # Find more pages, ONLY from the Table of Contents box (not from the page body)
            entries = parse_toc(r.text, url, args.toc_selector)
            if entries is None:
                print("  warning: no Table of Contents box (%s) on this page; nothing followed from it"
                      % args.toc_selector)
                entries = []
            merge_toc(entries)
            # Depth first, in the manual's order. The queue is a stack, so the entries pushed
            # last are visited first: this page's own sub-pages go on top, then everything else.
            fresh = [(link, parent) for link, _title, parent in entries
                     if link not in seen and is_page_link(link, args.prefix)]
            sub_pages = [link for link, parent in fresh if parent == url]
            others = [link for link, parent in fresh if parent != url]
            for link in list(reversed(others)) + list(reversed(sub_pages)):
                if link not in seen:
                    seen.add(link)
                    queue.append(link)
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\nStopped by you. Saving what was collected so far...")
    finally:
        link_counts = fix_links(page_rows)
        for p in page_rows:
            t = TOC.get(p["url"])
            path, parent = [], t
            while parent is not None:  # build "1. Mechanics > B. First Law > 10. Bed of Nails"
                path.insert(0, parent["title"])
                parent = TOC.get(parent["parent"]) if parent["parent"] else None
            p["toc_path"] = " > ".join(path)
        write_csv(os.path.join(args.out, "toc.csv"), toc_rows(saved_urls),
                  ["order", "depth", "path", "title", "url", "saved"])
        write_csv(os.path.join(args.out, "pages.csv"), page_rows,
                  ["url", "title", "toc_path", "html_file", "text_file", "text_chars", "media_count",
                   "needs_review", "notes"])
        write_csv(os.path.join(args.out, "media.csv"), media_rows,
                  ["page_url", "kind", "source_url", "local_file", "status", "note"])

    review = sum(1 for p in page_rows if p["needs_review"])
    failed = sum(1 for m in media_rows if m["status"] == "FAILED")
    print("\nDone. %d pages saved, %d media entries, %d pages need review, %d files failed."
          % (done, len(media_rows), review, failed))
    print("Links between pages: %d now open the saved copy, %d still point at the old website "
          "(those pages were not saved in this run)." % link_counts)
    print("Open pages.csv, media.csv and toc.csv in the %s folder to see the details." % args.out)
    unvisited = [r for r in toc_rows(saved_urls) if not r["saved"]]
    if unvisited and not stopped_early:
        print("%d contents entries were not saved (external links or failed pages). See toc.csv."
              % len(unvisited))
    if stopped_early:
        print("\nThat was a trial run limited to %d pages, and at least %d more pages are waiting. "
              "Add  --limit 0  to process every page." % (args.limit, len(queue)))


if __name__ == "__main__":
    sys.exit(main())