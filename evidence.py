"""Expose links from actual successful tool results, never from model prose."""
import json
import re
from urllib.parse import urlsplit


def sources_for_turn(messages):
    start = next((i for i in range(len(messages) - 1, -1, -1) if messages[i]['role'] == 'user'), 0)
    sources = []
    seen = set()

    def walk(value):
        if isinstance(value, dict):
            url = value.get('url')
            if isinstance(url, str):
                try:
                    parsed = urlsplit(url)
                    allowed = (parsed.scheme == 'https' and parsed.netloc == 'github.com') or bool(
                        re.fullmatch(r'/knowledge/[0-9a-f-]{36}/chunks/[0-9a-f-]{36}', url)) or bool(
                        re.fullmatch(r'/memory/[0-9a-f-]{36}/(?:items/[0-9a-f-]{36}|turns/[1-9][0-9]*)', url))
                except ValueError:
                    allowed = False
                if allowed and url not in seen:
                    seen.add(url)
                    sources.append({'url': url, 'label': str(value.get('path') or value.get('title') or value.get('full_name') or url)[:160]})
            for key, child in value.items():
                if key not in {'content', 'body', 'readme', 'patch'}:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for message in messages[start:]:
        if message['role'] == 'tool':
            try:
                payload = json.loads(message['content'])
                if isinstance(payload, dict) and payload.get('ok') is True:
                    walk(payload)
            except (ValueError, TypeError):
                pass
    return sources[:30]
