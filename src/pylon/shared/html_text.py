"""Extract body evidence without losing paragraphs or nested elements."""
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit, parse_qs

MAX_BODY_CHARS = 32000


class BodyText(HTMLParser):
    def __init__(self, classes: set[str] | None = None):
        super().__init__(convert_charrefs=True)
        self.classes = classes
        self.stack: list[tuple[str, bool, bool, str]] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        active = self.classes is None or any(v[1] for v in self.stack) or bool(self.classes.intersection(values.get('class', '').split()))
        hidden = (tag in {'script', 'style', 'blockquote'}
                  or bool({'quote','blockquote','attach_nopermission'} & set(values.get('class','').split()))
                  or any(v[2] for v in self.stack))
        if active and not hidden and tag in {'br', 'p', 'div', 'li', 'tr'}:
            self.parts.append(' ' if tag!='tr' and any(v[0] in {'td','th'} for v in self.stack) else '\n')
        if tag not in {'br', 'hr', 'img', 'input', 'meta', 'link', 'wbr'}:
            href = values.get('href', '') if tag == 'a' and active and not hidden else ''
            try:
                parsed = urlsplit(href)
                if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password:
                    href = ''
                elif (re.search(r'\.(?:jpe?g|png|gif|webp|bmp|heic)$',parsed.path,re.I)
                      or parsed.path.endswith('/attachment.php')
                      or parse_qs(parsed.query).get('mod')==['attachment']):
                    # Forum image download controls are not transaction links.
                    href = ''
            except ValueError:
                href = ''
            self.stack.append((tag, active, hidden, href))

    def handle_endtag(self, tag):
        if self.stack and any(v[1] and not v[2] for v in self.stack) and tag in {'p', 'div', 'li', 'tr'}:
            self.parts.append(' ' if tag!='tr' and any(v[0] in {'td','th'} for v in self.stack) else '\n')
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                _, active, hidden, href = self.stack[i]
                if active and not hidden:
                    if tag in {'td','th'}:
                        self.parts.append(' | ')
                    if href and not ''.join(self.parts[-4:]).rstrip().endswith(href):
                        self.parts.append(' '+href)
                del self.stack[i:]
                break

    def handle_data(self, data):
        if (self.classes is None or any(v[1] for v in self.stack)) and not any(v[2] for v in self.stack):
            if any(v[0] in {'table','tr','td','th'} for v in self.stack):
                if not data.strip():
                    return
                data=re.sub(r'\s+',' ',data)
            self.parts.append(data)

    def text(self):
        text = ''.join(self.parts).replace('\xa0', ' ')
        text = re.sub(r'[^\S\n]+', ' ', text)
        return re.sub(r'\n[ \t]*\n+', '\n\n', text).strip()


def body_text(html: str, *, classes: set[str] | None = None) -> str:
    parser = BodyText(classes)
    parser.feed(html)
    return parser.text()
