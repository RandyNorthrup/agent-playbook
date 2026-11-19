Synthetic samples in the real response shapes, written by hand for the script tests. Nothing here was
fetched: every id, handle, company and number is made up.

- `li_search_page.html`, `li_job.html`, `li_wall.html`: LinkedIn guest jobs API (search cards, one job, a login wall).
- `x_timeline.html`, `x_tweet.json`, `x_tweet_reply.json`, `x_tombstone.json`: X syndication timeline and tweet-result.
- `bsky_*.json`: Bluesky public XRPC replies (getAuthorFeed, getFeed, getPostThread, resolveHandle, getProfile).
- `guard/sitecustomize.py`: loaded into script subprocesses by the tests. It records any network call in the
  test's marker file and blocks it, and can hide modules to exercise the "needs X" exit.
