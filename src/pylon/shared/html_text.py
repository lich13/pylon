"""Extract body evidence without losing paragraphs or nested elements."""
from html.parser import HTMLParser
import re

MAX_BODY_CHARS = 32000


class BodyText(HTMLParser):
    def __init__(self, classes: set[str] | None = None):
        super().__init__(convert_charrefs=True)
        self.classes = classes
        self.stack: list[tuple[str, bool, bool]] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        active = self.classes is None or any(v[1] for v in self.stack) or bool(self.classes.intersection(values.get('class', '').split()))
        hidden = tag in {'script', 'style', 'blockquote'} or any(v[2] for v in self.stack)
        if active and not hidden and tag in {'br', 'p', 'div', 'li', 'tr'}:
            self.parts.append('\n')
        if tag not in {'br', 'hr', 'img', 'input', 'meta', 'link', 'wbr'}:
            self.stack.append((tag, active, hidden))

    def handle_endtag(self, tag):
        if self.stack and any(v[1] and not v[2] for v in self.stack) and tag in {'p', 'div', 'li', 'tr'}:
            self.parts.append('\n')
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if (self.classes is None or any(v[1] for v in self.stack)) and not any(v[2] for v in self.stack):
            self.parts.append(data)

    def text(self):
        text = ''.join(self.parts).replace('\xa0', ' ')
        text = re.sub(r'[^\S\n]+', ' ', text)
        return re.sub(r'\n[ \t]*\n+', '\n\n', text).strip()


def body_text(html: str, *, classes: set[str] | None = None) -> str:
    parser = BodyText(classes)
    parser.feed(html)
    return parser.text()
