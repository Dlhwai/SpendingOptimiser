"""Run with uv run --locked python examples/gmail_synthetic.py. Offline only."""

import base64
from urllib.parse import parse_qs, urlsplit

from spending_optimiser.gmail import GmailAdapter, GmailRestClient


class SyntheticTokens:
    def access_token(self):
        return "synthetic-not-a-real-token"


class SyntheticTransport:
    """The actual REST client executes against synthetic Gmail API responses."""

    def get_json(self, url, headers):
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        if parsed.path.endswith("/messages"):
            if "pageToken" not in params:
                return {"messages": [{"id": "synthetic1"}], "nextPageToken": "page2"}
            # Repeated IDs are normal when a mailbox changes during pagination.
            return {"messages": [{"id": "synthetic1"}, {"id": "synthetic2"}]}
        mid = parsed.path.rsplit("/", 1)[1]
        body = b"Synthetic notice only. No financial fields have been extracted."
        return {
            "id": mid,
            "threadId": "syntheticThread",
            "internalDate": "1738540800000",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "Synthetic <nobody@example.invalid>"},
                    {"name": "Content-Type", "value": "text/plain; charset=utf-8"},
                ],
                "body": {
                    "size": len(body),
                    "data": base64.urlsafe_b64encode(body).decode(),
                },
            },
        }


def main() -> int:
    adapter = GmailAdapter(
        GmailRestClient(SyntheticTokens(), SyntheticTransport()), namespace="synthetic-mailbox"
    )
    seen_ids = set()
    seen_tokens = set()
    token = None
    pages = candidates = duplicates = 0
    while True:
        page = adapter.fetch_page(query="subject:synthetic", page_token=token, seen_ids=seen_ids)
        pages += 1
        duplicates += page.duplicate_count
        if not page.successful:
            # A real caller must retain successes and retry failures before advancing.
            print("status: failed\nnext: retry current page; do not advance continuation")
            return 1
        for result in page.results:
            seen_ids.add(result.message_id)
            candidates += 1
            # Hand result.candidate to a private extractor, not public card research.
        if page.exhausted:
            break
        token = page.next_page_token
        if token in seen_tokens:
            print("status: failed\nerror: pagination_cycle\nnext: restart scan safely")
            return 1
        seen_tokens.add(token)
    print(
        f"status: synthetic_complete\npages: {pages}\ncandidates: {candidates}\n"
        f"duplicates: {duplicates}\naccepted_transactions: 0\n"
        "next: authorized OAuth and verified bank samples remain separate work"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
