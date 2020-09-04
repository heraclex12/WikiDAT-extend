# WikiDAT-extend

This project includes:
+ [WikiExtractor](https://github.com/attardi/wikiextractor): clean Wikipedia content.
+ [WikiDAT](https://github.com/glimmerphoenix/WikiDAT): get entire revisions in Wikipedia history
+ [Google-Diff](https://github.com/google/diff-match-patch/tree/master/python3): get different between two revisions.

I combined 3 project and edited them for my goal. Some changes:
+ Dump data to Elasticsearch.
+ Get text instead of sha1 hash.
+ Clean unreadable text.
+ Get different between two revisions.
