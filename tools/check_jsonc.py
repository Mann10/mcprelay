"""Check that load_existing reports JSONC stripping correctly."""
import os

from mcprelay import agentconfig as ac

CASES = {
    "plain.json": '{"a": 1, "b": 2}',
    "commented.jsonc": '// leading comment\n{"a": 1, // trailing\n "b": 2}\n',
    "trailing_comma.json": '{"a": 1, "b": 2,}',
    "block_comment.jsonc": '/* block */\n{"a": 1}\n',
    "empty.json": "   ",
    "quoted_slash.json": '{"url": "http://x/y", "note": "a // b"}',
    "broken.json": "{ this is not json",
}

for name, body in CASES.items():
    path = os.path.join("var", name)
    os.makedirs("var", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    try:
        doc, report = ac.load_existing(path)
        print(f"{name:26} format={report['format']:5} stripped={str(report['stripped']):5} doc={doc}")
    except ValueError as exc:
        print(f"{name:26} REFUSED: {str(exc)[:70]}")
    os.remove(path)
