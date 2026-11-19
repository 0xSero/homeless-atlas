# search-index — make the index cheap on a phone
Shards are now keyed by the first letter of every token, which fixed multi-word queries, but single shards
reach 2 MB (c, f, p, r) and a phone downloads a whole one per keystroke.
Split each shard into <letter>-<0..9> buckets by a hash of the title, add a per-bucket count to manifest.json,
and load only the buckets the client needs (or add a 2-level index: token -> bucket list).
accept: no file the client fetches exceeds 250 KB, "continuum of care" and "housing clinic" still return
        the same top hits, and search.html reports p95 query latency under 300 ms on a simulated 3G throttle.
