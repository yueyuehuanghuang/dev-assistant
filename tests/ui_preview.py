"""Local UI QA with temporary, synthetic history; no model or GitHub requests."""
import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import app
from memory import ConversationStore
import uvicorn


if __name__ == '__main__':
    with TemporaryDirectory() as directory:
        app.state.conversations = ConversationStore(Path(directory) / 'preview.db')
        url = 'https://github.com/demo/project/issues/3'
        for i in range(3):
            messages = [{'role': 'user', 'content': f'Demo issue analysis {i + 1}'},
                {'role': 'tool', 'content': json.dumps({'ok': True, 'issue': {'url': url, 'title': 'Startup crash'}})},
                {'role': 'assistant', 'content': '# Issue analysis\nThe application has a **startup crash**.\n- Read `main.py`\n- Add a regression test\n```python\ndef main():\n    return 42\n```\n[Issue source](' + url + ')\n<script>alert("unsafe")</script>'}]
            app.state.conversations.repository.save(uuid4(), messages)
        uvicorn.run(app, host='127.0.0.1', port=8767)
